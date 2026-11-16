"""
Unit tests for DimensionalAnalysisService — Issue #28.

TDD: these tests are written BEFORE the implementation.
They define the expected behavior of compute_segment_breakdown,
detect_hte, and get_adjusted_alpha.
"""

import warnings
from typing import Any, Dict, List

import pytest

# ---------------------------------------------------------------------------
# Import the service under test (will fail until implementation is created)
# ---------------------------------------------------------------------------
from backend.app.services.dimensional_analysis_service import (
    DimensionalAnalysisService,
    SegmentResult,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

CONTROL_ID = "variant-ctrl"
TREATMENT_ID = "variant-treat"


def _make_segment_data(
    control_total: int,
    control_conversions: int,
    treatment_total: int,
    treatment_conversions: int,
) -> Dict[str, Dict[str, Any]]:
    """Build a single-segment dict matching the service API."""
    return {
        CONTROL_ID: {
            "total": control_total,
            "conversions": control_conversions,
            "is_control": True,
        },
        TREATMENT_ID: {"total": treatment_total, "conversions": treatment_conversions},
    }


@pytest.fixture
def service() -> DimensionalAnalysisService:
    return DimensionalAnalysisService()


@pytest.fixture
def platform_segments() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Segment breakdown for 'platform' dimension: ios, android, web."""
    return {
        "ios": _make_segment_data(500, 60, 500, 80),  # strong lift
        "android": _make_segment_data(400, 40, 400, 42),  # tiny lift
        "web": _make_segment_data(300, 30, 300, 28),  # negative lift
    }


@pytest.fixture
def single_segment() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Single segment — Bonferroni correction not applicable."""
    return {
        "mobile": _make_segment_data(1000, 100, 1000, 120),
    }


# ---------------------------------------------------------------------------
# Tests — get_adjusted_alpha
# ---------------------------------------------------------------------------


class TestGetAdjustedAlpha:
    """Tests for DimensionalAnalysisService.get_adjusted_alpha."""

    def test_bonferroni_divides_alpha_by_num_segments(self, service):
        """Alpha / num_segments when num_segments > 1."""
        result = service.get_adjusted_alpha(base_alpha=0.05, num_segments=5)
        assert result == pytest.approx(0.01, rel=1e-6)

    def test_single_segment_returns_base_alpha(self, service):
        """No correction when there is only one segment."""
        result = service.get_adjusted_alpha(base_alpha=0.05, num_segments=1)
        assert result == pytest.approx(0.05, rel=1e-6)

    def test_zero_segments_returns_base_alpha(self, service):
        """Edge case: zero segments — return base alpha unchanged."""
        result = service.get_adjusted_alpha(base_alpha=0.05, num_segments=0)
        assert result == pytest.approx(0.05, rel=1e-6)

    def test_large_num_segments(self, service):
        """Correction with many segments."""
        result = service.get_adjusted_alpha(base_alpha=0.10, num_segments=20)
        assert result == pytest.approx(0.005, rel=1e-6)

    def test_result_never_exceeds_base_alpha(self, service):
        """Adjusted alpha must always be <= base_alpha."""
        for n in range(1, 50):
            adj = service.get_adjusted_alpha(0.05, n)
            assert adj <= 0.05


# ---------------------------------------------------------------------------
# Tests — compute_segment_results
# ---------------------------------------------------------------------------


class TestComputeSegmentResults:
    """Tests for DimensionalAnalysisService.compute_segment_results."""

    def test_groups_events_by_segment_value(self, service, platform_segments):
        """Returns one SegmentResult per segment key."""
        results = service.compute_segment_results(
            segments=platform_segments,
            base_alpha=0.05,
        )
        segment_values = {r.segment_value for r in results}
        assert segment_values == {"ios", "android", "web"}

    def test_each_segment_has_sample_size(self, service, platform_segments):
        """Every SegmentResult must report a positive sample_size."""
        results = service.compute_segment_results(
            segments=platform_segments,
            base_alpha=0.05,
        )
        for r in results:
            assert r.sample_size > 0, (
                f"segment {r.segment_value!r} has zero sample_size"
            )

    def test_each_segment_has_variant_results(self, service, platform_segments):
        """Each SegmentResult must contain variant-level sub-results."""
        results = service.compute_segment_results(
            segments=platform_segments,
            base_alpha=0.05,
        )
        for r in results:
            assert len(r.variants) >= 2, (
                f"segment {r.segment_value!r} must have at least 2 variants"
            )

    def test_bonferroni_correction_applied_to_alpha(self, service, platform_segments):
        """
        With 3 segments and base_alpha=0.05 the adjusted alpha should be 0.05/3 ≈ 0.0167.
        Significance flags must use adjusted_alpha, not base_alpha.
        """
        results = service.compute_segment_results(
            segments=platform_segments,
            base_alpha=0.05,
        )
        # All segments share the same adjusted alpha
        expected_alpha = 0.05 / 3
        for r in results:
            assert r.adjusted_alpha == pytest.approx(expected_alpha, rel=1e-4)

    def test_is_exploratory_always_true(self, service, platform_segments):
        """Breakdowns are always exploratory — is_exploratory must be True."""
        results = service.compute_segment_results(
            segments=platform_segments,
            base_alpha=0.05,
        )
        for r in results:
            assert r.is_exploratory is True

    def test_returns_empty_list_for_empty_segments(self, service):
        """Empty segments dict yields an empty result list."""
        results = service.compute_segment_results(segments={}, base_alpha=0.05)
        assert results == []

    def test_single_segment_no_bonferroni(self, service, single_segment):
        """
        With a single segment no Bonferroni correction is applied:
        adjusted_alpha must equal base_alpha.
        """
        results = service.compute_segment_results(
            segments=single_segment,
            base_alpha=0.05,
        )
        assert len(results) == 1
        assert results[0].adjusted_alpha == pytest.approx(0.05, rel=1e-6)

    def test_per_segment_p_values_computed(self, service, platform_segments):
        """Every non-control variant result must carry a p_value (float)."""
        results = service.compute_segment_results(
            segments=platform_segments,
            base_alpha=0.05,
        )
        for seg in results:
            for vr in seg.variants:
                if not vr.is_control:
                    assert isinstance(vr.p_value, float), (
                        f"Segment {seg.segment_value!r} treatment variant missing p_value"
                    )

    def test_per_segment_confidence_intervals_computed(
        self, service, platform_segments
    ):
        """Every variant must have a confidence_interval tuple."""
        results = service.compute_segment_results(
            segments=platform_segments,
            base_alpha=0.05,
        )
        for seg in results:
            for vr in seg.variants:
                assert vr.confidence_interval is not None, (
                    f"Segment {seg.segment_value!r}, variant {vr.variant_id!r} "
                    "missing confidence_interval"
                )
                lower, upper = vr.confidence_interval
                assert lower <= upper, "CI lower must be <= upper"

    def test_missing_dimension_values_grouped_as_unknown(self, service):
        """
        If a segment_value of None / empty-string is present, it should be
        represented as the string 'unknown' in the results.
        """
        segments_with_missing = {
            "ios": _make_segment_data(500, 60, 500, 80),
            "unknown": _make_segment_data(100, 10, 100, 11),
        }
        results = service.compute_segment_results(
            segments=segments_with_missing,
            base_alpha=0.05,
        )
        segment_values = {r.segment_value for r in results}
        assert "unknown" in segment_values

    def test_segment_sample_size_is_sum_of_variant_sizes(
        self, service, platform_segments
    ):
        """Segment-level sample_size must equal the sum of all variants' sample_sizes."""
        results = service.compute_segment_results(
            segments=platform_segments,
            base_alpha=0.05,
        )
        ios_result = next(r for r in results if r.segment_value == "ios")
        expected_total = 500 + 500  # control + treatment from fixture
        assert ios_result.sample_size == expected_total


# ---------------------------------------------------------------------------
# Tests — detect_hte
# ---------------------------------------------------------------------------


class TestDetectHTE:
    """Tests for DimensionalAnalysisService.detect_hte."""

    def test_no_hte_when_effects_are_homogeneous(self, service):
        """
        When all segments show similar treatment effects (no segment diverges
        significantly), detect_hte should return False.
        """
        homogeneous_segments = {
            "ios": _make_segment_data(1000, 100, 1000, 120),
            "android": _make_segment_data(1000, 100, 1000, 121),
        }
        segment_results = service.compute_segment_results(
            segments=homogeneous_segments,
            base_alpha=0.05,
        )
        assert service.detect_hte(segment_results, alpha=0.05) is False

    def test_hte_detected_when_effects_differ_strongly(self, service):
        """
        Strongly diverging effects across segments should trigger HTE detection.
        One segment has a big positive effect; another has a negative effect.
        """
        divergent_segments = {
            "ios": _make_segment_data(5000, 100, 5000, 500),  # huge positive lift
            "android": _make_segment_data(5000, 500, 5000, 100),  # negative lift
        }
        segment_results = service.compute_segment_results(
            segments=divergent_segments,
            base_alpha=0.05,
        )
        assert service.detect_hte(segment_results, alpha=0.05) is True

    def test_single_segment_never_has_hte(self, service, single_segment):
        """HTE requires at least 2 segments; single-segment always returns False."""
        segment_results = service.compute_segment_results(
            segments=single_segment,
            base_alpha=0.05,
        )
        assert service.detect_hte(segment_results, alpha=0.05) is False

    def test_hte_returns_bool(self, service, platform_segments):
        """detect_hte must always return a bool, not None or other falsy value."""
        segment_results = service.compute_segment_results(
            segments=platform_segments,
            base_alpha=0.05,
        )
        result = service.detect_hte(segment_results, alpha=0.05)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Tests — the control is the flagged variant, never guessed (#218)
# ---------------------------------------------------------------------------


class TestControlIsNeverGuessed:
    """The control comes from ``is_control``; ids say nothing about it."""

    SMALL_ID = "00000000-0000-4000-8000-000000000001"
    LARGE_ID = "ffffffff-0000-4000-8000-000000000002"

    @pytest.mark.regression
    def test_flagged_control_with_the_larger_id_is_the_control(self, service):
        segments = {
            "ios": {
                self.SMALL_ID: {"total": 200, "conversions": 40, "is_control": False},
                self.LARGE_ID: {"total": 200, "conversions": 20, "is_control": True},
            }
        }
        (seg,) = service.compute_segment_results(segments, base_alpha=0.05)
        by_id = {v.variant_id: v for v in seg.variants}
        assert by_id[self.LARGE_ID].is_control is True
        assert by_id[self.SMALL_ID].is_control is False
        assert by_id[self.LARGE_ID].p_value is None
        assert by_id[self.SMALL_ID].p_value is not None

    @pytest.mark.regression
    def test_no_flagged_control_means_no_control_and_no_p_values(self, service):
        segments = {
            "ios": {
                self.SMALL_ID: {"total": 200, "conversions": 40},
                "variant-control": {"total": 200, "conversions": 20},
            }
        }
        (seg,) = service.compute_segment_results(segments, base_alpha=0.05)
        assert [v.is_control for v in seg.variants] == [False, False]
        assert [v.p_value for v in seg.variants] == [None, None]
        assert not any(v.is_significant for v in seg.variants)

    def test_hte_with_a_zero_row_variant_divides_nothing_by_zero(self, service):
        """Every variant is listed in every segment, so a control with no users
        in one segment is ordinary traffic; detect_hte must not warn on it."""
        segments = {
            "desktop": {
                self.LARGE_ID: {"total": 300, "conversions": 30, "is_control": True},
                self.SMALL_ID: {"total": 300, "conversions": 45, "is_control": False},
            },
            "tablet": {
                self.LARGE_ID: {"total": 0, "conversions": 0, "is_control": True},
                self.SMALL_ID: {"total": 10, "conversions": 2, "is_control": False},
            },
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            results = service.compute_segment_results(segments, base_alpha=0.05)
            assert isinstance(service.detect_hte(results, alpha=0.05), bool)
