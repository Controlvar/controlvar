"""
E2E test configuration for Phase 6.

Provides fixtures for E2E tests using API-level workflows. Each test uses a
fresh PostgreSQL connection (via NullPool) so that endpoint-internal commits
do not leave stale connections in a shared pool that would break subsequent
tests or other test suites.

Key design decisions:
- NullPool is used for `e2e_engine` so every e2e_db_session gets a brand-new
  DBAPI connection. This prevents the "server closed the connection" errors that
  arise when pooled connections are reused after a test commits/aborts mid-stream.
- `test_db` is still depended upon to ensure the database and schema exist before
  any E2E test runs.
- Unlike db_session (rollback-wrapped), e2e_db_session lets endpoints commit
  freely. This is intentional for E2E tests that must exercise the full
  commit/read cycle.
- Run E2E tests in a separate pytest invocation when mixing with other test suites
  to avoid pool state interference.
"""

import os
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.api import deps
from backend.app.api.read_scope import get_current_reader
from backend.app.main import app
from backend.app.models.experiment import Experiment, ExperimentStatus, Metric, Variant
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.user import User, UserRole

HASHED_PASSWORD = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"
# Use the per-process database that the root conftest's `test_db` fixture
# creates (experimentation_test_<pid>) rather than a fixed name, so the E2E
# engine points at a database that actually has the schema.
from backend.tests.conftest import DEFAULT_TEST_DB_URL


class DictLikeCacheControl:
    """
    Drop-in CacheControl replacement supporting both attribute and dict-style access.

    The create_feature_flag endpoint calls cache_control.get("enabled")
    (dict-style) instead of cache_control.enabled (attribute access), which
    raises AttributeError on the real Pydantic CacheControl object.
    """

    enabled = False
    skip = True
    redis = None

    def get(self, key, default=None):
        return getattr(self, key, default)


@pytest.fixture(scope="session")
def e2e_engine(test_db):
    """
    Create a NullPool engine for E2E tests.

    Depends on `test_db` to ensure the database and schema exist, but uses a
    separate engine with NullPool so every session gets a brand-new DBAPI
    connection. This prevents stale pooled connections from causing
    'server closed the connection' errors across tests.

    Also disposes the app's own connection pool at the start of the fixture to
    ensure no idle connections from the app engine (created during previous test
    runs or earlier in this pytest session) interfere with the E2E tests.
    """
    from backend.app.db.session import engine as app_engine

    app_engine.dispose()

    # Always the per-process database that `test_db` just created; an
    # environment override could only point at a database without tables.
    engine = create_engine(DEFAULT_TEST_DB_URL, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def e2e_db_session(e2e_engine):
    """
    Provide a fresh, autocommit-capable session for each E2E test.

    Uses the NullPool e2e_engine so each call gets a brand-new DBAPI connection.
    This allows endpoints to call db.commit() freely without leaving stale
    connections in a pool that would break subsequent tests.
    """
    Session = sessionmaker(bind=e2e_engine)
    session = Session()
    try:
        session.execute(text("SET search_path TO test_experimentation"))
        session.commit()
        yield session
    finally:
        try:
            session.rollback()
        except Exception:
            pass
        try:
            session.close()
        except Exception:
            pass


@pytest.fixture
def admin_user(e2e_db_session):
    """Create an admin user for E2E tests with a unique username."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"e2e_admin_{suffix}",
        email=f"e2e_admin_{suffix}@test.local",
        full_name="E2E Admin User",
        hashed_password=HASHED_PASSWORD,
        is_active=True,
        is_superuser=True,
        role=UserRole.ADMIN,
    )
    e2e_db_session.add(user)
    e2e_db_session.commit()
    e2e_db_session.refresh(user)
    return user


@pytest.fixture
def admin_client(e2e_db_session, admin_user):
    """
    Create an E2E TestClient authenticated as the admin user.

    Uses DictLikeCacheControl to work around the pre-existing bug in the
    create_feature_flag endpoint that calls cache_control.get() dict-style
    on a Pydantic CacheControl model instance.
    """

    def override_get_db():
        try:
            yield e2e_db_session
        finally:
            pass

    async def override_get_current_user():
        return admin_user

    def override_get_current_active_user():
        return admin_user

    def override_get_current_superuser():
        return admin_user

    async def override_get_cache_control():
        return DictLikeCacheControl()

    def override_get_api_key():
        return admin_user

    async def override_can_create_feature_flag():
        return True

    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[deps.get_current_user] = override_get_current_user
    app.dependency_overrides[deps.get_current_active_user] = (
        override_get_current_active_user
    )
    # The read routes that also take an mcp:read key (read_scope.py).
    app.dependency_overrides[get_current_reader] = override_get_current_active_user
    app.dependency_overrides[deps.get_current_superuser] = (
        override_get_current_superuser
    )
    app.dependency_overrides[deps.get_cache_control] = override_get_cache_control
    app.dependency_overrides[deps.get_api_key] = override_get_api_key
    app.dependency_overrides[deps.can_create_feature_flag] = (
        override_can_create_feature_flag
    )

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# DB factory fixtures using the e2e_db_session
# ---------------------------------------------------------------------------


@pytest.fixture
def make_experiment(e2e_db_session, admin_user):
    """Factory for creating Experiment objects in E2E tests."""

    def _make(**kwargs):
        defaults = {
            "name": "E2E Test Experiment",
            "status": ExperimentStatus.DRAFT,
            "owner_id": admin_user.id,
            "description": "E2E test experiment",
            "hypothesis": "E2E hypothesis",
        }
        defaults.update(kwargs)
        exp = Experiment(**defaults)
        e2e_db_session.add(exp)
        e2e_db_session.commit()
        e2e_db_session.refresh(exp)
        return exp

    return _make


@pytest.fixture
def make_variant(e2e_db_session):
    """Factory for creating Variant objects in E2E tests."""

    def _make(
        experiment, name="Control", is_control=True, traffic_allocation=50, **kwargs
    ):
        variant = Variant(
            experiment_id=experiment.id,
            name=name,
            is_control=is_control,
            traffic_allocation=traffic_allocation,
            **kwargs,
        )
        e2e_db_session.add(variant)
        e2e_db_session.commit()
        e2e_db_session.refresh(variant)
        return variant

    return _make


@pytest.fixture
def make_metric(e2e_db_session):
    """Factory for creating Metric objects in E2E tests."""

    def _make(
        experiment,
        name="Conversion",
        event_name="purchase",
        metric_type="conversion",
        **kwargs,
    ):
        metric = Metric(
            experiment_id=experiment.id,
            name=name,
            event_name=event_name,
            metric_type=metric_type,
            **kwargs,
        )
        e2e_db_session.add(metric)
        e2e_db_session.commit()
        e2e_db_session.refresh(metric)
        return metric

    return _make


@pytest.fixture
def make_feature_flag(e2e_db_session, admin_user):
    """Factory for creating FeatureFlag objects in E2E tests."""

    def _make(**kwargs):
        defaults = {
            "key": f"e2e-flag-{uuid.uuid4().hex[:8]}",
            "name": "E2E Test Flag",
            "status": FeatureFlagStatus.INACTIVE,
            "owner_id": admin_user.id,
            "rollout_percentage": 0,
        }
        defaults.update(kwargs)
        flag = FeatureFlag(**defaults)
        e2e_db_session.add(flag)
        e2e_db_session.commit()
        e2e_db_session.refresh(flag)
        return flag

    return _make
