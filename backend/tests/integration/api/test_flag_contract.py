"""The feature-flag create/update contract (#94: D39 as narrowed by D40).

What these pin, through the real routes and a real database:

* a new flag is inactive unless ``is_active: true`` is sent, and every flag
  response carries ``is_active`` as a boolean;
* an unknown request field answers 422 instead of being dropped;
* the read-only fields of a response are accepted and ignored, so any flag
  response can be sent back with PUT unchanged; a ``status`` must agree;
* ``targeting_rules`` is the one name for targeting; ``rules``, ``variants``,
  ``metrics`` and ``last_evaluated`` are gone from every response;
* ``default_value`` is stored, returned and type-checked, and only ``false`` is
  accepted (D40), for every flag; a boolean flag's evaluation is unchanged
  (the value fields of #1497 are pinned in test_flag_values_api.py);
* an explicit null on a NOT NULL field answers 422 (it was a 500, or a silent
  switch-off for ``is_active``);
* ``is_active: false`` on an archived flag keeps it archived, and
  ``is_active: true`` answers 400 (#631; every other status route is pinned in
  test_archived_flag_verbs.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.schemas.feature_flag import (
    DEFAULT_VALUE_UNSUPPORTED,
    STATUS_READ_ONLY,
    FeatureFlagCreate,
    FeatureFlagUpdate,
)
from backend.app.services.feature_flag_service import (
    FeatureFlagService,
    FlagStatusReadOnly,
)
from backend.tests.integration.helpers import unique_flag_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

COLLECTION = "/api/v1/feature-flags"

#: Every flag response has exactly these keys.
READ_KEYS = {
    "id",
    "key",
    "name",
    "description",
    "status",
    "is_active",
    "rollout_percentage",
    "targeting_rules",
    "default_value",
    "value_type",
    "on_value",
    "off_value",
    "tags",
    "owner_id",
    "created_at",
    "updated_at",
}

RULES = {
    "logical_operator": "OR",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "US"}
            ],
        }
    ],
}


def _row(db_session, flag_id) -> FeatureFlag:
    db_session.expire_all()
    return db_session.get(FeatureFlag, uuid.UUID(str(flag_id)))


def _snapshot(flag: FeatureFlag) -> dict:
    """Every column a request could reach, except ``updated_at``."""
    return {
        "id": flag.id,
        "key": flag.key,
        "name": flag.name,
        "description": flag.description,
        "status": flag.status,
        "owner_id": flag.owner_id,
        "rollout_percentage": flag.rollout_percentage,
        "targeting_rules": flag.targeting_rules,
        "default_value": flag.default_value,
        "value_type": flag.value_type,
        "on_value": flag.on_value,
        "off_value": flag.off_value,
        "tags": flag.tags,
        "created_at": flag.created_at,
    }


def _create(client, **body):
    body.setdefault("key", unique_flag_key("contract"))
    body.setdefault("name", "Contract flag")
    return client.post(f"{COLLECTION}/", json=body)


def _errors(response):
    assert response.status_code == 422, response.text
    return [(e["type"], e["loc"]) for e in response.json()["detail"]]


# --- D39-1: a new flag is off ---------------------------------------------


@pytest.mark.regression
def test_create_without_is_active_is_inactive(admin_client, db_session):
    """R6/V6: the only gate for the inactive default (the live seed writes through the ORM)."""
    response = _create(admin_client)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["is_active"] is False
    assert body["status"] == "inactive"
    assert _row(db_session, body["id"]).status == FeatureFlagStatus.INACTIVE

    evaluated = admin_client.get(
        f"{COLLECTION}/evaluate/{body['key']}", params={"user_id": "u-1"}
    )
    assert evaluated.status_code == 200, evaluated.text
    assert evaluated.json()["enabled"] is False
    assert evaluated.json()["reason"] == "inactive"


@pytest.mark.regression
@pytest.mark.parametrize("sent", [None, False, True], ids=["omitted", "false", "true"])
def test_is_active_is_a_boolean_on_every_surface(admin_client, sent):
    """R7/V6b: create, GET, PUT and list all carry ``is_active``, never null."""
    body = {} if sent is None else {"is_active": sent}
    created = _create(admin_client, **body)
    assert created.status_code == 201, created.text
    expected = bool(sent)
    flag = created.json()

    got = admin_client.get(f"{COLLECTION}/{flag['id']}").json()
    put = admin_client.put(
        f"{COLLECTION}/{flag['id']}", json={"description": "touched"}
    ).json()
    (item,) = [
        i
        for i in admin_client.get(
            f"{COLLECTION}/", params={"search": flag["key"]}
        ).json()["items"]
        if i["key"] == flag["key"]
    ]
    for surface, value in (
        ("create", flag),
        ("get", got),
        ("put", put),
        ("list", item),
    ):
        assert value["is_active"] is expected, surface
        assert value["status"] == ("active" if expected else "inactive"), surface


# --- D39-2: unknown fields refused, read-only fields ignored ---------------


@pytest.mark.regression
@pytest.mark.parametrize("field", ["rules", "enabled", "variants", "flag_type"])
def test_create_unknown_field_is_422(admin_client, db_session, field):
    key = unique_flag_key("unknown")
    response = _create(admin_client, key=key, **{field: {"a": 1}})

    assert _errors(response) == [("extra_forbidden", ["body", field])]
    assert db_session.query(FeatureFlag).filter(FeatureFlag.key == key).count() == 0


@pytest.mark.regression
@pytest.mark.parametrize("field", ["rules", "enabled", "variants", "last_evaluated"])
def test_update_unknown_field_is_422(
    admin_client, db_session, make_feature_flag, field
):
    flag = make_feature_flag(rollout_percentage=10)
    before = _snapshot(_row(db_session, flag.id))

    response = admin_client.put(
        f"{COLLECTION}/{flag.id}", json={"rollout_percentage": 50, field: 1}
    )

    assert _errors(response) == [("extra_forbidden", ["body", field])]
    assert _snapshot(_row(db_session, flag.id)) == before


def _surfaces(client, db_session, make_feature_flag, status, owner_id):
    """(name, response body) for every route that returns a flag."""
    # Nothing below changes the flag: the PUT re-sends the stored description.
    flag = make_feature_flag(
        status=status, owner_id=owner_id, targeting_rules=RULES, description="d"
    )
    path = f"{COLLECTION}/{flag.id}"
    out = [("get", client.get(path).json())]
    (item,) = [
        i
        for i in client.get(f"{COLLECTION}/", params={"search": flag.key}).json()[
            "items"
        ]
        if i["key"] == flag.key
    ]
    out.append(("list_item", item))
    out.append(("put", client.put(path, json={"description": "d"}).json()))
    if status is FeatureFlagStatus.ACTIVE:
        out.append(("activate_noop", client.post(f"{path}/activate").json()))
    if status is FeatureFlagStatus.INACTIVE:
        out.append(("deactivate_noop", client.post(f"{path}/deactivate").json()))
    return flag, out


@pytest.mark.regression
@pytest.mark.parametrize(
    "status",
    [FeatureFlagStatus.ACTIVE, FeatureFlagStatus.INACTIVE, FeatureFlagStatus.ARCHIVED],
    ids=lambda s: s.value.lower(),
)
@pytest.mark.parametrize("owned", [True, False], ids=["owned", "null_owner"])
def test_every_response_can_be_put_back_unchanged(
    admin_client, admin_user, db_session, make_feature_flag, status, owned
):
    """V4: every surface's body, verbatim, PUT back → 200 and nothing changes.

    The key set comes from the live response, never a hand-typed list, and an
    archived flag and a flag whose owner is gone are among them.
    """
    flag, surfaces = _surfaces(
        admin_client,
        db_session,
        make_feature_flag,
        status,
        admin_user.id if owned else None,
    )
    for name, body in surfaces:
        assert set(body) == READ_KEYS, (name, set(body) ^ READ_KEYS)
        assert body["owner_id"] == (str(admin_user.id) if owned else None), name
        before = _snapshot(_row(db_session, flag.id))

        response = admin_client.put(f"{COLLECTION}/{flag.id}", json=body)

        assert response.status_code == 200, (name, response.text)
        assert _snapshot(_row(db_session, flag.id)) == before, name


@pytest.mark.regression
def test_the_create_response_can_be_put_back_unchanged(admin_client, db_session):
    created = _create(admin_client, is_active=True, targeting_rules=RULES, tags=["t"])
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == READ_KEYS
    before = _snapshot(_row(db_session, body["id"]))

    response = admin_client.put(f"{COLLECTION}/{body['id']}", json=body)

    assert response.status_code == 200, response.text
    assert _snapshot(_row(db_session, body["id"])) == before


@pytest.mark.regression
def test_read_only_fields_in_a_put_are_ignored(
    admin_client, db_session, make_feature_flag, developer_user
):
    """V4c through the route: ``id``, ``owner_id`` and timestamps never land."""
    flag = make_feature_flag()
    before = _snapshot(_row(db_session, flag.id))

    response = admin_client.put(
        f"{COLLECTION}/{flag.id}",
        json={
            "id": str(uuid.uuid4()),
            "owner_id": str(developer_user.id),
            "created_at": "2000-01-01T00:00:00",
            "updated_at": "2000-01-01T00:00:00",
            "rollout_percentage": 40,
        },
    )

    assert response.status_code == 200, response.text
    after = _snapshot(_row(db_session, flag.id))
    assert after == {**before, "rollout_percentage": 40}


@pytest.mark.regression
def test_read_only_fields_in_a_create_are_ignored(
    admin_client, admin_user, developer_user
):
    spoofed_id = str(uuid.uuid4())
    response = _create(
        admin_client,
        id=spoofed_id,
        owner_id=str(developer_user.id),
        created_at="2000-01-01T00:00:00",
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] != spoofed_id
    assert body["owner_id"] == str(admin_user.id)
    assert not body["created_at"].startswith("2000")


@pytest.mark.regression
def test_the_service_alone_ignores_read_only_fields(
    db_session, make_feature_flag, developer_user
):
    """V4c at the service (PE C1): no route strip is needed for this to hold.

    ``status`` is sent the way GET returns it (lower case); written to the
    column it would fail the enum, and ``id`` would rewrite the primary key.
    """
    flag = make_feature_flag()
    before = _snapshot(_row(db_session, flag.id))
    update = FeatureFlagUpdate(
        id=uuid.uuid4(),
        owner_id=developer_user.id,
        created_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        status="inactive",
    )

    FeatureFlagService(db_session).update_feature_flag(flag.id, update)

    assert _snapshot(_row(db_session, flag.id)) == before


@pytest.mark.regression
def test_the_service_alone_ignores_read_only_fields_on_create(
    db_session, admin_user, developer_user
):
    spoofed = uuid.uuid4()
    created = FeatureFlagService(db_session).create_feature_flag(
        FeatureFlagCreate(
            key=unique_flag_key("svc"),
            name="Service",
            id=spoofed,
            owner_id=developer_user.id,
            created_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            status="INACTIVE",
        ),
        owner_id=admin_user.id,
    )

    row = _row(db_session, created.id)
    assert row.id != spoofed
    assert row.owner_id == admin_user.id
    assert row.created_at.year != 2000
    assert row.status == FeatureFlagStatus.INACTIVE


# --- status: read-only, but it must agree ---------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("sent", ["inactive", "INACTIVE", "Inactive"])
def test_put_status_equal_to_the_stored_one_is_accepted(
    admin_client, make_feature_flag, sent
):
    flag = make_feature_flag()
    response = admin_client.put(
        f"{COLLECTION}/{flag.id}", json={"status": sent, "rollout_percentage": 20}
    )
    assert response.status_code == 200, response.text
    assert response.json()["rollout_percentage"] == 20


@pytest.mark.regression
@pytest.mark.parametrize("sent", ["ACTIVE", "active", "archived", "DRAFT", 1, None])
def test_put_status_that_differs_is_422(
    admin_client, db_session, make_feature_flag, sent
):
    """#94's own example: ``{"status": "ACTIVE"}`` was a silent no-op."""
    flag = make_feature_flag()
    before = _snapshot(_row(db_session, flag.id))

    response = admin_client.put(f"{COLLECTION}/{flag.id}", json={"status": sent})

    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {"type": "read_only", "loc": ["body", "status"], "msg": STATUS_READ_ONLY}
        ]
    }
    assert _snapshot(_row(db_session, flag.id)) == before


