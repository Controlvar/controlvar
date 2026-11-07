"""FX, the nightly scenario with every fault combined
(``tests/ground_truth/nightly_fx.py``, #1544 part D3), with no server.

* FX is the plan's: 40,000 users at the nightly scale whatever
  ``GROUND_TRUTH_SCALE`` says, its eight definitions with their named
  integers, F7's loss at least 10% of its users; its reproduce command names
  its own file and scale.
* On a correct server's ledger (``faults.expected_records``), at FX's own
  size, every row of the combined sensitivity check is an exact sum of the
  named integers, for 20 seeds; F2's bot volume and F4's future-dated users
  are counted on the plan. (``test_sensitivity.py`` shows the same check red
  when a fault is placed wrong.)
* F7's loss misses the sample-ratio alarm with a probability of at most 1e-9,
  computed from FX's users and lost users.
* FX's gates (the file's test functions) pass on what the real driver records
  against a fake correct server, and S and P fail, naming the fault and FX's
  reproduce command, when a fault never reached the server.
"""

from __future__ import annotations

import functools
import importlib
import math
import os
import subprocess
import sys
from collections import Counter
from dataclasses import replace

import pytest
from scipy import stats

from backend.tests.unit.ground_truth.library import (
    REPO_ROOT,
    contract,
    faults,
    generator,
)
from backend.tests.unit.ground_truth.test_fault_gates import failed, fake_run

# tests/ground_truth is on sys.path once library is imported.
nightly = importlib.import_module("nightly_fx")
suite = importlib.import_module("test_faults")
clean = importlib.import_module("test_replay")
sensitivity = importlib.import_module("sensitivity")

pytestmark = pytest.mark.unit

SEED = 20261103
#: The seeds FX is checked under at its own size: this file's, the D2 live
#: run's, and eighteen more.
SEEDS = (SEED, 194306375, *range(1, 19))


def _one(planted: tuple, kind: type) -> object:
    (fault,) = [f for f in planted if isinstance(f, kind)]
    return fault


@functools.lru_cache(maxsize=1)
def _plan(seed: int) -> generator.Plan:
    return nightly.scenario(seed).plan(seed)


# ---------------------------------------------------------------------------
# FX as the plan defines it
# ---------------------------------------------------------------------------
def test_fx_is_the_plan_s():
    case = nightly.scenario(SEED)
    shop = case.scenario
    assert shop.users == 40000 == 10000 * clean.SCALES["nightly"]
    assert [(a.name, a.allocation) for a in shop.arms] == [
        ("control", 50),
        ("treatment", 50),
    ]
    assert [(m.event_name, m.type) for m in shop.metrics] == [
        ("purchase", "conversion"),
        ("purchase", "count"),
        ("purchase", "revenue"),
    ]
    assert case.planted == (
        faults.Duplicates(r4_drops=120, no_id_repeats=20),
        faults.BotTraffic(bots=20, events_per_bot=250, huge_values=20),
        faults.LateEvents(
            r2_status_untags=80,
            paused_assigns=40,
            completed_assigns=20,
            paused_resends=20,
        ),
        faults.ClockSkew(future_users_per_arm=6),
        faults.EarlyEvents(r2_order_untags=80),
        faults.VariantMismatch(
            other_arm=80, never_assigned=80, tagged_then_assigned=40, own_arm=40
        ),
        faults.ExposureLoss(lost_users=4000),
        faults.Hostile(per_class=1, value=1.0),
    )
    assert {f.NAME for f in case.planted} == set(faults.COMBINED_ORDER)
    lost = _one(case.planted, faults.ExposureLoss).lost_users
    assert lost * 10 >= shop.users, "F7's loss is at least 10% of FX's users"
    others = {c.scenario.experiment_key for c in suite.scenarios(SEED).values()}
    others |= {s.experiment_key for s in clean.scenarios(SEED, 4).values()}
    assert shop.experiment_key not in others and str(SEED) in shop.experiment_key


_SIZE = (
    "import sys; sys.path.insert(0, 'tests/ground_truth'); import nightly_fx;"
    " print('users', nightly_fx.FX.scenario.users)"
)


