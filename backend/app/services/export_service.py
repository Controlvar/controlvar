"""
Data export service — converts DB data to CSV/JSON for download.
EP-020: Data Export & Reporting
"""

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import RawMetric
from backend.app.schemas.export import (
    ExperimentExportRow,
    ExportFormat,
    ExportRequest,
    FeatureFlagExportRow,
    PlatformOverviewReport,
    VariantExportRow,
)
from backend.app.services.analysis_service import AnalysisService

logger = logging.getLogger(__name__)

# A CSV cell whose text begins with one of these is written with a leading
# single quote, so that a spreadsheet opening the file shows it as text.
_SPREADSHEET_LEADING_CHARS = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe(value: Any) -> Any:
    """Return ``value`` as it is written to a CSV cell.

    A string beginning with ``=``, ``+``, ``-``, ``@``, a tab or a carriage
    return gets a single quote in front of it; every other value, including
    numbers, is unchanged.
    """
    if isinstance(value, str) and value.startswith(_SPREADSHEET_LEADING_CHARS):
        return "'" + value
    return value


class ExportService:
    """
    Service for exporting platform data to CSV or JSON format.

    Handles experiments, variants, feature flags, and platform overview reports.
    Does not import FastAPI-specific code; pure service layer.
    """

    def __init__(self, db: Session) -> None:
        """Initialize with a database session."""
        self.db = db
        # get_experiment_results() output per experiment id, so that a report
        # that builds both experiment and variant rows computes it once.
        self._results: Dict[str, Optional[Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public export methods
    # ------------------------------------------------------------------

    def export_experiments(
        self,
        request: ExportRequest,
        experiment_ids: Optional[List[str]] = None,
    ) -> Tuple[str, str]:
        """
        Export experiment data.

        Args:
            request: ExportRequest specifying format, scope, and date filters.
            experiment_ids: Optional list of experiment IDs to filter by.

        Returns:
            Tuple of (content: str, content_type: str).
        """
        rows = self._build_experiment_rows(request, experiment_ids)
        if request.format == ExportFormat.CSV:
            return self._to_csv(rows, ExperimentExportRow), "text/csv"
        return (
            json.dumps([r.model_dump() for r in rows], default=str),
            "application/json",
        )

    def export_variants(
        self,
        request: ExportRequest,
        experiment_ids: Optional[List[str]] = None,
    ) -> Tuple[str, str]:
        """
        Export per-variant results.

        Args:
            request: ExportRequest specifying format and optional date filters.
            experiment_ids: Optional list of experiment IDs to filter by.

        Returns:
            Tuple of (content: str, content_type: str).
        """
        rows = self._build_variant_rows(request, experiment_ids)
        if request.format == ExportFormat.CSV:
            return self._to_csv(rows, VariantExportRow), "text/csv"
        return (
            json.dumps([r.model_dump() for r in rows], default=str),
            "application/json",
        )

    def export_feature_flags(
        self,
        request: ExportRequest,
    ) -> Tuple[str, str]:
        """
        Export feature flag usage data.

        Args:
            request: ExportRequest specifying format and optional date filters.

        Returns:
            Tuple of (content: str, content_type: str).
        """
        rows = self._build_feature_flag_rows(request)
        if request.format == ExportFormat.CSV:
            return self._to_csv(rows, FeatureFlagExportRow), "text/csv"
        return (
            json.dumps([r.model_dump() for r in rows], default=str),
            "application/json",
        )

    def experiment_report_rows(
        self, experiment_id: Union[str, UUID]
    ) -> Optional[Tuple[List[ExperimentExportRow], List[VariantExportRow]]]:
        """
        The experiment and variant rows of one experiment's report.

        Returns:
            ``(experiment_rows, variant_rows)``, or None when no experiment
            has this id.
        """
        experiment = (
            self.db.query(Experiment).filter(Experiment.id == experiment_id).first()
        )
        if experiment is None:
            return None
        return (
            self._experiments_to_rows([experiment]),
            self._variants_to_rows([experiment]),
        )

    def variants_to_csv(self, rows: List[VariantExportRow]) -> str:
        """CSV of variant rows, with the same columns as ``/export/variants``."""
        return self._to_csv(rows, VariantExportRow)

    def generate_platform_overview(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> PlatformOverviewReport:
        """
        Generate platform-wide summary report.

        Args:
            start_date: Optional inclusive start of the reporting period.
            end_date: Optional inclusive end of the reporting period.

        Returns:
            PlatformOverviewReport with aggregate counts.
        """
        # Base experiment counts
        exp_query = self.db.query(Experiment)
        if start_date:
            exp_query = exp_query.filter(Experiment.created_at >= start_date)
        if end_date:
            exp_query = exp_query.filter(Experiment.created_at <= end_date)

        total_experiments: int = exp_query.count()

        active_experiments: int = exp_query.filter(
            Experiment.status == ExperimentStatus.ACTIVE
        ).count()
        completed_experiments: int = exp_query.filter(
            Experiment.status == ExperimentStatus.COMPLETED
        ).count()

        # Feature flag counts
        ff_query = self.db.query(FeatureFlag)
        if start_date:
            ff_query = ff_query.filter(FeatureFlag.created_at >= start_date)
        if end_date:
            ff_query = ff_query.filter(FeatureFlag.created_at <= end_date)

        total_feature_flags: int = ff_query.count()
        active_feature_flags: int = ff_query.filter(
            FeatureFlag.status == FeatureFlagStatus.ACTIVE
        ).count()

        # Assignment and event counts via relationships — use scalar counts where available
        # These are approximated from what the DB can provide without raw-event tables
        total_assignments: int = self._count_assignments(start_date, end_date)
        total_events: int = self._count_events(start_date, end_date)

        # Experiments with a winner are those whose summary JSON contains a winner
        experiments_with_winners: int = self._count_experiments_with_winners(
            start_date, end_date
        )

        # Average duration for completed experiments
        average_duration: Optional[float] = self._average_experiment_duration(
            start_date, end_date
        )

        generated_at = datetime.now(tz=timezone.utc).isoformat()

        return PlatformOverviewReport(
            generated_at=generated_at,
            period_start=start_date.isoformat() if start_date else None,
            period_end=end_date.isoformat() if end_date else None,
            total_experiments=total_experiments,
            active_experiments=active_experiments,
            completed_experiments=completed_experiments,
            total_feature_flags=total_feature_flags,
            active_feature_flags=active_feature_flags,
            total_assignments=total_assignments,
            total_events=total_events,
            experiments_with_winners=experiments_with_winners,
            average_experiment_duration_days=average_duration,
        )

    # ------------------------------------------------------------------
    # Internal row builders
    # ------------------------------------------------------------------

    def _build_experiment_rows(
        self,
        request: ExportRequest,
        experiment_ids: Optional[List[str]],
    ) -> List[ExperimentExportRow]:
        """Query experiments and map to ExperimentExportRow instances."""
        query = self.db.query(Experiment)
        if experiment_ids:
            query = query.filter(Experiment.id.in_(experiment_ids))
        if request.start_date:
            query = query.filter(Experiment.created_at >= request.start_date)
        if request.end_date:
            query = query.filter(Experiment.created_at <= request.end_date)
        experiments = query.all()
        return self._experiments_to_rows(experiments)

    def _build_variant_rows(
        self,
        request: ExportRequest,
        experiment_ids: Optional[List[str]],
    ) -> List[VariantExportRow]:
        """Query experiments+variants and map to VariantExportRow instances."""
        query = self.db.query(Experiment)
        if experiment_ids:
            query = query.filter(Experiment.id.in_(experiment_ids))
        if request.start_date:
            query = query.filter(Experiment.created_at >= request.start_date)
        if request.end_date:
            query = query.filter(Experiment.created_at <= request.end_date)
        experiments = query.all()
        return self._variants_to_rows(experiments)

    def _build_feature_flag_rows(
        self,
        request: ExportRequest,
    ) -> List[FeatureFlagExportRow]:
        """Query feature flags and map to FeatureFlagExportRow instances."""
        query = self.db.query(FeatureFlag)
        if request.start_date:
            query = query.filter(FeatureFlag.created_at >= request.start_date)
        if request.end_date:
            query = query.filter(FeatureFlag.created_at <= request.end_date)
        flags = query.all()
        evaluations = self._count_by(RawMetric.feature_flag_id, [ff.id for ff in flags])
        return [
            self._feature_flag_to_row(ff, evaluations.get(ff.id, 0)) for ff in flags
        ]

    # ------------------------------------------------------------------
    # Results and counts shared by the row builders
    # ------------------------------------------------------------------

    def _experiments_to_rows(
        self, experiments: Sequence[Experiment]
    ) -> List[ExperimentExportRow]:
        """Experiment rows, with the assignment and event totals counted in SQL."""
        ids = [exp.id for exp in experiments]
        assignments = self._count_by(Assignment.experiment_id, ids)
        events = self._count_by(Event.experiment_id, ids)
        return [
            self._experiment_to_row(
                exp,
                total_assignments=assignments.get(exp.id, 0),
                total_events=events.get(exp.id, 0),
                results=self._results_for(exp),
            )
            for exp in experiments
        ]

    def _variants_to_rows(
        self, experiments: Sequence[Experiment]
    ) -> List[VariantExportRow]:
        """
        One row per variant, with the primary metric's numbers from the results API.

        Each value is read from ``AnalysisService.get_experiment_results`` --
        the computation behind ``GET /api/v1/results/{id}`` -- so the export
        and the results API agree. An experiment whose results cannot be
        computed (it has no control variant, for example) keeps its variant
        rows with the result columns empty.  ``conversion_rate`` is empty too
        when the primary metric is analysed as a mean (a revenue metric): its
        ``mean`` is a value per user, not a rate.
        """
        rows: List[VariantExportRow] = []
        for exp in experiments:
            primary = self._primary_metric(self._results_for(exp))
            by_variant: Dict[str, Dict[str, Any]] = (
                {str(v["variant_id"]): v for v in primary["variants"]}
                if primary
                else {}
            )
            is_rate = (primary or {}).get("analysis", "proportion") == "proportion"
            for variant in getattr(exp, "variants", []):
                result = by_variant.get(str(variant.id))
                rows.append(
                    VariantExportRow(
                        experiment_id=str(exp.id),
                        experiment_name=str(exp.name),
                        variant_id=str(variant.id),
                        variant_name=str(variant.name),
                        is_control=bool(getattr(variant, "is_control", False)),
                        assignments=result["sample_size"] if result else None,
                        conversions=result["conversions"] if result else None,
                        conversion_rate=result["mean"] if result and is_rate else None,
                        p_value=result["p_value"] if result else None,
                        is_significant=result["is_significant"] if result else None,
                        relative_improvement_pct=(
                            result["relative_improvement_pct"] if result else None
                        ),
                    )
                )
        return rows

    def _results_for(self, exp: Experiment) -> Optional[Dict[str, Any]]:
        """``get_experiment_results`` for one experiment, or None if it has none."""
        key = str(exp.id)
        if key not in self._results:
            try:
                self._results[key] = AnalysisService(self.db).get_experiment_results(
                    exp.id, include_bayesian=False
                )
            except ValueError as exc:
                logger.info("Export: no results for experiment %s: %s", key, exc)
                self._results[key] = None
        return self._results[key]

    @staticmethod
    def _primary_metric(
        results: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """The primary metric's result, chosen as the results summary chooses it."""
        metrics = (results or {}).get("metrics") or []
        if not metrics:
            return None
        return next((m for m in metrics if m.get("is_primary")), metrics[0])

    def _count_by(self, column: Any, ids: Sequence[Any]) -> Dict[Any, int]:
        """``{id: row count}`` for the rows whose ``column`` is in ``ids``, in one query."""
        if not ids:
            return {}
        rows = (
            self.db.query(column, func.count())
            .filter(column.in_(ids))
            .group_by(column)
            .all()
        )
        return {key: int(count) for key, count in rows}

    # ------------------------------------------------------------------
    # Internal model-to-row mappers
    # ------------------------------------------------------------------

    def _experiment_to_row(
        self,
        exp: Experiment,
        total_assignments: int = 0,
        total_events: int = 0,
        results: Optional[Dict[str, Any]] = None,
    ) -> ExperimentExportRow:
        """
        Map an Experiment model instance to ExperimentExportRow.

        ``total_assignments`` and ``total_events`` are counted by the caller;
        ``results`` is the experiment's ``get_experiment_results`` output (None
        leaves the winner and recommendation empty).
        """
        # Resolve status value — handle both enum and plain string
        status_val: str
        if hasattr(exp.status, "value"):
            status_val = exp.status.value
        else:
            status_val = str(exp.status)

        # Resolve experiment_type value
        type_val: str
        if hasattr(exp.experiment_type, "value"):
            type_val = exp.experiment_type.value
        else:
            type_val = str(exp.experiment_type)

        start_str: Optional[str] = (
            exp.start_date.isoformat() if exp.start_date else None
        )
        end_str: Optional[str] = exp.end_date.isoformat() if exp.end_date else None

        duration: Optional[float] = None
        if exp.start_date and exp.end_date:
            delta = exp.end_date - exp.start_date
            duration = round(delta.total_seconds() / 86400, 2)

        summary = (results or {}).get("summary") or {}
        winner_id = summary.get("winning_variant_id")
        winner_name: Optional[str] = None
        if winner_id:
            winner_name = next(
                (
                    str(v.name)
                    for v in getattr(exp, "variants", [])
                    if str(v.id) == str(winner_id)
                ),
                None,
            )

        return ExperimentExportRow(
            experiment_id=str(exp.id),
            experiment_name=str(exp.name),
            status=status_val,
            experiment_type=type_val,
            start_date=start_str,
            end_date=end_str,
            duration_days=duration,
            total_assignments=total_assignments,
            total_events=total_events,
            winner_variant=winner_name,
            recommendation=summary.get("recommendation"),
        )

    def _feature_flag_to_row(
        self, ff: FeatureFlag, total_evaluations: int = 0
    ) -> FeatureFlagExportRow:
        """
        Map a FeatureFlag model instance to FeatureFlagExportRow.

        ``total_evaluations`` is the flag's ``raw_metrics`` row count, counted
        by the caller.
        """
        status_val: str
        if hasattr(ff.status, "value"):
            status_val = ff.status.value
        else:
            status_val = str(ff.status)

        total_evals: int = total_evaluations
        # No raw_metrics column records whether an evaluation returned
        # enabled, so this has always been 0; it is kept as it was.
        enabled_evals: int = 0

        enabled_rate: float = (
            round(enabled_evals / total_evals, 4) if total_evals > 0 else 0.0
        )

        created_str: str = (
            ff.created_at.isoformat()
            if ff.created_at
            else datetime.now(tz=timezone.utc).isoformat()
        )
        updated_str: str = (
            ff.updated_at.isoformat()
            if ff.updated_at
            else datetime.now(tz=timezone.utc).isoformat()
        )

        return FeatureFlagExportRow(
            flag_id=str(ff.id),
            flag_key=str(ff.key),
            flag_name=str(ff.name),
            status=status_val,
            rollout_percentage=int(ff.rollout_percentage),
            total_evaluations=total_evals,
            enabled_evaluations=enabled_evals,
            enabled_rate=enabled_rate,
            created_at=created_str,
            updated_at=updated_str,
        )

    # ------------------------------------------------------------------
    # Generic CSV serializer
    # ------------------------------------------------------------------

    def _to_csv(self, rows: list, schema_class: type) -> str:
        """
        Generic CSV serializer using schema field names as headers.

        Every text cell is written spreadsheet-safe (see ``spreadsheet_safe``).

        Args:
            rows: List of Pydantic model instances.
            schema_class: The Pydantic model class (used for headers).

        Returns:
            CSV string, or empty string if rows is empty.
        """
        if not rows:
            return ""
        output = io.StringIO()
        fields = list(schema_class.model_fields.keys())
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: spreadsheet_safe(value)
                    for key, value in row.model_dump().items()
                }
            )
        return output.getvalue()

    # ------------------------------------------------------------------
    # Private aggregate helpers
    # ------------------------------------------------------------------

    def _count_assignments(
        self,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> int:
        """Count total assignments, optionally filtered by date range."""
        try:
            from backend.app.models.assignment import Assignment

            query = self.db.query(Assignment)
            if start_date:
                query = query.filter(Assignment.created_at >= start_date)
            if end_date:
                query = query.filter(Assignment.created_at <= end_date)
            return int(query.count())
        except Exception:
            return 0

    def _count_events(
        self,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> int:
        """Count total events, optionally filtered by date range."""
        try:
            from backend.app.models.event import Event

            query = self.db.query(Event)
            if start_date:
                query = query.filter(Event.created_at >= start_date)
            if end_date:
                query = query.filter(Event.created_at <= end_date)
            return int(query.count())
        except Exception:
            return 0

    def _count_experiments_with_winners(
        self,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> int:
        """
        Count experiments whose reports indicate a winner exists.

        Falls back to 0 when the Report model or relationship is unavailable.
        """
        try:
            from backend.app.models.report import Report

            query = self.db.query(Report)
            if start_date:
                query = query.filter(Report.created_at >= start_date)
            if end_date:
                query = query.filter(Report.created_at <= end_date)
            # Reports that have a winner recorded in their summary JSON
            query = query.filter(Report.summary.isnot(None))
            return int(query.count())
        except Exception:
            return 0

    def _average_experiment_duration(
        self,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> Optional[float]:
        """
        Compute average duration (days) of completed experiments that have both
        start_date and end_date set.
        """
        try:
            query = (
                self.db.query(Experiment)
                .filter(Experiment.status == ExperimentStatus.COMPLETED)
                .filter(Experiment.start_date.isnot(None))
                .filter(Experiment.end_date.isnot(None))
            )
            if start_date:
                query = query.filter(Experiment.created_at >= start_date)
            if end_date:
                query = query.filter(Experiment.created_at <= end_date)
            experiments = query.all()
            if not experiments:
                return None
            durations = [
                (exp.end_date - exp.start_date).total_seconds() / 86400
                for exp in experiments
                if exp.end_date and exp.start_date
            ]
            if not durations:
                return None
            return round(sum(durations) / len(durations), 2)
        except Exception:
            return None
