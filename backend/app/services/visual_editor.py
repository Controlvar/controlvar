"""
The visual editor's one-time sign-in (#1545): the statements behind an editor
link, its claim and the session, and the texts their refusals carry.

The sign-in is a chain of ``visual_tokens`` rows, each pointing at the one
before it through ``parent_id``:

* **ticket** (``editor_ticket``, 120 seconds): written when the editor is
  opened from the dashboard. Writing it first ends every live editor
  credential of the experiment, whoever holds it (:func:`reopen`, reason
  ``reopened``). The editor link carries the ticket in its fragment.
* **claim** (``editor_claim``, 120 seconds): the page the link opened sends
  the ticket and a value of its own, 43 base64url characters; the ticket is
  spent and the claim is written as its child, under the hash of that value.
* **session** (``editor_session``, 4 hours): the editor's panel exchanges the
  claim; the claim is spent and the session, ``cves_`` and 43 base64url
  characters, is written as the claim's child.

Each step spends its credential with one UPDATE (:func:`spend`) and commits at
once, so of two requests carrying the same credential exactly one finds it
unspent, and a refusal that follows the spend leaves it spent. A spent ticket
or claim presented again ends the ticket and every live row descended from it
(:func:`end_descendants`, reason ``ticket_reused``): the editor that link
opened closes, and the answer is :data:`LINK_USED_TWICE`.

Every request that ends or writes an editor credential of an experiment takes
:func:`hold_editor` for that experiment first, in the transaction that does
it: opening the editor before it ends the earlier credentials, a reuse before
it ends the link's, and the claim and the exchange before they write the next
credential, which they then write only if the one they spent is still live
(:func:`parent_refusal`). For one experiment they run one after another.

Only the SHA-256 of a credential is stored (``hash_api_key``). No answer but
the one that hands a credential out holds it, and no log line or audit entry
holds a credential or its hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.mutual_exclusion_group import MutualExclusionGroup
from backend.app.models.visual_token import (
    EDITOR_KINDS,
    EDITOR_SESSION,
    REOPENED,
    SCHEDULED,
    SIGNED_OUT,
    STARTED,
    TICKET_REUSED,
    VisualToken,
)
from backend.app.services.audit_service import SYSTEM_VISUAL_EDITOR, AuditService
from backend.app.services.global_holdout_service import GlobalHoldoutService
from backend.app.services.visual import design_etag, is_visual
from backend.app.services.visual_origins import site_origins

#: The fragment an editor link carries its ticket in.
EDITOR_FRAGMENT = "controlvar-editor"
#: How long each credential lives.
TICKET_LIFETIME = timedelta(seconds=120)
CLAIM_LIFETIME = timedelta(seconds=120)
SESSION_LIFETIME = timedelta(hours=4)
#: A session token is this prefix and 43 base64url characters.
SESSION_TOKEN_PREFIX = "cves_"

# --- texts ----------------------------------------------------------------------

#: An editor link that is unknown, expired or ended: one text for every case.
LINK_NOT_VALID = (
    "This editor link has expired or is not valid. Open the editor again from "
    "the experiment's page in Controlvar."
)
#: A spent editor link presented again (409).
LINK_USED_TWICE = (
    "This editor link was already used, so the editor it opened has been "
    "closed. Open the editor again from Controlvar."
)
#: The page's value is already stored (422).
CLAIM_IN_USE = "This value is already in use. Send a new random one."
#: The experiment has no visual settings (400).
NOT_VISUAL = (
    "This experiment has no visual settings, so it cannot be opened in the "
    "visual editor."
)
#: The experiment has left draft (400).
NOT_A_DRAFT = (
    "This experiment is no longer a draft, so the visual editor cannot open it."
)
#: A visual draft with a stored start date (400).
DATED_DRAFT = (
    "A visual draft with a start date cannot be opened in the editor: it starts "
    "on its own with the design it has then. Remove the start date to edit it."
)
#: The editor's user may no longer change the experiment (403).
CANNOT_CHANGE = (
    "You can no longer change this experiment. Ask an administrator for "
    "permission to change experiments."
)
EXPERIMENT_NOT_FOUND = "Experiment not found"
#: An editor session whose experiment was deleted (404).
EXPERIMENT_GONE = "This experiment no longer exists."
#: A variant id that is not one of the session's experiment's (404).
VARIANT_NOT_FOUND = "This experiment has no variant with that id."

#: The reason codes an editor session's 401 carries besides the stored
#: ``revoked_reason`` values: no session has that token, or it has ended.
NOT_RECOGNISED = "not_recognised"
EXPIRED = "expired"
#: What the editor shows for each reason code, with focus on it.
CLOSED_TEXTS: Dict[str, str] = {
    REOPENED: (
        "The editor was opened again for this experiment, so this one has "
        "closed. Your saved changes are kept."
    ),
    TICKET_REUSED: (
        "This editor link was used twice, so Controlvar closed the editor it "
        "opened. Your saved changes are kept. Open the editor again from the "
        "experiment's page."
    ),
    STARTED: "This experiment was started in Controlvar, so the editor has closed.",
    SIGNED_OUT: (
        "You signed out of the visual editor. Open the editor again from the "
        "experiment's page in Controlvar."
    ),
    SCHEDULED: (
        "This draft now has a start date, so the editor has closed. Your saved "
        "changes are kept. Remove the start date on the experiment's page to "
        "edit again."
    ),
    EXPIRED: (
        "Your editing session has ended. Saved changes are kept. Open the "
        "editor again from the experiment's page in Controlvar."
    ),
    NOT_RECOGNISED: (
        "The visual editor does not recognise this sign-in. Open the editor "
        "again from the experiment's page in Controlvar."
    ),
}


def closed_reason(reason: Optional[str]) -> str:
    """*reason* if it has a text, else :data:`NOT_RECOGNISED`."""
    return reason if reason in CLOSED_TEXTS else NOT_RECOGNISED


def utcnow() -> datetime:
    """The time every step of the sign-in reads: an aware UTC datetime. The
    expiry a row is written with and the expiry it is compared against both
    come from here, never from the database's clock."""
    return datetime.now(timezone.utc)


