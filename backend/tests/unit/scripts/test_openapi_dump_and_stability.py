"""How the OpenAPI documents are produced, and what the API promises around them (#1142).

``make openapi`` writes three documents with ``backend.scripts.dump_openapi``:
the dashboard's paths-and-methods fixture and the two full snapshots.
``--profile full`` refuses to run without the modules. Deprecation is declared
in the documents only: no response carries a ``Deprecation`` or ``Sunset``
header. The results cache key names the statistics engine version, and a
finished experiment's answer is cached for 24 hours.
"""

from __future__ import annotations

import ast
import pathlib
import re
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.v1.endpoints import results as results_endpoint
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.main import app
from backend.app.models.user import UserRole
from backend.scripts import dump_openapi

REPO = pathlib.Path(__file__).resolve().parents[4]


def _openapi_recipe():
    lines = (REPO / "Makefile").read_text().splitlines()
    start = [line.startswith("openapi:") for line in lines].index(True)
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return recipe


def test_make_openapi_regenerates_all_three():
    commands = [line for line in _openapi_recipe() if "dump_openapi" in line]
    by_output = {line.split()[-1].rsplit("/", 1)[-1]: line for line in commands}
    assert set(by_output) == {
        "openapi.json",
        "openapi-v1.stable.json",
        "openapi-v1.full.json",
    }
    assert "--profile full" in by_output["openapi.json"]
    assert "--full" not in by_output["openapi.json"].replace("--profile full", "")
    assert by_output["openapi.json"].split()[-1].endswith("fixtures/openapi.json")
    assert "--profile core --full" in by_output["openapi-v1.stable.json"]
    assert "--profile full --full" in by_output["openapi-v1.full.json"]


def test_the_dashboard_fixture_is_paths_and_methods_only():
    document = dump_openapi.build_document(full=False)
    assert set(document) == {"openapi", "info", "paths"}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            assert method in dump_openapi.HTTP_METHODS, (path, method)
            assert set(operation) == {"operationId", "summary"}, (path, method)


def test_profile_full_fails_without_the_modules():
    with (
        patch("backend.app.modules_loader.load_modules", return_value=False),
        patch("backend.app.modules_loader.modules_failure", return_value=None),
    ):
        with pytest.raises(RuntimeError, match="--profile full"):
            dump_openapi.build_document(full=True, profile="full")


def test_no_response_carries_a_deprecation_or_sunset_header():
    named = []
    for tree in (REPO / "backend" / "app", REPO / "modules" / "backend" / "app"):
        if not tree.is_dir():
            continue
        for path in tree.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if re.fullmatch(r"(?i)deprecation|sunset", node.value.strip()):
                        named.append(f"{path}:{node.lineno}")
    assert named == []

    client = TestClient(app)
    for path in ("/health", "/api/v1/openapi.json", "/api/v1/experiments/"):
        headers = {h.lower() for h in client.get(path).headers}
        assert not headers & {"deprecation", "sunset"}, path


def test_the_results_cache_key_names_the_engine_and_a_finished_answer_lasts_a_day():
    experiment_id = uuid.uuid4()
    key = results_endpoint._results_cache_key(experiment_id, 0.95, "none", None)
    assert key.startswith(f"results:{experiment_id}:{ENGINE_VERSION}:")

    experiment = SimpleNamespace(
        id=experiment_id,
        correction_method="none",
        confidence_level=0.95,
        sequential_testing_enabled=False,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = experiment
    user = SimpleNamespace(
        id=uuid.uuid4(), role=UserRole.VIEWER, is_active=True, is_superuser=False
    )
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_db] = lambda: db
    cache = MagicMock()
    cache.get.return_value = None
    analysed = {
        "experiment_id": str(experiment_id),
        "experiment_name": "Checkout",
        "status": "completed",
        "start_date": None,
        "end_date": None,
        "confidence_level": 0.95,
        "correction_method": "none",
        "computed_at": "2026-03-01T12:00:00+00:00",
        "sample_size_adequate": False,
        "summary": {
            "total_users": 0,
            "total_events": 0,
            "total_conversions": 0,
            "duration_days": 0,
            "has_winner": False,
            "winning_variant_id": None,
            "recommendation": "CONTINUE_TESTING",
            "recommendation_reason": "No significant winner yet.",
        },
        "metrics": [],
    }
    try:
        with (
            patch.object(results_endpoint, "_get_cache_service", return_value=cache),
            patch.object(results_endpoint, "AnalysisService") as analysis,
            patch.object(results_endpoint, "_srm_block", return_value=None),
        ):
            analysis.return_value.get_experiment_results.return_value = analysed
            response = TestClient(app).get(f"/api/v1/results/{experiment_id}")
    finally:
        app.dependency_overrides.pop(deps.get_current_active_user, None)
        app.dependency_overrides.pop(deps.get_db, None)
    assert response.status_code == 200, response.text
    stored_key, _ = cache.set.call_args.args
    assert ENGINE_VERSION in stored_key
    assert cache.set.call_args.kwargs["expire"] == 24 * 60 * 60


def test_the_statistics_are_core_and_the_manifest_says_why():
    services = REPO / "backend" / "app" / "services"
    for name in (
        "bayesian_service.py",
        "cuped_service.py",
        "sequential_testing_service.py",
    ):
        assert (services / name).is_file(), name
    manifest = (REPO / "modules-manifest.txt").read_text()
    deliberately = manifest.split("DELIBERATELY NOT IN THIS MANIFEST", 1)[1]
    assert "BAYESIAN + CUPED → keep core" in deliberately
    listed = manifest.split("DELIBERATELY NOT IN THIS MANIFEST", 1)[0]
    for name in ("bayesian_service", "cuped_service", "sequential_testing_service"):
        assert name not in listed, name
