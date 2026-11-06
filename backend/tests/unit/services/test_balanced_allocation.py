"""The group, the block and the refusals of a balanced experiment (#448).

No database: :mod:`backend.app.core.balanced_blocks`, the pure parts of the
allocation, and the block-order key the ``Experiment`` listeners make.

* The label table: SPEC A's inputs, one parametrised case each, and the group
  of every generated JSON value is a named group, ``other`` or ``unknown``,
  without raising.
* The bound: over every split whose shares are multiples of 5 (the 627
  partitions of 100 into fives), each in four orders and once with a zero
  share added, every prefix of a group's first 500 users keeps each
  variant's count within ``B * w * (1 - w)`` of its share, and exactly on it
  at every multiple of ``B``; a zero share gets nobody.
* Each user's chance of each variant is still its share: the first user of a
  group, over many experiments' block-order keys, lands on each variant at
  its share.
* The block order of a fixed key, against literals typed in once: the same
  in every release.
* The refusals, verbatim: a split that is not in steps of 5 (``34/33/33``,
  ``1/99``), a bandit, a split URL experiment.
"""

from __future__ import annotations

import math
import random
import string
from typing import Iterator, List, Sequence, Tuple

import numpy as np
import pytest

from backend.app.core import balanced_blocks as ba
from backend.app.models.experiment import new_balance_seed

pytestmark = pytest.mark.unit

TIER = {"attribute": "tier", "values": ["gold", "silver", "3", "true"]}
DEVICE = {"attribute": "web_device"}
BROWSER = {"attribute": "web_browser"}
SEED = "5" * 64


# ---------------------------------------------------------------------------
# The label table (SPEC A)
# ---------------------------------------------------------------------------

LABELS = [
    # a listed string is its group
    (TIER, {"tier": "gold"}, "gold"),
    # exact text: no case folding, no trimming
    (TIER, {"tier": "Gold"}, "other"),
    (TIER, {"tier": " gold"}, "other"),
    # an unlisted string
    (TIER, {"tier": "bronze"}, "other"),
    # an int is its decimal text
    (TIER, {"tier": 3}, "3"),
    (TIER, {"tier": "3"}, "3"),
    # a float, even when its text could match
    (TIER, {"tier": 3.0}, "other"),
    (TIER, {"tier": 1e2}, "other"),
    # a bool is "true" or "false", tested before int
    (TIER, {"tier": True}, "true"),
    (TIER, {"tier": False}, "other"),
    # a list or an object
    (TIER, {"tier": ["gold"]}, "other"),
    (TIER, {"tier": {"name": "gold"}}, "other"),
    # over 100 characters
    (TIER, {"tier": "g" * 101}, "other"),
    # text PostgreSQL cannot store
    (TIER, {"tier": "a\u0000b"}, "other"),
    (TIER, {"tier": "\ud800"}, "other"),
    # empty, null or missing
    (TIER, {"tier": ""}, "unknown"),
    (TIER, {"tier": None}, "unknown"),
    (TIER, {}, "unknown"),
    (TIER, None, "unknown"),
    # the literal names merge into those groups
    (TIER, {"tier": "unknown"}, "unknown"),
    (TIER, {"tier": "other"}, "other"),
    # top-level key only; no expand_context
    (TIER, {"user": {"tier": "gold"}}, "unknown"),
    (TIER, {"user.tier": "gold"}, "unknown"),
    # a closed key needs no list
    (DEVICE, {"web_device": "phone"}, "phone"),
    (DEVICE, {"web_device": "tv"}, "other"),
    (BROWSER, {"web_browser": "other"}, "other"),
    (BROWSER, {"web_browser": "firefox"}, "firefox"),
]


@pytest.mark.parametrize(
    ("setting", "context", "group"),
    LABELS,
    ids=[f"{s['attribute']}:{c!r:.40}" for s, c, _ in LABELS],
)
def test_the_label_table(setting, context, group):
    found = ba.group_of(setting, context)
    assert found == group, f"{context!r}: {found} != {group}"


