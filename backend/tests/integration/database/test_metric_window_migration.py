"""``t1934_metric_window``: a nullable conversion window on each metric, through the transitions (#1934).

The revision adds ``metrics.conversion_window_hours`` and the check
``check_metric_conversion_window``, each only when absent, and nothing else. A
schema built by ``create_all`` already has both, so the tests that start from
before it take a bootstrapped schema with the column DROPPED (the check goes
with it) and ``alembic_version`` at the earlier revision's rows
(``t2012_flag_split``, or 0.48.0's heads with everything 0.49.0 adds dropped
too), in each profile, and run the documented commands. Then:

1. ``alembic upgrade heads``: the ``alembic_version`` rows are this release's
   (printed, with the column and the check), every metric's window is NULL,
   nothing else of ``metrics`` changes, and autogenerate is empty;
2. an upgraded database and a bootstrapped one have the same columns, checks
   and indexes on ``metrics``, compared by name;
3. the check holds on both: NULL, 1 and 8760 are stored, 0, 8761 and -1 are
   refused;
4. the previous release's code (this tree without the revision and the column)
   creates an experiment with metrics, reads it and its results on this
   schema, stores no window, and a stored window stays where it is;
5. the statements each direction runs, captured online: the upgrade's two
   ALTERs straight between the bound and the reset, the downgrade's one;
6. the waits, each leg naming its revision, never ``heads``: a session reading
   ``metrics`` ends the upgrade at its bound with this revision's refusal,
   applying nothing -- no revision of the run, when one run applies 0.49.0's
   other two too -- and the next run goes through; a deadlock ends it with the
   same refusal; a session reading ``metrics`` ends the downgrade with its
   refusal, changing nothing;
7. while one run from 0.48.0 waits for ``metrics``, a new SDK request waits
   for ``api_keys`` with it, and is answered once the run ends;
8. the downgrade drops the column and every window and nothing else; a run
   that goes on past ``t2012_flag_split`` with a split in use refuses there
   and keeps the column and its windows;
9. offline (``--sql``), both directions refuse and emit no DDL;
10. this release, still running against the downgraded schema, answers 500 to
    what reads a metric, and SDK requests are still answered;
11. ``alembic heads`` names exactly this revision and the modules head.
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.lock_capture import until_the_pid_waits
from backend.tests.integration.database.metric_window_column import (
    without_the_metric_window,
)
from backend.tests.integration.database.test_flag_environments_migration import (
    _captured,
    _columns,
    _constraints,
    _deadlocks,
    _holding,
    _indexes,
    _insert_key,
    _key_column,
    _rows,
    _scratch,
    _set_aside,
    _stamp,
    _tables,
    _token,
    _until_waiting_for,
    _user,
)
from backend.tests.integration.database.test_flag_environments_migration import (
    _to_previous_release as _to_0_48_0,
)
from backend.tests.integration.database.test_flag_split_migration import (
    PROCESS_SECONDS,
    SPLIT,
    _answers,
    _counted_a_deadlock,
    _evaluate,
    _in_the_background,
    _insert_flag,
    _leg,
    _reading,
    _refusal,
    _split_columns,
    _table_lock,
)
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, the core revision it extends, the one before that, and
#: 0.48.0's core head.
REVISION = "t1934_metric_window"
PREVIOUS_CORE_HEAD = "t2012_flag_split"
FLAG_ENVIRONMENTS = "t2011_flag_environments"
RELEASED_CORE_HEAD = "t449_identity_aliases"
#: The core head of this tree.
CORE_HEAD = "t1934_metric_window"
MODULES_HEAD = "modules_0005_redshift"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
RELEASED_ROWS = {
    CORE: {RELEASED_CORE_HEAD},
    FULL: {RELEASED_CORE_HEAD, MODULES_HEAD},
}
ROWS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}
#: Where every downgrade leg starts: this revision's own rows, whatever later
#: revision the tree's head is.
REVISION_ROWS = {CORE: {REVISION}, FULL: {REVISION, MODULES_HEAD}}

TABLE = "metrics"
COLUMN = "conversion_window_hours"
CHECK = "check_metric_conversion_window"
#: The column after either path: (``format_type``, NOT NULL, default).
EXPECTED_COLUMN = ("integer", False, None)
#: The check after either path, as PostgreSQL prints it.
EXPECTED_CHECK = (
    CHECK,
    "c",
    "CHECK (((conversion_window_hours IS NULL) OR ((conversion_window_hours >= 1) "
    "AND (conversion_window_hours <= 8760))))",
)

#: Each online leg's own bound on any one statement: a wait the revision did
#: not bound ends here (57014), never in a hung test.
LEG_STATEMENT_TIMEOUT = "-c statement_timeout=10s"

#: The full-profile case needs a ``modules/`` directory; the core one builds
#: its own sealed tree and runs in either checkout.
BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]

UPGRADED = "upgraded"
BOOTSTRAPPED = "bootstrapped"

#: Where an upgrade starts: the revision before this one, where it runs this
#: one alone, and 0.48.0's heads, where one run applies
#: ``t2011_flag_environments``, ``t2012_flag_split`` and then this one.
FROM_T2012 = "from-t2012"
FROM_0_48_0 = "from-0.48.0"
STARTS = [FROM_0_48_0, FROM_T2012]


@pytest.fixture
def scratch_schema(test_db):
    yield from _scratch(test_db, "metricwin")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "metricwinref")


# ---------------------------------------------------------------------------
# Reading the catalog and the rows
# ---------------------------------------------------------------------------
def _window_column(engine, schema: str):
    """The column as (``format_type``, NOT NULL, default), or None."""
    found = [row[1:] for row in _columns(engine, schema, TABLE) if row[0] == COLUMN]
    return found[0] if found else None


def _check(engine, schema: str):
    found = [c for c in _constraints(engine, schema, TABLE) if c[0] == CHECK]
    return found[0] if found else None


def _definition(engine, schema: str) -> tuple:
    """``metrics``' columns by name, constraints and indexes, schema-neutral."""
    return (
        {row[0]: row[1:] for row in _columns(engine, schema, TABLE)},
        _constraints(engine, schema, TABLE),
        _indexes(engine, schema, TABLE),
    )


