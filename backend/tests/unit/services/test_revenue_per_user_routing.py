"""
Which metric types ``/results`` analyses as a mean, and the pinned output.

``AnalysisService.get_experiment_results`` analyses a revenue, count,
duration or custom metric as its mean value per assigned user (Welch's
t-test) and a conversion metric as the share of users who converted (Fisher's
exact test) (#439).  A count metric's per-user value is read with
``event_counts``, every other mean type's with ``value_totals``.  The routing
is held over every ``MetricType``, through the real entry point with only the
database replaced, as ``test_binomial_metric_result_characterisation.py``
replaces it.

The output for an experiment with a revenue primary metric is pinned per
``ENGINE_VERSION``.  The rule: an engine-changing PR bumps ``ENGINE_VERSION``
to the next minor and appends it to ``RELEASED_ENGINE_VERSIONS`` ("frozen on
main") in the same change.  A frozen version's entries are never edited, and
no key is ever added under one.  A PR is engine-changing when, for the same
database rows and request, it adds, removes or changes any number, decision
(winner, recommendation, Bayesian decision, ``can_stop``) or allocation weight
that a path stamping ``engine_version`` returns, caches or persists: results
and its snapshots, Bayesian, CUPED, sequential, and the bandit's state,
history and status.  A revert of an engine-changing PR bumps again.

The revenue analysis is new in 1.5.0 (v0.38.0 and v0.39.0).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models.experiment import MetricType
from backend.app.schemas.results import MetricResult
from backend.app.services import analysis_service as analysis_module
from backend.app.services.analysis_service import AnalysisService
from backend.tests.unit.services import (
    test_binomial_metric_result_characterisation as characterisation,
)
from backend.tests.unit.services import (
    test_sufficient_stats_fingerprint as sufficient_stats,
)

pytestmark = [pytest.mark.unit]

_CONTROL = "88888888-0000-4000-8000-00000000000c"
_TREATMENT = "00000000-0000-4000-8000-00000000000a"

#: Users per variant, and users who sent each metric's event.
_UNITS = {_CONTROL: 400, _TREATMENT: 400}
_CONVERTED = {_CONTROL: 40, _TREATMENT: 60}
#: Value totals of the control's and the treatment's buyers.
_TOTALS = {
    _CONTROL: [40.0 + (i % 9) * 5.0 for i in range(40)],
    _TREATMENT: [55.0 + (i % 13) * 4.0 for i in range(60)],
}
#: Events per user of the control's and the treatment's users who sent one.
_COUNTS = {
    _CONTROL: [float(1 + i % 3) for i in range(40)],
    _TREATMENT: [float(1 + i % 4) for i in range(60)],
}

#: sha256 of the canonical ``get_experiment_results`` output for the revenue
#: experiment below, per engine version.
REVENUE_RESULTS_FINGERPRINTS: Dict[str, str] = {
    "1.5.0": "5ade759aa8ce18ef518dfde1acc9d6a3c2a3eda4dc1d31d8898e68d62225ef06",
    # 1.6.0 (#1385) changes a lower-is-better bandit only: 1.5.0's.
    "1.6.0": "5ade759aa8ce18ef518dfde1acc9d6a3c2a3eda4dc1d31d8898e68d62225ef06",
    # 1.7.0 (#439) routes count, duration and custom to the mean; this
    # experiment's revenue and conversion results are 1.6.0's.
    "1.7.0": "5ade759aa8ce18ef518dfde1acc9d6a3c2a3eda4dc1d31d8898e68d62225ef06",
}


def _metric(metric_type: MetricType, is_primary: bool = False) -> SimpleNamespace:
    name = metric_type.value
    return SimpleNamespace(
        id=str(uuid.UUID(int=list(MetricType).index(metric_type) + 1)),
        name=f"{name} metric",
        event_name=f"{name}_event",
        metric_type=metric_type,
        is_primary=is_primary,
        minimum_sample_size=100,
    )


def _experiment(metrics: List[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        id="99999999-0000-4000-8000-000000000009",
        name="Checkout total",
        status="active",
        start_date=None,
        end_date=None,
        variants=[
            SimpleNamespace(id=_TREATMENT, name="treatment", is_control=False),
            SimpleNamespace(id=_CONTROL, name="control", is_control=True),
        ],
        metric_definitions=metrics,
    )


def _results(
    metrics: List[SimpleNamespace], monkeypatch: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """``get_experiment_results`` with the counts and totals above."""
    fixture = characterisation._Fixture(units=_UNITS, converted={})
    totals_read: List[str] = []
    counts_read: List[str] = []

    def converting(_db, _experiment_id, variant_id, _event):
        return _CONVERTED[str(variant_id)]

    def value_totals(_self, _experiment_id, variant_id, event_name):
        totals_read.append(event_name)
        return list(_TOTALS[str(variant_id)])

    def event_counts(_self, _experiment_id, variant_id, event_name):
        counts_read.append(event_name)
        return list(_COUNTS[str(variant_id)])

    monkeypatch.setattr(analysis_module, "count_converting_users", converting)
    monkeypatch.setattr(AnalysisService, "value_totals", value_totals)
    monkeypatch.setattr(AnalysisService, "event_counts", event_counts)
    monkeypatch.setattr(
        AnalysisService,
        "calculate_experiment_summary",
        lambda self, experiment: {
            "total_users": sum(_UNITS.values()),
            "total_events": 0,
            "total_conversions": 0,
            "duration_days": None,
        },
    )
    experiment = _experiment(metrics)
    output = AnalysisService(
        characterisation._FakeSession(experiment, fixture)
    ).get_experiment_results(
        experiment.id,
        confidence_level=0.95,
        correction_method="none",
        include_bayesian=False,
    )
    output.pop("computed_at")
    assert output.pop("srm") is None
    output["totals_read_for"] = sorted(totals_read)
    if counts_read:
        output["counts_read_for"] = sorted(counts_read)
    return output


@pytest.mark.regression
def test_the_results_analyse_every_type_but_conversion_as_a_mean(monkeypatch):
    """#439: a count, duration or custom metric is its mean per assigned user,
    as a revenue metric is; only a conversion metric is a share of users.  On
    the code before #439 the count, duration and custom rows were
    ``("proportion", "fisher_exact")``."""
    metrics = [_metric(t, is_primary=t is MetricType.CONVERSION) for t in MetricType]
    output = _results(metrics, monkeypatch)

    routes = {}
    for result in output["metrics"]:
        [treatment] = [v for v in result["variants"] if not v["is_control"]]
        routes[result["metric_type"]] = (
            result.get("analysis", "proportion"),
            treatment["statistical_test_used"],
        )
    assert routes == {
        "conversion": ("proportion", "fisher_exact"),
        "revenue": ("mean", "welch_t_test"),
        "count": ("mean", "welch_t_test"),
        "duration": ("mean", "welch_t_test"),
        "custom": ("mean", "welch_t_test"),
    }
    # Revenue, duration and custom read event values, once per variant ...
    assert output["totals_read_for"] == sorted(
        ["revenue_event", "duration_event", "custom_event"] * 2
    )
    # ... and a count metric reads its number of events instead, never a sum.
    assert output["counts_read_for"] == ["count_event", "count_event"]
    # Each result is the schema's, and says how it was analysed.
    analysis = {
        r["metric_type"]: MetricResult.model_validate(r).analysis
        for r in output["metrics"]
    }
    assert analysis == {
        "conversion": "proportion",
        "revenue": "mean",
        "count": "mean",
        "duration": "mean",
        "custom": "mean",
    }


def test_a_revenue_primary_names_its_winner_in_the_summary(monkeypatch):
    output = _results([_metric(MetricType.REVENUE, is_primary=True)], monkeypatch)
    [result] = output["metrics"]
    treatment = next(v for v in result["variants"] if not v["is_control"])
    assert treatment["is_significant"] is True
    assert output["summary"]["winning_variant_id"] == _TREATMENT
    assert output["summary"]["recommendation"] == "SHIP_VARIANT"
    assert output["summary"]["recommendation_reason"] == (
        f"treatment shows {treatment['relative_improvement_pct']:.1f}% "
        f"improvement on revenue metric (p={treatment['p_value']:.4f})."
    )


def _fingerprint(output: Dict[str, Any]) -> str:
    payload = json.dumps(
        characterisation._canonical(output), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_the_revenue_results_are_pinned_for_the_running_engine(monkeypatch):
    assert ENGINE_VERSION in REVENUE_RESULTS_FINGERPRINTS, (
        f"ENGINE_VERSION {ENGINE_VERSION} has no pinned revenue results; see "
        "this module's docstring."
    )
    output = _results(
        [
            _metric(MetricType.REVENUE, is_primary=True),
            _metric(MetricType.CONVERSION),
        ],
        monkeypatch,
    )
    assert _fingerprint(output) == REVENUE_RESULTS_FINGERPRINTS[ENGINE_VERSION]


#: The engine that introduced the revenue analysis (v0.38.0).
REVENUE_FIRST_VERSION = "1.5.0"


def test_the_revenue_analysis_is_pinned_only_from_the_version_that_added_it():
    """A revenue metric was a proportion in every engine before 1.5.0, so the
    revenue entry is absent from 1.1.0-1.4.0 and present under 1.5.0 (which
    shipped, so it is in ``RELEASED_ENGINE_VERSIONS``) and the running version.
    """
    released = sufficient_stats.RELEASED_ENGINE_VERSIONS
    assert REVENUE_FIRST_VERSION in released
    for version in released:
        pinned = version in REVENUE_RESULTS_FINGERPRINTS
        if sufficient_stats._key(version) < sufficient_stats._key(
            REVENUE_FIRST_VERSION
        ):
            assert not pinned, (
                f"revenue is pinned under {version}, which shipped before it"
            )
        else:
            assert pinned, f"revenue is missing from {version}"
    assert ENGINE_VERSION in REVENUE_RESULTS_FINGERPRINTS
