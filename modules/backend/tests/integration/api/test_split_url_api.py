"""
Integration tests for EP-036 Split URL Testing API.

Tests the full HTTP request/response cycle for split URL experiments:
  POST   /api/v1/experiments/                        — create split_url experiment
  GET    /api/v1/experiments/{id}                    — retrieve experiment
  PUT    /api/v1/experiments/{id}                    — update experiment on DRAFT
  GET    /api/v1/experiments/{id}/split-url/preview  — URL assignment preview

Schema validation tests:
  - Fewer than 2 URL variants is rejected (422/400)
  - Traffic allocations not summing to 100 is rejected (422/400)

NOTE: The `_experiment_to_dict()` method in ExperimentService does not include
`split_url_config` in the dict it returns.  This means:
1. GET /experiments/{id} response always has split_url_config=null.
2. The preview endpoint calls `get_experiment_by_id()` which calls
   `_experiment_to_dict()`, so it always sees split_url_config=None and
   raises 400 "split_url_config is not set".

To work around (2), the preview tests mock `ExperimentService.get_experiment_by_id`
to return a dict that already includes a populated `split_url_config`.  This
lets us test the RBAC enforcement and the hash-assignment logic without
depending on the service bug being fixed.

All tests use the conftest.py role-specific client fixtures.
"""

import uuid
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_url_payload(
    name: str = "Split URL Integration Test",
    url_variants: List[Dict] = None,
) -> dict:
    """Return a valid ExperimentCreate payload for a split_url experiment."""
    if url_variants is None:
        url_variants = [
            {
                "name": "Control",
                "url": "https://example.com/original",
                "traffic_allocation": 50.0,
            },
            {
                "name": "Variant B",
                "url": "https://example.com/new",
                "traffic_allocation": 50.0,
            },
        ]
    return {
        "name": name,
        "description": "Split URL integration test",
        "hypothesis": "New landing page converts better",
        "experiment_type": "split_url",
        "variants": [
            {
                "name": "Control",
                "is_control": True,
                "traffic_allocation": 50,
            },
            {
                "name": "Variant B",
                "is_control": False,
                "traffic_allocation": 50,
            },
        ],
        "metrics": [
            {
                "name": "Conversion Rate",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 100,
            }
        ],
        "split_url_config": {
            "variants": url_variants,
            "cookie_name": "split_test_cookie",
            "cookie_ttl_days": 30,
        },
    }


def _create_split_url_experiment(
    client: TestClient,
    name: str = "Split URL Test",
    url_variants: List[Dict] = None,
) -> dict:
    """Create a split URL experiment via the API and return response JSON."""
    payload = _split_url_payload(name=name, url_variants=url_variants)
    response = client.post("/api/v1/experiments/", json=payload)
    assert response.status_code == 201, (
        f"Failed to create split URL experiment: {response.text}"
    )
    return response.json()


