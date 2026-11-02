"""
Split URL routing for a server or edge: where to send each visitor (#393).

MODULE: split_url. Two routes, both beta and both behind an API key
(``X-API-Key``), mounted at ``/api/v1/tracking/split-url`` by the modules'
registration (a core deployment does not serve them, and answers 404):

* ``GET /routes``: the entries of every ACTIVE split URL experiment (the
  control URL's host and path), to keep for ``max_age`` seconds and match
  each request against.
* ``POST /assign``: for a request whose host and path are an experiment's
  entry, the visitor's variant and where to send them. It answers through
  ``POST /api/v1/tracking/assign``'s own function, so the visitor gets the
  same assignment row, exposure, global holdout, mutual exclusion, targeting
  and bandit choice as that route gives.

The SPEC is ``docs/api/split-url.md``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session, joinedload

from backend.app.api import deps
from backend.app.api.v1.endpoints import tracking
from backend.app.core.context_limits import check_context
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.schemas.split_url_config import normalize_host, normalize_path
from backend.app.schemas.tracking import AssignmentRequest
from modules.backend.app.api.v1.endpoints.experiments_split_url import (
    NO_SPLIT_URL_CONFIG_DETAIL,
    NOT_SPLIT_URL_DETAIL,
    SPLIT_URL_CONFIG_NOT_VALID_DETAIL,
    is_split_url,
)
from modules.backend.app.services import split_url_service

logger = logging.getLogger(__name__)

#: Mounted behind nothing: each route takes an API key itself.
public_router = APIRouter()

#: The reasons the assign route adds to ``/tracking/assign``'s.
REASON_NOT_ENTRY = "not_entry"
REASON_BOT = "bot"

#: Every ``reason`` the assign route answers: ``/tracking/assign``'s, then
#: the two it adds.
REASONS = (
    "assigned",
    "holdout",
    "mutual_exclusion",
    "targeting",
    "draft",
    "paused",
    "completed",
    "archived",
    REASON_NOT_ENTRY,
    REASON_BOT,
)

#: The longest ``host``: a 253-character name, ``:`` and a 5-digit port, or a
#: bracketed IPv6 literal with its port.
MAX_HOST_LENGTH = 261
MAX_PATH_LENGTH = 2048
MAX_QUERY_LENGTH = 8192
MAX_USER_AGENT_LENGTH = 1024


def _has_space_or_control(value: str) -> bool:
    return any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)


class SplitUrlAssignRequest(AssignmentRequest):
    """``POST /api/v1/tracking/assign``'s request, plus the request the
    visitor made: its host, path, query and User-Agent."""

    host: str = Field(
        ...,
        min_length=1,
        max_length=MAX_HOST_LENGTH,
        description=(
            "The host the visitor asked for (their Host header), with or without "
            "a :port; compared without case or port. No whitespace or control "
            "characters."
        ),
    )
    path: str = Field(
        ...,
        min_length=1,
        max_length=MAX_PATH_LENGTH,
        description=(
            "The path the visitor asked for, as they sent it (not percent-decoded), "
            "starting with /. One trailing / is ignored."
        ),
    )
    query: Optional[str] = Field(
        None,
        max_length=MAX_QUERY_LENGTH,
        description=(
            "The visitor's query string, without the ?, as they sent it. It is "
            "added to the URL of a variant on the control URL's host. No #, "
            "whitespace or control characters."
        ),
    )
    user_agent: Optional[str] = Field(
        None,
        max_length=MAX_USER_AGENT_LENGTH,
        description=(
            "The visitor's User-Agent. A crawler is answered as a new visitor "
            "would be, and nothing is recorded for it."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "experiment_key": "checkout-pages",
                "user_id": "6f1c2a9e-3b7d-4e1a-9c55-0d2b8e4f7a10",
                "host": "shop.example.com",
                "path": "/checkout",
                "query": "utm_source=mail",
                "user_agent": "Mozilla/5.0",
            }
        }
    )

    @field_validator("host")
    @classmethod
    def _host_has_no_space(cls, value: str) -> str:
        if _has_space_or_control(value):
            raise ValueError("host must not contain whitespace or control characters")
        return value

    @field_validator("path")
    @classmethod
    def _path_starts_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with /")
        return value

    @field_validator("query")
    @classmethod
    def _query_has_no_fragment(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and ("#" in value or _has_space_or_control(value)):
            raise ValueError(
                "query must not contain #, whitespace or control characters"
            )
        return value


class SplitUrlAssignResponse(BaseModel):
    """Where to send the visitor, and why."""

    experiment_key: str
    user_id: str
    action: Literal["redirect", "serve"] = Field(
        ...,
        description=(
            "redirect: answer 302 with Location set to location. serve: serve the "
            "page the visitor asked for."
        ),
    )
    location: Optional[str] = Field(
        None,
        description=(
            "On redirect, the variant's URL, with the visitor's query string added "
            "when the variant is on the control URL's host; otherwise null."
        ),
    )
    variant_id: Optional[str] = Field(
        None, description="The visitor's variant; null when reason is not_entry."
    )
    variant_name: Optional[str] = Field(
        None, description="The visitor's variant; null when reason is not_entry."
    )
    assigned: bool = Field(
        ...,
        description=(
            "True when the visitor holds an assignment in the experiment; false "
            "when nothing was recorded for them."
        ),
    )
    reason: str = Field(
        ...,
        description=(
            "assigned | holdout | mutual_exclusion | targeting, the status of an "
            "experiment that is not ACTIVE (draft | paused | completed | "
            "archived), not_entry (the host and path are not the experiment's "
            "control URL's; nothing recorded) or bot (a crawler; nothing recorded)"
        ),
    )


class SplitUrlRoute(BaseModel):
    """One ACTIVE split URL experiment's entry."""

    experiment_key: str
    experiment_id: str
    host: str = Field(..., description="The control URL's host name, in lower case")
    path: str = Field(
        ...,
        description="The control URL's path, without one trailing / (except /)",
    )


