"""
The analysis status table (``backend/app/core/analysis_status.py``) and the
responses that carry it -- #217 (CUPED) and #219 (interaction analysis).

* The table's statuses are pinned literally, so a flipped entry fails here.
* Every labelled response reads the table when it is built: patching an entry
  changes the response, so no response can hard-code its own label.
* A notice is present exactly when the status is ``beta``.
* The two routes whose numbers are labelled beta are marked
  ``x-stability: beta`` in the live OpenAPI document; ``/interactions/scan``
  and ``/results/{id}`` are not.  ``/interactions/{a}/{b}/novelty`` is gone.
* CUPED's ``none`` applies no adjustment (θ exactly 0.0), and the stable
  scan invents no sub-result.
"""

import uuid
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.core import analysis_status as table
from backend.app.core.analysis_status import (
    ANALYSIS_STATUS,
    AnalysisLabel,
    analysis_notice,
    analysis_status,
)
from backend.app.schemas.interaction import (
    InteractionAnalysisResponse,
    InteractionPairResponse,
)
from backend.app.schemas.variance_reduction import (
    CupedResultsResponse,
    VarianceReductionMethod,
)
from backend.app.services.cuped_service import CupedService
from backend.app.services.interaction_detection_service import (
    InteractionDetectionService,
)

pytestmark = pytest.mark.unit

#: What this release says about each analysis.  Changing a status is a
#: decision: change it here and in the table together.
EXPECTED_STATUS = {
    "cuped": "ga",
    "interactions": "beta",
    "sequential": "beta",
    "holdout": "beta",
    "warehouse_proportion": "ga",
    "warehouse_mean": "ga",
}

EXPECTED_ISSUE = {"interactions": 219, "sequential": 232, "holdout": 445}


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_the_table_holds_exactly_the_expected_statuses():
    assert {name: label.status for name, label in ANALYSIS_STATUS.items()} == (
        EXPECTED_STATUS
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_STATUS))
def test_a_notice_is_present_exactly_when_beta(name):
    label = ANALYSIS_STATUS[name]
    assert (label.status == "beta") == (label.notice is not None)
    if label.notice is not None:
        assert label.notice.startswith("Beta: ")
        issue = EXPECTED_ISSUE[name]
        assert label.notice.endswith(
            f"https://github.com/Controlvar/controlvar/issues/{issue}"
        )


def test_the_label_refuses_beta_without_a_notice():
    with pytest.raises(ValueError):
        AnalysisLabel("beta")
    with pytest.raises(ValueError):
        AnalysisLabel("beta", "")


def test_the_label_refuses_ga_with_a_notice():
    with pytest.raises(ValueError):
        AnalysisLabel("ga", "Beta: something")


def test_the_label_refuses_an_unknown_status():
    with pytest.raises(ValueError):
        AnalysisLabel("alpha", "Beta: something")  # type: ignore[arg-type]


def test_an_unknown_analysis_is_a_key_error():
    with pytest.raises(KeyError):
        analysis_status("no-such-analysis")


# ---------------------------------------------------------------------------
# Every labelled response reads the table when it is built
# ---------------------------------------------------------------------------


def _cuped() -> CupedResultsResponse:
    return CupedResultsResponse(
        experiment_id=str(uuid.uuid4()),
        method=VarianceReductionMethod.NONE,
        metrics=[],
        computed_at="2026-09-27T00:00:00+00:00",
    )


def _pair() -> InteractionPairResponse:
    return InteractionPairResponse(
        experiment_a_id="a",
        experiment_b_id="b",
        shared_users=0,
        share_of_a=0.0,
        share_of_b=0.0,
        overlap_coefficient=0.0,
        has_significant_overlap=False,
        min_users_per_cell=100,
        min_expected_per_cell=25,
    )


BUILDERS = {"cuped": _cuped, "interactions": _pair}


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_a_response_carries_the_tables_current_label(name):
    body = BUILDERS[name]().model_dump(mode="json")
    assert body["analysis_status"] == analysis_status(name) == EXPECTED_STATUS[name]
    assert body["analysis_notice"] == analysis_notice(name)


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_a_response_follows_a_changed_table_entry(name, monkeypatch):
    """Flip the entry to ga: the response must follow, with no notice."""
    monkeypatch.setitem(table.ANALYSIS_STATUS, name, AnalysisLabel("ga"))
    body = BUILDERS[name]().model_dump(mode="json")
    assert body["analysis_status"] == "ga"
    assert body["analysis_notice"] is None


def test_the_stable_scan_schema_is_not_labelled():
    """/interactions/scan is stable: its item schema gains no field."""
    assert "analysis_status" not in InteractionAnalysisResponse.model_fields
    assert "analysis_notice" not in InteractionAnalysisResponse.model_fields


# ---------------------------------------------------------------------------
# The routes: beta in the OpenAPI document, labelled in the answer
# ---------------------------------------------------------------------------

