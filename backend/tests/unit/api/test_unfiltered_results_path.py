"""The unfiltered results path, as main has it (#1933).

#1933 adds a separate route for one saved segment's results. These pins,
written against main's code before any of #1933's code existed, hold the
path every existing answer takes:

* ``ExperimentResultsResponse`` and ``ExperimentSummary`` keep exactly their
  fields: a field added for a segment's sake would change every unfiltered
  body (``test_unfiltered_results_pinned.py`` pins the bytes);
* ``GET /results/{id}`` and its alias call ``AnalysisService.
  get_experiment_results`` once, with exactly the experiment id, the
  confidence level and the correction method, and record the analysis
  snapshots once;
* the results cache key is unchanged character for character. A field
  inserted in the middle of it passed every unit test before this one.

The real routes and the real analysis run over the characterisation's fake
session; only the database, the cache and the snapshot writer are replaced.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.app.api.deps import get_current_user, get_db
from backend.app.api.read_scope import get_current_reader
from backend.app.api.v1.endpoints import results as results_endpoints
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.main import app
from backend.app.models.user import User
from backend.app.schemas.results import ExperimentResultsResponse, ExperimentSummary
from backend.app.services import analysis_service as analysis_module
from backend.app.services.analysis_service import AnalysisService
from backend.tests.unit.services import (
    test_binomial_metric_result_characterisation as characterisation,
)

pytestmark = [pytest.mark.unit]

_THREE = characterisation._FIXTURES["three_variants"]
_ID = characterisation._experiment().id
RESULTS = f"/api/v1/results/{_ID}"
ALIAS = f"/api/v1/experiments/{_ID}/results"


# --- the two schemas keep their fields ----------------------------------------------


def test_the_results_response_keeps_exactly_its_fields():
    assert sorted(ExperimentResultsResponse.model_fields) == [
        "bayesian_results",
        "breakdown",
        "computed_at",
        "confidence_level",
        "correction_method",
        "end_date",
        "experiment_id",
        "experiment_name",
        "metrics",
        "sample_size_adequate",
        "sequential_testing",
        "srm",
        "start_date",
        "status",
        "summary",
    ]


def test_the_summary_keeps_exactly_its_fields():
    assert sorted(ExperimentSummary.model_fields) == [
        "duration_days",
        "has_winner",
        "recommendation",
        "recommendation_reason",
        "total_conversions",
        "total_events",
        "total_users",
        "winning_variant_id",
    ]


# --- the cache key ---------------------------------------------------------------------


def test_the_results_cache_key_is_unchanged_character_for_character():
    experiment_id = UUID("00000000-0000-4000-8000-000000000001")
    key = results_endpoints._results_cache_key(
        experiment_id, 0.95, "benjamini_hochberg", None
    )
    assert key == (
        f"results:00000000-0000-4000-8000-000000000001:{ENGINE_VERSION}:"
        "0.95:benjamini_hochberg:"
    )
    with_breakdown = results_endpoints._results_cache_key(
        experiment_id, 0.9, "none", "tier"
    )
    assert with_breakdown == (
        f"results:00000000-0000-4000-8000-000000000001:{ENGINE_VERSION}:0.9:none:tier"
    )


# --- the path the unfiltered request takes --------------------------------------------


class _DictCache:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.gets: List[str] = []
        self.sets: List[str] = []

    def get(self, key: str):
        self.gets.append(key)
        return self.store.get(key)

    def set(self, key: str, value: str, expire: int = 0) -> None:
        self.sets.append(key)
        self.store[key] = value


class _Path:
    """What one request did: the analysis calls, the snapshot writes and the
    cache calls."""

    def __init__(self) -> None:
        self.analysis_calls: List[Dict[str, Any]] = []
        self.snapshots = 0
        self.cache = _DictCache()
        self.client: Any = None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Path]:
    """The app as a superuser over a fake session, with a dict cache, a
    snapshot counter, no SRM query and a spy on the analysis."""
    seen = _Path()
    experiment = characterisation._experiment()
    experiment.correction_method = "benjamini_hochberg"
    experiment.confidence_level = 0.95
    fixture = _THREE

    def converting_units(_db, _experiment_id, variant_id, event):
        return fixture.converted[(event, str(variant_id))]

    monkeypatch.setattr(analysis_module, "count_converting_users", converting_units)
    monkeypatch.setattr(
        AnalysisService,
        "calculate_experiment_summary",
        lambda _self, _experiment: {
            "total_users": sum(fixture.units.values()),
            "total_events": 0,
            "total_conversions": 0,
            "duration_days": None,
        },
    )
    monkeypatch.setattr(analysis_module, "sample_ratio_check", lambda *_a: None)

    real = AnalysisService.get_experiment_results

    def spy(self, *args, **kwargs):
        seen.analysis_calls.append({"args": args, "kwargs": kwargs})
        return real(self, *args, **kwargs)

    monkeypatch.setattr(AnalysisService, "get_experiment_results", spy)

    def record(_db, _response):
        seen.snapshots += 1

    monkeypatch.setattr(results_endpoints, "_record_results_snapshots", record)
    monkeypatch.setattr(results_endpoints, "_get_cache_service", lambda: seen.cache)

    session = characterisation._FakeSession(experiment, fixture)

    def override_get_db():
        yield session

    user = MagicMock(spec=User)
    user.id = "12345678-1234-5678-1234-567812345678"
    user.is_active = True
    user.is_superuser = True
    user.role = "ADMIN"

    async def override_get_current_user():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_current_reader] = override_get_current_user
    seen.client = TestClient(app)
    try:
        yield seen
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_current_reader, None)


@pytest.mark.parametrize("route", [RESULTS, ALIAS], ids=["results", "alias"])
def test_the_unfiltered_request_takes_the_old_path(harness, route):
    path = harness
    response = path.client.get(route)
    assert response.status_code == 200, response.text
    assert path.analysis_calls == [
        {
            "args": (UUID(_ID),),
            "kwargs": {
                "confidence_level": 0.95,
                "correction_method": "benjamini_hochberg",
            },
        }
    ]
    assert path.snapshots == 1
    assert len(path.cache.gets) == 1
    assert len(path.cache.sets) == 1
