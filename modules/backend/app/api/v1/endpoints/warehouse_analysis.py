"""Warehouse analysis: connections, sources and runs (#312).  Beta.

Mounted at ``/api/v1/warehouse/analysis`` behind authentication.  Every route
is ``x-stability: beta``.  The exact set of routes is pinned by
``modules/backend/tests/smoke/test_module_route_table.py``.

Who may do what (founder decisions D11, D25, D34 and D50):

=====================================================  =====  =========  =======  ======
Action                                                  ADMIN  DEVELOPER  ANALYST  VIEWER
=====================================================  =====  =========  =======  ======
List connectors                                         yes    yes        yes      yes
List and read connections (never a secret)              yes    yes        yes      no
Create, update, delete, test, regenerate a key          yes    no         no       no
Read sources                                            yes    yes        yes      no
Create, edit, delete, validate, preview assignment src  yes    yes        no       no
Create, edit, validate, preview metric sources          yes    yes        yes      no
Delete a metric source                                  yes    yes        no       no
Start a run                                             yes    yes        no       no
Read runs, previews and results (D50)                   yes    yes        yes      no
Read the SQL a run sent (``statements``; D34)           yes    yes        yes      no
=====================================================  =====  =========  =======  ======

A superuser counts as ADMIN.  A refusal names the role needed and the caller's.
A run, analysis or preview, is read only by the roles that may read its SQL,
so every caller who reaches a run also gets ``statements``.  ``run_out`` still
takes ``include_sql`` from each caller and, when it is false, withholds the
kind, dialect, SHA-256 and SQL of every statement: that holds if a route's
roles are ever widened again.

Errors are ``{"detail": {"code": ..., "message": ...}}`` with a code from a
fixed set.  A request that fails validation is answered with the location and
the reason of each error but never the submitted value, so a key pasted into
a malformed body is not sent back.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import (
    Annotated,
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Tuple,
    Union,
)

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool

from backend.app.api import deps
from backend.app.models.compliance_audit_event import AuditAction, AuditOutcome
from backend.app.models.experiment import Experiment
from backend.app.models.user import User
from backend.app.services.audit_log_service import AuditLogService
from backend.app.services.srm_service import _is_fixed_allocation
from modules.backend.app.core.credential_crypto import (
    CredentialKeysUnavailable,
    CredentialUndecryptable,
)
from modules.backend.app.core.warehouse_identifiers import WarehouseQueryRefused
from modules.backend.app.models.warehouse_analysis_run import WarehouseAnalysisRun
from modules.backend.app.models.warehouse_connection import WarehouseConnection
from modules.backend.app.models.warehouse_source import WarehouseSource
from modules.backend.app.schemas import responses_warehouse as out
from modules.backend.app.schemas.warehouse_connections import (
    ConnectionCreate,
    ConnectionTest,
    ConnectionUpdate,
)
from modules.backend.app.schemas.warehouse_runs import PreviewRequest, RunCreate
from modules.backend.app.schemas.warehouse_sources import (
    AssignmentSourceCreate,
    AssignmentSourceUpdate,
    MetricSourceCreate,
    MetricSourceUpdate,
)
from modules.backend.app.services import warehouse_clients as clients
from modules.backend.app.services import warehouse_runner as runner
from modules.backend.app.services import warehouse_source_service as sources
from modules.backend.app.services.warehouse_query_builder import (
    AnalysisWindow,
    build_assignment_preview_query,
    build_diagnostics_query,
    build_metric_preview_query,
    build_metric_query,
)
from modules.backend.app.services.warehouse_sufficient_stats import (
    WarehouseResultRefused,
)
from modules.backend.app.warehouse import connectors
from modules.backend.app.warehouse.deadlines import (
    connection_test_total,
    preview_total,
    run_total,
)
from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode
from modules.backend.app.warehouse.executor import (
    REQUEST_SIDE_GRACE_SECONDS,
    JobKind,
    WarehouseExecutor,
    get_executor,
)

logger = logging.getLogger(__name__)

BETA: Dict[str, Any] = {"x-stability": "beta"}

#: How far back a preview looks when no window is given.
DEFAULT_PREVIEW_DAYS = 7
#: The most runs ``GET /experiments/{id}/runs`` returns, newest first.
RUN_LIST_LIMIT = 50


class _ValuesOmittedRoute(APIRoute):
    """Answer a request that fails validation without repeating what was sent.

    FastAPI's default 422 carries each error's ``input`` (for a body-level
    error, the whole body) and ``ctx``.  A connection body can carry a
    service-account key, so this route class keeps only ``type``, ``loc`` and
    ``msg``.
    """

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                errors = [
                    {
                        "type": str(error.get("type", "")),
                        "loc": [str(part) for part in error.get("loc", ())],
                        "msg": str(error.get("msg", "")),
                    }
                    for error in exc.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": errors})

        return handler


router = APIRouter(route_class=_ValuesOmittedRoute)


# -- seams (overridden in tests) ----------------------------------------------------


def get_warehouse_executor() -> WarehouseExecutor:
    return get_executor()


def get_client_factory() -> clients.ClientFactory:
    return clients.build_client


def get_job_session_factory() -> runner.SessionFactory:
    from backend.app.db.session import SessionLocal

    return SessionLocal


def get_clock() -> runner.Clock:
    return runner.utc_now


def get_enabled_connectors() -> FrozenSet[str]:
    return connectors.ENABLED_CONNECTORS


def get_estimator() -> Callable[..., Dict[str, Any]]:
    """The proportion estimator ``/results`` uses (tests wrap it to record)."""
    from backend.app.services.sufficient_stats_analysis import binomial_metric_result

    return binomial_metric_result


def get_mean_estimator() -> Callable[..., Dict[str, Any]]:
    """The core mean estimator for centred sums (tests wrap it to record)."""
    from backend.app.services.sufficient_stats_analysis import mean_metric_result

    return mean_metric_result


def _limits() -> Any:
    from modules.backend.app.settings import settings

    return settings


# -- roles ----------------------------------------------------------------------------

ADMIN = ("ADMIN",)
EDITORS = ("ADMIN", "DEVELOPER")
READERS = ("ADMIN", "DEVELOPER", "ANALYST")
EVERYONE = ("ADMIN", "DEVELOPER", "ANALYST", "VIEWER")


def role_of(user: User) -> Optional[str]:
    if getattr(user, "is_superuser", False):
        return "ADMIN"
    role = getattr(user, "role", None)
    value = getattr(role, "value", role)
    return str(value).upper() if value else None


def _roles_text(roles: Tuple[str, ...]) -> str:
    if len(roles) == 1:
        return f"the {roles[0]} role"
    return "the " + ", ".join(roles[:-1]) + f" or {roles[-1]} role"


def require(user: User, allowed: Tuple[str, ...], action: str) -> None:
    """403 unless the caller's role is one of ``allowed``."""
    role = role_of(user)
    if role in allowed:
        return
    held = f"you are {role}" if role else "you have no role"
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "role_required",
            "message": f"{action} requires {_roles_text(allowed)}; {held}.",
        },
    )


