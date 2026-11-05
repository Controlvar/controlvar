"""The modules' signed-in routes answer every request of a burst larger than
they can serve at once, and answer again after it.

The same check as ``backend/tests/integration/api/test_signed_in_routes_under_overload.py``
(its module docstring describes the harness), for the routes that exist only
in the full profile: the invite preview, and one route of each module shape.
The harness is imported from that core file; these rows live here because a
core file never imports a module (``backend/tests/smoke/test_core_boundary.py``).
"""

from __future__ import annotations

import uuid

import pytest

from backend.tests.integration.api.test_signed_in_routes_under_overload import (
    PoolWatch,
    Row,
    Send,
    World,
    _bearer,
    _get,
    check_a_burst,
    small_pool,
    world,
)
from modules.backend.app.api.v1.endpoints import etl, realtime_counters
from modules.backend.app.models.workspace import (
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
)
from modules.backend.app.schemas.etl import GlueCrawlerStatus
from modules.backend.app.schemas.realtime_counters import ExperimentCounters
from modules.backend.app.services.workspace_service import workspace_service

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
    """Every step of the route runs; the experiment does not exist, so its
    normal answer is 404."""
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


#: One route for each module shape of signed-in route (named as in the core
#: file's parameter ids), and the invite preview.
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
]


@pytest.mark.regression
@pytest.mark.parametrize("label, row", ROWS)
async def test_every_request_of_a_burst_is_answered(
    label: str, row: Row, small_pool: PoolWatch, world: World
):
    await check_a_burst(label, row, small_pool, world)
