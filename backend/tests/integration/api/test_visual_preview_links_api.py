"""Preview links for a visual experiment's variants (#1545, PR 2, S4).

``POST`` and ``DELETE /api/v1/experiments/{id}/visual/preview-links`` on the
dashboard, and ``POST /api/v1/tracking/visual/preview`` from the page, on
Postgres through real routes, with users of each role signed in with local
tokens. The rows of PLAN-v3.1's VERIFICATION held here, each with the defect
that fails it:

* V26  ADMIN, DEVELOPER and a superuser make and revoke links; ANALYST and
       VIEWER get 403; in the full profile a VIEWER with a direct grant of
       experiment (update) may, and one whose grant never took effect may
       not. Plant: no change-access check (403 becomes 201).
* V30  each make and each revoke writes one audit entry, the signed-in user as
       its actor, holding neither the token nor a variant name.
* V32  (server) a preview is isolated: 50 reads record no assignment, no
       exposure and no event; a tampered, expired or revoked token answers one
       404 text. Plant: the read assigns the visitor (0 becomes 50).
* V84  (preview create) the experiment's origins pass C4 or the link is not
       made: no dashboard origin, ``*``, a site the server does not accept and
       a page on the dashboard each answer 422 with their text, and no row is
       written.
* V96  (preview read) a key with any scope is refused with 403.
* C11  the presented key decides: an owner's key with no scopes is served and
       the same owner's ``sdk:ruleset`` key refused, on both page routes,
       whichever of the two was made last. Plant: the check reads the
       owner's newest key instead of the presented one.

Also: a link works on a draft and on the control, survives a variants save
that re-creates the variants, lasts 1 to 168 hours, and is shown once (only
its hash is stored).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api.page_key import SCOPED_KEY_IN_PAGE_DETAIL
from backend.app.api.v1.endpoints import tracking_visual
from backend.app.api.v1.endpoints.visual_preview_links import NOT_VISUAL
from backend.app.core.config import settings
from backend.app.core.permissions import (
    Action,
    ResourceType,
    get_permission_error_message,
)
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import UserRole
from backend.app.models.visual_token import VisualToken
from backend.app.services import visual_origins
from backend.tests.integration.api.route_helpers import (
    api_client,
    api_key,
    auth,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
PREVIEW = "/api/v1/tracking/visual/preview"
VISUAL = "/api/v1/tracking/visual"
PREFIX = "visual1545prev-"
SCHEMA = "test_experimentation"
HOST = "shop.example.com"
SHOP = f"https://{HOST}"
DASHBOARD = "http://localhost:3100"
CHANGES = {
    "version": 1,
    "changes": [{"op": "set_text", "selector": "#headline", "text": "Hello"}],
}
PAGE = {"match": "prefix", "url": f"{SHOP}/"}
CONFIG = {
    "version": 1,
    "editor_url": f"{SHOP}/start?x=1",
    "pages": [PAGE],
    "goals": [
        {"event_name": "click_buy", "kind": "click", "selector": "#buy", "page": PAGE}
    ],
}
UPDATE_REFUSED = get_permission_error_message(ResourceType.EXPERIMENT, Action.UPDATE)


@pytest.fixture(autouse=True)
def _local_auth_and_origins(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "BACKEND_CORS_ORIGINS", [])
    monkeypatch.setattr(settings, "CORS_ORIGINS", [SHOP])
    monkeypatch.setattr(settings, "DASHBOARD_ORIGINS", [DASHBOARD])
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)


def _session(db_session):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)
    session = factory()
    session.execute(text(f"SET search_path TO {SCHEMA}"))
    return session


@pytest.fixture(autouse=True)
def _remove_what_the_tests_made(db_session):
    yield
    session = _session(db_session)
    try:
        if _has_table(session, "direct_permission_grants"):
            session.execute(
                text(
                    "DELETE FROM direct_permission_grants WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE 'prev1545_%')"
                )
            )
        session.execute(
            text("DELETE FROM experiments WHERE name LIKE :p"), {"p": f"{PREFIX}%"}
        )
        session.commit()
    finally:
        session.close()


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


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


def _user(db_session, role: UserRole, superuser: bool = False):
    return make_user(db_session, role, superuser=superuser, tag="prev1545")


@pytest.fixture
def developer(db_session):
    return _user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def key(client, developer):
    return api_key(client, developer)


def _draft(client, user, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
    return response.json()


def _variant(experiment: Dict[str, Any], name: str) -> str:
    return next(v["id"] for v in experiment["variants"] if v["name"] == name)


def _make_link(client, user, experiment, variant="B", status=201, **body):
    response = client.post(
        f"{EXPERIMENTS}/{experiment['id']}/visual/preview-links",
        json={"variant_id": _variant(experiment, variant), **body},
        headers=auth(user),
    )
    assert response.status_code == status, response.text
    return response


def _token(link_response) -> str:
    url = link_response.json()["url"]
    match = re.fullmatch(r".*#controlvar-preview=([A-Za-z0-9_-]{43})", url)
    assert match, url
    return match.group(1)


def _read(client, key, token: str, status: int = 200, host: str = HOST):
    response = client.post(
        PREVIEW, json={"token": token, "host": host, "path": "/shoes"}, headers=key
    )
    assert response.status_code == status, response.text
    return response


def _rows(db_session, experiment_id: str) -> List[VisualToken]:
    session = _session(db_session)
    try:
        return (
            session.query(VisualToken)
            .filter(VisualToken.experiment_id == uuid.UUID(experiment_id))
            .all()
        )
    finally:
        session.close()


def _audit(db_session, experiment_id: str) -> List[AuditLog]:
    session = _session(db_session)
    try:
        return (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_id == uuid.UUID(experiment_id),
                AuditLog.action_type.like("experiment_preview%"),
            )
            .order_by(AuditLog.timestamp)
            .all()
        )
    finally:
        session.close()


# --- making a link and opening it --------------------------------------------------


def test_a_link_shows_its_variant_on_the_page(client, db_session, developer, key):
    experiment = _draft(client, developer)
    before = datetime.now(timezone.utc)
    made = _make_link(client, developer, experiment)
    assert made.headers["cache-control"] == "no-store"
    body = made.json()
    assert body["url"].startswith(f"{SHOP}/start?x=1#controlvar-preview=")
    token = _token(made)
    expires = datetime.fromisoformat(body["expires_at"])
    assert (
        before + timedelta(hours=24)
        <= expires
        <= datetime.now(timezone.utc) + timedelta(hours=24)
    )

    (row,) = _rows(db_session, experiment["id"])
    assert row.kind == "preview" and row.variant_name == "B"
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert row.user_id == developer.id
    assert token not in str(
        {c.name: getattr(row, c.name) for c in VisualToken.__table__.columns}
    )

    read = _read(client, key, token)
    assert read.headers["cache-control"] == "no-store"
    assert read.json() == {
        "experiment_key": experiment["key"],
        "variant_name": "B",
        "changes": CHANGES,
        "pages": {
            "host": HOST,
            "rules": [{"match": "prefix", "path": "/"}],
            "max_age": 60,
        },
    }
    # Another host gets the variant with no rule for it.
    other = _read(client, key, token, host="other.example.com").json()
    assert other["pages"]["rules"] == []


def test_a_link_works_for_the_control_and_survives_a_variants_save(
    client, developer, key
):
    experiment = _draft(client, developer)
    control_token = _token(_make_link(client, developer, experiment, variant="Control"))
    assert _read(client, key, control_token).json()["changes"] is None
    b_token = _token(_make_link(client, developer, experiment))
    new_changes = {
        "version": 1,
        "changes": [{"op": "hide", "selector": "#promo"}],
    }
    saved = client.put(
        f"{EXPERIMENTS}/{experiment['id']}",
        json={
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {
                    "name": "B",
                    "is_control": False,
                    "traffic_allocation": 50,
                    "visual_changes": new_changes,
                },
            ]
        },
        headers=auth(developer),
    )
    assert saved.status_code == 200, saved.text
    assert _variant(saved.json(), "B") != _variant(experiment, "B")
    assert _read(client, key, b_token).json()["changes"] == new_changes


def test_the_link_lasts_one_to_168_hours(client, developer):
    experiment = _draft(client, developer)
    for hours in (1, 168):
        made = _make_link(client, developer, experiment, expires_in_hours=hours).json()
        left = datetime.fromisoformat(made["expires_at"]) - datetime.now(timezone.utc)
        assert (
            timedelta(hours=hours) - timedelta(minutes=1)
            < left
            <= timedelta(hours=hours)
        )
    for refused in (0, 169, "24", 1.5, None):
        _make_link(client, developer, experiment, expires_in_hours=refused, status=422)
    _make_link(client, developer, experiment, status=422, uses=3)


def test_what_is_refused_before_a_link_is_made(client, db_session, developer):
    experiment = _draft(client, developer)
    unknown = client.post(
        f"{EXPERIMENTS}/{uuid.uuid4()}/visual/preview-links",
        json={"variant_id": str(uuid.uuid4())},
        headers=auth(developer),
    )
    assert unknown.status_code == 404
    other_variant = client.post(
        f"{EXPERIMENTS}/{experiment['id']}/visual/preview-links",
        json={"variant_id": str(uuid.uuid4())},
        headers=auth(developer),
    )
    assert other_variant.status_code == 404
    code = client.post(
        f"{EXPERIMENTS}/",
        json={
            "name": f"{PREFIX}code-{uuid.uuid4().hex[:6]}",
            "experiment_type": "a_b",
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {"name": "B", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {"name": "Buy", "event_name": "click_buy", "metric_type": "conversion"}
            ],
        },
        headers=auth(developer),
    ).json()
    not_visual = _make_link(client, developer, code, status=400)
    assert not_visual.json()["detail"] == NOT_VISUAL
    assert _rows(db_session, experiment["id"]) == []
    assert _rows(db_session, code["id"]) == []


# --- V32: a preview is isolated --------------------------------------------------------


def _counts(db_session, experiment_id: str) -> Dict[str, int]:
    session = _session(db_session)
    try:
        uid = uuid.UUID(experiment_id)
        return {
            "assignments": session.execute(
                text("SELECT count(*) FROM assignments WHERE experiment_id = :e"),
                {"e": uid},
            ).scalar_one(),
            "events": session.execute(
                text("SELECT count(*) FROM events WHERE experiment_id = :e"),
                {"e": uid},
            ).scalar_one(),
        }
    finally:
        session.close()


@pytest.mark.regression
def test_a_preview_records_nothing(client, db_session, developer, key):
    experiment = _draft(client, developer)
    token = _token(_make_link(client, developer, experiment))
    for _ in range(50):
        assert _read(client, key, token).json()["variant_name"] == "B"
    assert _counts(db_session, experiment["id"]) == {"assignments": 0, "events": 0}
    (row,) = _rows(db_session, experiment["id"])
    assert row.used_at is None


@pytest.mark.regression
def test_a_tampered_expired_or_revoked_link_answers_one_404(
    client, db_session, developer, key
):
    experiment = _draft(client, developer)
    token = _token(_make_link(client, developer, experiment))
    expired = _token(_make_link(client, developer, experiment))
    session = _session(db_session)
    try:
        session.execute(
            text(
                "UPDATE visual_tokens SET expires_at = now() - interval '1 second' "
                "WHERE token_hash = :h"
            ),
            {"h": hashlib.sha256(expired.encode()).hexdigest()},
        )
        session.commit()
    finally:
        session.close()
    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    for refused in (tampered, expired, token[:-1], token + "x", "x" * 128):
        response = _read(client, key, refused, status=404)
        assert response.json()["detail"] == tracking_visual.PREVIEW_NOT_FOUND
    assert _read(client, key, token).status_code == 200
    revoked = client.delete(
        f"{EXPERIMENTS}/{experiment['id']}/visual/preview-links",
        headers=auth(developer),
    )
    assert revoked.status_code == 204, revoked.text
    response = _read(client, key, token, status=404)
    assert response.json()["detail"] == tracking_visual.PREVIEW_NOT_FOUND


def test_revoking_ends_only_this_experiments_links(client, developer, key):
    first = _draft(client, developer)
    second = _draft(client, developer)
    first_token = _token(_make_link(client, developer, first))
    second_token = _token(_make_link(client, developer, second))
    assert (
        client.delete(
            f"{EXPERIMENTS}/{first['id']}/visual/preview-links", headers=auth(developer)
        ).status_code
        == 204
    )
    _read(client, key, first_token, status=404)
    _read(client, key, second_token)


# --- V96 (preview read): keys ------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("scopes", [["sdk:ruleset"], ["mcp:read"], ["future:x"]])
def test_a_key_with_any_scope_cannot_read_a_preview(client, developer, scopes):
    experiment = _draft(client, developer)
    token = _token(_make_link(client, developer, experiment))
    scoped = api_key(client, developer, scopes=scopes)
    response = _read(client, scoped, token, status=403)
    assert response.json()["detail"] == SCOPED_KEY_IN_PAGE_DETAIL


# --- C11: the presented key decides, not another key of its owner ----------------------


def _page_statuses(client, key: Dict[str, str], token: str) -> Dict[str, int]:
    """The status each page route answers *key* with."""
    page = client.post(
        VISUAL,
        json={
            "user_id": f"prev1545-{uuid.uuid4().hex[:12]}",
            "host": f"other-{uuid.uuid4().hex[:8]}.example.com",
            "path": "/",
            "format_version": 1,
        },
        headers=key,
    )
    preview = client.post(
        PREVIEW, json={"token": token, "host": HOST, "path": "/"}, headers=key
    )
    return {"visual": page.status_code, "preview": preview.status_code}


@pytest.mark.regression
@pytest.mark.parametrize("first", ["unscoped", "scoped"])
def test_the_presented_key_decides_on_both_page_routes(
    client, db_session, developer, first
):
    """One owner holds a key with no scopes and one with ``sdk:ruleset``,
    made in either order: on both page routes the first is served and the
    second refused, whichever of them is the owner's newest."""
    experiment = _draft(client, developer)
    token = _token(_make_link(client, developer, experiment))
    owner = _user(db_session, UserRole.DEVELOPER)
    scopes = {"unscoped": [], "scoped": ["sdk:ruleset"]}
    second = "scoped" if first == "unscoped" else "unscoped"
    keys = {
        name: api_key(client, owner, scopes=scopes[name]) for name in (first, second)
    }
    assert _page_statuses(client, keys["unscoped"], token) == {
        "visual": 200,
        "preview": 200,
    }
    assert _page_statuses(client, keys["scoped"], token) == {
        "visual": 403,
        "preview": 403,
    }


