"""The admin helper against a fake server (httpx's MockTransport), with no
database and no network: sign-in through the ground-truth driver and the 429
it would otherwise wait out unseen, each credential handed to ``mask`` as it
is minted, the 429 every other request raises, the pacing of raw evaluations
and of everything else, and the credential and body each request carries.

The fake answers a toggle with no JSON body 422, as the API does
(``backend/tests/integration/api/test_feature_flags_api.py``,
``test_toggle_requires_json_body``): a helper that drops the body fails here,
not first against a live server.

Time is a fake clock (``harness.Clock``): pacing is checked exactly and takes
no time.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from backend.tests.unit.usecases.harness import Clock, admin

pytestmark = pytest.mark.unit

#: Stand-ins with no entropy, so no secret scanner mistakes them for one.
TOKEN = "T" * 40
KEY = "K" * 32
FLAG_ID = "11111111-1111-4111-8111-111111111111"
USER = "u-" + "7" * 8
LOGIN = "/api/v1/auth/login"
API_KEYS = "/api/v1/api-keys"
RULESET = "/api/v1/sdk/ruleset"


class FakeServer:
    """The routes the helper uses. ``script`` holds answers to give first,
    per path: an ``httpx.Response`` or an exception to raise."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.sent: list[tuple[float, httpx.Request]] = []
        self.script: dict[str, list[Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append((self.clock.now, request))
        path = request.url.path
        if self.script.get(path):
            answer = self.script[path].pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if path == LOGIN:
            return httpx.Response(200, json={"access_token": TOKEN})
        if path == API_KEYS:
            return httpx.Response(201, json={"key": KEY, "id": FLAG_ID})
        if path.startswith("/api/v1/feature-flags/evaluate/"):
            return httpx.Response(200, json={"enabled": True, "reason": "rollout"})
        if path.endswith("/toggle"):
            if request.headers.get("content-type") != "application/json" or not (
                request.content
            ):
                return httpx.Response(422, json={"detail": "body required"})
            return httpx.Response(200, json={"id": FLAG_ID, "is_active": False})
        if path == RULESET:
            return httpx.Response(
                200, json={"schema": 1, "version": "v9"}, headers={"ETag": '"v9"'}
            )
        return httpx.Response(200, json={"id": FLAG_ID})

    def paths(self) -> list[str]:
        return [request.url.path for _, request in self.sent]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def server(clock: Clock) -> FakeServer:
    return FakeServer(clock)


@pytest.fixture
def client(server: FakeServer) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url="http://usecases.test", transport=httpx.MockTransport(server)
    ) as session:
        yield session


@pytest.fixture
def helper(client: httpx.Client, clock: Clock) -> admin.Admin:
    return admin.Admin(
        client, token=TOKEN, api_key=KEY, clock=clock.time, sleep=clock.sleep
    )


def _flag_calls(helper: admin.Admin) -> dict[str, Any]:
    return {
        "create": lambda: helper.create_flag("uc-flag", rollout_percentage=10),
        "rollout": lambda: helper.set_rollout(FLAG_ID, 50),
        "toggle": lambda: helper.toggle(FLAG_ID),
        "evaluate": lambda: helper.evaluate("uc-flag", USER),
        "ruleset": lambda: helper.ruleset(),
    }


# -- sign-in through the ground-truth driver -------------------------------------------
def test_sign_in_returns_the_ground_truth_driver_s_token_and_scoped_key(
    client, server, clock
):
    token, key = admin.sign_in(
        client, "admin@example.com", "admin", clock=clock.time, sleep=clock.sleep
    )
    assert (token, key) == (TOKEN, KEY)
    assert server.paths() == [LOGIN, API_KEYS]
    created = json.loads(server.sent[1][1].content)
    assert created["scopes"] == ["sdk:ruleset"]


def test_a_429_while_signing_in_raises_although_the_driver_sends_it_again(
    client, server, clock
):
    server.script[LOGIN] = [
        httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "3"})
    ]
    with pytest.raises(admin.RateLimited, match=f"429 from {LOGIN} while signing in"):
        admin.sign_in(
            client, "admin@example.com", "admin", clock=clock.time, sleep=clock.sleep
        )
    # The driver waited it out and sent it again: without the check, it passes.
    assert 3.0 in clock.sleeps
    assert server.paths() == [LOGIN, LOGIN, API_KEYS]


def test_each_credential_is_masked_as_it_is_minted(client, server, clock):
    masked: list[tuple[str | None, int]] = []

    def mask(value: str | None) -> None:
        masked.append((value, len(server.sent)))

    admin.sign_in(
        client,
        "admin@example.com",
        "admin",
        mask=mask,
        clock=clock.time,
        sleep=clock.sleep,
    )
    # The token before the key is asked for (one request sent), then the key.
    assert masked == [(TOKEN, 1), (KEY, 2)]


def test_the_token_is_masked_although_the_key_is_refused(client, server, clock):
    server.script[API_KEYS] = [httpx.Response(403, json={"detail": "no"})]
    masked: list[str | None] = []
    with pytest.raises(admin.ground_truth_driver.ReplayError, match="answered 403"):
        admin.sign_in(
            client,
            "admin@example.com",
            "admin",
            mask=masked.append,
            clock=clock.time,
            sleep=clock.sleep,
        )
    assert masked == [TOKEN]


# -- a 429 raises, and is never sent again ---------------------------------------------
@pytest.mark.parametrize("call", ["create", "rollout", "toggle", "evaluate", "ruleset"])
def test_a_429_raises_and_is_not_sent_again(helper, server, call):
    for path in (
        "/api/v1/feature-flags/",
        f"/api/v1/feature-flags/{FLAG_ID}",
        f"/api/v1/feature-flags/{FLAG_ID}/toggle",
        "/api/v1/feature-flags/evaluate/uc-flag",
        RULESET,
    ):
        server.script[path] = [
            httpx.Response(429, json={"detail": "x"}, headers={"Retry-After": "1"})
        ]
    with pytest.raises(admin.RateLimited, match="answered 429"):
        _flag_calls(helper)[call]()
    assert len(server.sent) == 1


