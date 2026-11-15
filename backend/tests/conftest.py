# conftest.py
"""
Test configuration for the experimentation platform.

This module sets up fixtures and configuration for pytest.
"""

import logging
import os

import pytest

# ---------------------------------------------------------------------------
# Process environment for the whole test session (set BEFORE the app import)
# ---------------------------------------------------------------------------
# * APP_ENV=test / TESTING=true select TestSettings (ENVIRONMENT == "test")
#   and the test_experimentation schema.  ENVIRONMENT itself is deliberately
#   NOT exported: tests that construct ProdSettings()/DevSettings() directly
#   must keep their class default, and the settings singleton already resolves
#   APP_ENV=test to the canonical "test".
# * Auth runs exactly as the core profile ships: AUTH_PROVIDER=local and
#   the dev-admin bypass OFF (fail-closed).  That is also what the existing
#   suite was written against (the old root conftest forced the Cognito env so
#   unauthenticated requests got 401): tests either override
#   deps.get_current_user / get_current_active_user or assert a 401.  The two
#   exceptions opt in with a module-level fixture — the WebSocket protocol
#   tests enable the bypass, the Cognito dependency-chain tests select
#   AUTH_PROVIDER=cognito — and backend/tests/integration/auth/conftest.py
#   runs its package under the Cognito provider.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("TESTING", "true")
# A developer shell may export ENVIRONMENT=development (see .env.example);
# ENVIRONMENT wins over APP_ENV, so drop it for the test process.
os.environ.pop("ENVIRONMENT", None)
# POSTGRES_SCHEMA likewise wins over APP_ENV in
# core.database_config.get_schema_name(), and the shell CONTRIBUTING.md documents --
# and scripts/run-backend-tests.sh -- exports POSTGRES_SCHEMA=experimentation.
# Set (not `setdefault`) and set HERE, before the first `backend.app` import
# below: the models bind their schema, their string ForeignKey colspecs and
# their index names when they are imported, so a session fixture is far too
# late. Without this a test run would build its tables in the *application*
# schema. The fixture below sets the same value for the record.
os.environ["POSTGRES_SCHEMA"] = "test_experimentation"

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session, configure_mappers, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.api import deps
from backend.app.api.deps import CacheControl
from backend.app.api.read_scope import get_current_reader
from backend.app.core.config import TestSettings, settings
from backend.app.core.database_config import get_schema_name
from backend.app.db.session import Base, get_db, init_db
from backend.app.main import app
from backend.app.models import register_core_models
from backend.app.models.base import set_schema
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User, UserRole
from backend.app.modules_loader import require_modules_or_absent

logger = logging.getLogger(__name__)

# The seam.  With `modules/` present this is the full profile: `load_modules()`
# (run by `backend.app.main` on import, above) registers the modules' routers,
# model modules, audit signer, capabilities and names through
# `modules.register(hooks)` (`modules/backend/app/register.py`).  Nothing from
# the modules is installed by hand here, and a core checkout (no `modules/`)
# runs the same file with every hook at its default;
# `pytest_collection_modifyitems` below then skips the `modules`-marked tests.


def pytest_collection_modifyitems(config, items):
    """Skip `modules`-marked tests when the modules did not load.

    The core build deletes the manifest paths and runs the suite that is
    left; a test outside the manifest that exercises a module's behaviour
    (a module route answering, the signer, split-URL routing) says so with
    the marker instead of failing there.
    """
    from backend.app.modules_loader import load_modules, modules_failure

    if load_modules():
        return
    # Absent is a core build; *broken* is a bug, and skipping the tests that
    # would have caught it (with exit code 0) is the one thing this hook must
    # not do.
    failure = modules_failure()
    if failure is not None:
        pytest.exit(
            "The modules package is present but failed to load; refusing to "
            "skip the modules-marked tests as if this were a core build. "
            f"Cause: {failure}",
            returncode=3,
        )
    skip = pytest.mark.skip(reason="needs the modules package (core build)")
    for item in items:
        if "modules" in item.keywords:
            item.add_marker(skip)


# Pin the auth mode on the singleton (see the header comment) regardless of
# what a developer's shell exports.  Set on the object rather than through
# DEV_AUTH_BYPASS/AUTH_PROVIDER in os.environ so that Settings objects built
# inside individual tests (e.g. ProdSettings()) keep their own defaults and do
# not trip the production bypass guard.
settings.AUTH_PROVIDER = "local"
settings.DEV_AUTH_BYPASS = False

