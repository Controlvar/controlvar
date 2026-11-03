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
  ``/track``, ``/batch`` and ``/events`` (6,000 a minute), 48 a minute for
  ``/assign/batch`` (60 a minute), 4 a second for every other route (300 a
  minute). The sign-in is one request per run.
* **Lifecycle.** ``create``, ``start``, ``pause``, ``resume`` (``POST
  .../start`` from PAUSED) and ``complete``. The create answer gives each
  variant's id; every ``/assign/batch`` and ``/assign`` answer must name the
  same id for the same variant, else the run stops (``ReplayError``): an
  ``/events`` send naming the wrong id would make its scenario prove nothing.
* **``/events``** names the experiment and the variant by id
  (``faults.events_request``); the ledger records the variant by name.
* **Hostile sends** (a fault plan's ``hostile`` ops) are raw request text,
  sent as bytes with the API key, outside the ledger: each must get the
  status it expects, and ``hostile`` lists every answer.
* **Expected answers.** Every tracking answer is compared with what its op
  expects (``faults.unexpected``); each difference is kept in
  ``unexpected``, which a fault scenario's gate P reads.
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
from ground_truth import faults, generator, ledger

SIGN_IN = "/api/v1/auth/login"
API_KEYS = "/api/v1/api-keys"
EXPERIMENTS = "/api/v1/experiments/"
#: The results route, as the statistics name it (one row for every experiment).
RESULTS = "/api/v1/results/{experiment_id}"
#: The scope ``/assign/batch`` requires (``backend/app/api/sdk_scope.py``).
SDK_RULESET_SCOPE = "sdk:ruleset"
#: Each lifecycle step after ``create``: the path under the experiment. A
#: resume is a start of a PAUSED experiment.
LIFECYCLE_PATHS = {
    "start": "start",
    "pause": "pause",
    "resume": "start",
    "complete": "complete",
}

#: Per route, the least time between two of its requests, in seconds: 80% of
#: its rate limit.
MIN_INTERVAL = {
    ledger.ASSIGN: 1 / 80,
    ledger.TRACK: 1 / 80,
    ledger.BATCH: 1 / 80,
    ledger.EVENTS: 1 / 80,
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


def _describe(op: generator.Op) -> str:
    """An op as an ``unexpected`` line names it: its kind, its route and, for
    a fault scenario's hostile payload, its class. No id, value or text."""
    if op.kind == "lifecycle":
        return f"lifecycle {op.body.get('action')}"
    text = f"{op.kind} {ledger.ROUTES.get(op.route or '', op.route)}"
    if op.kind == "hostile":
        return f"{text}, class {op.body.get('class')!r}"
    bodies = op.body.get("events") if op.kind == "batch" else [op.body]
    classes = sorted(
        {
            cls
            for body in (bodies if isinstance(bodies, list) else [])
            if isinstance(body, Mapping) and isinstance(body.get("user_id"), str)
            for cls in [faults.f9_class(body["user_id"])]
            if cls is not None
        }
    )
    return f"{text}, class {', '.join(classes)}" if classes else text


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
        #: The plan being sent: each variant's name -> the id its create
        #: answer gave.
        self.variant_ids: dict[str, str] = {}
        #: The plan being sent: every answer that is not the one its op
        #: expects, as "<label> op <n> (<kind> <route>): <why>".
        self.unexpected: list[str] = []
        #: The plan being sent: each hostile send answered, as (class, route,
        #: status), in order.
        self.hostile: list[tuple[str, str, int]] = []

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
        content: bytes | None = None,
    ) -> httpx.Response:
        """One request, paced, counted; a transport error propagates. *content*
        is a raw body, sent as given, in place of *json*."""
        self._pace(route)
        stats = self.routes.setdefault(route, RouteStats())
        started = self._clock()
        try:
            response = self._client.request(
                method,
                path,
                json=json,
                params=params,
                headers=dict(headers),
                content=content,
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

    def _variant_ids(self, plan: generator.Plan, body: Any) -> dict[str, str]:
        """Each of the plan's variants -> the id the create answer gave it."""
        key = plan.scenario.experiment_key
        variants = body.get("variants") if isinstance(body, Mapping) else None
        given = {
            v.get("name"): v.get("id")
            for v in (variants if isinstance(variants, list) else [])
            if isinstance(v, Mapping)
        }
        names = [arm.name for arm in plan.scenario.arms]
        missing = [name for name in names if not isinstance(given.get(name), str)]
        if missing:
            raise ReplayError(
                f"create of experiment {key} answered no id for variant(s) {missing}"
            )
        return {name: given[name] for name in names}

    def _lifecycle(
        self, writer: ledger.Writer, plan: generator.Plan, action: str
    ) -> str | None:
        """Create the plan's experiment, or start, pause, resume or complete
        it; the id ``create`` answers."""
        key = plan.scenario.experiment_key
        while True:
            seq = writer.send_action(action, key)
            if action == "create":
                path, route, body = EXPERIMENTS, EXPERIMENTS, create_body(plan.scenario)
            elif action in LIFECYCLE_PATHS and self._experiment_id is not None:
                step = LIFECYCLE_PATHS[action]
                path = f"{EXPERIMENTS}{self._experiment_id}/{step}"
                route, body = f"{EXPERIMENTS}{{experiment_id}}/{step}", None
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
                answer = _body(response) or {}
                created = answer.get("id") if isinstance(answer, Mapping) else None
                if not isinstance(created, str):
                    raise ReplayError(f"create of experiment {key} answered no id")
                self.variant_ids = self._variant_ids(plan, answer)
                return created
            return None

    def _check_variant_ids(self, route: str, body: Any) -> None:
        """Stop the run when an assignment answer names a variant by another
        id than the create answer gave it."""
        if not isinstance(body, Mapping):
            return
        if route == ledger.ASSIGN_BATCH:
            variants = body.get("variants")
            pairs = [
                (v.get("name") if isinstance(v, Mapping) else None, variant_id)
                for variant_id, v in (
                    variants.items() if isinstance(variants, Mapping) else ()
                )
            ]
        elif route == ledger.ASSIGN:
            pairs = [(body.get("variant_name"), body.get("variant_id"))]
        else:
            return
        wrong = sorted(
            {
                repr(name)
                for name, variant_id in pairs
                if self.variant_ids.get(name) != variant_id
            }
        )
        if wrong:
            raise ReplayError(
                f"{ledger.ROUTES.get(route, route)} named variant(s) {', '.join(wrong)} by"
                " another id than the experiment's create answer gave"
            )

    def _expect(self, where: str, op: generator.Op, status: int, body: Any) -> None:
        problem = faults.unexpected(op, status, body)
        if problem is not None:
            self.unexpected.append(f"{where}: {problem}")

    def _send(
        self,
        writer: ledger.Writer,
        op: generator.Op,
        route: str,
        body: Mapping[str, Any],
        where: str,
        **target: Any,
    ) -> None:
        """One tracking request, recorded, its answer checked against what
        *op* expects; sent again after each 429. *target* is ``exp`` and
        ``variant`` for an ``/events`` send (``ledger.Writer.send``)."""
        while True:
            seq = writer.send(route, body, **target)
            try:
                response = self._request(
                    "POST", route, route=route, headers=self._key(), json=body
                )
            except httpx.TransportError as exc:
                self._no_answer(writer, seq, exc)
            answer = _body(response)
            # Refuses a 5xx, which ends the run.
            writer.answer(seq, response.status_code, answer)
            if response.status_code != 429:
                if 200 <= response.status_code < 300:
                    self._check_variant_ids(route, answer)
                self._expect(where, op, response.status_code, answer)
                return
            self._sleep(retry_after(response))

    def _events(self, writer: ledger.Writer, op: generator.Op, where: str) -> None:
        """An ``events`` op: its body with the experiment and the variant
        named by the ids the create answer gave."""
        variant = op.body.get("variant")
        if self._experiment_id is None:
            raise ReplayError(f"{where}: an /events send before the create")
        if variant is not None and variant not in self.variant_ids:
            raise ReplayError(f"{where}: names variant {variant!r}, which has no id")
        body = faults.events_request(
            op.body, experiment_id=self._experiment_id, variant_ids=self.variant_ids
        )
        self._send(
            writer,
            op,
            ledger.EVENTS,
            body,
            where,
            exp=op.body["experiment_key"],
            variant=variant,
        )

    def _hostile(self, op: generator.Op, where: str) -> None:
        """A ``hostile`` op: its raw text, as bytes, with the API key; outside
        the ledger. Sent again after each 429; no answer is unexpected."""
        cls, route = op.body["class"], op.route
        if route is None:
            raise ReplayError(f"{where}: a hostile op names no route")
        headers = {**self._key(), "Content-Type": "application/json"}
        content = op.body["content"].encode("utf-8")
        while True:
            try:
                response = self._request(
                    "POST", route, route=route, headers=headers, content=content
                )
            except httpx.TransportError as exc:
                self.unexpected.append(
                    f"{where}: class {cls!r} got no answer ({type(exc).__name__})"
                )
                return
            if response.status_code == 429:
                self._sleep(retry_after(response))
                continue
            self.hostile.append((cls, route, response.status_code))
            self._expect(f"{where}, class {cls!r}", op, response.status_code, None)
            return

    @property
    def hostile_sends(self) -> dict[str, int]:
        """Hostile sends answered, per class (``sensitivity``'s
        ``hostile_sends``)."""
        counts: dict[str, int] = {}
        for cls, _, _ in self.hostile:
            counts[cls] = counts.get(cls, 0) + 1
        return counts

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
        no answer) and ``ReplayError`` when a lifecycle step is refused or an
        answer names a variant by another id. ``unexpected`` and ``hostile``
        then describe this plan only.
        """
        label = label or plan.scenario.name
        self._experiment_id = None
        self.variant_ids = {}
        self.unexpected = []
        self.hostile = []
        writer = ledger.Writer(stream, **plan.start(declared), secrets=self.secrets)
        for index, op in enumerate(plan.ops):
            started = self._clock()
            where = f"{label} op {index} ({_describe(op)})"
            if op.kind == "lifecycle":
                created = self._lifecycle(writer, plan, op.body["action"])
                if created is not None:
                    self._experiment_id = created
            elif op.kind == "hostile":
                self._hostile(op, where)
            elif op.route is None:
                raise ReplayError(f"a {op.kind} operation names no route")
            elif op.kind == "events":
                self._events(writer, op, where)
            else:
                self._send(writer, op, op.route, op.body, where)
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