# --- the statements ---------------------------------------------------------------


@dataclass(frozen=True)
class Spent:
    """A credential :func:`spend` spent, or :func:`used_row` found spent."""

    id: UUID
    experiment_id: UUID
    user_id: Optional[UUID]
    parent_id: Optional[UUID]


@dataclass(frozen=True)
class Ended:
    """A credential :func:`reopen` or :func:`end_descendants` ended."""

    id: UUID
    kind: str
    user_id: Optional[UUID]


_SPENT_COLUMNS = (
    VisualToken.id,
    VisualToken.experiment_id,
    VisualToken.user_id,
    VisualToken.parent_id,
)
_ENDED_COLUMNS = (VisualToken.id, VisualToken.kind, VisualToken.user_id)


def spend(db: Session, kind: str, token_hash: str, now: datetime) -> Optional[Spent]:
    """Spend the live, unspent credential of *kind* stored under *token_hash*:
    the ONE statement, ``UPDATE visual_tokens SET used_at = :now WHERE
    token_hash = :h AND kind = :kind AND used_at IS NULL AND revoked_at IS
    NULL AND expires_at > :now RETURNING ...``. ``None`` when nothing was
    spent. Two transactions running it on one row: the second waits for the
    first, then finds ``used_at`` set and spends nothing. The caller commits."""
    row = db.execute(
        update(VisualToken)
        .where(
            VisualToken.token_hash == token_hash,
            VisualToken.kind == kind,
            VisualToken.used_at.is_(None),
            VisualToken.revoked_at.is_(None),
            VisualToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(*_SPENT_COLUMNS)
        .execution_options(synchronize_session=False)
    ).first()
    return Spent(*row) if row is not None else None


def used_row(db: Session, kind: str, token_hash: str) -> Optional[Spent]:
    """The credential of *kind* stored under *token_hash* if it was spent,
    whether or not it has been ended since; else ``None``."""
    row = db.execute(
        select(*_SPENT_COLUMNS).where(
            VisualToken.token_hash == token_hash,
            VisualToken.kind == kind,
            VisualToken.used_at.is_not(None),
        )
    ).first()
    return Spent(*row) if row is not None else None


def hash_is_stored(db: Session, token_hash: str) -> bool:
    """Whether any credential, of any kind, is stored under *token_hash*."""
    return (
        db.execute(
            select(VisualToken.id).where(VisualToken.token_hash == token_hash)
        ).first()
        is not None
    )


def hold_editor(db: Session, experiment_id: Any) -> None:
    """Wait until no other transaction holds *experiment_id*'s editor
    credentials, then hold them until this transaction commits or rolls back:
    a PostgreSQL advisory transaction lock on the experiment's id. Taken
    before ending or writing any editor credential of the experiment (see the
    module docstring); nothing else takes it."""
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"controlvar.visual-editor.{experiment_id}"},
    )


