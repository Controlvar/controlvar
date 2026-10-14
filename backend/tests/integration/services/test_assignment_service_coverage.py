"""
Coverage-focused integration tests for AssignmentService.

These tests exercise AssignmentService methods against a real PostgreSQL
database (via the shared `db_session` / factory fixtures from
backend/tests/integration/conftest.py) rather than mocking out the ORM
layer. The goal is behavioural coverage of:

    get_assignment, assign_user, get_user_assignments,
    assign_user_with_targeting, _evaluate_experiment_targeting,
    get_targeting_performance_stats, clear_targeting_metrics, reassign_user,
    _hash_user_to_variant, delete_assignments_by_experiment

Notes on current product behaviour (kept here as documentation, asserted
against explicitly in the relevant tests):

- `EventService.track_event`/`track_exposure` persist a real `Event` row
  (mapping `event_type`, `event_metadata`, `created_at`, etc. correctly),
  so `AssignmentService.*(..., track_exposure=True)` can be exercised
  against the real `EventService` without crashing. Most tests here still
  monkeypatch `service.event_service.track_exposure` to assert on call
  arguments deterministically and to keep tests focused on assignment
  behaviour rather than event persistence; at least one test
  (`TestAssignUser.test_track_exposure_persists_real_event`) verifies a
  real `Event` row is written.
- `AssignmentService.reassign_user` updates the existing `Assignment` row
  in place (variant_id, and context when provided) rather than inserting a
  second row, so calling it repeatedly for the same user succeeds and
  `get_assignment` reflects the latest variant. An invalid forced
  `variant_id` still raises `ValueError` and leaves the existing row
  untouched.
"""

import json
import uuid
from uuid import uuid4

import pytest

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event, EventType
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.schemas.targeting_rule import (
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)
from backend.app.services.assignment_service import AssignmentService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def service(db_session):
    """AssignmentService bound to the shared integration test db_session."""
    return AssignmentService(db_session)


def _active_experiment(make_experiment, make_variant, name, allocations=(50, 50)):
    """Create an ACTIVE experiment with N variants at the given allocations."""
    exp = make_experiment(name=name, status=ExperimentStatus.ACTIVE)
    variants = []
    for i, alloc in enumerate(allocations):
        v = make_variant(
            experiment=exp,
            name=f"Variant-{i}",
            is_control=(i == 0),
            traffic_allocation=alloc,
        )
        variants.append(v)
    return exp, variants


def _uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# get_assignment
# ---------------------------------------------------------------------------


class TestGetAssignment:
    def test_returns_none_when_no_assignment(self, service):
        result = service.get_assignment(_uid("nobody"), uuid4())
        assert result is None

    def test_returns_full_assignment_dict(
        self, service, make_experiment, make_variant, make_assignment
    ):
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "GetAssignment Full Dict"
        )
        user_id = _uid("get-assign")
        make_assignment(experiment=exp, variant=control, user_id=user_id)

        result = service.get_assignment(user_id, exp.id)

        assert result is not None
        assert result["user_id"] == user_id
        assert result["experiment_id"] == str(exp.id)
        assert result["variant_id"] == str(control.id)
        assert result["variant_name"] == control.name
        assert result["is_control"] is True
        assert result["created_at"] is not None
        assert result["updated_at"] is not None


# ---------------------------------------------------------------------------
# assign_user
# ---------------------------------------------------------------------------


