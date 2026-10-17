#!/usr/bin/env python3
"""StreamPulse rollout story — the 7-step "Player v2" narrative, driven against the platform API.

Every step talks to the dashboard API with bearer auth and prints what to look at in the
platform dashboard and in the StreamPulse app afterwards.  Without ``--token`` the story signs
in first with ``POST /api/v1/auth/login`` as ``--email``/``--password``, which default to the
seeded demo admin (``admin@demo.com`` / ``Demo1234!``), so it works against the local demo
backend as ``setup-local.sh`` starts it.  ``--token`` uses a bearer token you already have.

  1  Internal + 5%      show the flag at 5% with the ``employee equals true`` rule
  2  25%                advance the rollout schedule to stage 2 → flag at 25%
  3  Incident           run traffic.py --incident android12 for 90 s, then the safety check;
                        while the check is still healthy, run it again (at most 5 rounds)
  4  Rollback           wait for the safety scheduler to roll back to 5% (or roll back by hand
                        when automatic rollbacks are off); the rollback pauses the schedule
  5  Fix shipped        add ``app_version semver_gte 3.2.1 → 100%`` next to the employee rule
  6  50%                resume the paused schedule and advance to stage 3 (after the
                        15-minute error window has cleared)
  7  100%               advance to stage 4, complete the schedule, remove the rules

Steps are idempotent: re-running a step that already happened is a no-op that prints the
current state.  ``--step N`` runs one step, ``--auto`` runs 1→7 with ``--pace`` seconds in
between.

Usage:
  python demo/streampulse/simulator/rollout_story.py --step 1
  python demo/streampulse/simulator/rollout_story.py --auto --pace 20
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from traffic import DEFAULT_API_KEY_FILE, PLAYER_FLAG, read_api_key_file  # noqa: E402

DEFAULT_API_URL = "http://localhost:8000"
# The seeded demo admin (backend/scripts/seed_demo_data.py); used to sign in when no --token is given.
DEFAULT_EMAIL = "admin@demo.com"
DEFAULT_PASSWORD = "Demo1234!"
DEFAULT_DASHBOARD_URL = "http://localhost:3100"
DEFAULT_APP_URL = "http://localhost:3300"
TRAFFIC_SCRIPT = Path(__file__).resolve().parent / "traffic.py"

INCIDENT_RATE = 6
INCIDENT_DURATION_S = 90
# The steady traffic setup-local.sh runs beside the story dilutes the crashes, so one round
# may leave the flag healthy: step 3 runs the incident again, up to this many rounds in all.
INCIDENT_ROUNDS = 5
ROLLBACK_WAIT_S = 6 * 60  # the safety scheduler runs every SAFETY_CHECK_INTERVAL_MINUTES (1 in the demo)
POLL_INTERVAL_S = 10
SAFETY_WINDOW_MIN = 15  # error_rate is computed over the last 15 minutes of evaluations
HEALTHY_WAIT_S = (SAFETY_WINDOW_MIN + 1) * 60
FIX_APP_VERSION = "3.2.1"
STEP_TITLES = {
    1: "Internal + 5% — Player v2 is live for employees and 5% of everyone",
    2: "Looks good — advance the schedule to 25%",
    3: "Crash rate spikes on Android 12 — the incident",
    4: "Safety monitoring rolls Player v2 back to 5%",
    5: f"Fix shipped in {FIX_APP_VERSION} — target the fixed build",
    6: "Resume the paused rollout — 50%",
    7: "Full rollout — 100%, schedule complete, rules removed",
}


class ApiError(Exception):
    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


class StoryError(Exception):
    """A step's precondition is not met (message is user facing)."""


# --------------------------------------------------------------------------------------
# Dashboard API client (bearer auth)
# --------------------------------------------------------------------------------------


