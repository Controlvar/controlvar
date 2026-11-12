"""Flags and experiments that target a segment (#440), through the routes.

Against a real database, with segments, members, flags and experiments
written directly and evaluated through ``POST /feature-flags/evaluate/{key}``,
``GET /feature-flags/user/{id}`` and ``POST /tracking/assign``:

* rules naming an active segment are accepted on every write route; a
  segment that is unknown, inactive, archived, or whose rules are not valid
  is refused at every rule-writing site (flag create and update, experiment
  create, update and clone, the deprecated wizard);
* membership is decided by the server: a ``$segments`` value in the request
  context is never membership, and only the request's ``user_id`` -- never
  one in the context -- decides id-list and rules-segment membership;
* flags and experiments agree, and a flag agrees with
  ``POST /segments/{id}/evaluate``;
* a segment whose membership cannot be decided fails closed: the flag
  answers ``reason: error`` with no ``error_logs`` row, the experiment
  refuses, for ``in_segment`` and ``not_in_segment`` alike; a database error
  does the same; one WARNING per request names the segments, never the user;
* ``/feature-flags/user/{id}`` resolves every segment once and answers each
  flag as ``/evaluate`` does;
* membership never reaches a stored row.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.segment import Segment, SegmentKind, SegmentMember
from backend.app.models.segment import SegmentStatus as ModelSegmentStatus
from backend.app.services import segment_membership
from backend.app.services.experiment_wizard_service import ExperimentWizardService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
FLAGS = "/api/v1/feature-flags"
EXPERIMENTS = "/api/v1/experiments"
ASSIGN = "/api/v1/tracking/assign"
SEGMENTS = "/api/v1/segments"

MEMBER = "member-440"
OUTSIDER = "outsider-440"

US_RULES = {
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

#: Stored rows whose rules fail ``validate_segment_rules``: the legacy shape
#: and the PE's four dashboard-shaped ones.
INVALID_RULES = {
    "legacy": {
        "operator": "and",
        "conditions": [{"attribute": "country", "operator": "eq", "value": "US"}],
    },
    "eq-operator": {
        "groups": [
            {"conditions": [{"attribute": "country", "operator": "eq", "value": "US"}]}
        ]
    },
    "no-groups": {"groups": []},
    "empty-group": {"groups": [{"conditions": []}]},
    "groups-text": {"groups": "x"},
}


def seg_rules(segment_id: str, op: str = "in_segment") -> Dict[str, Any]:
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "segment", "operator": op, "value": segment_id}
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def no_holdout(db_session):
    """No active global holdout for the duration, restored afterwards."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    yield
    db_session.rollback()
    for row in parked:
        db_session.merge(row).is_active = True
    db_session.commit()


