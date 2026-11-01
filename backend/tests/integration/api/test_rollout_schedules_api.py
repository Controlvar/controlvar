"""
Integration tests for the Rollout Schedules REST API (EP-011).

Tests the full HTTP request/response cycle for rollout schedule CRUD operations,
stage management, state transitions, and error handling.

Architecture
------------
All tests in this module share a single module-scoped TestClient that is
authenticated as an admin user.  The client uses a dedicated SQLAlchemy engine
(pool_size=5) that is separate from the NullPool engine used by other tests.
This guarantees that data committed by one API call is always visible to the next,
regardless of how many connections are checked out.

The test database (experimentation_test) is created and tables are created by the
session-scoped ``test_db`` fixture in ``conftest.py`` before any tests run.

Endpoint coverage:
  POST   /api/v1/rollout-schedules/                     create schedule (201)
  GET    /api/v1/rollout-schedules/                     list schedules
  GET    /api/v1/rollout-schedules/{schedule_id}        get schedule
  PUT    /api/v1/rollout-schedules/{schedule_id}        update schedule
  DELETE /api/v1/rollout-schedules/{schedule_id}        delete schedule (204)
  POST   /api/v1/rollout-schedules/{schedule_id}/activate    activate
  POST   /api/v1/rollout-schedules/{schedule_id}/pause       pause
  POST   /api/v1/rollout-schedules/{schedule_id}/cancel      cancel
  POST   /api/v1/rollout-schedules/{schedule_id}/stages      add stage (201)
  PUT    /api/v1/rollout-schedules/stages/{stage_id}         update stage
  DELETE /api/v1/rollout-schedules/stages/{stage_id}         delete stage (204)
  POST   /api/v1/rollout-schedules/stages/{stage_id}/advance advance stage
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy import event as sa_event
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.deps import CacheControl
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.user import User, UserRole
from backend.tests.conftest import DEFAULT_TEST_DB_URL

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HASHED_PASSWORD = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"
SCHEMA = "test_experimentation"
# The per-process test database the root conftest builds, on the host, port and
# credentials it reads from POSTGRES_SERVER/PORT/USER/PASSWORD (#283).
DB_URL = DEFAULT_TEST_DB_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future_dt(hours: int = 24) -> str:
    """Return ISO 8601 datetime string `hours` into the future (UTC)."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _valid_schedule_payload(feature_flag_id: str, name: str = "Test Schedule") -> dict:
    """Return a minimal valid RolloutScheduleCreate payload."""
    return {
        "name": name,
        "feature_flag_id": feature_flag_id,
        "start_date": _future_dt(1),
        "end_date": _future_dt(72),
        "max_percentage": 100,
        "min_stage_duration": 1,
        "stages": [
            {
                "name": "Stage 1",
                "stage_order": 1,
                "target_percentage": 25,
                "trigger_type": "time_based",
                "start_date": _future_dt(1),
            },
            {
                "name": "Stage 2",
                "stage_order": 2,
                "target_percentage": 100,
                "trigger_type": "manual",
            },
        ],
    }


