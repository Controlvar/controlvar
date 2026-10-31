"""What the results paths return when no correction method is asked for (#580).

Each experiment stores a correction method and a confidence level,
Benjamini-Hochberg at 0.95 by default, and the results paths read them when
the request names none. The existing characterisation
(``test_binomial_metric_result_characterisation.py``) always passes the method
explicitly, so it cannot see a changed default. This file pins the behaviour
with **no** method. It was written against the earlier default, ``none``; the
change that made the stored setting the default shows, as its diff to the
expectations here, exactly which numbers moved.

* **One valid treatment (A3).** A two-variant experiment, and a three-variant
  one in which one treatment has no p-value, so ``k = 1``. Every field except
  ``adjusted_p_value`` is identical under ``none``, Bonferroni,
  Benjamini-Hochberg and the default (one hash per fixture). The default is
  the stored Benjamini-Hochberg, so ``adjusted_p_value`` equals the p-value,
  as under either correction; under ``none`` it is null.
* **Three variants (A6).** The characterisation's ``three_variants`` fixture:
  treatment A's purchase p is 0.035, significant at 0.95 uncorrected and not
  after Benjamini-Hochberg (0.071). The precondition test shows the fixture
  really moves the verdict. Then each path that takes the default --
  the service, the variant export and the report's variant rows,
  ``GET /results/{id}`` and ``GET /experiments/{id}/results`` -- is pinned to
  the stored Benjamini-Hochberg: treatment A not significant, adjusted p-value
  0.071.

Only the database is replaced, as in the characterisation.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api.deps import get_current_user, get_db
from backend.app.api.v1.endpoints import results as results_endpoints
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.main import app
from backend.app.models.user import User
from backend.app.services import analysis_service as analysis_module
from backend.app.services import sufficient_stats_analysis
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.export_service import ExportService
from backend.tests.unit.services import (
    test_binomial_metric_result_characterisation as characterisation,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

_TREATMENT_A = characterisation._TREATMENT_A
_CONTROL = characterisation._CONTROL
_TREATMENT_B = characterisation._TREATMENT_B
_Fixture = characterisation._Fixture
_FakeSession = characterisation._FakeSession

_METHODS = ("none", "bonferroni", "benjamini_hochberg")
#: A request that names no method takes the service's default.
_DEFAULT = "default"

#: The two-variant fixture: the three-variant counts without treatment B, so
#: treatment A's purchase p is still 0.035.
_TWO_VARIANTS = _Fixture(
    units={_TREATMENT_A: 1000, _CONTROL: 1010},
    converted={
        ("purchase", _TREATMENT_A): 149,
        ("purchase", _CONTROL): 118,
        ("signup", _TREATMENT_A): 402,
        ("signup", _CONTROL): 399,
    },
)

#: The k = 1 fixture: the three-variant counts, but treatment B's exact test
#: cannot be computed, so its p-value is null and treatment A is the only
#: valid comparison. ``binomial_variant_results`` reports a failed
#: ``fisher_exact`` as ``p_value = None``; that is the only way the
#: proportion path produces a null treatment p, so the failure is planted.
_ONE_NULL_P = characterisation._FIXTURES["three_variants"]

#: sha256 of the output less ``computed_at``, ``correction_method`` and every
#: ``adjusted_p_value``: the same under every method when ``k = 1``. Recorded
#: on main at ENGINE_VERSION 1.2.0.  The rule: an engine-changing PR bumps
#: ``ENGINE_VERSION`` to the next minor and appends it to
#: ``RELEASED_ENGINE_VERSIONS`` ("frozen on main") in the same change; a frozen
#: version's entries are never edited, and no key is ever added under one.  A
#: PR is engine-changing when, for the same database rows and request, it adds,
#: removes or changes any number, decision (winner, recommendation, Bayesian
#: decision, ``can_stop``) or allocation weight that a path stamping
#: ``engine_version`` returns, caches or persists: results and its snapshots,
#: Bayesian, CUPED, sequential, and the bandit's state, history and status.  A
#: revert of an engine-changing PR bumps again.
ONE_TREATMENT_PINS: Dict[str, Dict[str, str]] = {
    "1.2.0": {
        "two_variants": "ecd8090f0b63152209a0fc370137017fb3026ee0a629665302efc1206e33abbe",
        "three_variants_one_null_p": (
            "3dbc690054ecb0b00c537bf44c67879e95f2457e432db20eb18b596dd79e3641"
        ),
    },  # 1.3.0 (#217) changes CUPED only.
    "1.3.0": {
        "two_variants": "ecd8090f0b63152209a0fc370137017fb3026ee0a629665302efc1206e33abbe",
        "three_variants_one_null_p": (
            "3dbc690054ecb0b00c537bf44c67879e95f2457e432db20eb18b596dd79e3641"
        ),
    },  # 1.4.0 (#854) changes the sequential route only.
    "1.4.0": {
        "two_variants": "ecd8090f0b63152209a0fc370137017fb3026ee0a629665302efc1206e33abbe",
        "three_variants_one_null_p": (
            "3dbc690054ecb0b00c537bf44c67879e95f2457e432db20eb18b596dd79e3641"
        ),
    },  # 1.5.0 (#1377): each metric result carries lower_is_better (False here).
    "1.5.0": {
        "two_variants": "9e41155e9bf3433659bc62c1af0f86371613da5ce204d16108a258f037a71be6",
        "three_variants_one_null_p": (
            "0b23f7ba940954b161499973d2c8830487fa8c6fb4e8d0196c68f2d5191fb8b2"
        ),
    },
    # 1.6.0 (#1385) changes a lower-is-better bandit only: 1.5.0's.
    "1.6.0": {
        "two_variants": "9e41155e9bf3433659bc62c1af0f86371613da5ce204d16108a258f037a71be6",
        "three_variants_one_null_p": (
            "0b23f7ba940954b161499973d2c8830487fa8c6fb4e8d0196c68f2d5191fb8b2"
        ),
    },
    # 1.7.0 (#439) routes count, duration and custom to the mean; these
    # fixtures have none: 1.6.0's.
    "1.7.0": {
        "two_variants": "9e41155e9bf3433659bc62c1af0f86371613da5ce204d16108a258f037a71be6",
        "three_variants_one_null_p": (
            "0b23f7ba940954b161499973d2c8830487fa8c6fb4e8d0196c68f2d5191fb8b2"
        ),
    },
}

#: The correction a request with no method gets, on every path: the stored
#: one, which for these fixtures is the migration's default.
STORED_DEFAULT = "benjamini_hochberg"


def _experiment(variant_ids: List[str]) -> SimpleNamespace:
    experiment = characterisation._experiment()
    experiment.variants = [v for v in experiment.variants if v.id in variant_ids]
    experiment.correction_method = STORED_DEFAULT
    experiment.confidence_level = 0.95
    return experiment


@pytest.fixture
def fake_counts(monkeypatch: pytest.MonkeyPatch):
    """Point the service's two count sources at a fixture; returns a setter."""

    def use(fixture: _Fixture) -> None:
        def converting_units(
            _db: Any, _experiment_id: Any, variant_id: Any, event: str
        ):
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

    return use


