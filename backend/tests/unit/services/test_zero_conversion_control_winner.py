"""
A significant treatment against a control with no conversions is the winner
(engine 1.11.0).

``binomial_metric_result`` named a winner only when the treatment was
significant and its ``relative_improvement_pct`` was above 0 (below 0 when
lower is better).  Against a control with no conversions the relative change
is infinite and is reported as None, so no treatment was ever named, at any
p-value: a control with 0 of 28 users converting against a treatment with 21
of 32 (Fisher p 2.9e-8) gave no winner, and the summary said "No
statistically significant difference on Purchase yet".  The side of the
control a treatment is on now comes from ``absolute_difference``, its rate
minus the control's, which ``mean_metric_result`` already reads for a mean
(the difference of the means).  ``relative_improvement_pct`` stays None: it
is undefined.

The summary's KEEP_CONTROL rule had the same hole on the other side:
treatments all significantly WORSE than a control at 0 (for a proportion only
a lower-is-better metric can have that: errors, say; for a mean, a control
mean of 0) read as "no significant difference".  It reads the absolute
difference where the relative change is None.

Significance is decided first, after the correction, as before: the
direction only picks which significant treatments win.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from backend.app.services.analysis_service import AnalysisService
from backend.app.services.sufficient_stats_analysis import (
    CORRECTION_METHODS,
    BinomialVariant,
    binomial_metric_result,
    per_user_mean_metric_result,
)
from backend.tests.unit.services import (
    test_binomial_metric_result_characterisation as characterisation,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

_CONTROL = "ffffffff-0000-4000-8000-000000000002"
_T1 = "00000000-0000-4000-8000-000000000001"
_T2 = "77777777-0000-4000-8000-000000000003"

CONTROL = BinomialVariant(_CONTROL, "control", True)
T1 = BinomialVariant(_T1, "t1", False)
T2 = BinomialVariant(_T2, "t2", False)

#: The split measured in CI: 0 of 28 control users converted, 21 of 32
#: treatment users (Fisher p 2.9e-8).
ZERO_CONTROL_WIN = [(CONTROL, 28, 0), (T1, 32, 21)]
#: Nobody converted in either arm (p 1.0).
BOTH_ZERO = [(CONTROL, 500, 0), (T1, 500, 0)]
#: 5 of 200 against 0 of 200: p 0.061, not significant at 0.05.
ZERO_CONTROL_SMALL = [(CONTROL, 200, 0), (T1, 200, 5)]
#: Two treatments, both significant against a control at 0 (p 4.5e-4 and
#: 1e-6 uncorrected): the higher rate wins.
ZERO_CONTROL_TWO_WINS = [(CONTROL, 400, 0), (T1, 400, 12), (T2, 400, 20)]
#: 6 of 200 against 0 of 200 (p 0.030) beside a treatment at 0 (p 1.0):
#: significant uncorrected, not after Bonferroni or Benjamini-Hochberg (0.060).
ZERO_CONTROL_BORDERLINE = [(CONTROL, 200, 0), (T1, 200, 6), (T2, 200, 0)]


def _metric(lower_is_better: bool = False, name: str = "Purchase") -> Any:
    return SimpleNamespace(
        id="33333333-0000-4000-8000-000000000003",
        name=name,
        metric_type="conversion",
        is_primary=True,
        lower_is_better=lower_is_better,
    )


def _proportion(
    counts, correction: str = "none", lower_is_better: bool = False, **kwargs: Any
) -> Dict[str, Any]:
    return binomial_metric_result(
        counts, 0.05, correction, metric=_metric(lower_is_better, **kwargs)
    )


def _variant(result: Dict[str, Any], variant_id: str) -> Dict[str, Any]:
    return next(v for v in result["variants"] if v["variant_id"] == variant_id)


def _decide(result: Dict[str, Any], adequate: bool = True) -> Dict[str, Any]:
    return AnalysisService._summarise_decision(None, [result], adequate)


class TestTheWinner:
    @pytest.mark.parametrize("correction", CORRECTION_METHODS)
    def test_a_significant_treatment_against_a_control_at_zero_wins(self, correction):
        result = _proportion(ZERO_CONTROL_WIN, correction)
        treatment = _variant(result, _T1)
        assert treatment["is_significant"] is True
        assert treatment["p_value"] < 1e-7
        # Undefined against a rate of 0, and still reported as None.
        assert treatment["relative_improvement_pct"] is None
        assert treatment["absolute_difference"] == pytest.approx(21 / 32)
        assert result["winning_variant_id"] == _T1

    def test_nobody_converting_in_either_arm_is_no_winner(self):
        for correction in CORRECTION_METHODS:
            result = _proportion(BOTH_ZERO, correction)
            treatment = _variant(result, _T1)
            assert treatment["is_significant"] is False
            assert treatment["absolute_difference"] == 0.0
            assert result["winning_variant_id"] is None

    def test_a_small_rise_from_zero_that_is_not_significant_is_no_winner(self):
        for correction in CORRECTION_METHODS:
            result = _proportion(ZERO_CONTROL_SMALL, correction)
            treatment = _variant(result, _T1)
            assert treatment["p_value"] == pytest.approx(0.0609, abs=1e-4)
            assert treatment["is_significant"] is False
            assert result["winning_variant_id"] is None

    @pytest.mark.parametrize("correction", CORRECTION_METHODS)
    def test_of_two_significant_treatments_the_higher_rate_wins(self, correction):
        result = _proportion(ZERO_CONTROL_TWO_WINS, correction)
        assert _variant(result, _T1)["is_significant"] is True
        assert _variant(result, _T2)["is_significant"] is True
        assert result["winning_variant_id"] == _T2

    def test_the_correction_decides_significance_before_the_direction(self):
        assert _proportion(ZERO_CONTROL_BORDERLINE, "none")["winning_variant_id"] == (
            _T1
        )
        for correction in ("bonferroni", "benjamini_hochberg"):
            result = _proportion(ZERO_CONTROL_BORDERLINE, correction)
            treatment = _variant(result, _T1)
            assert treatment["adjusted_p_value"] == pytest.approx(0.0602, abs=1e-4)
            assert treatment["is_significant"] is False
            assert result["winning_variant_id"] is None

    def test_a_rise_from_zero_is_never_a_win_when_lower_is_better(self):
        for correction in CORRECTION_METHODS:
            result = _proportion(ZERO_CONTROL_WIN, correction, lower_is_better=True)
            assert _variant(result, _T1)["is_significant"] is True
            assert result["winning_variant_id"] is None

    def test_a_treatment_with_no_users_is_never_the_winner(self):
        # The characterisation's edges: treatment A has no users, treatment B
        # converts 12 of 400 against a control at 0 of 500.
        counts = [
            (T1, 0, 0),
            (CONTROL, 500, 0),
            (T2, 400, 12),
        ]
        for alpha in (0.05, 0.10):
            for correction in CORRECTION_METHODS:
                result = binomial_metric_result(
                    counts, alpha, correction, metric=_metric()
                )
                assert _variant(result, _T1)["absolute_difference"] is None
                assert _variant(result, _T1)["is_significant"] is False
                assert result["winning_variant_id"] == _T2


class TestTheSummary:
    def test_the_winner_is_named_and_shipped(self):
        decision = _decide(_proportion(ZERO_CONTROL_WIN))
        assert decision == {
            "has_winner": True,
            "winning_variant_id": _T1,
            "recommendation": "SHIP_VARIANT",
            "recommendation_reason": "t1 is significantly better on Purchase.",
        }

    def test_before_the_minimum_sample_the_winner_is_named_and_testing_continues(
        self,
    ):
        decision = _decide(_proportion(ZERO_CONTROL_WIN), adequate=False)
        assert decision["has_winner"] is True
        assert decision["winning_variant_id"] == _T1
        assert decision["recommendation"] == "CONTINUE_TESTING"
        assert decision["recommendation_reason"].startswith(
            "t1 is significantly better on Purchase. Not every variant"
        )

    def test_nobody_converting_is_still_no_significant_difference(self):
        assert _decide(_proportion(BOTH_ZERO)) == {
            "has_winner": False,
            "winning_variant_id": None,
            "recommendation": "CONTINUE_TESTING",
            "recommendation_reason": (
                "No statistically significant difference on Purchase yet "
                "(1000 users assigned)."
            ),
        }

    def test_a_significant_rise_from_zero_keeps_the_control_when_lower_is_better(
        self,
    ):
        result = _proportion(ZERO_CONTROL_WIN, lower_is_better=True, name="Errors")
        assert _decide(result) == {
            "has_winner": False,
            "winning_variant_id": None,
            "recommendation": "KEEP_CONTROL",
            "recommendation_reason": (
                "Every treatment is significantly worse than control on "
                "Errors, where lower is better."
            ),
        }

    def test_a_mean_significantly_above_a_control_mean_of_zero_keeps_the_control(
        self,
    ):
        # A count metric where lower is better: no control user sent the
        # event, 30 of 50 treatment users sent it once (Welch p 2.6e-11).
        metric = SimpleNamespace(
            id="44444444-0000-4000-8000-000000000004",
            name="Errors",
            metric_type="count",
            is_primary=True,
            lower_is_better=True,
        )
        result = per_user_mean_metric_result(
            [(CONTROL, 50, 0, []), (T1, 50, 30, [1.0] * 30)],
            0.05,
            "none",
            metric=metric,
        )
        treatment = _variant(result, _T1)
        assert treatment["is_significant"] is True
        assert treatment["relative_improvement_pct"] is None
        assert treatment["absolute_difference"] == pytest.approx(0.6)
        assert result["winning_variant_id"] is None
        assert _decide(result)["recommendation"] == "KEEP_CONTROL"


class TestResults:
    """Through ``get_experiment_results``, on the characterisation's edges
    fixture, whose pinned hash moved for exactly this in 1.11.0."""

    @pytest.mark.parametrize("correction", ["none", "bonferroni"])
    def test_the_edges_fixture_names_treatment_b(self, correction, monkeypatch):
        output = characterisation.results_output("edges", 0.95, correction, monkeypatch)
        purchased: List[Dict[str, Any]] = [
            m for m in output["metrics"] if m["metric_name"] == "Purchased"
        ]
        assert purchased[0]["winning_variant_id"] == characterisation._TREATMENT_B
        summary = output["summary"]
        assert summary["has_winner"] is True
        assert summary["winning_variant_id"] == characterisation._TREATMENT_B
        # Treatment A has no users, under the minimum of 100.
        assert summary["recommendation"] == "CONTINUE_TESTING"
        reason = summary["recommendation_reason"]
        assert reason.startswith("treatment-b is significantly better on Purchased.")
        assert "No statistically significant difference" not in reason
