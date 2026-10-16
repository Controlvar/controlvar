"""Local-evaluation runtime: fetches and refreshes the ruleset, answers flags from it, and
reports how many evaluations it answered so the server's safety monitoring keeps its
denominator.

Failure handling (after the first successful load the SDK "has a ruleset"):

=========================================  ============================  ==========================================
Ruleset response                           Without a ruleset             With a ruleset
=========================================  ============================  ==========================================
200 / 304                                  load                          swap, or keep
5xx, network error, timeout                stay on the server; back off  keep serving it; ``on_error``; back off
200 that is not a valid ruleset            stay on the server            keep serving it; ``on_error``; back off
429                                        wait ``Retry-After``          keep serving it; wait ``Retry-After``
401 / 403                                  stay on the server            DISCARD it; every flag goes to the server
404, or an unknown ``schema``/``bucketing``  stay on the server          DISCARD it; every flag goes to the server
=========================================  ============================  ==========================================

401/403/404 and an unknown format are reported once (until a refresh succeeds) and retried every
10 minutes. "Goes to the server" means the call is made exactly as in server mode.

Only the regular refresh interval is jittered (+/-10%). A backoff step, the 5-minute backoff cap,
a ``Retry-After`` wait and the 10-minute retry are exact: ``Retry-After`` is a floor and the caps
are ceilings.

Threads and processes: one daemon thread per client refreshes the ruleset and sends the counts.
All state is guarded by one lock. After ``os.fork()`` the child gets fresh locks, drops the
counts it inherited (the parent reports those), and starts its own thread
(``os.register_at_fork``, plus a PID check on every call for the cases that hook misses).
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import random
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .evaluator import (
    DEFER,
    IndexedRuleset,
    evaluate_locally,
    index_ruleset,
    is_supported_format,
    to_wire_context,
)
from .transport import Response
from .types import ExperimentationError

__all__ = [
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "EVALUATIONS_PATH",
    "FLUSH_INTERVAL_SECONDS",
    "LocalEvaluation",
    "LocalEvaluationStatus",
    "MAX_BACKOFF_SECONDS",
    "MIN_REFRESH_INTERVAL_SECONDS",
    "MIN_SERVER_RELEASE",
    "REFUSED_RETRY_SECONDS",
    "RULESET_PATH",
    "ReadyResult",
]

logger = logging.getLogger("experimentation")

RULESET_PATH = "/api/v1/sdk/ruleset"
EVALUATIONS_PATH = "/api/v1/tracking/evaluations"
#: The first server release that serves the ruleset and accepts evaluation counts.
MIN_SERVER_RELEASE = "0.11.0"

DEFAULT_REFRESH_INTERVAL_SECONDS = 30.0
MIN_REFRESH_INTERVAL_SECONDS = 5.0
#: Longest wait between refreshes while the server is failing.
MAX_BACKOFF_SECONDS = 300.0
#: Wait between refreshes after 401, 403, 404 or an unknown ruleset format.
REFUSED_RETRY_SECONDS = 600.0
#: How often evaluation counts are sent.
FLUSH_INTERVAL_SECONDS = 60.0

#: Server limits of ``POST /api/v1/tracking/evaluations``.
_MAX_REPORT_ENTRIES = 1000
_MAX_ENTRY_COUNT = 1_000_000
_MAX_WINDOW_SECONDS = 600.0

#: ``send(method, path, headers, body) -> Response``; raises on a transport failure.
SendFn = Callable[[str, str, Mapping[str, str], Optional[bytes]], Response]
#: ``report(error, operation)`` with operation ``"refresh"``, ``"flush"`` or ``"evaluate"``.
ReportFn = Callable[[ExperimentationError, str], None]


@dataclass(frozen=True)
class ReadyResult:
    """``ready()``'s answer; truthy when flags can be answered locally. ``ready()`` never raises."""

    ok: bool
    ruleset_version: Optional[str] = None
    error: Optional[ExperimentationError] = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class LocalEvaluationStatus:
    """Where flags are being evaluated, and the state of the ruleset."""

    #: The configured mode: ``"server"`` or ``"local"``.
    evaluation: str
    #: Whether flags are being answered locally right now (always ``True`` in server mode).
    ready: bool
    #: The version of the ruleset in use, or ``None``.
    ruleset_version: Optional[str] = None
    #: When the ruleset was last fetched or confirmed unchanged (UTC), or ``None``.
    last_refresh_at: Optional[datetime] = None
    #: The last refresh failure, cleared by a successful refresh.
    last_error: Optional[ExperimentationError] = None
    #: Active flags the server evaluates even in local mode (their rules are not portable).
    server_evaluated_flags: List[str] = field(default_factory=list)


