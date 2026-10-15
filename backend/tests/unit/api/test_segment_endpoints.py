"""
Unit tests for Audience Segmentation API endpoints (P3-C).

Uses FastAPI TestClient with dependency overrides.
No real database required — all DB and service calls are mocked.
"""

import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api import deps
from backend.app.api.v1.endpoints.segments import SEGMENT_RULES_NOT_VALID
from backend.app.main import app
from backend.app.models.segment import Segment
from backend.app.models.segment import SegmentStatus as ModelSegmentStatus
from backend.app.models.user import User, UserRole
from backend.app.schemas.segment import (
    AudiencePreviewResponse,
    BulkSegmentMembershipResponse,
    SegmentExperimentResponse,
    SegmentMembershipResponse,
    SegmentStatus,
)
from backend.app.services.audience_service import SegmentRulesNotValid

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(role=UserRole.DEVELOPER, is_superuser=False):
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.username = "testuser"
    user.email = "test@example.com"
    user.is_active = True
    user.is_superuser = is_superuser
    user.role = role
    return user


def _make_segment(
    segment_id=None,
    name="Test Segment",
    status=ModelSegmentStatus.ACTIVE,
    rules=None,
):
    seg = MagicMock(spec=Segment)
    seg.id = segment_id or uuid.uuid4()
    seg.name = name
    seg.description = "Test description"
    seg.status = status
    seg.rules = rules or VALID_RULES
    seg.created_at = datetime(2025, 1, 1, 12, 0)
    seg.updated_at = datetime(2025, 1, 2, 12, 0)
    return seg


VALID_RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "US"}
            ],
        }
    ],
}


class TestListSegments:
    """GET /api/v1/segments"""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_get_segments_returns_list(self):
        """GET /segments returns 200 with a list."""
        mock_user = _make_user()
        mock_db = MagicMock()
        seg = _make_segment()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.list_segments",
            return_value=[seg],
        ):
            client = TestClient(app)
            response = client.get("/api/v1/segments")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1

    def test_get_segments_with_status_filter(self):
        """GET /segments?status=active returns filtered list."""
        mock_user = _make_user()
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.list_segments",
            return_value=[],
        ) as mock_list:
            client = TestClient(app)
            response = client.get("/api/v1/segments?status=active")

        assert response.status_code == 200

    def test_get_segments_returns_empty_list_when_no_segments(self):
        """GET /segments returns empty list when no segments exist."""
        mock_user = _make_user()
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.list_segments",
            return_value=[],
        ):
            client = TestClient(app)
            response = client.get("/api/v1/segments")

        assert response.status_code == 200
        assert response.json() == []


class TestCreateSegment:
    """POST /api/v1/segments"""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_create_segment_returns_201_for_developer(self):
        """POST /segments returns 201 Created for DEVELOPER role."""
        mock_user = _make_user(role=UserRole.DEVELOPER)
        mock_db = MagicMock()
        seg = _make_segment(name="New Segment")

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.create_segment",
            return_value=seg,
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/segments",
                json={"name": "New Segment", "rules": VALID_RULES},
            )

        assert response.status_code == 201

    def test_create_segment_returns_201_for_admin(self):
        """POST /segments returns 201 for ADMIN role."""
        mock_user = _make_user(role=UserRole.ADMIN, is_superuser=True)
        mock_db = MagicMock()
        seg = _make_segment()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.create_segment",
            return_value=seg,
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/segments",
                json={"name": "Admin Segment", "rules": VALID_RULES},
            )

        assert response.status_code == 201

    def test_create_segment_returns_403_for_viewer(self):
        """POST /segments returns 403 Forbidden for VIEWER role."""
        mock_user = _make_user(role=UserRole.VIEWER)
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        client = TestClient(app)
        response = client.post(
            "/api/v1/segments",
            json={"name": "Viewer Segment", "rules": VALID_RULES},
        )

        assert response.status_code == 403

    def test_create_segment_returns_422_for_invalid_name(self):
        """POST /segments returns 422 for too-short name."""
        mock_user = _make_user(role=UserRole.DEVELOPER)
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        client = TestClient(app)
        response = client.post(
            "/api/v1/segments",
            json={"name": "A", "rules": VALID_RULES},
        )

        assert response.status_code == 422


