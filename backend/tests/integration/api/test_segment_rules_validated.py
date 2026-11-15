"""Segment rules use the targeting rule format and are checked when saved (#440).

Through the routes, against a real database:

* ``POST /segments``, ``PUT /segments/{id}`` and ``POST /segments/{id}/preview``
  answer 422 for rules not in the targeting rule format, with ``loc``
  ``["body", "rules"]`` and a fixed message that never repeats the value;
* a dashboard-shaped or typo'd segment no longer matches every user;
* a row stored before the check (the legacy shape, or one of the four
  invalid dashboard-shaped rows) answers 409 "rules not valid" at evaluate,
  and bulk-evaluate reports it as ``false`` while still answering the others;
* the preview counts only users whose latest exposure carries a context;
* ``{segment_id}`` is a UUID: other text answers 422 on every route;
* ``python -m backend.scripts.check_targeting_rules`` lists the segments
  whose rules are not valid, and writes nothing.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.api.v1.endpoints.segments import SEGMENT_RULES_NOT_VALID
from backend.app.models.segment import Segment
from backend.app.models.segment import SegmentStatus as ModelSegmentStatus
from backend.scripts import check_targeting_rules
from backend.tests.integration.api.test_segment_preview_exposures import (
    _active_experiment,
    no_exposures,
)
from backend.tests.unit.core.test_segment_rule_validation import (
    LEGACY,
    MARKER,
    PE_FOUR,
    PE_FOUR_IDS,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

COLLECTION = "/api/v1/segments"


def _cond(attribute="country", operator="equals", value="US"):
    return {"attribute": attribute, "operator": operator, "value": value}


def _rules(*conditions):
    return {
        "logical_operator": "AND",
        "groups": [{"logical_operator": "AND", "conditions": list(conditions)}],
    }


US = _rules(_cond())

WRITE_REFUSED = [
    (LEGACY, "rules: unknown key"),
    ({"rules": []}, "rules: unknown key"),
    ({}, "groups: at least one group is required"),
    ({"anything": [MARKER]}, "rules: unknown key"),
    (
        _rules(_cond(operator="equal", value=MARKER)),
        "groups[0].conditions[0].operator: unknown operator",
    ),
] + PE_FOUR
WRITE_IDS = ["legacy", "native", "empty", "flat", "typo"] + [
    f"pe-{i}" for i in PE_FOUR_IDS
]

STORED_NOT_VALID = [LEGACY] + [rules for rules, _ in PE_FOUR]
STORED_IDS = ["legacy"] + [f"pe-{i}" for i in PE_FOUR_IDS]


def _create(client, rules=US, name=None):
    response = client.post(
        COLLECTION,
        json={"name": name or f"Seg {uuid.uuid4().hex[:8]}", "rules": rules},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def stored(db_session, admin_user):
    """Write a segment row directly, as a release before #440 could have."""
    made = []

    def _stored(rules, status=ModelSegmentStatus.ACTIVE, name=None):
        segment = Segment(
            name=name or f"Stored {uuid.uuid4().hex[:8]}",
            rules=rules,
            status=status,
            owner_id=admin_user.id,
        )
        db_session.add(segment)
        db_session.commit()
        db_session.refresh(segment)
        made.append(segment.id)
        return segment

    yield _stored
    db_session.rollback()
    if made:
        db_session.query(Segment).filter(Segment.id.in_(made)).delete(
            synchronize_session=False
        )
        db_session.commit()


def _assert_refused(response, message):
    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "rules"]
    assert error["msg"] == f"Value error, {message}"
    assert MARKER not in response.text


# ---------------------------------------------------------------------------
# Refused on write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rules, message", WRITE_REFUSED, ids=WRITE_IDS)
def test_refused_on_create(admin_client, rules, message):
    response = admin_client.post(COLLECTION, json={"name": "Refused", "rules": rules})
    _assert_refused(response, message)


@pytest.mark.parametrize("rules, message", WRITE_REFUSED, ids=WRITE_IDS)
def test_refused_on_update_and_nothing_is_written(
    admin_client, db_session, rules, message
):
    segment = _create(admin_client)
    response = admin_client.put(f"{COLLECTION}/{segment['id']}", json={"rules": rules})
    _assert_refused(response, message)
    stored = admin_client.get(f"{COLLECTION}/{segment['id']}").json()
    assert stored["rules"] == US