@pytest.mark.parametrize("status", [400, 403, 404, 409, 500, 503])
def test_any_other_refusal_raises_naming_the_route_and_no_credential(
    helper, server, status
):
    server.script[f"/api/v1/feature-flags/{FLAG_ID}"] = [
        httpx.Response(status, json={"detail": f"{TOKEN} {KEY} {USER}"})
    ]
    with pytest.raises(admin.AdminError) as raised:
        helper.set_rollout(FLAG_ID, 50)
    message = str(raised.value)
    assert message == f"PUT /api/v1/feature-flags/{{flag_id}} answered {status}"
    assert not isinstance(raised.value, admin.RateLimited)


def test_no_answer_raises_naming_the_route(helper, server):
    server.script["/api/v1/feature-flags/evaluate/uc-flag"] = [
        httpx.ConnectError("refused")
    ]
    with pytest.raises(
        admin.AdminError,
        match=r"GET /api/v1/feature-flags/evaluate/\{flag_key\} got no answer \(ConnectError\)",
    ):
        helper.evaluate("uc-flag", USER)


# -- pacing, on a fake clock -------------------------------------------------------------
def _gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in zip(times, times[1:])]


def test_raw_evaluations_are_paced_at_most_80_a_second(helper, server):
    for index in range(40):
        helper.evaluate("uc-flag", f"u-{index}")
    times = [at for at, _ in server.sent]
    assert len(times) == 40
    assert min(_gaps(times)) >= admin.EVALUATE_INTERVAL - 1e-9
    assert admin.EVALUATE_INTERVAL == pytest.approx(1 / 80, rel=1e-12)


def test_every_other_request_is_paced_at_most_4_a_second(helper, server):
    helper.create_flag("uc-flag", rollout_percentage=10)
    for percentage in (50, 10, 60):
        helper.set_rollout(FLAG_ID, percentage)
    helper.toggle(FLAG_ID)
    times = [at for at, _ in server.sent]
    assert len(times) == 5
    assert min(_gaps(times)) >= admin.ADMIN_INTERVAL - 1e-9
    assert admin.ADMIN_INTERVAL == pytest.approx(1 / 4, rel=1e-12)


def test_the_two_paces_are_kept_apart(helper, server, clock):
    # An evaluation right after an admin request waits for neither's interval.
    helper.toggle(FLAG_ID)
    helper.evaluate("uc-flag", USER)
    assert clock.sleeps == []


def test_the_intervals_are_80_percent_of_the_server_s_limits():
    # The server's own limits (the helper, as harness code, may not import
    # backend; this unit test may).
    from backend.app.middleware.rate_limiter import resolve_rate_limit

    def interval(path: str) -> float:
        limit, window = resolve_rate_limit(path)
        return window / (limit * 0.8)

    assert admin.EVALUATE_INTERVAL == pytest.approx(
        interval("/api/v1/feature-flags/evaluate/uc-flag"), rel=1e-12
    )
    for path in (
        "/api/v1/feature-flags/",
        f"/api/v1/feature-flags/{FLAG_ID}",
        f"/api/v1/feature-flags/{FLAG_ID}/toggle",
    ):
        assert admin.ADMIN_INTERVAL == pytest.approx(interval(path), rel=1e-12)
    # The ruleset is an SDK route (6,000 a minute), read at the admin pace.
    assert admin.ADMIN_INTERVAL >= interval(admin.RULESET)


# -- what each request carries ---------------------------------------------------------------
def test_each_request_carries_its_one_credential_and_its_body(helper, server):
    helper.create_flag("uc-flag", rollout_percentage=10)
    helper.set_rollout(FLAG_ID, 60)
    helper.toggle(FLAG_ID)
    helper.evaluate("uc-flag", USER)
    assert helper.ruleset()["version"] == "v9"
    (create, rollout, toggle, evaluate, ruleset) = (
        request for _, request in server.sent
    )
    for request in (create, rollout, toggle):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert "X-API-Key" not in request.headers
    for request in (evaluate, ruleset):
        assert request.headers["X-API-Key"] == KEY
        assert "Authorization" not in request.headers
    assert (ruleset.method, ruleset.url.path) == ("GET", RULESET)
    # The toggle route answers 422 to a request with no JSON body.
    assert toggle.headers["content-type"] == "application/json"
    assert json.loads(toggle.content) == {}
    assert (create.method, create.url.path) == ("POST", "/api/v1/feature-flags/")
    assert json.loads(create.content) == {
        "key": "uc-flag",
        "name": "Use case uc-flag",
        "rollout_percentage": 10,
        "is_active": True,
    }
    assert (rollout.method, rollout.url.path) == (
        "PUT",
        f"/api/v1/feature-flags/{FLAG_ID}",
    )
    assert json.loads(rollout.content) == {"rollout_percentage": 60}
    assert (toggle.method, toggle.url.path) == (
        "POST",
        f"/api/v1/feature-flags/{FLAG_ID}/toggle",
    )
    assert (evaluate.method, evaluate.url.path) == (
        "GET",
        "/api/v1/feature-flags/evaluate/uc-flag",
    )
    assert dict(evaluate.url.params) == {"user_id": USER}
    assert helper.requests == {
        admin.FLAGS: 1,
        admin.FLAG: 1,
        admin.TOGGLE: 1,
        admin.EVALUATE: 1,
        admin.RULESET: 1,
    }
    assert helper.secrets == (TOKEN, KEY)
