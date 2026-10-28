"""The CUPED oracles, and the results journey's two CUPED reads that use them.

The results journey (``experiment-results.yaml``) ends with two reads of
``GET /api/v1/results/{experiment_id}/cuped``: new users (the A/B experiment,
whose users sent nothing before their assignment) and returning users (an
experiment whose traffic step sends pre-period events first, ``history``).
Each number is held to an oracle written from ``docs/api/cuped.md`` and the
textbook formula (``docs_runner/oracles.py``, ``cuped_*``). What decides
whether those checks can fail is pure, and is tested here, in the unit job:

* each oracle is the page's formula: known answers worked by hand as
  fractions, and ``theta`` and the residuals of an ordinary least squares fit
  of ``Y ~ arm + X`` on the users written out one by one (statsmodels), the
  regression the page names;
* the page's promises hold: ``theta`` is 0 with no history (or history for
  everyone), and then every adjusted number is the unadjusted one; the
  interval leaves out 0 exactly when the p-value is under ``1 -
  confidence_level``; and each oracle refuses what no comparison can be;
* each read's oracles are given the counts its traffic step sends, history
  included, and the history is inside the experiment's lookback;
* each read's tolerances hold the formula's answer and refuse the answer each
  defect would give (theta 0, the history sent too late, the sign flipped,
  theta from the control alone or from both arms lumped together, the
  variance over n, a t-test), and the runner fails the step on each.

No network, no browser, no product code.
"""

from __future__ import annotations

import functools
import importlib
import math
import re
import sys
import types
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pytest
import statsmodels.api as sm
import yaml
from scipy import stats

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import log, oracles

JOURNEY = RUNNER_ROOT / "journeys" / "experiment-results.yaml"
COUNT_ARGS = (
    "control_users",
    "control_converted",
    "control_history",
    "treatment_users",
    "treatment_converted",
    "treatment_history",
)
#: The journey's two populations, in COUNT_ARGS order.
NEW_USERS = (400, 40, 0, 400, 64, 0)
RETURNING = (256, 40, 80, 256, 56, 48)
READS = ("cuped-new-users", "cuped-returning-users")
CUPED_ORACLES = (
    "cuped_theta",
    "cuped_adjusted_mean",
    "cuped_adjusted_effect",
    "cuped_unadjusted_effect",
    "cuped_adjusted_se",
    "cuped_unadjusted_se",
    "cuped_adjusted_p",
    "cuped_adjusted_interval",
    "cuped_variance_reduction_pct",
    "cuped_coverage_pct",
)


def _all(counts: Tuple[int, ...], confidence_level: float = 0.95) -> Dict[str, float]:
    """Every number of one comparison, by the oracles, keyed as the API names them."""
    return {
        "theta": oracles.cuped_theta(*counts),
        "adjusted_control_mean": oracles.cuped_adjusted_mean(
            *counts, variant="control"
        ),
        "adjusted_treatment_mean": oracles.cuped_adjusted_mean(
            *counts, variant="treatment"
        ),
        "adjusted_effect": oracles.cuped_adjusted_effect(*counts),
        "unadjusted_effect": oracles.cuped_unadjusted_effect(*counts),
        "adjusted_se": oracles.cuped_adjusted_se(*counts),
        "unadjusted_se": oracles.cuped_unadjusted_se(*counts),
        "adjusted_p_value": oracles.cuped_adjusted_p(*counts),
        "adjusted_ci_lower": oracles.cuped_adjusted_interval(
            *counts, confidence_level=confidence_level, bound="lower"
        ),
        "adjusted_ci_upper": oracles.cuped_adjusted_interval(
            *counts, confidence_level=confidence_level, bound="upper"
        ),
        "variance_reduction_pct": oracles.cuped_variance_reduction_pct(*counts),
        "covariate_coverage_pct": oracles.cuped_coverage_pct(*counts),
    }


