# backend/app/services/analysis_service.py
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

import numpy as np
from scipy import stats
from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload

from backend.app.core.database_config import get_schema_name
from backend.app.core.stats_engine import ENGINE_VERSION, as_of_bucket, derive_seed
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, Metric
from backend.app.schemas.bayesian import (
    BayesianConfig,
    BayesianDecision,
    BayesianPosteriorResult,
    BayesianResultsResponse,
    BayesianVariantResult,
)
from backend.app.services.analysis_settings import resolve_analysis_settings
from backend.app.services.bayesian_service import DEFAULT_N_SAMPLES, BayesianService
from backend.app.services.event_matching import (
    CONVERSION_SQL_PREDICATE,
    CONVERTING_USERS_JOIN,
    CONVERTING_USERS_SQL,
    count_converting_users,
    count_converting_users_any,
    first_conversion_times,
)
from backend.app.services.srm_service import (
    recommendation_under_srm,
    sample_ratio_check,
)
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    adjusted_p_values,
    binomial_metric_result,
    binomial_variant_results,
    effect_size_label,
    metric_lower_is_better,
)

logger = logging.getLogger(__name__)

#: Monte Carlo samples per variant for the Bayesian block of the results API.
BAYESIAN_N_SAMPLES: int = DEFAULT_N_SAMPLES