def _windows(engine, schema: str) -> dict[str, object]:
    """Each metric's name -> its window."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(f'SELECT name, {COLUMN} FROM "{schema}".{TABLE} ORDER BY name')
        ).all()
    return dict(rows)


def _windows_by_experiment(engine, schema: str, experiments: dict) -> dict:
    """``(label, metric name) -> window`` for *experiments* (label -> id)."""
    labels = {experiment_id: label for label, experiment_id in experiments.items()}
    with engine.connect() as conn:
        rows = conn.execute(
            text(f'SELECT experiment_id::text, name, {COLUMN} FROM "{schema}".{TABLE}')
        ).all()
    return {(labels[e], name): window for e, name, window in rows if e in labels}


def _stored(engine, schema: str) -> list[str]:
    """Every row of ``metrics``, every column but the window, as JSON text."""
    with engine.connect() as conn:
        return sorted(
            json.dumps(row, sort_keys=True)
            for row in conn.execute(
                text(f"SELECT to_jsonb(m) - '{COLUMN}' FROM \"{schema}\".{TABLE} m")
            ).scalars()
        )


def _report(profile, label, engine, schema) -> None:
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(
        f"[{profile}] {label}: {TABLE}.{COLUMN}={_window_column(engine, schema)}, "
        f"check={_check(engine, schema)}"
    )


VERSIONS = (
    Path(__file__).resolve().parents[3] / "app" / "db" / "migrations" / "versions"
)


def _load_revision(schema: str):
    """This revision's module, its schema pointed at *schema*."""
    (path,) = VERSIONS.glob(f"{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(
        f"_rev_{REVISION}_{uuid.uuid4().hex[:6]}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._SCHEMA = schema
    return module


# ---------------------------------------------------------------------------
# Building the databases
# ---------------------------------------------------------------------------
def _bootstrapped(profile, schema, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, schema)
    assert created.returncode == 0, created.stderr[-3000:]
    return tree


def _to_t2012(engine, profile, schema) -> None:
    """Turn a bootstrapped *schema* into what ``t2012_flag_split`` left."""
    with engine.begin() as conn:
        # IF EXISTS: a plant that removes it from the model leaves create_all
        # without it, and the test must reach the assertion that says so.
        conn.execute(
            text(f'ALTER TABLE "{schema}".{TABLE} DROP COLUMN IF EXISTS {COLUMN}')
        )
    _stamp(engine, schema, PREVIOUS_ROWS[profile])
    assert _window_column(engine, schema) is None
    assert _check(engine, schema) is None


def _starting(test_db, profile, schema, tmp_path, start):
    """A schema at *start*, and the tree to upgrade it with."""
    tree = _bootstrapped(profile, schema, tmp_path)
    if start == FROM_T2012:
        _to_t2012(test_db, profile, schema)
    else:
        _to_0_48_0(test_db, profile, schema)
        assert _window_column(test_db, schema) is None
    return tree


def _at_this_revision(test_db, profile, schema, tmp_path):
    """A schema whose ``alembic_version`` is this revision's, named: a
    downgrade from here runs this revision's downgrade first."""
    tree = _bootstrapped(profile, schema, tmp_path)
    _stamp(test_db, schema, REVISION_ROWS[profile])
    assert _window_column(test_db, schema) == EXPECTED_COLUMN
    return tree


def _upgrade(tree, schema: str):
    return tree_profiles.alembic(tree, schema, "upgrade", "heads")


def _at_head(test_db, profile, built_by, schema, tmp_path):
    if built_by == UPGRADED:
        tree = _starting(test_db, profile, schema, tmp_path, FROM_0_48_0)
        upgrade = _upgrade(tree, schema)
        assert upgrade.returncode == 0, upgrade.stderr[-3000:]
        return tree
    return _bootstrapped(profile, schema, tmp_path)


def _insert_experiment(engine, schema: str, windows: dict) -> str:
    """A draft experiment with one metric per entry of *windows* (name ->
    window), written with SQL: the window only where one is given, so it can
    be written before the column exists too."""
    experiment_id = str(uuid.uuid4())
    suffix = experiment_id[:8]
    with engine.begin() as conn:
        owner = conn.execute(
            text(
                f'INSERT INTO "{schema}".users (id, username, email, hashed_password, '
                "is_active, is_superuser, role, created_at, updated_at) VALUES "
                "(gen_random_uuid(), :u, :e, 'x', true, false, 'ADMIN', now(), now()) "
                "RETURNING id"
            ),
            {"u": f"window_{suffix}", "e": f"window_{suffix}@window.test"},
        ).scalar()
        conn.execute(
            text(
                f'INSERT INTO "{schema}".experiments (id, name, key, status, '
                "experiment_type, optimization_type, sequential_testing_enabled, "
                "bayesian_enabled, owner_id, created_at, updated_at) VALUES "
                "(:id, :name, :key, 'DRAFT', 'A_B', 'fixed', false, false, :owner, "
                "now(), now())"
            ),
            {
                "id": experiment_id,
                "name": f"Windows {suffix}",
                "key": f"windows-{suffix}",
                "owner": owner,
            },
        )
        for name, window in windows.items():
            columns = {
                "id": str(uuid.uuid4()),
                "experiment_id": experiment_id,
                "name": name,
                "event_name": f"{name}_event",
                "metric_type": "CONVERSION",
            }
            if window is not None:
                columns[COLUMN] = window
            names = ", ".join([*columns, "created_at", "updated_at"])
            values = ", ".join([*(f":{c}" for c in columns), "now()", "now()"])
            conn.execute(
                text(f'INSERT INTO "{schema}".{TABLE} ({names}) VALUES ({values})'),
                columns,
            )
    return experiment_id


# ---------------------------------------------------------------------------
# 1-3. The upgrade, the definitions and the check
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("start", STARTS)
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_earlier_database_gains_the_column_and_the_check_and_no_data(
    profile, start, test_db, scratch_schema, tmp_path
):
    tree = _starting(test_db, profile, scratch_schema, tmp_path, start)
    _insert_experiment(test_db, scratch_schema, {"signup": None, "purchase": None})
    before = _stored(test_db, scratch_schema)
    _report(profile, f"{start}, before", test_db, scratch_schema)

    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    print(f"[{profile}/{start}] upgrade stderr tail:\n{upgrade.stderr[-1500:]}")
    _report(profile, f"{start}, after upgrade heads", test_db, scratch_schema)
    assert f"Running upgrade {PREVIOUS_CORE_HEAD} -> {REVISION}" in upgrade.stderr
    if start == FROM_0_48_0:
        for step in (
            f"Running upgrade {RELEASED_CORE_HEAD} -> {FLAG_ENVIRONMENTS}",
            f"Running upgrade {FLAG_ENVIRONMENTS} -> {PREVIOUS_CORE_HEAD}",
        ):
            assert step in upgrade.stderr, upgrade.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _window_column(test_db, scratch_schema) == EXPECTED_COLUMN
    assert _check(test_db, scratch_schema) == EXPECTED_CHECK
    assert _windows(test_db, scratch_schema) == {"purchase": None, "signup": None}
    assert _stored(test_db, scratch_schema) == before

    generated = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    print(f"[{profile}/{start}] autogenerate: {generated.operations}")
    assert generated.body is not None, generated.describe()
    assert generated.operations == [], generated.describe()


@pytest.mark.regression
@pytest.mark.parametrize("start", STARTS)
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_have_the_same_metrics_table(
    profile, start, test_db, scratch_schema, reference_schema, tmp_path
):
    """Compared by name: an upgraded table has the column last, a bootstrapped
    one before ``id``, ``created_at`` and ``updated_at``."""
    tree = _starting(test_db, profile, scratch_schema, tmp_path, start)
    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    _bootstrapped(profile, reference_schema, tmp_path / "reference")
    upgraded = _definition(test_db, scratch_schema)
    bootstrapped = _definition(test_db, reference_schema)
    assert upgraded == bootstrapped
    assert upgraded[0][COLUMN] == EXPECTED_COLUMN
    assert EXPECTED_CHECK in upgraded[1]


@pytest.mark.regression
@pytest.mark.parametrize("built_by", [UPGRADED, BOOTSTRAPPED])
def test_the_check_stores_null_and_1_to_8760_and_refuses_the_rest(
    built_by, test_db, scratch_schema, tmp_path
):
    _at_head(test_db, CORE, built_by, scratch_schema, tmp_path)
    stored = {"none": None, "one": 1, "a-week": 168, "a-year": 8760}
    _insert_experiment(test_db, scratch_schema, stored)
    assert _windows(test_db, scratch_schema) == stored
    for refused in (0, 8761, -1):
        with pytest.raises(IntegrityError) as raised:
            _insert_experiment(test_db, scratch_schema, {"refused": refused})
        assert CHECK in str(raised.value), refused
    assert _windows(test_db, scratch_schema) == stored


# ---------------------------------------------------------------------------
# 4. The previous release on this schema
# ---------------------------------------------------------------------------
def _previous_release(profile: str, tmp_path) -> Path:
    """Release N-1: a copy of this tree without the revision and the column."""
    destination = tmp_path / "release-n-minus-1"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    without_the_metric_window(tree)
    return tree


def _experiment_body(suffix: str) -> dict:
    return {
        "name": f"Windows {suffix}",
        "key": f"windows-{suffix}",
        "hypothesis": "the previous release ignores a metric's window",
        "experiment_type": "a_b",
        "variants": [
            {"name": "control", "is_control": True, "traffic_allocation": 50},
            {"name": "treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Purchase",
                "event_name": f"purchase_{suffix}",
                "metric_type": "conversion",
                "is_primary": True,
            },
            {
                "name": "Signup",
                "event_name": f"signup_{suffix}",
                "metric_type": "conversion",
            },
        ],
    }


@pytest.mark.regression
@pytest.mark.parametrize("built_by", [UPGRADED, BOOTSTRAPPED])
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_writes_and_reads_metrics_on_this_schema(
    profile, built_by, test_db, scratch_schema, tmp_path
):
    """It names the column in no statement: an experiment it creates has no
    window on either metric, it reads the experiment and its results, and a
    window stored on another experiment's metric stays through its reads and
    a change to that experiment."""
    _at_head(test_db, profile, built_by, scratch_schema, tmp_path)
    release_n_minus_1 = _previous_release(profile, tmp_path)
    user = _user(test_db, scratch_schema)
    headers = {"Authorization": f"Bearer {_token(user)}"}
    created = {}
    for name in ("new", "windowed"):
        ((status, made, _),) = _answers(
            release_n_minus_1,
            scratch_schema,
            tmp_path,
            [["POST", "/api/v1/experiments/", headers, _experiment_body(name)]],
        )
        assert status in (200, 201), made
        created[name] = made["id"]
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".{TABLE} SET {COLUMN} = 168 '
                "WHERE experiment_id = :id AND name = 'Purchase'"
            ),
            {"id": created["windowed"]},
        )
    expected = {
        ("new", "Purchase"): None,
        ("new", "Signup"): None,
        ("windowed", "Purchase"): 168,
        ("windowed", "Signup"): None,
    }
    assert _windows_by_experiment(test_db, scratch_schema, created) == expected
    new = f"/api/v1/experiments/{created['new']}"
    windowed = f"/api/v1/experiments/{created['windowed']}"
    asked = [
        ["GET", new, headers, None],
        ["GET", "/api/v1/experiments/", headers, None],
        ["POST", f"{new}/start", headers, None],
        ["GET", f"/api/v1/results/{created['new']}", headers, None],
        ["GET", windowed, headers, None],
        ["PUT", windowed, headers, {"description": "changed by the previous release"}],
        ["POST", f"{windowed}/start", headers, None],
        ["GET", f"/api/v1/results/{created['windowed']}", headers, None],
    ]
    answered = _answers(release_n_minus_1, scratch_schema, tmp_path, asked)
    statuses = [answer[0] for answer in answered]
    print(f"[{profile}/{built_by}] the previous release answered {statuses}")
    assert statuses == [200] * len(asked), answered
    assert sorted(m["name"] for m in answered[0][1]["metrics"]) == [
        "Purchase",
        "Signup",
    ]
    for answer in answered:
        assert COLUMN not in json.dumps(answer[1]), answer
    assert _windows_by_experiment(test_db, scratch_schema, created) == expected


