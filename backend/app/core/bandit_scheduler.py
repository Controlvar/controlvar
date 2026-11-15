"""
BanditScheduler — periodically recomputes MAB variant weights.

Reads per-variant pull/conversion counts and updates BanditState in
PostgreSQL so the dashboard and the ``/tracking/assign`` endpoint can read
fresh weights.

Stats sources, in order of preference
-------------------------------------
1. DynamoDB real-time counters (``DynamoDBCounterService.get_experiment_counters``),
   an optional module reached through ``core.optional_modules``; a build
   without it simply starts at source 2.  Used only when they are at least as
   complete as PostgreSQL's: every variant's DynamoDB pulls and successes must
   both be ``>=`` its PostgreSQL pulls and successes (#426).  A partial
   DynamoDB count -- one manual increment, a counter that started late --
   never replaces the complete PostgreSQL count.
2. PostgreSQL: ``count(Assignment)`` per variant for pulls and, for
   successes, the converting users exactly as ``/results`` counts them
   (``services/event_matching.count_converting_users`` on the primary
   metric's ``event_name``): users assigned to the variant with at least one
   matching event tagged with that variant, each counted once.  Used when
   DynamoDB is unavailable, holds no data for the experiment, or has fewer
   pulls or successes than PostgreSQL for any variant.
3. The previously persisted ``BanditState`` row.
4. Zero-count priors (equal weights).

Typical run cycle
-----------------
1. Query PostgreSQL for all ACTIVE experiments whose optimization_type is not "fixed".
2. For each experiment, fetch per-variant conversion counts (see above).
3. Compute new weights via BanditService.
4. Upsert BanditState row (create if missing, update if present).
5. Return a summary dict ``{updated: N, skipped: N, errors: N}``.

``BanditSchedulerRunner`` wraps the synchronous scheduler in an asyncio
loop so the FastAPI app can keep the weights fresh in the background.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.core.optional_modules import realtime_counter_service
from backend.app.core.scheduler_tick import run_locked_tick
from backend.app.core.stats_engine import ENGINE_VERSION, as_of_bucket, derive_seed
from backend.app.models.analysis_snapshot import AnalysisKind
from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState, BanditStateHistory
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.services.analysis_snapshot_service import build_snapshot
from backend.app.services.bandit_service import (
    BanditService,
    ThompsonSampling,
    VariantStats,
)
from backend.app.services.event_matching import (
    EXPOSURE_EVENT_TYPES,
    count_assigned_users_matching,
    count_converting_users,
)

logger = logging.getLogger(__name__)

SCHEDULER_NAME = "bandit"


def _has_pulls(stats: Optional[Dict[str, VariantStats]]) -> bool:
    """Return True when at least one variant in ``stats`` recorded a pull."""
    return bool(stats) and any(vs.pulls > 0 for vs in stats.values())


def _as_rewards(
    stats: Dict[str, VariantStats], lower_is_better: bool
) -> Dict[str, VariantStats]:
    """Turn converting-user counts into the bandit's reward counts.

    Higher is better: a converting user is a success, unchanged.  Lower is
    better (an error or churn event): a user who did NOT convert is the
    success, so the arm with fewer converting users earns the traffic (#1385).
    ``total_reward`` (UCB1) follows ``successes``.
    """
    if not lower_is_better:
        return stats
    flipped: Dict[str, VariantStats] = {}
    for vid, vs in stats.items():
        successes = max(vs.pulls - vs.successes, 0)
        flipped[vid] = VariantStats(
            variant_id=vid,
            successes=successes,
            failures=vs.pulls - successes,
            pulls=vs.pulls,
            total_reward=float(successes),
        )
    return flipped


def _at_least_as_complete(
    candidate: Optional[Dict[str, VariantStats]],
    reference: Optional[Dict[str, VariantStats]],
) -> bool:
    """Return True when ``candidate`` has at least ``reference``'s pulls and
    successes for every variant.

    Compared variant by variant, not on the totals, so a surplus in one
    variant cannot hide a missing count in another; and on successes as well
    as pulls, so complete assignments with partial conversions cannot
    understate the conversion rate.  Ties count as complete.  A ``reference``
    that is ``None`` (the source was unavailable) is trivially covered; a
    ``candidate`` that is ``None`` covers nothing.
    """
    if candidate is None:
        return False
    if not reference:
        return True
    for vid, ref in reference.items():
        cand = candidate.get(vid)
        if cand is None:
            if ref.pulls > 0 or ref.successes > 0:
                return False
            continue
        if cand.pulls < ref.pulls or cand.successes < ref.successes:
            return False
    return True


class BanditScheduler:
    """
    Scheduler that keeps MAB allocation weights fresh in PostgreSQL.

    Can be called from a background task (``BanditSchedulerRunner``) or
    invoked directly in tests via ``run_once()``.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> Dict[str, int]:
        """
        Execute one scheduling pass.

        Returns
        -------
        dict
            ``{"updated": int, "skipped": int, "errors": int}``
        """
        summary: Dict[str, int] = {"updated": 0, "skipped": 0, "errors": 0}

        # Fetch all ACTIVE experiments that use a bandit algorithm
        active_mab_experiments = (
            self.db.query(Experiment)
            .filter(
                Experiment.status == ExperimentStatus.ACTIVE,
                Experiment.optimization_type != "fixed",
            )
            .all()
        )

        if not active_mab_experiments:
            logger.info("BanditScheduler: no active MAB experiments found")
            return summary

        for experiment in active_mab_experiments:
            try:
                updated = self.update_experiment(experiment)
                if updated:
                    summary["updated"] += 1
                else:
                    summary["skipped"] += 1
            except Exception as exc:
                logger.error(
                    "BanditScheduler: failed to update experiment %s: %s",
                    experiment.id,
                    exc,
                    exc_info=True,
                )
                summary["errors"] += 1
                try:
                    self.db.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass

        logger.info(
            "BanditScheduler run complete: %s",
            summary,
        )
        return summary

    def update_experiment(self, experiment: Experiment) -> bool:
        """
        Recompute and persist bandit weights for a single experiment.

        Parameters
        ----------
        experiment:
            An active MAB experiment SQLAlchemy object.

        Returns
        -------
        bool
            ``True`` on success, ``False`` if skipped (e.g. fixed algorithm).
        """
        if experiment.optimization_type == "fixed":
            logger.debug(
                "Skipping experiment %s (optimization_type=fixed)", experiment.id
            )
            return False

        # Collect variant IDs
        variant_ids: List[str] = [str(v.id) for v in experiment.variants]

        if not variant_ids:
            logger.warning("Experiment %s has no variants — skipping", experiment.id)
            return False

        # Fetch stats (DynamoDB → PostgreSQL → BanditState → priors)
        variant_stats = self.get_variant_stats_from_counters(
            experiment.id, variant_ids, experiment=experiment
        )

        # variant_stats holds RAW converting-user counts: those are what is
        # stored and reported.  The algorithms and the regret estimate get
        # reward counts (a non-converting user is the success when the
        # primary metric is lower-is-better), from the one helper.
        lower_is_better = self._primary_lower_is_better(experiment)
        reward_stats = _as_rewards(variant_stats, lower_is_better)

        # Build the dict expected by BanditService
        variant_data: Dict[str, Dict] = {
            vid: {
                "successes": vs.successes,
                "failures": vs.failures,
                "pulls": vs.pulls,
                "total_reward": vs.total_reward,
            }
            for vid, vs in reward_stats.items()
        }

        # Deterministic seed for the Monte Carlo algorithms:
        # blake2b(experiment_id | tick UTC day | n_samples).  Only Thompson
        # sampling draws random numbers; the others record NULL provenance.
        tick_at = datetime.now(timezone.utc)
        algorithm = experiment.optimization_type
        stochastic = BanditService.is_stochastic(algorithm)
        n_samples: Optional[int] = ThompsonSampling.N_SAMPLES if stochastic else None
        seed: Optional[int] = (
            derive_seed(experiment.id, as_of_bucket(tick_at), n_samples)
            if stochastic
            else None
        )

        # Compute new weights
        new_weights: Dict[str, float] = BanditService.compute_weights(
            algorithm=algorithm,
            variant_data=variant_data,
            seed=seed,
        )

        total_pulls = sum(vs.pulls for vs in variant_stats.values())
        regret_pct = self.estimate_regret_reduction(new_weights, reward_stats)
        now_iso = tick_at.isoformat()

        # Build full variant_weights payload (weights + stats per variant)
        weights_payload: Dict[str, Dict] = {}
        for vid, weight in new_weights.items():
            stats = variant_stats.get(vid)
            weights_payload[vid] = {
                "weight": weight,
                "successes": stats.successes if stats else 0,
                "failures": stats.failures if stats else 0,
                "pulls": stats.pulls if stats else 0,
            }

        # Upsert BanditState
        bandit_state: Optional[BanditState] = (
            self.db.query(BanditState)
            .filter(BanditState.experiment_id == experiment.id)
            .first()
        )

        if bandit_state is None:
            bandit_state = BanditState(
                experiment_id=experiment.id,
                algorithm=algorithm,
                variant_weights=weights_payload,
                total_pulls=total_pulls,
                regret_reduction_pct=regret_pct if regret_pct > 0 else None,
                last_computed_at=now_iso,
                seed=seed,
                n_samples=n_samples,
                engine_version=ENGINE_VERSION,
            )
            self.db.add(bandit_state)
        else:
            bandit_state.algorithm = algorithm
            bandit_state.variant_weights = weights_payload
            bandit_state.total_pulls = total_pulls
            bandit_state.regret_reduction_pct = regret_pct if regret_pct > 0 else None
            bandit_state.last_computed_at = now_iso
            bandit_state.seed = seed
            bandit_state.n_samples = n_samples
            bandit_state.engine_version = ENGINE_VERSION

        # Audit trail: one bandit_state_history row per variant plus one
        # analysis_snapshots row for the tick.  Best-effort inside a savepoint
        # so a failure here never loses the weight update itself.
        self._record_tick_history(
            experiment=experiment,
            algorithm=algorithm,
            weights_payload=weights_payload,
            total_pulls=total_pulls,
            regret_pct=regret_pct,
            seed=seed,
            n_samples=n_samples,
            tick_at=tick_at,
            lower_is_better=lower_is_better,
        )

        self.db.commit()

        logger.info(
            "BanditScheduler: updated weights for experiment %s (%s)",
            experiment.id,
            experiment.optimization_type,
        )
        return True

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    @staticmethod
    def build_tick_history(
        experiment_id: UUID,
        algorithm: str,
        weights_payload: Dict[str, Dict],
        tick_at: datetime,
        seed: Optional[int],
        n_samples: Optional[int],
        lower_is_better: bool = False,
    ) -> List[BanditStateHistory]:
        """Build the ``bandit_state_history`` rows for one tick (not persisted).

        ``successes``/``failures`` are the raw converting-user counts.
        ``alpha``/``beta`` are the Beta-Bernoulli posterior parameters the
        tick used: ``prior + successes`` / ``prior + failures``, from the
        reward counts, so for a lower-is-better metric they are swapped
        (a user who did not convert is the success).  Variant ids
        that are not UUIDs are skipped: they cannot reference ``variants.id``.
        """
        rows: List[BanditStateHistory] = []
        for vid, vdata in weights_payload.items():
            try:
                variant_uuid = vid if isinstance(vid, UUID) else UUID(str(vid))
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "BanditScheduler: skipping history for non-UUID variant id %r", vid
                )
                continue
            successes = int(vdata.get("successes", 0))
            failures = int(vdata.get("failures", 0))
            reward_successes, reward_failures = (
                (failures, successes) if lower_is_better else (successes, failures)
            )
            rows.append(
                BanditStateHistory(
                    experiment_id=experiment_id,
                    variant_id=variant_uuid,
                    algorithm=algorithm,
                    alpha=ThompsonSampling.PRIOR_ALPHA + reward_successes,
                    beta=ThompsonSampling.PRIOR_BETA + reward_failures,
                    weight=float(vdata.get("weight", 0.0)),
                    successes=successes,
                    failures=failures,
                    pulls=int(vdata.get("pulls", 0)),
                    seed=seed,
                    n_samples=n_samples,
                    engine_version=ENGINE_VERSION,
                    tick_at=tick_at,
                )
            )
        return rows

    def _record_tick_history(
        self,
        experiment: Experiment,
        algorithm: str,
        weights_payload: Dict[str, Dict],
        total_pulls: int,
        regret_pct: float,
        seed: Optional[int],
        n_samples: Optional[int],
        tick_at: datetime,
        lower_is_better: bool = False,
    ) -> None:
        """Stage the history rows and the tick snapshot; never raises.

        The rows join the caller's transaction (so a single commit covers
        state + history), but they are flushed inside a savepoint: if the
        audit insert fails only the savepoint is rolled back and the weight
        update still commits.
        """
        try:
            rows: List[Any] = list(
                self.build_tick_history(
                    experiment_id=experiment.id,
                    algorithm=algorithm,
                    weights_payload=weights_payload,
                    tick_at=tick_at,
                    seed=seed,
                    n_samples=n_samples,
                    lower_is_better=lower_is_better,
                )
            )
            rows.append(
                build_snapshot(
                    experiment.id,
                    AnalysisKind.BANDIT,
                    {
                        "algorithm": algorithm,
                        "variant_weights": weights_payload,
                        "total_pulls": int(total_pulls),
                        "regret_reduction_pct": regret_pct if regret_pct > 0 else None,
                    },
                    seed=seed,
                    n_samples=n_samples,
                    as_of=tick_at,
                )
            )
            with self.db.begin_nested():
                self.db.add_all(rows)
        except Exception as exc:
            logger.warning(
                "BanditScheduler: could not record tick history for experiment %s: %s",
                experiment.id,
                exc,
            )

    # ------------------------------------------------------------------
    # Stats sources
    # ------------------------------------------------------------------

    def get_variant_stats_from_counters(
        self,
        experiment_id: UUID,
        variant_ids: List[str],
        experiment: Optional[Experiment] = None,
    ) -> Dict[str, VariantStats]:
        """
        Fetch per-variant pull/conversion counts for an experiment.

        Sources are tried in order: DynamoDB counters, PostgreSQL
        (assignments + conversion events), the persisted BanditState, and
        finally zero-count priors.  A source is skipped when it raises or
        when no variant has recorded a pull.  DynamoDB is additionally
        skipped when any variant has fewer pulls or successes there than in
        PostgreSQL:
        PostgreSQL's assignment rows are the complete record, and the
        real-time counters are preferred only while they keep up with it.

        Parameters
        ----------
        experiment_id:
            UUID of the experiment.
        variant_ids:
            List of variant ID strings to fetch.
        experiment:
            Optional experiment object; used to read the primary metric
            without an extra query.  Loaded from the DB when omitted.

        Returns
        -------
        dict
            ``{variant_id: VariantStats}``
        """
        dynamo_stats = self._stats_from_dynamodb(experiment_id, variant_ids)
        pg_stats = self._stats_from_postgres(experiment_id, variant_ids, experiment)

        # Every source returns RAW counts (successes = converting users, as
        # stored and reported); the metric's direction is applied only at the
        # algorithm input, by ``_as_rewards`` in ``update_experiment``.
        if _has_pulls(dynamo_stats):
            if _at_least_as_complete(dynamo_stats, pg_stats):
                return dynamo_stats  # type: ignore[return-value]
            logger.warning(
                "BanditScheduler: DynamoDB counters for experiment %s have fewer "
                "pulls or successes than PostgreSQL; using PostgreSQL",
                experiment_id,
            )

        if _has_pulls(pg_stats):
            return pg_stats  # type: ignore[return-value]

        stats = self._stats_from_bandit_state(experiment_id, variant_ids)
        if stats is not None:
            return stats

        # No data at all — use zero-count priors
        return {vid: VariantStats(variant_id=vid) for vid in variant_ids}

    @staticmethod
    def _load_counter_service_class() -> Optional[type]:
        """
        Return the DynamoDB counter service class, or ``None`` when absent.

        Real-time DynamoDB counters are the optional ``counters`` module, so
        the core never imports it directly — it asks the
        ``core.optional_modules`` seam.  A build that does not ship the
        service is a supported configuration, not an error: the caller simply
        falls through to the next stats source (PostgreSQL).
        """
        return realtime_counter_service()

    def _stats_from_dynamodb(
        self, experiment_id: UUID, variant_ids: List[str]
    ) -> Optional[Dict[str, VariantStats]]:
        """Read counters from DynamoDB; ``None`` when the service is unavailable."""
        try:
            # Inside the try: the provider resolves the class at each call and
            # can raise (an import that fails mid-process); that is a reason to
            # fall through to PostgreSQL, not to abandon the tick.
            counter_service_cls = self._load_counter_service_class()
            if counter_service_cls is None:
                return None
            counter_service = counter_service_cls()
            counters = counter_service.get_experiment_counters(str(experiment_id))
        except Exception as exc:
            logger.warning(
                "BanditScheduler: DynamoDB unavailable for experiment %s (%s); "
                "falling back to PostgreSQL",
                experiment_id,
                exc,
            )
            return None

        by_variant: Dict[str, Any] = {}
        for vc in getattr(counters, "variants", None) or []:
            by_variant[str(vc.variant_id)] = vc

        stats: Dict[str, VariantStats] = {}
        for vid in variant_ids:
            vc = by_variant.get(vid)
            assignments = int(getattr(vc, "assignments", 0) or 0) if vc else 0
            conversions = int(getattr(vc, "conversions", 0) or 0) if vc else 0
            stats[vid] = VariantStats(
                variant_id=vid,
                successes=conversions,
                failures=max(assignments - conversions, 0),
                pulls=assignments,
                total_reward=float(conversions),
            )
        return stats

    def _stats_from_postgres(
        self,
        experiment_id: UUID,
        variant_ids: List[str],
        experiment: Optional[Experiment] = None,
    ) -> Optional[Dict[str, VariantStats]]:
        """
        Derive pulls/successes from assignments and conversion events.

        pulls     = number of Assignment rows per variant
        successes = converting users per variant, as ``/results`` counts
                    them (``event_matching.count_converting_users``): users
                    assigned to the variant with at least one event for the
                    primary metric's ``event_name`` tagged with that variant.
        """
        try:
            if experiment is None:
                experiment = (
                    self.db.query(Experiment)
                    .filter(Experiment.id == experiment_id)
                    .first()
                )
            event_name = self._primary_event_name(experiment)
            pulls = self._count_assignments_by_variant(experiment_id)
            successes = self._count_conversions_by_variant(
                experiment_id, event_name, variant_ids
            )
        except Exception as exc:
            logger.warning(
                "BanditScheduler: PostgreSQL stats unavailable for experiment %s (%s); "
                "falling back to BanditState",
                experiment_id,
                exc,
            )
            try:
                self.db.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            return None

        stats: Dict[str, VariantStats] = {}
        for vid in variant_ids:
            n_pulls = int(pulls.get(vid, 0))
            n_successes = min(int(successes.get(vid, 0)), n_pulls)
            stats[vid] = VariantStats(
                variant_id=vid,
                successes=n_successes,
                failures=max(n_pulls - n_successes, 0),
                pulls=n_pulls,
                total_reward=float(n_successes),
            )
        return stats

    def _stats_from_bandit_state(
        self, experiment_id: UUID, variant_ids: List[str]
    ) -> Optional[Dict[str, VariantStats]]:
        """Read the previously persisted stats; ``None`` when there are none."""
        bandit_state: Optional[BanditState] = (
            self.db.query(BanditState)
            .filter(BanditState.experiment_id == experiment_id)
            .first()
        )

        if not bandit_state or not bandit_state.variant_weights:
            return None

        stats: Dict[str, VariantStats] = {}
        for vid in variant_ids:
            vdata = bandit_state.variant_weights.get(vid, {})
            if not isinstance(vdata, dict):
                vdata = {}
            stats[vid] = VariantStats(
                variant_id=vid,
                successes=int(vdata.get("successes", 0)),
                failures=int(vdata.get("failures", 0)),
                pulls=int(vdata.get("pulls", 0)),
                total_reward=float(vdata.get("successes", 0)),
            )
        return stats

    @staticmethod
    def _primary_metric(experiment: Optional[Experiment]) -> Any:
        """The ``is_primary`` Metric row, else the first one, else ``None``."""
        if experiment is None:
            return None
        metrics = list(getattr(experiment, "metric_definitions", None) or [])
        primary = next((m for m in metrics if getattr(m, "is_primary", False)), None)
        if primary is None and metrics:
            primary = metrics[0]
        return primary

    @classmethod
    def _primary_lower_is_better(cls, experiment: Optional[Experiment]) -> bool:
        """The primary metric's direction; only ``True`` means lower is better."""
        primary = cls._primary_metric(experiment)
        return getattr(primary, "lower_is_better", False) is True

    @staticmethod
    def _primary_event_name(experiment: Optional[Experiment]) -> Optional[str]:
        """
        Resolve the conversion event for an experiment.

        Uses the ``is_primary`` Metric row, then the first Metric row, then
        the legacy ``experiments.metrics['primary_metric']`` JSON value.
        Returns ``None`` when nothing is configured.
        """
        if experiment is None:
            return None

        primary = BanditScheduler._primary_metric(experiment)
        if primary is not None and getattr(primary, "event_name", None):
            return str(primary.event_name)

        legacy = getattr(experiment, "metrics", None)
        if isinstance(legacy, dict):
            name = legacy.get("primary_metric")
            if isinstance(name, str) and name:
                return name
        return None

    def _count_assignments_by_variant(self, experiment_id: UUID) -> Dict[str, int]:
        """``{variant_id: assignment_count}`` for the experiment."""
        rows = (
            self.db.query(Assignment.variant_id, func.count(Assignment.id))
            .filter(Assignment.experiment_id == experiment_id)
            .group_by(Assignment.variant_id)
            .all()
        )
        return {str(variant_id): int(count) for variant_id, count in rows}

    def _count_conversions_by_variant(
        self,
        experiment_id: UUID,
        event_name: Optional[str],
        variant_ids: List[str],
    ) -> Dict[str, int]:
        """
        ``{variant_id: converting_users}`` for the experiment.

        The definition ``/results`` uses (#338): a user counts once, however
        many conversion events they sent, and only for the variant they are
        assigned to and the event is tagged with.  When ``event_name`` is
        ``None`` (no metric configured) every event other than an experiment view
        counts.
        """
        if event_name:
            return {
                vid: count_converting_users(self.db, experiment_id, vid, event_name)
                for vid in variant_ids
            }
        any_event = Event.event_type.notin_(EXPOSURE_EVENT_TYPES)
        return {
            vid: count_assigned_users_matching(self.db, experiment_id, vid, any_event)
            for vid in variant_ids
        }

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------

    def estimate_regret_reduction(
        self,
        weights: Dict[str, float],
        variant_stats: Dict[str, VariantStats],
    ) -> float:
        """
        Estimate percentage regret reduction vs. uniform allocation.

        Regret is defined as the opportunity cost of not always showing
        the best variant.  We compare:
        - Expected reward under current weights
        - Expected reward under uniform allocation

        Parameters
        ----------
        weights:
            Current bandit weights ``{variant_id: float}``.
        variant_stats:
            Per-variant stats ``{variant_id: VariantStats}``.

        Returns
        -------
        float
            Percentage improvement (0.0 for uniform weights or no data).
        """
        if not weights or not variant_stats:
            return 0.0

        # Compute conversion rates
        rates: Dict[str, float] = {
            vid: vs.conversion_rate for vid, vs in variant_stats.items()
        }

        # Expected reward under current bandit weights
        bandit_reward = sum(
            weights.get(vid, 0.0) * rates.get(vid, 0.0) for vid in weights
        )

        # Expected reward under uniform allocation
        n = len(weights)
        if n == 0:
            return 0.0
        uniform_weight = 1.0 / n
        uniform_reward = sum(uniform_weight * rates.get(vid, 0.0) for vid in weights)

        if uniform_reward == 0.0:
            return 0.0

        reduction = (bandit_reward - uniform_reward) / uniform_reward * 100.0
        return max(0.0, reduction)

    def get_recommendation(
        self,
        weights: Dict[str, float],
        variant_names: Dict[str, str],
    ) -> str:
        """
        Return a human-readable recommendation string.

        Rules
        -----
        - ``max_weight >= 0.8``  → ``"DEPLOYING_<VariantName>"``
        - ``max_weight >= 0.5``  → ``"CONVERGING"``
        - otherwise             → ``"EXPLORING"``

        Parameters
        ----------
        weights:
            Current bandit weights ``{variant_id: float}``.
        variant_names:
            Mapping ``{variant_id: variant_name}`` for display.

        Returns
        -------
        str
            Recommendation string.
        """
        if not weights:
            return "EXPLORING"

        max_weight = max(weights.values())

        if max_weight >= 0.8:
            winning_id = max(weights, key=weights.get)
            name = variant_names.get(winning_id, winning_id)
            return f"DEPLOYING_{name}"

        if max_weight >= 0.5:
            return "CONVERGING"

        return "EXPLORING"


