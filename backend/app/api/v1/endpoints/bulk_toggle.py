"""
Bulk feature flag toggle endpoints (P1-B).

POST /api/v1/feature-flags/bulk-toggle  — enable/disable/archive multiple flags
GET  /api/v1/feature-flags/{id}/history — change history for a single flag
GET  /api/v1/audit-logs/stream          — SSE stream of recent audit events (last 100)
"""

import asyncio
import json
import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.api.query_params import MAX_OFFSET, describe_as_int64
from backend.app.core.logger import failure_detail, unexpected_failure
from backend.app.core.permissions import (
    Action,
    can_act_on_feature_flag,
    can_read_all_audit_logs,
)
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.feature_flag import (
    ARCHIVED_FLAG_DETAIL,
    ArchivedFlagError,
    FeatureFlag,
    flag_status_name,
)
from backend.app.models.user import User
from backend.app.schemas.advanced_toggle import (
    BulkToggleRequest,
    BulkToggleResponse,
    BulkToggleResult,
    DetailedAuditLogResponse,
    FlagChangeHistoryResponse,
)
from backend.app.services.audit_service import AuditActor, AuditService
from backend.app.services.feature_flag_service import FlagVerb, transition
from backend.app.services.flag_environment_service import (
    turn_off_environment_settings,
)

logger = logging.getLogger(__name__)

#: Each bulk action, as the status verb it asks for and the audit action.
_ACTIONS = {
    "enable": (FlagVerb.ON, ActionType.TOGGLE_ENABLE),
    "disable": (FlagVerb.OFF, ActionType.TOGGLE_DISABLE),
    "archive": (FlagVerb.ARCHIVE, ActionType.FEATURE_FLAG_UPDATE),
}

router = APIRouter(route_class=OffLoopRoute)


def _recorded(old_status: str, new_status: str, turned_off: List[str]):
    """``(before, after)`` of a flag's entry: the two statuses, and the
    environments an archive turned the flag off in, when there is one."""
    if not turned_off:
        return old_status, new_status
    return tuple(
        {
            "status": status,
            "environments": {name: {"is_active": on} for name in turned_off},
        }
        for status, on in ((old_status, True), (new_status, False))
    )


