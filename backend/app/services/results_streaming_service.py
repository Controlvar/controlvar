"""
Results Streaming Service for EP-058: Real-time WebSocket Streaming Results.

Computes live experiment result snapshots for broadcasting to WebSocket subscribers.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ResultsStreamingService:
    """
    Computes live results snapshots for broadcasting to WebSocket subscribers.

    Uses a db_session_factory (callable returning a Session) so that each
    snapshot computation gets a fresh, short-lived DB session.
    """

    def __init__(self, db_session_factory: Callable) -> None:
        self._db_factory = db_session_factory

    async def get_live_snapshot(self, experiment_id: str) -> dict:
        """
        Compute and return a live results snapshot for the given experiment.

        Returns a dict with the shape:
        {
            "event": "results_update",
            "experiment_id": str,
            "timestamp": ISO string,
            "status": "active" | "paused" | "completed" | "draft",
            "variants": [
                {
                    "key": str,
                    "name": str,
                    "participant_count": int,
                    "conversion_count": int,
                    "conversion_rate": float,
                    "relative_lift": float,
                    "p_value": float | null,
                    "is_control": bool
                }
            ],
            "total_participants": int,
            "days_running": int | null,
            "is_significant": bool
        }

        Returns a skeleton snapshot with 0 counts if the experiment is not found
        or if metrics data is unavailable.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            db = self._db_factory()
            try:
                return await self._compute_snapshot(experiment_id, db, now_iso)
            finally:
                db.close()
        except Exception as exc:
            logger.warning(
                "Failed to compute snapshot for experiment %s: %s",
                experiment_id,
                exc,
            )
            return self._error_snapshot(experiment_id, now_iso, str(exc))

    async def _compute_snapshot(
        self, experiment_id: str, db: Any, now_iso: str
    ) -> dict:
        """Internal method: queries DB and builds snapshot dict."""
        from sqlalchemy.orm import joinedload

        from backend.app.models.experiment import Experiment

        # Fetch experiment with variants
        experiment = (
            db.query(Experiment)
            .options(joinedload(Experiment.variants))
            .filter(Experiment.id == experiment_id)
            .first()
        )

        if experiment is None:
            return self._not_found_snapshot(experiment_id, now_iso)

        # Determine status string
        status_val = experiment.status
        if hasattr(status_val, "value"):
            status_str = status_val.value
        else:
            status_str = str(status_val)

        # Compute days running
        days_running: Optional[int] = None
        if experiment.start_date:
            start = experiment.start_date
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - start
            days_running = max(0, delta.days)

        # Build variant results
        variants_data = self._compute_variant_results(experiment_id, experiment, db)

        total_participants = sum(v["participant_count"] for v in variants_data)

        # Determine overall significance
        is_significant = any(
            v["p_value"] is not None and v["p_value"] < 0.05
            for v in variants_data
            if not v["is_control"]
        )

        return {
            "event": "results_update",
            "experiment_id": experiment_id,
            "timestamp": now_iso,
            "status": status_str,
            "variants": variants_data,
            "total_participants": total_participants,
            "days_running": days_running,
            "is_significant": is_significant,
        }

    def _compute_variant_results(
        self, experiment_id: str, experiment: Any, db: Any
    ) -> List[dict]:
        """
        Query assignment and event counts per variant.

        Returns a list of variant result dicts. Gracefully handles missing
        assignments/events tables by returning 0 counts.
        """
        from sqlalchemy import func

        variants = experiment.variants or []
        if not variants:
            return []

        # Try to pull per-variant counts from DB
        try:
            from backend.app.models.assignment import Assignment

            # Assignment counts per variant
            assignment_rows = (
                db.query(Assignment.variant_id, func.count(Assignment.id).label("cnt"))
                .filter(Assignment.experiment_id == experiment_id)
                .group_by(Assignment.variant_id)
                .all()
            )
            assignment_map: Dict[str, int] = {
                str(row.variant_id): int(row.cnt) for row in assignment_rows
            }

            # Conversion counts per variant (using first available primary metric)
            primary_metric = None
            if (
                hasattr(experiment, "metric_definitions")
                and experiment.metric_definitions
            ):
                primary_metric = next(
                    (m for m in experiment.metric_definitions if m.is_primary),
                    experiment.metric_definitions[0]
                    if experiment.metric_definitions
                    else None,
                )

            from backend.app.services.event_matching import count_converting_users

            # Converting users per variant, as /results counts them (#233):
            # a user with several conversion events counts once.
            event_name = primary_metric.event_name if primary_metric else None
            conversion_map: Dict[str, int] = {
                str(v.id): count_converting_users(db, experiment_id, v.id, event_name)
                for v in variants
            }

        except Exception as exc:
            logger.debug("Could not query assignments/events: %s", exc)
            assignment_map = {}
            conversion_map = {}

        # Identify control variant for lift calculation
        control_rate: Optional[float] = None
        control_participants = 0
        control_conversions = 0
        for v in variants:
            if v.is_control:
                vid = str(v.id)
                control_participants = assignment_map.get(vid, 0)
                control_conversions = conversion_map.get(vid, 0)
                control_rate = (
                    control_conversions / control_participants
                    if control_participants > 0
                    else 0.0
                )
                break

        results: List[dict] = []
        for v in variants:
            vid = str(v.id)
            participants = assignment_map.get(vid, 0)
            conversions = conversion_map.get(vid, 0)
            rate = conversions / participants if participants > 0 else 0.0

            # Relative lift vs control
            if control_rate is not None and control_rate > 0 and not v.is_control:
                relative_lift = (rate - control_rate) / control_rate
            else:
                relative_lift = 0.0

            # Compute p-value using two-proportion z-test if we have data
            p_value: Optional[float] = None
            if not v.is_control and control_rate is not None:
                p_value = self._compute_p_value(
                    participants,
                    conversions,
                    control_participants,
                    control_conversions,
                )

            results.append(
                {
                    "key": v.name.lower().replace(" ", "_"),
                    "name": v.name,
                    "participant_count": participants,
                    "conversion_count": conversions,
                    "conversion_rate": round(rate, 6),
                    "relative_lift": round(relative_lift, 6),
                    "p_value": p_value,
                    "is_control": bool(v.is_control),
                }
            )

        return results

    def _compute_p_value(
        self,
        treatment_n: int,
        treatment_conv: int,
        control_n: int,
        control_conv: int,
    ) -> Optional[float]:
        """
        Compute a two-sided pooled two-proportion z-test p-value.

        The pooled rate is (x1 + x2) / (n1 + n2) and the standard error uses
        each arm's own n: sqrt(p (1 - p) (1/n1 + 1/n2)).  The arms need not be
        the same size (a bandit, a 90/10 split).

        Returns None if either arm is too small for meaningful inference (no
        control participants, fewer than 5 treatment participants) or if the
        pooled rate is 0 or 1.
        """
        try:
            import math

            if treatment_n < 5 or control_n < 1:
                return None

            p_pool = (treatment_conv + control_conv) / (treatment_n + control_n)
            if p_pool <= 0 or p_pool >= 1:
                return None

            se = math.sqrt(p_pool * (1 - p_pool) * (1 / treatment_n + 1 / control_n))
            if se == 0:
                return None

            treatment_rate = treatment_conv / treatment_n
            control_rate = control_conv / control_n
            z = abs(treatment_rate - control_rate) / se

            # Approximate two-sided p-value from z-score using erfc
            p_value = 2 * (1 - _norm_cdf(z))
            return round(p_value, 6)
        except Exception:
            return None

    def _not_found_snapshot(self, experiment_id: str, now_iso: str) -> dict:
        """Return a snapshot indicating the experiment was not found."""
        return {
            "event": "results_update",
            "experiment_id": experiment_id,
            "timestamp": now_iso,
            "status": "not_found",
            "variants": [],
            "total_participants": 0,
            "days_running": None,
            "is_significant": False,
            "error": f"Experiment {experiment_id} not found",
        }

    def _error_snapshot(self, experiment_id: str, now_iso: str, error_msg: str) -> dict:
        """Return a skeleton snapshot when an error occurs."""
        return {
            "event": "results_update",
            "experiment_id": experiment_id,
            "timestamp": now_iso,
            "status": "error",
            "variants": [],
            "total_participants": 0,
            "days_running": None,
            "is_significant": False,
            "error": error_msg,
        }

    async def compute_and_broadcast(
        self,
        manager: Any,
        experiment_id: str,
    ) -> None:
        """Compute a live snapshot and broadcast it to all subscribers."""
        snapshot = await self.get_live_snapshot(experiment_id)
        await manager.broadcast(experiment_id, snapshot)


def _norm_cdf(z: float) -> float:
    """Approximate normal CDF using math.erfc."""
    import math

    return 0.5 * math.erfc(-z / math.sqrt(2))
