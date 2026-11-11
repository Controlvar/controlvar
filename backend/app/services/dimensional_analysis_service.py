"""
DimensionalAnalysisService — Issue #28: POST-MVP Dimensional Analysis & Segment Breakdown.

Pure-function service (no DB dependency) that computes per-segment statistics
for experiment results, applying Bonferroni correction across segments and
detecting heterogeneous treatment effects (HTE).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes (returned by the service)
# ---------------------------------------------------------------------------


@dataclass
class SegmentVariantResult:
    """Per-variant statistics for a single segment."""

    variant_id: str
    variant_name: str
    is_control: bool
    sample_size: int
    conversions: Optional[int]
    mean: float
    confidence_interval: Optional[Tuple[float, float]]
    p_value: Optional[float]
    is_significant: bool


@dataclass
class SegmentResult:
    """Aggregated statistics for one segment value (e.g. 'ios', 'US')."""

    segment_value: str
    sample_size: int
    adjusted_alpha: float
    is_exploratory: bool  # Always True for breakdowns
    variants: List[SegmentVariantResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DimensionalAnalysisService:
    """
    Compute per-dimension segment breakdowns alongside experiment results.

    All methods are pure (no DB interaction); callers supply pre-aggregated
    event data grouped by dimension value and variant.

    Segments dict schema
    --------------------
    {
        "<segment_value>": {
            "<variant_id>": {
                "total":       int,   # assignments / exposures
                "conversions": int,   # conversion events
                "is_control":   bool,  # the control is never guessed
                # optional:
                "variant_name": str,  # human-readable name
            },
            ...
        },
        ...
    }
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_adjusted_alpha(self, base_alpha: float, num_segments: int) -> float:
        """
        Apply Bonferroni correction to the significance threshold.

        Returns base_alpha / num_segments when num_segments > 1, otherwise
        returns base_alpha unchanged (no correction needed for a single segment).

        Args:
            base_alpha:    The experiment-level significance threshold (e.g. 0.05).
            num_segments:  Number of distinct segment values being tested.

        Returns:
            Bonferroni-corrected alpha value in (0, base_alpha].
        """
        if num_segments > 1:
            return base_alpha / num_segments
        return base_alpha

    def compute_segment_results(
        self,
        segments: Dict[str, Dict[str, Any]],
        base_alpha: float = 0.05,
    ) -> List[SegmentResult]:
        """
        Compute per-segment variant statistics with Bonferroni-corrected alpha.

        Args:
            segments:   Nested dict mapping segment_value → {variant_id: {total, conversions, …}}.
            base_alpha: Base significance threshold before correction.

        Returns:
            List of SegmentResult objects, one per segment value.
        """
        if not segments:
            return []

        num_segments = len(segments)
        adjusted_alpha = self.get_adjusted_alpha(base_alpha, num_segments)

        results: List[SegmentResult] = []

        for segment_value, variant_map in segments.items():
            # Normalise missing/None keys to "unknown"
            seg_val = segment_value if segment_value else "unknown"

            # The control is the variant flagged is_control=True; with none
            # flagged there is nothing to test against.
            control_id = self._find_control_id(variant_map)

            control_data = variant_map.get(control_id, {}) if control_id else {}
            control_total = control_data.get("total", 0)
            control_conversions = control_data.get("conversions", 0)

            # Total sample across all variants in this segment
            total_sample = sum(v.get("total", 0) for v in variant_map.values())

            variant_results: List[SegmentVariantResult] = []

            for variant_id, vdata in variant_map.items():
                v_total = vdata.get("total", 0)
                v_conversions = vdata.get("conversions", 0)
                v_mean = v_conversions / v_total if v_total > 0 else 0.0
                v_ci = self._wilson_ci(v_conversions, v_total, adjusted_alpha)
                is_ctrl = variant_id == control_id

                if is_ctrl or control_id is None:
                    p_value = None
                    is_significant = False
                else:
                    p_value = self._fisher_exact_p_value(
                        control_conversions,
                        control_total,
                        v_conversions,
                        v_total,
                    )
                    is_significant = (p_value is not None) and (
                        p_value < adjusted_alpha
                    )

                variant_results.append(
                    SegmentVariantResult(
                        variant_id=variant_id,
                        variant_name=vdata.get("variant_name", variant_id),
                        is_control=is_ctrl,
                        sample_size=v_total,
                        conversions=v_conversions,
                        mean=v_mean,
                        confidence_interval=v_ci,
                        p_value=p_value,
                        is_significant=is_significant,
                    )
                )

            results.append(
                SegmentResult(
                    segment_value=seg_val,
                    sample_size=total_sample,
                    adjusted_alpha=adjusted_alpha,
                    is_exploratory=True,
                    variants=variant_results,
                )
            )

        return results

    def detect_hte(self, segment_results: List[SegmentResult]) -> bool:
        """
        Detect heterogeneous treatment effects (HTE) across segments.

        Uses a chi-squared-like test on the treatment conversion rates across
        segments.  Returns True when at least two segments differ significantly
        in their observed treatment effect.

        Args:
            segment_results: Output of compute_segment_results.

        Returns:
            True when HTE is detected, False otherwise.
        """
        if len(segment_results) < 2:
            return False

        # Gather (control_total, control_conv, treatment_total, treatment_conv)
        # for each segment — use the first non-control variant as "treatment".
        segment_data: List[Tuple[int, int, int, int]] = []
        for seg in segment_results:
            ctrl = next((v for v in seg.variants if v.is_control), None)
            treat = next((v for v in seg.variants if not v.is_control), None)
            if ctrl is None or treat is None:
                continue
            segment_data.append(
                (
                    ctrl.sample_size,
                    ctrl.conversions or 0,
                    treat.sample_size,
                    treat.conversions or 0,
                )
            )

        if len(segment_data) < 2:
            return False

        # Build a 2×N contingency table (rows: control/treatment, cols: segments)
        # and run chi-squared test.  High chi-squared = heterogeneous effects.
        ctrl_conv = np.array([d[1] for d in segment_data], dtype=float)
        ctrl_total = np.array([d[0] for d in segment_data], dtype=float)
        treat_conv = np.array([d[3] for d in segment_data], dtype=float)
        treat_total = np.array([d[2] for d in segment_data], dtype=float)

        # A variant can have no users in a segment (every variant is listed),
        # so divide only where the total is positive; the rate is 0 elsewhere.
        ctrl_rate = np.divide(
            ctrl_conv,
            ctrl_total,
            out=np.zeros_like(ctrl_conv, dtype=float),
            where=ctrl_total > 0,
        )
        treat_rate = np.divide(
            treat_conv,
            treat_total,
            out=np.zeros_like(treat_conv, dtype=float),
            where=treat_total > 0,
        )
        lift = treat_rate - ctrl_rate

        # Chi-squared test on treatment conversion counts across segments
        # If variances in conversion rates are large relative to their means, HTE detected.
        # Create a 2xN contingency table for chi-squared
        contingency = np.array(
            [
                [int(c) for c in treat_conv],
                [int(c) for c in ctrl_conv],
            ]
        )

        # Only test if cells are non-trivially small
        total_treat = treat_total.sum()
        total_ctrl = ctrl_total.sum()
        if total_treat == 0 or total_ctrl == 0:
            return False

        try:
            chi2_stat, p_value, dof, _ = stats.chi2_contingency(contingency)
            # Use 0.05 as the HTE detection threshold
            if p_value < 0.05 and dof > 0:
                return True
        except Exception as exc:
            logger.debug("HTE chi-squared test failed: %s", exc)

        # Secondary heuristic: compare lift variance to mean lift
        if len(lift) >= 2:
            lift_std = float(np.std(lift, ddof=1))
            lift_mean = float(np.abs(np.mean(lift)))
            # High coefficient of variation in lift across segments → HTE
            if lift_mean > 0 and (lift_std / lift_mean) > 0.5:
                return True
            # Segments with opposing signs (positive and negative lift) → HTE
            if any(l > 0 for l in lift) and any(l < 0 for l in lift):
                return True

        return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_control_id(variant_map: Dict[str, Any]) -> Optional[str]:
        """
        Return the variant_id the caller flagged ``is_control``, or ``None``.

        The control is never guessed from an id (#218): an id sorts by chance,
        and a guessed control turns every p-value in the segment into a
        comparison against the wrong arm.  With no flagged variant, no
        variant is a control and no p-value is computed.
        """
        for vid, vdata in variant_map.items():
            if vdata.get("is_control", False):
                return vid
        return None

    @staticmethod
    def _wilson_ci(
        successes: int,
        total: int,
        alpha: float = 0.05,
    ) -> Tuple[float, float]:
        """
        Wilson score confidence interval for a proportion.

        Returns (lower, upper) clamped to [0, 1].
        """
        if total <= 0:
            return (0.0, 1.0)

        confidence_level = 1.0 - alpha
        z = float(stats.norm.ppf((1.0 + confidence_level) / 2.0))
        z2 = z * z
        n = float(total)
        p_hat = successes / n

        center = (p_hat + z2 / (2.0 * n)) / (1.0 + z2 / n)
        margin = (
            z
            * math.sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4.0 * n * n))
            / (1.0 + z2 / n)
        )

        lower = max(0.0, center - margin)
        upper = min(1.0, center + margin)
        return (lower, upper)

    @staticmethod
    def _fisher_exact_p_value(
        control_conversions: int,
        control_total: int,
        treatment_conversions: int,
        treatment_total: int,
    ) -> Optional[float]:
        """
        Two-sided p-value of Fisher's exact test, treatment against control.

        The test the headline uses for a conversion rate, on the same 2x2
        table (``sufficient_stats_analysis.binomial_variant_results``):
        converting and non-converting users, treatment row first.  Exact at
        every sample size, so a segment of a few users is not flagged on an
        approximation.  None when either arm has no users.
        """
        if control_total <= 0 or treatment_total <= 0:
            return None

        table = [
            [treatment_conversions, treatment_total - treatment_conversions],
            [control_conversions, control_total - control_conversions],
        ]
        _, p_value = stats.fisher_exact(table)
        return float(p_value)