def sign_in(api_url: str, email: str, password: str, timeout: float = 15.0) -> str:
    """Return a bearer token from ``POST /api/v1/auth/login`` (the local auth provider).

    Raises ``ApiError`` on any failure: status 0 when the API is unreachable, otherwise the
    HTTP status (401 wrong credentials, 404 the backend is not on the local provider,
    423 locked, 429 rate limited).
    """
    url = f"{api_url.rstrip('/')}/api/v1/auth/login"
    req = urllib.request.Request(
        url,
        data=json.dumps({"email": email, "password": password}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode("utf-8", "replace"), url) from None
    except urllib.error.URLError as e:
        raise ApiError(0, str(e.reason), url) from None
    except (TimeoutError, OSError) as e:
        raise ApiError(0, str(e), url) from None
    try:
        token = json.loads(raw).get("access_token")
    except (ValueError, AttributeError):
        token = None
    if not isinstance(token, str) or not token:
        raise ApiError(502, "the login response carried no access_token", url)
    return token


class StoryClient:
    def __init__(self, api_url: str = DEFAULT_API_URL, token: str = "", timeout: float = 15.0):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_url}/api/v1{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace"), url) from None
        except urllib.error.URLError as e:
            raise ApiError(0, str(e.reason), url) from None
        except (TimeoutError, OSError) as e:
            raise ApiError(0, str(e), url) from None
        if not raw:
            return None
        return json.loads(raw)

    # --- flags ---
    def flag_by_key(self, key: str) -> dict[str, Any]:
        page = self.request("GET", "/feature-flags/", params={"search": key, "limit": 100}) or {}
        for item in page.get("items", []):
            if item.get("key") == key:
                return item
        raise StoryError(f"feature flag '{key}' not found — run backend/scripts/seed_streampulse.py first")

    def get_flag(self, flag_id: str) -> dict[str, Any]:
        return self.request("GET", f"/feature-flags/{flag_id}") or {}

    def update_flag(self, flag_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", f"/feature-flags/{flag_id}", body) or {}

    # --- rollout schedules ---
    def schedule_for_flag(self, flag_id: str) -> dict[str, Any]:
        page = self.request("GET", "/rollout-schedules/", params={"feature_flag_id": flag_id, "limit": 100}) or {}
        items = page.get("items", [])
        if not items:
            raise StoryError(f"no rollout schedule for flag {flag_id} — re-run the seed")
        active = [s for s in items if str(s.get("status", "")).lower() == "active"]
        return (active or items)[0]

    def update_stage(self, stage_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", f"/rollout-schedules/stages/{stage_id}", body) or {}

    def advance_stage(self, stage_id: str) -> dict[str, Any]:
        return self.request("POST", f"/rollout-schedules/stages/{stage_id}/advance") or {}

    def activate_schedule(self, schedule_id: str) -> dict[str, Any]:
        """Resume a paused schedule (a safety rollback pauses it)."""
        return self.request("POST", f"/rollout-schedules/{schedule_id}/activate") or {}

    # --- safety ---
    def safety_settings(self) -> dict[str, Any]:
        return self.request("GET", "/safety/settings") or {}

    def enable_automatic_rollbacks(self) -> dict[str, Any]:
        return self.request("POST", "/safety/settings", {"enable_automatic_rollbacks": True}) or {}

    def safety_config(self, flag_id: str) -> dict[str, Any]:
        return self.request("GET", f"/safety/feature-flags/{flag_id}/config") or {}

    def safety_check(self, flag_id: str) -> dict[str, Any]:
        return self.request("GET", f"/safety/feature-flags/{flag_id}/check") or {}

    def rollback(self, flag_id: str, percentage: int, reason: str) -> dict[str, Any]:
        return self.request("POST", f"/safety/feature-flags/{flag_id}/rollback", params={"percentage": percentage, "reason": reason}) or {}


# --------------------------------------------------------------------------------------
# Story context + helpers
# --------------------------------------------------------------------------------------


@dataclass
class Story:
    client: Any
    out: TextIO = sys.stdout
    api_key: str | None = None
    api_url: str = DEFAULT_API_URL
    dashboard_url: str = DEFAULT_DASHBOARD_URL
    app_url: str = DEFAULT_APP_URL
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    run_traffic: Callable[[list[str]], int] | None = None
    incident_rate: float = INCIDENT_RATE
    incident_duration: float = INCIDENT_DURATION_S
    incident_rounds: int = INCIDENT_ROUNDS
    rollback_wait: float = ROLLBACK_WAIT_S
    healthy_wait: float = HEALTHY_WAIT_S
    poll_interval: float = POLL_INTERVAL_S
    _flag_id: str | None = field(default=None, init=False)

    # --- output ---
    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def header(self, step: int) -> None:
        self.say("")
        self.say("=" * 78)
        self.say(f"  Step {step}/7 — {STEP_TITLES[step]}")
        self.say("=" * 78)

    def look_at(self, *lines: str) -> None:
        self.say("  Look at:")
        for line in lines:
            self.say(f"    • {line}")

    # --- state ---
    @property
    def flag_id(self) -> str:
        if self._flag_id is None:
            self._flag_id = str(self.client.flag_by_key(PLAYER_FLAG)["id"])
        return self._flag_id

    def flag(self) -> dict[str, Any]:
        return self.client.get_flag(self.flag_id)

    def flag_rules(self, flag: dict[str, Any] | None = None) -> dict[str, Any]:
        flag = flag or self.flag()
        rules = flag.get("targeting_rules")
        return rules if isinstance(rules, dict) else {}

    def flag_page(self) -> str:
        return f"{self.dashboard_url}/feature-flags/{self.flag_id}"

    def print_flag(self, flag: dict[str, Any] | None = None) -> dict[str, Any]:
        flag = flag or self.flag()
        self.say(f"  Flag {PLAYER_FLAG}: rollout {flag.get('rollout_percentage')}%  status {flag.get('status')}")
        rules = self.flag_rules(flag)
        if rules.get("groups"):
            self.say(f"  Targeting ({rules.get('logical_operator', 'AND')} across groups) → 100% when matched:")
            for group in rules["groups"]:
                conds = " AND ".join(describe_condition(c) for c in group.get("conditions", []))
                self.say(f"    - {conds}")
        else:
            self.say("  Targeting: none (global rollout % only)")
        return flag

    def print_schedule(self, schedule: dict[str, Any]) -> None:
        self.say(f"  Schedule '{schedule.get('name')}' ({schedule.get('status')}):")
        for stage in sorted_stages(schedule):
            marker = {"in_progress": "▶", "completed": "✔", "pending": "·"}.get(stage_status(stage), "?")
            self.say(
                f"    {marker} stage {stage.get('stage_order')}  {stage.get('name'):<14} {stage.get('target_percentage'):>3}%  "
                f"{stage_status(stage):<11} trigger={str(stage.get('trigger_type', '')).lower()}"
            )

    def print_safety(self) -> dict[str, Any]:
        check = self.client.safety_check(self.flag_id)
        state = "HEALTHY" if check.get("is_healthy") else "CRITICAL"
        self.say(f"  Safety check: {state}")
        for metric in check.get("metrics", []):
            details = metric.get("details") or {}
            self.say(
                f"    {metric.get('name')}: {100 * float(metric.get('current_value') or 0):.1f}%  "
                f"(warning {100 * float(details.get('warning_threshold') or 0):.0f}%, critical {100 * float(metric.get('threshold') or 0):.0f}%)"
            )
        return check

    # --- schedule driving ---
    def ensure_manual(self, stage: dict[str, Any]) -> None:
        if str(stage.get("trigger_type", "")).lower() != "manual":
            self.client.update_stage(str(stage["id"]), {"trigger_type": "manual"})
            self.say(
                f"  (stage {stage.get('stage_order')} '{stage.get('name')}' switched from a time-based to a manual trigger "
                "so we can advance it by hand; in production its start date would fire it)"
            )

    def advance_schedule_to(self, target_order: int) -> dict[str, Any]:
        """Drive the flag's schedule until stage ``target_order`` is in progress (idempotent)."""
        for _ in range(10):
            schedule = self.client.schedule_for_flag(self.flag_id)
            stages = sorted_stages(schedule)
            target = next((s for s in stages if int(s.get("stage_order", 0)) == target_order), None)
            if target is None:
                raise StoryError(f"schedule has no stage {target_order}")
            if stage_status(target) in ("in_progress", "completed"):
                return schedule
            current = next((s for s in stages if stage_status(s) == "in_progress"), None)
            if current is None:
                pending = next((s for s in stages if stage_status(s) == "pending"), None)
                if pending is None:
                    raise StoryError("no pending stage left to activate")
                self.ensure_manual(pending)
                self.client.advance_stage(str(pending["id"]))
                self.say(f"  Activated stage {pending.get('stage_order')} '{pending.get('name')}' → {pending.get('target_percentage')}%")
            else:
                self.ensure_manual(current)
                self.client.advance_stage(str(current["id"]))
                self.say(f"  Completed stage {current.get('stage_order')} '{current.get('name')}'; the next pending stage is now in progress")
        raise StoryError(f"could not reach stage {target_order}")

    def complete_stage(self, order: int) -> dict[str, Any]:
        schedule = self.client.schedule_for_flag(self.flag_id)
        stage = next((s for s in sorted_stages(schedule) if int(s.get("stage_order", 0)) == order), None)
        if stage is not None and stage_status(stage) == "in_progress":
            self.ensure_manual(stage)
            self.client.advance_stage(str(stage["id"]))
            self.say(f"  Completed stage {order} '{stage.get('name')}' — the schedule is finished")
        return self.client.schedule_for_flag(self.flag_id)

    def wait_for_rollout(self, target: int, timeout: float) -> bool:
        deadline = self.clock() + timeout
        while True:
            flag = self.flag()
            pct = int(flag.get("rollout_percentage") or 0)
            if pct == target:
                return True
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            self.say(f"    … rollout still {pct}% (waiting up to {int(remaining)}s more)")
            self.sleep(min(self.poll_interval, max(remaining, 0)))

    def wait_for_healthy(self, timeout: float) -> bool:
        deadline = self.clock() + timeout
        while True:
            check = self.client.safety_check(self.flag_id)
            if check.get("is_healthy"):
                return True
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            rate = next((float(m.get("current_value") or 0) for m in check.get("metrics", []) if m.get("name") == "error_rate"), 0.0)
            self.say(f"    … error rate still {100 * rate:.1f}% (critical 5%); the {SAFETY_WINDOW_MIN}-minute window clears in at most {int(remaining)}s")
            self.sleep(min(30.0, max(remaining, 0)))

    # --- the incident subprocess ---
    def traffic_args(self) -> list[str]:
        args = [
            "--incident", "android12",
            "--rate", f"{self.incident_rate:g}",
            "--duration", f"{self.incident_duration:g}",
            "--api-url", self.api_url,
            "--no-probe",
        ]
        if self.api_key:
            args += ["--api-key", self.api_key]
        return args

    def run_incident_traffic(self) -> int:
        if self.run_traffic is not None:
            return self.run_traffic(self.traffic_args())
        cmd = [sys.executable, str(TRAFFIC_SCRIPT), *self.traffic_args()]
        self.say("  $ " + " ".join(shlex_quote(a) for a in cmd))
        self.out.flush()
        return subprocess.call(cmd)  # inherits stdout so the 10-second stats stream through

    # ------------------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------------------

    def step_1(self) -> bool:
        self.header(1)
        self.say("  We are launching a redesigned player. Employees always get it (targeting rule);")
        self.say("  everyone else follows the staged rollout, which started two days ago at 5%.")
        self.say("")
        self.print_flag()
        self.print_schedule(self.client.schedule_for_flag(self.flag_id))
        config = self.client.safety_config(self.flag_id)
        settings = self.client.safety_settings()
        metrics = config.get("metrics") or {}
        error = metrics.get("error_rate") or {}
        self.say(
            f"  Safety monitoring: {'enabled' if config.get('enabled') else 'disabled'} — error_rate warning "
            f"{100 * float(error.get('warning_threshold') or 0):.0f}% / critical {100 * float(error.get('critical_threshold') or 0):.0f}%, "
            f"rollback to {config.get('rollback_percentage')}%; automatic rollbacks globally "
            f"{'ON' if settings.get('enable_automatic_rollbacks') else 'OFF'}"
        )
        self.print_safety()
        self.look_at(
            f"dashboard flag page: {self.flag_page()} (rollout 5%, targeting rule 'employee equals true', schedule stage 1 in progress)",
            f"app {self.app_url}: pick 'Internal tester' → Player tab shows Player v2 ON with reason targeting_rule",
            "app: pick 'iPhone 15 · iOS 17.4 · US · premium' → Player v2 off (reason rollout, only 5% are in)",
        )
        return True

    def step_2(self) -> bool:
        self.header(2)
        self.say("  Two days at 5% with a healthy error rate — time to widen the rollout to a quarter of devices.")
        before = int(self.flag().get("rollout_percentage") or 0)
        schedule = self.advance_schedule_to(2)
        flag = self.flag()
        after = int(flag.get("rollout_percentage") or 0)
        if before == after:
            self.say(f"  Already at stage 2: rollout is {after}% (nothing to do).")
        else:
            self.say(f"  Rollout {before}% → {after}%")
        self.print_schedule(schedule)
        self.look_at(
            f"dashboard flag page: {self.flag_page()} (rollout 25%, stage 2 '25%' in progress)",
            f"app {self.app_url}: 'Galaxy S10 · Android 12 · US · premium · app 3.1.0' preset may now be in (bucketed by device id)",
            "simulator: python demo/streampulse/simulator/traffic.py --rate 6 → player_v2 on-rate ≈ 25% + employees",
        )
        return True

    def step_3(self) -> bool:
        self.header(3)
        pct = int(self.flag().get("rollout_percentage") or 0)
        self.say(f"  Player v2 is at {pct}%. A broken build crashes Player v2 on Android 12 devices — 40% of the Android 12")
        self.say(f"  devices that have the flag ON report a crash and relaunch (crash loop). Running {self.incident_duration:g}s of incident traffic:")
        self.say("  The simulator reports the crashes the way the StreamPulse server would forward them, with its server-side key.")
        if pct < 25:
            self.say("  (note: below 25% the crash share is too small to cross the 5% critical threshold — run step 2 first)")
        check: dict[str, Any] = {}
        for round_ in range(1, self.incident_rounds + 1):
            rc = self.run_incident_traffic()
            if rc != 0:
                raise StoryError(
                    f"traffic.py exited with code {rc} (2 = bad API key, or no server key in demo/streampulse/.api_key_server: "
                    "run backend/scripts/seed_streampulse.py; 3 = not seeded, 4 = API unreachable)"
                )
            self.say("")
            check = self.print_safety()
            if not check.get("is_healthy"):
                break
            if round_ < self.incident_rounds:
                self.say(
                    f"  Still healthy: the steady traffic dilutes the crashes. Running the incident again "
                    f"(round {round_ + 1} of at most {self.incident_rounds}):"
                )
        if check.get("is_healthy"):
            self.say(f"  The check is still healthy after {self.incident_rounds} rounds; re-run step 3.")
        else:
            self.say("  error_rate is above the 5% critical threshold — the flag is now CRITICAL.")
        self.look_at(
            f"dashboard safety page: {self.dashboard_url}/admin/safety (Player v2 error rate in the red)",
            f"dashboard flag page: {self.flag_page()}",
            f"app {self.app_url}: 'Galaxy S10 · Android 12' preset → Player tab (this is the population that crashes)",
        )
        return True

    def step_4(self) -> bool:
        self.header(4)
        config = self.client.safety_config(self.flag_id)
        target = int(config.get("rollback_percentage") or 0)
        flag = self.flag()
        pct = int(flag.get("rollout_percentage") or 0)
        if pct == target:
            self.say(f"  Player v2 is already rolled back to {target}% (nothing to do).")
            self.print_safety()
            self.look_at(f"dashboard flag page: {self.flag_page()} (rollout {target}%, rollout schedule paused)")
            return True
        check = self.print_safety()
        if check.get("is_healthy"):
            raise StoryError("the safety check is healthy, so there is nothing to roll back — run step 3 first (and step 2 before it)")
        settings = self.client.safety_settings()
        rolled = False
        if settings.get("enable_automatic_rollbacks"):
            self.say(f"  Automatic rollbacks are ON: waiting for the safety scheduler to roll the flag back to {target}% "
                     f"(it runs every SAFETY_CHECK_INTERVAL_MINUTES; up to {int(self.rollback_wait)}s)…")
            rolled = self.wait_for_rollout(target, self.rollback_wait)
            if not rolled:
                self.say("  The scheduler did not act in time (is the backend running with SAFETY_CHECK_INTERVAL_MINUTES=1?); rolling back by hand.")
        else:
            self.say("  Automatic rollbacks are OFF in the global safety settings: rolling back by hand.")
        if not rolled:
            result = self.client.rollback(self.flag_id, target, "Player v2 crash rate on Android 12 above 5% (StreamPulse rollout story)")
            self.say(f"  {result.get('message') or result}")
        flag = self.flag()
        self.say(f"  Rollout {pct}% → {flag.get('rollout_percentage')}%  (employees keep Player v2 through the targeting rule)")
        self.say("  The rollback also paused the rollout schedule, so no stage can raise the rollout again; step 6 resumes it.")
        self.look_at(
            f"dashboard safety page: {self.dashboard_url}/admin/safety (a rollback made from that page is listed there; "
            "an automatic one is not listed yet)",
            f"dashboard flag page: {self.flag_page()} (rollout {target}%, rollout schedule paused)",
            f"app {self.app_url}: 'Galaxy S10 · Android 12' preset → Player v2 off again; 'Internal tester' still ON (targeting_rule)",
        )
        return True

    def step_5(self) -> bool:
        self.header(5)
        flag = self.flag()
        rules = self.flag_rules(flag)
        groups = list(rules.get("groups") or [])
        if any(has_fix_condition(g) for g in groups):
            self.say(f"  The fixed-build rule (app_version semver_gte {FIX_APP_VERSION}) is already there (nothing to do).")
        else:
            employee_groups = [g for g in groups if any(c.get("attribute") == "employee" for c in g.get("conditions", []))]
            if not employee_groups:
                employee_groups = [
                    {"id": "grp-employees", "logical_operator": "AND",
                     "conditions": [{"id": "cond-employee", "attribute": "employee", "operator": "equals", "value": True}]}
                ]
            new_rules = {
                "logical_operator": "OR",
                "groups": employee_groups + [
                    {
                        "id": "grp-fixed-build",
                        "logical_operator": "AND",
                        "conditions": [
                            {"id": "cond-app-version", "attribute": "app_version", "operator": "semver_gte", "value": FIX_APP_VERSION}
                        ],
                    }
                ],
            }
            self.client.update_flag(self.flag_id, {"targeting_rules": new_rules})
            self.say(f"  Added rule: app_version semver_gte {FIX_APP_VERSION} → 100% (kept: employee equals true)")
        self.say(f"  The crash is fixed in {FIX_APP_VERSION}. Devices on the fixed build get Player v2 in full; everyone else stays on the global %.")
        self.print_flag()
        self.look_at(
            f"dashboard flag page: {self.flag_page()} (targeting editor shows two groups joined with OR)",
            f"app {self.app_url}: 'Internal tester' (app 3.2.1) → ON (targeting_rule); 'Galaxy S10 … app 3.1.0' → off (rollout)",
            "simulator: player_v2 on-rate ≈ 20% (the 3.2.1 share) + 5% + employees",
        )
        return True

    def step_6(self) -> bool:
        self.header(6)
        schedule = self.client.schedule_for_flag(self.flag_id)
        stage3 = next((s for s in sorted_stages(schedule) if int(s.get("stage_order", 0)) == 3), None)
        if stage3 is not None and stage_status(stage3) in ("in_progress", "completed"):
            self.say(f"  Already at stage 3: rollout is {self.flag().get('rollout_percentage')}% (nothing to do).")
            self.print_schedule(schedule)
            return True
        self.say(f"  Before widening the rollout the flag must be healthy again: error_rate is computed over the last {SAFETY_WINDOW_MIN}")
        self.say("  minutes, and the safety scheduler would roll a critical flag straight back to 5%.")
        if not self.wait_for_healthy(self.healthy_wait):
            self.say("  WARNING: still critical after waiting — advancing anyway; expect the scheduler to roll back again.")
        else:
            self.say("  Safety check: HEALTHY")
        if str(schedule.get("status", "")).lower() == "paused":
            self.client.activate_schedule(str(schedule["id"]))
            self.say("  Resumed the rollout schedule (the rollback in step 4 paused it).")
        before = int(self.flag().get("rollout_percentage") or 0)
        schedule = self.advance_schedule_to(3)
        after = int(self.flag().get("rollout_percentage") or 0)
        self.say(f"  Rollout {before}% → {after}%")
        self.print_schedule(schedule)
        self.look_at(
            f"dashboard flag page: {self.flag_page()} (rollout 50%, stage 3 in progress)",
            "simulator: player_v2 on-rate ≈ 50% (+ fixed-build and employee devices at 100%)",
        )
        return True

    def step_7(self) -> bool:
        self.header(7)
        schedule = self.advance_schedule_to(4)
        flag = self.flag()
        self.say(f"  Rollout is now {flag.get('rollout_percentage')}%")
        schedule = self.complete_stage(4)
        if self.flag_rules(flag).get("groups"):
            self.client.update_flag(self.flag_id, {"targeting_rules": {}})
            self.say("  Removed the targeting rules — at 100% they are no longer needed (lifecycle cleanup).")
        else:
            self.say("  Targeting rules already removed.")
        self.print_flag()
        self.print_schedule(schedule)
        self.look_at(
            f"dashboard flag page: {self.flag_page()} (rollout 100%, no rules, schedule completed)",
            f"app {self.app_url}: every preset → Player v2 ON (reason rollout)",
            f"dashboard audit trail: {self.dashboard_url}/admin/audit (every flag update from this story is logged)",
        )
        return True

    def run_step(self, step: int) -> bool:
        return {1: self.step_1, 2: self.step_2, 3: self.step_3, 4: self.step_4, 5: self.step_5, 6: self.step_6, 7: self.step_7}[step]()


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def sorted_stages(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(schedule.get("stages") or [], key=lambda s: int(s.get("stage_order", 0)))


def stage_status(stage: dict[str, Any]) -> str:
    return str(stage.get("status", "")).lower()


def describe_condition(condition: dict[str, Any]) -> str:
    return f"{condition.get('attribute')} {condition.get('operator')} {json.dumps(condition.get('value'))}"


def has_fix_condition(group: dict[str, Any]) -> bool:
    return any(
        c.get("attribute") == "app_version" and c.get("operator") == "semver_gte" and str(c.get("value")) == FIX_APP_VERSION
        for c in group.get("conditions", [])
    )


def shlex_quote(value: str) -> str:
    return value if all(ch.isalnum() or ch in "-_./:=" for ch in value) else json.dumps(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rollout_story.py",
        description="Drive the StreamPulse 'Player v2' rollout story against the Controlvar API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--token", default=None, help="bearer token for the dashboard API (default: sign in with --email/--password)")
    parser.add_argument("--email", default=DEFAULT_EMAIL, help="user to sign in as when --token is not given")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="password for --email")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Controlvar API origin")
    parser.add_argument("--api-key", default=None, help=f"X-API-Key for the incident traffic (default: contents of {DEFAULT_API_KEY_FILE})")
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL, help="platform dashboard origin (for the 'look at' hints)")
    parser.add_argument("--app-url", default=DEFAULT_APP_URL, help="StreamPulse app origin (for the 'look at' hints)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--step", type=int, choices=range(1, 8), metavar="N", help="run one step (1-7)")
    mode.add_argument("--auto", action="store_true", help="run all seven steps in order")
    parser.add_argument("--pace", type=float, default=15.0, help="seconds to pause between steps with --auto")
    parser.add_argument("--incident-rate", type=float, default=INCIDENT_RATE, help="sessions per second for the step-3 incident traffic")
    parser.add_argument("--incident-duration", type=float, default=INCIDENT_DURATION_S, help="seconds of incident traffic in step 3")
    parser.add_argument("--rollback-wait", type=float, default=ROLLBACK_WAIT_S, help="seconds step 4 waits for the safety scheduler before rolling back by hand")
    parser.add_argument("--no-wait", action="store_true", help="step 6: do not wait for the error window to clear")
    return parser


def main(
    argv: list[str] | None = None,
    client_factory: Callable[..., Any] = StoryClient,
    sleep: Callable[[float], None] = time.sleep,
    run_traffic: Callable[[list[str]], int] | None = None,
    out: TextIO = sys.stdout,
    login: Callable[[str, str, str], str] = sign_in,
) -> int:
    args = build_parser().parse_args(argv)
    token = args.token
    if not token:
        try:
            token = login(args.api_url, args.email, args.password)
        except ApiError as err:
            if err.status == 0:
                print(f"\nerror: cannot reach {args.api_url}: {err.body}. Is the backend running?", file=sys.stderr)
                return 4
            print(
                f"\nerror: could not sign in to {args.api_url} as {args.email} ({err.status}: {err.body[:200]}). "
                "Pass --email/--password for an existing user, or --token with a bearer token.",
                file=sys.stderr,
            )
            return 2
    client = client_factory(args.api_url, token)
    story = Story(
        client=client,
        out=out,
        api_key=args.api_key or read_api_key_file(DEFAULT_API_KEY_FILE),
        api_url=args.api_url,
        dashboard_url=args.dashboard_url,
        app_url=args.app_url,
        sleep=sleep,
        run_traffic=run_traffic,
        incident_rate=args.incident_rate,
        incident_duration=args.incident_duration,
        rollback_wait=args.rollback_wait,
        healthy_wait=0.0 if args.no_wait else HEALTHY_WAIT_S,
    )
    steps = list(range(1, 8)) if args.auto else [args.step]
    try:
        for i, step in enumerate(steps):
            story.run_step(step)
            if args.auto and i < len(steps) - 1 and args.pace > 0:
                print(f"\n  (next step in {args.pace:g}s)", file=out)
                sleep(args.pace)
    except StoryError as err:
        print(f"\nerror: {err}", file=sys.stderr)
        return 1
    except ApiError as err:
        if err.status in (401, 403):
            print(
                f"\nerror: the dashboard API rejected the bearer token ({err.status}). "
                "Pass --token with a valid token, or --email/--password for a user who may change flags.",
                file=sys.stderr,
            )
            return 2
        if err.status == 0:
            print(f"\nerror: cannot reach {args.api_url}: {err.body}. Is the backend running?", file=sys.stderr)
            return 4
        print(f"\nerror: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
