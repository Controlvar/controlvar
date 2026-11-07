"""Seven async module routes use the database only in worker threads.

``GET /api/v1/auth/sso/oidc/{provider}/login``, ``GET /api/v1/auth/sso/login``,
``POST /api/v1/auth/sso/exchange``, ``GET /api/v1/auth/sso/oidc/{provider}/callback``
and ``POST /api/v1/integrations/webhooks/{jira,salesforce,github}`` are async
routes. Each does its database work in a worker thread, on a session it opens
and closes there: the event loop never checks out a connection, and the
request holds none while it waits for the identity provider or for another
worker.

Driven through the real app with httpx's ``ASGITransport``, as
``backend/tests/integration/api/test_api_key_routes_under_overload.py`` is,
with ``deps.SessionLocal`` pointed at a QueuePool engine on the test database.
The identity provider is stubbed in ``sso_service``; nothing leaves the
process.

* ``test_no_database_step_runs_on_the_event_loop`` -- a checkout listener on
  the engine records the thread of each checkout during one request that
  reaches every database step of its route: none is the event loop's thread.
* ``test_no_connection_is_held_when_a_route_asks_for_a_worker`` -- at each
  worker request (``anyio.to_thread.run_sync``) during that request, and at
  each call to the stubbed identity provider, the pool has no connection
  checked out; and the route asked for one worker per database step, no more
  and no fewer.
* ``test_the_github_email_lookup_runs_off_the_event_loop`` -- the GitHub
  callback's call to the emails endpoint (a blocking ``requests.get``, faked
  here) runs in a worker thread, not on the event loop's.
* ``test_each_refusal_answers_as_before`` -- for each refusal, its status,
  body, ``Location``, ``Set-Cookie`` and ``Cache-Control``, and whether it
  reached the database at all, are pinned: the same answers the routes gave
  when they took a session from ``get_db``. A check that moved before or after
  another changes one of them (several cases fail two checks at once).
* ``test_no_refusal_uses_the_database_on_the_event_loop`` -- the first
  property, over every refusal in that table.
"""

from __future__ import annotations

import gc
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch
from urllib.parse import urlencode

import anyio.to_thread
import httpx
import pytest
import requests
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from backend.app.api import deps
from backend.app.core.config import settings as core_settings
from backend.app.main import app
from backend.app.models.user import User, UserRole
from modules.backend.app.models.integration_config import (
    IntegrationConfig,
    IntegrationType,
)
from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType
from modules.backend.app.services import sso_service
from modules.backend.app.services.integrations import webhook_auth

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
DASH = "https://dash.example.com"
REQUEST_ID = "routes-off-the-loop"
#: The hand-off known-answer pair from test_sso_dashboard_sign_in.py.
SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
HH = "Yw3NKWbEM2aRElRIu7JbT_QSpJxzLbLIq8G4WBvXEN0"
WEBHOOK_SECRET = "routes-off-the-loop-webhook-secret"
EXPIRING_COOKIE = (
    f'{sso_service.OIDC_STATE_COOKIE}=""; HttpOnly; Max-Age=0; Path=/; '
    "SameSite=lax; Secure"
)
#: The stubbed provider's token exchange fails for this code.
FAILING_CODE = "the-provider-refuses-this-code"
#: How long a request's connections get to come back to the pool.
DRAIN_SECONDS = 10

LOGIN = "/api/v1/auth/sso/login"
EXCHANGE = "/api/v1/auth/sso/exchange"


# ---------------------------------------------------------------------------
# The engine, its listeners and the worker spy
# ---------------------------------------------------------------------------


@dataclass
class Seen:
    """What one request did with the pool."""

    checkouts: int
    loop_checkouts: int
    held_at_worker_requests: List[int]
    #: The pool's checked-out count at each call to the (stubbed) provider.
    held_at_provider_calls: List[int]
    loop_thread: Optional[int]
    #: The thread of each call to the provider's emails endpoint.
    emails_lookup_threads: List[int]