def test_the_named_groups_never_include_other_or_unknown():
    assert ba.named_groups(TIER) == ("gold", "silver", "3", "true")
    assert ba.named_groups(DEVICE) == ("phone", "tablet", "desktop")
    assert ba.named_groups(BROWSER) == (
        "chrome",
        "edge",
        "firefox",
        "safari",
        "samsung",
        "opera",
    )


def _json_values(rng: random.Random, depth: int = 0) -> object:
    """A random JSON-ish value: every type, nesting, lengths 0-300, NUL and
    lone surrogates."""
    kind = rng.randrange(10 if depth < 3 else 8)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.choice([0, 3, -7, 10**30, rng.randrange(-(10**6), 10**6)])
    if kind == 3:
        return rng.choice([3.0, 1e2, float("inf"), float("nan"), rng.random()])
    if kind in (4, 5, 6):
        alphabet = string.printable + "\u0000𐏿é\U0001f600"
        length = rng.choice([0, 1, 4, 99, 100, 101, 255, 300])
        return "".join(rng.choice(alphabet) for _ in range(length))
    if kind == 7:
        return rng.choice(["gold", "silver", "3", "true", "unknown", "other", ""])
    if kind == 8:
        return [_json_values(rng, depth + 1) for _ in range(rng.randrange(3))]
    return {
        rng.choice(["tier", "name", "user"]): _json_values(rng, depth + 1)
        for _ in range(rng.randrange(3))
    }


@pytest.mark.parametrize(
    "setting", [TIER, DEVICE, BROWSER], ids=lambda s: s["attribute"]
)
def test_every_generated_value_has_exactly_one_group(setting):
    rng = random.Random(448)
    allowed = set(ba.named_groups(setting)) | {"other", "unknown"}
    for _ in range(4000):
        value = _json_values(rng)
        context = rng.choice(
            [
                {setting["attribute"]: value},
                value,
                {"user": {setting["attribute"]: value}},
            ]
        )
        group = ba.group_of(setting, context)
        assert group in allowed, (context, group)


# ---------------------------------------------------------------------------
# The block and the bound (SPEC A)
# ---------------------------------------------------------------------------


def _partitions(total: int, largest: int) -> Iterator[Tuple[int, ...]]:
    """Every multiset of positive integers summing to *total*, parts at most
    *largest*, in non-increasing order."""
    if total == 0:
        yield ()
        return
    for part in range(min(total, largest), 0, -1):
        for rest in _partitions(total - part, part):
            yield (part, *rest)


#: Every split of 100 whose shares are multiples of 5.
SPLITS = [tuple(5 * part for part in parts) for parts in _partitions(20, 20)]


def test_there_are_627_splits_in_steps_of_5():
    assert len(SPLITS) == 627


@pytest.mark.parametrize(
    ("shares", "size"),
    [
        ((50, 50), 2),
        ((20, 80), 5),
        ((20, 30, 50), 10),
        ((40, 30, 30), 10),
        ((35, 35, 30), 20),
    ],
)
def test_block_sizes(shares, size):
    assert ba.block_size(shares) == size
    assert sorted(ba.block_composition(shares)) == ba.block_composition(shares)
    assert len(ba.block_composition(shares)) == size


def _orders(shares: Tuple[int, ...], rng: random.Random) -> List[Tuple[int, ...]]:
    ascending = tuple(sorted(shares))
    shuffled = list(shares)
    rng.shuffle(shuffled)
    again = list(shares)
    rng.shuffle(again)
    with_zero = list(shares)
    with_zero.insert(rng.randrange(len(with_zero) + 1), 0)
    return [shares, ascending, tuple(shuffled), tuple(again), tuple(with_zero)]


