"""The Amazon Redshift connector: the Data API, the customer's role, and limits.

Disabled (:mod:`.connectors`): it is enabled by its own change, once the check
against a real Redshift (``live_warehouse/test_redshift_live.py``) has passed.

How it reaches Redshift
-----------------------
Only through the Redshift Data API (``redshift-data``), with the boto3 clients
:func:`~.aws.call` builds: the regional endpoint
``redshift-data.<region>.amazonaws.com`` on 443, never the cluster's or the
workgroup's own address, so no network path to the warehouse is needed and
none is opened.  Every call is signed with the customer's IAM role, assumed by
:func:`~.aws.assume_role_session` with the connection's external ID.  No
password and no Secrets Manager ARN is stored or sent.

The connection
--------------
:class:`RedshiftConnection`: the region, the role, the database, and exactly
one of a Serverless workgroup (the Data API's ``WorkgroupName``) or a
provisioned cluster with its database user (``ClusterIdentifier`` and
``DbUser``).  The workgroup, the cluster, the database user and the database
are checked again here, by the request schemas' own field types, and the
region against :data:`~.aws.AWS_REGIONS`; the role ARN and the external ID
are checked by :func:`~.aws.assume_role_session` before any call.

Statements
----------
Each statement is one ``BatchExecuteStatement`` of five SQL statements, run as
one transaction (``ExecutionMode="TRANSACTION"``), with JSON results and a
fixed ``StatementName`` (:data:`STATEMENT_NAME`) the customer can find in their
query history:

1. ``SET timezone TO 'UTC'``;
2. ``SET extra_float_digits TO 2``, so a DOUBLE PRECISION cast to text has 17
   significant digits -- Redshift has no format function that names them;
3. ``SET statement_timeout TO <ms>``: Redshift's own stop, set
   :data:`STATEMENT_TIMEOUT_MARGIN_SECONDS` past ours, for a statement whose
   id was never learned;
4. the probe (:data:`PROBE_SQL`): ``0.1 + 0.2`` in DOUBLE PRECISION, cast to
   text, and the session's time zone.  It must read back as exactly
   ``0.30000000000000004`` (else ``result_invalid``) and ``UTC`` (else
   ``timezone_not_utc``), so a session whose settings did not hold is refused
   on every run and every connection test;
5. the statement itself.

The batch must report exactly five sub-statements; the probe is read from
``<id>:4`` and the result from ``<id>:5``.  No session is kept between batches
(no ``SessionKeepAliveSeconds`` or ``SessionId``), and the Data API is never
asked to wait (``WaitTimeSeconds``): the batch is polled with
``DescribeStatement``.

Limits
------
A statement runs for at most the connection's ``query_timeout_seconds``, or
less if less of the operation's total is left (:data:`CANCEL_RESERVE_SECONDS`
is kept back to cancel in), or :data:`MAX_STATEMENT_SECONDS` if that is less.
When its limit passes before Redshift reports how it ended,
``CancelStatement`` is sent once and the statement fails with ``time_limit``;
any other failure while it may still be running sends it too.

The role's session
------------------
The role's credentials are asked for :data:`SESSION_SECONDS` (an hour: the
longest STS gives a role assumed with another role's credentials, and no
role's maximum session is set below it), not STS's 900 s default: the
deployment's default ceiling for a query is 1800 s.  An operation -- a
connection test, a column listing, a preview, a run -- uses one session while
it has the next statement's limit and :data:`SESSION_RESERVE_SECONDS` left,
and assumes the role again when it has not; so no call is signed with
credentials that have run out.  For the same reason a statement's limit is
never more than a session less that reserve (:data:`MAX_STATEMENT_SECONDS`),
which binds only if an operator raises ``WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS``
above it.  The credentials are held by the adapter, which lives for one
operation, and are never logged or shown in a repr.

Outcomes
--------
``FINISHED`` is read.  ``ABORTED`` is ``cancelled``.  ``FAILED`` is
``unrecognised_warehouse_error`` with ``vendor_code`` ``FAILED`` and, as the
request id (logged, not returned), the ``RedshiftQueryId`` of the
sub-statement that failed, for an operator to look the query up in Redshift's
query history.  The ``Error`` text Redshift returns
with a failed statement is never read: :func:`described` copies the fields it
uses and nothing else.

Results
-------
``GetStatementResult`` is read page by page (``NextToken``), up to
:data:`MAX_RESULT_PAGES` pages and :data:`MAX_RESULT_ROWS` rows.  A column name
is an identifier, read in lower case; two columns with one name are refused.
Every value must be ``isNull``, ``longValue`` (a count, an epoch second) or
``stringValue`` (a label, or a number our SQL wrote as text); anything else,
a ``doubleValue`` included, is ``result_invalid``.

Errors
------
An AWS error maps by its ``Code`` (:data:`REDSHIFT_CODES`), then by the HTTP
status (:func:`~.errors.error_for_status`), with the ``Code`` kept as the
vendor code.  The parser gives the Codes with their ``Exception`` suffix.
``ResourceNotFoundException`` names our own statement id, so it is never
``object_not_found``.  No message text from AWS or Redshift is read into
anything we raise, return or log.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Final, List, Mapping, Optional, Tuple

import boto3
from pydantic import TypeAdapter, ValidationError

from modules.backend.app.core.warehouse_identifiers import (
    REDSHIFT,
    validate_table_parts,
)
from modules.backend.app.schemas.warehouse_connections import (
    RedshiftCluster,
    RedshiftDatabase,
    RedshiftDbUser,
    RedshiftWorkgroup,
)
from modules.backend.app.services.warehouse_query_builder import BuiltQuery
from modules.backend.app.services.warehouse_sufficient_stats import (
    WarehouseResultRefused,
    parse_float,
)
from modules.backend.app.warehouse import aws
from modules.backend.app.warehouse.deadlines import Deadline, Sleeper
from modules.backend.app.warehouse.egress import Resolver
from modules.backend.app.warehouse.errors import (
    WarehouseError,
    WarehouseErrorCode,
    log_warehouse_error,
    sanitised,
)

WAREHOUSE: Final = "redshift"
#: The one AWS service this connector calls besides STS.
SERVICE: Final = "redshift-data"

#: Every batch's name, which the customer's query history shows.
STATEMENT_NAME: Final = "controlvar-analysis"
#: The session settings every batch starts with.
SESSION_SETUP: Final = ("SET timezone TO 'UTC'", "SET extra_float_digits TO 2")
#: The probe: sub-statement 4 of every batch.
PROBE_SQL: Final = (
    "SELECT CAST(CAST(0.1 AS DOUBLE PRECISION) + CAST(0.2 AS DOUBLE PRECISION)"
    " AS VARCHAR) AS probe, current_setting('timezone') AS tz"
)
#: What the probe must read back as: binary64's 0.1 + 0.2, and UTC.
PROBE_VALUE: Final = 0.1 + 0.2
PROBE_TIME_ZONE: Final = "UTC"
#: The connection test's statement: it reads no table.
CONNECTION_TEST_SQL: Final = "SELECT 1 AS connected"
#: A batch's sub-statements, and where the probe and the result are.
SUB_STATEMENTS: Final = 5
PROBE_INDEX: Final = 4
RESULT_INDEX: Final = 5

#: How long the role's credentials are asked for.
SESSION_SECONDS: Final = aws.ROLE_SESSION_MAX_SECONDS
#: A session is used for a statement only with its limit and this much left.
SESSION_RESERVE_SECONDS: Final = 60
#: The longest a statement may run: a whole session less the reserve.
MAX_STATEMENT_SECONDS: Final = SESSION_SECONDS - SESSION_RESERVE_SECONDS
#: Kept back from the operation's total, to cancel in.
CANCEL_RESERVE_SECONDS: Final = 5.0
#: Redshift's own statement_timeout is this much past ours.
STATEMENT_TIMEOUT_MARGIN_SECONDS: Final = 10
#: Waits between status polls, in order; the last one repeats.
POLL_PAUSES_SECONDS: Final = (0.25, 0.5, 1.0, 2.0)
#: Result pages and rows read for one sub-statement (a result is at most 51
#: rows), and column pages for one table.
MAX_RESULT_PAGES: Final = 5
MAX_RESULT_ROWS: Final = 10_000
MAX_COLUMN_PAGES: Final = 5
COLUMNS_PER_PAGE: Final = 1000

_C = WarehouseErrorCode

#: The Data API's ``Code`` -> ours.  Every other Code maps by HTTP status.
REDSHIFT_CODES: Final[Mapping[str, WarehouseErrorCode]] = {
    "ActiveStatementsExceededException": _C.WAREHOUSE_BUSY,
    "ActiveSessionsExceededException": _C.WAREHOUSE_BUSY,
    "ActiveWaitingRequestsExceededException": _C.WAREHOUSE_BUSY,
    "QueryTimeoutException": _C.TIME_LIMIT,
    # It names our statement id, not a table: never object_not_found.
    "ResourceNotFoundException": _C.UNRECOGNISED_WAREHOUSE_ERROR,
    # Not in botocore's model; spelt as AWS's JSON services spell them, until
    # the check against a real Redshift records what it sends.
    "ThrottlingException": _C.WAREHOUSE_BUSY,
    "AccessDeniedException": _C.PERMISSION_DENIED,
}
#: The Codes above that botocore's redshift-data model does not have.
UNMODELLED_CODES: Final = ("ThrottlingException", "AccessDeniedException")

_RUNNING: Final = frozenset({"SUBMITTED", "PICKED", "STARTED"})
_ENDED: Final = frozenset({"FINISHED", "FAILED", "ABORTED"})

_STATEMENT_ID: Final = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_COLUMN_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,126}")
#: A DescribeTable ``typeName``: words, and an optional (precision[,scale]).
_TYPE_NAME: Final = re.compile(
    r"[A-Za-z][A-Za-z0-9_ ]{0,62}(\([0-9]{1,5}(,[0-9]{1,5})?\))?"
)
#: The request schemas' own types for these fields, so the rules are theirs.
_WORKGROUP: Final = TypeAdapter(RedshiftWorkgroup)
_CLUSTER: Final = TypeAdapter(RedshiftCluster)
_DATABASE: Final = TypeAdapter(RedshiftDatabase)
_DB_USER: Final = TypeAdapter(RedshiftDbUser)
_MAX_TOKEN_CHARS: Final = 8192


def _fullmatch(pattern: "re.Pattern[str]", value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _schema_valid(field_type: "TypeAdapter[str]", value: object) -> bool:
    """Whether ``value`` is a str the request schemas would accept for the field."""
    if not isinstance(value, str):
        return False
    try:
        field_type.validate_python(value, strict=True)
    except ValidationError:
        return False
    return True


def _invalid() -> WarehouseError:
    return WarehouseError(_C.RESULT_INVALID, warehouse=WAREHOUSE)


def _internal() -> WarehouseError:
    return WarehouseError(_C.INTERNAL, warehouse=WAREHOUSE)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def statement_timeout_sql(limit_seconds: int) -> str:
    """``SET statement_timeout TO <ms>``: our limit plus the margin, never 0."""
    if isinstance(limit_seconds, bool) or not isinstance(limit_seconds, int):
        raise _internal()
    if limit_seconds < 1:
        raise _internal()
    milliseconds = (limit_seconds + STATEMENT_TIMEOUT_MARGIN_SECONDS) * 1000
    return f"SET statement_timeout TO {milliseconds}"


def batch_sqls(select_sql: str, limit_seconds: int) -> List[str]:
    """The five statements of a batch, in order."""
    return [
        *SESSION_SETUP,
        statement_timeout_sql(limit_seconds),
        PROBE_SQL,
        select_sql,
    ]


# -- the connection -----------------------------------------------------------


@dataclass(frozen=True, repr=False)
class RedshiftConnection:
    """One Redshift connection's non-secret parameters and limit.

    Anything that does not pass its pattern is ``internal``: the request
    schemas checked these values before they were stored.
    """

    region: str
    role_arn: str
    external_id: str
    database: str
    query_timeout_seconds: int
    workgroup_name: Optional[str] = None
    cluster_identifier: Optional[str] = None
    db_user: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.region, str) or self.region not in aws.AWS_REGIONS:
            raise _internal()
        if not isinstance(self.role_arn, str) or not isinstance(self.external_id, str):
            raise _internal()
        if not _schema_valid(_DATABASE, self.database):
            raise _internal()
        if self.workgroup_name is not None:
            if self.cluster_identifier is not None or self.db_user is not None:
                raise _internal()
            if not _schema_valid(_WORKGROUP, self.workgroup_name):
                raise _internal()
        elif not (
            _schema_valid(_CLUSTER, self.cluster_identifier)
            and _schema_valid(_DB_USER, self.db_user)
        ):
            raise _internal()
        value = self.query_timeout_seconds
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _internal()

    @property
    def serverless(self) -> bool:
        return self.workgroup_name is not None

    @property
    def location(self) -> str:
        """The workgroup's or the cluster's name."""
        return str(self.workgroup_name or self.cluster_identifier)

    def target(self) -> Dict[str, str]:
        """The Data API parameters that say where a statement runs."""
        if self.workgroup_name is not None:
            return {"Database": self.database, "WorkgroupName": self.workgroup_name}
        return {
            "Database": self.database,
            "ClusterIdentifier": str(self.cluster_identifier),
            "DbUser": str(self.db_user),
        }

    def __repr__(self) -> str:
        where = (
            f"workgroup_name={self.workgroup_name!r}"
            if self.workgroup_name is not None
            else f"cluster_identifier={self.cluster_identifier!r}"
        )
        return f"RedshiftConnection(region={self.region!r}, {where})"


# -- results ------------------------------------------------------------------


@dataclass(frozen=True)
class QueryResult:
    """What a finished statement returned: the rows and the statement's metadata."""

    rows: Tuple[Dict[str, Any], ...]
    statement_handle: str
    warehouse: str
    elapsed_ms: int