class TestAssignUser:
    def test_new_assignment_created(self, service, make_experiment, make_variant):
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "AssignUser New"
        )
        user_id = _uid("assign-new")

        result = service.assign_user(user_id, exp.id, track_exposure=False)

        assert result["user_id"] == user_id
        assert result["experiment_id"] == str(exp.id)
        assert result["variant_id"] in {str(control.id), str(treatment.id)}

        row = (
            service.db.query(Assignment)
            .filter(Assignment.user_id == user_id, Assignment.experiment_id == exp.id)
            .first()
        )
        assert row is not None

    def test_sticky_assignment_returns_same_variant(
        self, service, make_experiment, make_variant
    ):
        exp, _ = _active_experiment(make_experiment, make_variant, "AssignUser Sticky")
        user_id = _uid("assign-sticky")

        first = service.assign_user(user_id, exp.id, track_exposure=False)
        second = service.assign_user(user_id, exp.id, track_exposure=False)

        assert first["variant_id"] == second["variant_id"]

        count = (
            service.db.query(Assignment)
            .filter(Assignment.user_id == user_id, Assignment.experiment_id == exp.id)
            .count()
        )
        assert count == 1

    def test_override_variant_id_used_when_valid(
        self, service, make_experiment, make_variant
    ):
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "AssignUser Override Valid"
        )
        user_id = _uid("assign-override")

        result = service.assign_user(
            user_id, exp.id, track_exposure=False, override_variant_id=treatment.id
        )

        assert result["variant_id"] == str(treatment.id)

    def test_override_variant_id_invalid_raises(
        self, service, make_experiment, make_variant
    ):
        exp, _ = _active_experiment(
            make_experiment, make_variant, "AssignUser Override Invalid"
        )
        user_id = _uid("assign-bad-override")

        with pytest.raises(ValueError, match="not valid for experiment"):
            service.assign_user(
                user_id, exp.id, track_exposure=False, override_variant_id=uuid4()
            )

    def test_experiment_not_found_raises(self, service):
        with pytest.raises(ValueError, match="not found"):
            service.assign_user(_uid("assign-missing"), uuid4(), track_exposure=False)

    def test_inactive_experiment_raises(self, service, make_experiment, make_variant):
        exp = make_experiment(name="AssignUser Inactive", status=ExperimentStatus.DRAFT)
        make_variant(
            experiment=exp, name="Control", is_control=True, traffic_allocation=100
        )

        with pytest.raises(ValueError, match="Cannot assign users"):
            service.assign_user(_uid("assign-inactive"), exp.id, track_exposure=False)

    def test_no_variants_raises(self, service, make_experiment):
        exp = make_experiment(
            name="AssignUser No Variants", status=ExperimentStatus.ACTIVE
        )

        with pytest.raises(ValueError, match="has no variants"):
            service.assign_user(_uid("assign-no-variant"), exp.id, track_exposure=False)

    def test_track_exposure_called_on_new_assignment(
        self, service, make_experiment, make_variant, monkeypatch
    ):
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "AssignUser Exposure New"
        )
        user_id = _uid("assign-exposure-new")

        calls = []

        def fake_track_exposure(user_id, experiment_id, variant_id, properties=None):
            calls.append((user_id, experiment_id, variant_id, properties))
            return {}

        monkeypatch.setattr(
            service.event_service, "track_exposure", fake_track_exposure
        )

        result = service.assign_user(
            user_id, exp.id, track_exposure=True, context={"platform": "web"}
        )

        assert len(calls) == 1
        called_user_id, called_exp_id, called_variant_id, called_props = calls[0]
        assert called_user_id == user_id
        assert called_exp_id == str(exp.id)
        assert called_variant_id == result["variant_id"]
        assert called_props == {"platform": "web"}

    def test_track_exposure_called_on_existing_assignment(
        self, service, make_experiment, make_variant, make_assignment, monkeypatch
    ):
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "AssignUser Exposure Existing"
        )
        user_id = _uid("assign-exposure-existing")
        make_assignment(experiment=exp, variant=control, user_id=user_id)

        calls = []
        monkeypatch.setattr(
            service.event_service,
            "track_exposure",
            lambda **kwargs: calls.append(kwargs),
        )

        result = service.assign_user(user_id, exp.id, track_exposure=True)

        assert result["variant_id"] == str(control.id)
        assert len(calls) == 1
        assert calls[0]["variant_id"] == str(control.id)

    def test_track_exposure_persists_real_event(
        self, service, db_session, make_experiment, make_variant
    ):
        """Exercises the real (non-mocked) EventService.track_exposure path
        and confirms an exposure Event row is actually committed."""
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "AssignUser Real Exposure Event"
        )
        user_id = _uid("assign-real-exposure")

        result = service.assign_user(
            user_id, exp.id, track_exposure=True, context={"platform": "ios"}
        )

        event = (
            db_session.query(Event)
            .filter(Event.experiment_id == exp.id, Event.user_id == user_id)
            .first()
        )
        assert event is not None
        assert event.event_type == EventType.EXPOSURE.value
        assert event.event_name == "variant_exposure"
        assert str(event.variant_id) == result["variant_id"]
        assert event.event_metadata == {"platform": "ios"}


# ---------------------------------------------------------------------------
# get_user_assignments
# ---------------------------------------------------------------------------