# ---------------------------------------------------------------------------
# Known answers, worked by hand
# ---------------------------------------------------------------------------
def test_a_population_worked_by_hand_as_fractions():
    """Control: 5 users, the first 2 convert, the first 1 returning; Treatment:
    5 users, the first 3 convert, the first 3 returning.

    Control: mean Y 2/5, mean X 1/5, S_xy = 1 - 1*2/5 = 3/5, S_xx = 1 - 1/5 =
    4/5, S_yy = 2 - 4/5 = 6/5. Treatment: mean Y 3/5, mean X 3/5, S_xy = 3 -
    9/5 = 6/5, S_xx = 6/5, S_yy = 6/5. theta = (3/5 + 6/5) / (4/5 + 6/5) =
    9/10. Everyone's mean X = 4/10. Control's adjusted rate 2/5 - 9/10 (1/5 -
    2/5) = 29/50, Treatment's 3/5 - 9/10 (3/5 - 2/5) = 21/50, the effect
    -8/50 (unadjusted +1/5). Residual sums of squares: Control 6/5 - 2 (9/10)
    (3/5) + (81/100)(4/5) = 96/125, Treatment 6/5 - 2 (9/10)(6/5) + (81/100)
    (6/5) = 3/250; over (n - 1) n = 20 each, the effect's variance is 39/1000
    (unadjusted 6/100 + 6/100 = 3/25). Variance reduction 100 (1 - (39/1000) /
    (3/25)) = 67.5; coverage 4 of 10 = 40.
    """
    counts = (5, 2, 1, 5, 3, 3)
    found = _all(counts)
    assert found["theta"] == 0.9
    assert found["adjusted_control_mean"] == 0.58
    assert found["adjusted_treatment_mean"] == 0.42
    assert found["adjusted_effect"] == float(Fraction(-8, 50))
    assert found["unadjusted_effect"] == 0.2
    assert found["adjusted_se"] == math.sqrt(0.039)
    assert found["unadjusted_se"] == math.sqrt(0.12)
    assert found["variance_reduction_pct"] == 67.5
    assert found["covariate_coverage_pct"] == 40.0
    z = 0.16 / math.sqrt(0.039)
    assert found["adjusted_p_value"] == pytest.approx(
        math.erfc(z / math.sqrt(2)), rel=1e-12
    )
    half = 1.959963984540054 * math.sqrt(0.039)
    assert found["adjusted_ci_lower"] == pytest.approx(-0.16 - half, rel=1e-12)
    assert found["adjusted_ci_upper"] == pytest.approx(-0.16 + half, rel=1e-12)


def test_the_overall_share_with_history_weights_the_arms_by_their_users():
    """Arms of unequal size, where an unweighted average of the arms' shares
    gives another answer. Control: 300 users, 30 convert, 10 returning (all 10
    among the 30); Treatment: 200 users, 40 convert, 150 returning (the 40
    among the 150).

    S_xy: 10 - 10*30/300 = 9 and 40 - 150*40/200 = 10; S_xx: 10 - 10^2/300 =
    29/3 and 150 - 150^2/200 = 75/2; theta = 19 / (29/3 + 75/2) = 114/283.
    Everyone's share with history is (10 + 150) / (300 + 200) = 8/25, not the
    mean of 1/30 and 3/4. Control's adjusted rate 1/10 - 114/283 (1/30 - 8/25)
    = 3049/14150; the unweighted mean 47/120 would give 1383/5660.
    """
    counts = (300, 30, 10, 200, 40, 150)
    assert oracles.cuped_theta(*counts) == float(Fraction(114, 283))
    assert oracles.cuped_coverage_pct(*counts) == 32.0
    assert oracles.cuped_adjusted_mean(*counts, variant="control") == float(
        Fraction(3049, 14150)
    )
    assert oracles.cuped_adjusted_mean(*counts, variant="control") != float(
        Fraction(1383, 5660)
    )


def test_balanced_history_leaves_the_effect_and_narrows_its_interval():
    """Control 4 users (2 convert, 2 returning), Treatment 4 (1 converts, 2
    returning): both arms' share with history is everyone's, 1/2, so the
    adjusted rates are the raw ones. S_xy 1 and 1/2, S_xx 1 and 1: theta 3/4.
    Residuals 1 - 3/2 + 9/16 = 1/16 and 3/4 - 3/4 + 9/16 = 9/16, over 12 each:
    5/96 against the unadjusted 1/12 + 1/16 = 7/48, a reduction of 9/14."""
    found = _all((4, 2, 2, 4, 1, 2))
    assert found["theta"] == 0.75
    assert found["adjusted_effect"] == found["unadjusted_effect"] == -0.25
    assert found["adjusted_se"] == math.sqrt(5 / 96)
    assert found["unadjusted_se"] == math.sqrt(7 / 48)
    assert found["variance_reduction_pct"] == pytest.approx(100 * 9 / 14, rel=1e-15)
    assert found["covariate_coverage_pct"] == 50.0


def test_the_journeys_returning_users_by_hand():
    """Control: 256 users, 40 convert, 80 returning (the 40 among the 80).
    Treatment: 256, 56 convert, 48 returning (the 48 among the 56).
    S_xy: 40 - 80*40/256 = 27.5 and 48 - 48*56/256 = 37.5; S_xx: 80 -
    80^2/256 = 55 and 48 - 48^2/256 = 39; theta = 65 / 94."""
    assert oracles.cuped_theta(*RETURNING) == float(Fraction(65, 94))
    pooled_within = (Fraction(55, 2) + Fraction(75, 2)) / (55 + 39)
    assert pooled_within == Fraction(65, 94)
    found = _all(RETURNING)
    assert found["covariate_coverage_pct"] == 25.0
    assert found["unadjusted_effect"] == 0.0625
    # 0.0625 + 65/94 * (80 - 48) / 256 = 0.0625 + 65/752
    assert found["adjusted_effect"] == float(Fraction(1, 16) + Fraction(65, 752))
    # What the journey's comment prints.
    assert round(found["theta"], 5) == 0.69149
    assert round(found["adjusted_effect"], 5) == 0.14894
    assert round(found["unadjusted_se"], 5) == 0.03446
    assert round(found["adjusted_se"], 5) == 0.02233
    assert f"{found['adjusted_p_value']:.1e}" == "2.6e-11"
    assert (
        round(found["adjusted_ci_lower"], 4),
        round(found["adjusted_ci_upper"], 4),
    ) == (
        0.1052,
        0.1927,
    )
    assert round(found["variance_reduction_pct"], 1) == 58.0
    assert oracles.cuped_adjusted_p(256, 40, 0, 256, 56, 0) > 0.05