@pytest.fixture
def world(db_session, admin_user, no_holdout):
    """Factories for segments, flags and experiments; everything removed after."""
    made: Dict[str, List[Any]] = {"segments": [], "flags": [], "experiments": []}

    def segment(
        *,
        rules: Any = None,
        members: Optional[List[str]] = None,
        status=ModelSegmentStatus.ACTIVE,
    ) -> str:
        kind = SegmentKind.ID_LIST if members is not None else SegmentKind.RULES
        row = Segment(
            name=f"Seg {uuid.uuid4().hex[:8]}",
            kind=kind.value,
            rules=rules,
            status=status,
            owner_id=admin_user.id,
        )
        db_session.add(row)
        db_session.commit()
        for member in members or []:
            db_session.add(SegmentMember(segment_id=row.id, member_id=member))
        db_session.commit()
        made["segments"].append(row.id)
        return str(row.id)

    def flag(
        rules: Any, rollout: int = 0, status=FeatureFlagStatus.ACTIVE
    ) -> FeatureFlag:
        row = FeatureFlag(
            key=f"seg-flag-{uuid.uuid4().hex[:10]}",
            name="Segment flag",
            status=status,
            owner_id=admin_user.id,
            rollout_percentage=rollout,
            targeting_rules=rules,
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        made["flags"].append(row.id)
        return row

    def experiment(rules: Any, status=ExperimentStatus.ACTIVE) -> Experiment:
        suffix = uuid.uuid4().hex[:10]
        row = Experiment(
            name=f"Segment experiment {suffix}",
            key=f"seg-exp-{suffix}",
            status=status,
            owner_id=admin_user.id,
            description="d",
            hypothesis="h",
            targeting_rules=rules,
        )
        db_session.add(row)
        db_session.commit()
        for i, name in enumerate(("control", "treatment")):
            db_session.add(
                Variant(
                    experiment_id=row.id,
                    name=name,
                    is_control=i == 0,
                    traffic_allocation=50,
                    configuration={},
                )
            )
        db_session.commit()
        db_session.refresh(row)
        made["experiments"].append(row.id)
        return row

    yield type(
        "World", (), {"segment": segment, "flag": flag, "experiment": experiment}
    )
    db_session.rollback()
    if made["experiments"]:
        ids = made["experiments"]
        db_session.query(Event).filter(Event.experiment_id.in_(ids)).delete(False)
        db_session.query(Assignment).filter(Assignment.experiment_id.in_(ids)).delete(
            False
        )
        db_session.query(Variant).filter(Variant.experiment_id.in_(ids)).delete(False)
        db_session.query(Experiment).filter(Experiment.id.in_(ids)).delete(False)
    if made["flags"]:
        ids = made["flags"]
        db_session.query(RawMetric).filter(RawMetric.feature_flag_id.in_(ids)).delete(
            False
        )
        db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id.in_(ids)).delete(
            False
        )
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(ids)).delete(False)
    if made["segments"]:
        db_session.query(Segment).filter(Segment.id.in_(made["segments"])).delete(False)
    db_session.commit()