def _a(kind: str) -> str:
    """``an assignment source`` / ``a metric source``."""
    return f"an {kind} source" if kind == "assignment" else f"a {kind} source"


def _source_roles(kind: str, *, deleting: bool = False) -> Tuple[str, ...]:
    if kind == "assignment" or deleting:
        return EDITORS
    return READERS


# -- errors ---------------------------------------------------------------------------


def _error(
    status_code: int,
    code: str,
    message: str,
    headers: Optional[Dict[str, str]] = None,
    **extra: Any,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, **extra},
        headers=headers,
    )


def _not_found(what: str) -> HTTPException:
    return _error(404, "not_found", f"{what} not found.")


def _warehouse_error(exc: WarehouseError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=exc.to_body(),
        headers=exc.response_headers() or None,
    )


def _credential_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CredentialKeysUnavailable):
        return _error(
            503,
            "credentials_unavailable",
            "Warehouse credentials can't be stored or used: "
            "WAREHOUSE_CREDENTIALS_KEYS is not set on this deployment. An operator "
            "must set it (see Self-hosting › Warehouse).",
        )
    return _error(
        409,
        "credentials_undecryptable",
        "This connection's stored credentials can't be read with the keys this "
        "deployment has. An admin must replace them (a new key file for BigQuery, "
        "a regenerated key pair for Snowflake).",
    )


def _refused_error(
    exc: Union[WarehouseQueryRefused, sources.SourceRefused],
) -> HTTPException:
    return _error(422, exc.code, str(exc), field=exc.field)


def _result_error(exc: WarehouseResultRefused) -> HTTPException:
    message = runner.run_message(exc.code) or "The warehouse result was refused."
    return _error(502, exc.code, message)


def _admission_error(exc: runner.AdmissionRefused) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code, detail=exc.detail(), headers=exc.headers or None
    )


def require_enabled(warehouse_type: str, enabled: FrozenSet[str]) -> None:
    try:
        connectors.require_enabled(warehouse_type, enabled)
    except connectors.ConnectorDisabled as exc:
        raise HTTPException(status_code=422, detail=exc.to_body()) from None


def _audit(
    db: Session,
    user: User,
    action: AuditAction,
    resource_type: str,
    resource_id: Any,
    *,
    old: Optional[Dict[str, Any]] = None,
    new: Optional[Dict[str, Any]] = None,
) -> None:
    AuditLogService(db).log(
        action=action,
        resource_type=resource_type,
        outcome=AuditOutcome.SUCCESS,
        resource_id=resource_id,
        actor_id=str(user.id),
        old_value=old,
        new_value=new,
    )


# -- shapes ---------------------------------------------------------------------------


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def connection_out(
    connection: WarehouseConnection, enabled: FrozenSet[str]
) -> Dict[str, Any]:
    fields = clients.PARAMETER_FIELDS.get(connection.warehouse_type, ())
    parameters = dict(connection.parameters or {})
    return {
        "id": connection.id,
        "name": connection.name,
        "warehouse_type": connection.warehouse_type,
        "parameters": {k: str(parameters[k]) for k in fields if k in parameters},
        "credentials_status": clients.credentials_status(connection),
        "public_key_fingerprint": connection.public_key_fingerprint,
        "pending_public_key_fingerprint": connection.pending_public_key_fingerprint,
        "external_id": connection.external_id,
        "query_timeout_seconds": connection.query_timeout_seconds,
        "max_bytes_per_query": connection.max_bytes_per_query,
        "max_runs_per_day": connection.max_runs_per_day,
        **clients.worst_case_per_day(connection),
        "enabled": connectors.is_enabled(connection.warehouse_type, enabled),
        "created_at": _utc(connection.created_at),
        "updated_at": _utc(connection.updated_at),
    }


def _connection_audit(connection: WarehouseConnection) -> Dict[str, Any]:
    """What the audit log records about a connection: never a secret."""
    return {
        "name": connection.name,
        "warehouse_type": connection.warehouse_type,
        "parameters": dict(connection.parameters or {}),
        "query_timeout_seconds": connection.query_timeout_seconds,
        "max_bytes_per_query": connection.max_bytes_per_query,
        "max_runs_per_day": connection.max_runs_per_day,
        "public_key_fingerprint": connection.public_key_fingerprint,
        "pending_public_key_fingerprint": connection.pending_public_key_fingerprint,
        "external_id": connection.external_id,
    }


def source_out(source: WarehouseSource) -> Dict[str, Any]:
    return {
        "id": source.id,
        "connection_id": source.connection_id,
        "kind": source.kind,
        "name": source.name,
        "table": ".".join(sources.table_parts(source)),
        "columns": {
            role: {"name": entry["name"], "type": entry.get("type")}
            for role, entry in (source.column_mapping or {}).items()
            if entry
        },
        "filters": list(source.filters or []),
        "metric_type": source.metric_type,
        "conversion_window_hours": source.conversion_window_hours,
        "cap_value": source.cap_value,
        "lower_is_better": sources.lower_is_better(source),
        "validated_at": _utc(source.validated_at),
        "created_at": _utc(source.created_at),
        "updated_at": _utc(source.updated_at),
    }


