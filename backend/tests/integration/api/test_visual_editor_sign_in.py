"""The visual editor's one-time sign-in and its session (#1545): opening the
editor from the dashboard, the editor link's claim from the site's page, the
exchange for an editor session, and the session's own routes, on Postgres
through the real routes.

Users of each role sign in with local tokens. TestClient sends no ``Origin``:
every request here sets the one it means, and every "wrong Origin" refusal is
paired with the same call from an accepted origin.

The rows of PLAN-v3.1's VERIFICATION held here, each with the defect that
fails it (the failing line is the assertion's message or comparison):

* Ticket  the link carries exactly 43 base64url characters in its fragment,
          never in its query, for 120 seconds, and only its hash is stored.
* V115    a claim of the wrong form, or one already stored, is refused and
          the ticket stays unused; a good one is stored and nothing comes
          back; the exchange takes a claim, never a ticket.
* V79     a used ticket or claim presented again, at once or after the
          ticket and the claim have expired, ends the ticket and everything
          descended from it; two at once store exactly one row; a link
          presented again while its claim is being written leaves nothing of
          it live; nothing is written for a ticket or a claim that expired
          after it was spent.
* V80     opening the editor again ends every earlier editor credential of
          that experiment, whoever holds it, and nothing of another one; two
          opens at once leave one link, and a re-open during an exchange
          leaves one editor.
* V81     the audit entries of each case, exactly.
* V82     the dashboard's origins for the dashboard's routes, the
          experiment's sites for the page's route.
* V84     the dashboard origins the mint sees, per environment.
* V26     who may open the editor, and a session whose user may no longer
          change experiments is refused.
* V65     a dated draft is not opened, and a session does not write to one.
* V27     the session's token works on its three routes, as a bearer token,
          and nowhere else, as a bearer token or as an API key; each
          credential lasts its time; another experiment's variant is not
          saved.
* V72a    a session's token makes no preview link.
* K3      an ended session's 401 says why: its text and its reason.
* V67     a start, by the route or the scheduler, ends the session (401
          started, not 400) and records it.
* V30     a save is one experiment change by the session's user, naming the
          session; a dashboard save names none.
* V93     no IP address is stored: after every route of the sign-in and the
          session, no text or JSON column holds the client's address, and no
          compliance event made since names one.
* Also: the session's GET is the exchange's answer without the token; a save
          keeps every other variant's changes; signing out is recorded, and
          works on a draft the session can no longer edit; a browser's
          preflighted request reaches the claim route; nothing stored or
          logged holds a credential or its hash; the editor cannot be opened
          while CORS_ORIGINS holds ``*``, even from a link made before; the
          session routes check the ``Origin`` before the token; the texts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints import visual_editor as editor_endpoints
from backend.app.core.config import DevSettings, ProdSettings, TestSettings, settings
from backend.app.core.permissions import (
    Action,
    ResourceType,
    get_permission_error_message,
)
from backend.app.core.security import hash_api_key
from backend.app.db import request_sessions
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.compliance_audit_event import ComplianceAuditEvent
from backend.app.models.experiment import Experiment
from backend.app.models.user import UserRole
from backend.app.models.visual_token import (
    EDITOR_CLAIM,
    EDITOR_SESSION,
    EDITOR_TICKET,
    REOPENED,
    TICKET_REUSED,
    VisualToken,
)
from backend.app.schemas import visual_editor as editor_schemas
from backend.app.services import visual_editor, visual_origins
from backend.tests.integration.api.route_helpers import (
    api_key,
    auth,
    client_address_count,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
CLAIM = "/api/v1/visual-editor/tickets/claim"
SESSIONS = "/api/v1/visual-editor/sessions"
PREFIX = "vis1545edit-"
TAG = "edit1545"
SCHEMA = "test_experimentation"

SHOP = "https://shop.example.com"
SHOP2 = "https://www.shop.example.com"
DASHBOARD = "https://dash.example.com"
DASHBOARD2 = "http://localhost:3100"
#: Origins that are neither a dashboard origin nor a site, as a browser
#: could send them.
WRONG_ORIGINS = {
    "evil": "https://evil.test",
    "null": "null",
    "suffix": "https://dash.example.com.evil.test",
    "http": "http://dash.example.com",
    "port": "https://dash.example.com:8443",
}
CHANGES = {
    "version": 1,
    "changes": [{"op": "set_text", "selector": "#headline", "text": "Hello"}],
}
PAGE = {"match": "prefix", "url": f"{SHOP}/"}
CONFIG = {
    "version": 1,
    "editor_url": f"{SHOP}/start?x=1",
    "pages": [PAGE, {"match": "prefix", "url": f"{SHOP2}/"}],
    "goals": [
        {"event_name": "click_buy", "kind": "click", "selector": "#buy", "page": PAGE}
    ],
}
UPDATE_REFUSED = get_permission_error_message(ResourceType.EXPERIMENT, Action.UPDATE)
BASE64URL_43 = re.compile(r"[A-Za-z0-9_-]{43}")


@pytest.fixture(autouse=True)
def _sign_in_and_origins(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "BACKEND_CORS_ORIGINS", [])
    monkeypatch.setattr(settings, "CORS_ORIGINS", [SHOP, SHOP2])
    monkeypatch.setattr(settings, "DASHBOARD_ORIGINS", [DASHBOARD, DASHBOARD2])
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)


def _session(db_session):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)
    session = factory()
    session.execute(text(f"SET search_path TO {SCHEMA}"))
    return session


def _has_table(session, name: str) -> bool:
    return (
        session.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = :t"
            ),
            {"s": SCHEMA, "t": name},
        ).scalar_one()
        > 0
    )


@pytest.fixture(autouse=True)
def _remove_what_the_tests_made(db_session):
    yield
    session = _session(db_session)
    try:
        if _has_table(session, "user_custom_roles"):
            session.execute(
                text(
                    "DELETE FROM user_custom_roles WHERE user_id IN "
                    f"(SELECT id FROM users WHERE username LIKE '{TAG}_%')"
                )
            )
            session.execute(
                text("DELETE FROM custom_roles WHERE name LIKE :p"), {"p": f"{TAG}-%"}
            )
        session.execute(
            text("DELETE FROM experiments WHERE name LIKE :p"), {"p": f"{PREFIX}%"}
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    """The application, with ``deps.get_db`` overridden for the test."""
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    async def registering_get_db():
        session = factory()
        request_sessions.register(session)
        try:
            yield session
        finally:
            try:
                session.close()
            finally:
                request_sessions.unregister(session)

    app.dependency_overrides[deps.get_db] = registering_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _user(db_session, role: UserRole, superuser: bool = False):
    return make_user(db_session, role, superuser=superuser, tag=TAG)


@pytest.fixture
def developer(db_session):
    return _user(db_session, UserRole.DEVELOPER)


def _draft(client, user, config: Optional[Dict[str, Any]] = None) -> str:
    body = {
        "name": f"{PREFIX}{uuid.uuid4().hex[:8]}",
        "experiment_type": "a_b",
        "visual_config": config or CONFIG,
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 50},
            {
                "name": "B",
                "is_control": False,
                "traffic_allocation": 50,
                "visual_changes": CHANGES,
            },
        ],
        "metrics": [
            {"name": "Buy", "event_name": "click_buy", "metric_type": "conversion"}
        ],
    }
    response = client.post(f"{EXPERIMENTS}/", json=body, headers=auth(user))
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def experiment(client, developer) -> str:
    return _draft(client, developer)


def _origin(origin: Optional[str]) -> Dict[str, str]:
    return {} if origin is None else {"Origin": origin}


def _mint(client, user, experiment_id: str, origin: Optional[str] = DASHBOARD):
    return client.post(
        f"{EXPERIMENTS}/{experiment_id}/visual/editor-tickets",
        headers={**auth(user), **_origin(origin)},
    )


def _ticket_of(response) -> str:
    assert response.status_code == 201, response.text
    _, _, fragment = response.json()["open_url"].partition("#")
    assert fragment.startswith("controlvar-editor="), fragment
    return fragment[len("controlvar-editor=") :]


def _ticket(client, user, experiment_id: str) -> str:
    return _ticket_of(_mint(client, user, experiment_id))


def _value() -> str:
    """A claim as the page draws it: 32 random bytes, 43 base64url characters."""
    return secrets.token_urlsafe(32)


def _claim(client, ticket: str, claim: str, origin: Optional[str] = SHOP):
    return client.post(
        CLAIM, json={"ticket": ticket, "claim": claim}, headers=_origin(origin)
    )


def _exchange(
    client, claim: str, page_origin: str = SHOP, origin: Optional[str] = DASHBOARD
):
    return client.post(
        SESSIONS,
        json={"claim": claim, "page_origin": page_origin},
        headers=_origin(origin),
    )


def _claimed(client, user, experiment_id: str) -> Dict[str, str]:
    ticket = _ticket(client, user, experiment_id)
    claim = _value()
    response = _claim(client, ticket, claim)
    assert response.status_code == 201, response.text
    return {"ticket": ticket, "claim": claim}


def _opened(client, user, experiment_id: str) -> Dict[str, str]:
    chain = _claimed(client, user, experiment_id)
    response = _exchange(client, chain["claim"])
    assert response.status_code == 201, response.text
    return {**chain, "token": response.json()["token"]}


def _row(db_session, value: str) -> Optional[VisualToken]:
    session = _session(db_session)
    try:
        return (
            session.query(VisualToken)
            .filter(VisualToken.token_hash == hash_api_key(value))
            .one_or_none()
        )
    finally:
        session.close()


def _rows(db_session, experiment_id: str, kind: Optional[str] = None):
    session = _session(db_session)
    try:
        query = session.query(VisualToken).filter(
            VisualToken.experiment_id == uuid.UUID(experiment_id)
        )
        if kind is not None:
            query = query.filter(VisualToken.kind == kind)
        return query.all()
    finally:
        session.close()


def _ended_why(db_session, value: str) -> Optional[str]:
    """Why the credential *value* (a ticket or a claim) was ended; ``None``
    while it is live."""
    row = _row(db_session, value)
    assert row is not None, "no such credential"
    return row.revoked_reason


CURRENT = f"{SESSIONS}/current"


def _bearer(token: str, origin: Optional[str] = None) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", **_origin(origin)}


def _current(client, token: str, origin: Optional[str] = None):
    """GET the session; with no ``Origin``, as the dashboard's own page sends
    it through its /api/ path."""
    return client.get(CURRENT, headers=_bearer(token, origin))


def _session_says(client, token: str):
    """(status, reason) of the session's GET: (200, None) while it works."""
    response = _current(client, token)
    if response.status_code == 401:
        return 401, response.json()["reason"]
    return response.status_code, None


def _save(
    client, token: str, variant_id: str, changes, origin: Optional[str] = DASHBOARD
):
    return client.put(
        f"{CURRENT}/variants/{variant_id}/changes",
        json=changes,
        headers=_bearer(token, origin),
    )


