"""A storyboard's simulated users, sent through the tracking API off camera.

Video 2 creates an experiment on camera, and then its users arrive. They are
``backend.tests.realistic.data_generator``'s (#1064) at the storyboard's seed:
the same user ids, and the same draw for whether each one converts, on every
run. ``plan`` predicts what the platform will count (the generator plans the
split with the server's own assignment hash, on the experiment's key);
``Sender`` sends them as the JavaScript SDK would:

* ``POST /api/v1/tracking/assign`` for each user, with the experiment's key;
* for a user who converts in the variant the server answered,
  ``POST /api/v1/tracking/track`` with the metric's event as both
  ``event_type`` and ``event_name`` (``sdk/js/src/client.ts``), and the key.

The report counts what the server answered, never what was sent: users the
server assigned, and those of them who converted, per variant. Gate 10 then
holds the plan, the report and the results API to the same numbers.

On the Quick Start stack the API keeps its default SDK limit (6,000 requests a
minute), so the sender is paced (``rate``: requests a second, every worker
together; EM C8 of T166). A 429 is honoured: the request waits for its
``Retry-After`` and is sent again, and every 429 is counted. The report says
how long the users took (``wall_seconds``) and how many 429s came back, and
any 429 refuses the video: the pace is set so that none does.

The generator is imported from this checkout, the tool's own, never from an
editable install of another one (``generator``).
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from showcase.capture import storyboard

#: The checkout this tool runs from: its ``backend/tests/realistic`` is the generator.
REPO_ROOT = Path(__file__).resolve().parents[4]
GENERATOR = "backend.tests.realistic.data_generator"


class TrafficFailed(RuntimeError):
    pass


def generator() -> Any:
    """The data generator module, from ``REPO_ROOT``; refused from anywhere else."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    module = importlib.import_module(GENERATOR)
    where = Path(module.__file__).resolve()
    if REPO_ROOT not in where.parents:
        raise TrafficFailed(
            f"the data generator imports from {where}, not from this checkout"
            f" ({REPO_ROOT}): an editable install of another checkout comes first"
        )
    return module


@dataclass(frozen=True)
class Plan:
    """Who arrives, in order, and what the platform must count when they have."""

    key: str
    event: str
    users: List[Any]
    #: ``events_for(user, "control" | "treatment")``: the user's events in that arm.
    events_for: Callable[[Any, str], List[Any]]
    #: Users and converting users per arm, as the server's hash splits them.
    split: Dict[str, Dict[str, int]]
    #: Fisher's exact two-sided p-value on ``split``: the results API's test.
    p_value: Optional[float]


def plan(traffic: storyboard.Traffic, key: str) -> Plan:
    """The storyboard's users for the experiment *key*; a pure function of both."""
    made = generator()
    scenario = made.DataScenario(
        name=key,
        users=traffic.users,
        control_cvr=traffic.control_rate,
        treatment_cvr=traffic.treatment_rate,
        # Every user converts at their arm's rate, no more: no outliers (who
        # would convert at five times it), no weekday or novelty effects.
        outlier_rate=0.0,
        seed=traffic.seed,
    )
    result = scenario.generate(experiment_key=key)
    if made.CONVERSION_EVENT != traffic.event:
        raise TrafficFailed(
            f"the generator's users convert by {made.CONVERSION_EVENT!r}; the"
            f" storyboard's metric counts {traffic.event!r}"
        )
    split = result.split()
    return Plan(
        key=key,
        event=traffic.event,
        users=list(result.users),
        events_for=scenario.events_for,
        split=split,
        p_value=made.fisher_p_value(split),
    )


