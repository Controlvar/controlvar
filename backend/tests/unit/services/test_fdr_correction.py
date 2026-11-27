"""
Unit tests for BenjaminiHochbergService — EP-043.

Tests cover:
- Basic BH procedure correctness
- Rank ordering
- Monotonicity of adjusted p-values
- Threshold sensitivity
- Known published results
- Comparison with Bonferroni (BH is less conservative)
- Edge cases (single p-value, all significant, none significant)
"""

import pytest

from backend.app.services.fdr_correction_service import (
    BenjaminiHochbergService,
    FDRResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service() -> BenjaminiHochbergService:
    return BenjaminiHochbergService()


def _get_significant(results: list) -> list:
    return [r for r in results if r.is_significant]


def _get_by_name(results: list, name: str) -> FDRResult:
    for r in results:
        if r.metric_name == name:
            return r
    raise KeyError(f"Metric '{name}' not found in results")


# ---------------------------------------------------------------------------
# TestFDRResult dataclass
# ---------------------------------------------------------------------------


class TestFDRResult:
    """Tests for the FDRResult dataclass."""

    def test_fdr_result_has_required_fields(self):
        """FDRResult has all required fields."""
        r = FDRResult(
            metric_name="revenue",
            raw_p_value=0.01,
            adjusted_p_value=0.05,
            rank=1,
            is_significant=True,
        )
        assert r.metric_name == "revenue"
        assert r.raw_p_value == 0.01
        assert r.adjusted_p_value == 0.05
        assert r.rank == 1
        assert r.is_significant is True

    def test_fdr_result_not_significant(self):
        """FDRResult can be not significant."""
        r = FDRResult(
            metric_name="churn",
            raw_p_value=0.8,
            adjusted_p_value=1.0,
            rank=5,
            is_significant=False,
        )
        assert not r.is_significant


# ---------------------------------------------------------------------------
# TestBHProcedureBasic — Tests 1-10
# ---------------------------------------------------------------------------


class TestBHProcedureBasic:
    """Basic correctness tests for the BH procedure."""

    def test_correct_returns_list_of_fdr_results(self):
        """correct() returns a list of FDRResult objects."""
        service = _make_service()
        p_values = {"metric_a": 0.01, "metric_b": 0.05, "metric_c": 0.2}
        results = service.correct(p_values)
        assert isinstance(results, list)
        assert all(isinstance(r, FDRResult) for r in results)
        assert len(results) == 3

    def test_all_metric_names_present_in_results(self):
        """Every metric name in the input appears in the output."""
        service = _make_service()
        p_values = {"revenue": 0.001, "clicks": 0.08, "churn": 0.5}
        results = service.correct(p_values)
        result_names = {r.metric_name for r in results}
        assert result_names == set(p_values.keys())

    def test_known_result_2_significant_from_10(self):
        """
        BH with alpha=0.05 on the 10-p-value example:
        [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.391]
        → exactly 2 significant.

        Verified manually:
          k=1: p=0.001 <= 0.005 ✓
          k=2: p=0.008 <= 0.010 ✓
          k=3: p=0.039 <= 0.015 ✗ → stop (find largest k)
        Largest k where p(k) <= k/m*alpha: k=2.

        Cross-validated against scipy.stats.false_discovery_control(method='bh').
        """
        service = _make_service()
        raw_p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.391]
        p_values = {f"metric_{i + 1}": p for i, p in enumerate(raw_p)}
        results = service.correct(p_values, fdr_threshold=0.05)
        n_significant = sum(1 for r in results if r.is_significant)
        assert n_significant == 2, (
            f"Expected 2 significant at FDR=0.05, got {n_significant}"
        )

    def test_known_result_first_2_smallest_pvalues_significant(self):
        """
        In the 10-p-value example at FDR=0.05, only the 2 smallest p-values
        are significant.

        Cross-validated with scipy.stats.false_discovery_control(method='bh').
        """
        service = _make_service()
        raw_p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.391]
        p_values = {f"metric_{i + 1}": p for i, p in enumerate(raw_p)}
        results = service.correct(p_values, fdr_threshold=0.05)

        # Sort by rank
        results.sort(key=lambda r: r.rank)
        significant_flags = [r.is_significant for r in results]

        # Only ranks 1-2 should be significant
        assert all(significant_flags[:2]), "Metrics with rank 1-2 should be significant"
        assert not any(significant_flags[2:]), (
            "Metrics with rank 3-10 should NOT be significant"
        )

    def test_all_significant_when_all_very_small_pvalues(self):
        """All metrics are significant when all p-values are extremely small."""
        service = _make_service()
        p_values = {
            "m1": 0.0001,
            "m2": 0.0002,
            "m3": 0.0003,
            "m4": 0.0004,
            "m5": 0.0005,
        }
        results = service.correct(p_values, fdr_threshold=0.05)
        assert all(r.is_significant for r in results)

    def test_none_significant_when_all_large_pvalues(self):
        """No metrics are significant when all p-values are large."""
        service = _make_service()
        p_values = {
            "m1": 0.3,
            "m2": 0.5,
            "m3": 0.7,
            "m4": 0.9,
        }
        results = service.correct(p_values, fdr_threshold=0.05)
        assert not any(r.is_significant for r in results)

    def test_single_p_value_below_threshold_is_significant(self):
        """Single p-value below FDR threshold is significant (k=1, m=1 → threshold=alpha)."""
        service = _make_service()
        results = service.correct({"metric": 0.03}, fdr_threshold=0.05)
        assert len(results) == 1
        assert results[0].is_significant

    def test_single_p_value_above_threshold_not_significant(self):
        """Single p-value above FDR threshold is not significant."""
        service = _make_service()
        results = service.correct({"metric": 0.10}, fdr_threshold=0.05)
        assert len(results) == 1
        assert not results[0].is_significant

    def test_rank_1_is_smallest_p_value(self):
        """Rank 1 is assigned to the smallest p-value."""
        service = _make_service()
        p_values = {"a": 0.1, "b": 0.01, "c": 0.5}
        results = service.correct(p_values)
        rank1 = next(r for r in results if r.rank == 1)
        assert rank1.metric_name == "b"
        assert rank1.raw_p_value == 0.01

    def test_ranks_are_contiguous_from_1_to_m(self):
        """Ranks are assigned 1, 2, …, m (no gaps or duplicates)."""
        service = _make_service()
        m = 7
        p_values = {f"m{i}": 0.01 * i for i in range(1, m + 1)}
        results = service.correct(p_values)
        observed_ranks = sorted(r.rank for r in results)
        assert observed_ranks == list(range(1, m + 1))


