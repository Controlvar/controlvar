"""Every table is erased or kept for a reason, both ways (#2042).

``ERASED_TABLES`` in ``backend/app/services/app_user_erasure.py`` is what the
erase deletes; it is read here, never retyped. Every other table of the
profile is in ``KEPT_CORE`` or ``KEPT_MODULES`` with a reason. The sets are
compared exactly, both ways:

* a new table fails ``test_every_table_is_classified`` until it is erased or
  kept here (``unclassified tables``), and a table that is gone fails it too
  (``classified but absent``);
* a column whose name looks like an app user's id (two rules: arch's anchored
  names and qa's substrings) is the id column ``ERASED_TABLES`` names for its
  table, or is listed below with a reason: ``PLATFORM`` for the columns that
  hold a dashboard account (a UUID of ``users``), ``KEPT_COLUMNS`` for the
  rest. A listed column that is gone fails too.

The module tables are required only when the modules load; the core set is
asserted in both profiles, so core-build's copy (no ``modules/``) runs the
same tests and is never skipped. The models are read from ``Base.metadata``
after ``register_core_models()`` and ``require_modules_or_absent()``, as the
test database and alembic read them. No database, no git.
"""

from __future__ import annotations

import re
from typing import Dict, Set, Tuple

import pytest
from sqlalchemy import String, Text
from sqlalchemy.dialects import postgresql

from backend.app.models import register_core_models
from backend.app.models.base import Base
from backend.app.modules_loader import require_modules_or_absent
from backend.app.schemas.app_user_erasure import AppUserEraseCounts
from backend.app.services.app_user_erasure import ERASED_TABLES, statements

pytestmark = [pytest.mark.unit, pytest.mark.regression]

#: Tables of the core profile that hold no app user's id, and why.
KEPT_CORE: Dict[str, str] = {
    "aggregated_metrics": "flag metrics per period: counts, no id",
    "analysis_snapshots": "an experiment's computed results per day: aggregates",
    "api_keys": "API keys; user_id is the dashboard account that made one",
    "audit_events_v2": "the compliance trail of dashboard actions; never deleted",
    "audit_logs": "the audit trail; never deleted, and the erase writes to it",
    "balance_group_counters": "assignments counted per balance group: counts",
    "bandit_state_history": "a bandit's weights over time: aggregates",
    "bandit_states": "a bandit's weights and counts per variant: aggregates",
    "experiments": "configuration; a targeting rule naming an id is kept",
    "feature_flag_safety_configs": "safety configuration of a flag",
    "feature_flags": "configuration; a targeting rule naming an id is kept",
    "flag_environment_settings": (
        "a flag's staging and development configuration; a targeting rule "
        "naming an id is kept"
    ),
    "global_holdouts": "holdout configuration",
    "llm_experiments": "LLM experiment configuration",
    "llm_variants": "LLM experiment configuration",
    "metrics": "an experiment's metric definitions",
    "mutual_exclusion_groups": "configuration",
    "notification_delivery_log": "notifications sent to dashboard accounts",
    "notification_preferences": "dashboard accounts' notification settings",
    "permissions": "dashboard access control",
    "reports": "saved reports: aggregates",
    "role_permission_association": "dashboard access control",
    "roles": "dashboard access control",
    "rollout_schedules": "flag rollout configuration",
    "rollout_stages": "flag rollout configuration",
    "safety_rollback_records": "flag rollbacks; executed_by_user_id is an account",
    "safety_settings": "safety configuration",
    "scheduler_runs": "background job bookkeeping",
    "sdk_evaluation_flag_counts": "SDK evaluations counted per flag: counts",
    "sdk_evaluation_key_counts": "SDK evaluations counted per API key: counts",
    "seed_markers": "demo data bookkeeping",
    "segments": "configuration; a rule naming an id is kept",
    "user_role_association": "dashboard access control",
    "user_totp": "dashboard accounts' two-step sign-in",
    "users": "dashboard accounts",
    "variants": "experiment configuration",
    "visual_tokens": "visual editor sessions of dashboard accounts",
}

#: Tables only the modules add, and why they are kept.
KEPT_MODULES: Dict[str, str] = {
    "baa_configs": "a HIPAA agreement's settings",
    "custom_roles": "dashboard access control",
    "direct_permission_grants": "dashboard access control",
    "integration_configs": "integration settings",
    "phi_audit_logs": "the PHI access trail of dashboard accounts",
    "sso_configs": "single sign-on settings",
    "user_custom_roles": "dashboard access control",
    "warehouse_analysis_runs": "analyses of your warehouse: aggregates",
    "warehouse_connections": "warehouse settings",
    "warehouse_sources": "warehouse settings",
    "workspace_api_keys": "workspace configuration",
    "workspace_invites": "invitations of dashboard accounts",
    "workspace_members": "workspace membership of dashboard accounts",
    "workspaces": "workspace configuration",
}