def _check_bound(shares: Sequence[int], seed: str, group: str, count: int) -> None:
    size = ba.block_size(shares)
    assert size <= ba.MAX_BLOCK, (shares, size)
    sequence = np.array(ba.group_sequence(seed, group, shares, count))
    n = np.arange(1, count + 1)
    for index, share in enumerate(shares):
        name = string.ascii_lowercase[index % 26]
        w = share / 100
        counts = np.cumsum(sequence == index)
        if share == 0:
            assert int(counts[-1]) == 0, f"variant {name}: {int(counts[-1])} != 0"
            continue
        gap = np.abs(counts - w * n)
        bound = size * w * (1 - w) + 1e-9
        worst = int(np.argmax(gap))
        assert gap[worst] <= bound, (
            f"{shares}: variant {name} at n={worst + 1}: |{int(counts[worst])} - "
            f"{w * (worst + 1):.2f}| > {bound:.2f}"
        )
        ends = gap[size - 1 :: size]
        assert np.all(ends < 1e-9), (
            f"{shares}: variant {name} off its share at a block end"
        )


def _seed(rng: random.Random) -> str:
    """A block-order key from *rng*: the same keys on every run."""
    return f"{rng.getrandbits(256):064x}"


def test_the_bound_holds_for_every_split_in_steps_of_5():
    rng = random.Random(4480)
    cases = 0
    for shares in SPLITS:
        for order in _orders(shares, rng):
            seed = _seed(rng)
            _check_bound(order, seed, rng.choice(["gold", "unknown", "x"]), 500)
            cases += 1
    assert cases == 627 * 5


def test_a_zero_share_gets_nobody():
    _check_bound((0, 50, 50), SEED, "gold", 500)
    _check_bound((50, 0, 50), SEED, "gold", 500)


def test_each_users_chance_is_their_share():
    """Over 4,000 block-order keys, the first user of a group takes each
    variant at its share (a two-sided 4-sigma band). The keys come from a
    fixed generator, so the answer is the same on every run."""
    rng = random.Random(44800)
    shares = (20, 30, 50)
    keys = 4000
    hits = [0, 0, 0]
    for _ in range(keys):
        hits[ba.slot_variant(_seed(rng), "gold", 0, shares)] += 1
    for index, share in enumerate(shares):
        p = share / 100
        z = (hits[index] - keys * p) / math.sqrt(keys * p * (1 - p))
        assert abs(z) < 4, (index, hits, z)


def test_the_order_depends_on_the_key_and_the_group():
    shares = (50, 50)
    one = ba.group_sequence(SEED, "gold", shares, 64)
    assert one == ba.group_sequence(SEED, "gold", shares, 64)
    assert one != ba.group_sequence("6" * 64, "gold", shares, 64)
    assert one != ba.group_sequence(SEED, "silver", shares, 64)
    assert [ba.slot_variant(SEED, "gold", n, shares) for n in range(64)] == one


#: A block-order key used only by the fixed vectors below.
VECTOR_KEY = "0123456789abcdef" * 4

#: The variant index each of a group's first 40 users takes under
#: VECTOR_KEY, typed in once. Never regenerate them from the code.
ORDER_VECTORS = [
    ((50, 50), "gold", "0101101010010101100110100110100101100110"),
    ((50, 50), "unknown", "0110101010101010101001101010011010100101"),
    ((20, 30, 50), "gold", "2102221201222101212021202102121011222220"),
    ((20, 30, 50), "unknown", "2022102211122112202010220212211222020121"),
]


@pytest.mark.parametrize(
    ("shares", "group", "expected"),
    ORDER_VECTORS,
    ids=[f"{'/'.join(map(str, s))}-{g}" for s, g, _ in ORDER_VECTORS],
)
def test_the_block_order_stays_the_same_across_releases(shares, group, expected):
    """The places a key gives a group stay the same from one release to the
    next. A running experiment's groups are part-way through a block when the
    server is upgraded, and during a rolling deploy two releases assign at
    once: if the order of a given key, group and block changed, both would
    shuffle the current block of every running balanced experiment. When this
    fails, restore the order; do not edit these literals."""
    by_slot = "".join(
        str(ba.slot_variant(VECTOR_KEY, group, n, shares)) for n in range(40)
    )
    in_sequence = "".join(
        str(v) for v in ba.group_sequence(VECTOR_KEY, group, shares, 40)
    )
    assert by_slot == expected, f"{shares} {group}: {by_slot} != {expected}"
    assert in_sequence == expected, f"{shares} {group}: {in_sequence} != {expected}"


