"""The Amazon Redshift connector against a real account (founder-run; never in CI).

Run with ``WAREHOUSE_LIVE_TARGET=redshift`` and the variables the runbook
lists (``harness.RS_REQUIRED``)::

    pytest -m warehouse_live modules/backend/tests/live_warehouse/

The session does not start unless ``WAREHOUSE_LIVE_RS_USAGE_LIMIT`` names the
usage limits set on the workgroup and on the cluster.  Before any statement
runs, the check reads both back from AWS and stops unless each named limit
it expects is there.  Every statement is stopped at the connection's limit
(at most 120 s).

The identity the customer's role is assumed from is read from
``WAREHOUSE_LIVE_RS_AWS_*``: the test session replaces every other AWS
credential with a dummy.  Every AWS call the connector makes is recorded
(:class:`~.harness.AwsRecording`); STS answers without their body.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, Tuple

import pytest

from modules.backend.app.core.warehouse_identifiers import (
    REDSHIFT,
    render_column,
    render_table,
)
from modules.backend.app.services.warehouse_clients import (
    NUMERIC_TYPES,
    TIME_TYPES,
    WarehouseClient,
)
from modules.backend.app.services.warehouse_query_builder import (
    REDSHIFT_SQL,
    AnalysisWindow,
    AssignmentMapping,
    BuiltQuery,
    MetricMapping,
    build_metric_query,
)
from modules.backend.app.services.warehouse_sufficient_stats import (
    parse_float,
    parse_metric_rows,
)
from modules.backend.app.warehouse import aws, redshift
from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode
from modules.backend.app.warehouse.redshift import RedshiftAdapter, RedshiftConnection
from modules.backend.tests.live_warehouse import harness

pytestmark = pytest.mark.warehouse_live

#: A name no workgroup or cluster in the account has (both patterns allow it).
MISSING_NAME = "controlvar-live-check-missing"
#: A TIMESTAMP on the day US Eastern time moves to daylight time; read as
#: UTC it is this epoch second, and in any other zone it is not.
DST_TIMESTAMP = "2026-03-08 02:30:00"
DST_EPOCH = calendar.timegm(datetime(2026, 3, 8, 2, 30).timetuple())


@dataclass
class RedshiftSession:
    config: Any
    platform: Any
    serverless: RedshiftAdapter
    cluster: RedshiftAdapter
    client: WarehouseClient
    cluster_client: WarehouseClient
    exposures: Tuple[str, str]
    events: Tuple[str, str]
    columns: Dict[str, Dict[str, str]]

    def deadline(self, seconds: float = 0) -> Deadline:
        return Deadline(seconds or self.config.query_timeout_seconds + 30)


def _connection(config, timeout: int, **shape: Any) -> RedshiftConnection:
    if not shape:
        shape = {"workgroup_name": config.workgroup}
    return RedshiftConnection(
        region=config.region,
        role_arn=config.role_arn,
        external_id=config.external_id,
        database=config.database,
        query_timeout_seconds=timeout,
        **shape,
    )


def _cluster_shape(config, cluster: str = "") -> Dict[str, str]:
    return {"cluster_identifier": cluster or config.cluster, "db_user": config.db_user}


def _adapter(config, platform, timeout: int, **shape: Any) -> RedshiftAdapter:
    return RedshiftAdapter(
        _connection(config, timeout, **shape), platform_session=platform
    )


def _client(adapter: RedshiftAdapter) -> WarehouseClient:
    return WarehouseClient(
        "redshift",
        adapter,
        REDSHIFT_SQL,
        TIME_TYPES["redshift"],
        NUMERIC_TYPES["redshift"],
    )


def _verify_usage_limits(platform, config, recorder) -> None:
    """Refuse to go on unless a named usage limit is on the workgroup and one on
    the cluster, as AWS itself reports them."""
    serverless = platform.client("redshift-serverless", region_name=config.region)
    workgroup = serverless.get_workgroup(workgroupName=config.workgroup)["workgroup"]
    on_workgroup = [
        u.get("usageLimitId")
        for u in serverless.list_usage_limits(
            resourceArn=workgroup["workgroupArn"]
        ).get("usageLimits", [])
    ]
    provisioned = platform.client("redshift", region_name=config.region)
    on_cluster = [
        u.get("UsageLimitId")
        for u in provisioned.describe_usage_limits(
            ClusterIdentifier=config.cluster
        ).get("UsageLimits", [])
    ]
    recorder.record(
        "rs_spend_cap",
        declared=list(config.usage_limits),
        workgroup=on_workgroup,
        cluster=on_cluster,
    )
    declared = set(config.usage_limits)
    for shape, found in (("workgroup", on_workgroup), ("cluster", on_cluster)):
        if not declared & set(found):
            raise AssertionError(
                f"none of the usage limits named in {harness.ENV_RS_USAGE_LIMIT} is "
                f"on the {shape} (AWS lists {found or 'none'}). Set one and read it "
                "back with the AWS CLI. Nothing else has run."
            )


@pytest.fixture(scope="session")
def rs(live_config, recorder, current_check) -> Iterator[RedshiftSession]:
    recording = harness.AwsRecording(recorder, current_check)
    recording.install()
    try:
        platform = live_config.platform_session()
        _verify_usage_limits(platform, live_config, recorder)
        timeout = live_config.query_timeout_seconds
        serverless = _adapter(live_config, platform, timeout)
        cluster = _adapter(
            live_config, platform, timeout, **_cluster_shape(live_config)
        )
        yield RedshiftSession(
            config=live_config,
            platform=platform,
            serverless=serverless,
            cluster=cluster,
            client=_client(serverless),
            cluster_client=_client(cluster),
            exposures=(live_config.schema, harness.EXPOSURES_TABLE),
            events=(live_config.schema, harness.EVENTS_TABLE),
            columns={},
        )
    finally:
        recording.uninstall()


@pytest.fixture(scope="session")
def fixture_arrays():
    return harness.live_fixture()


@pytest.fixture(scope="session")
def reference(fixture_arrays):
    return harness.exact_reference(*fixture_arrays)


@pytest.fixture(scope="session")
def columns(rs, recorder) -> Dict[str, Dict[str, str]]:
    if not rs.columns:
        for name, table, wanted in (
            ("exposures", rs.exposures, harness.EXPOSURE_COLUMNS),
            ("events", rs.events, harness.EVENT_COLUMNS),
        ):
            reported = rs.serverless.table_columns(table, rs.deadline())
            recorder.record("rs_table_columns", **{name: [list(c) for c in reported]})
            rs.columns[name] = harness.resolve_fixture_columns(
                rs.client, reported, wanted
            )
    return rs.columns


def _built(sql: str, tables=frozenset()) -> BuiltQuery:
    return BuiltQuery(kind="preview", dialect="redshift", sql=sql, tables=tables)


def _batch(adapter: RedshiftAdapter, sqls, seconds: float = 90) -> Tuple[Any, str, Any]:
    """Send a batch of our choosing and wait for it: (session, id, what it ended as)."""
    session, _ = adapter._role_session(seconds + 60, Deadline(seconds))
    statement_id = adapter._submit(session, list(sqls), Deadline(seconds))
    seen = adapter._wait(session, statement_id, Deadline(seconds))
    return session, statement_id, seen


def _refusal(exc: WarehouseError) -> Dict[str, Any]:
    return {
        "refused": exc.code.value,
        "vendor_code": exc.vendor_code,
        "request_id": exc.request_id,
    }


# -- the checks, in order -------------------------------------------------------------


def test_rs_spend_cap(rs, recorder):
    """A named usage limit is on the workgroup and on the cluster (the fixture)."""
    seen = recorder.checks["rs_spend_cap"]
    declared = set(seen["declared"])
    assert declared & set(seen["workgroup"]) and declared & set(seen["cluster"])


def test_rs_trust_requires_external_id(rs, recorder):
    """STS refuses the role to this identity without the connection's external ID."""
    refused = None
    try:
        aws.call(
            rs.platform,
            "sts",
            rs.config.region,
            "assume_role",
            warehouse="redshift",
            deadline=Deadline(30),
            codes=aws.STS_CODES,
            RoleArn=rs.config.role_arn,
            RoleSessionName=aws.ROLE_SESSION_NAME,
            # What the connector asks for, so the refusal recorded is for its request.
            DurationSeconds=redshift.SESSION_SECONDS,
        )
    except WarehouseError as exc:
        refused = exc
    recorder.record(
        "rs_trust_requires_external_id",
        assumed_without_it=refused is None,
        **(_refusal(refused) if refused else {}),
    )
    assert refused is not None, "the role was assumed without the external ID"
    assert refused.code is WarehouseErrorCode.AUTH_FAILED
    assert refused.vendor_code == "AccessDenied"


