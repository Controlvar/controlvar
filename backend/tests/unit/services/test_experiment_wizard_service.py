"""
Unit tests for ExperimentWizardService.

Tests cover wizard step validation, draft management, and experiment creation.
No real database required — uses in-memory draft store.
"""

import uuid

import pytest

from backend.app.schemas.experiment import ExperimentCreate
from backend.app.services.experiment_wizard_service import (
    EXPERIMENT_TYPES,
    WIZARD_STEPS,
    ExperimentWizardService,
    WizardDraft,
    WizardValidationResult,
    _drafts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_drafts():
    """Clear the in-memory draft store between tests."""
    _drafts.clear()


# ---------------------------------------------------------------------------
# TestWizardValidation
# ---------------------------------------------------------------------------


class TestWizardValidation:
    """Tests for ExperimentWizardService.validate_wizard_step."""

    def setup_method(self):
        _clear_drafts()

    def test_validate_returns_wizard_validation_result(self):
        """validate_wizard_step returns a WizardValidationResult object."""
        result = ExperimentWizardService.validate_wizard_step(
            "choose_type", {"experiment_type": "ab"}
        )
        assert isinstance(result, WizardValidationResult)
        assert hasattr(result, "is_valid")
        assert hasattr(result, "errors")

    def test_choose_type_valid_ab(self):
        """Step 'choose_type': type 'ab' is valid."""
        result = ExperimentWizardService.validate_wizard_step(
            "choose_type", {"experiment_type": "ab"}
        )
        assert result.is_valid is True
        assert result.errors == []

    def test_choose_type_valid_multivariate(self):
        """Step 'choose_type': type 'multivariate' is valid."""
        result = ExperimentWizardService.validate_wizard_step(
            "choose_type", {"experiment_type": "multivariate"}
        )
        assert result.is_valid is True

    def test_choose_type_valid_feature_flag_rollout(self):
        """Step 'choose_type': type 'feature_flag_rollout' is valid."""
        result = ExperimentWizardService.validate_wizard_step(
            "choose_type", {"experiment_type": "feature_flag_rollout"}
        )
        assert result.is_valid is True

    def test_choose_type_invalid_returns_error(self):
        """Step 'choose_type': invalid type returns error."""
        result = ExperimentWizardService.validate_wizard_step(
            "choose_type", {"experiment_type": "invalid_type"}
        )
        assert result.is_valid is False
        assert len(result.errors) > 0
        assert any("experiment_type" in e for e in result.errors)

    def test_choose_type_empty_returns_error(self):
        """Step 'choose_type': missing type returns error."""
        result = ExperimentWizardService.validate_wizard_step("choose_type", {})
        assert result.is_valid is False

    def test_define_hypothesis_requires_hypothesis_min_10_chars(self):
        """Step 'define_hypothesis': hypothesis must be at least 10 characters."""
        result = ExperimentWizardService.validate_wizard_step(
            "define_hypothesis",
            {"hypothesis": "Too short", "primary_metric_id": "metric-123"},
        )
        assert result.is_valid is False
        assert any("hypothesis" in e and "10" in e for e in result.errors)

    def test_define_hypothesis_requires_primary_metric_id(self):
        """Step 'define_hypothesis': primary_metric_id is required."""
        result = ExperimentWizardService.validate_wizard_step(
            "define_hypothesis",
            {
                "hypothesis": "We believe adding a CTA button will increase conversions.",
                "primary_metric_id": None,
            },
        )
        assert result.is_valid is False
        assert any("primary_metric_id" in e for e in result.errors)

    def test_define_hypothesis_valid(self):
        """Step 'define_hypothesis': valid data passes."""
        result = ExperimentWizardService.validate_wizard_step(
            "define_hypothesis",
            {
                "hypothesis": "We believe adding a prominent CTA button will increase conversions by 10%.",
                "primary_metric_id": "metric-abc-123",
            },
        )
        assert result.is_valid is True
        assert result.errors == []

    def test_targeting_empty_rules_valid(self):
        """Step 'targeting': empty rules are valid (targets all users)."""
        result = ExperimentWizardService.validate_wizard_step(
            "targeting", {"targeting_rules": []}
        )
        assert result.is_valid is True
        assert result.errors == []

    def test_targeting_no_rules_valid(self):
        """Step 'targeting': missing targeting_rules key is also valid."""
        result = ExperimentWizardService.validate_wizard_step("targeting", {})
        assert result.is_valid is True

    def test_sample_size_valid(self):
        """Step 'sample_size': valid baseline_rate and mde pass."""
        result = ExperimentWizardService.validate_wizard_step(
            "sample_size", {"baseline_rate": 0.1, "mde": 0.05}
        )
        assert result.is_valid is True

    def test_sample_size_requires_baseline_rate(self):
        """Step 'sample_size': missing baseline_rate returns error."""
        result = ExperimentWizardService.validate_wizard_step(
            "sample_size", {"mde": 0.05}
        )
        assert result.is_valid is False
        assert any("baseline_rate" in e for e in result.errors)

    def test_sample_size_requires_mde(self):
        """Step 'sample_size': missing mde returns error."""
        result = ExperimentWizardService.validate_wizard_step(
            "sample_size", {"baseline_rate": 0.1}
        )
        assert result.is_valid is False
        assert any("mde" in e for e in result.errors)

    def test_sample_size_baseline_must_be_between_0_and_1(self):
        """Step 'sample_size': baseline_rate outside (0,1) returns error."""
        result = ExperimentWizardService.validate_wizard_step(
            "sample_size", {"baseline_rate": 1.5, "mde": 0.05}
        )
        assert result.is_valid is False

    def test_sample_size_mde_must_be_between_0_and_1(self):
        """Step 'sample_size': mde outside (0,1) returns error."""
        result = ExperimentWizardService.validate_wizard_step(
            "sample_size", {"baseline_rate": 0.1, "mde": 1.2}
        )
        assert result.is_valid is False

    def test_review_all_valid_fields(self):
        """Step 'review': all required fields present → is_valid=True."""
        result = ExperimentWizardService.validate_wizard_step(
            "review",
            {
                "experiment_type": "ab",
                "hypothesis": "We believe adding a CTA will increase signups significantly.",
                "primary_metric_id": "metric-xyz",
            },
        )
        assert result.is_valid is True

    def test_review_missing_field_returns_errors(self):
        """Step 'review': missing required field returns is_valid=False with error list."""
        result = ExperimentWizardService.validate_wizard_step(
            "review",
            {
                "experiment_type": "ab",
                # hypothesis missing
                "primary_metric_id": "metric-xyz",
            },
        )
        assert result.is_valid is False
        assert isinstance(result.errors, list)
        assert len(result.errors) > 0
        assert any("hypothesis" in e for e in result.errors)


# ---------------------------------------------------------------------------
# TestWizardDraftManagement
# ---------------------------------------------------------------------------


class TestWizardDraftManagement:
    """Tests for draft creation, retrieval, updating, listing, and submission."""

    def setup_method(self):
        _clear_drafts()

    def test_create_draft_returns_wizard_draft(self):
        """create_draft returns WizardDraft with id and current_step='choose_type'."""
        draft = ExperimentWizardService.create_draft(user_id="user-1")
        assert isinstance(draft, WizardDraft)
        assert draft.id is not None
        assert draft.user_id == "user-1"
        assert draft.current_step == "choose_type"

    def test_create_draft_with_experiment_type(self):
        """create_draft accepts experiment_type parameter."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="multivariate"
        )
        assert draft.experiment_type == "multivariate"

    def test_update_draft_advances_to_next_step(self):
        """update_draft advances current_step to the next wizard step."""
        draft = ExperimentWizardService.create_draft(user_id="user-1")
        assert draft.current_step == "choose_type"

        updated = ExperimentWizardService.update_draft(
            draft.id, draft.user_id, "choose_type", {"experiment_type": "ab"}
        )
        assert updated is not None
        assert updated.current_step == "define_hypothesis"

    def test_update_draft_stores_data(self):
        """update_draft persists data into the draft."""
        draft = ExperimentWizardService.create_draft(user_id="user-1")
        ExperimentWizardService.update_draft(
            draft.id, draft.user_id, "choose_type", {"experiment_type": "ab"}
        )
        ExperimentWizardService.update_draft(
            draft.id,
            draft.user_id,
            "define_hypothesis",
            {
                "hypothesis": "We believe showing a new CTA will improve click-through rates.",
                "primary_metric_id": "m-001",
            },
        )
        retrieved = ExperimentWizardService.get_draft(draft.id, draft.user_id)
        assert retrieved is not None
        assert retrieved.experiment_type == "ab"
        assert retrieved.primary_metric_id == "m-001"

    def test_get_draft_returns_draft_with_accumulated_data(self):
        """get_draft returns draft including all accumulated step data."""
        draft = ExperimentWizardService.create_draft(user_id="user-2")
        ExperimentWizardService.update_draft(
            draft.id, draft.user_id, "choose_type", {"experiment_type": "multivariate"}
        )
        retrieved = ExperimentWizardService.get_draft(draft.id, draft.user_id)
        assert retrieved is not None
        assert retrieved.experiment_type == "multivariate"

    def test_get_draft_returns_none_for_missing_id(self):
        """get_draft returns None for a non-existent draft id."""
        result = ExperimentWizardService.get_draft("nonexistent-id", "user-1")
        assert result is None

    def test_validate_and_submit_returns_experiment_id_on_success(self):
        """validate_and_submit returns experiment_id when draft is complete."""
        draft = ExperimentWizardService.create_draft(user_id="user-1")
        ExperimentWizardService.update_draft(
            draft.id, draft.user_id, "choose_type", {"experiment_type": "ab"}
        )
        ExperimentWizardService.update_draft(
            draft.id,
            draft.user_id,
            "define_hypothesis",
            {
                "hypothesis": "We believe adding a new button will increase conversions by 15%.",
                "primary_metric_id": "metric-123",
            },
        )
        # No db: validated dry run, nothing written.
        result = ExperimentWizardService.validate_and_submit(
            draft.id, user_id=draft.user_id
        )
        assert result["success"] is True
        assert result["persisted"] is False
        assert result["experiment_id"] is None
        payload = result["payload"]
        assert payload["experiment_type"] == "a_b"
        assert payload["metrics"][0]["is_primary"] is True
        assert all("traffic_allocation" in v for v in payload["variants"])
        # The draft survives a dry run.
        assert ExperimentWizardService.get_draft(draft.id, draft.user_id) is not None

    def test_validate_and_submit_returns_errors_when_draft_incomplete(self):
        """validate_and_submit returns errors when draft is missing required fields."""
        draft = ExperimentWizardService.create_draft(user_id="user-1")
        # Only chose type, didn't fill hypothesis or metric
        ExperimentWizardService.update_draft(
            draft.id, draft.user_id, "choose_type", {"experiment_type": "ab"}
        )
        result = ExperimentWizardService.validate_and_submit(
            draft.id, user_id=draft.user_id
        )
        assert result["success"] is False
        assert isinstance(result["errors"], list)
        assert len(result["errors"]) > 0

    def test_validate_and_submit_missing_draft_returns_error(self):
        """validate_and_submit with non-existent draft_id returns error."""
        result = ExperimentWizardService.validate_and_submit("bad-id", user_id="user-1")
        assert result["success"] is False
        assert any("not found" in e.lower() for e in result["errors"])

    def test_list_drafts_returns_user_drafts(self):
        """list_drafts returns all drafts belonging to the given user_id."""
        ExperimentWizardService.create_draft(user_id="user-A")
        ExperimentWizardService.create_draft(user_id="user-A")
        ExperimentWizardService.create_draft(user_id="user-B")

        drafts_a = ExperimentWizardService.list_drafts("user-A")
        drafts_b = ExperimentWizardService.list_drafts("user-B")

        assert len(drafts_a) == 2
        assert len(drafts_b) == 1
        assert all(d.user_id == "user-A" for d in drafts_a)

    def test_list_drafts_empty_for_unknown_user(self):
        """list_drafts returns empty list when user has no drafts."""
        drafts = ExperimentWizardService.list_drafts("no-such-user")
        assert drafts == []


# ---------------------------------------------------------------------------
# TestWizardExperimentCreation
# ---------------------------------------------------------------------------


class TestWizardExperimentCreation:
    """Tests for build_experiment_payload."""

    def setup_method(self):
        _clear_drafts()

    def test_build_payload_returns_dict(self):
        """build_experiment_payload returns a dictionary."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="ab"
        )
        draft.hypothesis = (
            "We believe increasing button size will raise click-through rates."
        )
        draft.primary_metric_id = "metric-001"
        payload = ExperimentWizardService.build_experiment_payload(draft)
        assert isinstance(payload, dict)

    def test_build_payload_includes_required_fields(self):
        """build_experiment_payload includes name, hypothesis, variants, metrics, targeting_rules."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="ab"
        )
        draft.hypothesis = (
            "We believe a new headline will improve conversions significantly."
        )
        draft.primary_metric_id = "metric-001"
        payload = ExperimentWizardService.build_experiment_payload(draft)

        assert "name" in payload
        assert "hypothesis" in payload
        assert "variants" in payload
        assert "primary_metric_id" in payload
        assert "targeting_rules" in payload

    def test_ab_type_produces_two_variants(self):
        """build_experiment_payload for A/B type creates exactly 2 variants."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="ab"
        )
        payload = ExperimentWizardService.build_experiment_payload(draft)
        assert len(payload["variants"]) == 2

    def test_ab_variants_include_control_and_treatment(self):
        """build_experiment_payload for A/B type has one control and one treatment."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="ab"
        )
        payload = ExperimentWizardService.build_experiment_payload(draft)
        control = [v for v in payload["variants"] if v.get("is_control") is True]
        treatment = [v for v in payload["variants"] if v.get("is_control") is False]
        assert len(control) == 1
        assert len(treatment) == 1

    def test_multivariate_type_produces_at_least_three_variants(self):
        """build_experiment_payload for multivariate type creates 3+ variants."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="multivariate"
        )
        payload = ExperimentWizardService.build_experiment_payload(draft)
        assert len(payload["variants"]) >= 3

    def test_rollout_type_produces_a_control_and_a_treatment(self):
        """feature_flag_rollout needs a held-back control to measure against.

        A lone 100% "Treatment" arm has no baseline, and every analysis path
        does ``next(v for v in variants if v['is_control'])``.
        """
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="feature_flag_rollout"
        )
        payload = ExperimentWizardService.build_experiment_payload(draft)
        assert len(payload["variants"]) == 2
        assert [v["is_control"] for v in payload["variants"]].count(True) == 1

    def test_rollout_variant_has_traffic_percentage(self):
        """build_experiment_payload for rollout variant includes traffic_percentage."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="feature_flag_rollout"
        )
        payload = ExperimentWizardService.build_experiment_payload(draft)
        assert "traffic_percentage" in payload["variants"][0]

    @pytest.mark.parametrize("wizard_type", sorted(EXPERIMENT_TYPES))
    def test_every_wizard_type_builds_a_payload_ExperimentCreate_accepts(
        self, wizard_type
    ):
        """Exactly one control and allocations summing to 100, for every type."""
        from backend.app.schemas.experiment import ExperimentCreate

        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type=wizard_type
        )
        draft.hypothesis = "We believe this change will lift conversion measurably."
        draft.primary_metric_id = "metric-001"
        payload = ExperimentWizardService.build_experiment_payload(draft)

        assert [v["is_control"] for v in payload["variants"]].count(True) == 1
        assert sum(v["traffic_percentage"] for v in payload["variants"]) == 100

        # Must survive the schema the submit endpoint feeds it to.
        created = ExperimentCreate(**payload)
        assert sum(v.traffic_allocation for v in created.variants) == 100
        assert sum(1 for v in created.variants if v.is_control) == 1

    def test_payload_name_uses_draft_name_if_set(self):
        """build_experiment_payload uses draft.name when provided."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="ab"
        )
        draft.name = "My Custom Experiment"
        payload = ExperimentWizardService.build_experiment_payload(draft)
        assert payload["name"] == "My Custom Experiment"

    @pytest.mark.regression
    def test_payload_saves_the_planned_effect_on_the_primary_metric_only(self):
        """#1257: the sample_size step's mde is the primary metric's expected_effect.

        The results page's Planned sample plans the primary metric for it; the
        guardrails carry none. Before the fix the draft's mde was dropped.
        """
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="ab"
        )
        draft.primary_metric_id = "purchase"
        draft.guardrail_metric_ids = ["refund"]
        draft.baseline_rate = 0.05
        draft.mde = 0.1
        payload = ExperimentWizardService.build_experiment_payload(draft)
        assert [
            (m["name"], m["is_primary"], m.get("expected_effect"))
            for m in payload["metrics"]
        ] == [
            ("purchase", True, 0.1),
            ("refund", False, None),
        ]
        assert "expected_effect" not in payload["metrics"][1]

        # The submit endpoint feeds it to ExperimentCreate, which keeps it.
        created = ExperimentCreate(**payload)
        assert [m.expected_effect for m in created.metrics] == [0.1, None]

    def test_payload_without_a_sample_size_step_plans_nothing(self):
        """No mde in the draft: the primary metric's expected_effect is None (the default plan)."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="ab"
        )
        payload = ExperimentWizardService.build_experiment_payload(draft)
        assert payload["metrics"][0]["expected_effect"] is None
        assert ExperimentCreate(**payload).metrics[0].expected_effect is None

    def test_payload_wraps_targeting_rules_in_the_dashboard_shape(self):
        """The wizard's flat condition list becomes the grouped shape ExperimentCreate takes."""
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="ab"
        )
        conditions = [{"attribute": "country", "operator": "eq", "value": "US"}]
        draft.targeting_rules = conditions
        payload = ExperimentWizardService.build_experiment_payload(draft)
        assert payload["targeting_rules"] == {
            "logical_operator": "and",
            "groups": [{"logical_operator": "and", "conditions": conditions}],
        }

    def test_payload_omits_targeting_rules_when_the_draft_has_none(self):
        draft = ExperimentWizardService.create_draft(
            user_id="user-1", experiment_type="ab"
        )
        assert (
            ExperimentWizardService.build_experiment_payload(draft)["targeting_rules"]
            is None
        )
