#!/usr/bin/env python3
"""StreamPulse device simulator.

Pretends to be thousands of mobile devices opening the StreamPulse app, using only the
public Controlvar API and the app's API key — no database access, stdlib only.

Per tick (one app session on one device):
  1. GET  /api/v1/feature-flags/evaluate/{key}?user_id=…&context=<device attributes>  (4 flags)
  2. POST /api/v1/tracking/assign  {experiment_key, user_id, context: <device attributes>}  (5 experiments)
     — ``assigned: false`` (global holdout / mutual exclusion / targeting) means no events
       are sent for that experiment
  3. Walk the screens with the true rates from the spec (§2) and the event names from §3
  4. POST /api/v1/tracking/batch (≤100 events per call) with an explicit experiment_key or
     feature_flag_key on every event
  5. ``--incident android12``: a broken Player v2 build on Android 12.  40% of Android 12
     devices that have ``streampulse_player_v2`` ON crash when the player opens, and the crash
     is reported with POST /api/v1/tracking/errors (``error_type: crash``).  The reports are
     sent with the StreamPulse server key (``--server-api-key``, default
     ``demo/streampulse/.api_key_server``, scope ``sdk:ruleset``), the way the StreamPulse
     server would forward its devices' crash reports: only reports sent with a server-side
     key count toward automatic rollback.  Every incident session is an Android 12 device's,
     and a crashed app gets relaunched by its user four times (crash loop: each relaunch
     re-evaluates the flag and crashes again).  The safety monitor's error rate is crash
     reports ÷ every evaluation of the flag over 15 minutes, the steady traffic
     setup-local.sh runs beside the incident included; at 25% the incident's reports come to
     well over 5% of its own and as much steady traffic's evaluations
     (``test_the_incident_trips_5_percent_beside_as_much_steady_traffic``).

Usage:
  python demo/streampulse/simulator/traffic.py --rate 6 --duration 300
  python demo/streampulse/simulator/traffic.py --incident android12 --rate 6 --duration 90
  python demo/streampulse/simulator/traffic.py --dry-run --seed 1

Exit codes: 0 ok · 2 bad/missing API key (or ``--incident`` without the server key) ·
3 catalogue not seeded · 4 API unreachable.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from devices import (  # noqa: E402
    DEFAULT_POPULATION,
    DEFAULT_SEED,
    PRESET_DEVICES,
    Device,
    generate_devices,
    stable_bucket,
)

# --------------------------------------------------------------------------------------
# Catalogue (mirrors backend/scripts/seed_streampulse.py)
# --------------------------------------------------------------------------------------

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_API_KEY_FILE = Path(__file__).resolve().parent.parent / ".api_key"
# The StreamPulse server key (scope sdk:ruleset), written by the seed; crash reports use it.
DEFAULT_SERVER_KEY_FILE = Path(__file__).resolve().parent.parent / ".api_key_server"
BATCH_LIMIT = 100
ERROR_BATCH_LIMIT = 100
REPORT_INTERVAL_S = 10.0

RECS_FLAG = "streampulse_recs_v2"
PLAYER_FLAG = "streampulse_player_v2"
AI_SEARCH_FLAG = "streampulse_ai_search"
OFFLINE_FLAG = "streampulse_offline_mode"
FLAG_KEYS: tuple[str, ...] = (RECS_FLAG, PLAYER_FLAG, AI_SEARCH_FLAG, OFFLINE_FLAG)

PUSH_KEY = "streampulse_push_frequency"
WRAPPED_KEY = "streampulse_wrapped"
BADGES_KEY = "streampulse_profile_badges"
ONBOARDING_KEY = "streampulse_onboarding_steps"
UPSELL_KEY = "streampulse_upsell_modal"
EXPERIMENT_KEYS: tuple[str, ...] = (PUSH_KEY, WRAPPED_KEY, BADGES_KEY, ONBOARDING_KEY, UPSELL_KEY)

# Variant split + configuration. Only used offline (--dry-run / tests); live, the server decides.
VARIANTS: dict[str, dict[str, dict[str, Any]]] = {
    PUSH_KEY: {"daily": {"frequency": "daily"}, "three_weekly": {"frequency": "3x_week"}},
    WRAPPED_KEY: {"control": {"wrapped": False}, "wrapped_2026": {"wrapped": True}},
    BADGES_KEY: {"control": {"badges": False}, "badges": {"badges": True}},
    ONBOARDING_KEY: {"five_step": {"steps": 5}, "three_step": {"steps": 3}},
    UPSELL_KEY: {"control": {"modal": "classic"}, "value_modal": {"modal": "value"}},
}
CONTROL_VARIANT: dict[str, str] = {
    PUSH_KEY: "daily", WRAPPED_KEY: "control", BADGES_KEY: "control", ONBOARDING_KEY: "five_step", UPSELL_KEY: "control",
}

PRIMARY_METRIC: dict[str, str] = {
    PUSH_KEY: "notification_open",
    WRAPPED_KEY: "wrapped_view",
    BADGES_KEY: "badge_tap",
    ONBOARDING_KEY: "onboarding_complete",
    UPSELL_KEY: "subscribe",
}

# --------------------------------------------------------------------------------------
# True rates (spec §2) — the history seeder uses the same numbers
# --------------------------------------------------------------------------------------

NOTIFICATION_OPEN_RATE = {"daily": 0.22, "three_weekly": 0.27}
APP_UNINSTALL_RATE = {"daily": 0.018, "three_weekly": 0.011}
WRAPPED_VIEW_RATE = {"control": 0.0, "wrapped_2026": 0.41}
SHARE_RATE_OF_VIEWERS = {"control": 0.06, "wrapped_2026": 0.12}
BADGE_TAP_RATE = {"control": 0.03, "badges": 0.14}
ONBOARDING_COMPLETE_RATE = {"five_step": 0.58, "three_step": 0.71}
FIRST_PLAY_RATE = {"five_step": 0.45, "three_step": 0.56}  # marginal; only after completion
SUBSCRIBE_RATE = {"control": 0.024, "value_modal": 0.031}
SUBSCRIPTION_PRICE = 9.99

# Screen visit probabilities (a converting session always visits the screen).
PLAYER_VISIT_RATE = 0.75
SEARCH_VISIT_RATE = 0.35
PROFILE_VISIT_RATE = 0.50
PAYMENTS_VISIT_RATE = 0.30

# --- Incident model -------------------------------------------------------------------
INCIDENT_AFFECTED_SHARE = 0.40  # of Android 12 devices with player v2 ON
# Every incident tick is an Android 12 device's: the incident is their crash loops, and the
# steady traffic beside it keeps the usual device mix. At half (with 2 relaunches) the reports
# came to 2.6% of the flag's evaluations on the demo stack, steady traffic included, and the
# flag never went critical.
INCIDENT_ANDROID12_SHARE = 1.0  # share of incident ticks drawn from Android 12 devices
INCIDENT_RELAUNCHES = 4  # relaunches after the first crash: each re-evaluates the flag and crashes again
CRASH_MESSAGE = "NullPointerException in PlayerV2Fragment"
INCIDENT_MODES = ("off", "android12")

TRACKS = ("trk-1001", "trk-1002", "trk-1003", "trk-1004", "trk-1005", "trk-1006", "trk-1007", "trk-1008")
SEARCH_QUERIES = ("lofi beats", "workout", "taylor", "jazz piano", "podcast news", "sleep sounds", "top hits", "indie rock")


def _mean(values) -> float:
    vals = list(values)
    return sum(vals) / len(vals)


def rate_for(table: dict[str, float], variant: str) -> float:
    """Rate for a variant; unknown names (renamed on the server) fall back to the table mean."""
    return table.get(variant, _mean(table.values()))


def is_affected_by_incident(device: Device) -> bool:
    """The 40% of Android 12 devices the broken build crashes on (stable per device)."""
    return device.is_android_12 and stable_bucket(device.device_id, "player_v2_crash") < int(INCIDENT_AFFECTED_SHARE * 100)


# --------------------------------------------------------------------------------------
# Session (pure: no network)
# --------------------------------------------------------------------------------------


@dataclass
class SessionPlan:
    device: Device
    flags: dict[str, dict[str, Any]]  # key -> {"enabled": bool, "reason": str}
    assignments: dict[str, dict[str, Any]]  # key -> {"variant_name", "assigned", "reason", "configuration"}
    events: list[dict[str, Any]] = field(default_factory=list)  # /tracking/batch bodies
    crash_reports: list[dict[str, Any]] = field(default_factory=list)  # /tracking/errors bodies
    relaunches: int = 0  # extra flag evaluations the crash loop causes
    outcomes: dict[str, bool] = field(default_factory=dict)
    first_session: bool = True  # onboarding only happens in a device's first session

    @property
    def user_id(self) -> str:
        return self.device.device_id

    def flag_on(self, key: str) -> bool:
        return bool(self.flags.get(key, {}).get("enabled"))

    def assigned(self, key: str) -> bool:
        return bool(self.assignments.get(key, {}).get("assigned"))

    def variant(self, key: str) -> str:
        return str(self.assignments.get(key, {}).get("variant_name") or CONTROL_VARIANT[key])

    def converted(self, experiment_key: str) -> bool:
        """Did the primary metric fire with this experiment's key (what the server sees)?"""
        metric = PRIMARY_METRIC[experiment_key]
        return any(e.get("experiment_key") == experiment_key and e["event_name"] == metric for e in self.events)


