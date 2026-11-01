"""
``frontend/src/tests/fixtures/results-intervals.json`` is what the server
computes (#1496, S2-b).

The dashboard's interval and impact tests render these metrics and assert the
text the page writes. They are only worth something if the numbers are the
results path's own, so the file is regenerated here, in-process, from the same
builders ``/results`` calls, and compared number by number (relative 1e-9:
scipy's critical values may differ in the last bit between platforms; the page
shows at most two decimals). It reads the file by path and never shells out,
so it runs the same in a checkout, in CI and in ``scripts/core_build.sh``'s
copy.

It also pins the numbers the S2-b words quote, so a fixture that drifted
together with the generator (both edited in one change) still fails.
"""

from __future__ import annotations

import json

import pytest

from backend.scripts import generate_results_interval_fixtures as generator

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def committed():
    assert generator.FIXTURES_PATH.exists(), (
        "frontend/src/tests/fixtures/results-intervals.json is missing; run "
        "`python -m backend.scripts.generate_results_interval_fixtures`"
    )
    return json.loads(generator.FIXTURES_PATH.read_text(encoding="utf-8"))


def _treatment(committed, case: str, name: str = "treatment"):
    variants = committed["cases"][case]["metric"]["variants"]
    return next(v for v in variants if v["variant_name"] == name)


def test_the_committed_fixtures_are_what_the_server_computes(committed):
    generated = json.loads(generator.render(generator.build_fixtures()))
    drift = generator.differences(generated, committed)
    assert drift == [], (
        "results-intervals.json is out of date: run "
        "`python -m backend.scripts.generate_results_interval_fixtures`\n"
        + "\n".join(drift[:20])
    )


def test_the_comparison_fails_on_a_moved_number(committed):
    """The drift check is not vacuous: one bound moved by 1e-6 is a drift."""
    moved = json.loads(json.dumps(committed))
    ci = _treatment(moved, "f1_wide_proportion")["relative_improvement_pct_ci"]
    ci[1] = ci[1] * (1 + 1e-6)
    assert generator.differences(committed, moved) != []
    gone = json.loads(json.dumps(committed))
    del _treatment(gone, "f4_control_no_conversions")["incremental_impact"]
    assert generator.differences(committed, gone) != []


def test_the_quoted_numbers(committed):
    """The numbers the words and the plan quote, from the server."""
    f1 = _treatment(committed, "f1_wide_proportion")
    assert [round(b, 1) for b in f1["relative_improvement_pct_ci"]] == [226.1, 26001.9]
    assert round(f1["incremental_impact"]) == 29

    low = _treatment(committed, "f5_low_counts")
    assert [round(b, 1) for b in low["relative_improvement_pct_ci"]] == [-34.2, 1206.3]

    above = _treatment(committed, "f2_above_a_million")["relative_improvement_pct_ci"]
    assert above[1] > 1_000_000
    below = _treatment(committed, "f2_below_a_million")["relative_improvement_pct_ci"]
    assert below[0] < -1_000_000

    f3 = _treatment(committed, "f3_mean_no_relative_interval")
    assert f3["relative_improvement_pct_ci"] is None
    assert f3["relative_improvement_pct"] == pytest.approx(40.0)
    assert f3["absolute_difference_ci"] is not None

    f4 = _treatment(committed, "f4_control_no_conversions")
    assert f4["relative_improvement_pct"] is None
    assert f4["relative_improvement_pct_ci"] is None
    assert f4["absolute_difference"] == pytest.approx(0.025)

    floor = _treatment(committed, "f5_treatment_no_conversions")
    assert floor["relative_improvement_pct_ci"][0] == pytest.approx(-100.0)

    empty = _treatment(committed, "f6_empty_treatment_proportion")
    assert empty["sample_size"] == 0
    assert empty["incremental_impact"] is None

    reaching = _treatment(committed, "c16_significant_reaching_zero")
    assert reaching["is_significant"] is True
    assert reaching["p_value"] == pytest.approx(0.049246, abs=1e-6)
    low, high = reaching["absolute_difference_ci"]
    assert low < 0 < high

    excluding = _treatment(committed, "c16_not_significant_excluding_zero")
    assert excluding["is_significant"] is False
    assert excluding["absolute_difference_ci"][0] > 0

    one_page = _treatment(committed, "f8_three_variants_bh", "one_page")
    assert one_page["p_value"] < 0.05 < one_page["adjusted_p_value"]
    assert one_page["absolute_difference_ci"][0] > 0

    # Under no correction the same arms are decided on the raw p.
    raw = _treatment(committed, "f13_two_treatments_no_correction", "one_page")
    assert raw["adjusted_p_value"] is None
    assert raw["is_significant"] is True

    # One treatment under Benjamini-Hochberg: the correction changes nothing.
    alone = _treatment(committed, "f13_one_treatment_bh")
    assert alone["adjusted_p_value"] == pytest.approx(alone["p_value"])


@pytest.mark.parametrize(
    ("case", "level", "p", "significant"),
    [
        # A p 3 decimals would print as the threshold.
        ("c16_significant_p_near_alpha", 0.95, 0.049814, True),
        # A p 4 decimals would print as the threshold.
        ("c16_significant_p_near_alpha_99", 0.99, 0.0099972, True),
        ("c16_not_significant_p_near_alpha_99", 0.99, 0.0100040, False),
    ],
)
def test_the_cases_at_the_threshold(committed, case, level, p, significant):
    """The notes' near-threshold cases are what the server computes: p just
    either side of 1 - level, with an interval that disagrees with it."""
    treatment = _treatment(committed, case)
    assert committed["cases"][case]["confidence_level"] == level
    assert treatment["p_value"] == pytest.approx(p, abs=5e-7)
    assert treatment["is_significant"] is significant
    low, high = treatment["absolute_difference_ci"]
    excludes_zero = low > 0 or high < 0
    assert excludes_zero is not significant