# ---------------------------------------------------------------------------
# Per-process database name
# ---------------------------------------------------------------------------
# Each pytest process gets its own database named
# "experimentation_test_<pid>".  This prevents concurrent test sessions from
# sharing-and trampling-each other's database (each session runs
# DROP DATABASE + CREATE DATABASE in its test_db fixture setup/teardown).
_PID = os.getpid()
_TEST_DB_NAME = f"experimentation_test_{_PID}"
# Honour the same POSTGRES_* variables that the app, CI workflows and
# scripts/run-backend-tests.sh use, so the compose test profile (port 5433)
# and GitHub Actions service containers both work without editing this file.
_DB_USER = os.environ.get("POSTGRES_USER", "postgres")
_DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "postgres")
_DB_HOST = (
    os.environ.get("POSTGRES_SERVER") or os.environ.get("POSTGRES_HOST") or "localhost"
)
_DB_PORT = os.environ.get("POSTGRES_PORT", "5432")
_BASE_DB_URL = f"postgresql://{_DB_USER}:{_DB_PASSWORD}@{_DB_HOST}:{_DB_PORT}"
DEFAULT_TEST_DB_URL = f"{_BASE_DB_URL}/{_TEST_DB_NAME}"


@pytest.fixture(scope="session", autouse=True)
def configure_all_mappers():
    """Configure all mappers before any tests run."""
    configure_mappers()


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Set up the test environment before any tests run."""
    # Set environment variables for testing
    os.environ["APP_ENV"] = "test"
    os.environ["TESTING"] = "true"
    os.environ["POSTGRES_DB"] = _TEST_DB_NAME
    os.environ["POSTGRES_SCHEMA"] = "test_experimentation"
    os.environ["POSTGRES_SERVER"] = _DB_HOST
    os.environ["POSTGRES_PORT"] = _DB_PORT
    os.environ["DATABASE_URI"] = DEFAULT_TEST_DB_URL

    # Initialize test settings
    global settings
    settings = TestSettings()

    # Set schema for all tables
    set_schema()

    yield

    # Clean up
    os.environ.pop("APP_ENV", None)
    os.environ.pop("TESTING", None)
    os.environ.pop("POSTGRES_DB", None)
    os.environ.pop("POSTGRES_SCHEMA", None)
    os.environ.pop("POSTGRES_SERVER", None)
    os.environ.pop("DATABASE_URI", None)


#: The engine this process built, and how many times it built one.  Session
#: scope is per *FixtureDef*, and pytest builds one FixtureDef per conftest
#: that defines (or re-exports) the fixture: `modules/backend/tests/conftest.py`
#: re-exports this function, so a session that spans both trees -- a bare
#: `pytest` with `testpaths = ["backend/tests", "modules/backend/tests"]`, an
#: IDE run, `pytest -k` across both -- entered this body a second time and
#: pg_terminate_backend()'d every connection, DROPped the database and rebuilt
#: ~50 tables under the half that was already running.  The module global is
#: shared (there is one function object, whatever pytest wraps it in), so the
#: second entry hands back the first engine and the database is built once per
#: process however many FixtureDefs point here.
_SESSION_ENGINE = None
_SESSION_DB_BUILDS = 0


@pytest.fixture(scope="session")
def test_db():
    """Create a per-process test database and schema.

    Using a PID-unique database name (experimentation_test_<pid>) means
    concurrent pytest processes never share or drop each other's database.

    Built once per process (see `_SESSION_ENGINE`): a second entry -- the
    modules' conftest re-exports this fixture, which gives it its own
    FixtureDef and therefore its own session cache -- yields the engine the
    first one built and tears nothing down.

    The production engine (session.py) uses a QueuePool(pool_size=20) that
    is created at module-import time.  We dispose it here so that its idle
    connections are closed before pg_terminate_backend() runs in the setup
    block.  Without disposal, those pool connections are killed by
    pg_terminate_backend() and then the pool tries to roll them back on
    return, generating 'server closed the connection unexpectedly' tracebacks
    that cascade through subsequent NullPool checkouts.
    """
    global _SESSION_ENGINE, _SESSION_DB_BUILDS
    if _SESSION_ENGINE is not None:
        # Already built by another FixtureDef in this session.  Yield it and
        # drop nothing: the FixtureDef that built it owns the teardown, and
        # it finalises last (pytest finalises in reverse order of setup).
        yield _SESSION_ENGINE
        return

    # Dispose the production engine pool to prevent interference.
    try:
        from backend.app.db.session import engine as _prod_engine

        _prod_engine.dispose()
        logger.info("Disposed production engine pool before test_db setup")
    except Exception as _e:
        logger.warning("Could not dispose production engine: %s", _e)

    db_url = DEFAULT_TEST_DB_URL
    max_retries = 3
    retry_count = 0

    while retry_count < max_retries:
        try:
            # Use a raw psycopg2 connection with autocommit=True for all
            # database-level DDL (DROP DATABASE / CREATE DATABASE).
            # These statements cannot run inside a transaction block in
            # PostgreSQL, so we must use autocommit mode.
            from urllib.parse import urlparse

            import psycopg2

            parsed = urlparse(db_url)
            admin_conn = psycopg2.connect(
                host=parsed.hostname,
                port=parsed.port or 5432,
                user=parsed.username,
                password=parsed.password,
                dbname="postgres",
            )
            admin_conn.autocommit = True
            admin_cur = admin_conn.cursor()

            # Kill any existing connections to this process's test database
            # so that DROP DATABASE does not fail with "other users are
            # connected".  This is a no-op when the DB does not exist yet.
            admin_cur.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s
                  AND pid <> pg_backend_pid()
                """,
                (_TEST_DB_NAME,),
            )

            # Drop and recreate the test database.
            admin_cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB_NAME}")
            admin_cur.execute(f"CREATE DATABASE {_TEST_DB_NAME}")

            admin_cur.close()
            admin_conn.close()

            # Create engine for test database.
            # NullPool ensures each db_session.connect() gets a brand-new
            # DBAPI connection that is closed (never returned to a pool)
            # when session.close() is called.
            engine = create_engine(db_url, poolclass=NullPool)

            # Register a session-scoped "connect" event so that every new
            # DBAPI connection automatically has search_path set to the test
            # schema.  This fires once per connection checkout (every
            # statement with NullPool).
            @sa_event.listens_for(engine, "connect")
            def set_schema_search_path(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("SET search_path TO test_experimentation")
                cursor.close()

            # Test the connection to the new database
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))

            # Initialize database with schema and tables
            os.environ["DATABASE_URI"] = db_url
            os.environ["APP_ENV"] = "test"
            os.environ["TESTING"] = "true"

            schema_name = "test_experimentation"

            # Create schema and tables
            with engine.connect() as conn:
                conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
                conn.execute(text(f"CREATE SCHEMA {schema_name}"))
                conn.execute(text(f"SET search_path TO {schema_name}"))
                conn.execute(text("COMMIT"))

                # Register every model on the metadata before create_all runs.
                # Missing models cause create_all to fail, which triggers the
                # retry loop.
                register_core_models()
                # The modules' model modules arrive through the seam -- the
                # same seven `hooks.register_model_module()` calls bootstrap
                # and alembic see -- so the test schema and a real one agree.
                # Strict, like them: a broken registration must fail the
                # session, not quietly build a 37-table schema.
                require_modules_or_absent()

                # Set schema for all tables
                Base.metadata.schema = schema_name

                # Make sure all tables use the correct schema
                for table in Base.metadata.tables.values():
                    table.schema = schema_name

                # Create tables
                Base.metadata.create_all(bind=engine)

            # If we get here, the database is ready
            break

        except Exception as e:
            retry_count += 1
            logger.error(f"Attempt {retry_count} failed to set up test database: {e}")
            if retry_count == max_retries:
                pytest.fail(
                    f"Failed to set up test database after {max_retries} attempts: {e}"
                )
            import time

            time.sleep(2)  # Wait before retrying

    _SESSION_ENGINE = engine
    _SESSION_DB_BUILDS += 1

    yield engine

    _SESSION_ENGINE = None

    # Teardown: drop the per-process test database.
    try:
        from urllib.parse import urlparse

        import psycopg2

        parsed = urlparse(db_url)
        cleanup_conn = psycopg2.connect(
            host=parsed.hostname,
            port=parsed.port or 5432,
            user=parsed.username,
            password=parsed.password,
            dbname="postgres",
        )
        cleanup_conn.autocommit = True
        cleanup_cur = cleanup_conn.cursor()

        cleanup_cur.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s
              AND pid <> pg_backend_pid()
            """,
            (_TEST_DB_NAME,),
        )
        cleanup_cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB_NAME}")
        cleanup_cur.close()
        cleanup_conn.close()

    except Exception as e:
        logger.error(f"Error during test database cleanup: {e}")
        # Don't fail the tests if cleanup fails


@pytest.fixture(scope="function")
def db_session(test_db):
    """Create a fresh database session for a test.

    The session is bound directly to the NullPool engine so that after each
    commit() the session acquires a fresh DBAPI connection on its next DML.

    expire_on_commit=False prevents SQLAlchemy from expiring all loaded
    objects after a commit().  Without this, attributes like
    user.is_superuser trigger a reload SELECT on the next access, which
    opens a new NullPool connection that may fail if the prior commit
    already closed the underlying DBAPI connection.

    We do NOT truncate tables in teardown.  TRUNCATE TABLE ... CASCADE holds
    AccessExclusiveLock on every table in the cascade set.  When pytest
    runs the teardown of one function-scoped fixture while setting up the
    next (which can happen when asyncio test fixtures overlap in the event
    loop), the TRUNCATE and the new test's INSERT deadlock against each
    other.  Since all test data uses UUID primary keys and unique per-test
    suffixes, there is no data contamination between tests.  The entire
    database is dropped and recreated at the start of every pytest session
    (in test_db), so accumulated rows are discarded after each full run.
    """
    Session = sessionmaker(
        bind=test_db,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,  # prevent reload SELECTs after commit()
    )
    session = Session()

    # Set schema search path for the initial connection.
    # The engine-level "connect" event (registered in test_db) re-applies
    # search_path on every new NullPool connection checkout automatically.
    session.execute(text("SET search_path TO test_experimentation"))

    try:
        yield session
    finally:
        # Rollback any uncommitted work left by the test.
        try:
            session.rollback()
        except Exception:
            pass

        try:
            session.close()
        except Exception:
            pass


@pytest.fixture
def client(db_session, monkeypatch):
    """Create a test client for the FastAPI application."""

    # Override the get_db dependency
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    # Mock Cognito auth service
    def mock_get_user_with_groups(*args, **kwargs):
        return {
            "username": "test_user",
            "attributes": {"sub": "sub-testuser", "email": "test@example.com"},
            "groups": ["admin-group"],
        }

    monkeypatch.setattr(
        "backend.app.services.auth_service.CognitoAuthService.get_user_with_groups",
        mock_get_user_with_groups,
    )

    # Mock security token decoder
    def mock_decode_token(*args, **kwargs):
        return {
            "sub": "test_user_id",
            "username": "test_user",
            "email": "test@example.com",
        }

    monkeypatch.setattr("backend.app.core.security.decode_token", mock_decode_token)

    # Get or create a superuser for authentication
    user = db_session.query(User).filter(User.email == "test@example.com").first()
    if not user:
        user = User(
            username="test_user",
            email="test@example.com",
            full_name="Test User",
            hashed_password="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",
            is_active=True,
            is_superuser=True,
            role=UserRole.ADMIN,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

    # Create both sync and async return functions for user
    async def override_get_current_user():
        """Override get_current_user to return a test user."""
        return user

    def override_get_current_active_user():
        """Override get_current_active_user to return a test user."""
        return user

    def override_get_current_superuser():
        """Override get_current_superuser to return a superuser."""
        return user

    async def override_get_cache_control():
        """Override cache control to disable caching in tests."""
        return CacheControl(enabled=False, skip=True)

    def override_get_api_key():
        """Override API key authentication to return a test user."""
        return user

    # Set up dependency overrides
    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[deps.get_current_user] = override_get_current_user
    app.dependency_overrides[deps.get_current_active_user] = (
        override_get_current_active_user
    )
    # The four read routes that also take an mcp:read key depend on this one,
    # which an override of get_current_active_user does not reach.
    app.dependency_overrides[get_current_reader] = override_get_current_active_user
    app.dependency_overrides[deps.get_current_superuser] = (
        override_get_current_superuser
    )
    # Reaches only routes that inject get_cache_control via Depends; a route that
    # calls deps.get_cache_control() directly (the flag list, #427) is not.
    app.dependency_overrides[deps.get_cache_control] = override_get_cache_control
    app.dependency_overrides[deps.get_api_key] = override_get_api_key

    # Create test client
    test_client = TestClient(app)

    yield test_client

    # Clean up
    app.dependency_overrides.clear()


@pytest.fixture
def normal_user(db_session):
    """Create a normal user for testing."""
    user = db_session.query(User).filter(User.email == "testuser@example.com").first()
    if not user:
        user = User(
            username="testuser",
            email="testuser@example.com",
            full_name="Test User",
            hashed_password="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",
            is_active=True,
            is_superuser=False,
            role=UserRole.DEVELOPER,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    return user


@pytest.fixture
def superuser(db_session):
    """Create a superuser for testing."""
    user = db_session.query(User).filter(User.email == "admin@example.com").first()
    if not user:
        user = User(
            username="admin",
            email="admin@example.com",
            full_name="Admin User",
            hashed_password="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",
            is_active=True,
            is_superuser=True,
            role=UserRole.ADMIN,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    return user


@pytest.fixture
def mock_auth(client, normal_user, monkeypatch):
    """The ``client`` test client, signed in as ``normal_user`` (a DEVELOPER).

    It requests ``client`` and yields it, so a test takes this one fixture
    rather than ``client`` and ``mock_auth`` together: both write the
    app-global auth override, and with two the user depended on which pytest
    set up last (#476, ``backend/tests/smoke/test_role_client_fixtures.py``).
    """

    async def override_get_current_user():
        return normal_user

    def override_get_current_active_user():
        return normal_user

    # Mock Cognito auth service
    def mock_get_user_with_groups(*args, **kwargs):
        return {
            "username": normal_user.username,
            "attributes": {"sub": f"sub-{normal_user.id}", "email": normal_user.email},
            "groups": ["developer-group"],
        }

    monkeypatch.setattr(
        "backend.app.services.auth_service.CognitoAuthService.get_user_with_groups",
        mock_get_user_with_groups,
    )

    # Mock security token decoder
    def mock_decode_token(*args, **kwargs):
        return {
            "sub": str(normal_user.id),
            "username": normal_user.username,
            "email": normal_user.email,
        }

    monkeypatch.setattr("backend.app.core.security.decode_token", mock_decode_token)

    app.dependency_overrides[deps.get_current_user] = override_get_current_user
    app.dependency_overrides[deps.get_current_active_user] = (
        override_get_current_active_user
    )
    app.dependency_overrides[get_current_reader] = override_get_current_active_user

    yield client

    app.dependency_overrides.pop(deps.get_current_user, None)
    app.dependency_overrides.pop(deps.get_current_active_user, None)
    app.dependency_overrides.pop(get_current_reader, None)


@pytest.fixture
def mock_auth_superuser(client, superuser, monkeypatch):
    """The ``client`` test client, signed in as ``superuser``; see mock_auth."""

    async def override_get_current_user():
        return superuser

    def override_get_current_active_user():
        return superuser

    def override_get_current_superuser():
        return superuser

    # Mock Cognito auth service
    def mock_get_user_with_groups(*args, **kwargs):
        return {
            "username": superuser.username,
            "attributes": {"sub": f"sub-{superuser.id}", "email": superuser.email},
            "groups": ["admin-group"],
        }

    monkeypatch.setattr(
        "backend.app.services.auth_service.CognitoAuthService.get_user_with_groups",
        mock_get_user_with_groups,
    )

    # Mock security token decoder
    def mock_decode_token(*args, **kwargs):
        return {
            "sub": str(superuser.id),
            "username": superuser.username,
            "email": superuser.email,
        }

    monkeypatch.setattr("backend.app.core.security.decode_token", mock_decode_token)

    # Set up dependency overrides
    app.dependency_overrides[deps.get_current_user] = override_get_current_user
    app.dependency_overrides[deps.get_current_active_user] = (
        override_get_current_active_user
    )
    app.dependency_overrides[get_current_reader] = override_get_current_active_user
    app.dependency_overrides[deps.get_current_superuser] = (
        override_get_current_superuser
    )

    yield client

    # Clean up dependency overrides
    app.dependency_overrides.pop(deps.get_current_user, None)
    app.dependency_overrides.pop(deps.get_current_active_user, None)
    app.dependency_overrides.pop(get_current_reader, None)
    app.dependency_overrides.pop(deps.get_current_superuser, None)


@pytest.fixture
def test_experiment(db_session, normal_user):
    """Create a test experiment for testing."""
    experiment = Experiment(
        name="Test Experiment",
        description="A test experiment",
        hypothesis="Test hypothesis",
        owner_id=normal_user.id,
        status=ExperimentStatus.DRAFT.value,
    )
    db_session.add(experiment)
    db_session.commit()
    db_session.refresh(experiment)
    return experiment


@pytest.fixture
def active_experiment(db_session, normal_user):
    """Create an active test experiment for testing."""
    experiment = Experiment(
        name="Active Experiment",
        description="An active test experiment",
        experiment_type="a_b",
        status=ExperimentStatus.ACTIVE,
        owner_id=normal_user.id,
    )
    db_session.add(experiment)
    db_session.commit()
    db_session.refresh(experiment)
    return experiment


@pytest.fixture
def mock_api_key(monkeypatch):
    """Mock the API key dependency."""
    user = User(
        username="api_user",
        email="api@example.com",
        full_name="API User",
        hashed_password="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )

    def override_get_api_key(**kwargs):
        return user

    # Set up dependency override
    app.dependency_overrides[deps.get_api_key] = override_get_api_key

    yield

    # Clean up dependency override
    app.dependency_overrides.pop(deps.get_api_key, None)