@dataclass
class Report:
    """What the server answered, per arm (``control`` and ``treatment``)."""

    sent: int = 0
    #: Requests sent, and 429 answers (each honoured, then sent again).
    requests: int = 0
    rate_limited: int = 0
    #: From the first request to the last answer, and the pace asked for.
    wall_seconds: float = 0.0
    rate: Optional[float] = None
    not_assigned: int = 0
    #: The server's variant differed from the plan's (0 on a clean run).
    mismatches: int = 0
    events: int = 0
    by_arm: Dict[str, Dict[str, int]] = field(default_factory=dict)
    #: The variant's name as the server gave it, per arm.
    names: Dict[str, str] = field(default_factory=dict)

    def as_numbers(self) -> Dict[str, Any]:
        return {
            "sent": self.sent,
            "requests": self.requests,
            "rate_limited": self.rate_limited,
            "wall_seconds": round(self.wall_seconds, 1),
            "requests_per_second": self.rate,
            "not_assigned": self.not_assigned,
            "mismatches": self.mismatches,
            "events": self.events,
            "by_arm": self.by_arm,
            "names": self.names,
        }


def direct_session() -> Any:
    """A ``requests`` session that takes no proxy settings from the environment.

    By default a session sends through ``HTTP_PROXY`` or ``ALL_PROXY`` when the
    caller set one; the users go to the tool's own API only, directly.
    """
    import requests

    session = requests.Session()
    session.trust_env = False
    return session


class Pacer:
    """At most *rate* requests a second, every thread together: each takes the
    next slot, ``1 / rate`` s after the one before."""

    def __init__(self, rate: Optional[float], clock=time.monotonic, sleep=time.sleep):
        if rate is not None and not rate > 0:
            raise ValueError(f"a pace of {rate!r} requests a second")
        self.rate = rate
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.rate is None:
            return
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + 1.0 / self.rate
        if slot > now:
            self._sleep(slot - now)


#: How often one request is sent again after a 429, and the longest wait.
RETRIES_429 = 5
RETRY_AFTER_MAX_S = 60.0


def retry_after(headers: Any) -> float:
    """``Retry-After`` in seconds (1 when absent or not a number), at most a minute."""
    try:
        value = float((headers or {}).get("Retry-After", 1))
    except (TypeError, ValueError):
        value = 1.0
    return min(max(value, 0.0), RETRY_AFTER_MAX_S)


