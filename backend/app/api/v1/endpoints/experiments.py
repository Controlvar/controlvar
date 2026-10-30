"""
Experiment API endpoints.

This module provides RESTful API endpoints for creating, reading, updating, and deleting
experiments in the experimentation platform. It implements the core functionality for
AB testing and feature experimentation.
"""

import copy
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Response,
    status,
)
from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from backend.app.api import deps, experiment_cache
from backend.app.api.segment_rules import (
    refuse_clone_with_unusable_segments,
    refuse_unusable_segments,
)
from backend.app.api.v1.endpoints import results as results_endpoints
from backend.app.core.logger import failure_detail, unexpected_failure
from backend.app.core.logging import logger
from backend.app.core.optional_modules import (
    SPLIT_URL_UNAVAILABLE_DETAIL,
    split_url_preview_handler,
    split_url_routing_available,
)
from backend.app.core.permissions import (
    Action,
    ResourceType,
    check_permission,
    get_permission_error_message,
)
from backend.app.core.scheduler import experiment_scheduler
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.compliance_audit_event import AuditAction, AuditOutcome
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User, UserRole
from backend.app.schemas.experiment import (
    ExperimentCreate,
    ExperimentDetailResponse,
    ExperimentListResponse,
    ExperimentResponse,
    ExperimentUpdate,
    ScheduleConfig,
)
from backend.app.schemas.results import ExperimentResultsResponse
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.audit_log_service import AuditLogService
from backend.app.services.audit_service import (
    AuditService,
    audit_identity,
    audit_snapshot,
    record_experiment_change,
)
from backend.app.services.experiment_service import (
    END_DATE_PASSED,
    AnalysisConfigError,
    EndDatePassedError,
    ExperimentService,
    is_experiment_key_conflict,
    record_end_date,
)
from backend.app.services.power_calculator_service import (
    SAMPLE_SIZE_NOT_FINITE_MESSAGE,
    TREATMENT_RATE_CEILING_MESSAGE,
    SampleSizeNotFiniteError,
    sample_size_two_proportions,
)

#: The states in which ``PUT /experiments/{id}`` accepts ``targeting_rules``
#: (#523). PAUSED only when targeting is the one field sent; see the route.
TARGETING_EDITABLE_STATUSES = frozenset(
    {ExperimentStatus.DRAFT, ExperimentStatus.PAUSED}
)

#: An experiment's status changes only through the lifecycle endpoints, which
#: carry each transition's checks (#542). The refusal names them.
STATUS_THROUGH_LIFECYCLE = (
    "status changes through POST /api/v1/experiments/{id}/start, /pause, "
    "/complete or /archive; it cannot be set by an update."
)
STATUS_ON_CREATE = (
    "a new experiment is created as draft; status changes through "
    "POST /api/v1/experiments/{id}/start, /pause, /complete or /archive."
)

# Create router with tag for documentation grouping
router = APIRouter(
    tags=["Experiments"],
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Authentication failed",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 401,
                            "message": "Could not validate credentials",
                        }
                    }
                }
            },
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Permission denied",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 403,
                            "message": "Not enough permissions",
                        }
                    }
                }
            },
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 500,
                            "message": "Internal server error",
                        }
                    }
                }
            },
        },
    },
)

#: How long a cached experiment detail or result set lives, in seconds.
EXPERIMENT_CACHE_TTL_SECONDS = 3600


# The experiment cache (#428, as #100 for the flags). `deps.get_cache_control`
# hands out a redis.asyncio client, so every call on it is awaited --
# un-awaited, `delete` and `setex` silently do nothing and a plain `for` over
# `scan_iter` raises TypeError, after the change has been committed. The cache
# is best-effort: a Redis error is logged and the request carries on.
async def _cache_get(cache_control: deps.CacheControl, key: str) -> Optional[str]:
    if not (cache_control.enabled and cache_control.redis):
        return None
    try:
        return await cache_control.redis.get(key)
    except Exception as cache_error:
        logger.warning("Experiment cache read failed for %s: %s", key, cache_error)
        return None


async def _cache_set(cache_control: deps.CacheControl, key: str, value: Any) -> None:
    if not (cache_control.enabled and cache_control.redis):
        return
    try:
        await cache_control.redis.setex(
            key, EXPERIMENT_CACHE_TTL_SECONDS, json.dumps(value, default=str)
        )
    except Exception as cache_error:
        logger.warning("Experiment cache write failed for %s: %s", key, cache_error)


async def _invalidate_experiment_cache(
    cache_control: deps.CacheControl, experiment_id: Optional[UUID]
) -> None:
    """Drop the cached detail of ``experiment_id`` and every cached list.

    Lists are keyed per user, but every role sees every experiment (#83), so a
    change drops all of them rather than only the owner's. Called after the
    change is committed, so a Redis error is logged rather than raised.
    """
    if not (cache_control.enabled and cache_control.redis):
        return
    try:
        if experiment_id is not None:
            await cache_control.redis.delete(
                experiment_cache.experiment_detail_cache_key(experiment_id)
            )
        keys = [
            key async for key in cache_control.redis.scan_iter(match="experiments:*")
        ]
        if keys:
            await cache_control.redis.delete(*keys)
    except Exception as cache_error:
        logger.warning("Experiment cache invalidation failed: %s", cache_error)


def _owner_name(db: Session, owner_id: Optional[UUID]) -> Optional[str]:
    """The name ``GET /experiments/{experiment_id}`` gives the owner (#921).

    The owner's full name when they have one, else their username when it
    contains no ``@``, else ``None``; never their email. Nothing refuses a
    username that is an email address, so one shaped like an address is not
    shown. ``None`` too when the experiment has no owner, or the account is
    gone by the time it is looked up. Read on every request, from the
    validated ``owner_id``, and never cached.
    """
    if owner_id is None:
        return None
    owner = db.get(User, owner_id)
    if owner is None:
        return None
    full_name = (owner.full_name or "").strip()
    if full_name:
        return full_name
    username = (owner.username or "").strip()
    if username and "@" not in username:
        return username
    return None


def _status_text(value: Any) -> Optional[str]:
    """An experiment status as its value (``"draft"``) for the audit record."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _same_experiment_type(requested: Any, stored: Any) -> bool:
    """Compare an incoming ``experiment_type`` with the stored one by value."""
    return getattr(requested, "value", requested) == getattr(stored, "value", stored)


def _refuse_status(message: str) -> HTTPException:
    """A 422 on ``status`` in the shape of a request-validation error."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[{"loc": ["body", "status"], "msg": message, "type": "value_error"}],
    )


def _reject_unroutable_split_url(experiment_type: Any) -> None:
    """
    Refuse ``experiment_type=split_url`` when nothing can route it.

    ``ExperimentType.SPLIT_URL`` is a live database enum value and cannot be
    withdrawn cheaply, so a build without the split_url module would otherwise
    happily store a split-URL experiment that never serves a single user. Fail
    loudly at the write instead: 501 says this deployment does not have it.
    """
    if experiment_type is None:
        return
    value = getattr(experiment_type, "value", experiment_type)
    if value != "split_url":
        return
    if split_url_routing_available():
        return
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=SPLIT_URL_UNAVAILABLE_DETAIL,
    )