def run_out(run: WarehouseAnalysisRun, *, include_sql: bool) -> Dict[str, Any]:
    """A run as the API returns it.

    ``include_sql`` has no default on purpose: every caller decides, from the
    caller's role, whether ``statements`` is returned (D34).  When it is false
    the whole list is withheld -- not only the SQL text, but each statement's
    kind, dialect and SHA-256 too.
    """
    request = {k: v for k, v in (run.request or {}).items() if k != "total_seconds"}
    return {
        "id": run.id,
        "kind": run.kind,
        "status": run.status,
        "experiment_id": run.experiment_id,
        "connection_id": run.connection_id,
        "connection_name": run.connection_name,
        "warehouse_type": run.warehouse_type,
        "request": request,
        "window_start": _utc(run.window_start),
        "window_end": _utc(run.window_end),
        "error_code": run.error_code,
        "error_message": runner.run_message(run.error_code),
        "statements": run.statements if include_sql else None,
        # A failed or unfinished run never reports numbers.
        "results": run.results if run.status == "succeeded" else None,
        "job_metadata": run.job_metadata,
        "created_at": _utc(run.created_at),
        "started_at": _utc(run.started_at),
        "finished_at": _utc(run.finished_at),
    }


# -- loading ----------------------------------------------------------------------------


def _connection(db: Session, connection_id: uuid.UUID) -> WarehouseConnection:
    found = db.get(WarehouseConnection, connection_id)
    if found is None:
        raise _not_found("Warehouse connection")
    return found


def _source(db: Session, source_id: uuid.UUID) -> WarehouseSource:
    found = db.get(WarehouseSource, source_id)
    if found is None:
        raise _not_found("Warehouse source")
    return found


def _spec(
    connection: WarehouseConnection, *, pending: bool = False
) -> clients.ConnectionSpec:
    try:
        return clients.spec_for(connection, pending=pending)
    except (CredentialKeysUnavailable, CredentialUndecryptable) as exc:
        raise _credential_error(exc) from None


def _client(
    factory: clients.ClientFactory, spec: clients.ConnectionSpec
) -> clients.WarehouseClient:
    try:
        return factory(spec)
    except WarehouseError as exc:
        raise _warehouse_error(exc) from None


async def _on_executor(
    executor: WarehouseExecutor, job: Callable, *, total_seconds: float, kind: JobKind
) -> Any:
    try:
        return await executor.run(job, total_seconds=total_seconds, kind=kind)
    except WarehouseError as exc:
        raise _warehouse_error(exc) from None


def _check_limits(body: Any) -> None:
    settings = _limits()
    if body.query_timeout_seconds > settings.WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS:
        raise _error(
            422,
            "limit_too_high",
            "query_timeout_seconds is above this deployment's maximum of "
            f"{settings.WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS}.",
            field="query_timeout_seconds",
        )
    if body.max_runs_per_day > settings.WAREHOUSE_MAX_RUNS_PER_DAY:
        raise _error(
            422,
            "limit_too_high",
            "max_runs_per_day is above this deployment's maximum of "
            f"{settings.WAREHOUSE_MAX_RUNS_PER_DAY}.",
            field="max_runs_per_day",
        )
    cap = getattr(body, "max_bytes_per_query", None)
    if cap is not None and cap > settings.WAREHOUSE_MAX_BYTES_PER_QUERY:
        raise _error(
            422,
            "limit_too_high",
            "max_bytes_per_query is above this deployment's maximum of "
            f"{settings.WAREHOUSE_MAX_BYTES_PER_QUERY}.",
            field="max_bytes_per_query",
        )


def _parameters(body: Any) -> Dict[str, str]:
    """The non-secret parameters of a connection body."""
    kind = body.warehouse_type
    if kind == "snowflake" and body.role.upper() in clients.SNOWFLAKE_ADMIN_ROLES:
        raise _error(
            422,
            "role_not_allowed",
            "Use a role made for this connection with read-only grants. "
            "ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, ORGADMIN and USERADMIN "
            "aren't accepted.",
            field="role",
        )
    fields = [f for f in clients.PARAMETER_FIELDS[kind] if f != "client_email"]
    return {name: getattr(body, name) for name in fields}


def _service_account(text: str) -> Any:
    from modules.backend.app.warehouse.bigquery import (
        BigQueryConfigRefused,
        parse_service_account_json,
    )

    try:
        return parse_service_account_json(text)
    except BigQueryConfigRefused as exc:
        raise HTTPException(status_code=422, detail=exc.to_body()) from None


def _generate_key_pair(user: str, slot: int) -> Any:
    from modules.backend.app.warehouse.snowflake import generate_key_pair

    return generate_key_pair(user, slot=slot)


# -- connectors and connections ------------------------------------------------------------


@router.get(
    "/connectors",
    response_model=out.ConnectorListOut,
    summary="List the warehouse connectors and whether each is available",
    openapi_extra=BETA,
)
def list_connectors(
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
) -> Dict[str, Any]:
    require(current_user, EVERYONE, "Listing warehouse connectors")
    names = {
        "bigquery": "BigQuery",
        "snowflake": "Snowflake",
        "athena": "Amazon Athena",
    }
    return {
        "connectors": [
            {
                "warehouse_type": kind,
                "name": names[kind],
                "enabled": connectors.is_enabled(kind, enabled),
            }
            for kind in connectors.KNOWN_CONNECTORS
        ]
    }


@router.get(
    "/connections",
    response_model=out.ConnectionListOut,
    summary="List warehouse connections",
    openapi_extra=BETA,
)
def list_connections(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
) -> Dict[str, Any]:
    require(current_user, READERS, "Viewing warehouse connections")
    rows = db.execute(
        select(WarehouseConnection).order_by(WarehouseConnection.created_at)
    ).scalars()
    return {"connections": [connection_out(c, enabled) for c in rows]}


