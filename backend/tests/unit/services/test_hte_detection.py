"""The heterogeneous-effects flag of a breakdown, through the route's own code (#1969).

``_compute_dimensional_breakdown`` (behind ``/results?breakdown=`` and
``/balanced-groups``) sets ``has_heterogeneous_effects`` and ``hte_warning``
from ``DimensionalAnalysisService.detect_hte``: a test that the first
treatment's difference from the control, in percentage points, is the same in
every tested segment, at 1 minus the confidence level the results are computed
at.  Most tests here run the whole helper on plain counts, with the reads
``segment_rows`` makes replaced by ``_Readers`` (``test_breakdown_reads.py``);
the rates of the test over many seeded tables are in
``backend/tests/unit/stats_validation/test_golden_hte_false_positive_rate.py``.
"""

from __future__ import annotations

import math
import uuid
import warnings
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from scipy import optimize, stats

from backend.app.api import deps
from backend.app.api.v1.endpoints import results as results_endpoint
from backend.app.main import app
from backend.app.models.experiment import ExperimentStatus
from backend.app.models.user import UserRole
from backend.app.schemas.dimensional import DimensionalBreakdownResponse
from backend.app.services import dimensional_analysis_service as das
from backend.app.services import interaction_analysis as ia
from backend.tests.unit.services.test_breakdown_reads import _Readers

pytestmark = pytest.mark.unit

EVENT = "checkout_completed"
CONTROL = "77777777-0000-4000-8000-000000000002"
TREATMENT = "00000000-0000-4000-8000-000000000001"
#: Two treatments whose names sort in the reverse of their ids: T1 has the
#: smaller id and the name that sorts second, T2 the larger id and the name
#: that sorts first.  Each (id, name) pair stays fixed.
T1 = ("00000000-0000-4000-8000-000000000001", "zeta")
T2 = ("ffffffff-0000-4000-8000-000000000003", "alpha")

Counts = Dict[Optional[str], Dict[str, Tuple[int, int]]]


def _warning(dimension: str) -> str:
    return (
        "The first treatment's difference from the control varies between "
        f"'{dimension}' segments by more than chance usually produces. Treat "
        "this as a lead to confirm with a follow-up experiment, not as a result."
    )


def _db(variants):
    """The variants and the experiment (one primary metric) the helper reads."""
    rows = [
        SimpleNamespace(id=vid, name=name, is_control=ctrl)
        for vid, name, ctrl in variants
    ]
    experiment = SimpleNamespace(
        metric_definitions=[
            SimpleNamespace(name="Checkout", is_primary=True, event_name=EVENT)
        ]
    )

    def query(model):
        q = MagicMock()
        if model.__name__ == "Variant":
            q.filter.return_value = rows
        else:
            q.filter.return_value.first.return_value = experiment
        return q

    db = MagicMock()
    db.query.side_effect = query
    return db


def _breakdown(
    monkeypatch,
    counts: Counts,
    alpha: float,
    variants=((CONTROL, "control", True), (TREATMENT, "treatment", False)),
    dimension: str = "device",
):
    """``_compute_dimensional_breakdown`` on ``{segment: {variant: (users,
    converting users)}}``; the segment ``None`` is users with no value."""
    assigned: Dict[str, List[str]] = {vid: [] for vid, _, _ in variants}
    values: Dict[str, str] = {}
    converters: Dict[str, List[str]] = {vid: [] for vid, _, _ in variants}
    for segment, arms in counts.items():
        for vid, (users, converting) in arms.items():
            for i in range(users):
                user = f"{segment}-{vid}-{i}"
                assigned[vid].append(user)
                if segment is not None:
                    values[user] = segment
                if i < converting:
                    converters[vid].append(user)
    _Readers(assigned, values, {EVENT: converters}).install(monkeypatch)
    return results_endpoint._compute_dimensional_breakdown(
        experiment_id=uuid.uuid4(),
        dimension=dimension,
        db=_db(variants),
        base_alpha=alpha,
    )


def _two_arms(control: Tuple[int, int], treatment: Tuple[int, int]):
    return {CONTROL: control, TREATMENT: treatment}


