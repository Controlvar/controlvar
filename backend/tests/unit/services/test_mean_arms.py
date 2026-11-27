"""
``mean_arms.prepare_mean_arms``: every per-user value of a mean metric is read
there, and the statistic takes nothing else (#2214).

* ``per_user_mean_metric_result`` refuses anything but a ``PreparedMeanArms``
  with ``TypeError``, so a results path that reads values some other way
  fails when it runs;
* ``prepare_mean_arms`` reads through the service it is given (the batched
  export's overrides included), by the metric's type, for each count row in
  order, and refuses a conversion metric, counts with no control, and a
  segment's members on the batched export's service;
* ``/results`` itself, on ``mean_metrics_harness.SHARE_IS_NOT_THE_MEAN``,
  answers the dataset's exact means, spread and difference, and Welch's
  interval as scipy computes it.

That this moves no number is held by the pin tables
(``test_engine_versions_frozen.py``), which go through ``prepare_mean_arms``
wherever they run ``/results``, and by ``test_mean_views_engine_bump.py``.
"""

from __future__ import annotations

import math
from fractions import Fraction
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import numpy as np
import pytest
from scipy import stats

from backend.app.models.experiment import MetricType
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.event_matching import MetricEvents
from backend.app.services.mean_arms import (
    PreparedMeanArms,
    _member_values,
    prepare_mean_arms,
    prepared_for_tests,
)
from backend.app.services.results_batch import (
    EventRows,
    GroupReads,
    _BatchedAnalysisService,
)
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    per_user_mean_metric_result,
)
from backend.tests.unit.services import mean_metrics_harness as harness

pytestmark = [pytest.mark.unit]

_EXPERIMENT = SimpleNamespace(id="e1")
_CONTROL = "00000000-2214-4000-8000-0000000000c0"
_TREATMENT = "00000000-2214-4000-8000-0000000000a1"
_COUNTS = [
    {
        "variant_id": _CONTROL,
        "variant_name": "control",
        "is_control": True,
        "sample_size": 10,
        "conversions": 2,
    },
    {
        "variant_id": _TREATMENT,
        "variant_name": "treatment",
        "is_control": False,
        "sample_size": 12,
        "conversions": 3,
    },
]
_VALUES = {_CONTROL: [4.0, 6.0], _TREATMENT: [1.0, 2.0, 9.0]}
_EVENTS = {_CONTROL: [1.0, 3.0], _TREATMENT: [2.0, 1.0, 1.0]}


def _metric(metric_type: Any, window_hours: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="00000000-2214-4000-8000-0000000000e1",
        name="Order value",
        event_name="purchase",
        metric_type=metric_type,
        is_primary=True,
        conversion_window_hours=window_hours,
    )


class _Reader(AnalysisService):
    """``AnalysisService`` with its two readers answered from memory."""

    def __init__(self) -> None:
        super().__init__(MagicMock())
        self.calls: List[Any] = []

    def value_totals(self, experiment_id, variant_id, events):
        self.calls.append(("value_totals", experiment_id, variant_id, events))
        return list(_VALUES[variant_id])

    def event_counts(self, experiment_id, variant_id, events):
        self.calls.append(("event_counts", experiment_id, variant_id, events))
        return list(_EVENTS[variant_id])


# ---------------------------------------------------------------------------
# The statistic takes a PreparedMeanArms only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [
        [(BinomialVariant(_CONTROL, "control", True), 3, 1, [2.0])],
        ((BinomialVariant(_CONTROL, "control", True), 3, 1, [2.0]),),
        iter([(BinomialVariant(_CONTROL, "control", True), 3, 1, [2.0])]),
    ],
    ids=["list", "tuple", "iterator"],
)
def test_the_statistic_refuses_values_not_read_by_prepare_mean_arms(given):
    """Before ``mean_arms``, ``per_user_mean_metric_result`` took any rows
    of ``(variant, n, n_converted, values)``, so a view could read the values
    its own way and the statistic would analyse them."""
    with pytest.raises(TypeError, match="PreparedMeanArms"):
        per_user_mean_metric_result(given, 0.05, "none", metric=_metric("revenue"))