def simulate_session(
    rng: random.Random,
    device: Device,
    flags: dict[str, dict[str, Any]],
    assignments: dict[str, dict[str, Any]],
    *,
    incident: str = "off",
    first_session: bool = True,
    start_time: datetime | None = None,
) -> SessionPlan:
    """Walk one app session: returns the events and crash reports to send. No network."""
    plan = SessionPlan(device, dict(flags), dict(assignments), first_session=first_session)
    clock = start_time or datetime.now(timezone.utc)
    user_id = device.device_id

    def stamp() -> str:
        nonlocal clock
        clock = clock + timedelta(seconds=rng.uniform(1.0, 15.0))
        return clock.isoformat(timespec="seconds")

    def emit(name: str, metadata: dict[str, Any] | None = None, *, experiment: str | None = None,
             flag: str | None = None, value: float | None = None) -> bool:
        """Queue an event attributed to one experiment or flag. Unassigned experiments get nothing."""
        if experiment is not None and not plan.assigned(experiment):
            return False
        body: dict[str, Any] = {
            "event_type": name,
            "event_name": name,
            "user_id": user_id,
            "metadata": metadata or {},
            "timestamp": stamp(),
        }
        if value is not None:
            body["value"] = round(float(value), 2)
        if experiment is not None:
            body["experiment_key"] = experiment
        else:
            body["feature_flag_key"] = flag
        plan.events.append(body)
        return True

    recs_on = plan.flag_on(RECS_FLAG)
    player_v2_on = plan.flag_on(PLAYER_FLAG)
    ai_search_on = plan.flag_on(AI_SEARCH_FLAG)
    outcomes: dict[str, bool] = {}

    # --- Onboarding (first session only) ------------------------------------------------
    onboarding_variant = plan.variant(ONBOARDING_KEY)
    completed = first_played = False
    if first_session:
        steps = plan.assignments.get(ONBOARDING_KEY, {}).get("configuration", {}).get("steps") or VARIANTS[ONBOARDING_KEY].get(onboarding_variant, {}).get("steps", 5)
        emit("screen_view", {"screen": "onboarding"}, experiment=ONBOARDING_KEY)
        complete_rate = rate_for(ONBOARDING_COMPLETE_RATE, onboarding_variant)
        completed = rng.random() < complete_rate
        if completed:
            emit("onboarding_complete", {"steps": steps}, experiment=ONBOARDING_KEY)
            first_played = rng.random() < rate_for(FIRST_PLAY_RATE, onboarding_variant) / max(complete_rate, 1e-9)
            if first_played:
                emit("first_play", {}, experiment=ONBOARDING_KEY)
    outcomes["onboarding_complete"] = completed
    outcomes["first_play"] = first_played

    # --- Home feed (recs v2 kill switch) ------------------------------------------------
    emit("screen_view", {"screen": "home"}, flag=RECS_FLAG)
    if recs_on:
        emit("recs_impression", {"algorithm": "recs_v2"}, flag=RECS_FLAG)
    outcomes["recs_impression"] = recs_on

    # --- Player (gradual rollout + the incident) ------------------------------------------
    crashed = False
    visit_player = first_played or rng.random() < PLAYER_VISIT_RATE
    if visit_player:
        emit("screen_view", {"screen": "player"}, flag=PLAYER_FLAG)
        if incident == "android12" and player_v2_on and is_affected_by_incident(device):
            crashed = True
            plan.relaunches = INCIDENT_RELAUNCHES
            for _ in range(1 + INCIDENT_RELAUNCHES):
                plan.crash_reports.append(
                    {
                        "feature_flag_key": PLAYER_FLAG,
                        "user_id": user_id,
                        "error_type": "crash",
                        "message": CRASH_MESSAGE,
                        "metadata": {
                            "os": device.os,
                            "os_version": device.os_version,
                            "device_model": device.device_model,
                            "app_version": device.app_version,
                        },
                        "timestamp": stamp(),
                    }
                )
        else:
            player = "v2" if player_v2_on else "classic"
            for _ in range(rng.randint(1, 3)):
                emit("play", {"track_id": rng.choice(TRACKS), "player": player}, flag=PLAYER_FLAG)
    outcomes["play"] = visit_player and not crashed
    outcomes["crash"] = crashed
    if crashed:
        # The app died on the player screen; nothing else happens this session.
        plan.outcomes = outcomes
        return plan

    # --- Search (targeting rules) ----------------------------------------------------------
    searched = rng.random() < SEARCH_VISIT_RATE
    if searched:
        emit("screen_view", {"screen": "search"}, flag=AI_SEARCH_FLAG)
        engine = "ai" if ai_search_on else "keyword"
        results = rng.randint(4, 12) if ai_search_on else rng.randint(1, 6)
        emit("search", {"query": rng.choice(SEARCH_QUERIES), "engine": engine, "results": results}, flag=AI_SEARCH_FLAG)
    outcomes["search"] = searched

    # --- Notifications (push frequency A/B, uninstall guardrail) ------------------------------
    push_variant = plan.variant(PUSH_KEY)
    frequency = plan.assignments.get(PUSH_KEY, {}).get("configuration", {}).get("frequency") or VARIANTS[PUSH_KEY].get(push_variant, {}).get("frequency", "daily")
    opened = rng.random() < rate_for(NOTIFICATION_OPEN_RATE, push_variant)
    uninstalled = rng.random() < rate_for(APP_UNINSTALL_RATE, push_variant)
    if opened:
        emit("screen_view", {"screen": "notifications"}, experiment=PUSH_KEY)
        emit("notification_open", {"frequency": frequency}, experiment=PUSH_KEY)
    outcomes["notification_open"] = opened

    # --- Profile (mutual exclusion group: Wrapped xor badges) ----------------------------------
    wrapped_variant = plan.variant(WRAPPED_KEY)
    badges_variant = plan.variant(BADGES_KEY)
    viewed_wrapped = plan.assigned(WRAPPED_KEY) and rng.random() < rate_for(WRAPPED_VIEW_RATE, wrapped_variant)
    tapped_badge = plan.assigned(BADGES_KEY) and rng.random() < rate_for(BADGE_TAP_RATE, badges_variant)
    visit_profile = viewed_wrapped or tapped_badge or rng.random() < PROFILE_VISIT_RATE
    if visit_profile:
        owner = WRAPPED_KEY if plan.assigned(WRAPPED_KEY) else BADGES_KEY
        emit("screen_view", {"screen": "profile"}, experiment=owner)
    shared = False
    if viewed_wrapped:
        emit("wrapped_view", {}, experiment=WRAPPED_KEY)
        shared = rng.random() < rate_for(SHARE_RATE_OF_VIEWERS, wrapped_variant)
        if shared:
            emit("share", {"surface": "wrapped"}, experiment=WRAPPED_KEY)
    if tapped_badge:
        emit("badge_tap", {}, experiment=BADGES_KEY)
    outcomes["wrapped_view"] = viewed_wrapped
    outcomes["share"] = shared
    outcomes["badge_tap"] = tapped_badge

    # --- Payments (upsell modal, audit-trail story) ---------------------------------------------
    upsell_variant = plan.variant(UPSELL_KEY)
    modal = plan.assignments.get(UPSELL_KEY, {}).get("configuration", {}).get("modal") or VARIANTS[UPSELL_KEY].get(upsell_variant, {}).get("modal", "classic")
    subscribed = rng.random() < rate_for(SUBSCRIBE_RATE, upsell_variant)
    if subscribed or rng.random() < PAYMENTS_VISIT_RATE:
        emit("screen_view", {"screen": "payments"}, experiment=UPSELL_KEY)
    if subscribed:
        plan_name = "premium" if device.tier == "free" else "family"
        emit("subscribe", {"plan": plan_name, "modal": modal}, experiment=UPSELL_KEY, value=SUBSCRIPTION_PRICE)
    outcomes["subscribe"] = subscribed

    # --- Uninstall (guardrail) ends the session --------------------------------------------------
    if uninstalled:
        emit("app_uninstall", {}, experiment=PUSH_KEY)
    outcomes["app_uninstall"] = uninstalled

    plan.outcomes = outcomes
    return plan


