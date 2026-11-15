"""
The write-time segment check for the routes that store targeting rules (#440).

Every segment a flag's or an experiment's rules name must exist, be active,
and, for a rules segment, have stored rules that are valid
(:func:`backend.app.services.segment_membership.segment_reference_problem`).
The pure checks (attribute ``segment``, a segment id, at most 10 segments, no
segment condition in a native ``default_rule``) run earlier, in the request
schemas. The segments an experiment balances within (#448) are held to the
same rule (:func:`refuse_unusable_balance_segments`).
"""

from typing import Any, Optional, Tuple

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.core.balanced_blocks import balance_segments
from backend.app.schemas.balance_within import SEGMENT_NOT_USABLE, UNUSABLE_SEGMENT
from backend.app.services.segment_membership import (
    SEGMENT_MISSING,
    balance_segment_problem,
    segment_reference_problem,
)

#: The clone's 409 for a segment it would balance within that cannot be used.
CLONE_UNUSABLE_BALANCE_SEGMENT = (
    "The experiment cannot be cloned: it balances within segment {segment_id}, "
    "which does not exist, is not active or has rules that are not valid. "
    "Create the experiment again with active segments."
)


def refuse_unusable_segments(db: Session, raw_rules: Any) -> None:
    """422 at ``["body", "targeting_rules"]`` when a named segment cannot be used.

    The message names the condition and the reason, never the submitted value.
    """
    problem = segment_reference_problem(db, raw_rules)
    if problem is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {
                    "loc": ["body", "targeting_rules"],
                    "msg": problem,
                    "type": "value_error",
                }
            ],
        )


def refuse_clone_with_unusable_segments(db: Session, raw_rules: Any) -> None:
    """409 when the rules a clone would copy name a segment that cannot be used."""
    problem = segment_reference_problem(db, raw_rules)
    if problem is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The experiment cannot be cloned: its targeting rules use a segment "
                f"that is not active or whose rules are not valid ({problem}). "
                "Create the experiment again with rules that use active segments."
            ),
        )


def _balance_segment_problem(
    db: Session, balance_within: Any
) -> Optional[Tuple[str, str]]:
    """The first segment *balance_within* lists that cannot be used, and why."""
    if isinstance(balance_within, BaseModel):
        balance_within = balance_within.model_dump(exclude_none=True)
    segment_ids = balance_segments(balance_within)
    if not segment_ids:
        return None
    return balance_segment_problem(db, segment_ids)


def refuse_unusable_balance_segments(db: Session, balance_within: Any) -> None:
    """422 at ``["body", "balance_within", "segments"]`` when a segment the
    experiment would balance within cannot be used (#448).

    The id is the request's own, and has passed the schema: a segment id.
    """
    problem = _balance_segment_problem(db, balance_within)
    if problem is None:
        return
    segment_id, reason = problem
    text = UNUSABLE_SEGMENT if reason == SEGMENT_MISSING else SEGMENT_NOT_USABLE
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[
            {
                "loc": ["body", "balance_within", "segments"],
                "msg": text.format(segment_id=segment_id),
                "type": "value_error",
            }
        ],
    )


def refuse_clone_with_unusable_balance_segments(
    db: Session, balance_within: Any
) -> None:
    """409 when a segment the clone would balance within cannot be used (#448)."""
    problem = _balance_segment_problem(db, balance_within)
    if problem is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=CLONE_UNUSABLE_BALANCE_SEGMENT.format(segment_id=problem[0]),
        )
