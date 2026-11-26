"""
Experiment Interaction Detection Service.

Two analyses of experiments that run at the same time:

* ``GET /interactions/scan`` (stable) reports the user overlap of every pair
  of active experiments (``scan_active_experiments``).  It does not test
  interactions: its interaction, novelty and SUTVA sub-results are None and
  its risk level comes from the overlap alone.
* ``GET /interactions/{a}/{b}`` (beta, #219) reports the real overlap of two
  experiments and, for each treatment of each one, tests whether its lift on
  the experiment's primary conversion metric differs across the other
  experiment's arms (``analyze_pair_interactions``; the statistics are in
  ``services/interaction_analysis.py``).
"""

from dataclasses import dataclass, field
from typing import Any, Container, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session, selectinload

from backend.app.models.assignment import Assignment
from backend.app.models.experiment import Experiment, MetricType
from backend.app.schemas.interaction import (
    InteractionArm,
    InteractionPairResponse,
    InteractionRow,
)
from backend.app.services import interaction_analysis as ia
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.analysis_settings import resolve_analysis_settings
from backend.app.services.event_matching import (
    MetricEvents,
    _assigned_pairs,
    converting_user_ids,
    metric_events,
)
from backend.app.services.sufficient_stats_analysis import adjusted_p_values

#: How many of the smaller experiment's assignments are streamed per fetch,
#: and how many of them are looked up in the larger experiment at once.
ASSIGNMENT_STREAM_CHUNK = 10_000

#: The pair route bounds each of its statements to this many milliseconds
#: (``SET LOCAL statement_timeout``, PostgreSQL only).  It bounds every
#: statement, not the request as a whole.
PAIR_STATEMENT_TIMEOUT_MS = 30_000

#: The sentence after "the interaction was not tested: " for each reason.
REASON_SENTENCES = {
    ia.MUTUAL_EXCLUSION_GROUP: (
        "both experiments are in the same mutual exclusion group, so no user "
        "should be in both."
    ),
    ia.NO_SHARED_USERS: "the two experiments share no users.",
    ia.NO_METRIC: "the experiment has no metric.",
    ia.NOT_A_PROPORTION_METRIC: (
        "its primary metric is not a conversion metric; only conversion "
        "metrics are tested."
    ),
    ia.NO_CONTROL_VARIANT: "the experiment has no control variant.",
    ia.TOO_FEW_SHARED_USERS: (
        "fewer than two of the other experiment's arms share users with it, or "
        f"some combination of arms has fewer than {ia.MIN_USERS_PER_CELL} "
        "shared users."
    ),
    ia.TOO_FEW_CONVERSIONS: (
        "some combination of arms is expected to have fewer than "
        f"{ia.MIN_EXPECTED_PER_CELL} converters or non-converters."
    ),
}

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class InteractionResult:
    """Result of a 2×2 interaction test between two experiments."""

    has_interaction: bool
    p_value: float
    interaction_effect_size: float
    warning_message: Optional[str] = None


@dataclass
class NoveltyResult:
    """Result of a novelty effect analysis on daily treatment effects."""

    has_novelty: bool
    decline_rate: float  # slope of linear regression on daily_effects
    recommendation: str


@dataclass
class SUTVAResult:
    """Result of a SUTVA (Stable Unit Treatment Value Assumption) check."""

    has_violation: bool
    contamination_rate: float
    warning_message: Optional[str] = None


@dataclass
class InteractionAnalysis:
    """Full interaction analysis for a pair of experiments."""

    experiment_a_id: str
    experiment_b_id: str
    overlap_coefficient: float
    has_significant_overlap: bool
    interaction_result: Optional[InteractionResult]
    novelty_result: Optional[NoveltyResult]
    sutva_result: Optional[SUTVAResult]
    overall_risk: str  # "low" | "medium" | "high"
    recommendations: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------


