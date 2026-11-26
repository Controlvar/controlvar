"""
A feature flag's settings in each environment (#2011, beta).

Two routers (``backend/app/api/api.py``):

* ``router``, under ``/feature-flags``: one flag's settings in every
  environment (``GET /{flag_id}/environments``) or in one
  (``GET`` and ``PUT /{flag_id}/environments/{environment}``), and
  ``POST /{flag_id}/environments/{target}/promote``, which copies one
  environment's settings into another;
* ``environments_router``, under ``/environments``: the list of environments,
  and ``GET /{environment}/feature-flag-settings``, many flags in one of them.

Production's settings are the flag row, so a change to production here goes
through ``FeatureFlagService.update_feature_flag``, as ``PUT
/feature-flags/{id}`` does, and records what it records. A change to staging
or development writes its side row (``flag_environment_service``) and never
the flag row. Either way the change and its audit entries are saved together
or not at all (#1042), and the flag row is locked first, so two changes of one
flag wait for each other.

Reading needs what reading a flag needs; changing and promoting need UPDATE
on the flag.
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.api.segment_rules import refuse_unusable_segments
from backend.app.api.v1.endpoints.feature_flags import flag_update_record
from backend.app.core.flag_environments import (
    DEFAULT,
    DISPLAY_NAMES,
    FLAG_ENVIRONMENTS,
    PRODUCTION,
    FlagEnvironmentName,
)
from backend.app.core.flag_values import SERVING_FIELDS, flag_value_type, serving_errors
from backend.app.core.logger import unexpected_failure
from backend.app.core.permissions import (
    Action,
    ResourceType,
    can_act_on_feature_flag,
    check_permission,
)
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.feature_flag import (
    ARCHIVED_FLAG_DETAIL,
    ArchivedFlagError,
    FeatureFlag,
    FlagEnvironmentSetting,
    flag_status_name,
)
from backend.app.models.user import User
from backend.app.schemas.feature_flag import FeatureFlagUpdate
from backend.app.schemas.flag_environment import (
    EnvironmentFlagSettings,
    FlagEnvironmentRead,
    FlagEnvironments,
    FlagEnvironmentSettings,
    FlagEnvironmentUpdate,
    FlagPromotion,
)
from backend.app.services.audit_service import AuditService, audit_changes
from backend.app.services.feature_flag_service import KEEP_SPLIT, FeatureFlagService
from backend.app.services.flag_environment_service import (
    FlagEnvironmentService,
    audit_setting,
    effective_settings,
    lock_flag,
    setting_of,
    settings_of,
)

logger = logging.getLogger(__name__)

#: Most flags one ``feature-flag-settings`` request may name.
MAX_FLAGS = 100

#: The 422 for a promotion into the environment it would copy from.
SAME_ENVIRONMENT = "from must name another environment than the one promoted into"

_BETA = {"x-stability": "beta"}
_SIGN_IN = {401: {"description": "Authentication failed"}}
_READ = {
    **_SIGN_IN,
    403: {"description": "Not enough permissions to access this feature flag"},
    404: {"description": "No feature flag has this id"},
}
_PROMOTE = {
    **_SIGN_IN,
    403: {"description": "Not enough permissions to update this feature flag"},
    404: {"description": "No feature flag has this id"},
    500: {
        "description": (
            "The change or its audit entry could not be saved; nothing was changed"
        )
    },
}
_CHANGE = {
    **_PROMOTE,
    400: {"description": "The flag is archived: unarchive it before turning it on"},
}

router = APIRouter(route_class=OffLoopRoute)
environments_router = APIRouter(route_class=OffLoopRoute)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
    )


def _may_not_update() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not enough permissions to update this feature flag",
    )


def _readable_flag(db: Session, flag_id: UUID, current_user: User) -> FeatureFlag:
    """The flag, or the refusal ``GET /feature-flags/{id}`` answers."""
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if flag is None:
        raise _not_found()
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to access this feature flag",
        )
    return flag


def _as_requested(changes: Dict[str, Any]) -> Dict[str, Any]:
    """*changes* but the split, which production's update takes as
    ``update_feature_flag(split=...)`` and :func:`_refuse_what_cannot_be_served`
    checks (#2012)."""
    return {name: value for name, value in changes.items() if name != "split"}


def _rechecked(changes: Dict[str, Any]) -> None:
    """Refuse settings a PUT would refuse, with the PUT's own 422."""
    try:
        FlagEnvironmentUpdate(**_as_requested(changes))
    except ValidationError as refused:
        raise RequestValidationError(
            [
                {**error, "loc": ("body", *error["loc"])}
                for error in refused.errors(include_url=False)
            ]
        ) from None


def _refuse_what_cannot_be_served(
    flag: FeatureFlag, environment: str, changes: Dict[str, Any], db: Session
) -> None:
    """Refuse a split or rules' own values the flag cannot serve in
    *environment*, with the 422 a flag update gives
    (``flag_values.serving_errors``, against the flag's type and what the
    environment stores now). *flag* is locked."""
    sent = {name: changes[name] for name in SERVING_FIELDS if name in changes}
    if not sent:
        return
    stored = effective_settings(flag, setting_of(db, flag.id, environment), environment)
    errors = serving_errors(flag_value_type(flag), sent, stored)
    if errors:
        raise _serving_refused(errors)


def _serving_refused(errors: List[Any]) -> RequestValidationError:
    return RequestValidationError(
        [
            {"type": error.type, "loc": ("body", *where), "msg": error.message()}
            for where, error in errors
        ]
    )


# ---------------------------------------------------------------------------
# The one write sequence: lock, write, audit, commit
# ---------------------------------------------------------------------------


def _save(
    db: Session,
    current_user: User,
    flag: FeatureFlag,
    environment: str,
    changes: Dict[str, Any],
    promoted_from: Optional[str] = None,
) -> FlagEnvironmentSettings:
    """Store *changes* for *flag* in *environment* and record them.

    *flag* is the row :func:`lock_flag` returned, locked until the commit.
    The change and its audit entries are committed together: anything that
    fails before that saves neither and answers 500.

    * Staging and development: one ``feature_flag_environment_update`` entry,
      or one ``feature_flag_promote``.
    * Production: what ``PUT /feature-flags/{id}`` records, and for a
      promotion one ``feature_flag_promote`` entry as well.
    """
    promoting = promoted_from is not None
    where = {"environment": environment}
    if promoting:
        where.update({"from": promoted_from, "to": environment})
    setting = setting_of(db, flag.id, environment)
    before = audit_setting(flag, setting, environment)
    if promoting:
        action = ActionType.FEATURE_FLAG_PROMOTE
    else:
        action = ActionType.FEATURE_FLAG_ENVIRONMENT_UPDATE

    def record_change(changed: Optional[FlagEnvironmentSetting]) -> None:
        # In the change's own transaction (#1042), from the row as changed.
        with AuditService.audit_with_change(
            db,
            action=action,
            entity_type=EntityType.FEATURE_FLAG,
            entity_id=flag.id,
        ):
            old, new = audit_changes(before, audit_setting(flag, changed, environment))
            AuditService.record(
                db,
                actor=current_user,
                action=action,
                entity_type=EntityType.FEATURE_FLAG,
                entity_id=flag.id,
                entity_name=flag.name,
                before={**where, **(old or {})},
                after={**where, **(new or {})},
            )

    try:
        if environment != PRODUCTION:
            changed = FlagEnvironmentService(db).save_environment_setting(
                flag, environment, changes, record=record_change
            )
            return FlagEnvironmentSettings.of(flag, changed, environment)

        record_update = flag_update_record(db, flag, current_user)

        def record(updated: FeatureFlag) -> None:
            record_update(updated)
            if promoting:
                record_change(None)

        updated_row = FeatureFlagService(db).update_feature_flag(
            flag,
            FeatureFlagUpdate(**_as_requested(changes)),
            record=record,
            split=changes.get("split", KEEP_SPLIT),
        )
        return FlagEnvironmentSettings.of(updated_row, None, PRODUCTION)
    except ArchivedFlagError:
        raise _archived_refusal() from None
    except Exception as e:
        raise unexpected_failure(
            e,
            "Feature flag promote" if promoting else "Feature flag environment update",
            "Could not promote the feature flag"
            if promoting
            else "Could not update the feature flag",
            db=db,
            logger=logger,
        )


def _archived_refusal() -> HTTPException:
    """The 400 for a change that would turn an archived flag on (#631)."""
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=ARCHIVED_FLAG_DETAIL
    )


