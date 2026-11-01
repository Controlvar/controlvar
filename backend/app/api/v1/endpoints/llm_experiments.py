"""
LLM Experiment CRUD endpoints (EP-046).

Manages the lifecycle of LLM/AI Model experiments: create, read, update,
start, pause, and manage variants.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.enum_filters import enum_filter
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.models.llm_experiment import LLMExperimentStatus, LLMTaskType
from backend.app.models.user import User
from backend.app.schemas.llm_experiments import (
    CreateLLMExperimentRequest,
    CreateLLMVariantRequest,
    LLMExperimentListResponse,
    LLMExperimentResponse,
    LLMVariantResponse,
    UpdateLLMExperimentRequest,
    UpdateLLMVariantRequest,
)
from backend.app.services.llm_experiment_service import LLMExperimentService

router = APIRouter(tags=["LLM Experiments"])
_service = LLMExperimentService()

#: The list's filter values, as the responses spell them (#955).
LLM_STATUS_SPELLINGS = ("DRAFT", "ACTIVE", "PAUSED", "COMPLETED")
LLM_TASK_TYPE_SPELLINGS = (
    "chat_completion",
    "text_generation",
    "classification",
    "summarization",
    "code_generation",
    "embedding",
)

LLM_STATUS_FILTER_DESCRIPTION = (
    "Only experiments with this status: `DRAFT`, `ACTIVE`, `PAUSED` or "
    "`COMPLETED`, in any case. Any other value answers 422; an empty value "
    "means no filter."
)
LLM_TASK_TYPE_DESCRIPTION = (
    "Only experiments with this task type: `chat_completion`, "
    "`text_generation`, `classification`, `summarization`, `code_generation` "
    "or `embedding`, in any case. Any other value answers 422; an empty value "
    "means no filter."
)


def _to_variant_response(v) -> LLMVariantResponse:
    return LLMVariantResponse(
        id=str(v.id),
        llm_experiment_id=str(v.llm_experiment_id),
        name=v.name,
        is_control=v.is_control,
        traffic_split=v.traffic_split,
        provider=v.provider.value if hasattr(v.provider, "value") else str(v.provider),
        model_name=v.model_name,
        system_prompt=v.system_prompt or "",
        prompt_template=v.prompt_template,
        temperature=v.temperature,
        max_tokens=v.max_tokens,
        additional_params=v.additional_params or {},
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


def _to_experiment_response(exp) -> LLMExperimentResponse:
    return LLMExperimentResponse(
        id=str(exp.id),
        name=exp.name,
        description=exp.description or "",
        status=exp.status.value if hasattr(exp.status, "value") else str(exp.status),
        task_type=exp.task_type.value
        if hasattr(exp.task_type, "value")
        else str(exp.task_type),
        evaluation_metric=exp.evaluation_metric.value
        if hasattr(exp.evaluation_metric, "value")
        else str(exp.evaluation_metric),
        experiment_id=str(exp.experiment_id) if exp.experiment_id else None,
        created_by=str(exp.created_by) if exp.created_by else None,
        variants=[_to_variant_response(v) for v in (exp.variants or [])],
        created_at=exp.created_at,
        updated_at=exp.updated_at,
    )


# ---------------------------------------------------------------------------
# Experiment CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/",
    response_model=LLMExperimentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create LLM experiment",
)
def create_llm_experiment(
    data: CreateLLMExperimentRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Create a new LLM/AI model evaluation experiment.  Requires DEVELOPER+ role."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to create experiments",
        )
    try:
        experiment = _service.create_experiment(db, data, created_by=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _to_experiment_response(experiment)


@router.get(
    "/",
    response_model=LLMExperimentListResponse,
    summary="List LLM experiments",
)
def list_llm_experiments(
    status_filter: Optional[str] = Query(
        None, alias="status", description=LLM_STATUS_FILTER_DESCRIPTION
    ),
    task_type: Optional[str] = Query(None, description=LLM_TASK_TYPE_DESCRIPTION),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """List LLM experiments with optional filters."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.LIST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )
    # After the permission check (#955); the members go to the query.
    status_member = enum_filter(
        status_filter, LLMExperimentStatus, "status", LLM_STATUS_SPELLINGS
    )
    task_type_member = enum_filter(
        task_type, LLMTaskType, "task_type", LLM_TASK_TYPE_SPELLINGS
    )
    items, total = _service.list_experiments(
        db,
        status=status_member,
        task_type=task_type_member,
        page=page,
        page_size=page_size,
    )
    return LLMExperimentListResponse(
        items=[_to_experiment_response(e) for e in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{experiment_id}",
    response_model=LLMExperimentResponse,
    summary="Get LLM experiment",
)
def get_llm_experiment(
    experiment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Retrieve a single LLM experiment with all its variants."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    experiment = _service.get_experiment(db, experiment_id)
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="LLM experiment not found"
        )
    return _to_experiment_response(experiment)


@router.put(
    "/{experiment_id}",
    response_model=LLMExperimentResponse,
    summary="Update LLM experiment",
)
def update_llm_experiment(
    experiment_id: UUID,
    data: UpdateLLMExperimentRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Update an LLM experiment's metadata."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    try:
        experiment = _service.update_experiment(db, experiment_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="LLM experiment not found"
        )
    return _to_experiment_response(experiment)


@router.post(
    "/{experiment_id}/start",
    response_model=LLMExperimentResponse,
    summary="Start LLM experiment",
)
def start_llm_experiment(
    experiment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Transition a DRAFT or PAUSED LLM experiment to ACTIVE."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    try:
        experiment = _service.start_experiment(db, experiment_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="LLM experiment not found"
        )
    return _to_experiment_response(experiment)


@router.post(
    "/{experiment_id}/pause",
    response_model=LLMExperimentResponse,
    summary="Pause LLM experiment",
)
def pause_llm_experiment(
    experiment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Transition an ACTIVE LLM experiment to PAUSED."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    try:
        experiment = _service.pause_experiment(db, experiment_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="LLM experiment not found"
        )
    return _to_experiment_response(experiment)


# ---------------------------------------------------------------------------
# Variant management
# ---------------------------------------------------------------------------


@router.post(
    "/{experiment_id}/variants",
    response_model=LLMVariantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add variant to LLM experiment",
)
def add_llm_variant(
    experiment_id: UUID,
    data: CreateLLMVariantRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Add a new variant to an existing LLM experiment."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    experiment = _service.get_experiment(db, experiment_id)
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="LLM experiment not found"
        )
    variant = _service.add_variant(db, experiment_id, data)
    return _to_variant_response(variant)


@router.put(
    "/{experiment_id}/variants/{variant_id}",
    response_model=LLMVariantResponse,
    summary="Update LLM variant",
)
def update_llm_variant(
    experiment_id: UUID,
    variant_id: UUID,
    data: UpdateLLMVariantRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Update an existing LLM variant."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    variant = _service.update_variant(db, variant_id, data)
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="LLM variant not found"
        )
    return _to_variant_response(variant)