def test_the_effective_shares_are_the_hash_paths():
    assert ba.effective_shares([50, 50]) == [50, 50]
    assert ba.effective_shares([40, 40]) == [40, 60]
    assert ba.effective_shares([60, 60]) == [60, 40]
    assert ba.effective_shares([0, 50, 50]) == [0, 50, 50]


def test_a_seed_is_64_hex_characters_and_fresh():
    seeds = {new_balance_seed() for _ in range(50)}
    assert len(seeds) == 50
    assert all(len(s) == 64 and set(s) <= set("0123456789abcdef") for s in seeds)


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------

R10_34 = (
    "To balance within groups, every share must be a multiple of 5, such as "
    "50/50, 20/80, 35/35/30 or 40/30/30; 34/33/33 is not."
)
R10_1 = (
    "To balance within groups, every share must be a multiple of 5, such as "
    "50/50, 20/80, 35/35/30 or 40/30/30; 1/99 is not."
)
R6 = (
    "A bandit experiment cannot balance its split within groups, because it "
    "moves traffic between variants as results come in. Turn one of them off."
)
R12 = (
    "Split URL experiments cannot balance within groups: the edge router "
    "sends no user attributes."
)


def _problem(allocations, optimization="fixed", kind="a_b", setting=TIER):
    return ba.balance_problem(
        setting,
        optimization_type=optimization,
        experiment_type=kind,
        allocations=allocations,
    )


@pytest.mark.parametrize(
    ("allocations", "text"), [((34, 33, 33), R10_34), ((1, 99), R10_1)]
)
def test_a_split_not_in_steps_of_5_is_refused(allocations, text):
    assert _problem(allocations) == text, f"{allocations}: expected a refusal"


@pytest.mark.parametrize(
    "shares", SPLITS[:40] + [(35, 35, 30), (40, 30, 30), (0, 50, 50)]
)
def test_a_split_in_steps_of_5_is_accepted(shares):
    assert _problem(shares) is None


@pytest.mark.parametrize(
    "optimization", ["thompson_sampling", "ucb1", "epsilon_greedy"]
)
def test_a_bandit_is_refused(optimization):
    assert _problem((50, 50), optimization=optimization) == R6
    assert _problem((50, 50), kind="bandit") == R6


def test_a_split_url_experiment_is_refused():
    assert _problem((50, 50), kind="split_url") == R12


def test_nothing_is_refused_without_a_setting():
    assert _problem((34, 33, 33), "ucb1", "split_url", setting=None) is None


class _Stored:
    """An object standing in for a stored experiment."""

    def __init__(self, **values):
        self.__dict__.update(values)


@pytest.mark.parametrize(
    ("stored", "setting"),
    [
        (_Stored(balance_within=TIER), TIER),
        (_Stored(balance_within=None), None),
        (_Stored(balance_within={}), None),
        (_Stored(balance_within=["tier"]), None),
        (_Stored(balance_within="tier"), None),
        (_Stored(), None),
    ],
    ids=["object", "null", "empty", "list", "string", "no-attribute"],
)
def test_only_an_object_is_a_setting(stored, setting):
    """A value that is not an object balances nothing: the experiment is
    assigned by the hash, as one without the setting is."""
    assert ba.balance_setting(stored) == setting


@pytest.mark.regression
@pytest.mark.parametrize(
    "module",
    [
        "backend.app.schemas.experiment",
        "backend.app.schemas.balance_within",
        "backend.app.core.balanced_blocks",
        "backend.app.services.balanced_allocation",
    ],
)
def test_each_module_imports_first_in_a_fresh_interpreter(module):
    """The request schemas read the setting's rules from a module that imports
    neither the services nor the schemas package: imported first, nothing
    cycles back to a module half-initialised (``seed_streampulse`` imports
    ``backend.app.schemas.experiment`` first)."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    done = subprocess.run(
        [sys.executable, "-B", "-c", f"import {module}"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