@router.get(
    "/",
    response_model=ExperimentListResponse,
    summary="List experiments",
    response_description="Returns a paginated list of experiments",
)
async def list_experiments(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
    status_filter: Optional[str] = Query(
        None,
        description="Filter by experiment status (draft, active, paused, completed, archived)",
    ),
    skip: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    limit: int = Query(
        100, ge=1, le=500, description="Maximum number of records to return"
    ),
    search: Optional[str] = Query(
        None, description="Search term to filter experiment names and descriptions"
    ),
    sort_by: Optional[str] = Query(
        "created_at",
        description="Field to sort by (created_at, updated_at, name, status)",
    ),
    sort_order: Optional[str] = Query("desc", description="Sort order (asc, desc)"),
) -> ExperimentListResponse:
    """
    List experiments with filtering and pagination.

    This endpoint retrieves a list of experiments based on the provided filters.
    Regular users only see their own experiments, while superusers can see all experiments.

    The results are paginated and can be sorted by various fields.

    - **status_filter**: Filter experiments by their current status
    - **skip/limit**: Pagination parameters
    - **search**: Filter experiments by name or description
    - **sort_by/sort_order**: Control the ordering of results

    Returns:
        ExperimentListResponse: Paginated list of experiments with total count
    """
    # Every other verb in this file checks its permission -- CREATE at :253,
    # READ at :375, DELETE at :732, UPDATE at :1012 -- and the equivalent
    # feature-flag endpoint checks LIST at feature_flags.py:123. This one
    # checked nothing: `get_current_active_user` establishes that the caller is
    # authenticated and not disabled, and that was the whole gate.
    #
    # It went unnoticed because the ownership filter below was doing the access
    # control by accident: a caller with no business listing experiments still
    # only saw their own. That makes the omission invisible until someone
    # widens the scoping -- which is exactly what #83 asks for.
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.LIST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to list experiments",
        )

    try:
        # Create experiment service
        experiment_service = ExperimentService(db)

        # Everyone the LIST check above admitted sees the whole platform.
        #
        # This used to branch on `is_superuser`: a superuser saw everything,
        # everyone else only rows they owned. That is a second access model
        # sitting on top of the role table, and it contradicts it -- all four
        # roles carry Action.LIST on experiments, and ANALYST exists precisely
        # to "view all data but not create or modify". An analyst
        # saw an empty platform (#83).
        #
        # A deployment is single tenant (founder, 2026-09-21), so "all
        # experiments" is the whole of what this installation holds. Tenant
        # isolation is the workspaces module's job: `Experiment.workspace_id`
        # is a bare indexed UUID that core never filters on, and if scoping is
        # ever wanted here it belongs there, keyed on membership, not on who
        # happens to have created a row.
        experiments = experiment_service.get_experiments(
            skip=skip,
            limit=limit,
            status=status_filter,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        total = experiment_service.count_experiments(
            status=status_filter, search=search
        )

        # Create response
        return ExperimentListResponse(
            items=[ExperimentResponse.model_validate(exp) for exp in experiments],
            total=total,
            skip=skip,
            limit=limit,
        )
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e, "Experiment list", "Could not list the experiments", db=db, logger=logger
        )


#: The fixed message for a create that failed for any reason other than a
#: key the caller chose already being taken.
CREATE_FAILED = "Something went wrong while creating the experiment"


@router.post(
    "/",
    response_model=ExperimentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create experiment",
    response_description="Returns the created experiment",
)
async def create_experiment(
    experiment_in: ExperimentCreate = Body(..., description="Experiment creation data"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> ExperimentResponse:
    """
    Create new experiment.

    This endpoint allows users to create a new experiment with variants and metrics.
    The created experiment is owned by the current user.

    A new experiment is always created as `draft`. `status` may be left out or
    sent as `"draft"`; any other value is refused with 422. Start it with
    `POST /api/v1/experiments/{id}/start`.

    Returns:
        ExperimentResponse: The created experiment

    Raises:
        HTTPException 422: If the experiment data is invalid, or `status` is
            anything other than `draft`
        HTTPException: If creation fails
    """
    # Role decides, not the username: this used to gate on the substring
    # "viewer" appearing in `username`, which both let a VIEWER named e.g.
    # "alice" create experiments and blocked a DEVELOPER named "viewer.smith".
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.EXPERIMENT, Action.CREATE),
        )

    # Refused, not silently replaced by DRAFT (#542): a client that asked for
    # an ACTIVE experiment must learn it did not get one.
    if _status_text(experiment_in.status) != ExperimentStatus.DRAFT.value:
        raise _refuse_status(STATUS_ON_CREATE)

    _reject_unroutable_split_url(getattr(experiment_in, "experiment_type", None))

    # Every segment the rules name exists and is active (#440).
    refuse_unusable_segments(db, experiment_in.targeting_rules)

    try:
        # Create experiment service
        experiment_service = ExperimentService(db)

        # Set owner_id to current user
        experiment_data = experiment_in.model_dump()
        experiment_data["owner_id"] = current_user.id

        # Ensure status is a string (convert enum to value if needed)
        if "status" in experiment_data:
            if isinstance(experiment_data["status"], ExperimentStatus):
                experiment_data["status"] = experiment_data["status"].value
            elif not isinstance(experiment_data["status"], str):
                # Default to DRAFT if invalid status
                experiment_data["status"] = "draft"

        # Create experiment
        experiment = experiment_service.create_experiment(
            obj_in=ExperimentCreate(**experiment_data),
            user_id=current_user.id,  # Add the user_id parameter
        )

        # Compliance audit logging (non-fatal)
        try:
            # The service returns the experiment as a dict, not a model, so
            # getattr() alone recorded neither the id nor the name.
            if isinstance(experiment, dict):
                exp_id, exp_name = experiment.get("id"), experiment.get("name")
            else:
                exp_id = getattr(experiment, "id", None)
                exp_name = getattr(experiment, "name", None)
            audit = AuditLogService(db)
            audit.log(
                action=AuditAction.CREATE,
                resource_type="experiment",
                outcome=AuditOutcome.SUCCESS,
                resource_id=str(exp_id) if exp_id else None,
                actor_id=str(current_user.id) if current_user else None,
                new_value={"name": exp_name},
            )
            # log() only flushes, and the create above has already committed, so
            # without this the record is rolled back when the session closes.
            db.commit()
        except Exception as _audit_err:
            db.rollback()
            logger.warning(
                f"Compliance audit logging failed for experiment create: {_audit_err}"
            )

        created_id = (
            experiment.get("id")
            if isinstance(experiment, dict)
            else getattr(experiment, "id", None)
        )
        record_experiment_change(
            db, current_user, ActionType.EXPERIMENT_CREATE, created_id
        )

        await _invalidate_experiment_cache(cache_control, None)

        return ExperimentResponse.model_validate(experiment)
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except (IntegrityError, DataError) as e:
        # The database refused the request's own data. Roll back first so the
        # session stays usable, then answer without the driver's text: that
        # goes to the server log only, tied to the request ID.
        db.rollback()
        key = experiment_in.key
        if key is not None and is_experiment_key_conflict(e):
            # Only a key the caller chose is named. A generated key that
            # collides is not theirs: the service has already tried other
            # keys (#388), and if every one was taken it takes the generic
            # answer below.
            logger.info("Experiment create refused: key %r already exists", key)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An experiment with the key '{key}' already exists.",
            )
        logger.exception("Experiment create failed (%s)", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_detail(CREATE_FAILED),
        )
    except Exception as e:
        raise unexpected_failure(
            e, "Experiment create", CREATE_FAILED, db=db, logger=logger
        )