def _sign_in(adapter, recorder, check: str) -> None:
    result = adapter.check_connection(Deadline(30))
    recorder.record(
        check,
        probe=result.probe,
        time_zone=result.time_zone,
        warehouse=result.warehouse,
    )
    assert parse_float(result.probe, "probe")[0] == harness.WIRE_PROBE_VALUE
    assert result.time_zone == redshift.PROBE_TIME_ZONE


def test_rs_sign_in(rs, recorder):
    """The role is assumed and a batch runs in the Serverless workgroup."""
    _sign_in(rs.serverless, recorder, "rs_sign_in")


def test_rs_sign_in_cluster(rs, recorder):
    """The same through the provisioned cluster and its database user."""
    _sign_in(rs.cluster, recorder, "rs_sign_in_cluster")


def test_rs_wire_probe(rs, recorder):
    """With extra_float_digits 2 the probe reads back exactly; without it, it
    does not -- so the probe can fail, and catches a SET that did not hold."""
    with_set = rs.serverless.check_connection(Deadline(30)).probe
    session, statement_id, seen = _batch(
        rs.serverless, ["SET timezone TO 'UTC'", redshift.PROBE_SQL]
    )
    without: Any = None
    if seen["Status"] == "FINISHED":
        _, rows = rs.serverless._result(session, f"{statement_id}:2", Deadline(60))
        without = rows[0].get("probe") if rows else None
    recorder.record(
        "rs_wire_probe",
        with_set=with_set,
        without_set=without,
        without_status=seen["Status"],
        statement_id=statement_id,
    )
    assert parse_float(with_set, "probe")[0] == harness.WIRE_PROBE_VALUE
    assert without is not None, seen
    assert parse_float(without, "probe")[0] != harness.WIRE_PROBE_VALUE, without


