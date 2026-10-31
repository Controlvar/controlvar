"""
Characterisation of the proportion results ``/results`` returns today.

``AnalysisService.get_experiment_results`` turns per-variant counts (units
assigned, units converted) into the legacy ``metrics_results`` dicts and the
schema-shaped ``metrics``: conversion rate, normal-approximation interval,
Fisher exact p-value, multiple-comparison correction, Cohen's h, observed
power and the winner.  This test pins that output for fixed counts, so the
computation can move into ``binomial_metric_result`` (#404) with the proof
that nothing changed: the test is committed before the move and passes,
unchanged, on both sides of it.

It drives the real entry point.  Only the database is replaced: the session
answers the per-variant assignment count, and ``count_converting_users``
answers the per-variant converting units.  The summary (totals, duration) is
not part of this computation and is fixed.

When this test fails, the numbers ``/results`` returns changed.  That is only
acceptable with an ``ENGINE_VERSION`` bump (see ``test_engine_fingerprint.py``),
which adds the new version's hashes to ``CHARACTERISATIONS``; never re-pin a
hash here to make a refactor pass.
"""

import dataclasses
import enum
import hashlib
import json
import math
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.services import analysis_service as analysis_module
from backend.app.services.analysis_service import AnalysisService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

# The control sorts between the two treatments, and is not listed first, so
# an implementation that guesses the control from position or id order
# produces different numbers.
_TREATMENT_A = "00000000-0000-4000-8000-00000000000a"
_CONTROL = "88888888-0000-4000-8000-00000000000c"
_TREATMENT_B = "ffffffff-0000-4000-8000-00000000000b"

_PRIMARY_METRIC = "11111111-0000-4000-8000-000000000001"
_SECONDARY_METRIC = "22222222-0000-4000-8000-000000000002"


@dataclasses.dataclass(frozen=True)
class _Fixture:
    """Units per variant, and converting units per (event, variant)."""

    units: Dict[str, int]
    converted: Dict[Tuple[str, str], int]


_FIXTURES: Dict[str, _Fixture] = {
    # Three variants, two metrics.  Treatment A's purchase p (0.035) clears
    # alpha = 0.05 uncorrected but not after Bonferroni or Benjamini-Hochberg
    # (0.071), which clears alpha = 0.10: both the correction and alpha move
    # the verdict and the recommendation.
    "three_variants": _Fixture(
        units={_TREATMENT_A: 1000, _CONTROL: 1010, _TREATMENT_B: 980},
        converted={
            ("purchase", _TREATMENT_A): 149,
            ("purchase", _CONTROL): 118,
            ("purchase", _TREATMENT_B): 131,
            ("signup", _TREATMENT_A): 402,
            ("signup", _CONTROL): 399,
            ("signup", _TREATMENT_B): 355,
        },
    ),
    # The edges: a control with no conversions (relative improvement is
    # infinite, reported as None) and a treatment with no units.
    "edges": _Fixture(
        units={_TREATMENT_A: 0, _CONTROL: 500, _TREATMENT_B: 400},
        converted={
            ("purchase", _TREATMENT_A): 0,
            ("purchase", _CONTROL): 0,
            ("purchase", _TREATMENT_B): 12,
            ("signup", _TREATMENT_A): 0,
            ("signup", _CONTROL): 250,
            ("signup", _TREATMENT_B): 200,
        },
    ),
}