def _sign_out(client, token: str, origin: Optional[str] = DASHBOARD):
    return client.delete(CURRENT, headers=_bearer(token, origin))


def _variant(client, user, experiment_id: str, name: str) -> str:
    detail = client.get(f"{EXPERIMENTS}/{experiment_id}", headers=auth(user))
    assert detail.status_code == 200, detail.text
    return next(v["id"] for v in detail.json()["variants"] if v["name"] == name)


def _entries(db_session, experiment_id: str) -> List[str]:
    """The experiment's visual editor audit entries, oldest first, as
    ``opened``, ``ticket_reused`` or ``closed(<reason>)``."""
    session = _session(db_session)
    try:
        rows = (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_id == uuid.UUID(experiment_id),
                AuditLog.action_type.like("visual_editor_%"),
            )
            .order_by(AuditLog.timestamp)
            .all()
        )
    finally:
        session.close()
    found = []
    for row in rows:
        if row.action_type == "visual_editor_session_closed":
            found.append(f"closed({json.loads(row.new_value)['reason']})")
        elif row.action_type == "visual_editor_session_opened":
            found.append("opened")
        else:
            found.append("ticket_reused")
    return found


def _audit_rows(db_session, experiment_id: str) -> List[AuditLog]:
    session = _session(db_session)
    try:
        return (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_id == uuid.UUID(experiment_id),
                AuditLog.action_type.like("visual_editor_%"),
            )
            .order_by(AuditLog.timestamp)
            .all()
        )
    finally:
        session.close()


def _set_start_date(db_session, experiment_id: str) -> None:
    session = _session(db_session)
    try:
        session.execute(
            update(Experiment)
            .where(Experiment.id == uuid.UUID(experiment_id))
            .values(start_date=datetime.now(timezone.utc) + timedelta(days=1))
        )
        session.commit()
    finally:
        session.close()


# --- the texts ----------------------------------------------------------------------


def test_the_refusal_texts_are_these():
    """Each text the sign-in answers with, literally; the tests below compare
    answers with the module constants."""
    assert visual_editor.LINK_NOT_VALID == (
        "This editor link has expired or is not valid. Open the editor again "
        "from the experiment's page in Controlvar."
    )
    assert visual_editor.LINK_USED_TWICE == (
        "This editor link was already used, so the editor it opened has been "
        "closed. Open the editor again from Controlvar."
    )
    assert visual_editor.CLAIM_IN_USE == (
        "This value is already in use. Send a new random one."
    )
    assert editor_schemas.CLAIM_FORMAT_REFUSED == (
        "Send exactly 43 characters, each a letter, a digit, - or _."
    )
    assert visual_editor.NOT_VISUAL == (
        "This experiment has no visual settings, so it cannot be opened in the "
        "visual editor."
    )
    assert visual_editor.NOT_A_DRAFT == (
        "This experiment is no longer a draft, so the visual editor cannot open it."
    )
    assert visual_editor.DATED_DRAFT == (
        "A visual draft with a start date cannot be opened in the editor: it "
        "starts on its own with the design it has then. Remove the start date "
        "to edit it."
    )
    assert visual_editor.CANNOT_CHANGE == (
        "You can no longer change this experiment. Ask an administrator for "
        "permission to change experiments."
    )
    assert visual_origins.NOT_FROM_DASHBOARD == (
        "The visual editor accepts requests only from the Controlvar dashboard."
    )
    assert visual_origins.DASHBOARD_NOT_KNOWN.format(origin="https://x.test") == (
        "Your Controlvar server does not know this dashboard's address "
        "(https://x.test), so it will not open the editor. Ask whoever runs "
        "Controlvar to add it to DASHBOARD_ORIGINS."
    )
    assert visual_origins.SITE_NOT_LISTED == (
        "The visual editor opens only on the experiment's own sites. Open it "
        "again from the experiment's page in Controlvar."
    )
    forbidden = ("ticket", "claim", "session token", "etag")
    for name in (
        "LINK_NOT_VALID",
        "LINK_USED_TWICE",
        "NOT_VISUAL",
        "NOT_A_DRAFT",
        "DATED_DRAFT",
        "CANNOT_CHANGE",
    ):
        assert not any(
            word in getattr(visual_editor, name).lower() for word in forbidden
        )
    for name in ("NOT_FROM_DASHBOARD", "DASHBOARD_NOT_KNOWN", "SITE_NOT_LISTED"):
        assert not any(
            word in getattr(visual_origins, name).lower() for word in forbidden
        )


# --- the editor link ------------------------------------------------------------------


@pytest.mark.regression
def test_the_link_carries_a_43_character_ticket_in_its_fragment_for_120_seconds(
    client, db_session, developer, experiment, monkeypatch
):
    at = datetime(2031, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(visual_editor, "utcnow", lambda: at)

    response = _mint(client, developer, experiment)

    assert response.status_code == 201, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"open_url", "expires_at"}
    open_url = body["open_url"]
    editor_url = CONFIG["editor_url"]
    base, _, fragment = open_url.partition("#")
    ticket = fragment.partition("=")[2]
    assert ticket not in urlsplit(open_url).query, "open_url query holds the ticket"
    assert len(ticket) == 43, f"ticket length {len(ticket)} != 43"
    assert BASE64URL_43.fullmatch(ticket)
    assert open_url == f"{editor_url}#controlvar-editor={ticket}"
    assert base == editor_url
    assert datetime.fromisoformat(body["expires_at"]) == at + timedelta(seconds=120)

    (row,) = _rows(db_session, experiment)
    assert row.kind == EDITOR_TICKET
    assert row.token_hash == hash_api_key(ticket)
    assert row.user_id == developer.id
    assert row.expires_at == at + timedelta(seconds=120)
    assert row.parent_id is None and row.used_at is None and row.revoked_at is None
    stored = json.dumps(
        {c.name: str(getattr(row, c.name)) for c in VisualToken.__table__.columns}
    )
    assert ticket not in stored


# --- V115: the claim route's refusals, and nothing comes back -----------------------


BAD_CLAIMS = {
    "42 chars": "a" * 42,
    "44 chars": "a" * 44,
    "plus": "a" * 42 + "+",
    "slash": "a" * 42 + "/",
    "padding": "a" * 42 + "=",
}


@pytest.mark.regression
@pytest.mark.parametrize("case", [*BAD_CLAIMS, "missing"])
def test_a_claim_of_the_wrong_form_is_refused_and_the_ticket_stays_unused(
    client, db_session, developer, experiment, case
):
    ticket = _ticket(client, developer, experiment)
    body = {"ticket": ticket}
    if case != "missing":
        body["claim"] = BAD_CLAIMS[case]

    response = client.post(CLAIM, json=body, headers={"Origin": SHOP})

    assert response.status_code == 422, f"{case}: {response.status_code} != 422"
    if case != "missing":
        assert (
            response.json()["detail"][0]["msg"] == editor_schemas.CLAIM_FORMAT_REFUSED
        )
        assert BAD_CLAIMS[case] not in response.text
    assert _row(db_session, ticket).used_at is None
    assert _rows(db_session, experiment, EDITOR_CLAIM) == []
    # The ticket was not spent: a good claim still takes it.
    assert _claim(client, ticket, _value()).status_code == 201


@pytest.mark.regression
@pytest.mark.parametrize("stored", ["the ticket itself", "another ticket"])
def test_a_claim_already_stored_is_refused_and_the_ticket_stays_unused(
    client, db_session, developer, experiment, stored
):
    ticket = _ticket(client, developer, experiment)
    other = _ticket(client, developer, _draft(client, developer))
    duplicate = ticket if stored == "the ticket itself" else other

    response = _claim(client, ticket, duplicate)

    assert response.status_code == 422, f"duplicate: {response.status_code} != 422"
    assert response.json()["detail"] == visual_editor.CLAIM_IN_USE
    assert _row(db_session, ticket).used_at is None, "duplicate: the ticket was spent"
    assert _rows(db_session, experiment, EDITOR_CLAIM) == []
    assert _claim(client, ticket, _value()).status_code == 201


@pytest.mark.regression
def test_a_claim_is_stored_under_its_hash_and_nothing_comes_back(
    client, db_session, developer, experiment, monkeypatch
):
    at = datetime(2031, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(visual_editor, "utcnow", lambda: at)
    ticket = _ticket(client, developer, experiment)
    claim = _value()

    # The exchange takes a claim: the ticket's own value is not one.
    refused = _exchange(client, ticket)
    assert refused.status_code == 401, f"ticket as claim: {refused.status_code} != 401"
    assert refused.json()["detail"] == visual_editor.LINK_NOT_VALID

    response = _claim(client, ticket, claim)

    assert response.status_code == 201, response.text
    tokens = set(BASE64URL_43.findall(response.text))
    assert not tokens and ticket not in response.text and claim not in response.text, (
        "201 body holds a token"
    )
    assert response.headers["Cache-Control"] == "no-store"
    assert set(response.json()) == {"expires_at"}
    expires_at = datetime.fromisoformat(response.json()["expires_at"])
    assert expires_at == at + timedelta(seconds=120)
    ticket_row, claim_row = _row(db_session, ticket), _row(db_session, claim)
    assert ticket_row.used_at == at
    assert claim_row.kind == EDITOR_CLAIM
    assert claim_row.token_hash == hash_api_key(claim)
    assert claim_row.parent_id == ticket_row.id
    assert claim_row.user_id == developer.id
    assert claim_row.expires_at == at + timedelta(seconds=120)
    assert claim_row.used_at is None

    # The claim then opens the session.
    opened = _exchange(client, claim)
    assert opened.status_code == 201, opened.text


@pytest.mark.parametrize(
    "value", ["a preview link's token", "an unknown value"], ids=["preview", "unknown"]
)
def test_a_value_that_is_not_a_ticket_is_refused_at_the_claim_route(
    client, db_session, developer, experiment, value
):
    if value == "a preview link's token":
        variant = next(
            v["id"]
            for v in client.get(
                f"{EXPERIMENTS}/{experiment}", headers=auth(developer)
            ).json()["variants"]
            if v["name"] == "B"
        )
        made = client.post(
            f"{EXPERIMENTS}/{experiment}/visual/preview-links",
            json={"variant_id": variant},
            headers=auth(developer),
        )
        assert made.status_code == 201, made.text
        ticket = made.json()["url"].partition("#controlvar-preview=")[2]
    else:
        ticket = _value()

    response = _claim(client, ticket, _value())

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == visual_editor.LINK_NOT_VALID
    assert _rows(db_session, experiment, EDITOR_CLAIM) == []


# --- V79: a used ticket or claim ends everything descended from its ticket ------------


@pytest.mark.regression
def test_a_second_claim_of_a_ticket_ends_the_first_claim(
    client, db_session, developer, experiment
):
    ticket = _ticket(client, developer, experiment)
    first = _value()
    assert _claim(client, ticket, first).status_code == 201

    again = _claim(client, ticket, _value())

    assert again.status_code == 409, again.text
    assert again.json()["detail"] == visual_editor.LINK_USED_TWICE
    assert _ended_why(db_session, first) == TICKET_REUSED
    late = _exchange(client, first)
    assert late.status_code == 401, f"first claim's exchange: {late.status_code} != 401"
    assert late.json()["detail"] == visual_editor.LINK_NOT_VALID
    assert _rows(db_session, experiment, EDITOR_SESSION) == []


@pytest.mark.regression
@pytest.mark.parametrize(
    "later", [None, timedelta(minutes=3)], ids=["at once", "3 min"]
)
@pytest.mark.parametrize("via", ["claim route", "exchange"])
def test_a_used_link_presented_again_ends_the_session_it_opened(
    client, db_session, developer, experiment, monkeypatch, via, later
):
    """Presented again at once, or 3 minutes later, when the link and its
    claim have expired and the session they opened has not."""
    if later is not None:
        at = [datetime.now(timezone.utc)]
        monkeypatch.setattr(visual_editor, "utcnow", lambda: at[0])
    chain = _opened(client, developer, experiment)
    assert _session_says(client, chain["token"]) == (200, None)
    if later is not None:
        at[0] += later

    if via == "claim route":
        again = _claim(client, chain["ticket"], _value())
    else:
        again = _exchange(client, chain["claim"])

    assert again.status_code == 409, again.text
    assert again.json()["detail"] == visual_editor.LINK_USED_TWICE
    status, reason = _session_says(client, chain["token"])
    assert status == 401, f"session {status} != 401"
    assert reason == TICKET_REUSED


def _waiting_on(db_session, pid) -> bool:
    """Whether some backend waits for a lock that backend *pid* holds."""
    session = _session(db_session)
    try:
        return bool(
            session.execute(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE NOT granted AND :pid = ANY(pg_blocking_pids(pid))"
                ),
                {"pid": pid},
            ).scalar()
        )
    finally:
        session.close()