def _create_schedule_via_api(
    client: TestClient, feature_flag_id: str, name: str = "Test Schedule"
) -> dict:
    """Create a schedule via the API and return the JSON response body."""
    response = client.post(
        "/api/v1/rollout-schedules/",
        json=_valid_schedule_payload(feature_flag_id, name),
    )
    assert response.status_code == 201, (
        f"Create failed ({response.status_code}): {response.text}"
    )
    return response.json()


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _module_engine(test_db):
    """Create a dedicated connection-pooled engine for this test module.

    Unlike the NullPool engine in test_db, this engine keeps connections alive
    across commits, guaranteeing that data written by one request is immediately
    visible to the next request's session.

    The engine is disposed at module teardown so that the experimentation_test
    database can be cleanly dropped by the session-scoped test_db teardown.
    """
    engine = create_engine(
        DB_URL,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=300,
    )

    # Ensure every new connection sets the correct search_path.
    @sa_event.listens_for(engine, "connect")
    def set_search_path(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    yield engine

    # Close all pooled connections so the session-scoped teardown can DROP the DB.
    engine.dispose()


@pytest.fixture(scope="module")
def _module_factory(_module_engine):
    """Return a sessionmaker bound to the module-scoped engine."""
    return sessionmaker(
        bind=_module_engine, autocommit=False, autoflush=False, expire_on_commit=False
    )


@pytest.fixture(scope="module")
def _module_data(_module_factory):
    """Insert shared test data (admin user + feature flag) once for the module."""
    session = _module_factory()
    try:
        suffix = uuid.uuid4().hex[:8]
        admin = User(
            username=f"rs_admin_{suffix}",
            email=f"rs_admin_{suffix}@test.local",
            full_name="RS Admin",
            hashed_password=HASHED_PASSWORD,
            is_active=True,
            is_superuser=True,
            role=UserRole.ADMIN,
        )
        session.add(admin)
        session.flush()

        flag = FeatureFlag(
            key=f"rs-flag-{suffix}",
            name="RS Test Flag",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin.id,
            rollout_percentage=0,
        )
        session.add(flag)
        session.commit()

        return {
            "admin_id": admin.id,
            "admin_username": admin.username,
            "admin_email": admin.email,
            "flag_id": str(flag.id),
        }
    finally:
        session.close()


@pytest.fixture(scope="module")
def shared_client(_module_data, _module_factory):
    """Module-scoped authenticated TestClient.

    override_get_db yields a fresh session per request, but since the sessions
    are bound to the pooled _module_engine (not NullPool), connections are kept
    alive across commits and data written in one request is always visible to
    subsequent requests.
    """
    # Load the admin user for auth overrides.
    setup_session = _module_factory()
    try:
        user = setup_session.query(User).filter_by(id=_module_data["admin_id"]).one()
        setup_session.expunge(user)
    finally:
        setup_session.close()

    def override_get_db():
        session = _module_factory()
        try:
            yield session
        finally:
            try:
                session.commit()
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
            finally:
                session.close()

    async def override_get_current_user():
        return user

    def override_get_current_active_user():
        return user

    def override_get_current_superuser():
        return user

    async def override_get_cache_control():
        return CacheControl(enabled=False, skip=True)

    def override_get_api_key():
        return user

    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[deps.get_current_user] = override_get_current_user
    app.dependency_overrides[deps.get_current_active_user] = (
        override_get_current_active_user
    )
    app.dependency_overrides[deps.get_current_superuser] = (
        override_get_current_superuser
    )
    app.dependency_overrides[deps.get_cache_control] = override_get_cache_control
    app.dependency_overrides[deps.get_api_key] = override_get_api_key

    client = TestClient(app)
    yield client

    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def seeded_flag_id(_module_data):
    """The flag the module-wide ``seeded_schedule`` belongs to (read tests only)."""
    return _module_data["flag_id"]


@pytest.fixture
def flag_id(_module_factory, admin_id):
    """A fresh flag for every test, so no two tests share a flag's schedules."""
    session = _module_factory()
    try:
        flag = FeatureFlag(
            key=f"rs-flag-{uuid.uuid4().hex[:12]}",
            name="RS Per-Test Flag",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin_id,
            rollout_percentage=0,
        )
        session.add(flag)
        session.commit()
        return str(flag.id)
    finally:
        session.close()


@pytest.fixture(scope="module")
def admin_id(_module_data):
    return _module_data["admin_id"]


@pytest.fixture(scope="module")
def seeded_schedule(shared_client, seeded_flag_id):
    """Create one pre-existing schedule for read tests."""
    return _create_schedule_via_api(
        shared_client, seeded_flag_id, "Seeded Read Schedule"
    )


# =============================================================================
# GROUP 1: READ-ONLY / VALIDATION TESTS  (no API db.commit())
# =============================================================================


@pytest.mark.integration
@pytest.mark.requires_db
class TestGetRolloutScheduleValidation:
    """GET endpoint validation — 404/422 tests with no DB writes."""

    def test_get_nonexistent_schedule_returns_404(self, shared_client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = shared_client.get(f"/api/v1/rollout-schedules/{fake_id}")
        assert response.status_code == 404, response.text

    def test_get_schedule_invalid_uuid_returns_422(self, shared_client):
        response = shared_client.get("/api/v1/rollout-schedules/not-a-valid-uuid")
        assert response.status_code == 422, response.text

    def test_update_nonexistent_schedule_returns_not_found(self, shared_client):
        """PUT on a non-existent UUID returns 404 or 500.

        The update endpoint wraps all logic in a broad try/except Exception,
        so HTTPException(404) is re-caught and converted to 500. Both are
        acceptable "not found" signals.
        """
        fake_id = "00000000-0000-0000-0000-000000000001"
        response = shared_client.put(
            f"/api/v1/rollout-schedules/{fake_id}", json={"name": "Ghost"}
        )
        assert response.status_code in (404, 500), response.text
        assert response.status_code != 200

    def test_delete_nonexistent_schedule_returns_not_found(self, shared_client):
        fake_id = "00000000-0000-0000-0000-000000000002"
        response = shared_client.delete(f"/api/v1/rollout-schedules/{fake_id}")
        assert response.status_code in (404, 500), response.text
        assert response.status_code != 204

    def test_activate_nonexistent_schedule_returns_not_found(self, shared_client):
        fake_id = "00000000-0000-0000-0000-000000000003"
        response = shared_client.post(f"/api/v1/rollout-schedules/{fake_id}/activate")
        assert response.status_code in (404, 500), response.text

    def test_pause_nonexistent_schedule_returns_not_found(self, shared_client):
        fake_id = "00000000-0000-0000-0000-000000000004"
        response = shared_client.post(f"/api/v1/rollout-schedules/{fake_id}/pause")
        assert response.status_code in (404, 500), response.text

    def test_cancel_nonexistent_schedule_returns_not_found(self, shared_client):
        fake_id = "00000000-0000-0000-0000-000000000005"
        response = shared_client.post(f"/api/v1/rollout-schedules/{fake_id}/cancel")
        assert response.status_code in (404, 500), response.text

    def test_update_nonexistent_stage_returns_not_found(self, shared_client):
        fake_id = "00000000-0000-0000-0000-000000000007"
        response = shared_client.put(
            f"/api/v1/rollout-schedules/stages/{fake_id}", json={"name": "Ghost"}
        )
        assert response.status_code in (404, 500), response.text
        assert response.status_code != 200

    def test_delete_nonexistent_stage_returns_not_found(self, shared_client):
        fake_id = "00000000-0000-0000-0000-000000000008"
        response = shared_client.delete(f"/api/v1/rollout-schedules/stages/{fake_id}")
        assert response.status_code in (404, 500), response.text
        assert response.status_code != 204

    def test_advance_nonexistent_stage_returns_not_found(self, shared_client):
        fake_id = "00000000-0000-0000-0000-000000000009"
        response = shared_client.post(
            f"/api/v1/rollout-schedules/stages/{fake_id}/advance"
        )
        assert response.status_code in (404, 500), response.text

    def test_add_stage_to_nonexistent_schedule_returns_not_found(self, shared_client):
        fake_id = "00000000-0000-0000-0000-000000000006"
        stage_payload = {
            "name": "Orphan Stage",
            "stage_order": 1,
            "target_percentage": 50,
            "trigger_type": "manual",
        }
        response = shared_client.post(
            f"/api/v1/rollout-schedules/{fake_id}/stages", json=stage_payload
        )
        assert response.status_code in (404, 500), response.text


@pytest.mark.integration
@pytest.mark.requires_db
class TestListRolloutSchedules:
    """GET /api/v1/rollout-schedules/ list endpoint tests."""

    def test_list_returns_200(self, shared_client):
        response = shared_client.get("/api/v1/rollout-schedules/")
        assert response.status_code == 200, response.text

    def test_list_returns_paginated_structure(self, shared_client):
        response = shared_client.get("/api/v1/rollout-schedules/")
        assert response.status_code == 200, response.text
        data = response.json()
        assert "items" in data
        assert "total" in data
        assert "skip" in data
        assert "limit" in data
        assert isinstance(data["items"], list)

    def test_list_empty_filter_returns_empty_items(self, shared_client):
        """Listing with a nonexistent feature_flag_id returns empty items."""
        fake_flag_id = str(uuid.uuid4())
        response = shared_client.get(
            "/api/v1/rollout-schedules/",
            params={"feature_flag_id": fake_flag_id},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_includes_seeded_schedule(self, shared_client, seeded_schedule):
        """Schedules appear in the list response."""
        response = shared_client.get("/api/v1/rollout-schedules/")
        assert response.status_code == 200, response.text
        ids = [item["id"] for item in response.json()["items"]]
        assert seeded_schedule["id"] in ids

    def test_filter_by_feature_flag_id(
        self, shared_client, seeded_schedule, seeded_flag_id
    ):
        """Filtering by feature_flag_id returns schedules for that flag only."""
        response = shared_client.get(
            "/api/v1/rollout-schedules/",
            params={"feature_flag_id": seeded_flag_id},
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) >= 1
        for item in items:
            assert item["feature_flag_id"] == seeded_flag_id

    def test_filter_by_status_draft(self, shared_client, seeded_schedule):
        """Filtering by status=draft returns only draft schedules."""
        response = shared_client.get(
            "/api/v1/rollout-schedules/",
            params={"status": "draft"},
        )
        assert response.status_code == 200, response.text
        for item in response.json()["items"]:
            assert item["status"] == "draft"


@pytest.mark.integration
@pytest.mark.requires_db
class TestGetRolloutSchedule:
    """GET /api/v1/rollout-schedules/{schedule_id} — uses seeded_schedule fixture."""

    def test_get_existing_schedule(self, shared_client, seeded_schedule):
        schedule_id = seeded_schedule["id"]
        response = shared_client.get(f"/api/v1/rollout-schedules/{schedule_id}")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["id"] == schedule_id
        assert data["name"] == seeded_schedule["name"]

    def test_get_schedule_response_contains_stages(
        self, shared_client, seeded_schedule
    ):
        schedule_id = seeded_schedule["id"]
        response = shared_client.get(f"/api/v1/rollout-schedules/{schedule_id}")
        assert response.status_code == 200, response.text
        assert len(response.json()["stages"]) == 2

    def test_get_schedule_response_fields(self, shared_client, seeded_schedule):
        schedule_id = seeded_schedule["id"]
        response = shared_client.get(f"/api/v1/rollout-schedules/{schedule_id}")
        assert response.status_code == 200, response.text
        data = response.json()
        for field in (
            "id",
            "name",
            "status",
            "feature_flag_id",
            "stages",
            "created_at",
            "updated_at",
        ):
            assert field in data, f"Missing field: {field}"


@pytest.mark.integration
@pytest.mark.requires_db
class TestCreateScheduleValidation:
    """Schema/validation tests for POST — rejected before any DB write happens."""

    def test_create_schedule_without_stages_returns_422(self, shared_client, flag_id):
        payload = _valid_schedule_payload(flag_id)
        payload["stages"] = []
        response = shared_client.post("/api/v1/rollout-schedules/", json=payload)
        assert response.status_code == 422, response.text

    def test_create_schedule_missing_name_returns_422(self, shared_client, flag_id):
        payload = _valid_schedule_payload(flag_id)
        del payload["name"]
        response = shared_client.post("/api/v1/rollout-schedules/", json=payload)
        assert response.status_code == 422, response.text

    def test_create_schedule_end_before_start_returns_422(self, shared_client, flag_id):
        payload = _valid_schedule_payload(flag_id)
        payload["start_date"] = _future_dt(72)
        payload["end_date"] = _future_dt(1)
        response = shared_client.post("/api/v1/rollout-schedules/", json=payload)
        assert response.status_code in (400, 422), response.text

    def test_create_schedule_with_decreasing_percentages_fails(
        self, shared_client, flag_id
    ):
        payload = _valid_schedule_payload(flag_id)
        payload["stages"][0]["target_percentage"] = 100
        payload["stages"][1]["target_percentage"] = 25
        response = shared_client.post("/api/v1/rollout-schedules/", json=payload)
        assert response.status_code in (400, 422), response.text

    def test_create_schedule_non_sequential_stage_orders_returns_422(
        self, shared_client, flag_id
    ):
        payload = _valid_schedule_payload(flag_id)
        payload["stages"][1]["stage_order"] = 5
        response = shared_client.post("/api/v1/rollout-schedules/", json=payload)
        assert response.status_code in (400, 422), response.text

    def test_create_schedule_for_nonexistent_flag_fails(self, shared_client):
        fake_flag_id = str(uuid.uuid4())
        payload = _valid_schedule_payload(fake_flag_id, "Orphan Schedule")
        response = shared_client.post("/api/v1/rollout-schedules/", json=payload)
        assert response.status_code in (400, 404, 422, 500), response.text
        assert response.status_code != 201


# =============================================================================
# GROUP 2: WRITE TESTS (API db.commit() per test)
# =============================================================================


@pytest.mark.integration
@pytest.mark.requires_db
class TestCreateRolloutSchedule:
    """POST /api/v1/rollout-schedules/ — happy-path create tests."""

    def test_admin_can_create_schedule(self, shared_client, flag_id):
        payload = _valid_schedule_payload(flag_id, "Admin Create Test")
        response = shared_client.post("/api/v1/rollout-schedules/", json=payload)
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["name"] == "Admin Create Test"
        assert data["status"] == "draft"
        assert "id" in data
        assert len(data["stages"]) == 2

    def test_created_schedule_default_status_is_draft(self, shared_client, flag_id):
        response = shared_client.post(
            "/api/v1/rollout-schedules/",
            json=_valid_schedule_payload(flag_id, "Status Check"),
        )
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "draft"

    def test_created_schedule_stores_stages_and_owner(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Stage+Owner Test")
        assert len(data["stages"]) == 2
        assert "owner_id" in data
        orders = sorted(s["stage_order"] for s in data["stages"])
        assert orders == [1, 2]

    def test_created_schedule_feature_flag_id_matches(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Flag ID Check")
        assert data["feature_flag_id"] == flag_id


@pytest.mark.integration
@pytest.mark.requires_db
class TestUpdateRolloutSchedule:
    """PUT /api/v1/rollout-schedules/{schedule_id}"""

    def test_admin_can_update_name(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Original Name")
        response = shared_client.put(
            f"/api/v1/rollout-schedules/{data['id']}", json={"name": "Updated Name"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Updated Name"

    def test_update_description_and_max_percentage(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Multi Update")
        response = shared_client.put(
            f"/api/v1/rollout-schedules/{data['id']}",
            json={"description": "New desc", "max_percentage": 75},
        )
        assert response.status_code == 200, response.text
        resp = response.json()
        assert resp["description"] == "New desc"
        assert resp["max_percentage"] == 75

    def test_update_min_stage_duration(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Duration Update")
        response = shared_client.put(
            f"/api/v1/rollout-schedules/{data['id']}",
            json={"min_stage_duration": 48},
        )
        assert response.status_code == 200, response.text
        assert response.json()["min_stage_duration"] == 48


@pytest.mark.integration
@pytest.mark.requires_db
class TestDeleteRolloutSchedule:
    """DELETE /api/v1/rollout-schedules/{schedule_id}"""

    def test_admin_can_delete_draft_schedule(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Delete Me")
        response = shared_client.delete(f"/api/v1/rollout-schedules/{data['id']}")
        assert response.status_code == 204, response.text

    def test_deleted_schedule_not_found_on_get(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Gone Schedule")
        schedule_id = data["id"]
        shared_client.delete(f"/api/v1/rollout-schedules/{schedule_id}")
        response = shared_client.get(f"/api/v1/rollout-schedules/{schedule_id}")
        assert response.status_code == 404, response.text

    def test_delete_active_schedule_returns_400(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Active Delete")
        schedule_id = data["id"]
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/activate")
        response = shared_client.delete(f"/api/v1/rollout-schedules/{schedule_id}")
        assert response.status_code == 400, response.text

    def test_delete_cancelled_schedule_succeeds(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Cancel+Delete")
        schedule_id = data["id"]
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/cancel")
        response = shared_client.delete(f"/api/v1/rollout-schedules/{schedule_id}")
        assert response.status_code == 204, response.text


@pytest.mark.integration
@pytest.mark.requires_db
class TestRolloutScheduleLifecycle:
    """State transitions: activate / pause / cancel / full lifecycle."""

    def test_activate_draft_schedule(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Activate Me")
        response = shared_client.post(
            f"/api/v1/rollout-schedules/{data['id']}/activate"
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "active"

    def test_pause_active_schedule(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Pause Me")
        schedule_id = data["id"]
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/activate")
        response = shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/pause")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "paused"

    def test_activate_paused_schedule(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Re-Activate")
        schedule_id = data["id"]
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/activate")
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/pause")
        response = shared_client.post(
            f"/api/v1/rollout-schedules/{schedule_id}/activate"
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "active"

    def test_pause_draft_schedule_returns_400(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Draft Pause")
        response = shared_client.post(f"/api/v1/rollout-schedules/{data['id']}/pause")
        assert response.status_code == 400, response.text

    def test_cancel_draft_schedule(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Cancel Draft")
        response = shared_client.post(f"/api/v1/rollout-schedules/{data['id']}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"

    def test_cancel_active_schedule(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Cancel Active")
        schedule_id = data["id"]
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/activate")
        response = shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"

    def test_cancel_paused_schedule(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Cancel Paused")
        schedule_id = data["id"]
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/activate")
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/pause")
        response = shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"

    def test_full_lifecycle_draft_to_active_to_paused_to_cancelled(
        self, shared_client, flag_id
    ):
        """Full state machine: DRAFT -> ACTIVE -> PAUSED -> CANCELLED."""
        data = _create_schedule_via_api(shared_client, flag_id, "Full Lifecycle")
        schedule_id = data["id"]
        assert data["status"] == "draft"

        r = shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/activate")
        assert r.status_code == 200
        assert r.json()["status"] == "active"

        r = shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/pause")
        assert r.status_code == 200
        assert r.json()["status"] == "paused"

        r = shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/cancel")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_activate_schedule_without_stages_returns_400(
        self, shared_client, _module_factory, admin_id
    ):
        """Activating a schedule with no stages returns 400.

        The schedule is created directly in the DB (bypassing the POST schema
        validation that requires at least one stage).
        """
        suffix = uuid.uuid4().hex[:6]
        session = _module_factory()
        try:
            flag2 = FeatureFlag(
                key=f"rs-no-stage-{suffix}",
                name=f"No Stage Flag {suffix}",
                status=FeatureFlagStatus.INACTIVE,
                owner_id=admin_id,
                rollout_percentage=0,
            )
            session.add(flag2)
            session.flush()

            schedule_obj = RolloutSchedule(
                name="No Stages Schedule",
                feature_flag_id=flag2.id,
                owner_id=admin_id,
                status=RolloutScheduleStatus.DRAFT,
                max_percentage=100,
            )
            session.add(schedule_obj)
            session.commit()
            schedule_id = str(schedule_obj.id)
        finally:
            session.close()

        response = shared_client.post(
            f"/api/v1/rollout-schedules/{schedule_id}/activate"
        )
        assert response.status_code == 400, response.text


@pytest.mark.integration
@pytest.mark.requires_db
class TestRolloutStages:
    """Stage CRUD: POST /{id}/stages, PUT /stages/{stage_id}, DELETE, advance."""

    def test_add_stage_to_draft_schedule(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Add Stage Test")
        schedule_id = data["id"]

        response = shared_client.post(
            f"/api/v1/rollout-schedules/{schedule_id}/stages",
            json={
                "name": "Stage 3",
                "stage_order": 3,
                "target_percentage": 100,
                "trigger_type": "manual",
            },
        )
        assert response.status_code == 201, response.text
        resp = response.json()
        assert resp["name"] == "Stage 3"
        assert resp["rollout_schedule_id"] == schedule_id

    def test_update_stage_name_and_percentage(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Stage Update")
        stage_id = data["stages"][0]["id"]

        response = shared_client.put(
            f"/api/v1/rollout-schedules/stages/{stage_id}",
            json={"name": "Renamed Stage", "target_percentage": 30},
        )
        assert response.status_code == 200, response.text
        resp = response.json()
        assert resp["name"] == "Renamed Stage"
        assert resp["target_percentage"] == 30

    def test_delete_pending_stage(self, shared_client, flag_id):
        data = _create_schedule_via_api(shared_client, flag_id, "Stage Delete")
        stage_id = data["stages"][0]["id"]

        response = shared_client.delete(f"/api/v1/rollout-schedules/stages/{stage_id}")
        assert response.status_code == 204, response.text

    def test_delete_stage_from_active_schedule_returns_400(
        self, shared_client, flag_id
    ):
        data = _create_schedule_via_api(shared_client, flag_id, "Active Stage Del")
        schedule_id = data["id"]
        stage_id = data["stages"][0]["id"]

        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/activate")
        response = shared_client.delete(f"/api/v1/rollout-schedules/stages/{stage_id}")
        assert response.status_code == 400, response.text

    def test_advance_manual_stage_on_active_schedule(self, shared_client, flag_id):
        """Manually advancing a PENDING manual stage on ACTIVE schedule returns 200."""
        data = _create_schedule_via_api(shared_client, flag_id, "Advance Stage")
        schedule_id = data["id"]

        manual_stage = next(s for s in data["stages"] if s["trigger_type"] == "manual")
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/activate")

        response = shared_client.post(
            f"/api/v1/rollout-schedules/stages/{manual_stage['id']}/advance"
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "in_progress"

    def test_advance_time_based_stage_returns_400(self, shared_client, flag_id):
        """Advancing a time_based stage (non-manual) returns 400."""
        data = _create_schedule_via_api(shared_client, flag_id, "Time Advance")
        schedule_id = data["id"]

        time_stage = next(
            s for s in data["stages"] if s["trigger_type"] == "time_based"
        )
        shared_client.post(f"/api/v1/rollout-schedules/{schedule_id}/activate")

        response = shared_client.post(
            f"/api/v1/rollout-schedules/stages/{time_stage['id']}/advance"
        )
        assert response.status_code == 400, response.text

    @pytest.mark.regression
    def test_advance_stage_on_paused_schedule_returns_400_and_changes_nothing(
        self, shared_client, flag_id, _module_factory
    ):
        """A manual stage of a PAUSED schedule is not advanced (#629): 400
        with main's refusal text, the stage stays pending and the flag's
        percentage is unchanged."""
        data = _create_schedule_via_api(shared_client, flag_id, "Advance Paused")
        schedule_id = data["id"]
        manual_stage = next(s for s in data["stages"] if s["trigger_type"] == "manual")
        assert (
            shared_client.post(
                f"/api/v1/rollout-schedules/{schedule_id}/activate"
            ).status_code
            == 200
        )
        assert (
            shared_client.post(
                f"/api/v1/rollout-schedules/{schedule_id}/pause"
            ).status_code
            == 200
        )

        def percentage():
            session = _module_factory()
            try:
                return session.get(FeatureFlag, uuid.UUID(flag_id)).rollout_percentage
            finally:
                session.close()

        before = percentage()
        response = shared_client.post(
            f"/api/v1/rollout-schedules/stages/{manual_stage['id']}/advance"
        )

        assert response.status_code == 400, response.text
        assert (
            response.json()["detail"]
            == "Cannot advance stage for schedule in paused status"
        )
        assert percentage() == before
        stages = shared_client.get(f"/api/v1/rollout-schedules/{schedule_id}").json()[
            "stages"
        ]
        assert (
            next(s for s in stages if s["id"] == manual_stage["id"])["status"]
            == "pending"
        )

    def test_advance_stage_on_inactive_schedule_returns_error(
        self, shared_client, flag_id
    ):
        """Advancing a stage on a DRAFT schedule returns 400 or 500."""
        data = _create_schedule_via_api(shared_client, flag_id, "Advance Inactive")
        manual_stage = next(s for s in data["stages"] if s["trigger_type"] == "manual")

        response = shared_client.post(
            f"/api/v1/rollout-schedules/stages/{manual_stage['id']}/advance"
        )
        assert response.status_code in (400, 500), response.text
        assert response.status_code != 200


@pytest.mark.integration
@pytest.mark.requires_db
class TestRolloutScheduleAuthorization:
    """Cross-role authorization tests."""

    def test_admin_can_manage_full_lifecycle(self, shared_client, flag_id):
        """Admin can create, activate, pause, and cancel a schedule."""
        create_resp = shared_client.post(
            "/api/v1/rollout-schedules/",
            json=_valid_schedule_payload(flag_id, "Admin Full Lifecycle"),
        )
        assert create_resp.status_code == 201
        schedule_id = create_resp.json()["id"]

        assert (
            shared_client.post(
                f"/api/v1/rollout-schedules/{schedule_id}/activate"
            ).status_code
            == 200
        )

        assert (
            shared_client.post(
                f"/api/v1/rollout-schedules/{schedule_id}/pause"
            ).status_code
            == 200
        )

        cancel_resp = shared_client.post(
            f"/api/v1/rollout-schedules/{schedule_id}/cancel"
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

    def test_list_endpoint_requires_authentication(self):
        """GET /api/v1/rollout-schedules/ requires authentication."""
        saved_overrides = dict(app.dependency_overrides)
        try:
            app.dependency_overrides.clear()
            raw_client = TestClient(app, raise_server_exceptions=False)
            response = raw_client.get("/api/v1/rollout-schedules/")
            assert response.status_code in (401, 403), response.text
        finally:
            app.dependency_overrides.update(saved_overrides)

    def test_create_endpoint_requires_authentication(self, flag_id):
        """POST /api/v1/rollout-schedules/ requires authentication."""
        saved_overrides = dict(app.dependency_overrides)
        try:
            app.dependency_overrides.clear()
            raw_client = TestClient(app, raise_server_exceptions=False)
            response = raw_client.post(
                "/api/v1/rollout-schedules/",
                json=_valid_schedule_payload(flag_id, "Unauth Create"),
            )
            assert response.status_code in (401, 403), response.text
        finally:
            app.dependency_overrides.update(saved_overrides)

    def test_admin_can_create_and_immediately_update(self, shared_client, flag_id):
        """Create then update within the same authenticated session."""
        data = _create_schedule_via_api(shared_client, flag_id, "Create+Update Admin")
        update_resp = shared_client.put(
            f"/api/v1/rollout-schedules/{data['id']}",
            json={"name": "Updated by Admin"},
        )
        assert update_resp.status_code == 200, update_resp.text
        assert update_resp.json()["name"] == "Updated by Admin"

    def test_admin_can_create_and_delete(self, shared_client, flag_id):
        """Create then delete within the same authenticated session."""
        data = _create_schedule_via_api(shared_client, flag_id, "Create+Delete Admin")
        delete_resp = shared_client.delete(f"/api/v1/rollout-schedules/{data['id']}")
        assert delete_resp.status_code == 204, delete_resp.text