# ---------------------------------------------------------------------------
# Background runner
# ---------------------------------------------------------------------------


def _is_test_environment() -> bool:
    """True when running under pytest / the test settings profile."""
    if bool(getattr(settings, "TESTING", False)):
        return True
    if os.environ.get("APP_ENV", "").lower() == "test":
        return True
    return os.environ.get("TESTING", "").lower() in ("1", "true", "yes")


class BanditSchedulerRunner:
    """
    Asyncio wrapper that runs ``BanditScheduler.run_once()`` periodically.

    Each pass opens a fresh ``SessionLocal()`` session and executes the
    synchronous scheduler in a worker thread so the event loop is never
    blocked.  Errors are logged and the loop keeps going.

    The runner does not start under the test profile (``settings.TESTING``
    or ``APP_ENV=test``) unless ``run_in_tests=True`` is passed.
    """

    def __init__(
        self,
        interval_minutes: Optional[int] = None,
        run_in_tests: bool = False,
    ) -> None:
        if interval_minutes is None:
            interval_minutes = int(
                getattr(settings, "BANDIT_UPDATE_INTERVAL_MINUTES", 5)
            )
        self.interval_minutes = interval_minutes
        # Seconds slept between passes; tests shorten this directly.
        self.interval_seconds: float = float(interval_minutes) * 60.0
        self.run_in_tests = run_in_tests
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self.run_count = 0
        self.error_count = 0
        self.last_result: Optional[Dict[str, int]] = None
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        """Start the background loop (no-op in the test environment)."""
        if self.is_running:
            logger.warning("Bandit scheduler is already running")
            return

        if not self.run_in_tests and _is_test_environment():
            logger.info("Bandit scheduler not started (test environment)")
            return

        self.is_running = True
        self.task = asyncio.create_task(self._run_scheduler())
        logger.info(
            "Bandit scheduler started with %s minute interval", self.interval_minutes
        )

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if not self.is_running:
            return

        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Bandit scheduler task ended with error: %s", exc)
            self.task = None
        logger.info("Bandit scheduler stopped")

    async def _run_scheduler(self) -> None:
        """Loop: run one pass, sleep, repeat until stopped."""
        while self.is_running:
            try:
                # One pass under the advisory lock; records the run and
                # skips when another replica holds the lock.
                await run_locked_tick(SCHEDULER_NAME, self._run_once_for_tick)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.error_count += 1
                self.last_error = str(exc)
                logger.error("Error in bandit scheduler: %s", exc, exc_info=True)

            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    async def run_once(self) -> Dict[str, int]:
        """Execute a single scheduler pass in a worker thread."""
        result = await asyncio.to_thread(self._run_sync)
        self.run_count += 1
        self.last_result = result
        return result

    async def _run_once_for_tick(self) -> Dict[str, Any]:
        """``run_once`` reshaped for ``run_locked_tick`` (items_processed/failed)."""
        result = await self.run_once()
        return {
            "items_processed": int(result.get("updated", 0))
            + int(result.get("skipped", 0)),
            "items_failed": int(result.get("errors", 0)),
            "metadata": dict(result),
        }

    @staticmethod
    def _run_sync() -> Dict[str, int]:
        """Open a session, run the scheduler once, close the session."""
        from backend.app.db.session import SessionLocal

        db = SessionLocal()
        try:
            return BanditScheduler(db).run_once()
        finally:
            db.close()


# Global instance started/stopped by the FastAPI lifespan
bandit_scheduler_runner = BanditSchedulerRunner()