def parent_refusal(db: Session, parent_id: Any) -> Optional[HTTPException]:
    """Read after :func:`hold_editor`, just before a request writes the
    credential that follows the one it spent (*parent_id*): ``None`` while
    that one is still live (not ended, not expired), else the refusal to
    raise, the 409 :data:`LINK_USED_TWICE` when it was ended because its link
    was used again, the 401 :data:`LINK_NOT_VALID` otherwise. The caller rolls
    back before raising it."""
    row = db.execute(
        select(
            VisualToken.revoked_at, VisualToken.revoked_reason, VisualToken.expires_at
        ).where(VisualToken.id == parent_id)
    ).first()
    if row is not None and row.revoked_at is None and row.expires_at > utcnow():
        return None
    if row is not None and row.revoked_reason == TICKET_REUSED:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=LINK_USED_TWICE
        )
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=LINK_NOT_VALID
    )


def reopen(db: Session, experiment_id: Any, now: datetime) -> List[Ended]:
    """End every live editor ticket, claim and session of *experiment_id*,
    whoever holds it, with the reason ``reopened``; returns each one ended.
    The caller holds :func:`hold_editor` and commits."""
    rows = db.execute(
        update(VisualToken)
        .where(
            VisualToken.experiment_id == experiment_id,
            VisualToken.kind.in_(EDITOR_KINDS),
            VisualToken.revoked_at.is_(None),
            VisualToken.expires_at > now,
        )
        .values(revoked_at=now, revoked_reason=REOPENED)
        .returning(*_ENDED_COLUMNS)
        .execution_options(synchronize_session=False)
    ).all()
    return [Ended(*row) for row in rows]


def end_descendants(db: Session, ticket_id: Any, now: datetime) -> List[Ended]:
    """End the ticket *ticket_id* and every row descended from it through
    ``parent_id`` -- its claims, and their sessions -- that is still live,
    with the reason ``ticket_reused``; returns each one ended. The walk goes
    through rows that have expired or ended. The caller holds
    :func:`hold_editor` and commits."""
    tree = (
        select(VisualToken.id)
        .where(VisualToken.id == ticket_id)
        .cte("descendants", recursive=True)
    )
    tree = tree.union_all(
        select(VisualToken.id).where(VisualToken.parent_id == tree.c.id)
    )
    rows = db.execute(
        update(VisualToken)
        .where(
            VisualToken.id.in_(select(tree.c.id)),
            VisualToken.revoked_at.is_(None),
            VisualToken.expires_at > now,
        )
        .values(revoked_at=now, revoked_reason=TICKET_REUSED)
        .returning(*_ENDED_COLUMNS)
        .execution_options(synchronize_session=False)
    ).all()
    return [Ended(*row) for row in rows]


# --- the checks --------------------------------------------------------------------


def refuse_unless_open_to_edit(experiment: Any) -> None:
    """Raise the 400 for an experiment the editor can no longer change, in
    this order: it is no longer a draft, it has a start date, it has no visual
    settings."""
    if experiment.status != ExperimentStatus.DRAFT:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=NOT_A_DRAFT)
    if experiment.start_date is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=DATED_DRAFT)
    if not is_visual(experiment):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=NOT_VISUAL)


# --- answers and audit -------------------------------------------------------------


#: What the editor may offer besides the design's own fields: nothing yet.
CAPABILITIES: Dict[str, bool] = {"balance_within": False, "country": False}