#: sha256 of the canonical ``/results`` output per ``ENGINE_VERSION`` and per
#: (fixture, confidence level, correction method).  1.1.0 was recorded from
#: the code before #404.  1.2.0 is #454: the interval multiplier is
#: ``norm.ppf(1 - (1 - level) / 2)`` instead of a fixed 1.96 (which also moves
#: the 95% intervals, in about the sixth significant digit), and the legacy
#: ``is_significant`` is decided at ``1 - level`` instead of 0.05.  The rules
#: are the fingerprints' (``test_engine_fingerprint.py``): an engine-changing
#: PR bumps ``ENGINE_VERSION`` to the next minor and appends it to
#: ``RELEASED_ENGINE_VERSIONS`` ("frozen on main") in the same change; a frozen
#: version's entries are never edited, and no key is ever added under one.  A
#: PR is engine-changing when, for the same database rows and request, it adds,
#: removes or changes any number, decision (winner, recommendation, Bayesian
#: decision, ``can_stop``) or allocation weight that a path stamping
#: ``engine_version`` returns, caches or persists: results and its snapshots,
#: Bayesian, CUPED, sequential, and the bandit's state, history and status.  A
#: revert of an engine-changing PR bumps again.
CHARACTERISATIONS: Dict[str, Dict[str, str]] = {
    "1.1.0": {
        "three_variants-0.95-none": "69488213db4a93d25b2ca00c0ed2f52674c24691fd8607c91ab567d5b07aed01",
        "three_variants-0.95-bonferroni": "d647c4f2971c0897b04bdb83597f7392ae737a5f723ccf467c888e78d18b647b",
        "three_variants-0.95-benjamini_hochberg": "bf8ea4c8179c1931fe7804637c701d1deabb54ec5f1e58915bfecd58b026df5b",
        "three_variants-0.9-benjamini_hochberg": "f0506143e40ae69169850edc39d74df4133607888f58cff64109423c124906e3",
        "edges-0.95-none": "9a53a0f0de2c4f0c65768c7b07365d989f0e19c395b59462ab26440ae33fa39a",
        "edges-0.95-bonferroni": "1737bfaf7c8f9692b899134c043e6d13032f45177cd5eb8dbfeff79da9c0326a",
    },
    "1.2.0": {
        "three_variants-0.95-none": "d897bf322f4fe5cca38d5e9acc0a43e42477e2a6282ba57ea31c3b7c3902c76d",
        "three_variants-0.95-bonferroni": "1485e9cf00349a258ec93da464d6b80b791cf06d7c9e20483eb3b50237c74303",
        "three_variants-0.95-benjamini_hochberg": "bcb71934fe55430b61665e883f0079f84e2133565c25f4f0003ce37f4be62b25",
        "three_variants-0.9-benjamini_hochberg": "9dc29fe6a32a100937e291cdb811fb7ab614c73cca809f13e89395655361331f",
        "edges-0.95-none": "8a393ac383215c05be91713bf0994289b03126f806ee3ff7b4ba27e4c1496e88",
        "edges-0.95-bonferroni": "d823df91499efad14663e562d09f0ef215926ca87b2746d9148846f1dcb66979",
    },  # 1.3.0 (#217) changes CUPED only; /results is as in 1.2.0.
    "1.3.0": {
        "three_variants-0.95-none": "d897bf322f4fe5cca38d5e9acc0a43e42477e2a6282ba57ea31c3b7c3902c76d",
        "three_variants-0.95-bonferroni": "1485e9cf00349a258ec93da464d6b80b791cf06d7c9e20483eb3b50237c74303",
        "three_variants-0.95-benjamini_hochberg": "bcb71934fe55430b61665e883f0079f84e2133565c25f4f0003ce37f4be62b25",
        "three_variants-0.9-benjamini_hochberg": "9dc29fe6a32a100937e291cdb811fb7ab614c73cca809f13e89395655361331f",
        "edges-0.95-none": "8a393ac383215c05be91713bf0994289b03126f806ee3ff7b4ba27e4c1496e88",
        "edges-0.95-bonferroni": "d823df91499efad14663e562d09f0ef215926ca87b2746d9148846f1dcb66979",
    },  # 1.4.0 (#854) changes the sequential route only.
    "1.4.0": {
        "three_variants-0.95-none": "d897bf322f4fe5cca38d5e9acc0a43e42477e2a6282ba57ea31c3b7c3902c76d",
        "three_variants-0.95-bonferroni": "1485e9cf00349a258ec93da464d6b80b791cf06d7c9e20483eb3b50237c74303",
        "three_variants-0.95-benjamini_hochberg": "bcb71934fe55430b61665e883f0079f84e2133565c25f4f0003ce37f4be62b25",
        "three_variants-0.9-benjamini_hochberg": "9dc29fe6a32a100937e291cdb811fb7ab614c73cca809f13e89395655361331f",
        "edges-0.95-none": "8a393ac383215c05be91713bf0994289b03126f806ee3ff7b4ba27e4c1496e88",
        "edges-0.95-bonferroni": "d823df91499efad14663e562d09f0ef215926ca87b2746d9148846f1dcb66979",
    },  # 1.5.0 (#1377): each metric result carries lower_is_better (False
    # here); without it these are 1.4.0's (test_lower_is_better_fingerprint.py).
    "1.5.0": {
        "three_variants-0.95-none": "7a6ca24d92fe5ef7ef9ad1833f24a0b0c8cd7c0db0e6556e54af334ee56b2d83",
        "three_variants-0.95-bonferroni": "69d361d903b23a6208356313627323fa088b87bf0e18eed42e8893043d94564b",
        "three_variants-0.95-benjamini_hochberg": "1d8407308e95ab257fc526cde9d041d909590ba3984f1218472cccc9eb54b98e",
        "three_variants-0.9-benjamini_hochberg": "ebb0ac8fb607cb07f9f815beaad98c1c63f5bf82a5e9ddd6b5568a575a294543",
        "edges-0.95-none": "fac2456d1239a6cc1e837f0ef1b886fdb6642227aa23458721377f53559c9172",
        "edges-0.95-bonferroni": "9d1452d5768d8b75a1260c3e9dca19e6926bee4f88f3305edb2a8944b0457cde",
    },
    # 1.6.0 (#1385) changes a lower-is-better bandit only; /results is as in
    # 1.5.0.
    "1.6.0": {
        "three_variants-0.95-none": "7a6ca24d92fe5ef7ef9ad1833f24a0b0c8cd7c0db0e6556e54af334ee56b2d83",
        "three_variants-0.95-bonferroni": "69d361d903b23a6208356313627323fa088b87bf0e18eed42e8893043d94564b",
        "three_variants-0.95-benjamini_hochberg": "1d8407308e95ab257fc526cde9d041d909590ba3984f1218472cccc9eb54b98e",
        "three_variants-0.9-benjamini_hochberg": "ebb0ac8fb607cb07f9f815beaad98c1c63f5bf82a5e9ddd6b5568a575a294543",
        "edges-0.95-none": "fac2456d1239a6cc1e837f0ef1b886fdb6642227aa23458721377f53559c9172",
        "edges-0.95-bonferroni": "9d1452d5768d8b75a1260c3e9dca19e6926bee4f88f3305edb2a8944b0457cde",
    },
    # 1.7.0 (#439) routes count, duration and custom to the mean; these
    # conversion metrics are 1.6.0's.
    "1.7.0": {
        "three_variants-0.95-none": "7a6ca24d92fe5ef7ef9ad1833f24a0b0c8cd7c0db0e6556e54af334ee56b2d83",
        "three_variants-0.95-bonferroni": "69d361d903b23a6208356313627323fa088b87bf0e18eed42e8893043d94564b",
        "three_variants-0.95-benjamini_hochberg": "1d8407308e95ab257fc526cde9d041d909590ba3984f1218472cccc9eb54b98e",
        "three_variants-0.9-benjamini_hochberg": "ebb0ac8fb607cb07f9f815beaad98c1c63f5bf82a5e9ddd6b5568a575a294543",
        "edges-0.95-none": "fac2456d1239a6cc1e837f0ef1b886fdb6642227aa23458721377f53559c9172",
        "edges-0.95-bonferroni": "9d1452d5768d8b75a1260c3e9dca19e6926bee4f88f3305edb2a8944b0457cde",
    },
}