def test_the_journeys_new_users_by_hand():
    found = _all(NEW_USERS)
    assert found["theta"] == 0.0
    assert found["covariate_coverage_pct"] == 0.0
    assert found["variance_reduction_pct"] == 0.0
    assert found["adjusted_effect"] == found["unadjusted_effect"] == 0.16 - 0.1
    # Each arm's sample variance of a 0/1 outcome, p (1 - p) n / (n - 1), over n.
    by_hand = 0.1 * 0.9 / 399 + 0.16 * 0.84 / 399
    assert found["adjusted_se"] == found["unadjusted_se"]
    assert found["adjusted_se"] == pytest.approx(math.sqrt(by_hand), rel=1e-14)
    assert round(found["adjusted_se"], 6) == 0.023715
    assert round(found["adjusted_p_value"], 4) == 0.0114
    assert (
        round(found["adjusted_ci_lower"], 4),
        round(found["adjusted_ci_upper"], 4),
    ) == (
        0.0135,
        0.1065,
    )


# ---------------------------------------------------------------------------
# The regression the page names, fitted on the users one by one
# ---------------------------------------------------------------------------
def _rows(users: int, converted: int, history: int) -> Tuple[np.ndarray, np.ndarray]:
    """One arm's users in the order chosen: the first ``converted`` have Y = 1,
    the first ``history`` have X = 1 (the traffic step's rule)."""
    y = np.zeros(users)
    y[:converted] = 1.0
    x = np.zeros(users)
    x[:history] = 1.0
    return y, x


POPULATIONS = [
    RETURNING,
    (256, 40, 120, 256, 56, 24),
    (300, 30, 10, 200, 40, 150),
    (60, 6, 30, 60, 24, 5),
    (1000, 100, 333, 1000, 130, 250),
    (50, 49, 3, 70, 1, 69),
]


@pytest.mark.parametrize("counts", POPULATIONS)
def test_theta_is_the_x_coefficient_of_y_on_arm_and_x(counts):
    yc, xc = _rows(*counts[:3])
    yt, xt = _rows(*counts[3:])
    y = np.concatenate([yc, yt])
    x = np.concatenate([xc, xt])
    arm = np.concatenate([np.zeros(len(yc)), np.ones(len(yt))])
    design = np.column_stack([np.ones(len(y)), arm, x])
    fit = sm.OLS(y, design).fit()
    assert oracles.cuped_theta(*counts) == pytest.approx(fit.params[2], rel=1e-10)
    # Each arm's residual variance (n - 1) over its size gives the effect's.
    residuals = fit.resid
    variance = sum(
        np.var(part, ddof=1) / len(part)
        for part in (residuals[: len(yc)], residuals[len(yc) :])
    )
    assert oracles.cuped_adjusted_se(*counts) == pytest.approx(
        math.sqrt(variance), rel=1e-10
    )
    # The adjusted effect is the regression's arm coefficient.
    assert oracles.cuped_adjusted_effect(*counts) == pytest.approx(
        fit.params[1], rel=1e-10
    )


@pytest.mark.parametrize("counts", POPULATIONS)
def test_theta_differs_from_the_control_alone_and_from_both_arms_lumped(counts):
    """The page's pooled-within slope is neither of the two a defect would use."""
    yc, xc = _rows(*counts[:3])
    yt, xt = _rows(*counts[3:])
    control_alone = np.cov(yc, xc)[0, 1] / np.var(xc, ddof=1)
    y, x = np.concatenate([yc, yt]), np.concatenate([xc, xt])
    lumped = np.cov(y, x)[0, 1] / np.var(x, ddof=1)
    theta = oracles.cuped_theta(*counts)
    assert abs(theta - control_alone) > 1e-3 * abs(theta)
    assert abs(theta - lumped) > 1e-3 * abs(theta)