@pytest.mark.regression
def test_a_get_body_with_is_active_flipped_turns_the_flag_on(
    admin_client, db_session, make_feature_flag
):
    """``status`` agrees with the stored value; ``is_active`` decides."""
    flag = make_feature_flag()
    body = admin_client.get(f"{COLLECTION}/{flag.id}").json()
    assert body["status"] == "inactive"

    response = admin_client.put(
        f"{COLLECTION}/{flag.id}", json={**body, "is_active": True}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert _row(db_session, flag.id).status == FeatureFlagStatus.ACTIVE


@pytest.mark.regression
@pytest.mark.parametrize(
    "body, ok",
    [
        ({"status": "inactive"}, True),
        ({"status": "INACTIVE", "is_active": False}, True),
        ({"status": "active", "is_active": True}, True),
        ({"status": "active"}, False),
        ({"status": "inactive", "is_active": True}, False),
        ({"status": "archived"}, False),
        ({"status": "DRAFT"}, False),
    ],
)
def test_create_status_must_be_the_one_the_flag_gets(
    admin_client, db_session, body, ok
):
    key = unique_flag_key("status")
    response = _create(admin_client, key=key, **body)

    if ok:
        assert response.status_code == 201, response.text
    else:
        assert _errors(response) == [("read_only", ["body", "status"])]
        assert db_session.query(FeatureFlag).filter(FeatureFlag.key == key).count() == 0


def test_the_service_refuses_a_disagreeing_status(db_session, make_feature_flag):
    flag = make_feature_flag()
    with pytest.raises(FlagStatusReadOnly):
        FeatureFlagService(db_session).update_feature_flag(
            flag.id, FeatureFlagUpdate(status="ACTIVE")
        )
    assert _row(db_session, flag.id).status == FeatureFlagStatus.INACTIVE


# --- archived: PUT keeps it archived; nothing turns it on (#631) -----------


@pytest.mark.regression
def test_put_is_active_false_keeps_an_archived_flag_archived(
    admin_client, db_session, make_feature_flag
):
    flag = make_feature_flag(status=FeatureFlagStatus.ARCHIVED)

    response = admin_client.put(f"{COLLECTION}/{flag.id}", json={"is_active": False})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"
    assert _row(db_session, flag.id).status == FeatureFlagStatus.ARCHIVED


@pytest.mark.regression
def test_put_is_active_true_on_an_archived_flag_answers_400(
    admin_client, db_session, make_feature_flag
):
    """Inverted by #631: this pin said 200 and ACTIVE; Rule S refuses it."""
    flag = make_feature_flag(status=FeatureFlagStatus.ARCHIVED)

    response = admin_client.put(f"{COLLECTION}/{flag.id}", json={"is_active": True})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "This flag is archived. Unarchive it before turning it on."
    )
    assert _row(db_session, flag.id).status == FeatureFlagStatus.ARCHIVED


