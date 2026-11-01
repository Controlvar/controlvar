"""
Experiment schema models for validation and serialization.

This module defines Pydantic models for experiment-related data structures.
These models are used for request/response validation and documentation.
"""

from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo, available_timezones

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    validate_experiment_targeting,
)
from backend.app.schemas.bandit import OptimizationType
from backend.app.schemas.bayesian import BayesianConfig
from backend.app.schemas.results import CorrectionMethod
from backend.app.schemas.split_url_config import (
    SPLIT_URL_CONFIG_ERROR,
    SplitUrlConfig,
    find_split_url_config_problem,
)
from backend.app.schemas.variance_reduction import VarianceReductionConfig


class ExperimentStatus(str, Enum):
    """Experiment status enum."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class ExperimentType(str, Enum):
    """Types of experiments."""

    A_B = "a_b"  # Simple A/B test
    MULTIVARIATE = "mv"  # Test with multiple variants
    SPLIT_URL = "split_url"  # Split test with different URLs
    BANDIT = "bandit"  # Multi-armed bandit


class MetricType(str, Enum):
    """Types of metrics."""

    CONVERSION = "conversion"  # Binary conversion event
    REVENUE = "revenue"  # Revenue/monetary value
    COUNT = "count"  # Event count
    DURATION = "duration"  # Time duration
    CUSTOM = "custom"  # Custom metric


#: The largest value a 32-bit ``INTEGER`` column holds. An integer field stored
#: in one carries ``le=INT32_MAX``, so a larger value answers 422 instead of
#: reaching the database (#559).
INT32_MAX = 2**31 - 1

#: What a metric's type means to the analysis (#439).  The same words on the
#: request (``MetricBase``) and the response (``MetricResponse``).
METRIC_TYPE_DESCRIPTION = (
    "What the metric measures.  conversion: the share of assigned users who "
    "sent event_name.  count: the number of event_name events per assigned "
    "user; their value is not read.  revenue, duration and custom: the total "
    "value of a user's event_name events, per assigned user.  In each mean a "
    "user with none counts as 0."
)

#: ``aggregation_method`` and ``event_value_path`` are accepted, stored and
#: returned, and no analysis reads them (#439).  Their descriptions say so.
AGGREGATION_METHOD_DESCRIPTION = (
    "Not read: the analysis does not use it, and how a metric is analysed "
    "follows from metric_type (see analysis in the results).  Stored and "
    "returned as sent."
)
EVENT_VALUE_PATH_DESCRIPTION = (
    "Not read: the analysis does not use it.  A revenue, duration or custom "
    "metric adds up each event's top-level value; a count or conversion "
    "metric does not read value, and values inside an event's properties are "
    "never read.  Stored and returned as sent."
)


#: The message for a ``time_zone`` that is not a known IANA zone name. Fixed:
#: it names the field and never repeats the submitted value (#483).
TIME_ZONE_ERROR = (
    "time_zone must be an IANA time zone name, such as UTC or America/Los_Angeles"
)


@lru_cache(maxsize=1)
def _known_time_zones() -> frozenset:
    """Every IANA zone name the interpreter can load (from ``tzdata``, which
    the runtime lock pins, or the system zone files).

    Membership, not ``ZoneInfo(name)`` alone, decides: on a case-insensitive
    file system ``ZoneInfo("utc")`` loads, so a bare load would accept a name
    on a laptop that the Linux image refuses.
    """
    return frozenset(available_timezones())


def _zone(name: str) -> tzinfo:
    """The zone *name* names. ``UTC`` always resolves, even with no zone data."""
    if name == "UTC":
        return timezone.utc
    return ZoneInfo(name)


class ScheduleConfig(BaseModel):
    """Configuration for experiment scheduling.

    ``time_zone`` is how a date written without a UTC offset is read: such a
    date is taken as wall-clock time in that zone and converted to UTC. A date
    that carries an offset (``Z``, ``+02:00``) means exactly that instant and
    the zone does not change it. Dates are stored in UTC; the zone itself is
    not stored.
    """

    start_date: Optional[datetime] = Field(
        None, description="Date and time when experiment should automatically start"
    )
    end_date: Optional[datetime] = Field(
        None, description="Date and time when experiment should automatically complete"
    )
    time_zone: str = Field(
        "UTC",
        max_length=64,
        description=(
            "IANA time zone name (for example America/Los_Angeles) for a date "
            "given without a UTC offset; a date with an offset keeps it. "
            "Dates are stored in UTC. Default UTC."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "start_date": "2023-12-01T00:00:00Z",
                "end_date": "2023-12-31T23:59:59Z",
                "time_zone": "UTC",
            }
        }
    )

    @field_validator("start_date")
    @classmethod
    def validate_start_date(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Validate that start_date is in the future if provided.

        A date without an offset is checked in ``validate_dates``, once it has
        been read in ``time_zone``.
        """
        if (
            v
            and v.tzinfo is not None
            and v < datetime.now(timezone.utc) - timedelta(minutes=10)
        ):  # Allow small buffer
            raise ValueError("Start date must be in the future")
        return v

    @field_validator("time_zone")
    @classmethod
    def validate_time_zone(cls, v: str) -> str:
        """Refuse a name that is not an IANA zone, with a fixed message (#483)."""
        if v != "UTC" and v not in _known_time_zones():
            raise ValueError(TIME_ZONE_ERROR)
        return v

    @model_validator(mode="after")
    def validate_dates(self) -> "ScheduleConfig":
        """Read offset-less dates in ``time_zone``, then check them.

        The conversion happens before any comparison, so a date without an
        offset is compared as the instant it names rather than raising on a
        naive/aware comparison.
        """
        zone = _zone(self.time_zone)
        naive_start = self.start_date is not None and self.start_date.tzinfo is None
        for name in ("start_date", "end_date"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                # Only a field the request carried can be naive, so this
                # assignment does not mark an omitted field as set.
                setattr(self, name, value.replace(tzinfo=zone).astimezone(timezone.utc))

        if naive_start and self.start_date < datetime.now(timezone.utc) - timedelta(
            minutes=10
        ):  # Allow small buffer
            raise ValueError("Start date must be in the future")

        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise ValueError("End date must be after start date")

        # Minimum duration check
        if self.start_date and self.end_date:
            min_duration = timedelta(hours=1)
            if self.end_date - self.start_date < min_duration:
                raise ValueError(f"Experiment must run for at least {min_duration}")

        return self


class MetricBase(BaseModel):
    """Base model for metric data."""

    name: str = Field(..., min_length=1, max_length=100, description="Metric name")
    description: Optional[str] = Field(None, description="Metric description")
    event_name: str = Field(
        ..., min_length=1, max_length=100, description="Event name to track"
    )
    metric_type: MetricType = Field(
        MetricType.CONVERSION, description=METRIC_TYPE_DESCRIPTION
    )
    is_primary: bool = Field(False, description="Whether this is the primary metric")
    aggregation_method: str = Field(
        "average", max_length=50, description=AGGREGATION_METHOD_DESCRIPTION
    )
    minimum_sample_size: int = Field(
        100,
        ge=10,
        le=INT32_MAX,
        description="Minimum sample size for statistical significance",
    )
    expected_effect: Optional[float] = Field(
        None,
        description=(
            "The minimum detectable effect the results page plans the sample "
            "for, when this is the primary metric: a relative change as a "
            "fraction (0.10 means 5% -> 5.5%). Used only when above 0 and "
            "below 1; otherwise the plan uses 0.05."
        ),
    )
    event_value_path: Optional[str] = Field(
        None,
        max_length=100,
        description=EVENT_VALUE_PATH_DESCRIPTION,
    )
    lower_is_better: bool = Field(False, description="Whether lower values are better")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Purchase Conversion",
                "description": "Percentage of users who complete a purchase",
                "event_name": "purchase_completed",
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 200,
                "expected_effect": 0.05,
                "lower_is_better": False,
            }
        }
    )