def test_rs_timezone(rs, recorder):
    """The session's zone reads back as UTC, and a TIMESTAMP on a DST day is
    read as UTC (TIMESTAMP to TIMESTAMPTZ, as the generated SQL casts it)."""
    sql = (
        "SELECT CAST(FLOOR(EXTRACT(EPOCH FROM CAST(CAST("
        f"'{DST_TIMESTAMP}' AS TIMESTAMP) AS TIMESTAMPTZ))) AS BIGINT) AS v"
    )
    executed = rs.serverless._execute(sql, rs.deadline())
    value = executed.rows[0]["v"] if executed.rows else None
    recorder.record(
        "rs_timezone",
        sql=sql,
        epoch=value,
        expected=DST_EPOCH,
        time_zone=executed.time_zone,
        statement_id=executed.statement_id,
    )
    assert executed.time_zone == "UTC"
    assert value == DST_EPOCH


def test_rs_invalid_set_fails_batch(rs, recorder):
    """A SET Redshift refuses fails the whole batch: the probe after it does
    not run (ExecutionMode TRANSACTION)."""
    _, statement_id, seen = _batch(
        rs.serverless,
        ["SET timezone TO 'UTC'", "SET extra_float_digits TO 99", redshift.PROBE_SQL],
    )
    statuses = [s.get("Status") for s in seen["SubStatements"]]
    recorder.record(
        "rs_invalid_set_fails_batch",
        status=seen["Status"],
        sub_statuses=statuses,
        statement_id=statement_id,
    )
    assert seen["Status"] == "FAILED", seen
    assert len(statuses) == 3 and statuses[2] != "FINISHED", statuses


def test_rs_table_columns(columns, recorder):
    """DescribeTable's type names are ones the product accepts (recorded)."""
    recorder.record("rs_table_columns", resolved=columns)
    assert set(columns["exposures"]) == set(harness.EXPOSURE_COLUMNS)
    assert set(columns["events"]) == set(harness.EVENT_COLUMNS)