ACCOUNT = "a dashboard account (users.id), not an app user"

#: Columns with an id-like name that hold a dashboard account, core profile.
PLATFORM_CORE: Dict[Tuple[str, str], str] = {
    ("api_keys", "user_id"): ACCOUNT,
    ("audit_logs", "user_id"): ACCOUNT,
    ("notification_preferences", "user_id"): ACCOUNT,
    ("safety_rollback_records", "executed_by_user_id"): ACCOUNT,
    ("user_role_association", "user_id"): ACCOUNT,
    ("user_totp", "user_id"): ACCOUNT,
    ("visual_tokens", "user_id"): ACCOUNT,
}

#: The same for the module tables.
PLATFORM_MODULES: Dict[Tuple[str, str], str] = {
    ("direct_permission_grants", "user_id"): ACCOUNT,
    ("phi_audit_logs", "user_id"): ACCOUNT,
    ("user_custom_roles", "user_id"): ACCOUNT,
    ("workspace_members", "user_id"): ACCOUNT,
}

#: Other columns with an id-like name on a kept table.
KEPT_COLUMNS: Dict[Tuple[str, str], str] = {
    ("aggregated_metrics", "distinct_users"): "a count of users, not an id",
}

#: arch's rule: the names an app user's id is stored under.
ID_NAME = re.compile(
    r"^(user_id|member_id|anonymous_id|visitor_id|distinct_id|device_id|.*_user_id)$"
)
#: qa's rule: substrings of such a name (catches ``visitor_ref``).
ID_SUBSTRING = re.compile(r"user_id|member_id|visitor|anonymous|distinct|end_user")


@pytest.fixture(scope="module")
def profile() -> Tuple[Dict[str, object], bool]:
    """``({table name: Table}, modules loaded)``."""
    register_core_models()
    loaded = require_modules_or_absent()
    return {t.name: t for t in Base.metadata.tables.values()}, loaded


def _erased() -> Set[str]:
    return {table.name for table in ERASED_TABLES}


def _id_like(name: str) -> bool:
    return bool(ID_NAME.search(name) or ID_SUBSTRING.search(name))


def _string(column) -> bool:
    return isinstance(column.type, (String, Text))


def test_every_table_is_classified(profile):
    tables, loaded = profile
    kept = set(KEPT_CORE) | (set(KEPT_MODULES) if loaded else set())
    expected = _erased() | kept
    unclassified = sorted(set(tables) - expected)
    absent = sorted(expected - set(tables))
    assert unclassified == [], f"unclassified tables: {unclassified}"
    assert absent == [], f"classified but absent: {absent}"


def test_the_lists_do_not_overlap_and_every_reason_is_given():
    erased = _erased()
    assert len(erased) == len(ERASED_TABLES), "a table is erased twice"
    assert erased.isdisjoint(KEPT_CORE)
    assert erased.isdisjoint(KEPT_MODULES)
    assert set(KEPT_CORE).isdisjoint(KEPT_MODULES)
    for reasons in (KEPT_CORE, KEPT_MODULES, PLATFORM_CORE, PLATFORM_MODULES):
        assert all(reason.strip() for reason in reasons.values())


def test_every_erased_table_is_core():
    """The erase works with ``modules/`` deleted: full ERASED == core ERASED."""
    for table in ERASED_TABLES:
        assert table.model.__module__.startswith("backend.app."), table.name


def test_every_erased_column_is_an_app_user_id(profile):
    """Each id column is a string of the width tracking stores (255)."""
    tables, _ = profile
    for table in ERASED_TABLES:
        for name in table.columns:
            column = tables[table.name].c[name]
            assert isinstance(column.type, String), (table.name, name)
            assert column.type.length == 255, (table.name, name)
            assert _id_like(name), (table.name, name)


def test_kept_tables_have_no_unlisted_id_column(profile):
    tables, loaded = profile
    erased = {(t.name, c) for t in ERASED_TABLES for c in t.columns}
    platform = {**PLATFORM_CORE, **(PLATFORM_MODULES if loaded else {})}
    listed = set(platform) | set(KEPT_COLUMNS)
    problems = []
    for name, table in sorted(tables.items()):
        for column in table.columns:
            if not _id_like(column.name):
                continue
            key = (name, column.name)
            if key in erased or key in listed:
                continue
            if name in _erased():
                problems.append(
                    f"erased table {name} has an id-like column it does not "
                    f"erase: {column.name}"
                )
            else:
                problems.append(
                    f"kept table {name} has an unlisted id-like column: {column.name}"
                )
    assert problems == []
    for name, column in sorted(platform):
        assert not _string(tables[name].c[column]), (
            f"{name}.{column} is text: an app user's id is never PLATFORM"
        )