@dataclass(frozen=True)
class ConnectionCheck:
    """What the connection test saw: the probe's text and the session's time zone."""

    probe: str
    time_zone: str
    warehouse: str


@dataclass(frozen=True)
class Executed:
    """One batch, run and read."""

    statement_id: str
    probe: str
    time_zone: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0


def described(answer: Any, statement_id: str) -> Dict[str, Any]:
    """The fields of a DescribeStatement answer this connector uses, and no others.

    The status, and each sub-statement's id, status, whether it has a result,
    and its Redshift query id.  ``Error`` and ``QueryString`` are not copied:
    nothing after this can read them.
    """
    answer = _mapping(answer)
    if answer.get("Id") != statement_id:
        raise _invalid()
    status = answer.get("Status")
    if status not in _RUNNING and status not in _ENDED:
        raise _invalid()
    subs = answer.get("SubStatements", [])
    if not isinstance(subs, list):
        raise _invalid()
    kept = []
    for sub in subs:
        sub = _mapping(sub)
        kept.append(
            {
                "Id": sub.get("Id"),
                "Status": sub.get("Status"),
                "HasResultSet": sub.get("HasResultSet"),
                "RedshiftQueryId": sub.get("RedshiftQueryId"),
            }
        )
    return {"Status": status, "SubStatements": kept}