#: The 422 message for a metric name that repeats in one request (#1461). It
#: gives the 0-based positions of the first repeated pair and never a name, so
#: nothing from the request is echoed.
DUPLICATE_METRIC_MESSAGE = (
    "duplicate metric name; each name may appear once per experiment "
    "(first seen at metrics[{first}], repeated at metrics[{second}])"
)

#: What ``metrics`` takes on a create and an update, in their descriptions.
METRIC_NAMES_DESCRIPTION = (
    "Each name may appear once: names are compared exactly, so case and spaces "
    "count (`Revenue` and `revenue` are two metrics), and a repeated name is "
    "refused with 422."
)


def refuse_duplicate_metric_names(
    metrics: Optional[List[MetricBase]],
) -> Optional[List[MetricBase]]:
    """Refuse a metric list in which a name repeats (#1461).

    Names are compared exactly, as the unique index on (experiment, name)
    compares them: nothing is trimmed or case-folded, so a pair the database
    stores side by side (``Revenue`` and ``revenue``) is still accepted.

    The error is ``value_error`` with :data:`DUPLICATE_METRIC_MESSAGE` filled
    in for the first name that repeats, scanning in order: the position where
    it was first seen and the position where it repeats.

    Pure: it reads the list and returns it unchanged, so a second run over the
    same list gives the same answer. The create route builds
    ``ExperimentCreate`` twice, and the wizard builds it once from its own
    payload. ``None`` (an update that leaves the metrics as they are) passes.
    """
    if metrics is None:
        return None
    first_seen: Dict[str, int] = {}
    for position, metric in enumerate(metrics):
        first = first_seen.setdefault(metric.name, position)
        if first != position:
            raise PydanticCustomError(
                "value_error",
                DUPLICATE_METRIC_MESSAGE.format(first=first, second=position),
            )
    return metrics


