"""
Unit tests for LLMExperimentService (EP-046).

Tests CRUD operations, variant assignment, and status transitions without
hitting a real database (uses SQLite in-memory via the unit test conftest).
"""

import uuid
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.llm_experiment import (
    LLMEvaluationMetric,
    LLMExperiment,
    LLMExperimentStatus,
    LLMProvider,
    LLMTaskType,
    LLMVariant,
)
from backend.app.schemas.llm_experiments import (
    CreateLLMExperimentRequest,
    CreateLLMVariantRequest,
    UpdateLLMExperimentRequest,
    UpdateLLMVariantRequest,
)
from backend.app.services.llm_experiment_service import LLMExperimentService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_variant_req(
    name: str = "control",
    is_control: bool = True,
    traffic_split: float = 0.5,
    provider: str = "anthropic",
    model_name: str = "claude-3-5-sonnet-20241022",
    prompt_template: str = "Hello {{topic}}",
) -> CreateLLMVariantRequest:
    return CreateLLMVariantRequest(
        name=name,
        is_control=is_control,
        traffic_split=traffic_split,
        provider=provider,
        model_name=model_name,
        system_prompt="You are a helpful assistant.",
        prompt_template=prompt_template,
        temperature=0.7,
        max_tokens=500,
    )


def _make_experiment_req(
    name: str = "Test LLM Exp",
    task_type: str = "chat_completion",
    evaluation_metric: str = "business_metric",
    variants: List[CreateLLMVariantRequest] = None,
) -> CreateLLMExperimentRequest:
    if variants is None:
        variants = [
            _make_variant_req("control", True, 0.5),
            _make_variant_req("treatment", False, 0.5, model_name="claude-opus-4-6"),
        ]
    return CreateLLMExperimentRequest(
        name=name,
        description="Test description",
        task_type=task_type,
        evaluation_metric=evaluation_metric,
        variants=variants,
    )


def _make_mock_experiment(
    status: LLMExperimentStatus = LLMExperimentStatus.DRAFT,
    num_variants: int = 2,
) -> LLMExperiment:
    """Build an in-memory LLMExperiment object (not persisted)."""
    exp = LLMExperiment()
    exp.id = uuid.uuid4()
    exp.name = "Mock Experiment"
    exp.description = ""
    exp.status = status
    exp.task_type = LLMTaskType.CHAT_COMPLETION
    exp.evaluation_metric = LLMEvaluationMetric.BUSINESS_METRIC

    variants = []
    total = num_variants
    for i in range(total):
        v = LLMVariant()
        v.id = uuid.uuid4()
        v.llm_experiment_id = exp.id
        v.name = "control" if i == 0 else f"treatment_{i}"
        v.is_control = i == 0
        v.traffic_split = 1.0 / total
        v.provider = LLMProvider.ANTHROPIC
        v.model_name = "claude-3-5-sonnet-20241022"
        v.system_prompt = ""
        v.prompt_template = "Hello {{topic}}"
        v.temperature = 0.7
        v.max_tokens = 500
        v.additional_params = {}
        variants.append(v)

    exp.variants = variants
    return exp


# ---------------------------------------------------------------------------
# Service instantiation
# ---------------------------------------------------------------------------


class TestLLMExperimentServiceInit:
    def test_can_instantiate(self):
        svc = LLMExperimentService()
        assert svc is not None


# ---------------------------------------------------------------------------
# Schema validation tests (no DB needed)
# ---------------------------------------------------------------------------


