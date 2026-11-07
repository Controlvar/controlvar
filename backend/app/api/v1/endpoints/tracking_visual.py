"""
The routes a web page calls to show a visual experiment (#1545, S3, S4).

Mounted under ``/tracking`` beside the event routes (``backend/app/api/api.py``),
so the SDK rate limit applies. Both take an API key with no scopes
(``backend/app/api/page_key.py``): the key sits in the page, where anyone can
read it.

``POST /api/v1/tracking/visual`` answers which visual changes and goals a
visitor gets on a page:

* The running (ACTIVE) visual experiments whose pages include the page's host
  and path, at most :data:`MAX_EXPERIMENTS_PER_PAGE`, the one that started
  first first. An experiment stored in a page-change format newer than the
  script's ``format_version`` is left out, and so is one whose stored
  settings or page changes fail their schema; nobody is assigned to either.
* Each is assigned exactly as ``POST /api/v1/tracking/assign`` assigns: this
  route awaits that route's own function, so the eligibility checks, the
  sticky answer, the assignment row and the exposure event (with the context,
  on every matched page view) are that route's. A visitor who is not in the
  experiment gets ``assigned: false`` and no changes or goals.
* The ``web_*`` website details in the context are cleaned first
  (``backend/app/schemas/visual_tracking.py``): a detail not in its documented
  form is dropped, never refused.
* ``pages`` lists the page rules of every running visual experiment on the
  host, for the script to keep for 60 seconds.
* One experiment that cannot be answered is left out and logged; the answer is
  a 500 only when experiments matched and none could be answered.

The page's host and path are used only to match page rules: neither is stored.

``POST /api/v1/tracking/visual/preview`` answers one variant's page changes
for a preview link (S4). It records nothing: no assignment, no exposure, no
event. A link that is unknown, expired or revoked answers one 404 text.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.api.page_key import require_page_key
from backend.app.api.v1.endpoints import tracking
from backend.app.core.context_limits import check_context
from backend.app.core.log_once import OnceLog
from backend.app.core.logger import failure_detail, unexpected_failure
from backend.app.core.security import hash_api_key
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.user import User
from backend.app.models.visual_token import PREVIEW, VisualToken
from backend.app.schemas.split_url_config import normalize_host, normalize_path
from backend.app.schemas.tracking import AssignmentRequest
from backend.app.schemas.visual import VisualChanges, VisualConfig, page_matches
from backend.app.schemas.visual_tracking import (
    VisualExperimentAnswer,
    VisualPageRule,
    VisualPages,
    VisualPreviewRequest,
    VisualPreviewResponse,
    VisualTrackingRequest,
    VisualTrackingResponse,
    clean_web_details,
)

router = APIRouter(route_class=OffLoopRoute)

logger = logging.getLogger(__name__)

#: The most experiments one answer carries.
MAX_EXPERIMENTS_PER_PAGE = 10
#: Seconds the script may keep ``pages``.
PAGE_RULES_MAX_AGE = 60

#: A preview link's token: 43 base64url characters (256 bits).
PREVIEW_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")

#: The one 404 for a preview link that is unknown, expired or revoked.
PREVIEW_NOT_FOUND = (
    "This preview link has expired, was revoked, or does not exist. Create a "
    "new one on the experiment's page in Controlvar."
)

#: The sentence of the 500 when experiments matched and none could be answered.
PAGE_FAILURE_SENTENCE = "Could not answer which visual changes this page gets"

#: One log line per process when details are dropped, and one per experiment
#: whose stored settings fail their schema: the same cause on every page view.
_NOTES = OnceLog()

_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    401: {"description": "No API key, or an unknown, expired or revoked one"},
    403: {"description": "The API key has scopes: a key in a web page must have none"},
    422: {"description": "The request is not in the documented form"},
    429: {"description": "More requests from this address than the SDK limit allows"},
}


class _Candidate(NamedTuple):
    """A running visual experiment, as plain values: an assignment commits,
    and a commit expires every ORM object the session holds."""

    id: UUID
    key: str
    visual_config: Dict[str, Any]


def _running_visual_experiments(db: Session) -> List[_Candidate]:
    """Every ACTIVE experiment with a key and visual settings, the one that
    started first first (then by id, so the order never changes)."""
    rows = (
        db.query(Experiment.id, Experiment.key, Experiment.visual_config)
        .filter(
            Experiment.status == ExperimentStatus.ACTIVE,
            Experiment.key.isnot(None),
            func.jsonb_typeof(Experiment.visual_config) == "object",
        )
        .order_by(Experiment.start_date.asc().nulls_last(), Experiment.id.asc())
        .all()
    )
    return [_Candidate(row[0], row[1], row[2]) for row in rows]


def _readable(config: Dict[str, Any], format_version: int) -> bool:
    """Whether the script's format reads this experiment's settings."""
    version = config.get("version")
    return isinstance(version, int) and version <= format_version


def _rules_on(config: Dict[str, Any], host: str) -> List[Tuple[str, str]]:
    """``(match, path)`` of each of *config*'s page rules on *host*."""
    rules = []
    for page in config.get("pages") or []:
        parts = urlsplit(page["url"])
        if normalize_host(parts.netloc) == host:
            rules.append((page["match"], normalize_path(parts.path)))
    return rules


