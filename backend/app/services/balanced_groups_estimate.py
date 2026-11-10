"""
One size-weighted estimate per treatment for an experiment that balances its
split within groups (#448).

An experiment that declares ``balance_within`` gives every group the same
split, so each group is a small experiment of its own. This module turns each
group's per-variant counts into one overall estimate per treatment: the
difference from the control in each group, weighted by the group's share of
the experiment's users. It reads no database; the caller passes the counts.

The method is the stratified (post-stratified, Neyman) difference in means
(Cochran, *Sampling Techniques*, ch. 5; Imbens and Rubin, *Causal Inference*,
ch. 9). For a treatment ``t`` and the control ``c``, over the groups ``s``:

* ``w_s = N_s / N``, where ``N_s`` counts the users of *every* variant in
  group ``s`` and ``N`` every user of the experiment;
* the difference is ``sum_s w_s * (p_ts - p_cs)``, where ``p_vs`` is the share
  of variant ``v``'s users in group ``s`` who converted;
* its variance is ``sum_s w_s**2 * (s2_ts / n_ts + s2_cs / n_cs)``, with
  ``s2 = p * (1 - p) * n / (n - 1)``, the unbiased variance of a 0/1 value;
* the relative change is the difference over ``sum_s w_s * p_cs``, in percent;
* the interval is the normal one at the experiment's confidence level, and
  the p-value the two-sided normal one.

This equals the treatment coefficient, and its HC2 standard error, of one
least-squares regression over all variants with the group indicators centred
on their shares and interacted with every treatment (Lin, 2013), which
``backend/tests/unit/services/test_balanced_groups_estimate.py`` holds it to.
It is not the treatment coefficient of ``y ~ t * C(group)``, which is the
first group's effect alone.

Small groups: a group with fewer than ``MIN_USERS_PER_ARM`` users in a
variant that has users is counted with the largest group that has at least
that many in every variant that has users (``merge_small_groups``), and none
having that many is ``REASON_TOO_FEW_USERS``. A treatment with no users at all
compares with nothing, so it is left out of that rule and its estimate is
empty; when no treatment has users, or the control has none, the answer is
``REASON_TOO_FEW_USERS``. Merging is approximately unbiased, not exactly, and
is valid because every group has the same target split.

A standard error of 0 (every group's rate is 0 or 1 in both variants) gives
no p-value and no interval, never a p-value of 0.0. The treatments' p-values
are corrected together with the experiment's correction method, as the
headline's are, and a treatment is significant when its corrected p-value (its
p-value, with no correction) is below ``1 - confidence_level``.

There is no CUPED adjustment of this estimate. The estimate is for a
proportion metric only: ``overall_estimate`` answers ``REASON_MEAN_METRIC``
for a mean metric and ``REASON_SEQUENTIAL_TESTING`` for an experiment that
uses sequential testing, with no estimate.
"""

import math
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from scipy import stats

from backend.app.services.sufficient_stats_analysis import (
    adjusted_p_values,
    two_sided_z,
)

#: A group with fewer users than this in a variant is merged
#: (``merge_small_groups``): the unbiased variance needs two.
MIN_USERS_PER_ARM = 2

#: No estimate: the primary metric is not a proportion.
REASON_MEAN_METRIC = "mean_metric"
#: No estimate: the experiment uses sequential testing.
REASON_SEQUENTIAL_TESTING = "sequential_testing"
#: No estimate: no treatment has users, or no group has ``MIN_USERS_PER_ARM``
#: users in every variant that has users.
REASON_TOO_FEW_USERS = "too_few_users"

#: A variant's users and converting users in one group.
Counts = Tuple[int, int]


class Arm(NamedTuple):
    """A variant of the experiment."""

    variant_id: str
    variant_name: str
    is_control: bool


class GroupCounts(NamedTuple):
    """One group's counts: ``counts`` maps a variant id to ``Counts``.

    A variant missing from ``counts`` has no users in the group.
    """

    group: str
    counts: Mapping[str, Counts]


def _users(group: GroupCounts) -> int:
    return sum(users for users, _ in group.counts.values())


def _arm(group: GroupCounts, variant_id: str) -> Counts:
    return group.counts.get(variant_id, (0, 0))


def merge_small_groups(
    groups: Sequence[GroupCounts], variant_ids: Sequence[str]
) -> Tuple[Optional[List[GroupCounts]], List[Dict[str, str]]]:
    """
    The groups to estimate over, with every small group merged.

    A group is small when one of ``variant_ids`` has fewer than
    ``MIN_USERS_PER_ARM`` users in it. Every small group's counts are added
    to the largest group (by users, all variants together) that is not small;
    of equally large ones, the first in ``groups``. Returns the groups in
    their order with the small ones removed, and one ``{"group", "into"}``
    per merged group, in their order. When every group is small, returns
    ``None`` and no merges.
    """
    is_small = [
        any(_arm(g, v)[0] < MIN_USERS_PER_ARM for v in variant_ids) for g in groups
    ]
    kept = [i for i, small in enumerate(is_small) if not small]
    if not kept:
        return None, []
    # max() keeps the first of equally large groups.
    target = max(kept, key=lambda i: _users(groups[i]))
    small = [i for i, s in enumerate(is_small) if s]
    if not small:
        return [groups[i] for i in kept], []

    merged_counts: Dict[str, List[int]] = {
        v: list(c) for v, c in groups[target].counts.items()
    }
    for i in small:
        for v, (users, converted) in groups[i].counts.items():
            entry = merged_counts.setdefault(v, [0, 0])
            entry[0] += users
            entry[1] += converted
    merged = GroupCounts(
        groups[target].group, {v: (c[0], c[1]) for v, c in merged_counts.items()}
    )
    result = [merged if i == target else groups[i] for i in kept]
    into = groups[target].group
    return result, [{"group": groups[i].group, "into": into} for i in small]