#: The cases pinned for the running engine.  The same six keys in every
#: version, so a version with no entry fails every case rather than none.
CHARACTERISATION: Dict[str, str] = CHARACTERISATIONS["1.1.0"]


def _canonical(value: Any) -> Any:
    """JSON-safe copy with floats at 10 significant digits.

    The same rounding as ``test_engine_fingerprint.py``: it absorbs last-bit
    libm/BLAS differences between the laptop and the CI runner, and a moved
    interval multiplier, alpha or test statistic changes far more.
    """
    if isinstance(value, enum.Enum):
        return _canonical(value.value)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return repr(value)
        return float(f"{value:.10g}")
    if value is None or isinstance(value, str):
        return value
    return str(value)


class _FakeQuery:
    """The two query shapes the proportion path issues against the session."""

    def __init__(self, session: "_FakeSession") -> None:
        self._session = session
        self._clauses: List[Any] = []

    def options(self, *_args: Any) -> "_FakeQuery":
        return self

    def filter(self, *clauses: Any) -> "_FakeQuery":
        self._clauses.extend(clauses)
        return self

    def first(self) -> Any:
        return self._session.experiment

    def scalar(self) -> int:
        # func.count(Assignment.id) filtered on experiment_id and variant_id.
        for clause in self._clauses:
            if getattr(getattr(clause, "left", None), "key", None) == "variant_id":
                return self._session.fixture.units[str(clause.right.value)]
        raise AssertionError(
            "unexpected scalar query in the proportion path: "
            f"{[str(c) for c in self._clauses]}"
        )