@pytest.mark.parametrize("rules, message", WRITE_REFUSED, ids=WRITE_IDS)
def test_refused_on_preview(admin_client, rules, message):
    segment = _create(admin_client)
    response = admin_client.post(
        f"{COLLECTION}/{segment['id']}/preview?sample_size=10",
        json={"name": "Refused", "rules": rules},
    )
    _assert_refused(response, message)


def test_accepted_rules_are_stored_as_sent(admin_client):
    rules = {
        "logical_operator": "OR",
        "groups": [
            {
                "id": "g-1",
                "logical_operator": "AND",
                "conditions": [
                    {"id": "c-1", **_cond("user.country", "in", "US, CA")},
                    _cond("app.version", "semver_gte", "17.4"),
                ],
            },
            {"conditions": [_cond("plan", "equals", "pro")]},
        ],
    }
    segment = _create(admin_client, rules)
    assert segment["rules"] == rules
    assert admin_client.get(f"{COLLECTION}/{segment['id']}").json()["rules"] == rules


def test_an_update_without_rules_keeps_stored_rules_that_are_not_valid(
    admin_client, stored
):
    """A legacy row can still be renamed; its rules are left as they are."""
    segment = stored(LEGACY)
    response = admin_client.put(f"{COLLECTION}/{segment.id}", json={"name": "Renamed"})
    assert response.status_code == 200, response.text
    assert response.json()["rules"] == LEGACY


# ---------------------------------------------------------------------------
# Regressions: these matched every user before #440
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_dashboard_shaped_segment_does_not_match_everyone(admin_client):
    """On main, ``equals`` was not an engine operator: the condition was
    dropped, the group was empty and every user was a member."""
    segment = _create(admin_client, US)

    def member(context):
        response = admin_client.post(
            f"{COLLECTION}/{segment['id']}/evaluate", json={"user_context": context}
        )
        assert response.status_code == 200, response.text
        return response.json()

    assert member({"country": "DE"})["is_member"] is False
    assert member({})["is_member"] is False
    us = member({"country": "US"})
    assert us["is_member"] is True
    assert us["matched_rules"] == ["country equals US"]
    assert member({"user": {"country": "US"}})["is_member"] is True


@pytest.mark.regression
def test_typod_segment_is_refused_not_everyone(admin_client, stored):
    """On main a typo'd operator was skipped and the segment matched everyone."""
    typo = _rules(_cond(operator="equal"))
    response = admin_client.post(COLLECTION, json={"name": "Typo", "rules": typo})
    _assert_refused(response, "groups[0].conditions[0].operator: unknown operator")

    segment = stored(typo)
    response = admin_client.post(
        f"{COLLECTION}/{segment.id}/evaluate", json={"user_context": {"country": "DE"}}
    )
    assert response.status_code == 409, response.text


# ---------------------------------------------------------------------------
# Stored rows that are not valid: 409 at evaluate, false in bulk
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("rules", STORED_NOT_VALID, ids=STORED_IDS)
def test_evaluate_answers_409_for_stored_rules_that_are_not_valid(
    admin_client, stored, rules
):
    segment = stored(rules)
    for context in ({"country": "US"}, {"country": "DE"}, {}):
        response = admin_client.post(
            f"{COLLECTION}/{segment.id}/evaluate", json={"user_context": context}
        )
        assert response.status_code == 409, response.text
        assert response.json() == {"detail": SEGMENT_RULES_NOT_VALID}
        assert "rules not valid" in response.json()["detail"]
        assert MARKER not in response.text


@pytest.mark.regression
def test_bulk_evaluate_reports_false_for_rules_not_valid_and_answers_the_rest(
    admin_client, stored
):
    valid = _create(admin_client, US)
    invalid = [str(stored(rules).id) for rules in STORED_NOT_VALID]
    response = admin_client.post(
        f"{COLLECTION}/bulk-evaluate",
        json={
            "user_context": {"country": "US"},
            "segment_ids": invalid + [valid["id"]],
        },
    )
    assert response.status_code == 200, response.text
    memberships = response.json()["memberships"]
    assert memberships == {**dict.fromkeys(invalid, False), valid["id"]: True}


