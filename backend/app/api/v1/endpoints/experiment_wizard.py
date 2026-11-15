"""
Experiment Wizard API endpoints.

Provides a step-by-step guided interface for non-technical users to design
and launch experiments without engineering involvement.

Every operation here is deprecated: the dashboard does not use them. Drafts
are per user -- a draft that belongs to someone else is answered with the
same 404 as one that does not exist.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.permissions import (
    Action,
    ResourceType,
    check_permission,
    get_permission_error_message,
)
from backend.app.models.audit_log import ActionType
from backend.app.models.user import User
from backend.app.schemas.experiment_wizard import (
    WizardDraftCreate,
    WizardDraftListResponse,
    WizardDraftResponse,
    WizardStepUpdate,
    WizardSubmitResponse,
    WizardValidationRequest,
    WizardValidationResponse,
)
from backend.app.services.audit_service import record_experiment_change
from backend.app.services.experiment_wizard_service import (
    ExperimentWizardService,
    WizardDraft,
    WizardStepDataError,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    route_class=OffLoopRoute,
    tags=["Experiment Wizard"],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required"},
        status.HTTP_403_FORBIDDEN: {"description": "Permission denied"},
        status.HTTP_404_NOT_FOUND: {"description": "Draft not found"},
    },
)

DEPRECATION_NOTE = (
    "**Deprecated.** The dashboard does not use the wizard API; it may be "
    "removed in a later release. Drafts are per user and held in the API "
    "process's memory.\n\n"
)

DRAFT_NOT_FOUND = "Draft not found."


def _not_found() -> HTTPException:
    """The one answer for a draft that is missing or belongs to someone else."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DRAFT_NOT_FOUND)


def _draft_to_response(draft: WizardDraft) -> WizardDraftResponse:
    """Convert a WizardDraft dataclass to a WizardDraftResponse schema."""
    return WizardDraftResponse(
        id=draft.id,
        user_id=draft.user_id,
        current_step=draft.current_step,
        experiment_type=draft.experiment_type,
        hypothesis=draft.hypothesis,
        primary_metric_id=draft.primary_metric_id,
        guardrail_metric_ids=draft.guardrail_metric_ids or [],
        targeting_rules=draft.targeting_rules or [],
        baseline_rate=draft.baseline_rate,
        mde=draft.mde,
        name=draft.name,
        description=draft.description,
    )


@router.post(
    "/drafts",
    response_model=WizardDraftResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new experiment wizard draft (deprecated)",
    deprecated=True,
    description=(
        DEPRECATION_NOTE + "Initialise a new wizard draft for the authenticated user. "
        "The draft starts at the 'choose_type' step and accumulates data "
        "as the user progresses through the wizard."
    ),
)
def create_draft(
    body: WizardDraftCreate,
    current_user: User = Depends(deps.get_current_active_user),
) -> WizardDraftResponse:
    """Create a new wizard draft."""
    user_id = str(current_user.id)
    draft = ExperimentWizardService.create_draft(
        user_id=user_id,
        experiment_type=body.experiment_type,
    )
    logger.info(
        "Wizard draft created",
        extra={"draft_id": draft.id, "user_id": user_id},
    )
    return _draft_to_response(draft)


@router.get(
    "/drafts",
    response_model=WizardDraftListResponse,
    summary="List wizard drafts for the current user (deprecated)",
    deprecated=True,
    description=(
        DEPRECATION_NOTE
        + "Returns all in-progress wizard drafts belonging to the authenticated user."
    ),
)
def list_drafts(
    current_user: User = Depends(deps.get_current_active_user),
) -> WizardDraftListResponse:
    """List all drafts for the authenticated user."""
    user_id = str(current_user.id)
    drafts = ExperimentWizardService.list_drafts(user_id)
    draft_responses = [_draft_to_response(d) for d in drafts]
    return WizardDraftListResponse(drafts=draft_responses, total=len(draft_responses))