BETA_OPERATIONS = {
    ("get", "/api/v1/results/{experiment_id}/cuped"),
    ("get", "/api/v1/interactions/{exp_a_id}/{exp_b_id}"),
    ("get", "/api/v1/holdout/{holdout_id}/results"),
}

STILL_STABLE = {
    ("get", "/api/v1/interactions/scan"),
    ("get", "/api/v1/results/{experiment_id}"),
}


@pytest.fixture(scope="module")
def openapi():
    from backend.app.main import app

    return app.openapi()


@pytest.mark.parametrize("method,path", sorted(BETA_OPERATIONS))
def test_demoted_routes_are_beta_in_the_openapi_document(openapi, method, path):
    operation = openapi["paths"][path][method]
    assert operation.get("x-stability") == "beta"
    assert operation["summary"].startswith("Beta: ")


@pytest.mark.parametrize("method,path", sorted(STILL_STABLE))
def test_the_neighbouring_routes_stay_stable(openapi, method, path):
    assert "x-stability" not in openapi["paths"][path][method]


@pytest.fixture
def client():
    from backend.app.api.deps import get_current_active_user, get_db
    from backend.app.main import app

    user = MagicMock()
    user.is_active = True
    user.is_superuser = True
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _users(first, second):
    return patch.object(
        InteractionDetectionService,
        "_get_experiment_users",
        side_effect=[first, second],
    )


SHARED_40 = {f"shared-user-{i}" for i in range(40)}


def _pair_route():
    """The pair route with its experiments found and its analysis built now."""
    service = InteractionDetectionService
    return (
        patch.object(service, "load_experiment", return_value=MagicMock()),
        patch.object(
            service, "analyze_pair_interactions", side_effect=lambda *a: _pair()
        ),
    )


def test_pair_route_answers_with_the_tables_label(client, monkeypatch):
    a, b = uuid.uuid4(), uuid.uuid4()
    found, analysed = _pair_route()
    with found, analysed:
        body = client.get(f"/api/v1/interactions/{a}/{b}").json()
    assert body["analysis_status"] == "beta"
    assert body["analysis_notice"] == analysis_notice("interactions")

    monkeypatch.setitem(table.ANALYSIS_STATUS, "interactions", AnalysisLabel("ga"))
    found, analysed = _pair_route()
    with found, analysed:
        body = client.get(f"/api/v1/interactions/{a}/{b}").json()
    assert body["analysis_status"] == "ga"
    assert body["analysis_notice"] is None


def test_the_pair_notice_states_the_corrected_decision_in_percentage_points():
    notice = analysis_notice("interactions")
    assert "percentage points" in notice
    assert "corrected" in notice
    assert "unadjusted" not in notice
    assert "novelty" not in notice.lower()


def test_novelty_is_gone_from_the_table_and_the_routes(client, openapi):
    assert "novelty" not in ANALYSIS_STATUS
    assert not [path for path in openapi["paths"] if "novelty" in path]
    a, b = uuid.uuid4(), uuid.uuid4()
    response = client.get(f"/api/v1/interactions/{a}/{b}/novelty")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "overlap,risk",
    [(0.0, "low"), (0.3, "low"), (0.31, "medium"), (0.6, "medium"), (0.61, "high")],
)
def test_risk_comes_from_the_overlap_bands(overlap, risk):
    assert InteractionDetectionService.risk_from_overlap(overlap) == risk


def test_scan_items_carry_null_sub_results(client):
    """The stable scan keeps its shape; its items simply have no sub-results."""
    service = InteractionDetectionService
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    with (
        patch.object(service, "_get_active_experiment_ids", return_value=ids),
        _users(SHARED_40, SHARED_40),
    ):
        body = client.get("/api/v1/interactions/scan").json()
    [item] = body["analyses"]
    assert item["interaction_result"] is None
    assert item["novelty_result"] is None
    assert item["sutva_result"] is None
    assert body["high_risk_pairs"] == 1
    assert "analysis_status" not in item


# ---------------------------------------------------------------------------
# #217: ``none`` applies no adjustment
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_cuped_none_gives_theta_exactly_zero_and_the_raw_estimate():
    # The outcome correlates with the covariate, so an estimated θ is far from 0.
    rng = np.random.default_rng(217)
    cx = np.arange(200, dtype=float)
    tx = np.arange(200, dtype=float)
    cy = (cx < 20).astype(float)
    ty = (tx < 50).astype(float) + rng.normal(0.0, 0.01, 200)

    adjusted = CupedService.compute_cuped_effect(cy, cx, ty, tx)
    assert abs(adjusted.theta) > 1e-4  # the probe: θ is estimable here

    none = CupedService.compute_cuped_effect(cy, cx, ty, tx, adjust=False)
    assert none.theta == 0.0
    assert none.variance_reduction_pct == 0.0
    assert none.adjusted_control_mean == float(cy.mean())
    assert none.adjusted_treatment_mean == float(ty.mean())
    assert none.adjusted_effect == float(ty.mean()) - float(cy.mean())
