"""The two per-segment breakdowns, who may read them, and the demo experiment (#1142).

``GET /api/v1/results/{id}?breakdown=<key>`` tests each variant against the
control within each segment, with a Bonferroni threshold and an ``unknown``
segment for events without the key. ``GET /experiments/{id}/segmented-results``
counts users and conversions per segment, variant and metric, and runs no test.
Any signed-in user may read both.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.v1.endpoints import results as results_endpoint
from backend.app.main import app
from backend.app.models.experiment import ExperimentStatus
from backend.app.models.user import UserRole
from backend.app.schemas.dimensional import DimensionalBreakdownResponse
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.dimensional_analysis_service import (
    DimensionalAnalysisService,
)
from backend.scripts import seed_demo_data as seed

CONTROL = str(uuid.uuid4())
TREATMENT = str(uuid.uuid4())


def _user(role):
    return SimpleNamespace(
        id=uuid.uuid4(), role=role, is_active=True, is_superuser=False
    )


@pytest.fixture
def as_role():
    def sign_in(role, db):
        user = _user(role)
        app.dependency_overrides[deps.get_current_active_user] = lambda: user
        app.dependency_overrides[deps.get_db] = lambda: db

    yield sign_in
    app.dependency_overrides.pop(deps.get_current_active_user, None)
    app.dependency_overrides.pop(deps.get_db, None)


def _experiment(status=ExperimentStatus.ACTIVE):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        owner_id=uuid.uuid4(),
        correction_method="benjamini_hochberg",
        confidence_level=0.95,
    )


# ---------------------------------------------------------------------------
# The demo experiment the page uses
# ---------------------------------------------------------------------------


def test_the_examples_experiment_counts_checkout_completed():
    db = MagicMock()
    admin = SimpleNamespace(id=uuid.uuid4())
    db.query.return_value.filter.return_value.first.return_value = None
    with patch.object(seed, "_bulk_insert"):
        experiment = seed.seed_checkout_button_experiment(db, admin)

    added = [c.args[0] for c in db.add.call_args_list]
    assert experiment.key == "checkout_button_color"
    (primary,) = [o for o in added if type(o).__name__ == "Metric" and o.is_primary]
    assert primary.event_name == "checkout_completed"
    assert primary.experiment_id == experiment.id


# ---------------------------------------------------------------------------
# Who may read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.name)
def test_any_signed_in_user_reads_both_breakdowns(as_role, role):
    experiment = _experiment()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = experiment
    as_role(role, db)
    client = TestClient(app)

    analysed = {
        "experiment_id": str(experiment.id),
        "experiment_name": "Checkout",
        "status": "active",
        "start_date": None,
        "end_date": None,
        "confidence_level": 0.95,
        "correction_method": "benjamini_hochberg",
        "computed_at": "2026-03-01T12:00:00+00:00",
        "sample_size_adequate": False,
        "summary": {
            "total_users": 0,
            "total_events": 0,
            "total_conversions": 0,
            "duration_days": 0,
            "has_winner": False,
            "winning_variant_id": None,
            "recommendation": "CONTINUE_TESTING",
            "recommendation_reason": "No significant winner yet.",
        },
        "metrics": [],
    }
    breakdown = DimensionalBreakdownResponse(
        dimension="device",
        segments=[],
        has_heterogeneous_effects=False,
        hte_warning=None,
        adjusted_alpha=0.05,
        is_exploratory=True,
    )
    with (
        patch.object(results_endpoint, "AnalysisService") as analysis,
        patch.object(
            results_endpoint, "_compute_dimensional_breakdown", return_value=breakdown
        ),
        patch.object(results_endpoint, "_embedded_sequential", return_value=None),
        patch.object(results_endpoint, "_srm_block", return_value=None),
    ):
        analysis.return_value.get_experiment_results.return_value = analysed
        read = client.get(
            f"/api/v1/results/{experiment.id}",
            params={"breakdown": "device", "use_cache": "false"},
        )
    assert read.status_code == 200, read.text
    assert read.json()["breakdown"]["dimension"] == "device"

    with patch.object(
        AnalysisService, "get_segmented_results", return_value={"segments": []}
    ):
        segmented = client.get(
            f"/api/v1/experiments/{experiment.id}/segmented-results/device"
        )
    assert segmented.status_code == 200, segmented.text


def test_a_draft_experiment_has_no_segmented_results(as_role):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = _experiment(
        ExperimentStatus.DRAFT
    )
    as_role(UserRole.ADMIN, db)
    response = TestClient(app).get(
        f"/api/v1/experiments/{uuid.uuid4()}/segmented-results/device"
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# /results?breakdown=
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


def _breakdown_db(segment_values):
    variants = [
        SimpleNamespace(id=CONTROL, name="blue_button", is_control=True),
        SimpleNamespace(id=TREATMENT, name="green_button", is_control=False),
    ]
    metric = SimpleNamespace(event_name="checkout_completed")
    statements = []

    def query(model):
        q = MagicMock()
        if model.__name__ == "Variant":
            q.filter.return_value = variants
        else:
            q.filter.return_value.order_by.return_value.first.return_value = metric
        return q

    def execute(statement, params):
        sql = str(statement)
        statements.append(sql)
        if "SELECT DISTINCT" in sql:
            return _Rows([(v,) for v in segment_values])
        if "COUNT(DISTINCT a.user_id) AS total" in sql:
            return _Rows([(CONTROL, 1), (TREATMENT, 3)])
        return _Rows([(CONTROL, 1), (TREATMENT, 2)])

    db = MagicMock()
    db.query.side_effect = query
    db.execute.side_effect = execute
    return db, statements


def test_events_without_the_key_form_an_unknown_segment_under_a_bonferroni_threshold():
    db, statements = _breakdown_db(["desktop", "mobile", "unknown"])
    breakdown = results_endpoint._compute_dimensional_breakdown(
        experiment_id=uuid.uuid4(), dimension="device", db=db, base_alpha=0.05
    )
    assert breakdown.dimension == "device"
    assert breakdown.is_exploratory is True
    assert [s.segment_value for s in breakdown.segments] == [
        "desktop",
        "mobile",
        "unknown",
    ]
    assert breakdown.adjusted_alpha == pytest.approx(0.05 / 3)
    # An event whose metadata lacks the key is counted under "unknown".
    distinct = next(s for s in statements if "SELECT DISTINCT" in s)
    assert "jsonb_extract_path_text(event_metadata, :dim_key)" in distinct
    assert "'unknown'" in distinct and "COALESCE" in distinct


def test_the_control_has_no_p_value_and_each_treatment_is_tested_against_it():
    service = DimensionalAnalysisService()
    segments = {
        "desktop": {
            CONTROL: {"total": 400, "conversions": 40, "is_control": True},
            TREATMENT: {"total": 400, "conversions": 64, "is_control": False},
        },
        "mobile": {
            CONTROL: {"total": 300, "conversions": 60, "is_control": True},
            TREATMENT: {"total": 300, "conversions": 45, "is_control": False},
        },
    }
    for segment in service.compute_segment_results(segments, base_alpha=0.05):
        counts = segments[segment.segment_value]
        by_id = {v.variant_id: v for v in segment.variants}
        assert by_id[CONTROL].p_value is None
        assert by_id[TREATMENT].p_value == service._two_prop_z_test(
            counts[CONTROL]["conversions"],
            counts[CONTROL]["total"],
            counts[TREATMENT]["conversions"],
            counts[TREATMENT]["total"],
        )


# ---------------------------------------------------------------------------
# /experiments/{id}/segmented-results/{key}
# ---------------------------------------------------------------------------


def test_segmented_results_are_counts_for_segments_that_carry_the_key():
    variants = [
        SimpleNamespace(id=CONTROL, name="blue_button", is_control=True),
        SimpleNamespace(id=TREATMENT, name="green_button", is_control=False),
    ]
    metric = SimpleNamespace(
        id=uuid.uuid4(), name="Checkout", event_name="checkout_completed"
    )
    experiment = SimpleNamespace(variants=variants, metric_definitions=[metric])
    db = MagicMock()
    db.query.return_value.options.return_value.filter.return_value.first.return_value = experiment
    statements = []
    scalars = iter([4, 1, 4, 3, 5, 2, 5, 1])

    def execute(statement, params):
        statements.append(str(statement))
        result = MagicMock()
        result.__iter__.return_value = iter([("desktop",), ("mobile",)])
        result.scalar.side_effect = lambda: next(scalars)
        return result

    db.execute.side_effect = execute
    with patch(
        "backend.app.services.analysis_service.get_schema_name",
        return_value="experimentation",
    ):
        result = AnalysisService(db).get_segmented_results(uuid.uuid4(), "device")

    # Only events that carry the key are segmented: no "unknown".
    assert "event_metadata ? :segment_key" in statements[0]
    assert [s["segment_value"] for s in result["segments"]] == ["desktop", "mobile"]
    desktop = result["segments"][0]["metrics"][0]["variants"]
    assert [(v["sample_size"], v["conversions"]) for v in desktop] == [(4, 1), (4, 3)]
    # A percentage, and no significance test.
    assert [v["conversion_rate"] for v in desktop] == [25.0, 75.0]
    for v in desktop:
        assert set(v) == {
            "variant_id",
            "variant_name",
            "is_control",
            "sample_size",
            "conversions",
            "conversion_rate",
        }


def test_the_demo_data_has_three_experiments():
    import ast
    import inspect

    assert set(seed.DEMO_EXPERIMENT_KEYS.values()) == {
        "homepage_hero_copy",
        "checkout_button_color",
        "recommendation_algorithm_mab",
    }
    called = {
        node.func.id
        for node in ast.walk(ast.parse(inspect.getsource(seed.main)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("seed_")
        and node.func.id.endswith("_experiment")
    }
    assert called == {
        "seed_homepage_hero_experiment",
        "seed_checkout_button_experiment",
        "seed_recommendation_mab_experiment",
    }