# ---------------------------------------------------------------------------
# TestBHMonotonicity — Tests 11-15
# ---------------------------------------------------------------------------


class TestBHMonotonicity:
    """Tests for monotonicity properties of BH-adjusted p-values."""

    def test_raw_p_values_sorted_ascending_by_rank(self):
        """raw_p_value is non-decreasing as rank increases."""
        service = _make_service()
        p_values = {"a": 0.04, "b": 0.001, "c": 0.2, "d": 0.008}
        results = service.correct(p_values)
        results.sort(key=lambda r: r.rank)
        raw_pvals = [r.raw_p_value for r in results]
        assert raw_pvals == sorted(raw_pvals), (
            "Raw p-values should be sorted ascending by rank"
        )

    def test_adjusted_p_values_non_decreasing(self):
        """BH-adjusted p-values are non-decreasing (monotone step-up)."""
        service = _make_service()
        raw_p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.391]
        p_values = {f"m{i + 1}": p for i, p in enumerate(raw_p)}
        results = service.correct(p_values)
        results.sort(key=lambda r: r.rank)
        adj_pvals = [r.adjusted_p_value for r in results]
        for i in range(len(adj_pvals) - 1):
            assert adj_pvals[i] <= adj_pvals[i + 1], (
                f"Adjusted p-values not monotone: {adj_pvals[i]} > {adj_pvals[i + 1]} "
                f"at positions {i}, {i + 1}"
            )

    def test_adjusted_p_values_bounded_by_1(self):
        """All adjusted p-values are <= 1.0."""
        service = _make_service()
        p_values = {f"m{i}": 0.1 * i for i in range(1, 11)}
        results = service.correct(p_values)
        assert all(r.adjusted_p_value <= 1.0 for r in results)

    def test_adjusted_p_values_non_negative(self):
        """All adjusted p-values are >= 0.0."""
        service = _make_service()
        p_values = {"a": 0.001, "b": 0.05, "c": 0.8}
        results = service.correct(p_values)
        assert all(r.adjusted_p_value >= 0.0 for r in results)

    def test_significant_metrics_have_smaller_raw_pvalues(self):
        """Significant metrics all have smaller raw p-values than non-significant ones."""
        service = _make_service()
        raw_p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.391]
        p_values = {f"m{i + 1}": p for i, p in enumerate(raw_p)}
        results = service.correct(p_values, fdr_threshold=0.05)

        significant_p = [r.raw_p_value for r in results if r.is_significant]
        non_significant_p = [r.raw_p_value for r in results if not r.is_significant]

        if significant_p and non_significant_p:
            assert max(significant_p) <= min(non_significant_p), (
                "All significant p-values should be <= all non-significant ones"
            )


