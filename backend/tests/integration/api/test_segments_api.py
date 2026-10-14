"""
Integration tests for the Audience Segments REST API (EP-011).

Tests the full HTTP request/response cycle for segment CRUD operations,
authorization checks, membership evaluation, bulk evaluation, and
audience preview.

All test data is created via the API (POST /api/v1/segments/) rather than
using direct DB factory fixtures, to avoid session commit isolation issues.

Endpoint coverage:
  GET    /api/v1/segments/                list_segments
  POST   /api/v1/segments/               create_segment  [DEVELOPER+]
  POST   /api/v1/segments/bulk-evaluate  bulk_evaluate   (before /{id})
  GET    /api/v1/segments/{id}           get_segment
  PUT    /api/v1/segments/{id}           update_segment  [DEVELOPER+]
  DELETE /api/v1/segments/{id}           archive_segment [DEVELOPER+]
  POST   /api/v1/segments/{id}/evaluate  evaluate membership
  GET    /api/v1/segments/{id}/experiments get linked experiments
  POST   /api/v1/segments/{id}/preview   preview audience size
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.tests.integration.conftest import make_client_for_user
from backend.tests.integration.helpers import list_all_segment_ids

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rules(*conditions, logical_operator: str = "AND") -> dict:
    """Segment rules in the targeting rule format (the dashboard shape)."""
    return {
        "logical_operator": logical_operator,
        "groups": [{"logical_operator": "AND", "conditions": list(conditions)}],
    }


def _eq(attribute: str, value) -> dict:
    return {"attribute": attribute, "operator": "equals", "value": value}


def _regex(value: str) -> dict:
    return {"attribute": "email", "operator": "regex", "value": value}


US_RULES = _rules(_eq("country", "US"))


def _valid_segment_payload(name: str = None) -> dict:
    """Return a minimal valid SegmentCreate payload."""
    return {
        "name": name or f"Segment {uuid.uuid4().hex[:8]}",
        "description": "Integration test segment",
        "rules": US_RULES,
    }


def _create_segment(client: TestClient, name: str = None) -> dict:
    """Create a segment via the API and return the response JSON."""
    response = client.post("/api/v1/segments/", json=_valid_segment_payload(name))
    assert response.status_code in (200, 201), f"Create failed: {response.text}"
    return response.json()


# ---------------------------------------------------------------------------
# Create segment
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestCreateSegment:
    """POST /api/v1/segments/"""

    def test_admin_can_create_segment(self, admin_client):
        """Admin user creates a segment — returns 201 with segment data."""
        payload = _valid_segment_payload("Admin Create Segment")
        response = admin_client.post("/api/v1/segments/", json=payload)

        assert response.status_code == 201, response.text
        data = response.json()
        assert data["name"] == "Admin Create Segment"
        assert data["status"] == "active"
        assert "id" in data
        assert "rules" in data

    def test_developer_can_create_segment(self, developer_client):
        """Developer user creates a segment — returns 201."""
        payload = _valid_segment_payload("Developer Create Segment")
        response = developer_client.post("/api/v1/segments/", json=payload)

        assert response.status_code == 201, response.text
        data = response.json()
        assert data["name"] == "Developer Create Segment"

    def test_analyst_cannot_create_segment(self, analyst_client):
        """Analyst role is blocked from creating segments — returns 403."""
        payload = _valid_segment_payload("Analyst Attempt Segment")
        response = analyst_client.post("/api/v1/segments/", json=payload)

        assert response.status_code == 403, response.text

    def test_viewer_cannot_create_segment(self, viewer_client):
        """Viewer role is blocked from creating segments — returns 403."""
        payload = _valid_segment_payload("Viewer Attempt Segment")
        response = viewer_client.post("/api/v1/segments/", json=payload)
        assert response.status_code == 403, response.text

    def test_name_too_short_returns_422(self, admin_client):
        """Name shorter than 2 chars fails validation — returns 422."""
        payload = _valid_segment_payload()
        payload["name"] = "A"  # min_length=2
        response = admin_client.post("/api/v1/segments/", json=payload)

        assert response.status_code == 422, response.text

    def test_missing_rules_returns_422(self, admin_client):
        """Missing required 'rules' field — returns 422."""
        payload = {"name": "No Rules Segment", "description": "No rules"}
        response = admin_client.post("/api/v1/segments/", json=payload)

        assert response.status_code == 422, response.text

    def test_created_segment_default_status_is_active(self, admin_client):
        """Newly created segment always starts with 'active' status."""
        payload = _valid_segment_payload("Status Check Segment")
        response = admin_client.post("/api/v1/segments/", json=payload)

        assert response.status_code == 201, response.text
        assert response.json()["status"] == "active"

    def test_persisted_and_retrievable(self, admin_client):
        """Created segment can be retrieved via GET — verifying persistence."""
        payload = _valid_segment_payload("DB Persist Segment")
        create_response = admin_client.post("/api/v1/segments/", json=payload)

        assert create_response.status_code == 201, create_response.text
        segment_id = create_response.json()["id"]

        get_response = admin_client.get(f"/api/v1/segments/{segment_id}")
        assert get_response.status_code == 200, get_response.text
        assert get_response.json()["name"] == "DB Persist Segment"

    def test_created_segment_contains_rules(self, admin_client):
        """Created segment response contains the provided rules."""
        rules = _rules(_eq("plan", "premium"))
        payload = {"name": "Premium Segment", "rules": rules}
        response = admin_client.post("/api/v1/segments/", json=payload)

        assert response.status_code == 201, response.text
        data = response.json()
        assert "rules" in data
        assert data["rules"] is not None


# ---------------------------------------------------------------------------
# List segments
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestListSegments:
    """GET /api/v1/segments/"""

    def test_list_segments_returns_200(self, admin_client):
        """List endpoint returns 200 for admin user."""
        response = admin_client.get("/api/v1/segments/")
        assert response.status_code == 200, response.text

    def test_list_segments_returns_list(self, admin_client):
        """List response is a JSON array."""
        response = admin_client.get("/api/v1/segments/")
        assert response.status_code == 200, response.text
        data = response.json()
        assert isinstance(data, list)

    def test_list_includes_created_segment(self, admin_client):
        """A segment created via API appears in the list."""
        seg = _create_segment(admin_client, "Visible Segment")
        seg_id = seg["id"]

        assert seg_id in list_all_segment_ids(admin_client)

    def test_filter_by_status_active(self, admin_client):
        """Filtering by status=active returns only active segments."""
        _create_segment(admin_client, "Active Filter Segment")

        response = admin_client.get("/api/v1/segments/?status=active")
        assert response.status_code == 200, response.text
        data = response.json()
        for item in data:
            assert item["status"] == "active"

    def test_pagination_limit(self, admin_client):
        """limit parameter restricts the number of results returned."""
        # Create at least 2 segments to test pagination
        _create_segment(admin_client, "Paginate Seg A")
        _create_segment(admin_client, "Paginate Seg B")
        _create_segment(admin_client, "Paginate Seg C")

        response = admin_client.get("/api/v1/segments/?limit=1")
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data) <= 1

    def test_pagination_offset(self, admin_client):
        """offset parameter skips results."""
        response_all = admin_client.get("/api/v1/segments/?limit=100&offset=0")
        response_offset = admin_client.get("/api/v1/segments/?limit=100&offset=1000")

        assert response_all.status_code == 200
        assert response_offset.status_code == 200
        # With a large offset, result should be empty or smaller
        all_count = len(response_all.json())
        offset_count = len(response_offset.json())
        assert offset_count <= all_count

    def test_archived_segment_not_in_active_list(self, admin_client):
        """Archived segment does not appear when filtering by status=active."""
        seg = _create_segment(admin_client, "Soon Archived Segment")
        seg_id = seg["id"]

        # Archive the segment
        delete_response = admin_client.delete(f"/api/v1/segments/{seg_id}")
        assert delete_response.status_code == 204, delete_response.text

        # Check it's not in the active list, on any page
        assert seg_id not in list_all_segment_ids(admin_client, status="active")

    def test_analyst_can_list_segments(self, analyst_client):
        """Analyst has LIST permission on segments (EXPERIMENT resource)."""
        response = analyst_client.get("/api/v1/segments/")
        assert response.status_code == 200, response.text

    def test_viewer_can_list_segments(self, viewer_client):
        """Viewer has LIST permission on segments (EXPERIMENT resource)."""
        response = viewer_client.get("/api/v1/segments/")
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Get single segment
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestGetSegment:
    """GET /api/v1/segments/{id}"""

    def test_get_existing_segment(self, admin_client):
        """GET returns full segment details for an existing segment."""
        seg = _create_segment(admin_client, "Get By ID Segment")
        seg_id = seg["id"]

        response = admin_client.get(f"/api/v1/segments/{seg_id}")
        assert response.status_code == 200, response.text

        data = response.json()
        assert data["id"] == seg_id
        assert data["name"] == "Get By ID Segment"

    def test_get_segment_contains_expected_fields(self, admin_client):
        """GET response includes all required fields."""
        seg = _create_segment(admin_client, "Fields Check Segment")

        response = admin_client.get(f"/api/v1/segments/{seg['id']}")
        assert response.status_code == 200, response.text

        data = response.json()
        assert "id" in data
        assert "name" in data
        assert "status" in data
        assert "rules" in data
        assert "created_at" in data
        assert "updated_at" in data

    def test_nonexistent_segment_returns_404(self, admin_client):
        """GET with a UUID that doesn't match any segment returns 404."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = admin_client.get(f"/api/v1/segments/{fake_id}")
        assert response.status_code == 404, response.text

    def test_analyst_can_get_segment(self, analyst_client):
        """Analyst has READ permission — GET returns non-403 for any segment ID.

        The endpoint should not reject analyst users with 403. We verify by
        calling with a non-existent ID — the expected response is 404 (not found),
        not 403 (forbidden), which proves READ permission is granted.
        """
        fake_id = "00000000-0000-0000-0000-cccccccccccc"
        response = analyst_client.get(f"/api/v1/segments/{fake_id}")
        # Analyst has READ permission — should get 404 (not found), NOT 403
        assert response.status_code != 403, (
            f"Analyst should have READ permission, got 403: {response.text}"
        )
        assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Update segment
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestUpdateSegment:
    """PUT /api/v1/segments/{id}"""

    def test_admin_can_update_name(self, admin_client):
        """Admin can update the name of a segment."""
        seg = _create_segment(admin_client, "Original Name Segment")

        payload = {"name": "Updated Segment Name"}
        response = admin_client.put(f"/api/v1/segments/{seg['id']}", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Updated Segment Name"

    def test_admin_can_update_description(self, admin_client):
        """Admin can update the description of a segment."""
        seg = _create_segment(admin_client, "Desc Update Segment")

        payload = {"description": "New integration test description"}
        response = admin_client.put(f"/api/v1/segments/{seg['id']}", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["description"] == "New integration test description"

    def test_developer_can_update_segment(
        self, admin_client, db_session, developer_user
    ):
        """Developer can update a segment — returns 200."""
        seg = _create_segment(admin_client, "Dev Update Segment")

        # Switch to the developer only after the admin's setup: the auth
        # override is app-global, so the last client made is the one in effect.
        developer_client = make_client_for_user(db_session, developer_user)
        payload = {"name": "Dev Updated Segment"}
        response = developer_client.put(f"/api/v1/segments/{seg['id']}", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Dev Updated Segment"

    def test_analyst_cannot_update_segment(self, analyst_client):
        """Analyst lacks UPDATE permission — returns 403 regardless of segment existence."""
        fake_id = "00000000-0000-0000-0000-dddddddddddd"
        payload = {"name": "Analyst Should Not Update"}
        response = analyst_client.put(f"/api/v1/segments/{fake_id}", json=payload)
        assert response.status_code == 403, response.text

    def test_viewer_cannot_update_segment(self, viewer_client):
        """Viewer lacks UPDATE permission — returns 403."""
        fake_id = "00000000-0000-0000-0000-aaaaaaaaaaaa"
        payload = {"name": "Viewer Should Not Update"}
        response = viewer_client.put(f"/api/v1/segments/{fake_id}", json=payload)
        assert response.status_code == 403, response.text

    def test_update_rules(self, admin_client):
        """Admin can update the targeting rules of a segment."""
        seg = _create_segment(admin_client, "Rules Update Segment")
        new_rules = _rules(
            _eq("country", "CA"), _eq("country", "UK"), logical_operator="OR"
        )
        payload = {"rules": new_rules}
        response = admin_client.put(f"/api/v1/segments/{seg['id']}", json=payload)
        assert response.status_code == 200, response.text

    def test_update_persisted_in_db(self, admin_client):
        """Updated segment name is reflected in subsequent GET after PUT."""
        seg = _create_segment(admin_client, "DB Update Check Segment")

        put_response = admin_client.put(
            f"/api/v1/segments/{seg['id']}", json={"name": "Persisted Update"}
        )
        assert put_response.status_code == 200, put_response.text

        get_response = admin_client.get(f"/api/v1/segments/{seg['id']}")
        assert get_response.status_code == 200, get_response.text
        assert get_response.json()["name"] == "Persisted Update"

    def test_update_nonexistent_segment_returns_404(self, admin_client):
        """PUT on a non-existent UUID returns 404."""
        fake_id = "00000000-0000-0000-0000-000000000001"
        response = admin_client.put(
            f"/api/v1/segments/{fake_id}", json={"name": "Ghost"}
        )
        assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Archive segment (soft delete)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestArchiveSegment:
    """DELETE /api/v1/segments/{id}  (soft delete — sets status to ARCHIVED)"""

    def test_admin_can_archive_segment(self, admin_client):
        """Admin can archive a segment — returns 204."""
        seg = _create_segment(admin_client, "Archive Me Segment")

        response = admin_client.delete(f"/api/v1/segments/{seg['id']}")
        assert response.status_code == 204, response.text

    def test_archived_segment_status_becomes_archived(self, admin_client):
        """After archiving, segment status changes to 'archived' (soft delete)."""
        seg = _create_segment(admin_client, "Status After Archive")

        admin_client.delete(f"/api/v1/segments/{seg['id']}")

        # Retrieve and verify status
        get_response = admin_client.get(f"/api/v1/segments/{seg['id']}")
        # Segment should still exist (soft delete)
        assert get_response.status_code == 200, get_response.text
        assert get_response.json()["status"] == "archived"

    def test_developer_can_archive_segment(
        self, admin_client, db_session, developer_user
    ):
        """Developer can archive a segment."""
        seg = _create_segment(admin_client, "Dev Archive Segment")

        # Switch to the developer only after the admin's setup (see above).
        developer_client = make_client_for_user(db_session, developer_user)
        response = developer_client.delete(f"/api/v1/segments/{seg['id']}")
        assert response.status_code == 204, response.text

    def test_analyst_cannot_archive_segment(self, analyst_client):
        """Analyst lacks DELETE permission — returns 403 regardless of segment existence."""
        fake_id = "00000000-0000-0000-0000-eeeeeeeeeeee"
        response = analyst_client.delete(f"/api/v1/segments/{fake_id}")
        assert response.status_code == 403, response.text

    def test_viewer_cannot_archive_segment(self, viewer_client):
        """Viewer lacks DELETE permission — returns 403."""
        fake_id = "00000000-0000-0000-0000-bbbbbbbbbbbb"
        response = viewer_client.delete(f"/api/v1/segments/{fake_id}")
        assert response.status_code == 403, response.text

    def test_archive_nonexistent_segment_returns_404(self, admin_client):
        """DELETE on a non-existent UUID returns 404."""
        fake_id = "00000000-0000-0000-0000-000000000002"
        response = admin_client.delete(f"/api/v1/segments/{fake_id}")
        assert response.status_code == 404, response.text

    def test_archived_segment_not_in_default_active_list(self, admin_client):
        """Archived segment does not appear in the active-filtered list."""
        seg = _create_segment(admin_client, "Not In Active List")

        admin_client.delete(f"/api/v1/segments/{seg['id']}")

        active_ids = list_all_segment_ids(admin_client, status="active")
        assert seg["id"] not in active_ids


# ---------------------------------------------------------------------------
# Evaluate membership
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestEvaluateMembership:
    """POST /api/v1/segments/{id}/evaluate"""

    def test_evaluate_user_matching_rule_is_member_true(self, admin_client):
        """User context that satisfies the segment rules returns is_member=True."""
        # Create a segment for US users
        payload = {
            "name": "US Segment",
            "rules": US_RULES,
        }
        create_resp = admin_client.post("/api/v1/segments/", json=payload)
        assert create_resp.status_code == 201, create_resp.text
        seg_id = create_resp.json()["id"]

        # Evaluate a US user
        eval_payload = {"user_context": {"country": "US", "user_id": "user-123"}}
        response = admin_client.post(
            f"/api/v1/segments/{seg_id}/evaluate", json=eval_payload
        )
        assert response.status_code == 200, response.text

        data = response.json()
        assert data["is_member"] is True
        assert data["segment_id"] == seg_id

    def test_evaluate_user_not_matching_is_member_false(self, admin_client):
        """User context that does not satisfy the rules returns is_member=False."""
        payload = {
            "name": "US Only Segment",
            "rules": US_RULES,
        }
        create_resp = admin_client.post("/api/v1/segments/", json=payload)
        assert create_resp.status_code == 201, create_resp.text
        seg_id = create_resp.json()["id"]

        # Evaluate a non-US user
        eval_payload = {"user_context": {"country": "CA", "user_id": "user-456"}}
        response = admin_client.post(
            f"/api/v1/segments/{seg_id}/evaluate", json=eval_payload
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_member"] is False

    def test_evaluate_response_contains_required_fields(self, admin_client):
        """Evaluate response includes segment_id, segment_name, is_member, matched_rules."""
        seg = _create_segment(admin_client, "Eval Fields Segment")

        eval_payload = {"user_context": {"country": "US"}}
        response = admin_client.post(
            f"/api/v1/segments/{seg['id']}/evaluate", json=eval_payload
        )
        assert response.status_code == 200, response.text

        data = response.json()
        assert "segment_id" in data
        assert "segment_name" in data
        assert "is_member" in data
        assert "matched_rules" in data

    def test_evaluate_nonexistent_segment_returns_404(self, admin_client):
        """Evaluating a non-existent segment returns 404."""
        fake_id = "00000000-0000-0000-0000-000000000003"
        eval_payload = {"user_context": {"country": "US"}}
        response = admin_client.post(
            f"/api/v1/segments/{fake_id}/evaluate", json=eval_payload
        )
        assert response.status_code == 404, response.text

    def test_evaluate_with_empty_context_returns_200(self, admin_client):
        """Evaluating with empty user_context does not raise an error — returns 200."""
        seg = _create_segment(admin_client, "Empty Context Segment")

        eval_payload = {"user_context": {}}
        response = admin_client.post(
            f"/api/v1/segments/{seg['id']}/evaluate", json=eval_payload
        )
        assert response.status_code == 200, response.text
        # With empty context, conditions should not match — is_member=False
        assert response.json()["is_member"] is False

    def test_evaluate_missing_user_context_returns_422(self, admin_client):
        """Missing user_context field in request body returns 422."""
        seg = _create_segment(admin_client, "Missing Context Segment")

        response = admin_client.post(f"/api/v1/segments/{seg['id']}/evaluate", json={})
        assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Bulk evaluate
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestBulkEvaluate:
    """POST /api/v1/segments/bulk-evaluate"""

    def test_bulk_evaluate_multiple_segments(self, admin_client):
        """Bulk evaluate user across multiple segments — returns membership dict."""
        seg1 = _create_segment(admin_client, "Bulk Seg US")
        seg2 = _create_segment(admin_client, "Bulk Seg Premium")

        payload = {
            "user_context": {"country": "US", "plan": "premium"},
            "segment_ids": [seg1["id"], seg2["id"]],
        }
        response = admin_client.post("/api/v1/segments/bulk-evaluate", json=payload)
        assert response.status_code == 200, response.text

        data = response.json()
        assert "memberships" in data
        assert seg1["id"] in data["memberships"]
        assert seg2["id"] in data["memberships"]

    def test_bulk_evaluate_returns_evaluation_time_ms(self, admin_client):
        """Bulk evaluate response includes evaluation_time_ms field."""
        seg = _create_segment(admin_client, "Timing Segment")

        payload = {
            "user_context": {"country": "US"},
            "segment_ids": [seg["id"]],
        }
        response = admin_client.post("/api/v1/segments/bulk-evaluate", json=payload)
        assert response.status_code == 200, response.text

        data = response.json()
        assert "evaluation_time_ms" in data
        assert isinstance(data["evaluation_time_ms"], (int, float))
        assert data["evaluation_time_ms"] >= 0

    def test_bulk_evaluate_returns_user_context(self, admin_client):
        """Bulk evaluate response echoes back the user_context."""
        seg = _create_segment(admin_client, "Context Echo Segment")

        user_ctx = {"country": "US", "user_id": "abc123"}
        payload = {
            "user_context": user_ctx,
            "segment_ids": [seg["id"]],
        }
        response = admin_client.post("/api/v1/segments/bulk-evaluate", json=payload)
        assert response.status_code == 200, response.text

        data = response.json()
        assert "user_context" in data
        assert data["user_context"]["country"] == "US"

    def test_bulk_evaluate_nonexistent_segment_treated_as_non_member(
        self, admin_client
    ):
        """Non-existent segment IDs in bulk evaluate are treated as non-member (False)."""
        fake_id = "00000000-0000-0000-0000-ffffffffffff"
        payload = {
            "user_context": {"country": "US"},
            "segment_ids": [fake_id],
        }
        response = admin_client.post("/api/v1/segments/bulk-evaluate", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["memberships"][fake_id] is False

    def test_empty_segment_ids_list_returns_422(self, admin_client):
        """Empty segment_ids list fails validation — returns 422."""
        payload = {
            "user_context": {"country": "US"},
            "segment_ids": [],
        }
        response = admin_client.post("/api/v1/segments/bulk-evaluate", json=payload)
        assert response.status_code == 422, response.text

    def test_bulk_evaluate_missing_user_context_returns_422(self, admin_client):
        """Missing user_context field returns 422."""
        payload = {"segment_ids": ["some-id"]}
        response = admin_client.post("/api/v1/segments/bulk-evaluate", json=payload)
        assert response.status_code == 422, response.text

    def test_analyst_can_bulk_evaluate(self, analyst_client):
        """Analyst has READ permission — can use bulk evaluate.

        We use a non-existent segment ID; the service treats unknown IDs as
        non-member (False) but the response should still be 200 (not 403).
        """
        fake_seg_id = "00000000-0000-0000-0000-111111111111"
        payload = {
            "user_context": {"country": "US"},
            "segment_ids": [fake_seg_id],
        }
        response = analyst_client.post("/api/v1/segments/bulk-evaluate", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["memberships"][fake_seg_id] is False


# ---------------------------------------------------------------------------
# Get linked experiments
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestGetSegmentExperiments:
    """GET /api/v1/segments/{id}/experiments"""

    def test_get_experiments_returns_200_and_structure(self, admin_client):
        """Get linked experiments returns 200 with correct response structure."""
        seg = _create_segment(admin_client, "Linked Exp Segment")

        response = admin_client.get(f"/api/v1/segments/{seg['id']}/experiments")
        assert response.status_code == 200, response.text

        data = response.json()
        assert "segment_id" in data
        assert "experiments" in data
        assert "feature_flags" in data
        assert data["segment_id"] == seg["id"]
        assert isinstance(data["experiments"], list)
        assert isinstance(data["feature_flags"], list)

    def test_get_experiments_for_nonexistent_segment_returns_404(self, admin_client):
        """GET /segments/{id}/experiments for non-existent segment returns 404."""
        fake_id = "00000000-0000-0000-0000-000000000004"
        response = admin_client.get(f"/api/v1/segments/{fake_id}/experiments")
        assert response.status_code == 404, response.text

    def test_new_segment_has_no_linked_experiments(self, admin_client):
        """Newly created segment has empty experiments and feature_flags lists."""
        seg = _create_segment(admin_client, "No Linked Exp Segment")

        response = admin_client.get(f"/api/v1/segments/{seg['id']}/experiments")
        assert response.status_code == 200, response.text

        data = response.json()
        assert data["experiments"] == []
        assert data["feature_flags"] == []


# ---------------------------------------------------------------------------
# Preview audience size
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestPreviewAudience:
    """POST /api/v1/segments/{id}/preview"""

    def test_preview_returns_200_with_estimate_structure(self, admin_client):
        """Preview endpoint returns 200 with estimated_percentage, sample_size, matched."""
        seg = _create_segment(admin_client, "Preview Segment")

        payload = {
            "name": seg["name"],
            "rules": US_RULES,
        }
        response = admin_client.post(
            f"/api/v1/segments/{seg['id']}/preview", json=payload
        )
        assert response.status_code == 200, response.text

        data = response.json()
        assert "estimated_percentage" in data
        assert "sample_size" in data
        assert "matched" in data
        assert isinstance(data["estimated_percentage"], (int, float))
        assert isinstance(data["sample_size"], int)
        assert isinstance(data["matched"], int)

    def test_preview_for_nonexistent_segment_returns_404(self, admin_client):
        """Preview endpoint returns 404 when segment does not exist."""
        fake_id = "00000000-0000-0000-0000-000000000005"
        payload = {
            "name": "Any Name",
            "rules": US_RULES,
        }
        response = admin_client.post(
            f"/api/v1/segments/{fake_id}/preview", json=payload
        )
        assert response.status_code == 404, response.text

    def test_preview_sample_size_query_param(self, admin_client):
        """sample_size query parameter is accepted and affects response."""
        seg = _create_segment(admin_client, "Sample Size Segment")

        payload = {
            "name": seg["name"],
            "rules": US_RULES,
        }
        response = admin_client.post(
            f"/api/v1/segments/{seg['id']}/preview?sample_size=100",
            json=payload,
        )
        assert response.status_code == 200, response.text

    def test_preview_invalid_sample_size_too_small_returns_422(self, admin_client):
        """sample_size below minimum (10) returns 422."""
        seg = _create_segment(admin_client, "Small Sample Segment")

        payload = {
            "name": seg["name"],
            "rules": US_RULES,
        }
        response = admin_client.post(
            f"/api/v1/segments/{seg['id']}/preview?sample_size=5",
            json=payload,
        )
        assert response.status_code == 422, response.text

    def test_preview_returns_non_negative_values(self, admin_client):
        """All numeric fields in preview response are non-negative."""
        seg = _create_segment(admin_client, "Non Negative Preview")

        payload = {
            "name": seg["name"],
            "rules": US_RULES,
        }
        response = admin_client.post(
            f"/api/v1/segments/{seg['id']}/preview", json=payload
        )
        assert response.status_code == 200, response.text

        data = response.json()
        assert data["estimated_percentage"] >= 0
        assert data["sample_size"] >= 0
        assert data["matched"] >= 0

    @pytest.mark.regression
    def test_preview_refuses_eleven_match_regex_conditions(self, admin_client):
        """More than 10 match_regex conditions is a 422 on rules, not a preview."""
        seg = _create_segment(admin_client, "Regex Limit Segment")
        conditions = [_regex(f"^u{i}") for i in range(11)]
        response = admin_client.post(
            f"/api/v1/segments/{seg['id']}/preview?sample_size=10",
            json={"name": seg["name"], "rules": _rules(*conditions)},
        )
        assert response.status_code == 422, response.text
        [error] = response.json()["detail"]
        assert error["loc"] == ["body", "rules"]
        assert error["type"] == "value_error"

    @pytest.mark.regression
    def test_preview_accepts_ten_regex_conditions_at_sample_size_45(self, admin_client):
        """10 regex conditions x sample_size 45 is inside the limits and previews."""
        seg = _create_segment(admin_client, "Regex At Limit Segment")
        conditions = [_regex(f"^u{i}") for i in range(10)]
        response = admin_client.post(
            f"/api/v1/segments/{seg['id']}/preview?sample_size=45",
            json={"name": seg["name"], "rules": _rules(*conditions)},
        )
        assert response.status_code == 200, response.text
        assert response.json()["sample_size"] <= 45

    def test_preview_refuses_conditions_plus_groups_times_sample_size_over_50000(
        self, admin_client
    ):
        """49 conditions and one group at sample_size 1,000 is 50,000 and is
        previewed; 50 conditions and one group is 51,000 and is a 422."""
        seg = _create_segment(admin_client, "Preview Size Segment")
        for count, expected in ((49, 200), (50, 422)):
            rules = _rules(*[_eq(f"a{i}", "x") for i in range(count)])
            response = admin_client.post(
                f"/api/v1/segments/{seg['id']}/preview?sample_size=1000",
                json={"name": seg["name"], "rules": rules},
            )
            assert response.status_code == expected, response.text
            if expected == 422:
                [error] = response.json()["detail"]
                assert error["loc"] == ["query", "sample_size"]

    @pytest.mark.regression
    def test_preview_refuses_a_sample_size_the_match_regex_conditions_exceed(
        self, admin_client
    ):
        """One match_regex condition at the default sample_size (1000) is a 422."""
        seg = _create_segment(admin_client, "Regex Sample Segment")
        rules = _rules(_regex("x"))
        response = admin_client.post(
            f"/api/v1/segments/{seg['id']}/preview",
            json={"name": seg["name"], "rules": rules},
        )
        assert response.status_code == 422, response.text
        [error] = response.json()["detail"]
        assert error["loc"] == ["query", "sample_size"]
