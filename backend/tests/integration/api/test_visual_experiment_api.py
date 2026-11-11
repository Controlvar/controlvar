"""A visual experiment through the experiment routes, on Postgres (#1545, PR 1).

Real routes, real local tokens, non-superusers of the right role; every
assertion that a refusal changed nothing reads the row back. The rows of
PLAN-v3.1's VERIFICATION held here, each with the defect that fails it:

* V3/V4  every write path validates: each hostile payload of the corpus is
         refused by create and by PUT, and a clone of a stored value that
         fails its schema is refused (a clone that copies without checking
         answers 201);
* V26    ADMIN and DEVELOPER may write a visual draft; ANALYST and VIEWER get 403;
* V30    a save that changes visual_config names it in changed_fields, and
         never stores its contents;
* D8     a variant's page changes are named as visual_changes, apart from
         variants (renaming the variant that has them names both);
* V54    (names) a goal's event name must be a metric's, on the result of a PUT;
* V67    a start revokes every editor ticket, session and claim of the
         experiment (reason started), and only those;
* V68    Start refuses a design the caller did not review: GET (e1), a PUT
         (e2), /start with e1 is 409, with none 400, with e2 200;
* V71a   PUT /schedule refuses a start date on a visual draft and reads the row;
* V71a2  create and PUT drop a start_date they are sent;
* V71c   a visual draft cannot stop being visual; a dated draft cannot become
         visual (K14, K16: the literal texts, with the real id);
* R1     a code experiment's free-form configuration is untouched;
* R3     a split URL experiment cannot be visual;
* R6     /start with no body, an empty body, null, {} or an unknown key starts
         an ordinary experiment;
* R11    the stable PUT still drops an unknown key with 200;
* U9     a clone copies the pages, the goals and the page changes;
* UC9    a visual experiment is created with no metric, and start refuses it
         with the metric text;
* S1     a variants PUT that leaves out visual_changes keeps them; null clears.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.user import User, UserRole
from backend.app.models.visual_token import VisualToken
from backend.app.schemas import visual as visual_schema
from backend.app.services import visual
from backend.scripts.generate_visual_vectors import REFUSED

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

BASE = "/api/v1/experiments"
PREFIX = "visual1545-"
PAGE = {"match": "prefix", "url": "https://shop.example.com/"}
CONFIG = {
    "version": 1,
    "editor_url": "https://shop.example.com/",
    "pages": [PAGE],
    "goals": [
        {"event_name": "click_buy", "kind": "click", "selector": "#buy", "page": PAGE}
    ],
}
CHANGES = {
    "version": 1,
    "changes": [{"op": "set_text", "selector": "#headline", "text": "Hello"}],
}
METRICS = [
    {"name": "Buy clicks", "event_name": "click_buy", "metric_type": "conversion"}
]


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _factory(db_session):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)

    def session():
        s = factory()
        s.execute(text("SET search_path TO test_experimentation"))
        return s

    return session


def _user(db_session, role, superuser=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"visual_{role.value}_{suffix}",
        email=f"visual_{suffix}@visual.test",
        full_name="Visual Test User",
        hashed_password="unused: signs in by token only",
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def developer(db_session):
    return _user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def client(db_session):
    session = _factory(db_session)

    def override_get_db():
        s = session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture(autouse=True)
def _remove_the_experiments_created(db_session):
    yield
    session = _factory(db_session)()
    try:
        session.execute(
            text("DELETE FROM experiments WHERE name LIKE :p"), {"p": f"{PREFIX}%"}
        )
        session.commit()
    finally:
        session.close()


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def _variants(changes=CHANGES):
    treatment = {"name": "B", "is_control": False, "traffic_allocation": 50}
    if changes is not None:
        treatment["visual_changes"] = changes
    return [
        {"name": "Control", "is_control": True, "traffic_allocation": 50},
        treatment,
    ]


def _body(**overrides):
    body = {
        "name": f"{PREFIX}{uuid.uuid4().hex[:8]}",
        "experiment_type": "a_b",
        "visual_config": CONFIG,
        "variants": _variants(),
        "metrics": METRICS,
    }
    body.update(overrides)
    return body


def _create(client, user, **overrides):
    return _ok(
        client.post(f"{BASE}/", json=_body(**overrides), headers=_auth(user)), 201
    )


def _row(db_session, experiment_id):
    session = _factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(str(experiment_id)))
        return {
            "status": row.status,
            "start_date": row.start_date,
            "resume_at": row.resume_at,
            "visual_config": row.visual_config,
            "changes": {v.name: v.visual_changes for v in row.variants},
        }
    finally:
        session.close()


def _write_row(db_session, experiment_id, **values):
    """Change a row the way no API path can (a value written outside the API)."""
    session = _factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(str(experiment_id)))
        for name, value in values.items():
            setattr(row, name, value)
        session.commit()
    finally:
        session.close()


def _write_changes(db_session, experiment_id, name, changes):
    session = _factory(db_session)()
    try:
        variant = (
            session.query(Variant)
            .filter(
                Variant.experiment_id == uuid.UUID(str(experiment_id)),
                Variant.name == name,
            )
            .one()
        )
        variant.visual_changes = changes
        session.commit()
    finally:
        session.close()


def _schedule(client, user, experiment_id, body):
    return client.put(
        f"{BASE}/{experiment_id}/schedule", json=body, headers=_auth(user)
    )


def _start(client, user, experiment_id, **kwargs):
    return client.post(f"{BASE}/{experiment_id}/start", headers=_auth(user), **kwargs)


def _soon(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


# --- the response ---------------------------------------------------------------


def test_a_visual_experiment_is_stored_and_answered_as_sent(client, developer):
    created = _create(client, developer)

    assert created["visual_config"] == CONFIG
    changes = {v["name"]: v["visual_changes"] for v in created["variants"]}
    assert changes == {"Control": None, "B": CHANGES}
    assert len(created["design_etag"]) == 64
    read = _ok(client.get(f"{BASE}/{created['id']}", headers=_auth(developer)))
    assert read["design_etag"] == created["design_etag"]
    assert read["visual_config"] == CONFIG


def test_an_ordinary_experiment_has_no_etag(client, developer):
    created = _create(client, developer, visual_config=None, variants=_variants(None))
    assert created["visual_config"] is None
    assert created["design_etag"] is None


# --- V3 / V4: every write path validates ------------------------------------------


@pytest.mark.parametrize("case_id,field,document", REFUSED, ids=[c[0] for c in REFUSED])
def test_create_refuses_each_hostile_payload(
    client, developer, case_id, field, document
):
    body = _body(variants=_variants(document))
    response = client.post(f"{BASE}/", json=body, headers=_auth(developer))
    assert response.status_code == 422, response.text
    locs = [error["loc"][:4] for error in response.json()["detail"]]
    assert ["body", "variants", 1, "visual_changes"] in locs, locs


def test_put_refuses_each_hostile_payload_and_keeps_the_stored_changes(
    client, db_session, developer
):
    created = _create(client, developer)
    refused = []
    for case_id, _, document in REFUSED:
        response = client.put(
            f"{BASE}/{created['id']}",
            json={"variants": _variants(document)},
            headers=_auth(developer),
        )
        if response.status_code != 422:
            refused.append((case_id, response.status_code))
    assert refused == []
    assert _row(db_session, created["id"])["changes"] == {"Control": None, "B": CHANGES}


@pytest.mark.regression
@pytest.mark.parametrize(
    "plant",
    ["changes", "config"],
    ids=["stored-changes-not-valid", "stored-config-not-valid"],
)
def test_a_clone_checks_what_it_copies(client, db_session, developer, plant):
    """X14: a value stored outside the API that fails its schema is not copied."""
    created = _create(client, developer)
    if plant == "changes":
        bad = {"version": 1, "changes": [{"op": "set_html", "selector": "a"}]}
        _write_changes(db_session, created["id"], "B", bad)
    else:
        _write_row(db_session, created["id"], visual_config={**CONFIG, "pages": []})

    response = client.post(f"{BASE}/{created['id']}/clone", headers=_auth(developer))

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == visual.CLONE_VISUAL_NOT_VALID


def test_u9_a_clone_copies_the_pages_goals_and_changes(client, developer):
    created = _create(client, developer)
    clone = _ok(
        client.post(f"{BASE}/{created['id']}/clone", headers=_auth(developer)), 201
    )

    assert clone["status"] == "draft"
    assert clone["visual_config"] == CONFIG
    assert {v["name"]: v["visual_changes"] for v in clone["variants"]} == {
        "Control": None,
        "B": CHANGES,
    }
    assert clone["design_etag"] is not None


# --- S1: carry-over ----------------------------------------------------------------


def test_a_variants_put_without_changes_keeps_them_and_null_clears(
    client, db_session, developer
):
    created = _create(client, developer)
    _ok(
        client.put(
            f"{BASE}/{created['id']}",
            json={"variants": _variants(None)},
            headers=_auth(developer),
        )
    )
    assert _row(db_session, created["id"])["changes"]["B"] == CHANGES

    cleared = _variants(None)
    cleared[1]["visual_changes"] = None
    _ok(
        client.put(
            f"{BASE}/{created['id']}",
            json={"variants": cleared},
            headers=_auth(developer),
        )
    )
    assert _row(db_session, created["id"])["changes"]["B"] is None


def test_page_changes_are_refused_on_the_control_and_without_visual_config(
    client, developer
):
    created = _create(client, developer)
    on_control = _variants()
    on_control[0]["visual_changes"] = CHANGES
    response = client.put(
        f"{BASE}/{created['id']}",
        json={"variants": on_control},
        headers=_auth(developer),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {
            "loc": ["body", "variants", 0, "visual_changes"],
            "msg": visual_schema.CONTROL_CHANGES_REFUSED,
            "type": "visual_changes",
        }
    ]

    plain = _create(client, developer, visual_config=None, variants=_variants(None))
    response = client.put(
        f"{BASE}/{plain['id']}",
        json={"variants": _variants()},
        headers=_auth(developer),
    )
    assert response.status_code == 422, response.text
    assert (
        response.json()["detail"][0]["msg"]
        == visual_schema.CHANGES_WITHOUT_CONFIG_REFUSED
    )


# --- V54 (names), on the result of a PUT ------------------------------------------


def test_v54_a_goal_needs_a_metric_with_its_event_name(client, db_session, developer):
    created = _create(client, developer)

    other_goal = copy.deepcopy(CONFIG)
    other_goal["goals"][0]["event_name"] = "click_other"
    response = client.put(
        f"{BASE}/{created['id']}",
        json={"visual_config": other_goal},
        headers=_auth(developer),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {
            "loc": ["body", "visual_config", "goals", 0, "event_name"],
            "msg": visual_schema.GOAL_WITHOUT_METRIC_REFUSED,
            "type": "visual_config",
        }
    ]

    other_metric = [{"name": "Signups", "event_name": "signup"}]
    response = client.put(
        f"{BASE}/{created['id']}",
        json={"metrics": other_metric},
        headers=_auth(developer),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "metrics"]
    assert _row(db_session, created["id"])["visual_config"] == CONFIG


# --- V71c, K14, K16: visual stays visual; a dated draft does not become visual ----


@pytest.mark.regression
def test_v71c_visual_config_cannot_be_removed_from_a_visual_draft(
    client, db_session, developer
):
    created = _create(client, developer)

    response = client.put(
        f"{BASE}/{created['id']}",
        json={"visual_config": None},
        headers=_auth(developer),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {
            "loc": ["body", "visual_config"],
            "msg": (
                "A visual experiment keeps its visual settings, so they cannot be "
                "removed. To test without page changes, create a new experiment, "
                "and delete this draft if you no longer need it."
            ),
            "type": "visual_config",
        }
    ]
    assert _row(db_session, created["id"])["visual_config"] == CONFIG
    # ... so the schedule's refusal still holds (the race PE P4 named).
    response = _schedule(
        client, developer, created["id"], {"start_date": _soon(days=1)}
    )
    assert response.status_code == 400, response.text


@pytest.mark.regression
def test_v71c_a_dated_draft_cannot_become_visual(client, db_session, developer):
    created = _create(client, developer, visual_config=None, variants=_variants(None))
    _ok(_schedule(client, developer, created["id"], {"start_date": _soon(days=1)}))

    response = client.put(
        f"{BASE}/{created['id']}",
        json={"visual_config": CONFIG},
        headers=_auth(developer),
    )

    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "visual_config"]
    assert error["msg"] == (
        "This draft has a scheduled start, so it cannot become a visual "
        "experiment. Remove the start date first: PUT /api/v1/experiments/"
        f'{created["id"]}/schedule with {{"start_date": null}}.'
    )
    assert "{id}" not in error["msg"]  # K16: the real id, never the template
    assert _row(db_session, created["id"])["visual_config"] is None


# --- V71a, K15: the one start-date writer refuses a visual draft ----------------


@pytest.mark.regression
def test_v71a_schedule_refuses_a_start_date_on_a_visual_draft(
    client, db_session, developer
):
    created = _create(client, developer)

    response = _schedule(
        client, developer, created["id"], {"start_date": _soon(days=1)}
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "A visual experiment cannot have a scheduled start: it starts only when "
        "someone clicks Start on its page in Controlvar. You can still set its "
        "end date."
    )
    assert _row(db_session, created["id"])["start_date"] is None

    # The end date alone is accepted.
    _ok(_schedule(client, developer, created["id"], {"end_date": _soon(days=10)}))
    assert _row(db_session, created["id"])["start_date"] is None

    # A start date written outside the API is cleared with null.
    _write_row(
        db_session,
        created["id"],
        start_date=datetime.now(timezone.utc) + timedelta(days=2),
    )
    _ok(_schedule(client, developer, created["id"], {"start_date": None}))
    assert _row(db_session, created["id"])["start_date"] is None


def test_v71a_a_paused_visual_experiment_can_still_schedule_its_resume(
    client, db_session, developer
):
    created = _create(client, developer)
    _ok(
        _start(
            client,
            developer,
            created["id"],
            json={"design_etag": created["design_etag"]},
        )
    )
    _ok(client.post(f"{BASE}/{created['id']}/pause", headers=_auth(developer)))

    body = _ok(
        _schedule(client, developer, created["id"], {"start_date": _soon(days=1)})
    )

    assert body["status"] == "paused"
    assert _row(db_session, created["id"])["resume_at"] is not None


@pytest.mark.regression
def test_v71a2_create_and_put_drop_a_start_date(client, db_session, developer):
    body = _body()
    body["start_date"] = _soon(days=1)
    created = _ok(client.post(f"{BASE}/", json=body, headers=_auth(developer)), 201)
    assert _row(db_session, created["id"])["start_date"] is None

    _ok(
        client.put(
            f"{BASE}/{created['id']}",
            json={"start_date": _soon(days=1), "name": f"{PREFIX}renamed"},
            headers=_auth(developer),
        )
    )
    assert _row(db_session, created["id"])["start_date"] is None


# --- V68, K16: Start refuses a design the caller did not review -----------------


@pytest.mark.regression
def test_v68_start_refuses_a_design_changed_after_it_was_read(
    client, db_session, developer
):
    created = _create(client, developer)
    e1 = _ok(client.get(f"{BASE}/{created['id']}", headers=_auth(developer)))[
        "design_etag"
    ]
    changed = copy.deepcopy(CHANGES)
    changed["changes"][0]["text"] = "Hello again"
    e2 = _ok(
        client.put(
            f"{BASE}/{created['id']}",
            json={"variants": _variants(changed)},
            headers=_auth(developer),
        )
    )["design_etag"]
    assert e1 != e2

    stale = _start(client, developer, created["id"], json={"design_etag": e1})
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == (
        "This experiment's design changed after this page loaded, so it was not "
        "started. Reload to see the change, then start again."
    )
    missing = _start(client, developer, created["id"])
    assert missing.status_code == 400, missing.text
    assert missing.json()["detail"] == (
        "To start a visual experiment, send the design_etag of the version you "
        f"reviewed (GET /api/v1/experiments/{created['id']})"
    )
    assert _row(db_session, created["id"])["status"] == ExperimentStatus.DRAFT

    started = _ok(_start(client, developer, created["id"], json={"design_etag": e2}))
    assert started["status"] == "active"


def test_uc9_a_visual_experiment_without_a_metric_is_created_and_cannot_start(
    client, developer
):
    config = {**CONFIG, "goals": []}
    created = _create(client, developer, visual_config=config, metrics=[])
    assert created["metrics"] == []

    response = _start(
        client, developer, created["id"], json={"design_etag": created["design_etag"]}
    )

    assert response.status_code == 400, response.text
    assert (
        response.json()["detail"] == "Experiment must have at least one metric defined"
    )


def test_start_refuses_a_visual_draft_with_no_page_change(client, developer):
    created = _create(client, developer, variants=_variants(None))
    response = _start(
        client, developer, created["id"], json={"design_etag": created["design_etag"]}
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == visual.NO_PAGE_CHANGES


def test_start_refuses_stored_changes_that_fail_their_schema(
    client, db_session, developer
):
    created = _create(client, developer)
    invalid = {"version": 2, "changes": [{"op": "hide", "selector": "#x"}]}
    _write_changes(db_session, created["id"], "B", invalid)
    etag = _ok(client.get(f"{BASE}/{created['id']}", headers=_auth(developer)))[
        "design_etag"
    ]

    response = _start(client, developer, created["id"], json={"design_etag": etag})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == visual.CHANGES_NOT_VALID.format(name="B")


# --- R6: an ordinary start takes any of these bodies ------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"content": b"", "headers_extra": {"Content-Type": "application/json"}},
        {"json": None},
        {"json": {}},
        {"json": {"not_a_field": 1, "design_etag": "ignored"}},
    ],
    ids=["no-body", "empty-body", "null", "empty-object", "unknown-key"],
)
def test_r6_an_ordinary_start_takes_no_body(client, developer, kwargs):
    created = _create(client, developer, visual_config=None, variants=_variants(None))
    kwargs = dict(kwargs)
    headers = {**_auth(developer), **kwargs.pop("headers_extra", {})}
    if "json" in kwargs and kwargs["json"] is None:
        kwargs = {"content": b"null"}
        headers["Content-Type"] = "application/json"
    response = client.post(f"{BASE}/{created['id']}/start", headers=headers, **kwargs)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"


@pytest.mark.regression
def test_r11_the_stable_put_drops_an_unknown_key(client, developer):
    created = _create(client, developer)
    response = client.put(
        f"{BASE}/{created['id']}",
        json={"not_a_field": 1, "hypothesis": "kept"},
        headers=_auth(developer),
    )
    assert response.status_code == 200, response.text
    assert response.json()["hypothesis"] == "kept"


# --- V67: a start ends the editor's credentials -----------------------------------


def _token(experiment_id, kind, reason=None):
    return VisualToken(
        kind=kind,
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        experiment_id=uuid.UUID(str(experiment_id)),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        revoked_at=datetime.now(timezone.utc) if reason else None,
        revoked_reason=reason,
    )


def _plant_tokens(db_session, experiment_id):
    session = _factory(db_session)()
    try:
        rows = {
            "ticket": _token(experiment_id, "editor_ticket"),
            "session": _token(experiment_id, "editor_session"),
            "claim": _token(experiment_id, "editor_claim"),
            "preview": _token(experiment_id, "preview"),
            "reopened": _token(experiment_id, "editor_session", reason="reopened"),
        }
        session.add_all(rows.values())
        session.commit()
        return {name: row.id for name, row in rows.items()}
    finally:
        session.close()


def _reasons(db_session, ids):
    session = _factory(db_session)()
    try:
        out = {}
        for name, row_id in ids.items():
            row = session.get(VisualToken, row_id)
            out[name] = (row.revoked_at is not None, row.revoked_reason)
        return out
    finally:
        session.close()


@pytest.mark.regression
def test_v67_start_revokes_the_editor_tokens_of_that_experiment_only(
    client, db_session, developer
):
    created = _create(client, developer)
    other = _create(client, developer)
    mine = _plant_tokens(db_session, created["id"])
    theirs = _plant_tokens(db_session, other["id"])

    _ok(
        _start(
            client,
            developer,
            created["id"],
            json={"design_etag": created["design_etag"]},
        )
    )

    assert _reasons(db_session, mine) == {
        "ticket": (True, "started"),
        "session": (True, "started"),
        "claim": (True, "started"),
        "preview": (False, None),
        "reopened": (True, "reopened"),
    }
    assert _reasons(db_session, theirs) == {
        "ticket": (False, None),
        "session": (False, None),
        "claim": (False, None),
        "preview": (False, None),
        "reopened": (True, "reopened"),
    }


def test_the_token_checks_admit_the_designed_values_only(db_session, client, developer):
    created = _create(client, developer)
    session = _factory(db_session)()
    try:
        for kind, reason in (("editor_claim", "scheduled"), ("preview", None)):
            session.add(_token(created["id"], kind, reason))
        session.commit()
        for kind, reason in (("cookie", None), ("editor_ticket", "expired")):
            session.add(_token(created["id"], kind, reason))
            with pytest.raises(Exception) as refused:
                session.commit()
            assert "ck_visual_tokens_" in str(refused.value)
            session.rollback()
    finally:
        session.close()


# --- V26: by role, non-superusers ------------------------------------------------


@pytest.mark.parametrize(
    "role,expected",
    [
        (UserRole.ADMIN, 200),
        (UserRole.DEVELOPER, 200),
        (UserRole.ANALYST, 403),
        (UserRole.VIEWER, 403),
    ],
)
def test_v26_who_may_write_a_visual_draft(
    client, db_session, developer, role, expected
):
    created = _create(client, developer)
    actor = _user(db_session, role)
    changed = copy.deepcopy(CHANGES)
    changed["changes"][0]["text"] = "By role"

    put = client.put(
        f"{BASE}/{created['id']}",
        json={"variants": _variants(changed)},
        headers=_auth(actor),
    )
    create = client.post(f"{BASE}/", json=_body(), headers=_auth(actor))

    assert put.status_code == expected, put.text
    assert create.status_code == (201 if expected == 200 else 403), create.text
    stored = _row(db_session, created["id"])["changes"]["B"]
    assert stored == (changed if expected == 200 else CHANGES)


# --- V30: the audit entry names visual_config, never its contents -----------------


def _entries(db_session, experiment_id):
    session = _factory(db_session)()
    try:
        rows = (
            session.query(AuditLog)
            .filter(AuditLog.entity_id == uuid.UUID(str(experiment_id)))
            .filter(AuditLog.action_type == "experiment_update")
            .order_by(AuditLog.timestamp)
            .all()
        )
        return [(row.old_value, row.new_value, row.user_id) for row in rows]
    finally:
        session.close()


def test_v30_a_save_names_visual_config_and_stores_none_of_it(
    client, db_session, developer
):
    created = _create(client, developer)
    moved = copy.deepcopy(CONFIG)
    moved["pages"][0]["url"] = "https://shop.example.com/canary-7731/"
    moved["editor_url"] = "https://shop.example.com/canary-7731/"
    moved["goals"][0]["page"] = moved["pages"][0]
    _ok(
        client.put(
            f"{BASE}/{created['id']}",
            json={"visual_config": moved},
            headers=_auth(developer),
        )
    )

    (entry,) = _entries(db_session, created["id"])
    old_value, new_value, actor = entry
    assert json.loads(new_value) == {"changed_fields": ["visual_config"]}
    assert old_value is None
    assert actor == developer.id
    assert "canary-7731" not in (new_value or "") + (old_value or "")


# --- D8: a variant's page changes are named as visual_changes ---------------------


def _page_changes_edited(variants):
    variants[1]["visual_changes"] = {
        "version": 1,
        "changes": [{"op": "set_text", "selector": "#headline", "text": "canary-5512"}],
    }
    return ["visual_changes"]


def _changed_variant_renamed(variants):
    variants[1]["name"] = "B renamed"
    return ["variants", "visual_changes"]


def _unchanged_variant_renamed(variants):
    variants[0]["name"] = "Control renamed"
    return ["variants"]


@pytest.mark.regression
@pytest.mark.parametrize(
    "change",
    [_page_changes_edited, _changed_variant_renamed, _unchanged_variant_renamed],
    ids=lambda change: change.__name__.lstrip("_"),
)
def test_d8_a_save_names_page_changes_as_visual_changes(
    client, db_session, developer, change
):
    """A variant's page changes are named apart from ``variants`` (D8): editing
    them names ``visual_changes`` only, renaming the variant that has them
    names both, renaming one without them names ``variants`` only. Their
    contents are never stored."""
    created = _create(client, developer)
    variants = _variants()
    expected = change(variants)
    _ok(
        client.put(
            f"{BASE}/{created['id']}",
            json={"variants": variants},
            headers=_auth(developer),
        )
    )

    (entry,) = _entries(db_session, created["id"])
    old_value, new_value, _ = entry
    assert json.loads(new_value) == {"changed_fields": expected}
    assert old_value is None
    assert "canary-5512" not in new_value


# --- R1, R3 ---------------------------------------------------------------------


@pytest.mark.regression
def test_r1_a_code_experiments_configuration_is_untouched(client, developer):
    configuration = {"html": "<b>x</b>", "url": "javascript:void(0)"}
    variants = _variants(None)
    variants[1]["configuration"] = configuration
    created = _create(client, developer, visual_config=None, variants=variants)

    read = _ok(client.get(f"{BASE}/{created['id']}", headers=_auth(developer)))
    assert {v["name"]: v["configuration"] for v in read["variants"]}[
        "B"
    ] == configuration


def test_r3_a_split_url_experiment_cannot_be_made_visual(client, db_session, developer):
    created = _create(client, developer, visual_config=None, variants=_variants(None))
    _write_row(
        db_session,
        created["id"],
        experiment_type=Experiment.experiment_type.type.enum_class.SPLIT_URL,
    )

    response = client.put(
        f"{BASE}/{created['id']}",
        json={"visual_config": CONFIG},
        headers=_auth(developer),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {
            "loc": ["body", "visual_config"],
            "msg": visual_schema.SPLIT_URL_VISUAL_REFUSED,
            "type": "visual_config",
        }
    ]


def test_visual_config_is_locked_once_the_experiment_leaves_draft(
    client, db_session, developer
):
    admin = _user(db_session, UserRole.ADMIN, superuser=True)
    created = _create(client, developer)
    _ok(
        _start(
            client,
            developer,
            created["id"],
            json={"design_etag": created["design_etag"]},
        )
    )

    response = client.put(
        f"{BASE}/{created['id']}", json={"visual_config": CONFIG}, headers=_auth(admin)
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "Cannot update visual_config for experiments in active status"
    )
