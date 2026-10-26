"""Synchronous client for the Controlvar public API.

* Dependency-free: HTTP goes through :mod:`urllib` from the standard library
  (or any injected :class:`~controlvar.transport.Transport`).
* **The server decides** by default. Experiment assignment and flag evaluation
  come from ``POST /api/v1/tracking/assign`` and
  ``GET /api/v1/feature-flags/evaluate/{key}``. With ``evaluation="local"``
  (server-side code, beta) flags are answered in-process from the server's
  ruleset whenever the answer is provably the server's, and by the server
  otherwise; experiments always stay on the server.
* Successful assignments/evaluations are cached per user + key for
  ``cache_ttl_seconds``; failures are never cached. Every successful answer also
  records its key for the key-less ``track()`` fan-out, for 24 hours
  (:mod:`controlvar.attribution`); ``clear_cache()`` keeps that record.
* ``get_assignment``/``get_feature_flag``/``get_all_flags``/``get_assignments``
  raise :class:`~controlvar.ExperimentationError`; the convenience
  wrappers ``get_variant``/``is_feature_enabled`` swallow it and return a
  default; ``track``/``track_batch`` never raise.
* Safe to share between threads.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Union
from urllib.parse import quote, urlencode

from .attribution import AttributedKeys
from .cache import UserKeyCache
from .evaluator import DEFER
from .local import (
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    MIN_REFRESH_INTERVAL_SECONDS,
    LocalEvaluation,
    LocalEvaluationStatus,
    ReadyResult,
)
from .transport import Response, Transport, UrllibTransport
from .types import Assignment, BatchResult, ExperimentationError, FlagEvaluation
from .version import __version__

__all__ = ["BATCH_LIMIT", "ExperimentationClient"]

logger = logging.getLogger(__name__)

#: Maximum events per ``POST /api/v1/tracking/batch`` request.
BATCH_LIMIT = 100
#: Upper bound honoured for a ``Retry-After`` header on 429.
MAX_RETRY_AFTER_SECONDS = 5.0
#: Wait used when a 429 carries no (parseable) ``Retry-After`` header.
DEFAULT_RETRY_AFTER_SECONDS = 1.0

_USER_AGENT = f"controlvar-python/{__version__}"


def _iso_timestamp(value: Union[datetime, str, None]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return _iso_timestamp(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _context_param(attributes: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Compact JSON for the ``context`` query parameter, or ``None`` when there is nothing to send.

    ``_request`` URL-encodes it with :func:`urllib.parse.quote` (percent-encoding, no
    ``+``), so ``{"country": "US"}`` travels as ``context=%7B%22country%22%3A%22US%22%7D``.
    """
    if not attributes:
        return None
    return json.dumps(dict(attributes), separators=(",", ":"), default=_json_default)


def _retry_after_seconds(response: Response) -> float:
    header = response.header("Retry-After")
    seconds = DEFAULT_RETRY_AFTER_SECONDS
    if header is not None:
        try:
            seconds = float(header.strip())
        except ValueError:
            seconds = DEFAULT_RETRY_AFTER_SECONDS
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