@router.get(
    "/{experiment_id}",
    response_model=ExperimentDetailResponse,
    summary="Get experiment",
    response_description="Returns the experiment details",
)
async def get_experiment(
    experiment_id: UUID = Path(..., description="The ID of the experiment to retrieve"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> ExperimentDetailResponse:
    """
    Get experiment by ID.

    Retrieves the detailed information for a specific experiment.
    Every role the role table grants READ on experiments may open any of them,
    as every role granted LIST sees all of them in the list.

    Returns:
        ExperimentResponse: The experiment details

    Raises:
        HTTPException 404: If experiment not found
        HTTPException 403: If user doesn't have access to this experiment
    """
    try:
        # Create experiment service
        experiment_service = ExperimentService(db)

        # Get experiment
        experiment = experiment_service.get_experiment_by_id(experiment_id)
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # The role table is the authority on who may read an experiment, as it
        # is on who may list them (see 4383c8ac): all four roles carry READ,
        # and the list already returns this same ExperimentResponse for every
        # experiment. There used to be an ownership requirement here as well,
        # and it refused everyone but superusers -- the service returns a dict,
        # `check_ownership` looks for an `owner_id` attribute, so not even the
        # creator matched.
        if not current_user.is_superuser and not check_permission(
            current_user, ResourceType.EXPERIMENT, Action.READ
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=get_permission_error_message(
                    ResourceType.EXPERIMENT, Action.READ
                ),
            )

        # The cached detail is returned only after the same checks as an
        # uncached read. The owner's name is not in the cache: it is looked
        # up on every read, cached or not, so a rename shows on the next one.
        detail_key = experiment_cache.experiment_detail_cache_key(experiment_id)
        cached_data = await _cache_get(cache_control, detail_key)
        if cached_data:
            detail = ExperimentDetailResponse.model_validate_json(cached_data)
            detail.owner_name = _owner_name(db, detail.owner_id)
            return detail

        # Create the response - if it's a dictionary, use model_validate directly
        if isinstance(experiment, dict):
            response = ExperimentResponse.model_validate(experiment)
        else:
            # Convert model to dict first - handle potential compatibility issues
            try:
                # First try standard jsonable_encoder
                experiment_dict = jsonable_encoder(experiment)
                response = ExperimentResponse.model_validate(experiment_dict)
            except TypeError as e:
                if "model_dump() got an unexpected keyword argument 'mode'" in str(e):
                    try:
                        # Try to use model_dump if it exists
                        if hasattr(experiment, "model_dump"):
                            experiment_dict = experiment.model_dump()
                            response = ExperimentResponse.model_validate(
                                experiment_dict
                            )
                        # Fall back to dict() for older pydantic versions
                        elif hasattr(experiment, "__table__") and hasattr(
                            experiment.__table__, "columns"
                        ):
                            experiment_dict = {
                                c.name: getattr(experiment, c.name)
                                for c in experiment.__table__.columns
                            }
                            response = ExperimentResponse.model_validate(
                                experiment_dict
                            )
                        else:
                            # Final fallback - convert all attributes
                            experiment_dict = {
                                k: v
                                for k, v in experiment.__dict__.items()
                                if not k.startswith("_")
                            }
                            response = ExperimentResponse.model_validate(
                                experiment_dict
                            )
                    except Exception as ex:
                        raise unexpected_failure(
                            ex,
                            "Experiment read",
                            "Could not load the experiment",
                            db=db,
                            logger=logger,
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        )
                else:
                    raise

        await _cache_set(cache_control, detail_key, response.model_dump(mode="json"))

        detail = ExperimentDetailResponse.model_validate(response.model_dump())
        detail.owner_name = _owner_name(db, detail.owner_id)
        return detail
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e, "Experiment read", "Could not load the experiment", db=db, logger=logger
        )


@router.put(
    "/{experiment_id}",
    response_model=ExperimentResponse,
    summary="Update experiment",
    response_description="Returns the updated experiment",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "The experiment's state does not allow this change",
        },
    },
)
async def update_experiment(
    experiment_id: UUID = Path(..., description="The ID of the experiment to update"),
    experiment_in: ExperimentUpdate = Body(..., description="Experiment update data"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> ExperimentResponse:
    """
    Update experiment.

    This endpoint allows users to update an existing experiment.
    The caller's role must hold UPDATE on experiments; who created the
    experiment is not considered.

    Fields that can be updated:
    - Basic properties: name, description, hypothesis
    - Variants: if experiment is in DRAFT status
    - Metrics: if experiment is in DRAFT status
    - Targeting (`targeting_rules`): while the experiment is DRAFT or PAUSED,
      for every role including superusers. On a PAUSED experiment it must be
      the only field in the request. On an ACTIVE, COMPLETED or ARCHIVED
      experiment a request that includes `targeting_rules` is refused, even
      when the value equals the stored one; pause the experiment first.
      People already assigned keep their variant; the new rules apply to
      people not yet in the experiment once it is resumed.

    `status` is not changed by an update, for any role including superusers.
    A `status` equal to the current one is accepted and changes nothing, so an
    experiment fetched with GET can be sent back. Any other value is refused
    with 422; status changes through `POST /api/v1/experiments/{id}/start`,
    `/pause`, `/complete` or `/archive`. `status` still counts as a field
    sent, so on a PAUSED experiment `targeting_rules` must be sent without it.

    `schedule` is not applied here: a request that contains it, with any
    value including null, is refused with 422. Schedule an experiment with
    `PUT /api/v1/experiments/{experiment_id}/schedule`.

    Returns:
        ExperimentResponse: The updated experiment

    Raises:
        HTTPException 403: If the caller's role does not hold UPDATE on
            experiments. This is checked before the experiment's state.
        HTTPException 400: If the experiment's state does not allow the change
            (the detail names the state): any change to a non-DRAFT experiment
            by a non-superuser, other than targeting alone on a PAUSED one;
            targeting outside DRAFT or PAUSED; targeting with other fields on
            a PAUSED experiment; variants or metrics outside DRAFT
        HTTPException 404: If the experiment doesn't exist
        HTTPException 422: If `targeting_rules` is not a valid rule set,
            `status` differs from the experiment's current status, or the
            request contains `schedule`
    """
    try:
        # Get experiment
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # READ and UPDATE on experiments; who created it is not considered
        experiment = deps.get_experiment_change_access(experiment, current_user)

        # Only now: the module check comes *after* existence and authorisation,
        # so an unauthorised caller (or a request for a missing experiment)
        # gets the 403/404 it always got, not a 501 that reveals which
        # modules this build lacks before it has checked who is asking.
        # And only for a *change* to split_url: a full-object PUT that
        # restates the stored type of a legacy split-URL row is renaming or
        # re-describing it, not storing a new experiment nothing routes.
        requested_type = getattr(experiment_in, "experiment_type", None)
        if requested_type is not None and not _same_experiment_type(
            requested_type, experiment.experiment_type
        ):
            _reject_unroutable_split_url(requested_type)

        # Targeting by state, for every role including the superuser (#523).
        # Keyed on the fields the client SENT (``model_fields_set``), not on
        # the dict built below, so no later processing step (the ``status``
        # normalisation, dropping nulls) can turn a request carrying another
        # field into a targeting-only one. Presence, not difference:
        # re-sending the stored value is an edit.
        fields_sent = experiment_in.model_fields_set
        sends_targeting = "targeting_rules" in fields_sent
        if sends_targeting and experiment.status not in TARGETING_EDITABLE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Targeting can be changed only while the experiment is "
                    f"draft or paused; it is {experiment.status.value}."
                ),
            )
        paused_targeting_only = experiment.status == ExperimentStatus.PAUSED and (
            fields_sent == {"targeting_rules"}
        )

        # Prevent updates to non-draft experiments unless user is superuser;
        # a PAUSED experiment's targeting, sent on its own, is the exception.
        if (
            experiment.status != ExperimentStatus.DRAFT
            and not paused_targeting_only
            and not current_user.is_superuser
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot update experiments in {experiment.status.value} status",
            )

        # The superuser passes the guard above, so say the PAUSED rule for it.
        if (
            experiment.status == ExperimentStatus.PAUSED
            and sends_targeting
            and not paused_targeting_only
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "While the experiment is paused, targeting can be changed "
                    "only on its own; send targeting_rules without other fields."
                ),
            )

        # Status changes only through /start, /pause, /complete and /archive,
        # which carry each transition's checks (#542), for every role
        # including the superuser. A status equal to the current one is a
        # no-op so a GET-then-PUT round trip still works. This runs after the
        # role and state refusals above, so a request those refuse keeps its
        # 403 or 400; and ``status`` stays in ``fields_sent``, so a PAUSED
        # experiment's targeting sent with a status is still refused there.
        # A null status never gets here: the schema refuses it (#541).
        if "status" in fields_sent:
            if _status_text(experiment_in.status) != _status_text(experiment.status):
                raise _refuse_status(STATUS_THROUGH_LIFECYCLE)

        # Every segment the rules name exists and is active (#440).
        if sends_targeting:
            refuse_unusable_segments(db, experiment_in.targeting_rules)

        # Get update data. The status, equal to the stored one if present, is
        # never written by an update.
        update_data = experiment_in.model_dump(exclude_unset=True)
        update_data.pop("status", None)

        # For non-draft experiments, prevent updates to restricted fields.
        # This runs after the superuser passes the guard above, so it binds
        # every role: once an experiment leaves draft, nobody changes how its
        # results are judged (#580). It works on presence, so re-sending the
        # stored value is refused too.
        if experiment.status != ExperimentStatus.DRAFT:
            restricted_fields = [
                "variants",
                "metrics",
                "start_date",
                "end_date",
                "correction_method",
                "confidence_level",
            ]
            for field in restricted_fields:
                if field in update_data:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Cannot update {field} for experiments in {experiment.status.value} status",
                    )

        # Capture pre-update snapshot for audit trail. The update below
        # rebinds targeting_rules (setattr) rather than changing the dict,
        # so today the copy is not needed; it guards the old value against
        # a future in-place change.
        old_exp_snapshot = {
            "name": experiment.name,
            "status": _status_text(experiment.status),
            "targeting_rules": copy.deepcopy(experiment.targeting_rules),
        }
        before_audit = audit_snapshot(EntityType.EXPERIMENT, experiment)

        # Create experiment service
        experiment_service = ExperimentService(db)

        # Update experiment.  A refused analysis config is a 422 naming the
        # field, in the same shape as a request-validation error.
        try:
            updated_experiment = experiment_service.update_experiment(
                experiment, update_data
            )
        except AnalysisConfigError as err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=[
                    {
                        "loc": ["body", err.field],
                        "msg": err.message,
                        "type": "value_error",
                    }
                ],
            )

        # Compliance audit logging (non-fatal)
        try:
            # As on create: the service returns a dict, not a model.
            def _updated(field: str) -> Any:
                if isinstance(updated_experiment, dict):
                    return updated_experiment.get(field)
                return getattr(updated_experiment, field, None)

            new_exp_snapshot = {
                "name": _updated("name"),
                "status": _status_text(_updated("status")),
                "targeting_rules": _updated("targeting_rules"),
            }
            audit = AuditLogService(db)
            audit.log(
                action=AuditAction.UPDATE,
                resource_type="experiment",
                outcome=AuditOutcome.SUCCESS,
                resource_id=str(experiment_id),
                actor_id=str(current_user.id) if current_user else None,
                old_value=old_exp_snapshot,
                new_value=new_exp_snapshot,
            )
            # log() only flushes, and the update above has already committed, so
            # without this the record is rolled back when the session closes.
            db.commit()
        except Exception as _audit_err:
            db.rollback()
            logger.warning(
                f"Compliance audit logging failed for experiment update: {_audit_err}"
            )

        record_experiment_change(
            db, current_user, ActionType.EXPERIMENT_UPDATE, experiment_id, before_audit
        )

        await _invalidate_experiment_cache(cache_control, experiment_id)

        # Handle the response with compatibility
        try:
            # First try standard jsonable_encoder
            experiment_dict = jsonable_encoder(updated_experiment)
            return ExperimentResponse.model_validate(experiment_dict)
        except TypeError as e:
            if "model_dump() got an unexpected keyword argument 'mode'" in str(e):
                try:
                    # Try to use model_dump if it exists
                    if hasattr(updated_experiment, "model_dump"):
                        experiment_dict = updated_experiment.model_dump()
                        return ExperimentResponse.model_validate(experiment_dict)
                    # Fall back to dict() for older pydantic versions
                    elif hasattr(updated_experiment, "__table__") and hasattr(
                        updated_experiment.__table__, "columns"
                    ):
                        experiment_dict = {
                            c.name: getattr(updated_experiment, c.name)
                            for c in updated_experiment.__table__.columns
                        }
                        return ExperimentResponse.model_validate(experiment_dict)
                    else:
                        # Final fallback - convert all attributes
                        experiment_dict = {
                            k: v
                            for k, v in updated_experiment.__dict__.items()
                            if not k.startswith("_")
                        }
                        return ExperimentResponse.model_validate(experiment_dict)
                except Exception as ex:
                    raise unexpected_failure(
                        ex,
                        "Experiment update",
                        "Could not update the experiment",
                        db=db,
                        logger=logger,
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )
            else:
                raise
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment update",
            "Could not update the experiment",
            db=db,
            logger=logger,
        )


