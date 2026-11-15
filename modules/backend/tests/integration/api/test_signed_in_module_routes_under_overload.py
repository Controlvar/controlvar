"""The modules' signed-in routes answer every request of a burst larger than
they can serve at once, and answer again after it.

The same check as ``backend/tests/integration/api/test_signed_in_routes_under_overload.py``
(its module docstring describes the harness), for the routes that exist only
in the full profile: the invite preview, and one route of each module shape.
The harness is imported from that core file; these rows live here because a
core file never imports a module (``backend/tests/smoke/test_core_boundary.py``).

The warehouse routes whose handlers are ``async`` (the two connection tests,
validate and preview) wait on the warehouse executor and on worker threads.
Their rows run them to their normal answer with a stand-in executor and
client factory, installed with ``monkeypatch`` because the harness wants
``app.dependency_overrides`` empty. The refusal table and the writes below pin
what those four routes answer and store, so that where their database steps
run can change without any answer changing.
"""

from __future__ import annotations

import uuid
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from cryptography.fernet import Fernet

from backend.app.models.compliance_audit_event import ComplianceAuditEvent
from backend.app.models.user import UserRole
from backend.tests.integration.api.test_signed_in_routes_under_overload import (
    REQUESTS,
    PoolWatch,
    Row,
    Send,
    World,
    _bearer,
    _client,
    _get,
    check_a_burst,
    small_pool,
    world,
)
from modules.backend.app import settings as modules_settings
from modules.backend.app.api.v1.endpoints import etl, realtime_counters
from modules.backend.app.api.v1.endpoints import warehouse_analysis as wa
from modules.backend.app.models.warehouse_analysis_run import WarehouseAnalysisRun
from modules.backend.app.models.warehouse_connection import WarehouseConnection
from modules.backend.app.models.warehouse_source import WarehouseSource
from modules.backend.app.models.workspace import (
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
)
from modules.backend.app.schemas.etl import GlueCrawlerStatus
from modules.backend.app.schemas.realtime_counters import ExperimentCounters
from modules.backend.app.services import warehouse_clients as clients
from modules.backend.app.services import warehouse_runner as runner
from modules.backend.app.services.warehouse_query_builder import (
    ATHENA_SQL,
    BuiltQuery,
)
from modules.backend.app.services.workspace_service import workspace_service
from modules.backend.app.warehouse import connectors
from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode

pytestmark = pytest.mark.integration


def _invite_preview(world: World) -> Send:
    owner = world.user()
    suffix = uuid.uuid4().hex[:8]
    workspace = workspace_service.create_workspace(
        db=world.db,
        name=f"Overload {suffix}",
        slug=f"overload-{suffix}",
        owner_id=owner.id,
    )
    invite = workspace_service.create_invite(
        world.db, workspace.id, f"invitee_{suffix}@int.test", "VIEWER", owner.id
    )

    def remove():
        world.db.rollback()
        for model in (WorkspaceInvite, WorkspaceMember):
            world.db.query(model).filter(model.workspace_id == workspace.id).delete(
                synchronize_session=False
            )
        world.db.query(Workspace).filter(Workspace.id == workspace.id).delete()
        world.db.commit()

    world.cleanups.append(remove)
    return _get(f"/api/v1/workspaces/invites/{invite.token}", owner)


def _warehouse_connectors(world: World) -> Send:
    return _get("/api/v1/warehouse/analysis/connectors", world.user())


def _etl_crawler_status(world: World) -> Send:
    class Glue:
        def get_crawler_status(self, crawler_name=None):
            return GlueCrawlerStatus(crawler_name="overload", state="READY")

    # The service, not the dependency: the real get_etl_service returns it.
    world.monkeypatch.setattr(etl, "_etl_service", Glue())
    return _get("/api/v1/etl/crawler/status", world.user())


def _counters(world: World) -> Send:
    class Counters:
        def get_experiment_counters(self, experiment_id):
            return ExperimentCounters(
                experiment_id=experiment_id,
                total_assignments=1,
                total_events=1,
                total_conversions=0,
                variants=[],
            )

    # The service, not the dependency: the real get_counter_service returns it.
    world.monkeypatch.setattr(realtime_counters, "_counter_service", Counters())
    return _get(f"/api/v1/counters/{uuid.uuid4()}", world.user())


