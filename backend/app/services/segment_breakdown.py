"""
The rows of a segment breakdown: each assigned user in exactly one segment (#1776).

Both breakdown routes build their rows here, so they place and count users the
same way: ``GET /api/v1/results/{experiment_id}?breakdown=<dimension>`` and
``GET /api/v1/experiments/{experiment_id}/segmented-results/{segment_by}``.

A user's segment is the value the user was first assigned with, or else the
first value tracked for them, and ``unknown`` when there is none:

* A user's value is the text of ``jsonb_extract_path_text(event_metadata,
  dimension)`` -- a top-level key, so ``a.b`` is not traversed -- on the
  user's first event of the experiment that carries one.  An event carries a
  value when that text is neither NULL nor ``''``.  The experiment's events
  count whatever variant they are tagged with, untagged ones included; events
  of other experiments never do.
* "First" puts exposures first: ``/tracking/assign`` stores the request's
  context as the exposure's metadata, so that is the context the user was
  assigned with.  Then the time the server stored the event (``updated_at``,
  set once on insert, which the client cannot set), then the event id,
  compared as a uuid.  The client's ``created_at`` is never read, so a client
  timestamp cannot move a user's segment.
* A value longer than ``MAX_VALUE_LENGTH`` characters is counted under
  ``other``.  The first carrying event decides: a long first value is not
  skipped for a later short one.

The ``MAX_LISTED_SEGMENTS`` values with the most users (all variants together)
are listed, by users descending and then by value; every other value is
counted under ``other``, listed after them, and a user with no value under
``unknown``, listed last.  A literal ``other`` or ``unknown`` value joins that
row.  A row with no users is not listed.

A user's conversions count in their segment when ``converting_user_ids``
contains the user: the converting users the headline counts.  So for every
variant the rows' users and converting users add up to the headline's.

No statement here reads both ``events`` and ``assignments``: the assignments
come from ``event_matching.assignment_times``, the converters from
``event_matching.converting_user_ids`` and the values from one ``events``
statement, and they are put together in Python (see
``event_matching._assigned_pairs`` for why, #233).
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Container, Dict, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import bindparam, case, func
from sqlalchemy.orm import Session

from backend.app.models.event import Event
from backend.app.services.event_matching import (
    EXPOSURE_EVENT_TYPES,
    assignment_times,
    converting_user_ids,
)

#: How many values a breakdown lists by themselves; the rest are counted under
#: ``OTHER``.  The dashboard's breakdown caption ("Values beyond the 20 with
#: the most users are counted together under other.") states this number too:
#: change both together.
MAX_LISTED_SEGMENTS = 20

#: The longest value, in characters (PostgreSQL ``length``, not bytes), that is
#: listed as itself.  A longer value is counted under ``OTHER``.
MAX_VALUE_LENGTH = 255

#: The row of the users with no value.
UNKNOWN = "unknown"

#: The row of the values that are not listed by themselves.
OTHER = "other"

#: The 400 detail both routes answer for a dimension containing ``\x00``.
NUL_IN_DIMENSION = "The dimension must not contain a NUL character."


@dataclass
class Segment:
    """One row of a breakdown.

    ``users`` maps each variant id with assigned users to how many of them are
    in this segment (zeros included); ``converters`` maps each requested event
    name to the same per-variant count of converting users.
    """

    value: str
    users: Dict[str, int] = field(default_factory=dict)
    converters: Dict[Optional[str], Dict[str, int]] = field(default_factory=dict)


def user_values(db: Session, experiment_id: Any, dimension: str) -> Dict[str, str]:
    """
    Each user's value of ``dimension`` in the experiment, in one statement.

    Maps the user id to the text of the user's first event that carries a
    value (the order is in the module docstring), or to ``OTHER`` when that
    text is longer than ``MAX_VALUE_LENGTH`` characters.  A user with no such
    event is absent.  Every user with a keyed event of the experiment is
    returned, assigned or not; the caller keeps the assigned ones.
    """
    value = func.jsonb_extract_path_text(
        Event.event_metadata, bindparam("dimension", dimension)
    )
    listed_value = case(
        (func.length(value) > bindparam("max_value_length", MAX_VALUE_LENGTH), OTHER),
        else_=value,
    )
    exposures_first = case((Event.event_type.in_(EXPOSURE_EVENT_TYPES), 0), else_=1)
    rows = (
        db.query(Event.user_id, listed_value)
        .filter(
            Event.experiment_id == experiment_id,
            value.isnot(None),
            value != "",
        )
        .order_by(Event.user_id, exposures_first, Event.updated_at, Event.id)
        .distinct(Event.user_id)
        .all()
    )
    return {str(user_id): text for user_id, text in rows}


def listed_values(values: Iterable[Optional[str]]) -> List[str]:
    """
    The values a breakdown lists by themselves, in the order it lists them.

    ``values`` holds one entry per user.  The ``MAX_LISTED_SEGMENTS`` values
    other than ``UNKNOWN`` and ``OTHER`` with the most users, by users
    descending and then by value (Python ``str`` order), so of equally common
    values the larger ones are counted under ``OTHER``.
    """
    users = Counter(v for v in values if v is not None and v not in (UNKNOWN, OTHER))
    ranked = sorted(users.items(), key=lambda item: (-item[1], item[0]))
    return [value for value, _ in ranked[:MAX_LISTED_SEGMENTS]]


def segment_of(value: Optional[str], listed: Container[str]) -> str:
    """The row of a user whose value is ``value`` (None for no value), given
    the listed values: ``UNKNOWN``, the value itself, or ``OTHER``."""
    if value is None or value == UNKNOWN:
        return UNKNOWN
    if value in listed:
        return value
    return OTHER


def fold(values: Mapping[str, Optional[str]]) -> Tuple[Dict[str, str], List[str]]:
    """
    Each user's row, and the rows in the order a breakdown lists them.

    ``values`` maps every user to their value (None for a user with none).
    The order is the listed values (``listed_values``), then ``OTHER``, then
    ``UNKNOWN``, each only when it has a user.
    """
    listed = listed_values(values.values())
    listed_set = set(listed)
    row_of = {user: segment_of(value, listed_set) for user, value in values.items()}
    present = set(row_of.values())
    order = listed + [special for special in (OTHER, UNKNOWN) if special in present]
    return row_of, order


def segment_rows(
    db: Session,
    experiment_id: Any,
    dimension: str,
    event_names: Iterable[Optional[str]],
) -> List[Segment]:
    """
    The breakdown of ``experiment_id`` by ``dimension``, one row per segment.

    Every user assigned to the experiment is in exactly one row; a row with no
    users is not listed, and an experiment with no assignments has no rows.
    For each name in ``event_names`` (``None`` is the legacy ``event_type =
    'conversion'`` convention, as in ``conversion_event_filter``) each row
    counts the variant's converting users in that segment, the users
    ``converting_user_ids`` returns.

    The rule is in the module docstring.  It is applied here, before any
    per-row statistics, cache write or snapshot, so both routes list at most
    ``MAX_LISTED_SEGMENTS`` values plus ``OTHER`` and ``UNKNOWN``.
    """
    assigned = assignment_times(db, experiment_id)
    variant_of = {
        user_id: variant_id
        for variant_id, users in assigned.items()
        for user_id in users
    }
    if not variant_of:
        return []

    values = user_values(db, experiment_id, dimension)
    row_of, order = fold({user_id: values.get(user_id) for user_id in variant_of})

    variants = sorted(assigned)
    rows = {row: Segment(value=row, users=dict.fromkeys(variants, 0)) for row in order}
    for user_id, row in row_of.items():
        rows[row].users[variant_of[user_id]] += 1

    for name in dict.fromkeys(event_names):
        for segment in rows.values():
            segment.converters[name] = dict.fromkeys(variants, 0)
        for variant_id in variants:
            for user_id in converting_user_ids(db, experiment_id, variant_id, name):
                # A converter is assigned to variant_id, so it has a row
                # unless it was assigned after the assignments were read.
                row = row_of.get(user_id)
                if row is not None:
                    rows[row].converters[name][variant_id] += 1

    return list(rows.values())
