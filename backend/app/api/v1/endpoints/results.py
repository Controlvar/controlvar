"""
Results and analysis endpoints (EP-016 + EP-021).

Provides endpoints for retrieving experiment results, daily time-series data,
sample size / power analysis, cache invalidation, and sequential testing analysis.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from backend.app.api.deps import get_current_active_user, get_current_superuser, get_db
from backend.app.core.analysis_status import analysis_notice, analysis_status
from backend.app.core.logger import unexpected_failure
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models.analysis_snapshot import AnalysisKind
from backend.app.models.experiment import Experiment, Metric, MetricType
from backend.app.models.user import User
from backend.app.schemas.bayesian import BayesianResultsResponse
from backend.app.schemas.dimensional import (
    DimensionalBreakdownResponse,
    SegmentBreakdown,
)
from backend.app.schemas.dimensional import (
    SegmentVariantResult as SchemaSegmentVariantResult,
)
from backend.app.schemas.results import (
    DailyDataPoint,
    DailyResultsResponse,
    ExperimentResultsResponse,
    SampleSizeResult,
    SRMResult,
    VariantTimeSeries,
)
from backend.app.schemas.sequential import SequentialTestingResponse
from backend.app.schemas.variance_reduction import (
    CupedMetricResult,
    CupedResultsResponse,
    VarianceReductionConfig,
    VarianceReductionMethod,
)
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.analysis_settings import resolve_analysis_settings
from backend.app.services.analysis_snapshot_service import record_snapshot
from backend.app.services.cache import CacheService
from backend.app.services.dimensional_analysis_service import DimensionalAnalysisService
from backend.app.services.event_matching import (
    assignment_times,
    converting_user_ids,
    covariate_user_ids,
)
from backend.app.services.power_calculator_service import (
    SAMPLE_SIZE_NOT_FINITE_MESSAGE,
    TREATMENT_RATE_CEILING_MESSAGE,
    SampleSizeNotFiniteError,
    compute_power,
    sample_size_two_proportions,
)
from backend.app.services.sequential_testing_service import SequentialTestingService
from backend.app.services.sufficient_stats_analysis import (
    SufficientStatsNotComputed,
    SufficientStatsRefused,
    cuped_metric_result,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper: sample-ratio mismatch (best-effort)
# ---------------------------------------------------------------------------


def _srm_block(
    experiment_id: UUID, raw: Optional[Dict[str, Any]]
) -> Optional[SRMResult]:
    """
    The response's ``srm`` block from the check ``AnalysisService`` ran.

    The check itself, and the recommendation it overrides, live in the
    service (``srm_service.sample_ratio_check``), so that the data export
    reads the same answer as this route (#880).  ``None`` when the check is
    undefined (fewer than two allocated variants, no assignments, or an
    adaptive/bandit allocation) or failed, and when the result does not
    serialise: an SRM check must never turn a results request into an error.
    """
    if raw is None:
        return None
    try:
        return SRMResult(**raw)
    except Exception as exc:
        logger.warning(
            "SRM result for experiment %s not serialisable: %s", experiment_id, exc
        )
        return None


# ---------------------------------------------------------------------------
# Helper: build a CacheService backed by Redis (best-effort)
# ---------------------------------------------------------------------------


def _cache_client_options() -> Dict[str, Any]:
    """Client options for the results cache: one connect attempt, 1 s timeouts.

    The cache is best-effort, so a Redis that is down should cost each call
    at most about a second, the connect timeout. With redis-py's defaults (ten
    retries with backoff, no socket timeouts) an uncached read, which makes
    two calls, took 7-9 s with Redis refused and about two minutes with Redis
    unreachable, measured on a laptop (#810).
    """
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    return {
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
        "retry": Retry(NoBackoff(), 0),
    }


def _get_cache_service() -> CacheService:
    """
    Attempt to create a synchronous Redis-backed CacheService.

    Returns a disabled CacheService (no Redis client) if Redis is not
    reachable or not configured, so callers never have to handle
    connection errors explicitly.
    """
    try:
        from backend.app.core.redis_client import create_redis_client

        r = create_redis_client(**_cache_client_options())
        # Quick ping to verify the connection is alive.
        r.ping()
        return CacheService(redis_client=r)
    except Exception:
        return CacheService(redis_client=None)


def _results_cache_key(
    experiment_id: UUID,
    confidence_level: float,
    correction_method: str,
    breakdown: Optional[str],
) -> str:
    """
    The Redis key for one ``GET /results/{experiment_id}`` answer.

    It starts with ``results:{experiment_id}:``, the prefix
    ``invalidate-cache`` clears, and then names the statistics engine version,
    so an answer cached by an engine that computed different numbers is never
    served after an upgrade: the new engine simply misses it.
    """
    return (
        f"results:{experiment_id}:{ENGINE_VERSION}:"
        f"{confidence_level}:{correction_method}:{breakdown or ''}"
    )


# ---------------------------------------------------------------------------
# Helper: compute dimensional breakdown (Issue #28)
# ---------------------------------------------------------------------------


def _compute_dimensional_breakdown(
    experiment_id: UUID,
    dimension: str,
    db,
    base_alpha: float = 0.05,
) -> DimensionalBreakdownResponse:
    """
    Query the database for per-segment event counts and compute breakdown statistics.

    Fetches segment values from event metadata (JSONB field), groups assignment
    and conversion counts by (segment_value, variant_id), then delegates to
    DimensionalAnalysisService for statistical computation.

    This function is tolerant: any error during data retrieval silently returns
    an empty-segment breakdown so that the main results response is not affected.
    """
    from sqlalchemy import text

    from backend.app.core.database_config import get_schema_name

    dim_service = DimensionalAnalysisService()
    segments: Dict[str, Dict[str, Any]] = {}

    # Conversions are the events named after the experiment's primary metric
    # (see services/event_matching.py); fall back to the legacy
    # event_type = 'conversion' convention when no metric row exists.
    from backend.app.models.experiment import Metric, Variant
    from backend.app.services.event_matching import (
        CONVERSION_SQL_PREDICATE,
        CONVERTING_USERS_JOIN,
        CONVERTING_USERS_SQL,
    )

    # Every variant of the experiment, with its real name and control flag
    # (#218).  Each segment is seeded from this list, so a variant with no
    # rows in a segment still appears with zero counts, and the control is the
    # variant the experiment says it is -- never guessed from its id.
    # Listed control first, then by name, so the order is stable.
    experiment_variants = sorted(
        (
            (str(v.id), v.name, bool(v.is_control))
            for v in db.query(Variant).filter(Variant.experiment_id == experiment_id)
        ),
        key=lambda row: (not row[2], row[1], row[0]),
    )

    primary_metric = (
        db.query(Metric)
        .filter(Metric.experiment_id == experiment_id)
        .order_by(Metric.is_primary.desc())
        .first()
    )
    if primary_metric is not None and primary_metric.event_name:
        conversion_predicate = CONVERSION_SQL_PREDICATE
        conversion_params: Dict[str, Any] = {"event_name": primary_metric.event_name}
    else:
        conversion_predicate = "event_type = 'conversion'"
        conversion_params = {}

    try:
        schema = get_schema_name()

        # Pull distinct segment values for this dimension from event metadata.
        # `schema` comes from get_schema_name(), which only ever returns one of
        # two literal identifiers; all request-derived values are bound params.
        seg_val_q = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            f"""
            SELECT DISTINCT
                COALESCE(
                    jsonb_extract_path_text(event_metadata, :dim_key),
                    'unknown'
                ) AS segment_value
            FROM {schema}.events
            WHERE experiment_id = :exp_id
              AND event_metadata IS NOT NULL
            """  # nosec B608 - schema is a fixed config identifier, not user input
        )
        seg_val_rows = db.execute(
            seg_val_q, {"dim_key": dimension, "exp_id": str(experiment_id)}
        ).fetchall()
        segment_values = [row[0] for row in seg_val_rows if row[0]]

        for seg_val in segment_values:
            # Count assignments per variant for this segment
            asgn_q = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                f"""
                SELECT a.variant_id::text, COUNT(DISTINCT a.user_id) AS total
                FROM {schema}.assignments a
                JOIN {schema}.events e
                  ON a.user_id = e.user_id
                 AND a.experiment_id = e.experiment_id
                WHERE a.experiment_id = :exp_id
                  AND e.event_metadata IS NOT NULL
                  AND COALESCE(
                      jsonb_extract_path_text(e.event_metadata, :dim_key),
                      'unknown'
                  ) = :seg_val
                GROUP BY a.variant_id
                """  # nosec B608 - schema is a fixed config identifier, not user input
            )
            asgn_rows = db.execute(
                asgn_q,
                {
                    "exp_id": str(experiment_id),
                    "dim_key": dimension,
                    "seg_val": seg_val,
                },
            ).fetchall()

            # Converting users per variant for this segment: assigned users
            # with a conversion event in the segment, each counted once
            # (event_matching.py), as /results counts them.
            conv_q = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                f"""
                SELECT e.variant_id::text, {CONVERTING_USERS_SQL} AS conversions
                FROM {schema}.events e
                JOIN {schema}.assignments a ON {CONVERTING_USERS_JOIN}
                WHERE e.experiment_id = :exp_id
                  AND {conversion_predicate}
                  AND e.event_metadata IS NOT NULL
                  AND COALESCE(
                      jsonb_extract_path_text(e.event_metadata, :dim_key),
                      'unknown'
                  ) = :seg_val
                GROUP BY e.variant_id
                """  # nosec B608 - schema is a fixed config identifier, not user input
            )
            conv_rows = db.execute(
                conv_q,
                {
                    "exp_id": str(experiment_id),
                    "dim_key": dimension,
                    "seg_val": seg_val,
                    **conversion_params,
                },
            ).fetchall()

            # Build variant_map for this segment, seeded with every variant
            variant_map: Dict[str, Any] = {
                vid: {
                    "variant_name": name,
                    "is_control": is_control,
                    "total": 0,
                    "conversions": 0,
                }
                for vid, name, is_control in experiment_variants
            }
            for vid, total in asgn_rows:
                variant_map.setdefault(
                    vid,
                    {"variant_name": vid, "is_control": False, "conversions": 0},
                )["total"] = int(total)
            for vid, conv in conv_rows:
                variant_map.setdefault(
                    vid, {"variant_name": vid, "is_control": False, "total": 0}
                )["conversions"] = int(conv)

            if variant_map:
                segments[seg_val] = variant_map

    except Exception as exc:
        # Non-fatal: log and return empty breakdown
        import logging

        logging.getLogger(__name__).warning(
            "Failed to fetch segment data for dimension %r: %s", dimension, exc
        )

    # Compute statistics
    segment_results = dim_service.compute_segment_results(
        segments=segments, base_alpha=base_alpha
    )
    has_hte = dim_service.detect_hte(segment_results)
    adjusted_alpha = (
        dim_service.get_adjusted_alpha(base_alpha, len(segments))
        if segments
        else base_alpha
    )

    hte_warning = (
        (
            f"Heterogeneous treatment effects detected across '{dimension}' segments. "
            "Results may differ significantly between groups — "
            "investigate per-segment effects before shipping."
        )
        if has_hte
        else None
    )

    # Convert service dataclasses to Pydantic schema
    schema_segments: list = []
    for seg in segment_results:
        schema_variants = [
            SchemaSegmentVariantResult(
                variant_id=v.variant_id,
                variant_name=v.variant_name,
                is_control=v.is_control,
                sample_size=v.sample_size,
                conversions=v.conversions,
                mean=v.mean,
                confidence_interval=v.confidence_interval,
                p_value=v.p_value,
                is_significant=v.is_significant,
            )
            for v in seg.variants
        ]
        schema_segments.append(
            SegmentBreakdown(
                segment_value=seg.segment_value,
                sample_size=seg.sample_size,
                variants=schema_variants,
            )
        )

    return DimensionalBreakdownResponse(
        dimension=dimension,
        is_exploratory=True,
        adjusted_alpha=adjusted_alpha,
        has_heterogeneous_effects=has_hte,
        hte_warning=hte_warning,
        segments=schema_segments,
    )


# ---------------------------------------------------------------------------
# Endpoint 1 — GET /{experiment_id}
# ---------------------------------------------------------------------------


@router.get("/{experiment_id}", response_model=ExperimentResultsResponse)
def get_experiment_results(
    experiment_id: UUID,
    confidence_level: Optional[float] = Query(
        default=None,
        ge=0.80,
        le=0.99,
        description=(
            "Confidence level for this request. Omit it to use the "
            "experiment's stored confidence_level (0.95 unless set otherwise)."
        ),
    ),
    correction_method: Optional[str] = Query(
        default=None,
        pattern="^(none|bonferroni|benjamini_hochberg)$",
        description=(
            "Multiple-comparison correction for this request. Omit it to use "
            "the experiment's stored correction_method (benjamini_hochberg "
            "unless set otherwise). 'none' shows the uncorrected numbers."
        ),
    ),
    use_cache: bool = Query(default=True),
    breakdown: Optional[str] = Query(
        default=None,
        description=(
            "Dimension to break down results by (e.g. 'platform', 'country', "
            "'user_tier'). When supplied the response includes a 'breakdown' field "
            "with per-segment statistics and Bonferroni-corrected significance."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ExperimentResultsResponse:
    """
    Get comprehensive statistical results for an experiment.

    Returns per-metric, per-variant statistics including p-values,
    confidence intervals, effect sizes, and an overall recommendation.

    Results are cached in Redis (TTL = 5 min for running experiments,
    24 h for completed/paused ones).  Pass ``use_cache=false`` to force
    a fresh computation.

    Supply ``breakdown=<dimension>`` to receive an additional per-segment
    breakdown of results (Issue #28).  Supported dimensions include
    ``platform``, ``country``, and ``user_tier``, but any dimension key
    present in event metadata is accepted.  Breakdowns are always marked
    exploratory and use Bonferroni-corrected significance thresholds.

    ``confidence_level`` and ``correction_method`` default to the
    experiment's stored settings (#580); a value in the request applies to
    that request only.  The settings are resolved before the cache key is
    built, and the analysis snapshot is written only for a computation under
    the stored settings.
    """
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    try:
        stored = resolve_analysis_settings(experiment)
        settings = resolve_analysis_settings(
            experiment, confidence_level, correction_method
        )
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Experiment results",
            "Could not compute the experiment's results",
            db=db,
            logger=logger,
        )

    cache_key = _results_cache_key(
        experiment_id,
        settings.confidence_level,
        settings.correction_method,
        breakdown,
    )

    # --- Cache read ---
    if use_cache:
        try:
            cache = _get_cache_service()
            cached = cache.get(cache_key)
            if cached:
                return ExperimentResultsResponse.model_validate_json(cached)
        except Exception:
            pass  # Cache miss is acceptable

    # --- Compute results ---
    try:
        service = AnalysisService(db)
        result = service.get_experiment_results(
            experiment_id,
            confidence_level=settings.confidence_level,
            correction_method=settings.correction_method,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Experiment not found")
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Experiment results",
            "Could not compute the experiment's results",
            db=db,
            logger=logger,
        )

    if result is None:
        raise HTTPException(status_code=404, detail="Experiment not found")

    # --- Map the service dict to the response schema ---
    # The service returns a dict — build ExperimentResultsResponse from it.
    # Fields that may be missing are handled with .get() and sensible defaults.
    try:
        # Normalise status to a string (may be an enum value)
        raw_status = result.get("status", "unknown")
        if hasattr(raw_status, "value"):
            raw_status = raw_status.value

        # Build summary sub-object
        summary_data = result.get("summary", {})

        # Build metrics list (may be keyed as metrics_results or metrics)
        metrics_data = result.get("metrics") or result.get("metrics_results") or []

        # --- Issue #28: Compute dimensional breakdown if requested ---
        breakdown_response: Optional[DimensionalBreakdownResponse] = None
        if breakdown:
            breakdown_response = _compute_dimensional_breakdown(
                experiment_id=experiment_id,
                dimension=breakdown,
                db=db,
                base_alpha=1.0 - result["confidence_level"],
            )

        # --- P0 statistical credibility: sample-ratio mismatch ---
        srm_response = _srm_block(experiment_id, result.get("srm"))

        response = ExperimentResultsResponse(
            experiment_id=result.get("experiment_id", str(experiment_id)),
            experiment_name=result.get("experiment_name", ""),
            status=raw_status,
            start_date=result.get("start_date"),
            end_date=result.get("end_date"),
            # What the numbers were computed under, from the computation
            # itself rather than from the request (#580).
            confidence_level=result["confidence_level"],
            correction_method=result["correction_method"],
            sample_size_adequate=result.get("sample_size_adequate", False),
            computed_at=result.get("computed_at", datetime.now(timezone.utc)),
            summary=summary_data,
            metrics=metrics_data,
            sequential_testing=_embedded_sequential(experiment, db),
            breakdown=breakdown_response,
            bayesian_results=result.get("bayesian_results"),
            srm=srm_response,
        )
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Experiment results",
            "Could not compute the experiment's results",
            db=db,
            logger=logger,
        )

    # --- Persist audit snapshots (best-effort; never fails the response) ---
    # Only a computation under the experiment's stored settings is recorded:
    # a request asking for other settings must not leave an audit row that
    # reads as the experiment's own results.
    if settings == stored:
        _record_results_snapshots(db, response)

    # --- Cache write ---
    try:
        cache = _get_cache_service()
        running_statuses = {"running", "active", "RUNNING", "ACTIVE"}
        ttl = 300 if raw_status in running_statuses else 86400
        cache.set(cache_key, response.model_dump_json(), expire=ttl)
    except Exception:
        pass  # Cache write failure is non-fatal

    return response


def _record_results_snapshots(db: Session, response: ExperimentResultsResponse) -> None:
    """
    Write the ``analysis_snapshots`` rows for one fresh results computation.

    One ``frequentist`` row carries the whole response; when the experiment
    has Bayesian analysis enabled a ``bayesian`` row records the seed and
    sample count that produced the posteriors, and when it has sequential
    testing enabled a ``sequential`` row records the embedded sequential
    block.  Cache hits do not reach this function, so a row means the numbers
    were actually recomputed.
    """
    try:
        payload = response.model_dump(mode="json")
    except Exception as exc:
        logger.warning("Could not serialise results for snapshot: %s", exc)
        return

    record_snapshot(
        db,
        response.experiment_id,
        AnalysisKind.FREQUENTIST,
        payload,
        as_of=response.computed_at,
    )

    bayesian = response.bayesian_results
    if bayesian is not None and bayesian.is_enabled:
        record_snapshot(
            db,
            response.experiment_id,
            AnalysisKind.BAYESIAN,
            payload.get("bayesian_results") or {},
            engine_version=bayesian.engine_version,
            seed=bayesian.seed,
            n_samples=bayesian.n_samples,
            as_of=response.computed_at,
        )

    # The dashboard reads the embedded block instead of calling /sequential
    # when it is present, so the sequential row is written here too (#922).
    # It is the same (experiment, kind, UTC day) row /sequential writes.
    if response.sequential_testing is not None:
        record_snapshot(
            db,
            response.experiment_id,
            AnalysisKind.SEQUENTIAL,
            payload.get("sequential_testing") or {},
            as_of=response.computed_at,
        )


# ---------------------------------------------------------------------------
# Endpoint 2 — GET /{experiment_id}/daily
# ---------------------------------------------------------------------------


@router.get("/{experiment_id}/daily", response_model=DailyResultsResponse)
def get_experiment_daily_results(
    experiment_id: UUID,
    metric_id: Optional[UUID] = Query(default=None),
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> DailyResultsResponse:
    """
    Get daily time-series results for an experiment.

    Returns both a raw daily view and a running cumulative view per variant,
    suitable for rendering trend charts.  Optionally filter to a single metric
    and/or a date range.
    """
    try:
        service = AnalysisService(db)
        daily_data: List[Dict] = service.get_daily_results(
            experiment_id,
            metric_id=metric_id,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Experiment not found")
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Daily results",
            "Could not compute the daily results",
            db=db,
            logger=logger,
        )

    # daily_data is a list of:
    # {
    #   "date": "YYYY-MM-DD",
    #   "metrics": [
    #     {
    #       "metric_id": "...",
    #       "metric_name": "...",
    #       "variants": [
    #         {
    #           "variant_id": "...",
    #           "variant_name": "...",
    #           "is_control": bool,
    #           "assignments": int,
    #           "conversions": int,
    #           "conversion_rate": float,
    #         },
    #         ...
    #       ],
    #     },
    #     ...
    #   ],
    # }
    #
    # We need to invert this into per-variant VariantTimeSeries objects,
    # each containing a list of daily data points plus cumulative totals.

    # Collect per-variant daily points.  Key = variant_id (str).
    variant_daily: Dict[str, List[Dict]] = {}  # variant_id -> list of daily dicts
    variant_meta: Dict[str, Dict] = {}  # variant_id -> {name, is_control}

    for day_entry in daily_data:
        date_str = day_entry.get("date", "")

        # Apply optional date range filter
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue

        # We take the first metric (or the one matching metric_id)
        metrics_list = day_entry.get("metrics", [])
        if not metrics_list:
            continue

        # When metric_id is provided the service already filters; otherwise
        # default to the first metric in the list.
        chosen_metric = metrics_list[0]

        for v in chosen_metric.get("variants", []):
            vid = str(v["variant_id"])
            if vid not in variant_daily:
                variant_daily[vid] = []
                variant_meta[vid] = {
                    "variant_name": v.get("variant_name", ""),
                    "is_control": v.get("is_control", False),
                }
            variant_daily[vid].append(
                {
                    "date": date_str,
                    "sample_size": v.get("assignments", 0),
                    "conversions": v.get("conversions"),
                    "mean": (v.get("conversion_rate") or 0.0) / 100.0,
                }
            )

    # Build VariantTimeSeries for each variant
    series: List[VariantTimeSeries] = []
    for vid, daily_points in variant_daily.items():
        # Sort by date ascending
        daily_points.sort(key=lambda p: p["date"])

        daily_dp = [
            DailyDataPoint(
                date=p["date"],
                sample_size=p["sample_size"],
                conversions=p.get("conversions"),
                mean=p["mean"],
            )
            for p in daily_points
        ]

        # Compute cumulative series
        cumulative_sample = 0
        cumulative_conversions = 0
        cumulative_dp: List[DailyDataPoint] = []
        for p in daily_points:
            cumulative_sample += p["sample_size"]
            cumulative_conversions += p.get("conversions") or 0
            rate = (
                cumulative_conversions / cumulative_sample
                if cumulative_sample > 0
                else 0.0
            )
            cumulative_dp.append(
                DailyDataPoint(
                    date=p["date"],
                    sample_size=cumulative_sample,
                    conversions=cumulative_conversions,
                    mean=rate,
                )
            )

        meta = variant_meta[vid]
        series.append(
            VariantTimeSeries(
                variant_id=UUID(vid),
                variant_name=meta["variant_name"],
                is_control=meta["is_control"],
                values=daily_dp,
                cumulative=cumulative_dp,
            )
        )

    return DailyResultsResponse(
        experiment_id=experiment_id,
        metric_id=metric_id,
        series=series,
    )


# ---------------------------------------------------------------------------
# Endpoint 3 — GET /{experiment_id}/sample-size
# ---------------------------------------------------------------------------


#: The relative MDE planned for when neither the request nor the experiment
#: names one.
DEFAULT_SAMPLE_SIZE_MDE = 0.05


def _planned_effect(metric: Optional[Metric]) -> Optional[float]:
    """The relative MDE saved with the metric planned on, when usable.

    Only the primary metric's ``expected_effect`` is a plan, and only a finite
    value strictly between 0 and 1 (the range the ``mde`` parameter accepts).
    Anything else -- no metric, a secondary metric, null, 0, 1 or more, a
    negative number, NaN or infinity -- is not used, and the plan falls back
    to the default.
    """
    if metric is None or not getattr(metric, "is_primary", False):
        return None
    value = getattr(metric, "expected_effect", None)
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not 0.0 < value < 1.0:
        return None
    return value


def _guide_only_reasons(experiment: Experiment) -> List[str]:
    """Why a fixed sample size only guides this experiment (see the schema)."""
    reasons: List[str] = []
    optimization = getattr(experiment, "optimization_type", None)
    optimization = getattr(optimization, "value", optimization)
    if optimization and str(optimization).lower() != "fixed":
        reasons.append("adaptive_allocation")
    else:
        allocations = {
            v.traffic_allocation
            for v in experiment.variants or []
            if v.traffic_allocation is not None
        }
        if len(allocations) > 1:
            reasons.append("unequal_allocation")
    if getattr(experiment, "sequential_testing_enabled", False):
        reasons.append("sequential_testing")
    if getattr(experiment, "bayesian_enabled", False):
        reasons.append("bayesian")
    return reasons


@router.get("/{experiment_id}/sample-size", response_model=SampleSizeResult)
def get_sample_size_status(
    experiment_id: UUID,
    baseline_conversion_rate: Optional[float] = Query(
        default=None,
        gt=0.0,
        lt=1.0,
        description=(
            "Baseline conversion rate to plan from. Omit it to use the rate "
            "observed in the control variant on the primary metric so far."
        ),
    ),
    mde: Optional[float] = Query(
        default=None,
        gt=0.0,
        lt=1.0,
        description=(
            "Minimum detectable effect, relative to the baseline: 0.05 means "
            "12% -> 12.6%. Omit it to use the effect saved with the experiment "
            "(the primary metric's expected_effect, when it is above 0 and "
            "below 1), or 0.05 when none was saved. mde_source says which."
        ),
    ),
    confidence_level: Optional[float] = Query(
        default=None,
        ge=0.80,
        le=0.99,
        description=(
            "Confidence level of the two-sided test. Omit it to use the "
            "experiment's stored confidence_level."
        ),
    ),
    power_target: float = Query(
        default=0.80,
        ge=0.50,
        le=0.99,
        description="Target power. Default 0.80.",
    ),
    correction_method: Optional[str] = Query(
        default=None,
        pattern="^(none|bonferroni|benjamini_hochberg)$",
        description=(
            "Correction for comparing several variants with the control. "
            "'bonferroni' and 'benjamini_hochberg' plan each comparison at "
            "alpha / (variants - 1). Omit it to use the experiment's stored "
            "correction_method, as the results do."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SampleSizeResult:
    """
    The planned sample size for an experiment, and how far it has got.

    Plans a two-sided two-proportion test on the primary metric: the users
    each variant needs to detect a relative lift of ``mde`` over the baseline
    at ``confidence_level`` with ``power_target`` power.  The MDE is ``mde``
    when sent, otherwise the primary metric's saved ``expected_effect`` when
    it is above 0 and below 1, otherwise 0.05 (``mde_source`` says which).
    The baseline is ``baseline_conversion_rate`` when sent, otherwise the
    control variant's conversion rate so far -- counted the way the results
    count it, but not read from the results cache, so for up to its five
    minutes it can be newer than the rate the results show.

    Progress is the smallest variant's assignments.  Nothing is saved.
    With no data to plan from the answer is 200 with
    ``required_sample_size_per_variant: null`` and ``unavailable_reason``.
    A sent baseline that the MDE raises to 100% or more answers 422, and so
    does one too close to its treatment rate for the size to be a finite
    number; an observed rate in that position gives ``effect_too_small``.
    """
    experiment = (
        db.query(Experiment)
        .options(
            joinedload(Experiment.variants),
            joinedload(Experiment.metric_definitions),
        )
        .filter(Experiment.id == experiment_id)
        .first()
    )
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")

    try:
        metrics = AnalysisService._ordered_metrics(experiment)
        metric = metrics[0] if metrics else None

        # The MDE: the request's, else the one saved with the experiment on the
        # metric planned on, else the default.
        mde_source: str
        if mde is not None:
            mde_source = "request"
        else:
            mde = _planned_effect(metric)
            if mde is not None:
                mde_source = "experiment"
            else:
                mde = DEFAULT_SAMPLE_SIZE_MDE
                mde_source = "default"

        if (
            baseline_conversion_rate is not None
            and baseline_conversion_rate * (1.0 + mde) >= 1.0
        ):
            raise HTTPException(status_code=422, detail=TREATMENT_RATE_CEILING_MESSAGE)

        confidence_level, correction_method = resolve_analysis_settings(
            experiment, confidence_level, correction_method
        )

        from sqlalchemy import func as sqla_func

        from backend.app.models.assignment import Assignment
        from backend.app.services.event_matching import count_converting_users

        variants = list(experiment.variants or [])
        comparisons = max(1, len(variants) - 1)
        alpha = 1.0 - confidence_level
        if correction_method != "none":
            alpha = alpha / comparisons

        # Assignments per variant, counted as the results count them.
        counts = dict(
            db.query(Assignment.variant_id, sqla_func.count(Assignment.id))
            .filter(Assignment.experiment_id == experiment.id)
            .group_by(Assignment.variant_id)
            .all()
        )
        per_variant = {str(v.id): int(counts.get(v.id, 0) or 0) for v in variants}
        current = min(per_variant.values()) if per_variant else 0

        control = next((v for v in variants if v.is_control), None)

        baseline: Optional[float] = None
        baseline_source: Optional[str] = None
        baseline_users: Optional[int] = None
        reason: Optional[str] = None

        if baseline_conversion_rate is not None:
            baseline = baseline_conversion_rate
            baseline_source = "request"
        elif metric is None:
            reason = "no_metric"
        elif control is None or per_variant.get(str(control.id), 0) == 0:
            reason = "no_control_data"
        else:
            baseline_users = per_variant[str(control.id)]
            converted = count_converting_users(
                db, experiment.id, control.id, metric.event_name
            )
            if converted == 0:
                reason = "no_control_conversions"
            elif converted >= baseline_users:
                reason = "rate_at_boundary"
            else:
                baseline = converted / baseline_users
                baseline_source = "observed"
                if baseline * (1.0 + mde) >= 1.0:
                    reason = "effect_out_of_range"

        required: Optional[int] = None
        achieved_power: Optional[float] = None
        if baseline is not None and reason is None:
            treatment = baseline * (1.0 + mde)
            try:
                required = sample_size_two_proportions(
                    baseline, treatment, alpha, power_target, two_tailed=True
                )
            except SampleSizeNotFiniteError:
                # The treatment rate is too close to the baseline for the size
                # to be a finite number. A sent baseline is the caller's input
                # (422); an observed one is not, so it is a reason (200).
                if baseline_source == "request":
                    raise HTTPException(
                        status_code=422, detail=SAMPLE_SIZE_NOT_FINITE_MESSAGE
                    )
                reason = "effect_too_small"
            else:
                achieved_power = compute_power(
                    current, baseline, treatment, alpha, two_tailed=True
                )

        metric_type = getattr(metric, "metric_type", None) if metric else None
        metric_type = getattr(metric_type, "value", metric_type)

        return SampleSizeResult(
            required_sample_size_per_variant=required,
            current_sample_size_per_variant=current,
            is_adequate=required is not None and current >= required,
            achieved_power=achieved_power,
            days_to_significance=None,
            projected_completion_date=None,
            baseline_rate=baseline,
            mde=mde,
            mde_source=mde_source,
            confidence_level=confidence_level,
            power_target=power_target,
            baseline_source=baseline_source,
            baseline_users=baseline_users,
            metric_id=metric.id if metric else None,
            metric_name=metric.name if metric else None,
            metric_type=str(metric_type).lower() if metric_type else None,
            analysed_as="conversion",
            alpha=alpha,
            comparisons=comparisons,
            correction_method=correction_method,
            mde_absolute=baseline * mde if baseline is not None else None,
            unavailable_reason=reason,
            guide_only_reasons=_guide_only_reasons(experiment),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Sample size",
            "Could not compute the sample size",
            db=db,
            logger=logger,
        )


# ---------------------------------------------------------------------------
# Endpoint 4 — POST /{experiment_id}/invalidate-cache
# ---------------------------------------------------------------------------


@router.post("/{experiment_id}/invalidate-cache")
def invalidate_results_cache(
    experiment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
) -> Dict:
    """
    Invalidate all cached results for an experiment (superuser only).

    Deletes every Redis key that matches ``results:{experiment_id}:*``.
    Cache invalidation is best-effort: a failure here does not raise an error.
    """
    try:
        from backend.app.core.redis_client import create_redis_client

        r = create_redis_client(**_cache_client_options())
        cache = CacheService(redis_client=r)
        cache.clear(pattern=f"results:{experiment_id}:*")
    except Exception:
        pass  # Cache invalidation is best-effort

    return {"status": "ok", "experiment_id": str(experiment_id)}


# ---------------------------------------------------------------------------
# EP-021: Sequential Testing Helpers
# ---------------------------------------------------------------------------


def _get_experiment_for_sequential(
    experiment_id: UUID, db: Session
) -> Optional[Experiment]:
    """Fetch experiment for sequential analysis. Returns None if not found."""
    return (
        db.query(Experiment)
        .options(
            joinedload(Experiment.variants),
            joinedload(Experiment.metric_definitions),
        )
        .filter(Experiment.id == experiment_id)
        .first()
    )


def _get_sequential_data(
    experiment: Experiment, db: Session
) -> Tuple[int, int, int, int]:
    """
    Extract control/treatment conversion data for sequential analysis.

    The treatment is the one created first: ``Experiment.variants`` is ordered
    by ``created_at`` then ``id`` (#929), so an experiment with three or more
    arms compares the control against the same arm on every call.

    Returns (control_successes, control_total, treatment_successes, treatment_total).
    """
    from sqlalchemy import func

    from backend.app.models.assignment import Assignment

    control_variant = next((v for v in experiment.variants if v.is_control), None)
    treatment_variant = next((v for v in experiment.variants if not v.is_control), None)

    if not control_variant or not treatment_variant:
        return (0, 0, 0, 0)

    # Get primary metric
    primary_metric = next(
        (m for m in experiment.metric_definitions if m.is_primary),
        experiment.metric_definitions[0] if experiment.metric_definitions else None,
    )

    def _count_assignments(variant_id):
        return (
            db.query(func.count(Assignment.id))
            .filter(
                Assignment.experiment_id == experiment.id,
                Assignment.variant_id == variant_id,
            )
            .scalar()
            or 0
        )

    def _count_conversions(variant_id):
        """Converting users, as /results counts them (event_matching.py)."""
        if not primary_metric:
            return 0
        from backend.app.services.event_matching import count_converting_users

        return count_converting_users(
            db, experiment.id, variant_id, primary_metric.event_name
        )

    control_total = _count_assignments(control_variant.id)
    control_successes = _count_conversions(control_variant.id)
    treatment_total = _count_assignments(treatment_variant.id)
    treatment_successes = _count_conversions(treatment_variant.id)

    return (control_successes, control_total, treatment_successes, treatment_total)


#: The significance levels the sequential analysis accepts: (0, MAX].
SEQUENTIAL_ALPHA_DEFAULT = 0.05
SEQUENTIAL_ALPHA_MAX = 0.2

ALWAYS_VALID_ALIAS_NOTICE = (
    "The configured method 'always_valid' is an alias of 'msprt'; this is the "
    "mSPRT analysis."
)


def _stored_sequential_alpha(config: Dict[str, Any]) -> Tuple[float, Optional[str]]:
    """The significance level stored in *config*, with a notice if it is unusable.

    The experiments API validates ``alpha`` on the way in, so a stored value
    outside (0, 0.2] can only have been written some other way.  It is not
    used silently: the default is used and the notice says so.
    """
    stored = config.get("alpha")
    if stored is None:
        return SEQUENTIAL_ALPHA_DEFAULT, None
    if (
        isinstance(stored, (int, float))
        and not isinstance(stored, bool)
        and 0.0 < stored <= SEQUENTIAL_ALPHA_MAX
    ):
        return float(stored), None
    return SEQUENTIAL_ALPHA_DEFAULT, (
        f"The stored alpha {stored!r} is outside (0, {SEQUENTIAL_ALPHA_MAX}]; "
        f"{SEQUENTIAL_ALPHA_DEFAULT} was used."
    )


def _compute_sequential_response(
    experiment: Experiment, db: Session, alpha: Optional[float]
) -> SequentialTestingResponse:
    """
    The sequential analysis of *experiment*, as ``/sequential`` serves it.

    *alpha* is the per-request significance level; ``None`` uses the stored
    ``sequential_testing_config.alpha``, else 0.05.  It neither checks that
    sequential testing is enabled nor records a snapshot: the callers do.
    Shared by ``GET /{experiment_id}/sequential`` and the block embedded in
    ``GET /{experiment_id}`` (#922), so the two cannot disagree.
    """
    # Extract config
    config: Dict[str, Any] = dict(experiment.sequential_testing_config or {})
    tau_squared = config.get("tau_squared", 0.001)
    notices: List[str] = []
    effective_alpha: float
    if alpha is None:
        effective_alpha, alpha_notice = _stored_sequential_alpha(config)
        if alpha_notice:
            notices.append(alpha_notice)
    else:
        effective_alpha = alpha
    # The sequential_testing_method column is not written by the experiments
    # API today (only the config's "method" is); the column is read in case it
    # was set some other way.
    if "always_valid" in (
        config.get("method"),
        getattr(experiment, "sequential_testing_method", None),
    ):
        notices.append(ALWAYS_VALID_ALIAS_NOTICE)

    # Get conversion data
    control_s, control_t, treatment_s, treatment_t = _get_sequential_data(
        experiment, db
    )

    # Calculate duration
    actual_days = 0
    if experiment.start_date:
        delta = (
            datetime.now(timezone.utc)
            - experiment.start_date.replace(tzinfo=timezone.utc)
            if experiment.start_date.tzinfo is None
            else datetime.now(timezone.utc) - experiment.start_date
        )
        actual_days = max(0, delta.days)

    # Run sequential analysis
    service = SequentialTestingService()
    analysis = service.run_sequential_analysis(
        control_successes=control_s,
        control_total=control_t,
        treatment_successes=treatment_s,
        treatment_total=treatment_t,
        config={
            "tau_squared": tau_squared,
            "alpha": effective_alpha,
            "actual_days": actual_days,
            "expected_days": config.get("expected_duration_days", 30),
            "required_sample_size": config.get("required_sample_size", 10000),
        },
    )

    # Convert dataclass to response schema
    msprt_data = None
    if analysis.msprt_result:
        msprt_data = {
            "lambda_ratio": analysis.msprt_result.lambda_ratio,
            "always_valid_p_value": analysis.msprt_result.always_valid_p_value,
            "can_stop": analysis.msprt_result.can_stop,
            "evidence_strength": analysis.msprt_result.evidence_strength.value,
            "boundary": analysis.msprt_result.boundary,
        }

    cs_data = None
    if analysis.confidence_sequence:
        cs_data = {
            "lower": analysis.confidence_sequence.lower,
            "upper": analysis.confidence_sequence.upper,
            "width": analysis.confidence_sequence.width,
            "sample_size": analysis.confidence_sequence.sample_size,
        }

    trajectory_data = [
        {
            "sample_size": pt.sample_size,
            "lambda_ratio": pt.lambda_ratio,
            "always_valid_p_value": pt.always_valid_p_value,
            "can_stop": pt.can_stop,
        }
        for pt in analysis.evidence_trajectory
    ]

    risk_data = None
    if analysis.long_running_risk:
        risk_data = {
            "is_at_risk": analysis.long_running_risk.is_at_risk,
            "expected_duration_days": analysis.long_running_risk.expected_duration_days,
            "actual_duration_days": analysis.long_running_risk.actual_duration_days,
            "risk_ratio": analysis.long_running_risk.risk_ratio,
            "recommendation": analysis.long_running_risk.recommendation,
        }

    status = analysis_status("sequential")
    notice = analysis_notice("sequential")
    if notices:
        # Appended to the table's notice, so only while the analysis is beta:
        # a ga label carries no notice, and these sentences must not create one.
        notice = " ".join([notice, *notices]) if notice else None

    return SequentialTestingResponse(
        method=analysis.method.value,
        msprt_result=msprt_data,
        confidence_sequence=cs_data,
        evidence_trajectory=trajectory_data,
        # No planned-looks table is computed (#232); the field stays in the
        # stable response shape and is always empty.
        alpha_spending=[],
        long_running_risk=risk_data,
        recommended_action=analysis.recommended_action,
        at_risk=risk_data["is_at_risk"] if risk_data else None,
        analysis_status=status,
        analysis_notice=notice,
    )


def _embedded_sequential(
    experiment: Any, db: Session
) -> Optional[SequentialTestingResponse]:
    """
    The ``sequential_testing`` block of a results response (#922).

    ``None`` when the experiment does not have sequential testing enabled, and
    when the analysis fails: a failure is logged as a warning and never turns
    the results request into an error.  It runs in a savepoint, so a failed
    query leaves the request's session usable.
    """
    if not getattr(experiment, "sequential_testing_enabled", False):
        return None
    try:
        with db.begin_nested():
            return _compute_sequential_response(experiment, db, alpha=None)
    except Exception as exc:
        logger.warning(
            "Sequential analysis for results of experiment %s failed (%s); "
            "sequential_testing is null",
            getattr(experiment, "id", None),
            type(exc).__name__,
        )
        return None


# ---------------------------------------------------------------------------
# Endpoint 5 — GET /{experiment_id}/sequential (EP-021)
# ---------------------------------------------------------------------------


@router.get(
    "/{experiment_id}/sequential",
    response_model=SequentialTestingResponse,
)
def get_sequential_results(
    experiment_id: UUID,
    alpha: Optional[float] = Query(
        default=None,
        gt=0.0,
        le=SEQUENTIAL_ALPHA_MAX,
        description=(
            "Significance level for this request, above 0 and at most 0.2. "
            "Overrides the experiment's stored sequential_testing_config.alpha "
            "(default 0.05). The mSPRT boundary is 1/alpha."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SequentialTestingResponse:
    """
    Get sequential testing analysis for an experiment (EP-021).

    Returns the mSPRT evidence ratio, an always-valid confidence interval,
    the evidence trajectory for charting, a recommended action
    (stop_for_effect or continue) and the advisory ``at_risk`` flag.
    ``alpha_spending`` is always empty: no planned-looks table is computed.

    The significance level is the ``alpha`` query parameter, else the stored
    ``sequential_testing_config.alpha``, else 0.05.

    Only available for experiments with sequential_testing_enabled=True.
    """
    experiment = _get_experiment_for_sequential(experiment_id, db)
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    if not experiment.sequential_testing_enabled:
        raise HTTPException(
            status_code=404,
            detail="Sequential testing is not enabled for this experiment",
        )

    response = _compute_sequential_response(experiment, db, alpha)

    # Audit snapshot (best-effort; mSPRT is closed-form, so no seed).
    try:
        record_snapshot(
            db,
            experiment_id,
            AnalysisKind.SEQUENTIAL,
            response.model_dump(mode="json"),
        )
    except Exception as exc:
        logger.warning("Sequential snapshot failed for %s: %s", experiment_id, exc)

    return response


# ---------------------------------------------------------------------------
# Issue #21 — CUPED helper + endpoint (#217: each user's own history)
# ---------------------------------------------------------------------------


class ExperimentNotFound(Exception):
    """The experiment does not exist: the only CUPED failure that answers 404.

    Deliberately not a ``ValueError``.  Statistics code raises ``ValueError``
    for conditions that are not a missing experiment (``fromisoformat``, the
    sufficient-statistics refusals), and mapping those to 404 would repeat
    their text.
    """


#: Reasons a CUPED comparison is listed without numbers (``unavailable_reason``).
NOT_A_PROPORTION_METRIC = "not_a_proportion_metric"
WINSORIZATION_NEEDS_MEAN_METRIC = "winsorization_needs_mean_metric"
NO_CONTROL_VARIANT = "no_control_variant"
NO_TREATMENT_VARIANT = "no_treatment_variant"
COVARIATE_METRIC_NOT_FOUND = "covariate_metric_not_found"
METRIC_HAS_NO_EVENT_NAME = "metric_has_no_event_name"

_ADJUSTED_METHODS = (VarianceReductionMethod.CUPED, VarianceReductionMethod.CUPED_PLUS)


def _metric_is_proportion(metric_def: Any) -> bool:
    metric_type = getattr(metric_def.metric_type, "value", metric_def.metric_type)
    return metric_type == MetricType.CONVERSION.value


def _cuped_reason_rows(
    metric_def: Any,
    control: Any,
    treatments: List[Any],
    reason: str,
    method: VarianceReductionMethod,
    sizes: Optional[Dict[str, int]] = None,
) -> List[CupedMetricResult]:
    """One row per treatment (or one row, with no variant) carrying ``reason``."""
    base = {
        "metric_id": str(metric_def.id),
        "metric_name": metric_def.name,
        "unavailable_reason": reason,
        "method": method,
    }
    if control is None or not treatments:
        return [CupedMetricResult(**base)]
    sizes = sizes or {}
    return [
        CupedMetricResult(
            **base,
            variant_id=str(treatment.id),
            variant_name=treatment.name,
            control_variant_id=str(control.id),
            control_sample_size=sizes.get(str(control.id)),
            treatment_sample_size=sizes.get(str(treatment.id)),
        )
        for treatment in treatments
    ]


def get_cuped_results_data(
    experiment_id: UUID,
    db: Session,
) -> CupedResultsResponse:
    """
    Compute CUPED variance-reduced results for an experiment (#217).

    For each proportion metric and each treatment, the treatment's conversion
    rate minus the control's, adjusted for a covariate ``X``: whether the user
    sent the covariate event in their own window before assignment
    (``event_matching.covariate_user_ids``).  The outcome ``Y`` is the
    converters ``/results`` counts (``event_matching.converting_user_ids``).
    The estimate itself is ``cuped_metric_result``, from per-arm sums.

    The method comes from the experiment's ``variance_reduction_config``, and
    the confidence level and correction from its stored settings
    (``resolve_analysis_settings``).  A metric that cannot be computed is
    listed with ``unavailable_reason``; any other failure propagates.

    Raises:
        ExperimentNotFound: the experiment does not exist.
        pydantic.ValidationError: the stored ``variance_reduction_config`` is
            one no request could have stored.
    """
    experiment = (
        db.query(Experiment)
        .options(
            joinedload(Experiment.variants),
            joinedload(Experiment.metric_definitions),
        )
        .filter(Experiment.id == experiment_id)
        .first()
    )
    if not experiment:
        raise ExperimentNotFound()

    config = VarianceReductionConfig.model_validate(
        experiment.variance_reduction_config or {}
    )
    method = config.method
    settings = resolve_analysis_settings(experiment)
    computed_at = datetime.now(timezone.utc).isoformat()

    control = next((v for v in experiment.variants if v.is_control), None)
    treatments = [v for v in experiment.variants if not v.is_control]
    users_by_variant = assignment_times(db, experiment.id)
    sizes = {
        str(v.id): len(users_by_variant.get(str(v.id), {})) for v in experiment.variants
    }
    assigned_at: Dict[str, datetime] = {}
    for users in users_by_variant.values():
        assigned_at.update(users)
    metrics_by_id = {str(m.id): m for m in experiment.metric_definitions}
    adjust = method in _ADJUSTED_METHODS
    # cuped_plus is computed as cuped; each row says what was computed.
    row_method = VarianceReductionMethod.CUPED if adjust else method

    covariates: Dict[str, Set[str]] = {}
    skipped: Dict[str, int] = {}
    metric_results: List[CupedMetricResult] = []

    for metric_def in experiment.metric_definitions:

        def reason(code: str) -> List[CupedMetricResult]:
            return _cuped_reason_rows(
                metric_def, control, treatments, code, row_method, sizes
            )

        if control is None:
            metric_results.extend(reason(NO_CONTROL_VARIANT))
            continue
        if not treatments:
            metric_results.extend(reason(NO_TREATMENT_VARIANT))
            continue
        if not _metric_is_proportion(metric_def):
            metric_results.extend(reason(NOT_A_PROPORTION_METRIC))
            continue
        if method == VarianceReductionMethod.WINSORIZATION:
            # Clipping 0/1 outcomes at a percentile zeroes every conversion
            # when fewer than (100 - percentile)% convert; it is for means.
            metric_results.extend(reason(WINSORIZATION_NEEDS_MEAN_METRIC))
            continue

        covariate_event: Optional[str] = None
        x_users: Set[str] = set()
        if adjust:
            covariate_metric = (
                metrics_by_id.get(str(config.covariate_metric_id))
                if config.covariate_metric_id
                else metric_def
            )
            if covariate_metric is None:
                metric_results.extend(reason(COVARIATE_METRIC_NOT_FOUND))
                continue
            covariate_event = covariate_metric.event_name
            if not covariate_event:
                metric_results.extend(reason(METRIC_HAS_NO_EVENT_NAME))
                continue
            if covariate_event not in covariates:
                found, unreadable = covariate_user_ids(
                    db, assigned_at, covariate_event, config.covariate_lookback_days
                )
                covariates[covariate_event] = found
                if unreadable:
                    skipped[str(metric_def.id)] = unreadable
            x_users = covariates[covariate_event]

        arms = []
        x_counts: Dict[str, int] = {}
        for variant in experiment.variants:
            users = users_by_variant.get(str(variant.id), {})
            converted = converting_user_ids(
                db, experiment.id, variant.id, metric_def.event_name
            )
            n_converted = len(converted)
            with_history = x_users.intersection(users)
            both = len(with_history & converted)
            x_counts[str(variant.id)] = len(with_history)
            arms.append(
                (
                    variant,
                    len(users),
                    float(n_converted),
                    float(len(with_history)),
                    float(n_converted),
                    float(len(with_history)),
                    float(both),
                )
            )

        try:
            rows = cuped_metric_result(
                arms,
                settings.confidence_level,
                settings.correction_method,
                metric=metric_def,
                adjust=adjust,
            )
        except (SufficientStatsNotComputed, SufficientStatsRefused) as exc:
            metric_results.extend(reason(exc.code))
            continue

        control_id = str(control.id)
        for row in rows:
            coverage = None
            if adjust:
                n = sizes[control_id] + sizes[row["variant_id"]]
                if n:
                    covered = x_counts[control_id] + x_counts[row["variant_id"]]
                    coverage = 100.0 * covered / n
            metric_results.append(
                CupedMetricResult(
                    **row,
                    method=row_method,
                    covariate_event_name=covariate_event,
                    covariate_coverage_pct=coverage,
                )
            )

    if skipped:
        # Ids and counts only: the stored values are client text.
        logger.warning(
            "CUPED skipped stored event timestamps it could not read, by metric: %s",
            ", ".join(f"{metric_id}={count}" for metric_id, count in skipped.items()),
        )

    return CupedResultsResponse(
        experiment_id=str(experiment_id),
        method=method,
        confidence_level=settings.confidence_level,
        correction_method=settings.correction_method,
        covariate_lookback_days=config.covariate_lookback_days,
        metrics=metric_results,
        computed_at=computed_at,
    )


# ---------------------------------------------------------------------------
# Endpoint 6 — GET /{experiment_id}/cuped (Issue #21)
# ---------------------------------------------------------------------------


@router.get(
    "/{experiment_id}/cuped",
    response_model=CupedResultsResponse,
    summary="Beta: CUPED variance-reduced results",
    # The numbers are GA (#217; analysis_status says so).  The route stays
    # x-stability: beta while its response is still being shaped (mean
    # metrics, #439).
    openapi_extra={"x-stability": "beta"},
)
def get_cuped_results(
    experiment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CupedResultsResponse:
    """
    Get CUPED variance-reduced results for an experiment (#21, #217).

    For each conversion metric and each treatment, the effect against the
    control adjusted for each user's own events in the
    ``covariate_lookback_days`` before their assignment.  History counts only
    if the server received it before the user was assigned.  Intervals use
    the experiment's stored ``confidence_level`` and ``corrected_p_value`` its
    ``correction_method``.  A comparison that cannot be computed is listed
    with ``unavailable_reason``.

    The method is read from the experiment's ``variance_reduction_config``:

    - ``none``          — No adjustment: the unadjusted effect, θ exactly 0.
    - ``cuped``         — CUPED adjustment.
    - ``cuped_plus``    — Computed as ``cuped`` (each row's ``method`` says so).
    - ``winsorization`` — Clipping, for mean metrics.

    Returns 404 if the experiment does not exist.
    """
    try:
        response = get_cuped_results_data(experiment_id, db)
    except ExperimentNotFound:
        raise HTTPException(status_code=404, detail="Experiment not found")
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "CUPED results",
            "Could not compute the CUPED results",
            db=db,
            logger=logger,
        )

    # Audit snapshot (best-effort; CUPED is closed-form, so no seed).  The
    # numbers are always computed under the stored settings.
    try:
        record_snapshot(
            db,
            experiment_id,
            AnalysisKind.CUPED,
            response.model_dump(mode="json"),
            engine_version=response.engine_version,
            as_of=response.computed_at,
        )
    except Exception as exc:
        logger.warning("CUPED snapshot failed for %s: %s", experiment_id, exc)

    return response


# ---------------------------------------------------------------------------
# Endpoint 7 — GET /{experiment_id}/bayesian (P0 statistical credibility)
# ---------------------------------------------------------------------------


@router.get(
    "/{experiment_id}/bayesian",
    response_model=BayesianResultsResponse,
)
def get_bayesian_results(
    experiment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> BayesianResultsResponse:
    """
    Get the Bayesian analysis block for an experiment on its own.

    Returns the same ``bayesian_results`` payload that ``GET /results/{id}``
    embeds, always freshly computed (no cache).  The Monte Carlo seed is
    derived from ``(experiment_id, UTC day, n_samples)`` and echoed in the
    response, so two calls on the same day return byte-identical posteriors
    and the same ``seed``.

    Returns ``is_enabled=false`` (HTTP 200) when the experiment has not opted
    into Bayesian analysis, and 404 when the experiment does not exist.
    """
    experiment = _get_experiment_for_sequential(experiment_id, db)
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    service = AnalysisService(db)
    if not service.is_bayesian_enabled(experiment):
        return BayesianResultsResponse(is_enabled=False)

    try:
        response = service.compute_bayesian_results(experiment)
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Bayesian results",
            "Could not compute the Bayesian results",
            db=db,
            logger=logger,
        )

    try:
        record_snapshot(
            db,
            experiment_id,
            AnalysisKind.BAYESIAN,
            response.model_dump(mode="json"),
            engine_version=response.engine_version,
            seed=response.seed,
            n_samples=response.n_samples,
        )
    except Exception as exc:
        logger.warning("Bayesian snapshot failed for %s: %s", experiment_id, exc)

    return response