def column_names(metadata: Any) -> List[str]:
    """A result's column names, in lower case; refused unless each is an identifier."""
    if not isinstance(metadata, list) or not metadata:
        raise _invalid()
    names: List[str] = []
    for column in metadata:
        name = _mapping(column).get("name")
        if not _fullmatch(_COLUMN_NAME, name) or name.lower() in names:
            raise _invalid()
        names.append(name.lower())
    return names


def decode_value(value: Any) -> Any:
    """One field of a record: None, an int or a str.  Anything else is refused."""
    if not isinstance(value, dict) or len(value) != 1:
        raise _invalid()
    ((kind, content),) = value.items()
    if kind == "isNull" and content is True:
        return None
    if (
        kind == "longValue"
        and isinstance(content, int)
        and not isinstance(content, bool)
    ):
        return content
    if kind == "stringValue" and isinstance(content, str):
        return content
    raise _invalid()


def decode_records(records: Any, width: int) -> List[List[Any]]:
    if not isinstance(records, list) or len(records) > MAX_RESULT_ROWS:
        raise _invalid()
    decoded: List[List[Any]] = []
    for record in records:
        if not isinstance(record, list) or len(record) != width:
            raise _invalid()
        decoded.append([decode_value(value) for value in record])
    return decoded


def _next_token(page: Mapping[str, Any]) -> Optional[str]:
    token = page.get("NextToken")
    if token is None or token == "":
        return None
    if not isinstance(token, str) or len(token) > _MAX_TOKEN_CHARS:
        raise _invalid()
    return token