class TestCreateLLMExperimentRequestValidation:
    def test_valid_request_passes(self):
        req = _make_experiment_req()
        assert req.name == "Test LLM Exp"

    def test_all_task_types_accepted(self):
        task_types = [
            "chat_completion",
            "text_generation",
            "classification",
            "summarization",
            "code_generation",
            "embedding",
        ]
        for tt in task_types:
            req = _make_experiment_req(task_type=tt)
            assert req.task_type == tt

    def test_all_evaluation_metrics_accepted(self):
        metrics = [
            "human_rating",
            "latency",
            "cost",
            "accuracy",
            "relevance",
            "fluency",
            "business_metric",
        ]
        for m in metrics:
            req = _make_experiment_req(evaluation_metric=m)
            assert req.evaluation_metric == m

    def test_invalid_task_type_raises(self):
        with pytest.raises(ValueError, match="task_type"):
            CreateLLMExperimentRequest(
                name="x",
                task_type="invalid_task",
                evaluation_metric="latency",
                variants=[
                    _make_variant_req("ctrl", True, 0.5),
                    _make_variant_req("trt", False, 0.5),
                ],
            )

    def test_invalid_evaluation_metric_raises(self):
        with pytest.raises(ValueError, match="evaluation_metric"):
            CreateLLMExperimentRequest(
                name="x",
                task_type="chat_completion",
                evaluation_metric="bad_metric",
                variants=[
                    _make_variant_req("ctrl", True, 0.5),
                    _make_variant_req("trt", False, 0.5),
                ],
            )

    def test_requires_at_least_two_variants(self):
        with pytest.raises(ValueError, match="at least 2"):
            CreateLLMExperimentRequest(
                name="x",
                task_type="chat_completion",
                evaluation_metric="latency",
                variants=[_make_variant_req("ctrl", True, 1.0)],
            )

    def test_requires_exactly_one_control(self):
        with pytest.raises(ValueError, match="Exactly 1"):
            CreateLLMExperimentRequest(
                name="x",
                task_type="chat_completion",
                evaluation_metric="latency",
                variants=[
                    _make_variant_req("ctrl1", True, 0.5),
                    _make_variant_req("ctrl2", True, 0.5),
                ],
            )

    def test_traffic_split_must_sum_to_one(self):
        with pytest.raises(ValueError, match="sum to 1.0"):
            CreateLLMExperimentRequest(
                name="x",
                task_type="chat_completion",
                evaluation_metric="latency",
                variants=[
                    _make_variant_req("ctrl", True, 0.4),
                    _make_variant_req("trt", False, 0.4),
                ],
            )

    def test_traffic_split_out_of_range_raises(self):
        with pytest.raises(ValueError):
            _make_variant_req(traffic_split=1.5)

    def test_traffic_split_negative_raises(self):
        with pytest.raises(ValueError):
            _make_variant_req(traffic_split=-0.1)

    def test_temperature_out_of_range_raises(self):
        with pytest.raises(ValueError, match="temperature"):
            CreateLLMVariantRequest(
                name="v",
                is_control=True,
                traffic_split=0.5,
                provider="openai",
                model_name="gpt-4o",
                prompt_template="Hi",
                temperature=3.0,
            )

    def test_max_tokens_zero_raises(self):
        with pytest.raises(ValueError, match="max_tokens"):
            CreateLLMVariantRequest(
                name="v",
                is_control=True,
                traffic_split=0.5,
                provider="openai",
                model_name="gpt-4o",
                prompt_template="Hi",
                max_tokens=0,
            )


# ---------------------------------------------------------------------------
# CRUD with mock DB session
# ---------------------------------------------------------------------------