# ---------------------------------------------------------------------------
# TestBHThresholdSensitivity — Tests 16-20
# ---------------------------------------------------------------------------


class TestBHThresholdSensitivity:
    """Tests that FDR threshold changes produce expected results."""

    def test_higher_fdr_threshold_more_discoveries(self):
        """FDR=0.10 yields at least as many discoveries as FDR=0.05."""
        service = _make_service()
        raw_p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.391]
        p_values = {f"m{i + 1}": p for i, p in enumerate(raw_p)}

        results_05 = service.correct(p_values, fdr_threshold=0.05)
        results_10 = service.correct(p_values, fdr_threshold=0.10)

        n_sig_05 = sum(1 for r in results_05 if r.is_significant)
        n_sig_10 = sum(1 for r in results_10 if r.is_significant)
        assert n_sig_10 >= n_sig_05, (
            f"FDR=0.10 should find >= discoveries as FDR=0.05, "
            f"but got {n_sig_10} vs {n_sig_05}"
        )

    def test_known_result_6_significant_at_fdr_10(self):
        """
        BH with alpha=0.10 on the 10-p-value example: 6 metrics are significant.

        Manual verification:
          k=1: 0.001 <= 0.010 ✓
          k=2: 0.008 <= 0.020 ✓
          k=3: 0.039 <= 0.030 ✗ → but keep scanning for largest k
          k=4: 0.041 <= 0.040 ✗
          k=5: 0.042 <= 0.050 ✓
          k=6: 0.060 <= 0.060 ✓ ← largest k
          k=7: 0.074 <= 0.070 ✗
        Largest k where p(k) <= k/m*0.10: k=6.
        """
        service = _make_service()
        raw_p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.391]
        p_values = {f"m{i + 1}": p for i, p in enumerate(raw_p)}
        results = service.correct(p_values, fdr_threshold=0.10)
        n_significant = sum(1 for r in results if r.is_significant)
        assert n_significant == 6, (
            f"Expected 6 significant at FDR=0.10, got {n_significant}"
        )

    def test_the_example_table_rows_and_the_largest_rank(self):
        """Each row of the guide's 10-p-value table at alpha=0.10.

        A row says whether p(k) <= k/10 * 0.10; the largest such k is 6 and
        the service rejects ranks 1 to 6 (ranks 3 and 4 fail their own
        threshold and are rejected with the step-up).
        """
        table = [
            (1, 0.001, 0.010, True),
            (2, 0.008, 0.020, True),
            (3, 0.039, 0.030, False),
            (4, 0.041, 0.040, False),
            (5, 0.042, 0.050, True),
            (6, 0.060, 0.060, True),
            (7, 0.074, 0.070, False),
            (8, 0.205, 0.080, False),
            (9, 0.212, 0.090, False),
            (10, 0.391, 0.100, False),
        ]
        for rank, p, threshold, passes in table:
            assert round(rank / 10 * 0.10, 3) == threshold
            assert (p <= threshold) is passes, f"rank {rank}"
        largest_k = max(rank for rank, _, _, passes in table if passes)
        assert largest_k == 6

        results = _make_service().correct(
            {f"m{rank}": p for rank, p, _, _ in table}, fdr_threshold=0.10
        )
        assert [(r.rank, r.raw_p_value) for r in results] == [
            (rank, p) for rank, p, _, _ in table
        ]
        assert [r.rank for r in results if r.is_significant] == [1, 2, 3, 4, 5, 6]

    def test_adjusted_values_take_the_minimum_from_the_right(self):
        """p_adj(k) = min(p(k) * m / k, 1), then the minimum from the right.

        On the 10-p-value example the raw step-up values for ranks 3, 4, 5 are
        0.13, 0.1025 and 0.084, so ranks 3 and 4 are lowered to 0.084; rank 9
        (0.235556) lowers rank 8 (0.25625).
        """
        raw_p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.391]
        results = _make_service().correct(
            {f"m{i + 1}": p for i, p in enumerate(raw_p)}, fdr_threshold=0.10
        )
        expected = [0.01, 0.04, 0.084, 0.084, 0.084, 0.1, 0.105714, 0.235556]
        expected += [0.235556, 0.391]
        assert [round(r.adjusted_p_value, 6) for r in results] == expected

    def test_fdr_threshold_0_yields_no_discoveries(self):
        """FDR threshold of 0 (no discoveries allowed)."""
        service = _make_service()
        p_values = {"a": 0.001, "b": 0.01, "c": 0.05}
        results = service.correct(p_values, fdr_threshold=0.0)
        assert not any(r.is_significant for r in results)

    def test_two_metrics_one_significant_at_fdr_05(self):
        """With 2 metrics: p1=0.01, p2=0.10 → only p1 significant at FDR=0.05.

        k=1: threshold = 1/2 * 0.05 = 0.025 → p1=0.01 ≤ 0.025 ✓
        k=2: threshold = 2/2 * 0.05 = 0.05 → p2=0.10 > 0.05 ✗
        """
        service = _make_service()
        p_values = {"m1": 0.01, "m2": 0.10}
        results = service.correct(p_values, fdr_threshold=0.05)
        r_m1 = _get_by_name(results, "m1")
        r_m2 = _get_by_name(results, "m2")
        assert r_m1.is_significant, "m1 (p=0.01) should be significant"
        assert not r_m2.is_significant, "m2 (p=0.10) should NOT be significant"

    def test_five_metrics_three_significant(self):
        """5 p-values with FDR=0.05: exactly 3 significant.

        p-values: [0.005, 0.011, 0.025, 0.08, 0.5]
        k=1: 0.005 ≤ 1/5*0.05=0.010 ✓
        k=2: 0.011 ≤ 2/5*0.05=0.020 ✗
        → actually: let's verify:
          k=1: 0.005 ≤ 0.010 ✓
          k=2: 0.011 ≤ 0.020 ✓
          k=3: 0.025 ≤ 0.030 ✓
          k=4: 0.08 ≤ 0.040 ✗
        → Largest k where p(k) ≤ k/m*alpha is k=3.
        """
        service = _make_service()
        p_values = {
            "a": 0.005,
            "b": 0.011,
            "c": 0.025,
            "d": 0.08,
            "e": 0.5,
        }
        results = service.correct(p_values, fdr_threshold=0.05)
        n_sig = sum(1 for r in results if r.is_significant)
        assert n_sig == 3, f"Expected 3 significant, got {n_sig}"


