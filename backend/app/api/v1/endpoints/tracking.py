"""
Tracking API endpoints.

This module provides API endpoints for experiment user assignment and event tracking.
These endpoints are designed to be called from client applications to participate
in experiments and record user interactions.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.sdk_scope import require_sdk_ruleset_key
from backend.app.core.context_limits import check_context
from backend.app.core.logger import failure_detail, unexpected_failure
from backend.app.core.metrics import record_event_tracked, record_experiment_assignment
from backend.app.models.api_key import APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag
from backend.app.schemas.storable_text import storable_text_param
from backend.app.schemas.tracking import (
    AssignmentBatchRequest,
    AssignmentBatchResponse,
    AssignmentRequest,
    BatchAssignment,
    BatchAssignmentCounts,
    BatchVariant,
    EventBatchRequest,
    EventBatchResponse,
    EventCreate,
    EventRequest,
    EventResponse,
    UntaggedEventCreate,
    VariantAssignmentResponse,
)
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.bandit_routing import bandit_chooser
from backend.app.services.event_service import EventService

# Create router
router = APIRouter()

logger = logging.getLogger(__name__)


def _invalid_event_detail(exc: ValidationError) -> str:
    """The 422 for an event the stored-event model refused: the field names
    only. pydantic's own text repeats the values that were sent."""
    fields = sorted(
        {
            str(error["loc"][0]) if error.get("loc") else "event"
            for error in exc.errors()
        }
    )
    return f"Invalid event: {', '.join(fields)} not valid"


def _unknown_keys_detail(
    experiment_key: Optional[str], feature_flag_key: Optional[str]
) -> str:
    """The 404 for a request that gave a key and none of its keys was found.

    Names only the keys that were given, so it never prints ``None`` (#217).
    """
    if experiment_key and feature_flag_key:
        return (
            f"Neither experiment key '{experiment_key}' nor feature flag key "
            f"'{feature_flag_key}' was found."
        )
    if experiment_key:
        return (
            f"No experiment has the key '{experiment_key}'. Leave experiment_key "
            "out to record the event without an experiment."
        )
    return (
        f"No feature flag has the key '{feature_flag_key}'. Leave feature_flag_key "
        "out to record the event without a flag."
    )


#: The 404 sentence for each id field ``/tracking/events`` was given and could
#: not find (#400). It names the field, never the id that was sent.
_UNKNOWN_ID_SENTENCES = {
    "experiment_id": "No experiment has that experiment_id.",
    "feature_flag_id": "No feature flag has that feature_flag_id.",
    "variant_id": "No variant has that variant_id.",
}


def _unknown_ids_detail(fields: List[str]) -> str:
    """The 404 for an event by ids naming an id that is no stored row."""
    return " ".join(_UNKNOWN_ID_SENTENCES[field] for field in fields)


def _event_model(
    experiment_id: Any, feature_flag_id: Any
) -> type[EventCreate] | type[UntaggedEventCreate]:
    """The model ``/track`` and ``/batch`` build for one event.

    ``EventCreate`` refuses an event with neither id, so an event whose
    request named no key is built as ``UntaggedEventCreate`` (#217).
    """
    if experiment_id or feature_flag_id:
        return EventCreate
    return UntaggedEventCreate


def _variant_at_receipt(db: Session, experiment: Experiment, user_id: str) -> Any:
    """The variant ``/track`` and ``/batch`` tag an event with.

    The user's stored assignment, while the experiment is ACTIVE; otherwise
    ``None``. The status is the one read when the event is received. For a
    draft, paused, completed or archived experiment the event is stored with
    its ``experiment_id`` and no variant, so it counts in no variant's
    results: an event received while the experiment is not running is not
    taken as evidence about the variant the user was assigned. The assignment
    rows are kept, and ``backend/scripts/retag_events_from_assignments.py``
    can tag such events from them afterwards.
    """
    assignment = (
        db.query(Assignment)
        .filter(
            Assignment.experiment_id == experiment.id,
            Assignment.user_id == user_id,
        )
        .order_by(Assignment.created_at.desc())
        .first()
    )
    return assignment.variant_id if assignment else None