class TestGetSegment:
    """GET /api/v1/segments/{id}"""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_get_segment_returns_200_when_found(self):
        """GET /segments/{id} returns 200 when segment exists."""
        mock_user = _make_user()
        mock_db = MagicMock()
        seg = _make_segment()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.get_segment",
            return_value=seg,
        ):
            client = TestClient(app)
            response = client.get(f"/api/v1/segments/{seg.id}")

        assert response.status_code == 200

    def test_get_segment_returns_404_when_not_found(self):
        """GET /segments/{id} returns 404 when segment doesn't exist."""
        mock_user = _make_user()
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.get_segment",
            side_effect=ValueError("not found"),
        ):
            client = TestClient(app)
            response = client.get(f"/api/v1/segments/{uuid.uuid4()}")

        assert response.status_code == 404


class TestUpdateSegment:
    """PUT /api/v1/segments/{id}"""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_update_segment_returns_200(self):
        """PUT /segments/{id} returns 200 for DEVELOPER."""
        mock_user = _make_user(role=UserRole.DEVELOPER)
        mock_db = MagicMock()
        seg = _make_segment(name="Updated Segment")

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.update_segment",
            return_value=seg,
        ):
            client = TestClient(app)
            response = client.put(
                f"/api/v1/segments/{seg.id}",
                json={"name": "Updated Segment"},
            )

        assert response.status_code == 200


class TestDeleteSegment:
    """DELETE /api/v1/segments/{id}"""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_delete_segment_returns_204(self):
        """DELETE /segments/{id} returns 204 No Content."""
        mock_user = _make_user(role=UserRole.DEVELOPER)
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.delete_segment",
            return_value=None,
        ):
            client = TestClient(app)
            response = client.delete(f"/api/v1/segments/{uuid.uuid4()}")

        assert response.status_code == 204


class TestEvaluateSegmentMembership:
    """POST /api/v1/segments/{id}/evaluate"""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_evaluate_returns_membership_result(self):
        """POST /segments/{id}/evaluate returns membership result."""
        mock_user = _make_user()
        mock_db = MagicMock()
        seg_id = str(uuid.uuid4())

        membership = SegmentMembershipResponse(
            segment_id=seg_id,
            segment_name="Test Segment",
            is_member=True,
            matched_rules=["country equals US"],
        )

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.evaluate_membership",
            return_value=membership,
        ):
            client = TestClient(app)
            response = client.post(
                f"/api/v1/segments/{seg_id}/evaluate",
                json={"user_context": {"country": "US"}},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["is_member"] is True
        assert data["segment_id"] == seg_id

    def test_evaluate_returns_404_for_missing_segment(self):
        """POST /segments/{id}/evaluate returns 404 when segment not found."""
        mock_user = _make_user()
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.evaluate_membership",
            side_effect=ValueError("not found"),
        ):
            client = TestClient(app)
            response = client.post(
                f"/api/v1/segments/{uuid.uuid4()}/evaluate",
                json={"user_context": {"country": "US"}},
            )

        assert response.status_code == 404

    def test_evaluate_returns_409_when_stored_rules_are_not_valid(self):
        """A segment stored before rules were checked answers 409, fixed text."""
        mock_user = _make_user()
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.evaluate_membership",
            side_effect=SegmentRulesNotValid("x"),
        ):
            client = TestClient(app)
            response = client.post(
                f"/api/v1/segments/{uuid.uuid4()}/evaluate",
                json={"user_context": {"country": "US"}},
            )

        assert response.status_code == 409
        assert response.json() == {"detail": SEGMENT_RULES_NOT_VALID}
        assert "rules not valid" in SEGMENT_RULES_NOT_VALID