# -- the adapter --------------------------------------------------------------


class RedshiftAdapter:
    """One Redshift connection's calls, each made within the caller's Deadline.

    Call it from the warehouse executor's worker thread.  ``platform_session``
    is a boto3 session holding this deployment's own AWS identity (by default
    boto3's, made when it is first needed); the customer's role is assumed
    from it.
    """

    def __init__(
        self,
        connection: RedshiftConnection,
        *,
        platform_session: Any = None,
        resolver: Optional[Resolver] = None,
        monotonic: Optional[Callable[[], float]] = None,
        sleeper: Optional[Sleeper] = None,
    ) -> None:
        if not isinstance(connection, RedshiftConnection):
            raise _internal()
        self.connection = connection
        self._platform_session = platform_session
        self._resolver = resolver
        self._monotonic = monotonic or time.monotonic
        self._sleeper = sleeper
        self._lock = threading.Lock()
        #: The role's session and when it runs out, on the monotonic clock.
        self._role: Optional[Tuple[Any, float]] = None

    def __repr__(self) -> str:
        return f"RedshiftAdapter({self.connection!r})"

    # -- the role --------------------------------------------------------

    def _platform(self) -> Any:
        if self._platform_session is None:
            self._platform_session = boto3.session.Session()
        return self._platform_session

    def _role_session(self, needed: float, deadline: Deadline) -> Tuple[Any, float]:
        """The role's session, with the seconds it has left.

        The session held is used if it has ``needed`` seconds left; otherwise
        the role is assumed again, for :data:`SESSION_SECONDS`.
        """
        with self._lock:
            if self._role is not None:
                session, until = self._role
                left = until - self._monotonic()
                if left >= needed:
                    return session, left
                self._role = None
            asked = self._monotonic()
            session = aws.assume_role_session(
                self._platform(),
                self.connection.role_arn,
                self.connection.external_id,
                self.connection.region,
                warehouse=WAREHOUSE,
                deadline=deadline,
                resolver=self._resolver,
                duration_seconds=SESSION_SECONDS,
            )
            # STS dates the credentials from when it issued them, which is
            # no earlier than when they were asked for.
            until = asked + SESSION_SECONDS
            self._role = (session, until)
            return session, until - self._monotonic()

    # -- the API ---------------------------------------------------------

    def _call(
        self, session: Any, operation: str, deadline: Deadline, **params: Any
    ) -> Any:
        return aws.call(
            session,
            SERVICE,
            self.connection.region,
            operation,
            warehouse=WAREHOUSE,
            deadline=deadline,
            resolver=self._resolver,
            codes=REDSHIFT_CODES,
            **params,
        )

    def _limit(self, deadline: Deadline) -> int:
        """Whole seconds the next statement may run (see Limits)."""
        left = math.floor(deadline.remaining() - CANCEL_RESERVE_SECONDS)
        if left < 1:
            raise WarehouseError(_C.TIME_LIMIT, warehouse=WAREHOUSE)
        return int(
            min(self.connection.query_timeout_seconds, left, MAX_STATEMENT_SECONDS)
        )

    def _submit(self, session: Any, sqls: List[str], deadline: Deadline) -> str:
        """Send the batch; its statement id."""
        answer = self._call(
            session,
            "batch_execute_statement",
            deadline,
            Sqls=list(sqls),
            ExecutionMode="TRANSACTION",
            ResultFormat="JSON",
            StatementName=STATEMENT_NAME,
            **self.connection.target(),
        )
        statement_id = _mapping(answer).get("Id")
        if not _fullmatch(_STATEMENT_ID, statement_id):
            raise _invalid()
        return statement_id

    def _wait(
        self, session: Any, statement_id: str, deadline: Deadline
    ) -> Dict[str, Any]:
        """Poll until the batch has ended; what DescribeStatement said then."""
        polls = 0
        while True:
            answer = self._call(
                session, "describe_statement", deadline, Id=statement_id
            )
            seen = described(answer, statement_id)
            if seen["Status"] in _ENDED:
                return seen
            pause = POLL_PAUSES_SECONDS[min(polls, len(POLL_PAUSES_SECONDS) - 1)]
            polls += 1
            if deadline.remaining() <= pause:
                raise WarehouseError(_C.TIME_LIMIT, warehouse=WAREHOUSE)
            deadline.sleep(pause, sleeper=self._sleeper)

    @staticmethod
    def _check_ended(seen: Mapping[str, Any], statement_id: str) -> None:
        """Raise for a batch that did not finish, or finished in the wrong shape."""
        subs = seen["SubStatements"]
        if seen["Status"] == "ABORTED":
            raise WarehouseError(_C.CANCELLED, warehouse=WAREHOUSE)
        if seen["Status"] == "FAILED":
            failed = [s for s in subs if s.get("Status") == "FAILED"]
            raise WarehouseError(
                _C.UNRECOGNISED_WAREHOUSE_ERROR,
                warehouse=WAREHOUSE,
                vendor_code="FAILED",
                request_id=failed[0].get("RedshiftQueryId") if failed else None,
            )
        expected = [f"{statement_id}:{n}" for n in range(1, SUB_STATEMENTS + 1)]
        if [s.get("Id") for s in subs] != expected:
            raise _invalid()
        if any(s.get("Status") != "FINISHED" for s in subs):
            raise _invalid()
        for index in (PROBE_INDEX, RESULT_INDEX):
            if subs[index - 1].get("HasResultSet") is not True:
                raise _invalid()

    def _result(
        self, session: Any, sub_id: str, deadline: Deadline
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Every page of one sub-statement's result: (column names, rows)."""
        names: Optional[List[str]] = None
        rows: List[List[Any]] = []
        token: Optional[str] = None
        for _ in range(MAX_RESULT_PAGES):
            params: Dict[str, Any] = {"Id": sub_id}
            if token is not None:
                params["NextToken"] = token
            page = _mapping(
                self._call(session, "get_statement_result", deadline, **params)
            )
            if names is None:
                names = column_names(page.get("ColumnMetadata"))
            elif page.get("ColumnMetadata") is not None and (
                column_names(page.get("ColumnMetadata")) != names
            ):
                raise _invalid()
            rows.extend(decode_records(page.get("Records"), len(names)))
            if len(rows) > MAX_RESULT_ROWS:
                raise _invalid()
            token = _next_token(page)
            if token is None:
                return names, [dict(zip(names, row)) for row in rows]
        raise _invalid()

    @staticmethod
    def _check_probe(names: List[str], rows: List[Dict[str, Any]]) -> Tuple[str, str]:
        if names != ["probe", "tz"] or len(rows) != 1:
            raise _invalid()
        text, zone = rows[0]["probe"], rows[0]["tz"]
        try:
            value, _ = parse_float(text, "probe")
        except WarehouseResultRefused:
            raise _invalid() from None
        if value != PROBE_VALUE:
            raise _invalid()
        if zone != PROBE_TIME_ZONE:
            raise WarehouseError(_C.TIMEZONE_NOT_UTC, warehouse=WAREHOUSE)
        return text, zone

    def _cancel(self, session: Any, statement_id: str) -> None:
        """Ask Redshift to cancel the batch, once, in its own short budget.

        Best effort: never raises; a failure is logged as a code.
        """
        failure: Optional[WarehouseError] = None
        try:
            answer = self._call(
                session,
                "cancel_statement",
                Deadline(CANCEL_RESERVE_SECONDS, clock=self._monotonic),
                Id=statement_id,
            )
            if _mapping(answer).get("Status") is not True:
                failure = WarehouseError(
                    _C.UNRECOGNISED_WAREHOUSE_ERROR, warehouse=WAREHOUSE
                )
        except WarehouseError as exc:
            failure = exc
        except Exception:
            failure = _internal()
        if failure is not None:
            log_warehouse_error(sanitised(failure), event="redshift cancel failed")

    def _execute(self, select_sql: str, deadline: Deadline) -> Executed:
        """Run one statement in its batch and read the probe and the result."""
        deadline.check()
        limit = self._limit(deadline)
        session, left = self._role_session(limit + SESSION_RESERVE_SECONDS, deadline)
        # The role may have just been assumed: what is left of both now.
        limit = min(self._limit(deadline), math.floor(left - SESSION_RESERVE_SECONDS))
        if limit < 1:
            raise WarehouseError(_C.TIME_LIMIT, warehouse=WAREHOUSE)
        statement = Deadline(limit, clock=self._monotonic)
        started = self._monotonic()
        statement_id: Optional[str] = None
        ended = False
        failure: Optional[WarehouseError] = None
        executed: Optional[Executed] = None
        try:
            statement_id = self._submit(
                session, batch_sqls(select_sql, limit), statement
            )
            seen = self._wait(session, statement_id, statement)
            ended = True
            self._check_ended(seen, statement_id)
            probe_text, zone = self._check_probe(
                *self._result(session, f"{statement_id}:{PROBE_INDEX}", deadline)
            )
            _, rows = self._result(session, f"{statement_id}:{RESULT_INDEX}", deadline)
            executed = Executed(
                statement_id=statement_id,
                probe=probe_text,
                time_zone=zone,
                rows=rows,
                elapsed_ms=max(0, int(round((self._monotonic() - started) * 1000))),
            )
        except WarehouseError as exc:
            failure = exc
        if failure is not None:
            # Still running as far as we know: stop it.
            if statement_id is not None and not ended:
                self._cancel(session, statement_id)
            raise sanitised(failure)
        if executed is None:  # unreachable: the block above returns or raises
            raise _internal()
        return executed

    # -- operations ------------------------------------------------------

    def run_query(self, query: BuiltQuery, deadline: Deadline) -> QueryResult:
        """Run and read one generated statement, within ``deadline``."""
        if not isinstance(query, BuiltQuery) or query.dialect != WAREHOUSE:
            raise _internal()
        executed = self._execute(query.sql, deadline)
        return QueryResult(
            rows=tuple(executed.rows),
            statement_handle=executed.statement_id,
            warehouse=self.connection.location,
            elapsed_ms=executed.elapsed_ms,
        )

    def check_connection(self, deadline: Deadline) -> ConnectionCheck:
        """Assume the role and run a batch whose statement reads no table.

        Proves the role's trust policy requires the external ID and admits
        this deployment, the role can run statements in the database, and the
        session's settings hold (the probe).
        """
        executed = self._execute(CONNECTION_TEST_SQL, deadline)
        if executed.rows != [{"connected": 1}]:
            raise _invalid()
        return ConnectionCheck(
            probe=executed.probe,
            time_zone=executed.time_zone,
            warehouse=self.connection.location,
        )

    def table_columns(
        self, table: Tuple[str, ...], deadline: Deadline
    ) -> Tuple[Tuple[str, str], ...]:
        """``DescribeTable``: the column names and types of a table or view.

        The reference is checked against the Redshift identifier rules before
        anything is sent (``WarehouseQueryRefused``, ``invalid_identifier``).
        Reads metadata only; no statement runs.  No column at all is
        ``object_not_found``: the table does not exist or the role cannot see
        it.
        """
        schema, name = validate_table_parts(REDSHIFT, table)
        deadline.check()
        session, _ = self._role_session(
            min(deadline.remaining(), MAX_STATEMENT_SECONDS) + SESSION_RESERVE_SECONDS,
            deadline,
        )
        columns: List[Tuple[str, str]] = []
        token: Optional[str] = None
        for _ in range(MAX_COLUMN_PAGES):
            params: Dict[str, Any] = {
                **self.connection.target(),
                "Schema": schema,
                "Table": name,
                "MaxResults": COLUMNS_PER_PAGE,
            }
            if token is not None:
                params["NextToken"] = token
            page = _mapping(self._call(session, "describe_table", deadline, **params))
            listed = page.get("ColumnList", [])
            if not isinstance(listed, list):
                raise _invalid()
            for column in listed:
                column = _mapping(column)
                column_name = column.get("name")
                kind = column.get("typeName")
                if not _fullmatch(_COLUMN_NAME, column_name) or not _fullmatch(
                    _TYPE_NAME, kind
                ):
                    raise _invalid()
                columns.append((column_name, kind))
            token = _next_token(page)
            if token is None:
                if not columns:
                    raise WarehouseError(_C.OBJECT_NOT_FOUND, warehouse=WAREHOUSE)
                return tuple(columns)
        raise _invalid()


__all__ = [
    "CONNECTION_TEST_SQL",
    "MAX_STATEMENT_SECONDS",
    "PROBE_SQL",
    "REDSHIFT_CODES",
    "SESSION_SECONDS",
    "SESSION_SETUP",
    "STATEMENT_NAME",
    "UNMODELLED_CODES",
    "ConnectionCheck",
    "QueryResult",
    "RedshiftAdapter",
    "RedshiftConnection",
    "batch_sqls",
    "column_names",
    "decode_records",
    "decode_value",
    "described",
    "statement_timeout_sql",
]