def _while_held(db_session, hold, send):
    """Run *hold* on a second connection and keep its transaction open; send
    the request; wait until Postgres shows the request waiting for the held
    row; commit; return the answer and whether the request was seen waiting."""
    other = _session(db_session)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        hold(other)
        other.flush()
        pid = other.execute(text("SELECT pg_backend_pid()")).scalar()
        answer = pool.submit(send)
        waited = False
        try:
            deadline = time.monotonic() + 30
            while not answer.done():
                if _waiting_on(db_session, pid):
                    waited = True
                    break
                assert time.monotonic() < deadline, (
                    "the request neither waited nor answered"
                )
                time.sleep(0.05)
        finally:
            other.commit()
        return answer.result(timeout=60), waited
    finally:
        pool.shutdown(wait=True)
        other.close()


@pytest.mark.regression
def test_two_claims_of_one_ticket_at_once_store_exactly_one(
    client, db_session, developer, experiment
):
    """The first claim holds its spend of the ticket uncommitted, on its own
    connection; the second, a real request, waits for that row, then finds
    the ticket used."""
    ticket = _ticket(client, developer, experiment)
    first = _value()

    def hold(other):
        now = visual_editor.utcnow()
        spent = visual_editor.spend(other, EDITOR_TICKET, hash_api_key(ticket), now)
        assert spent is not None
        other.add(
            VisualToken(
                kind=EDITOR_CLAIM,
                token_hash=hash_api_key(first),
                experiment_id=spent.experiment_id,
                user_id=spent.user_id,
                parent_id=spent.id,
                expires_at=now + visual_editor.CLAIM_LIFETIME,
            )
        )

    response, waited = _while_held(
        db_session, hold, lambda: _claim(client, ticket, _value())
    )

    claims = _rows(db_session, experiment, EDITOR_CLAIM)
    assert len(claims) == 1, f"claims {len(claims)} != 1"
    assert waited, "the second claim never waited for the first"
    assert response.status_code == 409, response.text


@pytest.mark.regression
def test_two_exchanges_of_one_claim_at_once_store_exactly_one_session(
    client, db_session, developer, experiment
):
    chain = _claimed(client, developer, experiment)

    def hold(other):
        now = visual_editor.utcnow()
        spent = visual_editor.spend(
            other, EDITOR_CLAIM, hash_api_key(chain["claim"]), now
        )
        assert spent is not None
        other.add(
            VisualToken(
                kind=EDITOR_SESSION,
                token_hash=hash_api_key(_value()),
                experiment_id=spent.experiment_id,
                user_id=spent.user_id,
                parent_id=spent.id,
                expires_at=now + visual_editor.SESSION_LIFETIME,
            )
        )

    response, waited = _while_held(
        db_session, hold, lambda: _exchange(client, chain["claim"])
    )

    sessions = _rows(db_session, experiment, EDITOR_SESSION)
    assert len(sessions) == 1, f"sessions {len(sessions)} != 1"
    assert waited, "the second exchange never waited for the first"
    assert response.status_code == 409, response.text


# --- one experiment's sign-in requests, one after another ----------------------------


class _Pause:
    """Stops the first request that calls ``owner.<name>`` just after that
    call returns, until :attr:`released` is set. When the call's first
    argument is the request's database session, :attr:`pid` is its backend,
    so a test can see another request wait for it."""

    def __init__(self, monkeypatch, owner, name: str):
        self.reached = threading.Event()
        self.released = threading.Event()
        self.pid: Optional[int] = None
        self._calls = 0
        self._count = threading.Lock()
        original = getattr(owner, name)

        def paused(*args, **kwargs):
            result = original(*args, **kwargs)
            with self._count:
                self._calls += 1
                first = self._calls == 1
            if first:
                if args and hasattr(args[0], "execute"):
                    self.pid = args[0].execute(text("SELECT pg_backend_pid()")).scalar()
                self.reached.set()
                assert self.released.wait(30), "the paused request was never let go"
            return result

        monkeypatch.setattr(owner, name, paused)


def _alongside(db_session, pause: _Pause, held, other):
    """Send *held* and wait until it reaches *pause*; send *other*; wait
    until Postgres shows *other* waiting for the held request, or *other* has
    answered; then let the held request go. Returns both answers and whether
    *other* was seen waiting."""
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(held)
        assert pause.reached.wait(30), "the held request never reached its pause"
        second = pool.submit(other)
        waited = False
        deadline = time.monotonic() + 30
        while not second.done():
            if pause.pid is not None and _waiting_on(db_session, pause.pid):
                waited = True
                break
            assert time.monotonic() < deadline, (
                "the second request neither waited nor answered"
            )
            time.sleep(0.05)
        pause.released.set()
        return first.result(timeout=60), second.result(timeout=60), waited
    finally:
        pause.released.set()
        pool.shutdown(wait=True)


def _live(db_session, experiment_id: str, kind: str) -> List[VisualToken]:
    return [
        row for row in _rows(db_session, experiment_id, kind) if row.revoked_at is None
    ]


@pytest.mark.regression
def test_a_link_opened_twice_at_once_leaves_one_editor(
    client, db_session, developer, experiment, monkeypatch
):
    """The first open is held after it ended the earlier credentials and
    before it writes its link; the second open waits for it, then ends the
    first open's link."""
    pause = _Pause(monkeypatch, visual_editor, "reopen")

    first, second, waited = _alongside(
        db_session,
        pause,
        lambda: _mint(client, developer, experiment),
        lambda: _mint(client, developer, experiment),
    )

    live = _live(db_session, experiment, EDITOR_TICKET)
    assert len(live) == 1, f"live tickets {len(live)} != 1"
    assert waited, "the second open never waited for the first"
    assert _claim(client, _ticket_of(first), _value()).status_code == 401
    newer = _ticket_of(second)
    claim = _value()
    assert _claim(client, newer, claim).status_code == 201
    assert _exchange(client, claim).status_code == 201
    assert len(_live(db_session, experiment, EDITOR_SESSION)) == 1


@pytest.mark.regression
@pytest.mark.parametrize("held_at", ["before its write", "at its write"])
def test_a_link_used_again_while_it_is_claimed_leaves_nothing_live(
    client, db_session, developer, experiment, monkeypatch, held_at
):
    """The first claim is held after it spent the link: before it writes the
    claim, or with the claim about to be written. The same link is presented
    again meanwhile (409); nothing that link led to stays live."""
    ticket = _ticket(client, developer, experiment)
    first = _value()
    if held_at == "before its write":
        pause = _Pause(monkeypatch, editor_endpoints, "site_origins")
    else:
        pause = _Pause(monkeypatch, visual_editor, "parent_refusal")

    held, again, waited = _alongside(
        db_session,
        pause,
        lambda: _claim(client, ticket, first),
        lambda: _claim(client, ticket, _value()),
    )

    assert again.status_code == 409, again.text
    live = _live(db_session, experiment, EDITOR_CLAIM)
    assert len(live) == 0, f"live claims {len(live)} != 0"
    assert _ended_why(db_session, ticket) == TICKET_REUSED
    if held_at == "before its write":
        assert held.status_code == 409, f"held claim {held.status_code} != 409"
        assert held.json()["detail"] == visual_editor.LINK_USED_TWICE
        assert _rows(db_session, experiment, EDITOR_CLAIM) == []
    else:
        assert held.status_code == 201, held.text
        assert waited, "the second claim never waited for the first"
        assert _ended_why(db_session, first) == TICKET_REUSED
    late = _exchange(client, first)
    assert late.status_code == 401, f"held claim's exchange {late.status_code} != 401"
    assert _rows(db_session, experiment, EDITOR_SESSION) == []


@pytest.mark.regression
@pytest.mark.parametrize("held_at", ["before its write", "at its write"])
def test_opening_the_editor_again_during_an_exchange_leaves_one_editor(
    client, db_session, developer, experiment, monkeypatch, held_at
):
    """The exchange is held after it spent the claim: before it writes the
    session, or with the session about to be written. The editor is opened
    again meanwhile; no session of the earlier link stays live, and the new
    link opens the one editor."""
    chain = _claimed(client, developer, experiment)
    if held_at == "before its write":
        pause = _Pause(monkeypatch, visual_editor, "refuse_unless_open_to_edit")
    else:
        pause = _Pause(monkeypatch, visual_editor, "parent_refusal")

    held, reopened, waited = _alongside(
        db_session,
        pause,
        lambda: _exchange(client, chain["claim"]),
        lambda: _mint(client, developer, experiment),
    )

    assert reopened.status_code == 201, reopened.text
    live = _live(db_session, experiment, EDITOR_SESSION)
    assert len(live) == 0, f"live sessions {len(live)} != 0"
    if held_at == "before its write":
        assert held.status_code == 401, f"held exchange {held.status_code} != 401"
        assert held.json()["detail"] == visual_editor.LINK_NOT_VALID
        assert _rows(db_session, experiment, EDITOR_SESSION) == []
    else:
        assert held.status_code == 201, held.text
        assert waited, "the re-open never waited for the exchange"
        says = _session_says(client, held.json()["token"])
        assert says == (401, REOPENED), (
            f"held exchange's session {says} != (401, 'reopened')"
        )
    claim = _value()
    assert _claim(client, _ticket_of(reopened), claim).status_code == 201
    assert _exchange(client, claim).status_code == 201
    assert len(_live(db_session, experiment, EDITOR_SESSION)) == 1