def test_rs_upper_case_column(rs, columns, recorder):
    """A column typed in upper case is matched to the table's own name, which
    the product stores and renders; whether Redshift folds a quoted upper-case
    name itself is recorded."""
    reported = rs.serverless.table_columns(rs.exposures, rs.deadline())
    upper = [name.upper() for name in harness.EXPOSURE_COLUMNS]
    resolved = harness.resolve_fixture_columns(rs.client, reported, upper)
    quoted = render_column(REDSHIFT, upper[0], "unit_id")
    table = render_table(REDSHIFT, rs.exposures)
    folded: Dict[str, Any] = {}
    try:
        result = rs.serverless.run_query(
            _built(
                f"SELECT COUNT({quoted}) AS n FROM {table}", frozenset({rs.exposures})
            ),
            rs.deadline(),
        )
        folded = {"ran": True, "n": result.rows[0].get("n") if result.rows else None}
    except WarehouseError as exc:
        folded = {"ran": False, **_refusal(exc)}
    recorder.record("rs_upper_case_column", resolved=resolved, quoted_upper=folded)
    assert {k.lower(): v for k, v in resolved.items()} == columns["exposures"]


def test_rs_load_fidelity(rs, columns, fixture_arrays, recorder):
    """The loaded amounts are exactly the fixture's binary64 values (a few spot rows)."""
    control, treatment = fixture_arrays
    wanted = {
        "control-0": float(control[0]),
        "control-1": float(control[1]),
        "treatment-0": float(treatment[0]),
        f"treatment-{len(treatment) - 1}": float(treatment[-1]),
    }
    e = columns["events"]
    unit = render_column(REDSHIFT, e["user_id"], "user_id")
    amount = render_column(REDSHIFT, e["amount"], "amount")
    ids = ", ".join(f"'{u}'" for u in wanted)  # fixed ids of [a-z0-9-]
    sql = (
        f"SELECT CAST({unit} AS {REDSHIFT_SQL.string_type}) AS unit_id, "
        f"{REDSHIFT_SQL.serialise(amount)} AS amount "
        f"FROM {render_table(REDSHIFT, rs.events)} WHERE {unit} IN ({ids})"
    )
    result = rs.serverless.run_query(_built(sql, frozenset({rs.events})), rs.deadline())
    seen = {row["unit_id"]: row["amount"] for row in result.rows}
    recorder.record("rs_load_fidelity", rows=seen, statement_id=result.statement_handle)
    assert set(seen) == set(wanted)
    for unit_id, expected in wanted.items():
        assert parse_float(seen[unit_id], "amount")[0] == expected, unit_id


def test_rs_mean_parity(rs, columns, reference, recorder):
    """F-MEAN-1 through CAST(x AS VARCHAR) with extra_float_digits 2, within rel 1e-12."""
    a, e = columns["exposures"], columns["events"]
    built = build_metric_query(
        REDSHIFT_SQL,
        AssignmentMapping(
            rs.exposures,
            a["user_id"],
            a["experiment_key"],
            a["variant"],
            a["exposed_at"],
        ),
        MetricMapping(rs.events, e["user_id"], e["event_at"], "mean", e["amount"]),
        harness.EXPERIMENT_KEY,
        AnalysisWindow(harness.WINDOW_START, harness.WINDOW_END),
    )
    result = rs.serverless.run_query(built, rs.deadline())
    stats = parse_metric_rows(list(result.rows))
    errors = harness.mean_parity_errors(stats, reference)
    worst = max(errors.values())
    recorder.record(
        "rs_mean_parity",
        tolerance=harness.F_MEAN_1_TOLERANCE,
        worst_relative_error=worst,
        relative_errors=errors,
        k_text=stats.k_text,
        variants=[
            {
                "variant": v.variant,
                "n": v.n,
                "sum_d": v.sum_d_text,
                "sum_d2": v.sum_d2_text,
            }
            for v in stats.variants
        ],
        sql_sha256=built.sha256,
        statement_id=result.statement_handle,
    )
    assert worst <= harness.F_MEAN_1_TOLERANCE, errors