# --- V26: who may make and revoke links -----------------------------------------------

ROLES = {
    "admin": (UserRole.ADMIN, False, True),
    "developer": (UserRole.DEVELOPER, False, True),
    "superuser": (UserRole.ADMIN, True, True),
    "analyst": (UserRole.ANALYST, False, False),
    "viewer": (UserRole.VIEWER, False, False),
}


@pytest.mark.regression
@pytest.mark.parametrize("role_name", sorted(ROLES))
def test_making_and_revoking_need_experiment_update(
    client, db_session, developer, role_name
):
    role, superuser, allowed = ROLES[role_name]
    user = _user(db_session, role, superuser)
    experiment = _draft(client, developer)
    made = client.post(
        f"{EXPERIMENTS}/{experiment['id']}/visual/preview-links",
        json={"variant_id": _variant(experiment, "B")},
        headers=auth(user),
    )
    revoked = client.delete(
        f"{EXPERIMENTS}/{experiment['id']}/visual/preview-links", headers=auth(user)
    )
    if allowed:
        assert (made.status_code, revoked.status_code) == (201, 204), made.text
    else:
        assert (made.status_code, revoked.status_code) == (403, 403)
        assert made.json()["detail"] == UPDATE_REFUSED
        assert _rows(db_session, experiment["id"]) == []


