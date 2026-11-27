"""What the visual editor session reads and saves of a draft's design
(#1545): its audience, split, metrics, goals and exclusion group, and the
segments and exclusion groups it may choose from, on Postgres through the
real routes.

Users of each role sign in with local tokens. TestClient sends no ``Origin``:
every request here sets the one it means, and every "wrong Origin" refusal is
paired with the same call from a dashboard origin.

The experiment's metrics come back in no fixed order, so every comparison
here pairs them by ``(event_name, name)``, never by position.

The rows of PLAN-v3.1's VERIFICATION held here, each with the defect that
fails it (the failing line is the assertion's message or comparison):

* Answer  the session's answer -- the exchange's, GET current's and the
          changes PUT's -- carries the draft's audience, each variant's
          share, every field of every metric, the goals and the exclusion
          group, each equal to the experiment's own answer for the same
          row. A metric model of four fields fails
          ``metrics[0].lower_is_better missing``; the group read from another
          row fails ``mutual_exclusion_group``.
* Holdout the global holdout goes to a user who may create experiments,
          never to one who may only change them, and is null when none is
          active. Shown to everyone: ``update-only role: {...} != None``.
* Reads   the segments read lists active segments only, as
          ``{id, name, kind, rules_valid}``, with ``rules_valid`` false for
          stored rules the segment check refuses, and needs experiment list;
          the exclusion-groups read lists active groups only, as
          ``{id, name}``, and needs experiment create.
* V82     each read: no Origin and each dashboard origin answer 200, each
          wrong origin 403; the save: no Origin and each wrong origin 403.

The save, ``PATCH .../sessions/current/experiment``:

* Split   the split names each variant once (a missing, extra or repeated
          id is refused); everything else about a variant, the page changes
          above all, is kept, and a preview link still opens. Built from
          the request alone: ``variant A changes lost``.
* Kept    a part not sent, the balance and the analysis settings included,
          stays as stored; a goals save keeps the pages
          (``pages lost``); a save of the split keeps every metric field
          (``lower_is_better False != True`` for a four-field metric model).
* V68     a save sent with an older version is refused with its own text and
          writes nothing; Start takes the version the save made, not the one
          before; a save without one is refused.
* V65     a dated draft is refused; ``start_date`` is refused (422).
* V60     ``balance_within`` is refused (422).
* V45c    an audience naming an archived segment is refused with the
          experiment update's own detail.
* Group   join, move and leave, each with the exclusion group routes' entry
          and the session's reason; an unknown or archived group is refused
          and nothing is saved; changing it needs experiment create.
* V30     one "Experiment changed" entry per save, naming the parts that
          changed, by the session's user with the session's reason; a save
          with only the version writes nothing.
* V26     ADMIN and DEVELOPER sessions save; a user who may no longer change
          the draft is refused.
* V93     the save and the reads store no IP address.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import hash_api_key
from backend.app.db import request_sessions
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.compliance_audit_event import ComplianceAuditEvent
from backend.app.models.experiment import Experiment
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.user import UserRole
from backend.app.models.visual_token import VisualToken
from backend.app.services import visual, visual_editor, visual_origins
from backend.tests.integration.api.route_helpers import (
    api_key,
    auth,
    client_address_count,
    make_user,
)

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
#: Every metric field set away from its default, except Thanks' conversion
#: window.
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
        "conversion_window_hours": 168,
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
        "conversion_window_hours": None,
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


def _metric_key(metric: Dict[str, Any]) -> Tuple[str, str]:
    return (metric["event_name"], metric["name"])


def _by_metric(metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """*metrics* in ``(event_name, name)`` order, the order ``design_etag``
    sorts them in. The experiment's metrics have no stored order: a read
    lists them in whatever order its query plan reads the rows."""
    return sorted(metrics, key=_metric_key)


def _paired(
    got: List[Dict[str, Any]], want: List[Dict[str, Any]]
) -> List[Tuple[int, Dict[str, Any], Dict[str, Any]]]:
    """Each metric of *want* with the metric of *got* of the same
    ``(event_name, name)``, both in that order."""
    got, want = _by_metric(got), _by_metric(want)
    keys = [_metric_key(m) for m in got], [_metric_key(m) for m in want]
    assert keys[0] == keys[1], f"metrics: {keys[0]} != {keys[1]}"
    return [(index, g, w) for index, (g, w) in enumerate(zip(got, want))]


def _detail(client, user, experiment_id: str) -> Dict[str, Any]:
    """The experiment's own answer, its metrics in ``(event_name, name)``
    order, so that two answers compare whatever order each read them in."""
    response = client.get(f"{EXPERIMENTS}/{experiment_id}", headers=auth(user))
    assert response.status_code == 200, response.text
    detail = response.json()
    return {**detail, "metrics": _by_metric(detail["metrics"])}


def _answer(response) -> Dict[str, Any]:
    """A session answer's body, its metrics in ``(event_name, name)`` order."""
    body = response.json()
    shown = body["experiment"]
    return {**body, "experiment": {**shown, "metrics": _by_metric(shown["metrics"])}}


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
    """The token of :func:`_opened_changing_only`'s session."""
    opened = _opened_changing_only(db_session, client, developer, experiment_id)
    return opened.json()["token"]


