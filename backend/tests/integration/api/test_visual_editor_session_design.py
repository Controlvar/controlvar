"""What the visual editor session reads of a draft's design (#1545): its
audience, split, metrics and goals, and the segments and exclusion groups it
may choose from, on Postgres through the real routes.

Users of each role sign in with local tokens. TestClient sends no ``Origin``:
every request here sets the one it means, and every "wrong Origin" refusal is
paired with the same call from a dashboard origin.

The rows of PLAN-v3.1's VERIFICATION held here, each with the defect that
fails it (the failing line is the assertion's message or comparison):

* BD2     the session's answer -- the exchange's, GET current's and the
          changes PUT's -- carries the draft's audience, each variant's
          share, every field of every metric, the goals and the exclusion
          group, each equal to the experiment's own answer for the same
          row. A metric model of four fields fails
          ``metrics[0].lower_is_better missing``; the group read from another
          row fails ``mutual_exclusion_group``.
* D9      the global holdout goes to a user who may create experiments,
          never to one who may only change them, and is null when none is
          active. Shown to everyone: ``update-only role: {...} != None``.
* Reads   the segments read lists active segments only, as
          ``{id, name, kind, rules_valid}``, with ``rules_valid`` false for
          stored rules the segment check refuses, and needs experiment list;
          the exclusion-groups read lists active groups only, as
          ``{id, name}``, and needs experiment create.
* V82     each read: no Origin and each dashboard origin answer 200, each
          wrong origin 403.
"""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.db import request_sessions
from backend.app.main import app
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.user import UserRole
from backend.app.services import visual_editor, visual_origins
from backend.tests.integration.api.route_helpers import auth, make_user

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
SEGMENTS_API = "/api/v1/segments"
GROUPS_API = "/api/v1/mutual-exclusion-groups"
CLAIM = "/api/v1/visual-editor/tickets/claim"
SESSIONS = "/api/v1/visual-editor/sessions"
CURRENT = f"{SESSIONS}/current"
EDITOR_SEGMENTS = f"{CURRENT}/segments"
EDITOR_GROUPS = f"{CURRENT}/exclusion-groups"
PREFIX = "vis1545design-"
TAG = "design1545"
SCHEMA = "test_experimentation"

SHOP = "https://shop.example.com"
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
NEW_CHANGES = {
    "version": 1,
    "changes": [{"op": "set_text", "selector": "#headline", "text": "Welcome"}],
}
PAGE = {"match": "prefix", "url": f"{SHOP}/"}
CONFIG = {
    "version": 1,
    "editor_url": f"{SHOP}/start",
    "pages": [PAGE],
    "goals": [
        {"event_name": "click_buy", "kind": "click", "selector": "#buy", "page": PAGE},
        {"event_name": "page_thanks", "kind": "page_view", "page": PAGE},
    ],
}
#: Every metric field set away from its default.
METRICS = [
    {
        "name": "Buy",
        "description": "Clicked buy",
        "event_name": "click_buy",
        "metric_type": "conversion",
        "is_primary": True,
        "aggregation_method": "sum",
        "minimum_sample_size": 500,
        "expected_effect": 0.1,
        "event_value_path": "amount",
        "lower_is_better": True,
    },
    {
        "name": "Thanks",
        "description": "Saw the thank-you page",
        "event_name": "page_thanks",
        "metric_type": "count",
        "is_primary": False,
        "aggregation_method": "median",
        "minimum_sample_size": 250,
        "expected_effect": 0.03,
        "event_value_path": "count",
        "lower_is_better": True,
    },
]
COUNTRY_RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "US"}
            ],
        }
    ],
}


def _in_segment(segment_id: str) -> Dict[str, Any]:
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {
                        "attribute": "segment",
                        "operator": "in_segment",
                        "value": segment_id,
                    }
                ],
            }
        ],
    }


