"""
A count, duration or custom metric in ``/results`` is its mean per assigned
user, as a revenue metric is (#439).

* A count metric's per-user value is the number of the metric's events the
  user sent, whatever their ``value`` (``AnalysisService.event_counts``).
* A duration or custom metric's is the total of those events' ``value``
  within ``VALUE_BOUND`` (``AnalysisService.value_totals``, as for revenue).
* A user with no such event is 0.

These tests drive the real entry point, ``get_experiment_results``, with only
the database replaced: the two readers answer from an in-memory list of
events with the semantics their SQL has (``value_totals`` adds up the values
within the bound; ``event_counts`` counts every event).  So they guard the
routing (which reader a type gets, and that every mean type is routed to the
mean); the SQL itself is held by
``backend/tests/integration/api/test_value_metrics_results.py`` against
Postgres.  The expected numbers come from scipy on the per-user arrays,
zeros included.

The output for an experiment with a count primary and a duration and a custom
metric is pinned per ``ENGINE_VERSION`` (``VALUE_RESULTS_FINGERPRINTS``), and
must not be pinned under an engine that analysed these types as a share of
users: #439 changes numbers on running experiments, so it ships under a new
engine version, or ``/results`` would serve cached share-of-users answers and
the day's snapshot rows would label the means with the old version.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import numpy as np
import pytest
from scipy import stats

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models.experiment import MetricType
from backend.app.schemas import experiment as experiment_schemas
from backend.app.schemas.experiment import MetricBase, MetricResponse
from backend.app.services import analysis_service as analysis_module
from backend.app.services import sufficient_stats_analysis as ssa
from backend.app.services.analysis_service import VALUE_BOUND, AnalysisService
from backend.tests.unit.services import (
    test_binomial_metric_result_characterisation as characterisation,
)
from backend.tests.unit.services import test_engine_fingerprint as engine_fingerprint
from backend.tests.unit.services import (
    test_lower_is_better_fingerprint as lower_is_better,
)
from backend.tests.unit.services import test_results_correction_pins as correction_pins
from backend.tests.unit.services import test_revenue_per_user_routing as routing
from backend.tests.unit.services import (
    test_sufficient_stats_fingerprint as sufficient_stats,
)

pytestmark = [pytest.mark.unit]

_CONTROL = "88888888-0000-4000-8000-00000000000c"
_TREATMENT = "00000000-0000-4000-8000-00000000000a"

#: Each variant's users, and the values of the metric's events each sent.
Events = Dict[str, Dict[str, List[Optional[float]]]]


def _metric(metric_type: str, **fields: Any) -> SimpleNamespace:
    metric = SimpleNamespace(
        id=fields.pop("id", str(uuid.uuid4())),
        name=f"{metric_type} metric",
        event_name="the_event",
        metric_type=metric_type,
        is_primary=True,
        minimum_sample_size=10,
    )
    for name, value in fields.items():
        setattr(metric, name, value)
    return metric


def _counted(value: Optional[float]) -> bool:
    """The SQL predicate ``abs(value) <= VALUE_BOUND``: NULL and NaN fail it."""
    return value is not None and not math.isnan(value) and abs(value) <= VALUE_BOUND


def _run(
    metric: SimpleNamespace,
    units: Dict[str, int],
    events: Events,
    monkeypatch: pytest.MonkeyPatch,
    *,
    confidence: float = 0.95,
    reader_calls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """``get_experiment_results`` for one metric over ``events``.

    ``value_totals`` answers, per user with a counted value, the sum of the
    values within the bound; ``event_counts`` answers, per user, the number of
    events.  Each lists only the users who have one, as the SQL does.
    """
    fixture = characterisation._Fixture(units=units, converted={})

    def converting(_db, _experiment_id, variant_id, _event):
        return sum(1 for values in events[str(variant_id)].values() if values)

    def value_totals(_self, _experiment_id, variant_id, _event):
        if reader_calls is not None:
            reader_calls.append("value_totals")
        totals = []
        for values in events[str(variant_id)].values():
            counted = [v for v in values if _counted(v)]
            if counted:
                totals.append(math.fsum(counted))
        return totals

    def event_counts(_self, _experiment_id, variant_id, _event):
        if reader_calls is not None:
            reader_calls.append("event_counts")
        return [float(len(v)) for v in events[str(variant_id)].values() if v]

    monkeypatch.setattr(analysis_module, "count_converting_users", converting)
    monkeypatch.setattr(AnalysisService, "value_totals", value_totals)
    monkeypatch.setattr(AnalysisService, "event_counts", event_counts)
    monkeypatch.setattr(
        AnalysisService,
        "calculate_experiment_summary",
        lambda self, experiment: {
            "total_users": sum(units.values()),
            "total_events": 0,
            "total_conversions": 0,
            "duration_days": None,
        },
    )
    experiment = SimpleNamespace(
        id="99999999-0000-4000-8000-000000000439",
        name="Value metrics",
        status="active",
        start_date=None,
        end_date=None,
        variants=[
            SimpleNamespace(id=_TREATMENT, name="treatment", is_control=False),
            SimpleNamespace(id=_CONTROL, name="control", is_control=True),
        ],
        metric_definitions=[metric],
    )
    output = AnalysisService(
        characterisation._FakeSession(experiment, fixture)
    ).get_experiment_results(
        experiment.id,
        confidence_level=confidence,
        correction_method="none",
        include_bayesian=False,
    )
    output.pop("computed_at")
    assert output.pop("srm") is None
    return output


def _arms(output: Dict[str, Any]):
    [result] = output["metrics"]
    by_name = {v["variant_name"]: v for v in result["variants"]}
    return result, by_name["control"], by_name["treatment"]


def _per_user(events: Dict[str, List[Optional[float]]], read) -> np.ndarray:
    return np.array([read(values) for values in events.values()], dtype=float)


# ---------------------------------------------------------------------------
# Classification: every MetricType is a proportion or a mean, by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "enum", [MetricType, experiment_schemas.MetricType], ids=["model", "schema"]
)
def test_every_metric_type_is_classified(enum):
    """A new ``MetricType`` member fails here until it is classified: as the
    proportion type (``conversion``) or in ``MEAN_METRIC_TYPES``."""
    values = {member.value for member in enum}
    assert values - {"conversion"} - set(ssa.MEAN_METRIC_TYPES) == set(), (
        "a metric type that is neither conversion nor in MEAN_METRIC_TYPES"
    )
    assert "conversion" not in ssa.MEAN_METRIC_TYPES
    assert set(ssa.MEAN_METRIC_TYPES) <= values
    assert set(ssa.EVENT_COUNT_METRIC_TYPES) <= set(ssa.MEAN_METRIC_TYPES)


def test_the_mean_types_are_every_type_but_conversion():
    assert ssa.MEAN_METRIC_TYPES == ("revenue", "count", "duration", "custom")
    assert ssa.EVENT_COUNT_METRIC_TYPES == ("count",)


# ---------------------------------------------------------------------------
# Count: the number of events per assigned user, whatever their value
# ---------------------------------------------------------------------------

#: 100 users per arm.  Control user i sends i % 4 events, treatment user i
#: sends i % 5, none of them with a value: 75 and 80 senders.
_F4_UNITS = {_CONTROL: 100, _TREATMENT: 100}
_F4_EVENTS: Events = {
    _CONTROL: {f"c{i}": [None] * (i % 4) for i in range(100)},
    _TREATMENT: {f"t{i}": [None] * (i % 5) for i in range(100)},
}


@pytest.mark.regression
def test_a_count_metric_whose_events_carry_no_value_is_not_0_vs_0(monkeypatch):
    """PE-5's planted defect: a count read through ``value_totals`` (the sum of
    ``value`` within the bound) is 0 for every user whose events carry no
    value, so the result is "0 vs 0" and nothing is ever reported.  Here the
    count is the number of events: 1.5 against 2.0.

    A routing guard only: the readers are faked with their SQL's semantics.
    The SQL (the value predicate kept for a count) is planted against Postgres
    in ``test_value_metrics_results.py``."""
    calls: List[str] = []
    output = _run(
        _metric("count"), _F4_UNITS, _F4_EVENTS, monkeypatch, reader_calls=calls
    )
    result, control, treatment = _arms(output)
    control_y = _per_user(_F4_EVENTS[_CONTROL], len)
    treatment_y = _per_user(_F4_EVENTS[_TREATMENT], len)
    welch = stats.ttest_ind(treatment_y, control_y, equal_var=False)

    assert result["analysis"] == "mean"
    assert calls == ["event_counts", "event_counts"]
    assert (control["mean"], treatment["mean"]) == (1.5, 2.0)
    assert (control["conversions"], treatment["conversions"]) == (75, 80)
    assert (control["sample_size"], treatment["sample_size"]) == (100, 100)
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)
    assert treatment["p_value"] == pytest.approx(0.006359822708272411, rel=1e-9)
    assert treatment["absolute_difference"] == pytest.approx(0.5, rel=1e-12)
    assert treatment["absolute_difference_ci"] == pytest.approx(
        tuple(welch.confidence_interval(0.95)), rel=1e-9
    )
    assert control["std_dev"] == pytest.approx(np.std(control_y, ddof=1), rel=1e-12)


def test_a_count_counts_every_event_whatever_its_value(monkeypatch):
    """Null, 0, negative, NaN, the infinities and values beyond the bound each
    count 1: a count does not read ``value``."""
    odd = [None, 0.0, -5.0, math.nan, math.inf, -math.inf, 1e308]
    events: Events = {
        _CONTROL: {"c0": odd, "c1": [1.0], "c2": []},
        _TREATMENT: {"t0": [2.0, 2.0], "t1": [None], "t2": []},
    }
    _, control, treatment = _arms(
        _run(_metric("count"), {_CONTROL: 3, _TREATMENT: 3}, events, monkeypatch)
    )
    assert control["mean"] == pytest.approx((len(odd) + 1) / 3, rel=1e-12)
    assert treatment["mean"] == pytest.approx(3 / 3, rel=1e-12)


def test_a_repeated_event_counts_each_time_it_was_stored(monkeypatch):
    """Tracking has no deduplication: an event stored twice counts twice."""
    events: Events = {
        _CONTROL: {"c0": [None], "c1": []},
        _TREATMENT: {"t0": [None, None], "t1": []},
    }
    _, control, treatment = _arms(
        _run(_metric("count"), {_CONTROL: 2, _TREATMENT: 2}, events, monkeypatch)
    )
    assert (control["mean"], treatment["mean"]) == (0.5, 1.0)


# ---------------------------------------------------------------------------
# Duration and custom: each user's total value, as revenue
# ---------------------------------------------------------------------------

#: 12 users per arm.  A repeat sender, a negative value, a value beyond the
#: bound and an event with no value are among them; the rest sent nothing.
_VALUE_UNITS = {_CONTROL: 12, _TREATMENT: 12}
_VALUE_EVENTS: Events = {
    _CONTROL: {
        "c0": [12.5],
        "c1": [30.0],
        "c2": [3.0, 4.25],
        "c3": [None],
        **{f"c{i}": [] for i in range(4, 12)},
    },
    _TREATMENT: {
        "t0": [40.0, 20.0],
        "t1": [18.5],
        "t2": [-2.0, 9.0],
        "t3": [7.75],
        "t4": [math.nextafter(VALUE_BOUND, math.inf), 1.0],
        **{f"t{i}": [] for i in range(5, 12)},
    },
}


def _total(values: List[Optional[float]]) -> float:
    return math.fsum(v for v in values if _counted(v))


@pytest.mark.parametrize("confidence", [0.95, 0.90])
@pytest.mark.parametrize("metric_type", ["duration", "custom"])
def test_duration_and_custom_match_scipy_on_the_per_user_values(
    metric_type, confidence, monkeypatch
):
    calls: List[str] = []
    output = _run(
        _metric(metric_type),
        _VALUE_UNITS,
        _VALUE_EVENTS,
        monkeypatch,
        confidence=confidence,
        reader_calls=calls,
    )
    result, control, treatment = _arms(output)
    control_y = _per_user(_VALUE_EVENTS[_CONTROL], _total)
    treatment_y = _per_user(_VALUE_EVENTS[_TREATMENT], _total)
    welch = stats.ttest_ind(treatment_y, control_y, equal_var=False)

    assert calls == ["value_totals", "value_totals"]
    assert result["analysis"] == "mean"
    assert result["metric_type"] == metric_type
    # Every assigned user counts, a user with none as 0; not the mean over
    # senders (12.4375 in the control) and not the mean per event.
    assert control["mean"] == pytest.approx(49.75 / 12, rel=1e-12)
    assert treatment["mean"] == pytest.approx(94.25 / 12, rel=1e-12)
    assert (control["conversions"], treatment["conversions"]) == (4, 5)
    for variant, y in ((control, control_y), (treatment, treatment_y)):
        assert variant["std_dev"] == pytest.approx(np.std(y, ddof=1), rel=1e-12)
        half = stats.t.ppf(1 - (1 - confidence) / 2, len(y) - 1) * stats.sem(y)
        assert variant["confidence_interval"] == pytest.approx(
            (y.mean() - half, y.mean() + half), rel=1e-12
        )
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)
    assert treatment["absolute_difference"] == pytest.approx(
        treatment_y.mean() - control_y.mean(), rel=1e-12
    )
    assert treatment["absolute_difference_ci"] == pytest.approx(
        tuple(welch.confidence_interval(confidence)), rel=1e-9
    )


def test_a_duration_is_time_per_assigned_user_not_per_event(monkeypatch):
    """The duration trap (UX; limitations.md): a page that loads in 3 s for
    the half of the treatment's users who still send the event reads as less
    time per user than one that loads in 2 s for every control user, because
    a duration is each user's total averaged over every assigned user.  No
    metric averages over events."""
    events: Events = {
        _CONTROL: {f"c{i}": [2.0] for i in range(10)},
        _TREATMENT: {f"t{i}": [3.0] if i < 5 else [] for i in range(10)},
    }
    _, control, treatment = _arms(
        _run(_metric("duration"), {_CONTROL: 10, _TREATMENT: 10}, events, monkeypatch)
    )
    assert control["mean"] == pytest.approx(2.0, rel=1e-12)
    assert treatment["mean"] == pytest.approx(1.5, rel=1e-12)
    assert treatment["absolute_difference"] == pytest.approx(-0.5, rel=1e-12)


@pytest.mark.parametrize("metric_type", ["duration", "custom"])
def test_values_not_sent_read_as_zero_with_their_senders_counted(
    metric_type, monkeypatch
):
    """Users sent a duration or custom metric's event with no value: every
    total is 0, nothing is compared, and the senders still count.  That is
    the shape the dashboard's "values not sent" note reads
    (``buyersWithNoValue``), so it is not shown as a measured 0."""
    events: Events = {
        _CONTROL: {"c0": [None], "c1": [None, None], "c2": []},
        _TREATMENT: {"t0": [None], "t1": [], "t2": []},
    }
    _, control, treatment = _arms(
        _run(_metric(metric_type), {_CONTROL: 3, _TREATMENT: 3}, events, monkeypatch)
    )
    for variant in (control, treatment):
        assert variant["mean"] == 0.0
        assert variant["std_dev"] == 0.0
    assert (control["conversions"], treatment["conversions"]) == (2, 1)
    assert treatment["p_value"] is None
    assert treatment["absolute_difference_ci"] is None
    assert treatment["is_significant"] is False


# ---------------------------------------------------------------------------
# The two fields nothing reads
# ---------------------------------------------------------------------------


def test_aggregation_method_and_event_value_path_change_nothing(monkeypatch):
    """Two metrics that differ only in the fields give the same result."""
    outputs = []
    for aggregation_method, event_value_path in (
        ("sum", "properties.amount"),
        ("average", None),
    ):
        metric = _metric(
            "custom",
            id="44444444-0000-4000-8000-000000000439",
            aggregation_method=aggregation_method,
            event_value_path=event_value_path,
        )
        output = _run(metric, _VALUE_UNITS, _VALUE_EVENTS, monkeypatch)
        outputs.append(json.dumps(output, sort_keys=True, default=str))
    assert outputs[0] == outputs[1]


#: The descriptions #439 gives the two fields, and the metric's type, on the
#: request and the response alike.
_DESCRIPTIONS = {
    "aggregation_method": (
        "Not read: the analysis does not use it, and how a metric is analysed "
        "follows from metric_type (see analysis in the results).  Stored and "
        "returned as sent."
    ),
    "event_value_path": (
        "Not read: the analysis does not use it.  A revenue, duration or "
        "custom metric adds up each event's top-level value; a count or "
        "conversion metric does not read value, and values inside an event's "
        "properties are never read.  Stored and returned as sent."
    ),
    "metric_type": (
        "What the metric measures.  conversion: the share of assigned users "
        "who sent event_name.  count: the number of event_name events per "
        "assigned user; their value is not read.  revenue, duration and "
        "custom: the total value of a user's event_name events, per assigned "
        "user.  In each mean a user with none counts as 0."
    ),
}


@pytest.mark.regression
@pytest.mark.parametrize("model", [MetricBase, MetricResponse])
def test_the_fields_nothing_reads_say_so(model):
    """#439: the descriptions said "How to aggregate the metric" and "JSON
    path to extract value from event payload", and no analysis has ever read
    either field."""
    fields = model.model_fields
    for name, description in _DESCRIPTIONS.items():
        assert fields[name].description == description, name


def test_no_example_sends_the_fields_nothing_reads():
    examples = [
        MetricBase.model_config["json_schema_extra"]["example"],
        *experiment_schemas.ExperimentCreate.model_config["json_schema_extra"][
            "example"
        ]["metrics"],
    ]
    for example in examples:
        assert "aggregation_method" not in example
        assert "event_value_path" not in example


# ---------------------------------------------------------------------------
# The pinned output, per engine version (PS-6)
# ---------------------------------------------------------------------------

#: The last engine version that analysed a count, duration or custom metric
#: as the share of users who sent its event (#1385's; #439 is 1.7.0).
VALUE_SHARE_UNTIL = "1.6.0"

#: sha256 of the canonical ``get_experiment_results`` output for the
#: experiment below (a count primary, a duration and a custom metric), per
#: engine version.  Only versions after ``VALUE_SHARE_UNTIL`` have an entry.
#: Registered in ``test_engine_versions_frozen.PIN_TABLES``, so a frozen
#: version's entry here is frozen too.
VALUE_RESULTS_FINGERPRINTS: Dict[str, str] = {
    "1.7.0": "989e0ff6d76177e6e675257634f16a18fb9b4b43a9e3dce919cdadff33be11e2",
    # 1.8.0 (#1496): each treatment carries the relative change's interval and
    # the impact; without them the output is 1.7.0's
    # (test_difference_intervals.py).
    "1.8.0": "01ce8332a03c5cf0032f72277011ac993875cd40c4a6b3c8d127c74e24da2e96",
}


def _value_metrics_output(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    return routing._results(
        [
            routing._metric(MetricType.COUNT, is_primary=True),
            routing._metric(MetricType.DURATION),
            routing._metric(MetricType.CUSTOM),
        ],
        monkeypatch,
    )


def test_the_value_metric_results_are_pinned_for_the_running_engine(monkeypatch):
    assert ENGINE_VERSION in VALUE_RESULTS_FINGERPRINTS, (
        f"ENGINE_VERSION {ENGINE_VERSION} has no pinned count, duration and "
        "custom results; see this module's docstring."
    )
    output = _value_metrics_output(monkeypatch)
    assert {m["analysis"] for m in output["metrics"]} == {"mean"}
    assert routing._fingerprint(output) == VALUE_RESULTS_FINGERPRINTS[ENGINE_VERSION]


@pytest.mark.regression
def test_the_value_metric_analysis_is_not_pinned_under_an_engine_that_shared_them():
    """#439 changes the numbers of every count, duration and custom metric, so
    it runs under an engine version after ``VALUE_SHARE_UNTIL``, the last one
    that analysed them as a share of users.  ``VALUE_SHARE_UNTIL`` is frozen:
    an engine frozen on main cannot take a new analysis under its number."""
    released = sufficient_stats.RELEASED_ENGINE_VERSIONS
    key = sufficient_stats._key
    assert VALUE_SHARE_UNTIL in released, (
        f"{VALUE_SHARE_UNTIL}, the last engine that analysed count, duration "
        "and custom metrics as a share of users, is not recorded as frozen"
    )
    for version in VALUE_RESULTS_FINGERPRINTS:
        assert key(version) > key(VALUE_SHARE_UNTIL), (
            f"the count, duration and custom analysis is pinned under "
            f"{version}, which analysed them as a share of users: bump "
            "ENGINE_VERSION instead"
        )
    assert key(ENGINE_VERSION) > key(VALUE_SHARE_UNTIL), (
        f"ENGINE_VERSION {ENGINE_VERSION} analysed count, duration and custom "
        "metrics as a share of users: bump ENGINE_VERSION"
    )


def test_conversion_and_revenue_numbers_are_the_engine_befores(monkeypatch):
    """#439 moves count, duration and custom only: every other pinned output
    under the engine that introduced it is the one ``VALUE_SHARE_UNTIL``
    pinned.

    Anchored to #439's own version, the first in
    ``VALUE_RESULTS_FINGERPRINTS``, not to the running engine, so a later
    engine-changing PR (#1496 at 1.8.0) does not break it; and
    ``VALUE_SHARE_UNTIL`` must be the frozen version immediately before that
    one."""
    released = sufficient_stats.RELEASED_ENGINE_VERSIONS
    value_first = min(VALUE_RESULTS_FINGERPRINTS, key=sufficient_stats._key)
    assert value_first in released
    assert released[released.index(value_first) - 1] == VALUE_SHARE_UNTIL, (
        f"VALUE_SHARE_UNTIL {VALUE_SHARE_UNTIL} is not the frozen version "
        f"before {value_first}, the first with count, duration and custom pins"
    )
    tables = {
        "characterisation": characterisation.CHARACTERISATIONS,
        "engine": engine_fingerprint.ENGINE_FINGERPRINTS,
        "sufficient stats": sufficient_stats.SUFFICIENT_STATS_FINGERPRINTS,
        "lower is better": lower_is_better.LOWER_IS_BETTER_FINGERPRINTS,
        "one treatment": correction_pins.ONE_TREATMENT_PINS,
        "revenue": routing.REVENUE_RESULTS_FINGERPRINTS,
    }
    for name, table in tables.items():
        if VALUE_SHARE_UNTIL in table:
            assert table[value_first] == table[VALUE_SHARE_UNTIL], name