def _opened_changing_only(db_session, client, developer, experiment_id: str):
    """The exchange's answer for a user with no built-in role (every built-in
    role may list experiments) and a custom role granting experiment read and
    update only: it may change the draft, and neither list nor create
    experiments."""
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
    return _exchanged(client, user, experiment_id)


# --- the session's answer -----------------------------------------------------------


def _assert_as_the_dashboard_shows(
    shown: Dict[str, Any], detail: Dict[str, Any], group_name: Optional[str]
) -> None:
    """Each new field of the session's ``experiment`` equals the experiment's
    own answer's for the same row, field by field; metrics are paired by
    ``(event_name, name)``."""
    assert shown["targeting_rules"] == detail["targeting_rules"], "targeting_rules"
    split = {v["id"]: v["traffic_allocation"] for v in detail["variants"]}
    for variant in shown["variants"]:
        assert "traffic_allocation" in variant, f"{variant['name']}: no share"
        assert variant["traffic_allocation"] == split[variant["id"]], variant["name"]
    assert len(shown["metrics"]) == len(detail["metrics"]), "metric count"
    for index, got, want in _paired(shown["metrics"], detail["metrics"]):
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
    opened = _opened_changing_only(db_session, client, developer, experiment)
    token = opened.json()["token"]

    current = _get(client, CURRENT, token)

    exchanged = opened.json()["experiment"]["holdout"]
    assert exchanged is None, f"exchange 201 update-only: {exchanged} != None"
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


# --- the save -----------------------------------------------------------------------

DESIGN = f"{CURRENT}/experiment"


def _patch(client, token: str, body: Dict[str, Any], origin: Optional[str] = DASHBOARD):
    return client.patch(DESIGN, json=body, headers=_bearer(token, origin))


def _shown(client, token: str) -> Dict[str, Any]:
    """GET current's ``experiment``."""
    response = _get(client, CURRENT, token)
    assert response.status_code == 200, response.text
    return response.json()["experiment"]


def _split(shown: Dict[str, Any], **shares: int) -> List[Dict[str, Any]]:
    """``variants`` for a save: each variant of *shown*, by name, its share."""
    return [
        {"id": v["id"], "traffic_allocation": shares[v["name"]]}
        for v in shown["variants"]
    ]


def _session_id(db_session, token: str) -> uuid.UUID:
    session = _session(db_session)
    try:
        return (
            session.query(VisualToken.id)
            .filter(VisualToken.token_hash == hash_api_key(token))
            .scalar()
        )
    finally:
        session.close()


def _entries_of(db_session, entity_id: str, action: str) -> List[AuditLog]:
    session = _session(db_session)
    try:
        return (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_id == uuid.UUID(entity_id),
                AuditLog.action_type == action,
            )
            .order_by(AuditLog.timestamp)
            .all()
        )
    finally:
        session.close()


def _changed_fields(entry: AuditLog) -> List[str]:
    return (json.loads(entry.new_value) if entry.new_value else {}).get(
        "changed_fields", []
    )