def evaluate(client, flag: FeatureFlag, user_id: str, context=None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"user_id": user_id}
    if context is not None:
        body["context"] = context
    response = client.post(f"{FLAGS}/evaluate/{flag.key}", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    return {"enabled": data["enabled"], "reason": data["reason"]}


def assign(client, experiment: Experiment, user_id: str, context=None):
    body: Dict[str, Any] = {"experiment_key": experiment.key, "user_id": user_id}
    if context is not None:
        body["context"] = context
    response = client.post(ASSIGN, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    return (data["assigned"], data["reason"])


def error_log_count(db_session) -> int:
    db_session.rollback()
    return db_session.execute(
        text(f"SELECT count(*) FROM {SCHEMA}.error_logs")
    ).scalar()


ON = {"enabled": True, "reason": "targeting_rule"}
OFF = {"enabled": False, "reason": "rollout"}
ERROR = {"enabled": False, "reason": "error"}
ASSIGNED = (True, "assigned")
REFUSED = (False, "targeting")


# ---------------------------------------------------------------------------
# C1: accepted on every write route
# ---------------------------------------------------------------------------


def _experiment_body(rules) -> Dict[str, Any]:
    return {
        "name": f"Seg write {uuid.uuid4().hex[:8]}",
        "description": "d",
        "hypothesis": "h",
        "experiment_type": "a_b",
        "targeting_rules": rules,
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 50},
            {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Conversion",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    }


@pytest.fixture
def cleanup_created(db_session):
    """Remove flags and experiments the routes created in a test."""
    db_session.expire_all()
    flags_before = {row[0] for row in db_session.query(FeatureFlag.id).all()}
    exps_before = {row[0] for row in db_session.query(Experiment.id).all()}
    yield
    db_session.rollback()
    new_flags = [
        r[0] for r in db_session.query(FeatureFlag.id).all() if r[0] not in flags_before
    ]
    new_exps = [
        r[0] for r in db_session.query(Experiment.id).all() if r[0] not in exps_before
    ]
    if new_flags:
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(new_flags)).delete(
            False
        )
    for exp_id in new_exps:
        db_session.execute(
            text(f"DELETE FROM {SCHEMA}.metrics WHERE experiment_id = :e"),
            {"e": exp_id},
        )
        db_session.query(Variant).filter(Variant.experiment_id == exp_id).delete(False)
        db_session.query(Experiment).filter(Experiment.id == exp_id).delete(False)
    db_session.commit()


@pytest.mark.parametrize("op", ["in_segment", "not_in_segment"])
def test_rules_naming_an_active_segment_are_accepted(
    admin_client, world, cleanup_created, op
):
    id_list = world.segment(members=[MEMBER])
    rules_segment = world.segment(rules=US_RULES)

    created = admin_client.post(
        f"{FLAGS}/",
        json={
            "key": f"seg-w-{uuid.uuid4().hex[:8]}",
            "name": "n",
            "targeting_rules": seg_rules(id_list, op),
        },
    )
    assert created.status_code == 201, created.text
    updated = admin_client.put(
        f"{FLAGS}/{created.json()['id']}",
        json={"targeting_rules": seg_rules(rules_segment, op)},
    )
    assert updated.status_code == 200, updated.text

    exp = admin_client.post(
        f"{EXPERIMENTS}/", json=_experiment_body(seg_rules(id_list, op))
    )
    assert exp.status_code == 201, exp.text
    exp_update = admin_client.put(
        f"{EXPERIMENTS}/{exp.json()['id']}",
        json={"targeting_rules": seg_rules(rules_segment, op)},
    )
    assert exp_update.status_code == 200, exp_update.text
    clone = admin_client.post(f"{EXPERIMENTS}/{exp.json()['id']}/clone")
    assert clone.status_code == 201, clone.text


# ---------------------------------------------------------------------------
# C3: the reference check at every rule-writing site
# ---------------------------------------------------------------------------

REFERENCE_CASES = ["unknown", "inactive", "archived", "invalid-rules"]


def _unusable(world, case: str) -> str:
    if case == "unknown":
        return str(uuid.uuid4())
    if case == "inactive":
        return world.segment(rules=US_RULES, status=ModelSegmentStatus.INACTIVE)
    if case == "archived":
        return world.segment(members=[MEMBER], status=ModelSegmentStatus.ARCHIVED)
    return world.segment(rules=INVALID_RULES["legacy"])


def _expected_message(case: str) -> str:
    reason = (
        "segment rules not valid"
        if case == "invalid-rules"
        else "segment not found or not active"
    )
    return f"groups[0].conditions[0].value: {reason}"


def _assert_422(response, case: str, segment_id: str) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {
            "loc": ["body", "targeting_rules"],
            "msg": _expected_message(case),
            "type": "value_error",
        }
    ]
    assert segment_id not in response.text


@pytest.mark.parametrize("case", REFERENCE_CASES)
def test_reference_refused_on_flag_create(
    admin_client, world, cleanup_created, db_session, case
):
    segment_id = _unusable(world, case)
    key = f"seg-r-{uuid.uuid4().hex[:8]}"
    response = admin_client.post(
        f"{FLAGS}/",
        json={"key": key, "name": "n", "targeting_rules": seg_rules(segment_id)},
    )
    _assert_422(response, case, segment_id)
    db_session.rollback()
    assert db_session.query(FeatureFlag).filter_by(key=key).count() == 0


@pytest.mark.parametrize("case", REFERENCE_CASES)
def test_reference_refused_on_flag_update(admin_client, world, case):
    segment_id = _unusable(world, case)
    flag = world.flag(None, rollout=10)
    response = admin_client.put(
        f"{FLAGS}/{flag.id}", json={"targeting_rules": seg_rules(segment_id)}
    )
    _assert_422(response, case, segment_id)


@pytest.mark.parametrize("case", REFERENCE_CASES)
def test_reference_refused_on_experiment_create(
    admin_client, world, cleanup_created, db_session, case
):
    segment_id = _unusable(world, case)
    body = _experiment_body(seg_rules(segment_id))
    response = admin_client.post(f"{EXPERIMENTS}/", json=body)
    _assert_422(response, case, segment_id)
    db_session.rollback()
    assert db_session.query(Experiment).filter_by(name=body["name"]).count() == 0


@pytest.mark.parametrize("case", REFERENCE_CASES)
def test_reference_refused_on_experiment_update(admin_client, world, case):
    segment_id = _unusable(world, case)
    experiment = world.experiment(None, status=ExperimentStatus.DRAFT)
    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment.id}",
        json={"targeting_rules": seg_rules(segment_id)},
    )
    _assert_422(response, case, segment_id)