@router.delete(
    "/{experiment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete experiment",
    response_description="No content, experiment successfully deleted",
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": "Experiment successfully deleted",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Not enough permissions to delete this experiment",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 403,
                            "message": "Not enough permissions",
                        }
                    }
                }
            },
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Experiment not found",
            "content": {
                "application/json": {
                    "example": {
                        "error": {"status_code": 404, "message": "Experiment not found"}
                    }
                }
            },
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Invalid experiment status",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 400,
                            "message": "Experiment not in DRAFT status",
                        }
                    }
                }
            },
        },
    },
)
async def delete_experiment(
    experiment_id: UUID = Path(..., description="The ID of the experiment to delete"),
    experiment_key: str = Query(
        ...,
        description="Key or ID of the experiment to delete (must match experiment_id)",
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> None:
    """
    Delete an experiment.

    This endpoint allows users to delete an existing experiment.
    The caller's role must hold READ and DELETE on experiments; who created
    the experiment is not considered.

    Deleting an experiment has the following effects:
    - The experiment and all its related data (variants, metrics) are permanently removed
    - Any cached data related to the experiment is invalidated

    **Note**: This operation cannot be undone. For active experiments, consider
    using the archive operation instead.

    Returns:
        None: No content, experiment was successfully deleted

    Raises:
        HTTPException 400: If experiment is not in DRAFT status
        HTTPException 403: If the user doesn't have permission to delete this experiment
        HTTPException 404: If the experiment doesn't exist
    """
    # Get experiment by ID
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not experiment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
        )

    # Verify experiment_key matches experiment_id
    if str(experiment.id) != experiment_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="experiment_key does not match experiment_id",
        )

    # READ and DELETE on experiments; who created it is not considered
    experiment = deps.get_experiment_change_access(
        experiment, current_user, Action.DELETE
    )

    # Additional check for experiment status for non-draft experiments
    if experiment.status != ExperimentStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete experiments that are not in DRAFT status",
        )

    # Capture experiment info before deletion for audit trail
    deleted_exp_snapshot = {
        "name": experiment.name,
        "status": str(experiment.status),
        "owner_id": str(experiment.owner_id) if experiment.owner_id else None,
    }
    deleted_exp_id = str(experiment.id)
    deleted_audit = audit_identity(audit_snapshot(EntityType.EXPERIMENT, experiment))

    # Delete experiment
    db.delete(experiment)
    db.commit()

    # Compliance audit logging (non-fatal)
    try:
        audit = AuditLogService(db)
        audit.log(
            action=AuditAction.DELETE,
            resource_type="experiment",
            outcome=AuditOutcome.SUCCESS,
            resource_id=deleted_exp_id,
            actor_id=str(current_user.id) if current_user else None,
            old_value=deleted_exp_snapshot,
        )
        # log() only flushes, and the delete above has already committed, so
        # without this the record is rolled back when the session closes.
        db.commit()
    except Exception as _audit_err:
        db.rollback()
        logger.warning(
            f"Compliance audit logging failed for experiment delete: {_audit_err}"
        )

    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.EXPERIMENT_DELETE,
        entity_type=EntityType.EXPERIMENT,
        entity_id=experiment_id,
        entity_name=deleted_audit["name"] or str(experiment_id),
        before=deleted_audit,
    )

    await _invalidate_experiment_cache(cache_control, experiment_id)

    # Return 204 No Content
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{experiment_id}/start",
    response_model=ExperimentResponse,
    summary="Start experiment",
    response_description="Returns the started experiment",
)
async def start_experiment(
    experiment_id: UUID = Path(..., description="The ID of the experiment to start"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> ExperimentResponse:
    """
    Start an experiment.

    This endpoint activates an experiment, changing its status to ACTIVE
    and setting the start date to now if not already set. A draft started
    before its scheduled start date starts now, and its start date is set to
    now; a scheduled end date is kept.

    Starting an experiment makes it eligible for:
    - User traffic assignment
    - Event tracking and analysis
    - Results calculation

    Prerequisites for starting an experiment:
    - The experiment must be in DRAFT or PAUSED status
    - The experiment must have at least one control and one non-control variant
    - The experiment must have at least one metric defined
    - An experiment with no start date must not have an end date that has
      passed: set a later one, or clear it, with
      PUT /{experiment_id}/schedule first

    Returns:
        ExperimentResponse: The updated experiment with ACTIVE status

    Raises:
        HTTPException 400: If the experiment cannot be started
        HTTPException 403: If the caller's role does not hold UPDATE on experiments
    """
    try:
        # Get experiment
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # READ and UPDATE on experiments; who created it is not considered
        experiment = deps.get_experiment_change_access(experiment, current_user)

        # Create experiment service
        experiment_service = ExperimentService(db)

        # Check if experiment can be started
        if (
            experiment.status != ExperimentStatus.DRAFT
            and experiment.status != ExperimentStatus.PAUSED
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot start experiment with status: {experiment.status.value}",
            )

        # Check for required components
        if not experiment.variants or len(experiment.variants) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Experiment must have at least two variants (one control and one test)",
            )

        has_control = any(variant.is_control for variant in experiment.variants)
        if not has_control:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Experiment must have at least one control variant",
            )

        if not experiment.metric_definitions or len(experiment.metric_definitions) < 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Experiment must have at least one metric defined",
            )

        before_audit = audit_snapshot(EntityType.EXPERIMENT, experiment)

        # Start experiment. A start whose start date would not be before the
        # end date is refused before anything is written (#953).
        try:
            started_experiment = experiment_service.start_experiment(experiment)
        except EndDatePassedError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=END_DATE_PASSED
            ) from err

        record_experiment_change(
            db, current_user, ActionType.EXPERIMENT_START, experiment_id, before_audit
        )

        await _invalidate_experiment_cache(cache_control, experiment_id)

        return ExperimentResponse.model_validate(started_experiment)
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment start",
            "Could not start the experiment",
            db=db,
            logger=logger,
        )


