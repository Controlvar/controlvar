"""Balance the split within a group as users arrive (#448).

An experiment that declares ``balance_within`` names one top-level key of the
assign ``context`` and the values to balance within. Every new, eligible user
``AssignmentService.assign_user`` assigns to it is put in a *group* by
:func:`group_of`, and takes the next place in that group's sequence of
randomly permuted blocks:

* a block holds every variant as often as its share allows: with shares that
  are all multiples of 5, ``B = 100 / gcd(shares)`` places, at most 20
  (:func:`block_size`, :func:`block_composition`);
* the group's ``n``-th user (``n`` from 0, the group's *seq*) takes place
  ``n % B`` of block ``n // B``; each block's order is a permutation keyed on
  the experiment's server-side ``balance_seed`` (:func:`slot_variant`).

So every user's chance of each variant is still its share, and within a group
each variant's count is never more than ``B * w * (1 - w)`` users from its
share of the group, and exactly its share at every multiple of ``B``.

The seq comes from the row of ``balance_group_counters`` for the experiment and
the group, taken in a SAVEPOINT of the assignment's own transaction
(:func:`take_seq`): one ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``,
under a ``statement_timeout`` of :data:`BALANCE_STEP_TIMEOUT_MS`. The increment
commits with the assignment row, so a rolled-back assignment gives its place
back and the seqs of a group stay exactly ``0 .. next_seq - 1``.

When no seq can be had -- the step waited longer than the timeout, the
process already has :data:`MAX_BALANCED_WAITS` steps in progress, or the
experiment has no ``balance_seed`` -- the user is assigned by the hash, as an
experiment that balances nothing assigns them, and stored with their group and
no seq. Such a user is counted in ``experiment_balance_fallback_total`` by
reason and is outside the blocks.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.core.log_once import OnceLog
from backend.app.core.metrics import (
    experiment_balance_fallback_total,
    experiment_balance_waits,
)
from backend.app.models.balance_group_counter import BalanceGroupCounter
from backend.app.models.experiment import (
    new_balance_seed as models_new_balance_seed,
)

logger = logging.getLogger(__name__)

#: At most this many named groups; ``other`` and ``unknown`` come on top.
MAX_GROUPS = 20
#: A value longer than this (in characters) is never a named group: it is
#: ``other``. ``assignments.balance_group`` is ``VARCHAR(100)``.
MAX_VALUE_LENGTH = 100
#: The group of a value that is not listed (or cannot be a group).
OTHER = "other"
#: The group of a user whose ``context`` has no value for the key.
UNKNOWN = "unknown"
RESERVED_GROUPS = (OTHER, UNKNOWN)
#: The largest block: every share a multiple of 5.
MAX_BLOCK = 20

#: Keys whose values are a closed set, so the setting needs no list. A value
#: outside the set is ``other``; ``web_browser``'s own ``other`` is the
#: ``other`` group.
CLOSED_KEYS: Mapping[str, Tuple[str, ...]] = {
    "web_device": ("phone", "tablet", "desktop"),
    "web_visitor": ("new", "returning"),
    "web_browser": ("chrome", "edge", "firefox", "safari", "samsung", "opera", OTHER),
}

#: Counter steps a process runs at once; past it a user is assigned by the
#: hash at once (reason ``busy``).
MAX_BALANCED_WAITS = 4
#: The ``statement_timeout`` of the counter step, in milliseconds. Read on
#: every call, so a test may raise it inside its own process.
BALANCE_STEP_TIMEOUT_MS = 250

#: The reasons a user is assigned outside the blocks.
REASON_TIMEOUT = "timeout"
REASON_BUSY = "busy"
REASON_NO_SEED = "no_seed"
FALLBACK_REASONS = (REASON_TIMEOUT, REASON_BUSY, REASON_NO_SEED)

#: SQLSTATEs of a counter step that waited too long: the statement timeout
#: this step sets (57014), and a ``lock_timeout`` set elsewhere (55P03).
_STEP_TIMED_OUT = frozenset({"57014", "55P03"})

_permits = threading.BoundedSemaphore(MAX_BALANCED_WAITS)
_waits_lock = threading.Lock()
_waits = {"current": 0, "peak": 0}
_fallback_notes = OnceLog()


# ---------------------------------------------------------------------------
# The group
# ---------------------------------------------------------------------------


def named_groups(setting: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """The groups a setting names: its ``values``, or a closed key's set.

    ``other`` and ``unknown`` are never among them. A setting with no
    attribute (or none at all) names nothing.
    """
    if not isinstance(setting, Mapping):
        return ()
    attribute = setting.get("attribute")
    if not isinstance(attribute, str):
        return ()
    values = setting.get("values")
    if values is None:
        values = CLOSED_KEYS.get(attribute, ())
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(
        value
        for value in values
        if isinstance(value, str) and value not in RESERVED_GROUPS
    )


def _storable(value: str) -> bool:
    """True when PostgreSQL can store *value* as text: no NUL character and
    nothing UTF-8 cannot encode (a lone surrogate)."""
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def group_of(setting: Optional[Mapping[str, Any]], context: Any) -> str:
    """The group a user with *context* is balanced in. Total: never raises.

    Read from the top-level key ``setting["attribute"]`` of *context* only (no
    aliasing: ``{"user": {"tier": ...}}`` and ``{"user.tier": ...}`` do not
    answer ``tier``):

    * missing, null or ``""``: ``unknown``;
    * a bool is ``"true"`` or ``"false"`` (tested before int, which a bool is
      in Python); an int is its decimal text; a float, a list or an object is
      ``other``;
    * a string is matched exactly (no case folding, no trimming): a named
      group is itself, ``"unknown"`` is ``unknown``, anything else -- an
      unlisted value, one over 100 characters, one PostgreSQL cannot store --
      is ``other``.
    """
    try:
        return _group_of(setting, context)
    except Exception:  # pragma: no cover - defensive: the rules above are total
        return OTHER


def _group_of(setting: Optional[Mapping[str, Any]], context: Any) -> str:
    if not isinstance(setting, Mapping):
        return UNKNOWN
    attribute = setting.get("attribute")
    if not isinstance(attribute, str) or not isinstance(context, Mapping):
        return UNKNOWN
    if attribute not in context:
        return UNKNOWN
    value = context[attribute]
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        text_value = "true" if value else "false"
    elif isinstance(value, int):
        text_value = str(value)
    elif isinstance(value, str):
        text_value = value
    else:
        return OTHER
    if text_value == "":
        return UNKNOWN
    if text_value == UNKNOWN:
        return UNKNOWN
    if text_value == OTHER:
        return OTHER
    if len(text_value) > MAX_VALUE_LENGTH or not _storable(text_value):
        return OTHER
    if text_value in named_groups(setting):
        return text_value
    return OTHER


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def effective_shares(allocations: Sequence[Optional[int]]) -> List[int]:
    """Each variant's share as the hash path takes it, in variant order.

    ``_hash_user_to_variant`` walks the cumulative allocations over a bucket
    of 0-99 and gives the tail to the last variant, so a share is what is left
    of 100 when its turn comes, and the last variant also takes whatever the
    others leave. When the allocations sum to 100 (create and update require
    it) these are the allocations themselves.
    """
    shares: List[int] = []
    cumulative = 0
    for allocation in allocations:
        before = min(cumulative, 100)
        cumulative += int(allocation or 0)
        shares.append(max(0, min(cumulative, 100) - before))
    if shares:
        shares[-1] += 100 - sum(shares)
    return shares


def block_size(shares: Sequence[int]) -> int:
    """``100 / gcd`` of the positive shares: the places in one block."""
    positive = [share for share in shares if share > 0]
    if not positive:
        return 1
    divisor = 0
    for share in positive:
        divisor = math.gcd(divisor, share)
    return 100 // math.gcd(divisor, 100)


def block_composition(shares: Sequence[int]) -> List[int]:
    """A block's places before it is permuted: each variant's index, as many
    times as its share of the block, in variant order. A zero share has none."""
    size = block_size(shares)
    composition: List[int] = []
    for index, share in enumerate(shares):
        composition.extend([index] * (share * size // 100))
    return composition


def _block_order(seed: str, group: str, block: int, size: int) -> List[int]:
    """The places of block *block* of *group*, in the order users take them.

    Sorted by ``(HMAC-SHA256(seed, f"{group}\\x1f{block}\\x1f{j}"), j)``.
    """
    key = seed.encode("utf-8")

    def rank(j: int) -> Tuple[bytes, int]:
        message = f"{group}\x1f{block}\x1f{j}".encode("utf-8")
        return hmac.new(key, message, hashlib.sha256).digest(), j

    return sorted(range(size), key=rank)


def slot_variant(seed: str, group: str, seq: int, shares: Sequence[int]) -> int:
    """The index of the variant the group's *seq*-th user takes."""
    composition = block_composition(shares)
    size = len(composition)
    block, position = divmod(seq, size)
    return composition[_block_order(seed, group, block, size)[position]]


