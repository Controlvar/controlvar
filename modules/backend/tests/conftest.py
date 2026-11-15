"""Test configuration for the modules' suite.

The modules' tests use the core fixtures -- the per-process database,
``db_session``, ``client``, the users -- unchanged.  pytest only applies the
``conftest.py`` files on a test's own directory path, and
``backend/tests/conftest.py`` is a sibling of this tree, not an ancestor, so
those fixtures are imported here and re-exported by name.  Importing the core
conftest as a module also runs its session set-up: ``APP_ENV=test`` and the
app import (which loads the modules' registration through ``modules_loader``).

Only fixtures are re-exported.  The core ``pytest_collection_modifyitems``
hook, which *skips* ``modules``-marked tests in a core build, is not: every
test under ``modules/backend/tests`` needs the modules, and a suite that runs
without the registration must fail loudly, which the hook below does.

Re-exporting a *session*-scoped fixture does not share its cache: pytest builds
one FixtureDef per conftest that defines the name, and caching is per
FixtureDef.  Both trees are in ``testpaths``, so any session that spans them
-- an IDE run, ``pytest -k`` across both, ``pytest backend/tests/unit
modules/backend/tests`` -- crosses from one to the other mid-session and
entered ``test_db`` a second time -- ``pg_terminate_backend()`` on every
connection, ``DROP DATABASE``, ``CREATE DATABASE``, ~50 tables rebuilt --
under the half that was already running.  ``backend/tests/conftest.py`` keeps
the engine in a module global for exactly that reason, so the second entry
hands back the first one's engine; ``unit/test_shared_session_database.py``
is the guard.  The other two session fixtures re-exported here
(``setup_test_environment``, ``configure_all_mappers``) are idempotent: they
set the same environment variables and call ``configure_mappers()`` again.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.modules_loader import load_modules, modules_failure
from backend.tests.conftest import (  # fixture re-exports
    active_experiment,
    client,
    configure_all_mappers,
    db_session,
    mock_api_key,
    mock_auth,
    mock_auth_superuser,
    normal_user,
    setup_test_environment,
    superuser,
    test_db,
    test_experiment,
)
from modules.backend.tests.live_warehouse.selection import ignore_live_directory


def pytest_ignore_collect(collection_path, config):
    """Gate 1 of 2: ignore ``live_warehouse/`` unless ``-m`` selects ``warehouse_live``.

    That directory is the founder-run check against real warehouse accounts.
    When pytest reaches it by walking a directory (``pytest modules/backend/tests``,
    a bare ``pytest``, the directory itself), it is ignored -- not collected, so
    not skipped either -- unless the marker expression selects it.

    pytest does not consult this hook for a file or node id named on the
    command line, so a named live file IS collected.  Gate 2, in
    ``live_warehouse/conftest.py``, then stops the session (exit 4) before any
    credential is read unless ``-m`` selects ``warehouse_live``.  The rule both
    use is in ``live_warehouse/selection.py``;
    ``unit/warehouse/test_live_check_harness.py`` runs both paths.
    """
    return ignore_live_directory(collection_path, config.getoption("markexpr", ""))


@pytest.fixture(autouse=True)
def _own_sessions_on_the_test_database(request, monkeypatch):
    """In a test that has the test database, routes' own sessions use it too.

    Some module routes do their database work in a worker thread, on a
    session they open there from ``deps.SessionLocal`` -- not on the request's
    ``get_db`` session, so a fixture that overrides ``get_db`` does not reach
    them. In every test that asks for ``db_session`` (directly or through
    another fixture), ``deps.SessionLocal`` makes a fresh session on the test
    engine instead: what ``make_client_for_user``'s ``get_db`` override does
    per request. A test that needs its own factory (a small pool, a mock)
    patches ``deps.SessionLocal`` itself; its fixture runs after this one.
    """
    if "db_session" not in request.fixturenames:
        return
    engine = request.getfixturevalue("db_session").get_bind()
    monkeypatch.setattr(
        deps,
        "SessionLocal",
        sessionmaker(bind=engine, autocommit=False, autoflush=False),
    )


def pytest_collection_modifyitems(config, items):
    """Refuse to run the modules' suite without the modules' registration.

    A core build has no ``modules/`` directory and therefore no such suite; a
    tree that *has* one but whose registration failed (or is absent) would
    otherwise fail every test here one at a time.  Exit with the cause
    instead, the same return code the core conftest uses for a broken
    registration.
    """
    if load_modules():
        return
    failure = modules_failure()
    pytest.exit(
        "modules/backend/tests needs the modules' registration, which "
        + (f"failed to load: {failure}" if failure else "did not load"),
        returncode=3,
    )