class VariantBase(BaseModel):
    """Base model for variant data."""

    name: str = Field(..., min_length=1, max_length=100, description="Variant name")
    description: Optional[str] = Field(None, description="Variant description")
    is_control: bool = Field(False, description="Whether this is the control variant")
    traffic_allocation: int = Field(
        50,
        ge=0,
        le=100,
        description="Percentage of traffic to allocate to this variant",
    )
    configuration: Optional[Dict[str, Any]] = Field(
        None, description="Variant-specific configuration"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Treatment A",
                "description": "New button design",
                "is_control": False,
                "traffic_allocation": 50,
                "configuration": {"button_color": "blue", "button_text": "Buy Now"},
            }
        }
    )

    @field_validator("traffic_allocation")
    @classmethod
    def validate_traffic_allocation(cls, v: int) -> int:
        """Validate traffic allocation percentage."""
        if v < 0 or v > 100:
            raise ValueError("Traffic allocation must be between 0 and 100")
        return v


def _checked_targeting_rules(
    value: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Validate experiment ``targeting_rules`` on create and update.

    The value is returned as given (never rewritten), so running this twice
    gives the same result. The error text is fixed and never carries the
    submitted rules (see ``validate_experiment_targeting``).
    """
    try:
        validate_experiment_targeting(value)
    except TargetingRulesError as err:
        raise ValueError(str(err)) from None
    return value


def split_url_config_error(
    title: str, loc: Tuple[Union[str, int], ...], message: str
) -> ValidationError:
    """A refusal of ``split_url_config`` at *loc* inside it, raised from its
    field validator so the 422 names the rule's place (#393)."""
    return ValidationError.from_exception_data(
        title,
        [
            InitErrorDetails(
                type=PydanticCustomError(SPLIT_URL_CONFIG_ERROR, message),
                loc=loc,
                input=None,
            )
        ],
    )


class ExperimentBase(BaseModel):
    """Base model for experiment data."""

    name: str = Field(..., min_length=1, max_length=100, description="Experiment name")
    key: Optional[str] = Field(
        None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        description="Stable identifier used by SDKs (generated from the name when omitted)",
    )
    description: Optional[str] = Field(
        None, max_length=2000, description="Experiment description"
    )
    hypothesis: Optional[str] = Field(
        None, max_length=2000, description="Experiment hypothesis"
    )
    experiment_type: ExperimentType = Field(
        ExperimentType.A_B, description="Type of experiment"
    )
    targeting_rules: Optional[Dict[str, Any]] = Field(
        None, description="Rules for targeting users"
    )
    tags: Optional[List[str]] = Field(None, description="Tags for categorization")


class SequentialTestingConfigInput(BaseModel):
    """Configuration for enabling sequential testing on an experiment."""

    method: str = Field(
        default="msprt",
        pattern="^(msprt|always_valid)$",
        description=(
            "Sequential testing method: 'msprt'. 'always_valid' is accepted as "
            "an alias of it; the analysis reports 'msprt'."
        ),
    )
    alpha: float = Field(
        default=0.05,
        gt=0.0,
        le=0.2,
        description=(
            "Significance level of the mSPRT, above 0 and at most 0.2; the "
            "stopping boundary is 1/alpha."
        ),
    )
    tau_squared: float = Field(
        default=0.001,
        gt=0.0,
        le=1.0,
        description="mSPRT mixing parameter.",
    )
    spending_function: str = Field(
        default="obrien_fleming",
        pattern="^(obrien_fleming|pocock)$",
        description=(
            "Alpha spending function. Accepted and stored, but no "
            "alpha-spending table is computed yet."
        ),
    )
    planned_looks: int = Field(
        default=10,
        ge=1,
        le=100,
        description=(
            "Number of planned interim analyses. Accepted and stored, but no "
            "alpha-spending table is computed yet."
        ),
    )


#: The descriptions of the two stored analysis settings (#580).
CORRECTION_METHOD_DESCRIPTION = (
    "Multiple-comparison correction the results apply across the treatments "
    "of each metric: none, bonferroni or benjamini_hochberg (default). A "
    "request to the results may name another for that request only. Locked "
    "once the experiment leaves draft."
)
CONFIDENCE_LEVEL_DESCRIPTION = (
    "Confidence level the results use for significance and intervals, from "
    "0.80 to 0.99 (default 0.95). A request to the results may name another "
    "for that request only. Locked once the experiment leaves draft. CUPED "
    "follows it; sequential analysis does not."
)


class ExperimentCreate(ExperimentBase):
    """Model for creating a new experiment."""

    status: ExperimentStatus = Field(
        ExperimentStatus.DRAFT,
        description=(
            "Always `draft`: a new experiment is created as a draft, and any "
            "other value is refused with 422. Start it with "
            "`POST /api/v1/experiments/{id}/start`."
        ),
    )
    variants: List[VariantBase] = Field(
        ..., min_length=1, description="Experiment variants"
    )
    metrics: List[MetricBase] = Field(
        ...,
        min_length=1,
        description="Metrics to track, at least one. " + METRIC_NAMES_DESCRIPTION,
    )

    # EP-021: Sequential testing
    sequential_testing_enabled: bool = Field(
        default=False, description="Enable sequential testing (mSPRT early stopping)."
    )
    sequential_testing_config: Optional[SequentialTestingConfigInput] = Field(
        default=None,
        description="Sequential testing configuration.",
    )

    # Issue #21: CUPED variance reduction
    variance_reduction_config: Optional[VarianceReductionConfig] = Field(
        default=None,
        description="CUPED / Winsorization variance-reduction configuration.",
    )

    # EP-035: Bayesian analysis (#216)
    bayesian_enabled: bool = Field(
        default=False,
        description=(
            "Compute Bayesian results for this experiment. When true and "
            "bayesian_config is omitted, the default BayesianConfig is stored."
        ),
    )
    bayesian_config: Optional[BayesianConfig] = Field(
        default=None,
        description="Bayesian analysis configuration (priors, loss threshold, ROPE).",
    )

    # Issue #22: MAB optimization type
    optimization_type: OptimizationType = Field(
        default=OptimizationType.FIXED,
        description="Traffic optimization algorithm (default: fixed A/B split).",
    )

    # EP-036: Split URL testing configuration. Declared after `variants` and
    # `experiment_type`, which its validator reads.
    split_url_config: Optional[SplitUrlConfig] = Field(
        default=None,
        description="Split URL experiment configuration (required when experiment_type=split_url).",
    )

    # #580: how the results judge this experiment
    correction_method: CorrectionMethod = Field(
        default=CorrectionMethod.BENJAMINI_HOCHBERG,
        description=CORRECTION_METHOD_DESCRIPTION,
    )
    confidence_level: float = Field(
        default=0.95,
        ge=0.80,
        le=0.99,
        description=CONFIDENCE_LEVEL_DESCRIPTION,
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Button Color Test",
                "description": "Testing different button colors on the checkout page",
                "hypothesis": "Changing the button color to blue will increase conversion rates",
                "experiment_type": "a_b",
                "status": "draft",
                "targeting_rules": {
                    "logical_operator": "AND",
                    "groups": [
                        {
                            "logical_operator": "AND",
                            "conditions": [
                                {
                                    "attribute": "country",
                                    "operator": "in",
                                    "value": ["US", "CA"],
                                },
                                {
                                    "attribute": "device",
                                    "operator": "in",
                                    "value": ["desktop", "mobile"],
                                },
                            ],
                        }
                    ],
                },
                "tags": ["checkout", "ui", "conversion"],
                "variants": [
                    {
                        "name": "Control",
                        "description": "Current green button",
                        "is_control": True,
                        "traffic_allocation": 50,
                        "configuration": {"button_color": "green"},
                    },
                    {
                        "name": "Treatment",
                        "description": "New blue button",
                        "is_control": False,
                        "traffic_allocation": 50,
                        "configuration": {"button_color": "blue"},
                    },
                ],
                "metrics": [
                    {
                        "name": "Conversion Rate",
                        "description": "Percentage of users who complete a purchase",
                        "event_name": "purchase",
                        "metric_type": "conversion",
                        "is_primary": True,
                    },
                    {
                        "name": "Revenue per user",
                        "description": (
                            "Each user's purchase values added up, averaged "
                            "over every assigned user"
                        ),
                        "event_name": "purchase",
                        "metric_type": "revenue",
                        "is_primary": False,
                    },
                ],
            }
        }
    )

    @field_validator("targeting_rules")
    @classmethod
    def validate_targeting_rules(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Refuse rules assignment would not apply as they read.

        Idempotent: the create route builds ``ExperimentCreate`` a second time
        from the first one's dump, and the value is returned unchanged.
        """
        return _checked_targeting_rules(value)

    @field_validator("metrics")
    @classmethod
    def validate_metric_names(cls, value: List[MetricBase]) -> List[MetricBase]:
        """Refuse a repeated metric name (#1461); the list is returned as is."""
        return refuse_duplicate_metric_names(value)

    @field_validator("split_url_config")
    @classmethod
    def validate_split_url_config(
        cls, config: Optional[SplitUrlConfig], info: ValidationInfo
    ) -> Optional[SplitUrlConfig]:
        """A split_url experiment's URL variants match its variants (#393).

        The 422 is at the rule's place inside ``split_url_config``. When
        ``variants`` was refused on its own there is nothing to compare.
        """
        experiment_type = info.data.get("experiment_type")
        variants = info.data.get("variants")
        if (
            config is None
            or variants is None
            or getattr(experiment_type, "value", experiment_type) != "split_url"
        ):
            return config
        problem = find_split_url_config_problem(config, variants)
        if problem is not None:
            raise split_url_config_error(cls.__name__, problem.loc, problem.message)
        return config

    @model_validator(mode="after")
    def default_bayesian_config(self):
        """Turning Bayesian analysis on without a config stores the defaults."""
        if self.bayesian_enabled and self.bayesian_config is None:
            self.bayesian_config = BayesianConfig()
        return self

    @model_validator(mode="after")
    def validate_variants(self):
        """Validate that there is at least one control variant."""
        if not any(variant.is_control for variant in self.variants):
            raise ValueError("At least one variant must be marked as control")

        # Check that traffic allocations sum to 100
        total_allocation = sum(variant.traffic_allocation for variant in self.variants)
        if total_allocation != 100:
            raise ValueError(
                f"Traffic allocations must sum to 100% (currently {total_allocation}%)"
            )

        return self


#: ``ExperimentUpdate`` fields stored in a NOT NULL column. An update may leave
#: any of them out, but may not set one to null (#541).
NOT_NULL_UPDATE_FIELDS = (
    "name",
    "status",
    "experiment_type",
    "sequential_testing_enabled",
    "optimization_type",
    "correction_method",
    "confidence_level",
)


#: The one message for a ``schedule`` sent to ``PUT /experiments/{id}`` (#595).
SCHEDULE_ON_UPDATE_REFUSED = (
    "schedule is not applied by this endpoint; use "
    "PUT /api/v1/experiments/{experiment_id}/schedule"
)


class ExperimentUpdate(BaseModel):
    """Model for updating an experiment.

    Every field is optional: a field left out is not changed. `name`,
    `status`, `experiment_type`, `sequential_testing_enabled`,
    `optimization_type`, `correction_method` and `confidence_level` cannot be
    set to null; a request that does is refused with 422.
    """

    # The NOT_NULL_UPDATE_FIELDS default to None only to mean "not sent":
    # pydantic does not validate a default, so leaving one out is accepted,
    # while an explicit null reaches ``refuse_null`` below.
    name: str = Field(None, min_length=1, max_length=100, description="Experiment name")
    description: Optional[str] = Field(
        None, max_length=2000, description="Experiment description"
    )
    hypothesis: Optional[str] = Field(
        None, max_length=2000, description="Experiment hypothesis"
    )
    status: ExperimentStatus = Field(
        None,
        description=(
            "Not changed by an update. A value equal to the current status is "
            "accepted and changes nothing; any other value is refused with 422. "
            "Status changes through `POST /api/v1/experiments/{id}/start`, "
            "`/pause`, `/complete` or `/archive`."
        ),
    )
    experiment_type: ExperimentType = Field(None, description="Type of experiment")
    targeting_rules: Optional[Dict[str, Any]] = Field(
        None, description="Rules for targeting users"
    )
    tags: Optional[List[str]] = Field(None, description="Tags for categorization")
    variants: Optional[List[VariantBase]] = Field(
        None, description="Experiment variants"
    )
    metrics: Optional[List[MetricBase]] = Field(
        None,
        min_length=1,
        description=(
            "Metrics to track, replacing the stored ones: at least one, so an "
            "empty list is refused with 422. "
            + METRIC_NAMES_DESCRIPTION
            + " Left out or null, the metrics are not changed. Changeable only "
            "while the experiment is a draft."
        ),
    )
    # Declared only so a request carrying it is refused (#595). Without the
    # declaration pydantic's default ``extra="ignore"`` would drop the key and
    # answer 200, which is the defect: the update never applied a schedule.
    schedule: Optional[ScheduleConfig] = Field(
        None,
        description=(
            "Not accepted. A request that contains `schedule`, with any value "
            "including null, is refused with 422. Schedule an experiment with "
            "`PUT /api/v1/experiments/{experiment_id}/schedule`."
        ),
        json_schema_extra={"deprecated": True},
    )

    # EP-021: Sequential testing
    sequential_testing_enabled: bool = Field(
        default=None, description="Enable or disable sequential testing."
    )
    sequential_testing_config: Optional[SequentialTestingConfigInput] = Field(
        default=None,
        description="Sequential testing configuration.",
    )

    # Issue #21: CUPED variance reduction
    variance_reduction_config: Optional[VarianceReductionConfig] = Field(
        default=None,
        description="CUPED / Winsorization variance-reduction configuration.",
    )

    # EP-035: Bayesian analysis (#216). bayesian_decision is not accepted:
    # it is computed by the analysis, never set by a client.
    bayesian_enabled: Optional[bool] = Field(
        default=None,
        description=(
            "Turn Bayesian analysis on or off. Turning it on with no stored or "
            "supplied bayesian_config stores the default BayesianConfig."
        ),
    )
    bayesian_config: Optional[BayesianConfig] = Field(
        default=None,
        description="Bayesian analysis configuration (priors, loss threshold, ROPE).",
    )

    # Issue #22: MAB optimization type
    optimization_type: OptimizationType = Field(
        default=None,
        description="Traffic optimization algorithm.",
    )

    # EP-036: Split URL testing configuration
    split_url_config: Optional[SplitUrlConfig] = Field(
        default=None,
        description="Split URL experiment configuration.",
    )

    # #580: how the results judge this experiment; draft only
    correction_method: CorrectionMethod = Field(
        default=None,
        description=CORRECTION_METHOD_DESCRIPTION
        + " Changeable only while the experiment is a draft; cannot be null.",
    )
    confidence_level: float = Field(
        default=None,
        ge=0.80,
        le=0.99,
        description=CONFIDENCE_LEVEL_DESCRIPTION
        + " Changeable only while the experiment is a draft; cannot be null.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Updated Button Color Test",
                "description": "Testing different button colors with updated hypothesis",
                "hypothesis": "Changing the button color to dark blue will increase conversion rates",
                "tags": ["checkout", "ui", "conversion", "updated"],
            }
        }
    )

    @field_validator(*NOT_NULL_UPDATE_FIELDS, mode="before")
    @classmethod
    def refuse_null(cls, value: Any, info: ValidationInfo) -> Any:
        """Refuse an explicit null for a field stored in a NOT NULL column."""
        if value is None:
            raise PydanticCustomError(
                "null_not_allowed", f"{info.field_name} cannot be null"
            )
        return value

    @field_validator("schedule", mode="before")
    @classmethod
    def refuse_schedule(cls, value: Any) -> Any:
        """Refuse ``schedule`` on presence, whatever its value (#595).

        A ``before`` validator runs before ``ScheduleConfig`` is validated, so
        a valid schedule, an invalid one, ``{}`` and null all get this one
        message, and nothing in it comes from the value. It does not run when
        the key is absent: pydantic does not validate a default.
        """
        raise PydanticCustomError("value_error", SCHEDULE_ON_UPDATE_REFUSED)

    @field_validator("targeting_rules")
    @classmethod
    def validate_targeting_rules(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Refuse rules assignment would not apply as they read."""
        return _checked_targeting_rules(value)

    @field_validator("metrics")
    @classmethod
    def validate_metric_names(
        cls, value: Optional[List[MetricBase]]
    ) -> Optional[List[MetricBase]]:
        """Refuse a repeated metric name (#1461), as a create does.

        Runs after ``min_length``, so an empty list keeps the ``too_short``
        error a create gives it; null passes and changes nothing.
        """
        return refuse_duplicate_metric_names(value)

    @model_validator(mode="after")
    def validate_variants(self):
        """Validate variants if provided."""
        if self.variants is not None:
            if not any(variant.is_control for variant in self.variants):
                raise ValueError("At least one variant must be marked as control")

            # Check that traffic allocations sum to 100
            total_allocation = sum(
                variant.traffic_allocation for variant in self.variants
            )
            if total_allocation != 100:
                raise ValueError(
                    f"Traffic allocations must sum to 100% (currently {total_allocation}%)"
                )

        return self


class VariantResponse(BaseModel):
    """Model for variant response data."""

    id: UUID4
    name: str
    description: Optional[str] = None
    is_control: bool
    traffic_allocation: int
    configuration: Optional[Dict[str, Any]] = None
    experiment_id: UUID4
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MetricResponse(BaseModel):
    """Model for metric response data."""

    id: UUID4
    name: str
    description: Optional[str] = None
    event_name: str
    metric_type: str = Field(..., description=METRIC_TYPE_DESCRIPTION)
    is_primary: bool
    aggregation_method: str = Field(..., description=AGGREGATION_METHOD_DESCRIPTION)
    minimum_sample_size: int
    expected_effect: Optional[float] = None
    event_value_path: Optional[str] = Field(
        None, description=EVENT_VALUE_PATH_DESCRIPTION
    )
    lower_is_better: bool
    experiment_id: UUID4
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExperimentResponse(BaseModel):
    """Model for experiment response data."""

    id: UUID4
    name: str
    key: Optional[str] = None
    description: Optional[str] = None
    hypothesis: Optional[str] = None
    experiment_type: str
    status: str
    targeting_rules: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None
    # Null once the creator's account has been removed (the column is
    # ON DELETE SET NULL): such an experiment still reads and lists, with
    # `owner_id: null`.
    owner_id: Optional[UUID4] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    resume_at: Optional[datetime] = Field(
        None,
        description=(
            "When a PAUSED experiment is scheduled to resume, set by PUT "
            "/experiments/{id}/schedule on a paused experiment. Null when no "
            "resume is scheduled; any change of status clears it."
        ),
    )
    created_at: datetime
    updated_at: datetime
    variants: List[VariantResponse]
    metrics: List[MetricResponse]

    # EP-021: Sequential testing
    sequential_testing_enabled: bool = False
    sequential_testing_method: Optional[str] = None
    sequential_testing_config: Optional[Dict[str, Any]] = None

    # EP-022: Mutual exclusion group
    mutual_exclusion_group_id: Optional[UUID4] = None

    # Issue #21: CUPED variance reduction
    variance_reduction_config: Optional[Dict[str, Any]] = None

    # EP-035: Bayesian analysis (#216)
    bayesian_enabled: bool = False
    bayesian_config: Optional[Dict[str, Any]] = None
    bayesian_decision: Optional[str] = Field(
        None,
        description=(
            "The latest Bayesian stopping recommendation (CONTINUE, STOP_WINNER, "
            "STOP_EQUIVALENT or STOP_FUTILE), computed by the analysis. Read-only: "
            "nothing stops the experiment on it."
        ),
    )

    # Issue #22: MAB optimization type
    optimization_type: OptimizationType = OptimizationType.FIXED

    # Free-form notes/insights (POST /experiments/{id}/metadata)
    experiment_metadata: Optional[Dict[str, Any]] = None

    # EP-036: Split URL testing configuration
    split_url_config: Optional[Dict[str, Any]] = None

    # #580: how the results judge this experiment. The defaults are what the
    # migration backfilled; ExperimentService._experiment_to_dict copies the
    # stored values, so a response never reports a default over a choice.
    correction_method: CorrectionMethod = Field(
        default=CorrectionMethod.BENJAMINI_HOCHBERG,
        description=CORRECTION_METHOD_DESCRIPTION,
    )
    confidence_level: float = Field(
        default=0.95, description=CONFIDENCE_LEVEL_DESCRIPTION
    )

    model_config = ConfigDict(from_attributes=True)

    @field_validator("status", "experiment_type", mode="before")
    @classmethod
    def convert_enum_to_string(cls, value):
        """Convert enum values to strings before validation."""
        if hasattr(value, "value"):
            return value.value
        return value


class ExperimentDetailResponse(ExperimentResponse):
    """The answer of ``GET /api/v1/experiments/{experiment_id}`` (#921).

    ``ExperimentResponse`` plus the owner's display name. Only that read
    carries it: the list and every other experiment route answer with
    ``ExperimentResponse``.
    """

    owner_name: Optional[str] = Field(
        None,
        description=(
            "The display name of the user who created the experiment: their "
            "full name, else their username when it contains no '@', else "
            "null. Never their email. Null when the experiment has no owner. "
            "Looked up on every read."
        ),
    )


class ExperimentListResponse(BaseModel):
    """Model for paginated experiment list response."""

    items: List[ExperimentResponse]
    total: int
    skip: int
    limit: int

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [
                    {
                        "id": "123e4567-e89b-12d3-a456-426614174000",
                        "name": "Button Color Test",
                        "description": "Testing different button colors",
                        "status": "active",
                        "total_users": 1000,
                        "created_at": "2023-01-01T00:00:00Z",
                    }
                ],
                "total": 1,
                "skip": 0,
                "limit": 100,
            }
        }
    )


class ExperimentResultMetric(BaseModel):
    """Model for experiment result metric data."""

    name: str
    control_value: float
    variant_values: Dict[str, float]
    differences: Dict[str, float]  # Difference from control
    p_values: Dict[str, float]  # Statistical significance
    significant: Dict[str, bool]  # Is the difference statistically significant
    confidence_intervals: Dict[str, List[float]]  # 95% confidence intervals

    model_config = ConfigDict(from_attributes=True)


class ExperimentResults(BaseModel):
    """Model for experiment results."""

    experiment_id: UUID4
    experiment_name: str
    status: str
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    metrics: List[ExperimentResultMetric]
    sample_sizes: Dict[str, int]
    conclusion: Optional[str] = None
    recommended_variant: Optional[str] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "experiment_id": "123e4567-e89b-12d3-a456-426614174000",
                "experiment_name": "Button Color Test",
                "status": "completed",
                "start_date": "2023-01-01T00:00:00Z",
                "end_date": "2023-01-15T00:00:00Z",
                "metrics": [
                    {
                        "name": "Conversion Rate",
                        "control_value": 0.10,
                        "variant_values": {"Treatment": 0.12},
                        "differences": {"Treatment": 0.02},
                        "p_values": {"Treatment": 0.03},
                        "significant": {"Treatment": True},
                        "confidence_intervals": {"Treatment": [0.01, 0.03]},
                    }
                ],
                "sample_sizes": {"Control": 5000, "Treatment": 5000},
                "conclusion": "The blue button significantly improved conversion rates by 20%",
                "recommended_variant": "Treatment",
            }
        }
    )
