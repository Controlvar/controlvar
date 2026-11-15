"""The key that recognises an event sent again with the same ``event_id`` (#1543).

An event sent to ``/tracking/track``, ``/tracking/batch`` or
``/tracking/events`` may carry an ``event_id``. The server stores the first
event it receives for a given user, experiment, flag, event name and
``event_id``; a later one with the same five values is answered as a duplicate
and writes nothing (``EventService.track_event_once``).

:func:`dedup_key` is the one place that key is computed. Every route calls it,
through ``track_event_once``, on the event it built: the experiment and flag
the request resolved to when it was received, and the event name after
``/track`` and ``/batch`` default it to the event type. The user is not part of
the key: it is the other half of ``event_dedup_keys``' primary key.

A key is never recomputed: not when a flag is deleted (``events.
feature_flag_id`` becomes NULL), not when an event is re-tagged, and not when
an experiment changes status.

The parts are length-prefixed and hashed, so the key has a fixed length
whatever the text (a 255-character ``event_id`` of four-byte characters
included) and no two different tuples of parts can encode to the same bytes:
``("ab", "c")`` and ``("a", "bc")`` differ, which a separator would not
guarantee.
"""

from __future__ import annotations

import hashlib
from typing import Optional, Union
from uuid import UUID

#: The length of every key: a SHA-256 digest as lowercase hex.
DEDUP_KEY_LENGTH = 64


def _id_text(value: Optional[Union[UUID, str]]) -> str:
    """An experiment or flag id as its canonical lowercase 36-character text, or ``""``."""
    if value is None:
        return ""
    return str(value if isinstance(value, UUID) else UUID(str(value)))


def _encoded(part: str) -> bytes:
    """``<byte length>:<UTF-8 bytes>``: the length is ASCII decimal digits."""
    data = part.encode("utf-8")
    return str(len(data)).encode("ascii") + b":" + data


def dedup_key(
    experiment_id: Optional[Union[UUID, str]],
    feature_flag_id: Optional[Union[UUID, str]],
    event_name: str,
    event_id: str,
) -> str:
    """The 64-character key of one event within its user.

    The lowercase SHA-256 hex digest of four parts, each encoded as
    ``str(len(b)).encode("ascii") + b":" + b`` where ``b`` is its UTF-8 bytes,
    concatenated in this order:

    1. the experiment id as its canonical lowercase 36-character text, or ``""``;
    2. the flag id, the same way;
    3. ``event_name``;
    4. ``event_id``.
    """
    digest = hashlib.sha256()
    for part in (
        _id_text(experiment_id),
        _id_text(feature_flag_id),
        event_name,
        event_id,
    ):
        digest.update(_encoded(part))
    return digest.hexdigest()
