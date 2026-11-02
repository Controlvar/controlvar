"""The core proportion estimator, wrapped to record what the runner gives it.

The runner's contract with ``binomial_metric_result`` is the counts it passes
-- units and converting units per experiment variant, the control first -- with
the run's alpha, correction method and the metric's identity and direction.  The wrapper
records that and returns the real function's result, so the tests see both.
"""

from __future__ import annotations

from typing import Any, Dict, List

from backend.app.services.sufficient_stats_analysis import (
    binomial_metric_result as _real,
)

CALLS: List[Dict[str, Any]] = []


def binomial_metric_result(
    variants, alpha, correction_method, *, metric
) -> Dict[str, Any]:
    CALLS.append(
        {
            "variants": [
                {
                    "variant_id": str(ref.id),
                    "variant_name": ref.name,
                    "is_control": ref.is_control,
                    "n": n,
                    "n_converted": converted,
                }
                for ref, n, converted in variants
            ],
            "alpha": alpha,
            "correction_method": correction_method,
            "metric": {
                "id": str(metric.id),
                "name": metric.name,
                "metric_type": metric.metric_type,
                "is_primary": metric.is_primary,
                # Attribute access, not getattr: a MetricRef without the
                # field fails here rather than reading as higher is better.
                "lower_is_better": metric.lower_is_better,
            },
        }
    )
    return _real(variants, alpha, correction_method, metric=metric)