@pytest.mark.regression
@pytest.mark.parametrize("via", ["claim route", "exchange"])
def test_nothing_is_written_for_a_link_that_expired_after_it_was_spent(
    client, db_session, developer, experiment, monkeypatch, via
):
    """The request is held after it spent the ticket (or the claim), the
    clock moves to that credential's expiry, and the request goes on: it
    writes nothing and answers the one 401."""
    base = datetime(2031, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    at = [base]
    monkeypatch.setattr(visual_editor, "utcnow", lambda: at[0])
    if via == "claim route":
        ticket = _ticket(client, developer, experiment)
        pause = _Pause(monkeypatch, editor_endpoints, "site_origins")
        send, kind = (lambda: _claim(client, ticket, _value())), EDITOR_CLAIM
    else:
        chain = _claimed(client, developer, experiment)
        pause = _Pause(monkeypatch, visual_editor, "refuse_unless_open_to_edit")
        send, kind = (lambda: _exchange(client, chain["claim"])), EDITOR_SESSION
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        answer = pool.submit(send)
        assert pause.reached.wait(30), "the request never reached its pause"
        at[0] = base + timedelta(seconds=120)
        pause.released.set()
        response = answer.result(timeout=60)
    finally:
        pause.released.set()
        pool.shutdown(wait=True)

    assert response.status_code == 401, f"{via}: {response.status_code} != 401"
    assert response.json()["detail"] == visual_editor.LINK_NOT_VALID
    assert _rows(db_session, experiment, kind) == []


# --- the exchange's checks on the page, and that the value stays spent ---------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "page",
    [
        "https://evil.test",
        DASHBOARD,
        "http://shop.example.com",
        "null",
        "not an origin",
    ],
)
def test_an_exchange_naming_a_page_outside_the_experiments_sites_is_refused_and_spends_the_claim(
    client, db_session, developer, experiment, page
):
    chain = _claimed(client, developer, experiment)

    refused = _exchange(client, chain["claim"], page_origin=page)

    assert refused.status_code == 403, f"exchange {refused.status_code} != 403"
    assert refused.json()["detail"] == visual_origins.SITE_NOT_LISTED
    assert _rows(db_session, experiment, EDITOR_SESSION) == []
    again = _exchange(client, chain["claim"], page_origin=SHOP)
    assert again.status_code == 409, f"claim reusable: {again.status_code} != 409"
    # The pair: a claim exchanged from each of the experiment's sites opens.
    for site in (SHOP, SHOP2):
        fresh = _claimed(client, developer, experiment)
        assert _exchange(client, fresh["claim"], page_origin=site).status_code == 201


def test_the_session_answer_holds_the_draft_and_its_sites(
    client, db_session, developer, experiment, monkeypatch
):
    at = datetime(2031, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(visual_editor, "utcnow", lambda: at)
    chain = _claimed(client, developer, experiment)

    response = _exchange(client, chain["claim"], page_origin=SHOP2 + "/")

    assert response.status_code == 201, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"token", "expires_at", "site_origins", "experiment"}
    assert datetime.fromisoformat(body["expires_at"]) == at + timedelta(hours=4)
    token = body["token"]
    assert token.startswith("cves_") and BASE64URL_43.fullmatch(token[5:])
    assert body["site_origins"] == [SHOP, SHOP2]
    detail = client.get(f"{EXPERIMENTS}/{experiment}", headers=auth(developer)).json()
    shown = body["experiment"]
    assert shown["id"] == experiment
    assert shown["key"] == detail["key"]
    assert shown["name"] == detail["name"]
    assert shown["editor_url"] == CONFIG["editor_url"]
    assert shown["pages"] == CONFIG["pages"]
    assert shown["design_etag"] == detail["design_etag"]
    assert sorted(shown["variants"], key=lambda v: v["name"]) == [
        {
            "id": next(v["id"] for v in detail["variants"] if v["name"] == name),
            "name": name,
            "is_control": name == "Control",
            "visual_changes": None if name == "Control" else CHANGES,
        }
        for name in ("B", "Control")
    ]
    row = _row(db_session, token)
    assert row.kind == EDITOR_SESSION
    assert row.parent_id == _row(db_session, chain["claim"]).id
    assert row.user_id == developer.id
    assert row.expires_at == at + timedelta(hours=4)
    assert _row(db_session, chain["claim"]).used_at == at


@pytest.mark.parametrize("change", ["started", "dated", "deactivated", "demoted"])
def test_an_exchange_for_a_draft_that_can_no_longer_be_edited_is_refused(
    client, db_session, developer, experiment, change
):
    chain = _claimed(client, developer, experiment)
    session = _session(db_session)
    try:
        if change == "started":
            session.execute(
                text("UPDATE experiments SET status = 'ACTIVE' WHERE id = :id"),
                {"id": uuid.UUID(experiment)},
            )
        elif change == "dated":
            session.execute(
                text(
                    "UPDATE experiments SET start_date = now() + interval '1 day' WHERE id = :id"
                ),
                {"id": uuid.UUID(experiment)},
            )
        elif change == "deactivated":
            session.execute(
                text("UPDATE users SET is_active = false WHERE id = :id"),
                {"id": developer.id},
            )
        else:
            session.execute(
                text("UPDATE users SET role = 'VIEWER' WHERE id = :id"),
                {"id": developer.id},
            )
        session.commit()
    finally:
        session.close()

    response = _exchange(client, chain["claim"])

    expected = {
        "started": (400, visual_editor.NOT_A_DRAFT),
        "dated": (400, visual_editor.DATED_DRAFT),
        "deactivated": (403, visual_editor.CANNOT_CHANGE),
        "demoted": (403, visual_editor.CANNOT_CHANGE),
    }[change]
    assert (response.status_code, response.json()["detail"]) == expected
    assert _rows(db_session, experiment, EDITOR_SESSION) == []
    assert _row(db_session, chain["claim"]).used_at is not None


# --- the claim route's origin, and that the ticket stays spent ------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://evil.test",
        DASHBOARD,
        "http://shop.example.com",
        f"{SHOP}:8443",
    ],
    ids=["none", "null", "evil", "dashboard", "http", "port"],
)
def test_a_claim_from_a_page_outside_the_experiments_sites_is_refused_and_spends_the_ticket(
    client, db_session, developer, experiment, origin
):
    ticket = _ticket(client, developer, experiment)

    refused = _claim(client, ticket, _value(), origin=origin)

    assert refused.status_code == 403, f"claim {refused.status_code} != 403"
    assert refused.json()["detail"] == visual_origins.SITE_NOT_LISTED
    assert _rows(db_session, experiment, EDITOR_CLAIM) == []
    assert _claim(client, ticket, _value()).status_code == 409
    # The pair: each of the experiment's sites claims a fresh ticket.
    for site in (SHOP, SHOP2):
        fresh = _ticket(client, developer, experiment)
        response = _claim(client, fresh, _value(), origin=site)
        assert response.status_code == 201, (
            f"site origin: {response.status_code} != 201"
        )


# --- V80: opening the editor again ----------------------------------------------------


@pytest.mark.regression
def test_opening_the_editor_again_ends_every_earlier_credential_of_that_experiment(
    client, db_session, developer, experiment
):
    colleague = _user(db_session, UserRole.DEVELOPER)
    elsewhere = _draft(client, developer)
    s_other = _opened(client, developer, elsewhere)
    s_colleague = _opened(client, colleague, experiment)
    s1 = _opened(client, developer, experiment)
    status, reason = _session_says(client, s_colleague["token"])
    assert status == 401, f"user B {status} != 401"
    assert reason == REOPENED
    assert _session_says(client, s1["token"]) == (200, None)
    pending = _claimed(client, developer, experiment)  # ends s1
    unclaimed = _ticket(client, developer, experiment)  # ends the pending claim
    s2 = _opened(client, developer, experiment)  # ends the unclaimed ticket

    assert _session_says(client, s1["token"]) == (401, REOPENED)
    assert _ended_why(db_session, pending["claim"]) == REOPENED
    assert _ended_why(db_session, pending["ticket"]) == REOPENED
    assert _ended_why(db_session, unclaimed) == REOPENED
    assert _claim(client, unclaimed, _value()).status_code == 401
    assert _exchange(client, pending["claim"]).status_code == 401
    assert _session_says(client, s2["token"]) == (200, None)
    status, _ = _session_says(client, s_other["token"])
    assert status == 200, f"other experiment {status} != 200"


@pytest.mark.regression
def test_a_reopen_closing_entry_names_who_opened_again_and_whose_session_closed(
    client, db_session, developer, experiment
):
    colleague = _user(db_session, UserRole.DEVELOPER)
    theirs = _opened(client, colleague, experiment)
    session_id = str(_row(db_session, theirs["token"]).id)

    _ticket(client, developer, experiment)

    closing = [
        row
        for row in _audit_rows(db_session, experiment)
        if row.action_type == "visual_editor_session_closed"
    ]
    assert len(closing) == 1
    (entry,) = closing
    actor = "B" if entry.user_id == colleague.id else entry.user_id
    assert entry.user_id == developer.id, f"actor {actor} != A"
    assert entry.user_email == developer.email
    assert json.loads(entry.new_value) == {
        "reason": REOPENED,
        "session_id": session_id,
        "session_user_id": str(colleague.id),
    }


# --- V81: the audit entries, exactly ---------------------------------------------------