@router.post(
    "/{experiment_id}/pause",
    response_model=ExperimentResponse,
    summary="Pause experiment",
    response_description="Returns the paused experiment",
)
async def pause_experiment(
    experiment_id: UUID = Path(..., description="The ID of the experiment to pause"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> ExperimentResponse:
    """
    Pause an experiment.

    This endpoint pauses an active experiment, changing its status to PAUSED.
    Pausing an experiment temporarily stops:
    - New user assignments (existing assignments remain in effect)
    - Data collection and analysis

    The experiment stays paused until it is started again with the start
    endpoint. It is never resumed automatically unless a resume is scheduled
    after pausing (PUT /{experiment_id}/schedule with a start_date), and a
    restart of the service does not resume it.

    Returns:
        ExperimentResponse: The updated experiment with PAUSED status

    Raises:
        HTTPException 400: If the experiment cannot be paused
        HTTPException 403: If the caller's role does not hold UPDATE on experiments
    """
    try:
        # Get experiment
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # READ and UPDATE on experiments; who created it is not considered
        experiment = deps.get_experiment_change_access(experiment, current_user)

        # Check if experiment can be paused
        if experiment.status != ExperimentStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot pause experiment with status: {experiment.status.value}",
            )

        before_audit = audit_snapshot(EntityType.EXPERIMENT, experiment)

        # Update experiment status
        experiment.status = ExperimentStatus.PAUSED
        db.commit()
        db.refresh(experiment)

        record_experiment_change(
            db, current_user, ActionType.EXPERIMENT_PAUSE, experiment_id, before_audit
        )

        await _invalidate_experiment_cache(cache_control, experiment_id)

        # Serialise through the service so `metrics` comes from the
        # metric_definitions relationship (the ORM `metrics` column is JSONB).
        return ExperimentResponse.model_validate(
            ExperimentService(db).to_response_dict(experiment)
        )
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment pause",
            "Could not pause the experiment",
            db=db,
            logger=logger,
        )


@router.put(
    "/{experiment_id}/schedule",
    response_model=ExperimentResponse,
    summary="Update experiment schedule",
    response_description="Returns the experiment with updated scheduling",
)
async def update_experiment_schedule(
    experiment_id: UUID = Path(..., description="The ID of the experiment to schedule"),
    schedule: ScheduleConfig = Body(..., description="Scheduling configuration"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> ExperimentResponse:
    """
    Update experiment scheduling configuration.

    On a **DRAFT** experiment this schedules its activation and completion:

    - **start_date**: When the experiment should automatically activate
    - **end_date**: When the experiment should automatically complete
    - **time_zone**: IANA time zone name (default: UTC) for a date given
      without a UTC offset; a date with an offset keeps it. Dates are stored
      in UTC. A name that is not an IANA zone answers 422.

    On either status, a field the request omits is left unchanged and an
    explicit null clears it. On a draft, end_date must be at least an hour
    after start_date (the stored one when the request omits it).

    On a **PAUSED** experiment, start_date is the time to resume it at. It is
    stored as ``resume_at``; the experiment's own start_date is never moved. A
    null start_date cancels a scheduled resume. end_date must be later than
    the experiment's start date and, with a resume scheduled, at least an hour
    after the resume time. A paused experiment is resumed automatically only
    by a resume scheduled after it was paused; any change of status cancels a
    scheduled resume.

    start_date may not be more than ten minutes in the past.

    Returns:
        ExperimentResponse: The updated experiment with scheduling information

    Raises:
        HTTPException 400: If scheduling parameters are invalid
        HTTPException 403: If the caller's role does not hold READ and UPDATE
            on experiments
        HTTPException 404: If experiment not found
        HTTPException 422: If a date is malformed or time_zone is not an IANA
            time zone name
    """
    try:
        # Get experiment by ID
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # READ and UPDATE on experiments; who created it is not considered
        experiment = deps.get_experiment_change_access(experiment, current_user)

        # Create experiment service
        experiment_service = ExperimentService(db)

        # Validate experiment status
        if experiment.status not in [ExperimentStatus.DRAFT, ExperimentStatus.PAUSED]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot schedule experiment with status {experiment.status.value}. "
                f"Experiment must be in DRAFT or PAUSED status.",
            )

        # Update experiment schedule
        try:
            # Convert ScheduleConfig to dictionary for service
            schedule_dict = {
                "start_date": schedule.start_date,
                "end_date": schedule.end_date,
                "time_zone": schedule.time_zone,
            }

            before_audit = audit_snapshot(EntityType.EXPERIMENT, experiment)

            # Update the experiment
            updated_experiment = experiment_service.update_experiment_schedule(
                experiment=experiment,
                schedule=schedule_dict,
                fields_set=schedule.model_fields_set,
            )

        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        record_experiment_change(
            db,
            current_user,
            ActionType.EXPERIMENT_UPDATE,
            experiment_id,
            before_audit,
            reason="schedule",
        )

        await _invalidate_experiment_cache(cache_control, experiment_id)

        return ExperimentResponse.model_validate(updated_experiment)
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment schedule update",
            "Could not update the experiment's schedule",
            db=db,
            logger=logger,
        )