@router.post(
    "/connections",
    response_model=out.ConnectionCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a warehouse connection",
    openapi_extra=BETA,
)
def create_connection(
    body: Annotated[ConnectionCreate, Body()],
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
) -> Dict[str, Any]:
    require(current_user, ADMIN, "Creating a warehouse connection")
    require_enabled(body.warehouse_type, enabled)
    _check_limits(body)
    parameters = _parameters(body)
    connection = WarehouseConnection(
        name=body.name,
        warehouse_type=body.warehouse_type,
        parameters=parameters,
        query_timeout_seconds=body.query_timeout_seconds,
        max_runs_per_day=body.max_runs_per_day,
        max_bytes_per_query=getattr(body, "max_bytes_per_query", None),
        created_by=current_user.id,
    )
    shown = None
    try:
        if body.warehouse_type == "bigquery":
            key = _service_account(body.service_account_json)
            parameters["client_email"] = key.client_email
            connection.parameters = parameters
            connection.set_credentials(key.to_blob())
        elif body.warehouse_type == "snowflake":
            pair = _generate_key_pair(body.user, 1)
            connection.set_credentials(pair.private_blob)
            connection.public_key_fingerprint = pair.shown.fingerprint
            connection.parameters = {**parameters, "key_slot": "1"}
            shown = pair.shown.to_body()
        else:
            connection.external_id = clients.new_external_id()
    except CredentialKeysUnavailable as exc:
        raise _credential_error(exc) from None
    db.add(connection)
    db.flush()
    _audit(
        db,
        current_user,
        AuditAction.CREATE,
        "warehouse_connection",
        connection.id,
        new=_connection_audit(connection),
    )
    db.commit()
    db.refresh(connection)
    return {**connection_out(connection, enabled), "public_key": shown}


@router.get(
    "/connections/{connection_id}",
    response_model=out.ConnectionOut,
    summary="Get a warehouse connection",
    openapi_extra=BETA,
)
def get_connection(
    connection_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
) -> Dict[str, Any]:
    require(current_user, READERS, "Viewing warehouse connections")
    return connection_out(_connection(db, connection_id), enabled)


@router.put(
    "/connections/{connection_id}",
    response_model=out.ConnectionOut,
    summary="Update a warehouse connection",
    openapi_extra=BETA,
)
def update_connection(
    connection_id: uuid.UUID,
    body: Annotated[ConnectionUpdate, Body()],
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
) -> Dict[str, Any]:
    require(current_user, ADMIN, "Changing a warehouse connection")
    connection = _connection(db, connection_id)
    if body.warehouse_type != connection.warehouse_type:
        raise _error(
            422,
            "warehouse_type_immutable",
            "A connection's warehouse type can't be changed; create a new connection.",
            field="warehouse_type",
        )
    require_enabled(connection.warehouse_type, enabled)
    _check_limits(body)
    before = _connection_audit(connection)
    kept = {
        k: v
        for k, v in (connection.parameters or {}).items()
        if k in ("client_email", "key_slot", "pending_key_slot")
    }
    connection.parameters = {**kept, **_parameters(body)}
    connection.name = body.name
    connection.query_timeout_seconds = body.query_timeout_seconds
    connection.max_runs_per_day = body.max_runs_per_day
    connection.max_bytes_per_query = getattr(body, "max_bytes_per_query", None)
    if getattr(body, "service_account_json", None) is not None:
        key = _service_account(body.service_account_json)
        clients.forget_cached_token(connection)
        try:
            connection.set_credentials(key.to_blob())
        except CredentialKeysUnavailable as exc:
            raise _credential_error(exc) from None
        connection.parameters = {
            **connection.parameters,
            "client_email": key.client_email,
        }
    _audit(
        db,
        current_user,
        AuditAction.UPDATE,
        "warehouse_connection",
        connection.id,
        old=before,
        new=_connection_audit(connection),
    )
    db.commit()
    db.refresh(connection)
    return connection_out(connection, enabled)