def _runner_parity(rs, client, columns, fixture_arrays, recorder, check: str) -> None:
    plan = harness.run_plan(client, rs.exposures, rs.events, columns)
    real = harness.execute_plan(
        plan, Deadline(3 * rs.config.query_timeout_seconds + 60)
    )
    oracle_client, o_exposures, o_events, o_columns = harness.duckdb_oracle(
        *fixture_arrays
    )
    oracle = harness.execute_plan(
        harness.run_plan(oracle_client, o_exposures, o_events, o_columns), Deadline(600)
    )
    problems = harness.compare_results(real["results"], oracle["results"])
    recorder.record(
        check,
        results=real["results"],
        job_metadata=real["job_metadata"],
        differences=problems,
    )
    assert not problems, problems


def test_rs_runner_parity(rs, columns, fixture_arrays, recorder):
    """The run executor over the workgroup equals it over the DuckDB oracle."""
    _runner_parity(rs, rs.client, columns, fixture_arrays, recorder, "rs_runner_parity")


def test_rs_runner_parity_cluster(rs, columns, fixture_arrays, recorder):
    """The same over the provisioned cluster."""
    _runner_parity(
        rs,
        rs.cluster_client,
        columns,
        fixture_arrays,
        recorder,
        "rs_runner_parity_cluster",
    )


def test_rs_statement_name_in_history(rs, recorder):
    """Our batches carry StatementName; the customer can find them by it in
    SYS_QUERY_HISTORY.  ListStatements by the name is recorded too (it needs
    redshift-data:ListStatements, which the connector itself does not)."""
    by_api: Dict[str, Any] = {}
    try:
        listed = aws.call(
            rs.serverless._role_session(120, Deadline(60))[0],
            "redshift-data",
            rs.config.region,
            "list_statements",
            warehouse="redshift",
            deadline=Deadline(60),
            codes=redshift.REDSHIFT_CODES,
            StatementName=redshift.STATEMENT_NAME,
            Status="ALL",
        )
        by_api = {"found": len(listed.get("Statements", []))}
    except WarehouseError as exc:
        by_api = _refusal(exc)
    sql = (
        "SELECT COUNT(*) AS n FROM sys_query_history "
        f"WHERE query_label = '{redshift.STATEMENT_NAME}'"
    )
    history: Dict[str, Any] = {}
    try:
        result = rs.serverless.run_query(_built(sql), rs.deadline())
        history = {"label_rows": result.rows[0].get("n") if result.rows else None}
    except WarehouseError as exc:
        history = _refusal(exc)
    recorder.record(
        "rs_statement_name_in_history",
        list_statements=by_api,
        sys_query_history=history,
        sql=sql,
    )
    assert (history.get("label_rows") or 0) > 0, history


def test_rs_missing_table(rs, recorder):
    """DescribeTable on a table that does not exist: the source check's
    object_not_found (the API's 422 identifier_not_found)."""
    refused = None
    try:
        rs.serverless.table_columns(
            (rs.config.schema, harness.PROBE_TABLE), rs.deadline()
        )
    except WarehouseError as exc:
        refused = exc
    recorder.record(
        "rs_missing_table", **(_refusal(refused) if refused else {"found": True})
    )
    assert refused is not None
    assert refused.code is WarehouseErrorCode.OBJECT_NOT_FOUND, _refusal(refused)


def test_rs_missing_grant(rs, recorder):
    """A relation the role may not read: the batch FAILS, reported as FAILED
    with the failed statement's query id (the Code is recorded)."""
    refused = None
    try:
        rs.serverless.run_query(
            _built("SELECT COUNT(*) AS n FROM pg_user_info"), rs.deadline()
        )
    except WarehouseError as exc:
        refused = exc
    history: Dict[str, Any] = {}
    if refused is not None and (refused.request_id or "").isdigit():
        # The id the connector logs for a FAILED statement, looked up where
        # the Troubleshooting docs will send an operator.
        sql = (
            "SELECT COUNT(*) AS n FROM sys_query_history "
            f"WHERE query_id = {int(refused.request_id)}"
        )
        try:
            found = rs.serverless.run_query(_built(sql), rs.deadline())
            history = {"rows": found.rows[0].get("n") if found.rows else None}
        except WarehouseError as exc:
            history = _refusal(exc)
    recorder.record(
        "rs_missing_grant",
        sys_query_history=history,
        **(_refusal(refused) if refused else {"read": True}),
    )
    assert refused is not None, "the role read pg_user_info"
    assert refused.code is WarehouseErrorCode.UNRECOGNISED_WAREHOUSE_ERROR
    assert refused.vendor_code == "FAILED" and refused.request_id
    assert (history.get("rows") or 0) > 0, history


