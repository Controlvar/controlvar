"""``alembic revision --autogenerate`` against a fresh database proposes nothing.

One property, and it is the one that catches this whole class of bug before it
is pushed: **bootstrap a database, ask alembic what has changed, and the answer
must be "nothing"**.  Every operation alembic proposes there is either a
migration somebody forgot to write or -- three times now -- a comparison
pointed at the wrong thing, and the second kind is destructive:

* *review round 3, finding 1.*  ``include_schemas=True`` made autogenerate
  reflect the application schema, which ``env.py`` named from
  ``POSTGRES_SCHEMA`` (default ``"experimentation"``) while ``target_metadata``
  named it from ``APP_ENV`` (``test_experimentation`` under the export sequence
  ``CONTRIBUTING.md`` documents).  The generated revision created all ~50 tables in
  one schema and ran ``op.drop_table`` on all ~50 in the other -- ``users``,
  ``experiments``, ``feature_flags``, everything.
* *review round 3, finding 2.*  Nothing filtered the twelve module tables, nine
  of which core-chain revisions create, so a **core** checkout proposed twelve
  ``op.drop_table`` calls against a database that has them.  Applied to a
  full-profile database that is every workspace, SSO configuration, BAA and PHI
  audit row.
* the schema translation itself.  The bootstrap used to move the models onto
  ``POSTGRES_SCHEMA`` by assigning ``table.schema``, which leaves each table's
  key and every auto-generated index name derived from the *old* schema; the
  database then held ``ix_test_experimentation_*`` indexes inside schema
  ``experimentation`` and autogenerate proposed dropping and re-creating all 67.

The test runs for **both profiles** (a core checkout is a tree with no
``modules/``; ``tree_profiles.py`` builds one) and asserts on the operations in
the generated revision, never on the exit status -- all three bugs above exited
0 and wrote a file.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = pytest.mark.integration

#: The full-profile case needs a ``modules/`` directory; the core one builds
#: its own tree and runs in either checkout.
BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]

#: How many module tables a full-profile schema holds
#: (``autogenerate_filters.MODULE_TABLES``, pinned to the manifest).
MODULE_TABLE_COUNT = 15


@pytest.fixture
def scratch_schema(test_db):
    schema = f"autogen_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _module_tables(engine, schema: str) -> set[str]:
    """The module-owned tables present in *schema*, by the filter's own list."""
    from sqlalchemy import inspect

    from backend.app.db.autogenerate_filters import MODULE_TABLES

    return MODULE_TABLES & set(inspect(engine).get_table_names(schema=schema))


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_fresh_bootstrap_leaves_nothing_to_autogenerate(
    profile, scratch_schema, tmp_path
):
    """The contract: models in, schema out, and the two then agree exactly."""
    tree = tree_profiles.tree_for(profile, tmp_path)

    created = tree_profiles.bootstrap_schema(tree, scratch_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )

    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