def _unbiased_variance(converted: int, users: int) -> float:
    """The unbiased variance of a 0/1 value with ``converted`` ones of ``users``."""
    rate = converted / users
    return rate * (1.0 - rate) * users / (users - 1)


def _empty_estimate(arm: Arm) -> Dict[str, Any]:
    return {
        "variant_id": arm.variant_id,
        "variant_name": arm.variant_name,
        "absolute_difference": None,
        "absolute_difference_ci": None,
        "standard_error": None,
        "relative_improvement_pct": None,
        "p_value": None,
        "adjusted_p_value": None,
        "is_significant": False,
    }


def treatment_estimate(
    groups: Sequence[GroupCounts],
    control_id: str,
    treatment: Arm,
    z: float,
) -> Dict[str, Any]:
    """
    One treatment's estimate over ``groups`` (none of them small).

    The weights count every variant's users in ``groups``. ``z`` is the
    two-sided normal critical value of the interval. ``adjusted_p_value``
    and ``is_significant`` are left for ``balanced_overall`` to set.
    """
    total = sum(_users(g) for g in groups)
    difference = 0.0
    variance = 0.0
    control_rate = 0.0
    for g in groups:
        weight = _users(g) / total
        users_t, converted_t = _arm(g, treatment.variant_id)
        users_c, converted_c = _arm(g, control_id)
        rate_t = converted_t / users_t
        rate_c = converted_c / users_c
        difference += weight * (rate_t - rate_c)
        variance += (
            weight
            * weight
            * (
                _unbiased_variance(converted_t, users_t) / users_t
                + _unbiased_variance(converted_c, users_c) / users_c
            )
        )
        control_rate += weight * rate_c

    standard_error = math.sqrt(variance)
    estimate = _empty_estimate(treatment)
    estimate["absolute_difference"] = difference
    estimate["standard_error"] = standard_error
    if control_rate > 0:
        estimate["relative_improvement_pct"] = difference / control_rate * 100.0
    if standard_error > 0:
        estimate["absolute_difference_ci"] = (
            difference - z * standard_error,
            difference + z * standard_error,
        )
        estimate["p_value"] = float(
            2.0 * stats.norm.sf(abs(difference) / standard_error)
        )
    return estimate


def balanced_overall(
    groups: Sequence[GroupCounts],
    arms: Sequence[Arm],
    *,
    confidence_level: float,
    correction_method: str,
) -> Dict[str, Any]:
    """
    One estimate per treatment, from each group's counts.

    ``groups`` are in the order they are listed; ``arms`` are the
    experiment's variants, the first flagged ``is_control`` being the
    control. Returns ``overall`` (one entry per treatment, in ``arms``
    order, or ``None``), ``reason`` (``None``, or ``REASON_TOO_FEW_USERS``
    with no estimate) and ``merged_groups`` (``merge_small_groups``).

    Raises:
        ValueError: when no arm is the control, or the level is not strictly
            between 0 and 1.
        UnknownCorrectionMethodError: for a correction method that is not one
            of ``CORRECTION_METHODS``.
    """
    z = two_sided_z(confidence_level)
    control = next((a for a in arms if a.is_control), None)
    if control is None:
        raise ValueError("No variant is the control")
    treatments = [a for a in arms if a is not control]

    users_of = {
        a.variant_id: sum(_arm(g, a.variant_id)[0] for g in groups) for a in arms
    }
    if users_of[control.variant_id] == 0:
        return {"overall": None, "reason": REASON_TOO_FEW_USERS, "merged_groups": []}
    compared = [control.variant_id] + [
        t.variant_id for t in treatments if users_of[t.variant_id] > 0
    ]
    if len(compared) == 1:
        # No treatment has users: there is nothing to compare.
        return {"overall": None, "reason": REASON_TOO_FEW_USERS, "merged_groups": []}
    kept, merged = merge_small_groups(groups, compared)
    if kept is None:
        return {"overall": None, "reason": REASON_TOO_FEW_USERS, "merged_groups": []}

    overall = [
        treatment_estimate(kept, control.variant_id, t, z)
        if users_of[t.variant_id] > 0
        else _empty_estimate(t)
        for t in treatments
    ]
    adjusted = adjusted_p_values([e["p_value"] for e in overall], correction_method)
    alpha = 1.0 - confidence_level
    for entry, adj in zip(overall, adjusted):
        entry["adjusted_p_value"] = adj
        decisive = adj if adj is not None else entry["p_value"]
        entry["is_significant"] = bool(decisive is not None and decisive < alpha)
    return {"overall": overall, "reason": None, "merged_groups": merged}


def overall_estimate(
    groups: Sequence[GroupCounts],
    arms: Sequence[Arm],
    *,
    confidence_level: float,
    correction_method: str,
    is_proportion: bool,
    sequential_testing: bool,
) -> Dict[str, Any]:
    """
    ``balanced_overall``, or no estimate and the reason there is none.

    A primary metric that is not a proportion answers ``REASON_MEAN_METRIC``,
    then an experiment with sequential testing ``REASON_SEQUENTIAL_TESTING``,
    each with ``overall`` ``None`` and no merges.
    """
    if not is_proportion:
        return {"overall": None, "reason": REASON_MEAN_METRIC, "merged_groups": []}
    if sequential_testing:
        return {
            "overall": None,
            "reason": REASON_SEQUENTIAL_TESTING,
            "merged_groups": [],
        }
    return balanced_overall(
        groups,
        arms,
        confidence_level=confidence_level,
        correction_method=correction_method,
    )