# ---------------------------------------------------------------------------
# The reported case and the old rule's other false flags (regressions)
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_walk_data_shows_no_heterogeneity(monkeypatch):
    """The data whose banner #1969 reported: the same lift in every platform.
    The unknown segment is below the floor (31 users at a pooled 13.8%), the
    other three give X^2 2.97 on 2 df, p 0.23.  0.47.0 flagged it."""
    walk = {
        "web": _two_arms((90, 7), (89, 16)),
        "android": _two_arms((88, 12), (88, 15)),
        "ios": _two_arms((84, 6), (84, 20)),
        None: _two_arms((31, 1), (32, 4)),
    }
    breakdown = _breakdown(monkeypatch, walk, 0.05, dimension="platform")
    assert [s.segment_value for s in breakdown.segments] == [
        "web",
        "android",
        "ios",
        "unknown",
    ]
    assert breakdown.has_heterogeneous_effects is False
    assert breakdown.hte_warning is None


@pytest.mark.regression
def test_equal_differences_on_an_unequal_split_are_not_flagged(monkeypatch):
    """10% -> 15% in both segments, split 600/200 and 200/600: p 1.0.  0.47.0
    tested the converting users alone and flagged it."""
    counts = {
        "desktop": _two_arms((600, 60), (200, 30)),
        "mobile": _two_arms((200, 20), (600, 90)),
    }
    breakdown = _breakdown(monkeypatch, counts, 0.05)
    assert breakdown.has_heterogeneous_effects is False
    assert breakdown.hte_warning is None


@pytest.mark.regression
def test_a_segment_without_control_users_is_not_tested(monkeypatch):
    """A segment with treatment users only is not a candidate, which leaves
    one tested segment: no flag.  0.47.0 measured its lift against a control
    rate of 0 and flagged it."""
    counts = {
        "desktop": _two_arms((300, 30), (300, 45)),
        "tablet": {TREATMENT: (10, 2)},
    }
    breakdown = _breakdown(monkeypatch, counts, 0.05)
    assert [s.segment_value for s in breakdown.segments] == ["desktop", "tablet"]
    assert breakdown.has_heterogeneous_effects is False


# ---------------------------------------------------------------------------
# The test runs at the alpha the breakdown is given
# ---------------------------------------------------------------------------

#: Four segments of 1,000 users per arm, the control at 100 in each, the
#: treatment at 100, 100, 100 and the last count.
ALPHA_MID = 152  # p 0.0214: flagged at .05, not at .01
ALPHA_HIGH = 144  # p 0.0693: flagged at .10, not at .05


def _alpha_counts(last: int) -> Counts:
    return {
        f"segment-{j}": _two_arms((1000, 100), (1000, x))
        for j, x in enumerate((100, 100, 100, last))
    }


def _reference_p(n, x) -> float:
    """The test written out independently of interaction_analysis: the
    additive model's likelihood maximised with scipy's bounded scalar search
    (each baseline for a given difference, then the difference), then
    Pearson's X^2 against the fitted rates on J - 1 degrees of freedom."""

    def baseline(j, d):
        def minus_loglik(b):
            t = b + d
            return -(
                x[0][j] * math.log(b)
                + (n[0][j] - x[0][j]) * math.log(1 - b)
                + x[1][j] * math.log(t)
                + (n[1][j] - x[1][j]) * math.log(1 - t)
            )

        low, high = max(0.0, -d) + 1e-12, min(1.0, 1.0 - d) - 1e-12
        return optimize.minimize_scalar(
            minus_loglik, bounds=(low, high), method="bounded", options={"xatol": 1e-13}
        )

    columns = range(len(n[0]))
    fit = optimize.minimize_scalar(
        lambda d: sum(baseline(j, d).fun for j in columns),
        bounds=(-0.5, 0.5),
        method="bounded",
        options={"xatol": 1e-13},
    )
    statistic = 0.0
    for j in columns:
        b = baseline(j, fit.x).x
        for row, rate in ((0, b), (1, b + fit.x)):
            expected = n[row][j] * rate
            statistic += (x[row][j] - expected) ** 2 / (expected * (1 - rate))
    return float(stats.chi2.sf(statistic, len(n[0]) - 1))