def _warehouse_run(world: World) -> Send:
    """The sign-in, the seams and the handler's worker trip run; the
    experiment does not exist, so the answer is 404 and no result is
    validated."""
    headers = _bearer(world.user())
    body = {
        "connection_id": str(uuid.uuid4()),
        "assignment_source_id": str(uuid.uuid4()),
        "metric_source_ids": [str(uuid.uuid4())],
    }

    def send(http, _index):
        return http.post(
            f"/api/v1/warehouse/analysis/experiments/{uuid.uuid4()}/runs",
            json=body,
            headers=headers,
        )

    return send


# ---------------------------------------------------------------------------
# The warehouse routes with async handlers
# ---------------------------------------------------------------------------

WA = "/api/v1/warehouse/analysis"
ALL_CONNECTORS = frozenset({"athena", "bigquery", "snowflake"})
#: What the stand-in warehouse says the table has.
COLUMNS = (
    ("user_id", "VARCHAR"),
    ("experiment_key", "VARCHAR"),
    ("variant", "VARCHAR"),
    ("exposed_at", "TIMESTAMP"),
)
MAPPING = {
    "unit_id": "user_id",
    "experiment_key": "experiment_key",
    "variant": "variant",
    "exposed_at": "exposed_at",
}
#: What the stand-in executor answers for a preview.
PREVIEW_SUMMARY: Dict[str, Any] = {
    "total_rows": 3,
    "null_unit_rows": 0,
    "null_variant_rows": 0,
    "variants": [],
    "earliest": None,
    "latest": None,
}
ATHENA_BODY: Dict[str, Any] = {
    "warehouse_type": "athena",
    "name": "Lake",
    "region": "us-east-1",
    "role_arn": "arn:aws:iam::123456789012:role/controlvar-warehouse/controlvar-reader",
    "workgroup": "primary",
    "database": "main",
    "max_bytes_per_query": 10_000_000_000,
}
#: The clock the refusal table runs at (the daily limit's answer names it).
NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


class Fakes:
    """The warehouse's stand-ins: the executor the route's seam returns (its
    ``run`` calls the job at once; ``try_submit`` hands back a finished
    preview without running the job, so no run row is ever updated) and the
    client factory (an Athena-dialect client whose adapter answers from
    ``columns``). Each ``*_error`` is raised where the real one would."""

    def __init__(self) -> None:
        self.columns = COLUMNS
        self.adapter_error: Optional[BaseException] = None
        self.factory_error: Optional[BaseException] = None
        self.submit_error: Optional[BaseException] = None

    def reset(self) -> None:
        self.__init__()

    def client(self, spec: clients.ConnectionSpec) -> clients.WarehouseClient:
        if self.factory_error is not None:
            raise self.factory_error
        return clients.WarehouseClient(
            spec.warehouse_type,
            _Adapter(self),
            ATHENA_SQL,
            clients.TIME_TYPES["athena"],
            clients.NUMERIC_TYPES["athena"],
        )

    async def run(self, job, *, total_seconds, kind):
        return job(None)

    def try_submit(self, job, *, total_seconds, kind, organisation=None, key=None):
        if self.submit_error is not None:
            raise self.submit_error
        future: Future = Future()
        future.set_result(dict(PREVIEW_SUMMARY))
        return future


class _Adapter:
    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    def table_columns(self, parts, deadline):
        if self._fakes.adapter_error is not None:
            raise self._fakes.adapter_error
        return self._fakes.columns

    def check_connection(self, deadline):
        if self._fakes.adapter_error is not None:
            raise self._fakes.adapter_error
        return {"ok": True}


def install_fakes(world: World) -> Fakes:
    fakes = Fakes()
    world.monkeypatch.setattr(wa, "get_executor", lambda: fakes)
    world.monkeypatch.setattr(clients, "build_client", fakes.client)
    world.monkeypatch.setattr(connectors, "ENABLED_CONNECTORS", ALL_CONNECTORS)
    # Every preview of a burst stays admitted (the stand-in never runs it).
    world.monkeypatch.setattr(
        modules_settings.settings, "WAREHOUSE_MAX_CONCURRENT_RUNS", 32
    )
    # The preview's statement, which the stand-in never sends: the builder's
    # own check of the statement needs sqlglot, which not every test
    # environment installs, and the statement is not what these rows test.
    world.monkeypatch.setattr(
        wa,
        "build_assignment_preview_query",
        lambda *_args, **_kwargs: BuiltQuery("preview", "athena", "SELECT 1"),
    )
    return fakes


