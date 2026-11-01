"""
Generate ``frontend/src/tests/fixtures/results-intervals.json``: server-computed
metric results for the dashboard's interval and impact tests (#1496, S2-b).

Every metric in the file is what ``GET /api/v1/results/{experiment_id}`` puts in
``metrics`` for the counts or per-user values given here: each case runs the
results path's own builder (``binomial_metric_result`` for a conversion metric,
``per_user_mean_metric_result`` for a revenue, count, duration or custom
metric, exactly as ``AnalysisService`` calls them, with ``alpha = 1 - level``),
then the response schema (``MetricResult``) serialises it as the API does.
Nothing in a metric is typed by hand, so a dashboard test that renders one of
these is checking the server's numbers, not numbers someone chose.

The cases are the ones the S2-b words name (plan-S, ``v1.2-ux-s2b-words``):
F1's 30/1000 against 1/1000 at 0.99, a relative bound above +1,000,000% and
one below -1,000,000%, a mean whose control is too uncertain around 0 for a
relative interval, a proportion control with no conversions, a treatment with
no conversions (its relative lower bound is exactly -100%), an arm with no
users, three variants under Benjamini-Hochberg, and the two boundary cases
where the interval and the test disagree (3/50 against 18/100; 128/1000
against 100/1000). Also the notes' p right at the threshold (45/200 against
5/50 at 0.95; 15/200 against 2/150 and 67/200 against 68/300 at 0.99), and the
three shapes the interval footnote tells apart (two treatments with and
without a correction, one treatment under Benjamini-Hochberg).

Usage (from the repository root, venv active):

    python -m backend.scripts.generate_results_interval_fixtures          # rewrite the file
    python -m backend.scripts.generate_results_interval_fixtures --check  # exit 1 on drift

``backend/tests/unit/scripts/test_results_interval_fixtures.py`` runs the same
comparison in every backend job (no database, no ``.git``), with a relative
tolerance of 1e-9 on every number: scipy's critical values may differ in the
last bit between platforms, and the dashboard shows at most two decimals. A
change to the results path that moves any number further than that fails it
until the file is regenerated, and then the dashboard tests say whether the
page still reads right.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from backend.app.schemas.results import MetricResult
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
    per_user_mean_metric_result,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_PATH = (
    REPO_ROOT / "frontend" / "src" / "tests" / "fixtures" / "results-intervals.json"
)

#: Ids are derived from the case and variant names, so they never change.
_NAMESPACE = uuid.UUID("5b1c2b0e-6f1d-4c55-9a1e-14960000b2b0")

#: ``(name, is_control, users, converting users)``
ProportionArm = Tuple[str, bool, int, int]
#: ``(name, is_control, users, users who sent the event, per-user values of
#: the users who have one)``; every other user's value is 0.
MeanArm = Tuple[str, bool, int, int, Sequence[float]]


def _id(*parts: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, "/".join(parts)))


def _metric(case: str, name: str, metric_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=_id(case, "metric"),
        name=name,
        metric_type=metric_type,
        is_primary=True,
        lower_is_better=False,
    )


def _proportion(
    case: str,
    name: str,
    metric_type: str,
    arms: Sequence[ProportionArm],
    level: float,
    correction: str,
) -> Dict[str, Any]:
    return binomial_metric_result(
        [
            (BinomialVariant(_id(case, arm), arm, is_control), n, x)
            for arm, is_control, n, x in arms
        ],
        1.0 - level,
        correction,
        metric=_metric(case, name, metric_type),
    )


def _mean(
    case: str,
    name: str,
    metric_type: str,
    arms: Sequence[MeanArm],
    level: float,
    correction: str,
) -> Dict[str, Any]:
    return per_user_mean_metric_result(
        [
            (BinomialVariant(_id(case, arm), arm, is_control), n, sent, list(values))
            for arm, is_control, n, sent, values in arms
        ],
        1.0 - level,
        correction,
        metric=_metric(case, name, metric_type),
    )


#: ``(case id, what it shows, level, correction, builder)``. The builder gets
#: the case id, the level and the correction.
Case = Tuple[str, str, float, str, Callable[[str, float, str], Dict[str, Any]]]

CASES: List[Case] = [
    (
        "f1_wide_proportion",
        "F1: 30/1000 against 1/1000 at 0.99: a wide relative interval, +226.1% to +26,001.9%",
        0.99,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 1000, 1), ("treatment", False, 1000, 30)],
            lv,
            cm,
        ),
    ),
    (
        "f2_above_a_million",
        "F2: 500/1000 against 1/100000 at 0.99: an upper relative bound above +1,000,000%",
        0.99,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 100_000, 1), ("treatment", False, 1000, 500)],
            lv,
            cm,
        ),
    ),
    (
        "f2_below_a_million",
        "F2 mirror: a custom mean whose control is near 0 and treatment far below it: a "
        "lower relative bound below -1,000,000%",
        0.95,
        "none",
        lambda c, lv, cm: _mean(
            c,
            "Balance",
            "custom",
            [
                ("control", True, 50, 50, [0.001] * 49 + [0.0012]),
                ("treatment", False, 50, 50, [-5.0, -15.0] * 25),
            ],
            lv,
            cm,
        ),
    ),
    (
        "f3_mean_no_relative_interval",
        "F3: a revenue mean whose control's mean plus or minus its margin reaches 0: the "
        "relative interval is null, the relative change and the difference's interval are not",
        0.95,
        "none",
        lambda c, lv, cm: _mean(
            c,
            "Order value",
            "revenue",
            [
                ("control", True, 20, 1, [50.0]),
                ("treatment", False, 20, 2, [40.0, 30.0]),
            ],
            lv,
            cm,
        ),
    ),
    (
        "f3_mean_no_relative_interval_99",
        "F3 at 0.99: the same arms",
        0.99,
        "none",
        lambda c, lv, cm: _mean(
            c,
            "Order value",
            "revenue",
            [
                ("control", True, 20, 1, [50.0]),
                ("treatment", False, 20, 2, [40.0, 30.0]),
            ],
            lv,
            cm,
        ),
    ),
    (
        "f4_control_no_conversions",
        "F4: 5/200 against 0/200: no relative change or interval, a difference and an impact",
        0.95,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 200, 0), ("treatment", False, 200, 5)],
            lv,
            cm,
        ),
    ),
    (
        "f5_treatment_no_conversions",
        "F5: 0/50 against 10/50: the relative lower bound is exactly -100%",
        0.95,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 50, 10), ("treatment", False, 50, 0)],
            lv,
            cm,
        ),
    ),
    (
        "f5_low_counts",
        "6/200 against 2/200: the wide interval #1517's description quotes, -34.2% to +1,206.3%",
        0.95,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 200, 2), ("treatment", False, 200, 6)],
            lv,
            cm,
        ),
    ),
    (
        "f6_empty_treatment_proportion",
        "F6: a treatment with no users against 18/100 (the server's relative change is -100%)",
        0.95,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 100, 18), ("treatment", False, 0, 0)],
            lv,
            cm,
        ),
    ),
    (
        "f6_empty_treatment_mean",
        "F6 for a mean: a treatment with no users",
        0.95,
        "none",
        lambda c, lv, cm: _mean(
            c,
            "Articles read",
            "count",
            [
                ("control", True, 100, 30, [1.0] * 20 + [2.0] * 10),
                ("treatment", False, 0, 0, []),
            ],
            lv,
            cm,
        ),
    ),
    (
        "f8_three_variants_bh",
        "F8 and C16: three variants under Benjamini-Hochberg (k = 2): every interval "
        "unadjusted; one treatment's interval excludes 0 but its adjusted p is not significant",
        0.95,
        "benjamini_hochberg",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [
                ("control", True, 1000, 100),
                ("one_page", False, 1000, 131),
                ("two_step", False, 1000, 104),
            ],
            lv,
            cm,
        ),
    ),
    (
        "c16_significant_reaching_zero",
        "C16 (PS-3): 3/50 against 18/100: significant (Fisher p 0.0492) while Newcombe's "
        "interval reaches 0",
        0.95,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 100, 18), ("treatment", False, 50, 3)],
            lv,
            cm,
        ),
    ),
    (
        "c16_not_significant_excluding_zero",
        "C16: 128/1000 against 100/1000: not significant (Fisher p 0.0573) while the "
        "interval excludes 0",
        0.95,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 1000, 100), ("treatment", False, 1000, 128)],
            lv,
            cm,
        ),
    ),
    (
        "c16_significant_p_near_alpha",
        "C16 at the threshold: 45/200 against 5/50: significant (Fisher p 0.04981, which "
        "3 decimals would print as 0.050) while Newcombe's interval reaches 0",
        0.95,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 50, 5), ("treatment", False, 200, 45)],
            lv,
            cm,
        ),
    ),
    (
        "c16_significant_p_near_alpha_99",
        "C16 at the threshold at 0.99: 15/200 against 2/150: significant (Fisher p "
        "0.0099972, which 4 decimals would print as 0.0100) while the interval reaches 0",
        0.99,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 150, 2), ("treatment", False, 200, 15)],
            lv,
            cm,
        ),
    ),
    (
        "c16_not_significant_p_near_alpha_99",
        "C16 at the threshold at 0.99: 67/200 against 68/300: not significant (Fisher p "
        "0.0100040) while the interval excludes 0",
        0.99,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 300, 68), ("treatment", False, 200, 67)],
            lv,
            cm,
        ),
    ),
    (
        "f13_two_treatments_no_correction",
        "F13 under no correction: the three-variant arms with correction_method none, so "
        "nothing is adjusted",
        0.95,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [
                ("control", True, 1000, 100),
                ("one_page", False, 1000, 131),
                ("two_step", False, 1000, 104),
            ],
            lv,
            cm,
        ),
    ),
    (
        "f13_one_treatment_bh",
        "F13 with one treatment under Benjamini-Hochberg (the default): the correction "
        "changes nothing, so no p-value is shown adjusted; 128/1000 against 100/1000",
        0.95,
        "benjamini_hochberg",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 1000, 100), ("treatment", False, 1000, 128)],
            lv,
            cm,
        ),
    ),
    (
        "f10_count_events",
        "F10: a count metric over 10,000 users per arm: an impact in thousands of events",
        0.95,
        "none",
        lambda c, lv, cm: _mean(
            c,
            "Articles read",
            "count",
            [
                ("control", True, 10_000, 6000, [1.0] * 5000 + [2.0] * 1000),
                (
                    "treatment",
                    False,
                    10_000,
                    7300,
                    [1.0] * 5200 + [2.0] * 1800 + [3.0] * 300,
                ),
            ],
            lv,
            cm,
        ),
    ),
    (
        "f10_revenue_values",
        "F10: a revenue metric over 4,000 users per arm: an impact in thousands, to 2 places",
        0.95,
        "none",
        lambda c, lv, cm: _mean(
            c,
            "Order value",
            "revenue",
            [
                ("control", True, 4000, 220, [40.0] * 120 + [80.0] * 100),
                (
                    "treatment",
                    False,
                    4000,
                    300,
                    [40.0] * 150 + [80.0] * 130 + [200.0] * 20,
                ),
            ],
            lv,
            cm,
        ),
    ),
    (
        "f10_zero_impact",
        "F10: 10/100 against 10/100: an impact of 0 with an interval",
        0.95,
        "none",
        lambda c, lv, cm: _proportion(
            c,
            "Signup",
            "conversion",
            [("control", True, 100, 10), ("treatment", False, 100, 10)],
            lv,
            cm,
        ),
    ),
    (
        "d_mean_control_zero",
        "(d): a count metric whose control sent no event: no relative change, a difference "
        "and an impact",
        0.95,
        "none",
        lambda c, lv, cm: _mean(
            c,
            "Articles read",
            "count",
            [
                ("control", True, 100, 0, []),
                ("treatment", False, 100, 10, [1.0] * 10),
            ],
            lv,
            cm,
        ),
    ),
    (
        "before_first_value",
        "A revenue metric before the first purchase: every total 0, no interval anywhere",
        0.95,
        "none",
        lambda c, lv, cm: _mean(
            c,
            "Order value",
            "revenue",
            [("control", True, 300, 0, []), ("treatment", False, 300, 0, [])],
            lv,
            cm,
        ),
    ),
    (
        "duration_values",
        "A duration metric: an impact in duration",
        0.95,
        "none",
        lambda c, lv, cm: _mean(
            c,
            "Watch time",
            "duration",
            [
                ("control", True, 500, 200, [30.0] * 150 + [90.0] * 50),
                ("treatment", False, 500, 230, [30.0] * 160 + [90.0] * 70),
            ],
            lv,
            cm,
        ),
    ),
]


def _schema_json(result: Dict[str, Any]) -> Dict[str, Any]:
    """The metric as the API serialises it: through ``MetricResult``."""
    return MetricResult.model_validate(result).model_dump(mode="json")


def build_fixtures() -> Dict[str, Any]:
    cases: Dict[str, Any] = {}
    for case_id, what, level, correction, build in CASES:
        cases[case_id] = {
            "what": what,
            "confidence_level": level,
            "correction_method": correction,
            "metric": _schema_json(build(case_id, level, correction)),
        }
    return {
        "generated_by": "python -m backend.scripts.generate_results_interval_fixtures",
        "cases": cases,
    }


def render(fixtures: Dict[str, Any]) -> str:
    return json.dumps(fixtures, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def differences(expected: Any, actual: Any, path: str = "$") -> List[str]:
    """Where two parsed fixture documents differ: numbers within a relative
    1e-9 (and an absolute 1e-12 near 0) agree, everything else must be equal."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return [] if expected == actual else [f"{path}: {expected!r} != {actual!r}"]
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-12):
            return []
        return [f"{path}: {expected!r} != {actual!r}"]
    if isinstance(expected, dict) and isinstance(actual, dict):
        out: List[str] = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                out.append(f"{path}.{key}: present on one side only")
            else:
                out.extend(differences(expected[key], actual[key], f"{path}.{key}"))
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: {len(expected)} items != {len(actual)} items"]
        out = []
        for index, (e, a) in enumerate(zip(expected, actual)):
            out.extend(differences(e, a, f"{path}[{index}]"))
        return out
    return [] if expected == actual else [f"{path}: {expected!r} != {actual!r}"]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the committed file and exit 1 if it differs",
    )
    parser.add_argument("--output", type=Path, default=FIXTURES_PATH)
    args = parser.parse_args(argv)

    fixtures = build_fixtures()
    if args.check:
        current = (
            json.loads(args.output.read_text(encoding="utf-8"))
            if args.output.exists()
            else {}
        )
        drift = differences(json.loads(render(fixtures)), current)
        if drift:
            print(
                f"{args.output} is out of date: run "
                "`python -m backend.scripts.generate_results_interval_fixtures`",
                file=sys.stderr,
            )
            for line in drift[:20]:
                print(f"  {line}", file=sys.stderr)
            return 1
        print(f"{args.output} is current")
        return 0
    args.output.write_text(render(fixtures), encoding="utf-8")
    print(f"wrote {args.output}: {len(fixtures['cases'])} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