@pytest.mark.parametrize(
    "last,low,high", [(ALPHA_MID, 0.01, 0.05), (ALPHA_HIGH, 0.05, 0.10)]
)
def test_the_alpha_fixtures_p_values(last, low, high):
    """Each fixture's p-value, by the independent reference, lies between the
    two alphas it is used at, and the product's test agrees with it."""
    n = [[1000] * 4, [1000] * 4]
    x = [[100] * 4, [100, 100, 100, last]]
    reference = _reference_p(n, x)
    assert low < reference < high, reference
    assert ia.interaction_test(n, x).p_value == pytest.approx(reference, rel=1e-6)


@pytest.mark.parametrize(
    "last,alpha,flagged",
    [
        (ALPHA_MID, 0.05, True),
        pytest.param(ALPHA_MID, 0.01, False, marks=pytest.mark.regression),
        (ALPHA_HIGH, 0.10, True),
        (ALPHA_HIGH, 0.05, False),
    ],
)
def test_the_breakdown_tests_at_the_alpha_it_is_given(
    monkeypatch, last, alpha, flagged
):
    """base_alpha is the heterogeneity test's alpha: not a fixed 0.05 (0.47.0
    flagged ALPHA-MID at .01) and not the per-segment Bonferroni threshold
    (.0125 here, which would leave ALPHA-MID unflagged at .05)."""
    breakdown = _breakdown(monkeypatch, _alpha_counts(last), alpha)
    assert breakdown.adjusted_alpha == pytest.approx(alpha / 4)
    assert breakdown.has_heterogeneous_effects is flagged
    assert breakdown.hte_warning == (_warning("device") if flagged else None)


@pytest.fixture
def signed_in():
    def sign_in(db):
        user = SimpleNamespace(
            id=uuid.uuid4(), role=UserRole.ADMIN, is_active=True, is_superuser=False
        )
        app.dependency_overrides[deps.get_current_active_user] = lambda: user
        app.dependency_overrides[deps.get_db] = lambda: db

    yield sign_in
    app.dependency_overrides.pop(deps.get_current_active_user, None)
    app.dependency_overrides.pop(deps.get_db, None)


def _empty_breakdown():
    return DimensionalBreakdownResponse(
        dimension="device",
        segments=[],
        has_heterogeneous_effects=False,
        hte_warning=None,
        adjusted_alpha=0.01,
        is_exploratory=True,
    )