def test_fx_is_sized_at_the_nightly_scale_and_its_reproduce_names_file_and_scale():
    env = {**os.environ, "GROUND_TRUTH_SCALE": "pr", "GROUND_TRUTH_SEED": "7"}
    result = subprocess.run(
        [sys.executable, "-B", "-c", _SIZE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "users 40000" in result.stdout, result.stdout
    assert nightly.reproduce(seed=7) == (
        "GROUND_TRUTH_SEED=7 GROUND_TRUTH_SCALE=nightly python -m pytest"
        " tests/ground_truth/nightly_fx.py"
    )


# ---------------------------------------------------------------------------
# A correct server's ledger, at FX's own size
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", SEEDS)
def test_fx_reaches_a_correct_server_as_planted(seed):
    """Every row exact: R4 = F1's 120 drops + F3's 20 paused resends;
    R2-status 80; R2-order 80; R8 160 nowhere and 80 counted; F2 20 refused
    and 4,980 stored; F7 4,000
    defined users never assigned and the alarm; F9 each class once and its 6
    valid users counting; and P-b for every rule."""
    case = nightly.scenario(seed)
    plan = _plan(seed) if seed == SEED else case.plan(seed)
    records = faults.expected_records(plan, faults.DECLARED)
    sensitivity.sensitivity(
        case.planted,
        plan,
        records,
        where=f"scenario FX, seed {seed}",
        hostile_sends=faults.hostile_counts(plan),
        combined=True,
    )
    experiment = contract.valid_events(records).experiments[
        case.scenario.experiment_key
    ]
    assert experiment.variant_mismatches == ()


def test_fx_sends_its_bot_volume_and_dates_its_users_in_the_future():
    """Counted on the plan, not by the sensitivity check: F2's 20 bots send
    250 events each; F4 dates every event of 6 users of each arm in 2099."""
    case = nightly.scenario(SEED)
    plan = _plan(SEED)
    bots = _one(case.planted, faults.BotTraffic)
    sent = Counter(
        body["user_id"]
        for op in plan.ops
        if op.kind == "batch"
        for body in op.body["events"]
        if "-f2-e" in (body.get("event_id") or "")
    )
    assert sorted(sent.values()) == [bots.events_per_bot] * bots.bots
    assert faults.future_dated_users(plan) == {"control": 6, "treatment": 6}


# ---------------------------------------------------------------------------
# F7's loss shows
# ---------------------------------------------------------------------------
def miss_probability(users: int, lost: int, alarm: float = faults.SRM_ALARM) -> float:
    """The chance the sample-ratio check does not fire: two arms, 50/50,
    each of *users* assigned by a fair coin, then *lost* of one arm never
    assigned. The difference between the arms is ``lost + Z * sqrt(users)``;
    the check fires when its square over the users left exceeds the
    chi-square threshold of *alarm* (normal approximation). The users another
    fault adds (F9's six) move the difference by at most their number."""
    reach = math.sqrt(stats.chi2.isf(alarm, 1) * (users - lost))
    spread = math.sqrt(users)
    return float(
        stats.norm.cdf((reach - lost) / spread)
        - stats.norm.cdf((-reach - lost) / spread)
    )


def test_f7_s_loss_misses_the_sample_ratio_alarm_at_most_once_in_a_billion():
    lost = _one(nightly.FX.planted, faults.ExposureLoss).lost_users
    users = nightly.FX.scenario.users
    assert miss_probability(users, lost) <= 1e-9, miss_probability(users, lost)
    # The PR tier's loss of 1,000 at FX's size would miss about one night in 25.
    assert 0.03 < miss_probability(40000, 1000) < 0.05


# ---------------------------------------------------------------------------
# FX's gates on the real driver against a fake correct server
# ---------------------------------------------------------------------------
#: FX's shape, small enough for the fake server: the same eight faults.
SMALL_FX = nightly.Combined(
    replace(nightly.FX.scenario, users=2000, experiment_key="gt-fx-small"),
    (
        faults.Duplicates(r4_drops=12, no_id_repeats=3),
        faults.BotTraffic(bots=4, events_per_bot=30, huge_values=6),
        faults.LateEvents(
            r2_status_untags=8, paused_assigns=3, completed_assigns=2, paused_resends=2
        ),
        faults.ClockSkew(future_users_per_arm=2),
        faults.EarlyEvents(r2_order_untags=8),
        faults.VariantMismatch(
            other_arm=5, never_assigned=4, tagged_then_assigned=3, own_arm=2
        ),
        faults.ExposureLoss(lost_users=400),
        faults.Hostile(per_class=1, value=1.0),
    ),
)
GATES = sorted(name for name in dir(nightly) if name.startswith("test_"))


def test_fx_runs_each_gate_of_a_fault_scenario_once():
    assert GATES == sorted(
        [
            "test_p_every_answer_is_the_one_its_op_expects",
            "test_g0_every_assignment_answer_is_o_assign",
            "test_g1_counts_and_means_equal_the_ledger",
            "test_g2_statistics_equal_the_reference",
            "test_z_no_answer_is_a_5xx",
            "test_s_every_fault_reached_the_ledger_as_planted",
            "test_f1d_every_repeat_is_answered_as_one",
            "test_f7_the_sample_ratio_check_overrides_the_recommendation",
            "test_f9_each_hostile_class_gets_its_listed_answer",
        ]
    )


def test_every_gate_of_fx_passes_on_a_correct_server(tmp_path):
    faulted, _ = fake_run(SMALL_FX, tmp_path)
    assert faulted.failure is None and faulted.unexpected == []
    for name in GATES:
        getattr(nightly, name)(faulted)


def test_s_and_p_name_a_fault_that_never_reached_the_server(tmp_path):
    def refused(op, status, body):
        if op.kind == "events":
            return 404, {"detail": "no such variant"}
        return status, body

    faulted, _ = fake_run(SMALL_FX, tmp_path, answer=refused)
    message = failed(nightly.test_s_every_fault_reached_the_ledger_as_planted, faulted)
    assert message.startswith(f"sensitivity failed: scenario FX, seed {SEED}")
    assert (
        "F6 (R8): the definition plants 9 /events events counted in no variant"
        " (cases i and ii); the ledger shows 0 (the fault touched nothing)" in message
    ), message
    assert (
        f"reproduce: GROUND_TRUTH_SEED={SEED} GROUND_TRUTH_SCALE=nightly python -m"
        " pytest tests/ground_truth/nightly_fx.py" in message
    ), message
    message = failed(nightly.test_p_every_answer_is_the_one_its_op_expects, faulted)
    assert message.startswith(f"P failed: scenario FX, seed {SEED}, scale nightly")
    assert "answered 404, want a 2xx (accepted)" in message