def test_bulk_evaluate_text_that_is_not_a_uuid_is_false_and_breaks_nothing(
    admin_client,
):
    valid = _create(admin_client, US)
    response = admin_client.post(
        f"{COLLECTION}/bulk-evaluate",
        json={"user_context": {"country": "US"}, "segment_ids": ["seg-1", valid["id"]]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["memberships"] == {"seg-1": False, valid["id"]: True}


# ---------------------------------------------------------------------------
# The preview counts only users whose exposure carries a context
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_preview_counts_only_assignments_with_a_context(
    admin_client, db_session, make_experiment, no_exposures
):
    """On main, rows without a context were counted as sampled non-matches
    (``sample_size: 26, matched: 0``). The preview reads the exposures
    /tracking/assign writes (#830): a user assigned with no context is counted
    in ``users_without_context``, never in the sample."""
    attribute = f"zq_preview_{uuid.uuid4().hex[:8]}"
    experiment = _active_experiment(db_session, make_experiment)
    segment = _create(admin_client)
    rules = _rules(_cond(attribute, "equals", "yes"))

    def assign(user_id, context=None):
        body = {"experiment_key": experiment.key, "user_id": user_id}
        if context is not None:
            body["context"] = context
        response = admin_client.post("/api/v1/tracking/assign", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["assigned"] is True, response.text

    def preview():
        response = admin_client.post(
            f"{COLLECTION}/{segment['id']}/preview?sample_size=10000",
            json={"name": "Preview", "rules": rules},
        )
        assert response.status_code == 200, response.text
        return response.json()

    for i in range(20):
        assign(f"{attribute}-none-{i}")
    body = preview()
    assert (body["sample_size"], body["matched"]) == (0, 0)
    assert body["users_without_context"] == 20
    assert body["estimated_percentage"] is None

    for suffix, value in (("a", "yes"), ("b", "yes"), ("c", "no")):
        assign(f"{attribute}-ctx-{suffix}", {attribute: value})
    assign(f"{attribute}-nested", {"user": {attribute: "yes"}})
    body = preview()
    assert body["sample_size"] == 4
    assert body["matched"] == 3
    assert body["users_without_context"] == 20
    assert body["reason"] == "too_few_users"


# ---------------------------------------------------------------------------
# {segment_id} is a UUID
# ---------------------------------------------------------------------------

ROUTES = [
    ("get", "/{id}", None),
    ("put", "/{id}", {"name": "Renamed"}),
    ("delete", "/{id}", None),
    ("post", "/{id}/evaluate", {"user_context": {}}),
    ("get", "/{id}/experiments", None),
    ("post", "/{id}/preview?sample_size=10", {"name": "Preview", "rules": US}),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "method, path, body", ROUTES, ids=[f"{m}-{p}" for m, p, _ in ROUTES]
)
def test_non_uuid_path_id_is_422(admin_client, method, path, body):
    url = COLLECTION + path.replace("{id}", "seg-not-a-uuid")
    kwargs = {"json": body} if body is not None else {}
    response = getattr(admin_client, method)(url, **kwargs)
    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    assert error["loc"] == ["path", "segment_id"]
    assert error["type"] == "uuid_parsing"


# ---------------------------------------------------------------------------
# check_targeting_rules lists them
# ---------------------------------------------------------------------------


def _segment_rows(db_session):
    db_session.expire_all()
    return sorted(
        (str(s.id), s.name, repr(s.rules), s.status.name)
        for s in db_session.query(Segment).all()
    )


def test_check_targeting_rules_lists_segments_whose_rules_are_not_valid(
    db_session, stored, capsys
):
    planted = {name: stored(rules) for name, rules in zip(STORED_IDS, STORED_NOT_VALID)}
    planted["null"] = stored(None)
    clean = stored(US)
    archived = stored(LEGACY, status=ModelSegmentStatus.ARCHIVED)
    tabbed = stored(LEGACY, name="Tab\tand\nnewline")
    before = _segment_rows(db_session)

    assert check_targeting_rules.main([]) == 0

    assert _segment_rows(db_session) == before, "the check wrote to segments"
    out = capsys.readouterr().out.splitlines()
    reasons = {
        "legacy": ("rules", "unknown key"),
        "pe-eq": ("groups[0].conditions[0].operator", "unknown operator"),
        "pe-no-groups": ("groups", "at least one group is required"),
        "pe-empty-group": (
            "groups[0].conditions",
            "at least one condition is required",
        ),
        "pe-groups-text": ("groups", "must be a list"),
        "null": ("rules", "must be an object"),
    }
    for name, (path, reason) in reasons.items():
        segment = planted[name]
        line = (
            f"segment\t{segment.id}\t{segment.name}\t{path}\trules not valid: {reason}"
        )
        assert line in out, (name, out)
    assert (
        f"segment\t{tabbed.id}\tTab and newline\trules\trules not valid: unknown key"
        in out
    )
    for absent in (clean, archived):
        assert not [o for o in out if f"\t{absent.id}\t" in o]