def _refuse_to_turn_an_archived_flag_on(
    flag: FeatureFlag, changes: Dict[str, Any]
) -> None:
    if changes.get("is_active") and flag_status_name(flag.status) == "ARCHIVED":
        raise _archived_refusal()


# ---------------------------------------------------------------------------
# One flag
# ---------------------------------------------------------------------------


@router.get(
    "/{flag_id}/environments",
    response_model=FlagEnvironments,
    summary="Get a feature flag's settings in every environment (beta)",
    response_description="The flag's settings in production, staging and development",
    openapi_extra=_BETA,
    responses=_READ,
)
def get_flag_environments(
    flag_id: UUID = Path(..., description="The ID of the feature flag"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> FlagEnvironments:
    """
    Get a feature flag's settings in every environment (beta).

    Production's are the flag's own. In staging and development a flag nobody
    has configured is off: `configured` is false, `is_active` is false, the
    percentage is 0 and there are no rules.

    Raises:
        HTTPException 404: No feature flag has this id
    """
    flag = _readable_flag(db, flag_id, current_user)
    rows = settings_of(db, [flag_id])
    return FlagEnvironments(
        flag_id=flag.id,
        key=flag.key,
        environments=[
            FlagEnvironmentSettings.of(flag, rows.get((flag.id, name)), name)
            for name in FLAG_ENVIRONMENTS
        ],
    )


@router.get(
    "/{flag_id}/environments/{environment}",
    response_model=FlagEnvironmentSettings,
    summary="Get a feature flag's settings in one environment (beta)",
    response_description="The flag's settings in the environment",
    openapi_extra=_BETA,
    responses=_READ,
)
def get_flag_environment(
    flag_id: UUID = Path(..., description="The ID of the feature flag"),
    environment: FlagEnvironmentName = Path(..., description="The environment"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> FlagEnvironmentSettings:
    """
    Get a feature flag's settings in one environment (beta).

    Raises:
        HTTPException 404: No feature flag has this id
        HTTPException 422: The environment is not `production`, `staging` or
            `development`
    """
    flag = _readable_flag(db, flag_id, current_user)
    return FlagEnvironmentSettings.of(
        flag, setting_of(db, flag.id, environment), environment
    )


@router.put(
    "/{flag_id}/environments/{environment}",
    response_model=FlagEnvironmentSettings,
    summary="Change a feature flag's settings in one environment (beta)",
    response_description="The flag's settings in the environment, as changed",
    openapi_extra=_BETA,
    responses=_CHANGE,
)
def update_flag_environment(
    flag_id: UUID = Path(..., description="The ID of the feature flag"),
    environment: FlagEnvironmentName = Path(
        ..., description="The environment to change; the others are not"
    ),
    settings_in: FlagEnvironmentUpdate = Body(
        ..., description="The settings to change"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> FlagEnvironmentSettings:
    """
    Change a feature flag's settings in one environment (beta).

    Only the fields sent change, and only in this environment: `is_active`,
    `rollout_percentage` (0 to 100), `targeting_rules` and `split`, which null
    clears. An empty body changes nothing. An unknown field answers 422, and
    so does an explicit null on `is_active` or `rollout_percentage`.

    `targeting_rules` the flag evaluator would not apply as written, or that
    name a segment that cannot be used, answer 422, as on a flag update; so do
    a `split` and rules' own values the flag cannot serve, checked against its
    value type exactly as on a flag update. An archived flag sent
    `is_active: true` answers 400, in every environment.

    The change to production is the same as `PUT /feature-flags/{id}`.

    The change and its audit entry are saved together: if the entry cannot be
    written, nothing is changed and the request answers 500.

    **Permissions**: ADMIN and DEVELOPER may change any flag, and in the full
    profile so may anyone whose custom role or direct grant gives
    feature_flag (update); anyone else gets 403.

    Raises:
        HTTPException 400: `is_active: true` on an archived flag
        HTTPException 403: If the user doesn't have permission to update this feature flag
        HTTPException 404: No feature flag has this id
        HTTPException 422: The environment or the body is not valid
        HTTPException 500: Nothing was changed
    """
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if flag is None:
        raise _not_found()
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise _may_not_update()

    changes = settings_in.model_dump(exclude_unset=True)
    if not changes:
        return FlagEnvironmentSettings.of(
            flag, setting_of(db, flag.id, environment), environment
        )
    # Every segment the rules name exists and is active (#440).
    if "targeting_rules" in changes:
        refuse_unusable_segments(db, changes["targeting_rules"])

    flag = lock_flag(db, flag_id)
    if flag is None:
        raise _not_found()
    _refuse_to_turn_an_archived_flag_on(flag, changes)
    _refuse_what_cannot_be_served(flag, environment, changes, db)
    return _save(db, current_user, flag, environment, changes)


@router.post(
    "/{flag_id}/environments/{target}/promote",
    response_model=FlagEnvironmentSettings,
    summary="Promote a feature flag's settings into an environment (beta)",
    response_description="The flag's settings in the target environment, as copied",
    openapi_extra=_BETA,
    responses=_PROMOTE,
)
def promote_flag_environment(
    flag_id: UUID = Path(..., description="The ID of the feature flag"),
    target: FlagEnvironmentName = Path(
        ..., description="The environment the settings are copied into"
    ),
    promotion: FlagPromotion = Body(
        ..., description="The environment the settings are copied from"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> FlagEnvironmentSettings:
    """
    Promote a feature flag's settings into an environment (beta).

    Copies `is_active`, `rollout_percentage`, `targeting_rules` and `split`
    from the environment `from` names into the target, all four together, as
    a change to the target would: the rules and the split are checked again,
    and the same permission is needed. A flag nobody configured in `from` is
    off there, so promoting from it turns the target off, and an archived flag
    is off in every environment. Nothing else is copied: not the flag's value
    type, on value and off value, which belong to the flag, its rollout
    schedules or its safety configuration.

    The change and its audit entries are saved together: if an entry cannot be
    written, nothing is changed and the request answers 500.

    **Permissions**: ADMIN and DEVELOPER may change any flag, and in the full
    profile so may anyone whose custom role or direct grant gives
    feature_flag (update); anyone else gets 403.

    Raises:
        HTTPException 403: If the user doesn't have permission to update this feature flag
        HTTPException 404: No feature flag has this id
        HTTPException 422: `from` is the target, an environment is not valid,
            or the rules or the split would be refused in the target
        HTTPException 500: Nothing was changed
    """
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if flag is None:
        raise _not_found()
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise _may_not_update()
    if promotion.source == target:
        raise RequestValidationError(
            [
                {
                    "type": "same_environment",
                    "loc": ("body", "from"),
                    "msg": SAME_ENVIRONMENT,
                }
            ]
        )

    # Locked before the source is read, so what is copied is what is stored.
    flag = lock_flag(db, flag_id)
    if flag is None:
        raise _not_found()
    source = promotion.source
    changes = effective_settings(flag, setting_of(db, flag.id, source), source)
    _rechecked(changes)
    _refuse_what_cannot_be_served(flag, target, changes, db)
    refuse_unusable_segments(db, changes["targeting_rules"])
    return _save(db, current_user, flag, target, changes, promoted_from=source)


# ---------------------------------------------------------------------------
# The environments
# ---------------------------------------------------------------------------


@environments_router.get(
    "",
    response_model=List[FlagEnvironmentRead],
    summary="List the flag environments (beta)",
    response_description="The three environments, the default first",
    openapi_extra=_BETA,
    responses=_SIGN_IN,
)
def list_environments(
    current_user: User = Depends(deps.get_current_active_user),
) -> List[FlagEnvironmentRead]:
    """
    List the flag environments (beta).

    The set is fixed: production, staging and development. Production is the
    default, where a flag's own settings are.
    """
    return [
        FlagEnvironmentRead(key=key, name=DISPLAY_NAMES[key], default=key == DEFAULT)
        for key in FLAG_ENVIRONMENTS
    ]


@environments_router.get(
    "/{environment}/feature-flag-settings",
    response_model=EnvironmentFlagSettings,
    summary="Get several feature flags' settings in one environment (beta)",
    response_description="Each flag's settings in the environment, by flag id",
    openapi_extra=_BETA,
    responses={
        **_SIGN_IN,
        403: {"description": "Not enough permissions to list feature flags"},
    },
)
def get_environment_flag_settings(
    environment: FlagEnvironmentName = Path(..., description="The environment"),
    flag_id: List[UUID] = Query(
        ...,
        min_length=1,
        max_length=MAX_FLAGS,
        description=f"The ID of a feature flag; 1 to {MAX_FLAGS} times.",
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> EnvironmentFlagSettings:
    """
    Get several feature flags' settings in one environment (beta).

    One entry per flag that exists; a flag nobody has configured in staging or
    development answers `configured: false` and off.

    Raises:
        HTTPException 403: If the user may not list feature flags
        HTTPException 422: `flag_id` is missing, not an id, or given more than
            100 times, or the environment is not valid
    """
    if not check_permission(current_user, ResourceType.FEATURE_FLAG, Action.LIST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to list feature flags",
        )
    flags = {
        flag.id: flag
        for flag in db.query(FeatureFlag).filter(FeatureFlag.id.in_(set(flag_id)))
    }
    rows = (
        {} if environment == PRODUCTION else settings_of(db, flags.keys(), environment)
    )
    return EnvironmentFlagSettings(
        environment=environment,
        settings={
            str(one): FlagEnvironmentSettings.of(
                flags[one], rows.get((one, environment)), environment
            )
            for one in dict.fromkeys(flag_id)
            if one in flags
        },
    )