@pytest.mark.parametrize("case", REFERENCE_CASES)
def test_reference_refused_on_clone(admin_client, world, cleanup_created, case):
    segment_id = _unusable(world, case)
    # Stored directly: a completed experiment may keep a segment that was
    # archived afterwards.
    source = world.experiment(seg_rules(segment_id), status=ExperimentStatus.COMPLETED)
    response = admin_client.post(f"{EXPERIMENTS}/{source.id}/clone")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "The experiment cannot be cloned: its targeting rules use a segment that is "
        f"not active or whose rules are not valid ({_expected_message(case)}). "
        "Create the experiment again with rules that use active segments."
    )


@pytest.mark.parametrize("case", REFERENCE_CASES)
def test_reference_refused_on_wizard_submit(test_db, world, admin_user, case):
    from sqlalchemy.orm import sessionmaker

    segment_id = _unusable(world, case)
    session = sessionmaker(bind=test_db)()
    session.execute(text(f"SET search_path TO {SCHEMA}"))
    user = str(admin_user.id)
    try:
        draft = ExperimentWizardService.create_draft(user_id=user, experiment_type="ab")
        for step, data in (
            ("choose_type", {"experiment_type": "ab"}),
            (
                "define_hypothesis",
                {
                    "hypothesis": "Segments are checked",
                    "name": f"Seg wizard {uuid.uuid4().hex[:8]}",
                    "primary_metric_id": "purchase",
                },
            ),
            (
                "configure_targeting",
                {
                    "targeting_rules": [
                        {
                            "attribute": "segment",
                            "operator": "in_segment",
                            "value": segment_id,
                        }
                    ]
                },
            ),
        ):
            ExperimentWizardService.update_draft(
                draft_id=draft.id, user_id=user, step=step, data=data
            )
        result = ExperimentWizardService.validate_and_submit(
            draft.id, user_id=user, db=session
        )
    finally:
        session.close()
    assert result["success"] is False, result
    assert result["persisted"] is False
    assert result["errors"] == [f"targeting_rules: {_expected_message(case)}"]


# ---------------------------------------------------------------------------
# C4 / C9: who decides membership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "context",
    [
        {"$segments": "SEG"},
        {"$segments": ["SEG"]},
        {"user": {"$segments": ["SEG"]}},
        {"segment": "SEG"},
    ],
    ids=["text", "list", "nested", "attribute"],
)
def test_a_caller_cannot_supply_membership(admin_client, world, context):
    segment_id = world.segment(members=[MEMBER])
    sent = json.loads(json.dumps(context).replace("SEG", segment_id))
    flag = world.flag(seg_rules(segment_id))
    experiment = world.experiment(seg_rules(segment_id))
    assert evaluate(admin_client, flag, OUTSIDER, sent) == OFF
    assert assign(admin_client, experiment, OUTSIDER, sent) == REFUSED
    assert evaluate(admin_client, flag, MEMBER, sent) == ON
    assert assign(admin_client, experiment, MEMBER, sent) == ASSIGNED


def test_context_user_id_does_not_decide_membership(admin_client, world):
    id_list = world.segment(members=[MEMBER])
    # A rules segment written as a list of ids.
    by_rule = world.segment(
        rules={
            "groups": [
                {
                    "conditions": [
                        {"attribute": "user_id", "operator": "in", "value": [MEMBER]}
                    ]
                }
            ]
        }
    )
    for segment_id in (id_list, by_rule):
        flag = world.flag(seg_rules(segment_id))
        experiment = world.experiment(seg_rules(segment_id))
        for forged in ({"user_id": MEMBER}, {"user": {"user_id": MEMBER}}):
            assert evaluate(admin_client, flag, OUTSIDER, forged) == OFF
            assert assign(admin_client, experiment, OUTSIDER, forged) == REFUSED
        # The request's own user id decides, whatever the context says.
        assert evaluate(admin_client, flag, MEMBER, {"user_id": OUTSIDER}) == ON
        assert assign(admin_client, experiment, MEMBER, {"user_id": OUTSIDER}) == (
            ASSIGNED
        )


# ---------------------------------------------------------------------------
# C7 / C8: parity
# ---------------------------------------------------------------------------