@router.post(
    "/{experiment_id}/complete",
    response_model=ExperimentResponse,
    summary="Complete experiment",
    response_description="Returns the completed experiment",
)
async def complete_experiment(
    experiment_id: UUID = Path(..., description="The ID of the experiment to complete"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> ExperimentResponse:
    """
    Complete an experiment.

    This endpoint marks an experiment as completed, changing its status to COMPLETED
    and setting the end date to now. A start date that is not before now,
    left by an earlier version on an experiment started before its scheduled
    start date, is moved to just before the end date.

    Completing an experiment:
    - Stops new user assignments
    - Finalizes data collection
    - Makes results available for final analysis

    **Note**: This operation is typically performed when an experiment has
    gathered sufficient data to make a decision.

    Returns:
        ExperimentResponse: The updated experiment with COMPLETED status

    Raises:
        HTTPException 400: If the experiment cannot be completed
        HTTPException 403: If the caller's role does not hold UPDATE on experiments
    """
    try:
        # Get experiment
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # READ and UPDATE on experiments; who created it is not considered
        experiment = deps.get_experiment_change_access(experiment, current_user)

        # Check if experiment is in a valid state to be completed
        if experiment.status not in [ExperimentStatus.ACTIVE, ExperimentStatus.PAUSED]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot complete experiment with status: {experiment.status.value}",
            )

        before_audit = audit_snapshot(EntityType.EXPERIMENT, experiment)

        # Update experiment status. A start date still ahead, which an
        # earlier version left on an experiment started by hand before its
        # scheduled start, is moved before the end date (#974).
        experiment.status = ExperimentStatus.COMPLETED
        record_end_date(experiment, datetime.now(timezone.utc))
        db.commit()
        db.refresh(experiment)

        record_experiment_change(
            db,
            current_user,
            ActionType.EXPERIMENT_COMPLETE,
            experiment_id,
            before_audit,
        )

        await _invalidate_experiment_cache(cache_control, experiment_id)

        # Serialise through the service so `metrics` comes from the
        # metric_definitions relationship (the ORM `metrics` column is JSONB).
        return ExperimentResponse.model_validate(
            ExperimentService(db).to_response_dict(experiment)
        )
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment complete",
            "Could not complete the experiment",
            db=db,
            logger=logger,
        )


@router.get(
    "/{experiment_id}/results",
    response_model=ExperimentResultsResponse,
    summary="Get experiment results",
    response_description="Returns the experiment results and analysis",
)
async def get_experiment_results(
    experiment_id: UUID = Path(
        ..., description="The ID of the experiment to get results for"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> ExperimentResultsResponse:
    """
    Get experiment results and statistical analysis.

    Requires READ on experiments (who created the experiment is not
    considered), refuses DRAFT experiments, and then delegates to the analytics
    results engine, so this endpoint returns exactly the same payload as
    ``GET /api/v1/results/{experiment_id}`` with default options: the
    experiment's stored correction method and confidence level.

    Raises:
        HTTPException 400: If the experiment is in DRAFT status
        HTTPException 403: If the caller's role does not hold READ on experiments
        HTTPException 404: If the experiment does not exist
    """
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not experiment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
        )

    deps.get_experiment_read_access(experiment, current_user)

    if experiment.status == ExperimentStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot get results for experiments in DRAFT status",
        )

    # None, explicitly: the route function's own defaults are ``Query``
    # objects, not values. None means the experiment's stored settings, so
    # this goes through the same resolution, cache key, snapshot rule and
    # labels as ``GET /results/{id}`` with no options (#580).
    return results_endpoints.get_experiment_results(
        experiment_id=experiment_id,
        confidence_level=None,
        correction_method=None,
        use_cache=True,
        breakdown=None,
        db=db,
        current_user=current_user,
    )


# New endpoints


@router.post(
    "/{experiment_id}/archive",
    response_model=ExperimentResponse,
    summary="Archive experiment",
    response_description="Returns the archived experiment",
)
async def archive_experiment(
    experiment_id: UUID = Path(..., description="The ID of the experiment to archive"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> ExperimentResponse:
    """
    Archive an experiment.

    This endpoint archives an experiment, changing its status to ARCHIVED.
    Archiving an experiment:
    - Makes it inactive for user assignments
    - Preserves all collected data for historical analysis
    - Moves it to the archive section in the UI

    This is the preferred alternative to deletion for experiments that are no longer
    active but whose data should be preserved.

    Returns:
        ExperimentResponse: The updated experiment with ARCHIVED status

    Raises:
        HTTPException 400: If the experiment is already archived
        HTTPException 403: If the caller's role does not hold UPDATE on experiments
    """
    try:
        # Get experiment
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # READ and UPDATE on experiments; who created it is not considered
        experiment = deps.get_experiment_change_access(experiment, current_user)

        # Check if experiment is already archived
        if experiment.status == ExperimentStatus.ARCHIVED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Experiment is already archived",
            )

        # Create experiment service
        experiment_service = ExperimentService(db)

        before_audit = audit_snapshot(EntityType.EXPERIMENT, experiment)

        # Archive experiment
        experiment_service.archive_experiment(experiment)

        record_experiment_change(
            db,
            current_user,
            ActionType.EXPERIMENT_UPDATE,
            experiment_id,
            before_audit,
            reason="archive",
        )

        await _invalidate_experiment_cache(cache_control, experiment_id)

        # Serialise through the service so `metrics` comes from the
        # metric_definitions relationship (the ORM `metrics` column is JSONB).
        return ExperimentResponse.model_validate(
            ExperimentService(db).to_response_dict(experiment)
        )
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment archive",
            "Could not archive the experiment",
            db=db,
            logger=logger,
        )