def group_sequence(
    seed: str, group: str, shares: Sequence[int], count: int
) -> List[int]:
    """The variant indices of the group's first *count* users, in seq order:
    :func:`slot_variant` for seq 0 .. count - 1, each block ordered once."""
    composition = block_composition(shares)
    size = len(composition)
    sequence: List[int] = []
    block = 0
    while len(sequence) < count:
        order = _block_order(seed, group, block, size)
        sequence.extend(composition[j] for j in order)
        block += 1
    return sequence[:count]


#: A fresh per-experiment block-order key: 32 random bytes as hex. Made by the
#: ``Experiment`` mapper listeners, the one place every writer passes through.
new_balance_seed = models_new_balance_seed


# ---------------------------------------------------------------------------
# What a balanced experiment refuses
# ---------------------------------------------------------------------------

#: R6: a bandit moves traffic between variants, so it cannot keep a split.
BANDIT_REFUSAL = (
    "A bandit experiment cannot balance its split within groups, because it "
    "moves traffic between variants as results come in. Turn one of them off."
)
#: R10, with the experiment's own split filled in.
SPLIT_REFUSAL = (
    "To balance within groups, every share must be a multiple of 5, such as "
    "50/50, 20/80, 35/35/30 or 40/30/30; {split} is not."
)
#: R12: the edge router sends no user attributes.
SPLIT_URL_REFUSAL = (
    "Split URL experiments cannot balance within groups: the edge router "
    "sends no user attributes."
)