def test_no_listed_column_is_stale(profile):
    tables, loaded = profile
    entries = {**PLATFORM_CORE, **KEPT_COLUMNS}
    if loaded:
        entries.update(PLATFORM_MODULES)
    stale = sorted(
        f"{name}.{column}"
        for name, column in entries
        if name not in tables or column not in tables[name].c or not _id_like(column)
    )
    assert stale == [], f"listed but not an id-like column: {stale}"


def test_the_response_counts_every_erased_table():
    assert list(AppUserEraseCounts.model_fields) == [t.name for t in ERASED_TABLES]


def test_each_erased_table_is_read_through_an_index(profile):
    """A table with a parent: an index on ``(parent, id)``; any other: an index
    (or the primary key) leading with the id column."""
    tables, _ = profile
    for table in ERASED_TABLES:
        t = tables[table.name]
        leads = [[c.name for c in index.columns] for index in t.indexes]
        leads.append([c.name for c in t.primary_key.columns])
        if table.parent is None:
            for column in table.columns:
                assert any(cols[:1] == [column] for cols in leads), (table.name, column)
        else:
            wanted = [table.parent[0], table.column]
            assert any(cols[:2] == wanted for cols in leads), table.name


def _sql(statement) -> str:
    """The statement as PostgreSQL gets it, on one line, schema names dropped."""
    text = str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    schema = ERASED_TABLES[0].model.__table__.schema
    if schema:
        text = text.replace(f"{schema}.", "")
    return " ".join(text.split())


@pytest.mark.parametrize("table", ERASED_TABLES, ids=lambda t: t.name)
def test_the_statement_shape_of_each_table(table):
    """Chunks by ``id``, by the dedup key's primary key, or one
    statement through the parent's primary key. Compared whole, so no other
    filter (an experiment, a workspace) narrows what is erased."""
    delete, check = (_sql(s) for s in statements(table, ["needle"]))
    t, c = table.name, table.column
    match = f"{t}.{c} IN ('needle')"
    if table.last:
        # The links: every id column, all rows at once, in the final transaction.
        both = " OR ".join(f"{t}.{column} IN ('needle')" for column in table.columns)
        assert table.columns == ("user_id", "anonymous_id")
        assert delete == f"DELETE FROM {t} WHERE {both}"
        assert check == f"SELECT 1 FROM {t} WHERE {both} LIMIT 1"
        return
    if table.parent is not None:
        column, parent = table.parent
        p = parent.__table__.name
        where = f"{t}.{column} IN (SELECT {p}.id FROM {p}) AND {match}"
        assert delete == f"DELETE FROM {t} WHERE {where}"
        assert check == f"SELECT 1 FROM {t} WHERE {where} LIMIT 1"
        return
    if t == "event_dedup_keys":
        keys = f"{t}.user_id, {t}.dedup_key"
        assert delete == (
            f"DELETE FROM {t} WHERE ({keys}) IN "
            f"(SELECT {keys} FROM {t} WHERE {match} LIMIT 1000)"
        )
    else:
        assert delete == (
            f"DELETE FROM {t} WHERE {t}.id IN "
            f"(SELECT {t}.id FROM {t} WHERE {match} LIMIT 1000)"
        )
    assert check == f"SELECT 1 FROM {t} WHERE {match} LIMIT 1"


def test_the_dashboard_has_no_control_for_the_erase():
    """No dashboard source names the route; the generated OpenAPI fixture
    under ``src/tests/fixtures`` is the only file that does."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[4] / "frontend" / "src"
    if not src.is_dir():
        pytest.skip("this tree has no frontend/src")
    fixtures = src / "tests" / "fixtures"
    callers = sorted(
        str(path.relative_to(src))
        for path in src.rglob("*")
        if path.is_file()
        and path.suffix in {".ts", ".tsx", ".js", ".jsx", ".json"}
        and fixtures not in path.parents
        and "app-users/erase" in path.read_text(encoding="utf-8", errors="replace")
    )
    assert callers == []


def test_the_erase_touches_no_cache_and_no_other_store():
    """The module imports no Redis, cache, DynamoDB or AWS client: an erase
    clears no cached result and writes only PostgreSQL."""
    import ast
    import inspect

    from backend.app.services import app_user_erasure

    tree = ast.parse(inspect.getsource(app_user_erasure))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    pattern = re.compile(r"redis|cache|dynamo|boto|kinesis|s3", re.I)
    assert sorted(m for m in imported if pattern.search(m)) == []