def session_state(
    db: Session, experiment: Any, expires_at: datetime, user: Any
) -> Dict[str, Any]:
    """What the editor's panel is told about its session: when it ends, the
    sites it may run on, and the experiment's design as stored now.

    The audience, the split, the metrics and the goals are the experiment's
    own answer's (``GET /api/v1/experiments/{id}``) for the same row; the
    global holdout is shown only to *user* if they may create experiments."""
    # Imported here: the experiment service imports this module.
    from backend.app.schemas.experiment import ExperimentResponse
    from backend.app.services.experiment_service import ExperimentService

    shown = ExperimentResponse.model_validate(
        ExperimentService(db).to_response_dict(experiment)
    )
    config = experiment.visual_config or {}
    return {
        "expires_at": expires_at,
        "site_origins": site_origins(experiment.visual_config),
        "experiment": {
            "id": experiment.id,
            "key": experiment.key,
            "name": experiment.name,
            "editor_url": config.get("editor_url"),
            "pages": list(config.get("pages") or []),
            "design_etag": design_etag(experiment),
            "variants": [
                {
                    "id": variant.id,
                    "name": variant.name,
                    "is_control": bool(variant.is_control),
                    "traffic_allocation": variant.traffic_allocation,
                    "visual_changes": variant.visual_changes,
                }
                for variant in experiment.variants
            ],
            "targeting_rules": shown.targeting_rules,
            "metrics": shown.metrics,
            "goals": list((shown.visual_config or {}).get("goals") or []),
            "mutual_exclusion_group": exclusion_group_of(
                db, shown.mutual_exclusion_group_id
            ),
            "holdout": holdout_for(db, user),
            "capabilities": dict(CAPABILITIES),
        },
    }


def exclusion_group_of(db: Session, group_id: Any) -> Optional[Dict[str, Any]]:
    """``{id, name}`` of the mutual exclusion group *group_id*, or ``None``
    when it is ``None`` or no such group exists."""
    if group_id is None:
        return None
    group = db.get(MutualExclusionGroup, group_id)
    return None if group is None else {"id": group.id, "name": group.name}


def holdout_for(db: Session, user: Any) -> Optional[Dict[str, Any]]:
    """``{percentage}`` of the active global holdout for a *user* who may
    create experiments; ``None`` for anyone else, and when none is active."""
    if user is None or not check_permission(
        user, ResourceType.EXPERIMENT, Action.CREATE
    ):
        return None
    holdout = GlobalHoldoutService(db).get_active_holdout()
    return None if holdout is None else {"percentage": holdout.holdout_percentage}


def audit_sessions_closed(
    db: Session,
    ended: List[Ended],
    *,
    actor: Any,
    reason: str,
    experiment_id: Any,
    experiment_name: Optional[str],
) -> None:
    """One ``visual_editor_session_closed`` entry for each session in
    *ended*, written after the caller's commit, naming the session and its
    user. Tickets and claims ended with them write none."""
    for row in ended:
        if row.kind != EDITOR_SESSION:
            continue
        AuditService.record_after_commit(
            db,
            actor=actor,
            action=ActionType.VISUAL_EDITOR_SESSION_CLOSED,
            entity_type=EntityType.EXPERIMENT,
            entity_id=experiment_id,
            entity_name=experiment_name,
            after=session_closed_value(reason, row),
        )


def session_closed_value(reason: str, row: Any) -> Dict[str, Any]:
    """``new_value`` of a ``visual_editor_session_closed`` entry: why the
    session closed, the session's row id and its user's id."""
    return {
        "reason": reason,
        "session_id": str(row.id),
        "session_user_id": str(row.user_id) if row.user_id else None,
    }


def live_sessions(rows: List[Any], now: datetime) -> List[Any]:
    """The editor sessions among *rows* (as ``revoke_editor_tokens`` returns
    them) that had not ended by *now*: the ones a start closes."""
    return [row for row in rows if row.kind == EDITOR_SESSION and row.expires_at > now]


def end_reused_link(
    db: Session, ticket_id: Any, ticket_user_id: Any, experiment_id: Any
) -> HTTPException:
    """A spent ticket or claim was presented again: end its ticket and
    everything descended from it, commit, write
    ``visual_editor_ticket_reused`` and one ``visual_editor_session_closed``
    per session ended (by ``system:visual-editor``), and return the 409 to
    raise."""
    hold_editor(db, experiment_id)
    ended = end_descendants(db, ticket_id, utcnow())
    name = db.execute(
        select(Experiment.name).where(Experiment.id == experiment_id)
    ).scalar()
    db.commit()
    AuditService.record_after_commit(
        db,
        actor=SYSTEM_VISUAL_EDITOR,
        action=ActionType.VISUAL_EDITOR_TICKET_REUSED,
        entity_type=EntityType.EXPERIMENT,
        entity_id=experiment_id,
        entity_name=name,
        after={"link_user_id": str(ticket_user_id) if ticket_user_id else None},
    )
    audit_sessions_closed(
        db,
        ended,
        actor=SYSTEM_VISUAL_EDITOR,
        reason=TICKET_REUSED,
        experiment_id=experiment_id,
        experiment_name=name,
    )
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=LINK_USED_TWICE)