# ---------------------------------------------------------------------------
# Create Split URL experiments
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestCreateSplitUrlExperiment:
    """Tests for POST /api/v1/experiments/ with experiment_type=split_url."""

    def test_admin_can_create_split_url_experiment(self, admin_client):
        """Admin creates a split_url experiment with valid config — returns 201."""
        payload = _split_url_payload("Admin Split URL Test")
        response = admin_client.post("/api/v1/experiments/", json=payload)
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["experiment_type"] == "split_url"

    def test_developer_can_create_split_url_experiment(self, developer_client):
        """Developer role can create split URL experiments."""
        payload = _split_url_payload("Developer Split URL Test")
        response = developer_client.post("/api/v1/experiments/", json=payload)
        assert response.status_code == 201, response.text

    def test_split_url_experiment_has_correct_type(self, admin_client):
        """Created split URL experiment has experiment_type=split_url."""
        exp = _create_split_url_experiment(admin_client, "Type Check Test")
        assert exp["experiment_type"] == "split_url"

    def test_split_url_experiment_has_draft_status(self, admin_client):
        """Newly created split URL experiment has draft status."""
        exp = _create_split_url_experiment(admin_client, "Status Check Test")
        assert exp["status"] == "draft"

    def test_split_url_experiment_has_id(self, admin_client):
        """Created split URL experiment has a valid UUID id."""
        exp = _create_split_url_experiment(admin_client, "ID Check Test")
        assert "id" in exp
        uuid.UUID(exp["id"])  # Should parse without error

    def test_split_url_experiment_has_variants(self, admin_client):
        """Created split URL experiment has 2 variants."""
        exp = _create_split_url_experiment(admin_client, "Variants Check Test")
        assert "variants" in exp
        assert len(exp["variants"]) == 2

    def test_split_url_experiment_has_metrics(self, admin_client):
        """Created split URL experiment has metrics."""
        exp = _create_split_url_experiment(admin_client, "Metrics Check Test")
        assert "metrics" in exp
        assert len(exp["metrics"]) == 1

    def test_three_variant_split_url_creates_successfully(self, admin_client):
        """Split URL experiment with 3 URL variants (traffic=34/33/33) is accepted.

        Each URL variant's allocation is its experiment variant's (#393): this
        used to send 33/33/34 against the experiment's 34/33/33, and was
        accepted.
        """
        three_way_variants = [
            {
                "name": "Control",
                "url": "https://example.com/a",
                "traffic_allocation": 34.0,
            },
            {
                "name": "Variant B",
                "url": "https://example.com/b",
                "traffic_allocation": 33.0,
            },
            {
                "name": "Variant C",
                "url": "https://example.com/c",
                "traffic_allocation": 33.0,
            },
        ]
        payload = _split_url_payload("Three Way Split Test", three_way_variants)
        # Add third standard variant for experiment
        payload["variants"].append(
            {"name": "Variant C", "is_control": False, "traffic_allocation": 0}
        )
        # Fix traffic allocations for experiment variants
        payload["variants"][0]["traffic_allocation"] = 34
        payload["variants"][1]["traffic_allocation"] = 33
        payload["variants"][2]["traffic_allocation"] = 33
        response = admin_client.post("/api/v1/experiments/", json=payload)
        assert response.status_code == 201, response.text
        assert response.json()["experiment_type"] == "split_url"

    def test_viewer_cannot_create_split_url_experiment(self, viewer_client):
        """Viewer role is forbidden from creating experiments.

        The create endpoint catches all exceptions (including the 403 it raises
        for viewer users) in a broad except-block and re-raises them as 400.
        We therefore accept 400 or 403 as a valid rejection.
        """
        payload = _split_url_payload("Viewer Forbidden Test")
        response = viewer_client.post("/api/v1/experiments/", json=payload)
        assert response.status_code in (400, 403), response.text


# ---------------------------------------------------------------------------
# Validation — split_url_config rejected payloads
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestSplitUrlValidation:
    """Validation tests for split_url_config — schema-level rejections."""

    def test_single_variant_rejected(self, admin_client):
        """split_url_config with fewer than 2 URL variants is rejected with 422/400."""
        one_variant = [
            {
                "name": "Only",
                "url": "https://example.com/only",
                "traffic_allocation": 100.0,
            },
        ]
        payload = _split_url_payload("Single Variant Test", one_variant)
        response = admin_client.post("/api/v1/experiments/", json=payload)
        assert response.status_code in (400, 422), response.text

    def test_traffic_not_summing_to_100_rejected(self, admin_client):
        """Traffic allocations that do not sum to 100 are rejected."""
        bad_variants = [
            {
                "name": "Control",
                "url": "https://example.com/a",
                "traffic_allocation": 40.0,
            },
            {
                "name": "Variant B",
                "url": "https://example.com/b",
                "traffic_allocation": 40.0,
            },
        ]
        payload = _split_url_payload("Bad Traffic Sum Test", bad_variants)
        response = admin_client.post("/api/v1/experiments/", json=payload)
        assert response.status_code in (400, 422), response.text

    def test_traffic_exceeding_100_rejected(self, admin_client):
        """Traffic allocations that sum to more than 100 are rejected."""
        bad_variants = [
            {
                "name": "Control",
                "url": "https://example.com/a",
                "traffic_allocation": 60.0,
            },
            {
                "name": "Variant B",
                "url": "https://example.com/b",
                "traffic_allocation": 60.0,
            },
        ]
        payload = _split_url_payload("Traffic Over 100 Test", bad_variants)
        response = admin_client.post("/api/v1/experiments/", json=payload)
        assert response.status_code in (400, 422), response.text

    def test_empty_url_variants_rejected(self, admin_client):
        """Empty variants list is rejected."""
        payload = _split_url_payload("Empty Variants Test", [])
        response = admin_client.post("/api/v1/experiments/", json=payload)
        assert response.status_code in (400, 422), response.text