class SplitUrlRoutesResponse(BaseModel):
    """The entries to match requests against, and how long to keep them."""

    routes: List[SplitUrlRoute]
    max_age: int = Field(
        ..., description="Seconds to keep this table before fetching it again"
    )


_KEY_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    401: {"description": "No API key, or an unknown, expired or revoked one"},
    429: {"description": "Over the SDK rate limit for this address and path"},
}


@public_router.get(
    "/routes",
    response_model=SplitUrlRoutesResponse,
    summary="List the split URL entries to match requests against (beta)",
    response_description="Every ACTIVE split URL experiment's control host and path",
    openapi_extra={"x-stability": "beta"},
    responses=_KEY_RESPONSES,
)
def get_split_url_routes(
    response: Response,
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
) -> SplitUrlRoutesResponse:
    """
    Every ACTIVE split URL experiment's entry: the control URL's host (lower
    case) and path, ordered by host, then path, with the experiment's key and
    id. Keep the table for ``max_age`` seconds, and for a GET whose host and
    path are an entry, ask ``POST /api/v1/tracking/split-url/assign``.

    An experiment whose stored configuration breaks the rules is left out,
    and so is one whose control page an experiment started before it already
    has, or that would chain with one.

    **Authentication**: Requires a valid API key in the X-API-Key header.
    """
    routes = split_url_service.routes_table(db)
    response.headers["Cache-Control"] = (
        f"private, max-age={split_url_service.ROUTES_MAX_AGE}"
    )
    return SplitUrlRoutesResponse(
        routes=[SplitUrlRoute(**route._asdict()) for route in routes],
        max_age=split_url_service.ROUTES_MAX_AGE,
    )


def _answer(
    request: SplitUrlAssignRequest,
    *,
    action: str,
    reason: str,
    assigned: bool,
    variant_id: Optional[str] = None,
    variant_name: Optional[str] = None,
    location: Optional[str] = None,
) -> SplitUrlAssignResponse:
    return SplitUrlAssignResponse(
        experiment_key=request.experiment_key,
        user_id=request.user_id,
        action=action,
        location=location,
        variant_id=variant_id,
        variant_name=variant_name,
        assigned=assigned,
        reason=reason,
    )


