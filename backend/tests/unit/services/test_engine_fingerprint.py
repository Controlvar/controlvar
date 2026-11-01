"""
The statistics engine's output fingerprint, pinned per ``ENGINE_VERSION``.

``ENGINE_VERSION`` is stamped on persisted snapshots, bandit state and every
Bayesian/CUPED response, and it is part of the ``/results`` cache key.  It only
means something if it moves whenever the numbers move.  This test runs a fixed
dataset through every analysis path, hashes the outputs, and compares the hash
with the one pinned for the current version.

When this test fails, the engine's output changed.  The rule: an
engine-changing PR bumps ``ENGINE_VERSION`` to the next minor and appends it to
``RELEASED_ENGINE_VERSIONS`` ("frozen on main") in the same change.  A frozen
version's entries are never edited, and no key is ever added under one.  A PR
is engine-changing when, for the same database rows and request, it adds,
removes or changes any number, decision (winner, recommendation, Bayesian
decision, ``can_stop``) or allocation weight that a path stamping
``engine_version`` returns, caches or persists: results and its snapshots,
Bayesian, CUPED, sequential, and the bandit's state, history and status.  A
revert of an engine-changing PR bumps again.

So bump ``ENGINE_VERSION`` in ``backend/app/core/stats_engine.py`` (both
OpenAPI snapshots follow it), append it to ``RELEASED_ENGINE_VERSIONS`` in
``test_sufficient_stats_fingerprint.py``, add the new version's hash here and
in every other pin table, and add its digest to ``FROZEN_DIGESTS`` in
``test_engine_versions_frozen.py``.  Never edit an existing entry.
"""

import dataclasses
import enum
import hashlib
import json
import math
from typing import Any, Dict
from unittest.mock import MagicMock

import numpy as np
import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.bayesian_service import BayesianService
from backend.app.services.cuped_service import CupedService
from backend.app.services.dimensional_analysis_service import (
    DimensionalAnalysisService,
)
from backend.app.services.sequential_testing_service import SequentialTestingService

pytestmark = pytest.mark.unit

#: sha256 of the canonical outputs below, per engine version.  1.0.0 was never
#: fingerprinted; 1.1.0 is the first version this test pins.  1.2.0 carries
#: two changes released together: #454 moved the proportion path's interval
#: multiplier and significance level (pinned by
#: ``test_sufficient_stats_fingerprint.py``, not by this dataset), and #231
#: replaced the sequential confidence sequence with the inverted mSPRT, which
#: moves the ``cs`` and ``analysis`` outputs below.  #231 also clips that
#: interval to [-1, 1]; the interval of this dataset lies inside that range,
#: so the clip leaves the 1.2.0 fingerprint as it was.  #242, in the same
#: version, made STOP_WINNER need a probability to be best of 0.975: the
#: ``bayesian`` case (0.97465) and the added ``bayesian_aa`` case both
#: answered STOP_WINNER before it and CONTINUE after.  The 1.1.0 entry is kept
#: as released; it predates the ``bayesian_aa`` case.
ENGINE_FINGERPRINTS: Dict[str, str] = {
    "1.1.0": "2feed80e7e305c3c8d0b183e1f2905799e8af73b34aa674cd358fa5948d2f089",
    "1.2.0": "4860dd07b1052f1f47329d61cc5fa72f58a1721177f07bb1e912d00754a0a2c3",
    # 1.3.0 is #217: CUPED's covariate and estimator moved to
    # ``cuped_metric_result`` (pinned in test_sufficient_stats_fingerprint.py).
    # ``CupedService``, the path this dataset runs, is unchanged.
    "1.3.0": "4860dd07b1052f1f47329d61cc5fa72f58a1721177f07bb1e912d00754a0a2c3",
    # 1.4.0 is #854: the sequential mSPRT and confidence sequence use the
    # Agresti-Caffo variance of the difference instead of the plug-in one,
    # which moves the ``msprt``, ``cs`` and ``analysis`` outputs below.
    "1.4.0": "003234aa770b7f48e4d42ee0e718895523371c24251dadb6e46dc8355b7e346c",
    # 1.5.0 is #1377: the winner, the recommendation and the Bayesian
    # decision read lower_is_better.  This dataset is higher-is-better
    # throughout, so its output is 1.4.0's.
    "1.5.0": "003234aa770b7f48e4d42ee0e718895523371c24251dadb6e46dc8355b7e346c",
    # 1.6.0 is #1385: a lower-is-better bandit's weights, regret and history
    # alpha/beta.  This dataset runs no bandit, so its output is 1.5.0's.
    "1.6.0": "003234aa770b7f48e4d42ee0e718895523371c24251dadb6e46dc8355b7e346c",
    # 1.7.0 is #439: count, duration and custom metrics are means in
    # /results.  This dataset has none, so its output is 1.6.0's.
    "1.7.0": "003234aa770b7f48e4d42ee0e718895523371c24251dadb6e46dc8355b7e346c",
}

# Two ids where the control sorts AFTER the treatment, so an engine that
# guesses the control from the smaller id produces a different fingerprint.
_CONTROL = "ffffffff-0000-4000-8000-000000000002"
_TREATMENT = "00000000-0000-4000-8000-000000000001"