@pytest.mark.regression
def test_a_save_of_the_split_keeps_everything_else_about_each_variant(
    client, developer
):
    a_changes = {"version": 1, "changes": [{"op": "hide", "selector": ".banner"}]}
    experiment = _draft(
        client,
        developer,
        variants=[
            {
                "name": "Control",
                "is_control": True,
                "traffic_allocation": 50,
                "configuration": {"button": "grey"},
            },
            {
                "name": "A",
                "description": "Banner hidden",
                "is_control": False,
                "traffic_allocation": 25,
                "configuration": {"button": "blue"},
                "visual_changes": a_changes,
            },
            {
                "name": "B",
                "is_control": False,
                "traffic_allocation": 25,
                "visual_changes": CHANGES,
            },
        ],
        balance_within={"attribute": "tier", "values": ["high_value", "new"]},
        confidence_level=0.9,
        correction_method="bonferroni",
    )
    before = _detail(client, developer, experiment)
    link = client.post(
        f"{EXPERIMENTS}/{experiment}/visual/preview-links",
        json={
            "variant_id": next(v["id"] for v in before["variants"] if v["name"] == "B")
        },
        headers=auth(developer),
    )
    assert link.status_code == 201, link.text
    preview = link.json()["url"].partition("#controlvar-preview=")[2]
    key = api_key(client, developer)
    token = _token(client, developer, experiment)
    shown = _shown(client, token)

    saved = _patch(
        client,
        token,
        {
            "design_etag": shown["design_etag"],
            "variants": _split(shown, Control=10, A=30, B=60),
        },
    )

    assert saved.status_code == 200, saved.text
    assert saved.headers["Cache-Control"] == "no-store"
    after = _detail(client, developer, experiment)
    stored = {v["name"]: v for v in after["variants"]}
    was = {v["name"]: v for v in before["variants"]}
    assert {n: v["traffic_allocation"] for n, v in stored.items()} == {
        "Control": 10,
        "A": 30,
        "B": 60,
    }
    for name in ("Control", "A", "B"):
        for field in ("description", "is_control", "configuration", "visual_changes"):
            assert stored[name][field] == was[name][field], (
                f"variant {name} {field.replace('visual_changes', 'changes')} lost"
            )
    for field in (
        "balance_within",
        "confidence_level",
        "correction_method",
        "targeting_rules",
        "metrics",
        "visual_config",
        "mutual_exclusion_group_id",
    ):
        if field == "metrics":
            strip = ("id", "created_at", "updated_at")
            got = [{k: v for k, v in m.items() if k not in strip} for m in after[field]]
            want = [
                {k: v for k, v in m.items() if k not in strip} for m in before[field]
            ]
            assert got == want, "metrics changed"
        else:
            assert after[field] == before[field], f"{field} changed"
    assert after["design_etag"] != before["design_etag"]
    assert _answer(saved) == _answer(_get(client, CURRENT, token))
    assert saved.json()["experiment"]["design_etag"] == after["design_etag"]
    read = client.post(
        "/api/v1/tracking/visual/preview",
        json={"token": preview, "host": "shop.example.com", "path": "/"},
        headers=key,
    )
    assert read.status_code == 200, read.text
    assert read.json()["changes"] == CHANGES


@pytest.mark.regression
@pytest.mark.parametrize("wrong", ["missing", "another experiment's", "repeated"])
def test_a_split_that_does_not_name_each_variant_once_is_refused(
    client, developer, experiment, wrong
):
    token = _token(client, developer, experiment)
    shown = _shown(client, token)
    split = _split(shown, Control=50, B=50)
    if wrong == "missing":
        split = [{**split[0], "traffic_allocation": 100}]
    elif wrong == "another experiment's":
        other = _shown(client, _token(client, developer, _draft(client, developer)))
        theirs = next(v["id"] for v in other["variants"] if v["name"] == "B")
        split = [split[0], {"id": theirs, "traffic_allocation": 50}]
    else:
        split = [split[0], split[0]]
    before = _detail(client, developer, experiment)

    refused = _patch(
        client, token, {"design_etag": shown["design_etag"], "variants": split}
    )

    assert refused.status_code == 422, f"{wrong}: {refused.status_code} != 422"
    assert refused.json()["detail"] == visual_editor.SPLIT_NOT_EACH_VARIANT
    assert _detail(client, developer, experiment) == before


