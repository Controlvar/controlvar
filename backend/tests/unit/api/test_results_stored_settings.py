"""``/results`` judges an experiment by its stored settings (#580).

Each experiment stores a ``correction_method`` and a ``confidence_level``.
``GET /results/{id}`` and its alias ``GET /experiments/{id}/results`` use them
when the request names none, and a request that names one uses it for that
request only. These tests run the real routes and the real analysis over the
characterisation's fake session (only the database is replaced), and pin:

* the alias passes None into the route, so it follows the stored settings,
  whatever they are, rather than any fixed value;
* a request naming ``none`` on a Benjamini-Hochberg experiment is labelled
  ``none`` and carries no adjusted p-value: the labels come from the
  computation, not from the request;
* the stored confidence level moves the verdict;
* the cache key is built from the resolved settings, so a setting changed in
  draft is never answered from an entry computed under the old one;
* the analysis snapshot is written only for a computation under the stored
  settings;
* an unknown method surfaces as a 500, never as a 404 and never as an
  export row with blank results.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api.deps import get_current_user, get_db
from backend.app.api.v1.endpoints import results as results_endpoints
from backend.app.main import app
from backend.app.models.user import User
from backend.app.services import analysis_service as analysis_module
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.export_service import ExportService
from backend.app.services.sufficient_stats_analysis import (
    UnknownCorrectionMethodError,
)
from backend.tests.unit.services import (
    test_binomial_metric_result_characterisation as characterisation,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

_TREATMENT_A = characterisation._TREATMENT_A
_CONTROL = characterisation._CONTROL
_TREATMENT_B = characterisation._TREATMENT_B
_Fixture = characterisation._Fixture
_FakeSession = characterisation._FakeSession

_THREE = characterisation._FIXTURES["three_variants"]
_ALL_THREE = [_TREATMENT_A, _CONTROL, _TREATMENT_B]
#: Treatment A's purchase p-value in the three-variant fixture, and its
#: Benjamini-Hochberg adjustment over the two treatments.
_P_A = 0.035493461026190276
_P_A_BH = 0.07098692205238055

#: Two variants, treatment A's purchase p between 0.05 and 0.10: significant at
#: a stored 0.90, not at 0.95.
_BETWEEN = _Fixture(
    units={_TREATMENT_A: 1000, _CONTROL: 1000},
    converted={
        ("purchase", _TREATMENT_A): 126,
        ("purchase", _CONTROL): 100,
        ("signup", _TREATMENT_A): 400,
        ("signup", _CONTROL): 400,
    },
)

_ID = characterisation._experiment().id
RESULTS = f"/api/v1/results/{_ID}"
ALIAS = f"/api/v1/experiments/{_ID}/results"


def _experiment(variant_ids: List[str], method: Any, level: float):
    experiment = characterisation._experiment()
    experiment.variants = [v for v in experiment.variants if v.id in variant_ids]
    experiment.correction_method = method
    experiment.confidence_level = level
    return experiment


class _DictCache:
    """A cache that keeps what it is given, so a write can be counted.

    Locally Redis is unreachable (REDIS_PORT=1) and the route swallows every
    cache error, so a test against the real cache would pass without testing
    anything.
    """

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.writes: List[str] = []
        self.expires: List[int] = []

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str, expire: int = 0) -> None:
        self.writes.append(key)
        self.expires.append(expire)
        self.store[key] = value


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.cache = _DictCache()
        self.snapshots: List[Dict[str, Any]] = []
        self.experiment: Any = None
        self.client: TestClient | None = None

    def use(self, fixture: _Fixture, variant_ids: List[str], method, level):
        self.experiment = _experiment(variant_ids, method, level)

        def converting_units(_db, _experiment_id, variant_id, event):
            return fixture.converted[(event, str(variant_id))]

        self.monkeypatch.setattr(
            analysis_module, "count_converting_users", converting_units
        )
        self.monkeypatch.setattr(
            AnalysisService,
            "calculate_experiment_summary",
            lambda _self, _experiment: {
                "total_users": sum(fixture.units.values()),
                "total_events": 0,
                "total_conversions": 0,
                "duration_days": None,
            },
        )
        session = _FakeSession(self.experiment, fixture)

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        return self

    def get(self, path: str, **params) -> Dict[str, Any]:
        response = self.client.get(path, params=params)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    """The app as a superuser over a fake session, with a dict cache, a
    snapshot recorder and no SRM query."""
    h = _Harness(monkeypatch)
    user = MagicMock(spec=User)
    user.id = "12345678-1234-5678-1234-567812345678"
    user.is_active = True
    user.is_superuser = True
    user.role = "ADMIN"

    monkeypatch.setattr(results_endpoints, "_get_cache_service", lambda: h.cache)
    monkeypatch.setattr(analysis_module, "sample_ratio_check", lambda *_a: None)
    monkeypatch.setattr(
        results_endpoints,
        "_record_results_snapshots",
        lambda _db, response: h.snapshots.append(response.model_dump(mode="json")),
    )

    async def override_get_current_user():
        return user

    app.dependency_overrides[get_current_user] = override_get_current_user
    h.client = TestClient(app)
    try:
        yield h
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


def _purchase_a(body: Dict[str, Any]) -> Dict[str, Any]:
    (metric,) = [m for m in body["metrics"] if m["metric_name"] == "Purchased"]
    (variant,) = [v for v in metric["variants"] if v["variant_id"] == _TREATMENT_A]
    return variant


# ---------------------------------------------------------------------------
# The alias follows the stored settings, whatever they are
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [RESULTS, ALIAS], ids=["results", "alias"])
def test_both_routes_follow_a_stored_none_at_090(harness, path):
    """Not the migration's default: a route hard-coding Benjamini-Hochberg or
    0.95 fails here, as one hard-coding ``none`` fails the pins in
    ``test_results_correction_pins.py``."""
    harness.use(_THREE, _ALL_THREE, "none", 0.90)
    body = harness.get(path, use_cache="false")
    assert (body["correction_method"], body["confidence_level"]) == ("none", 0.9)
    variant = _purchase_a(body)
    assert variant["adjusted_p_value"] is None
    assert variant["is_significant"] is True


# ---------------------------------------------------------------------------
# PE condition 3: a request's own method, labelled as computed
# ---------------------------------------------------------------------------
def test_a_request_naming_none_on_a_stored_bh_experiment_is_labelled_none(harness):
    harness.use(_THREE, _ALL_THREE, "benjamini_hochberg", 0.95)
    body = harness.get(RESULTS, correction_method="none")
    assert body["correction_method"] == "none"
    assert body["confidence_level"] == 0.95
    variant = _purchase_a(body)
    assert variant["adjusted_p_value"] is None
    assert variant["p_value"] == pytest.approx(_P_A, rel=1e-9)
    assert variant["is_significant"] is True


def test_with_no_method_the_same_experiment_is_judged_by_its_stored_bh(harness):
    harness.use(_THREE, _ALL_THREE, "benjamini_hochberg", 0.95)
    body = harness.get(RESULTS)
    assert body["correction_method"] == "benjamini_hochberg"
    variant = _purchase_a(body)
    assert variant["adjusted_p_value"] == pytest.approx(_P_A_BH, rel=1e-9)
    assert variant["is_significant"] is False


# ---------------------------------------------------------------------------
# A7: the stored confidence level moves the verdict
# ---------------------------------------------------------------------------
def test_precondition_the_two_variant_p_is_between_005_and_010(harness):
    harness.use(_BETWEEN, [_TREATMENT_A, _CONTROL], "none", 0.95)
    p = _purchase_a(harness.get(RESULTS))["p_value"]
    assert 0.05 < p < 0.10, p


@pytest.mark.parametrize("level, significant", [(0.90, True), (0.95, False)])
def test_the_stored_confidence_level_moves_the_verdict(harness, level, significant):
    harness.use(_BETWEEN, [_TREATMENT_A, _CONTROL], "benjamini_hochberg", level)
    body = harness.get(RESULTS)
    assert body["confidence_level"] == level
    assert _purchase_a(body)["is_significant"] is significant


# ---------------------------------------------------------------------------
# A8: the cache key is built from the resolved settings
# ---------------------------------------------------------------------------
def test_a_setting_changed_in_draft_is_not_answered_from_the_old_cache_entry(
    harness,
):
    """A draft's results are cached for 24 hours, and a draft is exactly when
    the settings can change."""
    harness.use(_THREE, _ALL_THREE, "benjamini_hochberg", 0.95)

    first = harness.get(RESULTS)
    assert first["correction_method"] == "benjamini_hochberg"
    assert len(harness.cache.writes) == 1
    (key,) = harness.cache.writes
    assert key.endswith(":0.95:benjamini_hochberg:"), key

    # Served from the cache: nothing written again.
    assert harness.get(RESULTS)["correction_method"] == "benjamini_hochberg"
    assert len(harness.cache.writes) == 1

    # PUT /experiments/{id} on a draft changes the stored method.
    harness.experiment.correction_method = "bonferroni"
    second = harness.get(RESULTS)
    assert second["correction_method"] == "bonferroni"
    assert harness.cache.writes[1].endswith(":0.95:bonferroni:")
    assert len(harness.cache.writes) == 2


# ---------------------------------------------------------------------------
# Snapshots only under the stored settings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "params, recorded",
    [
        ({}, True),
        ({"correction_method": "benjamini_hochberg", "confidence_level": 0.95}, True),
        ({"correction_method": "none"}, False),
        ({"confidence_level": 0.99}, False),
    ],
    ids=["no-options", "the-stored-values-named", "other-method", "other-level"],
)
def test_a_snapshot_is_recorded_only_under_the_stored_settings(
    harness, params, recorded
):
    harness.use(_THREE, _ALL_THREE, "benjamini_hochberg", 0.95)
    harness.get(RESULTS, use_cache="false", **params)
    assert len(harness.snapshots) == (1 if recorded else 0)
    if recorded:
        assert harness.snapshots[0]["correction_method"] == "benjamini_hochberg"


# ---------------------------------------------------------------------------
# PE condition 4: an unknown method is a 500
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [RESULTS, ALIAS], ids=["results", "alias"])
def test_an_unknown_stored_method_is_a_500_not_a_404(harness, path):
    harness.use(_THREE, _ALL_THREE, "benjamini-hochberg", 0.95)
    response = harness.client.get(path, params={"use_cache": "false"})
    assert response.status_code == 500, response.text
    assert "Could not compute the experiment's results" in response.json()["detail"]


def test_an_unknown_method_raised_by_the_analysis_is_a_500(harness, monkeypatch):
    harness.use(_THREE, _ALL_THREE, "benjamini_hochberg", 0.95)

    def unknown(*_args, **_kwargs):
        raise UnknownCorrectionMethodError("holm")

    monkeypatch.setattr(AnalysisService, "get_experiment_results", unknown)
    response = harness.client.get(RESULTS, params={"use_cache": "false"})
    assert response.status_code == 500, response.text


def test_the_export_does_not_read_an_unknown_method_as_no_results(monkeypatch):
    """``_results_for`` turns a ``ValueError`` into blank columns; this is not
    one, so it reaches the export route's own failure handling."""
    experiment = _experiment(_ALL_THREE, "benjamini-hochberg", 0.95)

    def converting_units(_db, _experiment_id, variant_id, event):
        return _THREE.converted[(event, str(variant_id))]

    monkeypatch.setattr(analysis_module, "count_converting_users", converting_units)
    service = ExportService(_FakeSession(experiment, _THREE))
    with pytest.raises(UnknownCorrectionMethodError):
        service._variants_to_rows([experiment])


# ---------------------------------------------------------------------------
# How long an answer is kept: 300 s while running, 86,400 s otherwise,
# whatever CACHE_ENABLED says (the results cache does not read it)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cache_enabled", [False, True])
@pytest.mark.parametrize(
    "status, ttl",
    [("active", 300), ("running", 300), ("completed", 86400), ("draft", 86400)],
)
@pytest.mark.parametrize("path", [RESULTS, ALIAS], ids=["results", "alias"])
def test_results_are_kept_300_seconds_while_running_and_86400_otherwise(
    harness, monkeypatch, path, status, ttl, cache_enabled
):
    monkeypatch.setattr(
        "backend.app.core.config.settings.CACHE_ENABLED", cache_enabled, raising=False
    )
    harness.use(_THREE, _ALL_THREE, "none", 0.95)
    harness.experiment.status = status
    harness.get(path)
    assert harness.cache.expires == [ttl]