def test_prepared_for_tests_passes_the_rows_on_unchanged():
    rows = [(BinomialVariant(_CONTROL, "control", True), 3, 1, [2.0])]
    prepared = prepared_for_tests(rows)
    assert isinstance(prepared, PreparedMeanArms)
    assert prepared.arms == tuple(rows)
    assert prepared.arms[0] is rows[0]
    assert prepared.window_hours is None


# ---------------------------------------------------------------------------
# What prepare_mean_arms reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "metric_type, reader, values",
    [
        (MetricType.REVENUE, "value_totals", _VALUES),
        ("duration", "value_totals", _VALUES),
        ("custom", "value_totals", _VALUES),
        (MetricType.COUNT, "event_counts", _EVENTS),
    ],
)
def test_each_arm_is_read_through_the_service_by_the_metric_type(
    metric_type, reader, values
):
    service = _Reader()
    metric = _metric(metric_type, window_hours=24)
    prepared = prepare_mean_arms(service, _EXPERIMENT, metric, _COUNTS)

    events = MetricEvents("purchase", 24)
    assert service.calls == [
        (reader, "e1", _CONTROL, events),
        (reader, "e1", _TREATMENT, events),
    ]
    assert prepared.window_hours == 24
    assert [
        (tuple(variant), n, converted, list(read))
        for variant, n, converted, read in prepared.arms
    ] == [
        ((_CONTROL, "control", True), 10, 2, values[_CONTROL]),
        ((_TREATMENT, "treatment", False), 12, 3, values[_TREATMENT]),
    ]


def test_the_batched_export_is_read_through_its_own_rows():
    """The batched export's service overrides both readers with the rows it
    has already read; ``prepare_mean_arms`` reads through them, so no
    statement runs."""
    db = MagicMock()
    events = MetricEvents("purchase", None)
    rows = EventRows(
        users={
            ("e1", events, _CONTROL): [(2, 10.0), (1, None)],
            ("e1", events, _TREATMENT): [(1, 4.5)],
        },
        read={("e1", events)},
    )
    service = _BatchedAnalysisService(db, GroupReads(rows=rows))

    revenue = prepare_mean_arms(service, _EXPERIMENT, _metric("revenue"), _COUNTS)
    count = prepare_mean_arms(service, _EXPERIMENT, _metric("count"), _COUNTS)

    assert [arm[3] for arm in revenue.arms] == [[10.0], [4.5]]
    assert [arm[3] for arm in count.arms] == [[2.0, 1.0], [1.0]]
    db.query.assert_not_called()


def test_a_segment_s_members_are_read_with_member_values():
    db = MagicMock()
    query = db.query.return_value.filter.return_value.group_by.return_value
    query.all.return_value = [("u1", 5.0), ("u2", 7.0), ("u3", 11.0)]
    service = AnalysisService(db)

    prepared = prepare_mean_arms(
        service,
        _EXPERIMENT,
        _metric("revenue"),
        _COUNTS,
        members={_CONTROL: {"u1", "u3"}, _TREATMENT: {"u2"}},
    )

    assert [arm[3] for arm in prepared.arms] == [[5.0, 11.0], [7.0]]
    assert _member_values(db, "e1", _CONTROL, _metric("revenue"), {"u2"}) == [7.0]


def test_a_conversion_metric_is_refused():
    with pytest.raises(TypeError, match="conversion"):
        prepare_mean_arms(_Reader(), _EXPERIMENT, _metric("conversion"), _COUNTS)
    with pytest.raises(TypeError, match="conversion"):
        prepare_mean_arms(
            _Reader(), _EXPERIMENT, _metric(MetricType.CONVERSION), _COUNTS
        )


def test_counts_with_no_control_are_refused_before_anything_is_read():
    service = _Reader()
    with pytest.raises(ValueError, match="control"):
        prepare_mean_arms(service, _EXPERIMENT, _metric("revenue"), _COUNTS[1:])
    assert service.calls == []


