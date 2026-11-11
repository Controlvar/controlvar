"""What the stored rows of a balanced experiment must say, per group (#448).

"On rows, per group": for each group, the non-null ``balance_seq`` values are
exactly ``0 .. next_seq - 1`` (each once), and every such row's variant is the
block place the experiment's ``balance_seed`` gives that seq. A count of each
variant (``|T - C|``) can pass a defect whose duplicates cancel out; the seqs
cannot. The tests of ``balance_within`` share this, so each states the same
property the same way.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.core import balanced_blocks


@dataclass
class GroupRows:
    """One group's stored rows."""

    next_seq: Optional[int] = None
    seqs: List[int] = field(default_factory=list)
    #: variant name -> rows with a seq (in the blocks)
    in_blocks: Counter = field(default_factory=Counter)
    #: variant name -> rows with no seq (assigned by hash)
    outside: Counter = field(default_factory=Counter)


@dataclass
class BalanceRows:
    """Every group of one experiment, and what is wrong with them."""

    groups: Dict[Optional[str], GroupRows]
    problems: List[str]
    rows: int

    def gap(self, group: str, a: str, b: str) -> int:
        """``|n_a - n_b|`` among the group's rows in the blocks."""
        counts = self.groups[group].in_blocks
        return abs(counts[a] - counts[b])

    def outside_blocks(self) -> int:
        return sum(sum(g.outside.values()) for g in self.groups.values())


def balance_rows(session: Session, experiment_id) -> BalanceRows:
    """Read the experiment's assignments and counters, and check them."""
    experiment = session.execute(
        text("SELECT balance_seed FROM experiments WHERE id = :id"),
        {"id": experiment_id},
    ).one()
    seed = experiment.balance_seed
    variants = session.execute(
        text(
            "SELECT id, name, traffic_allocation FROM variants "
            "WHERE experiment_id = :id ORDER BY created_at, id"
        ),
        {"id": experiment_id},
    ).all()
    names = {str(v.id): v.name for v in variants}
    order = [v.name for v in variants]
    shares = balanced_blocks.effective_shares([v.traffic_allocation for v in variants])

    groups: Dict[Optional[str], GroupRows] = defaultdict(GroupRows)
    for row in session.execute(
        text(
            "SELECT balance_group, next_seq FROM balance_group_counters "
            "WHERE experiment_id = :id"
        ),
        {"id": experiment_id},
    ):
        groups[row.balance_group].next_seq = int(row.next_seq)

    problems: List[str] = []
    rows = 0
    by_seq: Dict[Optional[str], Dict[int, str]] = defaultdict(dict)
    for row in session.execute(
        text(
            "SELECT variant_id, balance_group, balance_seq FROM assignments "
            "WHERE experiment_id = :id"
        ),
        {"id": experiment_id},
    ):
        rows += 1
        name = names.get(str(row.variant_id), str(row.variant_id))
        group = groups[row.balance_group]
        if row.balance_seq is None:
            group.outside[name] += 1
            continue
        seq = int(row.balance_seq)
        group.seqs.append(seq)
        group.in_blocks[name] += 1
        by_seq[row.balance_group][seq] = name
        if seed and row.balance_group is not None:
            want = order[
                balanced_blocks.slot_variant(seed, row.balance_group, seq, shares)
            ]
            if name != want:
                problems.append(
                    f"group {row.balance_group}: seq {seq}: variant {name} != "
                    f"slot {want}"
                )

    for label, group in sorted(groups.items(), key=lambda item: str(item[0])):
        taken = Counter(group.seqs)
        for seq, times in sorted(taken.items()):
            if times > 1:
                problems.append(f"group {label}: seq {seq} taken {times} times")
        if group.next_seq is None:
            if group.seqs:
                problems.append(f"group {label}: seqs {sorted(taken)} with no counter")
            continue
        want = set(range(group.next_seq))
        if set(taken) != want:
            missing = sorted(want - set(taken))
            beyond = sorted(set(taken) - want)
            problems.append(
                f"group {label}: seqs {sorted(taken)} != {sorted(want)}"
                + (f" (seq {missing[0]} missing)" if missing else "")
                + (f" (seq {beyond[0]} beyond next_seq)" if beyond else "")
            )
    return BalanceRows(groups=dict(groups), problems=problems, rows=rows)