def _page_rules(rules: List[Tuple[str, str]]) -> List[VisualPageRule]:
    return [VisualPageRule(match=m, path=p) for m, p in sorted(set(rules))]


def _matched(
    candidates: List[_Candidate], host: str, path: str, format_version: int
) -> Tuple[List[_Candidate], List[Tuple[str, str]]]:
    """The candidates whose pages include ``host`` and ``path`` (the script
    reads their format), and every readable candidate's rules on ``host``.

    A candidate whose stored settings cannot be read is skipped and logged.
    """
    matched: List[_Candidate] = []
    rules: List[Tuple[str, str]] = []
    for candidate in candidates:
        config = candidate.visual_config
        try:
            if not _readable(config, format_version):
                continue
            rules.extend(_rules_on(config, host))
            if any(page_matches(host, path, page) for page in config["pages"]):
                matched.append(candidate)
        except Exception as exc:  # a row written outside the API
            _log_unreadable(candidate.id, exc)
    return matched, rules


def _log_unreadable(experiment_id: Any, exc: BaseException) -> None:
    if _NOTES.first(str(experiment_id), "unreadable"):
        logger.warning(
            "Visual page answer skips experiment %s: its stored visual "
            "settings or page changes are not valid (%s)",
            experiment_id,
            type(exc).__name__,
        )


class _Design(NamedTuple):
    """A matched experiment's checked settings, and its variants' changes."""

    goals: List[Any]
    changes: Dict[str, Optional[Dict[str, Any]]]


def _designs(db: Session, matched: List[_Candidate]) -> Dict[UUID, _Design]:
    """Each matched experiment's goals and variants' page changes, checked
    against their schemas; one statement for the variants of all of them. An
    experiment whose stored values fail is left out (and logged)."""
    variants: Dict[UUID, Dict[str, Optional[Dict[str, Any]]]] = {
        candidate.id: {} for candidate in matched
    }
    rows = (
        db.query(Variant.experiment_id, Variant.id, Variant.visual_changes)
        .filter(Variant.experiment_id.in_([c.id for c in matched]))
        .all()
    )
    for experiment_id, variant_id, changes in rows:
        variants[experiment_id][str(variant_id)] = changes
    designs: Dict[UUID, _Design] = {}
    for candidate in matched:
        try:
            config = VisualConfig.model_validate(candidate.visual_config)
            for changes in variants[candidate.id].values():
                if changes is not None:
                    VisualChanges.model_validate(changes)
        except ValidationError as exc:
            _log_unreadable(candidate.id, exc)
            continue
        designs[candidate.id] = _Design(config.goals, variants[candidate.id])
    return designs


async def _answer(
    db: Session,
    owner: User,
    candidate: _Candidate,
    design: _Design,
    user_id: str,
    context: Optional[Dict[str, Any]],
) -> VisualExperimentAnswer:
    """Assign the visitor as ``/tracking/assign`` does, and the answer."""
    assigned = await tracking.assign_user_to_experiment(
        request=AssignmentRequest(
            experiment_key=candidate.key, user_id=user_id, context=context
        ),
        db=db,
        api_key_info=owner,
    )
    shown = assigned.assigned
    return VisualExperimentAnswer(
        experiment_key=candidate.key,
        variant_id=assigned.variant_id,
        variant_name=assigned.variant_name,
        assigned=shown,
        reason=assigned.reason,
        changes=design.changes.get(assigned.variant_id) if shown else None,
        goals=design.goals if shown else None,
    )