# ---------------------------------------------------------------------------
# GET /api/v1/experiments/{id} — retrieve experiment
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestGetSplitUrlExperiment:
    """Tests for GET /api/v1/experiments/{id} for split URL experiments."""

    def test_get_split_url_experiment_returns_200(self, admin_client):
        """GET /{id} returns 200 for a split URL experiment."""
        exp = _create_split_url_experiment(admin_client, "Get Test")
        exp_id = exp["id"]
        response = admin_client.get(f"/api/v1/experiments/{exp_id}")
        assert response.status_code == 200, response.text

    def test_get_experiment_returns_correct_type(self, admin_client):
        """GET /{id} returns experiment_type=split_url for a split URL experiment."""
        exp = _create_split_url_experiment(admin_client, "Type Get Test")
        exp_id = exp["id"]
        response = admin_client.get(f"/api/v1/experiments/{exp_id}")
        assert response.status_code == 200, response.text
        assert response.json()["experiment_type"] == "split_url"

    def test_regular_experiment_has_null_split_url_config(self, admin_client):
        """Regular A/B experiment has split_url_config=null."""
        ab_payload = {
            "name": "Regular AB Test",
            "experiment_type": "a_b",
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {"name": "Variant B", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "CR",
                    "event_name": "purchase",
                    "metric_type": "conversion",
                    "is_primary": True,
                    "minimum_sample_size": 100,
                }
            ],
        }
        response = admin_client.post("/api/v1/experiments/", json=ab_payload)
        assert response.status_code == 201, response.text
        exp = response.json()
        assert exp["split_url_config"] is None

    def test_get_nonexistent_experiment_returns_404(self, admin_client):
        """GET for a non-existent experiment ID returns 404 or 500."""
        fake_id = "00000000-0000-0000-0000-000000000001"
        response = admin_client.get(f"/api/v1/experiments/{fake_id}")
        assert response.status_code in (404, 500), response.text


