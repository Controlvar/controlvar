"""
Segment membership for flag and experiment targeting (#440).

A targeting condition ``{"attribute": "segment", "operator": "in_segment" |
"not_in_segment", "value": "<segment id>"}`` is answered from membership the
server resolves here, never from the request's context:

* :func:`resolve_segment_memberships` decides, for one user, membership of
  the segments a ruleset references, in at most two queries: one for the
  segments, and one for the id lists among them;
* :func:`with_memberships` puts the answer in the evaluation context under
  :data:`~backend.app.core.rules_engine.SEGMENT_MEMBERSHIP_KEY`, after
  removing whatever the request sent under that name;
* :func:`segment_reference_problem` is the write-time check: every segment a
  ruleset names must exist, be active, and (for a rules segment) have rules
  that pass ``validate_segment_rules``;
* :func:`segment_references` lists the flags and experiments that would stop
  evaluating if a segment were archived or deactivated.

A segment whose membership cannot be decided -- unknown, not active, stored
rules that are not valid, a pattern that cannot be evaluated, or a database
error -- is *unavailable*. A ruleset that references an unavailable segment is
not evaluated: the flag answers ``reason: error`` and the experiment refuses
the user. Answering "not a member" instead would make ``not_in_segment``
match everyone.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Collection, Dict, List, Mapping, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.pattern_match import PatternUnevaluable, report_unevaluable
from backend.app.core.rules_engine import SEGMENT_MEMBERSHIP_KEY, TimeWindowUnreadable
from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    _is_dashboard_rules_shape,
    _segment_conditions,
    is_segment_id,
    match_targeting_rule,
    normalise_targeting_rules,
    segment_ids_in,
    validate_segment_rules,
)
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.segment import (
    Segment,
    SegmentKind,
    SegmentMember,
    SegmentStatus,
)
from backend.app.schemas.segment import MAX_MEMBER_ID_LENGTH
from backend.app.schemas.storable_text import contains_unstorable_text
from backend.app.schemas.targeting_rule import TargetingRules

logger = logging.getLogger(__name__)

#: Experiment statuses whose rules can still be evaluated: a segment they
#: reference cannot be archived or deactivated.
REFERENCING_EXPERIMENT_STATUSES = (
    ExperimentStatus.DRAFT,
    ExperimentStatus.ACTIVE,
    ExperimentStatus.PAUSED,
)

#: The two write-time refusals. Fixed text: never the submitted value.
SEGMENT_NOT_ACTIVE = "segment not found or not active"
SEGMENT_RULES_NOT_VALID = "segment rules not valid"


@dataclass(frozen=True)
class SegmentMemberships:
    """One user's membership of a set of segments.

    ``evaluated`` is every id that was asked about; ``unavailable`` the ones
    whose membership could not be decided; ``members`` the ones the user is
    a member of.
    """

    members: frozenset
    evaluated: frozenset
    unavailable: frozenset

    def undecided(self, segment_ids: Collection[str]) -> frozenset:
        """The ids in ``segment_ids`` whose membership is not decided here."""
        return frozenset(
            sid
            for sid in segment_ids
            if sid not in self.evaluated or sid in self.unavailable
        )


NO_SEGMENTS = SegmentMemberships(frozenset(), frozenset(), frozenset())


def _storable_member_id(user_id: Any) -> bool:
    return (
        isinstance(user_id, str)
        and 1 <= len(user_id) <= MAX_MEMBER_ID_LENGTH
        and not contains_unstorable_text(user_id)
    )


def segment_context(context: Mapping[str, Any], user_id: Any) -> Dict[str, Any]:
    """The context a rules segment is evaluated on.

    The referencing request's expanded context, with ``user_id`` (and its
    ``user.user_id`` alias) ASSIGNED from the request's own user id: a
    ``user_id`` the request sent in its context never decides membership.
    """
    seg_ctx = dict(context)
    seg_ctx.pop(SEGMENT_MEMBERSHIP_KEY, None)
    seg_ctx["user_id"] = user_id
    seg_ctx["user.user_id"] = user_id
    return seg_ctx


def resolve_segment_memberships(
    db: Session,
    user_id: Any,
    context: Mapping[str, Any],
    segment_ids: Collection[str],
) -> SegmentMemberships:
    """Decide ``user_id``'s membership of ``segment_ids``.

    ``context`` is the referencing request's context, already expanded
    (``expand_context``). At most two queries: the segments, then -- only
    when an active id list is among them and ``user_id`` could be stored --
    the id-list rows for this user.

    * a rules segment: its stored rules, checked by ``validate_segment_rules``,
      matched on :func:`segment_context`;
    * an id list: a member when the request's ``user_id`` (never one in the
      context) is stored in it.

    Unknown, inactive or archived segments, rules that are not valid, a
    pattern that cannot be evaluated and a ``time_window`` that cannot be read
    are ``unavailable``. A database error
    is rolled back and makes every requested id unavailable.
    """
    requested = frozenset(segment_ids)
    if not requested:
        return NO_SEGMENTS
    unavailable = {sid for sid in requested if not is_segment_id(sid)}
    wanted = requested - unavailable
    members = set()
    if wanted:
        try:
            rows = (
                db.query(Segment.id, Segment.kind, Segment.status, Segment.rules)
                .filter(Segment.id.in_([uuid.UUID(sid) for sid in wanted]))
                .all()
            )
            found = {str(row.id): row for row in rows}
            unavailable |= wanted - set(found)
            id_lists: List[uuid.UUID] = []
            seg_ctx: Optional[Dict[str, Any]] = None
            for sid in sorted(found):
                row = found[sid]
                if row.status != SegmentStatus.ACTIVE:
                    unavailable.add(sid)
                    continue
                if row.kind == SegmentKind.ID_LIST.value:
                    id_lists.append(row.id)
                    continue
                try:
                    rules = validate_segment_rules(row.rules)
                except TargetingRulesError:
                    unavailable.add(sid)
                    continue
                if seg_ctx is None:
                    seg_ctx = segment_context(context, user_id)
                try:
                    if match_targeting_rule(rules, seg_ctx) is not None:
                        members.add(sid)
                except PatternUnevaluable as exc:
                    report_unevaluable(exc, f"segment:{sid}")
                    unavailable.add(sid)
                except TimeWindowUnreadable:
                    # Already reported by the rules engine (#1145).
                    unavailable.add(sid)
            if id_lists and _storable_member_id(user_id):
                hits = (
                    db.query(SegmentMember.segment_id)
                    .filter(
                        SegmentMember.segment_id.in_(id_lists),
                        SegmentMember.member_id == user_id,
                    )
                    .all()
                )
                members |= {str(hit.segment_id) for hit in hits}
        except SQLAlchemyError as exc:
            db.rollback()
            # The type only: a database error's text repeats its parameters.
            logger.warning(
                "Segment membership could not be read (%s)", type(exc).__name__
            )
            return SegmentMemberships(frozenset(), requested, requested)
    return SegmentMemberships(
        frozenset(members) - frozenset(unavailable), requested, frozenset(unavailable)
    )


def with_memberships(
    context: Mapping[str, Any], memberships: Optional[frozenset]
) -> Dict[str, Any]:
    """``context`` with the resolved membership, and nothing the request sent.

    The request's own :data:`SEGMENT_MEMBERSHIP_KEY` is always removed; the
    resolved ``frozenset`` is assigned when there is one. With none, a
    segment condition the scan did not see raises when it is evaluated.
    """
    ctx = dict(context)
    ctx.pop(SEGMENT_MEMBERSHIP_KEY, None)
    if memberships is not None:
        ctx[SEGMENT_MEMBERSHIP_KEY] = memberships
    return ctx


def log_unavailable(owner: str, segment_ids: Collection[str]) -> None:
    """The one WARNING a request logs when segment membership is unavailable.

    Names the flag or experiment and the segment ids, never the user.
    """
    logger.warning(
        "Segment membership could not be decided for %s (segments: %s); "
        "its targeting was not evaluated.",
        owner,
        ", ".join(sorted(str(s) for s in segment_ids)),
    )


# ---------------------------------------------------------------------------
# Write-time reference check
# ---------------------------------------------------------------------------


def segment_reference_problem(db: Session, raw_rules: Any) -> Optional[str]:
    """Why ``raw_rules`` cannot be stored, or ``None``.

    ``raw_rules`` has already passed the flag or experiment validator (shape,
    attribute ``segment``, segment-id values, at most 10 segments). Every
    segment it names must exist and be active, and a rules segment's stored
    rules must pass ``validate_segment_rules``. The answer is
    ``"<path>: <reason>"`` for the first condition that fails, in the
    submitted value's terms, and never repeats a submitted value.
    """
    if not isinstance(raw_rules, dict) or not raw_rules:
        return None
    rules = normalise_targeting_rules(raw_rules)
    if rules is None:
        return None
    groups_base = "groups" if _is_dashboard_rules_shape(raw_rules) else None
    conditions = list(_segment_conditions(rules, groups_base))
    if not conditions:
        return None
    ids = {c.value for _p, c in conditions if is_segment_id(c.value)}
    rows = (
        db.query(Segment.id, Segment.kind, Segment.status, Segment.rules)
        .filter(Segment.id.in_([uuid.UUID(sid) for sid in ids]))
        .all()
        if ids
        else []
    )
    found = {str(row.id): row for row in rows}
    for path, condition in conditions:
        row = found.get(condition.value)
        if row is None or row.status != SegmentStatus.ACTIVE:
            return f"{path}.value: {SEGMENT_NOT_ACTIVE}"
        if row.kind != SegmentKind.ID_LIST.value:
            try:
                validate_segment_rules(row.rules)
            except TargetingRulesError:
                return f"{path}.value: {SEGMENT_RULES_NOT_VALID}"
    return None


# ---------------------------------------------------------------------------
# What a segment is used by
# ---------------------------------------------------------------------------


def _references(raw_rules: Any, segment_id: str) -> bool:
    """Whether stored rules that mention ``segment_id`` reference it.

    Rules that cannot be read count as a reference: the archive check fails
    closed.
    """
    try:
        rules: Optional[TargetingRules] = normalise_targeting_rules(raw_rules)
        if rules is None:
            return True
        return segment_id in {sid.lower() for sid in segment_ids_in(rules)}
    except Exception:
        return True


def segment_references(
    db: Session, segment_id: Any
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The flags and experiments whose rules reference ``segment_id``.

    Flags that are not archived and experiments that are draft, active or
    paused: the ones whose rules can still be evaluated. A text prefilter
    finds candidate rows; each is confirmed by reading its rules, and a row
    whose rules cannot be read counts. Query errors propagate.

    Returns:
        ``(feature_flags, experiments)``: ``[{id, key, name}]`` and
        ``[{id, key, name, status}]``, each ordered by key.
    """
    sid = str(segment_id).lower()
    pattern = f"%{sid}%"
    flag_rows = (
        db.query(
            FeatureFlag.id,
            FeatureFlag.key,
            FeatureFlag.name,
            FeatureFlag.targeting_rules,
        )
        .filter(
            FeatureFlag.status != FeatureFlagStatus.ARCHIVED,
            sa.cast(FeatureFlag.targeting_rules, sa.Text).ilike(pattern),
        )
        .order_by(FeatureFlag.key)
        .all()
    )
    experiment_rows = (
        db.query(
            Experiment.id,
            Experiment.key,
            Experiment.name,
            Experiment.status,
            Experiment.targeting_rules,
        )
        .filter(
            Experiment.status.in_(REFERENCING_EXPERIMENT_STATUSES),
            sa.cast(Experiment.targeting_rules, sa.Text).ilike(pattern),
        )
        .order_by(Experiment.key)
        .all()
    )
    flags = [
        {"id": str(row.id), "key": row.key, "name": row.name}
        for row in flag_rows
        if _references(row.targeting_rules, sid)
    ]
    experiments = [
        {
            "id": str(row.id),
            "key": row.key,
            "name": row.name,
            "status": getattr(row.status, "value", row.status),
        }
        for row in experiment_rows
        if _references(row.targeting_rules, sid)
    ]
    return flags, experiments


def segment_in_use_detail(
    flags: List[Dict[str, Any]], experiments: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """The 409 ``detail`` for archiving or deactivating a segment in use."""
    parts = []
    if flags:
        parts.append(f"{len(flags)} feature flag{'s' if len(flags) != 1 else ''}")
    if experiments:
        parts.append(
            f"{len(experiments)} experiment{'s' if len(experiments) != 1 else ''}"
        )
    return {
        "code": "segment_in_use",
        "message": (
            f"This segment is used by {' and '.join(parts)}. Remove it from their "
            "targeting rules first."
        ),
        "feature_flags": flags,
        "experiments": experiments,
    }