@pytest.mark.regression
def test_each_case_writes_exactly_its_audit_entries(client, db_session, developer):
    opened = _draft(client, developer)
    chain = _opened(client, developer, opened)
    assert _entries(db_session, opened) == ["opened"]
    session_row = _row(db_session, chain["token"])
    (entry,) = _audit_rows(db_session, opened)
    assert entry.user_id == developer.id
    assert json.loads(entry.new_value) == {"session_id": str(session_row.id)}

    for via in ("claim route", "exchange"):
        experiment = _draft(client, developer)
        chain = _opened(client, developer, experiment)
        before = len(_entries(db_session, experiment))
        if via == "claim route":
            assert _claim(client, chain["ticket"], _value()).status_code == 409
        else:
            assert _exchange(client, chain["claim"]).status_code == 409
        found = _entries(db_session, experiment)[before:]
        assert found == ["ticket_reused", "closed(ticket_reused)"], f"{via}: {found}"
        reused, closed = _audit_rows(db_session, experiment)[before:]
        for row in (reused, closed):
            assert row.user_id is None and row.user_email == "system:visual-editor"
        assert json.loads(reused.new_value) == {"link_user_id": str(developer.id)}
        assert json.loads(closed.new_value)["session_id"] == str(
            _row(db_session, chain["token"]).id
        )

    # Opening the editor again closes the live session (a re-open leaves at
    # most one), and a re-open over a ticket alone closes none.
    reopened = _draft(client, developer)
    colleague = _user(db_session, UserRole.DEVELOPER)
    _opened(client, colleague, reopened)
    before = len(_entries(db_session, reopened))
    _ticket(client, developer, reopened)
    assert _entries(db_session, reopened)[before:] == ["closed(reopened)"]
    _ticket(client, developer, reopened)
    assert _entries(db_session, reopened)[before:] == ["closed(reopened)"]


# --- V82: who may call each route, by Origin --------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("wrong", [*WRONG_ORIGINS, "none"])
def test_the_editor_is_opened_only_from_a_dashboard_origin(
    client, db_session, developer, experiment, wrong
):
    origin = WRONG_ORIGINS.get(wrong)

    refused = _mint(client, developer, experiment, origin=origin)

    if wrong in ("null", "none"):
        expected = visual_origins.NOT_FROM_DASHBOARD
    else:
        expected = visual_origins.DASHBOARD_NOT_KNOWN.format(origin=origin)
    label = "POST no Origin" if wrong == "none" else origin
    assert refused.status_code == 403, f"{label}: {refused.status_code} != 403"
    assert refused.json()["detail"] == expected
    assert _rows(db_session, experiment, EDITOR_TICKET) == []
    for dashboard in (DASHBOARD, DASHBOARD2):
        assert _mint(client, developer, experiment, origin=dashboard).status_code == 201


@pytest.mark.regression
@pytest.mark.parametrize("wrong", [*WRONG_ORIGINS, "none"])
def test_a_session_is_started_only_from_a_dashboard_origin(
    client, db_session, developer, experiment, wrong
):
    origin = WRONG_ORIGINS.get(wrong)
    chain = _claimed(client, developer, experiment)

    refused = _exchange(client, chain["claim"], origin=origin)

    label = "POST no Origin" if wrong == "none" else origin
    assert refused.status_code == 403, f"{label}: {refused.status_code} != 403"
    assert refused.json()["detail"] == visual_origins.NOT_FROM_DASHBOARD
    # Refused before the claim is looked at: it is still unspent.
    assert _row(db_session, chain["claim"]).used_at is None
    assert _exchange(client, chain["claim"], origin=DASHBOARD).status_code == 201
    fresh = _claimed(client, developer, experiment)
    assert _exchange(client, fresh["claim"], origin=DASHBOARD2).status_code == 201


# --- V84: the dashboard origins the mint sees ---------------------------------------------


@pytest.fixture
def no_origin_env(monkeypatch):
    for name in (
        "BACKEND_CORS_ORIGINS",
        "CORS_ORIGINS",
        "DASHBOARD_ORIGINS",
        "PUBLIC_BASE_URL",
        "ALLOWED_HOSTS",
        "TESTING",
    ):
        monkeypatch.delenv(name, raising=False)


def _local_config(port: int) -> Dict[str, Any]:
    page = {"match": "prefix", "url": f"http://localhost:{port}/"}
    return {
        "version": 1,
        "editor_url": f"http://localhost:{port}/",
        "pages": [page],
        "goals": [],
    }


@pytest.mark.regression
@pytest.mark.parametrize("origin", [None, DASHBOARD2], ids=["none", "3100"])
def test_a_test_server_with_no_dashboard_origin_refuses_before_the_origin_rule(
    client, developer, experiment, monkeypatch, no_origin_env, origin
):
    monkeypatch.setattr(visual_origins, "app_settings", TestSettings(_env_file=None))
    assert visual_origins.app_settings.dashboard_origins == []

    response = _mint(client, developer, experiment, origin=origin)

    assert response.status_code == 422, f"test env: mint {response.status_code} != 422"
    assert response.json()["detail"] == visual_origins.NO_DASHBOARD_ORIGIN


def test_a_test_server_with_a_dashboard_origin_refuses_a_page_on_it(
    client, developer, monkeypatch, no_origin_env
):
    monkeypatch.setattr(
        visual_origins,
        "app_settings",
        TestSettings(DASHBOARD_ORIGINS=DASHBOARD2, _env_file=None),
    )
    on_dashboard = _draft(client, developer, _local_config(3100))
    elsewhere = _draft(client, developer, _local_config(3200))

    refused = _mint(client, developer, on_dashboard, origin=DASHBOARD2)

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == visual_origins.PAGE_ON_DASHBOARD
    assert _mint(client, developer, elsewhere, origin=DASHBOARD2).status_code == 201


def test_a_development_server_takes_its_own_dashboards(
    client, developer, monkeypatch, no_origin_env
):
    monkeypatch.setattr(visual_origins, "app_settings", DevSettings(_env_file=None))
    page = _draft(client, developer, _local_config(3200))
    on_dashboard = _draft(client, developer, _local_config(3100))

    for dashboard in ("http://localhost:3100", "http://localhost:3000"):
        assert _mint(client, developer, page, origin=dashboard).status_code == 201
    refused = _mint(client, developer, on_dashboard, origin="http://localhost:3000")
    assert refused.json()["detail"] == visual_origins.PAGE_ON_DASHBOARD
    stranger = _mint(client, developer, page, origin="http://localhost:3200")
    assert stranger.status_code == 403
    assert stranger.json()["detail"] == visual_origins.DASHBOARD_NOT_KNOWN.format(
        origin="http://localhost:3200"
    )


def test_a_production_server_takes_its_public_address(
    client, developer, experiment, monkeypatch, no_origin_env
):
    monkeypatch.setattr(
        visual_origins,
        "app_settings",
        ProdSettings(
            ENVIRONMENT="production",
            SECRET_KEY="p" * 64,
            FIRST_SUPERUSER_PASSWORD="Str0ng-first-admin",
            PUBLIC_BASE_URL=DASHBOARD,
            CORS_ORIGINS=f"{SHOP},{SHOP2}",
            _env_file=None,
        ),
    )

    assert _mint(client, developer, experiment, origin=DASHBOARD).status_code == 201
    refused = _mint(client, developer, experiment, origin=DASHBOARD2)
    assert refused.status_code == 403
    assert refused.json()["detail"] == visual_origins.DASHBOARD_NOT_KNOWN.format(
        origin=DASHBOARD2
    )


# --- V26 and the mint's other refusals ----------------------------------------------------


ROLES = {
    "admin": (UserRole.ADMIN, False, True),
    "developer": (UserRole.DEVELOPER, False, True),
    "superuser": (UserRole.ADMIN, True, True),
    "analyst": (UserRole.ANALYST, False, False),
    "viewer": (UserRole.VIEWER, False, False),
}


@pytest.mark.regression
@pytest.mark.parametrize("role_name", sorted(ROLES))
def test_opening_the_editor_needs_experiment_update(
    client, db_session, developer, experiment, role_name
):
    role, superuser, allowed = ROLES[role_name]
    user = _user(db_session, role, superuser)

    response = _mint(client, user, experiment)

    if allowed:
        assert response.status_code == 201, response.text
    else:
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == UPDATE_REFUSED
        assert _rows(db_session, experiment) == []


def _custom_role(db_session, user, actions: List[str]) -> None:
    session = _session(db_session)
    try:
        role_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO custom_roles (id, name, description, is_system_role, "
                "permissions, created_at, updated_at) VALUES (:id, :name, NULL, "
                "false, CAST(:perms AS jsonb), now(), now())"
            ),
            {
                "id": role_id,
                "name": f"{TAG}-{uuid.uuid4().hex[:10]}",
                "perms": json.dumps([{"resource": "experiment", "actions": actions}]),
            },
        )
        session.execute(
            text(
                "INSERT INTO user_custom_roles (id, user_id, role_id, assigned_at, "
                "activated_at, created_at, updated_at) VALUES (:id, :user, :role, "
                "now(), now(), now(), now())"
            ),
            {"id": uuid.uuid4(), "user": user.id, "role": role_id},
        )
        session.commit()
    finally:
        session.close()


@pytest.mark.modules
@pytest.mark.regression
@pytest.mark.parametrize(
    "actions, allowed",
    [(["read"], False), (["read", "update"], True)],
    ids=["read only", "grant"],
)
def test_a_custom_role_decides_by_experiment_update(
    client, db_session, developer, experiment, actions, allowed
):
    viewer = _user(db_session, UserRole.VIEWER)
    _custom_role(db_session, viewer, actions)

    response = _mint(client, viewer, experiment)

    expected = 201 if allowed else 403
    label = "grant row" if allowed else "read-only row"
    assert response.status_code == expected, (
        f"{label}: {response.status_code} != {expected}"
    )


@pytest.mark.regression
def test_a_draft_with_a_start_date_is_not_opened(
    client, db_session, developer, experiment
):
    _set_start_date(db_session, experiment)

    response = _mint(client, developer, experiment)

    assert response.status_code == 400, f"dated draft: {response.status_code} != 400"
    assert response.json()["detail"] == visual_editor.DATED_DRAFT
    assert _rows(db_session, experiment) == []


def test_what_else_is_refused_before_a_link_is_made(client, db_session, developer):
    plain = client.post(
        f"{EXPERIMENTS}/",
        json={
            "name": f"{PREFIX}{uuid.uuid4().hex[:8]}",
            "experiment_type": "a_b",
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {"name": "B", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {"name": "Buy", "event_name": "buy", "metric_type": "conversion"}
            ],
        },
        headers=auth(developer),
    )
    assert plain.status_code == 201, plain.text
    unknown = _mint(client, developer, str(uuid.uuid4()))
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == visual_editor.EXPERIMENT_NOT_FOUND
    not_visual = _mint(client, developer, plain.json()["id"])
    assert (not_visual.status_code, not_visual.json()["detail"]) == (
        400,
        visual_editor.NOT_VISUAL,
    )
    started = _draft(client, developer)
    session = _session(db_session)
    try:
        session.execute(
            text("UPDATE experiments SET status = 'ACTIVE' WHERE id = :id"),
            {"id": uuid.UUID(started)},
        )
        session.commit()
    finally:
        session.close()
    not_draft = _mint(client, developer, started)
    assert (not_draft.status_code, not_draft.json()["detail"]) == (
        400,
        visual_editor.NOT_A_DRAFT,
    )
    stranger = _draft(
        client,
        developer,
        {
            **CONFIG,
            "editor_url": "https://other.test/",
            "pages": [{"match": "prefix", "url": "https://other.test/"}],
            "goals": [],
        },
    )
    refused = _mint(client, developer, stranger)
    assert refused.status_code == 422
    assert refused.json()["detail"] == visual_origins.ORIGIN_NOT_ALLOWED.format(
        origin="https://other.test"
    )
    # Without a token: 401 before anything else.
    assert (
        client.post(
            f"{EXPERIMENTS}/{started}/visual/editor-tickets",
            headers={"Origin": DASHBOARD},
        ).status_code
        == 401
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "listed", [["*"], ["*", SHOP, SHOP2]], ids=["only *", "* and the sites"]
)
def test_no_draft_is_opened_while_cors_origins_holds_every_site(
    client, db_session, developer, experiment, monkeypatch, listed
):
    monkeypatch.setattr(settings, "CORS_ORIGINS", listed)

    refused = _mint(client, developer, experiment)

    assert refused.status_code == 422, f"mint with *: {refused.status_code} != 422"
    assert refused.json()["detail"] == visual_origins.EVERY_SITE_ALLOWED
    assert _rows(db_session, experiment) == []
    # The pair: the sites alone, and the same draft opens.
    monkeypatch.setattr(settings, "CORS_ORIGINS", [SHOP, SHOP2])
    assert _mint(client, developer, experiment).status_code == 201


