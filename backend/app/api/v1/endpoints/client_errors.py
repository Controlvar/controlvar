"""
Client error reporting endpoints (``POST /api/v1/tracking/errors``).

SDKs and demo simulators use these API-key endpoints to report errors that
happened while a feature flag or experiment was active (a crash in a
redesigned screen, a failed request behind a flag, ...). Each report becomes
an :class:`~backend.app.models.metrics.metric.ErrorLog` row attached to the
flag.

Which reports count toward the flag's ``error_rate`` (errors divided by
evaluations) and so toward automatic rollback depends on the key that sent
them, decided once per request by :func:`report_source`:

* a server-side key -- one with the ``sdk:ruleset`` scope whose owner can
  change feature flags (:func:`backend.app.api.sdk_scope.presented_key_is_server_key`)
  -- stores ``request_data["source"] = "server"``, and the report counts, like
  a flag evaluation that fails on the server;
* any other key, such as the key in a browser or mobile app, stores
  ``"client"``: the report is accepted and stored as before, but safety
  monitoring does not count it (``uncounted_error_count`` on the flag's check).

An app whose errors should count sends them to its own server, which forwards
them with a server-side key. Every valid key is accepted: neither route
answers 403.

Stored rows are timed by the server on receipt; a ``timestamp`` sent by the
client is kept in the row's metadata as ``client_timestamp``. ``stack_trace``
and ``metadata`` are bounded in UTF-8 bytes (``STACK_TRACE_MAX_BYTES``,
``METADATA_MAX_BYTES``) before they are stored.

The router is mounted under the ``/tracking`` prefix next to the event
tracking endpoints (see ``backend/app/api/api.py``).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.sdk_scope import presented_key_is_server_key
from backend.app.core.logger import failure_detail
from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.metrics.metric import (
    REPORT_SOURCE_CLIENT,
    REPORT_SOURCE_SERVER,
    ErrorLog,
)
from backend.app.models.user import User
from backend.app.schemas.metrics import ErrorLogCreate
from backend.app.schemas.storable_text import StorableTextModel
from backend.app.services.metrics_service import MetricsService

router = APIRouter()

logger = logging.getLogger(__name__)

#: Maximum number of reports accepted by the batch endpoint.
MAX_BATCH_SIZE = 100

# Column limits on ``error_logs`` (see ErrorLog model).
_ERROR_TYPE_MAX = 100
_MESSAGE_MAX = 1000

#: Most bytes (UTF-8) of ``stack_trace`` stored; the rest is cut off.
STACK_TRACE_MAX_BYTES = 8 * 1024
#: Most bytes (UTF-8, compact JSON) of ``metadata`` stored. Larger metadata is
#: replaced by ``{"metadata_truncated": true, "metadata_bytes": <size>}``.
METADATA_MAX_BYTES = 8 * 1024
#: ``metadata`` key under which the client-sent ``timestamp`` is kept.
CLIENT_TIMESTAMP_KEY = "client_timestamp"


# ---------------------------------------------------------------------------
# Schemas (kept local: this module owns the client error contract)
# ---------------------------------------------------------------------------


class ClientErrorRequest(StorableTextModel):
    """One client-side error report."""

    feature_flag_key: Optional[str] = Field(
        None,
        description="Key of the feature flag that was active when the error happened",
    )
    experiment_key: Optional[str] = Field(
        None,
        description="Key of the experiment the user was in when the error happened",
    )
    user_id: Optional[str] = Field(None, max_length=255, description="User / device id")
    error_type: str = Field(
        ...,
        min_length=1,
        max_length=_ERROR_TYPE_MAX,
        description='Error class, e.g. "crash"',
    )
    message: str = Field(..., min_length=1, description="Human readable error message")
    stack_trace: Optional[str] = Field(None, description="Optional stack trace")
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Additional debug data (os, os_version, device_model, ...)"
    )
    timestamp: Optional[datetime] = Field(
        None, description="When the error happened (ISO 8601); defaults to now"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "feature_flag_key": "streampulse_player_v2",
                "user_id": "device-1234",
                "error_type": "crash",
                "message": "NullPointerException in PlayerV2Fragment",
                "metadata": {
                    "os": "Android",
                    "os_version": "12.0.0",
                    "app_version": "3.2.0",
                },
            }
        }
    )

    @model_validator(mode="after")
    def _require_a_target(self) -> "ClientErrorRequest":
        if not (self.feature_flag_key or self.experiment_key):
            raise ValueError(
                "At least one of feature_flag_key or experiment_key is required"
            )
        return self


class ClientErrorResponse(BaseModel):
    """Confirmation of a stored error report."""

    id: str
    error_type: str
    feature_flag_id: Optional[str] = None
    experiment_id: Optional[str] = None
    user_id: Optional[str] = None
    timestamp: datetime


class ClientErrorBatchRequest(BaseModel):
    """Batch of error reports."""

    errors: List[ClientErrorRequest] = Field(
        ..., min_length=1, description="Error reports"
    )


class ClientErrorBatchResponse(BaseModel):
    """Outcome of a batch of error reports."""

    success_count: int
    failure_count: int
    errors: Optional[List[Dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_targets(
    db: Session, report: ClientErrorRequest
) -> Tuple[Optional[UUID], Optional[UUID]]:
    """Resolve ``feature_flag_key`` / ``experiment_key`` to ids; ``LookupError`` when unknown."""
    feature_flag_id: Optional[UUID] = None
    experiment_id: Optional[UUID] = None

    if report.feature_flag_key:
        flag = (
            db.query(FeatureFlag)
            .filter(FeatureFlag.key == report.feature_flag_key)
            .first()
        )
        if flag is None:
            raise LookupError(
                f"Feature flag with key '{report.feature_flag_key}' not found"
            )
        feature_flag_id = flag.id

    if report.experiment_key:
        experiment = (
            db.query(Experiment).filter(Experiment.key == report.experiment_key).first()
        )
        if experiment is None:
            raise LookupError(
                f"Experiment with key '{report.experiment_key}' not found"
            )
        experiment_id = experiment.id

    return feature_flag_id, experiment_id


def _truncate_utf8(value: Optional[str], limit: int) -> Optional[str]:
    """``value`` cut to at most ``limit`` UTF-8 bytes, never mid-character."""
    if value is None:
        return None
    # A character is at least one byte, so slicing characters first bounds the
    # work before encoding.
    head = value[:limit]
    encoded = head.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return head
    return encoded[:limit].decode("utf-8", "ignore")


def _stored_metadata(report: ClientErrorRequest) -> Optional[Dict[str, Any]]:
    """The ``meta_data`` JSON stored with the row.

    Client metadata larger than ``METADATA_MAX_BYTES`` is replaced by a marker
    carrying its size. A client-sent ``timestamp`` is kept under
    ``CLIENT_TIMESTAMP_KEY``; the row itself is timed on receipt.
    """
    metadata: Optional[Dict[str, Any]] = report.metadata
    if metadata is not None:
        size = len(
            json.dumps(
                metadata, separators=(",", ":"), ensure_ascii=False, default=str
            ).encode("utf-8", "replace")
        )
        if size > METADATA_MAX_BYTES:
            metadata = {"metadata_truncated": True, "metadata_bytes": size}
    if report.timestamp is not None:
        metadata = dict(metadata or {})
        metadata[CLIENT_TIMESTAMP_KEY] = report.timestamp.isoformat()
    return metadata


def report_source(
    db: Session = Depends(deps.get_db),
    api_key_header: Optional[str] = Depends(deps.API_KEY_HEADER),
    _owner: User = Depends(deps.get_api_key),
) -> str:
    """``"server"`` or ``"client"``: what this request's reports are stamped with.

    Layered on ``deps.get_api_key``, so a missing or invalid key is still a 401
    and nothing else is refused. The value comes from the presented key alone
    (never from the body) and covers every report in a batch.
    """
    if presented_key_is_server_key(db, api_key_header):
        return REPORT_SOURCE_SERVER
    return REPORT_SOURCE_CLIENT


def _request_data(
    report: ClientErrorRequest, experiment_id: Optional[UUID], source: str
) -> Dict[str, Any]:
    """The ``request_data`` JSON stored with the row.

    ``source`` is set here, from the presented key (:func:`report_source`);
    the request body has no way to set it.
    """
    data: Dict[str, Any] = {"source": source}
    if report.feature_flag_key:
        data["feature_flag_key"] = report.feature_flag_key
    if report.experiment_key:
        data["experiment_key"] = report.experiment_key
    if experiment_id is not None:
        data["experiment_id"] = str(experiment_id)
    return data


def _to_error_log_create(
    report: ClientErrorRequest,
    feature_flag_id: Optional[UUID],
    experiment_id: Optional[UUID],
    source: str,
) -> ErrorLogCreate:
    return ErrorLogCreate(
        error_type=report.error_type[:_ERROR_TYPE_MAX],
        feature_flag_id=feature_flag_id,
        user_id=report.user_id,
        message=report.message[:_MESSAGE_MAX],
        stack_trace=_truncate_utf8(report.stack_trace, STACK_TRACE_MAX_BYTES),
        request_data=_request_data(report, experiment_id, source),
        metadata=_stored_metadata(report),
        # Server receive time (naive UTC, like the column).
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _response(row: ErrorLog, experiment_id: Optional[UUID]) -> ClientErrorResponse:
    return ClientErrorResponse(
        id=str(row.id),
        error_type=row.error_type,
        feature_flag_id=str(row.feature_flag_id) if row.feature_flag_id else None,
        experiment_id=str(experiment_id) if experiment_id else None,
        user_id=row.user_id,
        timestamp=row.timestamp,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/errors",
    response_model=ClientErrorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Report a client-side error",
    response_description="Returns the stored error log entry",
)
async def report_client_error(
    report: ClientErrorRequest = Body(..., description="Error report"),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
    source: str = Depends(report_source),
) -> ClientErrorResponse:
    """
    Record an error that happened while a feature flag or experiment was
    active.

    The row is attached to the feature flag resolved from ``feature_flag_key``.
    ``experiment_key`` is resolved and stored in ``request_data``.

    Only a report sent with a server-side key (an API key with the
    ``sdk:ruleset`` scope, whose owner can change feature flags) counts toward
    the flag's ``error_rate`` (errors / flag evaluations in the monitoring
    window) and automatic rollback. A report sent with any other key is stored
    the same way and answers 201, but is not counted; the flag's safety check
    shows it as ``uncounted_error_count``.

    **Authentication**: Requires a valid API key in the X-API-Key header.

    Raises:
        HTTPException 401: If the API key is invalid
        HTTPException 404: If ``feature_flag_key`` or ``experiment_key`` is unknown
        HTTPException 422: If neither key is given or the body is invalid
    """
    try:
        feature_flag_id, experiment_id = _resolve_targets(db, report)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    try:
        row = MetricsService.log_error(
            db=db,
            data=_to_error_log_create(report, feature_flag_id, experiment_id, source),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Error report failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=failure_detail("Could not store the error report"),
        )

    return _response(row, experiment_id)


@router.post(
    "/errors/batch",
    response_model=ClientErrorBatchResponse,
    summary="Report multiple client-side errors",
    response_description="Returns batch processing results",
)
async def report_client_errors_batch(
    request: ClientErrorBatchRequest = Body(..., description="Batch of error reports"),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
    source: str = Depends(report_source),
) -> ClientErrorBatchResponse:
    """
    Record up to 100 error reports in one request.

    Reports whose keys cannot be resolved are skipped and listed in ``errors``
    (with their index); the rest are stored in a single transaction.

    As for a single report, only reports sent with a server-side key (an API
    key with the ``sdk:ruleset`` scope, whose owner can change feature flags)
    count toward a flag's ``error_rate`` and automatic rollback. Reports sent
    with any other key are stored and counted in ``success_count``, but not
    toward the error rate.

    **Authentication**: Requires a valid API key in the X-API-Key header.

    Raises:
        HTTPException 401: If the API key is invalid
        HTTPException 413: If more than 100 reports are sent
        HTTPException 422: If the batch is malformed
    """
    if len(request.errors) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Batch size exceeds the limit of {MAX_BATCH_SIZE} errors",
        )

    success_count = 0
    failure_count = 0
    failures: List[Dict[str, Any]] = []
    rows: List[ErrorLog] = []

    for index, report in enumerate(request.errors):
        try:
            feature_flag_id, experiment_id = _resolve_targets(db, report)
            data = _to_error_log_create(report, feature_flag_id, experiment_id, source)
            rows.append(
                ErrorLog(
                    error_type=data.error_type,
                    timestamp=data.timestamp,
                    feature_flag_id=data.feature_flag_id,
                    user_id=data.user_id,
                    message=data.message,
                    stack_trace=data.stack_trace,
                    request_data=data.request_data,
                    meta_data=data.metadata,
                )
            )
            success_count += 1
        except ValidationError as exc:
            # The field names only: pydantic's text repeats what was sent.
            fields = sorted(
                {str(e["loc"][0]) if e.get("loc") else "report" for e in exc.errors()}
            )
            failure_count += 1
            failures.append(
                {
                    "index": index,
                    "error_type": report.error_type,
                    "user_id": report.user_id,
                    "error": f"Invalid report: {', '.join(fields)} not valid",
                }
            )
        except (LookupError, ValueError) as exc:  # unknown key or invalid report
            failure_count += 1
            failures.append(
                {
                    "index": index,
                    "error_type": report.error_type,
                    "user_id": report.user_id,
                    "error": str(exc),
                }
            )
        except Exception as exc:
            logger.exception("Error report batch item failed (%s)", type(exc).__name__)
            db.rollback()
            failure_count += 1
            failures.append(
                {
                    "index": index,
                    "error_type": report.error_type,
                    "user_id": report.user_id,
                    "error": failure_detail("Could not store this error report"),
                }
            )

    if rows:
        try:
            db.add_all(rows)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.exception("Error report batch failed (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=failure_detail("Could not store the error reports"),
            )

    return ClientErrorBatchResponse(
        success_count=success_count,
        failure_count=failure_count,
        errors=failures or None,
    )