class TestLLMExperimentCRUD:
    def _make_mock_db(self, experiment=None):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = experiment
        db.query.return_value.filter.return_value.count.return_value = (
            1 if experiment else 0
        )
        db.query.return_value.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = (
            [experiment] if experiment else []
        )
        db.query.return_value.count.return_value = 1 if experiment else 0
        db.query.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = (
            [experiment] if experiment else []
        )
        return db

    def test_create_experiment_draft_status(self):
        svc = LLMExperimentService()
        db = MagicMock()
        req = _make_experiment_req()

        # Mock DB operations
        exp = _make_mock_experiment()
        db.flush.return_value = None
        db.commit.return_value = None
        db.refresh.return_value = None
        db.add.return_value = None

        # Patch the ORM constructor to return our mock
        with patch(
            "backend.app.services.llm_experiment_service.LLMExperiment"
        ) as MockExp:
            instance = MagicMock()
            instance.id = uuid.uuid4()
            instance.status = LLMExperimentStatus.DRAFT
            instance.variants = []
            MockExp.return_value = instance
            with patch(
                "backend.app.services.llm_experiment_service.LLMVariant"
            ) as MockVar:
                var_instance = MagicMock()
                MockVar.return_value = var_instance
                result = svc.create_experiment(db, req)
                assert result.status == LLMExperimentStatus.DRAFT

    def test_get_experiment_returns_none_when_missing(self):
        svc = LLMExperimentService()
        db = self._make_mock_db(experiment=None)
        result = svc.get_experiment(db, uuid.uuid4())
        assert result is None

    def test_get_experiment_returns_experiment_when_found(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment()
        db = self._make_mock_db(experiment=exp)
        result = svc.get_experiment(db, exp.id)
        assert result is exp

    def test_update_experiment_returns_none_when_missing(self):
        svc = LLMExperimentService()
        db = self._make_mock_db(experiment=None)
        result = svc.update_experiment(
            db, uuid.uuid4(), UpdateLLMExperimentRequest(name="new")
        )
        assert result is None

    def test_update_experiment_changes_name(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment()
        db = self._make_mock_db(experiment=exp)
        db.commit.return_value = None
        db.refresh.return_value = None
        result = svc.update_experiment(
            db, exp.id, UpdateLLMExperimentRequest(name="updated")
        )
        assert exp.name == "updated"

    def test_start_experiment_transitions_to_active(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment(status=LLMExperimentStatus.DRAFT)
        db = self._make_mock_db(experiment=exp)
        db.commit.return_value = None
        db.refresh.return_value = None
        result = svc.start_experiment(db, exp.id)
        assert exp.status == LLMExperimentStatus.ACTIVE

    def test_start_experiment_from_paused(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment(status=LLMExperimentStatus.PAUSED)
        db = self._make_mock_db(experiment=exp)
        db.commit.return_value = None
        db.refresh.return_value = None
        result = svc.start_experiment(db, exp.id)
        assert exp.status == LLMExperimentStatus.ACTIVE

    def test_start_experiment_from_completed_raises(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment(status=LLMExperimentStatus.COMPLETED)
        db = self._make_mock_db(experiment=exp)
        with pytest.raises(ValueError, match="Cannot start"):
            svc.start_experiment(db, exp.id)

    def test_start_experiment_without_variants_raises(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment(status=LLMExperimentStatus.DRAFT, num_variants=0)
        exp.variants = []
        db = self._make_mock_db(experiment=exp)
        with pytest.raises(ValueError, match="without variants"):
            svc.start_experiment(db, exp.id)

    def test_start_experiment_without_control_raises(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment(status=LLMExperimentStatus.DRAFT)
        for v in exp.variants:
            v.is_control = False
        db = self._make_mock_db(experiment=exp)
        with pytest.raises(ValueError, match="control variant"):
            svc.start_experiment(db, exp.id)

    def test_pause_experiment_transitions_to_paused(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment(status=LLMExperimentStatus.ACTIVE)
        db = self._make_mock_db(experiment=exp)
        db.commit.return_value = None
        db.refresh.return_value = None
        result = svc.pause_experiment(db, exp.id)
        assert exp.status == LLMExperimentStatus.PAUSED

    def test_pause_experiment_from_draft_raises(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment(status=LLMExperimentStatus.DRAFT)
        db = self._make_mock_db(experiment=exp)
        with pytest.raises(ValueError, match="Cannot pause"):
            svc.pause_experiment(db, exp.id)

    def test_update_experiment_task_type_enum_coercion(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment()
        db = self._make_mock_db(experiment=exp)
        db.commit.return_value = None
        db.refresh.return_value = None
        svc.update_experiment(
            db, exp.id, UpdateLLMExperimentRequest(task_type="summarization")
        )
        assert exp.task_type == LLMTaskType.SUMMARIZATION

    def test_list_experiments_with_no_filters(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment()
        db = MagicMock()
        db.query.return_value.count.return_value = 1
        db.query.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = [
            exp
        ]
        items, total = svc.list_experiments(db)
        assert total == 1

    def test_list_experiments_with_status_filter(self):
        svc = LLMExperimentService()
        exp = _make_mock_experiment(status=LLMExperimentStatus.ACTIVE)
        db = MagicMock()
        filter_chain = MagicMock()
        filter_chain.filter.return_value = filter_chain
        filter_chain.count.return_value = 1
        filter_chain.order_by.return_value.offset.return_value.limit.return_value.all.return_value = [
            exp
        ]
        db.query.return_value.filter.return_value = filter_chain
        items, total = svc.list_experiments(db, status="ACTIVE")
        assert total == 1


# ---------------------------------------------------------------------------
# Hash bucket tests (pure-function, no DB)
# ---------------------------------------------------------------------------


class TestHashBucket:
    def test_same_inputs_same_output(self):
        b1 = LLMExperimentService._hash_bucket("exp-1", "user-1")
        b2 = LLMExperimentService._hash_bucket("exp-1", "user-1")
        assert b1 == b2

    def test_different_users_different_buckets(self):
        buckets = {
            LLMExperimentService._hash_bucket("exp-1", f"user-{i}") for i in range(100)
        }
        assert len(buckets) > 50  # Not all same

    def test_bucket_in_range(self):
        for i in range(50):
            b = LLMExperimentService._hash_bucket("exp-x", f"u{i}")
            assert 0.0 <= b < 1.0

    def test_bucket_float_type(self):
        b = LLMExperimentService._hash_bucket("exp-1", "user-1")
        assert isinstance(b, float)


# ---------------------------------------------------------------------------
# Variant assignment tests
# ---------------------------------------------------------------------------


class TestVariantAssignment:
    def _make_active_experiment(self, num_variants=2) -> LLMExperiment:
        return _make_mock_experiment(
            status=LLMExperimentStatus.ACTIVE,
            num_variants=num_variants,
        )

    def test_assign_variant_returns_variant(self):
        svc = LLMExperimentService()
        exp = self._make_active_experiment()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = exp
        variant = svc.assign_variant(db, exp.id, "user-1")
        assert isinstance(variant, LLMVariant)

    def test_same_user_always_same_variant(self):
        svc = LLMExperimentService()
        exp = self._make_active_experiment()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = exp
        assignments = [svc.assign_variant(db, exp.id, "user-99") for _ in range(10)]
        assert len({str(a.id) for a in assignments}) == 1

    def test_different_users_get_different_variants(self):
        svc = LLMExperimentService()
        exp = self._make_active_experiment()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = exp
        assigned = [
            str(svc.assign_variant(db, exp.id, f"user-{i}").id) for i in range(200)
        ]
        unique = set(assigned)
        assert len(unique) == 2  # Two variants

    def test_traffic_split_respected_at_scale(self):
        """50/50 split should result in ~50% each over 1000 users."""
        svc = LLMExperimentService()
        exp = self._make_active_experiment(num_variants=2)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = exp

        variant_ids = [v.id for v in exp.variants]
        counts = {str(vid): 0 for vid in variant_ids}

        for i in range(1000):
            v = svc.assign_variant(db, exp.id, f"user-{i}")
            counts[str(v.id)] += 1

        # Each variant should be within 10% of expected 500
        for vid, count in counts.items():
            assert 400 <= count <= 600, (
                f"Variant {vid} got {count} assignments (expected ~500)"
            )

    def test_three_way_split_at_scale(self):
        """33/33/34 split over 900 users stays inside a binomial band.

        Deterministic (#831): the experiment and variant ids were random per
        run, so the buckets, and the counts, changed every run. They are
        fixed here. The band is the 99.9% interval of Binomial(900, 1/3):
        300 +/- 3.29 * sqrt(900 * 1/3 * 2/3) = 300 +/- 46, so a correct split
        cannot fall outside it by chance, while 0 or 50% of the users on one
        variant always does.
        """
        svc = LLMExperimentService()
        exp = _make_mock_experiment(status=LLMExperimentStatus.ACTIVE, num_variants=3)
        exp.id = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
        for i, v in enumerate(exp.variants):
            v.id = uuid.UUID(f"00000000-0000-4000-8000-0000000000a{i}")
            v.llm_experiment_id = exp.id
            v.traffic_split = 1.0 / 3
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = exp

        n, p = 900, 1 / 3
        mean = n * p
        half_width = 3.29 * (n * p * (1 - p)) ** 0.5
        low, high = mean - half_width, mean + half_width

        counts = {str(v.id): 0 for v in exp.variants}
        for i in range(n):
            v = svc.assign_variant(db, exp.id, f"u-{i}")
            counts[str(v.id)] += 1

        assert sum(counts.values()) == n
        for vid, count in counts.items():
            assert low <= count <= high, (
                f"Variant {vid} got {count} of {n} "
                f"(expected {mean:.0f}, band {low:.0f}..{high:.0f}); all: {counts}"
            )

    def test_assign_variant_inactive_experiment_raises(self):
        svc = LLMExperimentService()
        exp = self._make_active_experiment()
        exp.status = LLMExperimentStatus.DRAFT
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = exp
        with pytest.raises(ValueError, match="not ACTIVE"):
            svc.assign_variant(db, exp.id, "user-1")

    def test_assign_variant_missing_experiment_raises(self):
        svc = LLMExperimentService()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(ValueError, match="not found"):
            svc.assign_variant(db, uuid.uuid4(), "user-1")


# ---------------------------------------------------------------------------
# Cost estimation tests
# ---------------------------------------------------------------------------


class TestCostEstimation:
    def test_openai_gpt4o_cost(self):
        from backend.app.services.llm_proxy_service import estimate_cost

        cost = estimate_cost("openai", "gpt-4o", input_tokens=1000, output_tokens=1000)
        assert cost == pytest.approx(0.0025 + 0.010, rel=1e-4)

    def test_anthropic_claude_sonnet_cost(self):
        from backend.app.services.llm_proxy_service import estimate_cost

        cost = estimate_cost(
            "anthropic",
            "claude-3-5-sonnet-20241022",
            input_tokens=1000,
            output_tokens=1000,
        )
        assert cost == pytest.approx(0.003 + 0.015, rel=1e-4)

    def test_google_gemini_flash_cost(self):
        from backend.app.services.llm_proxy_service import estimate_cost

        cost = estimate_cost(
            "google", "gemini-1.5-flash", input_tokens=1000, output_tokens=1000
        )
        assert cost == pytest.approx(0.000075 + 0.0003, rel=1e-4)

    def test_unknown_provider_returns_zero(self):
        from backend.app.services.llm_proxy_service import estimate_cost

        cost = estimate_cost("unknown_provider", "unknown-model", 500, 500)
        assert cost == 0.0

    def test_unknown_model_returns_zero(self):
        from backend.app.services.llm_proxy_service import estimate_cost

        cost = estimate_cost("openai", "nonexistent-model", 500, 500)
        assert cost == 0.0

    def test_zero_tokens_zero_cost(self):
        from backend.app.services.llm_proxy_service import estimate_cost

        cost = estimate_cost("openai", "gpt-4o", 0, 0)
        assert cost == 0.0

    def test_cost_scales_with_tokens(self):
        from backend.app.services.llm_proxy_service import estimate_cost

        cost1 = estimate_cost("anthropic", "claude-opus-4-6", 500, 500)
        cost2 = estimate_cost("anthropic", "claude-opus-4-6", 1000, 1000)
        assert cost2 == pytest.approx(cost1 * 2, rel=1e-4)


# ---------------------------------------------------------------------------
# Prompt template rendering
# ---------------------------------------------------------------------------


class TestPromptRendering:
    def test_render_with_single_variable(self):
        from backend.app.services.llm_proxy_service import render_prompt_template

        result = render_prompt_template("Hello {{name}}!", {"name": "World"})
        assert result == "Hello World!"

    def test_render_with_multiple_variables(self):
        from backend.app.services.llm_proxy_service import render_prompt_template

        result = render_prompt_template(
            "{{greeting}} {{name}}, you are {{age}} years old.",
            {"greeting": "Hi", "name": "Alice", "age": 30},
        )
        assert result == "Hi Alice, you are 30 years old."

    def test_render_missing_variable_raises(self):
        from backend.app.services.llm_proxy_service import render_prompt_template

        with pytest.raises(ValueError, match="Missing variables"):
            render_prompt_template("Hello {{name}} from {{city}}!", {"name": "Bob"})

    def test_render_no_variables_returns_template(self):
        from backend.app.services.llm_proxy_service import render_prompt_template

        result = render_prompt_template("Just static text", {})
        assert result == "Just static text"

    def test_render_extra_variables_ignored(self):
        from backend.app.services.llm_proxy_service import render_prompt_template

        result = render_prompt_template(
            "Hello {{name}}!", {"name": "Eve", "extra": "ignored"}
        )
        assert result == "Hello Eve!"

    def test_render_numeric_variable(self):
        from backend.app.services.llm_proxy_service import render_prompt_template

        result = render_prompt_template("Count: {{n}}", {"n": 42})
        assert result == "Count: 42"