@pytest.mark.regression
@pytest.mark.parametrize(
    "route, body, code",
    [
        ("{id}/deactivate", None, 200),
        ("{id}/disable", {}, 200),
        ("bulk-toggle", {"action": "disable"}, 200),
        ("{id}/toggle", {}, 400),
    ],
    ids=["deactivate", "disable", "bulk_disable", "toggle"],
)
def test_the_other_routes_leave_an_archived_flag_archived(
    admin_client, db_session, make_feature_flag, route, body, code
):
    """Inverted by #631: these took the flag out of ARCHIVED (three to INACTIVE,
    toggle to ACTIVE). An off-verb now succeeds and changes nothing; toggle
    would turn it on, so it answers 400."""
    flag = make_feature_flag(status=FeatureFlagStatus.ARCHIVED)
    if route == "bulk-toggle":
        body = {**body, "flag_ids": [str(flag.id)]}

    response = admin_client.post(f"{COLLECTION}/{route.format(id=flag.id)}", json=body)

    assert response.status_code == code, response.text
    assert _row(db_session, flag.id).status == FeatureFlagStatus.ARCHIVED


# --- D39-3: one name for targeting -----------------------------------------


@pytest.mark.regression
def test_detail_returns_targeting_rules_not_rules(admin_client, make_feature_flag):
    """R8/V7b: exact key set on every single-flag route."""
    flag = make_feature_flag(status=FeatureFlagStatus.INACTIVE, targeting_rules=RULES)
    path = f"{COLLECTION}/{flag.id}"
    for name, response in (
        ("get", admin_client.get(path)),
        ("put", admin_client.put(path, json={"description": "x"})),
        ("activate", admin_client.post(f"{path}/activate")),
        ("deactivate", admin_client.post(f"{path}/deactivate")),
    ):
        assert response.status_code == 200, (name, response.text)
        body = response.json()
        assert set(body) == READ_KEYS, (name, set(body) ^ READ_KEYS)
        assert body["targeting_rules"] == RULES, name