def _remove_warehouse_rows(world: World, connection_ids: List[uuid.UUID]) -> None:
    world.db.rollback()
    world.db.query(WarehouseAnalysisRun).filter(
        WarehouseAnalysisRun.connection_id.in_(connection_ids)
    ).delete(synchronize_session=False)
    world.db.query(WarehouseSource).filter(
        WarehouseSource.connection_id.in_(connection_ids)
    ).delete(synchronize_session=False)
    world.db.query(WarehouseConnection).filter(
        WarehouseConnection.id.in_(connection_ids)
    ).delete(synchronize_session=False)
    world.db.commit()


def make_connection(world: World, warehouse_type: str = "athena", **values):
    """A stored connection (``credential``: stored with ``keys``)."""
    owner = values.pop("owner", None) or world.user(UserRole.ADMIN)
    credential = values.pop("credential", None)
    pending = values.pop("pending", None)
    keys = values.pop("keys", None)
    parameters = {
        "athena": {
            "region": "us-east-1",
            "role_arn": "arn:aws:iam::123456789012:role/controlvar-warehouse/controlvar-reader",
            "workgroup": "primary",
            "database": "main",
        },
        "bigquery": {
            "billing_project": "overload",
            "location": "US",
            "client_email": "reader@overload.iam.gserviceaccount.com",
        },
        "snowflake": {
            "account": "overload",
            "user": "READER",
            "role": "READER",
            "warehouse": "WH",
        },
    }[warehouse_type]
    connection = WarehouseConnection(
        name=values.pop("name", f"Overload {uuid.uuid4().hex[:8]}"),
        warehouse_type=warehouse_type,
        parameters={**parameters, **values.pop("parameters", {})},
        external_id=clients.new_external_id() if warehouse_type == "athena" else None,
        query_timeout_seconds=60,
        max_bytes_per_query=10_000_000_000
        if warehouse_type in ("athena", "bigquery")
        else None,
        max_runs_per_day=values.pop("max_runs_per_day", 20),
        created_by=owner.id,
    )
    if credential is not None:
        connection.set_credentials(credential, keys=keys)
    if pending is not None:
        connection.set_credentials(pending, pending=True, keys=keys)
        connection.pending_public_key_fingerprint = "SHA256:pending"
    connection.public_key_fingerprint = values.pop("public_key_fingerprint", None)
    world.db.add(connection)
    world.db.commit()
    world.db.refresh(connection)
    world.cleanups.append(lambda: _remove_warehouse_rows(world, [connection.id]))
    return connection


def make_source(world: World, connection, *, validated: bool = False, **values):
    kind = values.pop("kind", "assignment")
    columns = values.pop("columns", MAPPING)
    source = WarehouseSource(
        connection_id=connection.id,
        kind=kind,
        name=values.pop("name", f"Exposures {uuid.uuid4().hex[:8]}"),
        table_reference={"parts": ["main", "exposures"]},
        column_mapping={
            role: {"name": name, "type": None} for role, name in columns.items()
        },
        filters=[],
        metric_type="proportion" if kind == "metric" else None,
        conversion_window_hours=168 if kind == "metric" else None,
        validated_at=NOW if validated else None,
        created_by=connection.created_by,
    )
    world.db.add(source)
    world.db.commit()
    world.db.refresh(source)
    return source


def _post(path: str, user, body: Optional[Dict[str, Any]] = None) -> Send:
    headers = _bearer(user)

    def send(http, _index):
        return http.post(path, json=body, headers=headers)

    return send


def _validate_unknown_source(world: World) -> Send:
    """A VIEWER asks to validate a source that does not exist: the load comes
    before the role check, so the normal answer is 404."""
    install_fakes(world)
    headers = _bearer(world.user(UserRole.VIEWER))

    def send(http, _index):
        return http.post(f"{WA}/sources/{uuid.uuid4()}/validate", headers=headers)

    return send


def _validate(world: World) -> Send:
    """Each request validates a source of its own."""
    install_fakes(world)
    connection = make_connection(world)
    sources = [make_source(world, connection) for _ in range(REQUESTS)]
    headers = _bearer(world.user(UserRole.DEVELOPER))

    def send(http, index):
        return http.post(f"{WA}/sources/{sources[index].id}/validate", headers=headers)

    return send


def _test_connection(world: World) -> Send:
    install_fakes(world)
    connection = make_connection(world)
    return _post(f"{WA}/connections/{connection.id}/test", world.user(UserRole.ADMIN))


def _test_unsaved_connection(world: World) -> Send:
    install_fakes(world)
    return _post(f"{WA}/connections/test", world.user(UserRole.ADMIN), ATHENA_BODY)