@router.post(
    "/feature-flags/bulk-toggle",
    response_model=BulkToggleResponse,
    summary="Bulk enable/disable/archive feature flags",
)
async def bulk_toggle_flags(
    request: BulkToggleRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Toggle multiple feature flags in a single operation.
    Each flag's result is reported individually.
    Requires UPDATE permission on feature_flag resource.
    Creates one audit log entry per successfully-processed flag.
    The operation is partial-success by design: if some flags fail,
    the endpoint still returns 200 with per-flag results.
    The changes and their audit entries are saved together: if they cannot
    be saved, no flag is changed and the endpoint returns 500.

    An archived flag is never turned on: with ``enable`` it is reported with
    ``success: false`` and left archived, and the other flags are processed.
    With ``disable`` it succeeds and stays archived.
    """
    results = []
    audit_log_ids = []
    # (flag id, name, action, before, after) of each flag changed, to be
    # recorded in the transaction that commits the changes.
    changed = []
    # Read now: after a flag's write fails, the session cannot load it.
    actor = AuditActor.of(current_user)

    for flag_id_str in request.flag_ids:
        try:
            flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id_str).first()
            if not flag:
                results.append(
                    BulkToggleResult(
                        flag_id=flag_id_str,
                        flag_key="unknown",
                        success=False,
                        error="Feature flag not found",
                    )
                )
                continue

            # Before any change to the row: a refused flag must be left exactly
            # as it was, because the final commit below covers every flag.
            # Archive is a status change, so all three actions need UPDATE.
            if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
                results.append(
                    BulkToggleResult(
                        flag_id=str(flag.id),
                        flag_key=flag.key,
                        success=False,
                        error="Not enough permissions to change this feature flag",
                    )
                )
                continue

            old_status = flag_status_name(flag.status)

            if request.action not in _ACTIONS:
                # Should not happen due to Pydantic validation, but guard anyway
                results.append(
                    BulkToggleResult(
                        flag_id=flag_id_str,
                        flag_key=flag.key,
                        success=False,
                        error=f"Unknown action: {request.action}",
                    )
                )
                continue
            verb, action_type = _ACTIONS[request.action]

            # Refused here, before the row is touched (#631): the model's own
            # guard would raise on the assignment instead, and the final commit
            # below covers every flag.
            try:
                new_status = transition(old_status, verb).value
            except ArchivedFlagError:
                results.append(
                    BulkToggleResult(
                        flag_id=str(flag.id),
                        flag_key=flag.key,
                        success=False,
                        error=ARCHIVED_FLAG_DETAIL,
                    )
                )
                continue

            if new_status != old_status:
                flag.status = new_status
            db.flush()
            # Archiving turns the flag off in staging and development too
            # (#2011), in the same commit; unarchiving leaves them off.
            turned_off = (
                turn_off_environment_settings(db, flag.id)
                if verb is FlagVerb.ARCHIVE
                else []
            )
            before, after = _recorded(old_status, new_status, turned_off)
            changed.append((flag.id, flag.name, action_type, before, after))

            results.append(
                BulkToggleResult(
                    flag_id=str(flag.id),
                    flag_key=flag.key,
                    success=True,
                    old_status=old_status,
                    new_status=new_status,
                )
            )

        except Exception as e:
            # Only the exception type goes to the log, under this request's
            # ID; the per-flag result carries a fixed sentence with that ID.
            logger.error(
                "Bulk toggle of flag %s failed (%s)",
                flag_id_str,
                type(e).__name__,
            )
            results.append(
                BulkToggleResult(
                    flag_id=flag_id_str,
                    flag_key="error",
                    success=False,
                    error=failure_detail("Could not change this flag"),
                )
            )

    # One entry per flag changed, in the same transaction as the changes: the
    # commit keeps every change and every entry, or nothing (#1042). Any
    # failure here, of an entry or of the commit, answers 500 with nothing
    # saved, never a per-flag success that was not kept.
    try:
        for flag_id, flag_name, action_type, before, after in changed:
            with AuditService.audit_with_change(
                db,
                action=action_type,
                entity_type=EntityType.FEATURE_FLAG,
                entity_id=flag_id,
            ):
                entry = AuditService.record(
                    db,
                    actor=actor,
                    action=action_type,
                    entity_type=EntityType.FEATURE_FLAG,
                    entity_id=flag_id,
                    entity_name=flag_name,
                    before=before,
                    after=after,
                    reason=request.reason,
                )
            audit_log_ids.append(str(entry.id))
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise unexpected_failure(
            e,
            "Bulk toggle",
            "Could not change the feature flags",
            db=db,
            logger=logger,
        )

    succeeded = sum(1 for r in results if r.success)
    return BulkToggleResponse(
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        results=results,
        audit_log_ids=audit_log_ids,
    )


@router.get(
    "/feature-flags/{flag_id}/history",
    response_model=FlagChangeHistoryResponse,
    summary="Get change history for a feature flag",
)
def get_flag_history(
    flag_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=MAX_OFFSET, json_schema_extra=describe_as_int64),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Returns all audit log entries for a specific feature flag.
    Ordered by timestamp descending (most recent first).
    Superusers, ADMIN and ANALYST, and in the full profile anyone whose custom
    role or direct grant gives audit_log (read), see other users' entries too.
    Anyone else sees only the entries they made, and `total_changes` counts
    only those.
    """
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(status_code=404, detail="Feature flag not found")

    logs, total = AuditService.get_flag_change_history(
        db,
        flag_id,
        limit,
        offset,
        user_id=None if can_read_all_audit_logs(current_user) else current_user.id,
    )

    return FlagChangeHistoryResponse(
        flag_id=str(flag.id),
        flag_key=flag.key,
        flag_name=flag.name,
        total_changes=total,
        history=[
            DetailedAuditLogResponse(
                id=str(log.id),
                timestamp=log.timestamp,
                user_id=str(log.user_id) if log.user_id else None,
                user_email=log.user_email,
                action_type=log.action_type,
                entity_type=log.entity_type,
                entity_id=str(log.entity_id),
                entity_name=log.entity_name,
                old_value=log.old_value,
                new_value=log.new_value,
                reason=log.reason,
            )
            for log in logs
        ],
    )


@router.get(
    "/audit-logs/stream",
    summary="SSE stream of recent audit log events",
)
async def stream_audit_logs(
    limit: int = Query(50, ge=1, le=100),
    entity_type: Optional[str] = Query(None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Returns recent audit log entries as Server-Sent Events.
    Useful for real-time audit dashboards.
    Streams up to `limit` entries (default 50, max 100) then closes.
    Superusers, ADMIN and ANALYST, and in the full profile anyone whose custom
    role or direct grant gives audit_log (read), receive other users' entries
    too. Anyone else receives only their own.
    """
    # Resolved in the handler, before the response starts, not inside the
    # generator.
    own_only = None if can_read_all_audit_logs(current_user) else current_user.id

    async def event_generator():
        from backend.app.models.audit_log import EntityType as EntityTypeEnum

        entity_type_enum = None
        if entity_type:
            try:
                entity_type_enum = EntityTypeEnum(entity_type)
            except ValueError:
                pass  # Ignore invalid entity_type silently in SSE stream

        # get_audit_logs uses page (1-based) rather than offset
        logs, _ = AuditService.get_audit_logs(
            db,
            user_id=own_only,
            limit=limit,
            entity_type=entity_type_enum,
            page=1,
        )
        for i, log in enumerate(logs):
            event_data = json.dumps(
                {
                    "id": str(log.id),
                    "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                    "user_email": log.user_email,
                    "action_type": log.action_type,
                    "entity_type": log.entity_type,
                    "entity_name": log.entity_name,
                    "old_value": log.old_value,
                    "new_value": log.new_value,
                }
            )
            yield f"data: {event_data}\n\n"
            await asyncio.sleep(0.01)
        yield 'data: {"event": "end"}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