@router.post(
    "/visual",
    response_model=VisualTrackingResponse,
    summary="The visual changes and goals a visitor gets on a page (beta)",
    response_description="Each running visual experiment on the page, and the host's page rules",
    openapi_extra={"x-stability": "beta"},
    responses=_RESPONSES,
)
async def visual_changes_for_page(
    response: Response,
    request: VisualTrackingRequest = Body(..., description="The page and the visitor"),
    db: Session = Depends(deps.get_db),
    owner: User = Depends(require_page_key),
) -> VisualTrackingResponse:
    """
    Which visual changes and goals this visitor gets on this page.

    Called by the visual editor's browser script on each page view, with the
    page's host and path (never its query) and the visitor's id. Answers the
    running visual experiments whose pages include the page, at most 10, the
    one that started first first. Each is assigned exactly as
    `POST /api/v1/tracking/assign` assigns, and records what it records: an
    assignment for a new visitor, and an exposure event with the context on
    every matched page view. A visitor who is not in an experiment gets
    `assigned: false` with the reason, and no changes or goals.

    The context's `web_*` website details are kept only in their documented
    form; any other `web_*` key, and `web_country`, is dropped, never refused.
    The host and the path are not stored.

    `pages` holds the page rules of every running visual experiment on this
    host; the script may keep them for `max_age` seconds and make no call on a
    page none of them matches. Sent with `Cache-Control: no-store`.

    **Authentication**: an API key with no scopes, in `X-API-Key`. A key with
    any scope is refused with 403: it belongs on a server.
    """
    response.headers["Cache-Control"] = "no-store"
    check_context(request.context)
    context, dropped = clean_web_details(request.context)
    if dropped and _NOTES.first("tracking_visual", "details dropped"):
        logger.info(
            "Visual page answer dropped website details that are not in their "
            "documented form (logged once per process)"
        )
    host = normalize_host(request.host)

    try:
        candidates = _running_visual_experiments(db)
        matched, rules = _matched(
            candidates, host, request.path, request.format_version
        )
        matched = matched[:MAX_EXPERIMENTS_PER_PAGE]
        designs = _designs(db, matched) if matched else {}
    except Exception as exc:
        raise unexpected_failure(
            exc, "Visual page answer", PAGE_FAILURE_SENTENCE, db=db, logger=logger
        )

    answers: List[VisualExperimentAnswer] = []
    for candidate in matched:
        design = designs.get(candidate.id)
        if design is None:
            continue
        try:
            answers.append(
                await _answer(db, owner, candidate, design, request.user_id, context)
            )
        except Exception as exc:
            # One experiment that cannot be answered is left out: the visitor
            # sees the page as it is for it, and the others still apply.
            try:
                db.rollback()
            except Exception:
                logger.warning("Rollback after a failed visual page answer also failed")
            logger.warning(
                "Visual page answer left out experiment %s (%s)",
                candidate.id,
                type(exc).__name__,
            )

    if matched and not answers:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=failure_detail(PAGE_FAILURE_SENTENCE),
        )
    return VisualTrackingResponse(
        experiments=answers,
        pages=VisualPages(
            host=host, rules=_page_rules(rules), max_age=PAGE_RULES_MAX_AGE
        ),
    )


def _live_preview(db: Session, token: str) -> Optional[VisualToken]:
    """The preview link *token* names, if it is neither expired nor revoked."""
    if PREVIEW_TOKEN_PATTERN.fullmatch(token) is None:
        return None
    return (
        db.query(VisualToken)
        .filter(
            VisualToken.token_hash == hash_api_key(token),
            VisualToken.kind == PREVIEW,
            VisualToken.revoked_at.is_(None),
            VisualToken.expires_at > datetime.now(timezone.utc),
        )
        .first()
    )


@router.post(
    "/visual/preview",
    response_model=VisualPreviewResponse,
    summary="One variant's visual changes, for a preview link (beta)",
    response_description="The variant's page changes and the host's page rules",
    openapi_extra={"x-stability": "beta"},
    responses={
        **_RESPONSES,
        404: {"description": "The preview link is unknown, expired or revoked"},
    },
)
async def visual_preview(
    response: Response,
    request: VisualPreviewRequest = Body(
        ..., description="The preview link's token and the page"
    ),
    db: Session = Depends(deps.get_db),
    owner: User = Depends(require_page_key),
) -> VisualPreviewResponse:
    """
    The page changes of the variant a preview link shows.

    The token is the one in the link's `#controlvar-preview=` fragment, made
    on the experiment's page in Controlvar (`POST /api/v1/experiments/{id}/
    visual/preview-links`). A preview ignores the experiment's audience and
    status and records nothing: no assignment, no exposure, no event. A link
    that is unknown, expired or revoked answers 404 with one text for all
    three. `pages` holds the experiment's page rules on this host. Sent with
    `Cache-Control: no-store`.

    **Authentication**: an API key with no scopes, in `X-API-Key`; a key with
    any scope is refused with 403.
    """
    response.headers["Cache-Control"] = "no-store"
    link = _live_preview(db, request.token)
    experiment = db.get(Experiment, link.experiment_id) if link is not None else None
    variant = (
        next((v for v in experiment.variants if v.name == link.variant_name), None)
        if experiment is not None and link is not None
        else None
    )
    if experiment is None or variant is None or experiment.visual_config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=PREVIEW_NOT_FOUND
        )
    try:
        changes = variant.visual_changes
        if changes is not None:
            VisualChanges.model_validate(changes)
        rules = _rules_on(experiment.visual_config, normalize_host(request.host))
    except Exception as exc:  # stored outside the API
        _log_unreadable(experiment.id, exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=PREVIEW_NOT_FOUND
        )
    return VisualPreviewResponse(
        experiment_key=experiment.key,
        variant_name=variant.name,
        changes=changes,
        pages=VisualPages(
            host=normalize_host(request.host),
            rules=_page_rules(rules),
            max_age=PAGE_RULES_MAX_AGE,
        ),
    )