def test_members_on_the_batched_export_s_service_are_refused():
    service = _BatchedAnalysisService(MagicMock(), GroupReads())
    with pytest.raises(ValueError, match="no user ids"):
        prepare_mean_arms(
            service,
            _EXPERIMENT,
            _metric("revenue"),
            _COUNTS,
            members={_CONTROL: set(), _TREATMENT: set()},
        )


# ---------------------------------------------------------------------------
# The dataset, and /results on it
# ---------------------------------------------------------------------------


def test_the_reference_is_the_dataset():
    """The hand count in ``mean_metrics_harness.reference`` agrees with the
    dataset it describes, user by user."""
    reference = harness.reference()
    for variant_id in (harness.CONTROL_ID, harness.TREATMENT_ID):
        values = [
            Fraction(int(user.value)) if user.value is not None else Fraction(0)
            for user in harness.SHARE_IS_NOT_THE_MEAN
            if user.variant_id == variant_id
        ]
        mean = sum(values) / len(values)
        assert len(values) == reference.users[variant_id] == 2_000
        assert sum(1 for v in values if v) == reference.buyers[variant_id] == 200
        assert mean == reference.means[variant_id]
        assert (
            sum((v - mean) ** 2 for v in values) / (len(values) - 1)
            == reference.variances[variant_id]
        )
    assert reference.means == {
        harness.CONTROL_ID: Fraction(5),
        harness.TREATMENT_ID: Fraction(13, 2),
    }
    assert reference.difference == Fraction(3, 2)
    assert reference.variances == {
        harness.CONTROL_ID: Fraction(470_000, 1_999),
        harness.TREATMENT_ID: Fraction(794_300, 1_999),
    }
    assert reference.pooled_variance == Fraction(1_264_300, 3_998)


def _headline(monkeypatch) -> Dict[str, Any]:
    session = harness.ViewsSession(harness.experiment())
    harness.install(session, monkeypatch)
    monkeypatch.setattr(
        AnalysisService,
        "calculate_experiment_summary",
        lambda self, experiment: {
            "total_users": 4_000,
            "total_events": 400,
            "total_conversions": 400,
            "duration_days": None,
        },
    )
    monkeypatch.setattr(AnalysisService, "_sample_ratio_check", lambda self, e: None)
    output = AnalysisService(session).get_experiment_results(
        harness.EXPERIMENT_ID, include_bayesian=False
    )
    assert session.unexpected == []
    (metric,) = output["metrics"]
    return metric


def test_results_on_the_dataset_is_the_reference(monkeypatch):
    """``/results`` reads the dataset's values through ``prepare_mean_arms``:
    the means 5.0 and 6.5 per user, the spread over every assigned user,
    a difference of exactly 1.5, and Welch's interval at 0.95."""
    reference = harness.reference()
    metric = _headline(monkeypatch)
    assert metric["analysis"] == "mean"
    by_id = {v["variant_id"]: v for v in metric["variants"]}
    control = by_id[harness.CONTROL_ID]
    treatment = by_id[harness.TREATMENT_ID]

    for variant_id, row in (
        (harness.CONTROL_ID, control),
        (harness.TREATMENT_ID, treatment),
    ):
        assert row["sample_size"] == 2_000
        assert row["conversions"] == 200
        assert row["mean"] == float(reference.means[variant_id])
        assert row["std_dev"] == pytest.approx(
            math.sqrt(reference.variances[variant_id]), rel=1e-12
        )
    assert treatment["absolute_difference"] == pytest.approx(
        float(reference.difference), rel=1e-12
    )

    def values(variant_id: str) -> np.ndarray:
        return np.array(
            [
                user.value or 0.0
                for user in harness.SHARE_IS_NOT_THE_MEAN
                if user.variant_id == variant_id
            ]
        )

    welch = stats.ttest_ind(
        values(harness.TREATMENT_ID), values(harness.CONTROL_ID), equal_var=False
    )
    assert treatment["absolute_difference_ci"] == pytest.approx(
        tuple(welch.confidence_interval(0.95)), rel=1e-9
    )
