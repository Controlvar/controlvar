"""
The sufficient-statistics estimators' output, pinned per ``ENGINE_VERSION``
and per function.

``backend/app/services/sufficient_stats_analysis.py`` computes metric results
from per-variant counts or sums, for ``/results`` and for callers with no
Controlvar database rows (warehouse analysis, #312).  This test runs fixed
counts through each estimator, hashes the outputs and compares the hash with
the one pinned for the current ``ENGINE_VERSION`` **and that function**.

It is separate from ``ENGINE_FINGERPRINTS`` in ``test_engine_fingerprint.py``,
which is not edited for these functions.  The rules:

* never edit the hash of a (version, function) pair that has shipped in a
  release;
* a new estimator adds its own key under the current version; it does not
  touch another function's entry;
* when an estimator's output changes after its version shipped, bump
  ``ENGINE_VERSION`` (see ``test_engine_fingerprint.py``) and add the new
  version's entries, keeping the old ones.
"""

import hashlib
import json
from types import SimpleNamespace
from typing import Any, Callable, Dict

import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
    cuped_metric_result,
    mean_metric_result,
)
from backend.tests.unit.services.test_binomial_metric_result_characterisation import (
    _FIXTURES,
    _canonical,
    _experiment,
    results_output,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

#: sha256 of the canonical outputs below, per engine version, per function.
SUFFICIENT_STATS_FINGERPRINTS: Dict[str, Dict[str, str]] = {
    "1.1.0": {
        "binomial_metric_result": "cd5de7d0dd07d9e53e847cd8bb56a2b7e1c1164b590e2370c50f35aee74e0411",
        "mean_metric_result": "ad119bc6bfff1ec62f70b7499e7c7eb29341f3a12df69587935128964e03125c",
    },
    # #454: the proportion interval uses norm.ppf at 1 - alpha instead of a
    # fixed 1.96.  The mean path already used t.ppf at alpha: 1.1.0's hash.
    "1.2.0": {
        "binomial_metric_result": "c65e109491f6e0968e0f5f8e248fcad9f571826960095622cf76e813516eba8a",
        "mean_metric_result": "ad119bc6bfff1ec62f70b7499e7c7eb29341f3a12df69587935128964e03125c",
    },
    # #217: CUPED from per-arm sums, new in 1.3.0.  The other two are 1.2.0's.
    "1.3.0": {
        "binomial_metric_result": "c65e109491f6e0968e0f5f8e248fcad9f571826960095622cf76e813516eba8a",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "ad119bc6bfff1ec62f70b7499e7c7eb29341f3a12df69587935128964e03125c",
    },
    # #854: the sequential route only.  These three are 1.3.0's.
    "1.4.0": {
        "binomial_metric_result": "c65e109491f6e0968e0f5f8e248fcad9f571826960095622cf76e813516eba8a",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "ad119bc6bfff1ec62f70b7499e7c7eb29341f3a12df69587935128964e03125c",
    },
}

#: Engine versions that have shipped in a release.  A function new in this
#: release is pinned under a later version only, never added to one of these
#: (a new key under a released version passes every hash, which is how a
#: changed engine could keep a released version number).
RELEASED_ENGINE_VERSIONS = ("1.1.0", "1.2.0", "1.3.0")

_CONTROL = "ffffffff-0000-4000-8000-000000000002"
_TREATMENT_1 = "00000000-0000-4000-8000-000000000001"
_TREATMENT_2 = "77777777-0000-4000-8000-000000000003"

_METRIC = SimpleNamespace(
    id="33333333-0000-4000-8000-000000000003",
    name="Converted",
    metric_type="conversion",
    is_primary=True,
)


def _binomial_outputs() -> Dict[str, Any]:
    """Fixed counts through ``binomial_metric_result``."""
    control = BinomialVariant(_CONTROL, "control", True)
    t1 = BinomialVariant(_TREATMENT_1, "t1", False)
    t2 = BinomialVariant(_TREATMENT_2, "t2", False)
    count_sets = {
        # Treatment first: the control is found by flag, not position.
        "three": [(t1, 1000, 150), (control, 1000, 120), (t2, 1000, 131)],
        "unbalanced": [(control, 2000, 40), (t1, 150, 9), (t2, 30000, 690)],
        "edges": [(t1, 0, 0), (control, 500, 0), (t2, 400, 12)],
    }
    outputs: Dict[str, Any] = {}
    for name, counts in count_sets.items():
        for alpha in (0.05, 0.10):
            for correction in ("none", "bonferroni", "benjamini_hochberg"):
                outputs[f"{name}/{alpha}/{correction}"] = binomial_metric_result(
                    counts, alpha, correction, metric=_METRIC
                )
    return outputs


_MEAN_METRIC = SimpleNamespace(
    id="44444444-0000-4000-8000-000000000004",
    name="Revenue per user",
    metric_type="revenue",
    is_primary=False,
)


def _mean_outputs() -> Dict[str, Any]:
    """Fixed centred sums through ``mean_metric_result``."""
    control = BinomialVariant(_CONTROL, "control", True)
    t1 = BinomialVariant(_TREATMENT_1, "t1", False)
    t2 = BinomialVariant(_TREATMENT_2, "t2", False)
    sum_sets = {
        # (k, [(variant, n, sum_d, sum_d2), ...]); treatment first again.
        "three": (
            42.5,
            [
                (t1, 1000, 180.0, 16300.0),
                (control, 1000, -150.0, 15800.0),
                (t2, 1000, -30.0, 16100.0),
            ],
        ),
        "unbalanced": (
            1e6,
            [
                (control, 2000, -12.0, 8000.5),
                (t1, 150, 30.0, 700.0),
                (t2, 30000, -18.0, 121000.0),
            ],
        ),
        "no_variation": (
            3.0,
            [(control, 10, 0.0, 0.0), (t1, 12, 12.0, 12.0), (t2, 5, 5.0, 9.0)],
        ),
    }
    outputs: Dict[str, Any] = {}
    for name, (k, sums) in sum_sets.items():
        for alpha in (0.05, 0.10):
            for correction in ("none", "bonferroni", "benjamini_hochberg"):
                outputs[f"{name}/{alpha}/{correction}"] = mean_metric_result(
                    k, sums, alpha, correction, metric=_MEAN_METRIC
                )
    return outputs


def _cuped_outputs() -> Dict[str, Any]:
    """Fixed per-arm sums through ``cuped_metric_result``."""
    control = BinomialVariant(_CONTROL, "control", True)
    t1 = BinomialVariant(_TREATMENT_1, "t1", False)
    t2 = BinomialVariant(_TREATMENT_2, "t2", False)
    sum_sets = {
        # (variant, n, sum_y, sum_x, sum_y2, sum_x2, sum_xy); treatment first.
        "three": [
            (t1, 1000, 150.0, 300.0, 150.0, 300.0, 80.0),
            (control, 1000, 120.0, 310.0, 120.0, 310.0, 70.0),
            (t2, 1000, 131.0, 290.0, 131.0, 290.0, 66.0),
        ],
        "unbalanced": [
            (control, 2000, 40.0, 500.0, 40.0, 500.0, 25.0),
            (t1, 150, 9.0, 30.0, 9.0, 30.0, 5.0),
            (t2, 30000, 690.0, 7000.0, 690.0, 7000.0, 400.0),
        ],
        "edges": [
            (t1, 0, 0.0, 0.0, 0.0, 0.0, 0.0),
            (control, 500, 10.0, 0.0, 10.0, 0.0, 0.0),
            (t2, 400, 12.0, 0.0, 12.0, 0.0, 0.0),
        ],
    }
    outputs: Dict[str, Any] = {}
    for name, sums in sum_sets.items():
        for level in (0.95, 0.90):
            for correction in ("none", "bonferroni", "benjamini_hochberg"):
                for adjust in (True, False):
                    outputs[f"{name}/{level}/{correction}/{adjust}"] = (
                        cuped_metric_result(
                            sums, level, correction, metric=_METRIC, adjust=adjust
                        )
                    )
    return outputs


#: Every function this file pins, by the key it is pinned under.
_ESTIMATORS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "binomial_metric_result": _binomial_outputs,
    "cuped_metric_result": _cuped_outputs,
    "mean_metric_result": _mean_outputs,
}


