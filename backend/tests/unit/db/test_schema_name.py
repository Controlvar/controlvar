"""There is one answer to "which schema?", and everything asks for it.

Review round 3, finding 1.  There used to be two:

* ``core.database_config.get_schema_name()`` -- read by every model for its
  ``__table_args__['schema']``, its string ``ForeignKey`` colspecs and the
  index names it spells -- looked only at ``APP_ENV``/``TESTING``;
* ``os.environ.get("POSTGRES_SCHEMA", "experimentation")`` -- read by alembic's
  ``env.py`` for what to reflect and by five revision modules for the names
  they build, each with its own copy of the literal default.

With ``export APP_ENV=test TESTING=true`` and ``POSTGRES_SCHEMA`` unset -- the
first thing ``CONTRIBUTING.md`` tells a developer to type -- the first said
``test_experimentation`` and the second said ``experimentation``.  ``alembic
revision --autogenerate`` compared a metadata describing one against a
reflection of the other and wrote ``op.create_table`` for all ~50 tables plus
``op.drop_table`` for all ~50 of the others: ``users``, ``experiments``,
``feature_flags``.  ``alembic upgrade heads`` in that shell migrated the
application schema while the models pointed at the test one.

No database here: the point is purely which string each caller resolves.
``backend/tests/integration/database/test_autogenerate_is_empty.py`` proves the
consequence against a real one.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table

from backend.app.core.database_config import get_schema_name
from backend.app.db import bootstrap
from backend.app.db.schema import (
    export_schema_name,
    metadata_for_schema,
    point_metadata_at,
    resolve_schema_name,
)

pytestmark = pytest.mark.unit

#: ``(APP_ENV, TESTING, POSTGRES_SCHEMA)`` -> the schema every caller must pick.
#: The last three rows are the ones that used to disagree.
ENVIRONMENTS = [
    ((None, None, None), "experimentation"),
    (("prod", None, None), "experimentation"),
    ((None, None, "experimentation"), "experimentation"),
    (("test", "true", None), "test_experimentation"),
    ((None, "true", None), "test_experimentation"),
    (("test", None, None), "test_experimentation"),
    # POSTGRES_SCHEMA names the schema: a deployment, the ECS migration task,
    # scripts/core_build.sh's scratch schema, the integration tests' own.
    (("test", "true", "experimentation"), "experimentation"),
    (("test", "true", "profile_1a2b3c4d"), "profile_1a2b3c4d"),
    ((None, None, "profile_1a2b3c4d"), "profile_1a2b3c4d"),
]

IDS = [
    f"APP_ENV={a!r},TESTING={t!r},POSTGRES_SCHEMA={s!r}"
    for (a, t, s), _ in ENVIRONMENTS
]


@pytest.fixture
def process_env(monkeypatch):
    def apply(app_env, testing, postgres_schema):
        for name, value in (
            ("APP_ENV", app_env),
            ("TESTING", testing),
            ("POSTGRES_SCHEMA", postgres_schema),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    return apply


# ---------------------------------------------------------------------------
# One resolution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("env,expected", ENVIRONMENTS, ids=IDS)
def test_the_schema_is_resolved_the_same_way_everywhere(env, expected, process_env):
    process_env(*env)
    assert get_schema_name() == expected


@pytest.mark.regression
@pytest.mark.parametrize("env,_expected", ENVIRONMENTS, ids=IDS)
def test_the_models_the_bootstrap_and_alembic_cannot_diverge(
    env, _expected, process_env
):
    """The gate.  These three are what the generated revision compares.

    ``get_schema_name()`` is what the models declare, ``bootstrap.schema_name()``
    is the schema ``python -m backend.app.db.bootstrap`` builds, and
    ``resolve_schema_name()`` is what alembic's ``env.py`` reflects and writes
    into ``POSTGRES_SCHEMA`` for the revision modules.  If a future change gives
    any one of them a rule of its own, this fails before it can generate a
    revision that drops the application schema.
    """
    process_env(*env)
    assert resolve_schema_name() == get_schema_name()
    assert bootstrap.schema_name() == get_schema_name()


# ---------------------------------------------------------------------------
# Publishing the answer for the revision modules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("env,expected", ENVIRONMENTS, ids=IDS)
def test_export_publishes_the_resolved_schema(env, expected, process_env):
    """Revision modules read ``POSTGRES_SCHEMA`` when alembic imports them.

    ``a7b8c9d0e1f2``, ``ep057_workspaces``, ``ep046_llm_experiments``,
    ``modules_0001_rbac``, ``modules_0002_warehouse_analysis`` and three more do
    ``os.environ.get("POSTGRES_SCHEMA", "experimentation")`` at module level, so
    the variable has to carry the resolved answer before a script directory is
    walked -- their defaults must never be reachable.
    """
    process_env(*env)

    assert export_schema_name() == expected
    assert os.environ["POSTGRES_SCHEMA"] == expected
    # What a revision module would then read, default and all.
    assert os.environ.get("POSTGRES_SCHEMA", "experimentation") == expected


def test_export_accepts_an_explicit_schema(process_env):
    process_env("test", "true", None)
    assert export_schema_name("somewhere_else") == "somewhere_else"
    assert os.environ["POSTGRES_SCHEMA"] == "somewhere_else"


# ---------------------------------------------------------------------------
# Aiming an already-imported metadata at a schema chosen later
# ---------------------------------------------------------------------------
def _toy_metadata(schema: str):
    metadata = MetaData(schema=schema)
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table(
        "things",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, ForeignKey(f"{schema}.users.id"), index=True),
    )
    return SimpleNamespace(metadata=metadata)


def test_metadata_for_schema_is_the_metadata_itself_when_it_already_agrees():
    base = _toy_metadata("old_schema")
    assert metadata_for_schema(base, "old_schema") is base.metadata


@pytest.mark.regression
def test_metadata_for_schema_moves_the_keys_the_names_and_the_foreign_keys():
    """A shallow ``table.schema = x`` leaves three things behind, and all three bite.

    The table *keys* stay ``"<old>.<table>"``, which is what alembic indexes a
    metadata by -- autogenerate raised ``KeyError: 'experimentation.
    audit_events_v2'`` mid-comparison.  The auto-generated index names stay
    derived from the old schema, so a bootstrap built schema
    ``experimentation`` full of ``ix_test_experimentation_*`` indexes and the
    next autogenerate proposed dropping and re-creating all 67.  And the string
    ``ForeignKey`` colspecs still name the old schema.
    """
    base = _toy_metadata("old_schema")
    translated = metadata_for_schema(base, "new_schema")

    assert sorted(translated.tables) == ["new_schema.things", "new_schema.users"]
    assert translated.tables["new_schema.things"].schema == "new_schema"
    names = sorted(i.name for t in translated.tables.values() for i in t.indexes)
    assert names == ["ix_new_schema_things_user_id"]
    foreign_key = next(iter(translated.tables["new_schema.things"].foreign_keys))
    assert foreign_key.target_fullname == "new_schema.users.id"

    # A copy: nothing else in the process sees a metadata it did not ask for.
    assert base.metadata.schema == "old_schema"
    assert sorted(base.metadata.tables) == ["old_schema.things", "old_schema.users"]


def test_point_metadata_at_moves_the_tables_in_place():
    """The ORM path (``bootstrap.ensure_first_superuser``) needs the real tables.

    A mapper is bound to the ``Table`` object its model declared, so a
    translated copy is invisible to ``session.query(User)``; the table objects
    themselves have to move.  Deliberately shallow -- see the docstring in
    ``backend/app/db/schema.py`` for why that is both correct here and wrong
    for DDL.
    """
    base = _toy_metadata("old_schema")

    point_metadata_at(base, "new_schema")

    assert base.metadata.schema == "new_schema"
    assert {t.schema for t in base.metadata.tables.values()} == {"new_schema"}