class TestBulkEvaluate:
    """POST /api/v1/segments/bulk-evaluate"""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_bulk_evaluate_returns_memberships_dict(self):
        """POST /segments/bulk-evaluate returns memberships dict."""
        mock_user = _make_user()
        mock_db = MagicMock()
        seg_id = str(uuid.uuid4())

        bulk_response = BulkSegmentMembershipResponse(
            user_context={"country": "US"},
            memberships={seg_id: True},
            evaluation_time_ms=1.23,
        )

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.bulk_evaluate_membership",
            return_value=bulk_response,
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/segments/bulk-evaluate",
                json={
                    "user_context": {"country": "US"},
                    "segment_ids": [seg_id],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "memberships" in data
        assert data["memberships"][seg_id] is True
        assert "evaluation_time_ms" in data

    def test_bulk_evaluate_returns_422_for_empty_segment_ids(self):
        """POST /segments/bulk-evaluate returns 422 when segment_ids is empty."""
        mock_user = _make_user()
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        client = TestClient(app)
        response = client.post(
            "/api/v1/segments/bulk-evaluate",
            json={
                "user_context": {"country": "US"},
                "segment_ids": [],
            },
        )

        assert response.status_code == 422


class TestGetSegmentExperiments:
    """GET /api/v1/segments/{id}/experiments"""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_get_segment_experiments_returns_linked_experiments(self):
        """GET /segments/{id}/experiments returns experiments list."""
        mock_user = _make_user()
        mock_db = MagicMock()
        seg_id = str(uuid.uuid4())

        exp_response = SegmentExperimentResponse(
            segment_id=seg_id,
            experiments=[{"id": "exp-1", "name": "Test Exp", "status": "active"}],
            feature_flags=[],
        )

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.get_segment_experiments",
            return_value=exp_response,
        ):
            client = TestClient(app)
            response = client.get(f"/api/v1/segments/{seg_id}/experiments")

        assert response.status_code == 200
        data = response.json()
        assert data["segment_id"] == seg_id
        assert len(data["experiments"]) == 1
        assert isinstance(data["feature_flags"], list)

    def test_get_segment_experiments_returns_404_when_segment_missing(self):
        """GET /segments/{id}/experiments returns 404 when segment not found."""
        mock_user = _make_user()
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.get_segment_experiments",
            side_effect=ValueError("not found"),
        ):
            client = TestClient(app)
            response = client.get(f"/api/v1/segments/{uuid.uuid4()}/experiments")

        assert response.status_code == 404

    @pytest.mark.regression
    def test_usage_query_failure_answers_500_not_empty_lists(self):
        """#888: a failed usage query is an error, not 200 with empty lists."""
        mock_user = _make_user()
        mock_db = MagicMock()
        mock_db.query.side_effect = SQLAlchemyError("planted")
        seg = _make_segment()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.services.audience_service.AudienceService.get_segment",
            return_value=seg,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get(f"/api/v1/segments/{seg.id}/experiments")

        assert response.status_code == 500
        assert "experiments" not in response.text


class TestPreviewAudienceSize:
    """POST /api/v1/segments/{id}/preview"""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_preview_returns_estimated_percentage(self):
        """POST /segments/{id}/preview returns estimated_percentage."""
        mock_user = _make_user()
        mock_db = MagicMock()
        seg = _make_segment()

        preview = AudiencePreviewResponse(
            estimated_percentage=42.0,
            sample_size=1000,
            matched=420,
        )

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.get_segment",
            return_value=seg,
        ):
            with patch(
                "backend.app.api.v1.endpoints.segments.AudienceService.preview_audience_size",
                return_value=preview,
            ):
                client = TestClient(app)
                response = client.post(
                    f"/api/v1/segments/{seg.id}/preview",
                    json={"name": "Preview Segment", "rules": VALID_RULES},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["estimated_percentage"] == 42.0
        assert "sample_size" in data
        assert "matched" in data

    def test_preview_returns_404_when_segment_not_found(self):
        """POST /segments/{id}/preview returns 404 when segment not found."""
        mock_user = _make_user()
        mock_db = MagicMock()

        app.dependency_overrides[deps.get_current_active_user] = lambda: mock_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.segments.AudienceService.get_segment",
            side_effect=ValueError("not found"),
        ):
            client = TestClient(app)
            response = client.post(
                f"/api/v1/segments/{uuid.uuid4()}/preview",
                json={"name": "Preview Segment", "rules": VALID_RULES},
            )

        assert response.status_code == 404