def test_a_split_that_does_not_add_up_is_refused_as_an_update_refuses_it(
    client, developer, experiment
):
    token = _token(client, developer, experiment)
    shown = _shown(client, token)
    before = _detail(client, developer, experiment)

    refused = _patch(
        client,
        token,
        {
            "design_etag": shown["design_etag"],
            "variants": _split(shown, Control=50, B=40),
        },
    )

    assert refused.status_code == 422, refused.text
    (error,) = refused.json()["detail"]
    assert error["loc"] == ["body"]
    assert "Traffic allocations must sum to 100% (currently 90%)" in error["msg"]
    too_much = _patch(
        client,
        token,
        {
            "design_etag": shown["design_etag"],
            "variants": _split(shown, Control=-1, B=101),
        },
    )
    assert too_much.status_code == 422, too_much.text
    assert _detail(client, developer, experiment) == before


@pytest.mark.regression
def test_a_save_keeps_every_metric_field_it_did_not_send(client, developer, experiment):
    token = _token(client, developer, experiment)
    shown = _shown(client, token)
    strip = ("id", "experiment_id", "created_at", "updated_at")

    def metrics() -> List[Dict[str, Any]]:
        detail = _detail(client, developer, experiment)
        return [
            {k: v for k, v in m.items() if k not in strip} for m in detail["metrics"]
        ]

    assert metrics() == _by_metric(METRICS)
    split = _patch(
        client,
        token,
        {
            "design_etag": shown["design_etag"],
            "variants": _split(shown, Control=20, B=80),
        },
    )
    assert split.status_code == 200, split.text
    for index, got, want in _paired(metrics(), METRICS):
        for field in ("lower_is_better", *want):
            assert got[field] == want[field], (
                f"metrics[{index}].{field}: {got[field]!r} != {want[field]!r}"
            )

    sent = [
        {
            **METRICS[0],
            "description": "Bought",
            "aggregation_method": "average",
            "minimum_sample_size": 900,
            "expected_effect": 0.2,
            "event_value_path": "total",
            "lower_is_better": True,
        },
        {**METRICS[1], "lower_is_better": True, "is_primary": False},
    ]
    saved = _patch(
        client,
        token,
        {"design_etag": split.json()["experiment"]["design_etag"], "metrics": sent},
    )
    assert saved.status_code == 200, saved.text
    for index, got, want in _paired(metrics(), sent):
        for field in ("lower_is_better", *want):
            assert got[field] == want[field], (
                f"lower_is_better {got[field]} != {want[field]}"
                if field == "lower_is_better"
                else f"metrics[{index}].{field}: {got[field]!r} != {want[field]!r}"
            )


TWO_PAGES = {
    **CONFIG,
    "pages": [PAGE, {"match": "exact", "url": f"{SHOP}/checkout"}],
}


@pytest.mark.regression
def test_a_goals_save_keeps_the_pages_and_the_editor_page(client, developer):
    experiment = _draft(client, developer, visual_config=TWO_PAGES)
    token = _token(client, developer, experiment)
    shown = _shown(client, token)
    goals = [{"event_name": "page_thanks", "kind": "page_view", "page": PAGE}]

    saved = _patch(client, token, {"design_etag": shown["design_etag"], "goals": goals})

    assert saved.status_code == 200, saved.text
    config = _detail(client, developer, experiment)["visual_config"]
    assert config["pages"] == TWO_PAGES["pages"], (
        f"pages lost: {config['pages']} != {TWO_PAGES['pages']}"
    )
    assert config["editor_url"] == TWO_PAGES["editor_url"], "editor page lost"
    assert config["goals"] == goals
    assert saved.json()["experiment"]["goals"] == goals

    before = _detail(client, developer, experiment)
    no_metric = [{"event_name": "click_nothing", "kind": "page_view", "page": PAGE}]
    refused = _patch(
        client,
        token,
        {"design_etag": saved.json()["experiment"]["design_etag"], "goals": no_metric},
    )
    assert refused.status_code == 422, refused.text
    goal_text = (
        "A goal's event_name must be the event_name of one of the experiment's metrics."
    )
    (error,) = refused.json()["detail"]
    assert error["msg"].endswith(goal_text), refused.json()
    # The shared write's place for it: a key the save's body does not have.
    assert error["loc"] == ["body", "visual_config", "goals", 0, "event_name"]
    assert _detail(client, developer, experiment) == before

    # A metrics save that leaves a stored goal without its metric.
    buy_only = [m for m in METRICS if m["event_name"] == "click_buy"]
    orphaned = _patch(
        client,
        token,
        {"design_etag": saved.json()["experiment"]["design_etag"], "metrics": buy_only},
    )
    assert orphaned.status_code == 422, orphaned.text
    (error,) = orphaned.json()["detail"]
    assert error["loc"] == ["body", "metrics"], orphaned.json()
    assert goal_text in error["msg"], orphaned.json()
    assert _detail(client, developer, experiment) == before


