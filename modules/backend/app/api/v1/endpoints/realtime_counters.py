"""
Real-time counter REST endpoints (P2-B).

Provides live experiment assignment / event / conversion counts backed by
DynamoDB atomic counters so dashboards can show up-to-date data without
waiting for batch analytics jobs.

Endpoints:
    GET  /api/v1/counters/{experiment_id}          — read all counters
    POST /api/v1/counters/{experiment_id}/increment — atomic single increment
    POST /api/v1/counters/bulk                      — atomic bulk increment
    POST /api/v1/counters/{experiment_id}/reset     — reset (ADMIN only)

Reading a counter needs permission to view the experiment, as
``GET /api/v1/experiments/{id}`` does; *writing* one needs UPDATE
on experiments (ADMIN, DEVELOPER, a superuser, or anyone whose custom role or
direct grant gives it), because these counters are
the first source ``BanditScheduler`` consults for a bandit experiment's arm
statistics — a forged conversion here re-weights live traffic.  Resetting is
ADMIN only, as it discards data.

The single-write routes take ``{experiment_id}`` **and honour it**: the path
value is what is written, and a body naming a different experiment is a 400
rather than a write somewhere else (#95).  They did not used to, and the path
parameter reached nothing but an error message.  ``/bulk`` has no path
parameter, because each of its up-to-100 entries names its own experiment —
see the note on that route.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.models.user import User, UserRole
from modules.backend.app.schemas.realtime_counters import (
    BulkIncrementRequest,
    BulkIncrementResponse,
    CounterResetRequest,
    ExperimentCounters,
    IncrementRequest,
    IncrementResponse,
)
from modules.backend.app.services.dynamodb_counter_service import DynamoDBCounterService

logger = logging.getLogger(__name__)

router = APIRouter(route_class=OffLoopRoute)

# ---------------------------------------------------------------------------
# Dependency — provides a shared DynamoDBCounterService instance
# ---------------------------------------------------------------------------

_counter_service: Optional[DynamoDBCounterService] = None


async def get_counter_service() -> DynamoDBCounterService:
    """
    Dependency that returns a singleton DynamoDBCounterService.

    ``async`` so that FastAPI calls it on the event loop rather than in a
    worker thread; the service is built once per process, on first use.

    This can be overridden in tests via app.dependency_overrides.
    """
    global _counter_service
    if _counter_service is None:
        _counter_service = DynamoDBCounterService()
    return _counter_service


def _require_counter_write(current_user: User) -> None:
    """Raise 403 unless *current_user*'s role may change experiment data.

    This is a **role** check, not a per-experiment one, and the docstring
    used to claim otherwise ("the same one that gates editing the experiment
    these numbers describe"). It asks ``experiment: update``
    (``backend/app/core/permissions.py``): ADMIN, DEVELOPER, superusers and
    anyone whose custom role or direct grant gives ``experiment (update)``
    pass; anyone else, who may read every experiment but change none, does
    not.

    A role check is the right one here. A deployment is single tenant
    (founder, 2026-09-21), so a DEVELOPER may edit any experiment this
    installation holds -- which is what the core list and update endpoints
    settled on for the same reason (#83). Tenant isolation is the workspaces
    module's job: ``Experiment.workspace_id`` is a bare indexed UUID that
    nothing filters on yet, and when scoping arrives it belongs there, keyed
    on membership, for every experiment route at once rather than for this
    one.
    """
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to write experiment counters",
        )


#: The OpenAPI entry for the 400 below. Without it the generated spec -- and
#: every client generated from it -- knows only about 200 and FastAPI's
#: automatic 422, so the one failure a correct caller can hit is invisible.
MISMATCH_RESPONSE = {
    status.HTTP_400_BAD_REQUEST: {
        "description": (
            "`experiment_id` in the body names a different experiment than "
            "the one in the path."
        )
    }
}


def _require_body_matches_path(*, path_experiment_id: str, body_experiment_id: str):
    """Refuse a body that names a different experiment than the URL (#95).

    A raise, not a predicate -- hence the name. Both parameters are strings
    and would be silently interchangeable positionally, so they are
    keyword-only: swapping them would only flip which id the message calls
    "path" and which "body", and no assertion on a substring of that message
    could tell.

    The write itself uses the **path** value, so this is not what makes the
    write land in the right place -- it is what stops a caller being quietly
    redirected. A request whose body and URL disagree has no correct
    interpretation; answering 400 says so instead of picking one.

    400 rather than 422: FastAPI already returns 422 for this body's own
    schema violations (`amount=0`, an unknown `counter_type`, more than 100
    increments) with its own list-shaped `detail`. Reusing 422 for a
    constraint that spans the URL and the body would put two different
    `detail` shapes behind one status code.
    """
    if body_experiment_id != path_experiment_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"experiment_id in the body ({body_experiment_id!r}) does not "
                f"match the one in the path ({path_experiment_id!r})"
            ),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{experiment_id}",
    response_model=ExperimentCounters,
    summary="Get real-time counters for an experiment",
    responses={
        200: {"description": "Experiment counters returned"},
        404: {"description": "No counter data found for this experiment"},
    },
    dependencies=[Depends(deps.require_experiment_read)],
)
def get_experiment_counters(
    experiment_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    svc: DynamoDBCounterService = Depends(get_counter_service),
) -> ExperimentCounters:
    """
    Return real-time counters for all variants of the given experiment.

    Returns HTTP 404 when the experiment has no counter data (i.e. no
    assignments, events, or conversions have been recorded yet).
    """
    counters = svc.get_experiment_counters(experiment_id)

    # Return 404 when there is no data at all
    if (
        counters.total_assignments == 0
        and counters.total_events == 0
        and counters.total_conversions == 0
        and not counters.variants
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No counter data found for experiment '{experiment_id}'",
        )

    return counters


@router.post(
    "/{experiment_id}/increment",
    response_model=IncrementResponse,
    summary="Atomically increment a single counter",
    status_code=status.HTTP_200_OK,
    responses=MISMATCH_RESPONSE,
)
def increment_counter(
    experiment_id: str,
    request: IncrementRequest,
    current_user: User = Depends(deps.get_current_active_user),
    svc: DynamoDBCounterService = Depends(get_counter_service),
) -> IncrementResponse:
    """
    Atomically increment a counter (assignment, event, or conversion) for a
    specific experiment variant.

    Uses DynamoDB's ADD update expression — safe under concurrent writes.
    Requires ``experiment: update``: ADMIN, DEVELOPER, a superuser, or anyone
    whose custom role or direct grant gives ``experiment (update)``.
    """
    _require_counter_write(current_user)
    _require_body_matches_path(
        path_experiment_id=experiment_id,
        body_experiment_id=request.experiment_id,
    )
    try:
        # The path value, not the body's. They are equal by the line above --
        # which is the point: if that guard is ever removed the write stays
        # correct, rather than silently reverting to #82's behaviour with
        # every "called with correct args" test still green.
        new_value = svc.increment_counter(
            experiment_id=experiment_id,
            variant_id=request.variant_id,
            counter_type=request.counter_type,
            amount=request.amount,
        )
    except Exception as exc:
        logger.error(
            "Failed to increment counter for experiment=%s variant=%s type=%s: %s",
            experiment_id,
            request.variant_id,
            request.counter_type,
            exc,
        )
        # The log line above already carries `exc` with the experiment and
        # variant. Repeating it to the caller adds nothing but disclosure: a
        # boto3 ClientError's text names the table, the operation and the ARN.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Counter increment failed",
        )

    return IncrementResponse(
        experiment_id=experiment_id,
        variant_id=request.variant_id,
        counter_type=request.counter_type,
        new_value=new_value,
    )


@router.post(
    "/bulk",
    response_model=BulkIncrementResponse,
    summary="Bulk-increment multiple counters",
    status_code=status.HTTP_200_OK,
)
def bulk_increment(
    request: BulkIncrementRequest,
    current_user: User = Depends(deps.get_current_active_user),
    svc: DynamoDBCounterService = Depends(get_counter_service),
) -> BulkIncrementResponse:
    """
    Increment multiple counters in a single API call.

    Accepts up to 100 increment requests, each naming its own experiment.
    Partial failures are collected and reported in the response — the
    operation does not abort on first error.
    Requires ``experiment: update``: ADMIN, DEVELOPER, a superuser, or anyone
    whose custom role or direct grant gives ``experiment (update)``.

    **This route has no ``{experiment_id}``**, and used to. Fixing the single
    write routes to honour their path (#95) put this one in an impossible
    position: ``BulkIncrementRequest.increments`` is a list whose every entry
    carries its own ``experiment_id``, precisely so a collector can flush one
    buffer spanning several experiments. Requiring them all to equal a path
    segment would make that required field single-valued and turn a
    100-increment flush into a 400, while the promise two lines up —
    "does not abort on first error" — became false.

    A batch has no one experiment to name, so it no longer claims to. The
    per-entry ``experiment_id`` is the only thing that decides where each
    increment lands, and nothing in the URL can now disagree with it.
    """
    _require_counter_write(current_user)
    return svc.bulk_increment(request.increments)


@router.post(
    "/{experiment_id}/reset",
    summary="Reset counters (ADMIN only)",
    status_code=status.HTTP_200_OK,
    responses=MISMATCH_RESPONSE,
)
def reset_counters(
    experiment_id: str,
    request: CounterResetRequest,
    current_user: User = Depends(deps.get_current_active_user),
    svc: DynamoDBCounterService = Depends(get_counter_service),
) -> dict:
    """
    Reset one or more counters to zero.

    - If ``variant_id`` is omitted, resets all variants.
    - If ``counter_type`` is omitted, resets all counter types.
    - Requires ADMIN role.
    """
    # ADMIN-only: check superuser flag OR explicit ADMIN role
    is_admin = (
        getattr(current_user, "is_superuser", False)
        or getattr(current_user, "role", None) == UserRole.ADMIN
    )
    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only ADMIN users can reset counters",
        )
    _require_body_matches_path(
        path_experiment_id=experiment_id,
        body_experiment_id=request.experiment_id,
    )

    svc.reset_counters(
        experiment_id=experiment_id,
        variant_id=request.variant_id,
        counter_type=request.counter_type,
        reason=request.reason,
    )

    return {
        "status": "ok",
        "experiment_id": experiment_id,
        "message": "Counters reset successfully",
    }