@pytest.mark.regression
@pytest.mark.modules
@pytest.mark.parametrize("head", ["modules@head"], ids=["modules_head"])
def test_a_fresh_full_bootstrap_leaves_nothing_to_autogenerate_on_the_modules_branch(
    head, scratch_schema, tmp_path
):
    """The same contract for the one revision that sees the module tables.

    ``--head <core head>`` puts the core filter in place, which skips every
    module table -- so the test above cannot see a module model and a module
    migration disagree.  ``--head modules@head`` is how a module revision is
    generated, and it compares *everything*: a fresh full bootstrap (the models)
    must leave it nothing to propose either.
    """
    tree = tree_profiles.tree_for(FULL, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, scratch_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    result = tree_profiles.autogenerate(tree, scratch_schema, tmp_path, head=head)

    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_autogenerate_never_proposes_dropping_a_table(
    profile, scratch_schema, tmp_path
):
    """Stated separately because this is the destructive half.

    ``test_a_fresh_bootstrap_leaves_nothing_to_autogenerate`` subsumes it on a
    green run; when it fails, this says whether the failure is "a migration is
    missing" or "this revision would delete the production schema".
    """
    tree = tree_profiles.tree_for(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )

    drops = [op for op in result.operations if op.startswith("op.drop_")]
    assert drops == [], result.describe()


@pytest.mark.regression
@pytest.mark.modules
def test_a_core_checkout_leaves_the_module_tables_alone(
    test_db, scratch_schema, tmp_path
):
    """Finding 2, end to end: the database has every module table, the metadata none.

    A full bootstrap builds the schema, the ``modules`` branch's row is then
    removed from ``alembic_version`` -- which is what a database the *core*
    chain brought up to date looks like, and the only state a core checkout can
    read at all (a row it has no file for stops alembic dead; that is finding 4,
    and ``test_profile_transitions.py`` covers it).  Nine of the twelve module
    tables are created by core-chain revisions, so the tables are still there
    while the core metadata knows nothing about them.

    Before the fix this produced twelve ``op.drop_table`` calls and 37
    ``op.drop_index`` calls -- a core migration that deletes every workspace,
    SSO configuration, BAA and PHI audit row on any full-profile database it is
    later applied to.
    """
    full = tree_profiles.tree_for(FULL, tmp_path)
    assert tree_profiles.bootstrap_schema(full, scratch_schema).returncode == 0

    core = tree_profiles.tree_for(CORE, tmp_path)
    head = tree_profiles.CORE_HEAD
    with test_db.begin() as conn:
        conn.execute(
            text(f'DELETE FROM "{scratch_schema}".alembic_version'),
        )
        conn.execute(
            text(f'INSERT INTO "{scratch_schema}".alembic_version VALUES (:rev)'),
            {"rev": head},
        )
    module_tables = _module_tables(test_db, scratch_schema)
    assert len(module_tables) == MODULE_TABLE_COUNT, sorted(module_tables)

    result = tree_profiles.autogenerate(core, scratch_schema, tmp_path, head=head)

    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()
    # Still there, and the revision would not have removed them.
    assert _module_tables(test_db, scratch_schema) == module_tables


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_test_environment_does_not_redirect_the_comparison(
    profile, scratch_schema, tmp_path
):
    """Finding 1: ``APP_ENV=test`` must not change *which* schema is compared.

    ``POSTGRES_SCHEMA`` names the schema; ``APP_ENV``/``TESTING`` named the one
    the models declared.  With the two disagreeing -- ``export APP_ENV=test
    TESTING=true`` in a shell pointed at a real database, which is the first
    thing ``CONTRIBUTING.md`` tells a developer to type -- autogenerate compared a
    metadata describing one schema against a reflection of the other and
    proposed creating every table in one and dropping every table from the
    other.
    """
    tree = tree_profiles.tree_for(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0

    process_env = {"APP_ENV": "test", "TESTING": "true"}
    result = tree_profiles.autogenerate(
        tree,
        scratch_schema,
        tmp_path,
        head=tree_profiles.CORE_HEAD,
        extra_env=process_env,
    )

    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_colliding_default_schema_does_not_hide_the_tables(
    profile, scratch_schema, tmp_path
):
    """Finding 5: when the connection's default schema *is* the app schema.

    alembic replaces the connection's default schema with ``None`` before it
    runs ``include_name`` (``autogenerate/compare/schema.py``), so a role with
    ``search_path`` set -- or ``PGOPTIONS``, or ``POSTGRES_SCHEMA=public`` --
    left ``env.py``'s filter rejecting the only entry that stood for the schema
    it means to compare.  Nothing was reflected, every existing table looked
    missing, and one ``alembic revision --autogenerate`` produced 37
    ``op.create_table`` calls and 116 ``op.create_index`` calls.

    Asserted on ``create_table`` alone, deliberately.  The ``None`` stand-in is
    not applied by alembic's *constraint* comparison or to
    ``version_table_schema``, so this configuration still produces foreign-key
    churn and an ``op.drop_table('alembic_version')``; a schema that is not the
    connection's default is the supported arrangement, and it is what
    ``test_a_fresh_bootstrap_leaves_nothing_to_autogenerate`` above pins.  What
    must never happen is autogenerate deciding the whole schema is empty.
    """
    tree = tree_profiles.tree_for(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0

    result = tree_profiles.autogenerate(
        tree,
        scratch_schema,
        tmp_path,
        head=tree_profiles.CORE_HEAD,
        extra_env={"PGOPTIONS": f"-c search_path={scratch_schema}"},
    )

    assert result.body is not None, result.describe()
    creates = [op for op in result.operations if op == "op.create_table("]
    assert creates == [], result.describe()