def test_each_route_hands_its_confidence_level_down_as_the_alpha(signed_in):
    """/results computed at 0.99 hands the breakdown 0.01; /balanced-groups
    hands it 1 minus the experiment's stored level, 0.90 here."""
    seen: List[float] = []

    def spy(experiment_id, dimension, db, base_alpha, value_source=None):
        seen.append(base_alpha)
        return _empty_breakdown()

    experiment = SimpleNamespace(
        id=uuid.uuid4(),
        status=ExperimentStatus.ACTIVE,
        owner_id=uuid.uuid4(),
        correction_method="none",
        confidence_level=0.99,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = experiment
    signed_in(db)
    analysed = {
        "experiment_id": str(experiment.id),
        "experiment_name": "Checkout",
        "status": "active",
        "start_date": None,
        "end_date": None,
        "confidence_level": 0.99,
        "correction_method": "none",
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
    with (
        patch.object(results_endpoint, "AnalysisService") as analysis,
        patch.object(results_endpoint, "_compute_dimensional_breakdown", spy),
        patch.object(results_endpoint, "_embedded_sequential", return_value=None),
        patch.object(results_endpoint, "_srm_block", return_value=None),
    ):
        analysis.return_value.get_experiment_results.return_value = analysed
        read = TestClient(app).get(
            f"/api/v1/results/{experiment.id}",
            params={"breakdown": "device", "use_cache": "false"},
        )
    assert read.status_code == 200, read.text
    assert seen == [pytest.approx(0.01)]

    class Stop(Exception):
        """Raised by the spy: the rest of the balanced-groups answer is not needed."""

    def stop(experiment_id, dimension, db, base_alpha, value_source=None):
        seen.append(base_alpha)
        raise Stop

    stored = SimpleNamespace(
        id=uuid.uuid4(), confidence_level=0.90, correction_method="none"
    )
    with patch.object(results_endpoint, "_compute_dimensional_breakdown", stop):
        with pytest.raises(Stop):
            results_endpoint._balanced_groups(
                stored, {"attribute": "tier"}, MagicMock()
            )
    assert seen == [pytest.approx(0.01), pytest.approx(0.10)]


@pytest.mark.parametrize(
    "alpha", [0, 1, -0.05, 1.5, float("nan"), float("inf"), "0.05", None]
)
def test_alpha_must_be_a_finite_number_between_0_and_1(alpha):
    """Refused before anything is read, even with nothing to test."""
    with pytest.raises(ValueError):
        das.DimensionalAnalysisService().detect_hte([], alpha=alpha)


# ---------------------------------------------------------------------------
# Which segments are tested: the floor
# ---------------------------------------------------------------------------

_AT_THE_FLOOR = {
    "A": _two_arms((200, 20), (200, 30)),
    "B": _two_arms((200, 20), (200, 30)),
    # Pooled 180 of 900, and 50 x 180 = 10 x 900: exactly at the floor.
    "C": _two_arms((50, 30), (50, 50)),
}


def test_a_segment_at_the_floor_is_tested(monkeypatch):
    """C is tested, and its difference (+40 points against +5) is flagged
    (p 4.4e-5).  A floor of 15 would leave C out and A and B, which are
    identical, unflagged."""
    breakdown = _breakdown(monkeypatch, _AT_THE_FLOOR, 0.05)
    assert breakdown.has_heterogeneous_effects is True


def test_a_segment_one_conversion_below_the_floor_is_not_tested(monkeypatch):
    """One converting control user fewer in C: pooled 179 of 900, and
    50 x 179 < 10 x 900, so only A and B are tested, and they are identical
    (p 1.0).  A floor of 5 would test C and flag it."""
    counts = dict(_AT_THE_FLOOR, C=_two_arms((50, 29), (50, 50)))
    breakdown = _breakdown(monkeypatch, counts, 0.05)
    assert breakdown.has_heterogeneous_effects is False
    assert breakdown.hte_warning is None


def test_the_floor_is_computed_in_integers():
    """770 users and 100 converting users pooled: 77 x 100 = 10 x 770, so the
    77-user segment is at the floor and tested, although 77 x (100 / 770) is
    9.999999999999998 in floating point.  One conversion fewer, it is not."""
    rows = [(154, 15, 154, 27), (154, 15, 154, 27), (77, 7, 77, 9)]
    assert 77 * (100 / 770) < das.HTE_MIN_EXPECTED_PER_CELL
    assert das.hte_tested_segments(rows) == [0, 1, 2]
    rows[2] = (77, 6, 77, 9)
    assert das.hte_tested_segments(rows) == [0, 1]


# ---------------------------------------------------------------------------
# Which treatment is tested: the first by name
# ---------------------------------------------------------------------------

#: The difference from the control: +5 points in every segment, or +15, +15,
#: 0 and 0.
_SAME = (150, 150, 150, 150)
_DIFFERENT = (250, 250, 100, 100)


def _three_arm_counts(t1, t2) -> Counts:
    return {
        f"segment-{j}": {
            CONTROL: (1000, 100),
            T1[0]: (1000, t1[j]),
            T2[0]: (1000, t2[j]),
        }
        for j in range(4)
    }


THREE_ARMS = ((CONTROL, "control", True), (T1[0], T1[1], False), (T2[0], T2[1], False))


@pytest.mark.parametrize(
    "case,t1,t2,flagged",
    [("A", _DIFFERENT, _SAME, False), ("B", _SAME, _DIFFERENT, True)],
)
def test_the_first_treatment_by_name_is_the_one_tested(
    monkeypatch, case, t1, t2, flagged
):
    """T2 ("alpha", the larger id) is tested, not T1 ("zeta", the smaller id).
    Case A: only T1's difference varies, so no flag.  Case B: the same names
    and ids with the varying counts moved to T2: flagged.  Choosing the
    treatment by id would test T1 and flip both."""
    breakdown = _breakdown(monkeypatch, _three_arm_counts(t1, t2), 0.05, THREE_ARMS)
    assert [v.variant_name for v in breakdown.segments[0].variants] == [
        "control",
        "alpha",
        "zeta",
    ]
    assert breakdown.has_heterogeneous_effects is flagged, case


# ---------------------------------------------------------------------------
# Degenerate input, and a failed fit
# ---------------------------------------------------------------------------


def _arms(control, treatment, control_flagged=True):
    arms = {}
    if control is not None:
        arms[CONTROL] = {
            "total": control[0],
            "conversions": control[1],
            "is_control": control_flagged,
        }
    if treatment is not None:
        arms[TREATMENT] = {"total": treatment[0], "conversions": treatment[1]}
    return arms


@pytest.mark.parametrize(
    "segments",
    [
        pytest.param({}, id="no-segments"),
        pytest.param({"a": _arms((1000, 100), (1000, 150))}, id="one-segment"),
        pytest.param(
            {"a": _arms((300, 30), (300, 45)), "b": _arms((0, 0), (300, 60))},
            id="one-empty-control",
        ),
        pytest.param(
            {"a": _arms((100, 0), (100, 0)), "b": _arms((100, 0), (100, 0))},
            id="no-conversions",
        ),
        pytest.param(
            {"a": _arms((100, 10), (0, 0)), "b": _arms((100, 20), (0, 0))},
            id="treatment-without-users",
        ),
        pytest.param(
            {
                "a": _arms((900, 90), (900, 270), control_flagged=False),
                "b": _arms((900, 180), (900, 90), control_flagged=False),
            },
            id="no-control-flagged",
        ),
        pytest.param(
            {"a": _arms((900, 90), None), "b": _arms((900, 180), None)},
            id="no-treatment",
        ),
    ],
)
def test_too_little_to_test_is_false_without_a_warning(segments, monkeypatch):
    """False, a Python bool, no warning, and the test is never run: with
    fewer than two tested segments it has no degrees of freedom, so its
    p-value would be nan (and nan < alpha happens to be False)."""

    def never(n, x):
        raise AssertionError(f"interaction_test ran on {n} / {x}")

    monkeypatch.setattr(ia, "interaction_test", never)
    service = das.DimensionalAnalysisService()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        results = service.compute_segment_results(segments, base_alpha=0.05)
        flagged = service.detect_hte(results, alpha=0.05)
    assert flagged is False


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"c": _arms((40, 0), (40, 0))}, id="no-conversions"),
        pytest.param(
            {"c": _arms((40, 0), (40, 0)), "d": _arms((20, 20), (20, 20))},
            id="no-conversions-and-all-converting",
        ),
    ],
)
def test_a_segment_with_no_variance_does_not_hide_a_difference(extra):
    """+20 points in one segment and -10 in the other (p 0.00018), beside
    segments with no converting user or only converting users: they are
    below the floor, and the difference is still flagged."""
    segments = {"a": _arms((90, 9), (90, 27)), "b": _arms((90, 18), (90, 9)), **extra}
    service = das.DimensionalAnalysisService()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        results = service.compute_segment_results(segments, base_alpha=0.05)
        flagged = service.detect_hte(results, alpha=0.05)
    assert flagged is True


def test_a_failed_fit_fails_the_whole_breakdown(monkeypatch):
    """InteractionFitError is never caught: the breakdown raises, and the
    route answers 500 for the whole request, never a guessed flag."""

    def failing(n, x):
        raise ia.InteractionFitError("planted")

    monkeypatch.setattr(ia, "interaction_test", failing)
    with pytest.raises(ia.InteractionFitError, match="planted"):
        _breakdown(monkeypatch, _alpha_counts(ALPHA_MID), 0.05)