@pytest.mark.regression
@pytest.mark.parametrize(
    "listed", [["*"], ["*", SHOP, SHOP2]], ids=["only *", "* and the sites"]
)
def test_a_link_made_before_cors_origins_held_every_site_opens_no_editor_after(
    client, db_session, developer, experiment, monkeypatch, listed
):
    chain = _claimed(client, developer, experiment)
    monkeypatch.setattr(settings, "CORS_ORIGINS", listed)

    refused = _exchange(client, chain["claim"])

    if listed == ["*"]:
        # No site is left, so the page is not one of them.
        want = (403, visual_origins.SITE_NOT_LISTED)
    else:
        want = (422, visual_origins.EVERY_SITE_ALLOWED)
    assert refused.status_code == want[0], (
        f"exchange with *: {refused.status_code} != {want[0]}"
    )
    assert refused.json()["detail"] == want[1]
    assert _rows(db_session, experiment, EDITOR_SESSION) == []
    # The pair: the sites alone, and a link exchanges.
    monkeypatch.setattr(settings, "CORS_ORIGINS", [SHOP, SHOP2])
    assert _exchange(client, chain["claim"]).status_code == 409
    fresh = _claimed(client, developer, experiment)
    assert _exchange(client, fresh["claim"]).status_code == 201


# --- a browser's request reaches the claim route ------------------------------------------


def _cors_origins() -> List[str]:
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return list(middleware.kwargs["allow_origins"])
    raise AssertionError("no CORS middleware")