@router.post(
    "/{experiment_id}/clone",
    response_model=ExperimentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Clone experiment",
    response_description="Returns the cloned experiment",
)
async def clone_experiment(
    experiment_id: UUID = Path(..., description="The ID of the experiment to clone"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> ExperimentResponse:
    """
    Clone an experiment.

    This endpoint creates a new experiment that is a copy of an existing experiment.
    The cloned experiment will:
    - Have a name prefixed with "Copy of", shortened to fit the 100-character
      name limit when the source's name is long
    - Be in DRAFT status
    - Have the same variants, metrics, and configuration as the original
    - Have the current user as the owner

    This is useful for creating variations of existing experiments or repeating
    successful experiments with minor modifications.

    Returns:
        ExperimentResponse: The newly created cloned experiment

    Raises:
        HTTPException 403: If the caller's role does not hold READ and CREATE on experiments
        HTTPException 404: If the source experiment doesn't exist
    """
    try:
        # Get source experiment
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # A clone is a new experiment: READ on the source, and CREATE, which
        # the create route requires too.
        experiment = deps.get_experiment_read_access(experiment, current_user)
        if not check_permission(current_user, ResourceType.EXPERIMENT, Action.CREATE):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=get_permission_error_message(
                    ResourceType.EXPERIMENT, Action.CREATE
                ),
            )

        # A clone stores a new experiment of the source's type, so it is held
        # to the same rule as create: a build that cannot route split-URL
        # traffic must not mint a second split-URL experiment from a legacy one.
        _reject_unroutable_split_url(experiment.experiment_type)

        # The clone's rules are the source's, and every segment they name
        # must still be usable (#440).
        refuse_clone_with_unusable_segments(db, experiment.targeting_rules)

        # Create experiment service
        experiment_service = ExperimentService(db)

        # Clone experiment
        cloned_experiment = experiment_service.clone_experiment(
            experiment, current_user.id
        )

        cloned_id = (
            cloned_experiment.get("id")
            if isinstance(cloned_experiment, dict)
            else getattr(cloned_experiment, "id", None)
        )
        record_experiment_change(
            db,
            current_user,
            ActionType.EXPERIMENT_CREATE,
            cloned_id,
            reason=f"clone of {experiment_id}",
        )

        await _invalidate_experiment_cache(cache_control, None)

        return ExperimentResponse.model_validate(cloned_experiment)
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment clone",
            "Could not clone the experiment",
            db=db,
            logger=logger,
        )


@router.get(
    "/{experiment_id}/daily-results",
    response_model=List[Dict[str, Any]],
    summary="Get daily experiment results",
    response_description="Returns the experiment results by day",
)
async def get_daily_experiment_results(
    experiment_id: UUID = Path(
        ..., description="The ID of the experiment to get results for"
    ),
    metric_id: Optional[UUID] = Query(
        None, description="Optional ID of specific metric to analyze"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> List[Dict[str, Any]]:
    """
    Get experiment results broken down by day.

    This endpoint retrieves the analytical results of an experiment by day, including:
    - Daily conversion rates for each variant
    - Daily sample sizes
    - Trend analysis over time

    This is useful for visualizing how the experiment is performing over time
    and identifying any temporal patterns or anomalies.

    Returns:
        List[Dict[str, Any]]: List of daily result objects

    Raises:
        HTTPException 400: If the experiment has no data or is in DRAFT status
        HTTPException 403: If the caller's role does not hold READ on experiments
    """
    try:
        # Get experiment
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # Before the cache read: the role must hold READ on experiments
        experiment = deps.get_experiment_read_access(experiment, current_user)

        # Check if experiment has results
        if experiment.status == ExperimentStatus.DRAFT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot get results for experiments in DRAFT status",
            )

        metric_part = f":{metric_id}" if metric_id else ""
        cache_key = f"experiment_daily_results:{experiment_id}{metric_part}"
        cached_data = await _cache_get(cache_control, cache_key)
        if cached_data:
            return json.loads(cached_data)

        # Create analysis service
        analysis_service = AnalysisService(db)

        # Get daily results
        try:
            if metric_id:
                results = analysis_service.get_daily_results(
                    experiment_id=experiment_id, metric_id=metric_id
                )
            else:
                results = analysis_service.get_daily_results(
                    experiment_id=experiment_id
                )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        await _cache_set(cache_control, cache_key, results)

        return results
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment daily results",
            "Could not load the daily results",
            db=db,
            logger=logger,
        )


@router.get(
    "/{experiment_id}/segmented-results/{segment_by}",
    response_model=Dict[str, Any],
    summary="Get segmented experiment results",
    response_description="Returns the experiment results segmented by a property",
)
async def get_segmented_experiment_results(
    experiment_id: UUID = Path(
        ..., description="The ID of the experiment to get results for"
    ),
    segment_by: str = Path(
        ..., description="Property to segment results by (e.g., 'country', 'device')"
    ),
    metric_id: Optional[UUID] = Query(
        None, description="Optional ID of specific metric to analyze"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> Dict[str, Any]:
    """
    Get experiment results segmented by a specific property.

    This endpoint retrieves the analytical results of an experiment segmented
    by a specific property such as country, device, browser, etc.

    This is useful for understanding how the experiment performs for different
    user segments and identifying any variations in the experiment's effect
    across different user populations.

    Returns:
        Dict[str, Any]: Segmented results object

    Raises:
        HTTPException 400: If the experiment has no data or is in DRAFT status
        HTTPException 403: If the caller's role does not hold READ on experiments
    """
    try:
        # Get experiment
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # Before the cache read: the role must hold READ on experiments
        experiment = deps.get_experiment_read_access(experiment, current_user)

        # Check if experiment has results
        if experiment.status == ExperimentStatus.DRAFT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot get results for experiments in DRAFT status",
            )

        metric_part = f":{metric_id}" if metric_id else ""
        cache_key = (
            f"experiment_segmented_results:{experiment_id}:{segment_by}{metric_part}"
        )
        cached_data = await _cache_get(cache_control, cache_key)
        if cached_data:
            return json.loads(cached_data)

        # Create analysis service
        analysis_service = AnalysisService(db)

        # Get segmented results
        try:
            if metric_id:
                results = analysis_service.get_segmented_results(
                    experiment_id=experiment_id,
                    segment_by=segment_by,
                    metric_id=metric_id,
                )
            else:
                results = analysis_service.get_segmented_results(
                    experiment_id=experiment_id, segment_by=segment_by
                )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        await _cache_set(cache_control, cache_key, results)

        return results
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment segmented results",
            "Could not load the segmented results",
            db=db,
            logger=logger,
        )


@router.post(
    "/{experiment_id}/metadata",
    response_model=Dict[str, Any],
    summary="Update experiment metadata",
    response_description="Returns the updated experiment with metadata",
)
async def update_experiment_metadata(
    experiment_id: UUID = Path(..., description="The ID of the experiment to update"),
    metadata: Dict[str, Any] = Body(..., description="Metadata to update"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    cache_control: Dict[str, Any] = Depends(deps.get_cache_control),
) -> Dict[str, Any]:
    """
    Update experiment metadata.

    This endpoint allows updating additional metadata associated with an experiment,
    such as notes, insights, learnings, and other custom fields.

    This metadata is separate from the core experiment configuration and can be
    updated regardless of the experiment's status.

    Returns:
        Dict[str, Any]: The updated experiment with metadata

    Raises:
        HTTPException 403: If the caller's role does not hold UPDATE on experiments
        HTTPException 404: If the experiment doesn't exist
    """
    try:
        # Get experiment
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found"
            )

        # READ and UPDATE on experiments; who created it is not considered
        experiment = deps.get_experiment_change_access(experiment, current_user)

        # Create experiment service
        experiment_service = ExperimentService(db)

        # Merge the new keys into the stored experiment_metadata JSONB column.
        # (`experiment.metadata` would be SQLAlchemy's table MetaData, which is
        # why this endpoint used to fail with a TypeError.)
        before_audit = audit_snapshot(EntityType.EXPERIMENT, experiment)
        current_metadata = dict(experiment.experiment_metadata or {})
        current_metadata.update(metadata)
        experiment.experiment_metadata = current_metadata
        db.commit()
        db.refresh(experiment)

        record_experiment_change(
            db,
            current_user,
            ActionType.EXPERIMENT_UPDATE,
            experiment_id,
            before_audit,
            reason="metadata",
        )
        updated_experiment = experiment_service.to_response_dict(experiment)

        await _invalidate_experiment_cache(cache_control, experiment_id)

        return updated_experiment
    except HTTPException:
        # Let deliberate 4xx responses through instead of wrapping them in a 500.
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Experiment metadata update",
            "Could not update the experiment's metadata",
            db=db,
            logger=logger,
        )