class ExperimentationClient:
    """Client for experiments, feature flags and event tracking.

    Args:
        api_url: Backend origin, e.g. ``"http://localhost:8000"``. The client
            appends ``/api/v1/...``; a trailing slash is ignored.
        api_key: Sent as ``X-API-Key`` on every request.
        timeout_seconds: Per-request timeout (default 5 s).
        cache_ttl_seconds: How long a successful assignment/evaluation is
            reused for the same user + key (default 300 s).
        default_variant: What ``get_variant`` returns when assignment fails
            (default ``"control"``).
        transport: Optional :class:`~controlvar.transport.Transport`
            (tests use :class:`controlvar.testing.FakeTransport`).
        evaluation: ``"server"`` (default): every flag is evaluated by the
            server. ``"local"``: flags are evaluated in-process from
            ``GET /api/v1/sdk/ruleset`` (beta) whenever the answer is provably
            the server's, and by the server otherwise. Server-side code only:
            the key needs the ``sdk:ruleset`` scope. Experiments are always
            assigned by the server.
        refresh_interval_seconds: Local mode: how often the ruleset is
            refreshed (default 30, minimum 5, +/-10% jitter).
        max_stale_seconds: Local mode: stop answering locally once the ruleset
            has not been refreshed for this long (default: no limit).
        on_error: Called as ``on_error(error, operation)`` when a local-mode
            ruleset refresh (``"refresh"``), evaluation-count report
            (``"flush"``) or local evaluation (``"evaluate"``; that call then
            goes to the server) fails. Those never raise.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        timeout_seconds: float = 5.0,
        cache_ttl_seconds: float = 300,
        default_variant: str = "control",
        transport: Optional[Transport] = None,
        evaluation: str = "server",
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        max_stale_seconds: Optional[float] = None,
        on_error: Optional[Callable[[ExperimentationError, str], None]] = None,
    ) -> None:
        if not api_url:
            raise ValueError("api_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.default_variant = default_variant
        self._api_key = api_key
        self._transport: Transport = transport if transport is not None else UrllibTransport()
        self._assignments: UserKeyCache[Assignment] = UserKeyCache(cache_ttl_seconds)
        self._flags: UserKeyCache[FlagEvaluation] = UserKeyCache(cache_ttl_seconds)
        # The keys a key-less track() is sent to; outlives the cache entries.
        self._attributed = AttributedKeys()
        self._sleep = time.sleep
        self._headers: Dict[str, str] = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if evaluation not in ("server", "local"):
            raise ValueError("evaluation must be 'server' or 'local'")
        self.evaluation = evaluation
        self._on_error = on_error
        self._local: Optional[LocalEvaluation] = None
        if evaluation == "local":
            interval = refresh_interval_seconds
            if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not (0 <= interval < float("inf")):
                raise ValueError("refresh_interval_seconds must be a finite number of seconds")
            if max_stale_seconds is not None and (
                isinstance(max_stale_seconds, bool)
                or not isinstance(max_stale_seconds, (int, float))
                or not max_stale_seconds > 0
            ):
                raise ValueError("max_stale_seconds must be a positive number of seconds")
            self._local = LocalEvaluation(
                send=self._send_raw,
                report=self._report,
                refresh_interval_seconds=max(float(interval), MIN_REFRESH_INTERVAL_SECONDS),
                max_stale_seconds=max_stale_seconds,
                json_default=_json_default,
            )

    # ------------------------------------------------------- local evaluation

    def _send_raw(self, method: str, path: str, headers: Mapping[str, str], body: Optional[bytes]) -> Response:
        """One request with the client's headers plus ``headers``; any status comes back."""
        return self._send(method, self.api_url + path, body, extra_headers=headers)

    def _report(self, error: ExperimentationError, operation: str) -> None:
        logger.warning("local evaluation %s failed: %s", operation, error)
        if self._on_error is None:
            return
        try:
            self._on_error(error, operation)
        except Exception:  # noqa: BLE001 - a failing handler must not stop the poller
            logger.exception("on_error handler raised")

    def ready(self, timeout_seconds: Optional[float] = None) -> ReadyResult:
        """Wait until flags can be answered locally, the first ruleset fetch has finished, or
        ``timeout_seconds`` passed. Never raises; the result is truthy when ready. In server
        mode it returns ``ReadyResult(ok=True)`` at once. Evaluations never wait for it: until
        a ruleset is loaded they are made on the server."""
        if self._local is None:
            return ReadyResult(True)
        return self._local.ready(timeout_seconds)

    def status(self) -> LocalEvaluationStatus:
        """Where flags are being evaluated, and the state of the ruleset."""
        if self._local is None:
            return LocalEvaluationStatus(evaluation="server", ready=True)
        return self._local.status()

    def close(self) -> None:
        """Local mode: stop refreshing the ruleset and send the evaluation counts not yet sent;
        later calls are evaluated by the server. A no-op in server mode. Never raises."""
        if self._local is not None:
            self._local.close()

    def __enter__(self) -> "ExperimentationClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ HTTP

    def _send(
        self,
        method: str,
        url: str,
        data: Optional[bytes],
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Response:
        headers = self._headers if not extra_headers else {**extra_headers, **self._headers}
        try:
            return self._transport.send(method, url, headers, data, self.timeout_seconds)
        except ExperimentationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise any transport failure
            raise ExperimentationError(f"{method} {url} failed: {exc}") from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """Perform one API call and return the decoded JSON body (``None`` if empty).

        Retries exactly once on 429, sleeping for ``Retry-After`` (capped at
        :data:`MAX_RETRY_AFTER_SECONDS`). Raises :class:`ExperimentationError`
        for any non-2xx response, network failure or undecodable body.
        """
        url = self.api_url + path
        if query:
            url += "?" + urlencode(query, quote_via=quote)
        data = None if body is None else json.dumps(body, default=_json_default).encode("utf-8")

        response = self._send(method, url, data)
        if response.status == 429:
            wait = _retry_after_seconds(response)
            logger.debug("429 from %s %s; retrying once after %.1fs", method, path, wait)
            self._sleep(wait)
            response = self._send(method, url, data)

        if response.status < 200 or response.status >= 300:
            raise ExperimentationError(
                f"{method} {path} failed with HTTP {response.status}",
                status=response.status,
                body=response.text(),
            )
        if not response.body:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ExperimentationError(
                f"{method} {path} returned a non-JSON body",
                status=response.status,
                body=response.text(),
            ) from exc

    # ----------------------------------------------------------- experiments

    def get_assignment(
        self,
        experiment_key: str,
        user_id: str,
        user_attributes: Optional[Mapping[str, Any]] = None,
    ) -> Assignment:
        """Assign ``user_id`` to ``experiment_key`` via ``POST /api/v1/tracking/assign``.

        Sticky on the server and cached here per user + key. ``user_attributes``
        is sent as ``context`` (used by targeting rules). Raises
        :class:`ExperimentationError` (``status == 404`` when the experiment is
        not ACTIVE or unknown).
        """
        cached = self._assignments.get(user_id, experiment_key)
        if cached is not None:
            return cached

        body: Dict[str, Any] = {"experiment_key": experiment_key, "user_id": user_id}
        if user_attributes is not None:
            body["context"] = dict(user_attributes)
        try:
            data = self._request("POST", "/api/v1/tracking/assign", body=body)
        except ExperimentationError as exc:
            # A 404: a key-less track() must no longer reach this experiment.
            if False:
                self._attributed.forget_experiment(user_id, experiment_key)
            raise

        if not isinstance(data, dict) or not isinstance(data.get("variant_name"), str):
            raise ExperimentationError(
                "POST /api/v1/tracking/assign returned an unexpected body",
                status=200,
                body=json.dumps(data),
            )
        assignment = Assignment(
            experiment_key=str(data.get("experiment_key") or experiment_key),
            user_id=str(data.get("user_id") or user_id),
            variant_id=None if data.get("variant_id") is None else str(data["variant_id"]),
            variant_name=data["variant_name"],
            is_control=bool(data.get("is_control", False)),
            configuration=data.get("configuration"),
            # Only taken when the server sent them: a missing ``assigned`` stays
            # None (unknown), never False, so older servers are not mistaken for
            # "not enrolled".
            assigned=data["assigned"] if isinstance(data.get("assigned"), bool) else None,
            reason=data["reason"] if isinstance(data.get("reason"), str) else None,
        )
        self._assignments.set(user_id, experiment_key, assignment)
        self._attributed.record_experiment(user_id, assignment.experiment_key, assignment.reason)
        return assignment

    def get_variant(
        self,
        experiment_key: str,
        user_id: str,
        user_attributes: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """``get_assignment(...).variant_name``, or ``default_variant`` on any failure."""
        try:
            return self.get_assignment(experiment_key, user_id, user_attributes).variant_name
        except ExperimentationError as exc:
            logger.warning("get_variant(%s, %s) failed: %s", experiment_key, user_id, exc)
            return self.default_variant

    def get_assignments(self, user_id: str, active_only: bool = True) -> List[Dict[str, Any]]:
        """The user's assignments from ``GET /api/v1/tracking/assignments/{user_id}`` (server-side, not cached)."""
        data = self._request(
            "GET",
            f"/api/v1/tracking/assignments/{quote(user_id, safe='')}",
            query={"active_only": "true" if active_only else "false"},
        )
        return list(data) if isinstance(data, list) else []

    # ---------------------------------------------------------- feature flags

    def get_feature_flag(
        self,
        flag_key: str,
        user_id: str,
        user_attributes: Optional[Mapping[str, Any]] = None,
    ) -> FlagEvaluation:
        """Evaluate one flag via
        ``GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…[&context=<url-encoded JSON>]``.

        ``user_attributes`` (when non-empty) is sent as ``context`` so the flag's
        targeting rules can evaluate against it. Cached per user + key only —
        attributes are assumed stable per user; call :meth:`clear_cache` after
        changing them. Raises :class:`ExperimentationError` (``status == 404``
        for a flag key the server does not know; an inactive flag is
        ``enabled=False, reason="inactive"``).

        With ``evaluation="local"``, a flag the ruleset can answer is answered
        in-process from the attributes passed (no request, no cache read) with
        ``source="local"``; anything else is evaluated by the server as above,
        with ``source="server"``.
        """
        if self._local is not None:
            answer = self._local.answer(flag_key, user_id, user_attributes)
            if answer is not DEFER:
                local = FlagEvaluation(
                    key=flag_key,
                    enabled=answer["enabled"],
                    config=None,
                    reason=answer["reason"],
                    source="local",
                )
                # Recorded for the key-less track() fan-out, as a server answer is.
                self._flags.set(user_id, flag_key, local)
                self._attributed.record_flag(user_id, flag_key)
                return local
        cached = self._flags.get(user_id, flag_key)
        if cached is not None:
            return cached

        query: Dict[str, str] = {"user_id": user_id}
        context = _context_param(user_attributes)
        if context is not None:
            query["context"] = context
        data = self._request(
            "GET",
            f"/api/v1/feature-flags/evaluate/{quote(flag_key, safe='')}",
            query=query,
        )
        if not isinstance(data, dict):
            raise ExperimentationError(
                "GET /api/v1/feature-flags/evaluate returned an unexpected body",
                status=200,
                body=json.dumps(data),
            )
        reason = data.get("reason")
        evaluation = FlagEvaluation(
            key=str(data.get("key") or flag_key),
            enabled=bool(data.get("enabled", False)),
            config=data.get("config"),
            reason=reason if isinstance(reason, str) else None,
            source="server" if self._local is not None else None,
        )
        self._flags.set(user_id, flag_key, evaluation)
        self._attributed.record_flag(user_id, flag_key)
        return evaluation

    def is_feature_enabled(
        self,
        flag_key: str,
        user_id: str,
        user_attributes: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """``get_feature_flag(...).enabled``, or ``False`` on any failure."""
        try:
            return self.get_feature_flag(flag_key, user_id, user_attributes).enabled
        except ExperimentationError as exc:
            logger.warning("is_feature_enabled(%s, %s) failed: %s", flag_key, user_id, exc)
            return False

    def get_all_flags(
        self,
        user_id: str,
        user_attributes: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, bool]:
        """All active flags for the user from
        ``GET /api/v1/feature-flags/user/{user_id}[?context=<url-encoded JSON>]`` (not cached).

        ``user_attributes`` (when non-empty) is sent as ``context`` for targeting rules.

        With ``evaluation="local"`` the answer is computed in-process when every
        active flag can be answered locally for these attributes; otherwise the
        whole call goes to the server.
        """
        if self._local is not None:
            flags = self._local.answer_all(user_id, user_attributes)
            if flags is not DEFER:
                return dict(flags)
        context = _context_param(user_attributes)
        data = self._request(
            "GET",
            f"/api/v1/feature-flags/user/{quote(user_id, safe='')}",
            query=None if context is None else {"context": context},
        )
        if not isinstance(data, dict):
            return {}
        return {str(key): bool(value) for key, value in data.items()}

    # --------------------------------------------------------------- tracking

    @staticmethod
    def _event_body(
        user_id: str,
        event_name: str,
        event_value: Optional[float],
        properties: Optional[Mapping[str, Any]],
        event_type: Optional[str],
        timestamp: Union[datetime, str, None],
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "event_type": event_type or event_name,
            "event_name": event_name,
            "user_id": user_id,
        }
        if event_value is not None:
            body["value"] = float(event_value)
        if properties is not None:
            body["metadata"] = dict(properties)
        stamp = _iso_timestamp(timestamp)
        if stamp is not None:
            body["timestamp"] = stamp
        return body

    def track(
        self,
        user_id: str,
        event_name: str,
        event_value: Optional[float] = None,
        properties: Optional[Mapping[str, Any]] = None,
        experiment_key: Optional[str] = None,
        feature_flag_key: Optional[str] = None,
        event_type: Optional[str] = None,
        timestamp: Union[datetime, str, None] = None,
    ) -> bool:
        """Record an event. Never raises.

        * With ``experiment_key`` and/or ``feature_flag_key``: one
          ``POST /api/v1/tracking/track``.
        * Without a key: one ``POST /api/v1/tracking/batch`` (chunked at 100)
          containing one entry per experiment (``experiment_key``) plus one per
          flag (``feature_flag_key``) this client answered for the user in the
          last 24 hours, cached or not. An experiment whose latest answer said it
          is a draft, paused, completed or archived gets no entry.
        * Without a key and with nothing to attribute it to: one
          ``POST /api/v1/tracking/track`` with no key, which the server stores as
          history.

        ``event_type`` defaults to ``event_name``. Returns ``True`` when the
        event was sent and the server answered 2xx (every request, when a
        fan-out takes more than one); ``False`` otherwise. A 2xx batch can still
        report a failed entry, for example a key the server no longer has; use
        :meth:`track_batch` to read the per-entry results.
        """
        try:
            base = self._event_body(user_id, event_name, event_value, properties, event_type, timestamp)
            if experiment_key or feature_flag_key:
                body = dict(base)
                if experiment_key:
                    body["experiment_key"] = experiment_key
                if feature_flag_key:
                    body["feature_flag_key"] = feature_flag_key
                self._request("POST", "/api/v1/tracking/track", body=body)
                return True

            experiments, flags = self._attributed.keys(user_id)
            events: List[Dict[str, Any]] = [dict(base, experiment_key=key) for key in experiments]
            events.extend(dict(base, feature_flag_key=key) for key in flags)
            if not events:
                # Nothing to attribute it to: sent once with no key, stored as history.
                self._request("POST", "/api/v1/tracking/track", body=base)
                return True
            return self._post_batches(events)
        except Exception as exc:  # noqa: BLE001 - fire-and-forget
            logger.warning("track(%s, %s) failed: %s", user_id, event_name, exc)
            return False

    def _post_batches(self, events: List[Dict[str, Any]]) -> bool:
        """``POST /api/v1/tracking/batch`` in chunks of 100; ``True`` when every
        chunk was answered 2xx. A failed chunk is logged and the rest are still sent."""
        ok = True
        for start in range(0, len(events), BATCH_LIMIT):
            try:
                self._request("POST", "/api/v1/tracking/batch", body={"events": events[start : start + BATCH_LIMIT]})
            except Exception as exc:  # noqa: BLE001 - never raise
                logger.warning("track fan-out chunk starting at %d failed: %s", start, exc)
                ok = False
        return ok

    def track_batch(self, events: Iterable[Mapping[str, Any]]) -> BatchResult:
        """Send raw track bodies via ``POST /api/v1/tracking/batch`` in chunks of 100. Never raises.

        Each event is a dict with the ``/tracking/track`` fields
        (``event_type``/``event_name``, ``user_id``, optional ``experiment_key``,
        ``feature_flag_key``, ``value``, ``metadata``, ``timestamp``). An event
        with no key is stored as history and counts in no experiment's results;
        tag outcome events.
        ``event_type`` defaults to ``event_name`` when missing. A chunk that
        fails outright counts every one of its events as a failure; error
        ``index`` values are absolute positions in ``events``.
        """
        result = BatchResult()
        normalised = [self._normalise_event(event) for event in events]
        for start in range(0, len(normalised), BATCH_LIMIT):
            chunk = normalised[start : start + BATCH_LIMIT]
            try:
                data = self._request("POST", "/api/v1/tracking/batch", body={"events": chunk})
            except Exception as exc:  # noqa: BLE001 - never raise
                logger.warning("track_batch chunk starting at %d failed: %s", start, exc)
                result.failure_count += len(chunk)
                result.errors.append(
                    {
                        "index": start,
                        "count": len(chunk),
                        "status": getattr(exc, "status", None),
                        "error": str(exc),
                    }
                )
                continue
            if isinstance(data, dict):
                result.success_count += int(data.get("success_count") or 0)
                result.failure_count += int(data.get("failure_count") or 0)
                for error in data.get("errors") or []:
                    entry = dict(error) if isinstance(error, Mapping) else {"error": error}
                    if isinstance(entry.get("index"), int):
                        entry["index"] += start
                    result.errors.append(entry)
            else:
                result.success_count += len(chunk)
        return result

    @staticmethod
    def _normalise_event(event: Mapping[str, Any]) -> Dict[str, Any]:
        body = dict(event)
        if not body.get("event_type") and body.get("event_name"):
            body["event_type"] = body["event_name"]
        if isinstance(body.get("timestamp"), datetime):
            body["timestamp"] = _iso_timestamp(body["timestamp"])
        return body

    # ------------------------------------------------------------------ cache

    def cached_assignments(self, user_id: str) -> List[Assignment]:
        """Successful, unexpired assignments cached for the user."""
        return [assignment for _, assignment in self._assignments.entries(user_id)]

    def cached_flags(self, user_id: str) -> List[FlagEvaluation]:
        """Successful, unexpired flag evaluations cached for the user."""
        return [evaluation for _, evaluation in self._flags.entries(user_id)]

    def clear_cache(self) -> None:
        """Drop every cached assignment and evaluation, so the next calls ask the
        server again. The keys a key-less ``track()`` is sent to are kept."""
        self._assignments.clear()
        self._flags.clear()