@pytest.fixture
def treatment_b_has_no_p(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fisher_exact`` fails for treatment B's two tables only."""
    real = sufficient_stats_analysis.stats.fisher_exact
    tables = {
        (
            (
                _ONE_NULL_P.converted[(event, _TREATMENT_B)],
                _ONE_NULL_P.units[_TREATMENT_B]
                - _ONE_NULL_P.converted[(event, _TREATMENT_B)],
            ),
            (
                _ONE_NULL_P.converted[(event, _CONTROL)],
                _ONE_NULL_P.units[_CONTROL] - _ONE_NULL_P.converted[(event, _CONTROL)],
            ),
        )
        for event in ("purchase", "signup")
    }

    def fisher_exact(table, *args, **kwargs):
        if tuple(tuple(row) for row in table) in tables:
            raise ValueError("planted: this comparison cannot be computed")
        return real(table, *args, **kwargs)

    monkeypatch.setattr(sufficient_stats_analysis.stats, "fisher_exact", fisher_exact)


def _results(fixture: _Fixture, variant_ids: List[str], method: str) -> Dict[str, Any]:
    experiment = _experiment(variant_ids)
    service = AnalysisService(_FakeSession(experiment, fixture))
    kwargs: Dict[str, Any] = {"confidence_level": 0.95, "include_bayesian": False}
    if method != _DEFAULT:
        kwargs["correction_method"] = method
    output = service.get_experiment_results(experiment.id, **kwargs)
    output.pop("computed_at")
    # As in the characterisation: the sample-ratio check (#880) is not part
    # of this computation, the fake session cannot answer it, and the pins
    # came before it.
    assert output.pop("srm") is None
    return output


def _without_correction_labels(output: Dict[str, Any]) -> Dict[str, Any]:
    """The output less the method label and every ``adjusted_p_value``."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: strip(v)
                for k, v in value.items()
                if k not in ("adjusted_p_value", "correction_method")
            }
        if isinstance(value, (list, tuple)):
            return [strip(v) for v in value]
        return value

    return strip(output)


def _sha(output: Dict[str, Any]) -> Tuple[str, str]:
    payload = json.dumps(
        characterisation._canonical(output), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), payload


def _treatments(output: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    for metric in output["metrics"]:
        for variant in metric["variants"]:
            if not variant["is_control"]:
                yield metric["metric_name"], variant


_ONE_TREATMENT_CASES = {
    "two_variants": (_TWO_VARIANTS, [_TREATMENT_A, _CONTROL], False),
    "three_variants_one_null_p": (
        _ONE_NULL_P,
        [_TREATMENT_A, _CONTROL, _TREATMENT_B],
        True,
    ),
}


def _one_treatment_output(
    case: str, method: str, fake_counts, request
) -> Dict[str, Any]:
    fixture, variant_ids, null_b = _ONE_TREATMENT_CASES[case]
    fake_counts(fixture)
    if null_b:
        request.getfixturevalue("treatment_b_has_no_p")
    return _results(fixture, variant_ids, method)


# ---------------------------------------------------------------------------
# A3: one valid treatment
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", sorted(_ONE_TREATMENT_CASES))
def test_the_k1_fixture_has_exactly_one_valid_treatment_p(case, fake_counts, request):
    output = _one_treatment_output(case, "none", fake_counts, request)
    for metric in output["metrics"]:
        valid = [
            v
            for v in metric["variants"]
            if not v["is_control"] and v["p_value"] is not None
        ]
        assert [v["variant_id"] for v in valid] == [_TREATMENT_A], metric["metric_name"]
    if case == "three_variants_one_null_p":
        nulls = [v for _, v in _treatments(output) if v["p_value"] is None]
        assert {v["variant_id"] for v in nulls} == {_TREATMENT_B}
        assert len(nulls) == 2


@pytest.mark.parametrize("method", _METHODS + (_DEFAULT,))
@pytest.mark.parametrize("case", sorted(_ONE_TREATMENT_CASES))
def test_with_one_valid_treatment_every_method_gives_the_same_output(
    case, method, fake_counts, request
):
    assert ENGINE_VERSION in ONE_TREATMENT_PINS, (
        f"ENGINE_VERSION {ENGINE_VERSION} has no pinned one-treatment output"
    )
    output = _one_treatment_output(case, method, fake_counts, request)
    actual, payload = _sha(_without_correction_labels(output))
    assert actual == ONE_TREATMENT_PINS[ENGINE_VERSION][case], (
        f"/results output changed for {case} under {method}: {actual}\n{payload}"
    )


@pytest.mark.parametrize("case", sorted(_ONE_TREATMENT_CASES))
@pytest.mark.parametrize("method", ("bonferroni", "benjamini_hochberg"))
def test_with_one_valid_treatment_a_correction_reports_the_p_value(
    case, method, fake_counts, request
):
    output = _one_treatment_output(case, method, fake_counts, request)
    assert output["correction_method"] == method
    for name, variant in _treatments(output):
        assert variant["adjusted_p_value"] == variant["p_value"], name


@pytest.mark.parametrize("case", sorted(_ONE_TREATMENT_CASES))
def test_with_one_valid_treatment_no_method_is_the_stored_setting(
    case, fake_counts, request
):
    """The stored default: the adjusted p-value is the p-value, labelled
    ``benjamini_hochberg``."""
    output = _one_treatment_output(case, _DEFAULT, fake_counts, request)
    assert output["correction_method"] == STORED_DEFAULT
    for name, variant in _treatments(output):
        assert variant["adjusted_p_value"] == variant["p_value"], name
    assert output == _one_treatment_output(case, STORED_DEFAULT, fake_counts, request)


# ---------------------------------------------------------------------------
# A6: three variants, every path that takes the default
# ---------------------------------------------------------------------------
_THREE = characterisation._FIXTURES["three_variants"]
_ALL_THREE = [_TREATMENT_A, _CONTROL, _TREATMENT_B]
#: Treatment A's purchase p-value, and its Benjamini-Hochberg adjustment over
#: the two treatments: 2 x 0.0355 = 0.071, past alpha = 0.05.
_P_A = 0.035493461026190276
_P_A_BH = 0.07098692205238055


def _purchase_a(output: Dict[str, Any]) -> Dict[str, Any]:
    (metric,) = [m for m in output["metrics"] if m["metric_name"] == "Purchased"]
    (variant,) = [v for v in metric["variants"] if v["variant_id"] == _TREATMENT_A]
    return variant


def test_precondition_the_three_variant_fixture_moves_the_verdict(fake_counts):
    """Uncorrected, treatment A's purchase is significant; after
    Benjamini-Hochberg it is not. Without this the path pins below could not
    tell the two defaults apart."""
    fake_counts(_THREE)
    none = _purchase_a(_results(_THREE, _ALL_THREE, "none"))
    bh = _purchase_a(_results(_THREE, _ALL_THREE, "benjamini_hochberg"))
    assert none["p_value"] == pytest.approx(_P_A, rel=1e-9)
    assert none["is_significant"] is True
    assert none["adjusted_p_value"] is None
    assert bh["p_value"] == pytest.approx(_P_A, rel=1e-9)
    assert bh["adjusted_p_value"] == pytest.approx(_P_A_BH, rel=1e-9)
    assert bh["is_significant"] is False


def test_the_service_default_is_the_stored_setting(fake_counts):
    """What the variant export and the report use: no method passed."""
    fake_counts(_THREE)
    output = _results(_THREE, _ALL_THREE, _DEFAULT)
    assert output["correction_method"] == STORED_DEFAULT
    assert _purchase_a(output)["is_significant"] is False
    assert _purchase_a(output)["adjusted_p_value"] == pytest.approx(_P_A_BH, rel=1e-9)


def test_the_variant_export_and_report_rows_are_the_stored_setting(fake_counts):
    """``/export/variants`` and the report's variant rows share
    ``_variants_to_rows``; purchase is the primary metric."""
    fake_counts(_THREE)
    experiment = _experiment(_ALL_THREE)
    rows = ExportService(_FakeSession(experiment, _THREE))._variants_to_rows(
        [experiment]
    )
    (row,) = [r for r in rows if r.variant_id == _TREATMENT_A]
    assert row.p_value == pytest.approx(_P_A, rel=1e-9)
    assert row.is_significant is False


@pytest.fixture
def superuser_results_client(
    fake_counts, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The app over the fake session, as a superuser, with no cache, no SRM
    query and no snapshot write (none of them is part of this computation)."""
    fake_counts(_THREE)
    session = _FakeSession(_experiment(_ALL_THREE), _THREE)
    user = MagicMock(spec=User)
    user.id = "12345678-1234-5678-1234-567812345678"
    user.is_active = True
    user.is_superuser = True
    user.role = "ADMIN"

    def no_cache():
        raise RuntimeError("no cache in this test")

    monkeypatch.setattr(results_endpoints, "_get_cache_service", no_cache)
    monkeypatch.setattr(analysis_module, "sample_ratio_check", lambda *_a: None)
    monkeypatch.setattr(
        results_endpoints, "_record_results_snapshots", lambda *_a: None
    )

    def override_get_db():
        yield session

    async def override_get_current_user():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


_EXPERIMENT_ID = characterisation._experiment().id


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/results/{_EXPERIMENT_ID}",
        f"/api/v1/experiments/{_EXPERIMENT_ID}/results",
    ],
    ids=["results", "experiments-alias"],
)
def test_the_results_routes_with_no_method_are_the_stored_setting(
    superuser_results_client, path
):
    response = superuser_results_client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["correction_method"] == STORED_DEFAULT
    assert body["confidence_level"] == 0.95
    variant = _purchase_a(body)
    assert variant["p_value"] == pytest.approx(_P_A, rel=1e-9)
    assert variant["is_significant"] is False
    assert variant["adjusted_p_value"] == pytest.approx(_P_A_BH, rel=1e-9)


def test_the_results_route_honours_an_explicit_method(superuser_results_client):
    """Control for the route pins: the same request naming ``none`` does
    change the verdict, so the routes are computing, not replaying."""
    response = superuser_results_client.get(
        f"/api/v1/results/{_EXPERIMENT_ID}",
        params={"correction_method": "none"},
    )
    assert response.status_code == 200, response.text
    variant = _purchase_a(response.json())
    assert variant["is_significant"] is True
    assert variant["adjusted_p_value"] is None