class TestGetUserAssignments:
    def test_active_only_filters_inactive_experiments(
        self, service, make_experiment, make_variant, make_assignment
    ):
        active_exp, (active_control, _) = _active_experiment(
            make_experiment, make_variant, "GetUserAssignments Active"
        )
        draft_exp = make_experiment(
            name="GetUserAssignments Draft", status=ExperimentStatus.DRAFT
        )
        draft_control = make_variant(
            experiment=draft_exp,
            name="Control",
            is_control=True,
            traffic_allocation=100,
        )

        user_id = _uid("user-assignments")
        make_assignment(experiment=active_exp, variant=active_control, user_id=user_id)
        make_assignment(experiment=draft_exp, variant=draft_control, user_id=user_id)

        active_only = service.get_user_assignments(user_id, active_only=True)
        all_assignments = service.get_user_assignments(user_id, active_only=False)

        active_exp_ids = {a["experiment_id"] for a in active_only}
        all_exp_ids = {a["experiment_id"] for a in all_assignments}

        assert str(active_exp.id) in active_exp_ids
        assert str(draft_exp.id) not in active_exp_ids
        assert str(active_exp.id) in all_exp_ids
        assert str(draft_exp.id) in all_exp_ids

    def test_returns_expected_fields(
        self, service, make_experiment, make_variant, make_assignment
    ):
        exp, (control, _) = _active_experiment(
            make_experiment, make_variant, "GetUserAssignments Fields"
        )
        user_id = _uid("user-assignments-fields")
        make_assignment(experiment=exp, variant=control, user_id=user_id)

        results = service.get_user_assignments(user_id, active_only=True)
        matching = [r for r in results if r["experiment_id"] == str(exp.id)]

        assert len(matching) == 1
        row = matching[0]
        assert row["experiment_name"] == exp.name
        assert row["variant_name"] == control.name
        assert row["is_control"] is True
        assert row["created_at"] is not None

    def test_no_assignments_returns_empty_list(self, service):
        results = service.get_user_assignments(_uid("nobody-assignments"))
        assert results == []


# ---------------------------------------------------------------------------
# assign_user_with_targeting
# ---------------------------------------------------------------------------

PREMIUM_RULES = {
    "version": "1.0",
    "rules": [
        {
            "id": "premium_users",
            "name": "Premium Users",
            "rule": {
                "operator": "and",
                "conditions": [
                    {"attribute": "plan", "operator": "eq", "value": "premium"}
                ],
            },
            "rollout_percentage": 100,
            "priority": 1,
        }
    ],
}


