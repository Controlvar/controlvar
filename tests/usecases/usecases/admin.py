"""What a case does to the server around the SDKs: sign in, make an API key,
change a flag, and evaluate it directly.

* **Sign-in and the API key** go through the ground-truth replay's driver
  (``tests/ground_truth/driver.py``), unchanged: ``sign_in`` returns its token
  and its key, which has the ``sdk:ruleset`` scope local evaluation needs,
  and hands each to ``mask`` the moment it is minted (the cases mask them in
  the GitHub Actions log). That driver waits out a 429 and sends the request
  again (``Driver._admin``), so afterwards ``sign_in`` asserts that no route
  it used was answered 429 (``routes[route].rate_limited == 0``) and raises
  ``RateLimited`` otherwise.
* **Flag changes and raw evaluations** go through ``Admin``, a small httpx
  helper: ``create_flag``, ``set_rollout`` (``PUT`` of the rollout
  percentage), ``toggle`` (with an empty JSON body: the route answers 422 to
  none, ``test_toggle_requires_json_body``), ``evaluate`` (``GET`` of the
  evaluate route with the API key) and ``ruleset`` (``GET`` of the SDK
  ruleset with the API key: the version the server serves now). Raw
  evaluations are paced at most 80 a second, 80% of the SDK routes' 6,000 a
  minute; everything else at most 4 a second, 80% of the other routes' 300 a
  minute. A 429 raises ``RateLimited`` and is never sent again; any other
  answer outside 2xx raises ``AdminError``.

``429 == 0`` is a pacing guard: the pacing is meant to keep the rate limiter
from ever answering 429, and a 429 means it did not. It says nothing about how
the limiter keys its counters.

Credentials go only into request headers: an error names the method, the
route and the status, never a header, a body or a user's id.

Imports httpx, the ground-truth driver and the standard library; nothing from
``backend``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import driver as ground_truth_driver
import httpx

FLAGS = "/api/v1/feature-flags/"
FLAG = "/api/v1/feature-flags/{flag_id}"
TOGGLE = "/api/v1/feature-flags/{flag_id}/toggle"
EVALUATE = "/api/v1/feature-flags/evaluate/{flag_key}"
RULESET = "/api/v1/sdk/ruleset"

#: The least time between two raw evaluations: 80% of 6,000 a minute.
EVALUATE_INTERVAL = 60 / 4800
#: The least time between two other requests: 80% of 300 a minute.
ADMIN_INTERVAL = 60 / 240


class AdminError(Exception):
    """A request was refused or got no answer. Never carries a credential:
    it names the method, the route and the status."""


class RateLimited(AdminError):
    """The rate limiter answered 429: the pacing did not keep it quiet."""


def _no_mask(value: str | None) -> None:
    return None


def sign_in(
    client: httpx.Client,
    email: str,
    password: str,
    *,
    mask: Callable[[str | None], None] = _no_mask,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """A bearer token for *email* and a new API key with the ``sdk:ruleset``
    scope, through the ground-truth driver; ``RateLimited`` if any of its
    requests was answered 429 (it sends them again, so it would pass). Each
    credential goes to ``mask`` as soon as it is minted, the token before the
    key is asked for (as ``tests/ground_truth/test_replay.py`` does)."""
    session = ground_truth_driver.Driver(client, clock=clock, sleep=sleep)
    session.sign_in(email, password)
    mask(session.token)
    session.create_api_key()
    mask(session.api_key)
    limited = sorted(r for r, stats in session.routes.items() if stats.rate_limited)
    if limited:
        raise RateLimited(f"429 from {', '.join(limited)} while signing in")
    if session.token is None or session.api_key is None:
        raise AdminError("signing in left no token or no API key")
    return session.token, session.api_key


class Admin:
    """Flag changes and raw evaluations against one server, paced."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        token: str,
        api_key: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._token = token
        self._api_key = api_key
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        #: Route -> requests sent.
        self.requests: dict[str, int] = {}

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self._token, self._api_key) if s)

    def _pace(self, kind: str) -> None:
        interval = EVALUATE_INTERVAL if kind == "evaluate" else ADMIN_INTERVAL
        last = self._last.get(kind)
        if last is not None:
            wait = last + interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last[kind] = self._clock()

    def _send(
        self,
        method: str,
        path: str,
        *,
        route: str,
        kind: str,
        headers: Mapping[str, str],
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        self._pace(kind)
        try:
            response = self._client.request(
                method, path, headers=dict(headers), json=json, params=params
            )
        except httpx.TransportError as exc:
            raise AdminError(
                f"{method} {route} got no answer ({type(exc).__name__})"
            ) from None
        self.requests[route] = self.requests.get(route, 0) + 1
        if response.status_code == 429:
            raise RateLimited(f"{method} {route} answered 429")
        if not 200 <= response.status_code < 300:
            raise AdminError(f"{method} {route} answered {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise AdminError(
                f"{method} {route} answered a body that is not JSON"
            ) from None

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def create_flag(
        self,
        key: str,
        *,
        rollout_percentage: int = 0,
        is_active: bool = True,
        targeting_rules: Any = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/feature-flags/``: the created flag."""
        body: dict[str, Any] = {
            "key": key,
            "name": f"Use case {key}"[:100],
            "rollout_percentage": rollout_percentage,
            "is_active": is_active,
        }
        if targeting_rules is not None:
            body["targeting_rules"] = targeting_rules
        return self._send(
            "POST", FLAGS, route=FLAGS, kind="admin", headers=self._bearer(), json=body
        )

    def set_rollout(self, flag_id: str, percentage: int) -> dict[str, Any]:
        """``PUT /api/v1/feature-flags/{flag_id}`` with the rollout percentage."""
        return self._send(
            "PUT",
            FLAG.format(flag_id=flag_id),
            route=FLAG,
            kind="admin",
            headers=self._bearer(),
            json={"rollout_percentage": percentage},
        )

    def toggle(self, flag_id: str) -> dict[str, Any]:
        """``POST /api/v1/feature-flags/{flag_id}/toggle`` with an empty JSON
        body (no ``reason``): the route answers 422 to a request with none."""
        return self._send(
            "POST",
            TOGGLE.format(flag_id=flag_id),
            route=TOGGLE,
            kind="admin",
            headers=self._bearer(),
            json={},
        )

    def ruleset(self) -> dict[str, Any]:
        """``GET /api/v1/sdk/ruleset`` with the API key: the document the
        server serves now, ``version`` included (its ETag is that version in
        quotes). Paced as an admin request: a case reads it a few times."""
        return self._send(
            "GET",
            RULESET,
            route=RULESET,
            kind="admin",
            headers={"X-API-Key": self._api_key},
        )

    def evaluate(self, flag_key: str, user_id: str) -> dict[str, Any]:
        """``GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=...`` with
        the API key: the server's answer, as it is."""
        return self._send(
            "GET",
            EVALUATE.format(flag_key=flag_key),
            route=EVALUATE,
            kind="evaluate",
            headers={"X-API-Key": self._api_key},
            params={"user_id": user_id},
        )