def _grant_experiment_update(db_session, user, activated: bool) -> None:
    session = _session(db_session)
    try:
        session.execute(
            text(
                "INSERT INTO direct_permission_grants (id, user_id, resource, actions, "
                "is_active, activated_at, created_at, updated_at) VALUES (:id, :user, "
                "'experiment', CAST('[\"update\"]' AS jsonb), true, "
                + ("now()" if activated else "NULL")
                + ", now(), now())"
            ),
            {"id": uuid.uuid4(), "user": user.id},
        )
        session.commit()
    finally:
        session.close()


@pytest.mark.modules
@pytest.mark.parametrize("activated", [True, False], ids=["grant", "inactive grant"])
def test_a_direct_grant_of_experiment_update_decides(
    client, db_session, developer, activated
):
    viewer = _user(db_session, UserRole.VIEWER)
    _grant_experiment_update(db_session, viewer, activated)
    experiment = _draft(client, developer)
    made = client.post(
        f"{EXPERIMENTS}/{experiment['id']}/visual/preview-links",
        json={"variant_id": _variant(experiment, "B")},
        headers=auth(viewer),
    )
    assert made.status_code == (201 if activated else 403), made.text


# --- V30: the audit entries -------------------------------------------------------------


@pytest.mark.regression
def test_making_and_revoking_are_audited(client, db_session, developer, key):
    experiment = _draft(client, developer)
    made = _make_link(client, developer, experiment, expires_in_hours=2)
    token = _token(made)
    _make_link(client, developer, experiment, variant="Control")
    revoked = client.delete(
        f"{EXPERIMENTS}/{experiment['id']}/visual/preview-links",
        headers=auth(developer),
    )
    assert revoked.status_code == 204
    again = client.delete(
        f"{EXPERIMENTS}/{experiment['id']}/visual/preview-links",
        headers=auth(developer),
    )
    assert again.status_code == 204
    rows = _audit(db_session, experiment["id"])
    assert [r.action_type for r in rows] == [
        "experiment_preview_link_create",
        "experiment_preview_link_create",
        "experiment_preview_links_revoke",
        "experiment_preview_links_revoke",
    ]
    assert {r.user_id for r in rows} == {developer.id}
    assert {r.entity_type for r in rows} == {"experiment"}
    assert {r.entity_name for r in rows} == {experiment["name"]}
    first = json.loads(rows[0].new_value)
    assert set(first) == {"variant_id", "expires_at"}
    assert first["variant_id"] == _variant(experiment, "B")
    assert datetime.fromisoformat(first["expires_at"]) == datetime.fromisoformat(
        made.json()["expires_at"]
    )
    assert json.loads(rows[2].new_value) == {"revoked": 2}
    assert json.loads(rows[3].new_value) == {"revoked": 0}
    for row in rows:
        stored = f"{row.old_value} {row.new_value} {row.reason}"
        assert token not in stored
        assert hashlib.sha256(token.encode()).hexdigest() not in stored
        assert '"B"' not in stored and "Control" not in stored