class TestAssignUserWithTargeting:
    def _experiment_with_rules(
        self, db_session, make_experiment, make_variant, name, rules
    ):
        exp, variants = _active_experiment(make_experiment, make_variant, name)
        exp.targeting_rules = rules
        db_session.commit()
        db_session.refresh(exp)
        return exp, variants

    def test_no_targeting_rules_assigns_everyone(
        self, service, db_session, make_experiment, make_variant
    ):
        exp, _ = _active_experiment(make_experiment, make_variant, "Targeting No Rules")
        user_id = _uid("targeting-no-rules")

        result = service.assign_user_with_targeting(
            user_id, exp.id, user_context={"country": "US"}, track_exposure=False
        )

        assert result["targeting_matched"] is True
        assert result["targeting_rule_id"] is None
        assert result["user_id"] == user_id

    def test_matched_rule_creates_assignment(
        self, service, db_session, make_experiment, make_variant
    ):
        exp, _ = self._experiment_with_rules(
            db_session, make_experiment, make_variant, "Targeting Match", PREMIUM_RULES
        )
        user_id = _uid("targeting-match")

        result = service.assign_user_with_targeting(
            user_id,
            exp.id,
            user_context={"user_id": user_id, "plan": "premium"},
            track_exposure=False,
        )

        assert result["targeting_matched"] is True
        assert result["targeting_rule_id"] == "premium_users"
        assert result["user_context_validated"] is True

        row = (
            db_session.query(Assignment)
            .filter(Assignment.user_id == user_id, Assignment.experiment_id == exp.id)
            .first()
        )
        assert row is not None

    def test_no_match_skips_assignment(
        self, service, db_session, make_experiment, make_variant
    ):
        exp, _ = self._experiment_with_rules(
            db_session,
            make_experiment,
            make_variant,
            "Targeting No Match",
            PREMIUM_RULES,
        )
        user_id = _uid("targeting-no-match")

        result = service.assign_user_with_targeting(
            user_id,
            exp.id,
            user_context={"plan": "basic"},
            track_exposure=False,
        )

        assert result["assignment"] is None
        assert result["targeting_matched"] is False
        assert result["targeting_rule_id"] is None
        assert "No targeting rules matched" in result["reason"]

        row = (
            db_session.query(Assignment)
            .filter(Assignment.user_id == user_id, Assignment.experiment_id == exp.id)
            .first()
        )
        assert row is None

    def test_existing_assignment_short_circuits_targeting(
        self, service, db_session, make_experiment, make_variant, make_assignment
    ):
        exp, variants = self._experiment_with_rules(
            db_session,
            make_experiment,
            make_variant,
            "Targeting Existing",
            PREMIUM_RULES,
        )
        control = variants[0]
        user_id = _uid("targeting-existing")
        make_assignment(experiment=exp, variant=control, user_id=user_id)

        result = service.assign_user_with_targeting(
            user_id,
            exp.id,
            user_context={"plan": "basic"},  # would not match if re-evaluated
            track_exposure=False,
        )

        assert result["variant_id"] == str(control.id)
        assert result["targeting_matched"] is True
        assert result["targeting_rule_id"] == "existing_assignment"

    def test_experiment_not_found_raises(self, service):
        with pytest.raises(ValueError, match="not found"):
            service.assign_user_with_targeting(
                _uid("targeting-missing"), uuid4(), user_context={}
            )

    def test_inactive_experiment_raises(self, service, make_experiment, make_variant):
        exp = make_experiment(name="Targeting Inactive", status=ExperimentStatus.DRAFT)
        make_variant(
            experiment=exp, name="Control", is_control=True, traffic_allocation=100
        )

        with pytest.raises(ValueError, match="Cannot assign users"):
            service.assign_user_with_targeting(
                _uid("targeting-inactive"), exp.id, user_context={}
            )

    @pytest.mark.regression
    def test_attribute_type_does_not_prevent_assignment(
        self, service, db_session, make_experiment, make_variant
    ):
        """A condition's ``attribute_type`` refuses nothing (#1159): ``v1.2.0``
        is enrolled, as a flag matches it, and a value that is not a version
        is simply not matched."""
        version_rules = {
            "version": "1.0",
            "rules": [
                {
                    "id": "version_gate",
                    "rule": {
                        "operator": "and",
                        "conditions": [
                            {
                                "attribute": "app_version",
                                "operator": "semantic_version",
                                "value": "1.2.0",
                                "additional_value": "gte",
                                "attribute_type": "semantic_version",
                            }
                        ],
                    },
                    "rollout_percentage": 100,
                    "priority": 1,
                }
            ],
        }
        exp, _ = self._experiment_with_rules(
            db_session,
            make_experiment,
            make_variant,
            "Targeting Attribute Type",
            version_rules,
        )

        result = service.assign_user_with_targeting(
            _uid("targeting-invalid-version"),
            exp.id,
            user_context={"app_version": "not-a-version"},
            track_exposure=False,
        )
        assert result["assignment"] is None
        assert result["targeting_matched"] is False
        assert result["user_context_validated"] is True

        user_id = _uid("targeting-v-prefixed-version")
        result = service.assign_user_with_targeting(
            user_id,
            exp.id,
            user_context={"app_version": "v1.2.0"},
            track_exposure=False,
        )
        assert result["targeting_matched"] is True
        assert result["targeting_rule_id"] == "version_gate"
        assert result["user_id"] == user_id

    def test_exposure_event_includes_targeting_info(
        self, service, db_session, make_experiment, make_variant, monkeypatch
    ):
        exp, _ = self._experiment_with_rules(
            db_session,
            make_experiment,
            make_variant,
            "Targeting Exposure",
            PREMIUM_RULES,
        )
        user_id = _uid("targeting-exposure")

        calls = []
        monkeypatch.setattr(
            service.event_service,
            "track_exposure",
            lambda **kwargs: calls.append(kwargs),
        )

        service.assign_user_with_targeting(
            user_id,
            exp.id,
            user_context={"plan": "premium"},
            track_exposure=True,
        )

        assert len(calls) == 1
        properties = calls[0]["properties"]
        assert properties["targeting_rule_id"] == "premium_users"
        assert properties["targeting_matched"] is True
        assert properties["plan"] == "premium"

    def test_existing_assignment_tracks_exposure(
        self,
        service,
        db_session,
        make_experiment,
        make_variant,
        make_assignment,
        monkeypatch,
    ):
        """Covers the exposure-tracking branch taken when a user already
        has an assignment (targeting rules are not re-evaluated)."""
        exp, variants = self._experiment_with_rules(
            db_session,
            make_experiment,
            make_variant,
            "Targeting Existing Exposure",
            PREMIUM_RULES,
        )
        control = variants[0]
        user_id = _uid("targeting-existing-exposure")
        make_assignment(experiment=exp, variant=control, user_id=user_id)

        calls = []
        monkeypatch.setattr(
            service.event_service,
            "track_exposure",
            lambda **kwargs: calls.append(kwargs),
        )

        result = service.assign_user_with_targeting(
            user_id,
            exp.id,
            user_context={"plan": "basic"},
            track_exposure=True,
        )

        assert result["targeting_rule_id"] == "existing_assignment"
        assert len(calls) == 1
        assert calls[0]["variant_id"] == str(control.id)
        # `assign_user_with_targeting` mutates user_context in place, adding
        # 'user_id' before it's passed through as exposure properties.
        assert calls[0]["properties"]["plan"] == "basic"
        assert calls[0]["properties"]["user_id"] == user_id


