"""
What engine 1.5.0 changed, and the proof that it changed nothing else.

1.5.0 makes the winner, the recommendation and the Bayesian decision read the
metric's ``lower_is_better`` (#1377), and adds that field to every metric
result.  For a metric that is higher-is-better, whether ``lower_is_better`` is
False or the definition has no such attribute at all, the output is 1.4.0's
plus the new field: these tests drop the field and compare with the hashes
pinned for 1.4.0, which is frozen and never edited.  A proportion result's
variants also carry the difference fields #1496 added later; those are dropped
too (``test_difference_intervals.py`` holds them).

The lower-is-better outputs are pinned here per ``ENGINE_VERSION``.  The rule:
an engine-changing PR bumps ``ENGINE_VERSION`` to the next minor and appends it
to ``RELEASED_ENGINE_VERSIONS`` ("frozen on main") in the same change.  A
frozen version's entries are never edited, and no key is ever added under one.
A PR is engine-changing when, for the same database rows and request, it adds,
removes or changes any number, decision (winner, recommendation, Bayesian
decision, ``can_stop``) or allocation weight that a path stamping
``engine_version`` returns, caches or persists: results and its snapshots,
Bayesian, CUPED, sequential, and the bandit's state, history and status.  A
revert of an engine-changing PR bumps again.
"""