@pytest.mark.regression
def test_a_save_made_from_an_older_version_is_refused(client, developer, experiment):
    token = _token(client, developer, experiment)
    e1 = _detail(client, developer, experiment)["design_etag"]
    assert _shown(client, token)["design_etag"] == e1

    first = _patch(client, token, {"design_etag": e1, "targeting_rules": COUNTRY_RULES})
    assert first.status_code == 200, first.text
    e2 = first.json()["experiment"]["design_etag"]
    assert e2 != e1 and _detail(client, developer, experiment)["design_etag"] == e2

    stale = _patch(client, token, {"design_etag": e1, "targeting_rules": None})
    assert stale.status_code == 409, f"stale PATCH: {stale.status_code} != 409"
    assert stale.json()["detail"] == (
        "This experiment's design changed in Controlvar after the editor loaded "
        "it, so this step was not saved. Reload the editor to see the change."
    )
    assert stale.json()["detail"] == visual_editor.DESIGN_CHANGED
    assert _detail(client, developer, experiment)["targeting_rules"] == COUNTRY_RULES
    no_version = _patch(client, token, {"targeting_rules": None})
    assert no_version.status_code == 422, f"no etag: {no_version.status_code} != 422"
    assert _detail(client, developer, experiment)["targeting_rules"] == COUNTRY_RULES

    start = f"{EXPERIMENTS}/{experiment}/start"
    old = client.post(start, json={"design_etag": e1}, headers=auth(developer))
    assert old.status_code == 409, f"start e1: {old.status_code} != 409"
    assert old.json()["detail"] == visual.DESIGN_ETAG_STALE
    new = client.post(start, json={"design_etag": e2}, headers=auth(developer))
    assert new.status_code == 200, f"start e2: {new.status_code} != 200 {new.text}"


def _date_the_draft(db_session, experiment_id: str) -> None:
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


@pytest.mark.regression
def test_a_save_on_a_dated_draft_or_carrying_a_start_date_is_refused(
    client, db_session, developer, experiment
):
    token = _token(client, developer, experiment)
    etag = _shown(client, token)["design_etag"]
    before = _detail(client, developer, experiment)

    carrying = _patch(
        client, token, {"design_etag": etag, "start_date": "2031-01-01T00:00:00Z"}
    )

    assert carrying.status_code == 422, f"start_date: {carrying.status_code} != 422"
    assert carrying.json()["detail"][0]["loc"] == ["body", "start_date"]
    assert _detail(client, developer, experiment) == before
    _date_the_draft(db_session, experiment)
    dated = _patch(
        client, token, {"design_etag": etag, "targeting_rules": COUNTRY_RULES}
    )
    assert dated.status_code == 400, f"PATCH after schedule: {dated.status_code} != 400"
    assert dated.json()["detail"] == (
        "A visual draft with a start date cannot be opened in the editor: it starts "
        "on its own with the design it has then. Remove the start date to edit it."
    )
    assert _detail(client, developer, experiment)["targeting_rules"] is None