# ---------------------------------------------------------------------------
# _evaluate_experiment_targeting
# ---------------------------------------------------------------------------


class TestEvaluateExperimentTargeting:
    def test_no_rules_defined_eligible(self, service, make_experiment):
        exp = make_experiment(name="Evaluate No Rules")
        exp.targeting_rules = None

        result = service._evaluate_experiment_targeting(exp, {"country": "US"})

        assert result["eligible"] is True
        assert result["rule_id"] is None
        assert "No targeting rules defined" in result["reason"]

    def test_rules_as_json_string_parsed(self, service, make_experiment):
        exp = make_experiment(name="Evaluate JSON String Rules")
        exp.targeting_rules = json.dumps(PREMIUM_RULES)

        result = service._evaluate_experiment_targeting(exp, {"plan": "premium"})

        assert result["eligible"] is True
        assert result["rule_id"] == "premium_users"

    def test_rules_as_dict_parsed(self, service, make_experiment):
        exp = make_experiment(name="Evaluate Dict Rules")
        exp.targeting_rules = PREMIUM_RULES

        result = service._evaluate_experiment_targeting(exp, {"plan": "premium"})

        assert result["eligible"] is True
        assert result["rule_id"] == "premium_users"

    def test_no_matching_rule_not_eligible(self, service, make_experiment):
        exp = make_experiment(name="Evaluate No Match")
        exp.targeting_rules = PREMIUM_RULES

        result = service._evaluate_experiment_targeting(exp, {"plan": "free"})

        assert result["eligible"] is False
        assert result["rule_id"] is None
        assert "No targeting rules matched" in result["reason"]

    def test_rules_already_a_targeting_rules_object(self, service, make_experiment):
        """When experiment.targeting_rules is already a TargetingRules
        instance (neither a JSON string nor a plain dict), the service
        should use it directly instead of re-parsing it."""
        exp = make_experiment(name="Evaluate Already TargetingRules Object")
        rules_obj = TargetingRules(
            version="1.0",
            rules=[
                TargetingRule(
                    id="premium_users",
                    rule=RuleGroup(
                        operator=LogicalOperator.AND,
                        conditions=[
                            Condition(
                                attribute="plan",
                                operator=OperatorType.EQUALS,
                                value="premium",
                            )
                        ],
                    ),
                    rollout_percentage=100,
                    priority=1,
                )
            ],
        )
        # Set directly on the in-memory ORM instance (not persisted/committed)
        # so it stays a TargetingRules object rather than round-tripping
        # through the JSONB column as a dict.
        exp.targeting_rules = rules_obj

        result = service._evaluate_experiment_targeting(exp, {"plan": "premium"})

        assert result["eligible"] is True
        assert result["rule_id"] == "premium_users"

    def test_malformed_rules_returns_error(self, service, make_experiment):
        exp = make_experiment(name="Evaluate Malformed Rules")
        # Missing required 'id' and 'rule' fields for a TargetingRule.
        exp.targeting_rules = {"version": "1.0", "rules": [{"name": "bad rule"}]}

        result = service._evaluate_experiment_targeting(exp, {"plan": "premium"})

        assert result["eligible"] is False
        assert result["rule_id"] is None
        assert "Targeting evaluation error" in result["reason"]