@router.get(
    "/analysis/sample-size",
    response_model=Dict[str, Any],
    summary="Calculate required sample size",
    response_description="Returns the required sample size for an experiment",
)
async def calculate_experiment_sample_size(
    baseline_rate: float = Query(
        ..., gt=0, lt=1, description="Baseline conversion rate (0-1)"
    ),
    minimum_detectable_effect: float = Query(
        ...,
        gt=0,
        description="Minimum detectable effect (relative change, e.g. 0.1 for 10%)",
    ),
    statistical_power: float = Query(
        0.8, ge=0.5, le=0.99, description="Statistical power (0.5-0.99)"
    ),
    significance_level: float = Query(
        0.05, ge=0.01, le=0.1, description="Significance level (0.01-0.1)"
    ),
    is_one_sided: bool = Query(
        False, description="Whether the test is one-sided (default: two-sided)"
    ),
    daily_traffic: Optional[int] = Query(
        None, gt=0, description="Daily traffic to the experiment (optional)"
    ),
    traffic_allocation: Optional[float] = Query(
        None,
        gt=0,
        le=1,
        description="Fraction of traffic allocated to the experiment (0-1)",
    ),
    variant_count: int = Query(
        2, ge=2, description="Number of variants (including control)"
    ),
    current_user: User = Depends(deps.get_current_active_user),
) -> Dict[str, Any]:
    """
    Calculate the required sample size for an experiment.

    This endpoint calculates the number of samples needed per variant and in total
    to achieve the desired statistical power for detecting the minimum effect size.

    Optionally, if daily traffic and traffic allocation are provided, it also
    estimates how long the experiment will need to run to collect the required sample.

    Returns:
        Dict[str, Any]: Sample size calculation results
    """
    import math

    # The treatment rate has to be a probability. Past 100% the formula takes
    # the square root of a negative variance.
    treatment_rate = baseline_rate + baseline_rate * minimum_detectable_effect
    if baseline_rate * (1 + minimum_detectable_effect) >= 1 or treatment_rate >= 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=TREATMENT_RATE_CEILING_MESSAGE,
        )

    # Two arms through the same formula as the results page's Sample Size tab
    # (pooled variance under H0, unpooled under H1), with no multiple-comparison
    # correction; every variant needs this many users.
    try:
        samples_per_variant = sample_size_two_proportions(
            p1=baseline_rate,
            p2=treatment_rate,
            alpha=significance_level,
            power=statistical_power,
            two_tailed=not is_one_sided,
        )
    except SampleSizeNotFiniteError:
        # The treatment rate is too close to the baseline for the size to be a
        # finite number (an effect or a baseline near zero).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=SAMPLE_SIZE_NOT_FINITE_MESSAGE,
        )

    # Calculate total sample size
    total_samples = samples_per_variant * variant_count

    # Calculate estimated duration if daily traffic and allocation are provided
    estimated_duration_days = None
    notes = None

    if daily_traffic is not None and traffic_allocation is not None:
        if traffic_allocation <= 0 or traffic_allocation > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Traffic allocation must be between 0 and 1",
            )

        # Calculate daily experiment traffic
        daily_experiment_traffic = daily_traffic * traffic_allocation

        # Calculate daily traffic per variant
        daily_variant_traffic = daily_experiment_traffic / variant_count

        # Calculate days needed
        days_needed = math.ceil(samples_per_variant / daily_variant_traffic)

        # Add estimates for different time periods
        estimated_duration_days = {
            "days": days_needed,
            "weeks": math.ceil(days_needed / 7),
            "months": math.ceil(days_needed / 30),
        }

        # Add notes for very long or very short experiments
        if days_needed < 7:
            notes = "This experiment is very short. Consider implementing proper guardrails to avoid data quality issues."
        elif days_needed > 90:
            notes = "This experiment will take a long time to complete. Consider increasing traffic allocation, reducing the number of variants, or adjusting the minimum detectable effect."

    # Return response
    return {
        "baseline_rate": baseline_rate,
        "minimum_detectable_effect": minimum_detectable_effect,
        "statistical_power": statistical_power,
        "significance_level": significance_level,
        "is_one_sided": is_one_sided,
        "samples_per_variant": samples_per_variant,
        "total_samples": total_samples,
        "estimated_duration_days": estimated_duration_days,
        "notes": notes,
    }


@router.post(
    "/schedules/process",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Manually trigger experiment schedule processing",
    description="Admin-only endpoint to manually process scheduled experiments",
    response_description="Processing scheduled experiments",
)
async def trigger_schedule_processing(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(deps.get_current_active_user),
) -> Dict[str, str]:
    """
    Manually trigger the scheduler to process experiments with scheduled dates.

    This is an admin-only endpoint that runs the scheduler process to:
    1. Activate experiments that have reached their start_date
    2. Complete experiments that have reached their end_date

    The processing happens in a background task to avoid blocking the response.

    Returns:
        A status message

    Raises:
        HTTPException 403: If user is not an admin
    """
    # Only allow admin users to trigger this
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can trigger scheduled processing",
        )

    # Process the scheduled experiments in the background
    background_tasks.add_task(experiment_scheduler.process_scheduled_experiments)

    return {"status": "Processing scheduled experiments in the background"}


@router.get(
    "/{experiment_id}/split-url/preview",
    summary="Preview split URL assignment for a user",
    response_description="Returns the predicted URL variant for the given user",
    responses={
        status.HTTP_501_NOT_IMPLEMENTED: {
            "description": "The split_url module is not installed in this deployment",
        },
    },
)
async def preview_split_url_assignment(
    experiment_id: UUID = Path(..., description="The ID of the split URL experiment"),
    user_id: str = Query(..., description="User ID to simulate assignment for"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Dict[str, Any]:
    """
    Preview the split URL variant assignment for a given user.

    The route is declared here so the URL exists in every profile; the body
    lives in the split_url module (``experiments_split_url``) and is reached
    through the ``core.optional_modules`` seam. Builds without split-URL
    routing answer HTTP 501 rather than a meaningless assignment.
    """
    # The route's own access policy comes before the seam is consulted, so a
    # VIEWER is told no in every profile rather than learning from a 501
    # which bodies this build has (the same order the compliance routes use).
    if not (
        getattr(current_user, "is_superuser", False)
        or getattr(current_user, "role", None) in (UserRole.ADMIN, UserRole.DEVELOPER)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only DEVELOPER or ADMIN users can access the split URL preview endpoint",
        )
    handler = split_url_preview_handler()
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=SPLIT_URL_UNAVAILABLE_DETAIL,
        )
    return await handler(
        experiment_id=experiment_id,
        user_id=user_id,
        db=db,
        current_user=current_user,
    )
