"""From a saved warehouse connection to something that can query it.

A route never talks to a connector directly.  It turns a connection -- a
saved row, or a body being tested before it is saved -- into a
:class:`ConnectionSpec` (the non-secret parameters, the limits, and the
credential's plaintext, held in memory only), and asks a
:data:`ClientFactory` for a :class:`WarehouseClient`: the connector's adapter
together with the SQL dialect its statements are built in and the column
types its metadata reports.  The default factory builds the real connectors;
the tests pass one that builds a DuckDB warehouse or a fake.

This module also owns the rules about what a connection stores:

* which parameters each type keeps (never a secret);
* the credential status a response shows (``ok``, ``unavailable`` when the
  operator's keys are not set, ``needs_new_credentials`` when no configured
  key opens the stored credential, ``pending_key`` while a regenerated
  Snowflake key waits for its first passing test);
* the worst case a day of runs can cost.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional

from modules.backend.app.core.credential_crypto import (
    CredentialKeysUnavailable,
    CredentialUndecryptable,
)
from modules.backend.app.models.warehouse_connection import (
    ROLE_TYPES,
    WarehouseConnection,
)
from modules.backend.app.services.warehouse_query_builder import (
    BIGQUERY_SQL,
    SNOWFLAKE_SQL,
    SqlDialect,
)
from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode

#: A run is one diagnostics statement plus at most ten metric statements.
STATEMENTS_PER_RUN_AT_MOST = 11

#: The parameters each type keeps, in the order responses show them.
PARAMETER_FIELDS: Mapping[str, tuple[str, ...]] = {
    "snowflake": ("account", "user", "role", "warehouse"),
    "bigquery": ("billing_project", "location", "client_email"),
    "athena": ("region", "role_arn", "workgroup", "database"),
}

#: Snowflake roles a connection may not use, compared without regard to case.
SNOWFLAKE_ADMIN_ROLES: FrozenSet[str] = frozenset(
    {"ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "ORGADMIN", "USERADMIN"}
)

#: Column types accepted for a time column and for a mean metric's value,
#: upper-cased, per type.  Anything else is ``unsupported_column_type``.
TIME_TYPES: Mapping[str, FrozenSet[str]] = {
    "snowflake": frozenset({"TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ"}),
    "bigquery": frozenset({"TIMESTAMP"}),
    "athena": frozenset({"TIMESTAMP", "TIMESTAMP WITH TIME ZONE"}),
}
NUMERIC_TYPES: Mapping[str, FrozenSet[str]] = {
    "snowflake": frozenset(
        {
            "FIXED",
            "REAL",
            "NUMBER",
            "DECIMAL",
            "NUMERIC",
            "INT",
            "INTEGER",
            "BIGINT",
            "SMALLINT",
            "TINYINT",
            "FLOAT",
            "DOUBLE",
        }
    ),
    "bigquery": frozenset(
        {"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"}
    ),
    "athena": frozenset(
        {"TINYINT", "SMALLINT", "INT", "INTEGER", "BIGINT", "REAL", "DOUBLE", "FLOAT"}
    ),
}


@dataclass(frozen=True, repr=False)
class ConnectionSpec:
    """Everything needed to reach one warehouse.  ``credential`` is plaintext.

    A role-based connection (``ROLE_TYPES``) has no credential and carries the
    ``external_id`` generated when it was saved instead.
    """

    warehouse_type: str
    parameters: Mapping[str, str]
    query_timeout_seconds: int
    max_bytes_per_query: Optional[int]
    credential: Optional[bytes] = None
    external_id: Optional[str] = None

    def __repr__(self) -> str:  # never the credential
        return f"ConnectionSpec(warehouse_type={self.warehouse_type!r})"


@dataclass(frozen=True)
class WarehouseClient:
    """A connector adapter with the dialect and column types that go with it.

    ``adapter`` has ``run_query(built, deadline)`` (an object with ``rows``),
    ``table_columns(parts, deadline)`` and ``check_connection(deadline)``;
    every call runs on the warehouse executor, within the deadline it is given.
    """

    warehouse_type: str
    adapter: Any
    dialect: SqlDialect
    time_types: FrozenSet[str] = field(default_factory=frozenset)
    numeric_types: FrozenSet[str] = field(default_factory=frozenset)

    def is_time_type(self, name: Optional[str]) -> bool:
        return isinstance(name, str) and _type_key(name) in self.time_types

    def is_numeric_type(self, name: Optional[str]) -> bool:
        return isinstance(name, str) and _type_key(name) in self.numeric_types


def _type_key(name: str) -> str:
    """``decimal(38,2)`` -> ``DECIMAL``; ``NUMBER(38,0)`` -> ``NUMBER``."""
    return name.split("(", 1)[0].strip().upper()


ClientFactory = Callable[[ConnectionSpec], WarehouseClient]


def _bigquery_client(spec: ConnectionSpec) -> WarehouseClient:
    from modules.backend.app.warehouse.bigquery import (
        BigQueryAdapter,
        BigQueryConnection,
        ServiceAccountKey,
    )

    if spec.credential is None or spec.max_bytes_per_query is None:
        raise WarehouseError(WarehouseErrorCode.INTERNAL, warehouse="bigquery")
    adapter = BigQueryAdapter(
        BigQueryConnection(
            billing_project=spec.parameters["billing_project"],
            location=spec.parameters["location"],
            max_bytes_per_query=spec.max_bytes_per_query,
            query_timeout_seconds=spec.query_timeout_seconds,
        ),
        ServiceAccountKey.from_blob(spec.credential),
    )
    return WarehouseClient(
        "bigquery",
        adapter,
        BIGQUERY_SQL,
        TIME_TYPES["bigquery"],
        NUMERIC_TYPES["bigquery"],
    )


def _snowflake_client(spec: ConnectionSpec) -> WarehouseClient:
    from modules.backend.app.warehouse.snowflake import (
        SnowflakeAdapter,
        SnowflakeConnection,
        SnowflakeKey,
    )

    if spec.credential is None:
        raise WarehouseError(WarehouseErrorCode.INTERNAL, warehouse="snowflake")
    adapter = SnowflakeAdapter(
        SnowflakeConnection(
            account=spec.parameters["account"],
            user=spec.parameters["user"],
            role=spec.parameters["role"],
            warehouse=spec.parameters["warehouse"],
            query_timeout_seconds=spec.query_timeout_seconds,
        ),
        SnowflakeKey.from_blob(spec.credential),
    )
    return WarehouseClient(
        "snowflake",
        adapter,
        SNOWFLAKE_SQL,
        TIME_TYPES["snowflake"],
        NUMERIC_TYPES["snowflake"],
    )


#: The connectors this code can build.  Athena's adapter is not built yet; a
#: type with no builder answers ``connector_disabled`` before it gets here.
_BUILDERS: Mapping[str, ClientFactory] = {
    "bigquery": _bigquery_client,
    "snowflake": _snowflake_client,
}


def build_client(spec: ConnectionSpec) -> WarehouseClient:
    """The default :data:`ClientFactory`: the real connector for ``spec``."""
    builder = _BUILDERS.get(spec.warehouse_type)
    if builder is None:
        raise WarehouseError(WarehouseErrorCode.INTERNAL)
    return builder(spec)


def spec_for(
    connection: WarehouseConnection, *, pending: bool = False
) -> ConnectionSpec:
    """A saved connection's spec, with its credential decrypted.

    Raises ``CredentialKeysUnavailable`` (503) or ``CredentialUndecryptable``
    (409) from :mod:`~modules.backend.app.core.credential_crypto`.
    """
    credential = None
    external_id = None
    if connection.warehouse_type in ROLE_TYPES:
        external_id = connection.external_id
    else:
        credential = connection.get_credentials(pending=pending)
        if credential is None:
            raise CredentialUndecryptable("no stored credential")
    return ConnectionSpec(
        warehouse_type=connection.warehouse_type,
        parameters=dict(connection.parameters or {}),
        query_timeout_seconds=int(connection.query_timeout_seconds),
        max_bytes_per_query=connection.max_bytes_per_query,
        credential=credential,
        external_id=external_id,
    )


def credentials_status(connection: WarehouseConnection) -> str:
    """``ok``, ``unavailable``, ``needs_new_credentials`` or ``pending_key``."""
    if connection.warehouse_type in ROLE_TYPES:
        return "ok"
    try:
        if connection.get_credentials() is None:
            return "needs_new_credentials"
    except CredentialKeysUnavailable:
        return "unavailable"
    except CredentialUndecryptable:
        return "needs_new_credentials"
    if connection.pending_credentials_ciphertext is not None:
        return "pending_key"
    return "ok"


def worst_case_per_day(connection: WarehouseConnection) -> Dict[str, Optional[int]]:
    """max_runs_per_day x 11 statements x the per-query cap, in bytes or seconds."""
    runs = int(connection.max_runs_per_day) * STATEMENTS_PER_RUN_AT_MOST
    if connection.max_bytes_per_query is not None:
        return {
            "worst_case_bytes_per_day": runs * int(connection.max_bytes_per_query),
            "worst_case_seconds_per_day": None,
        }
    return {
        "worst_case_bytes_per_day": None,
        "worst_case_seconds_per_day": runs * int(connection.query_timeout_seconds),
    }


def new_external_id() -> str:
    """``exp-`` and 32 hex characters, generated here and never accepted."""
    return "exp-" + secrets.token_hex(16)


def forget_cached_token(connection: WarehouseConnection) -> None:
    """Drop the in-memory BigQuery access token for a connection's key(s).

    Called when a connection is deleted or its key replaced, so a token signed
    for a key that is gone is not kept for the rest of its hour.  Best effort:
    an undecryptable credential has no token to forget.
    """
    if connection.warehouse_type != "bigquery":
        return
    from modules.backend.app.warehouse import bigquery

    for pending in (False, True):
        try:
            blob = connection.get_credentials(pending=pending)
        except (CredentialKeysUnavailable, CredentialUndecryptable):
            continue
        if blob is None:
            continue
        try:
            key = bigquery.ServiceAccountKey.from_blob(blob)
        except WarehouseError:
            continue
        bigquery._TOKEN_CACHE.discard(key.cache_key)


__all__ = [
    "PARAMETER_FIELDS",
    "SNOWFLAKE_ADMIN_ROLES",
    "STATEMENTS_PER_RUN_AT_MOST",
    "ClientFactory",
    "ConnectionSpec",
    "WarehouseClient",
    "build_client",
    "credentials_status",
    "forget_cached_token",
    "new_external_id",
    "spec_for",
    "worst_case_per_day",
]