# ---------------------------------------------------------------------------
# get_targeting_performance_stats / clear_targeting_metrics
# ---------------------------------------------------------------------------


class TestTargetingPerformanceStats:
    def test_empty_stats_before_any_evaluation(self, service):
        assert service.get_targeting_performance_stats() == {}

    def test_stats_populated_after_evaluations(self, service, make_experiment):
        exp = make_experiment(name="Perf Stats Populated")
        exp.targeting_rules = PREMIUM_RULES

        for plan in ("premium", "free", "premium"):
            service._evaluate_experiment_targeting(exp, {"plan": plan})

        stats = service.get_targeting_performance_stats()

        assert stats["total_evaluations"] == 3
        assert stats["avg_evaluation_time_ms"] >= 0
        assert 0 <= stats["match_rate"] <= 1

    def test_clear_targeting_metrics_resets_stats(self, service, make_experiment):
        exp = make_experiment(name="Perf Stats Clear")
        exp.targeting_rules = PREMIUM_RULES

        service._evaluate_experiment_targeting(exp, {"plan": "premium"})
        assert service.get_targeting_performance_stats() != {}

        service.clear_targeting_metrics()

        assert service.get_targeting_performance_stats() == {}


# ---------------------------------------------------------------------------
# reassign_user
# ---------------------------------------------------------------------------


class TestReassignUser:
    def test_reassign_new_user_with_explicit_variant(
        self, service, make_experiment, make_variant
    ):
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "Reassign New User"
        )
        user_id = _uid("reassign-new")

        result = service.reassign_user(
            user_id, exp.id, variant_id=treatment.id, track_exposure=False
        )

        assert result["variant_id"] == str(treatment.id)

    def test_reassign_invalid_variant_raises(
        self, service, make_experiment, make_variant
    ):
        exp, _ = _active_experiment(
            make_experiment, make_variant, "Reassign Invalid Variant"
        )
        user_id = _uid("reassign-invalid-variant")

        with pytest.raises(ValueError, match="not valid for experiment"):
            service.reassign_user(
                user_id, exp.id, variant_id=uuid4(), track_exposure=False
            )

    def test_reassign_experiment_not_found_raises(self, service):
        with pytest.raises(ValueError, match="not found"):
            service.reassign_user(
                _uid("reassign-missing"), uuid4(), track_exposure=False
            )

    def test_reassign_uses_hash_when_no_variant_given(
        self, service, make_experiment, make_variant
    ):
        exp, _ = _active_experiment(
            make_experiment, make_variant, "Reassign Hash Default"
        )
        user_id = _uid("reassign-hash")

        result = service.reassign_user(user_id, exp.id, track_exposure=False)

        assert result is not None
        assert result["user_id"] == user_id

    def test_reassign_already_assigned_user_updates_existing_row(
        self, service, db_session, make_experiment, make_variant
    ):
        """reassign_user updates the existing Assignment row in place
        rather than inserting a second row for the same (experiment, user)
        pair. A subsequent invalid forced variant_id still raises
        ValueError and leaves the row untouched."""
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "Reassign Update In Place"
        )
        user_id = _uid("reassign-update")

        first = service.reassign_user(
            user_id, exp.id, variant_id=control.id, track_exposure=False
        )
        assert first["variant_id"] == str(control.id)

        second = service.reassign_user(
            user_id, exp.id, variant_id=treatment.id, track_exposure=False
        )

        assert second["variant_id"] == str(treatment.id)
        assert second["id"] == first["id"]  # same row updated, not a new one

        rows = (
            db_session.query(Assignment)
            .filter(Assignment.user_id == user_id, Assignment.experiment_id == exp.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].variant_id == treatment.id

        # A forced invalid variant still raises and leaves the row alone.
        with pytest.raises(ValueError, match="not valid for experiment"):
            service.reassign_user(
                user_id, exp.id, variant_id=uuid4(), track_exposure=False
            )

        unchanged = (
            db_session.query(Assignment)
            .filter(Assignment.user_id == user_id, Assignment.experiment_id == exp.id)
            .one()
        )
        assert unchanged.variant_id == treatment.id

    def test_reassign_updates_context_when_provided(
        self, service, db_session, make_experiment, make_variant
    ):
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "Reassign Context Update"
        )
        user_id = _uid("reassign-context")

        service.reassign_user(
            user_id,
            exp.id,
            variant_id=control.id,
            track_exposure=False,
            context={"platform": "web"},
        )
        service.reassign_user(
            user_id,
            exp.id,
            variant_id=treatment.id,
            track_exposure=False,
            context={"platform": "ios"},
        )

        row = (
            db_session.query(Assignment)
            .filter(Assignment.user_id == user_id, Assignment.experiment_id == exp.id)
            .one()
        )
        assert row.variant_id == treatment.id
        assert row.context == {"platform": "ios"}

    def test_reassign_tracks_exposure(
        self, service, make_experiment, make_variant, monkeypatch
    ):
        exp, (control, _) = _active_experiment(
            make_experiment, make_variant, "Reassign Exposure"
        )
        user_id = _uid("reassign-exposure")

        calls = []
        monkeypatch.setattr(
            service.event_service,
            "track_exposure",
            lambda **kwargs: calls.append(kwargs),
        )

        service.reassign_user(
            user_id, exp.id, variant_id=control.id, track_exposure=True
        )

        assert len(calls) == 1
        assert calls[0]["variant_id"] == str(control.id)


