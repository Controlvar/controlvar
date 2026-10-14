"""
Safety monitoring service for feature flags.

Responsibilities:

* global safety settings (``safety_settings`` table, a single row),
* per-flag safety configuration (``feature_flag_safety_configs``),
* evaluating the configured metric thresholds against the metrics the
  platform actually records (``raw_metrics`` / ``error_logs``),
* rolling a flag back -- a target of 0 turns it off, any other target lowers
  its global rollout percentage -- pausing its active rollout schedules and
  recording it in ``safety_rollback_records`` (#629).

The async methods are the API used by the endpoints and the
``SafetyScheduler``; the static ``*_record`` helpers are the thin CRUD layer
underneath them and are also handy in tests.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.core.logger import failure_detail
from backend.app.core.logging import get_logger
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
    flag_status_name,
)
from backend.app.models.metrics.metric import (
    ErrorLog,
    MetricType,
    RawMetric,
    counted_error_filter,
    uncounted_error_filter,
)
from backend.app.models.rollout_schedule import RolloutSchedule, RolloutScheduleStatus
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    RollbackTriggerType,
    SafetyRollbackRecord,
    SafetySettings,
)
from backend.app.models.user import User
from backend.app.schemas.safety import (
    FeatureFlagSafetyConfigCreate,
    FeatureFlagSafetyConfigResponse,
    FeatureFlagSafetyConfigUpdate,
    MetricStatus,
    MetricThreshold,
    RollbackResponse,
    SafetyCheckResponse,
    SafetySettingsCreate,
    SafetySettingsResponse,
    SafetySettingsUpdate,
)
from backend.app.services.audit_service import SYSTEM_SAFETY_MONITOR, AuditService
from backend.app.services.feature_flag_service import FlagVerb, transition

logger = get_logger(__name__)

# Placeholder id returned when a flag has no stored safety configuration and
# the defaults from the global settings are used instead.
DEFAULT_CONFIG_ID = UUID("00000000-0000-0000-0000-000000000000")

#: Most error types listed in ``get_error_metrics()["error_types"]``.
ERROR_TYPE_BREAKDOWN_LIMIT = 100

# Metric names understood by check_feature_flag_safety(). Anything else is
# reported as "no data source" and treated as healthy.
_ERROR_METRICS = {"error_rate", "error_count", "total_evaluations"}
_LATENCY_METRICS = {
    "latency",
    "avg_latency",
    "p95_latency",
    "max_latency",
    "min_latency",
}


#: What a rollback does to a flag: turn it off, or lower its global rollout.
RollbackChange = Literal["deactivate", "lower"]


def rollback_change(flag: Any, target: int) -> Optional[RollbackChange]:
    """What a rollback to *target* percent would do to *flag*, or ``None``.

    The one rule the rollback itself, the safety monitor and
    ``should_rollback`` share (#629):

    * a flag that is not ACTIVE (INACTIVE or ARCHIVED, enum or string) --
      ``None``: it already serves no one, and an archived flag is never
      written (#631);
    * a target of 0 -- ``"deactivate"``, whatever the global percentage, so a
      flag that serves only users matched by a targeting rule (global 0%) can
      still be rolled back;
    * a target above 0 -- ``"lower"`` when the global percentage is above it,
      otherwise ``None``.
    """
    if flag_status_name(flag.status) != FeatureFlagStatus.ACTIVE.value:
        return None
    if target == 0:
        return "deactivate"
    if (flag.rollout_percentage or 0) > target:
        return "lower"
    return None


def _clamped_percentage(value: Any) -> int:
    """*value* as a percentage within 0-100; anything not a number is 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(100, int(value)))


def _no_rollback_message(flag: Any, target: int) -> str:
    """Why a rollback to *target* changes nothing, naming the reason."""
    status_name = flag_status_name(flag.status)
    if status_name == FeatureFlagStatus.ARCHIVED.value:
        return f"Feature flag '{flag.key}' is archived; nothing to roll back"
    if status_name != FeatureFlagStatus.ACTIVE.value:
        return f"Feature flag '{flag.key}' is already off; nothing to roll back"
    return (
        f"Feature flag '{flag.key}' is already at {flag.rollout_percentage}% "
        f"(target {target}%); no rollback needed"
    )


def _metric_to_trigger(metric_name: str) -> RollbackTriggerType:
    if metric_name in _ERROR_METRICS:
        return RollbackTriggerType.ERROR_RATE
    if metric_name in _LATENCY_METRICS:
        return RollbackTriggerType.LATENCY
    return RollbackTriggerType.CUSTOM_METRIC


def _as_threshold(value: Any) -> MetricThreshold:
    """Config metrics are stored as JSON; accept dicts or MetricThreshold objects."""
    if isinstance(value, MetricThreshold):
        return value
    if isinstance(value, dict):
        return MetricThreshold(**value)
    return MetricThreshold()


def _breaches(value: float, limit: Optional[float], comparison: str) -> bool:
    if limit is None:
        return False
    if comparison == "less_than":
        return value < limit
    if comparison == "equal_to":
        return value == limit
    return value > limit  # "greater_than" (default)


def format_metric_value(value: float) -> str:
    """A metric value as a person would write it in a rollback reason.

    Four significant figures, never in exponent form: 1/14 reads ``0.07143``,
    0.1 reads ``0.1``, a 30000.12 ms latency reads ``30000`` (``.4g`` alone
    would print ``3e+04``). The raw value stays in the safety check response.
    """
    return format(Decimal(f"{value:.4g}"), "f")


def format_metric_reading(name: str, value: float) -> str:
    """A metric value with its unit, as an alert shows it.

    ``error_rate`` is a fraction and reads as a percentage (1/14 reads
    ``7.143%``, 0.05 reads ``5%``); the latency metrics are milliseconds
    (``523.5 ms``); anything else is a plain number. The number itself is
    ``format_metric_value``'s, so it matches the rollback reason.
    """
    if name == "error_rate":
        return f"{format_metric_value(value * 100)}%"
    if name in _LATENCY_METRICS:
        return f"{format_metric_value(value)} ms"
    return format_metric_value(value)


# comparison_type -> (the word a reason uses, the symbol between value and
# threshold). Anything else reads as "greater_than", as ``_breaches`` does.
_COMPARISON_WORDS = {
    "greater_than": ("above", ">"),
    "less_than": ("below", "<"),
    "equal_to": ("equal to", "="),
}


def breach_comparison(status: Any) -> Optional[str]:
    """The ``comparison_type`` a ``MetricStatus`` carries in its ``details``.

    None when it carries none (or something that is not a string); the wording
    helpers read None as ``greater_than``.
    """
    details = getattr(status, "details", None)
    comparison = details.get("comparison_type") if isinstance(details, dict) else None
    return comparison if isinstance(comparison, str) else None


def breach_words(comparison_type: Optional[str]) -> Tuple[str, str]:
    """The word and the symbol for a ``comparison_type``.

    ``greater_than`` is (``above``, ``>``), ``less_than`` (``below``, ``<``),
    ``equal_to`` (``equal to``, ``=``). None and anything unknown read as
    ``greater_than``, as ``_breaches`` does.
    """
    return _COMPARISON_WORDS.get(comparison_type, _COMPARISON_WORDS["greater_than"])


def describe_breach(status: Any) -> str:
    """How a failing metric breached its threshold, for a rollback reason.

    Worded by the threshold's ``comparison_type`` (``details`` of the
    ``MetricStatus``, ``greater_than`` when it is absent) and written in the
    metric's unit (``format_metric_reading``): 1 error in 14 evaluations
    against ``0.05`` reads ``above threshold (7.143% > 5%)``; 40
    ``total_evaluations`` against a ``less_than`` floor of 100 reads
    ``below threshold (40 < 100)``.

    ``SafetyService.should_rollback`` and the safety scheduler both build
    their reason from this, and the rollback alert takes its word from
    ``breach_words``, so none of them can word the same breach differently.
    """
    word, symbol = breach_words(breach_comparison(status))
    value = format_metric_reading(status.name, status.current_value)
    limit = format_metric_reading(status.name, status.threshold)
    return f"{word} threshold ({value} {symbol} {limit})"


class SafetyService:
    """Service for safety monitoring and rollback functionality."""

    def __init__(self, db: Session):
        """Initialize the safety service with a database session."""
        self.db = db

    # ------------------------------------------------------------------
    # Record-level helpers (sync)
    # ------------------------------------------------------------------

    @staticmethod
    def get_safety_settings_record(db: Session) -> Optional[SafetySettings]:
        """Return the global settings row, or None if it has not been created."""
        return db.query(SafetySettings).first()

    @staticmethod
    def create_safety_settings(
        db: Session, data: SafetySettingsCreate
    ) -> SafetySettings:
        """Create the global settings row; raises ValueError if one exists."""
        if db.query(SafetySettings).first():
            raise ValueError("Safety settings already exist, use update instead")

        db_obj = SafetySettings(**_settings_columns(data.model_dump()))
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    @staticmethod
    def update_safety_settings(
        db: Session, data: SafetySettingsUpdate
    ) -> Optional[SafetySettings]:
        """Update the global settings row; returns None if it does not exist."""
        db_obj = db.query(SafetySettings).first()
        if not db_obj:
            return None

        for field, value in _settings_columns(
            data.model_dump(exclude_unset=True)
        ).items():
            setattr(db_obj, field, value)

        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    @staticmethod
    def get_feature_flag_safety_config_record(
        db: Session, feature_flag_id: UUID
    ) -> Optional[FeatureFlagSafetyConfig]:
        """Return the stored config for a flag, or None."""
        return (
            db.query(FeatureFlagSafetyConfig)
            .filter(FeatureFlagSafetyConfig.feature_flag_id == feature_flag_id)
            .first()
        )

    @staticmethod
    def create_feature_flag_safety_config(
        db: Session,
        data: FeatureFlagSafetyConfigCreate,
        feature_flag_id: Optional[UUID] = None,
    ) -> FeatureFlagSafetyConfig:
        """
        Create the safety config for a flag.

        ``feature_flag_id`` may be passed explicitly or carried by ``data``
        (``FeatureFlagSafetyConfigBase`` has the field, the create schema does
        not). Raises ValueError when the flag does not exist or already has a
        config.
        """
        flag_id = feature_flag_id or getattr(data, "feature_flag_id", None)
        if flag_id is None:
            raise ValueError("feature_flag_id is required to create a safety config")

        if SafetyService.get_feature_flag_safety_config_record(db, flag_id):
            raise ValueError(f"Safety config already exists for feature flag {flag_id}")

        if not db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first():
            raise ValueError(f"Feature flag {flag_id} does not exist")

        payload = _config_columns(data.model_dump())
        payload.pop("feature_flag_id", None)
        db_obj = FeatureFlagSafetyConfig(feature_flag_id=flag_id, **payload)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    @staticmethod
    def update_feature_flag_safety_config(
        db: Session, feature_flag_id: UUID, data: FeatureFlagSafetyConfigUpdate
    ) -> Optional[FeatureFlagSafetyConfig]:
        """Update a flag's safety config; returns None if there is none."""
        db_obj = SafetyService.get_feature_flag_safety_config_record(
            db, feature_flag_id
        )
        if not db_obj:
            return None

        for field, value in _config_columns(
            data.model_dump(exclude_unset=True)
        ).items():
            setattr(db_obj, field, value)

        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_error_metrics(
        self, db: Session, feature_flag_id: UUID, timeframe_minutes: int = 15
    ) -> Dict[str, Any]:
        """Error counts and error rate for a flag over the last ``timeframe_minutes``.

        This is the only source of the safety monitor's error numbers (the
        scheduler, ``should_rollback`` and ``GET .../check`` all read it).

        Which rows count: every ``error_logs`` row of the flag in the window
        except error reports sent with a key that is not a server-side key,
        i.e. rows whose ``request_data->>'source'`` is ``'client'``
        (``REPORT_SOURCE_CLIENT``). The rule is ``counted_error_filter()``,
        which the metrics summary uses too; it is ``IS DISTINCT FROM``, so rows
        with no ``source`` count: flag evaluations that failed on the server
        (``{"context": ...}``) and rows with no ``request_data``.
        ``uncounted_error_count`` is the number of ``'client'`` rows
        (``uncounted_error_filter()``): stored, and shown on the check, but
        not counted.

        ``error_count``, ``error_rate`` and ``error_types`` cover the counted
        rows; ``total_evaluations`` is unchanged.

        The counts and the per-type breakdown are computed by the database
        (``COUNT ... FILTER`` and ``COUNT ... GROUP BY error_type``, filtered on
        ``feature_flag_id`` and ``timestamp``, the columns of the composite
        ``error_logs`` index); no ``ErrorLog`` row is loaded.
        ``error_types`` lists at most ``ERROR_TYPE_BREAKDOWN_LIMIT`` types, the
        most frequent first; ``error_count`` is never limited by it.
        """
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(minutes=timeframe_minutes)

        if not db.query(FeatureFlag).filter(FeatureFlag.id == feature_flag_id).first():
            raise ValueError(f"Feature flag {feature_flag_id} does not exist")

        in_window = (
            ErrorLog.feature_flag_id == feature_flag_id,
            ErrorLog.timestamp.between(start_time, end_time),
        )
        counted = counted_error_filter()
        uncounted = uncounted_error_filter()
        error_count, uncounted_error_count = (
            int(n or 0)
            for n in db.query(
                func.count(ErrorLog.id).filter(counted),
                func.count(ErrorLog.id).filter(uncounted),
            )
            .filter(*in_window)
            .one()
        )

        per_type = func.count(ErrorLog.id)
        error_types: Dict[str, int] = {}
        if error_count:
            error_types = {
                error_type: int(count)
                for error_type, count in db.query(ErrorLog.error_type, per_type)
                .filter(*in_window, counted)
                .group_by(ErrorLog.error_type)
                .order_by(per_type.desc(), ErrorLog.error_type)
                .limit(ERROR_TYPE_BREAKDOWN_LIMIT)
            }

        total_evaluations = (
            db.query(func.coalesce(func.sum(RawMetric.count), 0))
            .filter(
                RawMetric.feature_flag_id == feature_flag_id,
                RawMetric.metric_type == MetricType.FLAG_EVALUATION.value,
                RawMetric.timestamp.between(start_time, end_time),
            )
            .scalar()
            or 0
        )

        error_rate = 0.0 if total_evaluations == 0 else error_count / total_evaluations

        return {
            "error_count": error_count,
            "uncounted_error_count": uncounted_error_count,
            "total_evaluations": int(total_evaluations),
            "error_rate": error_rate,
            "error_types": error_types,
            "timeframe_minutes": timeframe_minutes,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
        }

    def get_latency_metrics(
        self, db: Session, feature_flag_id: UUID, timeframe_minutes: int = 15
    ) -> Dict[str, Any]:
        """Latency statistics (ms) for a flag over the last ``timeframe_minutes``."""
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(minutes=timeframe_minutes)

        if not db.query(FeatureFlag).filter(FeatureFlag.id == feature_flag_id).first():
            raise ValueError(f"Feature flag {feature_flag_id} does not exist")

        rows = (
            db.query(RawMetric.value)
            .filter(
                RawMetric.feature_flag_id == feature_flag_id,
                RawMetric.metric_type == MetricType.LATENCY.value,
                RawMetric.value.isnot(None),
                RawMetric.timestamp.between(start_time, end_time),
            )
            .all()
        )
        latencies = sorted(float(v) for (v,) in rows)

        stats: Dict[str, Any] = {
            "avg_latency": 0,
            "max_latency": 0,
            "min_latency": 0,
            "p95_latency": 0,
            "total_requests": len(latencies),
            "timeframe_minutes": timeframe_minutes,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
        }
        if latencies:
            p95_index = min(int(len(latencies) * 0.95), len(latencies) - 1)
            stats.update(
                avg_latency=sum(latencies) / len(latencies),
                max_latency=latencies[-1],
                min_latency=latencies[0],
                p95_latency=latencies[p95_index],
            )
        return stats

    def _error_window(self, feature_flag_id: UUID) -> Dict[str, Any]:
        """The error and evaluation counts behind ``error_rate``, for the details.

        ``error_rate`` reads 0.0 when a flag has errors but no recorded
        evaluations (nothing to divide by), which is indistinguishable from a
        healthy flag without these counts; the dashboard shows that state.
        ``uncounted_error_count`` is the number of error reports in the window
        that were stored but do not count (sent without a server-side key);
        the dashboard shows it too.
        """
        metrics = self.get_error_metrics(self.db, feature_flag_id)
        return {
            "error_count": metrics["error_count"],
            "uncounted_error_count": metrics["uncounted_error_count"],
            "total_evaluations": metrics["total_evaluations"],
            "timeframe_minutes": metrics["timeframe_minutes"],
        }

    def _get_metric_value(
        self, feature_flag_id: UUID, metric_name: str
    ) -> Optional[float]:
        """Current value of a named metric, or None when the platform has no source for it."""
        if metric_name in _ERROR_METRICS:
            return float(self.get_error_metrics(self.db, feature_flag_id)[metric_name])
        if metric_name in _LATENCY_METRICS:
            latency = self.get_latency_metrics(self.db, feature_flag_id)
            key = "avg_latency" if metric_name == "latency" else metric_name
            return float(latency[key])
        return None

    # ------------------------------------------------------------------
    # Global settings (async API)
    # ------------------------------------------------------------------

    async def get_safety_settings(self) -> SafetySettingsResponse:
        """Return the global settings, creating the default row on first use."""
        settings = self.db.query(SafetySettings).first()
        if not settings:
            settings = SafetySettings(
                enable_automatic_rollbacks=False, default_metrics=None
            )
            self.db.add(settings)
            self.db.commit()
            self.db.refresh(settings)
        return SafetySettingsResponse.model_validate(settings)

    async_get_safety_settings = get_safety_settings

    async def create_or_update_safety_settings(
        self, settings: SafetySettingsCreate
    ) -> SafetySettingsResponse:
        """Create or update the global settings."""
        existing = self.db.query(SafetySettings).first()
        payload = _settings_columns(settings.model_dump(exclude_unset=True))

        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
        else:
            existing = SafetySettings(**_settings_columns(settings.model_dump()))
            self.db.add(existing)

        self.db.commit()
        self.db.refresh(existing)
        return SafetySettingsResponse.model_validate(existing)

    # ------------------------------------------------------------------
    # Per-flag configuration (async API)
    # ------------------------------------------------------------------

    def _require_flag(self, feature_flag_id: UUID) -> FeatureFlag:
        feature_flag = (
            self.db.query(FeatureFlag).filter(FeatureFlag.id == feature_flag_id).first()
        )
        if not feature_flag:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Feature flag with ID {feature_flag_id} not found",
            )
        return feature_flag

    async def get_feature_flag_safety_config(
        self, feature_flag_id: UUID
    ) -> FeatureFlagSafetyConfigResponse:
        """
        Return the flag's safety config.

        When no config has been stored, a default built from the global
        settings is returned with ``DEFAULT_CONFIG_ID`` as its id (nothing is
        written).
        """
        self._require_flag(feature_flag_id)

        config = self.get_feature_flag_safety_config_record(self.db, feature_flag_id)
        if config:
            return FeatureFlagSafetyConfigResponse.model_validate(config)

        settings = await self.get_safety_settings()
        now = datetime.utcnow()
        return FeatureFlagSafetyConfigResponse(
            id=DEFAULT_CONFIG_ID,
            feature_flag_id=feature_flag_id,
            enabled=True,
            metrics=settings.default_metrics or {},
            rollback_percentage=0,
            created_at=now,
            updated_at=now,
        )

    async_get_feature_flag_safety_config = get_feature_flag_safety_config

    async def create_or_update_feature_flag_safety_config(
        self, feature_flag_id: UUID, config: FeatureFlagSafetyConfigCreate
    ) -> FeatureFlagSafetyConfigResponse:
        """Create or update the flag's safety config."""
        self._require_flag(feature_flag_id)

        existing = self.get_feature_flag_safety_config_record(self.db, feature_flag_id)
        if existing:
            for key, value in _config_columns(
                config.model_dump(exclude_unset=True)
            ).items():
                setattr(existing, key, value)
        else:
            # Only the fields sent: one left out takes its column default. A
            # full dump would store a JSON null for an unsent ``metrics`` (#954).
            payload = _config_columns(config.model_dump(exclude_unset=True))
            payload.pop("feature_flag_id", None)
            existing = FeatureFlagSafetyConfig(
                feature_flag_id=feature_flag_id, **payload
            )
            self.db.add(existing)

        self.db.commit()
        self.db.refresh(existing)
        return FeatureFlagSafetyConfigResponse.model_validate(existing)

    # ------------------------------------------------------------------
    # Safety checks
    # ------------------------------------------------------------------

    async def check_feature_flag_safety(
        self, feature_flag_id: UUID
    ) -> SafetyCheckResponse:
        """
        Evaluate every configured metric threshold for the flag.

        A metric is unhealthy when its current value breaches the critical
        threshold (per ``comparison_type``). Metrics the platform cannot
        measure are reported with ``current_value`` 0 and noted in details.
        """
        feature_flag = self._require_flag(feature_flag_id)
        config = await self.get_feature_flag_safety_config(feature_flag_id)
        window = self._error_window(feature_flag_id)

        if not config.enabled:
            return SafetyCheckResponse(
                feature_flag_id=feature_flag_id,
                is_healthy=True,
                metrics=[],
                last_checked=datetime.utcnow(),
                details={
                    "message": "Safety monitoring is disabled for this feature flag",
                    **window,
                },
            )

        statuses: List[MetricStatus] = []
        unavailable: List[str] = []
        is_healthy = True

        for name, raw_threshold in (config.metrics or {}).items():
            threshold = _as_threshold(raw_threshold)
            value = self._get_metric_value(feature_flag_id, name)

            if value is None:
                unavailable.append(name)
                value = 0.0
                critical = warning = False
            else:
                critical = _breaches(
                    value, threshold.critical_threshold, threshold.comparison_type
                )
                warning = _breaches(
                    value, threshold.warning_threshold, threshold.comparison_type
                )

            if critical:
                is_healthy = False

            limit = (
                threshold.critical_threshold
                if threshold.critical_threshold is not None
                else (threshold.warning_threshold or 0.0)
            )
            statuses.append(
                MetricStatus(
                    name=name,
                    current_value=value,
                    threshold=limit,
                    is_healthy=not critical,
                    details={
                        "comparison_type": threshold.comparison_type,
                        "warning_threshold": threshold.warning_threshold,
                        "critical_threshold": threshold.critical_threshold,
                        "warning": warning,
                        "measured": name not in unavailable,
                    },
                )
            )

        details: Dict[str, Any] = {"feature_flag_key": feature_flag.key, **window}
        if unavailable:
            details["unmeasured_metrics"] = unavailable

        return SafetyCheckResponse(
            feature_flag_id=feature_flag_id,
            is_healthy=is_healthy,
            metrics=statuses,
            last_checked=datetime.utcnow(),
            details=details,
        )

    async def should_rollback(
        self, db: Session, feature_flag_id: UUID
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Decide whether an automatic rollback is warranted.

        Requires: a flag a rollback to its configured percentage would change
        (``rollback_change``), monitoring enabled for the flag, automatic
        rollbacks enabled globally, and a failing safety check.
        """
        feature_flag = (
            db.query(FeatureFlag).filter(FeatureFlag.id == feature_flag_id).first()
        )
        if not feature_flag:
            return False, None, None

        config = await self.get_feature_flag_safety_config(feature_flag_id)
        if not config.enabled:
            return False, None, None

        target = _clamped_percentage(config.rollback_percentage)
        if rollback_change(feature_flag, target) is None:
            return False, None, None

        settings = await self.get_safety_settings()
        if not settings.enable_automatic_rollbacks:
            return False, None, None

        try:
            safety_check = await self.check_feature_flag_safety(feature_flag_id)
        except Exception as exc:  # metric sources failing must not roll flags back
            logger.error(
                f"Error checking safety for feature flag {feature_flag_id}: {exc}"
            )
            return False, None, None

        if safety_check.is_healthy:
            return False, None, None

        failing = next((m for m in safety_check.metrics if not m.is_healthy), None)
        if failing:
            reason = f"Metric '{failing.name}' is {describe_breach(failing)}"
            trigger_type = _metric_to_trigger(failing.name)
            trigger_value, threshold_value = failing.current_value, failing.threshold
        else:
            reason, trigger_type = (
                "Multiple issues detected",
                RollbackTriggerType.AUTOMATIC,
            )
            trigger_value = threshold_value = None

        return (
            True,
            reason,
            {
                "trigger_type": trigger_type,
                "trigger_value": trigger_value,
                "threshold_value": threshold_value,
                "safety_check": safety_check.model_dump(),
            },
        )

    # ------------------------------------------------------------------
    # Rollbacks
    # ------------------------------------------------------------------

    def execute_rollback(
        self,
        db: Session,
        feature_flag_id: UUID,
        reason: str = "Manual rollback",
        trigger_type: RollbackTriggerType = RollbackTriggerType.MANUAL,
        metrics_data: Optional[Dict[str, Any]] = None,
        trigger_value: Optional[float] = None,
        threshold_value: Optional[float] = None,
        target_percentage: int = 0,
        executed_by_user_id: Optional[UUID] = None,
        actor: Any = None,
    ) -> RollbackResponse:
        """
        Roll the flag back and record it, in one transaction (#629).

        What changes is ``rollback_change(flag, target_percentage)``:

        * ``"deactivate"`` (target 0): the flag turns off --
          ``transition(status, OFF)``, so INACTIVE -- and its global
          percentage goes to 0. Every user then gets ``enabled: false`` with
          ``reason: "inactive"``, users matched by a targeting rule included;
        * ``"lower"`` (target 1-100): the global percentage drops to the
          target; users matched by a targeting rule keep their rule's
          percentage;
        * ``None``: nothing is written and ``success`` is false, with a
          message naming the reason (archived, already off, already at or
          below the target).

        Both changes pause every ACTIVE rollout schedule of the flag, so a
        stage cannot raise the percentage again; the ids are in
        ``details["paused_schedules"]``. The flag row is locked first and
        nothing commits until the end, so the lock is held until the one
        commit.

        The flag's safety configuration is read, never written: a flag with
        one keeps it, and a flag without one gets none, so it stays on the
        global defaults. The record's ``safety_config_id`` is the
        configuration's id, or NULL for a flag on the defaults (#1206).

        The audit entry (``safety_rollback`` on the flag) is written in a
        savepoint of that transaction, by ``actor`` (the calling user) or,
        when there is none, by the safety monitor. A failed entry never
        blocks the rollback: the savepoint is rolled back, an ERROR is logged
        and the rollback still commits (#221).

        Never raises: failures are reported with ``success=False`` and the
        transaction is rolled back.
        """
        trigger_name = str(getattr(trigger_type, "value", trigger_type))
        try:
            feature_flag = (
                db.query(FeatureFlag)
                .filter(FeatureFlag.id == feature_flag_id)
                .with_for_update()
                .first()
            )
            if not feature_flag:
                # Answered here, not raised into the handler below: that one
                # reports a fixed sentence, and this one is ours to name.
                db.rollback()
                return RollbackResponse(
                    success=False,
                    feature_flag_id=feature_flag_id,
                    message=f"Feature flag {feature_flag_id} does not exist",
                    trigger_type=trigger_name,
                    timestamp=datetime.utcnow(),
                    details={"reason": reason},
                )

            previous_percentage = feature_flag.rollout_percentage
            change = rollback_change(feature_flag, target_percentage)
            if change is None:
                # Nothing to roll back. Without this guard the safety monitor
                # writes a new rollback record every cycle while the error
                # window is hot.
                message = _no_rollback_message(feature_flag, target_percentage)
                db.rollback()
                return RollbackResponse(
                    success=False,
                    feature_flag_id=feature_flag_id,
                    message=message,
                    trigger_type=trigger_name,
                    previous_percentage=previous_percentage,
                    new_percentage=previous_percentage,
                )

            # The flag's own configuration, if it has one. None is created: a
            # flag without one stays on the global defaults, and its record's
            # safety_config_id is NULL (#1206).
            safety_config = self.get_feature_flag_safety_config_record(
                db, feature_flag_id
            )
            # Who the audit entry names, read before anything is changed.
            audit_actor = self._rollback_actor(db, actor, executed_by_user_id)

            deactivated = change == "deactivate"
            if deactivated:
                feature_flag.status = transition(feature_flag.status, FlagVerb.OFF)
                feature_flag.rollout_percentage = 0
            else:
                feature_flag.rollout_percentage = target_percentage
            feature_flag.updated_at = datetime.utcnow()
            db.add(feature_flag)

            # The flag row is locked above; the schedules are locked after it,
            # the order every other writer takes them in.
            schedules = (
                db.query(RolloutSchedule)
                .filter(
                    RolloutSchedule.feature_flag_id == feature_flag_id,
                    RolloutSchedule.status == RolloutScheduleStatus.ACTIVE,
                )
                .order_by(RolloutSchedule.id)
                .with_for_update()
                .all()
            )
            for schedule in schedules:
                schedule.status = RolloutScheduleStatus.PAUSED
                db.add(schedule)
            paused_schedules = [str(schedule.id) for schedule in schedules]

            record = SafetyRollbackRecord(
                feature_flag_id=feature_flag_id,
                safety_config_id=safety_config.id if safety_config else None,
                trigger_type=trigger_name,
                trigger_reason=reason,
                previous_percentage=previous_percentage,
                target_percentage=target_percentage,
                success=True,
                executed_by_user_id=executed_by_user_id,
            )
            db.add(record)
            # The audit entry rides in a savepoint: if it fails, only it is
            # lost and the rollback below still commits (#221).
            AuditService.record_in_savepoint(
                db,
                actor=audit_actor,
                action=ActionType.SAFETY_ROLLBACK,
                entity_type=EntityType.FEATURE_FLAG,
                entity_id=feature_flag_id,
                entity_name=feature_flag.name or feature_flag.key,
                after={
                    "trigger_type": trigger_name,
                    "previous_percentage": previous_percentage,
                    "new_percentage": target_percentage,
                    "deactivated": deactivated,
                    "paused_schedules": paused_schedules,
                },
                reason=reason,
            )
            db.commit()
            db.refresh(record)

            logger.info(
                f"Rolled back feature flag {feature_flag_id} from {previous_percentage}% "
                f"to {target_percentage}% (turned off: {deactivated}, schedules paused: "
                f"{len(paused_schedules)}) due to {reason} (trigger: {trigger_name})"
            )
            if deactivated:
                message = (
                    f"Feature flag '{feature_flag.key}' turned off and rolled back "
                    f"from {previous_percentage}% to 0%"
                )
            else:
                message = (
                    f"Feature flag '{feature_flag.key}' rolled back from "
                    f"{previous_percentage}% to {target_percentage}%"
                )
            return RollbackResponse(
                success=True,
                feature_flag_id=feature_flag_id,
                message=message,
                trigger_type=trigger_name,
                previous_percentage=previous_percentage,
                new_percentage=target_percentage,
                rollback_record_id=record.id,
                timestamp=datetime.utcnow(),
                details={
                    "reason": reason,
                    "trigger_value": trigger_value,
                    "threshold_value": threshold_value,
                    "metrics_data": metrics_data,
                    "deactivated": deactivated,
                    "paused_schedules": paused_schedules,
                },
            )

        except Exception as exc:
            db.rollback()
            logger.error(
                "Rollback of feature flag %s failed (%s)",
                feature_flag_id,
                type(exc).__name__,
                exc_info=exc,
            )
            return RollbackResponse(
                success=False,
                feature_flag_id=feature_flag_id,
                message=failure_detail("Rollback failed"),
                trigger_type=trigger_name,
                timestamp=datetime.utcnow(),
                details={"reason": reason},
            )

    @staticmethod
    def _rollback_actor(db: Session, actor: Any, executed_by_user_id: Any) -> Any:
        """Who a rollback's audit entry names: ``actor``, else the user whose
        id the rollback records, else the safety monitor."""
        if actor is not None:
            return actor
        if executed_by_user_id is not None:
            user = db.get(User, executed_by_user_id)
            if user is not None:
                return user
        return SYSTEM_SAFETY_MONITOR

    async def rollback_feature_flag(
        self,
        feature_flag_id: UUID,
        percentage: Optional[int] = 0,
        reason: Optional[str] = "Manual rollback",
        trigger_type: RollbackTriggerType = RollbackTriggerType.MANUAL,
        executed_by_user_id: Optional[UUID] = None,
        actor: Any = None,
    ) -> RollbackResponse:
        """Roll the flag back to ``percentage`` and record it. 404 if the flag is unknown.

        ``actor`` is who the audit entry names: the calling user for a manual
        rollback; none (the safety monitor) for an automatic one.
        """
        self._require_flag(feature_flag_id)
        return self.execute_rollback(
            self.db,
            feature_flag_id,
            reason=reason or "Manual rollback",
            trigger_type=trigger_type,
            target_percentage=percentage or 0,
            executed_by_user_id=executed_by_user_id,
            actor=actor,
        )

    async_rollback_feature_flag = rollback_feature_flag


# ----------------------------------------------------------------------
# Schema -> column mapping helpers
# ----------------------------------------------------------------------


def _settings_columns(data: Dict[str, Any]) -> Dict[str, Any]:
    """Serialise nested MetricThreshold models so they can be stored as JSONB."""
    out = dict(data)
    if out.get("default_metrics") is not None:
        out["default_metrics"] = {
            name: (t.model_dump() if hasattr(t, "model_dump") else t)
            for name, t in out["default_metrics"].items()
        }
    return out


def _config_columns(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    if out.get("metrics") is not None:
        out["metrics"] = {
            name: (t.model_dump() if hasattr(t, "model_dump") else t)
            for name, t in out["metrics"].items()
        }
    return out
