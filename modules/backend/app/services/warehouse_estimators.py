"""The estimators a warehouse run computes its results with.

Warehouse analysis does not have a statistics engine of its own.  A
proportion metric's result comes from the function ``/results`` uses,
:func:`backend.app.services.sufficient_stats_analysis.binomial_metric_result`,
called with the counts the warehouse returned: units and converting units per
variant.  On identical counts the two paths give identical numbers.

A mean metric's result comes from
:func:`backend.app.services.sufficient_stats_analysis.mean_metric_result`,
called with the grand mean ``k`` and each variant's unit count and sums of
``y - k`` and ``(y - k) ** 2``, exactly as the warehouse returned them.

Nothing here computes a p-value, an interval or an effect size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Sequence
from uuid import UUID

from backend.app.services.sufficient_stats_analysis import (
    binomial_metric_result,
    mean_metric_result,
)

#: ``binomial_metric_result(variants, alpha, correction_method, *, metric)``.
BinomialEstimator = Callable[..., Dict[str, Any]]
#: ``mean_metric_result(k, variants, alpha, correction_method, *, metric)``.
MeanEstimator = Callable[..., Dict[str, Any]]


@dataclass(frozen=True)
class VariantRef:
    """An experiment variant as the estimator needs it."""

    id: UUID
    name: str
    is_control: bool


@dataclass(frozen=True)
class MetricRef:
    """The metric's identity and direction, which the result carries.

    ``metric_type`` is ``conversion`` for a proportion metric: the share of
    units with at least one event, what ``/results`` calls a conversion
    metric.  A mean metric carries ``mean``.

    ``lower_is_better`` is the metric source's direction (#1386).  The core
    estimators read it (``metric_lower_is_better``): when it is true, the
    winner is a significant decrease, and the result reports it.  False, the
    default, is how a metric with no direction has always been read.
    """

    id: UUID
    name: str
    is_primary: bool
    metric_type: str = "conversion"
    lower_is_better: bool = False


@dataclass(frozen=True)
class VariantCounts:
    variant: VariantRef
    n: int
    n_converted: int


@dataclass(frozen=True)
class VariantMeanSums:
    """One variant's unit count and its sums centred on the grand mean."""

    variant: VariantRef
    n: int
    sum_d: float
    sum_d2: float


def _json_safe(value: Any) -> Any:
    """The result as JSON values: tuples as lists, UUIDs as text, and a
    non-finite float (which JSON cannot hold) as null."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def proportion_result(
    counts: Sequence[VariantCounts],
    *,
    alpha: float,
    correction_method: str,
    metric: MetricRef,
    estimator: BinomialEstimator = binomial_metric_result,
) -> Dict[str, Any]:
    """The ``MetricResult``-shaped result for the warehouse's counts, as JSON.

    ``counts`` holds every experiment variant, the control first.
    """
    variants = [(c.variant, c.n, c.n_converted) for c in counts]
    return _json_safe(estimator(variants, alpha, correction_method, metric=metric))


def mean_result(
    k: float,
    sums: Sequence[VariantMeanSums],
    *,
    alpha: float,
    correction_method: str,
    metric: MetricRef,
    estimator: MeanEstimator = mean_metric_result,
) -> Dict[str, Any]:
    """The ``MetricResult``-shaped result for the warehouse's centred sums, as
    JSON.  Each variant carries ``note``: None, or why its comparison was not
    computed.

    ``sums`` holds every experiment variant, the control first.  The core
    estimator's refusals (``SufficientStatsRefused``,
    ``SufficientStatsNotComputed``) propagate to the caller.
    """
    variants = [(s.variant, s.n, s.sum_d, s.sum_d2) for s in sums]
    return _json_safe(estimator(k, variants, alpha, correction_method, metric=metric))


__all__ = [
    "BinomialEstimator",
    "MeanEstimator",
    "MetricRef",
    "VariantCounts",
    "VariantMeanSums",
    "VariantRef",
    "binomial_metric_result",
    "mean_metric_result",
    "mean_result",
    "proportion_result",
]
