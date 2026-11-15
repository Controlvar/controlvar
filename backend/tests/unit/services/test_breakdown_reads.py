"""The two per-segment breakdowns, who may read them, and the demo experiment (#1142).

``GET /api/v1/results/{id}?breakdown=<key>`` tests each variant against the
control within each segment, with a Bonferroni threshold and an ``unknown``
segment for users without the key. ``GET /experiments/{id}/segmented-results``
counts users and conversions per segment, variant and metric, and runs no test.
Both put each assigned user in one segment (``segment_breakdown``). Any
signed-in user may read both.
"""

from __future__ import annotations

import math
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
from backend.app.services import segment_breakdown
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
def test_every_shipped_role_reads_both_breakdowns(as_role, role):
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


def _fisher_two_sided(control_conv, control_n, treatment_conv, treatment_n):
    """Two-sided Fisher's exact p-value, written out with math.comb: the sum of
    the probabilities of every table with the same margins that is no more
    likely than the observed one."""
    converted = control_conv + treatment_conv
    total = control_n + treatment_n

    def weight(x):
        return math.comb(treatment_n, x) * math.comb(control_n, converted - x)

    observed = weight(treatment_conv)
    lowest, highest = max(0, converted - control_n), min(treatment_n, converted)
    as_extreme = sum(
        w for w in (weight(x) for x in range(lowest, highest + 1)) if w <= observed
    )
    return as_extreme / math.comb(total, converted)


class _Readers:
    """The reads ``segment_breakdown.segment_rows`` makes, from plain values.

    ``assigned`` maps a variant id to its users, ``values`` a user to the value
    of the dimension, and ``converters`` an event name to each variant's
    converting users.  Every call is recorded."""

    def __init__(self, assigned, values, converters):
        self.assigned = assigned
        self.values = values
        self.converters = converters
        self.converter_reads = []

    def install(self, monkeypatch):
        monkeypatch.setattr(
            segment_breakdown,
            "assignment_times",
            lambda db, experiment_id: {
                v: dict.fromkeys(users) for v, users in self.assigned.items()
            },
        )
        monkeypatch.setattr(
            segment_breakdown,
            "user_values",
            lambda db, experiment_id, dimension: dict(self.values),
        )
        monkeypatch.setattr(
            segment_breakdown, "converting_user_ids", self.converting_user_ids
        )

    def converting_user_ids(self, db, experiment_id, variant_id, event_name):
        self.converter_reads.append((variant_id, event_name))
        return set(self.converters.get(event_name, {}).get(variant_id, ()))


#: Three desktop users and two mobile users per variant, and in each variant
#: one user with no device (never tracked with it, or never tracked at all).
DEVICE_USERS = {
    CONTROL: ["c1", "c2", "c3", "c4", "c5", "c6"],
    TREATMENT: ["t1", "t2", "t3", "t4", "t5", "t6"],
}
DEVICE_VALUES = {
    **dict.fromkeys(("c1", "c2", "c3", "t1", "t2", "t3"), "desktop"),
    **dict.fromkeys(("c4", "c5", "t4", "t5"), "mobile"),
}
DEVICE_CONVERTERS = {
    "checkout_completed": {CONTROL: ["c1", "c6"], TREATMENT: ["t1", "t2", "t4"]},
    "add_to_cart": {CONTROL: ["c1", "c2", "c3"], TREATMENT: ["t6"]},
}


def _breakdown_db():
    variants = [
        SimpleNamespace(id=CONTROL, name="blue_button", is_control=True),
        SimpleNamespace(id=TREATMENT, name="green_button", is_control=False),
    ]
    # The primary metric is the one listed first; the other has a name that
    # sorts first and no is_primary at all.
    experiment = SimpleNamespace(
        metric_definitions=[
            SimpleNamespace(
                name="Add to cart", is_primary=None, event_name="add_to_cart"
            ),
            SimpleNamespace(
                name="Checkout", is_primary=True, event_name="checkout_completed"
            ),
        ]
    )

    def query(model):
        q = MagicMock()
        if model.__name__ == "Variant":
            q.filter.return_value = variants
        else:
            q.filter.return_value.first.return_value = experiment
        return q

    db = MagicMock()
    db.query.side_effect = query
    return db