def _retry_after(response: Response) -> Optional[float]:
    header = response.header("Retry-After")
    if not header:
        return None
    try:
        return max(float(header.strip()), 0.0)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


def _detail(response: Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text()[:200]
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return response.text()[:200]


def _http_error(response: Response, path: str) -> ExperimentationError:
    return ExperimentationError(
        f"GET {path} failed with HTTP {response.status}", status=response.status, body=response.text()
    )


# Every live runtime, so the fork hook can reach them without keeping them alive.
_RUNTIMES: "weakref.WeakSet[LocalEvaluation]" = weakref.WeakSet()


def _after_fork_in_child() -> None:
    for runtime in list(_RUNTIMES):
        runtime._reinitialise_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


class LocalEvaluation:
    """The local-evaluation state of one client."""

    def __init__(
        self,
        send: SendFn,
        report: ReportFn,
        refresh_interval_seconds: float,
        max_stale_seconds: Optional[float] = None,
        json_default: Optional[Callable[[Any], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        start: bool = True,
    ) -> None:
        self._send = send
        self._report = report
        self._interval = float(refresh_interval_seconds)
        self._max_stale = max_stale_seconds
        self._json_default = json_default
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._first_attempt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pid = os.getpid()
        self._closed = False

        self._ruleset: Optional[IndexedRuleset] = None
        self._etag: Optional[str] = None
        self._last_refresh_monotonic: Optional[float] = None
        self._last_refresh_at: Optional[datetime] = None
        self._last_error: Optional[ExperimentationError] = None
        self._failures = 0
        self._refusal: Optional[str] = None
        #: Whether the delay the last refresh returned is the regular interval (jittered).
        self._jitter_next = False
        self._tallies: Dict[str, List[int]] = {}
        self._window_start: Optional[datetime] = None
        self._flush_lock = threading.Lock()

        _RUNTIMES.add(self)
        if start:
            self._start_thread()

    # ----------------------------------------------------------------- threads

    def _start_thread(self) -> None:
        thread = threading.Thread(target=self._run, name="controlvar-local-evaluation", daemon=True)
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        next_refresh = self._clock()
        next_flush = self._clock() + FLUSH_INTERVAL_SECONDS
        while not self._stop.is_set():
            now = self._clock()
            if now >= next_refresh:
                delay = self.refresh_once()
                if self._jitter_next:
                    delay *= random.uniform(0.9, 1.1)
                next_refresh = self._clock() + delay
            if now >= next_flush:
                self.flush()
                next_flush = self._clock() + FLUSH_INTERVAL_SECONDS
            self._stop.wait(max(0.0, min(next_refresh, next_flush) - self._clock()))

    def _reinitialise_after_fork(self) -> None:
        """In a forked child: new locks, no inherited counts, and a thread of its own."""
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        first_attempt_done = self._first_attempt.is_set()
        self._first_attempt = threading.Event()
        if first_attempt_done:
            self._first_attempt.set()
        # The parent reports what it counted before the fork; the child reports its own.
        self._tallies = {}
        self._window_start = None
        self._pid = os.getpid()
        self._thread = None
        if not self._closed:
            self._start_thread()

    def _check_pid(self) -> None:
        if os.getpid() != self._pid:
            self._reinitialise_after_fork()

    # ----------------------------------------------------------------- answers

    def _usable(self) -> Optional[IndexedRuleset]:
        if self._closed or self._ruleset is None:
            return None
        if self._max_stale is not None:
            if self._last_refresh_monotonic is None or self._clock() - self._last_refresh_monotonic > self._max_stale:
                return None
        return self._ruleset

    def answer(self, flag_key: str, user_id: str, attributes: Optional[Mapping[str, Any]]) -> Any:
        """One flag, answered locally and counted, or :data:`DEFER`."""
        self._check_pid()
        ruleset = self._usable()
        if ruleset is None:
            return DEFER
        try:
            context = to_wire_context(attributes, self._json_default)
            if context is DEFER:
                return DEFER
            answer = evaluate_locally(ruleset, flag_key, user_id, context)
        except Exception as exc:  # noqa: BLE001 - never a wrong answer: ask the server
            self._evaluation_failed(exc)
            return DEFER
        if answer is not DEFER:
            self._count({flag_key: answer["enabled"]})
        return answer

    def answer_all(self, user_id: str, attributes: Optional[Mapping[str, Any]]) -> Any:
        """Every active flag (inactive ones omitted, as the server does), or :data:`DEFER` when
        any active flag cannot be answered locally for these attributes."""
        self._check_pid()
        ruleset = self._usable()
        if ruleset is None:
            return DEFER
        try:
            context = to_wire_context(attributes, self._json_default)
            if context is DEFER:
                return DEFER
            flags: Dict[str, bool] = {}
            for key, flag in ruleset.flags.items():
                if not flag["active"]:
                    continue
                answer = evaluate_locally(ruleset, key, user_id, context)
                if answer is DEFER:
                    return DEFER
                flags[key] = answer["enabled"]
        except Exception as exc:  # noqa: BLE001
            self._evaluation_failed(exc)
            return DEFER
        self._count(flags)
        return flags

    def _evaluation_failed(self, exc: Exception) -> None:
        """A bug in local evaluation: reported as ``"evaluate"``; the call still goes to the server."""
        self._report(
            ExperimentationError(f"Local evaluation failed; asking the server instead: {exc!r}"),
            "evaluate",
        )

    # ----------------------------------------------------------------- lifecycle

    def ready(self, timeout_seconds: Optional[float] = None) -> ReadyResult:
        self._check_pid()
        if self._usable() is None and not self._closed:
            self._first_attempt.wait(timeout_seconds)
        ruleset = self._usable()
        if ruleset is not None:
            return ReadyResult(True, ruleset.version)
        error = self._last_error or ExperimentationError(
            "The client is closed" if self._closed else "No flag ruleset has been loaded yet"
        )
        return ReadyResult(False, None, error)

    def status(self) -> LocalEvaluationStatus:
        self._check_pid()
        ruleset = self._ruleset
        remote = sorted(
            key
            for key, flag in (ruleset.flags.items() if ruleset is not None else [])
            if flag["active"] and flag.get("evaluation") != "local"
        )
        return LocalEvaluationStatus(
            evaluation="local",
            ready=self._usable() is not None,
            ruleset_version=ruleset.version if ruleset is not None else None,
            last_refresh_at=self._last_refresh_at,
            last_error=self._last_error,
            server_evaluated_flags=remote,
        )

    def close(self) -> None:
        """Stop refreshing, send the remaining counts, and answer every later call on the server."""
        self._check_pid()
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._first_attempt.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5.0)
        self.flush()

    # ----------------------------------------------------------------- refresh

    def refresh_once(self) -> float:
        """One refresh. Returns the delay before the next one; never raises.

        Only a success returns the regular interval, which the thread jitters; every other delay
        is exact.
        """
        self._jitter_next = False
        try:
            return self._refresh()
        finally:
            self._first_attempt.set()

    def _refresh(self) -> float:
        headers: Dict[str, str] = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        try:
            response = self._send("GET", RULESET_PATH, headers, None)
        except Exception as exc:  # noqa: BLE001
            error = exc if isinstance(exc, ExperimentationError) else ExperimentationError(str(exc))
            return self._transient(error)
        if self._closed:
            return self._interval
        try:
            return self._handle(response)
        except Exception as exc:  # noqa: BLE001
            return self._transient(ExperimentationError(f"GET {RULESET_PATH} failed: {exc}"))

    def _handle(self, response: Response) -> float:
        status = response.status
        if status == 304:
            if self._ruleset is not None:
                return self._succeeded()
            with self._lock:
                self._etag = None
            return self._transient(
                ExperimentationError(f"Unexpected 304 without a ruleset (GET {RULESET_PATH})", status=304)
            )
        if 200 <= status < 300:
            return self._load(response)
        if status in (401, 403):
            return self._refused(
                str(status),
                ExperimentationError(
                    f"The API key was refused ({status}) fetching the flag ruleset: {_detail(response)} "
                    "Local evaluation needs a key with the 'sdk:ruleset' scope whose owner can change "
                    "feature flags. Until a refresh succeeds every flag is evaluated by the server; "
                    "the SDK retries every 10 minutes.",
                    status=status,
                    body=response.text(),
                ),
            )
        if status == 404:
            return self._refused(
                "404",
                ExperimentationError(
                    f"The server has no {RULESET_PATH} (404). Local evaluation needs Controlvar "
                    f"{MIN_SERVER_RELEASE} or later: upgrade the server, or leave evaluation at "
                    "'server'. Until then every flag is evaluated by the server.",
                    status=404,
                ),
            )
        if status == 429:
            error = _http_error(response, RULESET_PATH)
            with self._lock:
                self._last_error = error
            self._report(error, "refresh")
            wait = _retry_after(response)
            # Retry-After is a floor: never earlier than the server asked, nor than the interval.
            return max(wait if wait is not None else self._interval, self._interval)
        return self._transient(_http_error(response, RULESET_PATH))

    def _load(self, response: Response) -> float:
        try:
            body = response.json()
        except ValueError:
            return self._transient(
                ExperimentationError(
                    f"Invalid JSON in the flag ruleset (GET {RULESET_PATH})",
                    status=response.status,
                    body=response.text()[:200],
                )
            )
        if not is_supported_format(body):
            doc = body if isinstance(body, dict) else {}
            return self._refused(
                "format",
                ExperimentationError(
                    "The server sent a flag ruleset in a format this SDK does not understand "
                    f"(schema {json.dumps(doc.get('schema'))}, bucketing "
                    f"{json.dumps(doc.get('bucketing'))}). Every flag is evaluated by the server; "
                    "upgrade the controlvar package to evaluate locally.",
                    status=response.status,
                ),
            )
        indexed = index_ruleset(body)
        if indexed is None:
            return self._transient(
                ExperimentationError(
                    f"The flag ruleset is malformed (GET {RULESET_PATH})", status=response.status
                )
            )
        with self._lock:
            self._ruleset = indexed
            self._etag = response.header("ETag") or f'"{indexed.version}"'
        return self._succeeded()

    def _succeeded(self) -> float:
        with self._lock:
            self._last_refresh_monotonic = self._clock()
            self._last_refresh_at = datetime.now(timezone.utc)
            self._last_error = None
            self._failures = 0
            self._refusal = None
            self._jitter_next = True
        return self._interval

    def _transient(self, error: ExperimentationError) -> float:
        """A failure that may pass: keep whatever ruleset there is, report, back off."""
        with self._lock:
            self._last_error = error
            self._failures += 1
            failures = self._failures
        self._report(error, "refresh")
        return min(self._interval * 2 ** (failures - 1), MAX_BACKOFF_SECONDS)

    def _refused(self, kind: str, error: ExperimentationError) -> float:
        """The server refused: discard the ruleset (nothing is answered locally), report once."""
        with self._lock:
            self._ruleset = None
            self._etag = None
            self._last_error = error
            self._failures = 0
            first = self._refusal != kind
            self._refusal = kind
        if first:
            self._report(error, "refresh")
        return REFUSED_RETRY_SECONDS

    # ----------------------------------------------------------------- counts

    def _count(self, answers: Mapping[str, bool]) -> None:
        if not answers:
            return
        with self._lock:
            if self._window_start is None:
                self._window_start = datetime.now(timezone.utc)
            for key, enabled in answers.items():
                tally = self._tallies.setdefault(key, [0, 0])
                tally[0] += 1
                if enabled:
                    tally[1] += 1

    def flush(self) -> None:
        """Send the counts gathered since the last flush. Never raises; failures go to ``on_error``."""
        with self._flush_lock:
            with self._lock:
                if not self._tallies:
                    return
                tallies, self._tallies = self._tallies, {}
                started, self._window_start = self._window_start, None
            end = datetime.now(timezone.utc)
            start = started or end
            if (end - start).total_seconds() > _MAX_WINDOW_SECONDS:
                start = end - timedelta(seconds=_MAX_WINDOW_SECONDS)
            entries: List[Dict[str, Any]] = []
            for key, (count, enabled) in tallies.items():
                while count > 0:
                    part = min(count, _MAX_ENTRY_COUNT)
                    part_enabled = min(enabled, part)
                    entries.append(
                        {
                            "flag_key": key,
                            "count": part,
                            "enabled_count": part_enabled,
                            "window_start": start.isoformat(),
                            "window_end": end.isoformat(),
                        }
                    )
                    count -= part
                    enabled -= part_enabled
            for index in range(0, len(entries), _MAX_REPORT_ENTRIES):
                body = json.dumps({"evaluations": entries[index : index + _MAX_REPORT_ENTRIES]}).encode("utf-8")
                try:
                    response = self._send("POST", EVALUATIONS_PATH, {}, body)
                except Exception as exc:  # noqa: BLE001
                    error = exc if isinstance(exc, ExperimentationError) else ExperimentationError(str(exc))
                    self._report(error, "flush")
                    continue
                if not 200 <= response.status < 300:
                    self._report(
                        ExperimentationError(
                            f"POST {EVALUATIONS_PATH} failed with HTTP {response.status}",
                            status=response.status,
                            body=response.text(),
                        ),
                        "flush",
                    )

    # For tests: what the lock protects, read atomically.
    def _snapshot(self) -> Tuple[Optional[IndexedRuleset], Optional[str]]:
        with self._lock:
            return self._ruleset, self._etag