def _missing_target(rs, recorder, check: str, **shape: Any) -> None:
    adapter = _adapter(rs.config, rs.platform, rs.config.query_timeout_seconds, **shape)
    refused = None
    try:
        adapter.check_connection(Deadline(30))
    except WarehouseError as exc:
        refused = exc
    recorder.record(check, **(_refusal(refused) if refused else {"ran": True}))
    assert refused is not None, f"a batch ran on {MISSING_NAME}"


def test_rs_missing_workgroup(rs, recorder):
    """A workgroup that does not exist: refused; its Code is recorded."""
    _missing_target(rs, recorder, "rs_missing_workgroup", workgroup_name=MISSING_NAME)


def test_rs_missing_cluster(rs, recorder):
    """A cluster that does not exist: refused; its Code is recorded."""
    _missing_target(
        rs, recorder, "rs_missing_cluster", **_cluster_shape(rs.config, MISSING_NAME)
    )


def test_rs_create_refused(rs, recorder):
    """The connection's role cannot create a table in the fixture's schema."""
    table = render_table(REDSHIFT, (rs.config.schema, harness.PROBE_TABLE))
    sql = f"CREATE TABLE {table} (x INT)"
    _, statement_id, seen = _batch(rs.serverless, [sql])
    recorder.record(
        "rs_create_refused", sql=sql, status=seen["Status"], statement_id=statement_id
    )
    assert seen["Status"] == "FAILED", (
        f"the role CREATED {table}; drop it and revoke what allowed it"
    )


def test_rs_time_limit(rs, recorder):
    """At the connection's limit the connector cancels the batch, and Redshift
    reports it ended."""
    short = _adapter(rs.config, rs.platform, harness.RS_TAMPER_TIMEOUT_SECONDS)
    events = render_table(REDSHIFT, rs.events)
    sql = (
        f"SELECT COUNT(*) AS n FROM {events} AS a CROSS JOIN {events} AS b "
        f"CROSS JOIN {events} AS c"
    )
    before = len(recorder.exchanges)
    started = time.monotonic()
    with pytest.raises(WarehouseError) as stopped:
        short.run_query(_built(sql, frozenset({rs.events})), Deadline(90))
    waited = time.monotonic() - started
    ids = [
        x["body"]["Id"]
        for x in recorder.exchanges[before:]
        if x["request"].get("operation") == "BatchExecuteStatement"
        and isinstance(x.get("body"), dict)
        and isinstance(x["body"].get("Id"), str)
    ]
    ended: Dict[str, Any] = {}
    if ids:
        session, _ = short._role_session(120, Deadline(60))
        for _ in range(6):
            seen = redshift.described(
                aws.call(
                    session,
                    "redshift-data",
                    rs.config.region,
                    "describe_statement",
                    warehouse="redshift",
                    deadline=Deadline(30),
                    codes=redshift.REDSHIFT_CODES,
                    Id=ids[0],
                ),
                ids[0],
            )
            ended = {"status": seen["Status"]}
            if seen["Status"] not in ("SUBMITTED", "PICKED", "STARTED"):
                break
            time.sleep(5)
    recorder.record(
        "rs_time_limit",
        timeout_seconds=harness.RS_TAMPER_TIMEOUT_SECONDS,
        sql=sql,
        stopped=stopped.value.code.value,
        vendor_code=stopped.value.vendor_code,
        seconds_waited=round(waited, 1),
        statement_id=ids[0] if ids else None,
        ended=ended,
    )
    assert stopped.value.code is WarehouseErrorCode.TIME_LIMIT
    assert ids, "no statement id was recorded"
    assert ended.get("status") in ("ABORTED", "FAILED"), ended
