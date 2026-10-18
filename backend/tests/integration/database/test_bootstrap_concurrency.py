"""
Two API replicas starting at once both run ``python -m backend.app.db.bootstrap``
(``backend/docker-entrypoint.sh``).  The bootstrap serialises itself on a
PostgreSQL advisory lock, so on a fresh database exactly one process creates
the schema and the first administrator and the other finds the work done.

Runs the real module in two subprocesses against a scratch schema of the
per-process test database (``backend/tests/conftest.py`` exports its name as
``POSTGRES_DB``, which is what the bootstrap reads).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)


def _run_bootstrap(schema: str, **overrides: str) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("APP_ENV", "TESTING", "ENVIRONMENT")
    }
    env.update(
        {
            "PYTHONPATH": REPO_ROOT,
            "POSTGRES_SCHEMA": schema,
            "FIRST_SUPERUSER": "race@example.com",
            "FIRST_SUPERUSER_PASSWORD": "Race-Passw0rd",
        }
    )
    env.update(overrides)
    return subprocess.run(
        [sys.executable, "-m", "backend.app.db.bootstrap"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.integration
def test_concurrent_bootstraps_on_a_fresh_schema_do_not_race(test_db):
    engine = test_db
    schema = f"boot_race_{uuid.uuid4().hex[:8]}"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_run_bootstrap, [schema, schema]))

        for proc in results:
            assert proc.returncode == 0, proc.stderr[-2000:]
        outcomes = sorted(
            "created" if "(created)" in p.stdout else "upgraded" for p in results
        )
        assert outcomes == ["created", "upgraded"], [p.stdout for p in results]

        with engine.connect() as conn:
            users = (
                conn.execute(text(f'SELECT email FROM "{schema}".users'))
                .scalars()
                .all()
            )
            assert users == ["race@example.com"]
            # Exactly one row per head, and the second replica must not have
            # added a duplicate: two rows in a full checkout (the core chain
            # and the `modules` branch), one in a core checkout.  The count
            # used to be hard-coded to 1, which was the bug in finding 1 --
            # `stamp heads` recorded only the modules head -- written down as
            # an assertion.
            from alembic.script import ScriptDirectory

            from backend.app.db.bootstrap import alembic_config

            heads = set(
                ScriptDirectory.from_config(alembic_config()).revision_map.heads
            )
            recorded = list(
                conn.execute(
                    text(f'SELECT version_num FROM "{schema}".alembic_version')
                ).scalars()
            )
            assert sorted(recorded) == sorted(heads)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.integration
@pytest.mark.regression
def test_the_bootstrap_says_whether_it_created_the_first_administrator(test_db):
    """#237: the first start never said it had created an administrator, and a
    later start never said FIRST_SUPERUSER* were being ignored -- so a changed
    FIRST_SUPERUSER_PASSWORD silently did nothing."""
    engine = test_db
    schema = f"boot_admin_{uuid.uuid4().hex[:8]}"
    try:
        first = _run_bootstrap(schema)
        assert first.returncode == 0, first.stderr[-2000:]
        assert (
            "created the first administrator race@example.com (role ADMIN)"
            in first.stderr
        ), first.stderr[-2000:]
        assert "FIRST_SUPERUSER settings ignored" not in first.stderr

        second = _run_bootstrap(schema)
        assert second.returncode == 0, second.stderr[-2000:]
        assert "users exist; FIRST_SUPERUSER settings ignored" in second.stderr, (
            second.stderr[-2000:]
        )
        assert "created the first administrator" not in second.stderr
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _users(engine, schema: str) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(text(f'SELECT email FROM "{schema}".users')).scalars())


@pytest.mark.integration
@pytest.mark.regression
def test_a_default_password_creates_no_administrator_when_the_environment_is_unset(
    test_db,
):
    """ENVIRONMENT unset used to mean development, and the bootstrap created a
    superuser with the class-default password. Now it refuses, exits 1 with one
    line, and the users table stays empty; the schema itself is created."""
    engine = test_db
    schema = f"boot_weak_{uuid.uuid4().hex[:8]}"
    try:
        refused = _run_bootstrap(schema, FIRST_SUPERUSER_PASSWORD="admin")
        assert refused.returncode == 1, refused.stderr[-2000:]
        assert "FIRST_SUPERUSER_PASSWORD is a well-known default" in refused.stderr, (
            refused.stderr[-2000:]
        )
        assert "Traceback" not in refused.stderr, refused.stderr[-2000:]
        assert _users(engine, schema) == []

        # The same password with ENVIRONMENT chosen is a local development
        # database, and the administrator is created on the re-run.
        chosen = _run_bootstrap(
            schema, FIRST_SUPERUSER_PASSWORD="admin", ENVIRONMENT="development"
        )
        assert chosen.returncode == 0, chosen.stderr[-2000:]
        assert _users(engine, schema) == ["race@example.com"]
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


# bcrypt hashes at most 72 bytes and raises ValueError on a longer password,
# so a longer FIRST_SUPERUSER_PASSWORD used to stop the first start with a
# traceback. 36 x "é" is 72 bytes in 36 characters; 37 of them is 74 bytes.
_AT_THE_LIMIT = "Aa1-" + "x" * 68
_OVER_THE_LIMIT = "Aa1-" + "x" * 69
_ACCENTED_AT_THE_LIMIT = "é" * 36
_ACCENTED_OVER_THE_LIMIT = "é" * 37
_TOO_LONG = "must be at most 72 bytes when encoded as UTF-8"


def _hashed_password(engine, schema: str) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text(f'SELECT hashed_password FROM "{schema}".users')
        ).scalar_one()


@pytest.mark.integration
@pytest.mark.regression
@pytest.mark.parametrize(
    "environment", [None, "development"], ids=["unset", "development"]
)
@pytest.mark.parametrize(
    "password",
    [_OVER_THE_LIMIT, _ACCENTED_OVER_THE_LIMIT],
    ids=["ascii-73-bytes", "accented-74-bytes"],
)
def test_a_first_administrator_password_over_72_bytes_is_refused_in_one_line(
    test_db, password, environment
):
    """An empty users table and a password over 72 bytes of UTF-8: exit 1 with
    one line naming the limit, no traceback, no user, and the value is not
    repeated. Refused whatever ENVIRONMENT is, since it cannot be hashed."""
    assert len(password.encode("utf-8")) > 72
    engine = test_db
    schema = f"boot_long_{uuid.uuid4().hex[:8]}"
    overrides = {"FIRST_SUPERUSER_PASSWORD": password}
    if environment:
        overrides["ENVIRONMENT"] = environment
    try:
        refused = _run_bootstrap(schema, **overrides)
        assert "Traceback" not in refused.stderr, refused.stderr[-2000:]
        assert refused.returncode == 1, refused.stderr[-2000:]
        assert _TOO_LONG in refused.stderr, refused.stderr[-2000:]
        assert password not in refused.stderr + refused.stdout
        assert _users(engine, schema) == []
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.integration
@pytest.mark.regression
def test_a_first_administrator_password_with_no_utf8_encoding_is_refused(test_db):
    """A byte that is not UTF-8 reaches Python as a lone surrogate, which has no
    UTF-8 encoding; refused with fixed text rather than a traceback."""
    engine = test_db
    schema = f"boot_enc_{uuid.uuid4().hex[:8]}"
    try:
        refused = _run_bootstrap(
            schema, FIRST_SUPERUSER_PASSWORD="Race-Passw0rd-\udcff"
        )
        assert "Traceback" not in refused.stderr, refused.stderr[-2000:]
        assert refused.returncode == 1, refused.stderr[-2000:]
        assert "FIRST_SUPERUSER_PASSWORD cannot be encoded as UTF-8" in (
            refused.stderr
        ), refused.stderr[-2000:]
        assert "Race-Passw0rd" not in refused.stderr + refused.stdout
        assert _users(engine, schema) == []
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.integration
@pytest.mark.regression
@pytest.mark.parametrize(
    "password",
    [_AT_THE_LIMIT, _ACCENTED_AT_THE_LIMIT],
    ids=["ascii-72-bytes", "accented-72-bytes"],
)
def test_a_first_administrator_password_of_exactly_72_bytes_is_accepted(
    test_db, password
):
    import bcrypt

    assert len(password.encode("utf-8")) == 72
    engine = test_db
    schema = f"boot_72_{uuid.uuid4().hex[:8]}"
    try:
        created = _run_bootstrap(
            schema, FIRST_SUPERUSER_PASSWORD=password, ENVIRONMENT="development"
        )
        assert created.returncode == 0, created.stderr[-2000:]
        assert _users(engine, schema) == ["race@example.com"]
        assert bcrypt.checkpw(
            password.encode("utf-8"),
            _hashed_password(engine, schema).encode("utf-8"),
        )
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.integration
@pytest.mark.regression
def test_a_long_password_setting_does_not_stop_a_database_that_has_users(test_db):
    """The limit applies only while users is empty. A deployment already
    running with a long value in FIRST_SUPERUSER_PASSWORD keeps starting, and
    the existing administrator is left alone."""
    engine = test_db
    schema = f"boot_kept_{uuid.uuid4().hex[:8]}"
    try:
        first = _run_bootstrap(schema)
        assert first.returncode == 0, first.stderr[-2000:]
        before = _hashed_password(engine, schema)

        # As production: the settings' own staging/production rules apply, so
        # this also fails if the limit is ever moved onto the setting.
        later = _run_bootstrap(
            schema,
            FIRST_SUPERUSER_PASSWORD=_OVER_THE_LIMIT,
            ENVIRONMENT="production",
            SECRET_KEY="bootstrap-test-" + "k" * 40,
            AUDIT_HMAC_KEY="bootstrap-test-" + "a" * 40,
            PUBLIC_BASE_URL="https://controlvar.example.com",
        )
        assert later.returncode == 0, later.stderr[-2000:]
        assert "users exist; FIRST_SUPERUSER settings ignored" in later.stderr
        assert _TOO_LONG not in later.stderr
        assert _users(engine, schema) == ["race@example.com"]
        assert _hashed_password(engine, schema) == before
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