# ---------------------------------------------------------------------------
# 5. The statements each direction runs
# ---------------------------------------------------------------------------
SET = "SET LOCAL lock_timeout = '500ms'"
RESET = "SET LOCAL lock_timeout TO DEFAULT"


def _added(schema: str) -> list[str]:
    return [
        SET,
        f"ALTER TABLE {schema}.{TABLE} ADD COLUMN {COLUMN} INTEGER",
        f"ALTER TABLE {schema}.{TABLE} ADD CONSTRAINT {CHECK} CHECK "
        f"({COLUMN} IS NULL OR {COLUMN} BETWEEN 1 AND 8760)",
        RESET,
    ]


@pytest.mark.regression
@pytest.mark.parametrize("start", STARTS)
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_upgrade_runs_exactly_its_statements(
    profile, start, test_db, scratch_schema, tmp_path
):
    """The two ALTERs straight between the bound and the reset, last in the
    run, and no data step. Nothing runs between the ``SET`` and the first
    ALTER, a read included: the catalog is read before the bound is set."""
    tree = _starting(test_db, profile, scratch_schema, tmp_path, start)
    captured = _captured(tree, scratch_schema, tmp_path, "upgrade", REVISION)
    for statement in captured:
        print(f"[{profile}/{start}] captured: {statement[:200]}")
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[profile]
    remaining = _set_aside(captured)
    assert remaining[-4:] == _added(scratch_schema), remaining
    if start == FROM_T2012:
        assert remaining == _added(scratch_schema), remaining
    for statement in remaining:
        verb = statement.split(" ", 1)[0].upper()
        assert verb not in {"INSERT", "UPDATE", "DELETE"}, f"a data step: {statement}"
    # Adjacent among everything captured, not only among what is left.
    first = captured.index(_added(scratch_schema)[1])
    assert captured[first - 1 : first + 3] == _added(scratch_schema)


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_runs_exactly_its_statements(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_this_revision(test_db, profile, scratch_schema, tmp_path)
    captured = _captured(
        tree, scratch_schema, tmp_path, "downgrade", PREVIOUS_CORE_HEAD
    )
    for statement in captured:
        print(f"[{profile}] captured: {statement[:200]}")
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _set_aside(captured) == [
        SET,
        f"ALTER TABLE {scratch_schema}.{TABLE} DROP COLUMN {COLUMN}",
        RESET,
    ]


# ---------------------------------------------------------------------------
# 6. The waits
# ---------------------------------------------------------------------------
def _nothing_applied(test_db, schema: str, start: str) -> None:
    """The schema is what it was at *start*: no revision of the run stayed."""
    rows = PREVIOUS_ROWS[CORE] if start == FROM_T2012 else RELEASED_ROWS[CORE]
    assert _rows(test_db, schema) == rows
    assert _window_column(test_db, schema) is None
    assert _check(test_db, schema) is None
    if start == FROM_0_48_0:
        assert "flag_environment_settings" not in _tables(test_db, schema)
        assert _key_column(test_db, schema) is None
        assert _split_columns(test_db, schema) == {}


@pytest.mark.regression
@pytest.mark.parametrize("start", STARTS)
def test_a_session_reading_metrics_ends_the_upgrade_with_the_refusal(
    start, test_db, scratch_schema, tmp_path
):
    """The ``ALTER TABLE`` waits for every session that has read ``metrics``
    and not ended its transaction, a results request among them. The bound
    ends the wait (SQLSTATE 55P03) with this revision's refusal, and nothing
    is applied, not even the revisions the same run had already run; once the
    session is gone, the same command runs through. Without the ``SET`` only
    the leg's own ``statement_timeout`` ends it (57014), and this fails."""
    tree = _starting(test_db, CORE, scratch_schema, tmp_path, start)
    expected = _load_revision(scratch_schema)._lock_refusal()
    with _holding(test_db, _reading(scratch_schema, TABLE)):
        upgrade = _leg(tree, scratch_schema, "upgrade", REVISION)
    print(f"[{start}] upgrade stderr tail:\n{upgrade.stderr[-1500:]}")
    assert upgrade.returncode != 0
    assert expected in upgrade.stderr, upgrade.stderr[-3000:]
    assert _refusal(upgrade.stderr) == expected
    assert "statement timeout" not in upgrade.stderr
    if start == FROM_0_48_0:
        assert f"Running upgrade {FLAG_ENVIRONMENTS} -> {PREVIOUS_CORE_HEAD}" in (
            upgrade.stderr
        )
    _nothing_applied(test_db, scratch_schema, start)
    again = _leg(tree, scratch_schema, "upgrade", REVISION)
    assert again.returncode == 0, again.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert _window_column(test_db, scratch_schema) == EXPECTED_COLUMN
    assert _check(test_db, scratch_schema) == EXPECTED_CHECK


#: The migration session's own deadlock check, in the leg that meets one: below
#: the 500 ms bound, so the deadlock is found before the bound ends the wait,
#: and long enough for the other session to be waiting by then.
DEADLOCK_OPTIONS = f"{LEG_STATEMENT_TIMEOUT} -c deadlock_timeout=300ms"


@pytest.mark.regression
def test_a_deadlock_ends_the_upgrade_with_the_refusal_and_applies_nothing(
    test_db, scratch_schema, tmp_path
):
    """One run from 0.48.0 holds ``api_keys`` (``t2011_flag_environments``)
    while this revision waits for ``metrics``. A session that read the metrics
    and then reads the keys waits for the run, and the run for it: PostgreSQL
    ends the run with 40P01, and it refuses with the same text as a lock
    timeout, applying nothing; the session's read goes through. A lock timeout
    gives the same text, so this counts the deadlocks."""
    tree = _starting(test_db, CORE, scratch_schema, tmp_path, FROM_0_48_0)
    expected = _load_revision(scratch_schema)._lock_refusal()
    before = _deadlocks(test_db)
    outcome: dict = {}
    with test_db.connect() as session:
        transaction = session.begin()
        session.execute(text("SET LOCAL statement_timeout = '60s'"))
        session.execute(text("SET LOCAL deadlock_timeout = '30s'"))
        pid = session.execute(text("SELECT pg_backend_pid()")).scalar()
        session.execute(text(_reading(scratch_schema, TABLE)))

        def read_the_keys() -> None:
            try:
                session.execute(text(_reading(scratch_schema, "api_keys")))
                outcome["read"] = True
            except Exception as exc:  # reported by the assertion below
                outcome["error"] = exc

        with _in_the_background(
            tree, scratch_schema, "upgrade", REVISION, options=DEADLOCK_OPTIONS
        ) as (process, name):
            _until_waiting_for(
                test_db, process, name, _table_lock(scratch_schema, TABLE)
            )
            reading = threading.Thread(target=read_the_keys)
            reading.start()
            waited_for = until_the_pid_waits(test_db, pid)
            stderr = process.communicate(timeout=PROCESS_SECONDS)[1]
            reading.join(timeout=PROCESS_SECONDS)
        transaction.rollback()
    assert waited_for == f"{scratch_schema}.api_keys", waited_for
    print(f"upgrade stderr tail:\n{stderr[-1500:]}")
    assert process.returncode != 0
    assert expected in stderr, stderr[-3000:]
    assert "statement timeout" not in stderr
    assert outcome == {"read": True}, outcome
    _counted_a_deadlock(test_db, before)
    _nothing_applied(test_db, scratch_schema, FROM_0_48_0)


@pytest.mark.regression
def test_a_session_reading_metrics_ends_the_downgrade_with_its_refusal(
    test_db, scratch_schema, tmp_path
):
    """The drop waits behind a request reading ``metrics``. The bound ends
    the wait with the downgrade's refusal and nothing is changed, the stored
    windows included; once the session is gone, the same command runs
    through. Without the ``SET`` only the leg's own ``statement_timeout`` ends
    it."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    _insert_experiment(test_db, scratch_schema, {"a-day": 24, "none": None})
    before = (_definition(test_db, scratch_schema), _windows(test_db, scratch_schema))
    expected = _load_revision(scratch_schema)._downgrade_lock_refusal()
    with _holding(test_db, _reading(scratch_schema, TABLE)):
        down = _leg(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    print(f"downgrade stderr tail:\n{down.stderr[-1500:]}")
    assert down.returncode != 0
    assert _refusal(down.stderr) == expected
    assert "Refusing to upgrade" not in down.stderr
    assert "statement timeout" not in down.stderr
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert (
        _definition(test_db, scratch_schema),
        _windows(test_db, scratch_schema),
    ) == before
    again = _leg(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert again.returncode == 0, again.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]


# ---------------------------------------------------------------------------
# 7. An SDK request during the wait
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_an_sdk_request_waits_with_the_run_and_is_answered_once_it_ends(
    test_db, scratch_schema, tmp_path
):
    """One run from 0.48.0: once ``t2011_flag_environments`` has ``api_keys``,
    the run holds it until it ends. While this revision waits for ``metrics``
    (here held for longer than the bound), a new SDK request, which reads its
    key first, waits for ``api_keys``; the bound ends the run and the request
    is answered. So the most this revision adds to an SDK request's wait is
    its bound. The wait is printed, not asserted."""
    tree = _starting(test_db, CORE, scratch_schema, tmp_path, FROM_0_48_0)
    outcome: dict = {}
    with _holding(test_db, _reading(scratch_schema, TABLE)):
        with test_db.connect() as sdk:
            transaction = sdk.begin()
            sdk.execute(text("SET LOCAL statement_timeout = '60s'"))
            pid = sdk.execute(text("SELECT pg_backend_pid()")).scalar()

            def read_a_key() -> None:
                started = time.monotonic()
                try:
                    sdk.execute(text(_reading(scratch_schema, "api_keys")))
                    outcome["read"] = True
                except Exception as exc:  # reported by the assertion below
                    outcome["error"] = exc
                outcome["waited"] = time.monotonic() - started

            with _in_the_background(tree, scratch_schema, "upgrade", REVISION) as (
                process,
                name,
            ):
                _until_waiting_for(
                    test_db, process, name, _table_lock(scratch_schema, TABLE)
                )
                reading = threading.Thread(target=read_a_key)
                reading.start()
                waited_for = until_the_pid_waits(test_db, pid)
                stderr = process.communicate(timeout=PROCESS_SECONDS)[1]
                reading.join(timeout=PROCESS_SECONDS)
            transaction.rollback()
    print(f"the SDK-shaped read waited {outcome.get('waited', 0):.3f} s")
    assert waited_for == f"{scratch_schema}.api_keys", waited_for
    assert process.returncode != 0
    assert _refusal(stderr) == _load_revision(scratch_schema)._lock_refusal()
    assert outcome["read"] is True, outcome
    _nothing_applied(test_db, scratch_schema, FROM_0_48_0)


# ---------------------------------------------------------------------------
# 8. The downgrade
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_drops_the_column_and_every_window_and_nothing_else(
    profile, test_db, scratch_schema, tmp_path
):
    """It does not refuse while windows are stored: they go with the column,
    the check with them, and every other column, constraint and index of
    ``metrics``, and every row, is what it was."""
    tree = _at_this_revision(test_db, profile, scratch_schema, tmp_path)
    _insert_experiment(test_db, scratch_schema, {"a-day": 24, "a-year": 8760})
    columns, constraints, indexes = _definition(test_db, scratch_schema)
    rows = _stored(test_db, scratch_schema)
    _report(profile, "before the downgrade", test_db, scratch_schema)
    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after the downgrade", test_db, scratch_schema)
    assert f"Running downgrade {REVISION} -> {PREVIOUS_CORE_HEAD}" in down.stderr
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _definition(test_db, scratch_schema) == (
        {name: column for name, column in columns.items() if name != COLUMN},
        {c for c in constraints if c[0] != CHECK},
        indexes,
    )
    assert _stored(test_db, scratch_schema) == rows


@pytest.mark.regression
def test_a_run_past_t2012_with_a_split_in_use_refuses_and_keeps_the_windows(
    test_db, scratch_schema, tmp_path
):
    """One run to ``t2011_flag_environments`` undoes this revision first, then
    ``t2012_flag_split``, which refuses while a flag has a split: the run is
    rolled back, so ``metrics`` keeps the column, the check and its windows."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    _insert_experiment(test_db, scratch_schema, {"a-day": 24})
    _insert_flag(test_db, scratch_schema, "in-use", split=SPLIT)
    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", FLAG_ENVIRONMENTS)
    print(f"downgrade stderr tail:\n{down.stderr[-1500:]}")
    assert down.returncode != 0
    assert f"Running downgrade {REVISION} -> {PREVIOUS_CORE_HEAD}" in down.stderr
    assert "t2012_flag_split found 1 feature flag(s)" in _refusal(down.stderr)
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert _window_column(test_db, scratch_schema) == EXPECTED_COLUMN
    assert _check(test_db, scratch_schema) == EXPECTED_CHECK
    assert _windows(test_db, scratch_schema) == {"a-day": 24}


# ---------------------------------------------------------------------------
# 9. Offline mode
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_offline_mode_is_refused_in_both_directions_and_emits_nothing(
    test_db, scratch_schema, tmp_path
):
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    expected = _load_revision(scratch_schema)._OFFLINE_REFUSAL
    for argv in (
        ("downgrade", f"{REVISION}:{PREVIOUS_CORE_HEAD}", "--sql"),
        ("upgrade", f"{PREVIOUS_CORE_HEAD}:{REVISION}", "--sql"),
    ):
        done = tree_profiles.alembic(tree, scratch_schema, *argv)
        assert done.returncode != 0, argv
        assert expected in done.stderr, done.stderr[-3000:]
        assert "ALTER TABLE" not in done.stdout, done.stdout[-3000:]
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert _window_column(test_db, scratch_schema) == EXPECTED_COLUMN


# ---------------------------------------------------------------------------
# 10. This release on the downgraded schema
# ---------------------------------------------------------------------------
def _started_experiment(tree, schema: str, tmp_path, headers: dict) -> dict:
    suffix = uuid.uuid4().hex[:8]
    body = _experiment_body(suffix)
    ((status, created, _),) = _answers(
        tree, schema, tmp_path, [["POST", "/api/v1/experiments/", headers, body]]
    )
    assert status in (200, 201), created
    experiment = f"/api/v1/experiments/{created['id']}"
    ((status, started, _),) = _answers(
        tree, schema, tmp_path, [["POST", f"{experiment}/start", headers, None]]
    )
    assert status == 200, started
    return {"id": created["id"], "key": body["key"], "event": f"purchase_{suffix}"}


@pytest.mark.regression
@pytest.mark.parametrize("experiment_cache", ["off", "on"])
def test_this_release_answers_500_where_it_reads_a_metric_on_the_downgraded_schema(
    test_db, scratch_schema, tmp_path, monkeypatch, experiment_cache
):
    """Why the database-first rollback's paragraph names what fails: until
    every task of this release is replaced, every statement that loads a
    metric selects the column the downgrade dropped. The SDK routes read no
    metric, and are answered.

    Computing the results is what fails (``use_cache=false``). An answer the
    results cache took before the downgrade is served from it until it
    expires, by ``/results/{id}`` and by ``/experiments/{id}/results``: with
    the cache reachable (CI's core build runs a Redis), each answers 200 and
    the answer computed before; without one, it is computed, and answers 500.
    With ``CACHE_ENABLED`` on (off by default) and the cache reachable, the
    experiment routes' own cache answers what it holds too, which here is the
    experiment's daily results (``CACHED_ROUTES``)."""
    if experiment_cache == "on":
        # Inherited by the API's subprocess.
        monkeypatch.setenv("CACHE_ENABLED", "true")
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    signed_in = {"Authorization": f"Bearer {_token(user)}"}
    _, key = _insert_key(test_db, scratch_schema, user.id, "production", "sdk:ruleset")
    sdk = {"X-API-Key": key}
    _insert_flag(test_db, scratch_schema, "windowed-flag")
    running = _started_experiment(tree, scratch_schema, tmp_path, signed_in)
    experiment = f"/api/v1/experiments/{running['id']}"
    results = f"/api/v1/results/{running['id']}"
    assign = {"experiment_key": running["key"], "user_id": "u1"}
    track = {
        "experiment_key": running["key"],
        "user_id": "u1",
        "event_type": running["event"],
    }
    requests = {
        "the experiments": ["GET", "/api/v1/experiments/", signed_in, None],
        "one experiment": ["GET", experiment, signed_in, None],
        "its results": ["GET", f"{results}?use_cache=false", signed_in, None],
        "its results, cached": ["GET", results, signed_in, None],
        "its results under the experiment": [
            "GET",
            f"{experiment}/results",
            signed_in,
            None,
        ],
        "its daily results": ["GET", f"{results}/daily", signed_in, None],
        "its daily results under the experiment": [
            "GET",
            f"{experiment}/daily-results",
            signed_in,
            None,
        ],
        "assign": ["POST", "/api/v1/tracking/assign", sdk, assign],
        "track": ["POST", "/api/v1/tracking/track", sdk, track],
        "evaluate": _evaluate("windowed-flag", "u1", key),
        "ruleset": ["GET", "/api/v1/sdk/ruleset", sdk, None],
    }
    # Asked after the downgrade only: each would change what the others ask.
    once = {
        "create an experiment": [
            "POST",
            "/api/v1/experiments/",
            signed_in,
            _experiment_body(uuid.uuid4().hex[:8]),
        ],
        "change it": ["PUT", experiment, signed_in, {"description": "changed"}],
        "pause it": ["POST", f"{experiment}/pause", signed_in, None],
        "clone it": ["POST", f"{experiment}/clone", signed_in, None],
    }

    def answers(asked) -> dict[str, tuple]:
        answered = _answers(tree, scratch_schema, tmp_path, list(asked.values()))
        return dict(zip(asked, answered))

    before = answers(requests)
    assert {name: a[0] for name, a in before.items()} == dict.fromkeys(requests, 200)
    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    after = answers({**requests, **once})
    measured = {name: answer[0] for name, answer in after.items()}
    cached = _results_cache_is_reachable()
    print(
        f"on the downgraded schema (results cache reachable: {cached}, "
        f"CACHE_ENABLED {experiment_cache}): {measured}"
    )
    sdk_routes = ("assign", "track", "evaluate", "ruleset")
    served_cached = ["its results, cached", "its results under the experiment"]
    if experiment_cache == "on":
        served_cached += CACHED_ROUTES
    assert measured == {
        **dict.fromkeys([*requests, *once], 500),
        **dict.fromkeys(sdk_routes, 200),
        **dict.fromkeys(served_cached, 200 if cached else 500),
    }
    if cached:
        # The answers computed before the downgrade, as they were cached: the
        # results the request with use_cache=false computed, and the rest
        # as each was first answered.
        for name in served_cached:
            assert after[name][1] == before[name][1], name
        assert after["its results, cached"][1] == before["its results"][1]


#: What the experiment routes' cache (``CACHE_ENABLED``) still answers on the
#: downgraded schema, once it holds the answer from before.
CACHED_ROUTES = ["its daily results under the experiment"]


def _results_cache_is_reachable() -> bool:
    """Whether the results cache the client's API uses answers (the same
    settings, from the same environment)."""
    from backend.app.api.v1.endpoints.results import _get_cache_service

    return _get_cache_service().enabled


# ---------------------------------------------------------------------------
# 11. The heads
# ---------------------------------------------------------------------------
HEADS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_alembic_heads_are_this_revision_and_the_modules_head(profile, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    heads = tree_profiles.alembic(tree, "public", "heads")
    assert heads.returncode == 0, heads.stderr[-3000:]
    printed = {line.split(" ", 1)[0] for line in heads.stdout.split("\n") if line}
    print(f"[{profile}] alembic heads: {heads.stdout.strip()!r}")
    assert printed == HEADS[profile]


@pytest.mark.regression
def test_the_tree_pins_this_revision_as_the_core_head():
    assert tree_profiles.CORE_HEAD == CORE_HEAD
    assert re.fullmatch(r"[a-z0-9_]{1,32}", REVISION)