def test_flags_and_experiments_agree(admin_client, world):
    """Segment-only rules: the same eight answers on both routes.

    The rules segment is matched on an aliased attribute (``user.country``
    in the context, ``country`` in the rule), and the experiment's context
    carries no ``segment`` key, so neither a required-attribute check nor an
    attribute lookup may stand between the segment condition and its answer.
    """
    id_list = world.segment(members=[MEMBER])
    rules_segment = world.segment(rules=US_RULES)
    us = {"user": {"country": "US"}}
    de = {"user": {"country": "DE"}}
    rows = [
        (id_list, "in_segment", MEMBER, None, True),
        (id_list, "in_segment", OUTSIDER, None, False),
        (id_list, "not_in_segment", MEMBER, None, False),
        (id_list, "not_in_segment", OUTSIDER, None, True),
        (rules_segment, "in_segment", OUTSIDER, us, True),
        (rules_segment, "in_segment", OUTSIDER, de, False),
        (rules_segment, "not_in_segment", OUTSIDER, us, False),
        (rules_segment, "not_in_segment", OUTSIDER, de, True),
    ]
    flag_answers, experiment_answers, expected = [], [], []
    for segment_id, op, user, context, member in rows:
        flag = world.flag(seg_rules(segment_id, op))
        experiment = world.experiment(seg_rules(segment_id, op))
        flag_answers.append(evaluate(admin_client, flag, user, context) == ON)
        experiment_answers.append(
            assign(admin_client, experiment, user, context) == ASSIGNED
        )
        expected.append(member)
    assert flag_answers == expected
    assert experiment_answers == expected


def test_flag_agrees_with_segment_evaluate(admin_client, world):
    rules_segment = world.segment(
        rules={
            "logical_operator": "OR",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {"attribute": "country", "operator": "equals", "value": "US"},
                        {
                            "attribute": "plan",
                            "operator": "in",
                            "value": ["pro", "team"],
                        },
                    ],
                },
                {
                    "conditions": [
                        {
                            "attribute": "app.version",
                            "operator": "semver_gte",
                            "value": "3.0.0",
                        }
                    ]
                },
            ],
        }
    )
    flag = world.flag(seg_rules(rules_segment))
    contexts = []
    for i in range(60):
        country = ["US", "DE", "FR"][i % 3]
        plan = ["pro", "free", "team", "enterprise"][i % 4]
        version = ["2.9.0", "3.0.0", "3.1.4"][i % 5 % 3]
        # Half nested, half flat: the aliases must apply on both sides.
        if i % 2:
            contexts.append(
                {
                    "user": {"country": country, "plan": plan},
                    "app": {"version": version},
                }
            )
        else:
            contexts.append({"country": country, "plan": plan, "app.version": version})
    flag_members = segment_members = 0
    for i, context in enumerate(contexts):
        if evaluate(admin_client, flag, f"u-{i}", context) == ON:
            flag_members += 1
        response = admin_client.post(
            f"{SEGMENTS}/{rules_segment}/evaluate", json={"user_context": context}
        )
        assert response.status_code == 200, response.text
        segment_members += response.json()["is_member"]
    expected = sum(
        1
        for i in range(60)
        if (
            ["US", "DE", "FR"][i % 3] == "US"
            and ["pro", "free", "team", "enterprise"][i % 4] in ("pro", "team")
        )
        or ["2.9.0", "3.0.0", "3.1.4"][i % 5 % 3] != "2.9.0"
    )
    assert flag_members == segment_members == expected
    assert 0 < expected < 60


# ---------------------------------------------------------------------------
# C10 / C11: fail closed, with no error_logs row
# ---------------------------------------------------------------------------

UNAVAILABLE_CASES = ["unknown", "inactive", "archived", *sorted(INVALID_RULES)]


def _stored_unavailable(world, case: str) -> str:
    if case == "unknown":
        return str(uuid.uuid4())
    if case == "inactive":
        return world.segment(members=[MEMBER], status=ModelSegmentStatus.INACTIVE)
    if case == "archived":
        return world.segment(rules=US_RULES, status=ModelSegmentStatus.ARCHIVED)
    return world.segment(rules=INVALID_RULES[case])