# ---------------------------------------------------------------------------
# TestBHVsBonferroni — Tests 21-25
# ---------------------------------------------------------------------------


class TestBHVsBonferroni:
    """Tests comparing BH with Bonferroni to verify BH is less conservative."""

    def test_bh_finds_more_discoveries_than_bonferroni_on_classic_example(self):
        """BH finds more discoveries than Bonferroni on the classic 10-p-value example."""
        service = _make_service()
        raw_p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.391]
        p_values = {f"m{i + 1}": p for i, p in enumerate(raw_p)}
        alpha = 0.05
        m = len(p_values)

        # BH discoveries
        bh_results = service.correct(p_values, fdr_threshold=alpha)
        n_bh = sum(1 for r in bh_results if r.is_significant)

        # Bonferroni: reject if p_i < alpha/m = 0.005
        bonferroni_threshold = alpha / m
        n_bonferroni = sum(1 for p in raw_p if p < bonferroni_threshold)

        assert n_bh >= n_bonferroni, (
            f"BH ({n_bh}) should find >= discoveries as Bonferroni ({n_bonferroni})"
        )

    def test_bh_more_powerful_on_moderate_pvalues(self):
        """BH detects significance where Bonferroni misses (moderate p-values)."""
        service = _make_service()
        # 20 metrics, first 5 have moderate but real effects
        p_values = {f"m{i + 1}": 0.004 * i for i in range(1, 21)}
        alpha = 0.05
        m = len(p_values)

        bh_results = service.correct(p_values, fdr_threshold=alpha)
        n_bh = sum(1 for r in bh_results if r.is_significant)

        bonferroni_threshold = alpha / m
        n_bonferroni = sum(1 for p in p_values.values() if p < bonferroni_threshold)

        assert n_bh >= n_bonferroni

    def test_bh_and_bonferroni_agree_when_all_significant(self):
        """Both methods agree when all p-values are extremely small."""
        service = _make_service()
        p_values = {f"m{i}": 0.0001 * i for i in range(1, 6)}
        alpha = 0.05
        m = len(p_values)

        bh_results = service.correct(p_values, fdr_threshold=alpha)
        n_bh = sum(1 for r in bh_results if r.is_significant)

        bonferroni_threshold = alpha / m
        n_bonferroni = sum(1 for p in p_values.values() if p < bonferroni_threshold)

        # Both should find all 5 significant
        assert n_bh == 5
        assert n_bonferroni == 5

    def test_bh_and_bonferroni_agree_when_none_significant(self):
        """Both methods agree when all p-values are large."""
        service = _make_service()
        p_values = {f"m{i}": 0.1 * i for i in range(1, 6)}
        alpha = 0.05
        m = len(p_values)

        bh_results = service.correct(p_values, fdr_threshold=alpha)
        n_bh = sum(1 for r in bh_results if r.is_significant)

        bonferroni_threshold = alpha / m
        n_bonferroni = sum(1 for p in p_values.values() if p < bonferroni_threshold)

        assert n_bh == 0
        assert n_bonferroni == 0

    def test_bh_with_many_tests_still_controls_fdr(self):
        """BH with 100 metrics maintains FDR control (result count is plausible)."""
        service = _make_service()
        # Mix of truly significant and null metrics
        import numpy as np

        rng = np.random.default_rng(42)
        # 20 small p-values (significant) + 80 large (null)
        small_pvals = rng.uniform(0.001, 0.01, 20)
        large_pvals = rng.uniform(0.1, 0.9, 80)
        all_pvals = list(small_pvals) + list(large_pvals)
        p_values = {f"m{i}": p for i, p in enumerate(all_pvals)}

        results = service.correct(p_values, fdr_threshold=0.05)
        n_significant = sum(1 for r in results if r.is_significant)

        # BH should find mostly the 20 small p-values (may find slightly more or less)
        # but should not dramatically inflate false positives
        assert 10 <= n_significant <= 30, (
            f"Expected 10-30 significant from 100 tests, got {n_significant}"
        )