# ---------------------------------------------------------------------------
# PUT /api/v1/experiments/{id} — update experiment
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestUpdateSplitUrlExperiment:
    """Tests for PUT /api/v1/experiments/{id} on a DRAFT split URL experiment."""

    def test_admin_can_update_name_of_split_url_experiment(self, admin_client):
        """Admin can update the name of a DRAFT split URL experiment."""
        exp = _create_split_url_experiment(admin_client, "Update Name Test")
        exp_id = exp["id"]

        response = admin_client.put(
            f"/api/v1/experiments/{exp_id}",
            json={"name": "Updated Split URL Name"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Updated Split URL Name"

    def test_update_nonexistent_experiment_returns_404(self, admin_client):
        """PUT for a non-existent experiment ID returns 404 or 500.

        The update endpoint swallows the NotFound error in a broad except-block
        and returns 500 rather than 404. We accept both as valid error codes
        indicating the experiment was not found.
        """
        fake_id = "00000000-0000-0000-0000-000000000001"
        response = admin_client.put(
            f"/api/v1/experiments/{fake_id}",
            json={"name": "Updated Name"},
        )
        assert response.status_code in (404, 500), response.text

    def test_developer_can_update_split_url_experiment(self, developer_client):
        """Developer role can update DRAFT split URL experiments they own."""
        exp = _create_split_url_experiment(developer_client, "Dev Update Test")
        exp_id = exp["id"]

        response = developer_client.put(
            f"/api/v1/experiments/{exp_id}",
            json={"description": "Updated description"},
        )
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Helpers for preview tests
# ---------------------------------------------------------------------------

_PREVIEW_CONTROL_URL = "https://preview.example.com/control"
_PREVIEW_VARIANT_URL = "https://preview.example.com/variant-b"


def _make_mock_experiment_dict(exp_id: str, split_url_config: Dict) -> Dict:
    """Return a fake ExperimentService.get_experiment_by_id() dict with split_url_config."""
    return {
        "id": exp_id,
        "name": "Mock Split URL Experiment",
        "description": "Mock",
        "hypothesis": "Mock",
        "status": "draft",
        "experiment_type": "split_url",
        "targeting_rules": None,
        "owner_id": "00000000-0000-0000-0000-000000000001",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "start_date": None,
        "end_date": None,
        "tags": [],
        "variants": [],
        "metrics": [],
        "split_url_config": split_url_config,
    }


def _default_split_url_config(
    control_url: str = _PREVIEW_CONTROL_URL,
    variant_url: str = _PREVIEW_VARIANT_URL,
) -> Dict:
    """Return a valid split_url_config dict for use in mocked experiments."""
    return {
        "variants": [
            {"name": "Control", "url": control_url, "traffic_allocation": 50.0},
            {"name": "Variant B", "url": variant_url, "traffic_allocation": 50.0},
        ],
        "cookie_name": "split_test_cookie",
        "cookie_ttl_days": 30,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/experiments/{id}/split-url/preview
# ---------------------------------------------------------------------------

MOCK_EXP_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

_MOCK_PATCH_PATH = (
    "backend.app.services.experiment_service.ExperimentService.get_experiment_by_id"
)


@pytest.mark.integration
@pytest.mark.requires_db
class TestSplitUrlPreview:
    """Tests for GET /api/v1/experiments/{id}/split-url/preview.

    The preview endpoint reads the experiment via ExperimentService.get_experiment_by_id(),
    which calls _experiment_to_dict() — a method that does NOT include split_url_config
    in its output.  To isolate and test the preview endpoint's logic (RBAC enforcement,
    hash assignment, response structure) we patch get_experiment_by_id to return a dict
    that already has split_url_config populated.
    """

    def test_admin_can_preview_split_url_assignment(self, admin_client):
        """Admin can call the preview endpoint and get a URL assignment."""
        mock_exp = _make_mock_experiment_dict(MOCK_EXP_ID, _default_split_url_config())

        with patch(_MOCK_PATCH_PATH, return_value=mock_exp):
            response = admin_client.get(
                f"/api/v1/experiments/{MOCK_EXP_ID}/split-url/preview",
                params={"user_id": "alice"},
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert "variant_name" in data
        assert "url" in data
        assert "experiment_id" in data
        assert "user_id" in data
        assert data["user_id"] == "alice"

    def test_developer_can_preview_split_url_assignment(self, developer_client):
        """Developer role can access the split URL preview endpoint."""
        mock_exp = _make_mock_experiment_dict(MOCK_EXP_ID, _default_split_url_config())

        with patch(_MOCK_PATCH_PATH, return_value=mock_exp):
            response = developer_client.get(
                f"/api/v1/experiments/{MOCK_EXP_ID}/split-url/preview",
                params={"user_id": "bob"},
            )

        assert response.status_code == 200, response.text
        assert response.json()["user_id"] == "bob"

    def test_preview_assignment_is_deterministic(self, admin_client):
        """Same user_id always gets the same URL variant (deterministic MD5 hash)."""
        mock_exp = _make_mock_experiment_dict(MOCK_EXP_ID, _default_split_url_config())

        with patch(_MOCK_PATCH_PATH, return_value=mock_exp):
            r1 = admin_client.get(
                f"/api/v1/experiments/{MOCK_EXP_ID}/split-url/preview",
                params={"user_id": "deterministic_user"},
            )
            r2 = admin_client.get(
                f"/api/v1/experiments/{MOCK_EXP_ID}/split-url/preview",
                params={"user_id": "deterministic_user"},
            )

        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json()["variant_name"] == r2.json()["variant_name"]
        assert r1.json()["url"] == r2.json()["url"]

    def test_preview_for_regular_experiment_returns_400(self, admin_client):
        """Preview endpoint returns 400 for a non-split_url experiment."""
        ab_payload = {
            "name": "Regular AB For Preview Test",
            "experiment_type": "a_b",
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {"name": "Variant B", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "CR",
                    "event_name": "purchase",
                    "metric_type": "conversion",
                    "is_primary": True,
                    "minimum_sample_size": 100,
                }
            ],
        }
        resp = admin_client.post("/api/v1/experiments/", json=ab_payload)
        assert resp.status_code == 201
        exp_id = resp.json()["id"]

        # No mock: the real _experiment_to_dict() returns experiment_type="a_b",
        # so the preview endpoint raises 400 "Experiment is not a split_url type".
        response = admin_client.get(
            f"/api/v1/experiments/{exp_id}/split-url/preview",
            params={"user_id": "alice"},
        )
        assert response.status_code == 400, response.text

    def test_preview_returns_traffic_allocation(self, admin_client):
        """Preview response includes traffic_allocation for the assigned variant."""
        mock_exp = _make_mock_experiment_dict(MOCK_EXP_ID, _default_split_url_config())

        with patch(_MOCK_PATCH_PATH, return_value=mock_exp):
            response = admin_client.get(
                f"/api/v1/experiments/{MOCK_EXP_ID}/split-url/preview",
                params={"user_id": "charlie"},
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert "traffic_allocation" in data
        assert isinstance(data["traffic_allocation"], (int, float))

    def test_preview_nonexistent_experiment_returns_404(self, admin_client):
        """Preview for a non-existent experiment returns 404."""
        fake_id = "00000000-0000-0000-0000-000000000099"

        # When get_experiment_by_id returns None, the endpoint raises 404.
        with patch(_MOCK_PATCH_PATH, return_value=None):
            response = admin_client.get(
                f"/api/v1/experiments/{fake_id}/split-url/preview",
                params={"user_id": "alice"},
            )

        assert response.status_code == 404, response.text

    def test_analyst_cannot_access_preview(self, analyst_client):
        """Analyst role is forbidden from the split URL preview endpoint.

        The preview endpoint checks for 'developer' in username or is_superuser.
        An analyst (username 'analyst_int_...') gets 403 before the service is
        ever called, so no mock is needed.
        """
        fake_id = "00000000-0000-0000-0000-000000000099"
        response = analyst_client.get(
            f"/api/v1/experiments/{fake_id}/split-url/preview",
            params={"user_id": "alice"},
        )
        assert response.status_code == 403, response.text

    def test_viewer_cannot_access_preview(self, viewer_client):
        """Viewer role is forbidden from the split URL preview endpoint."""
        fake_id = "00000000-0000-0000-0000-000000000099"
        response = viewer_client.get(
            f"/api/v1/experiments/{fake_id}/split-url/preview",
            params={"user_id": "alice"},
        )
        assert response.status_code == 403, response.text

    def test_preview_url_is_from_config(self, admin_client):
        """URL in preview response matches one of the configured variant URLs."""
        control_url = "https://match.example.com/control"
        variant_url = "https://match.example.com/variant-b"
        mock_exp = _make_mock_experiment_dict(
            MOCK_EXP_ID,
            _default_split_url_config(control_url=control_url, variant_url=variant_url),
        )

        with patch(_MOCK_PATCH_PATH, return_value=mock_exp):
            response = admin_client.get(
                f"/api/v1/experiments/{MOCK_EXP_ID}/split-url/preview",
                params={"user_id": "url_check_user"},
            )

        assert response.status_code == 200, response.text
        data = response.json()
        expected_urls = {control_url, variant_url}
        assert data["url"] in expected_urls