import copy
import hashlib
import json
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.services.sufficient_stats_analysis import (
    DIFFERENCE_FIELDS,
    BinomialVariant,
    binomial_metric_result,
    mean_metric_result,
)
from backend.tests.unit.services import (
    test_binomial_metric_result_characterisation as characterisation,
)
from backend.tests.unit.services import (
    test_sufficient_stats_fingerprint as sufficient_stats,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

#: The last engine before the direction was read; it is frozen.
BEFORE_DIRECTION = "1.4.0"

#: Leave the attribute off the metric altogether.
ABSENT = object()


def _with_direction(metric: Any, lower_is_better: Any) -> SimpleNamespace:
    fields = dict(vars(metric))
    fields.pop("lower_is_better", None)
    if lower_is_better is not ABSENT:
        fields["lower_is_better"] = lower_is_better
    return SimpleNamespace(**fields)


#: The estimators whose results carry the #1496 fields, added after 1.5.0.
_CARRY_DIFFERENCE_FIELDS = {"binomial_metric_result"}


def _without_the_difference_fields(metric: Dict[str, Any]) -> None:
    """Drop the fields #1496 added after 1.5.0 from a proportion result's
    variants, after checking each carries them all.
    ``test_difference_intervals.py`` holds those fields to their own oracles
    and holds the rest of the output to the engine before them."""
    for variant in metric["variants"]:
        for field in DIFFERENCE_FIELDS:
            assert field in variant, field
            del variant[field]


def _without_the_field(outputs: Dict[str, Any], function: str) -> Dict[str, Any]:
    """The estimator outputs less ``lower_is_better``, after checking that
    every one of them carried it as False (so the strip is not vacuous), and
    less the #1496 fields for an estimator that carries them."""
    stripped = copy.deepcopy(outputs)
    for output in stripped.values():
        assert output.pop("lower_is_better") is False
        if function in _CARRY_DIFFERENCE_FIELDS:
            _without_the_difference_fields(output)
    return stripped


def _hash(outputs: Any) -> str:
    payload = json.dumps(
        characterisation._canonical(outputs), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("lower_is_better", [ABSENT, False, None], ids=repr)
@pytest.mark.parametrize(
    "function, metric_name",
    [
        ("binomial_metric_result", "_METRIC"),
        ("mean_metric_result", "_MEAN_METRIC"),
    ],
)
def test_a_higher_is_better_metric_gives_the_output_of_1_4_0(
    function, metric_name, lower_is_better, monkeypatch
):
    """The estimators' fixed fixtures, with the attribute absent, False and a
    stored NULL: 1.4.0's hash once the new field is dropped."""
    metric = getattr(sufficient_stats, metric_name)
    monkeypatch.setattr(
        sufficient_stats, metric_name, _with_direction(metric, lower_is_better)
    )
    outputs = sufficient_stats._ESTIMATORS[function]()
    assert (
        _hash(_without_the_field(outputs, function))
        == sufficient_stats.SUFFICIENT_STATS_FINGERPRINTS[BEFORE_DIRECTION][function]
    )


@pytest.mark.parametrize("lower_is_better", [ABSENT, False], ids=repr)
@pytest.mark.parametrize("case", sorted(characterisation.CHARACTERISATION))
def test_results_for_higher_is_better_metrics_are_those_of_1_4_0(
    case, lower_is_better, monkeypatch
):
    """``/results`` through the real entry point: 1.4.0's characterisation
    once each metric's new field is dropped."""
    make_experiment = characterisation._experiment

    def experiment():
        built = make_experiment()
        built.metric_definitions = [
            _with_direction(m, lower_is_better) for m in built.metric_definitions
        ]
        return built

    monkeypatch.setattr(characterisation, "_experiment", experiment)
    fixture_name, confidence, correction = case.split("-")
    output = characterisation.results_output(
        fixture_name, float(confidence), correction, monkeypatch
    )
    for metric in output["metrics"]:
        assert metric.pop("lower_is_better") is False
        _without_the_difference_fields(metric)
    actual, _payload = characterisation._fingerprint(output)
    assert actual == characterisation.CHARACTERISATIONS[BEFORE_DIRECTION][case]


_CONTROL = BinomialVariant("ffffffff-0000-4000-8000-000000000002", "control", True)
_T1 = BinomialVariant("00000000-0000-4000-8000-000000000001", "t1", False)
_T2 = BinomialVariant("77777777-0000-4000-8000-000000000003", "t2", False)

_LOWER_METRIC = SimpleNamespace(
    id="55555555-0000-4000-8000-000000000005",
    name="Uninstall",
    metric_type="conversion",
    is_primary=True,
    lower_is_better=True,
)


def _lower_is_better_binomial() -> Dict[str, Any]:
    """Each count set has a treatment significantly lower than the control."""
    count_sets = {
        "drop": [(_T1, 2000, 125), (_CONTROL, 2000, 200)],
        "two_drops": [(_CONTROL, 2000, 200), (_T1, 2000, 150), (_T2, 2000, 100)],
        "mixed": [(_T2, 2000, 300), (_CONTROL, 2000, 200), (_T1, 2000, 100)],
    }
    outputs: Dict[str, Any] = {}
    for name, counts in count_sets.items():
        for alpha in (0.05, 0.10):
            for correction in ("none", "bonferroni", "benjamini_hochberg"):
                outputs[f"{name}/{alpha}/{correction}"] = binomial_metric_result(
                    counts, alpha, correction, metric=_LOWER_METRIC
                )
    return outputs


def _lower_is_better_mean() -> Dict[str, Any]:
    """Treatment 1's mean (1.7) is significantly below the control's (2.2)."""
    k, sums = (
        2.0,
        [
            (_T2, 500, 40.0, 950.0),
            (_CONTROL, 500, 100.0, 1000.0),
            (_T1, 500, -150.0, 1100.0),
        ],
    )
    outputs: Dict[str, Any] = {}
    for alpha in (0.05, 0.10):
        for correction in ("none", "bonferroni", "benjamini_hochberg"):
            outputs[f"drop/{alpha}/{correction}"] = mean_metric_result(
                k, sums, alpha, correction, metric=_LOWER_METRIC
            )
    return outputs


_LOWER_IS_BETTER_ESTIMATORS = {
    "binomial_metric_result": _lower_is_better_binomial,
    "mean_metric_result": _lower_is_better_mean,
}

#: sha256 of the lower-is-better outputs above, per engine version, per
#: function.  1.5.0 is the first engine that reads the direction.
LOWER_IS_BETTER_FINGERPRINTS: Dict[str, Dict[str, str]] = {
    "1.5.0": {
        "binomial_metric_result": "427251bbac41bc5f639179de2e55944d85bf2a7d831c71c538aecb2915208ab3",
        "mean_metric_result": "11b6c73a38a716f809bfeb2ef9735045459973fbaa457087c4409074456952df",
    },
    # 1.6.0 (#1385) changes a lower-is-better bandit only: 1.5.0's.
    "1.6.0": {
        "binomial_metric_result": "427251bbac41bc5f639179de2e55944d85bf2a7d831c71c538aecb2915208ab3",
        "mean_metric_result": "11b6c73a38a716f809bfeb2ef9735045459973fbaa457087c4409074456952df",
    },
    # 1.7.0 (#439) routes count, duration and custom to the mean in /results;
    # these estimators are unchanged: 1.6.0's.
    "1.7.0": {
        "binomial_metric_result": "427251bbac41bc5f639179de2e55944d85bf2a7d831c71c538aecb2915208ab3",
        "mean_metric_result": "11b6c73a38a716f809bfeb2ef9735045459973fbaa457087c4409074456952df",
    },
}


@pytest.mark.parametrize("function", sorted(_LOWER_IS_BETTER_ESTIMATORS))
def test_lower_is_better_fingerprint(function):
    outputs = _LOWER_IS_BETTER_ESTIMATORS[function]()
    assert all(o["lower_is_better"] is True for o in outputs.values())
    # Every fixture has a winner: a significant treatment below the control.
    assert all(o["winning_variant_id"] is not None for o in outputs.values())
    pinned = LOWER_IS_BETTER_FINGERPRINTS.get(ENGINE_VERSION, {})
    actual = _hash(outputs)
    assert function in pinned, (
        f"{function} has no pinned lower-is-better fingerprint for "
        f"ENGINE_VERSION {ENGINE_VERSION}; add {actual!r} (see this module's "
        "docstring)."
    )
    assert actual == pinned[function], (
        f"{function}'s lower-is-better output changed under ENGINE_VERSION "
        f"{ENGINE_VERSION} (fingerprint {actual}). See this module's docstring."
    )


def test_the_engine_that_reads_the_direction_is_not_a_frozen_one():
    """1.4.0 is frozen without the direction, so the engine that reads it
    must be a later version."""
    assert BEFORE_DIRECTION in sufficient_stats.RELEASED_ENGINE_VERSIONS
    assert sufficient_stats._key(ENGINE_VERSION) > sufficient_stats._key(
        BEFORE_DIRECTION
    )