def split_text(allocations: Sequence[Optional[int]]) -> str:
    """An experiment's split as R10 names it: ``34/33/33``."""
    return "/".join(str(int(allocation or 0)) for allocation in allocations)


def balance_problem(
    balance_within: Any,
    *,
    optimization_type: Any,
    experiment_type: Any,
    allocations: Sequence[Optional[int]],
) -> Optional[str]:
    """Why an experiment with these settings cannot balance, or None.

    Judged on the experiment as it would be stored, so create, update and
    ``/start`` ask the same question. Nothing is refused when
    *balance_within* is empty. In order: a split URL experiment (R12), a
    bandit (R6: the ``bandit`` type, or any optimization but ``fixed``), a
    split with a share that is not a multiple of 5 (R10).
    """
    if not balance_within:
        return None
    kind = str(getattr(experiment_type, "value", experiment_type))
    if kind == "split_url":
        return SPLIT_URL_REFUSAL
    optimization = str(getattr(optimization_type, "value", optimization_type) or "")
    if kind == "bandit" or optimization not in ("", "fixed"):
        return BANDIT_REFUSAL
    shares = effective_shares(allocations)
    if any(share % 5 for share in shares):
        return SPLIT_REFUSAL.format(split=split_text(allocations))
    return None


# ---------------------------------------------------------------------------
# The counter step
# ---------------------------------------------------------------------------


def _sqlstate(exc: BaseException) -> Optional[str]:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)


def _wait_started() -> None:
    with _waits_lock:
        _waits["current"] += 1
        _waits["peak"] = max(_waits["peak"], _waits["current"])
        experiment_balance_waits.set(_waits["current"])


def _wait_ended() -> None:
    with _waits_lock:
        _waits["current"] -= 1
        experiment_balance_waits.set(_waits["current"])


def balanced_waits() -> Dict[str, int]:
    """The counter steps in progress in this process now, and the most at
    once since :func:`reset_balanced_wait_peak`."""
    with _waits_lock:
        return dict(_waits)


def reset_balanced_wait_peak() -> None:
    """Start counting the most steps at once from the steps in progress now."""
    with _waits_lock:
        _waits["peak"] = _waits["current"]


def take_seq(
    db: Session, experiment_id: Any, group: str
) -> Tuple[Optional[int], Optional[str]]:
    """The group's next seq, or why there is none: ``(seq, None)`` or
    ``(None, reason)``.

    Runs in the caller's transaction, which commits the increment with the
    assignment row (and rolls it back with it). The process's permit is taken
    before the statement, without waiting: none free is ``busy``. In a
    SAVEPOINT, the ``statement_timeout`` is set for the upsert alone; a step
    that runs past it (or past a ``lock_timeout`` set elsewhere) is rolled
    back to the savepoint, which also undoes the setting and leaves no row,
    and is ``timeout``. Any other database error propagates.
    """
    if not _permits.acquire(blocking=False):
        return None, REASON_BUSY
    _wait_started()
    try:
        counter = BalanceGroupCounter.__table__
        upsert = (
            pg_insert(counter)
            .values(experiment_id=experiment_id, balance_group=group, next_seq=1)
            .on_conflict_do_update(
                index_elements=[counter.c.experiment_id, counter.c.balance_group],
                set_={"next_seq": counter.c.next_seq + 1},
            )
            .returning(counter.c.next_seq - 1)
        )
        try:
            with db.begin_nested():
                db.execute(
                    text(
                        f"SET LOCAL statement_timeout = {int(BALANCE_STEP_TIMEOUT_MS)}"
                    )
                )
                seq = db.execute(upsert).scalar_one()
                db.execute(text("SET LOCAL statement_timeout TO DEFAULT"))
        except OperationalError as exc:
            if _sqlstate(exc) in _STEP_TIMED_OUT:
                return None, REASON_TIMEOUT
            raise
        return int(seq), None
    finally:
        _wait_ended()
        _permits.release()


def note_fallback(experiment_id: Any, reason: str) -> None:
    """Count a user assigned outside the blocks, and log it once per process
    per experiment (the experiment id only: never a user or a value)."""
    experiment_balance_fallback_total.labels(reason=reason).inc()
    if _fallback_notes.first(str(experiment_id), "balance fallback"):
        logger.warning(
            "Experiment %s: a new user was assigned by hash instead of by its "
            "group's block (%s); experiment_balance_fallback_total counts "
            "every such user by reason. Logged once.",
            experiment_id,
            reason,
        )