def _event_response(event: Event) -> EventResponse:
    """Build the public response model from a stored Event row."""
    return EventResponse(
        id=str(event.id),
        event_type=event.event_type,
        event_name=event.event_name or event.event_type,
        user_id=event.user_id,
        experiment_id=str(event.experiment_id) if event.experiment_id else None,
        feature_flag_id=str(event.feature_flag_id) if event.feature_flag_id else None,
        variant_id=str(event.variant_id) if event.variant_id else None,
        value=event.value,
        properties=event.event_metadata,
        timestamp=event.created_at,
        created_at=event.created_at,
        updated_at=event.updated_at or datetime.now(timezone.utc),
    )


@router.get("/")
def get_tracking():
    """
    Tracking API information endpoint.

    Returns general information about the tracking API endpoints.
    """
    return {"message": "Tracking API Endpoints"}


@router.post(
    "/assign",
    response_model=VariantAssignmentResponse,
    summary="Assign user to experiment variant",
    response_description="Returns the variant assignment for the user",
)
async def assign_user_to_experiment(
    request: AssignmentRequest = Body(..., description="Assignment request data"),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
) -> VariantAssignmentResponse:
    """
    Assign a user to an experiment variant.

    This endpoint assigns a user to a variant of the specified experiment.
    It handles consistent assignment for returning users.

    The assignment is deterministic based on:
    - User ID
    - Experiment ID
    - Targeting rules
    - Traffic allocation percentages

    This means that the same user will always get the same variant assignment
    for a specific experiment, including across experiments in the same mutual
    exclusion group: a user enrolled in one experiment of a group is not
    enrolled in another, even as experiments in the group are activated,
    paused or resumed.

    New users are first checked for eligibility: the global holdout, the
    experiment's mutual exclusion group and its targeting rules (evaluated
    against ``context``; top-level keys also answer ``user.<key>`` /
    ``device.<key>`` / ``app.<key>``).  Ineligible users receive HTTP 200 with
    the control variant, ``assigned: false`` and ``reason`` set to
    ``holdout`` | ``mutual_exclusion`` | ``targeting``; nothing is recorded
    for them.  Existing assignments are always returned as-is.

    **Authentication**: Requires a valid API key in the X-API-Key header.

    Returns:
        VariantAssignmentResponse: The variant assignment with configuration
        details plus ``assigned`` / ``reason``

    Raises:
        HTTPException 401: If the API key is invalid
        HTTPException 404: If the experiment doesn't exist or is not active
        HTTPException 500: If there's an error during assignment
    """
    # The limits of backend.app.core.context_limits, before anything is read
    # or written.
    check_context(request.context)

    # Get experiment by key
    experiment = (
        db.query(Experiment)
        .filter(
            Experiment.key == request.experiment_key,
            Experiment.status == ExperimentStatus.ACTIVE,
        )
        .first()
    )

    if not experiment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active experiment with key '{request.experiment_key}' not found",
        )

    # Create assignment service
    assignment_service = AssignmentService(db)

    # Multi-armed bandit experiments: route *new* users according to the
    # latest BanditState weights.  Existing (sticky) assignments always win
    # because assign_user returns the stored row before using the override.
    override_variant_id: Optional[uuid.UUID] = bandit_chooser(db, experiment)(
        request.user_id
    )

    try:
        # Attempt to assign the user
        assignment_data = assignment_service.assign_user(
            user_id=request.user_id,
            experiment_id=str(experiment.id),
            context=request.context,
            override_variant_id=override_variant_id,
        )

        # Get the variant
        variant_id = assignment_data.get("variant_id")
        variant = next(
            (v for v in experiment.variants if str(v.id) == variant_id),
            None,
        )

        if not variant:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Assigned variant not found in experiment",
            )

        # Prometheus: count every assignment decision handed out (label by
        # experiment/variant id; ineligible users are counted under their
        # reason so holdout/exclusion volume is visible too).
        assigned_flag = bool(assignment_data.get("assigned", True))
        record_experiment_assignment(
            experiment_id=str(experiment.id),
            variant_id=str(variant.id)
            if assigned_flag
            else str(assignment_data.get("reason") or "ineligible"),
        )

        # Create response.  Ineligible users (holdout / mutual exclusion /
        # targeting) get the control variant with assigned=False.
        return VariantAssignmentResponse(
            experiment_key=request.experiment_key,
            user_id=request.user_id,
            variant_id=str(variant.id),
            variant_name=variant.name,
            is_control=bool(variant.is_control),
            configuration=variant.configuration,
            assigned=bool(assignment_data.get("assigned", True)),
            reason=str(assignment_data.get("reason") or "assigned"),
        )
    except ValidationError as e:
        # A response that does not build is not the caller's mistake, and
        # pydantic's text repeats the values: answer as an unexpected failure.
        raise unexpected_failure(
            e,
            "Tracking assign",
            "Could not assign the user to the experiment",
            db=db,
            logger=logger,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("Tracking assign failed (%s)", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=failure_detail("Could not assign the user to the experiment"),
        )


#: The 409 for an experiment that stopped accepting users part-way through a
#: batch (paused, completed or deleted, or its variants changed). The users
#: before that point are committed, one by one, as N single calls would be.
EXPERIMENT_CHANGED_DETAIL = (
    "The experiment changed during the request. Users earlier in the list may "
    "already be assigned; resending the same request is safe."
)

#: The sentence of the 500 for any other failure part-way through a batch.
BATCH_FAILURE_SENTENCE = "Could not assign the users to the experiment"


@router.post(
    "/assign/batch",
    response_model=AssignmentBatchResponse,
    summary="Assign a list of users to an experiment (beta)",
    response_description="Each user's variant, in request order",
    openapi_extra={"x-stability": "beta"},
    responses={
        401: {"description": "No API key, or an unknown, expired or revoked one"},
        403: {
            "description": "The key lacks the sdk:ruleset scope, or its owner "
            "can no longer change feature flags or experiments"
        },
        404: {"description": "No ACTIVE experiment has that key; nobody is assigned"},
        409: {
            "description": "The experiment changed during the request; users "
            "earlier in the list may be assigned, and resending is safe"
        },
        429: {"description": "More than 60 requests a minute from this address"},
    },
)
def assign_users_to_experiment_batch(
    request: AssignmentBatchRequest = Body(
        ..., description="The experiment and its users"
    ),
    db: Session = Depends(deps.get_db),
    api_key: APIKey = Depends(require_sdk_ruleset_key),
) -> AssignmentBatchResponse:
    """
    Assign up to 1,000 users to an ACTIVE experiment in one request.

    Each user gets what ``POST /api/v1/tracking/assign`` would give them, with
    the bandit weights read at the start of the request: the same eligibility
    checks for a new user (global holdout, mutual exclusion group, targeting
    rules against that user's ``context``) and the same sticky answer for a
    user already assigned. Users are processed in request order and each new
    assignment is committed on its own, as N single calls would be.

    Only assignments are recorded, never a view event: the user's first
    ``POST /api/v1/tracking/assign`` records that they saw the experiment.

    **Authentication**: an API key with the ``sdk:ruleset`` scope, whose owner
    can change feature flags and experiments (ADMIN, DEVELOPER or a superuser).

    A plain ``def``: FastAPI runs it in the thread pool, so a batch does not
    hold the event loop while it works through its users.
    """
    # Every user's context is held to the limits of
    # backend.app.core.context_limits before the first user is assigned: one
    # over a limit refuses the whole request, and nobody is assigned.
    for index in range(len(request.users)):
        check_context(request.users[index].context, field=f"users[{index}]")

    experiment = (
        db.query(Experiment)
        .filter(
            Experiment.key == request.experiment_key,
            Experiment.status == ExperimentStatus.ACTIVE,
        )
        .first()
    )
    if not experiment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active experiment with key '{request.experiment_key}' not found",
        )

    # Plain values only from here on: every commit below expires the ORM
    # objects of a production session, and touching one again reloads it.
    experiment_id = str(experiment.id)
    variants = {
        str(v.id): BatchVariant(
            name=v.name,
            is_control=bool(v.is_control),
            configuration=v.configuration,
        )
        for v in experiment.variants
    }
    choose = bandit_chooser(db, experiment)
    service = AssignmentService(db)
    total = len(request.users)

    assignments: List[BatchAssignment] = []
    counts = BatchAssignmentCounts()
    for index, user in enumerate(request.users):
        try:
            data = service.assign_user(
                user_id=user.user_id,
                experiment_id=experiment_id,
                track_exposure=False,
                override_variant_id=choose(user.user_id),
                context=user.context,
            )
        except ValidationError as e:
            # pydantic's ValidationError is a ValueError, and not the
            # experiment changing: an unexpected failure.
            logger.error(
                "Batch assign failed at user %d of %d in experiment %s (%s)",
                index + 1,
                total,
                experiment_id,
                type(e).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=failure_detail(BATCH_FAILURE_SENTENCE),
            )
        except ValueError as e:
            # assign_user raises ValueError when the experiment is gone, is no
            # longer ACTIVE, or its variants no longer fit.
            logger.warning(
                "Batch assign stopped at user %d of %d: experiment %s changed (%s)",
                index + 1,
                total,
                experiment_id,
                type(e).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=EXPERIMENT_CHANGED_DETAIL,
            )
        except Exception as e:
            # The class name only: a database error's text repeats the row's
            # parameters, the user id among them.
            logger.error(
                "Batch assign failed at user %d of %d in experiment %s (%s)",
                index + 1,
                total,
                experiment_id,
                type(e).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=failure_detail(BATCH_FAILURE_SENTENCE),
            )

        variant_id = str(data.get("variant_id"))
        if variant_id not in variants:
            logger.error(
                "Batch assign failed at user %d of %d in experiment %s "
                "(assigned variant not in the experiment)",
                index + 1,
                total,
                experiment_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=failure_detail(BATCH_FAILURE_SENTENCE),
            )
        assigned = bool(data.get("assigned", True))
        reason = str(data.get("reason") or "assigned")
        record_experiment_assignment(
            experiment_id=experiment_id,
            variant_id=variant_id if assigned else reason,
        )
        assignments.append(
            BatchAssignment(
                user_id=user.user_id,
                variant_id=variant_id,
                assigned=assigned,
                reason=reason,
            )
        )
        if reason in BatchAssignmentCounts.model_fields:
            setattr(counts, reason, getattr(counts, reason) + 1)

    return AssignmentBatchResponse(
        experiment_key=request.experiment_key,
        variants=variants,
        assignments=assignments,
        counts=counts,
    )