# --- D39-4 as narrowed by D40: default_value --------------------------------


@pytest.mark.regression
def test_default_value_round_trips(admin_client, db_session):
    """R9: stored, returned, false by default."""
    response = _create(admin_client)
    assert response.status_code == 201, response.text
    assert response.json()["default_value"] is False
    assert _row(db_session, response.json()["id"]).default_value is False

    explicit = _create(admin_client, default_value=False)
    assert explicit.status_code == 201, explicit.text
    assert explicit.json()["default_value"] is False


@pytest.mark.regression
@pytest.mark.parametrize("method", ["post", "put"])
def test_default_value_true_is_422(admin_client, db_session, make_feature_flag, method):
    if method == "post":
        response = _create(admin_client, default_value=True)
    else:
        flag = make_feature_flag()
        response = admin_client.put(
            f"{COLLECTION}/{flag.id}", json={"default_value": True}
        )
        assert _row(db_session, flag.id).default_value is False

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {
            "type": "default_value_unsupported",
            "loc": ["body", "default_value"],
            "msg": DEFAULT_VALUE_UNSUPPORTED,
        }
    ]


@pytest.mark.regression
@pytest.mark.parametrize(
    "sent, error",
    [
        ("false", "bool_type"),
        (0, "bool_type"),
        (1, "bool_type"),
        ("true", "bool_type"),
        ({}, "bool_type"),
        ("s3cr3t-value", "bool_type"),
        (None, "null_not_allowed"),
    ],
    ids=repr,
)
@pytest.mark.parametrize("method", ["post", "put"])
def test_default_value_must_be_json_false(
    admin_client, make_feature_flag, sent, error, method
):
    if method == "post":
        response = _create(admin_client, default_value=sent)
    else:
        flag = make_feature_flag()
        response = admin_client.put(
            f"{COLLECTION}/{flag.id}", json={"default_value": sent}
        )

    assert _errors(response) == [(error, ["body", "default_value"])]
    assert "s3cr3t" not in response.text