# ---------------------------------------------------------------------------
# The page's promises
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "counts",
    [
        NEW_USERS,
        (256, 40, 0, 256, 56, 0),
        (256, 40, 256, 256, 56, 256),
        (20, 3, 20, 30, 9, 30),
    ],
    ids=["new-users", "no-history", "everyone-returning", "everyone-returning-small"],
)
def test_with_no_history_or_history_for_everyone_nothing_is_adjusted(counts):
    """api/cuped.md: "With no history, theta is 0, variance_reduction_pct is 0
    and the adjusted effect equals the unadjusted one" (theta is 0 "when no
    user (or every user) has history")."""
    found = _all(counts)
    assert found["theta"] == 0.0
    assert found["variance_reduction_pct"] == 0.0
    assert found["adjusted_effect"] == found["unadjusted_effect"]
    assert found["adjusted_se"] == found["unadjusted_se"]
    users_c, converted_c, _, users_t, converted_t, _ = counts
    assert found["adjusted_control_mean"] == converted_c / users_c
    assert found["adjusted_treatment_mean"] == converted_t / users_t


@pytest.mark.parametrize("counts", POPULATIONS + [NEW_USERS])
@pytest.mark.parametrize("confidence_level", [0.9, 0.95, 0.99])
def test_the_interval_leaves_out_zero_exactly_when_p_is_under_one_minus_the_level(
    counts, confidence_level
):
    found = _all(counts, confidence_level)
    outside = found["adjusted_ci_lower"] > 0 or found["adjusted_ci_upper"] < 0
    assert outside == (found["adjusted_p_value"] < 1 - confidence_level)
    middle = (found["adjusted_ci_lower"] + found["adjusted_ci_upper"]) / 2
    assert middle == pytest.approx(found["adjusted_effect"], rel=1e-12, abs=1e-15)


@pytest.mark.parametrize("counts", POPULATIONS + [NEW_USERS])
def test_the_variance_reduction_is_one_minus_the_ratio_of_the_variances(counts):
    found = _all(counts)
    ratio = (found["adjusted_se"] / found["unadjusted_se"]) ** 2
    assert found["variance_reduction_pct"] == pytest.approx(100 * (1 - ratio), abs=1e-9)
    # "At r % the interval is sqrt(1 - r/100) as wide."
    width = found["adjusted_ci_upper"] - found["adjusted_ci_lower"]
    unadjusted = 2 * stats.norm.ppf(0.975) * found["unadjusted_se"]
    assert width / unadjusted == pytest.approx(
        math.sqrt(1 - found["variance_reduction_pct"] / 100), rel=1e-9
    )


def test_a_pooled_slope_can_make_one_comparison_noisier():
    """api/cuped.md: variance_reduction_pct "can be negative": a small control
    (20 users, 1 converting, 11 returning) whose history says nothing, beside a
    large treatment whose history predicts every outcome (100 of 200 returning,
    the same 100 converting), takes the treatment's slope and is noisier for it."""
    counts = (20, 1, 11, 200, 100, 100)
    assert oracles.cuped_theta(*counts) > 0.9
    assert oracles.cuped_variance_reduction_pct(*counts) < -100


def test_the_n_minus_one_divisor_is_not_the_n_divisor():
    """The page does not name the divisor; the oracle takes n - 1. Over n,
    each standard error is sqrt((n - 1) / n) of it: 0.2% less at 256 users."""
    yc, xc = _rows(*RETURNING[:3])
    yt, xt = _rows(*RETURNING[3:])
    theta = oracles.cuped_theta(*RETURNING)
    over_n = math.sqrt(
        np.var(yc - theta * xc) / len(yc) + np.var(yt - theta * xt) / len(yt)
    )
    se = oracles.cuped_adjusted_se(*RETURNING)
    assert over_n / se == pytest.approx(math.sqrt(255 / 256), rel=1e-12)
    assert abs(over_n - se) > 1e-3 * se


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
REFUSALS = [
    (
        lambda: oracles.cuped_theta(10, 2, -1, 10, 3, 0),
        "control_history must be from 0",
    ),
    (
        lambda: oracles.cuped_theta(10, 2, 11, 10, 3, 0),
        "control_history must be from 0",
    ),
    (
        lambda: oracles.cuped_theta(10, 2, 0, 10, 3, 11),
        "treatment_history must be from 0",
    ),
    (
        lambda: oracles.cuped_theta(10, 2, 2.0, 10, 3, 0),
        "control_history must be a whole",
    ),
    (
        lambda: oracles.cuped_theta(10, 2, True, 10, 3, 0),
        "control_history must be a whole",
    ),
    (
        lambda: oracles.cuped_theta(10, 2, "2", 10, 3, 0),
        "control_history must be a whole",
    ),
    (lambda: oracles.cuped_theta(10, 11, 0, 10, 3, 0), "more control users converted"),
    (
        lambda: oracles.cuped_theta(10, -1, 0, 10, 3, 0),
        "control_converted cannot be negative",
    ),
    (
        lambda: oracles.cuped_theta(10.0, 2, 0, 10, 3, 0),
        "control_users must be a whole",
    ),
    (lambda: oracles.cuped_theta(0, 0, 0, 10, 3, 0), "the control has no users"),
    (
        lambda: oracles.cuped_theta(1, 1, 0, 10, 3, 0),
        "the control needs at least 2 users",
    ),
    (
        lambda: oracles.cuped_theta(10, 3, 0, 1, 0, 0),
        "the treatment needs at least 2 users",
    ),
    (
        lambda: oracles.cuped_theta(10, 0, 4, 10, 0, 2),
        "neither variant's outcome varies",
    ),
    (
        lambda: oracles.cuped_theta(10, 10, 4, 10, 10, 2),
        "neither variant's outcome varies",
    ),
    (
        lambda: oracles.cuped_adjusted_p(10, 3, 3, 10, 6, 6),
        "history predicts every outcome",
    ),
    (
        lambda: oracles.cuped_adjusted_mean(*RETURNING, variant="Treatment"),
        "variant must be 'control' or 'treatment'",
    ),
    (
        lambda: oracles.cuped_adjusted_interval(*RETURNING, bound="both"),
        "bound must be 'lower' or 'upper'",
    ),
    (
        lambda: oracles.cuped_adjusted_interval(*RETURNING, confidence_level=1.0),
        "confidence_level must be above 0 and below 1",
    ),
    (
        lambda: oracles.cuped_adjusted_interval(*RETURNING, confidence_level=95),
        "confidence_level must be above 0 and below 1",
    ),
    (
        lambda: oracles.cuped_adjusted_interval(*RETURNING, confidence_level=True),
        "confidence_level must be a number",
    ),
]


