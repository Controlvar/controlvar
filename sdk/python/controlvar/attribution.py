"""The keys a key-less ``track()`` is sent to, kept apart from the TTL value cache.

Every successful answer (an assignment, ``assigned=False`` included, or a flag
evaluation, local or from the server) records its key for the user. A key-less
event goes to each key recorded for its user, so a purchase made after the cache
entry expired still reaches the experiment the user was answered for.
``clear_cache()`` does not touch this record: a user's sticky assignments do not
change when their attributes do.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Dict, FrozenSet, List, Optional, Tuple

from . import cache

__all__ = ["ATTRIBUTION_HORIZON_SECONDS", "LIFECYCLE_REASONS", "MAX_ATTRIBUTED_USERS", "AttributedKeys"]

#: Users kept per client instance; the user answered least recently is dropped first.
MAX_ATTRIBUTED_USERS = 10_000

#: A key is used for this long after its latest successful answer, then dropped.
ATTRIBUTION_HORIZON_SECONDS = 24 * 60 * 60

#: An experiment whose latest answer gave one of these reasons is not running, so a
#: key-less event is not sent to it. Other reasons (``holdout``,
#: ``mutual_exclusion``, ``targeting``) are kept: holdout results count those
#: users' events.
LIFECYCLE_REASONS: FrozenSet[str] = frozenset({"draft", "paused", "completed", "archived"})

# key -> (answered_at, reason); dict order is first-answer order.
_Answers = Dict[str, Tuple[float, Optional[str]]]


class AttributedKeys:
    """``user_id -> (experiment answers, flag answers)``, least recently answered user first.

    Time is ``controlvar.cache._now`` (monotonic), read at call time, so a test that
    patches it moves this record and the cache together.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users: "OrderedDict[str, Tuple[_Answers, _Answers]]" = OrderedDict()

    def record_experiment(self, user_id: str, experiment_key: str, reason: Optional[str] = None) -> None:
        self._record(user_id, experiment_key, reason, 0)

    def record_flag(self, user_id: str, flag_key: str) -> None:
        self._record(user_id, flag_key, None, 1)

    def forget_experiment(self, user_id: str, experiment_key: str) -> None:
        """Drop one experiment's key for the user: the server answered its assignment 404, so
        a key-less event must not reach it (a server that answers 404 for an experiment that is
        not running would otherwise credit the user's stored arm while the user is shown
        control)."""
        with self._lock:
            answers = self._users.get(user_id)
            if answers is None:
                return
            answers[0].pop(experiment_key, None)
            if not answers[0] and not answers[1]:
                del self._users[user_id]

    def keys(self, user_id: str) -> Tuple[List[str], List[str]]:
        """The user's experiment keys and flag keys within the horizon, each in the
        order they were first answered. Keys past the horizon are dropped here."""
        with self._lock:
            answers = self._users.get(user_id)
            if answers is None:
                return [], []
            now = cache._now()
            experiments, flags = _live(answers[0], now), _live(answers[1], now)
            if not answers[0] and not answers[1]:
                del self._users[user_id]
            return experiments, flags

    def __len__(self) -> int:
        with self._lock:
            return len(self._users)

    def _record(self, user_id: str, key: str, reason: Optional[str], kind: int) -> None:
        with self._lock:
            answers = self._users.get(user_id)
            if answers is None:
                answers = ({}, {})
                self._users[user_id] = answers
            else:
                self._users.move_to_end(user_id)  # now the most recently answered user
            # Assigning an existing key keeps its place: the order stays first-answer order.
            answers[kind][key] = (cache._now(), reason)
            while len(self._users) > MAX_ATTRIBUTED_USERS:
                self._users.popitem(last=False)


def _live(answers: _Answers, now: float) -> List[str]:
    out: List[str] = []
    for key, (answered_at, reason) in list(answers.items()):
        if now - answered_at > ATTRIBUTION_HORIZON_SECONDS:
            del answers[key]
        elif reason not in LIFECYCLE_REASONS:
            out.append(key)
    return out