@pytest.mark.regression
def test_the_save_does_not_take_the_balance(client, developer, experiment):
    token = _token(client, developer, experiment)
    etag = _shown(client, token)["design_etag"]
    before = _detail(client, developer, experiment)

    refused = _patch(
        client,
        token,
        {
            "design_etag": etag,
            "balance_within": {"attribute": "tier", "values": ["high_value", "new"]},
        },
    )

    assert refused.status_code == 422, f"balance_within: {refused.status_code} != 422"
    assert refused.json()["detail"][0]["loc"] == ["body", "balance_within"]
    assert _detail(client, developer, experiment) == before


@pytest.mark.regression
def test_an_audience_naming_an_archived_segment_is_refused(
    client, developer, experiment
):
    segment = _segment(client, developer, rules=COUNTRY_RULES)
    gone = client.delete(f"{SEGMENTS_API}/{segment['id']}", headers=auth(developer))
    assert gone.status_code in (200, 204), gone.text
    token = _token(client, developer, experiment)
    before = _detail(client, developer, experiment)

    refused = _patch(
        client,
        token,
        {
            "design_etag": _shown(client, token)["design_etag"],
            "targeting_rules": _in_segment(segment["id"]),
        },
    )

    assert refused.status_code == 422, (
        f"archived segment saved: {refused.status_code} != 422"
    )
    (error,) = refused.json()["detail"]
    assert error["loc"] == ["body", "targeting_rules"]
    dashboard = client.put(
        f"{EXPERIMENTS}/{experiment}",
        json={"targeting_rules": _in_segment(segment["id"])},
        headers=auth(developer),
    )
    assert dashboard.status_code == 422
    assert refused.json()["detail"] == dashboard.json()["detail"]
    assert _detail(client, developer, experiment) == before


def _group_entries(db_session, group_id: str) -> List[Dict[str, Any]]:
    return [
        {
            "before": json.loads(e.old_value) if e.old_value else None,
            "after": json.loads(e.new_value) if e.new_value else None,
            "reason": e.reason,
            "user_id": e.user_id,
        }
        for e in _entries_of(db_session, group_id, "mutual_exclusion_group_update")
    ]


@pytest.mark.regression
def test_a_save_moves_the_draft_between_exclusion_groups(
    client, db_session, developer, experiment
):
    first, second = _group(client, developer), _group(client, developer)
    colleague = _user(db_session, UserRole.DEVELOPER)
    token = _token(client, colleague, experiment)
    reason = f"visual editor session {_session_id(db_session, token).hex[:8]}"
    entry = {"experiment_id": experiment}
    etags = [_shown(client, token)["design_etag"]]

    for wanted in (first, second, None):
        group_id = None if wanted is None else wanted["id"]
        saved = _patch(
            client,
            token,
            {"design_etag": etags[-1], "mutual_exclusion_group_id": group_id},
        )
        assert saved.status_code == 200, saved.text
        shown = saved.json()["experiment"]
        expected = (
            None if wanted is None else {"id": wanted["id"], "name": wanted["name"]}
        )
        assert shown["mutual_exclusion_group"] == expected
        assert (
            _detail(client, developer, experiment)["mutual_exclusion_group_id"]
            == group_id
        )
        # Leaving the group brings back the first design, and its etag.
        assert shown["design_etag"] != etags[-1], "design_etag unchanged by the group"
        etags.append(shown["design_etag"])
    assert etags[-1] == etags[0]
    assert len(set(etags)) == 3

    assert _group_entries(db_session, first["id"]) == [
        {"before": None, "after": entry, "reason": reason, "user_id": colleague.id},
        {"before": entry, "after": None, "reason": reason, "user_id": colleague.id},
    ]
    assert _group_entries(db_session, second["id"]) == [
        {"before": None, "after": entry, "reason": reason, "user_id": colleague.id},
        {"before": entry, "after": None, "reason": reason, "user_id": colleague.id},
    ]
    # The group is saved by the group service alone: no experiment change.
    assert _entries_of(db_session, experiment, "experiment_update") == []