# ---------------------------------------------------------------------------
# _hash_user_to_variant
# ---------------------------------------------------------------------------


class TestHashUserToVariant:
    def test_deterministic_for_same_user_and_experiment(
        self, service, make_experiment, make_variant
    ):
        exp, _ = _active_experiment(make_experiment, make_variant, "Hash Deterministic")
        user_id = _uid("hash-consistent")

        first = service._hash_user_to_variant(user_id, exp)
        second = service._hash_user_to_variant(user_id, exp)

        assert first == second

    def test_distribution_roughly_matches_traffic_allocation(
        self, service, make_experiment, make_variant
    ):
        exp, (control, treatment) = _active_experiment(
            make_experiment, make_variant, "Hash Distribution", allocations=(50, 50)
        )

        control_count = 0
        treatment_count = 0
        sample_size = 300
        for i in range(sample_size):
            user_id = f"hash-dist-{i}"
            variant_id = service._hash_user_to_variant(user_id, exp)
            if variant_id == control.id:
                control_count += 1
            elif variant_id == treatment.id:
                treatment_count += 1

        assert control_count + treatment_count == sample_size
        # Allow a generous tolerance band around the 50/50 split.
        assert 0.35 * sample_size <= control_count <= 0.65 * sample_size
        assert 0.35 * sample_size <= treatment_count <= 0.65 * sample_size

    def test_raises_when_no_variants(self, service, make_experiment):
        exp = make_experiment(name="Hash No Variants")
        with pytest.raises(ValueError, match="has no variants"):
            service._hash_user_to_variant(_uid("hash-no-variant"), exp)

    def test_fallback_branch_when_allocations_below_100(
        self, service, make_experiment, make_variant
    ):
        """A single variant with traffic_allocation < 100 leaves a hash
        'gap' that only the fallback (variants[-1].id) path can fill."""
        exp, (only_variant,) = _active_experiment(
            make_experiment, make_variant, "Hash Fallback", allocations=(10,)
        )

        for i in range(20):
            user_id = f"hash-fallback-{i}"
            variant_id = service._hash_user_to_variant(user_id, exp)
            assert variant_id == only_variant.id


# ---------------------------------------------------------------------------
# delete_assignments_by_experiment
# ---------------------------------------------------------------------------


class TestDeleteAssignmentsByExperiment:
    def test_deletes_all_assignments_and_returns_count(
        self, service, db_session, make_experiment, make_variant, make_assignment
    ):
        exp, (control, _) = _active_experiment(
            make_experiment, make_variant, "Delete Assignments"
        )
        user_ids = [_uid(f"delete-{i}") for i in range(3)]
        for uid in user_ids:
            make_assignment(experiment=exp, variant=control, user_id=uid)

        deleted_count = service.delete_assignments_by_experiment(exp.id)

        assert deleted_count == 3
        remaining = (
            db_session.query(Assignment)
            .filter(Assignment.experiment_id == exp.id)
            .count()
        )
        assert remaining == 0

    def test_returns_zero_when_no_assignments(self, service, make_experiment):
        exp = make_experiment(name="Delete No Assignments")
        assert service.delete_assignments_by_experiment(exp.id) == 0