class Sender:
    """Sends a plan's users from ``workers`` threads; ``finish`` waits for the last.

    Any answer but a 2xx stops every thread, and ``finish`` (or ``wait_for``)
    raises with the call and its status. The API key is never in a message.
    """

    def __init__(
        self,
        made: Plan,
        *,
        api_url: str,
        api_key: str,
        workers: int,
        session: Optional[Callable[[], Any]] = None,
        rate: Optional[float] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.plan = made
        self.pacer = Pacer(rate, clock=clock, sleep=sleep)
        self._clock = clock
        self._sleep = sleep
        self._began: Optional[float] = None
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.workers = workers
        self._session = session if session is not None else direct_session
        self.report = Report(rate=rate)
        self.failure: Optional[str] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    # -- sending ---------------------------------------------------------------

    def _post(self, client: Any, what: str, path: str, body: Dict[str, Any]) -> Any:
        for _attempt in range(RETRIES_429 + 1):
            self.pacer.wait()
            answer = client.post(
                self.api_url + path,
                json=body,
                headers={"X-API-Key": self.api_key},
                timeout=60,
            )
            with self._lock:
                self.report.requests += 1
                if answer.status_code == 429:
                    self.report.rate_limited += 1
            # Unpaced (the uvicorn stack's raised limit), a 429 stops the
            # traffic at once, like any other refusal.
            if answer.status_code != 429 or self.pacer.rate is None:
                break
            self._sleep(retry_after(getattr(answer, "headers", None)))
        if not 200 <= answer.status_code < 300:
            raise TrafficFailed(f"{what}: POST {path} answered {answer.status_code}")
        return answer.json()

    def _one(self, client: Any, user: Any) -> None:
        key = self.plan.key
        assigned = self._post(
            client,
            f"assign {user.user_id}",
            "/api/v1/tracking/assign",
            {"experiment_key": key, "user_id": user.user_id},
        )
        if not isinstance(assigned, dict) or "is_control" not in assigned:
            raise TrafficFailed(f"assign {user.user_id}: no variant in the answer")
        if not assigned.get("assigned", False):
            with self._lock:
                self.report.sent += 1
                self.report.not_assigned += 1
            return
        arm = "control" if assigned["is_control"] else "treatment"
        events = self.plan.events_for(user, arm)
        for event in events:
            stored = self._post(
                client,
                f"track {user.user_id}",
                "/api/v1/tracking/track",
                {
                    "event_type": event.event_name,
                    "event_name": event.event_name,
                    "user_id": user.user_id,
                    "experiment_key": key,
                    "value": event.value,
                },
            )
            if not isinstance(stored, dict) or not stored.get("id"):
                raise TrafficFailed(f"track {user.user_id}: no stored event")
        with self._lock:
            report = self.report
            report.sent += 1
            report.events += len(events)
            report.mismatches += arm != user.variant_name
            report.names.setdefault(arm, str(assigned.get("variant_name")))
            counts = report.by_arm.setdefault(arm, {"users": 0, "converting_users": 0})
            counts["users"] += 1
            counts["converting_users"] += any(
                e.event_name == self.plan.event for e in events
            )

    def _work(self, index: int) -> None:
        client = self._session()
        try:
            for user in self.plan.users[index :: self.workers]:
                if self._stop.is_set():
                    return
                self._one(client, user)
        except Exception as error:  # the first failure stops every thread
            with self._lock:
                if self.failure is None:
                    self.failure = str(error) or type(error).__name__
            self._stop.set()

    def start(self) -> None:
        self._began = self._clock()
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._work, args=(index,), daemon=True, name=f"traffic-{index}"
            )
            self._threads.append(thread)
            thread.start()

    # -- waiting ---------------------------------------------------------------

    def sent(self) -> int:
        with self._lock:
            return self.report.sent

    def _raise_failure(self) -> None:
        if self.failure is not None:
            raise TrafficFailed(f"the traffic stopped: {self.failure}")

    def wait_for(self, count: int, timeout: float) -> None:
        """Until *count* users are in; raises on a failure or after *timeout* s."""
        deadline = time.monotonic() + timeout
        while self.sent() < count:
            self._raise_failure()
            if time.monotonic() > deadline:
                raise TrafficFailed(
                    f"{self.sent()} of {count} users in after {timeout:.0f} s"
                )
            time.sleep(0.1)
        self._raise_failure()

    def finish(
        self, timeout: float, progress: Optional[Callable[[int], None]] = None
    ) -> Report:
        """Wait for every user; the report, or raises."""
        deadline = time.monotonic() + timeout
        last = time.monotonic()
        for thread in self._threads:
            while thread.is_alive():
                thread.join(0.5)
                if time.monotonic() > deadline:
                    self.stop()
                    raise TrafficFailed(
                        f"{self.sent()} of {len(self.plan.users)} users in after"
                        f" {timeout:.0f} s"
                    )
                if progress is not None and time.monotonic() - last >= 10:
                    progress(self.sent())
                    last = time.monotonic()
        if self._began is not None:
            self.report.wall_seconds = self._clock() - self._began
        self._raise_failure()
        if self.report.sent != len(self.plan.users):
            raise TrafficFailed(
                f"{self.report.sent} of {len(self.plan.users)} users were sent"
            )
        if self.report.rate_limited:
            raise TrafficFailed(
                f"EM C8: the API answered 429 {self.report.rate_limited} times;"
                f" the traffic is paced at {self.report.rate} requests a second"
                " so that it never does"
            )
        return self.report

    def stop(self) -> None:
        """Stop sending (the stack is coming down); returns once the threads have."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=70)