# --- V84 (preview create): the experiment's origins --------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "cors,dashboard,config_url,expected",
    [
        ([SHOP], [], f"{SHOP}/", visual_origins.NO_DASHBOARD_ORIGIN),
        (["*"], [DASHBOARD], f"{SHOP}/", visual_origins.EVERY_SITE_ALLOWED),
        (
            [SHOP],
            [DASHBOARD],
            "https://staging.example.com/",
            visual_origins.ORIGIN_NOT_ALLOWED.format(
                origin="https://staging.example.com"
            ),
        ),
        (
            [SHOP],
            [DASHBOARD],
            "https://shop.example.com.evil.test/",
            visual_origins.ORIGIN_NOT_ALLOWED.format(
                origin="https://shop.example.com.evil.test"
            ),
        ),
        ([SHOP], [SHOP], f"{SHOP}/", visual_origins.PAGE_ON_DASHBOARD),
    ],
    ids=[
        "no dashboard origin",
        "every site",
        "not allowed",
        "suffix",
        "on the dashboard",
    ],
)
def test_a_link_is_made_only_for_origins_the_editor_may_use(
    client, db_session, developer, monkeypatch, cors, dashboard, config_url, expected
):
    page = {"match": "prefix", "url": config_url}
    experiment = _draft(
        client,
        developer,
        {"version": 1, "editor_url": config_url, "pages": [page], "goals": []},
    )
    monkeypatch.setattr(settings, "CORS_ORIGINS", cors)
    monkeypatch.setattr(settings, "DASHBOARD_ORIGINS", dashboard)
    refused = _make_link(client, developer, experiment, status=422)
    assert refused.json()["detail"] == expected
    assert _rows(db_session, experiment["id"]) == []
    assert _audit(db_session, experiment["id"]) == []