@router.post(
    "/track",
    response_model=EventResponse,
    summary="Track event",
    response_description="Returns confirmation of event tracking",
)
async def track_event(
    request: EventRequest = Body(..., description="Event data to track"),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
) -> EventResponse:
    """
    Track an event for an experiment.

    This endpoint records a user interaction event associated with an experiment.
    Events are used to measure experiment metrics and outcomes.

    Event tracking can be associated with:
    - A specific experiment (using experiment_key)
    - A feature flag (using feature_flag_key)
    - Or both

    While the experiment is running (ACTIVE), the event is tagged with the
    variant the user is assigned to. An event for a draft, paused, completed
    or archived experiment is stored with the experiment and no variant, so
    it counts in no variant's results.

    Additional data can be included:
    - Numeric value (for revenue, durations, counts)
    - Metadata (additional contextual information)
    - Custom timestamp (defaults to server time if not provided)

    **Authentication**: Requires a valid API key in the X-API-Key header.

    Returns:
        EventResponse: Confirmation of successful event tracking with event ID

    Raises:
        HTTPException 401: If the API key is invalid
        HTTPException 422: If the event data is invalid
    """
    # Find experiment ID if specified
    experiment_id = None
    variant_id = None
    if request.experiment_key:
        experiment = (
            db.query(Experiment)
            .filter(Experiment.key == request.experiment_key)
            .first()
        )

        if experiment:
            experiment_id = experiment.id
            variant_id = _variant_at_receipt(db, experiment, request.user_id)

    # Find feature flag ID if specified
    feature_flag_id = None
    if request.feature_flag_key:
        feature_flag = (
            db.query(FeatureFlag)
            .filter(FeatureFlag.key == request.feature_flag_key)
            .first()
        )

        if feature_flag:
            feature_flag_id = feature_flag.id

    # A key that was given and not found answers 404. Only an absent key
    # (missing, null or "") records the event with no experiment or flag, as
    # history (#217); " " counts as given.
    keys_given = bool(request.experiment_key) or bool(request.feature_flag_key)
    if keys_given and not experiment_id and not feature_flag_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_unknown_keys_detail(
                request.experiment_key, request.feature_flag_key
            ),
        )

    try:
        # Create event data
        event_data = _event_model(experiment_id, feature_flag_id)(
            user_id=request.user_id,
            event_type=request.event_type,
            event_name=request.event_name or request.event_type,
            experiment_id=str(experiment_id) if experiment_id else None,
            feature_flag_id=str(feature_flag_id) if feature_flag_id else None,
            variant_id=str(variant_id) if variant_id else None,
            value=request.value,
            properties=request.metadata,
            timestamp=request.timestamp or datetime.now(timezone.utc),
        )

        # Track the event
        event = EventService(db).track_event(event_data, allow_untagged=True)
        record_event_tracked(str(event_data.event_type))
        return _event_response(event)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_invalid_event_detail(e),
        )
    except ValueError as e:
        # EventService's own sentences.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid event: {e!s}",
        )
    except Exception as e:
        logger.exception("Tracking event failed (%s)", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=failure_detail("Could not store the event"),
        )