@pytest.mark.regression
@pytest.mark.parametrize("which", ["unknown", "archived"])
def test_an_unknown_or_archived_group_is_refused_and_nothing_is_saved(
    client, db_session, developer, experiment, which
):
    if which == "unknown":
        group_id = str(uuid.uuid4())
    else:
        group_id = _group(client, developer)["id"]
        admin = _user(db_session, UserRole.ADMIN)
        gone = client.delete(f"{GROUPS_API}/{group_id}", headers=auth(admin))
        assert gone.status_code in (200, 204), gone.text
    token = _token(client, developer, experiment)
    before = _detail(client, developer, experiment)

    refused = _patch(
        client,
        token,
        {
            "design_etag": _shown(client, token)["design_etag"],
            "targeting_rules": COUNTRY_RULES,
            "mutual_exclusion_group_id": group_id,
        },
    )

    assert refused.status_code == 422, f"{which}: {refused.status_code} != 422"
    assert refused.json()["detail"] == visual_editor.GROUP_NOT_AVAILABLE
    assert _detail(client, developer, experiment) == before, "design part saved"
    assert _entries_of(db_session, experiment, "experiment_update") == []


@pytest.mark.modules
@pytest.mark.regression
def test_a_group_change_needs_experiment_create(
    client, db_session, developer, experiment
):
    group = _group(client, developer)
    token = _changing_only(db_session, client, developer, experiment)
    shown = _shown(client, token)
    before = _detail(client, developer, experiment)

    refused = _patch(
        client,
        token,
        {
            "design_etag": shown["design_etag"],
            "targeting_rules": COUNTRY_RULES,
            "mutual_exclusion_group_id": group["id"],
        },
    )

    assert refused.status_code == 403, f"update-only role: {refused.status_code} != 403"
    assert refused.json()["detail"] == (
        "Not enough permissions to manage mutual exclusion groups"
    )
    assert _detail(client, developer, experiment) == before
    assert _group_entries(db_session, group["id"]) == []
    # Without a group change the same user saves; with one, a developer does.
    split = _patch(
        client,
        token,
        {
            "design_etag": shown["design_etag"],
            "variants": _split(shown, Control=30, B=70),
        },
    )
    assert split.status_code == 200, split.text
    developer_token = _token(client, developer, experiment)
    joined = _patch(
        client,
        developer_token,
        {
            "design_etag": _shown(client, developer_token)["design_etag"],
            "mutual_exclusion_group_id": group["id"],
        },
    )
    assert joined.status_code == 200, joined.text


@pytest.mark.regression
def test_each_save_is_one_experiment_change_naming_what_changed(
    client, db_session, developer, experiment
):
    colleague = _user(db_session, UserRole.DEVELOPER)
    token = _token(client, colleague, experiment)
    reason = f"visual editor session {_session_id(db_session, token).hex[:8]}"
    shown = _shown(client, token)
    goals = [
        {"event_name": "click_buy", "kind": "click", "selector": "#go", "page": PAGE}
    ]
    saves = [
        ("split", {"variants": _split(shown, Control=30, B=70)}, ["variants"]),
        (
            "metrics",
            {"metrics": [{**METRICS[0], "minimum_sample_size": 700}, METRICS[1]]},
            ["metrics"],
        ),
        (
            "identical re-send",
            {"metrics": [{**METRICS[0], "minimum_sample_size": 700}, METRICS[1]]},
            [],
        ),
        ("goals", {"goals": goals}, ["visual_config"]),
        ("audience", {"targeting_rules": COUNTRY_RULES}, ["targeting_rules"]),
        (
            "two parts",
            {"targeting_rules": None, "goals": []},
            ["targeting_rules", "visual_config"],
        ),
    ]
    etag = shown["design_etag"]
    for label, body, fields in saves:
        before = len(_entries_of(db_session, experiment, "experiment_update"))
        saved = _patch(client, token, {"design_etag": etag, **body})
        assert saved.status_code == 200, f"{label}: {saved.text}"
        etag = saved.json()["experiment"]["design_etag"]
        found = _entries_of(db_session, experiment, "experiment_update")[before:]
        assert len(found) == 1, f"{label}: {len(found)} entries"
        (entry,) = found
        assert _changed_fields(entry) == fields, (
            f"{label}: {_changed_fields(entry)} != {fields}"
        )
        assert entry.reason == reason, f"{entry.reason!r} != {reason!r}"
        assert entry.user_id == colleague.id, f"{label}: actor {entry.user_id}"