@dataclass
class Watch:
    engine: Any
    recording: bool = False
    loop_thread: Optional[int] = None
    checkout_threads: List[int] = field(default_factory=list)
    held_at_worker_requests: List[int] = field(default_factory=list)
    held_at_provider_calls: List[int] = field(default_factory=list)
    emails_lookup_threads: List[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def begin(self) -> None:
        with self.lock:
            self.loop_thread = threading.get_ident()
            self.checkout_threads.clear()
            self.held_at_worker_requests.clear()
            self.held_at_provider_calls.clear()
            self.emails_lookup_threads.clear()
            self.recording = True

    def provider_call(self) -> None:
        """Called by every stubbed provider call, wherever the route makes it."""
        with self.lock:
            if self.recording:
                self.held_at_provider_calls.append(self.engine.pool.checkedout())

    def emails_lookup(self) -> None:
        with self.lock:
            if self.recording:
                self.emails_lookup_threads.append(threading.get_ident())

    def end(self) -> Seen:
        with self.lock:
            self.recording = False
            return Seen(
                checkouts=len(self.checkout_threads),
                loop_checkouts=sum(
                    1 for t in self.checkout_threads if t == self.loop_thread
                ),
                held_at_worker_requests=list(self.held_at_worker_requests),
                held_at_provider_calls=list(self.held_at_provider_calls),
                loop_thread=self.loop_thread,
                emails_lookup_threads=list(self.emails_lookup_threads),
            )


@pytest.fixture
def pool(test_db, monkeypatch) -> Any:
    """``deps.SessionLocal`` on a QueuePool engine with a checkout listener,
    and every worker request recorded with the pool's checked-out count."""
    engine = create_engine(
        test_db.url, pool_size=2, max_overflow=2, pool_timeout=5, pool_pre_ping=True
    )
    assert isinstance(engine.pool, QueuePool)
    watch = Watch(engine=engine)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    @event.listens_for(engine, "checkout")
    def _checkout(_dbapi_connection, _record, _proxy):
        with watch.lock:
            if watch.recording:
                watch.checkout_threads.append(threading.get_ident())

    real_run_sync = anyio.to_thread.run_sync

    async def run_sync(func, *args, **kwargs):
        # On the event loop, at the moment the route asks for a worker.
        with watch.lock:
            if watch.recording:
                watch.held_at_worker_requests.append(engine.pool.checkedout())
        return await real_run_sync(func, *args, **kwargs)

    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync)
    monkeypatch.setattr(
        deps,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    try:
        yield watch
    finally:
        _wait_for_checkins(engine)
        engine.dispose()


def _wait_for_checkins(engine) -> None:
    deadline = time.monotonic() + DRAIN_SECONDS
    while engine.pool.checkedout() and time.monotonic() < deadline:
        gc.collect()
        time.sleep(0.05)


@dataclass(frozen=True)
class Call:
    method: str
    url: str
    headers: Tuple[Tuple[str, str], ...] = ()
    content: Optional[bytes] = None


async def _send(watch: Watch, call: Call) -> Tuple[httpx.Response, Seen]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        watch.begin()
        try:
            response = await http.request(
                call.method,
                call.url,
                headers={"X-Request-ID": REQUEST_ID, **dict(call.headers)},
                content=call.content,
            )
        finally:
            seen = watch.end()
    _wait_for_checkins(watch.engine)
    return response, seen


# ---------------------------------------------------------------------------
# What the routes act on
# ---------------------------------------------------------------------------


@dataclass
class World:
    okta: SSOConfig
    inactive_okta: SSOConfig
    google: SSOConfig
    github: SSOConfig
    saml_domain: str
    microsoft_domain: str
    member_id: uuid.UUID
    inactive_member_id: uuid.UUID
    inactive_member_email: str

    @property
    def domain(self) -> str:
        return self.okta.org_domain

    def new_email(self) -> str:
        return f"new-{uuid.uuid4().hex[:10]}@{self.domain}"

    def github_email(self, kind: str = "new") -> str:
        """An address in the GitHub config's domain; *kind* picks what the
        stubbed emails endpoint answers for it (see ``_stub_the_identity_provider``)."""
        return f"{kind}-{uuid.uuid4().hex[:10]}@{self.github.org_domain}"


def _sso_config(db: Session, provider: SSOProviderType, *, active: bool = True):
    cfg = SSOConfig(
        org_name="Off the loop",
        org_domain=f"off-loop-{uuid.uuid4().hex[:10]}.example.com",
        provider_type=provider,
        entity_id="off-loop-client",
        client_secret="off-loop-client-secret",
        sso_url="https://idp.invalid/oauth2/default",
        role_mapping={},
        is_enforced=False,
        is_active=active,
    )
    db.add(cfg)
    return cfg


def _member(db: Session, email: str, *, active: bool) -> User:
    user = User(
        username=f"off_loop_{uuid.uuid4().hex[:10]}",
        email=email,
        hashed_password="not-a-real-hash",
        is_active=active,
        is_superuser=False,
        role=UserRole.VIEWER,
    )
    db.add(user)
    return user


def _stub_the_identity_provider(monkeypatch, watch: Watch) -> None:
    """The provider answers for whichever email the code names (``as:<email>``).

    Each call records the pool's checked-out count (``watch.provider_call``):
    a request must hold no connection while it waits for the provider.
    GitHub's emails endpoint is reached through ``requests.get``, so that is
    faked too, recording its thread: the email's first word picks its answer
    (``unverified`` -- not verified; ``lookup-fails`` -- no connection).
    """

    async def exchange_oidc_code(config, code, redirect_uri, *, code_verifier, **_):
        watch.provider_call()
        if code == FAILING_CODE:
            raise sso_service.SSORefusal(
                400, "OIDC token exchange failed", sso_service.SSO_FAILED
            )
        email = code.split(":", 1)[1]
        return {"access_token": email, "id_token": email}

    def verify_id_token(config, provider_key, id_token, nonce):
        watch.provider_call()
        return {"sub": f"sub-{id_token}", "email": id_token, "email_verified": True}

    async def get_oidc_user_info(config, access_token, http_client=None):
        watch.provider_call()
        return {
            "email": access_token,
            "name": "Off Loop",
            "sub": f"sub-{access_token}",
            "groups": [],
            "raw": {},
        }

    def requests_get(url, *, headers=None, **_):
        watch.provider_call()
        watch.emails_lookup()
        assert url == sso_service._GITHUB_EMAILS_URL, f"unexpected request to {url}"
        email = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if email.startswith("lookup-fails-"):
            raise requests.ConnectionError("the stub refuses this lookup")
        entries = [
            {
                "email": email,
                "verified": not email.startswith("unverified-"),
                "primary": True,
            }
        ]
        return SimpleNamespace(
            is_redirect=False, raise_for_status=lambda: None, json=lambda: entries
        )

    monkeypatch.setattr(sso_service, "exchange_oidc_code", exchange_oidc_code)
    monkeypatch.setattr(sso_service, "verify_id_token", verify_id_token)
    monkeypatch.setattr(sso_service, "get_oidc_user_info", get_oidc_user_info)
    monkeypatch.setattr(requests, "get", requests_get)


@pytest.fixture
def world(db_session: Session, pool: Watch, monkeypatch) -> Any:
    okta = _sso_config(db_session, SSOProviderType.OKTA)
    inactive_okta = _sso_config(db_session, SSOProviderType.OKTA, active=False)
    google = _sso_config(db_session, SSOProviderType.GOOGLE)
    github = _sso_config(db_session, SSOProviderType.GITHUB)
    saml = _sso_config(db_session, SSOProviderType.SAML)
    microsoft = _sso_config(db_session, SSOProviderType.MICROSOFT)
    db_session.commit()
    member = _member(
        db_session, f"member-{uuid.uuid4().hex[:10]}@x.example", active=True
    )
    inactive_email = f"inactive-{uuid.uuid4().hex[:10]}@{okta.org_domain}"
    inactive = _member(db_session, inactive_email, active=False)
    db_session.commit()

    _stub_the_identity_provider(monkeypatch, pool)
    monkeypatch.setattr(core_settings, "DASHBOARD_ORIGINS", [DASH])
    monkeypatch.setattr(core_settings, "PUBLIC_BASE_URL", None)
    configs = [okta, inactive_okta, google, github, saml, microsoft]
    try:
        yield World(
            okta=okta,
            inactive_okta=inactive_okta,
            google=google,
            github=github,
            saml_domain=saml.org_domain,
            microsoft_domain=microsoft.org_domain,
            member_id=member.id,
            inactive_member_id=inactive.id,
            inactive_member_email=inactive_email,
        )
    finally:
        db_session.rollback()
        for cfg in configs:
            db_session.query(SSOConfig).filter(SSOConfig.id == cfg.id).delete()
        db_session.commit()


@pytest.fixture
def integrations(db_session: Session):
    """Set each integration type's configuration: absent, active with or
    without a ``webhook_secret``, or inactive."""

    def set_states(states: Dict[IntegrationType, str]) -> None:
        for integration_type, state in states.items():
            db_session.query(IntegrationConfig).filter(
                IntegrationConfig.integration_type == integration_type
            ).delete()
            if state == "absent":
                continue
            db_session.add(
                IntegrationConfig(
                    integration_type=integration_type,
                    is_active=state != "inactive",
                    encrypted_config=(
                        {}
                        if state == "no-secret"
                        else {"webhook_secret": WEBHOOK_SECRET}
                    ),
                )
            )
        db_session.commit()

    yield set_states
    db_session.rollback()
    db_session.query(IntegrationConfig).delete()
    db_session.commit()


# ---------------------------------------------------------------------------
# The calls
# ---------------------------------------------------------------------------


def _cookie(
    config: Any, *, dashboard: bool, expired: bool = False, provider: str = "okta"
) -> Tuple[str, str]:
    """(state, Cookie header) for a login started with *config*."""
    kwargs = {"return_to": DASH, "handoff": HH} if dashboard else {}
    if expired:
        with patch.object(sso_service.time, "time", return_value=time.time() - 3600):
            started = sso_service.start_oidc_login(config, provider, **kwargs)
    else:
        started = sso_service.start_oidc_login(config, provider, **kwargs)
    return started.state, f"{sso_service.OIDC_STATE_COOKIE}={started.cookie_value}"


def _callback(
    config: Any,
    *,
    dashboard: bool,
    code: Optional[str] = None,
    state: Optional[str] = "the-cookies",
    error: Optional[str] = None,
    provider: str = "okta",
    cookie_provider: str = "okta",
    cookie: bool = True,
    expired: bool = False,
) -> Call:
    started_state, header = _cookie(
        config, dashboard=dashboard, expired=expired, provider=cookie_provider
    )
    params = {"code": code, "error": error}
    params["state"] = started_state if state == "the-cookies" else state
    query = urlencode({k: v for k, v in params.items() if v is not None})
    headers = (("cookie", header),) if cookie else ()
    return Call("GET", f"/api/v1/auth/sso/oidc/{provider}/callback?{query}", headers)


def _github_callback(world: World, *, dashboard: bool, kind: str = "new") -> Call:
    """The callback of a GitHub sign-in: its email comes from the emails endpoint."""
    return _callback(
        world.github,
        dashboard=dashboard,
        code=f"as:{world.github_email(kind)}",
        provider="github",
        cookie_provider="github",
    )


def _sso_login(domain: Optional[str], return_to: Optional[str] = DASH, handoff=HH):
    params = {"domain": domain, "return_to": return_to, "handoff": handoff}
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return Call("GET", f"{LOGIN}?{query}")


def _exchange(content: bytes) -> Call:
    return Call(
        "POST", EXCHANGE, (("content-type", "application/json"),), content=content
    )


def _exchange_for(user_id: uuid.UUID, secret: str = SECRET) -> Call:
    code = sso_service.issue_handoff_code(user_id, HH)
    return _exchange(json.dumps({"code": code, "secret": secret}).encode())


def _webhook(name: str, body: bytes, secret: str = WEBHOOK_SECRET) -> Call:
    if name == "github":
        headers = (
            ("X-Hub-Signature-256", webhook_auth.expected_signature(secret, body)),
            ("X-GitHub-Event", "ping"),
        )
    else:
        headers = ((webhook_auth.SHARED_SECRET_HEADER, secret),)
    return Call(
        "POST",
        f"/api/v1/integrations/webhooks/{name}",
        headers + (("content-type", "application/json"),),
        content=body,
    )


WEBHOOK_TYPES = {
    "jira": IntegrationType.JIRA,
    "salesforce": IntegrationType.SALESFORCE,
    "github": IntegrationType.GITHUB,
}
EVENT = b'{"webhookEvent": "jira:issue_updated", "event": "ping"}'


# ---------------------------------------------------------------------------
# Gates 1 and 2: one request per route that reaches each of its database steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    id: str
    call: Callable[[World], Call]
    status: int
    #: Database steps the request takes, each in a worker of its own.
    steps: int
    #: Calls the request makes to the (stubbed) identity provider.
    provider_calls: int = 0


ROUTES = [
    Route(
        "oidc_login",
        lambda w: Call(
            "GET",
            f"/api/v1/auth/sso/oidc/okta/login?{urlencode({'org_domain': w.domain})}",
        ),
        302,
        1,
    ),
    Route("sso_login", lambda w: _sso_login(w.domain), 302, 1),
    Route("sso_exchange", lambda w: _exchange_for(w.member_id), 200, 1),
    Route(
        "oidc_callback-json",
        lambda w: _callback(w.okta, dashboard=False, code=f"as:{w.new_email()}"),
        200,
        2,
        provider_calls=3,
    ),
    Route(
        "oidc_callback-dashboard",
        lambda w: _callback(w.okta, dashboard=True, code=f"as:{w.new_email()}"),
        302,
        2,
        provider_calls=3,
    ),
    Route("jira_webhook", lambda w: _webhook("jira", EVENT), 200, 1),
    Route("salesforce_webhook", lambda w: _webhook("salesforce", EVENT), 200, 1),
    Route("github_webhook", lambda w: _webhook("github", EVENT), 200, 1),
]


def _ids(items) -> List[str]:
    return [item.id for item in items]


@pytest.mark.regression
@pytest.mark.parametrize("route", ROUTES, ids=_ids(ROUTES))
async def test_no_database_step_runs_on_the_event_loop(
    route: Route, pool: Watch, world: World, integrations
):
    integrations(dict.fromkeys(IntegrationType, "active"))
    response, seen = await _send(pool, route.call(world))

    assert response.status_code == route.status, response.text
    assert seen.checkouts >= route.steps, (
        f"{route.id}: {seen.checkouts} checkouts, want at least {route.steps}: "
        "the request did not reach its database steps, so it proves nothing"
    )
    assert seen.loop_checkouts == 0, (
        f"{route.id}: {seen.loop_checkouts} of {seen.checkouts} checkouts on the "
        "event loop's thread, want 0"
    )


@pytest.mark.regression
@pytest.mark.parametrize("route", ROUTES, ids=_ids(ROUTES))
async def test_no_connection_is_held_when_a_route_asks_for_a_worker(
    route: Route, pool: Watch, world: World, integrations
):
    integrations(dict.fromkeys(IntegrationType, "active"))
    response, seen = await _send(pool, route.call(world))

    assert response.status_code == route.status, response.text
    assert seen.held_at_worker_requests == [0] * route.steps, (
        f"{route.id}: connections checked out at each worker request "
        f"{seen.held_at_worker_requests}, want {[0] * route.steps} (one worker "
        "per database step, none asked for while a connection is held)"
    )
    assert seen.held_at_provider_calls == [0] * route.provider_calls, (
        f"{route.id}: connections checked out at each identity provider call "
        f"{seen.held_at_provider_calls}, want {[0] * route.provider_calls} (none "
        "held while the request waits for the provider)"
    )


@pytest.mark.regression
@pytest.mark.parametrize("dashboard", [False, True], ids=["json", "dashboard"])
async def test_the_github_email_lookup_runs_off_the_event_loop(
    dashboard: bool, pool: Watch, world: World
):
    """GitHub has no ID token, so the callback asks its emails endpoint, over a
    blocking HTTP client: that call must run in a worker, holding no
    connection, like the database steps around it."""
    response, seen = await _send(pool, _github_callback(world, dashboard=dashboard))

    if dashboard:
        assert response.status_code == 302, response.text
        assert response.headers["location"].startswith(f"{DASH}/sso/complete#code=")
    else:
        assert response.status_code == 200, response.text
    assert len(seen.emails_lookup_threads) == 1, seen.emails_lookup_threads
    assert seen.emails_lookup_threads[0] != seen.loop_thread, (
        "the GitHub emails lookup (requests.get) ran on the event loop's thread"
    )
    # The config lookup, the emails lookup and the provisioning: one worker each.
    assert seen.held_at_worker_requests == [0, 0, 0], seen.held_at_worker_requests
    assert seen.held_at_provider_calls == [0, 0, 0], seen.held_at_provider_calls
    assert seen.loop_checkouts == 0, seen.loop_checkouts


# ---------------------------------------------------------------------------
# Gate 3: the refusals answer as they did
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    status: int
    body: str
    location: Optional[str] = None
    set_cookie: Tuple[str, ...] = ()
    cache_control: Optional[str] = None
    #: Whether the request checked out a connection at all.
    database: bool = False


def _json(detail: str) -> str:
    return json.dumps({"detail": detail}, ensure_ascii=False, separators=(",", ":"))


def _to_login(sso_error: str, provider: Optional[str] = "okta", **extra) -> str:
    params = [("sso_error", sso_error)]
    if provider:
        params.append(("provider", provider))
    params += list(extra.items())
    params.append(("request_id", REQUEST_ID))
    return f"{DASH}/login?{urlencode(params)}"


def _refused_to_dashboard(sso_error: str, *, database: bool, **kwargs) -> Answer:
    return Answer(
        302,
        "",
        location=_to_login(sso_error, **kwargs),
        set_cookie=(EXPIRING_COOKIE,),
        cache_control="no-store",
        database=database,
    )


def _refused_json(status: int, detail: str, *, database: bool, cookie=False):
    return Answer(
        status,
        _json(detail),
        set_cookie=(EXPIRING_COOKIE,) if cookie else (),
        database=database,
    )


NOT_CONFIGURED_OKTA = "No active SSO config found for provider 'okta'"
UNSUPPORTED = "This single sign-on provider is not supported yet"
EXCHANGE_REFUSED = "This sign-in could not be completed; start it again"
WEBHOOK_REFUSED = _json("Webhook authentication failed")
OTHER_DOMAIN = "someone@elsewhere.example"
EMAIL_UNVERIFIED = "The identity provider has not verified this account's email address"
DOMAIN_REFUSED = (
    "This account's email is not in the domain this single sign-on is configured for"
)


@dataclass(frozen=True)
class Refusal:
    id: str
    call: Callable[[World], Call]
    answer: Answer
    #: DASHBOARD_ORIGINS for the case (a primary dashboard origin or none).
    dashboard: bool = True
    integrations: Tuple[Tuple[IntegrationType, str], ...] = ()


def _webhook_refusals(name: str) -> List[Refusal]:
    kind = WEBHOOK_TYPES[name]
    return [
        Refusal(
            f"{name}-no-integration",
            lambda w, n=name: _webhook(n, EVENT),
            Answer(401, WEBHOOK_REFUSED, database=True),
            integrations=((kind, "absent"),),
        ),
        Refusal(
            f"{name}-inactive-integration",
            lambda w, n=name: _webhook(n, EVENT),
            Answer(401, WEBHOOK_REFUSED, database=True),
            integrations=((kind, "inactive"),),
        ),
        Refusal(
            f"{name}-no-webhook-secret",
            lambda w, n=name: _webhook(n, EVENT),
            Answer(401, WEBHOOK_REFUSED, database=True),
            integrations=((kind, "no-secret"),),
        ),
        Refusal(
            f"{name}-wrong-secret",
            lambda w, n=name: _webhook(n, EVENT, secret="not-the-secret"),
            Answer(401, WEBHOOK_REFUSED, database=True),
            integrations=((kind, "active"),),
        ),
        Refusal(
            # Two failures: the sender is checked before the body is read.
            f"{name}-no-integration-and-malformed-body",
            lambda w, n=name: _webhook(n, b"not json"),
            Answer(401, WEBHOOK_REFUSED, database=True),
            integrations=((kind, "absent"),),
        ),
        Refusal(
            f"{name}-wrong-secret-and-malformed-body",
            lambda w, n=name: _webhook(n, b"not json", secret="not-the-secret"),
            Answer(401, WEBHOOK_REFUSED, database=True),
            integrations=((kind, "active"),),
        ),
        Refusal(
            f"{name}-malformed-body",
            lambda w, n=name: _webhook(n, b"not json"),
            Answer(400, _json("Invalid JSON payload"), database=True),
            integrations=((kind, "active"),),
        ),
        Refusal(
            f"{name}-accepted",
            lambda w, n=name: _webhook(n, EVENT),
            Answer(200, '{"status":"received"}', database=True),
            integrations=((kind, "active"),),
        ),
    ]


REFUSALS = [
    # --- GET /oidc/{provider}/login ------------------------------------------
    Refusal(
        "oidc_login-unsupported-provider",
        lambda w: Call(
            "GET", f"/api/v1/auth/sso/oidc/microsoft/login?org_domain={w.domain}"
        ),
        _refused_json(400, UNSUPPORTED, database=False),
    ),
    Refusal(
        "oidc_login-unknown-domain",
        lambda w: Call("GET", "/api/v1/auth/sso/oidc/okta/login?org_domain=nx.example"),
        _refused_json(404, NOT_CONFIGURED_OKTA, database=True),
    ),
    Refusal(
        "oidc_login-inactive-config",
        lambda w: Call(
            "GET",
            f"/api/v1/auth/sso/oidc/okta/login?org_domain={w.inactive_okta.org_domain}",
        ),
        _refused_json(404, NOT_CONFIGURED_OKTA, database=True),
    ),
    Refusal(
        "oidc_login-no-config-of-that-provider",
        lambda w: Call("GET", "/api/v1/auth/sso/oidc/nope/login"),
        _refused_json(
            404, "No active SSO config found for provider 'nope'", database=True
        ),
    ),
    # --- GET /login ------------------------------------------------------------
    Refusal(
        "sso_login-no-return-to",
        lambda w: _sso_login(w.domain, return_to=None),
        _refused_json(
            400, "return_to is not a dashboard origin this API accepts", database=False
        ),
    ),
    Refusal(
        # Two failures: the parameters are checked before any configuration.
        "sso_login-foreign-return-to-and-unknown-domain",
        lambda w: _sso_login("nx.example", return_to="https://evil.example"),
        _refused_json(
            400, "return_to is not a dashboard origin this API accepts", database=False
        ),
    ),
    Refusal(
        "sso_login-short-handoff-and-unknown-domain",
        lambda w: _sso_login("nx.example", handoff=HH[:-1]),
        _refused_json(400, "handoff must be 43 base64url characters", database=False),
    ),
    Refusal(
        "sso_login-malformed-domain",
        lambda w: _sso_login("not a domain"),
        _refused_json(
            400, "domain must be an email domain such as example.com", database=False
        ),
    ),
    Refusal(
        "sso_login-unknown-domain",
        lambda w: _sso_login("nx.example"),
        _refused_to_dashboard("sso_not_configured", provider=None, database=True),
    ),
    Refusal(
        "sso_login-inactive-config",
        lambda w: _sso_login(w.inactive_okta.org_domain),
        _refused_to_dashboard("sso_not_configured", provider=None, database=True),
    ),
    Refusal(
        "sso_login-saml-only-domain",
        lambda w: _sso_login(w.saml_domain),
        _refused_to_dashboard("sso_saml_only", provider=None, database=True),
    ),
    Refusal(
        "sso_login-unsupported-provider",
        lambda w: _sso_login(w.microsoft_domain),
        _refused_to_dashboard("sso_failed", provider="microsoft", database=True),
    ),
    # --- POST /exchange ----------------------------------------------------------
    Refusal(
        "sso_exchange-not-json",
        lambda w: _exchange(b"not json"),
        _refused_json(400, EXCHANGE_REFUSED, database=False),
    ),
    Refusal(
        "sso_exchange-not-an-object",
        lambda w: _exchange(b"[]"),
        _refused_json(400, EXCHANGE_REFUSED, database=False),
    ),
    Refusal(
        "sso_exchange-bad-code",
        lambda w: _exchange(json.dumps({"code": "x", "secret": SECRET}).encode()),
        _refused_json(400, EXCHANGE_REFUSED, database=False),
    ),
    Refusal(
        "sso_exchange-wrong-secret",
        lambda w: _exchange_for(w.member_id, secret="B" * 43),
        _refused_json(400, EXCHANGE_REFUSED, database=False),
    ),
    Refusal(
        "sso_exchange-unknown-account",
        lambda w: _exchange_for(uuid.uuid4()),
        _refused_json(400, EXCHANGE_REFUSED, database=True),
    ),
    Refusal(
        "sso_exchange-inactive-account",
        lambda w: _exchange_for(w.inactive_member_id),
        _refused_json(400, EXCHANGE_REFUSED, database=True),
    ),
    # --- GET /oidc/{provider}/callback, a login started with /oidc/.../login --
    Refusal(
        "callback-no-cookie-no-dashboard",
        lambda w: _callback(w.okta, dashboard=False, code="c", cookie=False),
        _refused_json(
            400,
            "OIDC sign-in was not started in this browser, or its cookie was not sent",
            database=False,
            cookie=True,
        ),
        dashboard=False,
    ),
    Refusal(
        "callback-no-cookie-to-the-dashboard",
        lambda w: _callback(w.okta, dashboard=False, code="c", cookie=False),
        _refused_to_dashboard("sso_state", database=False),
    ),
    Refusal(
        # Two failures: the provider is checked before the state.
        "callback-unsupported-provider-and-wrong-state",
        lambda w: _callback(
            w.okta, dashboard=False, code="c", provider="microsoft", state="wrong"
        ),
        _refused_json(400, UNSUPPORTED, database=False, cookie=True),
    ),
    Refusal(
        "callback-provider-error-and-no-code",
        lambda w: _callback(w.okta, dashboard=False, error="access_denied"),
        _refused_json(
            400,
            "OIDC provider returned error: access_denied",
            database=False,
            cookie=True,
        ),
    ),
    Refusal(
        "callback-no-code",
        lambda w: _callback(w.okta, dashboard=False),
        _refused_json(
            400, "Authorization code is required", database=False, cookie=True
        ),
    ),
    Refusal(
        "callback-wrong-state",
        lambda w: _callback(w.okta, dashboard=False, code="c", state="wrong"),
        _refused_json(
            400,
            "OIDC state does not match this browser's sign-in",
            database=False,
            cookie=True,
        ),
    ),
    Refusal(
        "callback-unknown-config",
        lambda w: _callback(
            SimpleNamespace(id=uuid.uuid4()), dashboard=False, code="c"
        ),
        _refused_json(404, NOT_CONFIGURED_OKTA, database=True, cookie=True),
    ),
    Refusal(
        "callback-inactive-config",
        lambda w: _callback(w.inactive_okta, dashboard=False, code="c"),
        _refused_json(404, NOT_CONFIGURED_OKTA, database=True, cookie=True),
    ),
    Refusal(
        "callback-config-of-another-provider",
        lambda w: _callback(w.google, dashboard=False, code="c"),
        _refused_json(404, NOT_CONFIGURED_OKTA, database=True, cookie=True),
    ),
    Refusal(
        "callback-the-provider-refuses-the-code",
        lambda w: _callback(w.okta, dashboard=False, code=FAILING_CODE),
        _refused_json(400, "OIDC token exchange failed", database=True, cookie=True),
    ),
    Refusal(
        "callback-email-outside-the-domain",
        lambda w: _callback(w.okta, dashboard=False, code=f"as:{OTHER_DOMAIN}"),
        _refused_json(403, DOMAIN_REFUSED, database=True, cookie=True),
    ),
    Refusal(
        "callback-inactive-account",
        lambda w: _callback(
            w.okta, dashboard=False, code=f"as:{w.inactive_member_email}"
        ),
        _refused_json(400, "Inactive user", database=True, cookie=True),
    ),
    # --- the same callback, a sign-in started from the dashboard ---------------
    Refusal(
        "dashboard-callback-provider-error-and-no-code",
        lambda w: _callback(w.okta, dashboard=True, error="access_denied"),
        _refused_to_dashboard(
            "sso_idp_error", idp_error="access_denied", database=False
        ),
    ),
    Refusal(
        "dashboard-callback-no-code",
        lambda w: _callback(w.okta, dashboard=True),
        _refused_to_dashboard("sso_failed", database=False),
    ),
    Refusal(
        "dashboard-callback-wrong-state",
        lambda w: _callback(w.okta, dashboard=True, code="c", state="wrong"),
        _refused_to_dashboard("sso_state", database=False),
    ),
    Refusal(
        "dashboard-callback-expired",
        lambda w: _callback(w.okta, dashboard=True, code="c", expired=True),
        _refused_to_dashboard("sso_expired", database=False),
    ),
    Refusal(
        "dashboard-callback-unknown-config",
        lambda w: _callback(SimpleNamespace(id=uuid.uuid4()), dashboard=True, code="c"),
        _refused_to_dashboard("sso_not_configured", database=True),
    ),
    Refusal(
        "dashboard-callback-inactive-config",
        lambda w: _callback(w.inactive_okta, dashboard=True, code="c"),
        _refused_to_dashboard("sso_not_configured", database=True),
    ),
    Refusal(
        "dashboard-callback-the-provider-refuses-the-code",
        lambda w: _callback(w.okta, dashboard=True, code=FAILING_CODE),
        _refused_to_dashboard("sso_failed", database=True),
    ),
    Refusal(
        "dashboard-callback-email-outside-the-domain",
        lambda w: _callback(w.okta, dashboard=True, code=f"as:{OTHER_DOMAIN}"),
        _refused_to_dashboard("sso_domain", database=True),
    ),
    Refusal(
        "dashboard-callback-inactive-account",
        lambda w: _callback(
            w.okta, dashboard=True, code=f"as:{w.inactive_member_email}"
        ),
        _refused_to_dashboard("sso_inactive", database=True),
    ),
    # --- the callback of a GitHub sign-in: its emails lookup refuses -----------
    Refusal(
        "callback-github-email-not-verified",
        lambda w: _github_callback(w, dashboard=False, kind="unverified"),
        _refused_json(403, EMAIL_UNVERIFIED, database=True, cookie=True),
    ),
    Refusal(
        "callback-github-emails-lookup-fails",
        lambda w: _github_callback(w, dashboard=False, kind="lookup-fails"),
        _refused_json(400, "OIDC userinfo fetch failed", database=True, cookie=True),
    ),
    Refusal(
        "dashboard-callback-github-email-not-verified",
        lambda w: _github_callback(w, dashboard=True, kind="unverified"),
        _refused_to_dashboard("sso_unverified", provider="github", database=True),
    ),
    Refusal(
        "dashboard-callback-github-emails-lookup-fails",
        lambda w: _github_callback(w, dashboard=True, kind="lookup-fails"),
        _refused_to_dashboard("sso_failed", provider="github", database=True),
    ),
    # --- POST /webhooks/{jira,salesforce,github} ---------------------------------
    *_webhook_refusals("jira"),
    *_webhook_refusals("salesforce"),
    *_webhook_refusals("github"),
]


def _answer(response: httpx.Response, seen: Seen) -> Answer:
    return Answer(
        status=response.status_code,
        body=response.text,
        location=response.headers.get("location"),
        set_cookie=tuple(response.headers.get_list("set-cookie")),
        cache_control=response.headers.get("cache-control"),
        database=seen.checkouts > 0,
    )


async def _refuse(case: Refusal, pool: Watch, world: World, integrations, monkeypatch):
    monkeypatch.setattr(
        core_settings, "DASHBOARD_ORIGINS", [DASH] if case.dashboard else []
    )
    if case.integrations:
        integrations(dict(case.integrations))
    return await _send(pool, case.call(world))


@pytest.mark.parametrize("case", REFUSALS, ids=_ids(REFUSALS))
async def test_each_refusal_answers_as_before(
    case: Refusal, pool: Watch, world: World, integrations, monkeypatch
):
    response, seen = await _refuse(case, pool, world, integrations, monkeypatch)
    assert _answer(response, seen) == case.answer


@pytest.mark.regression
@pytest.mark.parametrize("case", REFUSALS, ids=_ids(REFUSALS))
async def test_no_refusal_uses_the_database_on_the_event_loop(
    case: Refusal, pool: Watch, world: World, integrations, monkeypatch
):
    response, seen = await _refuse(case, pool, world, integrations, monkeypatch)
    assert response.status_code == case.answer.status, response.text
    assert seen.loop_checkouts == 0, (
        f"{case.id}: {seen.loop_checkouts} of {seen.checkouts} checkouts on the "
        "event loop's thread, want 0"
    )
    assert seen.loop_thread not in seen.emails_lookup_threads, (
        f"{case.id}: the GitHub emails lookup (requests.get) ran on the event "
        "loop's thread"
    )
    assert set(seen.held_at_provider_calls) <= {0}, (
        f"{case.id}: connections checked out at each identity provider call "
        f"{seen.held_at_provider_calls}, want 0 at each"
    )