@router.post(
    "/events",
    response_model=EventResponse,
    summary="Track event by ids",
    response_description="Returns the stored event",
    responses={
        404: {
            "description": "An experiment_id, feature_flag_id or variant_id names "
            "no stored row; the detail names each such field, and nothing is stored"
        },
    },
)
async def track_event_by_ids(
    event_data: EventCreate = Body(
        ..., description="Event data keyed by experiment/flag ids"
    ),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
) -> EventResponse:
    """
    Track an event that names its experiment, flag and variant by id.

    Unlike ``/track`` (which resolves ``experiment_key``/``feature_flag_key``),
    this endpoint accepts ``experiment_id``/``feature_flag_id``/``variant_id``
    directly.  It is used by server-side integrations and the data seeding
    tooling that already hold the ids.

    An id that is not a UUID answers 422. A UUID that names no stored
    experiment, feature flag or variant answers 404 naming the field, and
    nothing is stored.

    **Authentication**: Requires a valid API key in the X-API-Key header.
    """
    service = EventService(db)
    try:
        # Raises ValueError for an id that is not a UUID, as track_event does.
        unknown = service.unknown_id_fields(event_data)
        if not unknown:
            event = service.track_event(event_data)
            record_event_tracked(str(event_data.event_type))
            return _event_response(event)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_invalid_event_detail(e),
        )
    except ValueError as e:
        # EventService's own sentences.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid event: {e!s}",
        )
    except Exception as e:
        logger.exception("Tracking event failed (%s)", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=failure_detail("Could not store the event"),
        )
    # An id named nothing: nothing was stored. The detail names the fields.
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=_unknown_ids_detail(unknown),
    )