@pytest.mark.parametrize("call, message", REFUSALS)
def test_each_oracle_refuses_what_no_comparison_can_be(call, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        call()


def test_the_registry_names_each_cuped_oracle():
    for name in CUPED_ORACLES:
        assert oracles.ORACLES[name] is getattr(oracles, name)


def test_no_oracle_reads_the_product():
    source = Path(oracles.__file__).read_text(encoding="utf-8")
    imported = re.findall(r"^\s*(?:from|import)\s+(\S+)", source, re.M)
    assert "scipy" in imported
    assert [
        m for m in imported if m.split(".")[0] in ("backend", "app", "modules")
    ] == []


# ---------------------------------------------------------------------------
# The journey's two reads
# ---------------------------------------------------------------------------
PLACEHOLDER = re.compile(r"\{\{([a-z0-9-]+)\}\}")


@functools.lru_cache(maxsize=1)
def _steps() -> List[Dict[str, Any]]:
    """The journey's steps, parsed once; no test changes them."""
    return yaml.safe_load(JOURNEY.read_text(encoding="utf-8"))["steps"]


def _index(step_id: str) -> int:
    return next(i for i, step in enumerate(_steps()) if step["id"] == step_id)


def _read_setup(read_id: str) -> Dict[str, Any]:
    """The read, its experiment, its traffic step, and how CUPED was configured."""
    steps = _steps()
    at = _index(read_id)
    (experiment,) = PLACEHOLDER.findall(steps[at]["do"]["api"]["path"])
    (traffic,) = [
        (i, s)
        for i, s in enumerate(steps)
        if (s["do"].get("traffic") or {}).get("experiment") == experiment
    ]
    configs = []
    for i, s in enumerate(steps[:at]):
        call = s["do"].get("api") or {}
        body = call.get("body") if isinstance(call.get("body"), dict) else {}
        config = body.get("variance_reduction_config")
        if config is None:
            continue
        put = (
            call["method"] == "PUT"
            and call["path"] == f"/api/v1/experiments/{{{{{experiment}}}}}"
        )
        made = call["method"] == "POST" and experiment in (s.get("save") or {})
        if put or made:
            configs.append((i, config, s))
    return {
        "read": steps[at],
        "at": at,
        "experiment": experiment,
        "traffic": traffic,
        "configs": configs,
    }


def _sent(setup: Dict[str, Any]) -> Tuple[int, ...]:
    plan = setup["traffic"][1]["do"]["traffic"]
    history = (plan.get("history") or {}).get("converted", {})
    return (
        plan["variants"]["Control"]["assigned"],
        plan["variants"]["Control"]["converted"],
        history.get("Control", 0),
        plan["variants"]["Treatment"]["assigned"],
        plan["variants"]["Treatment"]["converted"],
        history.get("Treatment", 0),
    )


def test_the_journey_reads_new_users_and_returning_users():
    assert _sent(_read_setup("cuped-new-users")) == NEW_USERS
    assert _sent(_read_setup("cuped-returning-users")) == RETURNING
    assert _index("cuped-new-users") < _index("cuped-returning-users")


@pytest.mark.parametrize("read_id", READS)
def test_each_read_comes_after_its_traffic_and_after_cuped_is_on(read_id):
    setup = _read_setup(read_id)
    assert setup["read"]["do"]["api"]["path"].endswith("/cuped")
    assert setup["traffic"][0] < setup["at"]
    assert setup["configs"], "nothing turns CUPED on"
    at, config, step = setup["configs"][-1]
    assert config["method"] == "cuped"
    expected = step["expect"]["json"]
    assert expected["variance_reduction_config.method"] == "cuped"
    lookback = config["covariate_lookback_days"]
    assert expected["variance_reduction_config.covariate_lookback_days"] == lookback
    assert setup["read"]["expect"]["json"]["covariate_lookback_days"] == lookback
    # The new users' experiment is turned on after its traffic (as the page
    # does); the returning users' is made with CUPED, before its traffic.
    if read_id == "cuped-new-users":
        assert setup["traffic"][0] < at
    else:
        assert at < setup["traffic"][0]


def test_the_history_is_inside_the_lookback_and_sent_with_the_metrics_event():
    setup = _read_setup("cuped-returning-users")
    plan = setup["traffic"][1]["do"]["traffic"]
    lookback = setup["configs"][-1][1]["covariate_lookback_days"]
    assert 1 <= plan["history"]["days_before"] < lookback
    made = next(s for s in _steps() if s["id"] == "cuped-experiment")
    (metric,) = made["do"]["api"]["body"]["metrics"]
    assert plan["event"] == metric["event_name"]
    assert (
        setup["read"]["expect"]["json"]["metrics.0.covariate_event_name"]
        == plan["event"]
    )


def test_no_two_traffic_steps_share_an_experiment_or_a_user_prefix():
    plans = [s["do"]["traffic"] for s in _steps() if "traffic" in s["do"]]
    assert len({p["experiment"] for p in plans}) == len(plans)
    assert len({p["users"] for p in plans}) == len(plans)


def _calls(node: Any) -> List[Dict[str, Any]]:
    if isinstance(node, dict):
        found = [node] if node.keys() == {"name", "args"} else []
        return found + [c for value in node.values() for c in _calls(value)]
    if isinstance(node, list):
        return [c for value in node for c in _calls(value)]
    return []


@pytest.mark.parametrize("read_id", READS)
def test_each_oracle_is_given_the_counts_and_the_history_the_journey_sends(read_id):
    setup = _read_setup(read_id)
    sent = dict(zip(COUNT_ARGS, _sent(setup)))
    computed = setup["read"]["expect"]["computed"]
    assert len(computed) == 13
    for path, check in computed.items():
        name, args = check["oracle"]["name"], check["oracle"]["args"]
        assert name in CUPED_ORACLES, path
        assert {k: args[k] for k in COUNT_ARGS} == sent, path
        if name == "cuped_adjusted_interval":
            assert (
                args["confidence_level"]
                == setup["read"]["expect"]["json"]["confidence_level"]
            )
            assert args["bound"] == path.rsplit("_", 1)[1], path
        if name == "cuped_adjusted_mean":
            assert args["variant"] == path.split("adjusted_")[1].split("_")[0], path


@pytest.mark.parametrize("read_id", READS)
def test_each_field_is_checked_by_its_own_oracle(read_id):
    computed = _read_setup(read_id)["read"]["expect"]["computed"]
    named = {
        path.split(".", 2)[2]: check["oracle"]["name"]
        for path, check in computed.items()
    }
    assert named == {
        "theta": "cuped_theta",
        "covariate_coverage_pct": "cuped_coverage_pct",
        "variance_reduction_pct": "cuped_variance_reduction_pct",
        "adjusted_effect": "cuped_adjusted_effect",
        "unadjusted_effect": "cuped_unadjusted_effect",
        "adjusted_se": "cuped_adjusted_se",
        "unadjusted_se": "cuped_unadjusted_se",
        "adjusted_control_mean": "cuped_adjusted_mean",
        "adjusted_treatment_mean": "cuped_adjusted_mean",
        "adjusted_p_value": "cuped_adjusted_p",
        # One treatment: a correction across the metric's treatments leaves its
        # p-value as it is (Benjamini-Hochberg of one p is that p).
        "corrected_p_value": "cuped_adjusted_p",
        "adjusted_ci_lower": "cuped_adjusted_interval",
        "adjusted_ci_upper": "cuped_adjusted_interval",
    }


@pytest.mark.parametrize("read_id", READS)
def test_the_literals_follow_from_the_oracles(read_id):
    setup = _read_setup(read_id)
    counts = _sent(setup)
    expected = setup["read"]["expect"]["json"]
    found = _all(counts, expected["confidence_level"])
    assert expected["metrics.0.is_significant"] is (
        found["adjusted_p_value"] < 1 - expected["confidence_level"]
    )
    assert expected["metrics.0.control_sample_size"] == counts[0]
    assert expected["metrics.0.treatment_sample_size"] == counts[3]
    assert expected["metrics.0.unavailable_reason"] is None
    assert expected["method"] == expected["metrics.0.method"] == "cuped"
    exact = {"covariate_coverage_pct"} | (
        {"theta", "variance_reduction_pct"} if read_id == "cuped-new-users" else set()
    )
    for path, check in setup["read"]["expect"]["computed"].items():
        field = path.split(".", 2)[2]
        assert check["rel"] == (0 if field in exact else 1.0e-9), path


def test_returning_users_narrow_the_interval_and_new_users_do_not():
    new, returning = _all(NEW_USERS), _all(RETURNING)
    assert new["adjusted_se"] == new["unadjusted_se"]
    assert returning["adjusted_se"] < 0.7 * returning["unadjusted_se"]
    assert returning["variance_reduction_pct"] > 50


# ---------------------------------------------------------------------------
# Each defect's answer fails the step
# ---------------------------------------------------------------------------
DEFECTS = (
    "theta-zero",
    "history-too-late",
    "sign-flipped",
    "control-alone",
    "arms-lumped",
    "overall-mean-control",
    "divisor-n",
    "t-test",
)


def _answer(counts: Tuple[int, ...], defect: str = "") -> Dict[str, float]:
    """The response's numbers, computed on the users written out one by one
    (numpy, not the oracles), right or with one defect."""
    yc, xc = _rows(*counts[:3])
    yt, xt = _rows(*counts[3:])
    if defect == "history-too-late":
        # Sent after the assignment, the history is not read: X is 0 for all.
        xc, xt = np.zeros_like(xc), np.zeros_like(xt)
    y, x = np.concatenate([yc, yt]), np.concatenate([xc, xt])

    def centred(values: np.ndarray) -> np.ndarray:
        return values - values.mean()

    if defect == "theta-zero":
        theta = 0.0
    elif defect == "control-alone":
        sxx = float(centred(xc) @ centred(xc))
        theta = float(centred(xc) @ centred(yc)) / sxx if sxx else 0.0
    elif defect == "arms-lumped":
        sxx = float(centred(x) @ centred(x))
        theta = float(centred(x) @ centred(y)) / sxx if sxx else 0.0
    else:
        sxx = float(centred(xc) @ centred(xc) + centred(xt) @ centred(xt))
        sxy = float(centred(xc) @ centred(yc) + centred(xt) @ centred(yt))
        theta = sxy / sxx if sxx else 0.0
    sign = 1.0 if defect == "sign-flipped" else -1.0
    overall = xc.mean() if defect == "overall-mean-control" else x.mean()
    adjusted_c = yc.mean() + sign * theta * (xc.mean() - overall)
    adjusted_t = yt.mean() + sign * theta * (xt.mean() - overall)
    ddof = 0 if defect == "divisor-n" else 1
    variance = np.var(yc - theta * xc, ddof=ddof) / len(yc) + np.var(
        yt - theta * xt, ddof=ddof
    ) / len(yt)
    unadjusted = np.var(yc, ddof=ddof) / len(yc) + np.var(yt, ddof=ddof) / len(yt)
    effect = adjusted_t - adjusted_c
    se = math.sqrt(variance)
    if defect == "t-test":
        p = float(2 * stats.t.sf(abs(effect) / se, len(y) - 2))
    else:
        p = float(2 * stats.norm.sf(abs(effect) / se))
    half = float(stats.norm.ppf(0.975)) * se
    return {
        "theta": theta,
        "adjusted_control_mean": adjusted_c,
        "adjusted_treatment_mean": adjusted_t,
        "adjusted_effect": effect,
        "unadjusted_effect": yt.mean() - yc.mean(),
        "adjusted_se": se,
        "unadjusted_se": math.sqrt(unadjusted),
        "adjusted_p_value": p,
        "corrected_p_value": p,
        "adjusted_ci_lower": effect - half,
        "adjusted_ci_upper": effect + half,
        "variance_reduction_pct": 100 * (1 - variance / unadjusted),
        "covariate_coverage_pct": 100 * x.mean(),
    }


def _checks(read_id: str) -> Dict[str, Tuple[float, float]]:
    """Per checked field: the oracle's number and the step's tolerance."""
    computed = _read_setup(read_id)["read"]["expect"]["computed"]
    return {
        path.split(".", 2)[2]: (
            oracles.ORACLES[check["oracle"]["name"]](**check["oracle"]["args"]),
            check["rel"],
        )
        for path, check in computed.items()
    }


@pytest.mark.parametrize("read_id", READS)
def test_the_rows_give_the_oracles_numbers(read_id):
    by_rows = _answer(_sent(_read_setup(read_id)))
    for field, (wanted, _) in _checks(read_id).items():
        assert by_rows[field] == pytest.approx(wanted, rel=1e-12, abs=1e-15), field


def _caught(read_id: str, defect: str) -> List[str]:
    """The checked fields a defect's answer moves outside their tolerance."""
    answer = _answer(_sent(_read_setup(read_id)), defect)
    return [
        field
        for field, (wanted, rel) in _checks(read_id).items()
        if abs(answer[field] - wanted) > rel * abs(wanted)
    ]


#: Per read, the defects its numbers can see: with no history, every defect in
#: theta or in how history is read gives the right answer.
SEEN = {
    "cuped-new-users": ("divisor-n", "t-test"),
    "cuped-returning-users": DEFECTS,
}


@pytest.mark.parametrize("read_id", READS)
@pytest.mark.parametrize("defect", DEFECTS)
def test_each_tolerance_refuses_every_defect_the_read_can_see(read_id, defect):
    caught = _caught(read_id, defect)
    if defect in SEEN[read_id]:
        assert caught, f"{defect} passes {read_id}"
    else:
        assert caught == [], (defect, caught)


def test_the_defects_move_the_returning_users_numbers_far_past_the_tolerance():
    """The nearest wrong answers are thousands of tolerances away."""
    right = _answer(RETURNING)
    assert (
        _answer(RETURNING, "arms-lumped")["adjusted_effect"] / right["adjusted_effect"]
        < 0.99
    )
    assert _answer(RETURNING, "divisor-n")["adjusted_se"] / right["adjusted_se"] == (
        pytest.approx(math.sqrt(255 / 256), rel=1e-12)
    )
    assert math.sqrt(255 / 256) < 0.9981
    assert (
        _answer(RETURNING, "t-test")["adjusted_p_value"] / right["adjusted_p_value"]
        > 1.01
    )
    assert _answer(RETURNING, "theta-zero")["adjusted_effect"] == 0.0625
    assert _answer(RETURNING, "control-alone")["theta"] == 0.5
    # History the server does not read is theta 0 with no coverage.
    assert set(_caught("cuped-returning-users", "history-too-late")) == set(
        _caught("cuped-returning-users", "theta-zero")
    ) | {"covariate_coverage_pct"}


@pytest.fixture
def execute_module(monkeypatch):
    """docs_runner.execute with a stand-in for Playwright, which the unit job lacks."""
    sync_api = types.ModuleType("playwright.sync_api")

    class _Expect:
        def __call__(self, *args, **kwargs):
            raise AssertionError("no browser here")

        def set_options(self, **kwargs):
            return None

    sync_api.Error = type("Error", (Exception,), {})
    sync_api.Browser = sync_api.Page = sync_api.Playwright = object
    sync_api.expect = _Expect()
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")
    yield module
    sys.modules.pop("docs_runner.execute", None)


class _Answer:
    def __init__(self, body: Dict[str, Any]):
        self.status = 200
        self._body = body

    def json(self):
        return self._body


class _Api:
    def __init__(self, body: Dict[str, Any]):
        self.body = body
        self.paths: List[str] = []

    def fetch(self, path, method, headers, data):
        self.paths.append(path)
        return _Answer(self.body)


def _body(read_id: str, numbers: Dict[str, float]) -> Dict[str, Any]:
    """The answer the step reads: its literals, and *numbers* for the rest."""
    expected = _read_setup(read_id)["read"]["expect"]["json"]
    row = {
        k.split(".", 2)[2]: v for k, v in expected.items() if k.startswith("metrics.0.")
    }
    row.update(numbers)
    top = {k: v for k, v in expected.items() if not k.startswith("metrics.")}
    return {**top, "metrics": [row]}


def _run(execute_module, tmp_path, read_id: str, body: Dict[str, Any]):
    from docs_runner.model import Step
    from docs_runner.stacks import Running

    step = Step.model_validate(_steps()[_index(read_id)])
    runner = execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )
    running = Running(name="compose-dev", base_url="http://dash/", api_url="http://api")
    runner.tokens[(running.api_url, "admin")] = "a-token-for-the-test"
    runner.values = {"experiment-id": "e1", "cuped-experiment-id": "e6"}
    api = _Api(body)
    observed, _ = runner._api_step(step, running, api, "experiment-results", 1)
    return observed, api.paths


@pytest.mark.parametrize("read_id", READS)
def test_the_runner_passes_the_formulas_answer_and_fails_each_defects(
    execute_module, tmp_path, read_id
):
    counts = _sent(_read_setup(read_id))
    observed, paths = _run(
        execute_module, tmp_path, read_id, _body(read_id, _answer(counts))
    )
    assert observed == "status 200"
    assert re.fullmatch(r"/api/v1/results/e[16]/cuped", paths[0])
    for defect in SEEN[read_id]:
        with pytest.raises(execute_module._Failed) as failed:
            _run(
                execute_module,
                tmp_path,
                read_id,
                _body(read_id, _answer(counts, defect)),
            )
        assert " gives " in failed.value.observed, defect