def _fingerprint(outputs: Dict[str, Any]) -> str:
    payload = json.dumps(_canonical(outputs), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("function", sorted(_ESTIMATORS))
def test_sufficient_stats_fingerprint(function):
    pinned = SUFFICIENT_STATS_FINGERPRINTS.get(ENGINE_VERSION, {})
    actual = _fingerprint(_ESTIMATORS[function]())
    assert function in pinned, (
        f"{function} has no pinned fingerprint for ENGINE_VERSION "
        f"{ENGINE_VERSION}; add {actual!r} (see this module's docstring)."
    )
    assert actual == pinned[function], (
        f"{function}'s output changed under ENGINE_VERSION {ENGINE_VERSION} "
        f"(fingerprint {actual}). See this module's docstring."
    )


#: The release that introduced ``cuped_metric_result`` (#217, v0.21.0).
CUPED_FIRST_VERSION = "1.3.0"


def _key(version):
    return tuple(int(part) for part in version.split("."))


def test_cuped_metric_result_is_pinned_only_from_the_version_that_added_it():
    """#217 changed CUPED's numbers after 1.2.0 shipped, so it bumped the
    engine to 1.3.0 and pinned ``cuped_metric_result`` there.

    The rule: CUPED is absent from every version released before #217 (1.1.0
    and 1.2.0), and present under 1.3.0 and under the running version.  1.3.0
    shipped in v0.21.0, so it is in ``RELEASED_ENGINE_VERSIONS``; its CUPED
    entry is the one that shipped, not a key added afterwards.
    """
    assert CUPED_FIRST_VERSION in RELEASED_ENGINE_VERSIONS
    for version in RELEASED_ENGINE_VERSIONS:
        pinned = "cuped_metric_result" in SUFFICIENT_STATS_FINGERPRINTS[version]
        if _key(version) < _key(CUPED_FIRST_VERSION):
            assert not pinned, (
                f"cuped_metric_result is pinned under {version}, which shipped "
                f"before #217"
            )
        else:
            assert pinned, f"cuped_metric_result is missing from {version}"
    assert "cuped_metric_result" in SUFFICIENT_STATS_FINGERPRINTS[ENGINE_VERSION]


def test_running_version_is_not_older_than_a_released_one():
    """``ENGINE_VERSION`` never goes back below a version that has shipped."""
    assert _key(ENGINE_VERSION) >= max(map(_key, RELEASED_ENGINE_VERSIONS))


def test_every_pinned_function_is_exercised():
    """A pinned key with no estimator behind it would pass by being skipped."""
    assert set(SUFFICIENT_STATS_FINGERPRINTS[ENGINE_VERSION]) == set(_ESTIMATORS)


@pytest.mark.parametrize("fixture_name", sorted(_FIXTURES))
@pytest.mark.parametrize(
    "confidence, correction",
    [(0.95, "none"), (0.95, "bonferroni"), (0.90, "benjamini_hochberg")],
)
def test_binomial_metric_result_is_what_results_returns(
    fixture_name, confidence, correction, monkeypatch
):
    """Same counts in, the same objects out: ``==``, not a rounded hash."""
    fixture = _FIXTURES[fixture_name]
    experiment = _experiment()
    output = results_output(fixture_name, confidence, correction, monkeypatch)
    for metric, from_results in zip(
        sorted(experiment.metric_definitions, key=lambda m: not m.is_primary),
        output["metrics"],
    ):
        assert from_results["metric_id"] == metric.id
        counts = [
            (
                v,
                fixture.units[v.id],
                fixture.converted[(metric.event_name, v.id)],
            )
            for v in experiment.variants
        ]
        assert (
            binomial_metric_result(counts, 1.0 - confidence, correction, metric=metric)
            == from_results
        )


def test_binomial_metric_result_needs_a_control():
    with pytest.raises(ValueError, match="control"):
        binomial_metric_result(
            [(BinomialVariant(_TREATMENT_1, "t1", False), 10, 1)],
            0.05,
            "none",
            metric=_METRIC,
        )