@router.get(
    "/drafts/{draft_id}",
    response_model=WizardDraftResponse,
    summary="Get a wizard draft by ID (deprecated)",
    deprecated=True,
    description=(
        DEPRECATION_NOTE
        + "Retrieve one of the caller's wizard drafts, including all accumulated "
        "step data. A draft that does not exist or belongs to another user "
        "answers 404."
    ),
)
def get_draft(
    draft_id: str,
    current_user: User = Depends(deps.get_current_active_user),
) -> WizardDraftResponse:
    """Get one of the caller's wizard drafts."""
    draft = ExperimentWizardService.get_draft(draft_id, current_user.id)
    if draft is None:
        raise _not_found()
    return _draft_to_response(draft)


@router.put(
    "/drafts/{draft_id}/step",
    response_model=WizardDraftResponse,
    summary="Update a wizard draft with step data (deprecated)",
    deprecated=True,
    description=(
        DEPRECATION_NOTE
        + "Submit data for the current wizard step and advance to the next step. "
        "The draft accumulates data across all steps. `data` may only contain "
        "step fields (`experiment_type`, `hypothesis`, `primary_metric_id`, "
        "`guardrail_metric_ids`, `targeting_rules`, `baseline_rate`, `mde`, "
        "`name`, `description`); any other key, or a step field of the wrong "
        "type, answers 422 and the draft is unchanged. A draft that does not "
        "exist or belongs to another user answers 404."
    ),
)
def update_draft_step(
    draft_id: str,
    body: WizardStepUpdate,
    current_user: User = Depends(deps.get_current_active_user),
) -> WizardDraftResponse:
    """Update one of the caller's drafts and advance to the next step."""
    try:
        draft = ExperimentWizardService.update_draft(
            draft_id=draft_id,
            user_id=current_user.id,
            step=body.step,
            data=body.data,
        )
    except WizardStepDataError as exc:
        # A fixed message: it lists the accepted fields, or names the one step
        # field of the wrong type; never a submitted key or value.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    if draft is None:
        raise _not_found()
    return _draft_to_response(draft)


@router.post(
    "/validate",
    response_model=WizardValidationResponse,
    summary="Validate wizard step data (deprecated)",
    deprecated=True,
    description=(
        DEPRECATION_NOTE
        + "Validate data for a specific wizard step without modifying any draft. "
        "Returns is_valid flag and list of validation errors."
    ),
)
def validate_step(
    body: WizardValidationRequest,
    current_user: User = Depends(deps.get_current_active_user),
) -> WizardValidationResponse:
    """Validate data for a specific wizard step."""
    result = ExperimentWizardService.validate_wizard_step(
        step=body.step,
        data=body.data,
    )
    return WizardValidationResponse(
        is_valid=result.is_valid,
        errors=result.errors,
    )


@router.post(
    "/drafts/{draft_id}/submit",
    response_model=WizardSubmitResponse,
    summary="Submit a completed wizard draft to create an experiment (deprecated)",
    deprecated=True,
    description=(
        DEPRECATION_NOTE
        + "Validate one of the caller's drafts and create the experiment it "
        "describes (status DRAFT, owned by the caller), then discard the draft. "
        "Returns the new experiment_id on success or validation errors on "
        "failure. A draft that does not exist or belongs to another user "
        "answers 404."
    ),
)
def submit_draft(
    draft_id: str,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> WizardSubmitResponse:
    """Submit a completed wizard draft and create the experiment."""
    # Submitting writes a real experiment, so it needs the same permission as
    # POST /api/v1/experiments/ — a VIEWER must not be able to create one here.
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.EXPERIMENT, Action.CREATE),
        )

    # Only the caller's own draft: someone else's is answered as missing.
    if ExperimentWizardService.get_draft(draft_id, current_user.id) is None:
        raise _not_found()

    result = ExperimentWizardService.validate_and_submit(
        draft_id, user_id=current_user.id, db=db
    )
    if result.get("success") and result.get("experiment_id"):
        record_experiment_change(
            db,
            current_user,
            ActionType.EXPERIMENT_CREATE,
            result["experiment_id"],
            reason="submitted from the wizard",
        )

    return WizardSubmitResponse(
        success=result["success"],
        experiment_id=result.get("experiment_id"),
        errors=result.get("errors", []),
    )
