"""
DimensionalAnalysisService — Issue #28: POST-MVP Dimensional Analysis & Segment Breakdown.

Pure-function service (no DB dependency) that computes per-segment statistics
for experiment results, applying Bonferroni correction across segments and
detecting heterogeneous treatment effects (HTE).
"""

from __future__ import annotations

import logging
import math
import numbers
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from scipy import stats

from backend.app.services import interaction_analysis

logger = logging.getLogger(__name__)

#: A segment enters the heterogeneity test only when, in both the control and
#: the tested treatment, its users times the pooled conversion rate, and its
#: users times one minus that rate, are each at least this
#: (``hte_tested_segments``).  The seeded simulations that hold it are in
#: ``backend/tests/unit/stats_validation/test_golden_hte_false_positive_rate.py``.
HTE_MIN_EXPECTED_PER_CELL = 10


def hte_tested_segments(rows: Sequence[Sequence[int]]) -> List[int]:
    """The segments the heterogeneity test uses, as indexes into ``rows``.

    Each row is one segment's ``(control users, control conversions,
    treatment users, treatment conversions)``.  The candidates are the
    segments with at least one user in both arms, and the pooled rate is
    their conversions over their users, both arms together.  A candidate is
    tested when, in both arms, its users ``n`` give ``n * rate`` and
    ``n * (1 - rate)`` of at least ``HTE_MIN_EXPECTED_PER_CELL``.

    The comparison is made in integers, ``n * conversions >= floor * users``,
    so a segment exactly at the floor is tested whatever floating point would
    round ``n * rate`` to.  A pooled rate of 0 or 1 tests nothing.
    """
    counts = [tuple(int(value) for value in row) for row in rows]
    candidates = [i for i, row in enumerate(counts) if row[0] > 0 and row[2] > 0]
    users = sum(counts[i][0] + counts[i][2] for i in candidates)
    conversions = sum(counts[i][1] + counts[i][3] for i in candidates)
    floor = HTE_MIN_EXPECTED_PER_CELL * users
    return [
        i
        for i in candidates
        if all(
            n * conversions >= floor and n * (users - conversions) >= floor
            for n in (counts[i][0], counts[i][2])
        )
    ]


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

    def detect_hte(self, segment_results: List[SegmentResult], *, alpha: float) -> bool:
        """
        Whether the treatment effect differs between segments (#1969).

        Tests whether the first treatment's difference from the control, in
        percentage points, is the same in every tested segment, and returns
        True only when that is rejected at ``alpha``.  The test is
        ``interaction_analysis.interaction_test`` (#219): the additive model
        (one baseline per segment, one common difference) fitted by binomial
        maximum likelihood, and Pearson's X^2 against it on J - 1 degrees of
        freedom, J being the number of tested segments.

        The control is the variant flagged ``is_control``, and the treatment
        the first other variant of the first segment.  The route lists the
        control first and then the variants by name, so it is the
        non-control variant whose name sorts first.  Both are looked up by id
        in every segment.  A segment is tested when it passes
        ``hte_tested_segments``; ``other`` and ``unknown`` are segments like
        any other.  With fewer than two tested segments (no control, no
        treatment, too few users or conversions) the answer is False, so
        False means either "tested, alike" or "too little data to tell".

        Args:
            segment_results: Output of compute_segment_results.
            alpha: The significance level, finite and strictly between 0 and
                1: the experiment's ``1 - confidence_level``, never the
                per-segment Bonferroni threshold.  Required, so a caller
                cannot silently test at a default.

        Returns:
            A Python ``bool``.

        Raises:
            ValueError: ``alpha`` is not a finite number between 0 and 1.
            interaction_analysis.InteractionFitError: the fit failed.  Never
                caught here: the breakdown fails as a whole (a 500), never
                with a partial or guessed answer.
        """
        if not (
            isinstance(alpha, numbers.Real)
            and math.isfinite(alpha)
            and 0.0 < alpha < 1.0
        ):
            raise ValueError(
                f"alpha must be a finite number between 0 and 1, got {alpha!r}"
            )
        if not segment_results:
            return False

        first = segment_results[0].variants
        control_id = next((v.variant_id for v in first if v.is_control), None)
        treatment_id = next((v.variant_id for v in first if not v.is_control), None)
        if control_id is None or treatment_id is None:
            return False

        rows: List[Tuple[int, int, int, int]] = []
        for seg in segment_results:
            by_id = {v.variant_id: v for v in seg.variants}
            control = by_id.get(control_id)
            treatment = by_id.get(treatment_id)
            rows.append(
                (
                    control.sample_size if control else 0,
                    (control.conversions or 0) if control else 0,
                    treatment.sample_size if treatment else 0,
                    (treatment.conversions or 0) if treatment else 0,
                )
            )

        tested = hte_tested_segments(rows)
        if len(tested) < 2:
            return False
        n = [[rows[i][0] for i in tested], [rows[i][2] for i in tested]]
        x = [[rows[i][1] for i in tested], [rows[i][3] for i in tested]]
        result = interaction_analysis.interaction_test(n, x)
        return bool(result.p_value < alpha)

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