@router.post(
    "/batch",
    response_model=EventBatchResponse,
    summary="Track multiple events in batch",
    response_description="Returns batch processing results",
)
async def track_events_batch(
    request: EventBatchRequest = Body(
        ...,
        description="Batch of events to track",
        examples={
            "default": {
                "summary": "Batch tracking example",
                "description": "A sample batch of events to track for an experiment",
                "value": {
                    "events": [
                        {
                            "event_type": "page_view",
                            "user_id": "user-123",
                            "experiment_key": "homepage-redesign",
                            "metadata": {"page": "/products", "referrer": "google"},
                        },
                        {
                            "event_type": "click",
                            "user_id": "user-123",
                            "experiment_key": "homepage-redesign",
                            "metadata": {"element": "buy-button"},
                        },
                    ]
                },
            }
        },
    ),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
) -> EventBatchResponse:
    """
    Track multiple events in a single batch operation.

    This endpoint allows tracking multiple events in a single request,
    improving performance for high-volume event tracking.

    The batch can contain up to 100 events. If any events fail validation,
    the response will include details about which events failed and why.

    Each event is tagged as ``/track`` tags it: with the user's variant while
    the experiment is running (ACTIVE), and with no variant for a draft,
    paused, completed or archived experiment.

    **Authentication**: Requires a valid API key in the X-API-Key header.

    Returns:
        EventBatchResponse: Results of the batch processing operation

    Raises:
        HTTPException 401: If the API key is invalid
        HTTPException 413: If the batch size exceeds the limit
        HTTPException 422: If the batch request format is invalid
    """
    # Check batch size
    if len(request.events) > 100:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Batch size exceeds the limit of 100 events",
        )

    # Create event service
    event_service = EventService(db)

    # Initialize counters
    success_count = 0
    failure_count = 0
    errors = []

    # Process each event
    for index, event_request in enumerate(request.events):
        try:
            # Find experiment ID if specified
            experiment_id = None
            variant_id = None
            if event_request.experiment_key:
                experiment = (
                    db.query(Experiment)
                    .filter(Experiment.key == event_request.experiment_key)
                    .first()
                )

                if experiment:
                    experiment_id = experiment.id
                    variant_id = _variant_at_receipt(
                        db, experiment, event_request.user_id
                    )

            # Find feature flag ID if specified
            feature_flag_id = None
            if event_request.feature_flag_key:
                feature_flag = (
                    db.query(FeatureFlag)
                    .filter(FeatureFlag.key == event_request.feature_flag_key)
                    .first()
                )

                if feature_flag:
                    feature_flag_id = feature_flag.id

            # Skip if a key was given and none was found; an event that names
            # no key is stored as history (#217).
            keys_given = bool(event_request.experiment_key) or bool(
                event_request.feature_flag_key
            )
            if keys_given and not experiment_id and not feature_flag_id:
                failure_count += 1
                errors.append(
                    {
                        "index": index,
                        "event_type": event_request.event_type,
                        "user_id": event_request.user_id,
                        "error": _unknown_keys_detail(
                            event_request.experiment_key,
                            event_request.feature_flag_key,
                        ),
                    }
                )
                continue

            # Create event data
            event_data = _event_model(experiment_id, feature_flag_id)(
                user_id=event_request.user_id,
                event_type=event_request.event_type,
                event_name=event_request.event_name or event_request.event_type,
                experiment_id=str(experiment_id) if experiment_id else None,
                feature_flag_id=str(feature_flag_id) if feature_flag_id else None,
                variant_id=str(variant_id) if variant_id else None,
                value=event_request.value,
                properties=event_request.metadata,
                timestamp=event_request.timestamp or datetime.now(timezone.utc),
            )

            # Track the event
            event_service.track_event(event_data, allow_untagged=True)
            record_event_tracked(str(event_data.event_type))
            success_count += 1

        except ValidationError as e:
            failure_count += 1
            errors.append(
                {
                    "index": index,
                    "event_type": event_request.event_type,
                    "user_id": event_request.user_id,
                    "error": _invalid_event_detail(e),
                }
            )
        except ValueError as e:
            failure_count += 1
            errors.append(
                {
                    "index": index,
                    "event_type": event_request.event_type,
                    "user_id": event_request.user_id,
                    "error": str(e),
                }
            )
        except Exception as e:
            logger.exception("Tracking batch item failed (%s)", type(e).__name__)
            db.rollback()
            failure_count += 1
            errors.append(
                {
                    "index": index,
                    "event_type": event_request.event_type,
                    "user_id": event_request.user_id,
                    "error": failure_detail("Could not store this event"),
                }
            )

    # Return batch response
    return EventBatchResponse(
        success_count=success_count,
        failure_count=failure_count,
        errors=errors if errors else None,
    )


@router.get(
    "/assignments/{user_id}",
    response_model=List[Dict[str, Any]],
    summary="Get user's experiment assignments",
    response_description="Returns all active experiment assignments for a user",
)
async def get_user_assignments(
    user_id: Annotated[
        str, Path(description="ID of the user"), storable_text_param("user_id")
    ],
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
    active_only: bool = Query(
        True, description="Only return active experiment assignments"
    ),
) -> List[Dict[str, Any]]:
    """
    Get all experiment assignments for a user.

    This endpoint retrieves all experiment variants assigned to a specific user.
    By default, it only returns assignments for active experiments.

    **Authentication**: Requires a valid API key in the X-API-Key header.

    Returns:
        List[Dict[str, Any]]: List of assignment details for the user

    Raises:
        HTTPException 401: If the API key is invalid
        HTTPException 404: If user has no assignments
    """
    # Create assignment service
    assignment_service = AssignmentService(db)

    # Get user assignments
    assignments = assignment_service.get_user_assignments(
        user_id=user_id, active_only=active_only
    )

    if not assignments:
        return []

    return assignments