class _FakeSession:
    def __init__(self, experiment: Any, fixture: _Fixture) -> None:
        self.experiment = experiment
        self.fixture = fixture

    def query(self, *_entities: Any) -> _FakeQuery:
        return _FakeQuery(self)


def _experiment() -> SimpleNamespace:
    variants = [
        SimpleNamespace(id=_TREATMENT_A, name="treatment-a", is_control=False),
        SimpleNamespace(id=_CONTROL, name="control", is_control=True),
        SimpleNamespace(id=_TREATMENT_B, name="treatment-b", is_control=False),
    ]
    metrics = [
        SimpleNamespace(
            id=_SECONDARY_METRIC,
            name="Signed up",
            event_name="signup",
            metric_type="conversion",
            is_primary=False,
            minimum_sample_size=100,
        ),
        SimpleNamespace(
            id=_PRIMARY_METRIC,
            name="Purchased",
            event_name="purchase",
            metric_type="conversion",
            is_primary=True,
            minimum_sample_size=100,
        ),
    ]
    return SimpleNamespace(
        id="99999999-0000-4000-8000-000000000009",
        name="Checkout copy",
        status="active",
        start_date=None,
        end_date=None,
        variants=variants,
        metric_definitions=metrics,
    )


def results_output(
    fixture_name: str,
    confidence_level: float,
    correction_method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Dict[str, Any]:
    """``get_experiment_results`` on the fixture, less ``computed_at``."""
    fixture = _FIXTURES[fixture_name]
    experiment = _experiment()

    def converting_units(_db: Any, _experiment_id: Any, variant_id: Any, event: str):
        return fixture.converted[(event, str(variant_id))]

    monkeypatch.setattr(analysis_module, "count_converting_users", converting_units)
    monkeypatch.setattr(
        AnalysisService,
        "calculate_experiment_summary",
        lambda self, experiment: {
            "total_users": sum(fixture.units.values()),
            "total_events": 0,
            "total_conversions": 0,
            "duration_days": None,
        },
    )
    service = AnalysisService(_FakeSession(experiment, fixture))
    output = service.get_experiment_results(
        experiment.id,
        confidence_level=confidence_level,
        correction_method=correction_method,
        include_bayesian=False,
    )
    output.pop("computed_at")
    # The sample-ratio check (#880) is not part of this computation either:
    # the fake session cannot answer it, so it is null and the summary is
    # the engine's.  It came after these hashes, which are pinned without it.
    assert output.pop("srm") is None
    return output


def _fingerprint(output: Dict[str, Any]) -> Tuple[str, str]:
    payload = json.dumps(_canonical(output), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), payload


def test_every_version_pins_the_same_cases():
    assert all(
        set(pins) == set(CHARACTERISATION) for pins in CHARACTERISATIONS.values()
    )


@pytest.mark.parametrize("case", sorted(CHARACTERISATION))
def test_binomial_metric_result_characterisation(case, monkeypatch):
    assert ENGINE_VERSION in CHARACTERISATIONS, (
        f"ENGINE_VERSION {ENGINE_VERSION} has no pinned /results "
        "characterisation; see this module's docstring."
    )
    fixture_name, confidence, correction = case.split("-")
    output = results_output(fixture_name, float(confidence), correction, monkeypatch)
    actual, payload = _fingerprint(output)
    assert actual == CHARACTERISATIONS[ENGINE_VERSION][case], (
        f"/results output changed for {case}: {actual}\n{payload}"
    )
