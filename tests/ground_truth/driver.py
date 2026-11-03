"""The live replay's driver: a plan sent through the public API, every answer
written to a ledger.

``Driver`` signs in, creates an API key, and sends a ``ground_truth.generator``
plan to a running Controlvar over HTTP (httpx): one request at a time, in the
plan's order, so the ledger lists the requests in the order the server received
them, which is the order ``contract.valid_events`` reads them in. It then reads
``GET /api/v1/results/{experiment_id}?use_cache=false``.

* **Pacing.** Each route at most 80% of its rate limit
  (``backend/app/middleware/rate_limiter.py``; the limiter is on under
  ``ENVIRONMENT=development``): 80 requests a second for ``/assign``,
  ``/track`` and ``/batch`` (6,000 a minute), 48 a minute for
  ``/assign/batch`` (60 a minute), 4 a second for every other route (300 a
  minute). The sign-in is one request per run.
* **A 429** is written to the ledger, where it stores nothing, and the request
  is sent again after ``Retry-After``, as a new record: nothing ran on the
  server, so sending it again is safe.
* **A 5xx, a timeout or a broken connection** leaves it unknown whether the
  server stored anything, so the ledger refuses the run (``LedgerRefused``).
  ``server_errors`` counts every 5xx, the results reads' included.
* **Results** are read with ``use_cache=false``: while an experiment runs a
  cached answer can be 300 s old.
* **Credentials** go only into request headers. The ledger is given the token
  and the key as secrets, so a record that carried either is refused.

The statistics a run leaves (``summary``) are counts and durations only.

Imports the library, httpx and the standard library; nothing from ``backend``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import IO, Any, NoReturn

import httpx
from ground_truth import generator, ledger

SIGN_IN = "/api/v1/auth/login"
API_KEYS = "/api/v1/api-keys"
EXPERIMENTS = "/api/v1/experiments/"
#: The results route, as the statistics name it (one row for every experiment).
RESULTS = "/api/v1/results/{experiment_id}"
#: The scope ``/assign/batch`` requires (``backend/app/api/sdk_scope.py``).
SDK_RULESET_SCOPE = "sdk:ruleset"

#: Per route, the least time between two of its requests, in seconds: 80% of
#: its rate limit.
MIN_INTERVAL = {
    ledger.ASSIGN: 1 / 80,
    ledger.TRACK: 1 / 80,
    ledger.BATCH: 1 / 80,
    ledger.ASSIGN_BATCH: 60 / 48,
}
#: Every other route: 80% of 300 a minute.
OTHER_INTERVAL = 60 / 240
#: The longest ``Retry-After`` honoured, and the wait when it is absent or not
#: a number of seconds.
MAX_RETRY_AFTER = 120.0
DEFAULT_RETRY_AFTER = 1.0
#: A request's longest wait for its answer: an ``/assign/batch`` of 1,000
#: users commits each one on its own.
TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class ReplayError(Exception):
    """The replay could not go on; the message says why. Never carries a
    credential: it names routes and statuses."""


@dataclass
class RouteStats:
    requests: int = 0
    rate_limited: int = 0
    server_errors: int = 0
    #: Time spent waiting for the route's answers (pacing excluded).
    seconds: float = 0.0


def create_body(scenario: generator.Scenario) -> dict[str, Any]:
    """``POST /api/v1/experiments/``'s body for *scenario*: its variants in the
    plan's order, the first the control, and its metrics, the first primary."""
    return {
        "name": f"Ground truth {scenario.name} {scenario.experiment_key}"[:100],
        "key": scenario.experiment_key,
        "description": "Seeded traffic from tests/ground_truth (#1544).",
        "variants": [
            {
                "name": arm.name,
                "is_control": index == 0,
                "traffic_allocation": arm.allocation,
            }
            for index, arm in enumerate(scenario.arms)
        ],
        "metrics": [
            {
                "name": metric.name,
                "event_name": metric.event_name,
                "metric_type": metric.type,
                "is_primary": index == 0,
            }
            for index, metric in enumerate(scenario.metrics)
        ],
        "correction_method": scenario.correction_method,
        "confidence_level": scenario.confidence_level,
    }


def retry_after(response: httpx.Response) -> float:
    """The seconds ``Retry-After`` asks for, within [0, MAX_RETRY_AFTER]."""
    value = response.headers.get("Retry-After", "").strip()
    if not value.isdigit():
        return DEFAULT_RETRY_AFTER
    return min(float(value), MAX_RETRY_AFTER)


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _detail(response: httpx.Response) -> str:
    """A short reason for a refusal: the answer's ``detail`` when it is text."""
    body = _body(response)
    detail = body.get("detail") if isinstance(body, Mapping) else None
    return detail[:200] if isinstance(detail, str) else ""