def _canonical(value: Any) -> Any:
    """Reduce an output to JSON with floats at 10 significant digits.

    The rounding absorbs last-bit differences between BLAS/libm builds (the
    developer's laptop and the CI runner), and nothing an operator would see.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _canonical(dataclasses.asdict(value))
    if isinstance(value, enum.Enum):
        return _canonical(value.value)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_canonical(v) for v in value.tolist()]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return repr(f)
        return float(f"{f:.10g}")
    return value


def _engine_outputs() -> Dict[str, Any]:
    """Run the fixed dataset through each analysis path."""
    outputs: Dict[str, Any] = {}

    # Frequentist helpers behind /results.
    analysis = AnalysisService(MagicMock())
    outputs["frequentist"] = {
        "z_test": analysis.z_test_proportions(120, 1000, 150, 1000),
        "welch": analysis.welch_t_test(
            [1.0, 2.5, 3.1, 4.8, 2.2, 3.3], [2.0, 3.5, 4.1, 5.2, 3.9, 4.4]
        ),
        "cohens_h": analysis.cohens_h(0.12, 0.15),
        "cohens_d": analysis.cohens_d(
            [1.0, 2.5, 3.1, 4.8, 2.2, 3.3], [2.0, 3.5, 4.1, 5.2, 3.9, 4.4]
        ),
        "wilson": analysis.wilson_confidence_interval(120, 1000, 0.95),
        "bonferroni": analysis.apply_bonferroni_correction(0.02, 3),
        "required_n": analysis.calculate_required_sample_size(0.12, 0.02),
        "bh": AnalysisService._adjusted_p_values(
            [0.01, 0.04, None, 0.03], "benjamini_hochberg"
        ),
    }

    # Dimensional breakdown (?breakdown=), control flagged on the larger id.
    dim = DimensionalAnalysisService()
    segments = {
        "desktop": {
            _TREATMENT: {
                "variant_name": "treatment",
                "is_control": False,
                "total": 400,
                "conversions": 70,
            },
            _CONTROL: {
                "variant_name": "control",
                "is_control": True,
                "total": 410,
                "conversions": 50,
            },
        },
        "mobile": {
            _TREATMENT: {
                "variant_name": "treatment",
                "is_control": False,
                "total": 300,
                "conversions": 30,
            },
            _CONTROL: {
                "variant_name": "control",
                "is_control": True,
                "total": 290,
                "conversions": 33,
            },
        },
    }
    segment_results = dim.compute_segment_results(segments, base_alpha=0.05)
    outputs["breakdown"] = {
        "segments": segment_results,
        "has_hte": dim.detect_hte(segment_results),
    }

    # Bayesian (/results bayesian_results, /bayesian), fixed seed.
    bayes = BayesianService().analyze(
        [{"conversions": 120, "total": 1000}, {"conversions": 150, "total": 1000}],
        n_samples=20_000,
        seed=20260926,
    )
    bayes.pop("engine_version", None)
    outputs["bayesian"] = bayes

    # Two identical 1% arms at 5,000 users each (#242): the expected loss is
    # below the default 0.001, so the pre-#242 rule answered STOP_WINNER; the
    # probability to be best is about 0.5, so the current rule answers CONTINUE.
    bayes_aa = BayesianService().analyze(
        [{"conversions": 50, "total": 5000}, {"conversions": 50, "total": 5000}],
        n_samples=20_000,
        seed=20260926,
    )
    bayes_aa.pop("engine_version", None)
    outputs["bayesian_aa"] = bayes_aa

    # Sequential (/sequential).
    seq = SequentialTestingService()
    outputs["sequential"] = {
        "msprt": seq.compute_msprt(120, 1000, 150, 1000),
        "cs": seq.compute_always_valid_ci(120, 1000, 150, 1000),
        # The full analysis at a non-default alpha, 45 days into a 14-day
        # plan: pins the 1/alpha boundary, the empty alpha_spending (#232)
        # and the time-based case answering "continue", not futility.
        "analysis": seq.run_sequential_analysis(
            120,
            1000,
            150,
            1000,
            {
                "alpha": 0.01,
                "actual_days": 45,
                "expected_days": 14,
                "required_sample_size": 10000,
            },
        ),
    }

    # CUPED (/cuped), deterministic covariates.
    rng = np.random.default_rng(7)
    cx = rng.normal(10.0, 2.0, 200)
    tx = rng.normal(10.0, 2.0, 200)
    cy = 0.8 * cx + rng.normal(0.0, 1.0, 200)
    ty = 0.8 * tx + 0.3 + rng.normal(0.0, 1.0, 200)
    outputs["cuped"] = CupedService.compute_cuped_effect(cy, cx, ty, tx)

    return outputs


def engine_fingerprint() -> str:
    """sha256 of the canonical JSON of every analysis output."""
    payload = json.dumps(
        _canonical(_engine_outputs()), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_engine_output_matches_the_fingerprint_for_this_version():
    assert ENGINE_VERSION in ENGINE_FINGERPRINTS, (
        f"ENGINE_VERSION {ENGINE_VERSION} has no pinned fingerprint; add "
        f"{engine_fingerprint()!r} for it (see this module's docstring)."
    )
    actual = engine_fingerprint()
    assert actual == ENGINE_FINGERPRINTS[ENGINE_VERSION], (
        f"The statistics engine's output changed under ENGINE_VERSION "
        f"{ENGINE_VERSION} (fingerprint {actual}). An engine-changing PR "
        "bumps ENGINE_VERSION to the next minor and appends it to "
        "RELEASED_ENGINE_VERSIONS; never edit this version's fingerprint. "
        "See this module's docstring."
    )


def test_fingerprint_is_stable_within_a_process():
    assert engine_fingerprint() == engine_fingerprint()