class InteractionDetectionService:
    """Service for detecting interactions between simultaneously running experiments."""

    OVERLAP_THRESHOLD = 0.30  # Flag pairs with > 30 % shared users
    HIGH_OVERLAP_THRESHOLD = 0.60  # Above this the overlap alone is high risk

    # ------------------------------------------------------------------
    # Overlap detection
    # ------------------------------------------------------------------

    @staticmethod
    def compute_user_overlap(users_a: Set[str], users_b: Set[str]) -> float:
        """Compute Jaccard similarity: |A ∩ B| / |A ∪ B|.

        Returns 0.0 when either set is empty.
        """
        if not users_a or not users_b:
            return 0.0
        intersection = len(users_a & users_b)
        union = len(users_a | users_b)
        if union == 0:
            return 0.0
        return intersection / union

    @staticmethod
    def has_significant_overlap(
        users_a: Set[str],
        users_b: Set[str],
        threshold: float = 0.30,
    ) -> bool:
        """Return True when the Jaccard similarity exceeds the threshold."""
        jaccard = InteractionDetectionService.compute_user_overlap(users_a, users_b)
        return jaccard > threshold

    # ------------------------------------------------------------------
    # High-level service methods (database-aware)
    # ------------------------------------------------------------------

    def analyze_experiment_pair(
        self,
        experiment_a_id: str,
        experiment_b_id: str,
        db,
    ) -> Optional[InteractionAnalysis]:
        """Full interaction analysis for two experiments.

        Returns None if the experiments do not have significant user overlap
        (Jaccard < OVERLAP_THRESHOLD).
        """
        users_a = self._get_experiment_users(experiment_a_id, db)
        users_b = self._get_experiment_users(experiment_b_id, db)

        overlap = self.compute_user_overlap(users_a, users_b)
        significant = overlap > self.OVERLAP_THRESHOLD

        if not significant:
            return None

        # Only the overlap is measured (#219).  The interaction, novelty and
        # SUTVA analyses need outcomes by variant (and, for novelty, by day),
        # which this service does not read yet, so they are left as None --
        # "not computed" -- rather than filled from user counts.
        analysis = InteractionAnalysis(
            experiment_a_id=experiment_a_id,
            experiment_b_id=experiment_b_id,
            overlap_coefficient=overlap,
            has_significant_overlap=significant,
            interaction_result=None,
            novelty_result=None,
            sutva_result=None,
            overall_risk=self.risk_from_overlap(overlap),
            recommendations=[],
        )
        analysis.recommendations = self._build_recommendations(analysis)
        return analysis

    def scan_active_experiments(self, db) -> List[InteractionAnalysis]:
        """Scan all active experiment pairs for interactions.

        Pairs with overlap < OVERLAP_THRESHOLD are excluded.
        """
        exp_ids = self._get_active_experiment_ids(db)
        results: List[InteractionAnalysis] = []

        for i in range(len(exp_ids)):
            for j in range(i + 1, len(exp_ids)):
                analysis = self.analyze_experiment_pair(exp_ids[i], exp_ids[j], db)
                if analysis is not None:
                    results.append(analysis)

        return results

    # ------------------------------------------------------------------
    # Risk aggregation
    # ------------------------------------------------------------------

    @classmethod
    def risk_from_overlap(cls, overlap: float) -> str:
        """Risk level from the overlap alone, the one input that is measured.

        At or below ``OVERLAP_THRESHOLD`` (0.3) the pair is ``low``; up to
        ``HIGH_OVERLAP_THRESHOLD`` (0.6) it is ``medium``; above that ``high``.
        These are the bands in docs/api/interaction-detection.md.
        """
        if overlap > cls.HIGH_OVERLAP_THRESHOLD:
            return "high"
        if overlap > cls.OVERLAP_THRESHOLD:
            return "medium"
        return "low"

    def _build_recommendations(self, analysis: InteractionAnalysis) -> List[str]:
        """Recommendations from the overlap, plus any sub-result that exists."""
        recs: List[str] = []

        if analysis.has_significant_overlap:
            recs.append(
                f"The two experiments share users (overlap "
                f"{analysis.overlap_coefficient:.2f}). Each one's results include "
                "users exposed to the other; consider a mutual exclusion group "
                "for future experiments on the same surface."
            )
        if analysis.interaction_result and analysis.interaction_result.warning_message:
            recs.append(analysis.interaction_result.warning_message)
        if analysis.novelty_result and analysis.novelty_result.has_novelty:
            recs.append(analysis.novelty_result.recommendation)
        if analysis.sutva_result and analysis.sutva_result.warning_message:
            recs.append(analysis.sutva_result.warning_message)
        if not recs:
            recs.append(
                "No significant interaction concerns detected for this experiment pair."
            )
        return recs

    # ------------------------------------------------------------------
    # The pair route (beta, #219): overlap and the interaction test
    # ------------------------------------------------------------------

    @staticmethod
    def load_experiment(db: Session, experiment_id: Any) -> Optional[Experiment]:
        """The experiment with its variants and metrics, or None."""
        return (
            db.query(Experiment)
            .options(
                selectinload(Experiment.variants),
                selectinload(Experiment.metric_definitions),
            )
            .filter(Experiment.id == experiment_id)
            .first()
        )

    @staticmethod
    def bound_statement_time(db: Session) -> None:
        """Bound each later statement of this transaction (PostgreSQL only).

        ``set_config('statement_timeout', ..., true)`` is ``SET LOCAL``: it
        lasts until the transaction ends, which the request's session does
        when it closes.
        """
        dialect = getattr(getattr(db.get_bind(), "dialect", None), "name", None)
        if dialect == "postgresql":
            db.execute(
                text("SELECT set_config('statement_timeout', :ms, true)"),
                {"ms": str(int(PAIR_STATEMENT_TIMEOUT_MS))},
            )

    @staticmethod
    def _arm_totals(db: Session, experiment_id: Any) -> Dict[str, int]:
        """Assigned users per variant of one experiment, in one query."""
        rows = (
            db.query(Assignment.variant_id, func.count(Assignment.id))
            .filter(Assignment.experiment_id == experiment_id)
            .group_by(Assignment.variant_id)
            .all()
        )
        return {str(variant_id): int(count) for variant_id, count in rows}

    @staticmethod
    def _shared_assignments(
        db: Session, smaller_id: Any, larger_id: Any
    ) -> Dict[str, Tuple[str, str]]:
        """``user_id -> (variant in smaller, variant in larger)`` for users in both.

        The smaller experiment's assignments are streamed
        ``ASSIGNMENT_STREAM_CHUNK`` at a time, and each chunk's users are
        looked up in the larger experiment by the bound-array lookup of
        ``event_matching._assigned_pairs`` (no SQL join).  Only shared users
        are kept, so memory grows with the shared users and one chunk, not
        with either experiment.
        """
        shared: Dict[str, Tuple[str, str]] = {}
        chunk: Dict[str, str] = {}
        rows = (
            db.query(Assignment.user_id, Assignment.variant_id)
            .filter(Assignment.experiment_id == smaller_id)
            .yield_per(ASSIGNMENT_STREAM_CHUNK)
        )
        for user_id, variant_id in rows:
            chunk[str(user_id)] = str(variant_id)
            if len(chunk) >= ASSIGNMENT_STREAM_CHUNK:
                _keep_shared(db, larger_id, chunk, shared)
        if chunk:
            _keep_shared(db, larger_id, chunk, shared)
        return shared

    @staticmethod
    def _shared_converters(
        db: Session,
        experiment: Experiment,
        events: Optional[MetricEvents],
        shared_users: Container[str],
    ) -> Set[str]:
        """The shared users ``/results`` counts as converters in ``experiment``.

        One variant's converters are read at a time, kept only where shared,
        and dropped before the next variant is read.
        """
        converted: Set[str] = set()
        for variant in experiment.variants:
            converters = converting_user_ids(db, experiment.id, variant.id, events)
            converted.update(user for user in converters if user in shared_users)
        return converted

    def analyze_pair_interactions(
        self,
        experiment_a: Experiment,
        experiment_b: Experiment,
        db: Session,
    ) -> InteractionPairResponse:
        """The overlap of two experiments and, per treatment, the interaction test.

        Every reader raises: a database error reaches the route as it is.
        PostgreSQL aborts the transaction on the first error, so a partial
        answer cannot be built.
        """
        totals_a = self._arm_totals(db, experiment_a.id)
        totals_b = self._arm_totals(db, experiment_b.id)
        size_a, size_b = sum(totals_a.values()), sum(totals_b.values())

        if size_a <= size_b:
            in_both = self._shared_assignments(db, experiment_a.id, experiment_b.id)
        else:
            flipped = self._shared_assignments(db, experiment_b.id, experiment_a.id)
            in_both = {user: (va, vb) for user, (vb, va) in flipped.items()}
            del flipped

        shared = len(in_both)
        union = size_a + size_b - shared
        overlap = shared / union if union else 0.0

        group_a = experiment_a.mutual_exclusion_group_id
        group_b = experiment_b.mutual_exclusion_group_id
        same_group = group_a is not None and group_a == group_b

        rows: List[InteractionRow] = []
        for tested, other, side in (
            (experiment_a, experiment_b, 0),
            (experiment_b, experiment_a, 1),
        ):
            rows.extend(self._rows_for(db, tested, other, side, in_both, same_group))

        if any(row.is_significant for row in rows):
            has_interaction: Optional[bool] = True
        elif rows and all(row.unavailable_reason is None for row in rows):
            has_interaction = False
        else:
            has_interaction = None

        response = InteractionPairResponse(
            experiment_a_id=str(experiment_a.id),
            experiment_b_id=str(experiment_b.id),
            shared_users=shared,
            share_of_a=shared / size_a if size_a else 0.0,
            share_of_b=shared / size_b if size_b else 0.0,
            overlap_coefficient=overlap,
            has_significant_overlap=overlap > self.OVERLAP_THRESHOLD,
            mutual_exclusion_group_id=str(group_a) if same_group else None,
            min_users_per_cell=ia.MIN_USERS_PER_CELL,
            min_expected_per_cell=ia.MIN_EXPECTED_PER_CELL,
            interaction_results=rows,
            has_interaction=has_interaction,
        )
        response.recommendations = self._pair_recommendations(
            response, experiment_a, experiment_b
        )
        return response

    def _rows_for(
        self,
        db: Session,
        tested: Experiment,
        other: Experiment,
        side: int,
        in_both: Dict[str, Tuple[str, str]],
        same_group: bool,
    ) -> List[InteractionRow]:
        """The rows of one experiment: one per treatment, or one with a reason."""
        settings = resolve_analysis_settings(tested)
        metrics = AnalysisService._ordered_metrics(tested)
        metric = metrics[0] if metrics else None
        variants = sorted(tested.variants, key=_variant_order)
        control = next((v for v in variants if v.is_control), None)
        treatments = [v for v in variants if v is not control]

        base: Dict[str, Any] = {
            "experiment_id": str(tested.id),
            "other_experiment_id": str(other.id),
            "metric_id": str(metric.id) if metric is not None else None,
            "metric_name": metric.name if metric is not None else None,
            "correction_method": settings.correction_method,
            "confidence_level": settings.confidence_level,
        }

        reason: Optional[str] = None
        if same_group:
            reason = ia.MUTUAL_EXCLUSION_GROUP
        elif not in_both:
            reason = ia.NO_SHARED_USERS
        elif metric is None:
            reason = ia.NO_METRIC
        elif _metric_type(metric) != MetricType.CONVERSION.value:
            reason = ia.NOT_A_PROPORTION_METRIC
        elif control is None:
            reason = ia.NO_CONTROL_VARIANT
        if reason is not None:
            return [InteractionRow(**base, unavailable_reason=reason)]

        converted = self._shared_converters(db, tested, metric_events(metric), in_both)

        # Cells keyed by (variant of the tested experiment, arm of the other).
        users: Dict[Tuple[str, str], int] = {}
        converters: Dict[Tuple[str, str], int] = {}
        for user, pair in in_both.items():
            cell = (pair[side], pair[1 - side])
            users[cell] = users.get(cell, 0) + 1
            if user in converted:
                converters[cell] = converters.get(cell, 0) + 1
        del converted

        arms_with_users = {arm for _, arm in users}
        arms = [
            v
            for v in sorted(other.variants, key=_variant_order)
            if str(v.id) in arms_with_users
        ]

        p_values: List[Optional[float]] = []
        built: List[Dict[str, Any]] = []
        for treatment in treatments:
            n = [
                [users.get((str(variant.id), str(arm.id)), 0) for arm in arms]
                for variant in (control, treatment)
            ]
            x = [
                [converters.get((str(variant.id), str(arm.id)), 0) for arm in arms]
                for variant in (control, treatment)
            ]
            row: Dict[str, Any] = {
                **base,
                "variant_id": str(treatment.id),
                "variant_name": treatment.name,
                "control_variant_id": str(control.id),
                "arms": [_arm(arm, n, x, j) for j, arm in zip(range(len(arms)), arms)],
            }
            row_reason = ia.floor_reason(n, x) if arms else ia.TOO_FEW_SHARED_USERS
            if row_reason is None:
                result = ia.interaction_test(n, x)
                row.update(
                    statistic=result.statistic,
                    degrees_of_freedom=result.degrees_of_freedom,
                    p_value=result.p_value,
                )
                p_values.append(result.p_value)
            else:
                row["unavailable_reason"] = row_reason
                p_values.append(None)
            built.append(row)

        corrected = adjusted_p_values(p_values, settings.correction_method)
        alpha = 1.0 - settings.confidence_level
        for row, p_value, corrected_p in zip(built, p_values, corrected):
            if p_value is not None:
                decided = corrected_p if corrected_p is not None else p_value
                row["corrected_p_value"] = corrected_p
                row["is_significant"] = bool(decided < alpha)
        return [InteractionRow(**row) for row in built]

    def _pair_recommendations(
        self,
        response: InteractionPairResponse,
        experiment_a: Experiment,
        experiment_b: Experiment,
    ) -> List[str]:
        """Plain sentences: the overlap, then one per row (or per experiment)."""
        names = {
            str(experiment_a.id): experiment_a.name,
            str(experiment_b.id): experiment_b.name,
        }
        if response.shared_users:
            recs = [
                f"The two experiments share {response.shared_users} users "
                f"({response.share_of_a:.0%} of {experiment_a.name}, "
                f"{response.share_of_b:.0%} of {experiment_b.name}). Sharing users "
                "is normal and harmless unless the experiments interact; use a "
                "mutual exclusion group only for experiments that change the same "
                "thing."
            ]
        else:
            recs = ["The two experiments share no users."]
        for row in response.interaction_results:
            name = names[row.experiment_id]
            other = names[row.other_experiment_id]
            if row.unavailable_reason is not None:
                recs.append(
                    f"{name}: the interaction was not tested"
                    + (f" for {row.variant_name}" if row.variant_name else "")
                    + f": {REASON_SENTENCES[row.unavailable_reason]}"
                )
            elif row.is_significant:
                label = "p" if row.corrected_p_value is None else "corrected p"
                decided = (
                    row.p_value
                    if row.corrected_p_value is None
                    else row.corrected_p_value
                )
                lifts = "; ".join(
                    f"{arm.other_variant_name}: {_points(arm)}" for arm in row.arms
                )
                recs.append(
                    f"{name}: the lift of {row.variant_name} on {row.metric_name} "
                    f"differs across the arms of {other} ({label} = {decided:.2g}). "
                    f"Lift in each arm of {other}: {lifts}. {name}'s result is "
                    f"still a valid average over {other}'s current split, but it "
                    f"may change once {other} ships one arm; consider deciding "
                    f"{other} first. The same pattern can also appear when one "
                    "experiment changes who enters the other."
                )
            else:
                recs.append(
                    f"{name}: no interaction found: the lift of {row.variant_name} "
                    f"on {row.metric_name} is similar across the arms of {other}. "
                    f"With {response.shared_users} shared users this test can miss "
                    "a small interaction."
                )
        return recs

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _get_experiment_users(self, experiment_id: str, db) -> Set[str]:
        """Return the set of user IDs assigned to an experiment.

        Queries the assignments table for the given experiment_id.  A failed
        query raises (#853): an empty set would read as "nobody assigned".
        """
        from backend.app.models.assignment import (
            Assignment,  # noqa: WPS433 — local import
        )

        rows = (
            db.query(Assignment.user_id)
            .filter(Assignment.experiment_id == experiment_id)
            .all()
        )
        return {str(row.user_id) for row in rows}

    def _get_active_experiment_ids(self, db) -> List[str]:
        """Return a list of IDs for all currently active experiments.

        A failed query raises (#853): an empty list would read as "no active
        experiments".
        """
        from backend.app.models.experiment import (  # noqa: WPS433
            Experiment,
            ExperimentStatus,
        )

        rows = (
            db.query(Experiment.id)
            .filter(Experiment.status == ExperimentStatus.ACTIVE)
            .all()
        )
        return [str(row.id) for row in rows]