@pytest.fixture(autouse=True)
def _sign_in_and_origins(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "BACKEND_CORS_ORIGINS", [])
    monkeypatch.setattr(settings, "CORS_ORIGINS", [SHOP])
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
        session.execute(
            text("DELETE FROM segments WHERE name LIKE :p"), {"p": f"{PREFIX}%"}
        )
        session.execute(
            text("DELETE FROM mutual_exclusion_groups WHERE name LIKE :p"),
            {"p": f"{PREFIX}%"},
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


def _name(label: str) -> str:
    return f"{PREFIX}{label}-{uuid.uuid4().hex[:8]}"


def _draft(client, user, **fields) -> str:
    body = {
        "name": _name("draft"),
        "experiment_type": "a_b",
        "visual_config": CONFIG,
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 40},
            {
                "name": "B",
                "is_control": False,
                "traffic_allocation": 60,
                "visual_changes": CHANGES,
            },
        ],
        "metrics": METRICS,
        **fields,
    }
    response = client.post(f"{EXPERIMENTS}/", json=body, headers=auth(user))
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def experiment(client, developer) -> str:
    return _draft(client, developer)


def _segment(client, user, **body) -> Dict[str, Any]:
    response = client.post(
        SEGMENTS_API, json={"name": _name("segment"), **body}, headers=auth(user)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _group(client, user) -> Dict[str, Any]:
    response = client.post(
        GROUPS_API, json={"name": _name("group")}, headers=auth(user)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _join(client, user, group_id: str, experiment_id: str) -> None:
    response = client.post(
        f"{GROUPS_API}/{group_id}/experiments",
        json={"experiment_id": experiment_id},
        headers=auth(user),
    )
    assert response.status_code == 200, response.text


def _origin(origin: Optional[str]) -> Dict[str, str]:
    return {} if origin is None else {"Origin": origin}


def _value() -> str:
    return secrets.token_urlsafe(32)


def _exchanged(client, user, experiment_id: str):
    """The exchange's answer for a new editor of *experiment_id*."""
    minted = client.post(
        f"{EXPERIMENTS}/{experiment_id}/visual/editor-tickets",
        headers={**auth(user), **_origin(DASHBOARD)},
    )
    assert minted.status_code == 201, minted.text
    ticket = minted.json()["open_url"].partition("#controlvar-editor=")[2]
    claim = _value()
    claimed = client.post(
        CLAIM, json={"ticket": ticket, "claim": claim}, headers=_origin(SHOP)
    )
    assert claimed.status_code == 201, claimed.text
    opened = client.post(
        SESSIONS,
        json={"claim": claim, "page_origin": SHOP},
        headers=_origin(DASHBOARD),
    )
    assert opened.status_code == 201, opened.text
    return opened


def _token(client, user, experiment_id: str) -> str:
    return _exchanged(client, user, experiment_id).json()["token"]


def _bearer(token: str, origin: Optional[str] = None) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", **_origin(origin)}


def _get(client, path: str, token: str, origin: Optional[str] = None):
    return client.get(path, headers=_bearer(token, origin))


def _detail(client, user, experiment_id: str) -> Dict[str, Any]:
    response = client.get(f"{EXPERIMENTS}/{experiment_id}", headers=auth(user))
    assert response.status_code == 200, response.text
    return response.json()


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


def _changing_only(db_session, client, developer, experiment_id: str) -> str:
    """A session for a user with no built-in role (every built-in role may
    list experiments) and a custom role granting experiment read and update
    only: it may change the draft, and neither list nor create experiments."""
    user = _user(db_session, UserRole.VIEWER)
    session = _session(db_session)
    try:
        session.execute(
            text("UPDATE users SET role = NULL WHERE id = :id"), {"id": user.id}
        )
        session.commit()
    finally:
        session.close()
    _custom_role(db_session, user, ["read", "update"])
    return _token(client, user, experiment_id)


# --- the session's answer -----------------------------------------------------------


def _assert_as_the_dashboard_shows(
    shown: Dict[str, Any], detail: Dict[str, Any], group_name: Optional[str]
) -> None:
    """Each new field of the session's ``experiment`` equals the experiment's
    own answer's for the same row, field by field."""
    assert shown["targeting_rules"] == detail["targeting_rules"], "targeting_rules"
    split = {v["id"]: v["traffic_allocation"] for v in detail["variants"]}
    for variant in shown["variants"]:
        assert "traffic_allocation" in variant, f"{variant['name']}: no share"
        assert variant["traffic_allocation"] == split[variant["id"]], variant["name"]
    assert len(shown["metrics"]) == len(detail["metrics"]), "metric count"
    for index, want in enumerate(detail["metrics"]):
        got = shown["metrics"][index]
        # lower_is_better first: it decides which variant wins (#1377).
        for field in ("lower_is_better", *want):
            assert field in got, f"metrics[{index}].{field} missing"
            assert got[field] == want[field], (
                f"metrics[{index}].{field}: {got[field]!r} != {want[field]!r}"
            )
        assert set(got) == set(want), (
            f"metrics[{index}]: {sorted(set(got) - set(want))}"
        )
    assert shown["goals"] == detail["visual_config"]["goals"], "goals"
    group_id = detail["mutual_exclusion_group_id"]
    expected = None if group_id is None else {"id": group_id, "name": group_name}
    assert shown["mutual_exclusion_group"] == expected, (
        f"mutual_exclusion_group: {shown['mutual_exclusion_group']} != {expected}"
    )
    assert shown["capabilities"] == {"balance_within": False, "country": False}
    assert shown["design_etag"] == detail["design_etag"], "design_etag"


@pytest.mark.regression
def test_the_session_answers_the_drafts_audience_split_metrics_and_goals(
    client, developer
):
    segment = _segment(client, developer, rules=COUNTRY_RULES)
    _segment(client, developer, rules=COUNTRY_RULES)  # another real segment
    experiment = _draft(client, developer, targeting_rules=_in_segment(segment["id"]))
    group = _group(client, developer)
    _join(client, developer, _group(client, developer)["id"], _draft(client, developer))
    _join(client, developer, group["id"], experiment)

    opened = _exchanged(client, developer, experiment)
    detail = _detail(client, developer, experiment)
    assert detail["targeting_rules"] == _in_segment(segment["id"])
    assert [m["lower_is_better"] for m in detail["metrics"]] == [True, True]
    _assert_as_the_dashboard_shows(opened.json()["experiment"], detail, group["name"])

    token = opened.json()["token"]
    current = _get(client, CURRENT, token)
    assert current.status_code == 200, current.text
    _assert_as_the_dashboard_shows(current.json()["experiment"], detail, group["name"])

    variant = next(v["id"] for v in detail["variants"] if v["name"] == "B")
    saved = client.put(
        f"{CURRENT}/variants/{variant}/changes",
        json=NEW_CHANGES,
        headers=_bearer(token, DASHBOARD),
    )
    assert saved.status_code == 200, saved.text
    after = _detail(client, developer, experiment)
    assert after["design_etag"] != detail["design_etag"]
    _assert_as_the_dashboard_shows(saved.json()["experiment"], after, group["name"])


def test_a_draft_with_no_audience_group_or_goals_answers_them_empty(client, developer):
    config = {**CONFIG, "goals": []}
    metrics = [{"name": "Buy", "event_name": "click_buy"}]
    experiment = _draft(client, developer, visual_config=config, metrics=metrics)

    shown = _exchanged(client, developer, experiment).json()["experiment"]

    detail = _detail(client, developer, experiment)
    assert shown["targeting_rules"] is None
    assert shown["goals"] == []
    assert shown["mutual_exclusion_group"] is None
    _assert_as_the_dashboard_shows(shown, detail, None)


@pytest.fixture
def holdout_slot(db_session):
    """Park any active holdout for the test, and delete the ones it creates.
    Parked with a plain UPDATE, so nothing stamps them as ended."""
    parked = [
        row.id
        for row in db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active)
    ]
    db_session.execute(
        update(GlobalHoldout)
        .where(GlobalHoldout.id.in_(parked))
        .values(is_active=False)
    )
    db_session.commit()
    made: List[uuid.UUID] = []

    def activate(percentage: int) -> None:
        holdout = GlobalHoldout(
            name=_name("holdout"), holdout_percentage=percentage, is_active=True
        )
        db_session.add(holdout)
        db_session.commit()
        made.append(holdout.id)

    yield activate
    db_session.rollback()
    db_session.query(GlobalHoldout).filter(GlobalHoldout.id.in_(made)).delete(
        synchronize_session=False
    )
    db_session.execute(
        update(GlobalHoldout).where(GlobalHoldout.id.in_(parked)).values(is_active=True)
    )
    db_session.commit()


@pytest.mark.regression
def test_the_holdout_is_shown_to_a_user_who_may_create_experiments(
    client, developer, experiment, holdout_slot
):
    token = _token(client, developer, experiment)
    assert _get(client, CURRENT, token).json()["experiment"]["holdout"] is None, (
        "no active holdout"
    )

    holdout_slot(7)

    shown = _get(client, CURRENT, token).json()["experiment"]["holdout"]
    assert shown == {"percentage": 7}, f"developer: {shown} != {{'percentage': 7}}"
    opened = _exchanged(client, developer, experiment).json()["experiment"]
    assert opened["holdout"] == {"percentage": 7}


@pytest.mark.modules
@pytest.mark.regression
def test_the_holdout_is_not_shown_to_a_user_who_may_only_change_experiments(
    client, db_session, developer, experiment, holdout_slot
):
    holdout_slot(7)
    token = _changing_only(db_session, client, developer, experiment)

    current = _get(client, CURRENT, token)

    assert current.status_code == 200, current.text
    shown = current.json()["experiment"]["holdout"]
    assert shown is None, f"update-only role: {shown} != None"
    # The pair: the developer's session on the same draft sees it.
    other = _get(client, CURRENT, _token(client, developer, experiment))
    assert other.json()["experiment"]["holdout"] == {"percentage": 7}


# --- the segments read --------------------------------------------------------------


def _store_rules(db_session, segment_id: str, rules: Any) -> None:
    """Rules written to the row directly: the segment routes refuse them."""
    session = _session(db_session)
    try:
        session.execute(
            text("UPDATE segments SET rules = CAST(:r AS json) WHERE id = :id"),
            {"r": json.dumps(rules), "id": uuid.UUID(segment_id)},
        )
        session.commit()
    finally:
        session.close()


def _statuses(db_session) -> Dict[str, str]:
    session = _session(db_session)
    try:
        return {
            str(row.id): row.status
            for row in session.execute(text("SELECT id, status FROM segments"))
        }
    finally:
        session.close()


@pytest.mark.regression
def test_the_segments_read_lists_active_segments_and_whether_each_can_be_used(
    client, db_session, developer, experiment
):
    good = _segment(client, developer, rules=COUNTRY_RULES)
    bad = _segment(client, developer, rules=COUNTRY_RULES)
    _store_rules(db_session, bad["id"], {"operator": "AND", "conditions": []})
    listed = _segment(client, developer, kind="id_list")
    archived = _segment(client, developer, rules=COUNTRY_RULES)
    gone = client.delete(f"{SEGMENTS_API}/{archived['id']}", headers=auth(developer))
    assert gone.status_code in (200, 204), gone.text
    token = _token(client, developer, experiment)

    response = _get(client, EDITOR_SEGMENTS, token)

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    items = response.json()
    for item in items:
        extra = sorted(set(item) - {"id", "name", "kind", "rules_valid"})
        assert not extra, f"unexpected key {extra[0]}"
        assert set(item) == {"id", "name", "kind", "rules_valid"}
    by_id = {item["id"]: item for item in items}
    assert archived["id"] not in by_id, f"archived segment {archived['name']} listed"
    statuses = _statuses(db_session)
    inactive = [i["name"] for i in items if statuses[i["id"]].lower() != "active"]
    assert inactive == [], f"listed but not active: {inactive}"
    assert by_id[good["id"]] == {
        "id": good["id"],
        "name": good["name"],
        "kind": "rules",
        "rules_valid": True,
    }
    assert by_id[bad["id"]]["rules_valid"] is False, (
        f"bad rules: {by_id[bad['id']]['rules_valid']} != False"
    )
    assert by_id[listed["id"]] == {
        "id": listed["id"],
        "name": listed["name"],
        "kind": "id_list",
        "rules_valid": True,
    }


@pytest.mark.modules
@pytest.mark.regression
def test_the_segments_read_needs_experiment_list(
    client, db_session, developer, experiment
):
    token = _changing_only(db_session, client, developer, experiment)

    refused = _get(client, EDITOR_SEGMENTS, token)

    assert refused.status_code == 403, f"no list: {refused.status_code} != 403"
    assert refused.json()["detail"] == (
        "Not enough permissions to perform this action on segments"
    )
    # The pair: the same draft, a developer's session.
    assert (
        _get(client, EDITOR_SEGMENTS, _token(client, developer, experiment)).status_code
        == 200
    )


# --- the exclusion-groups read --------------------------------------------------------


@pytest.mark.regression
def test_the_exclusion_groups_read_lists_active_groups(
    client, db_session, developer, experiment
):
    active = _group(client, developer)
    archived = _group(client, developer)
    admin = _user(db_session, UserRole.ADMIN)
    gone = client.delete(f"{GROUPS_API}/{archived['id']}", headers=auth(admin))
    assert gone.status_code in (200, 204), gone.text
    token = _token(client, developer, experiment)

    response = _get(client, EDITOR_GROUPS, token)

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    items = response.json()
    for item in items:
        assert set(item) == {"id", "name"}, f"keys {sorted(item)}"
    ids = {item["id"] for item in items}
    assert archived["id"] not in ids, f"archived group {archived['name']} listed"
    assert {"id": active["id"], "name": active["name"]} in items
    session = _session(db_session)
    try:
        statuses = {
            str(row.id): row.status
            for row in session.execute(
                text("SELECT id, status FROM mutual_exclusion_groups")
            )
        }
    finally:
        session.close()
    inactive = [i["name"] for i in items if statuses[i["id"]].lower() != "active"]
    assert inactive == [], f"listed but not active: {inactive}"


@pytest.mark.modules
@pytest.mark.regression
def test_the_exclusion_groups_read_needs_experiment_create(
    client, db_session, developer, experiment
):
    token = _changing_only(db_session, client, developer, experiment)

    refused = _get(client, EDITOR_GROUPS, token)

    assert refused.status_code == 403, f"no create: {refused.status_code} != 403"
    assert refused.json()["detail"] == (
        "Not enough permissions to manage mutual exclusion groups"
    )
    assert (
        _get(client, EDITOR_GROUPS, _token(client, developer, experiment)).status_code
        == 200
    )


# --- the Origin rule on the reads -----------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("wrong", ["none", *WRONG_ORIGINS])
def test_the_reads_take_the_dashboards_origin(client, developer, experiment, wrong):
    origin = WRONG_ORIGINS.get(wrong)
    token = _token(client, developer, experiment)

    for path in (EDITOR_SEGMENTS, EDITOR_GROUPS):
        answer = _get(client, path, token, origin)
        if wrong == "none":
            assert answer.status_code == 200, f"{path} no Origin: {answer.status_code}"
        else:
            assert answer.status_code == 403, (
                f"{path} from {origin}: {answer.status_code} != 403"
            )
            assert answer.json()["detail"] == visual_origins.NOT_FROM_DASHBOARD
        # The pair: from each dashboard origin, the same read answers.
        for dashboard in (DASHBOARD, DASHBOARD2):
            paired = _get(client, path, token, dashboard)
            assert paired.status_code == 200, f"{path} from {dashboard}"


@pytest.mark.regression
def test_the_reads_refuse_a_session_that_cannot_be_used(client, developer, experiment):
    token = _token(client, developer, experiment)
    signed_out = client.delete(CURRENT, headers=_bearer(token, DASHBOARD))
    assert signed_out.status_code == 204

    for path in (EDITOR_SEGMENTS, EDITOR_GROUPS):
        refused = _get(client, path, token, DASHBOARD)
        assert refused.status_code == 401, f"{path}: {refused.status_code} != 401"
        assert refused.json() == {
            "detail": visual_editor.CLOSED_TEXTS["signed_out"],
            "reason": "signed_out",
        }
        made_up = _get(client, path, "cves_" + _value(), DASHBOARD)
        assert made_up.status_code == 401
        assert made_up.json()["reason"] == "not_recognised"
