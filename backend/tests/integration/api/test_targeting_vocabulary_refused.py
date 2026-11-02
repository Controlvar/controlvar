"""A save refuses a targeting word its evaluator never matches (#1204), through
the routes.

Each reason of plan D211 S1 is sent to every write route it applies to, and
the 422 is asserted whole: the exact ``loc`` and the exact ``msg``, written
out here rather than imported, so a changed reason fails (EMD-1). The routes:

* ``POST /api/v1/feature-flags/`` (and without the slash), ``PUT
  /api/v1/feature-flags/{flag_id}``;
* ``POST /api/v1/experiments/``, ``PUT /api/v1/experiments/{experiment_id}``;
* ``POST /api/v1/segments``, ``PUT /api/v1/segments/{segment_id}``, and the
  preview, which takes the same body;
* the wizard's ``POST /api/v1/wizard/drafts/{draft_id}/submit``, which reports
  the refusal in ``errors`` and creates nothing.

Nothing is written on a refusal. A request that leaves the rules out is
unaffected; stored rules keep their answers, and one sent back unchanged is
refused (D41). Clone copies stored rules as they are. The reference page's
example window is accepted. ``check_targeting_rules`` lists stored rules a
save now refuses, with the API's path and reason.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any, Dict

import pytest
from sqlalchemy import text

from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.segment import Segment
from backend.app.models.segment import SegmentStatus as ModelSegmentStatus
from backend.scripts import check_targeting_rules
from backend.tests.integration.helpers import unique_flag_key
from backend.tests.unit.core.test_targeting_vocabulary import FRONTEND_FIXTURE
from backend.tests.unit.docs.test_time_window_doc import _example as reference_example

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

FLAGS = "/api/v1/feature-flags"
EXPERIMENTS = "/api/v1/experiments"
SEGMENTS = "/api/v1/segments"

# Plan D211 S1's reasons, verbatim.
NOT_ON_FLAGS = (
    "not supported for feature flags; json_path and percentage_bucket are for "
    "experiments only"
)
VERSION = (
    "unknown version comparison; use eq, gt, gte, lt or lte, or leave it out for eq"
)
LENGTH = (
    "unknown length comparison; use eq, gt, gte, lt, lte or between, or leave it "
    "out for eq"
)
DISTANCE = (
    "unknown distance comparison; use lte, lt, gte, gt or eq, or leave it out for lte"
)
UNIT = "unknown distance unit; use km, mi or m, or leave it out for km"
WINDOW = (
    "unreadable time window; see the time_window example on the Targeting Rules: "
    "Operator Reference page"
)

POINT = [37.7749, -122.4194]


def _native(condition: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rules": [
            {
                "id": "r",
                "rule": {"operator": "and", "conditions": [condition]},
                "rollout_percentage": 100,
                "priority": 0,
            }
        ]
    }


def _cond(operator: str, value: Any, additional: Any = None) -> Dict[str, Any]:
    condition = {"attribute": "a", "operator": operator, "value": value}
    if additional is not None:
        condition["additional_value"] = additional
    return condition


_AT = "rules[0].rule.conditions[0]"

#: One row per reason (and the dashboard shape of the unit), with the kinds it
#: is refused on: name -> (kinds, rules, msg).
ROWS: Dict[str, Any] = {
    "operator": (
        {"flag"},
        _native(_cond("json_path", "$.b", 1)),
        f"Value error, {_AT}.operator: {NOT_ON_FLAGS}",
    ),
    "version comparison": (
        {"flag", "experiment"},
        _native(_cond("semantic_version", "1.2.0", "ne")),
        f"Value error, {_AT}.additional_value: {VERSION}",
    ),
    "length comparison": (
        {"flag", "experiment"},
        _native(_cond("array_length", 2, "within")),
        f"Value error, {_AT}.additional_value: {LENGTH}",
    ),
    "distance comparison": (
        {"flag", "experiment"},
        _native(_cond("geo_distance", POINT, {"radius": 10, "comparison": "within"})),
        f"Value error, {_AT}.additional_value: {DISTANCE}",
    ),
    "distance unit, native": (
        {"flag", "experiment"},
        _native(_cond("geo_distance", POINT, {"radius": 10, "unit": "furlong"})),
        f"Value error, {_AT}.additional_value: {UNIT}",
    ),
    "time window, native": (
        {"flag", "experiment"},
        _native(_cond("time_window", {"start": "09:00", "end": "17:00"})),
        f"Value error, {_AT}.value: {WINDOW}",
    ),
    "distance unit, dashboard": (
        {"flag", "experiment", "segment"},
        FRONTEND_FIXTURE["rules"],
        FRONTEND_FIXTURE["msg"],
    ),
}

#: The dashboard line's msg, written out (the fixture is the frontend's copy).
DASHBOARD_UNIT_MSG = f"Value error, groups[0].conditions[1].value: {UNIT}"


def _rows(kind: str):
    return [name for name, (kinds, _, _) in ROWS.items() if kind in kinds]


def _refused(response, msg: str, field: str = "targeting_rules") -> None:
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", field],
                "msg": msg,
                "ctx": {"error": {}},
            }
        ]
    }


@pytest.fixture(autouse=True)
def _remove_rows_created_here(db_session):
    """Delete the flags and segments each test made (the suite shares one
    database, and list pages other tests read would fill with them)."""
    db_session.expire_all()
    flags = {row[0] for row in db_session.query(FeatureFlag.id).all()}
    segments = {row[0] for row in db_session.query(Segment.id).all()}
    yield
    db_session.rollback()
    new_flags = [
        r[0] for r in db_session.query(FeatureFlag.id).all() if r[0] not in flags
    ]
    if new_flags:
        for model in (RawMetric, ErrorLog):
            db_session.query(model).filter(model.feature_flag_id.in_(new_flags)).delete(
                synchronize_session=False
            )
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(new_flags)).delete(
            synchronize_session=False
        )
    new_segments = [
        r[0] for r in db_session.query(Segment.id).all() if r[0] not in segments
    ]
    if new_segments:
        db_session.query(Segment).filter(Segment.id.in_(new_segments)).delete(
            synchronize_session=False
        )
    db_session.commit()


def _experiment_body(name: str, rules: Any) -> Dict[str, Any]:
    return {
        "name": name,
        "description": "Targeting vocabulary refused on save",
        "hypothesis": "A word the evaluator never matches is refused",
        "experiment_type": "a_b",
        "targeting_rules": rules,
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 50},
            {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Conversion Rate",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    }


def _count(db_session, model, **where) -> int:
    db_session.expire_all()
    return db_session.query(model).filter_by(**where).count()


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("collection", [f"{FLAGS}/", FLAGS], ids=["slash", "bare"])
@pytest.mark.parametrize("name", _rows("flag"))
def test_flag_create_refuses_the_word(admin_client, db_session, collection, name):
    _, rules, msg = ROWS[name]
    key = unique_flag_key("vocab")
    response = admin_client.post(
        collection,
        json={
            "key": key,
            "name": "Vocabulary",
            "targeting_rules": copy.deepcopy(rules),
        },
    )
    _refused(response, msg)
    assert _count(db_session, FeatureFlag, key=key) == 0


@pytest.mark.regression
@pytest.mark.parametrize("name", _rows("flag"))
def test_flag_update_refuses_the_word_and_writes_nothing(
    admin_client, db_session, make_feature_flag, name
):
    _, rules, msg = ROWS[name]
    stored = {
        "logical_operator": "AND",
        "groups": [
            {
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "US"}
                ]
            }
        ],
    }
    flag = make_feature_flag(targeting_rules=stored, rollout_percentage=10)
    response = admin_client.put(
        f"{FLAGS}/{flag.id}",
        json={"targeting_rules": copy.deepcopy(rules), "rollout_percentage": 90},
    )
    _refused(response, msg)
    db_session.expire_all()
    row = db_session.get(FeatureFlag, flag.id)
    assert (row.targeting_rules, row.rollout_percentage) == (stored, 10)


@pytest.mark.regression
def test_a_stored_flag_keeps_its_answer_and_cannot_be_sent_back(
    admin_client, db_session, make_feature_flag
):
    """Stored before #1204: still read and evaluated as before (the json_path
    condition does not hold, so the 0% rollout decides); a GET body sent back
    unchanged answers 422; leaving the rules out still saves (D41)."""
    _, stored, msg = ROWS["operator"]
    flag = make_feature_flag(
        key=unique_flag_key("vocab-stored"),
        targeting_rules=copy.deepcopy(stored),
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
    )
    evaluated = admin_client.get(
        f"{FLAGS}/evaluate/{flag.key}",
        params={"user_id": "u-1204", "context": json.dumps({"a": {"b": 1}})},
    )
    assert evaluated.status_code == 200, evaluated.text
    assert (evaluated.json()["enabled"], evaluated.json()["reason"]) == (
        False,
        "rollout",
    )

    body = admin_client.get(f"{FLAGS}/{flag.id}").json()
    assert body["targeting_rules"] == stored
    _refused(admin_client.put(f"{FLAGS}/{flag.id}", json=body), msg)

    without = {k: v for k, v in body.items() if k != "targeting_rules"}
    without["rollout_percentage"] = 30
    saved = admin_client.put(f"{FLAGS}/{flag.id}", json=without)
    assert saved.status_code == 200, saved.text
    db_session.expire_all()
    row = db_session.get(FeatureFlag, flag.id)
    assert (row.targeting_rules, row.rollout_percentage) == (stored, 30)


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("name", _rows("experiment"))
def test_experiment_create_refuses_the_word(admin_client, db_session, name):
    _, rules, msg = ROWS[name]
    title = f"Vocabulary {uuid.uuid4().hex[:8]}"
    response = admin_client.post(
        f"{EXPERIMENTS}/", json=_experiment_body(title, copy.deepcopy(rules))
    )
    _refused(response, msg)
    assert _count(db_session, Experiment, name=title) == 0


@pytest.mark.regression
@pytest.mark.parametrize("name", _rows("experiment"))
def test_experiment_update_refuses_the_word_and_writes_nothing(
    admin_client, db_session, name
):
    _, rules, msg = ROWS[name]
    created = admin_client.post(
        f"{EXPERIMENTS}/",
        json=_experiment_body(f"Vocabulary {uuid.uuid4().hex[:8]}", None),
    )
    assert created.status_code == 201, created.text
    response = admin_client.put(
        f"{EXPERIMENTS}/{created.json()['id']}",
        json={"targeting_rules": copy.deepcopy(rules)},
    )
    _refused(response, msg)
    db_session.expire_all()
    assert (
        db_session.get(Experiment, uuid.UUID(created.json()["id"])).targeting_rules
        is None
    )


@pytest.mark.regression
def test_an_experiment_still_accepts_json_path_and_percentage_bucket(admin_client):
    for condition in (
        _cond("json_path", "$.b", 1),
        _cond("percentage_bucket", 50),
    ):
        rules = _native(condition)
        response = admin_client.post(
            f"{EXPERIMENTS}/",
            json=_experiment_body(f"Vocabulary {uuid.uuid4().hex[:8]}", rules),
        )
        assert response.status_code == 201, response.text
        assert response.json()["targeting_rules"] == rules


@pytest.mark.regression
def test_clone_copies_stored_rules_a_save_now_refuses(admin_client, db_session):
    """(1f) Clone is not validated, as before (#523)."""
    created = admin_client.post(
        f"{EXPERIMENTS}/",
        json=_experiment_body(f"Vocabulary {uuid.uuid4().hex[:8]}", None),
    )
    assert created.status_code == 201, created.text
    source_id = uuid.UUID(created.json()["id"])
    _, stored, _ = ROWS["version comparison"]
    db_session.expire_all()
    db_session.get(Experiment, source_id).targeting_rules = copy.deepcopy(stored)
    db_session.commit()

    response = admin_client.post(f"{EXPERIMENTS}/{source_id}/clone")

    assert response.status_code == 201, response.text
    assert response.json()["targeting_rules"] == stored
    db_session.expire_all()
    clone = db_session.get(Experiment, uuid.UUID(response.json()["id"]))
    assert clone.targeting_rules == stored


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

US_SEGMENT = {
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


def _segment(admin_client) -> Dict[str, Any]:
    created = admin_client.post(
        SEGMENTS, json={"name": f"Seg {uuid.uuid4().hex[:8]}", "rules": US_SEGMENT}
    )
    assert created.status_code == 201, created.text
    return created.json()


@pytest.mark.regression
def test_segment_create_update_and_preview_refuse_an_unknown_unit(
    admin_client, db_session
):
    _, rules, msg = ROWS["distance unit, dashboard"]
    assert msg == DASHBOARD_UNIT_MSG
    name = f"Seg {uuid.uuid4().hex[:8]}"
    _refused(
        admin_client.post(SEGMENTS, json={"name": name, "rules": rules}), msg, "rules"
    )
    assert _count(db_session, Segment, name=name) == 0

    segment = _segment(admin_client)
    _refused(
        admin_client.put(f"{SEGMENTS}/{segment['id']}", json={"rules": rules}),
        msg,
        "rules",
    )
    assert admin_client.get(f"{SEGMENTS}/{segment['id']}").json()["rules"] == US_SEGMENT

    _refused(
        admin_client.post(
            f"{SEGMENTS}/{segment['id']}/preview",
            json={"name": "preview", "rules": rules},
        ),
        msg,
        "rules",
    )


@pytest.mark.regression
def test_a_stored_segment_with_an_unknown_unit_is_still_evaluated(
    admin_client, db_session, admin_user
):
    """(1e) Not "rules not valid" (409): the read gate does not run the new
    check, so membership is decided, and the condition does not hold."""
    _, rules, _ = ROWS["distance unit, dashboard"]
    segment = Segment(
        name=f"Stored {uuid.uuid4().hex[:8]}",
        rules=copy.deepcopy(rules),
        status=ModelSegmentStatus.ACTIVE,
        owner_id=admin_user.id,
    )
    db_session.add(segment)
    db_session.commit()
    response = admin_client.post(
        f"{SEGMENTS}/{segment.id}/evaluate",
        json={"user_context": {"country": "US", "location": POINT}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_member"] is False


# ---------------------------------------------------------------------------
# The wizard launch
# ---------------------------------------------------------------------------


def _step(client, draft_id: str, step: str, data: Dict[str, Any]) -> None:
    response = client.put(
        f"/api/v1/wizard/drafts/{draft_id}/step", json={"step": step, "data": data}
    )
    assert response.status_code == 200, response.text


@pytest.mark.regression
def test_the_wizard_launch_reports_the_refusal_and_creates_nothing(
    admin_client, db_session
):
    title = f"Wizard {uuid.uuid4().hex[:8]}"
    created = admin_client.post("/api/v1/wizard/drafts", json={"experiment_type": "ab"})
    assert created.status_code in (200, 201), created.text
    draft_id = created.json()["id"]
    _step(admin_client, draft_id, "choose_type", {"experiment_type": "ab"})
    _step(
        admin_client,
        draft_id,
        "define_hypothesis",
        {
            "name": title,
            "hypothesis": "A word the evaluator never matches is refused",
            "primary_metric_id": "checkout_completed",
        },
    )
    conditions = FRONTEND_FIXTURE["rules"]["groups"][0]["conditions"]
    _step(admin_client, draft_id, "targeting", {"targeting_rules": conditions})
    _step(admin_client, draft_id, "sample_size", {"baseline_rate": 0.1, "mde": 0.05})

    submitted = admin_client.post(f"/api/v1/wizard/drafts/{draft_id}/submit")

    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["success"] is False
    assert submitted.json()["experiment_id"] is None
    assert submitted.json()["errors"] == [f"targeting_rules: {DASHBOARD_UNIT_MSG}"]
    assert _count(db_session, Experiment, name=title) == 0


# ---------------------------------------------------------------------------
# What stays accepted
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_reference_pages_example_window_is_accepted_on_every_kind(admin_client):
    """(1n) The window the time_window reason points a writer to."""
    example = reference_example()
    condition = {**example, "operator": example["operator"].value}
    flag = admin_client.post(
        f"{FLAGS}/",
        json={
            "key": unique_flag_key("vocab-example"),
            "name": "Example window",
            "targeting_rules": _native(condition),
        },
    )
    assert flag.status_code == 201, flag.text
    experiment = admin_client.post(
        f"{EXPERIMENTS}/",
        json=_experiment_body(f"Example {uuid.uuid4().hex[:8]}", _native(condition)),
    )
    assert experiment.status_code == 201, experiment.text
    dashboard = {"groups": [{"conditions": [condition]}]}
    segment = admin_client.post(
        SEGMENTS, json={"name": f"Seg {uuid.uuid4().hex[:8]}", "rules": dashboard}
    )
    assert segment.status_code == 201, segment.text


# ---------------------------------------------------------------------------
# The check script
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_check_script_lists_what_a_save_now_refuses(
    db_session, make_feature_flag, make_experiment, admin_user, capsys
):
    """(1g) With the API's path and reason, and nothing is written."""
    json_path = make_feature_flag(targeting_rules=ROWS["operator"][1])
    window = make_feature_flag(targeting_rules=ROWS["time window, native"][1])
    suffix = uuid.uuid4().hex[:8]
    experiment = make_experiment(
        name=f"Vocabulary {suffix}",
        key=f"vocab-{suffix}",
        status=ExperimentStatus.ACTIVE,
        targeting_rules=copy.deepcopy(ROWS["version comparison"][1]),
    )
    segment = Segment(
        name=f"Stored {suffix}",
        rules=copy.deepcopy(ROWS["distance unit, dashboard"][1]),
        status=ModelSegmentStatus.ACTIVE,
        owner_id=admin_user.id,
    )
    db_session.add(segment)
    db_session.commit()
    flag_lines = {
        f"feature_flag\t{json_path.id}\t{json_path.key}\t{_AT}.operator\t{NOT_ON_FLAGS}",
        f"feature_flag\t{window.id}\t{window.key}\t{_AT}.value\t{WINDOW}",
    }
    experiment_line = (
        f"experiment\t{experiment.id}\t{experiment.key}\tACTIVE\t"
        f"{_AT}.additional_value\t{VERSION}"
    )
    segment_line = (
        f"segment\t{segment.id}\t{segment.name}\tgroups[0].conditions[1].value\t{UNIT}"
    )
    experiment_id = experiment.id

    def rows():
        db_session.expire_all()
        return [
            tuple(r)
            for r in db_session.execute(
                text(
                    "SELECT 'f', id::text, targeting_rules::text FROM feature_flags "
                    "UNION ALL SELECT 'e', id::text, targeting_rules::text FROM "
                    "experiments UNION ALL SELECT 's', id::text, rules::text FROM "
                    "segments ORDER BY 1, 2"
                )
            ).all()
        ]

    before = rows()
    try:
        assert check_targeting_rules.main([]) == 0
        assert rows() == before, "the check wrote"
    finally:
        # An ACTIVE experiment left behind would be read by other tests.
        db_session.rollback()
        db_session.query(Experiment).filter(Experiment.id == experiment_id).delete(
            synchronize_session=False
        )
        db_session.commit()
    out = set(capsys.readouterr().out.splitlines())

    assert flag_lines <= out
    assert experiment_line in out
    assert segment_line in out