def test_a_put_of_only_default_value_false_succeeds(admin_client, make_feature_flag):
    flag = make_feature_flag()
    response = admin_client.put(
        f"{COLLECTION}/{flag.id}", json={"default_value": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["default_value"] is False


@pytest.mark.regression
def test_list_and_detail_report_the_stored_default_value(
    admin_client, make_feature_flag
):
    """R12: read from the column, not a schema default.

    No request can store ``true`` (D40); the row is written directly so a
    reader that answered from a default would be caught.
    """
    flag = make_feature_flag(default_value=True)

    detail = admin_client.get(f"{COLLECTION}/{flag.id}").json()
    (item,) = [
        i
        for i in admin_client.get(f"{COLLECTION}/", params={"search": flag.key}).json()[
            "items"
        ]
        if i["key"] == flag.key
    ]
    assert detail["default_value"] is True
    assert item["default_value"] is True


def test_evaluation_is_unchanged_an_inactive_flag_is_off(
    admin_client, db_session, make_feature_flag
):
    """D40: a boolean flag's off-value is false exactly as before; evaluation
    never reads the ``default_value`` column. Since #1497 the answer also
    carries ``value`` (a boolean flag's is ``enabled``) and ``value_type``; a
    value flag serves its ``off_value`` (test_flag_values_api.py)."""
    flag = make_feature_flag(status=FeatureFlagStatus.INACTIVE, rollout_percentage=100)
    assert _row(db_session, flag.id).default_value is False

    response = admin_client.get(
        f"{COLLECTION}/evaluate/{flag.key}", params={"user_id": "u-9"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "key": flag.key,
        "enabled": False,
        "value": False,
        "value_type": "boolean",
        "config": None,
        "reason": "inactive",
    }


# --- explicit null ----------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "field", ["key", "name", "is_active", "rollout_percentage", "default_value"]
)
def test_put_null_on_a_not_null_field_is_422(
    admin_client, db_session, make_feature_flag, field
):
    """R2-R4: ``key``/``name``/``rollout_percentage`` were 500s, and
    ``is_active: null`` silently switched a live flag off."""
    flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=30)
    before = _snapshot(_row(db_session, flag.id))

    response = admin_client.put(f"{COLLECTION}/{flag.id}", json={field: None})

    assert _errors(response) == [("null_not_allowed", ["body", field])]
    assert _snapshot(_row(db_session, flag.id)) == before


@pytest.mark.parametrize("field", ["description", "targeting_rules", "tags"])
def test_put_null_clears_a_nullable_field(
    admin_client, db_session, make_feature_flag, field
):
    flag = make_feature_flag(description="d", targeting_rules=RULES, tags=["t"])

    response = admin_client.put(f"{COLLECTION}/{flag.id}", json={field: None})

    assert response.status_code == 200, response.text
    assert response.json()[field] is None


def test_a_name_longer_than_the_column_is_422_not_500(admin_client):
    assert _errors(_create(admin_client, name="n" * 101)) == [
        ("string_too_long", ["body", "name"])
    ]


# --- order of checks, per route (PE M4) --------------------------------------


@pytest.mark.regression
def test_post_checks_permission_before_the_body(viewer_client):
    """POST's permission is a dependency, so a VIEWER gets 403 first."""
    response = viewer_client.post(
        f"{COLLECTION}/", json={"key": unique_flag_key("v"), "name": "n", "rules": {}}
    )
    assert response.status_code == 403, response.text


@pytest.mark.regression
def test_put_checks_the_body_before_permission(viewer_client, make_feature_flag):
    """PUT checks permission in the handler, after FastAPI validated the body."""
    flag = make_feature_flag()
    response = viewer_client.put(f"{COLLECTION}/{flag.id}", json={"rules": {}})
    assert _errors(response) == [("extra_forbidden", ["body", "rules"])]

    allowed_shape = viewer_client.put(
        f"{COLLECTION}/{flag.id}", json={"description": "x"}
    )
    assert allowed_shape.status_code == 403, allowed_shape.text


# --- the CRUD writers -------------------------------------------------------


@pytest.mark.regression
def test_the_crud_writers_ignore_read_only_fields(db_session, developer_user):
    """The list route's CRUD object can write flags too (no route calls it).

    It replaces the column guard the migration PR added (#698): with the
    request field typed and ``true`` refused, ``default_value`` is written,
    and false is the only value that can arrive.
    """
    from backend.app.crud.crud_feature_flag import crud_feature_flag

    spoofed = uuid.uuid4()
    created = crud_feature_flag.create(
        db_session,
        obj_in=FeatureFlagCreate(
            key=unique_flag_key("crud"),
            name="CRUD",
            id=spoofed,
            owner_id=developer_user.id,
            status="inactive",
        ),
    )
    row = _row(db_session, created.id)
    assert row.id != spoofed
    assert row.owner_id is None
    assert row.status == FeatureFlagStatus.INACTIVE
    assert row.default_value is False

    crud_feature_flag.update(
        db_session,
        db_obj=row,
        obj_in=FeatureFlagUpdate(
            owner_id=developer_user.id, status="inactive", is_active=True
        ),
    )
    row = _row(db_session, created.id)
    assert row.owner_id is None
    assert row.status == FeatureFlagStatus.ACTIVE