def _preview(world: World) -> Send:
    """Each request previews a validated source on a connection of its own (a
    connection takes one preview at a time)."""
    install_fakes(world)
    owner = world.user(UserRole.ADMIN)
    sources = [
        make_source(world, make_connection(world, owner=owner), validated=True)
        for _ in range(REQUESTS)
    ]
    headers = _bearer(world.user(UserRole.DEVELOPER))

    def send(http, index):
        return http.post(
            f"{WA}/sources/{sources[index].id}/preview", json={}, headers=headers
        )

    return send


#: One route for each module shape of signed-in route (named as in the core
#: file's parameter ids), the invite preview, and the warehouse routes whose
#: handlers are ``async``.
ROWS = [
    pytest.param(
        "GET /api/v1/workspaces/invites/{token}",
        Row(_invite_preview, 200, True),
        id="invite-preview",
    ),
    pytest.param(
        "GET /api/v1/warehouse/analysis/connectors",
        Row(_warehouse_connectors, 200, True),
        id="user-active-connectors-handler-validation",
    ),
    pytest.param(
        "GET /api/v1/etl/crawler/status",
        Row(_etl_crawler_status, 200, True),
        id="user-active-etl-handler-validation",
    ),
    pytest.param(
        "GET /api/v1/counters/{experiment_id}",
        Row(_counters, 200, True),
        id="user-active-counters-handler-validation",
    ),
    pytest.param(
        "POST /api/v1/warehouse/analysis/experiments/{experiment_id}/runs",
        Row(_warehouse_run, 404, True),
        id="user-active-warehouse-seams-handler-validation",
    ),
    pytest.param(
        "POST /api/v1/warehouse/analysis/sources/{source_id}/validate (VIEWER)",
        Row(_validate_unknown_source, 404, False, steps_in_workers=True),
        id="async-warehouse-validate-unknown-source",
    ),
    pytest.param(
        "POST /api/v1/warehouse/analysis/sources/{source_id}/validate",
        Row(_validate, 200, False, steps_in_workers=True),
        id="async-warehouse-validate",
    ),
    pytest.param(
        "POST /api/v1/warehouse/analysis/connections/{connection_id}/test",
        Row(_test_connection, 200, False, steps_in_workers=True),
        id="async-warehouse-connection-test",
    ),
    pytest.param(
        "POST /api/v1/warehouse/analysis/connections/test",
        Row(_test_unsaved_connection, 200, False, steps_in_workers=True),
        id="async-warehouse-unsaved-connection-test",
    ),
    pytest.param(
        "POST /api/v1/warehouse/analysis/sources/{source_id}/preview",
        Row(_preview, 200, False, steps_in_workers=True),
        id="async-warehouse-seams-preview",
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize("label, row", ROWS)
async def test_every_request_of_a_burst_is_answered(
    label: str, row: Row, small_pool: PoolWatch, world: World
):
    await check_a_burst(label, row, small_pool, world)


# ---------------------------------------------------------------------------
# The four warehouse routes: refusals and writes, unchanged
# ---------------------------------------------------------------------------


def _detail(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"detail": {"code": code, "message": message, **extra}}


def _role_refused(action: str, roles: str, role: str) -> Dict[str, Any]:
    return _detail("role_required", f"{action} requires {roles}; you are {role}.")


TESTING = "Testing a warehouse connection"
EDITORS = "the ADMIN or DEVELOPER role"
CONNECTOR_OFF = _detail(
    "connector_disabled", "Amazon Athena isn't available on this deployment yet."
)
KEYS_UNAVAILABLE = _detail(
    "credentials_unavailable",
    "Warehouse credentials can't be stored or used: WAREHOUSE_CREDENTIALS_KEYS "
    "is not set on this deployment. An operator must set it (see Self-hosting › "
    "Warehouse).",
)
KEYS_WRONG = _detail(
    "credentials_undecryptable",
    "This connection's stored credentials can't be read with the keys this "
    "deployment has. An admin must replace them (a new key file for BigQuery, a "
    "regenerated key pair for Snowflake).",
)
PERMISSION_DENIED = _detail(
    "permission_denied", "The warehouse role does not have permission for this query."
)
SOURCE_NOT_FOUND = _detail("not_found", "Warehouse source not found.")
BUSY = _detail(
    "warehouse_busy",
    "Another warehouse analysis is using this deployment's capacity. Try again "
    "shortly.",
)
IN_PROGRESS = _detail(
    "run_in_progress", "A warehouse job for this connection is already running."
)

#: label -> (status, body, Retry-After). Taken from the code before the change
#: that moved these routes' database steps, and unchanged by it. Rows that fail
#: two checks at once pin the order of the checks: the role before the
#: connection's existence (test), the source's existence before the role
#: (validate, preview: the role depends on the source's kind), the role before
#: the connector, the connector before validation, validation before the window.
EXPECTED: Dict[str, Any] = {
    "test: VIEWER, unknown": (
        403,
        _role_refused(TESTING, "the ADMIN role", "VIEWER"),
        None,
    ),
    "test: DEVELOPER": (
        403,
        _role_refused(TESTING, "the ADMIN role", "DEVELOPER"),
        None,
    ),
    "test: ADMIN, unknown": (
        404,
        _detail("not_found", "Warehouse connection not found."),
        None,
    ),
    "test: connector off": (422, CONNECTOR_OFF, None),
    "test: keys unset": (503, KEYS_UNAVAILABLE, None),
    "test: keys wrong": (409, KEYS_WRONG, None),
    "test: client refused": (
        500,
        _detail(
            "internal",
            "Something went wrong on our side while talking to the warehouse.",
        ),
        None,
    ),
    "test: warehouse refuses": (
        502,
        _detail(
            "auth_failed", "The warehouse did not accept the connection's credentials."
        ),
        None,
    ),
    "unsaved: VIEWER": (403, _role_refused(TESTING, "the ADMIN role", "VIEWER"), None),
    "unsaved: VIEWER, connector off": (
        403,
        _role_refused(TESTING, "the ADMIN role", "VIEWER"),
        None,
    ),
    "unsaved: connector off": (422, CONNECTOR_OFF, None),
    "unsaved: limit too high": (
        422,
        _detail(
            "limit_too_high",
            "query_timeout_seconds is above this deployment's maximum of 100.",
            field="query_timeout_seconds",
        ),
        None,
    ),
    "unsaved: warehouse refuses": (502, PERMISSION_DENIED, None),
    "validate: VIEWER, unknown": (404, SOURCE_NOT_FOUND, None),
    "validate: VIEWER": (
        403,
        _role_refused("Validating an assignment source", EDITORS, "VIEWER"),
        None,
    ),
    "validate: ANALYST": (
        403,
        _role_refused("Validating an assignment source", EDITORS, "ANALYST"),
        None,
    ),
    "validate: VIEWER, connector off": (
        403,
        _role_refused("Validating an assignment source", EDITORS, "VIEWER"),
        None,
    ),
    "validate: connector off": (422, CONNECTOR_OFF, None),
    "validate: keys unset": (503, KEYS_UNAVAILABLE, None),
    "validate: table not found": (
        422,
        _detail(
            "identifier_not_found",
            "The table or view was not found, or the connection's role can't see it.",
            field="table",
        ),
        None,
    ),
    "validate: warehouse refuses": (502, PERMISSION_DENIED, None),
    "validate: column missing": (
        422,
        _detail(
            "identifier_not_found",
            "columns.unit_id names a column the table does not have",
            field="columns.unit_id",
        ),
        None,
    ),
    "validate: time column is text": (
        422,
        _detail(
            "unsupported_column_type",
            "columns.exposed_at must be a timestamp column",
            field="columns.exposed_at",
        ),
        None,
    ),
    "preview: VIEWER, unknown": (404, SOURCE_NOT_FOUND, None),
    "preview: VIEWER": (
        403,
        _role_refused("Previewing an assignment source", EDITORS, "VIEWER"),
        None,
    ),
    "preview: connector off, not validated": (422, CONNECTOR_OFF, None),
    "preview: not validated, bad window": (
        422,
        _detail(
            "source_not_validated",
            "Validate the source “Fresh” before using it.",
            field="source",
        ),
        None,
    ),
    "preview: bad window": (
        422,
        _detail(
            "invalid_window",
            "window_start must be before window_end.",
            field="window_start",
        ),
        None,
    ),
    "preview: keys unset": (503, KEYS_UNAVAILABLE, None),
    "preview: executor busy": (429, BUSY, "30"),
    "preview: executor has the connection": (409, IN_PROGRESS, None),
    "preview: daily limit": (
        429,
        _detail(
            "daily_run_limit_reached",
            "Not run: Full has reached its limit of 1 analyses per day (UTC, "
            "previews included). The count resets at 00:00 UTC, in 12 h 0 min. "
            "An admin can change the limit in Warehouse › Connections › Full.",
            limit=1,
            resets_at="2026-09-21T00:00:00Z",
        ),
        "43200",
    ),
    "preview: a run in progress": (409, IN_PROGRESS, None),
    "preview: runs at the cap": (429, BUSY, "30"),
}


async def _answers(world: World, fakes: Fakes) -> Dict[str, Any]:
    keys = Fernet.generate_key().decode()
    other_keys = Fernet.generate_key().decode()
    world.monkeypatch.setattr(runner, "utc_now", lambda: NOW)
    settings = modules_settings.settings
    viewer = world.user(UserRole.VIEWER)
    analyst = world.user(UserRole.ANALYST)
    developer = world.user(UserRole.DEVELOPER)
    admin = world.user(UserRole.ADMIN)
    owner = admin

    lake = make_connection(world, owner=owner, name="Lake")
    fresh = make_source(world, lake, name="Fresh")
    ready = make_source(world, lake, name="Ready", validated=True)
    bq = make_connection(
        world, "bigquery", owner=owner, credential=b"key-file", keys=keys
    )
    bq_ready = make_source(world, bq, name="BQ ready", validated=True)
    full = make_connection(world, owner=owner, name="Full", max_runs_per_day=1)
    full_ready = make_source(world, full, name="Full", validated=True)
    busy = make_connection(world, owner=owner, name="Busy")
    busy_ready = make_source(world, busy, name="Busy", validated=True)
    for connection, status in ((full, "succeeded"), (busy, "queued")):
        world.db.add(
            WarehouseAnalysisRun(
                kind="preview",
                status=status,
                connection_id=connection.id,
                connection_name=connection.name,
                warehouse_type=connection.warehouse_type,
                request={"total_seconds": 600},
                statements=[],
                created_at=(NOW - timedelta(minutes=5)).replace(tzinfo=None),
                updated_at=(NOW - timedelta(minutes=5)).replace(tzinfo=None),
            )
        )
    world.db.commit()

    unknown = uuid.uuid4()
    off = frozenset({"bigquery", "snowflake"})
    late_window = {
        "window_start": "2026-09-10T00:00:00Z",
        "window_end": "2026-09-09T00:00:00Z",
    }

    def warehouse(code):
        return lambda: setattr(fakes, "adapter_error", WarehouseError(code))

    def enabled(value):
        return lambda: world.monkeypatch.setattr(
            connectors, "ENABLED_CONNECTORS", value
        )

    def keys_are(value):
        return lambda: world.monkeypatch.setattr(
            settings, "WAREHOUSE_CREDENTIALS_KEYS", value
        )

    def nothing():
        return None

    test_path = f"{WA}/connections/{{}}/test"
    validate_path = f"{WA}/sources/{{}}/validate"
    preview_path = f"{WA}/sources/{{}}/preview"
    cases = [
        # POST /connections/{id}/test: ADMIN only, checked before the load.
        ("test: VIEWER, unknown", nothing, test_path.format(unknown), viewer, None),
        ("test: DEVELOPER", nothing, test_path.format(lake.id), developer, None),
        ("test: ADMIN, unknown", nothing, test_path.format(unknown), admin, None),
        ("test: connector off", enabled(off), test_path.format(lake.id), admin, None),
        ("test: keys unset", keys_are(None), test_path.format(bq.id), admin, None),
        (
            "test: keys wrong",
            keys_are(other_keys),
            test_path.format(bq.id),
            admin,
            None,
        ),
        (
            "test: client refused",
            lambda: setattr(
                fakes, "factory_error", WarehouseError(WarehouseErrorCode.INTERNAL)
            ),
            test_path.format(lake.id),
            admin,
            None,
        ),
        (
            "test: warehouse refuses",
            warehouse(WarehouseErrorCode.AUTH_FAILED),
            test_path.format(lake.id),
            admin,
            None,
        ),
        # POST /connections/test (nothing stored).
        ("unsaved: VIEWER", nothing, f"{WA}/connections/test", viewer, ATHENA_BODY),
        (
            "unsaved: VIEWER, connector off",
            enabled(off),
            f"{WA}/connections/test",
            viewer,
            ATHENA_BODY,
        ),
        (
            "unsaved: connector off",
            enabled(off),
            f"{WA}/connections/test",
            admin,
            ATHENA_BODY,
        ),
        (
            "unsaved: limit too high",
            lambda: world.monkeypatch.setattr(
                settings, "WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS", 100
            ),
            f"{WA}/connections/test",
            admin,
            ATHENA_BODY,
        ),
        (
            "unsaved: warehouse refuses",
            warehouse(WarehouseErrorCode.PERMISSION_DENIED),
            f"{WA}/connections/test",
            admin,
            ATHENA_BODY,
        ),
        # POST /sources/{id}/validate: the role depends on the source's kind,
        # so the source is loaded first.
        (
            "validate: VIEWER, unknown",
            nothing,
            validate_path.format(unknown),
            viewer,
            None,
        ),
        ("validate: VIEWER", nothing, validate_path.format(fresh.id), viewer, None),
        ("validate: ANALYST", nothing, validate_path.format(fresh.id), analyst, None),
        (
            "validate: VIEWER, connector off",
            enabled(off),
            validate_path.format(fresh.id),
            viewer,
            None,
        ),
        (
            "validate: connector off",
            enabled(off),
            validate_path.format(fresh.id),
            developer,
            None,
        ),
        (
            "validate: keys unset",
            keys_are(None),
            validate_path.format(bq_ready.id),
            developer,
            None,
        ),
        (
            "validate: table not found",
            warehouse(WarehouseErrorCode.OBJECT_NOT_FOUND),
            validate_path.format(fresh.id),
            developer,
            None,
        ),
        (
            "validate: warehouse refuses",
            warehouse(WarehouseErrorCode.PERMISSION_DENIED),
            validate_path.format(fresh.id),
            developer,
            None,
        ),
        (
            "validate: column missing",
            lambda: setattr(fakes, "columns", COLUMNS[1:]),
            validate_path.format(fresh.id),
            developer,
            None,
        ),
        (
            "validate: time column is text",
            lambda: setattr(
                fakes, "columns", COLUMNS[:3] + (("exposed_at", "VARCHAR"),)
            ),
            validate_path.format(fresh.id),
            developer,
            None,
        ),
        # POST /sources/{id}/preview
        ("preview: VIEWER, unknown", nothing, preview_path.format(unknown), viewer, {}),
        ("preview: VIEWER", nothing, preview_path.format(ready.id), viewer, {}),
        (
            "preview: connector off, not validated",
            enabled(off),
            preview_path.format(fresh.id),
            developer,
            {},
        ),
        (
            "preview: not validated, bad window",
            nothing,
            preview_path.format(fresh.id),
            developer,
            late_window,
        ),
        (
            "preview: bad window",
            nothing,
            preview_path.format(ready.id),
            developer,
            late_window,
        ),
        (
            "preview: keys unset",
            keys_are(None),
            preview_path.format(bq_ready.id),
            developer,
            {},
        ),
        (
            "preview: executor busy",
            lambda: setattr(
                fakes, "submit_error", WarehouseError(WarehouseErrorCode.WAREHOUSE_BUSY)
            ),
            preview_path.format(ready.id),
            developer,
            {},
        ),
        (
            "preview: executor has the connection",
            lambda: setattr(
                fakes,
                "submit_error",
                WarehouseError(WarehouseErrorCode.RUN_IN_PROGRESS),
            ),
            preview_path.format(ready.id),
            developer,
            {},
        ),
        (
            "preview: daily limit",
            nothing,
            preview_path.format(full_ready.id),
            developer,
            {},
        ),
        (
            "preview: a run in progress",
            nothing,
            preview_path.format(busy_ready.id),
            developer,
            {},
        ),
        (
            "preview: runs at the cap",
            lambda: world.monkeypatch.setattr(
                settings, "WAREHOUSE_MAX_CONCURRENT_RUNS", 1
            ),
            preview_path.format(ready.id),
            developer,
            {},
        ),
    ]
    found: Dict[str, Any] = {}
    async with _client() as http:
        for label, prepare, path, user, body in cases:
            fakes.reset()
            world.monkeypatch.setattr(connectors, "ENABLED_CONNECTORS", ALL_CONNECTORS)
            world.monkeypatch.setattr(settings, "WAREHOUSE_CREDENTIALS_KEYS", keys)
            world.monkeypatch.setattr(settings, "WAREHOUSE_MAX_CONCURRENT_RUNS", 32)
            world.monkeypatch.setattr(
                settings, "WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS", 1800
            )
            prepare()
            response = await http.post(path, json=body, headers=_bearer(user))
            found[label] = (
                response.status_code,
                response.json(),
                response.headers.get("retry-after"),
            )
    return found


@pytest.mark.regression
async def test_the_refusals_of_the_four_routes_are_unchanged(
    small_pool: PoolWatch, world: World
):
    fakes = install_fakes(world)
    found = await _answers(world, fakes)
    wrong = {
        label: {"got": found.get(label), "want": want}
        for label, want in EXPECTED.items()
        if found.get(label) != want
    }
    assert set(found) == set(EXPECTED), sorted(set(found) ^ set(EXPECTED))
    assert not wrong, wrong


def _events(world: World, resource_id, action: str) -> List[ComplianceAuditEvent]:
    world.db.expire_all()
    return [
        event
        for event in world.db.query(ComplianceAuditEvent).filter(
            ComplianceAuditEvent.resource_id == str(resource_id)
        )
        if getattr(event.action, "value", event.action) == action
    ]


@pytest.mark.regression
async def test_what_the_four_routes_store_is_unchanged(
    small_pool: PoolWatch, world: World
):
    """A passing validation, a test that promotes a pending key and a preview:
    the rows each writes and its audit entry."""
    keys = Fernet.generate_key().decode()
    fakes = install_fakes(world)
    world.monkeypatch.setattr(
        modules_settings.settings, "WAREHOUSE_CREDENTIALS_KEYS", keys
    )
    developer = world.user(UserRole.DEVELOPER)
    admin = world.user(UserRole.ADMIN)
    lake = make_connection(world, owner=admin)
    fresh = make_source(world, lake)
    snow = make_connection(
        world,
        "snowflake",
        owner=admin,
        credential=b"current",
        pending=b"pending",
        keys=keys,
        parameters={"key_slot": "1", "pending_key_slot": "2"},
        public_key_fingerprint="SHA256:current",
    )
    pending_ciphertext = snow.pending_credentials_ciphertext
    ready = make_source(world, lake, name="Ready", validated=True)

    async with _client() as http:
        validated = await http.post(
            f"{WA}/sources/{fresh.id}/validate", headers=_bearer(developer)
        )
        tested = await http.post(
            f"{WA}/connections/{snow.id}/test", headers=_bearer(admin)
        )
        previewed = await http.post(
            f"{WA}/sources/{ready.id}/preview", json={}, headers=_bearer(developer)
        )

    assert validated.status_code == 200, validated.text
    body = validated.json()
    assert body["columns"] == [{"name": n, "type": t} for n, t in COLUMNS]
    assert body["source"]["id"] == str(fresh.id)
    assert body["source"]["validated_at"] is not None
    world.db.expire_all()
    stored = world.db.get(WarehouseSource, fresh.id)
    assert stored.validated_at is not None
    assert stored.column_mapping == {
        role: {"name": name, "type": dict(COLUMNS)[name]}
        for role, name in MAPPING.items()
    }
    (event,) = _events(world, fresh.id, "UPDATE")
    assert event.actor_id == developer.id
    assert event.old_value["validated_at"] is None
    assert event.new_value["validated_at"] is not None

    assert tested.status_code == 200, tested.text
    assert tested.json() == {
        "ok": True,
        "warehouse_type": "snowflake",
        "promoted_pending_key": True,
    }
    world.db.expire_all()
    promoted = world.db.get(WarehouseConnection, snow.id)
    assert promoted.credentials_ciphertext == pending_ciphertext
    assert promoted.pending_credentials_ciphertext is None
    assert promoted.public_key_fingerprint == "SHA256:pending"
    assert promoted.pending_public_key_fingerprint is None
    assert promoted.parameters["key_slot"] == "2"
    assert "pending_key_slot" not in promoted.parameters
    (event,) = _events(world, snow.id, "KEY_CREATE")
    assert event.actor_id == admin.id
    assert event.old_value["public_key_fingerprint"] == "SHA256:current"
    assert event.new_value["public_key_fingerprint"] == "SHA256:pending"

    assert previewed.status_code == 200, previewed.text
    preview = previewed.json()
    assert {k: preview[k] for k in PREVIEW_SUMMARY} == PREVIEW_SUMMARY
    world.db.expire_all()
    (run,) = world.db.query(WarehouseAnalysisRun).filter(
        WarehouseAnalysisRun.connection_id == lake.id
    )
    assert str(run.id) == preview["run_id"]
    assert (run.kind, run.status, run.requested_by) == (
        "preview",
        "queued",
        developer.id,
    )
    assert run.request["source_id"] == str(ready.id)