# ---------------------------------------------------------------------------
# Helpers for the pair route
# ---------------------------------------------------------------------------


def _keep_shared(
    db: Session,
    larger_id: Any,
    chunk: Dict[str, str],
    shared: Dict[str, Tuple[str, str]],
) -> None:
    """Look ``chunk``'s users up in the larger experiment; keep those found.

    ``chunk`` maps each user to their variant in the smaller experiment, and
    is emptied.
    """
    for variant_id, user_id in _assigned_pairs(db, larger_id, list(chunk)):
        shared[user_id] = (chunk[user_id], variant_id)
    chunk.clear()


def _variant_order(variant: Any) -> Tuple[bool, str, str]:
    """Control first, then by name, then by id: a fixed order for the response."""
    return (not bool(variant.is_control), variant.name or "", str(variant.id))


def _metric_type(metric: Any) -> Any:
    """A metric's type as its stored string."""
    return getattr(metric.metric_type, "value", metric.metric_type)


def _rate(converted: int, users: int) -> Optional[float]:
    return converted / users if users else None


def _arm(arm: Any, n: List[List[int]], x: List[List[int]], j: int) -> InteractionArm:
    """One arm of the other experiment: the row's counts, rates and lift."""
    control_rate = _rate(x[0][j], n[0][j])
    treatment_rate = _rate(x[1][j], n[1][j])
    effect = (
        treatment_rate - control_rate
        if control_rate is not None and treatment_rate is not None
        else None
    )
    relative = effect / control_rate if effect is not None and control_rate else None
    return InteractionArm(
        other_variant_id=str(arm.id),
        other_variant_name=arm.name,
        n_control=n[0][j],
        converted_control=x[0][j],
        n_treatment=n[1][j],
        converted_treatment=x[1][j],
        control_rate=control_rate,
        treatment_rate=treatment_rate,
        effect=effect,
        relative_lift=relative,
    )


def _points(arm: InteractionArm) -> str:
    """``+5.0 percentage points (+50% relative)`` for one arm."""
    if arm.effect is None:
        return "not measured"
    text_ = f"{arm.effect * 100:+.1f} percentage points"
    if arm.relative_lift is not None:
        text_ += f" ({arm.relative_lift:+.0%} relative)"
    return text_