class AnalysisService:
    """
    Service for analyzing experiment results and calculating statistics.

    This service provides the analytics capabilities of the platform, including:
    - Calculating conversion rates and other metrics
    - Performing statistical tests to determine significance
    - Generating result summaries and reports
    - Computing confidence intervals and effect sizes
    """

    def __init__(self, db: Session):
        """Initialize with a database session."""
        self.db = db

    @staticmethod
    def _parse_dt(value) -> datetime:
        """Parse a datetime value that may be a datetime object or ISO string."""
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        raise ValueError(f"Cannot parse datetime from {type(value)}: {value!r}")

    def get_experiment_results(
        self,
        experiment_id: Union[str, UUID],
        confidence_level: Optional[float] = None,
        correction_method: Optional[str] = None,
        include_bayesian: bool = True,
    ) -> Dict[str, Any]:
        """
        Get comprehensive results for an experiment.

        Args:
            experiment_id: ID of the experiment
            confidence_level: level of every variant interval; ``1 -
                confidence_level`` is the significance level.  None (the
                default) uses the experiment's stored level (#580).
            correction_method: ``none``, ``bonferroni`` or
                ``benjamini_hochberg``.  None (the default) uses the
                experiment's stored method; the data export and the report
                pass none, so they follow the stored setting.
            include_bayesian: Compute ``bayesian_results`` when the experiment
                has Bayesian analysis enabled. The data export passes False: it
                reads only the frequentist ``metrics`` and ``summary``, which
                do not depend on it.

        Returns:
            Dictionary containing experiment results data
        """
        # Get experiment with variants and metrics
        experiment = (
            self.db.query(Experiment)
            .options(
                joinedload(Experiment.variants),
                joinedload(Experiment.metric_definitions),
            )
            .filter(Experiment.id == experiment_id)
            .first()
        )

        if not experiment:
            raise ValueError(f"Experiment {experiment_id} not found")

        confidence_level, correction_method = resolve_analysis_settings(
            experiment, confidence_level, correction_method
        )

        # Calculate results for each metric (primary metric first)
        metric_definitions = self._ordered_metrics(experiment)
        metrics_results = []
        for metric in metric_definitions:
            metric_result = self.calculate_metric_results(
                experiment, metric, confidence_level
            )
            metrics_results.append(metric_result)

        # Calculate overall summary statistics
        summary = self.calculate_experiment_summary(experiment)

        # EP-035 Batch 2: Compute Bayesian results if enabled
        bayesian_results = None
        if include_bayesian and self.is_bayesian_enabled(experiment):
            try:
                bayesian_results = self.compute_bayesian_results(experiment)
            except Exception as exc:
                logger.warning("Bayesian analysis failed (non-critical): %s", exc)

        # Return formatted results
        # Schema-shaped results for the analytics API (schemas/results.py).
        alpha = 1.0 - confidence_level
        metrics = [
            binomial_metric_result(
                [
                    (
                        BinomialVariant(
                            v["variant_id"], v["variant_name"], v["is_control"]
                        ),
                        v["sample_size"],
                        v["conversions"],
                    )
                    for v in raw["variant_results"]
                ],
                alpha,
                correction_method,
                metric=metric,
            )
            for metric, raw in zip(metric_definitions, metrics_results)
        ]
        sample_size_adequate = self._sample_size_adequate(experiment, metrics_results)
        summary.update(
            self._summarise_decision(experiment, metrics, sample_size_adequate)
        )
        # A failed sample-ratio check overrides the recommendation (#880).
        # Here rather than in the endpoint, so that /results, the data export
        # and the report, which all read this dict, give the same answer.
        # Like the check, the rule fails open: if it raises, ``srm`` is None
        # and the recommendation is the engine's.
        srm = sample_ratio_check(self.db, experiment.id)
        try:
            summary.update(recommendation_under_srm(summary, srm))
        except Exception as exc:
            logger.warning(
                "SRM recommendation rule failed for experiment %s (%s)",
                experiment.id,
                type(exc).__name__,
            )
            srm = None

        return {
            "experiment_id": str(experiment_id),
            "experiment_name": experiment.name,
            "status": (
                experiment.status.value
                if hasattr(experiment.status, "value")
                else str(experiment.status)
            ),
            "start_date": experiment.start_date,
            "end_date": experiment.end_date,
            "confidence_level": confidence_level,
            "correction_method": correction_method,
            "sample_size_adequate": sample_size_adequate,
            "computed_at": datetime.now(timezone.utc),
            # Legacy per-metric dicts (variant_results / conversion_rate ...)
            "metrics_results": metrics_results,
            # Schema-shaped per-metric results
            "metrics": metrics,
            "summary": summary,
            "bayesian_results": bayesian_results,
            # The sample-ratio check behind the recommendation, or None.
            "srm": srm,
        }

    # ------------------------------------------------------------------
    # Mapping to the analytics results schema (schemas/results.py)
    # ------------------------------------------------------------------

    # The proportion computation lives in sufficient_stats_analysis (#404);
    # these names stay for the callers and tests that use them.
    _adjusted_p_values = staticmethod(adjusted_p_values)

    @staticmethod
    def _sample_size_adequate(
        experiment: Experiment, metrics_results: List[Dict[str, Any]]
    ) -> bool:
        """True when every variant meets the primary metric's minimum sample size."""
        if not metrics_results:
            return False
        primary = next((m for m in experiment.metric_definitions if m.is_primary), None)
        minimum = int(getattr(primary, "minimum_sample_size", None) or 100)
        raw = next(
            (
                r
                for r in metrics_results
                if primary and r["metric_id"] == str(primary.id)
            ),
            metrics_results[0],
        )
        return all(v["sample_size"] >= minimum for v in raw["variant_results"])

    @staticmethod
    def _summarise_decision(
        experiment: Experiment,
        metrics: List[Dict[str, Any]],
        sample_size_adequate: bool = True,
    ) -> Dict[str, Any]:
        """
        has_winner / winning_variant_id / recommendation for the summary.

        A significant winner is only recommended for shipping once every
        variant has reached the primary metric's minimum sample size; before
        that the result is reported but the recommendation stays
        CONTINUE_TESTING (early peeks inflate false positives).

        The primary metric's ``lower_is_better`` decides the direction: the
        winner is the metric result's ``winning_variant_id`` (already
        direction-aware), and KEEP_CONTROL means every treatment is
        significantly worse in that direction.  For a lower-is-better metric
        the reason gives the improvement as a positive figure and says that
        lower is better; the higher-is-better sentences are unchanged.
        """
        if not metrics:
            return {
                "has_winner": False,
                "winning_variant_id": None,
                "recommendation": "CONTINUE_TESTING",
                "recommendation_reason": "No metrics are defined for this experiment yet.",
            }
        primary = next((m for m in metrics if m["is_primary"]), metrics[0])
        lower_is_better = primary.get("lower_is_better", False) is True
        winner_id = primary["winning_variant_id"]
        if winner_id:
            winner = next(
                v for v in primary["variants"] if v["variant_id"] == winner_id
            )
            pct = winner["relative_improvement_pct"]
            p = (
                winner["adjusted_p_value"]
                if winner["adjusted_p_value"] is not None
                else winner["p_value"]
            )
            if lower_is_better:
                # The figure is the fall, so it is positive in the good
                # direction; never "-20.0% improvement".
                reason = (
                    f"{winner['variant_name']} shows "
                    f"{abs(pct):.1f}% improvement on {primary['metric_name']} "
                    f"(p={p:.4f}): it fell by {abs(pct):.1f}%, and lower is "
                    "better for this metric."
                    if pct is not None and p is not None
                    else f"{winner['variant_name']} is significantly better on "
                    f"{primary['metric_name']}, where lower is better."
                )
            else:
                reason = (
                    f"{winner['variant_name']} shows "
                    f"{pct:.1f}% improvement on {primary['metric_name']} (p={p:.4f})."
                    if pct is not None and p is not None
                    else f"{winner['variant_name']} is significantly better on {primary['metric_name']}."
                )
            if not sample_size_adequate:
                return {
                    "has_winner": True,
                    "winning_variant_id": winner_id,
                    "recommendation": "CONTINUE_TESTING",
                    "recommendation_reason": (
                        f"{reason} Not every variant has reached the minimum sample size yet, "
                        "so keep the experiment running before shipping."
                    ),
                }
            return {
                "has_winner": True,
                "winning_variant_id": winner_id,
                "recommendation": "SHIP_VARIANT",
                "recommendation_reason": reason,
            }
        treatments = [v for v in primary["variants"] if not v["is_control"]]
        # "Worse" is the signed change against the metric's direction.
        worse_sign = 1 if lower_is_better else -1
        if treatments and all(
            v["is_significant"]
            and (v["relative_improvement_pct"] or 0) * worse_sign > 0
            for v in treatments
        ):
            return {
                "has_winner": False,
                "winning_variant_id": None,
                "recommendation": "KEEP_CONTROL",
                "recommendation_reason": (
                    f"Every treatment is significantly worse than control on "
                    f"{primary['metric_name']}, where lower is better."
                    if lower_is_better
                    else f"Every treatment is significantly worse than control on {primary['metric_name']}."
                ),
            }
        total = sum(v["sample_size"] for v in primary["variants"])
        if total == 0:
            return {
                "has_winner": False,
                "winning_variant_id": None,
                "recommendation": "CONTINUE_TESTING",
                "recommendation_reason": "No users have been assigned yet.",
            }
        return {
            "has_winner": False,
            "winning_variant_id": None,
            "recommendation": "CONTINUE_TESTING",
            "recommendation_reason": (
                f"No statistically significant difference on {primary['metric_name']} yet "
                f"({total} users assigned)."
            ),
        }

    @staticmethod
    def _ordered_metrics(experiment: Experiment) -> List[Metric]:
        """Metric definitions with the primary metric first, then by name."""
        metrics = list(experiment.metric_definitions or [])
        return sorted(
            metrics,
            key=lambda m: (not bool(getattr(m, "is_primary", False)), m.name or ""),
        )

    def calculate_metric_results(
        self,
        experiment: Experiment,
        metric: Metric,
        confidence_level: float = 0.95,
    ) -> Dict[str, Any]:
        """
        Calculate results for a specific metric in an experiment.

        Args:
            experiment: Experiment model object
            metric: Metric model object
            confidence_level: level of each variant's interval; its
                complement is the significance level

        Returns:
            Dictionary containing metric results
        """
        # Get variant IDs for the experiment
        variant_ids = [str(v.id) for v in experiment.variants]

        # Find control variant
        control_variant = next((v for v in experiment.variants if v.is_control), None)
        if not control_variant:
            raise ValueError(f"Experiment {experiment.id} has no control variant")

        # Get assignment counts per variant
        assignments = {}
        for variant_id in variant_ids:
            count = (
                self.db.query(func.count(Assignment.id))
                .filter(
                    Assignment.experiment_id == experiment.id,
                    Assignment.variant_id == variant_id,
                )
                .scalar()
                or 0
            )
            assignments[variant_id] = count

        # Converting users per variant (event_matching.py): a user with
        # several conversion events counts once, so conversions <= assignments.
        conversions = {}
        for variant_id in variant_ids:
            conversions[variant_id] = count_converting_users(
                self.db, experiment.id, variant_id, metric.event_name
            )

        # Rates, intervals and Fisher p-values from the counts
        results = binomial_variant_results(
            [
                (variant, assignments[str(variant.id)], conversions[str(variant.id)])
                for variant in experiment.variants
            ],
            confidence_level,
        )

        # Format metric result
        return {
            "metric_id": str(metric.id),
            "metric_name": metric.name,
            "event_name": metric.event_name,
            "variant_results": results,
            "total_conversions": sum(conversions.values()),
            "has_significant_results": any(r["is_significant"] for r in results),
            "best_variant": self._find_best_variant(
                results, lower_is_better=metric_lower_is_better(metric)
            ),
        }

    def calculate_experiment_summary(self, experiment: Experiment) -> Dict[str, Any]:
        """
        Calculate overall summary statistics for an experiment.

        Args:
            experiment: Experiment model object

        Returns:
            Dictionary containing summary statistics
        """
        # Get start and end dates
        start_date = experiment.start_date
        end_date = experiment.end_date

        # Calculate experiment duration in days
        if start_date:
            try:
                start_dt = self._parse_dt(experiment.start_date)
                end_dt = (
                    self._parse_dt(experiment.end_date)
                    if experiment.end_date
                    else datetime.now(timezone.utc)
                )
                duration_days = (end_dt - start_dt).days
            except (ValueError, TypeError):
                duration_days = None
        else:
            duration_days = None

        # Get total number of users in experiment
        total_users = (
            self.db.query(func.count(Assignment.user_id.distinct()))
            .filter(Assignment.experiment_id == experiment.id)
            .scalar()
            or 0
        )

        # Get total number of events
        total_events = (
            self.db.query(func.count(Event.id))
            .filter(Event.experiment_id == experiment.id)
            .scalar()
            or 0
        )

        # Converting users across every metric (each user once)
        total_conversions = count_converting_users_any(
            self.db,
            experiment.id,
            (m.event_name for m in (experiment.metric_definitions or [])),
        )

        # Return formatted summary
        return {
            "total_users": total_users,
            "total_events": total_events,
            "total_conversions": total_conversions,
            "start_date": start_date,
            "end_date": end_date,
            "duration_days": duration_days,
        }

    def get_daily_results(
        self,
        experiment_id: Union[str, UUID],
        metric_id: Optional[Union[str, UUID]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get daily results for an experiment or specific metric.

        Args:
            experiment_id: ID of the experiment
            metric_id: Optional ID of the metric to filter by

        Returns:
            List of daily result dictionaries
        """
        # Get experiment with variants and metrics
        experiment = (
            self.db.query(Experiment)
            .options(
                joinedload(Experiment.variants),
                joinedload(Experiment.metric_definitions),
            )
            .filter(Experiment.id == experiment_id)
            .first()
        )

        if not experiment:
            raise ValueError(f"Experiment {experiment_id} not found")

        # Determine start and end dates
        if not experiment.start_date:
            return []

        start_dt = self._parse_dt(experiment.start_date)
        end_dt = (
            self._parse_dt(experiment.end_date)
            if experiment.end_date
            else datetime.now(timezone.utc)
        )

        # Generate list of dates
        dates = []
        current_dt = start_dt
        while current_dt <= end_dt:
            dates.append(current_dt.strftime("%Y-%m-%d"))
            current_dt += timedelta(days=1)

        # Filter metrics if metric_id is provided
        metrics = [
            m
            for m in experiment.metric_definitions
            if not metric_id or str(m.id) == str(metric_id)
        ]

        if not metrics or not dates:
            return []

        # Get daily assignments (can be optimized with a single query)
        daily_assignments = {}
        for date in dates:
            daily_variant_assignments = {}

            # Get counts by variant for this date
            for variant in experiment.variants:
                # Convert date string to datetime range
                date_start = datetime.fromisoformat(f"{date}T00:00:00+00:00")
                date_end = datetime.fromisoformat(f"{date}T23:59:59+00:00")

                # Query assignments for this variant on this date
                count = (
                    self.db.query(func.count(Assignment.id))
                    .filter(
                        Assignment.experiment_id == experiment.id,
                        Assignment.variant_id == variant.id,
                        Assignment.created_at >= date_start,
                        Assignment.created_at <= date_end,
                    )
                    .scalar()
                    or 0
                )

                daily_variant_assignments[str(variant.id)] = count

            daily_assignments[date] = daily_variant_assignments

        # The day (YYYY-MM-DD) of each converting user's first conversion,
        # per metric and variant.  A day's conversions are the users who
        # converted for the first time that day, so the running sum over days
        # is the converting-user count that /results reports.  A first
        # conversion before the first day or after the last one is clamped
        # into that day, so the series still ends at the /results count.
        first_conversion_days = {
            (str(metric.id), str(variant.id)): [
                min(max(first[:10], dates[0]), dates[-1])
                for first in first_conversion_times(
                    self.db, experiment.id, variant.id, metric.event_name
                )
            ]
            for metric in metrics
            for variant in experiment.variants
        }

        # Get daily conversions by metric and variant
        results = []
        for date in dates:
            date_results = {"date": date, "metrics": []}

            for metric in metrics:
                metric_result = {
                    "metric_id": str(metric.id),
                    "metric_name": metric.name,
                    "variants": [],
                }

                for variant in experiment.variants:
                    # Users whose first conversion falls on this date
                    conversions = first_conversion_days[
                        (str(metric.id), str(variant.id))
                    ].count(date)

                    # Calculate conversion rate
                    assignments = daily_assignments[date][str(variant.id)]
                    rate = (conversions / assignments) * 100 if assignments > 0 else 0

                    variant_result = {
                        "variant_id": str(variant.id),
                        "variant_name": variant.name,
                        "is_control": variant.is_control,
                        "assignments": assignments,
                        "conversions": conversions,
                        "conversion_rate": rate,
                    }

                    metric_result["variants"].append(variant_result)

                date_results["metrics"].append(metric_result)

            results.append(date_results)

        return results

    def _find_best_variant(
        self,
        variant_results: List[Dict[str, Any]],
        lower_is_better: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Find the best performing variant from a list of variant results.

        Args:
            variant_results: List of variant result dictionaries
            lower_is_better: the metric's direction; when true the best is
                the significant variant with the lowest rate below the
                control's, otherwise the highest above it.

        Returns:
            Dictionary containing best variant info or None if no clear winner
        """
        # Find control variant
        control = next((v for v in variant_results if v["is_control"]), None)
        if not control:
            return None

        # Find variants with significant improvement over control, in the
        # metric's direction
        significant_variants = [
            v
            for v in variant_results
            if not v["is_control"]
            and v["is_significant"]
            and (
                v["relative_improvement"] < 0
                if lower_is_better
                else v["relative_improvement"] > 0
            )
        ]

        if not significant_variants:
            return None

        # The best rate: the lowest when lower is better, else the highest
        pick = min if lower_is_better else max
        best_variant = pick(significant_variants, key=lambda v: v["conversion_rate"])

        return {
            "variant_id": best_variant["variant_id"],
            "variant_name": best_variant["variant_name"],
            "improvement": best_variant["relative_improvement"],
            "conversion_rate": best_variant["conversion_rate"],
            "p_value": best_variant["p_value"],
        }

    # -----------------------------------------------------------------------
    # EP-035 Batch 2: Bayesian Analysis Helper
    # -----------------------------------------------------------------------

    @staticmethod
    def is_bayesian_enabled(experiment: Experiment) -> bool:
        """True when the experiment opted into Bayesian analysis and has a config."""
        return bool(
            getattr(experiment, "bayesian_enabled", False)
            and getattr(experiment, "bayesian_config", None)
        )

    @staticmethod
    def _bayesian_primary_metric(experiment: Experiment) -> Optional[Metric]:
        """The metric the Bayesian analysis reads: the primary, else the first."""
        definitions = list(getattr(experiment, "metric_definitions", None) or [])
        return next(
            (m for m in definitions if m.is_primary),
            definitions[0] if definitions else None,
        )

    def _bayesian_observations(
        self, experiment: Experiment
    ) -> Dict[str, Dict[str, int]]:
        """Per-variant ``{conversions, total}`` for the primary metric."""
        primary_metric = self._bayesian_primary_metric(experiment)
        metrics_data: Dict[str, Dict[str, int]] = {}
        for variant in experiment.variants:
            vid = str(variant.id)
            total = (
                self.db.query(func.count(Assignment.id))
                .filter(
                    Assignment.experiment_id == experiment.id,
                    Assignment.variant_id == variant.id,
                )
                .scalar()
                or 0
            )
            if primary_metric:
                convs = count_converting_users(
                    self.db, experiment.id, variant.id, primary_metric.event_name
                )
            else:
                convs = 0
            metrics_data[vid] = {"conversions": convs, "total": total}
        return metrics_data

    def compute_bayesian_results(
        self,
        experiment: Experiment,
        as_of: Optional[datetime] = None,
    ) -> BayesianResultsResponse:
        """Read the primary-metric counts and run the Bayesian analysis.

        Also records ``experiment.bayesian_decision`` (flushed, not
        committed).  The decision is a recommendation: nothing stops the
        experiment on it.

        Args:
            experiment: Experiment ORM object with variants and metrics loaded.
            as_of: Analysis time used for the reproducibility bucket
                (defaults to now, UTC).
        """
        metrics_data = self._bayesian_observations(experiment)
        bayesian_response = self._compute_bayesian_results(
            experiment, metrics_data, as_of=as_of
        )

        # Persist the decision back to the experiment record
        if bayesian_response.decision is not None:
            experiment.bayesian_decision = bayesian_response.decision.value
            self.db.add(experiment)
            self.db.flush()

        return bayesian_response

    def _compute_bayesian_results(
        self,
        experiment: Experiment,
        metrics_data: Dict[str, Dict[str, int]],
        as_of: Optional[datetime] = None,
        n_samples: int = BAYESIAN_N_SAMPLES,
    ) -> BayesianResultsResponse:
        """Compute Bayesian inference results for all variants.

        The Monte Carlo seed is ``derive_seed(experiment.id, as_of day,
        n_samples)`` so repeated calls on the same day return identical
        posterior summaries; the seed is echoed in the response.

        Args:
            experiment: Experiment ORM object with bayesian_config populated.
            metrics_data: Mapping of variant_id (str) ->
                {'conversions': int, 'total': int}.
            as_of: Analysis time for the reproducibility bucket (default now).
            n_samples: Monte Carlo samples per variant.

        Returns:
            BayesianResultsResponse with posterior distributions, PtBB,
            expected loss, a BayesianDecision, and seed provenance.
        """
        # Load BayesianConfig from the JSONB column
        raw_config = experiment.bayesian_config or {}
        if isinstance(raw_config, str):
            raw_config = json.loads(raw_config)

        try:
            config = BayesianConfig(**raw_config)
        except Exception:
            config = BayesianConfig()

        service = BayesianService(config)

        # Build ordered list of variant observations matching experiment.variants order
        variant_observations = []
        variant_keys = []
        for variant in experiment.variants:
            vid = str(variant.id)
            obs = metrics_data.get(vid, {"conversions": 0, "total": 0})
            variant_observations.append(obs)
            variant_keys.append(variant.name)

        # Deterministic seed: same experiment + same UTC day + same n_samples
        # → byte-identical Monte Carlo draws.
        seed = derive_seed(experiment.id, as_of_bucket(as_of), n_samples)

        # Run full Bayesian analysis, in the primary metric's direction
        analysis = service.analyze(
            variant_observations,
            n_samples=n_samples,
            seed=seed,
            lower_is_better=metric_lower_is_better(
                self._bayesian_primary_metric(experiment)
            ),
        )

        posteriors = analysis["posteriors"]
        credible_intervals = analysis["credible_intervals"]
        ptbb = analysis["probability_to_be_best"]
        losses = analysis["expected_loss"]
        decision: BayesianDecision = analysis["decision"]

        # Build per-variant results
        variant_results: List[BayesianVariantResult] = []
        for i, (variant, posterior, ci, p, loss) in enumerate(
            zip(experiment.variants, posteriors, credible_intervals, ptbb, losses)
        ):
            alpha = float(posterior["alpha"])
            beta_val = float(posterior["beta"])
            mean = alpha / (alpha + beta_val)

            posterior_result = BayesianPosteriorResult(
                alpha=alpha,
                beta=beta_val,
                mean=mean,
                credible_interval_lower=float(ci[0]),
                credible_interval_upper=float(ci[1]),
            )

            variant_results.append(
                BayesianVariantResult(
                    variant_key=variant.name,
                    posterior=posterior_result,
                    probability_to_be_best=float(p),
                    expected_loss=float(loss),
                )
            )

        return BayesianResultsResponse(
            is_enabled=True,
            decision=decision,
            variant_results=variant_results,
            seed=seed,
            n_samples=int(n_samples),
            engine_version=ENGINE_VERSION,
        )

    # -----------------------------------------------------------------------
    # EP-016 Statistical Helper Methods
    # -----------------------------------------------------------------------

    _effect_size_label = staticmethod(effect_size_label)

    def select_statistical_test(
        self,
        metric_type: str,
        control_size: int,
        treatment_size: int,
    ) -> str:
        """Select the most appropriate statistical test.

        Returns 'fisher_exact' for small samples (n < 30), 'z_test' for
        conversion metrics with large samples, and 'welch_t_test' for
        continuous metrics (revenue, duration, count, custom).
        """
        if min(control_size, treatment_size) < 30:
            return "fisher_exact"
        if metric_type == "conversion":
            return "z_test"
        return "welch_t_test"

    def z_test_proportions(
        self,
        control_conversions: int,
        control_size: int,
        treatment_conversions: int,
        treatment_size: int,
    ) -> float:
        """Two-proportion z-test (pooled). Returns the two-tailed p-value.

        Formula:
            pooled_p = (x_c + x_t) / (n_c + n_t)
            se = sqrt(pooled_p * (1 - pooled_p) * (1/n_c + 1/n_t))
            z = (p_t - p_c) / se
            p = 2 * (1 - Phi(|z|))
        """
        if control_size <= 0 or treatment_size <= 0:
            return 1.0

        p_c = control_conversions / control_size
        p_t = treatment_conversions / treatment_size
        total = control_size + treatment_size
        pooled_p = (control_conversions + treatment_conversions) / total

        se = math.sqrt(
            pooled_p * (1.0 - pooled_p) * (1.0 / control_size + 1.0 / treatment_size)
        )
        if se == 0.0:
            return 1.0

        z = (p_t - p_c) / se
        p_value = 2.0 * (1.0 - float(stats.norm.cdf(abs(z))))
        return min(1.0, max(0.0, p_value))

    def welch_t_test(
        self,
        control_values: List[float],
        treatment_values: List[float],
    ) -> float:
        """Welch's t-test for two independent groups (unequal variances).

        Returns the two-tailed p-value. Returns NaN when the within-group
        variance is zero (identical data), which callers should handle.
        """
        result = stats.ttest_ind(control_values, treatment_values, equal_var=False)
        return float(result.pvalue)

    def cohens_h(
        self,
        p1: float,
        p2: float,
    ) -> Tuple[float, str]:
        """Cohen's h effect size for two proportions.

        h = 2 * arcsin(sqrt(p2)) - 2 * arcsin(sqrt(p1))

        Positive h means p2 > p1 (treatment outperforms control when called
        as cohens_h(p_control, p_treatment)).

        Labels (EP-016): negligible < 0.2, small < 0.5, medium < 0.8, large >= 0.8
        """
        phi1 = 2.0 * math.asin(math.sqrt(max(0.0, min(1.0, p1))))
        phi2 = 2.0 * math.asin(math.sqrt(max(0.0, min(1.0, p2))))
        h = phi2 - phi1
        label = self._effect_size_label(abs(h))
        return (h, label)

    def cohens_d(
        self,
        control_values: List[float],
        treatment_values: List[float],
    ) -> Tuple[float, str]:
        """Cohen's d effect size for two groups of continuous measurements.

        d = (mean_treatment - mean_control) / pooled_std
        pooled_std = sqrt((std_c^2 + std_t^2) / 2)

        When pooled_std == 0 (constant data in both groups), d is set to
        +inf / -inf based on the direction of the mean difference (or 0.0
        when both means are equal).

        Labels (EP-016): negligible < 0.2, small < 0.5, medium < 0.8, large >= 0.8
        """
        arr_c = np.array(control_values, dtype=float)
        arr_t = np.array(treatment_values, dtype=float)

        mean_c = float(np.mean(arr_c))
        mean_t = float(np.mean(arr_t))

        std_c = float(np.std(arr_c, ddof=1)) if len(arr_c) > 1 else 0.0
        std_t = float(np.std(arr_t, ddof=1)) if len(arr_t) > 1 else 0.0

        pooled_std = math.sqrt((std_c**2 + std_t**2) / 2.0)

        if pooled_std == 0.0:
            if mean_t > mean_c:
                d = float("inf")
            elif mean_t < mean_c:
                d = float("-inf")
            else:
                d = 0.0
        else:
            d = (mean_t - mean_c) / pooled_std

        label = self._effect_size_label(abs(d))
        return (d, label)

    def wilson_confidence_interval(
        self,
        successes: int,
        total: int,
        confidence_level: float = 0.95,
    ) -> Tuple[float, float]:
        """Wilson score confidence interval for a proportion.

        Preferred over the normal approximation because it stays within
        [0, 1] and has better coverage for extreme proportions (p near 0 or 1).

        Returns (lower, upper) both clamped to [0, 1].
        """
        if total <= 0:
            return (0.0, 1.0)

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

    def apply_bonferroni_correction(
        self,
        p_value: float,
        n_tests: int,
    ) -> float:
        """Apply Bonferroni correction to a p-value.

        Corrected p = min(p * n_tests, 1.0).  Controls the family-wise
        error rate (FWER) for multiple comparisons.
        """
        return min(p_value * n_tests, 1.0)

    def calculate_required_sample_size(
        self,
        baseline_rate: float,
        minimum_detectable_effect: float,
        alpha: float = 0.05,
        power: float = 0.80,
    ) -> int:
        """Calculate the required sample size per variant (two-proportion z-test).

        Uses the two-sided formula from EP-016:
            n = (z_alpha * sqrt(2*p_bar*(1-p_bar)) + z_beta * sqrt(p1*(1-p1) + p2*(1-p2)))^2
                / (p2 - p1)^2

        Args:
            baseline_rate: Control group conversion rate (p1).
            minimum_detectable_effect: Absolute change in proportion (p2 = p1 + mde).
            alpha: Type I error rate (1 - confidence_level).
            power: Desired statistical power (1 - beta).

        Returns:
            Minimum observations per variant, rounded up to the nearest integer.
        """
        p1 = baseline_rate
        p2 = p1 + minimum_detectable_effect
        p2 = min(max(p2, 1e-9), 1.0 - 1e-9)

        p_bar = (p1 + p2) / 2.0

        z_alpha = float(stats.norm.ppf(1.0 - alpha / 2.0))
        z_beta = float(stats.norm.ppf(power))

        numerator = (
            z_alpha * math.sqrt(2.0 * p_bar * (1.0 - p_bar))
            + z_beta * math.sqrt(p1 * (1.0 - p1) + p2 * (1.0 - p2))
        ) ** 2
        denominator = (p2 - p1) ** 2

        if denominator == 0.0:
            return 1

        n = math.ceil(numerator / denominator)
        return max(1, n)

    def get_segmented_results(
        self,
        experiment_id: Union[str, UUID],
        segment_by: str,
        metric_id: Optional[Union[str, UUID]] = None,
    ) -> Dict[str, Any]:
        """
        Get experiment results segmented by a specific property.

        Args:
            experiment_id: ID of the experiment
            segment_by: Property to segment results by (e.g., 'country', 'device')
            metric_id: Optional ID of specific metric to analyze

        Returns:
            Dictionary containing segmented results
        """
        # Get experiment with variants and metrics
        experiment = (
            self.db.query(Experiment)
            .options(
                joinedload(Experiment.variants),
                joinedload(Experiment.metric_definitions),
            )
            .filter(Experiment.id == experiment_id)
            .first()
        )

        if not experiment:
            raise ValueError(f"Experiment {experiment_id} not found")

        # Filter metrics if metric_id is provided
        metrics = [
            m
            for m in experiment.metric_definitions
            if not metric_id or str(m.id) == str(metric_id)
        ]

        if not metrics:
            return {"segments": []}

        # Get segment values from event metadata.
        # `schema` comes from get_schema_name(), which only ever returns one of
        # two literal identifiers; all request-derived values are bound params.
        schema = get_schema_name()
        segment_values_query = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            f"""
            SELECT DISTINCT jsonb_extract_path_text(event_metadata, :segment_key) as segment_value
            FROM {schema}.events
            WHERE experiment_id = :experiment_id
            AND event_metadata ? :segment_key
            AND jsonb_extract_path_text(event_metadata, :segment_key) IS NOT NULL
            AND jsonb_extract_path_text(event_metadata, :segment_key) != ''
        """  # nosec B608 - schema is a fixed config identifier, not user input
        )

        segment_values_result = self.db.execute(
            segment_values_query,
            {"segment_key": segment_by, "experiment_id": str(experiment_id)},
        )

        segment_values = [row[0] for row in segment_values_result]

        if not segment_values:
            return {"segments": []}

        # Process each segment
        segments = []
        for segment_value in segment_values:
            segment_result = {"segment_value": segment_value, "metrics": []}

            for metric in metrics:
                metric_result = {
                    "metric_id": str(metric.id),
                    "metric_name": metric.name,
                    "variants": [],
                }

                for variant in experiment.variants:
                    # Get assignments count for this variant in this segment
                    # This is an approximation - ideally would track segment with the assignment
                    segment_assignments_query = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                        f"""
                        SELECT COUNT(DISTINCT a.user_id)
                        FROM {schema}.assignments a
                        JOIN {schema}.events e ON a.user_id = e.user_id AND a.experiment_id = e.experiment_id
                        WHERE a.experiment_id = :experiment_id
                        AND a.variant_id = :variant_id
                        AND e.event_metadata ? :segment_key
                        AND jsonb_extract_path_text(e.event_metadata, :segment_key) = :segment_value
                    """  # nosec B608 - schema is a fixed config identifier, not user input
                    )

                    assignments = (
                        self.db.execute(
                            segment_assignments_query,
                            {
                                "experiment_id": str(experiment_id),
                                "variant_id": str(variant.id),
                                "segment_key": segment_by,
                                "segment_value": segment_value,
                            },
                        ).scalar()
                        or 0
                    )

                    # Get conversions for this variant and segment
                    segment_conversions_query = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                        f"""
                        SELECT {CONVERTING_USERS_SQL}
                        FROM {schema}.events e
                        JOIN {schema}.assignments a ON {CONVERTING_USERS_JOIN}
                        WHERE e.experiment_id = :experiment_id
                        AND e.variant_id = :variant_id
                        AND {CONVERSION_SQL_PREDICATE}
                        AND e.event_metadata ? :segment_key
                        AND jsonb_extract_path_text(e.event_metadata, :segment_key) = :segment_value
                    """  # nosec B608 - schema is a fixed config identifier, not user input
                    )

                    conversions = (
                        self.db.execute(
                            segment_conversions_query,
                            {
                                "experiment_id": str(experiment_id),
                                "variant_id": str(variant.id),
                                "event_name": metric.event_name,
                                "segment_key": segment_by,
                                "segment_value": segment_value,
                            },
                        ).scalar()
                        or 0
                    )

                    # Calculate conversion rate
                    rate = (conversions / assignments) * 100 if assignments > 0 else 0

                    variant_result = {
                        "variant_id": str(variant.id),
                        "variant_name": variant.name,
                        "is_control": variant.is_control,
                        "sample_size": assignments,
                        "conversions": conversions,
                        "conversion_rate": rate,
                    }

                    metric_result["variants"].append(variant_result)

                segment_result["metrics"].append(metric_result)

            segments.append(segment_result)

        return {"segments": segments}