@pytest.mark.regression
def test_a_save_with_only_its_version_saves_nothing(
    client, db_session, developer, experiment
):
    token = _token(client, developer, experiment)
    shown = _shown(client, token)
    before = _detail(client, developer, experiment)
    entries = len(_entries_of(db_session, experiment, "experiment_update"))

    saved = _patch(client, token, {"design_etag": shown["design_etag"]})

    assert saved.status_code == 200, saved.text
    assert _answer(saved) == _answer(_get(client, CURRENT, token))
    assert _detail(client, developer, experiment) == before
    assert len(_entries_of(db_session, experiment, "experiment_update")) == entries


@pytest.mark.regression
@pytest.mark.parametrize("role", ["ADMIN", "DEVELOPER"])
def test_admins_and_developers_save_through_the_session(
    client, db_session, developer, experiment, role
):
    user = _user(db_session, UserRole[role])
    token = _token(client, user, experiment)
    shown = _shown(client, token)

    saved = _patch(
        client,
        token,
        {
            "design_etag": shown["design_etag"],
            "variants": _split(shown, Control=45, B=55),
        },
    )

    assert saved.status_code == 200, f"{role}: {saved.status_code} != 200"


@pytest.mark.regression
@pytest.mark.parametrize("change", ["demoted", "deactivated"])
def test_a_save_by_a_user_who_may_no_longer_change_the_draft_is_refused(
    client, db_session, developer, experiment, change
):
    token = _token(client, developer, experiment)
    shown = _shown(client, token)
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
    owner = _user(db_session, UserRole.ADMIN)
    before = _detail(client, owner, experiment)

    refused = _patch(
        client,
        token,
        {"design_etag": shown["design_etag"], "targeting_rules": COUNTRY_RULES},
    )

    assert refused.status_code == 403, f"{change}: {refused.status_code} != 403"
    assert refused.json()["detail"] == visual_editor.CANNOT_CHANGE
    assert _detail(client, owner, experiment) == before


@pytest.mark.regression
@pytest.mark.parametrize("wrong", ["none", *WRONG_ORIGINS])
def test_the_save_takes_the_dashboards_origin(client, developer, experiment, wrong):
    origin = WRONG_ORIGINS.get(wrong)
    token = _token(client, developer, experiment)
    shown = _shown(client, token)
    body = {"design_etag": shown["design_etag"], "targeting_rules": COUNTRY_RULES}
    before = _detail(client, developer, experiment)

    refused = _patch(client, token, body, origin)

    label = "no Origin" if origin is None else origin
    assert refused.status_code == 403, f"PATCH {label}: {refused.status_code} != 403"
    assert refused.json()["detail"] == visual_origins.NOT_FROM_DASHBOARD
    assert _detail(client, developer, experiment) == before
    # The pair: from each dashboard origin, the same save answers.
    for dashboard in (DASHBOARD, DASHBOARD2):
        saved = _patch(client, token, body, dashboard)
        assert saved.status_code == 200, f"PATCH from {dashboard}: {saved.text}"
        body = {**body, "design_etag": saved.json()["experiment"]["design_etag"]}


@pytest.mark.regression
def test_no_ip_address_is_stored_by_the_save_or_the_reads(
    client, db_session, developer, experiment
):
    """TestClient's client address is ``testclient``. The save (a design part
    and a group change) and both reads run once; no text or JSON column gains
    the address, and no compliance event made since has an actor address."""
    address = "testclient"
    group = _group(client, developer)
    token = _token(client, developer, experiment)
    before = client_address_count(db_session, address)
    started = datetime.now(timezone.utc)

    shown = _shown(client, token)
    saved = _patch(
        client,
        token,
        {
            "design_etag": shown["design_etag"],
            "targeting_rules": COUNTRY_RULES,
            "mutual_exclusion_group_id": group["id"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert _get(client, EDITOR_SEGMENTS, token).status_code == 200
    assert _get(client, EDITOR_GROUPS, token).status_code == 200

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
