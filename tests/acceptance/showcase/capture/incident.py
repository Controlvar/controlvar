"""Video 4's incident, off camera (S4, S5 and the B9 gate of plan v2.2).

Three steps run off camera between video 4's scenes (``storyboard.Story``,
``Crashes``, ``WaitForRollback``), and each is refused, never worked around,
when it does not go as the storyboard says:

* ``story: 2``: the StreamPulse demo's own rollout script, its step 2 only, run
  from the recorded tree as a child of kind ``story``, against the tool's API
  only. Its argv must equal, element for element, the one ``story_argv``
  builds (PE 6 of T178 14): a missing ``--api-url`` (the script would drive a
  developer's API), a second one, an abbreviation, an added flag or another
  step are all refused before it starts. Its environment is
  ``guard.story_settings`` and nothing else. It must exit 0, and the API must
  then show the flag at stage 2's percentage, stage 2 in progress.
* ``crashes`` (T156 C15, plan v2 C13): first B9's anchor (T165 C5). The safety
  scheduler's run history is read as the wait starts, and the anchor is the
  first successful run that was not there then; a run that was there never
  counts. With none within three intervals the video is refused ("no safety
  run completed"); a log line or the health endpoint's ``last_run_at`` is
  never used instead. The sends begin after that run completed, and at most
  ``config.SEND_AFTER_TICK_MAX_S`` after it. They run in this process
  (``traffic.direct_session``: no proxy) and go to the stack's API origin
  only, refused before any request otherwise (PE 7). Android 12 devices ask
  for the flag with the app's key, and each crashing device's crashes are
  reported with the StreamPulse server key, held in this process: never on an
  argv, in an environment, a log or a frame; on disk only in needles.txt
  (mode 0600) until the render deletes it. Every answer must be 2xx, and every
  crashing device must have been answered "on". Then the API's count: b, the
  flag's evaluations in the monitor's window less the sender's, is the
  browser's, and is at most ``config.BROWSER_EVALUATIONS_MAX``; the crashes
  the API counts are the ones sent; and the flag's error rate (the safety
  check's ``error_rate`` metric) is above its limit.
* ``wait_for_rollback``: the flag's audit log is read until the safety
  monitor's rollback is there and the flag is at the percentage, refused
  after ``within_s``; a rollback by anyone else is refused at once. Nothing
  here asks for a rollback. Then the B9 gate: B9's page showed the flag
  unhealthy, with the error rate the API counted after the crashes and the
  rollback's reason gives (plan v2 C13: the rate on screen is the API's), and
  the rollback came after B9's last frame, comparing the host's clock with the
  API's, which may differ by at most ``config.CLOCK_SKEW_MAX_S`` (measured at
  the anchor).

S5: the card after the wait says the gap the cut hides, from B9's last frame
to the card, on the wall clock (``time.time``): the recording's own clock does
not advance across a cut, so a gap measured on it is about 0 and would always
read "1 minute" (PE 8). After the card is filmed, ``card_gap_problem`` checks
the minutes it showed against the gap to its first frame, within 30 s.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from showcase.capture import config, guard, storyboard, traffic
from showcase.capture import gates as judge
from showcase.capture.director import CaptureFailed

#: How long the rollout script may take: it signs in, reads, and moves a stage.
STORY_TIMEOUT_S = 120
#: How far the minutes a time card says may be from the gap it hides (U3).
CARD_GAP_TOLERANCE_S = 30
#: The longest one look at the API may take, for B9's clock comparison.
REQUEST_LATENCY_MAX_S = 0.5


def parse_utc(text: Any) -> float:
    """An ISO 8601 time from the API as seconds since the epoch; a time with no
    offset is UTC (the API's ``datetime.utcnow``). Refused when unreadable."""
    if not isinstance(text, str) or not text:
        raise CaptureFailed("B9", f"the API gave no time ({text!r})")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise CaptureFailed("B9", f"the API's time {text!r} cannot be read") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


class Api:
    """The tool's own reads of the stack's API, signed in as the demo admin."""

    def __init__(self, base: str, token: str, opener: Any = None):
        self.base = base.rstrip("/")
        self.token = token
        self.opener = opener or guard.http_opener()

    def get(self, path: str) -> Any:
        request = urllib.request.Request(
            self.base + path, headers={"Authorization": f"Bearer {self.token}"}
        )
        with self.opener.open(request, timeout=60) as answer:
            return json.load(answer)

    def get_with_key(self, path: str, key: str) -> Any:
        """A GET as an app makes it, with an API key (``X-API-Key``)."""
        request = urllib.request.Request(self.base + path, headers={"X-API-Key": key})
        with self.opener.open(request, timeout=60) as answer:
            return json.load(answer)


def in_scene(where: str, scene_id: str) -> bool:
    """Whether the director's ``where`` is in *scene_id*: its load, off camera
    (``scene <id>``), or one of its beats (``scene <id> beat <n>``)."""
    return where == f"scene {scene_id}" or where.startswith(f"scene {scene_id} beat ")


def flag_by_key(api: Any, key: str) -> Dict[str, Any]:
    """The one flag with *key*, through the API; refused when there is not one."""
    listed = api.get(
        f"/api/v1/feature-flags/?search={urllib.parse.quote(key)}&limit=100"
    )
    found = [item for item in listed.get("items") or [] if item.get("key") == key]
    if len(found) != 1:
        raise CaptureFailed("S4", f"{len(found)} flags have the key {key}")
    return found[0]


def flag_audit(api: Any, flag_id: str) -> List[Dict[str, Any]]:
    """The flag's audit entries, newest first, as the audit API lists them."""
    return list(
        api.get(
            f"/api/v1/audit-logs/?entity_type=feature_flag&entity_id={flag_id}&limit=100"
        ).get("items")
        or []
    )


# ---------------------------------------------------------------------------
# story: 2
# ---------------------------------------------------------------------------


def story_argv(
    tree: Path, *, api: str, dashboard: str, app: str, python: str = sys.executable
) -> List[str]:
    """The rollout script's one argv: step 2, every address the tool's own."""
    return [
        python,
        str(tree.joinpath(*config.ROLLOUT_STORY)),
        "--step",
        "2",
        "--api-url",
        api,
        "--dashboard-url",
        dashboard,
        "--app-url",
        app,
    ]


def refuse_story_argv(argv: Sequence[Any], wanted: Sequence[str]) -> None:
    """Refused unless *argv* is *wanted*, element for element (PE 6)."""
    given = [str(arg) for arg in argv]
    if given == list(wanted):
        return
    at = next(
        (i for i, (a, b) in enumerate(zip(given, wanted)) if a != b),
        min(len(given), len(wanted)),
    )
    raise guard.Refused(
        f"the rollout script runs as `{' '.join(wanted[1:])}` exactly; its argv"
        f" differs at element {at} ({len(given)} elements, not {len(wanted)})"
    )


def run_story(
    *,
    tree: Path,
    children: Any,
    logs: Path,
    parent: Mapping[str, str],
    api_url: str,
    dashboard_url: str,
    app_url: str,
    ports: Any,
    python: str = sys.executable,
) -> None:
    """``rollout_story.py --step 2`` from the recorded tree; refused unless it
    exits 0. Its argv, built from the stack's addresses, must equal element for
    element the one built from the tool's own ports (PE 6): an address that is
    not the tool's, a developer's API among them, refuses before it starts."""
    argv = story_argv(
        tree, api=api_url, dashboard=dashboard_url, app=app_url, python=python
    )
    refuse_story_argv(
        argv,
        story_argv(
            tree,
            api=guard.origin(ports.api),
            dashboard=guard.origin(ports.dashboard),
            app=guard.origin(ports.app("streampulse")),
            python=python,
        ),
    )
    status = children.run(
        "story-2",
        argv,
        cwd=tree,
        env=guard.child_env(guard.story_settings(), parent=parent),
        log=logs / "story-2.log",
        kind="story",
        timeout=STORY_TIMEOUT_S,
    )
    if status != 0:
        raise CaptureFailed(
            "S4", f"the rollout script's step 2 exited {status}; see {logs}/story-2.log"
        )


def story_done(api: Any, key: str) -> Dict[str, Any]:
    """After step 2: the flag is at stage 2's percentage, stage 2 in progress,
    the schedule active (A3 of the UX draft, B6). The numbers, or refused."""
    flag = flag_by_key(api, key)
    schedules = (
        api.get(
            f"/api/v1/rollout-schedules/?feature_flag_id={flag['id']}&limit=100"
        ).get("items")
        or []
    )
    if len(schedules) != 1:
        raise CaptureFailed(
            "S4", f"{key} has {len(schedules)} rollout schedules, not 1"
        )
    schedule = schedules[0]
    stages = {int(s.get("stage_order") or 0): s for s in schedule.get("stages") or []}
    second = stages.get(2) or {}
    numbers = {
        "rollout_percentage": flag.get("rollout_percentage"),
        "schedule": str(schedule.get("status")).lower(),
        "stage_2": str(second.get("status")).lower(),
        "stage_2_target": second.get("target_percentage"),
    }
    if (
        numbers["schedule"] != "active"
        or numbers["stage_2"] != "in_progress"
        or flag.get("rollout_percentage") != second.get("target_percentage")
    ):
        raise CaptureFailed(
            "S4",
            f"after the rollout script's step 2, {key} reads {numbers}: stage 2 in"
            " progress at its percentage, the schedule active",
        )
    return numbers


# ---------------------------------------------------------------------------
# crashes
# ---------------------------------------------------------------------------

#: Android 12 shipped on these (demo/streampulse/simulator/devices.py,
#: ``ANDROID_12_MODELS``).
ANDROID_12_MODELS = ("Pixel 6", "Galaxy S10", "Galaxy S21")
REGIONS = ("US", "GB", "DE", "IN", "BR")
APP_VERSIONS = ("3.1.0", "3.2.0", "3.2.1")


@dataclass(frozen=True)
class Call:
    """One request the crash sender makes: an ask for the flag, or a crash report."""

    kind: str
    device: str
    attributes: Dict[str, Any]


def _device(ident: str, index: int, *, crashing: bool) -> Dict[str, Any]:
    """An Android 12 device's attributes, as the StreamPulse simulator sends
    them (``Device.attributes``: every field, its id included)."""
    return {
        "device_id": ident,
        "os": "Android",
        "os_version": "12.0.0",
        # The broken build: every crashing device runs it.
        "app_version": "3.1.0" if crashing else APP_VERSIONS[index % 3],
        "region": "US" if crashing else REGIONS[index % 5],
        "tier": ("free", "premium")[index % 2],
        "employee": False,
        "device_model": ANDROID_12_MODELS[index % 3],
    }


def crash_calls(plan: storyboard.CrashPlan) -> List[Call]:
    """Every request of the crash, in order: a pure function of the plan.

    ``plan.reports // CRASH_LOOP`` crashing devices (``config.CRASH_DEVICES``),
    each asking for the flag and crashing ``CRASH_LOOP`` times; every other ask
    is a device that asks once and does not crash. The crashing devices are
    spread among the others."""
    crashing = config.CRASH_DEVICES[: plan.reports // config.CRASH_LOOP]
    others = [
        _device(f"sp-a12-{n:03d}", n, crashing=False)
        for n in range(1, plan.evaluations - plan.reports + 1)
    ]
    block = len(others) // (len(crashing) + 1)
    calls: List[Call] = []
    waiting = iter(others)
    for index, ident in enumerate(crashing):
        for _ in range(block):
            attributes = next(waiting)
            calls.append(Call("evaluate", attributes["device_id"], attributes))
        attributes = _device(ident, index, crashing=True)
        for _ in range(config.CRASH_LOOP):
            calls.append(Call("evaluate", ident, attributes))
            calls.append(Call("report", ident, attributes))
    for attributes in waiting:
        calls.append(Call("evaluate", attributes["device_id"], attributes))
    return calls


def report_body(call: Call) -> Dict[str, Any]:
    """A crash report as the StreamPulse simulator sends it (POST /tracking/errors)."""
    a = call.attributes
    return {
        "feature_flag_key": config.CRASH_FLAG,
        "user_id": call.device,
        "error_type": "crash",
        "message": config.CRASH_MESSAGE,
        "metadata": {
            "os": a["os"],
            "os_version": a["os_version"],
            "device_model": a["device_model"],
            "app_version": a["app_version"],
        },
    }


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class CrashSender:
    """Sends the crash's requests from this process, to the stack's API only.

    Refused before any request: an address other than *allowed* (the stack's
    API origin; PE 7), or no server key read. A request answered other than
    2xx, or a crashing device answered "off", refuses at once. No message names
    either key.
    """

    def __init__(
        self,
        calls: Sequence[Call],
        *,
        api_url: str,
        allowed: str,
        app_key: str,
        server_key: Optional[str],
        rate: Optional[float] = config.CRASH_REQUESTS_PER_SECOND,
        session: Callable[[], Any] = traffic.direct_session,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ):
        if api_url.rstrip("/") != allowed.rstrip("/") or guard.reserved_in(api_url):
            raise CaptureFailed(
                "S4",
                f"the crash sender sends to the stack's API ({allowed}) and nowhere"
                f" else, not {api_url}",
            )
        if not server_key or server_key == app_key:
            raise CaptureFailed(
                "S4",
                "no StreamPulse server key was read: the crashes cannot be reported",
            )
        self.calls = list(calls)
        self.api_url = api_url.rstrip("/")
        self.app_key = app_key
        self.server_key = server_key
        self.pacer = traffic.Pacer(rate, clock=clock, sleep=sleep)
        self._session = session
        self._wall = wall

    def send(self) -> Dict[str, Any]:
        """Every call, in order; the numbers (counts and wall times), or raises."""
        client = self._session()
        flag = urllib.parse.quote(config.CRASH_FLAG)
        crashing = sorted({c.device for c in self.calls if c.kind == "report"})
        first: Optional[float] = None
        evaluations = reports = 0
        for call in self.calls:
            self.pacer.wait()
            if first is None:
                first = self._wall()
            try:
                if call.kind == "evaluate":
                    answer = client.get(
                        f"{self.api_url}/api/v1/feature-flags/evaluate/{flag}",
                        params={
                            "user_id": call.device,
                            "context": compact_json(call.attributes),
                        },
                        headers={"X-API-Key": self.app_key},
                        timeout=30,
                    )
                else:
                    answer = client.post(
                        f"{self.api_url}/api/v1/tracking/errors",
                        json=report_body(call),
                        headers={"X-API-Key": self.server_key},
                        timeout=30,
                    )
            except Exception as error:  # the type only: no request is quoted
                raise CaptureFailed(
                    "S4",
                    f"the crash sender's {call.kind} for {call.device} failed"
                    f" ({type(error).__name__})",
                ) from None
            if not 200 <= answer.status_code < 300:
                raise CaptureFailed(
                    "S4",
                    f"the crash sender's {call.kind} for {call.device} was answered"
                    f" {answer.status_code}",
                )
            if call.kind == "evaluate":
                evaluations += 1
                body = answer.json()
                on = bool(body.get("enabled")) and body.get("reason") == "rollout"
                if call.device in crashing and not on:
                    # Before its crash is reported: a device that never got
                    # the flag cannot crash in it.
                    raise CaptureFailed(
                        "S4",
                        f"{call.device} would crash in Player v2, and the API answered"
                        " it off: a device that never got the flag cannot crash in it",
                    )
            else:
                reports += 1
        return {
            "evaluations": evaluations,
            "reports": reports,
            "crashing_devices": len(crashing),
            "first_send": first,
            "last_answer": self._wall(),
            "requests_per_second": self.pacer.rate,
        }


def wait_for_anchor(
    api: Any,
    *,
    interval_s: float,
    wall: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = config.ANCHOR_POLL_S,
) -> Dict[str, Any]:
    """B9's anchor (T165 C5): the first successful safety run that was not in
    the history when the wait began. Refused after three intervals, and when
    the API's clock and the host's disagree by more than ``CLOCK_SKEW_MAX_S``."""
    path = f"/api/v1/scheduler/{config.SAFETY_SCHEDULER}/history?limit=20"
    before = {str(row.get("id")) for row in api.get(path)}
    began = wall()
    deadline = began + 3 * interval_s
    while True:
        rows = api.get(path)
        seen = wall()
        fresh = [
            row
            for row in rows
            if str(row.get("id")) not in before
            and row.get("status") == "success"
            and row.get("completed_at")
        ]
        if fresh:
            anchor = min(fresh, key=lambda row: parse_utc(row["completed_at"]))
            completed = parse_utc(anchor["completed_at"])
            lag = seen - completed
            numbers = {
                "run": str(anchor.get("id")),
                "completed_at": anchor["completed_at"],
                "runs_before": len(before),
                "waited_s": round(seen - began, 2),
                "lag_s": round(lag, 3),
            }
            if lag < -config.CLOCK_SKEW_MAX_S or lag > (
                poll_s + REQUEST_LATENCY_MAX_S + config.CLOCK_SKEW_MAX_S
            ):
                raise CaptureFailed(
                    "B9",
                    f"the safety run completed at {anchor['completed_at']} by the"
                    f" API's clock and was seen {lag:+.2f} s later by the host's:"
                    " the two clocks disagree, so B9 cannot be timed",
                )
            numbers["completed_at_s"] = completed
            return numbers
        if seen > deadline:
            raise CaptureFailed(
                "B9",
                f"no safety run completed within {3 * interval_s:.0f} s (three"
                " intervals) after the wait began",
            )
        sleep(poll_s)


def check_sends_after(anchor: Mapping[str, Any], sent: Mapping[str, Any]) -> float:
    """The sends began after the anchor run completed, and at most
    ``SEND_AFTER_TICK_MAX_S`` after it (T165 C5: within 5 s of its
    ``completed_at``); the delay, or refused. The first send is on the host's
    clock and the completion on the API's, which the anchor found at most
    ``CLOCK_SKEW_MAX_S`` apart: a send earlier than that began before the run
    completed (plan v2 C3: never on the run's start or its log line)."""
    delay = float(sent["first_send"]) - float(anchor["completed_at_s"])
    if delay < -config.CLOCK_SKEW_MAX_S:
        raise CaptureFailed(
            "B9",
            f"the crashes began {-delay:.1f} s before the safety run completed;"
            " they are sent after a completed run",
        )
    if delay > config.SEND_AFTER_TICK_MAX_S:
        raise CaptureFailed(
            "B9",
            f"the crashes began {delay:.1f} s after the safety run completed; at most"
            f" {config.SEND_AFTER_TICK_MAX_S:g} s",
        )
    return delay


def browser_evaluations(
    api: Any, flag_id: str, plan: storyboard.CrashPlan
) -> Dict[str, Any]:
    """After the sends, through the flag's safety check (which only evaluates):
    b, the browser's own evaluations of the flag in the window, at most
    ``BROWSER_EVALUATIONS_MAX``; the crashes counted are the ones sent; and the
    check reads unhealthy, its error rate above its limit. The numbers, or
    refused. The rate is the answer's ``error_rate`` metric, the number the
    flag page draws; the answer's ``details`` hold only the counts behind it."""
    check = api.get(f"/api/v1/safety/feature-flags/{flag_id}/check")
    details = check.get("details") or {}
    total = int(details.get("total_evaluations") or 0)
    errors = int(details.get("error_count") or 0)
    left_out = int(details.get("uncounted_error_count") or 0)
    browser = total - plan.evaluations
    metric = judge.error_rate_metric(check)
    rate = judge.safety_error_rate(check)
    numbers = {
        "total_evaluations": total,
        "browser_evaluations": browser,
        "crashes_counted": errors,
        "reports_left_out": left_out,
        "error_rate": rate,
        "is_healthy": check.get("is_healthy"),
    }
    if left_out:
        raise CaptureFailed(
            "S4",
            f"the API holds {left_out} error report(s) it leaves out of the flag's"
            " error rate; the video's are every one counted",
        )
    if browser < 0:
        raise CaptureFailed(
            "S4",
            f"the API counts {total} evaluations of the flag, fewer than the"
            f" {plan.evaluations} sent",
        )
    if browser > config.BROWSER_EVALUATIONS_MAX:
        raise CaptureFailed(
            "S4",
            f"browser evaluations {browser} > {config.BROWSER_EVALUATIONS_MAX}:"
            " the error rate on screen would not be the crash's",
        )
    if errors != plan.reports:
        raise CaptureFailed(
            "S4", f"the API counts {errors} crashes; {plan.reports} were sent"
        )
    if check.get("is_healthy") is not False:
        raise CaptureFailed(
            "S4", "after the crashes the flag's safety check still reads healthy"
        )
    if rate is None or metric is None or metric.get("is_healthy") is not False:
        # B9 says the error rate passes its limit: that metric, not another.
        raise CaptureFailed(
            "S4",
            "after the crashes the flag's safety check does not answer its error"
            f" rate above its limit (error_rate: {metric!r})",
        )
    return numbers


# ---------------------------------------------------------------------------
# wait_for_rollback, and the B9 gate
# ---------------------------------------------------------------------------


def wait_for_rollback(
    api: Any,
    wait: storyboard.RollbackWait,
    *,
    wall: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = config.ROLLBACK_POLL_S,
) -> Dict[str, Any]:
    """The safety monitor's own rollback of the flag to the percentage, read
    from the audit log and the flag; refused after ``within_s``, or at once
    when the rollback is anyone else's. Nothing here asks for one."""
    flag = flag_by_key(api, wait.flag)
    began = wall()
    while True:
        rolled = [
            e
            for e in flag_audit(api, str(flag["id"]))
            if e.get("action_type") == "safety_rollback"
        ]
        if rolled:
            entry = rolled[0]
            if entry.get("user_email") != "system:safety-monitor" or entry.get(
                "user_id"
            ):
                raise CaptureFailed(
                    "S4",
                    f"{wait.flag} was rolled back by {entry.get('user_email')!r}, not"
                    " by the safety monitor",
                )
            now = flag_by_key(api, wait.flag)
            if now.get("rollout_percentage") == wait.percentage:
                return {
                    "rollback_at": entry.get("timestamp"),
                    "rollback_at_s": parse_utc(entry.get("timestamp")),
                    "waited_s": round(wall() - began, 1),
                    "reason": entry.get("reason"),
                }
        if wall() - began > wait.within_s:
            raise CaptureFailed(
                "S4",
                f"the safety monitor did not roll {wait.flag} back to"
                f" {wait.percentage}% within {wait.within_s} s",
            )
        sleep(poll_s)


def rate_problem(
    page: Sequence[Optional[float]],
    counted: Optional[float],
    reason: Optional[str],
) -> Optional[str]:
    """Plan v2 C13, the error rate on screen is the API's: each rate B9's page
    got from the flag's safety check and drew, the rate the API counted right
    after the crashes, and the rate the safety monitor's rollback reason gives
    are one reading, as the platform writes it (``gates.rate_reading``); or
    why not. In video 4 nothing evaluates the flag from the crashes to the
    rollback: the StreamPulse page the crashes are sent under fetches only when
    a device is picked or Refresh now is pressed (it has no timer), the crash
    card replaces it, and the dashboard evaluates no flag; so in a good take
    the three are the same counts."""
    if counted is None:
        return "the crashes' error rate was never read from the API"
    wanted = judge.rate_reading(counted)
    drawn = [None if r is None else judge.rate_reading(r) for r in page]
    said = judge.reason_rate(reason)
    if not drawn or any(d != wanted for d in drawn) or said != wanted:
        return (
            f"B9's page drew the error rate as {drawn}, the API counted {wanted}"
            f" after the crashes, and the rollback's reason says {said}: the rate"
            " on screen is the API's and the reason's"
        )
    return None


def b9_problem(
    b9_end: float,
    rollback_at: float,
    lag_s: float,
    poll_s: float = config.ANCHOR_POLL_S,
) -> Optional[str]:
    """The B9 gate: the rollback came after B9's last frame (T165 C5), or why not.

    *b9_end* is the host's wall time when B9's recording ended (its last frame
    is held to there); *rollback_at* the API's time of the rollback. The API's
    time is put on the host's clock with the lag measured at the anchor, at its
    earliest: the lag less the longest it could have taken to see the run."""
    earliest = rollback_at + lag_s - poll_s - REQUEST_LATENCY_MAX_S
    if earliest <= b9_end:
        return (
            f"the safety monitor rolled back at {earliest - b9_end:+.1f} s from B9's"
            " last frame: B9 would film a page the monitor had already changed"
        )
    return None


# ---------------------------------------------------------------------------
# S5: the card after the wait
# ---------------------------------------------------------------------------

_MINUTES = re.compile(r"\b([0-9]+) minutes?\b")


def card_gap_problem(card_text: str, gap_s: float) -> Optional[str]:
    """U3: the minutes the card says are within ``CARD_GAP_TOLERANCE_S`` of the
    gap its cut hid, measured on the wall clock; or why not."""
    found = _MINUTES.findall(card_text)
    if len(found) != 1:
        return f"the time card says {len(found)} durations, not the one measured"
    said = int(found[0]) * 60
    if abs(said - gap_s) > CARD_GAP_TOLERANCE_S:
        return (
            f"the time card says {found[0]} minute(s); the cut hid {gap_s:.0f} s"
            f" (at most {CARD_GAP_TOLERANCE_S} s apart)"
        )
    return None


# ---------------------------------------------------------------------------
# B9's fit, before anything starts (T165 C5)
# ---------------------------------------------------------------------------


def b9_fit_ms(
    board: storyboard.Storyboard,
    samples: Optional[Mapping[Any, int]] = None,
) -> Optional[int]:
    """From the first crash sent to B9's last frame, as estimated before the
    run: the sends at the sender's pace, then each scene from the crash card
    to B9, held as the capture holds it (``voiced_estimate_ms`` with the
    clips, ``estimate_ms`` silent), with what each costs off camera. None for
    a storyboard without the incident."""
    at = storyboard.incident_at(board)
    if "crashes" not in at or "wait_for_rollback" not in at:
        return None
    plan = next(
        item.crashes
        for item in board.scenes[at["crashes"]].off_camera
        if isinstance(item, storyboard.Crashes)
    )
    calls = len(crash_calls(plan))
    total = int(1000 * calls / config.CRASH_REQUESTS_PER_SECOND)
    for scene in board.scenes[at["crashes"] : at["wait_for_rollback"]]:
        part = board.model_copy(update={"scenes": [scene]})
        held = (
            storyboard.voiced_estimate_ms(part, samples)
            if samples is not None
            else storyboard.estimate_ms(part)
        )
        overhead = (
            config.CARD_OVERHEAD_MS
            if scene.card is not None
            else config.PAGE_OVERHEAD_MS
        )
        total += held + overhead
    return total


def b9_fit_problem(fit_ms: Optional[int], interval_s: float) -> Optional[str]:
    """Refused unless at least ``config.B9_SPARE_S`` of the interval remains
    after B9's last frame (T165 C5)."""
    if fit_ms is None:
        return None
    limit = (interval_s - config.B9_SPARE_S) * 1000
    if fit_ms > limit:
        return (
            f"from the first crash to B9's last frame takes about {fit_ms / 1000:.1f} s;"
            f" the safety monitor runs every {interval_s:.0f} s and B9 must end"
            f" {config.B9_SPARE_S} s before it can act (at most {limit / 1000:.0f} s)"
        )
    return None


# ---------------------------------------------------------------------------
# C6: the stated interval, read back from the API's own log
# ---------------------------------------------------------------------------


def log_lines(text: str, wanted: str) -> List[str]:
    """The lines of the API's log that hold *wanted*, stripped (C6 keeps only these)."""
    return [line.strip() for line in text.splitlines() if wanted in line]


def rollback_log_counts(text: str, key: str) -> Dict[str, int]:
    """How many rollbacks of the flag the API's log holds, and of which kind
    (``SafetyService.execute_rollback`` names its trigger; the safety
    scheduler says when it rolled a flag back)."""
    return {
        "rolled_back": len(
            log_lines(text, f"Successfully rolled back feature flag {key}")
        ),
        "automatic": len(log_lines(text, "(trigger: automatic)")),
        "manual": len(log_lines(text, "(trigger: manual)")),
    }


def minutes_text(seconds: float) -> str:
    """``12 minutes``: the measured time, to the nearest minute (at least 1).
    The traffic's card and the card after the wait say minutes this way."""
    minutes = max(1, int(round(seconds / 60)))
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


# ---------------------------------------------------------------------------
# The incident across the video's scenes
# ---------------------------------------------------------------------------


class Incident:
    """Video 4's incident across its scenes: each off-camera step when its
    scene comes (``step``), S5's card filled and checked, the agreement read
    at its beat, and what each found, for the B9 gate, gate 10/4 and the
    manifest (``numbers``)."""

    def __init__(
        self,
        board: storyboard.Storyboard,
        *,
        api: Any,
        stack: Any,
        keys: List[str],
        interval_s: float,
        log: Callable[[str], None] = lambda line: None,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        session: Callable[[], Any] = traffic.direct_session,
    ):
        at = storyboard.incident_at(board)
        end = board.end_state.rollback
        if end is None or "wait_for_rollback" not in at:
            raise CaptureFailed("S4", f"{board.slug} has no incident")
        self.board = board
        self.end = end
        self.api = api
        self.stack = stack
        #: The values the run read from a file: the StreamPulse server key, once
        #: its seed has run (``run.keep_private``).
        self.keys = keys
        self.interval_s = interval_s
        self.log = log
        self.wall = wall
        self.sleep = sleep
        self.session = session
        b9 = storyboard.b9_scene(board)
        assert b9 is not None
        self.b9 = b9.id
        self.card = board.scenes[at["wait_for_rollback"]].id
        self.anchor: Optional[Dict[str, Any]] = None
        self.agreement_api: Optional[Dict[str, Any]] = None
        self.card_said: Optional[str] = None
        self.numbers: Dict[str, Any] = {"interval_s": interval_s}

    def step(self, item: Any, director: Any) -> None:
        """One off-camera step, before its scene."""
        if isinstance(item, storyboard.Story):
            self.story()
        elif isinstance(item, storyboard.Crashes):
            self.crashes(item.crashes)
        elif isinstance(item, storyboard.WaitForRollback):
            self.wait(item.wait_for_rollback, director)

    def story(self) -> None:
        self.log("story: the rollout script's step 2, off camera")
        run_story(
            tree=self.stack.tree,
            children=self.stack.children,
            logs=self.stack.logs,
            parent=self.stack.parent,
            api_url=self.stack.api_url,
            dashboard_url=self.stack.dashboard_url,
            app_url=self.stack.app_url("streampulse"),
            ports=self.stack.ports,
        )
        self.numbers["story"] = story_done(self.api, self.end.key)

    def crashes(self, plan: storyboard.CrashPlan) -> None:
        server_key = self.keys[0] if len(self.keys) == 1 else None
        sender = CrashSender(
            crash_calls(plan),
            api_url=self.stack.api_url,
            allowed=self.stack.api_url,
            app_key=config.APP_STATIC_KEY["streampulse"],
            server_key=server_key,
            session=self.session,
            sleep=self.sleep,
            wall=self.wall,
        )
        self.log("crashes: waiting for a safety run to complete (B9's anchor)")
        self.anchor = wait_for_anchor(
            self.api, interval_s=self.interval_s, wall=self.wall, sleep=self.sleep
        )
        sent = sender.send()
        delay = check_sends_after(self.anchor, sent)
        flag = flag_by_key(self.api, self.end.key)
        counted = browser_evaluations(self.api, str(flag["id"]), plan)
        self.log(
            f"crashes: {sent['evaluations']} asks and {sent['reports']} crash reports"
            f" sent; browser evaluations {counted['browser_evaluations']}"
        )
        self.numbers["crashes"] = {**sent, **counted, "send_delay_s": round(delay, 2)}
        self.numbers["anchor"] = {
            k: v for k, v in self.anchor.items() if k != "completed_at_s"
        }

    def wait(self, wait: storyboard.RollbackWait, director: Any) -> None:
        if self.anchor is None:
            raise CaptureFailed(
                "B9", "the wait for the rollback came before the crashes"
            )
        found = wait_for_rollback(self.api, wait, wall=self.wall, sleep=self.sleep)
        b9_end = director.scene_walls[self.b9]["end"] / 1000.0
        checks = [
            c for c in director.tally.safety_checks if in_scene(c["where"], self.b9)
        ]
        self.numbers["b9"] = {
            "scene": self.b9,
            "rollback_at": found["rollback_at"],
            "waited_s": found["waited_s"],
            "rollback_after_b9_s": round(
                found["rollback_at_s"] + float(self.anchor["lag_s"]) - b9_end, 2
            ),
            "page_checks": [c["is_healthy"] for c in checks],
        }
        if not checks or any(c["is_healthy"] is not False for c in checks):
            raise CaptureFailed(
                "B9",
                f"B9's page asked for the flag's safety check {len(checks)} time(s),"
                f" answered {[c['is_healthy'] for c in checks]}: it must show it unhealthy",
            )
        page = [c.get("error_rate") for c in checks]
        counted = (self.numbers.get("crashes") or {}).get("error_rate")
        self.numbers["b9"]["rates"] = {
            "page": [None if r is None else judge.rate_reading(r) for r in page],
            "counted": None if counted is None else judge.rate_reading(counted),
            "reason": judge.reason_rate(found["reason"]),
        }
        problem = rate_problem(page, counted, found["reason"])
        if problem:
            raise CaptureFailed("B9", problem)
        problem = b9_problem(
            b9_end, found["rollback_at_s"], float(self.anchor["lag_s"])
        )
        if problem:
            raise CaptureFailed("B9", problem)
        self.numbers["rollback"] = {"reason": found["reason"]}

    def with_minutes(self, scene: storyboard.Scene, director: Any) -> storyboard.Scene:
        """S5: the card after the wait, its ``{rollback_minutes}`` the wall-clock
        gap from B9's last frame to now (the card is drawn next)."""
        if scene.id != self.card or scene.card is None:
            return scene
        gap = self.wall() - director.scene_walls[self.b9]["end"] / 1000.0
        said = minutes_text(gap)
        card = scene.card
        update = {
            name: getattr(card, name).replace(storyboard.ROLLBACK_MINUTES, said)
            for name in ("headline", "note")
            if getattr(card, name)
        }
        filled = card.model_copy(update=update)
        # U3 reads back what the card says the gap was: the text that held the
        # placeholder. The rest of the card has durations of its own (video
        # 4's note states the interval's default, "every 5 minutes").
        self.card_said = " ".join(
            update[name]
            for name in ("headline", "note")
            if storyboard.ROLLBACK_MINUTES in (getattr(card, name) or "")
        )
        self.numbers["card"] = {"said": said, "gap_at_fill_s": round(gap, 1)}
        return scene.model_copy(update={"card": filled})

    def after_scene(self, scene: storyboard.Scene, director: Any) -> None:
        """U3, after the card is filmed: the minutes it said are within 30 s of
        the gap from B9's last frame to the card's first."""
        if scene.id != self.card:
            return
        walls = director.scene_walls
        gap = (walls[self.card]["start"] - walls[self.b9]["end"]) / 1000.0
        self.numbers.setdefault("card", {})["gap_s"] = round(gap, 1)
        problem = card_gap_problem(self.card_said or "", gap)
        if problem:
            raise CaptureFailed("U3", problem)

    def at_cue(self, scene_id: str, index: int) -> None:
        """At the agreement's beat: the API's answer for the device, read off
        camera with the app's key and the context the app sends."""
        agree = self.end.agree
        if (scene_id, index) != (agree.scene, agree.beat):
            return
        attributes = config.STREAMPULSE_DEVICES[agree.device]
        query = urllib.parse.urlencode(
            {"user_id": agree.device, "context": compact_json(attributes)}
        )
        self.agreement_api = self.api.get_with_key(
            f"/api/v1/feature-flags/evaluate/{urllib.parse.quote(self.end.key)}?{query}",
            config.APP_STATIC_KEY["streampulse"],
        )

    def end_state(
        self,
        readings: Mapping[str, str],
        rollback_posts: Sequence[str],
        log_counts: Mapping[str, int],
    ) -> Any:
        """Gate 10/4's half: the API's answers, judged (``gates.rollback_end``)."""
        end = self.end

        def named(name: str) -> List[Dict[str, Any]]:
            listed = (
                self.api.get(
                    f"/api/v1/feature-flags/?search={urllib.parse.quote(name)}&limit=100"
                ).get("items")
                or []
            )
            return [item for item in listed if item.get("name") == name]

        flags = named(end.name)
        kills = named(end.kill_switch.name)
        schedules: List[Dict[str, Any]] = []
        audit: List[Dict[str, Any]] = []
        if len(flags) == 1:
            schedules = list(
                self.api.get(
                    f"/api/v1/rollout-schedules/?feature_flag_id={flags[0]['id']}&limit=100"
                ).get("items")
                or []
            )
            audit = flag_audit(self.api, str(flags[0]["id"]))
        kill_audit = (
            flag_audit(self.api, str(kills[0]["id"])) if len(kills) == 1 else []
        )
        me = self.api.get("/api/v1/auth/me")
        return judge.rollback_end(
            end,
            named=flags,
            schedules=schedules,
            audit=audit,
            actor={"email": me.get("email"), "id": me.get("id")},
            kill_named=kills,
            kill_audit=kill_audit,
            readings=readings,
            api_at_agreement=self.agreement_api,
            rollback_posts=rollback_posts,
            log=log_counts,
        )