@pytest.mark.parametrize("op", ["in_segment", "not_in_segment"])
@pytest.mark.parametrize("case", UNAVAILABLE_CASES)
def test_unavailable_segment_fails_closed(admin_client, world, db_session, case, op):
    segment_id = _stored_unavailable(world, case)
    flag = world.flag(seg_rules(segment_id, op), rollout=100)
    experiment = world.experiment(seg_rules(segment_id, op))
    before = error_log_count(db_session)
    for user in (MEMBER, OUTSIDER):
        assert evaluate(admin_client, flag, user, {"country": "US"}) == ERROR
        assert assign(admin_client, experiment, user, {"country": "US"}) == REFUSED
    assert error_log_count(db_session) == before


class _FailingQueries:
    """A session whose queries fail as a lost connection does."""

    def __init__(self, db):
        self._db = db
        self.rolled_back = False

    def query(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    def rollback(self):
        self.rolled_back = True
        self._db.rollback()


def test_database_error_fails_closed(admin_client, world, db_session, monkeypatch):
    segment_id = world.segment(members=[MEMBER])
    flag = world.flag(seg_rules(segment_id, "not_in_segment"), rollout=100)
    experiment = world.experiment(seg_rules(segment_id, "not_in_segment"))
    original = segment_membership.resolve_segment_memberships
    sessions: List[_FailingQueries] = []

    def failing_resolve(db, user_id, context, segment_ids):
        sessions.append(_FailingQueries(db))
        return original(sessions[-1], user_id, context, segment_ids)

    for module in ("feature_flag_service", "assignment_service"):
        monkeypatch.setattr(
            f"backend.app.services.{module}.resolve_segment_memberships",
            failing_resolve,
        )
    before = error_log_count(db_session)
    assert evaluate(admin_client, flag, OUTSIDER) == ERROR
    assert assign(admin_client, experiment, OUTSIDER) == REFUSED
    assert len(sessions) == 2 and all(s.rolled_back for s in sessions)
    assert error_log_count(db_session) == before


# ---------------------------------------------------------------------------
# C12: union parity on /feature-flags/user/{id}
# ---------------------------------------------------------------------------


def test_user_flags_union_parity(admin_client, world):
    healthy = world.segment(members=[MEMBER])
    # A stored regex the engine cannot evaluate for a value over its input
    # limit: membership of this segment is unavailable for this context.
    failing = world.segment(
        rules={
            "groups": [
                {
                    "conditions": [
                        {"attribute": "email", "operator": "regex", "value": "^a"}
                    ]
                }
            ]
        }
    )
    context = {"email": "a" * 300}
    on_healthy = world.flag(seg_rules(healthy))
    on_failing = world.flag(seg_rules(failing, "not_in_segment"), rollout=100)
    plain = world.flag(None, rollout=100)

    single = {
        flag.key: evaluate(admin_client, flag, MEMBER, context)["enabled"]
        for flag in (on_healthy, on_failing, plain)
    }
    response = admin_client.get(
        f"{FLAGS}/user/{MEMBER}", params={"context": json.dumps(context)}
    )
    assert response.status_code == 200, response.text
    bulk = response.json()
    assert single == {on_healthy.key: True, on_failing.key: False, plain.key: True}
    assert {key: bulk[key] for key in single} == single


# ---------------------------------------------------------------------------
# C13: one WARNING per request, never the user id
# ---------------------------------------------------------------------------


def _warnings(caplog) -> List[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "backend.app.services.segment_membership"
    ]


def test_one_warning_per_request(admin_client, world, caplog):
    archived = world.segment(members=[MEMBER], status=ModelSegmentStatus.ARCHIVED)
    flag_a = world.flag(seg_rules(archived))
    world.flag(seg_rules(archived, "not_in_segment"))
    experiment = world.experiment(seg_rules(archived))
    user = "warn-user-7731"

    caplog.set_level(logging.WARNING, logger="backend.app.services.segment_membership")
    caplog.clear()
    evaluate(admin_client, flag_a, user, {"email": "x@example.com"})
    [line] = _warnings(caplog)
    assert archived in line and f"flag:{flag_a.key}" in line
    assert user not in line and "x@example.com" not in line

    caplog.clear()
    response = admin_client.get(f"{FLAGS}/user/{user}")
    assert response.status_code == 200
    [line] = _warnings(caplog)
    assert archived in line and user not in line

    caplog.clear()
    assign(admin_client, experiment, user)
    [line] = _warnings(caplog)
    assert archived in line and f"experiment:{experiment.id}" in line
    assert user not in line


# ---------------------------------------------------------------------------
# C14: statement counts
# ---------------------------------------------------------------------------


@contextmanager
def _statements(engine):
    seen: List[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _segment_statements(statements: List[str]) -> int:
    return sum(
        1
        for s in statements
        if f"FROM {SCHEMA}.segments" in s or f"FROM {SCHEMA}.segment_members" in s
    )


def _or_rules(*segment_ids: str) -> Dict[str, Any]:
    return {
        "logical_operator": "OR",
        "groups": [
            {
                "logical_operator": "OR",
                "conditions": [
                    {"attribute": "segment", "operator": "in_segment", "value": s}
                    for s in segment_ids
                ],
            }
        ],
    }


def test_statement_counts(admin_client, world, db_session):
    engine = db_session.get_bind()
    id_lists = [world.segment(members=[MEMBER]) for _ in range(5)]
    rule_segments = [world.segment(rules=US_RULES) for _ in range(5)]
    plain = world.flag(
        {
            "groups": [
                {
                    "conditions": [
                        {"attribute": "country", "operator": "equals", "value": "US"}
                    ]
                }
            ]
        }
    )
    one_rules = world.flag(seg_rules(rule_segments[0]))
    mixed_two = world.flag(_or_rules(id_lists[0], rule_segments[0]))
    mixed_ten = world.flag(_or_rules(*id_lists, *rule_segments))

    def count(flag) -> int:
        evaluate(admin_client, flag, MEMBER, {"country": "US"})  # warm up
        with _statements(engine) as seen:
            evaluate(admin_client, flag, MEMBER, {"country": "US"})
        return len(seen), _segment_statements(seen)

    base, base_segment = count(plain)
    assert base_segment == 0
    assert count(one_rules) == (base + 1, 1)
    assert count(mixed_two) == (base + 2, 2)
    assert count(mixed_ten) == (base + 2, 2)

    # /feature-flags/user/{id}: the segments of every flag, resolved once.
    with _statements(engine) as seen:
        response = admin_client.get(
            f"{FLAGS}/user/{MEMBER}", params={"context": json.dumps({"country": "US"})}
        )
    assert response.status_code == 200
    assert _segment_statements(seen) == 2

    # A new assignment: +2 with an id list and a rules segment.
    plain_exp = world.experiment(None)
    seg_exp = world.experiment(_or_rules(id_lists[0], rule_segments[0]))
    with _statements(engine) as seen_plain:
        assign(admin_client, plain_exp, "count-new-1", {"country": "US"})
    with _statements(engine) as seen_seg:
        assign(admin_client, seg_exp, "count-new-2", {"country": "US"})
    assert _segment_statements(seen_seg) == 2
    assert len(seen_seg) == len(seen_plain) + 2


# ---------------------------------------------------------------------------
# C15: membership never stored
# ---------------------------------------------------------------------------


def test_membership_is_not_stored(admin_client, world, monkeypatch):
    """The evaluation records no context at all, and the view event carries
    the request's context exactly: the resolved membership is never written
    into either."""
    from backend.app.services.event_service import EventService
    from backend.app.services.metrics_service import MetricsService

    recorded: List[Any] = []
    real_record = MetricsService.record_flag_evaluation
    real_view = EventService.track_exposure

    def spy_record(*args, **kwargs):
        recorded.append(("metric", kwargs.get("metadata")))
        return real_record(*args, **kwargs)

    def spy_view(self, *args, **kwargs):
        recorded.append(("event", json.loads(json.dumps(kwargs["properties"]))))
        return real_view(self, *args, **kwargs)

    monkeypatch.setattr(MetricsService, "record_flag_evaluation", spy_record)
    monkeypatch.setattr(EventService, "track_exposure", spy_view)
    segment_id = world.segment(members=[MEMBER])
    flag = world.flag(seg_rules(segment_id))
    experiment = world.experiment(seg_rules(segment_id))
    context = {"country": "US", "plan": "pro"}
    assert evaluate(admin_client, flag, MEMBER, context) == ON
    assert assign(admin_client, experiment, MEMBER, context) == ASSIGNED
    assert recorded == [("metric", None), ("event", context)]