class Driver:
    """Sends plans to one server. ``sign_in`` and ``create_api_key`` first."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self.token: str | None = None
        self.api_key: str | None = None
        #: Route (``RESULTS`` for every results read) -> its statistics.
        self.routes: dict[str, RouteStats] = {}
        #: (scenario, phase) -> wall-clock seconds, pacing included. A phase
        #: is a kind of operation: lifecycle, assign_batch, assign, track,
        #: batch, or results.
        self.phases: dict[tuple[str, str], float] = {}
        #: Every 5xx answer.
        self.server_errors = 0
        #: The experiment the plan being sent created.
        self._experiment_id: str | None = None

    # -- requests -------------------------------------------------------------
    def _pace(self, route: str) -> None:
        interval = MIN_INTERVAL.get(route, OTHER_INTERVAL)
        last = self._last.get(route)
        if last is not None:
            wait = last + interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last[route] = self._clock()

    def _request(
        self,
        method: str,
        path: str,
        *,
        route: str,
        headers: Mapping[str, str],
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One request, paced, counted; a transport error propagates."""
        self._pace(route)
        stats = self.routes.setdefault(route, RouteStats())
        started = self._clock()
        try:
            response = self._client.request(
                method, path, json=json, params=params, headers=dict(headers)
            )
        finally:
            stats.seconds += self._clock() - started
        stats.requests += 1
        if response.status_code == 429:
            stats.rate_limited += 1
        if response.status_code >= 500:
            stats.server_errors += 1
            self.server_errors += 1
        return response

    def _bearer(self) -> dict[str, str]:
        if self.token is None:
            raise ReplayError("sign in first")
        return {"Authorization": f"Bearer {self.token}"}

    def _key(self) -> dict[str, str]:
        if self.api_key is None:
            raise ReplayError("create the API key first")
        return {"X-API-Key": self.api_key}

    def _admin(
        self,
        method: str,
        path: str,
        *,
        route: str,
        headers: Mapping[str, str],
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """A request outside the ledger: sent again after a 429; anything but a
        2xx in the end is a ``ReplayError``."""
        while True:
            try:
                response = self._request(
                    method, path, route=route, headers=headers, json=json, params=params
                )
            except httpx.TransportError as exc:
                raise ReplayError(
                    f"{method} {route} got no answer ({type(exc).__name__})"
                ) from None
            if response.status_code == 429:
                self._sleep(retry_after(response))
                continue
            if not 200 <= response.status_code < 300:
                raise ReplayError(
                    f"{method} {route} answered {response.status_code}"
                    + (f": {_detail(response)}" if _detail(response) else "")
                )
            return response

    # -- credentials ------------------------------------------------------------
    def sign_in(self, email: str, password: str) -> None:
        """A bearer token for *email* (``AUTH_PROVIDER=local``)."""
        response = self._admin(
            "POST",
            SIGN_IN,
            route=SIGN_IN,
            headers={},
            json={"email": email, "password": password},
        )
        token = (_body(response) or {}).get("access_token")
        if not isinstance(token, str) or not token:
            raise ReplayError(f"POST {SIGN_IN} answered no access_token")
        self.token = token

    def create_api_key(self) -> None:
        """An API key with the ``sdk:ruleset`` scope, which ``/assign/batch``
        needs, owned by the signed-in user."""
        response = self._admin(
            "POST",
            API_KEYS,
            route=API_KEYS,
            headers=self._bearer(),
            json={"name": "ground-truth replay", "scopes": [SDK_RULESET_SCOPE]},
        )
        key = (_body(response) or {}).get("key")
        if not isinstance(key, str) or not key:
            raise ReplayError(f"POST {API_KEYS} answered no key")
        self.api_key = key

    @property
    def secrets(self) -> tuple[str, ...]:
        """What the ledger must never carry."""
        return tuple(s for s in (self.token, self.api_key) if s)

    # -- the plan -----------------------------------------------------------------
    def _no_answer(
        self, writer: ledger.Writer, seq: int, exc: httpx.TransportError
    ) -> NoReturn:
        if isinstance(exc, httpx.TimeoutException):
            writer.transport_error(seq, "timeout")
        if isinstance(exc, httpx.ConnectError):
            writer.transport_error(seq, "refused")
        writer.transport_error(seq, "reset")

    def _lifecycle(
        self, writer: ledger.Writer, plan: generator.Plan, action: str
    ) -> str | None:
        """Create or start the plan's experiment; the id ``create`` answers."""
        key = plan.scenario.experiment_key
        while True:
            seq = writer.send_action(action, key)
            if action == "create":
                path, route, body = EXPERIMENTS, EXPERIMENTS, create_body(plan.scenario)
            elif action == "start" and self._experiment_id is not None:
                path = f"{EXPERIMENTS}{self._experiment_id}/start"
                route, body = f"{EXPERIMENTS}{{experiment_id}}/start", None
            else:
                raise ReplayError(f"no rule to send lifecycle action {action!r}")
            try:
                response = self._request(
                    "POST", path, route=route, headers=self._bearer(), json=body
                )
            except httpx.TransportError as exc:
                self._no_answer(writer, seq, exc)
            writer.answer(seq, response.status_code)
            if response.status_code == 429:
                self._sleep(retry_after(response))
                continue
            if not 200 <= response.status_code < 300:
                raise ReplayError(
                    f"{action} of experiment {key} answered {response.status_code}"
                    + (f": {_detail(response)}" if _detail(response) else "")
                )
            if action == "create":
                created = (_body(response) or {}).get("id")
                if not isinstance(created, str):
                    raise ReplayError(f"create of experiment {key} answered no id")
                return created
            return None

    def _send(self, writer: ledger.Writer, route: str, body: Mapping[str, Any]) -> None:
        """One tracking request, recorded; sent again after each 429."""
        while True:
            seq = writer.send(route, body)
            try:
                response = self._request(
                    "POST", route, route=route, headers=self._key(), json=body
                )
            except httpx.TransportError as exc:
                self._no_answer(writer, seq, exc)
            # Refuses a 5xx, which ends the run.
            writer.answer(seq, response.status_code, _body(response))
            if response.status_code != 429:
                return
            self._sleep(retry_after(response))

    def replay(
        self,
        plan: generator.Plan,
        stream: IO[str],
        *,
        declared: Mapping[str, Any],
        label: str | None = None,
    ) -> str:
        """Send *plan*, writing its ledger to *stream*; the experiment's id.

        Raises ``ledger.LedgerRefused`` when the ledger refuses the run (a 5xx,
        no answer) and ``ReplayError`` when a lifecycle step is refused.
        """
        label = label or plan.scenario.name
        self._experiment_id = None
        writer = ledger.Writer(stream, **plan.start(declared), secrets=self.secrets)
        for op in plan.ops:
            started = self._clock()
            if op.kind == "lifecycle":
                created = self._lifecycle(writer, plan, op.body["action"])
                if created is not None:
                    self._experiment_id = created
            elif op.route is None:
                raise ReplayError(f"a {op.kind} operation names no route")
            else:
                self._send(writer, op.route, op.body)
            phase = (label, op.kind)
            self.phases[phase] = self.phases.get(phase, 0.0) + self._clock() - started
        writer.end()
        if self._experiment_id is None:
            raise ReplayError(f"plan {label} created no experiment")
        return self._experiment_id

    def results(self, experiment_id: str, *, label: str | None = None) -> Any:
        """``GET /api/v1/results/{experiment_id}?use_cache=false``, as JSON."""
        started = self._clock()
        try:
            response = self._admin(
                "GET",
                RESULTS.format(experiment_id=experiment_id),
                route=RESULTS,
                headers=self._bearer(),
                params={"use_cache": "false"},
            )
        finally:
            if label is not None:
                phase = (label, "results")
                self.phases[phase] = (
                    self.phases.get(phase, 0.0) + self._clock() - started
                )
        return response.json()

    # -- what a run leaves ----------------------------------------------------------
    def summary(self) -> list[str]:
        """Markdown: requests per route, their rate and each phase's minutes.
        Counts and durations only: no id, value or credential."""
        lines = [
            "| route | requests | 429 | 5xx | seconds waiting for answers | requests a second |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for route, stats in sorted(self.routes.items()):
            rate = stats.requests / stats.seconds if stats.seconds > 0 else 0.0
            lines.append(
                f"| `{route}` | {stats.requests} | {stats.rate_limited} |"
                f" {stats.server_errors} | {stats.seconds:.1f} | {rate:.1f} |"
            )
        lines += ["", "| scenario | phase | minutes |", "|---|---|---:|"]
        for (label, phase), seconds in sorted(self.phases.items()):
            lines.append(f"| {label} | {phase} | {seconds / 60:.2f} |")
        return lines
