"""Response bodies of the warehouse analysis API (``/api/v1/warehouse/analysis``).

Kept apart from the request models (``warehouse_*.py``), whose every string
field is pinned by ``test_request_schemas.py``; nothing here is ever read from
a client.

What a response never carries: a credential (a service-account key, a private
key, a token), a row of the customer's data, or text from the warehouse.  A
preview returns counts and time bounds; a run returns aggregates and the
statistics computed from them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ConnectorOut(BaseModel):
    warehouse_type: Literal["bigquery", "snowflake", "athena"]
    name: str
    enabled: bool


class ConnectorListOut(BaseModel):
    connectors: List[ConnectorOut]


class ConnectionOut(BaseModel):
    """A connection as every role that may list connections sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    warehouse_type: str
    #: The non-secret parameters (account, user, role, warehouse; billing
    #: project, location, service-account e-mail; region, role ARN,
    #: workgroup, database).
    parameters: Dict[str, str]
    credentials_status: Literal[
        "ok", "unavailable", "needs_new_credentials", "pending_key"
    ]
    public_key_fingerprint: Optional[str] = None
    pending_public_key_fingerprint: Optional[str] = None
    external_id: Optional[str] = None
    query_timeout_seconds: int
    max_bytes_per_query: Optional[int] = None
    max_runs_per_day: int
    #: The most one day of runs on this connection can read (BigQuery,
    #: Athena) or run for (Snowflake): max_runs_per_day x 11 x the query cap.
    worst_case_bytes_per_day: Optional[int] = None
    worst_case_seconds_per_day: Optional[int] = None
    enabled: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PublicKeyOut(BaseModel):
    """What the customer runs in Snowflake to register a generated key."""

    public_key: str
    public_key_fingerprint: str
    statement: str


class ConnectionCreatedOut(ConnectionOut):
    #: Snowflake only: the statement that registers the generated public key.
    public_key: Optional[PublicKeyOut] = None


class ConnectionListOut(BaseModel):
    connections: List[ConnectionOut]


class ConnectionTestOut(BaseModel):
    ok: bool
    warehouse_type: str
    #: Set when a passing test promoted a pending Snowflake key.
    promoted_pending_key: bool = False


class SourceColumnOut(BaseModel):
    name: str
    type: Optional[str] = None


class SourceOut(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    kind: Literal["assignment", "metric"]
    name: str
    table: str
    columns: Dict[str, SourceColumnOut]
    filters: List[Dict[str, Any]]
    metric_type: Optional[str] = None
    conversion_window_hours: Optional[int] = None
    cap_value: Optional[float] = None
    lower_is_better: Optional[bool] = Field(
        default=None,
        description=(
            "True for a metric source where a decrease is the improvement. "
            "Null for an assignment source."
        ),
    )
    validated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SourceListOut(BaseModel):
    sources: List[SourceOut]


class ValidateOut(BaseModel):
    """The table's columns as the warehouse's metadata describes them."""

    source: SourceOut
    columns: List[SourceColumnOut]


class PreviewVariantOut(BaseModel):
    label: str
    units: int


class PreviewOut(BaseModel):
    """Aggregates only: counts and the earliest and latest time.  No rows."""

    kind: Literal["assignment", "metric"]
    window_start: datetime
    window_end: datetime
    total_rows: int
    null_unit_rows: int
    #: Assignment previews: rows with no variant, and units per variant label
    #: (at most 51 labels, each cut to 64 characters).
    null_variant_rows: Optional[int] = None
    variants: Optional[List[PreviewVariantOut]] = None
    #: Metric previews of a mean metric: rows whose value is NULL.
    null_value_rows: Optional[int] = None
    earliest: Optional[datetime] = None
    latest: Optional[datetime] = None
    run_id: uuid.UUID


class RunAcceptedOut(BaseModel):
    run_id: uuid.UUID
    status: str


class RunOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: Literal["queued", "running", "succeeded", "failed"]
    experiment_id: Optional[uuid.UUID] = None
    connection_id: Optional[uuid.UUID] = None
    connection_name: str
    warehouse_type: str
    request: Dict[str, Any]
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    #: Each statement sent: its kind, its SHA-256 and the SQL itself (View SQL).
    #: Returned only to the ADMIN, DEVELOPER and ANALYST roles (founder decision D34).
    statements: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Each statement the run sent: its kind, dialect, SHA-256 and SQL. "
            "Returned to the ADMIN, DEVELOPER and ANALYST roles (a superuser "
            "counts as ADMIN), the only roles that may read a run (D34, D50)."
        ),
    )
    #: Present only for a run that succeeded.
    results: Optional[Dict[str, Any]] = None
    job_metadata: Optional[List[Dict[str, Any]]] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class RunListOut(BaseModel):
    runs: List[RunOut]