def test_users_without_the_key_form_an_unknown_segment_under_a_bonferroni_threshold(
    monkeypatch,
):
    readers = _Readers(DEVICE_USERS, DEVICE_VALUES, DEVICE_CONVERTERS)
    readers.install(monkeypatch)

    breakdown = results_endpoint._compute_dimensional_breakdown(
        experiment_id=uuid.uuid4(),
        dimension="device",
        db=_breakdown_db(),
        base_alpha=0.05,
    )

    assert breakdown.dimension == "device"
    assert breakdown.is_exploratory is True
    # The values by users, then the users with no device, last.
    assert [s.segment_value for s in breakdown.segments] == [
        "desktop",
        "mobile",
        "unknown",
    ]
    assert breakdown.adjusted_alpha == pytest.approx(0.05 / 3)
    # Each user in one segment, with the primary metric's converting users.
    assert {
        s.segment_value: {
            v.variant_name: (v.sample_size, v.conversions) for v in s.variants
        }
        for s in breakdown.segments
    } == {
        "desktop": {"blue_button": (3, 1), "green_button": (3, 2)},
        "mobile": {"blue_button": (2, 0), "green_button": (2, 1)},
        "unknown": {"blue_button": (1, 1), "green_button": (1, 0)},
    }
    assert {name for _, name in readers.converter_reads} == {"checkout_completed"}


def test_the_control_has_no_p_value_and_each_treatment_has_fishers_exact_test_against_it():
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
        "tablet": {
            CONTROL: {"total": 3, "conversions": 0, "is_control": True},
            TREATMENT: {"total": 3, "conversions": 3, "is_control": False},
        },
    }
    results = service.compute_segment_results(segments, base_alpha=0.05)
    assert [s.segment_value for s in results] == ["desktop", "mobile", "tablet"]
    for segment in results:
        counts = segments[segment.segment_value]
        by_id = {v.variant_id: v for v in segment.variants}
        assert by_id[CONTROL].p_value is None
        assert by_id[TREATMENT].p_value == pytest.approx(
            _fisher_two_sided(
                counts[CONTROL]["conversions"],
                counts[CONTROL]["total"],
                counts[TREATMENT]["conversions"],
                counts[TREATMENT]["total"],
            ),
            rel=1e-6,
        )
    # Three users in each arm, all of one and none of the other: 0.1, which is
    # not significant at 0.05 / 3.
    tablet = {v.variant_id: v for v in results[2].variants}
    assert tablet[TREATMENT].p_value == pytest.approx(0.1, rel=1e-6)
    assert tablet[TREATMENT].is_significant is False


# ---------------------------------------------------------------------------
# /experiments/{id}/segmented-results/{key}
# ---------------------------------------------------------------------------


def test_segmented_results_count_each_user_in_one_segment_with_no_test(monkeypatch):
    readers = _Readers(DEVICE_USERS, DEVICE_VALUES, DEVICE_CONVERTERS)
    readers.install(monkeypatch)
    variants = [
        SimpleNamespace(id=CONTROL, name="blue_button", is_control=True),
        SimpleNamespace(id=TREATMENT, name="green_button", is_control=False),
    ]
    metric = SimpleNamespace(
        id=uuid.uuid4(), name="Checkout", event_name="checkout_completed"
    )
    experiment = SimpleNamespace(
        id=uuid.uuid4(), variants=variants, metric_definitions=[metric]
    )
    db = MagicMock()
    db.query.return_value.options.return_value.filter.return_value.first.return_value = experiment

    result = AnalysisService(db).get_segmented_results(uuid.uuid4(), "device")

    # The same segments as /results?breakdown=, unknown included.
    assert [s["segment_value"] for s in result["segments"]] == [
        "desktop",
        "mobile",
        "unknown",
    ]
    counts = {
        s["segment_value"]: [
            (v["variant_name"], v["sample_size"], v["conversions"])
            for v in s["metrics"][0]["variants"]
        ]
        for s in result["segments"]
    }
    assert counts == {
        "desktop": [("blue_button", 3, 1), ("green_button", 3, 2)],
        "mobile": [("blue_button", 2, 0), ("green_button", 2, 1)],
        "unknown": [("blue_button", 1, 1), ("green_button", 1, 0)],
    }
    desktop = result["segments"][0]["metrics"][0]["variants"]
    # A percentage, and no significance test.
    assert [v["conversion_rate"] for v in desktop] == [
        pytest.approx(100 / 3),
        pytest.approx(200 / 3),
    ]
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