# --------------------------------------------------------------------------------------
# API client (urllib only)
# --------------------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class ApiClient:
    """The device calls use ``api_key`` (the app's key); error reports use ``server_api_key``."""

    def __init__(self, api_url: str = DEFAULT_API_URL, api_key: str = "", timeout: float = 5.0, server_api_key: str = ""):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.server_api_key = server_api_key
        self.timeout = timeout

    def request(
        self, method: str, path: str, body: Any = None, params: dict[str, str] | None = None, api_key: str | None = None
    ) -> Any:
        url = f"{self.api_url}/api/v1{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"X-API-Key": api_key or self.api_key, "Content-Type": "application/json", "Accept": "application/json"},
        )
        raw = b""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    # Back off for the server's Retry-After (capped) and retry instead of
                    # counting a burst of rate-limit rejections as errors.
                    try:
                        wait = float(e.headers.get("Retry-After", "1"))
                    except (TypeError, ValueError):
                        wait = 1.0
                    time.sleep(min(max(wait, 0.5), 5.0))
                    continue
                raise ApiError(e.code, e.read().decode("utf-8", "replace"), url) from None
            except urllib.error.URLError as e:
                raise ApiError(0, str(e.reason), url) from None
            except (TimeoutError, OSError) as e:
                raise ApiError(0, str(e), url) from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(0, f"non-JSON response: {raw[:100]!r}", url) from None

    def evaluate_flag(self, flag_key: str, user_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """GET /feature-flags/evaluate/{key}?user_id=&context=<url-encoded compact JSON>."""
        return self.request(
            "GET",
            f"/feature-flags/evaluate/{flag_key}",
            params={"user_id": user_id, "context": compact_json(context)},
        )

    def assign(self, experiment_key: str, user_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/tracking/assign", {"experiment_key": experiment_key, "user_id": user_id, "context": context})

    def batch(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        if len(events) > BATCH_LIMIT:
            raise ValueError(f"batch too large: {len(events)} > {BATCH_LIMIT}")
        return self.request("POST", "/tracking/batch", {"events": events}) or {}

    def _server_key(self) -> str:
        if not self.server_api_key:
            raise ValueError("error reports are sent with the server key: pass --server-api-key")
        return self.server_api_key

    def report_error(self, report: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/tracking/errors", report, api_key=self._server_key()) or {}

    def report_errors(self, reports: list[dict[str, Any]]) -> dict[str, Any]:
        if len(reports) > ERROR_BATCH_LIMIT:
            raise ValueError(f"error batch too large: {len(reports)} > {ERROR_BATCH_LIMIT}")
        return self.request("POST", "/tracking/errors/batch", {"errors": reports}, api_key=self._server_key()) or {}


def chunked(items: list[dict[str, Any]], size: int = BATCH_LIMIT) -> Iterator[list[dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# --------------------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------------------

def is_ai_search_target(device: Device) -> bool:
    """The population the seeded ``streampulse_ai_search`` rule targets: iOS 17+, US, premium."""
    return device.os == "iOS" and device.os_major >= 17 and device.region == "US" and device.tier == "premium"


SEGMENTS: tuple[tuple[str, Callable[[Device], bool]], ...] = (
    ("iOS", lambda d: d.os == "iOS"),
    ("Android", lambda d: d.os == "Android"),
    ("Android12", lambda d: d.is_android_12),
    ("premium", lambda d: d.tier == "premium"),
    ("free", lambda d: d.tier == "free"),
    ("iOS17+/US/prem", is_ai_search_target),
    ("employee", lambda d: d.employee),
)
UNASSIGNED_REASONS = ("holdout", "mutual_exclusion", "targeting")


class Stats:
    def __init__(self, incident: str = "off") -> None:
        self.started = time.monotonic()
        self.incident = incident
        self.ticks = 0
        self.devices: set[str] = set()
        self.evaluations = 0
        self.events_sent = 0
        self.rejected = 0  # failure_count reported by /tracking/batch
        self.api_errors = 0
        self.crash_reports = 0
        self.crashed_ticks = 0
        self.flag_eval: Counter[str] = Counter()
        self.flag_on: Counter[str] = Counter()
        self.flag_reason: dict[str, Counter[str]] = {k: Counter() for k in FLAG_KEYS}
        self.flag_seg_eval: dict[str, Counter[str]] = {k: Counter() for k in FLAG_KEYS}
        self.flag_seg_on: dict[str, Counter[str]] = {k: Counter() for k in FLAG_KEYS}
        self.variant_devices: dict[str, Counter[str]] = {k: Counter() for k in EXPERIMENT_KEYS}
        self.variant_conversions: dict[str, Counter[str]] = {k: Counter() for k in EXPERIMENT_KEYS}
        self.unassigned: dict[str, Counter[str]] = {k: Counter() for k in EXPERIMENT_KEYS}

    def record(self, plan: SessionPlan) -> None:
        self.ticks += 1
        self.devices.add(plan.user_id)
        self.events_sent += len(plan.events)
        self.crash_reports += len(plan.crash_reports)
        if plan.crash_reports:
            self.crashed_ticks += 1
        for key, result in plan.flags.items():
            if key not in self.flag_reason:
                continue
            self.flag_eval[key] += 1
            self.flag_reason[key][str(result.get("reason") or "?")] += 1
            enabled = bool(result.get("enabled"))
            if enabled:
                self.flag_on[key] += 1
            for name, pred in SEGMENTS:
                if pred(plan.device):
                    self.flag_seg_eval[key][name] += 1
                    if enabled:
                        self.flag_seg_on[key][name] += 1
        for key in EXPERIMENT_KEYS:
            result = plan.assignments.get(key)
            if result is None:
                continue
            if result.get("assigned"):
                variant = str(result.get("variant_name"))
                self.variant_devices[key][variant] += 1
                if plan.converted(key):
                    self.variant_conversions[key][variant] += 1
            else:
                self.unassigned[key][str(result.get("reason") or "?")] += 1

    def rate(self, experiment_key: str, variant: str) -> float:
        n = self.variant_devices[experiment_key][variant]
        return self.variant_conversions[experiment_key][variant] / n if n else 0.0

    def flag_rate(self, key: str, segment: str | None = None) -> float:
        if segment is None:
            n = self.flag_eval[key]
            return self.flag_on[key] / n if n else 0.0
        n = self.flag_seg_eval[key][segment]
        return self.flag_seg_on[key][segment] / n if n else 0.0

    def render(self) -> str:
        elapsed = max(time.monotonic() - self.started, 1e-9)
        mm, ss = divmod(int(elapsed), 60)
        lines = [
            f"[{mm:02d}:{ss:02d}] ticks={self.ticks} ({self.ticks / elapsed:.1f}/s) devices={len(self.devices)} "
            f"evaluations={self.evaluations} events={self.events_sent} rejected={self.rejected} "
            f"api_errors={self.api_errors} crash_reports={self.crash_reports}"
        ]
        lines.append(f"  {'flag':<26}{'on/eval':>10}  " + "  ".join(f"{name:>13}" for name, _ in SEGMENTS) + "   reasons")

        def seg(key: str, name: str) -> str:
            n = self.flag_seg_eval[key][name]
            return f"{self.flag_seg_on[key][name]}/{n} {100 * self.flag_rate(key, name):3.0f}%" if n else "-"

        for key in FLAG_KEYS:
            if not self.flag_eval[key]:
                continue
            reasons = " ".join(f"{r}={n}" for r, n in sorted(self.flag_reason[key].items()))
            lines.append(
                f"  {key:<26}{self.flag_on[key]:>4}/{self.flag_eval[key]:<5} "
                + "  ".join(f"{seg(key, name):>13}" for name, _ in SEGMENTS)
                + f"   {reasons}"
            )
        lines.append(f"  {'experiment':<30}{'primary metric':<20}variants (devices, conversion)      not assigned")
        for key in EXPERIMENT_KEYS:
            parts = [f"{v}={n} {100 * self.rate(key, v):.1f}%" for v, n in sorted(self.variant_devices[key].items())]
            skipped = " ".join(f"{r}={n}" for r, n in sorted(self.unassigned[key].items()))
            lines.append(f"  {key:<30}{PRIMARY_METRIC[key]:<20}{'  '.join(parts):<36}  {skipped}")
        if self.incident != "off":
            lines.append(
                f"  incident={self.incident}: crashed sessions={self.crashed_ticks} crash reports sent={self.crash_reports} "
                f"(player_v2 error rate ≈ {100 * self.crash_reports / max(self.flag_eval[PLAYER_FLAG] + self.crash_reports - self.crashed_ticks, 1):.1f}% of its evaluations this run)"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------


class Population:
    """The device population plus the Android 12 subset the incident oversamples."""

    def __init__(self, devices: list[Device]):
        self.devices = list(devices)
        self.android12 = [d for d in self.devices if d.is_android_12]
        self.seen: set[str] = set()

    def pick(self, rng: random.Random, incident: str = "off") -> Device:
        if incident == "android12" and self.android12 and rng.random() < INCIDENT_ANDROID12_SHARE:
            return rng.choice(self.android12)
        return rng.choice(self.devices)


def evaluate_flags(client: Any, device: Device) -> dict[str, dict[str, Any]]:
    attrs = device.attributes()
    flags: dict[str, dict[str, Any]] = {}
    for key in FLAG_KEYS:
        resp = client.evaluate_flag(key, device.device_id, attrs) or {}
        flags[key] = {"enabled": bool(resp.get("enabled")), "reason": str(resp.get("reason") or "rollout")}
    return flags


def assign_experiments(client: Any, device: Device) -> dict[str, dict[str, Any]]:
    attrs = device.attributes()
    assignments: dict[str, dict[str, Any]] = {}
    for key in EXPERIMENT_KEYS:
        resp = client.assign(key, device.device_id, attrs) or {}
        assignments[key] = {
            "variant_name": str(resp.get("variant_name") or CONTROL_VARIANT[key]),
            "assigned": bool(resp.get("assigned", True)),
            "reason": str(resp.get("reason") or "assigned"),
            "configuration": resp.get("configuration") or {},
        }
    return assignments


def run_tick(client: Any, rng: random.Random, population: Population, stats: Stats, incident: str = "off") -> SessionPlan:
    """Evaluate flags, assign experiments, walk one session and send everything for one device."""
    device = population.pick(rng, incident)
    first_session = device.device_id not in population.seen
    population.seen.add(device.device_id)

    flags = evaluate_flags(client, device)
    stats.evaluations += len(FLAG_KEYS)
    assignments = assign_experiments(client, device)
    plan = simulate_session(rng, device, flags, assignments, incident=incident, first_session=first_session)

    for chunk in chunked(plan.events):
        resp = client.batch(chunk)
        stats.rejected += int(resp.get("failure_count", 0) or 0)

    # Crash loop: the user relaunches the app, which re-reads the flag (still ON) and crashes again.
    for _ in range(plan.relaunches):
        client.evaluate_flag(PLAYER_FLAG, device.device_id, device.attributes())
        stats.evaluations += 1
    if len(plan.crash_reports) == 1:
        client.report_error(plan.crash_reports[0])
    elif plan.crash_reports:
        for chunk in chunked(plan.crash_reports, ERROR_BATCH_LIMIT):
            client.report_errors(chunk)

    stats.record(plan)
    return plan


def offline_flags(device: Device) -> dict[str, dict[str, Any]]:
    """Evaluate the seeded rules locally (dry-run and tests only)."""
    player_on = device.employee or stable_bucket(device.device_id, PLAYER_FLAG) < 5
    ai_on = is_ai_search_target(device)
    return {
        RECS_FLAG: {"enabled": True, "reason": "rollout"},
        PLAYER_FLAG: {"enabled": player_on, "reason": "targeting_rule" if device.employee else "rollout"},
        AI_SEARCH_FLAG: {"enabled": ai_on, "reason": "targeting_rule" if ai_on else "rollout"},
        OFFLINE_FLAG: {"enabled": True, "reason": "rollout"},
    }


def offline_assignments(rng: random.Random, device: Device) -> dict[str, dict[str, Any]]:
    """Pick variants locally with a 2% holdout and the profile mutual exclusion group (dry-run and tests only)."""
    in_holdout = stable_bucket(device.device_id, "global_holdout") < 2
    wrapped_side = stable_bucket(device.device_id, "meg-profile") < 50
    assignments: dict[str, dict[str, Any]] = {}
    for key, split in VARIANTS.items():
        variant = rng.choice(list(split))
        if in_holdout:
            variant, assigned, reason = CONTROL_VARIANT[key], False, "holdout"
        elif key == WRAPPED_KEY and not wrapped_side or key == BADGES_KEY and wrapped_side:
            variant, assigned, reason = CONTROL_VARIANT[key], False, "mutual_exclusion"
        else:
            assigned, reason = True, "assigned"
        assignments[key] = {"variant_name": variant, "assigned": assigned, "reason": reason, "configuration": split[variant]}
    return assignments


def dry_run(rng: random.Random, out: TextIO, incident: str = "off", devices: int = DEFAULT_POPULATION, seed: int = DEFAULT_SEED) -> SessionPlan:
    population = Population(generate_devices(devices, seed))
    if incident == "android12":
        # Show what an affected device does: an Android 12 device that has Player v2 ON and crashes.
        candidates = [d for d in population.android12 if is_affected_by_incident(d) and offline_flags(d)[PLAYER_FLAG]["enabled"]]
        rng.shuffle(candidates)
        plan = None
        for device in candidates or [population.pick(rng, incident)]:
            plan = simulate_session(rng, device, offline_flags(device), offline_assignments(rng, device), incident=incident, first_session=True)
            if plan.crash_reports:
                break
        assert plan is not None
        device, flags, assignments = plan.device, plan.flags, plan.assignments
    else:
        device = population.pick(rng, incident)
        flags = offline_flags(device)
        assignments = offline_assignments(rng, device)
        plan = simulate_session(rng, device, flags, assignments, incident=incident, first_session=True)
    print(f"# dry run — no network. device={device.device_id} ({device.label})", file=out)
    print(f"# context={compact_json(device.attributes())}", file=out)
    print(f"# flags={compact_json(flags)}", file=out)
    print(f"# assignments={compact_json({k: [v['variant_name'], v['reason']] for k, v in assignments.items()})}", file=out)
    print(f"# outcomes={compact_json(plan.outcomes)}", file=out)
    print(f"# {len(plan.events)} track bodies and {len(plan.crash_reports)} crash reports would be sent:", file=out)
    for event in plan.events:
        print(json.dumps(event, sort_keys=True), file=out)
    for report in plan.crash_reports:
        print(json.dumps({"POST /tracking/errors": report}, sort_keys=True), file=out)
    return plan


def probe_presets(client: Any, out: TextIO) -> list[dict[str, Any]]:
    """Show what the app's five device presets get right now (flags + experiments)."""
    rows: list[dict[str, Any]] = []
    print("Device presets (what the app's device panel shows right now):", file=out)
    for device in PRESET_DEVICES:
        flags = evaluate_flags(client, device)
        assignments = assign_experiments(client, device)
        rows.append({"device": device, "flags": flags, "assignments": assignments})
        flag_text = "  ".join(
            f"{key.removeprefix('streampulse_')}={'ON' if r['enabled'] else 'off'}({r['reason']})" for key, r in flags.items()
        )
        exp_text = "  ".join(
            f"{key.removeprefix('streampulse_')}={r['variant_name'] if r['assigned'] else '-'}({r['reason']})"
            for key, r in assignments.items()
        )
        print(f"  {device.label}\n    flags: {flag_text}\n    experiments: {exp_text}", file=out)
    return rows


def read_api_key_file(path: Path) -> str | None:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traffic.py",
        description="Simulate StreamPulse mobile devices against the Controlvar public API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Controlvar API origin")
    parser.add_argument("--api-key", default=None, help=f"X-API-Key value (default: contents of {DEFAULT_API_KEY_FILE})")
    parser.add_argument(
        "--server-api-key",
        default=None,
        help=f"X-API-Key for the crash reports: the StreamPulse server key, scope sdk:ruleset (default: contents of {DEFAULT_SERVER_KEY_FILE})",
    )
    parser.add_argument("--rate", type=float, default=3.0, help="device sessions per second")
    parser.add_argument("--duration", type=float, default=0.0, help="seconds to run (0 = forever)")
    parser.add_argument("--seed", type=int, default=None, help="random seed for reproducible traffic")
    parser.add_argument("--devices", type=int, default=DEFAULT_POPULATION, metavar="N", help="size of the simulated device population")
    parser.add_argument("--population-seed", type=int, default=DEFAULT_SEED, help="seed of the device population (keep it fixed so device ids are stable)")
    parser.add_argument("--incident", choices=INCIDENT_MODES, default="off", help="android12: Player v2 crashes on 40%% of Android 12 devices that have it ON")
    parser.add_argument("--max-ticks", type=int, default=0, help="stop after this many sessions (0 = no limit)")
    parser.add_argument("--no-probe", action="store_true", help="skip the device-preset table printed at start-up")
    parser.add_argument("--dry-run", action="store_true", help="print one device's planned events and exit (no network)")
    return parser


FATAL_EXIT_CODES = {401: 2, 403: 2, 404: 3, 0: 4}


def fatal_message(err: ApiError, api_url: str) -> str | None:
    if err.status in (401, 403):
        return (
            f"Unauthorized ({err.status}) from {api_url}: the API key was rejected. "
            "Re-run backend/scripts/seed_streampulse.py (it writes demo/streampulse/.api_key) or pass --api-key."
        )
    if err.status == 404:
        return (
            f"Not found (404) from {err.url}: the StreamPulse experiments are not seeded or not ACTIVE, or its flags are not seeded. "
            "Run backend/scripts/seed_streampulse.py first."
        )
    if err.status == 0:
        return f"Cannot reach {api_url}: {err.body}. Is the backend running (uvicorn app.main:app --port 8000)?"
    return None


def main(
    argv: list[str] | None = None,
    client_factory: Callable[..., Any] = ApiClient,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    if args.rate <= 0:
        print("error: --rate must be > 0", file=sys.stderr)
        return 2
    if args.devices <= 0:
        print("error: --devices must be > 0", file=sys.stderr)
        return 2
    rng = random.Random(args.seed)

    if args.dry_run:
        dry_run(rng, out, incident=args.incident, devices=args.devices, seed=args.population_seed)
        return 0

    api_key = args.api_key or read_api_key_file(DEFAULT_API_KEY_FILE)
    if not api_key:
        print(
            f"error: no API key. Pass --api-key or run backend/scripts/seed_streampulse.py to create {DEFAULT_API_KEY_FILE}.",
            file=sys.stderr,
        )
        return 2

    server_api_key = args.server_api_key or read_api_key_file(DEFAULT_SERVER_KEY_FILE)
    if args.incident != "off" and not server_api_key:
        print(
            "error: --incident sends crash reports with the StreamPulse server key, and there is none. "
            f"Pass --server-api-key or run backend/scripts/seed_streampulse.py to create {DEFAULT_SERVER_KEY_FILE}.",
            file=sys.stderr,
        )
        return 2

    client = client_factory(args.api_url, api_key, server_api_key=server_api_key or "")
    population = Population(generate_devices(args.devices, args.population_seed))
    stats = Stats(incident=args.incident)
    interval = 1.0 / args.rate
    print(
        f"StreamPulse devices → {args.api_url}  key={api_key[:4]}…  rate={args.rate}/s  "
        f"duration={'∞' if not args.duration else f'{args.duration:g}s'}  devices={len(population.devices)} "
        f"(Android 12: {len(population.android12)})  incident={args.incident}  seed={args.seed}",
        file=out,
    )
    exit_code = 0
    try:
        if not args.no_probe:
            probe_presets(client, out)
    except ApiError as err:
        message = fatal_message(err, args.api_url)
        print(f"\nerror: {message or err}", file=sys.stderr)
        return FATAL_EXIT_CODES.get(err.status, 1)

    start = time.monotonic()
    next_report = start + REPORT_INTERVAL_S
    reported_ticks = -1
    try:
        while True:
            now = time.monotonic()
            if args.duration and now - start >= args.duration:
                break
            if args.max_ticks and stats.ticks >= args.max_ticks:
                break
            try:
                run_tick(client, rng, population, stats, incident=args.incident)
            except ApiError as err:
                message = fatal_message(err, args.api_url)
                if message:
                    print(f"\nerror: {message}", file=sys.stderr)
                    exit_code = FATAL_EXIT_CODES.get(err.status, 1)
                    break
                stats.api_errors += 1
                print(f"warning: {err}", file=sys.stderr)
            if time.monotonic() >= next_report:
                print(stats.render(), file=out)
                reported_ticks = stats.ticks
                next_report += REPORT_INTERVAL_S
            target = start + stats.ticks * interval
            delay = target - time.monotonic()
            if delay > 0:
                sleep(delay)
    except KeyboardInterrupt:
        print("\ninterrupted", file=out)
    if stats.ticks != reported_ticks:
        print(stats.render(), file=out)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