@public_router.post(
    "/assign",
    response_model=SplitUrlAssignResponse,
    summary="Where to send a split URL experiment's visitor (beta)",
    response_description="redirect, with the variant's URL, or serve",
    openapi_extra={"x-stability": "beta"},
    responses={
        **_KEY_RESPONSES,
        400: {
            "description": "The experiment is not a split URL experiment, or has no "
            "split_url_config"
        },
        404: {"description": "No experiment has the key"},
        409: {"description": "The stored split_url_config breaks the rules"},
    },
)
async def assign_split_url_visitor(
    request: SplitUrlAssignRequest = Body(..., description="The visitor's request"),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
) -> SplitUrlAssignResponse:
    """
    Where to send the visitor who asked for ``host`` + ``path``.

    In order:

    1. 404 when no experiment has the key; 400 when it is not a split URL
       experiment, or has no ``split_url_config``; 409 when the stored
       configuration breaks the rules.
    2. ``serve``, reason ``not_entry``, nothing recorded, when the host
       (without case or port) and the path (without one trailing ``/``) are
       not the control URL's.
    3. For an ACTIVE experiment and a crawler's User-Agent: the variant a new
       visitor with that id would get, reason ``bot``, nothing recorded.
    4. Otherwise ``POST /api/v1/tracking/assign``'s answer: ``redirect`` to the
       variant's URL for a visitor assigned to a variant that is not the
       control, else ``serve`` with that route's reason (``assigned``,
       ``holdout``, ``mutual_exclusion``, ``targeting``, or the status of an
       experiment that is not ACTIVE).

    ``location`` is the variant's stored URL, with ``query`` added when the
    variant is on the control URL's host.

    **Authentication**: Requires a valid API key in the X-API-Key header.
    """
    check_context(request.context)

    experiment = (
        db.query(Experiment)
        .options(joinedload(Experiment.variants))
        .filter(Experiment.key == request.experiment_key)
        .first()
    )
    if not experiment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No experiment has the key '{request.experiment_key}'.",
        )
    if not is_split_url(experiment):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=NOT_SPLIT_URL_DETAIL
        )
    if not experiment.split_url_config:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=NO_SPLIT_URL_CONFIG_DETAIL
        )
    entries = split_url_service.entries_of(experiment)
    if entries is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=SPLIT_URL_CONFIG_NOT_VALID_DETAIL,
        )

    if (normalize_host(request.host), normalize_path(request.path)) != entries.entry:
        return _answer(request, action="serve", reason=REASON_NOT_ENTRY, assigned=False)

    urls = split_url_service.variant_urls(experiment)
    control = split_url_service.control_variant(experiment)
    control_url = urls[control.name]

    if experiment.status == ExperimentStatus.ACTIVE and split_url_service.is_bot(
        request.user_agent
    ):
        variant = split_url_service.new_visitor_variant(db, experiment, request.user_id)
        if variant is None or variant.is_control:
            variant = variant or control
            return _answer(
                request,
                action="serve",
                reason=REASON_BOT,
                assigned=False,
                variant_id=str(variant.id),
                variant_name=variant.name,
            )
        return _answer(
            request,
            action="redirect",
            reason=REASON_BOT,
            assigned=False,
            variant_id=str(variant.id),
            variant_name=variant.name,
            location=split_url_service.location(
                urls[variant.name], control_url, request.query
            ),
        )

    answer = await tracking.assign_user_to_experiment(
        request=AssignmentRequest(
            experiment_key=request.experiment_key,
            user_id=request.user_id,
            context=request.context,
        ),
        db=db,
        api_key_info=api_key_info,
    )
    if answer.assigned and not answer.is_control:
        return _answer(
            request,
            action="redirect",
            reason=answer.reason,
            assigned=True,
            variant_id=answer.variant_id,
            variant_name=answer.variant_name,
            location=split_url_service.location(
                urls[answer.variant_name], control_url, request.query
            ),
        )
    return _answer(
        request,
        action="serve",
        reason=answer.reason,
        assigned=answer.assigned,
        variant_id=answer.variant_id,
        variant_name=answer.variant_name,
    )