@router.delete(
    "/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a warehouse connection, its credentials and its sources",
    openapi_extra=BETA,
)
def delete_connection(
    connection_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    """The row is deleted, not marked: its credentials go with it, its sources
    are deleted with it, and its runs keep their own copy of its name."""
    require(current_user, ADMIN, "Deleting a warehouse connection")
    connection = _connection(db, connection_id)
    before = _connection_audit(connection)
    clients.forget_cached_token(connection)
    db.execute(
        delete(WarehouseConnection).where(WarehouseConnection.id == connection_id)
    )
    _audit(
        db,
        current_user,
        AuditAction.DELETE,
        "warehouse_connection",
        connection_id,
        old=before,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _check_job(client: clients.WarehouseClient) -> Callable:
    def job(deadline: Any) -> Any:
        return client.adapter.check_connection(deadline)

    return job


@router.post(
    "/connections/test",
    response_model=out.ConnectionTestOut,
    summary="Test a warehouse connection before saving it",
    openapi_extra=BETA,
)
async def test_unsaved_connection(
    body: Annotated[ConnectionTest, Body()],
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
    executor: WarehouseExecutor = Depends(get_warehouse_executor),
    factory: clients.ClientFactory = Depends(get_client_factory),
) -> Dict[str, Any]:
    """The configuration is held in memory for the test and not stored.
    Snowflake connections are tested after they are created, when their key
    pair exists."""
    require(current_user, ADMIN, "Testing a warehouse connection")
    require_enabled(body.warehouse_type, enabled)
    _check_limits(body)
    parameters = _parameters(body)
    credential = None
    if body.warehouse_type == "bigquery":
        key = _service_account(body.service_account_json)
        parameters["client_email"] = key.client_email
        credential = key.to_blob()
    spec = clients.ConnectionSpec(
        warehouse_type=body.warehouse_type,
        parameters=parameters,
        query_timeout_seconds=body.query_timeout_seconds,
        max_bytes_per_query=getattr(body, "max_bytes_per_query", None),
        credential=credential,
    )
    client = _client(factory, spec)
    await _on_executor(
        executor,
        _check_job(client),
        total_seconds=connection_test_total(),
        kind=JobKind.CONNECTION_TEST,
    )
    return {"ok": True, "warehouse_type": body.warehouse_type}


@router.post(
    "/connections/{connection_id}/test",
    response_model=out.ConnectionTestOut,
    summary="Test a saved warehouse connection",
    openapi_extra=BETA,
)
async def test_connection(
    connection_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
    executor: WarehouseExecutor = Depends(get_warehouse_executor),
    factory: clients.ClientFactory = Depends(get_client_factory),
) -> Dict[str, Any]:
    """Signs in and runs a statement that reads no table.  A pending Snowflake
    key is tested in place of the current one and, if the test passes,
    becomes the current one.  Tests are not counted by the daily limit."""
    require(current_user, ADMIN, "Testing a warehouse connection")
    connection = await run_in_threadpool(_connection, db, connection_id)
    require_enabled(connection.warehouse_type, enabled)
    pending = connection.pending_credentials_ciphertext is not None
    spec = _spec(connection, pending=pending)
    client = _client(factory, spec)
    await _on_executor(
        executor,
        _check_job(client),
        total_seconds=connection_test_total(),
        kind=JobKind.CONNECTION_TEST,
    )
    if pending:
        await run_in_threadpool(_promote_pending_key, db, connection, current_user)
    return {
        "ok": True,
        "warehouse_type": connection.warehouse_type,
        "promoted_pending_key": pending,
    }


def _promote_pending_key(
    db: Session, connection: WarehouseConnection, user: User
) -> None:
    before = _connection_audit(connection)
    parameters = dict(connection.parameters or {})
    parameters["key_slot"] = parameters.pop("pending_key_slot", "1")
    connection.credentials_ciphertext = connection.pending_credentials_ciphertext
    connection.public_key_fingerprint = connection.pending_public_key_fingerprint
    connection.pending_credentials_ciphertext = None
    connection.pending_public_key_fingerprint = None
    connection.parameters = parameters
    _audit(
        db,
        user,
        AuditAction.KEY_CREATE,
        "warehouse_connection",
        connection.id,
        old=before,
        new=_connection_audit(connection),
    )
    db.commit()


@router.post(
    "/connections/{connection_id}/regenerate-key",
    response_model=out.ConnectionCreatedOut,
    summary="Generate a new Snowflake key pair for a connection",
    openapi_extra=BETA,
)
def regenerate_key(
    connection_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
) -> Dict[str, Any]:
    """The new key is pending: the current one keeps working until a test
    with the new one passes.  The response carries the statement that
    registers the new public key in the user's other key slot."""
    require(current_user, ADMIN, "Regenerating a connection's key")
    connection = _connection(db, connection_id)
    if connection.warehouse_type != "snowflake":
        raise _error(
            422,
            "not_applicable",
            "Only Snowflake connections have a key pair generated by the platform.",
        )
    require_enabled(connection.warehouse_type, enabled)
    parameters = dict(connection.parameters or {})
    slot = 2 if parameters.get("key_slot", "1") == "1" else 1
    before = _connection_audit(connection)
    pair = _generate_key_pair(parameters["user"], slot)
    try:
        connection.set_credentials(pair.private_blob, pending=True)
    except CredentialKeysUnavailable as exc:
        raise _credential_error(exc) from None
    connection.pending_public_key_fingerprint = pair.shown.fingerprint
    connection.parameters = {**parameters, "pending_key_slot": str(slot)}
    _audit(
        db,
        current_user,
        AuditAction.KEY_CREATE,
        "warehouse_connection",
        connection.id,
        old=before,
        new=_connection_audit(connection),
    )
    db.commit()
    db.refresh(connection)
    return {**connection_out(connection, enabled), "public_key": pair.shown.to_body()}


# -- sources ------------------------------------------------------------------------------

SourceCreate = Annotated[
    Union[AssignmentSourceCreate, MetricSourceCreate], Field(discriminator="kind")
]
SourceUpdate = Annotated[
    Union[AssignmentSourceUpdate, MetricSourceUpdate], Field(discriminator="kind")
]


def _apply_source(source: WarehouseSource, body: Any, rules: Any) -> None:
    columns = body.columns.model_dump()
    filters = [f.model_dump() for f in body.filters]
    try:
        parts = sources.check_definition(rules, body.table, columns, filters)
    except WarehouseQueryRefused as exc:
        raise _refused_error(exc) from None
    source.name = body.name
    source.table_reference = {"parts": list(parts)}
    source.column_mapping = {
        role: {"name": name, "type": None}
        for role, name in columns.items()
        if name is not None
    }
    source.filters = filters
    source.validated_at = None
    if body.kind == "metric":
        source.metric_type = body.metric_type
        source.conversion_window_hours = body.conversion_window_hours
        source.cap_value = body.cap_value
        # The body is the whole definition: a PUT without it sets false.
        source.lower_is_better = body.lower_is_better


def _name_taken() -> HTTPException:
    return _error(
        409,
        "source_name_taken",
        "This connection already has a source of this kind with that name.",
        field="name",
    )


@router.get(
    "/sources",
    response_model=out.SourceListOut,
    summary="List warehouse sources",
    openapi_extra=BETA,
)
def list_sources(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Dict[str, Any]:
    require(current_user, READERS, "Viewing warehouse sources")
    rows = db.execute(
        select(WarehouseSource).order_by(WarehouseSource.created_at)
    ).scalars()
    return {"sources": [source_out(s) for s in rows]}


@router.post(
    "/sources",
    response_model=out.SourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a warehouse source (a table or view and a column mapping)",
    openapi_extra=BETA,
)
def create_source(
    body: Annotated[SourceCreate, Body()],
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Dict[str, Any]:
    require(
        current_user,
        _source_roles(body.kind),
        f"Creating {_a(body.kind)}",
    )
    connection = _connection(db, body.connection_id)
    source = WarehouseSource(
        connection_id=connection.id, kind=body.kind, created_by=current_user.id
    )
    _apply_source(source, body, sources.rules_for(connection.warehouse_type))
    db.add(source)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _name_taken() from None
    _audit(
        db,
        current_user,
        AuditAction.CREATE,
        "warehouse_source",
        source.id,
        new=sources.stored_definition(source),
    )
    db.commit()
    db.refresh(source)
    return source_out(source)


@router.get(
    "/sources/{source_id}",
    response_model=out.SourceOut,
    summary="Get a warehouse source",
    openapi_extra=BETA,
)
def get_source(
    source_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Dict[str, Any]:
    require(current_user, READERS, "Viewing warehouse sources")
    return source_out(_source(db, source_id))


@router.put(
    "/sources/{source_id}",
    response_model=out.SourceOut,
    summary="Update a warehouse source",
    openapi_extra=BETA,
)
def update_source(
    source_id: uuid.UUID,
    body: Annotated[SourceUpdate, Body()],
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Dict[str, Any]:
    source = _source(db, source_id)
    require(current_user, _source_roles(source.kind), f"Editing {_a(source.kind)}")
    if body.kind != source.kind:
        raise _error(
            422,
            "kind_immutable",
            "A source's kind can't be changed; create a new source.",
            field="kind",
        )
    connection = _connection(db, source.connection_id)
    before = sources.stored_definition(source)
    _apply_source(source, body, sources.rules_for(connection.warehouse_type))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _name_taken() from None
    _audit(
        db,
        current_user,
        AuditAction.UPDATE,
        "warehouse_source",
        source.id,
        old=before,
        new=sources.stored_definition(source),
    )
    db.commit()
    db.refresh(source)
    return source_out(source)


@router.delete(
    "/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a warehouse source",
    openapi_extra=BETA,
)
def delete_source(
    source_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    source = _source(db, source_id)
    require(
        current_user,
        _source_roles(source.kind, deleting=True),
        f"Deleting {_a(source.kind)}",
    )
    before = sources.stored_definition(source)
    db.execute(delete(WarehouseSource).where(WarehouseSource.id == source_id))
    _audit(
        db, current_user, AuditAction.DELETE, "warehouse_source", source_id, old=before
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _load_for_warehouse(
    db: Session, source_id: uuid.UUID
) -> Tuple[WarehouseSource, WarehouseConnection]:
    source = _source(db, source_id)
    return source, _connection(db, source.connection_id)


@router.post(
    "/sources/{source_id}/validate",
    response_model=out.ValidateOut,
    summary="Check a source's table and columns against the warehouse's metadata",
    openapi_extra=BETA,
)
async def validate_source(
    source_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
    executor: WarehouseExecutor = Depends(get_warehouse_executor),
    factory: clients.ClientFactory = Depends(get_client_factory),
) -> Dict[str, Any]:
    """Reads the table's column names and types; no query reads data.
    Validation is not counted by the daily limit."""
    source, connection = await run_in_threadpool(_load_for_warehouse, db, source_id)
    require(current_user, _source_roles(source.kind), f"Validating {_a(source.kind)}")
    require_enabled(connection.warehouse_type, enabled)
    client = _client(factory, _spec(connection))
    parts = sources.table_parts(source)

    def job(deadline: Any) -> Any:
        return client.adapter.table_columns(parts, deadline)

    try:
        columns = await _on_executor(
            executor,
            job,
            total_seconds=connection_test_total(),
            kind=JobKind.CONNECTION_TEST,
        )
    except HTTPException as exc:
        if (
            exc.status_code == 502
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == WarehouseErrorCode.OBJECT_NOT_FOUND.value
        ):
            raise _error(
                422,
                "identifier_not_found",
                "The table or view was not found, or the connection's role can't see it.",
                field="table",
            ) from None
        raise
    try:
        mapping, filters = sources.resolve_columns(source, client, columns)
    except sources.SourceRefused as exc:
        raise _refused_error(exc) from None

    def save() -> Dict[str, Any]:
        before = sources.stored_definition(source)
        sources.mark_validated(source, mapping, filters)
        _audit(
            db,
            current_user,
            AuditAction.UPDATE,
            "warehouse_source",
            source.id,
            old=before,
            new=sources.stored_definition(source),
        )
        db.commit()
        db.refresh(source)
        return source_out(source)

    return {
        "source": await run_in_threadpool(save),
        "columns": [{"name": name, "type": kind} for name, kind in columns],
    }


def _resolve_window(
    start: Optional[datetime],
    end: Optional[datetime],
    default_start: Optional[datetime],
    default_end: datetime,
) -> AnalysisWindow:
    """``[start, end)`` in UTC; a value without an offset is read as UTC."""
    resolved_start = _utc(start) if start is not None else _utc(default_start)
    resolved_end = _utc(end) if end is not None else _utc(default_end)
    if resolved_start is None:
        raise _error(
            422,
            "experiment_has_no_window",
            "The experiment has no start date. Give window_start and window_end.",
            field="window_start",
        )
    if not resolved_start < resolved_end:  # type: ignore[operator]
        raise _error(
            422,
            "invalid_window",
            "window_start must be before window_end.",
            field="window_start",
        )
    return AnalysisWindow(resolved_start, resolved_end)  # type: ignore[arg-type]


def _require_validated(source: WarehouseSource) -> None:
    if source.validated_at is None:
        raise _error(
            422,
            "source_not_validated",
            f"Validate the source “{source.name}” before using it.",
            field="source",
        )


async def _admit_and_wait(
    db: Session,
    executor: WarehouseExecutor,
    job: Callable[[Any, uuid.UUID], Any],
    new: runner.NewRun,
    *,
    kind: JobKind,
    total_seconds: float,
    now: datetime,
) -> Tuple[uuid.UUID, Any]:
    """Reserve, admit, release; return the run id and the job's future."""
    try:
        reservation, future = runner.reserve(
            executor,
            job,
            kind=kind,
            connection_id=new.connection_id,
            total_seconds=total_seconds,
        )
    except runner.AdmissionRefused as exc:
        raise _admission_error(exc) from None
    try:
        run_id = await run_in_threadpool(
            runner.admit,
            db,
            new,
            now=now,
            max_concurrent_runs=_limits().WAREHOUSE_MAX_CONCURRENT_RUNS,
        )
    except runner.AdmissionRefused as exc:
        reservation.cancel()
        raise _admission_error(exc) from None
    except BaseException:
        reservation.cancel()
        raise
    reservation.release(run_id)
    return run_id, future


@router.post(
    "/sources/{source_id}/preview",
    response_model=out.PreviewOut,
    summary="Preview a source: counts and time bounds, no rows",
    openapi_extra=BETA,
)
async def preview_source(
    source_id: uuid.UUID,
    body: Annotated[Optional[PreviewRequest], Body()] = None,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
    executor: WarehouseExecutor = Depends(get_warehouse_executor),
    factory: clients.ClientFactory = Depends(get_client_factory),
    session_factory: runner.SessionFactory = Depends(get_job_session_factory),
    clock: runner.Clock = Depends(get_clock),
) -> Dict[str, Any]:
    """One warehouse query, capped like a run's, that returns aggregates only.
    A preview counts against the connection's daily limit and, like a run,
    cannot start while another run or preview on the connection is in
    progress."""
    body = body or PreviewRequest()
    source, connection = await run_in_threadpool(_load_for_warehouse, db, source_id)
    require(current_user, _source_roles(source.kind), f"Previewing {_a(source.kind)}")
    require_enabled(connection.warehouse_type, enabled)
    _require_validated(source)
    now = clock()
    window = _resolve_window(
        body.window_start,
        body.window_end,
        now - timedelta(days=DEFAULT_PREVIEW_DAYS),
        now,
    )
    client = _client(factory, _spec(connection))
    is_mean = source.metric_type == "mean"
    try:
        if source.kind == "assignment":
            query = build_assignment_preview_query(
                client.dialect,
                sources.assignment_mapping(source),
                window,
                body.experiment_key,
            )
        else:
            query = build_metric_preview_query(
                client.dialect, sources.metric_mapping(source), window
            )
    except WarehouseQueryRefused as exc:
        raise _refused_error(exc) from None
    total = preview_total(connection.query_timeout_seconds)
    kind = source.kind

    def job(deadline: Any, run_id: uuid.UUID) -> Any:
        return runner.execute_preview(
            client, query, kind, is_mean, session_factory, clock, deadline, run_id
        )

    new = runner.NewRun(
        kind="preview",
        connection_id=connection.id,
        connection_name=connection.name,
        warehouse_type=connection.warehouse_type,
        max_runs_per_day=connection.max_runs_per_day,
        experiment_id=None,
        requested_by=current_user.id,
        request={
            "source_id": str(source.id),
            "experiment_key": body.experiment_key,
            "total_seconds": total,
        },
        window_start=window.start,
        window_end=window.end,
        statements=runner.statements_json([query]),
    )
    run_id, future = await _admit_and_wait(
        db, executor, job, new, kind=JobKind.PREVIEW, total_seconds=total, now=now
    )
    try:
        summary = await asyncio.wait_for(
            asyncio.wrap_future(future), timeout=total + REQUEST_SIDE_GRACE_SECONDS
        )
    except asyncio.TimeoutError:
        raise _warehouse_error(WarehouseError(WarehouseErrorCode.TIME_LIMIT)) from None
    except WarehouseError as exc:
        raise _warehouse_error(exc) from None
    except WarehouseResultRefused as exc:
        raise _result_error(exc) from None
    if summary is None:
        raise _warehouse_error(WarehouseError(WarehouseErrorCode.INTERNAL))
    return {
        "kind": source.kind,
        "window_start": window.start,
        "window_end": window.end,
        "run_id": run_id,
        **summary,
    }


# -- runs ---------------------------------------------------------------------------------


def _experiment(db: Session, experiment_id: uuid.UUID) -> Experiment:
    found = (
        db.query(Experiment)
        .options(joinedload(Experiment.variants))
        .filter(Experiment.id == experiment_id)
        .first()
    )
    if found is None:
        raise _not_found("Experiment")
    return found


def _run_sources(
    db: Session, body: RunCreate, connection: WarehouseConnection
) -> Tuple[WarehouseSource, List[WarehouseSource]]:
    assignment = _source(db, body.assignment_source_id)
    if assignment.kind != "assignment" or assignment.connection_id != connection.id:
        raise _error(
            422,
            "source_mismatch",
            "assignment_source_id must name an assignment source on this connection.",
            field="assignment_source_id",
        )
    _require_validated(assignment)
    metrics: List[WarehouseSource] = []
    for index, source_id in enumerate(body.metric_source_ids):
        metric = _source(db, source_id)
        if metric.kind != "metric" or metric.connection_id != connection.id:
            raise _error(
                422,
                "source_mismatch",
                "metric_source_ids must name metric sources on this connection.",
                field=f"metric_source_ids[{index}]",
            )
        _require_validated(metric)
        metrics.append(metric)
    return assignment, metrics


@router.post(
    "/experiments/{experiment_id}/runs",
    response_model=out.RunAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a warehouse analysis of an experiment",
    openapi_extra=BETA,
)
def start_run(
    experiment_id: uuid.UUID,
    body: RunCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    enabled: FrozenSet[str] = Depends(get_enabled_connectors),
    executor: WarehouseExecutor = Depends(get_warehouse_executor),
    factory: clients.ClientFactory = Depends(get_client_factory),
    session_factory: runner.SessionFactory = Depends(get_job_session_factory),
    clock: runner.Clock = Depends(get_clock),
    estimator: Callable[..., Dict[str, Any]] = Depends(get_estimator),
    mean_estimator: Callable[..., Dict[str, Any]] = Depends(get_mean_estimator),
) -> Dict[str, Any]:
    """Answers 202 with the run id; the run continues on the warehouse
    executor.  Poll ``GET /runs/{run_id}``."""
    require(current_user, EDITORS, "Starting a warehouse analysis")
    experiment = _experiment(db, experiment_id)
    connection = _connection(db, body.connection_id)
    require_enabled(connection.warehouse_type, enabled)
    assignment, metrics = _run_sources(db, body, connection)
    if not experiment.key:
        raise _error(
            422,
            "experiment_has_no_key",
            "The experiment has no key, so its exposures can't be found.",
        )
    variants = tuple(
        runner.VariantInfo(
            id=v.id,
            name=v.name,
            is_control=bool(v.is_control),
            traffic_allocation=v.traffic_allocation,
        )
        for v in experiment.variants
    )
    variant_ids = {v.id for v in variants}
    variant_map = dict(body.variant_map or {})
    unknown = sorted(
        label for label, vid in variant_map.items() if vid not in variant_ids
    )
    if unknown:
        raise _error(
            422,
            "unknown_variant",
            "variant_map names a variant id that is not one of this experiment's.",
            field="variant_map",
        )
    now = clock()
    end_default = now
    if experiment.end_date is not None:
        end_default = min(_utc(experiment.end_date), now)  # type: ignore[type-var]
    window = _resolve_window(
        body.window_start, body.window_end, experiment.start_date, end_default
    )
    client = _client(factory, _spec(connection))
    mapping = sources.assignment_mapping(assignment)
    try:
        diagnostics = build_diagnostics_query(
            client.dialect, mapping, experiment.key, window
        )
        # Each metric's direction is read from its own source here, in the
        # request, and frozen in the plan: an edit after admission does not
        # change a queued or running analysis (#1386).
        metric_plans = tuple(
            runner.MetricPlan(
                source_id=metric.id,
                name=metric.name,
                metric_type=metric.metric_type,
                is_primary=index == 0,
                lower_is_better=metric.lower_is_better is True,
                query=build_metric_query(
                    client.dialect,
                    mapping,
                    sources.metric_mapping(metric),
                    experiment.key,
                    window,
                ),
            )
            for index, metric in enumerate(metrics)
        )
    except WarehouseQueryRefused as exc:
        raise _refused_error(exc) from None
    plan = runner.RunPlan(
        client=client,
        diagnostics=diagnostics,
        metrics=metric_plans,
        variants=variants,
        variant_map=variant_map,
        fixed_allocation=_is_fixed_allocation(experiment.optimization_type),
        alpha=1.0 - body.confidence_level,
        correction_method=body.correction_method,
        session_factory=session_factory,
        clock=clock,
        estimator=estimator,
        mean_estimator=mean_estimator,
    )
    total = run_total(connection.query_timeout_seconds, len(metric_plans))
    new = runner.NewRun(
        kind="analysis",
        connection_id=connection.id,
        connection_name=connection.name,
        warehouse_type=connection.warehouse_type,
        max_runs_per_day=connection.max_runs_per_day,
        experiment_id=experiment.id,
        requested_by=current_user.id,
        request={
            "connection_id": str(connection.id),
            "assignment_source_id": str(assignment.id),
            "metric_source_ids": [str(m.id) for m in metrics],
            "variant_map": {label: str(vid) for label, vid in variant_map.items()},
            "confidence_level": body.confidence_level,
            "correction_method": body.correction_method,
            "total_seconds": total,
        },
        window_start=window.start,
        window_end=window.end,
        statements=runner.statements_json(
            [diagnostics, *(m.query for m in metric_plans)]
        ),
    )

    def job(deadline: Any, run_id: uuid.UUID) -> Any:
        return runner.execute_run(plan, deadline, run_id)

    try:
        reservation, _future = runner.reserve(
            executor,
            job,
            kind=JobKind.ANALYSIS,
            connection_id=connection.id,
            total_seconds=total,
        )
    except runner.AdmissionRefused as exc:
        raise _admission_error(exc) from None
    try:
        run_id = runner.admit(
            db,
            new,
            now=now,
            max_concurrent_runs=_limits().WAREHOUSE_MAX_CONCURRENT_RUNS,
        )
    except runner.AdmissionRefused as exc:
        reservation.cancel()
        raise _admission_error(exc) from None
    except BaseException:
        reservation.cancel()
        raise
    reservation.release(run_id)
    try:
        _audit(
            db,
            current_user,
            AuditAction.CREATE,
            "warehouse_analysis_run",
            run_id,
            new={k: v for k, v in new.request.items() if k != "total_seconds"},
        )
        db.commit()
    except Exception as exc:  # the run is admitted; the audit entry is best effort
        db.rollback()
        logger.warning("warehouse run audit entry failed: %s", type(exc).__name__)
    return {"run_id": run_id, "status": "queued"}


@router.get(
    "/experiments/{experiment_id}/runs",
    response_model=out.RunListOut,
    summary="List an experiment's warehouse analyses, newest first",
    openapi_extra=BETA,
)
def list_runs(
    experiment_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Dict[str, Any]:
    require(current_user, READERS, "Viewing warehouse analyses")
    rows = db.execute(
        select(WarehouseAnalysisRun)
        .where(
            WarehouseAnalysisRun.experiment_id == experiment_id,
            WarehouseAnalysisRun.kind == "analysis",
        )
        .order_by(WarehouseAnalysisRun.created_at.desc())
        .limit(RUN_LIST_LIMIT)
    ).scalars()
    include_sql = role_of(current_user) in READERS
    return {"runs": [run_out(r, include_sql=include_sql) for r in rows]}


@router.get(
    "/runs/{run_id}",
    response_model=out.RunOut,
    summary="Get a warehouse analysis or preview, with its results and SQL",
    openapi_extra=BETA,
)
def get_run(
    run_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Dict[str, Any]:
    require(current_user, READERS, "Viewing warehouse analyses")
    run = db.get(WarehouseAnalysisRun, run_id)
    if run is None:
        raise _not_found("Warehouse run")
    return run_out(run, include_sql=role_of(current_user) in READERS)
