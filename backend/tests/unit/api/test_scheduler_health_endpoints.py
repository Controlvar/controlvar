"""
Unit tests for scheduler health endpoints (P3-B TDD).

Tests cover:
- GET /api/v1/scheduler/health returns list of 4 health responses
- GET /api/v1/scheduler/health/{name} returns health for named scheduler
- GET /api/v1/scheduler/{name}/history returns list of run records
- POST /api/v1/scheduler/notify/test returns 200 for ADMIN user
- POST /api/v1/scheduler/notify/test returns 403 for non-ADMIN user
- GET /api/v1/scheduler/health/{name} with invalid name returns 422
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.schemas.scheduler import (
    SchedulerHealthResponse,
    SchedulerName,
    SchedulerRunRecord,
    SchedulerRunStatus,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_admin_user():
    user = MagicMock(spec=User)
    user.id = uuid4()
    user.email = "admin@example.com"
    user.username = "admin"
    user.is_active = True
    user.is_superuser = True
    user.role = UserRole.ADMIN
    return user


def make_developer_user():
    user = MagicMock(spec=User)
    user.id = uuid4()
    user.email = "dev@example.com"
    user.username = "developer"
    user.is_active = True
    user.is_superuser = False
    user.role = UserRole.DEVELOPER
    return user


def make_mock_db():
    from sqlalchemy.orm import Session

    return MagicMock(spec=Session)


def make_health_response(name: SchedulerName) -> SchedulerHealthResponse:
    return SchedulerHealthResponse(
        scheduler_name=name,
        is_running=True,
        last_run_at="2026-03-01T10:00:00Z",
        last_run_status=SchedulerRunStatus.SUCCESS,
        consecutive_failures=0,
        average_duration_seconds=3.5,
    )


def make_run_record(name: SchedulerName) -> SchedulerRunRecord:
    return SchedulerRunRecord(
        id=str(uuid4()),
        scheduler_name=name,
        started_at="2026-03-01T10:00:00Z",
        completed_at="2026-03-01T10:00:05Z",
        status=SchedulerRunStatus.SUCCESS,
        items_processed=3,
        items_failed=0,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/scheduler/health
# ---------------------------------------------------------------------------


class TestGetAllSchedulerHealth:
    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_returns_200_for_authenticated_user(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        all_health = [make_health_response(name) for name in SchedulerName]

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.SchedulerHealthService.get_all_health",
            return_value=all_health,
        ):
            client = TestClient(app)
            response = client.get("/api/v1/scheduler/health")

        assert response.status_code == 200

    def test_returns_one_entry_per_scheduler(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        all_health = [make_health_response(name) for name in SchedulerName]

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.SchedulerHealthService.get_all_health",
            return_value=all_health,
        ):
            client = TestClient(app)
            response = client.get("/api/v1/scheduler/health")

        data = response.json()
        assert isinstance(data, list)
        assert len(data) == len(SchedulerName)

    def test_unauthenticated_returns_401(self):
        # No dependency override — no auth provided
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/scheduler/health")
        assert response.status_code in (401, 403, 422)


# ---------------------------------------------------------------------------
# GET /api/v1/scheduler/health/{name}
# ---------------------------------------------------------------------------


class TestGetSchedulerHealthByName:
    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_returns_200_for_valid_name(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        health = make_health_response(SchedulerName.EXPERIMENT)

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.SchedulerHealthService.get_health",
            return_value=health,
        ):
            client = TestClient(app)
            response = client.get("/api/v1/scheduler/health/experiment")

        assert response.status_code == 200

    def test_returns_correct_scheduler_name(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        health = make_health_response(SchedulerName.SAFETY)

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.SchedulerHealthService.get_health",
            return_value=health,
        ):
            client = TestClient(app)
            response = client.get("/api/v1/scheduler/health/safety")

        data = response.json()
        assert data["scheduler_name"] == "safety"

    def test_invalid_scheduler_name_returns_422(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/scheduler/health/nonexistent_scheduler")
        assert response.status_code == 422

    def test_returns_health_fields(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        health = make_health_response(SchedulerName.METRICS)

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.SchedulerHealthService.get_health",
            return_value=health,
        ):
            client = TestClient(app)
            response = client.get("/api/v1/scheduler/health/metrics")

        data = response.json()
        assert "is_running" in data
        assert "consecutive_failures" in data


# ---------------------------------------------------------------------------
# GET /api/v1/scheduler/{name}/history
# ---------------------------------------------------------------------------


class TestGetSchedulerHistory:
    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_returns_200(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        records = [make_run_record(SchedulerName.ROLLOUT) for _ in range(3)]

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.SchedulerHealthService.get_run_history",
            return_value=records,
        ):
            client = TestClient(app)
            response = client.get("/api/v1/scheduler/rollout/history")

        assert response.status_code == 200

    def test_returns_list(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        records = [make_run_record(SchedulerName.EXPERIMENT) for _ in range(5)]

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.SchedulerHealthService.get_run_history",
            return_value=records,
        ):
            client = TestClient(app)
            response = client.get("/api/v1/scheduler/experiment/history")

        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 5

    @pytest.mark.regression
    @pytest.mark.parametrize("limit", [0, -1, 1001])
    def test_rejects_limit_outside_bounds(self, limit):
        """A limit outside 1..1000 is invalid, not a database error."""

        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.SchedulerHealthService.get_run_history",
        ) as get_run_history:
            client = TestClient(app)
            response = client.get(
                f"/api/v1/scheduler/experiment/history?limit={limit}",
            )

        assert response.status_code == 422
        get_run_history.assert_not_called()

    @pytest.mark.regression
    @pytest.mark.parametrize("limit", [1, 1000])
    def test_accepts_limit_at_bounds(self, limit):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.SchedulerHealthService.get_run_history",
            return_value=[],
        ):
            client = TestClient(app)
            response = client.get(
                f"/api/v1/scheduler/experiment/history?limit={limit}",
            )

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/scheduler/notify/test
# ---------------------------------------------------------------------------


class TestPostNotifyTest:
    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_returns_200_for_admin_user(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.NotificationService.send_webhook",
            return_value=True,
        ):
            client = TestClient(app)
            response = client.post("/api/v1/scheduler/notify/test")

        assert response.status_code == 200

    def test_returns_403_for_non_superuser(self):
        """Non-superuser (developer) should receive 403 Forbidden."""
        dev_user = make_developer_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: dev_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        # Override get_current_superuser to raise 403
        from fastapi import HTTPException

        def raise_403():
            raise HTTPException(status_code=403, detail="Not enough permissions")

        app.dependency_overrides[deps.get_current_superuser] = raise_403

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/api/v1/scheduler/notify/test")

        assert response.status_code == 403

    def test_response_includes_status_field(self):
        admin_user = make_admin_user()
        mock_db = make_mock_db()

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user
        app.dependency_overrides[deps.get_db] = lambda: mock_db

        with patch(
            "backend.app.api.v1.endpoints.scheduler_health.NotificationService.send_webhook",
            return_value=True,
        ):
            client = TestClient(app)
            response = client.post("/api/v1/scheduler/notify/test")

        data = response.json()
        assert "status" in data or "message" in data or "sent" in data
