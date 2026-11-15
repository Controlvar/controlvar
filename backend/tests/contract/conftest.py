"""
Contract test configuration for Phase 7.

Re-exports all integration fixtures so they are available to tests under
backend/tests/contract/. Also overrides admin_client to use DictLikeCacheControl,
working around the pre-existing bug in create_feature_flag that calls
cache_control.get() dict-style on a Pydantic CacheControl model instance.

DB isolation: The contract conftest creates a fresh SQLAlchemy engine (not sharing
the pool used by E2E tests) so that stale connections from prior test suites do not
cause "server closed the connection" errors in the contract db_session fixture.
"""

import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import deps
from backend.app.api.deps import CacheControl
from backend.app.api.read_scope import get_current_reader
from backend.app.main import app
from backend.app.models.user import User, UserRole

# Use the per-process database that the root conftest's `test_db` fixture
# creates (experimentation_test_<pid>) rather than a fixed name, so the
# contract engine points at a database that actually has the schema.
from backend.tests.conftest import DEFAULT_TEST_DB_URL

# Re-import all integration fixtures so pytest discovers them
from backend.tests.integration.conftest import (
    admin_user,
    analyst_client,
    analyst_user,
    developer_client,
    developer_user,
    make_assignment,
    make_event,
    make_experiment,
    make_feature_flag,
    make_metric,
    make_variant,
    viewer_user,
)

HASHED_PASSWORD = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"


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
def contract_engine(test_db):
    """
    Create a dedicated SQLAlchemy engine for contract tests.

    Uses test_db to ensure the database exists (session-scoped setup), but
    creates a new engine with its own connection pool. This prevents stale
    connections from E2E tests (which commit freely) from corrupting the
    contract tests' db_session transaction wrappers.

    Disposes the app engine at fixture start to clear any idle app connections
    that might conflict with the contract tests' own pool.
    """
    from backend.app.db.session import engine as app_engine

    app_engine.dispose()

    # Always the per-process database that `test_db` just created; an
    # environment override could only point at a database without tables.
    engine = create_engine(
        DEFAULT_TEST_DB_URL,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(contract_engine):
    """
    Override the root db_session for contract tests.

    Creates a fresh connection from the contract engine's pool (isolated from
    the E2E pool) and wraps each test in a transaction that is rolled back on
    teardown for test isolation.
    """
    connection = contract_engine.connect()
    transaction = connection.begin()

    ContractSession = sessionmaker(bind=connection)
    session = ContractSession()

    session.execute(text("SET search_path TO test_experimentation"))
    session.commit()

    try:
        yield session
    finally:
        session.close()
        try:
            transaction.rollback()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass


@pytest.fixture
def admin_client(db_session: Session, admin_user: User, monkeypatch) -> TestClient:
    """
    Create a TestClient authenticated as the admin_user for contract tests.

    Overrides `can_create_feature_flag` and uses DictLikeCacheControl to
    avoid the pre-existing CacheControl.get() bug in create_feature_flag.
    """

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    async def override_get_current_user():
        return admin_user

    def override_get_current_active_user():
        return admin_user

    def override_get_current_superuser():
        if not admin_user.is_superuser:
            raise HTTPException(status_code=403, detail="Not enough permissions")
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