@pytest.mark.regression
def test_a_browser_reaches_the_claim_route_from_a_site(client, developer, monkeypatch):
    """The page's script sends a JSON body with no API key and no cookie, so
    the browser asks first (a preflight), then sends it; both answers must
    let the page's origin through."""
    dashboards = set(settings.dashboard_origins)
    site = next(o for o in _cors_origins() if o not in dashboards and o != "*")
    monkeypatch.setattr(settings, "CORS_ORIGINS", [site])
    page = {"match": "prefix", "url": f"{site}/"}
    experiment = _draft(
        client,
        developer,
        {"version": 1, "editor_url": f"{site}/", "pages": [page], "goals": []},
    )
    ticket = _ticket(client, developer, experiment)

    preflight = client.options(
        CLAIM,
        headers={
            "Origin": site,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.headers["access-control-allow-origin"] == site

    response = client.post(
        CLAIM,
        content=json.dumps({"ticket": ticket, "claim": _value()}),
        headers={"Origin": site, "Content-Type": "application/json"},
    )
    assert response.status_code == 201, (
        f"claim without X-API-Key: {response.status_code} != 201"
    )
    assert response.headers["access-control-allow-origin"] == site


# --- nothing kept holds a credential --------------------------------------------------------


@pytest.mark.regression
def test_no_log_line_or_stored_entry_holds_a_credential(
    client, db_session, developer, experiment, caplog, capfd
):
    started = datetime.now(timezone.utc)
    caplog.set_level(logging.DEBUG)
    capfd.readouterr()
    colleague = _user(db_session, UserRole.DEVELOPER)

    first = _opened(client, developer, experiment)
    _claim(client, first["ticket"], _value())  # used again: ended
    second = _opened(client, colleague, experiment)
    _exchange(client, second["claim"])  # used again: ended
    third = _opened(client, developer, experiment)
    _ticket(client, developer, experiment)  # opened again: ended

    secrets_seen = {}
    for name, chain in (("first", first), ("second", second), ("third", third)):
        for part, value in chain.items():
            secrets_seen[f"{name} {part}"] = value
            secrets_seen[f"{name} {part} hash"] = hash_api_key(value)
    session = _session(db_session)
    try:
        audit = (
            session.execute(
                text("SELECT a::text FROM audit_logs a WHERE a.timestamp >= :t"),
                {"t": started},
            )
            .scalars()
            .all()
        )
        compliance = (
            session.execute(
                text(f"SELECT c::text FROM {ComplianceAuditEvent.__tablename__} c")
            )
            .scalars()
            .all()
        )
    finally:
        session.close()
    printed = capfd.readouterr()
    # The capture sees the application's own lines: each audit entry logs one.
    assert "visual_editor_session_opened" in caplog.text, "the logs were not seen"
    assert audit, "no audit entries were read"
    places = {
        "logs": caplog.text + printed.out + printed.err,
        "audit_logs": "\n".join(audit),
        "compliance events": "\n".join(compliance),
    }
    found = sorted(
        f"{label} in {place}"
        for place, haystack in places.items()
        for label, value in secrets_seen.items()
        if value in haystack
    )
    assert found == []


# --- V93: no IP address is stored -----------------------------------------------------------


@pytest.mark.regression
def test_no_ip_address_is_stored_by_the_editors_routes(client, db_session, developer):
    """TestClient's client address is ``testclient``. Every route of the
    sign-in and the session runs once: opening the editor, the page's claim,
    the exchange, a save, signing out, opening it again over a live session
    and a used link presented again. No text or JSON column gains the
    address (the scan of test_visual_tracking_api.py), and no compliance
    event made since has an actor address."""
    address = "testclient"
    experiment = _draft(client, developer)
    before = client_address_count(db_session, address)
    started = datetime.now(timezone.utc)

    first = _opened(client, developer, experiment)
    variant = _variant(client, developer, experiment, "B")
    assert _save(client, first["token"], variant, NEW_CHANGES).status_code == 200
    assert _sign_out(client, first["token"]).status_code == 204
    second = _opened(client, developer, experiment)
    _ticket(client, developer, experiment)
    assert _session_says(client, second["token"]) == (401, "reopened")
    assert _claim(client, second["ticket"], _value()).status_code == 409

    assert _entries(db_session, experiment) == [
        "opened",
        "closed(signed_out)",
        "opened",
        "closed(reopened)",
        "ticket_reused",
    ]
    session = _session(db_session)
    try:
        events = (
            session.query(ComplianceAuditEvent)
            .filter(
                ComplianceAuditEvent.timestamp >= started,
                ComplianceAuditEvent.resource_id == experiment,
            )
            .all()
        )
    finally:
        session.close()
    assert events, "the save made no compliance event: the scan sees nothing"
    addresses = [event.actor_ip for event in events if event.actor_ip is not None]
    assert addresses == [], f"{addresses[0]!r} in compliance_audit_events.actor_ip"
    assert client_address_count(db_session, address) == before


# --- the session's routes --------------------------------------------------------

NEW_CHANGES = {
    "version": 1,
    "changes": [{"op": "set_text", "selector": "#headline", "text": "Welcome"}],
}


def _stored_changes(db_session, experiment_id: str) -> Dict[str, Any]:
    session = _session(db_session)
    try:
        rows = session.execute(
            text("SELECT name, visual_changes FROM variants WHERE experiment_id = :id"),
            {"id": uuid.UUID(experiment_id)},
        ).all()
    finally:
        session.close()
    return dict(rows)


def _start(client, user, experiment_id: str):
    etag = client.get(f"{EXPERIMENTS}/{experiment_id}", headers=auth(user)).json()[
        "design_etag"
    ]
    return client.post(
        f"{EXPERIMENTS}/{experiment_id}/start",
        json={"design_etag": etag},
        headers=auth(user),
    )


def _tick(db_session) -> None:
    """One experiment scheduler tick against the test database, notifications
    stubbed."""
    from backend.app.core.scheduler import ExperimentScheduler
    from backend.app.services.notification_service import NotificationService

    notifications = NotificationService()
    notifications._slack = MagicMock()
    notifications._email = MagicMock()
    notifications._send = lambda event: True
    scheduler = ExperimentScheduler()
    scheduler._notification_service = notifications
    factory = sessionmaker(bind=db_session.get_bind())
    with patch("backend.app.core.scheduler.SessionLocal", factory):
        asyncio.run(scheduler.process_scheduled_experiments())


def _date_in_the_past(db_session, experiment_id: str) -> None:
    session = _session(db_session)
    try:
        session.execute(
            update(Experiment)
            .where(Experiment.id == uuid.UUID(experiment_id))
            .values(start_date=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        session.commit()
    finally:
        session.close()


def test_the_closing_texts_are_these():
    """Each reason's text, literally; the tests compare answers with
    ``CLOSED_TEXTS``."""
    assert visual_editor.CLOSED_TEXTS == {
        "reopened": (
            "The editor was opened again for this experiment, so this one has "
            "closed. Your saved changes are kept."
        ),
        "ticket_reused": (
            "This editor link was used twice, so Controlvar closed the editor it "
            "opened. Your saved changes are kept. Open the editor again from the "
            "experiment's page."
        ),
        "started": (
            "This experiment was started in Controlvar, so the editor has closed."
        ),
        "signed_out": (
            "You signed out of the visual editor. Open the editor again from the "
            "experiment's page in Controlvar."
        ),
        "scheduled": (
            "This draft now has a start date, so the editor has closed. Your "
            "saved changes are kept. Remove the start date on the experiment's "
            "page to edit again."
        ),
        "expired": (
            "Your editing session has ended. Saved changes are kept. Open the "
            "editor again from the experiment's page in Controlvar."
        ),
        "not_recognised": (
            "The visual editor does not recognise this sign-in. Open the editor "
            "again from the experiment's page in Controlvar."
        ),
    }
    assert visual_editor.EXPERIMENT_GONE == "This experiment no longer exists."
    assert visual_editor.VARIANT_NOT_FOUND == (
        "This experiment has no variant with that id."
    )
    for text_ in visual_editor.CLOSED_TEXTS.values():
        lowered = text_.lower()
        assert not any(w in lowered for w in ("ticket", "claim", "token", "etag"))


def test_the_session_reads_its_draft_as_the_exchange_answered(
    client, developer, experiment
):
    chain = _claimed(client, developer, experiment)
    opened = _exchange(client, chain["claim"])
    assert opened.status_code == 201, opened.text
    body = opened.json()
    token = body.pop("token")

    response = _current(client, token)

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == body


@pytest.mark.regression
@pytest.mark.parametrize(
    "reason",
    [
        "reopened",
        "ticket_reused",
        "started",
        "signed_out",
        "scheduled",
        "expired",
        "not_recognised",
        "dashboard token",
    ],
)
def test_an_ended_session_says_why(
    client, db_session, developer, experiment, monkeypatch, reason
):
    at = [datetime.now(timezone.utc)]
    monkeypatch.setattr(visual_editor, "utcnow", lambda: at[0])
    chain = _opened(client, developer, experiment)
    headers = _bearer(chain["token"])
    expected = reason
    if reason == "reopened":
        _ticket(client, developer, experiment)
    elif reason == "ticket_reused":
        assert _claim(client, chain["ticket"], _value()).status_code == 409
    elif reason == "started":
        assert _start(client, developer, experiment).status_code == 200
    elif reason == "signed_out":
        assert _sign_out(client, chain["token"]).status_code == 204
    elif reason == "scheduled":
        session = _session(db_session)
        try:
            session.execute(
                text(
                    "UPDATE visual_tokens SET revoked_at = now(), "
                    "revoked_reason = 'scheduled' WHERE token_hash = :h"
                ),
                {"h": hash_api_key(chain["token"])},
            )
            session.commit()
        finally:
            session.close()
    elif reason == "expired":
        at[0] += timedelta(hours=4)
    elif reason == "not_recognised":
        headers = _bearer("cves_" + _value())
    else:
        headers = auth(developer)
        expected = "not_recognised"

    response = client.get(CURRENT, headers=headers)

    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    want = visual_editor.CLOSED_TEXTS[expected]
    assert body["detail"] == want, f"{expected}: {body['detail']!r} != {want!r}"
    assert body == {"detail": want, "reason": expected}


@pytest.mark.regression
@pytest.mark.parametrize("path", ["route", "scheduler"])
def test_a_started_experiments_session_says_started_and_is_recorded(
    client, db_session, developer, experiment, path
):
    chain = _opened(client, developer, experiment)
    session_id = str(_row(db_session, chain["token"]).id)
    before = len(_entries(db_session, experiment))
    if path == "route":
        assert _start(client, developer, experiment).status_code == 200
        label = "start"
    else:
        _date_in_the_past(db_session, experiment)
        _tick(db_session)
        label = "scheduled start"
    state = client.get(f"{EXPERIMENTS}/{experiment}", headers=auth(developer))
    assert state.json()["status"] == "active", state.text

    status, reason = _session_says(client, chain["token"])

    assert status == 401, f"{label}: {status} != 401"
    assert reason == "started"
    found = _entries(db_session, experiment)[before:]
    assert found == ["closed(started)"], f"{found} != ['closed(started)']"
    entry = _audit_rows(db_session, experiment)[-1]
    if path == "route":
        assert entry.user_id == developer.id
    else:
        assert entry.user_id is None
        assert entry.user_email == "system:experiment-scheduler"
    assert json.loads(entry.new_value) == {
        "reason": "started",
        "session_id": session_id,
        "session_user_id": str(developer.id),
    }


@pytest.mark.regression
def test_a_session_write_on_a_dated_draft_is_refused(
    client, db_session, developer, experiment
):
    chain = _opened(client, developer, experiment)
    variant = _variant(client, developer, experiment, "B")
    _set_start_date(db_session, experiment)

    response = _save(client, chain["token"], variant, NEW_CHANGES)

    assert response.status_code == 400, (
        f"changes PUT after schedule: {response.status_code} != 400"
    )
    assert response.json()["detail"] == visual_editor.DATED_DRAFT
    assert _stored_changes(db_session, experiment)["B"] == CHANGES


@pytest.mark.regression
def test_a_save_is_one_experiment_change_by_the_editors_user(
    client, db_session, developer, experiment
):
    """The draft is the developer's; a colleague opens the editor and saves.
    The entry is the colleague's, names the session, and a dashboard save of
    the same draft names none."""
    colleague = _user(db_session, UserRole.DEVELOPER)
    chain = _opened(client, colleague, experiment)
    session_id = _row(db_session, chain["token"]).id
    variant = _variant(client, developer, experiment, "B")

    def updates():
        return [
            row
            for row in _all_audit(db_session, experiment)
            if row.action_type == "experiment_update"
        ]

    before = len(updates())
    saved = _save(client, chain["token"], variant, NEW_CHANGES)
    assert saved.status_code == 200, saved.text

    found = updates()[before:]
    assert len(found) == 1, found
    (entry,) = found
    assert entry.user_id == colleague.id, "actor_id mismatch"
    assert json.loads(entry.new_value) == {"changed_fields": ["visual_changes"]}
    want = f"visual editor session {session_id.hex[:8]}"
    assert entry.reason == want, f"{entry.reason!r} != {want!r}"

    dashboard = client.put(
        f"{EXPERIMENTS}/{experiment}",
        json={"description": "from the dashboard"},
        headers=auth(developer),
    )
    assert dashboard.status_code == 200, dashboard.text
    (plain,) = updates()[before + 1 :]
    assert plain.reason is None and plain.user_id == developer.id


def _all_audit(db_session, experiment_id: str) -> List[AuditLog]:
    session = _session(db_session)
    try:
        return (
            session.query(AuditLog)
            .filter(AuditLog.entity_id == uuid.UUID(experiment_id))
            .order_by(AuditLog.timestamp)
            .all()
        )
    finally:
        session.close()


def test_signing_out_closes_the_session_with_its_entry(
    client, db_session, developer, experiment
):
    chain = _opened(client, developer, experiment)
    session_id = str(_row(db_session, chain["token"]).id)
    before = len(_entries(db_session, experiment))

    response = _sign_out(client, chain["token"])

    assert response.status_code == 204, response.text
    assert _entries(db_session, experiment)[before:] == ["closed(signed_out)"]
    entry = _audit_rows(db_session, experiment)[-1]
    assert entry.user_id == developer.id
    assert json.loads(entry.new_value) == {
        "reason": "signed_out",
        "session_id": session_id,
        "session_user_id": str(developer.id),
    }
    assert _session_says(client, chain["token"]) == (401, "signed_out")
    # The saved design is kept.
    assert _stored_changes(db_session, experiment)["B"] == CHANGES


@pytest.mark.regression
@pytest.mark.parametrize(
    "change", ["dated", "no longer a draft", "deactivated", "demoted"]
)
def test_a_session_signs_out_of_a_draft_it_can_no_longer_edit(
    client, db_session, developer, experiment, change
):
    chain = _opened(client, developer, experiment)
    session_id = str(_row(db_session, chain["token"]).id)
    if change == "dated":
        _set_start_date(db_session, experiment)
        refusal = (400, visual_editor.DATED_DRAFT)
    else:
        statement, values, refusal = {
            "no longer a draft": (
                "UPDATE experiments SET status = 'ACTIVE' WHERE id = :id",
                {"id": uuid.UUID(experiment)},
                (400, visual_editor.NOT_A_DRAFT),
            ),
            "deactivated": (
                "UPDATE users SET is_active = false WHERE id = :id",
                {"id": developer.id},
                (403, visual_editor.CANNOT_CHANGE),
            ),
            "demoted": (
                "UPDATE users SET role = 'VIEWER' WHERE id = :id",
                {"id": developer.id},
                (403, visual_editor.CANNOT_CHANGE),
            ),
        }[change]
        session = _session(db_session)
        try:
            session.execute(text(statement), values)
            session.commit()
        finally:
            session.close()
    got = _current(client, chain["token"])
    assert (got.status_code, got.json()["detail"]) == refusal, (
        f"{change}: GET {got.status_code} != {refusal[0]}"
    )
    before = len(_entries(db_session, experiment))

    response = _sign_out(client, chain["token"])

    assert response.status_code == 204, (
        f"{change}: sign-out {response.status_code} != 204"
    )
    assert _entries(db_session, experiment)[before:] == ["closed(signed_out)"]
    entry = _audit_rows(db_session, experiment)[-1]
    assert entry.user_id == developer.id, f"{change}: actor {entry.user_id}"
    assert json.loads(entry.new_value) == {
        "reason": "signed_out",
        "session_id": session_id,
        "session_user_id": str(developer.id),
    }
    assert _session_says(client, chain["token"]) == (401, "signed_out")


@pytest.mark.regression
def test_a_sign_out_whose_experiment_is_deleted_meanwhile_answers_204(
    client, db_session, developer, experiment, monkeypatch
):
    """Deleting an experiment deletes its editor sessions. The sign-out is
    held once it has found its session; the experiment is deleted; the
    sign-out then answers 204 and writes no entry."""
    chain = _opened(client, developer, experiment)
    pause = _Pause(monkeypatch, visual_editor, "utcnow")
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        held = pool.submit(_sign_out, client, chain["token"])
        assert pause.reached.wait(30), "the sign-out never reached its pause"
        session = _session(db_session)
        try:
            session.execute(
                text("DELETE FROM experiments WHERE id = :id"),
                {"id": uuid.UUID(experiment)},
            )
            session.commit()
        finally:
            session.close()
        assert _rows(db_session, experiment) == []
        pause.released.set()
        response = held.result(timeout=60)
    finally:
        pause.released.set()
        pool.shutdown(wait=True)

    assert response.status_code == 204, f"sign-out {response.status_code} != 204"
    assert _entries(db_session, experiment) == ["opened"]
    assert _session_says(client, chain["token"]) == (401, "not_recognised")


@pytest.mark.regression
@pytest.mark.parametrize("wrong", [*WRONG_ORIGINS, "none"])
def test_the_session_routes_take_the_dashboards_origin(
    client, db_session, developer, experiment, wrong
):
    origin = WRONG_ORIGINS.get(wrong)
    signed_out = _opened(client, developer, experiment)["token"]
    assert _sign_out(client, signed_out).status_code == 204
    chain = _opened(client, developer, experiment)
    token = chain["token"]
    variant = _variant(client, developer, experiment, "B")

    # The Origin comes before the token: a made-up token and a signed-out one
    # get the same 403, never a 401, except a GET with no Origin.
    for label, other in (("made-up", "cves_" + _value()), ("signed-out", signed_out)):
        answers = (
            ("GET", _current(client, other, origin)),
            ("PUT", _save(client, other, variant, NEW_CHANGES, origin)),
            ("DELETE", _sign_out(client, other, origin)),
        )
        for name, answer in answers:
            want = 401 if wrong == "none" and name == "GET" else 403
            assert answer.status_code == want, (
                f"{label} token {name} from {origin}: {answer.status_code} != {want}"
            )
            if want == 403:
                assert answer.json()["detail"] == visual_origins.NOT_FROM_DASHBOARD

    got = _current(client, token, origin)
    put = _save(client, token, variant, NEW_CHANGES, origin)
    deleted = _sign_out(client, token, origin)

    if wrong == "none":
        assert got.status_code == 200, f"same-origin GET {got.status_code} != 200"
        assert put.status_code == 403, f"PUT no Origin: {put.status_code} != 403"
        assert deleted.status_code == 403, (
            f"DELETE no Origin: {deleted.status_code} != 403"
        )
        refused = (put, deleted)
    else:
        for name, answer in (("GET", got), ("PUT", put), ("DELETE", deleted)):
            assert answer.status_code == 403, (
                f"{name} {origin}: {answer.status_code} != 403"
            )
        refused = (got, put, deleted)
    for answer in refused:
        assert answer.json()["detail"] == visual_origins.NOT_FROM_DASHBOARD
    assert _stored_changes(db_session, experiment)["B"] == CHANGES
    # The pair: from each dashboard origin, all three answer.
    for dashboard in (DASHBOARD, DASHBOARD2):
        assert _current(client, token, dashboard).status_code == 200
        variant = _variant(client, developer, experiment, "B")
        assert _save(client, token, variant, NEW_CHANGES, dashboard).status_code == 200
    assert _sign_out(client, token, DASHBOARD2).status_code == 204


@pytest.mark.regression
def test_a_save_keeps_every_other_variants_changes(client, db_session, developer):
    a_changes = {
        "version": 1,
        "changes": [{"op": "hide", "selector": ".banner"}],
    }
    body = {
        "name": f"{PREFIX}{uuid.uuid4().hex[:8]}",
        "experiment_type": "a_b",
        "visual_config": CONFIG,
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 34},
            {
                "name": "A",
                "is_control": False,
                "traffic_allocation": 33,
                "visual_changes": a_changes,
            },
            {
                "name": "B",
                "is_control": False,
                "traffic_allocation": 33,
                "visual_changes": CHANGES,
            },
        ],
        "metrics": [
            {"name": "Buy", "event_name": "click_buy", "metric_type": "conversion"}
        ],
    }
    created = client.post(f"{EXPERIMENTS}/", json=body, headers=auth(developer))
    assert created.status_code == 201, created.text
    experiment = created.json()["id"]
    link = client.post(
        f"{EXPERIMENTS}/{experiment}/visual/preview-links",
        json={"variant_id": _variant(client, developer, experiment, "A")},
        headers=auth(developer),
    )
    assert link.status_code == 201, link.text
    preview = link.json()["url"].partition("#controlvar-preview=")[2]
    key = api_key(client, developer)
    chain = _opened(client, developer, experiment)

    saved = _save(
        client,
        chain["token"],
        _variant(client, developer, experiment, "B"),
        NEW_CHANGES,
    )

    assert saved.status_code == 200, saved.text
    stored = _stored_changes(db_session, experiment)
    assert stored["A"] == a_changes, "variant A changes lost"
    assert stored["B"] == NEW_CHANGES
    assert stored["Control"] is None
    shown = {
        v["name"]: v["visual_changes"] for v in saved.json()["experiment"]["variants"]
    }
    assert shown == {"Control": None, "A": a_changes, "B": NEW_CHANGES}
    read = client.post(
        "/api/v1/tracking/visual/preview",
        json={"token": preview, "host": "shop.example.com", "path": "/"},
        headers=key,
    )
    assert read.status_code == 200, read.text
    assert read.json()["changes"] == a_changes


@pytest.mark.regression
@pytest.mark.parametrize("change", ["demoted", "deactivated"])
def test_a_session_whose_user_may_no_longer_change_the_draft_is_refused(
    client, db_session, developer, experiment, change
):
    chain = _opened(client, developer, experiment)
    variant = _variant(client, developer, experiment, "B")
    session = _session(db_session)
    try:
        if change == "demoted":
            statement = "UPDATE users SET role = 'VIEWER' WHERE id = :id"
        else:
            statement = "UPDATE users SET is_active = false WHERE id = :id"
        session.execute(text(statement), {"id": developer.id})
        session.commit()
    finally:
        session.close()

    got = _current(client, chain["token"])
    put = _save(client, chain["token"], variant, NEW_CHANGES)

    for answer in (got, put):
        assert answer.status_code == 403, f"{change}: {answer.status_code} != 403"
        assert answer.json()["detail"] == visual_editor.CANNOT_CHANGE
    assert _stored_changes(db_session, experiment)["B"] == CHANGES


# --- the session's reach ----------------------------------------------------------

SESSION_ROUTES = {
    ("GET", "/api/v1/visual-editor/sessions/current"),
    ("DELETE", "/api/v1/visual-editor/sessions/current"),
    ("PUT", "/api/v1/visual-editor/sessions/current/variants/{variant_id}/changes"),
}
EDITOR_ROUTES = SESSION_ROUTES | {
    ("POST", "/api/v1/visual-editor/tickets/claim"),
    ("POST", "/api/v1/visual-editor/sessions"),
}


def _http_routes() -> List[tuple]:
    found = set()
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                found.add((method, ctx.path))
    return sorted(found)


def test_the_editor_routes_are_exactly_these():
    routes = _http_routes()
    editor = {(m, p) for m, p in routes if p.startswith("/api/v1/visual-editor/")}
    unexpected = sorted(editor - EDITOR_ROUTES)
    assert not unexpected, f"unexpected route {unexpected}"
    assert editor == EDITOR_ROUTES
    visual = {
        (m, p) for m, p in routes if p.startswith(f"{EXPERIMENTS}/") and "/visual/" in p
    }
    assert visual == {
        ("POST", "/api/v1/experiments/{experiment_id}/visual/preview-links"),
        ("DELETE", "/api/v1/experiments/{experiment_id}/visual/preview-links"),
        ("POST", "/api/v1/experiments/{experiment_id}/visual/editor-tickets"),
    }
    words = ("renew", "audience", "preview")
    stray = [p for m, p in editor for w in words if w in p]
    assert stray == [], f"unexpected route {stray}"


def _answer(response):
    """What is compared: status, WWW-Authenticate, and a refusal's body."""
    body = None
    if response.status_code in (401, 403):
        try:
            body = response.json()
        except ValueError:
            body = response.text
    return response.status_code, response.headers.get("www-authenticate"), body


#: Each place a request carries a credential the API reads.
POSITIONS = {
    "Authorization": lambda token: {"Authorization": f"Bearer {token}"},
    "X-API-Key": lambda token: {"X-API-Key": token},
}


def _send(client, method: str, path: str, token: str, position: str):
    url = re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000000", path)
    body = {} if method in ("POST", "PUT", "PATCH", "DELETE") else None
    headers = {**POSITIONS[position](token), **_origin(DASHBOARD)}
    return client.request(method, url, json=body, headers=headers)


@pytest.mark.regression
def test_no_other_route_takes_a_session_token(client, developer, experiment):
    """Every route but the session's own, and the session's own routes in
    every place but ``Authorization: Bearer``, answer a live session's token
    exactly as they answer a made-up one, the token sent as a bearer token
    and as an API key; on its own three routes, as a bearer token, the live
    token gets in and the made-up one does not."""
    chain = _opened(client, developer, experiment)
    live, made_up = chain["token"], "cves_" + _value()
    routes = _http_routes()
    assert len(routes) >= 150, len(routes)
    taken = []
    for method, path in routes:
        for position in POSITIONS:
            if (method, path) in SESSION_ROUTES and position == "Authorization":
                continue
            if _answer(_send(client, method, path, live, position)) != _answer(
                _send(client, method, path, made_up, position)
            ):
                taken.append(f"{method} {path} ({position})")
    assert not taken, f"beyond allow-list: {sorted(taken)}"
    variant = _variant(client, developer, experiment, "B")
    assert _current(client, made_up).status_code == 401
    assert _current(client, live).status_code == 200
    assert _save(client, made_up, variant, NEW_CHANGES).status_code == 401
    assert _save(client, live, variant, NEW_CHANGES).status_code == 200
    assert _sign_out(client, made_up).status_code == 401
    assert _sign_out(client, live).status_code == 204


@pytest.mark.regression
def test_each_credential_lasts_its_time(
    client, db_session, developer, experiment, monkeypatch
):
    base = datetime(2031, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    at = [base]
    monkeypatch.setattr(visual_editor, "utcnow", lambda: at[0])

    ticket = _ticket(client, developer, experiment)
    at[0] = base + timedelta(seconds=120)
    late = _claim(client, ticket, _value())
    assert late.status_code == 401, f"ticket after 120 s: {late.status_code} != 401"
    assert late.json()["detail"] == visual_editor.LINK_NOT_VALID

    at[0] = base
    chain = _claimed(client, developer, experiment)
    at[0] = base + timedelta(seconds=120)
    late = _exchange(client, chain["claim"])
    assert late.status_code == 401, f"claim after 120 s: {late.status_code} != 401"

    at[0] = base
    chain = _opened(client, developer, experiment)
    at[0] = base + timedelta(hours=4) - timedelta(seconds=1)
    assert _session_says(client, chain["token"]) == (200, None)
    variant = _variant(client, developer, experiment, "B")
    assert _save(client, chain["token"], variant, NEW_CHANGES).status_code == 200
    at[0] = base + timedelta(hours=4)
    assert _session_says(client, chain["token"]) == (401, "expired")
    # What was saved is kept, and the editor opens again from the draft.
    assert _stored_changes(db_session, experiment)["B"] == NEW_CHANGES
    assert _mint(client, developer, experiment).status_code == 201


@pytest.mark.regression
def test_a_variant_of_another_experiment_is_not_saved(
    client, db_session, developer, experiment
):
    other = _draft(client, developer)
    theirs = _variant(client, developer, other, "B")
    chain = _opened(client, developer, experiment)

    response = _save(client, chain["token"], theirs, NEW_CHANGES)

    assert response.status_code == 404, f"{response.status_code} != 404"
    assert response.json()["detail"] == visual_editor.VARIANT_NOT_FOUND
    assert _stored_changes(db_session, other)["B"] == CHANGES
    assert _stored_changes(db_session, experiment)["B"] == CHANGES


@pytest.mark.regression
def test_a_session_token_makes_no_preview_link(client, developer, experiment):
    chain = _opened(client, developer, experiment)
    variant = _variant(client, developer, experiment, "B")
    url = f"{EXPERIMENTS}/{experiment}/visual/preview-links"

    refused = client.post(
        url, json={"variant_id": variant}, headers=_bearer(chain["token"], DASHBOARD)
    )

    assert refused.status_code == 401, f"{refused.status_code} != 401"
    made = client.post(url, json={"variant_id": variant}, headers=auth(developer))
    assert made.status_code == 201, made.text
