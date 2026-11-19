"""The visual editor's one-time sign-in (#1545): opening the editor from the
dashboard, the editor link's claim from the site's page, and the exchange for
an editor session, on Postgres through the real routes.

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
* V79     a used ticket or claim presented again ends everything descended
          from the ticket; two at once store exactly one row.
* V80     opening the editor again ends every earlier editor credential of
          that experiment, whoever holds it, and nothing of another one.
* V81     the audit entries of each case, exactly.
* V82     the dashboard's origins for the dashboard's routes, the
          experiment's sites for the page's route.
* V84     the dashboard origins the mint sees, per environment.
* V26     who may open the editor.
* V65     a dated draft is not opened.
* Also: a browser's preflighted request reaches the claim route; nothing
  stored or logged holds a credential or its hash; the texts.

Until the session routes exist, "a session answers 401" is read from its row
(``revoked_reason``).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
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
from backend.tests.integration.api.route_helpers import auth, make_user

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
    """Why the credential *value* was ended; ``None`` while it is live."""
    row = _row(db_session, value)
    assert row is not None, "no such credential"
    return row.revoked_reason


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
@pytest.mark.parametrize("via", ["claim route", "exchange"])
def test_a_used_link_presented_again_ends_the_session_it_opened(
    client, db_session, developer, experiment, via
):
    chain = _opened(client, developer, experiment)
    assert _ended_why(db_session, chain["token"]) is None

    if via == "claim route":
        again = _claim(client, chain["ticket"], _value())
    else:
        again = _exchange(client, chain["claim"])

    assert again.status_code == 409, again.text
    assert again.json()["detail"] == visual_editor.LINK_USED_TWICE
    reason = _ended_why(db_session, chain["token"])
    assert reason == TICKET_REUSED, f"session {reason!r} != {TICKET_REUSED!r}"
    assert _rows(db_session, experiment, EDITOR_SESSION)[0].revoked_at is not None


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
    theirs = _ended_why(db_session, s_colleague["token"])
    assert theirs == REOPENED, f"user B's session: {theirs!r} != {REOPENED!r}"
    assert _ended_why(db_session, s1["token"]) is None
    pending = _claimed(client, developer, experiment)  # ends s1
    unclaimed = _ticket(client, developer, experiment)  # ends the pending claim
    s2 = _opened(client, developer, experiment)  # ends the unclaimed ticket

    assert _ended_why(db_session, s1["token"]) == REOPENED
    assert _ended_why(db_session, pending["claim"]) == REOPENED
    assert _ended_why(db_session, pending["ticket"]) == REOPENED
    assert _ended_why(db_session, unclaimed) == REOPENED
    assert _claim(client, unclaimed, _value()).status_code == 401
    assert _exchange(client, pending["claim"]).status_code == 401
    assert _ended_why(db_session, s2["token"]) is None
    other = _ended_why(db_session, s_other["token"])
    assert other is None, f"other experiment's session: {other!r} != None"


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
