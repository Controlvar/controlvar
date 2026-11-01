# backend/app/services/experiment_service.py
import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Union
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from backend.app.core.targeting_adapter import (
    _is_dashboard_rules_shape,
    buckets_on_rule_id,
    stored_rule_id,
)
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.schemas.bayesian import BayesianConfig
from backend.app.schemas.experiment import (
    ExperimentCreate,
    ExperimentUpdate,
    SequentialTestingConfigInput,
)
from backend.app.schemas.split_url_config import split_url_config_problem
from backend.app.schemas.variance_reduction import VarianceReductionConfig

logger = logging.getLogger(__name__)

#: Postgres error code for a unique constraint violation.
_UNIQUE_VIOLATION = "23505"

#: How many keys a create without a ``key`` generates before it gives up. The
#: random suffix has 16**6 values, so a second collision in a row is already
#: vanishingly rare; the bound keeps a broken generator from looping (#388).
KEY_GENERATION_ATTEMPTS = 3

#: The longest name an experiment can store, read from the column so the two
#: cannot drift. A clone's "Copy of <name>" is cut to it: the caller never sent
#: that name, so refusing it would refuse a value they cannot change (#627).
NAME_MAX = Experiment.__table__.c.name.type.length


def is_experiment_key_conflict(exc: BaseException) -> bool:
    """True when *exc* is the database refusing a second experiment with a key.

    Decided from the driver's structured diagnostics, never from the message
    text. The index name follows the schema (``ix_<schema>_experiments_key``),
    so it is derived from the diagnostics' own schema name rather than written
    out: the schema differs between a deployment, CI and the core build.
    Anything without those diagnostics is not recognised, which answers the
    generic message instead of the 409.
    """
    orig = getattr(exc, "orig", None)
    if orig is None or getattr(orig, "pgcode", None) != _UNIQUE_VIOLATION:
        return False
    diag = getattr(orig, "diag", None)
    if diag is None:
        return False
    schema = getattr(diag, "schema_name", None)
    return (
        getattr(diag, "table_name", None) == "experiments"
        and schema is not None
        and getattr(diag, "constraint_name", None) == f"ix_{schema}_experiments_key"
    )


def _as_utc(value: Any) -> Optional[datetime]:
    """A stored or requested time as an aware UTC datetime.

    ``start_date`` and ``end_date`` are ``timestamp without time zone`` and
    come back naive (they hold UTC); ``resume_at`` and request values are
    aware. A string (an in-memory ISO value not yet reloaded) is parsed.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def record_end_date(experiment: Experiment, now: datetime) -> None:
    """Set an experiment's ``end_date`` to *now*, the time it is completed.

    ``check_experiment_dates`` requires ``end_date > start_date``. A start
    date that is not before *now* is moved to one microsecond before it, the
    latest start the row accepts. An experiment an earlier version started
    by hand before its scheduled start holds one: that start kept the
    scheduled date, which can still be ahead (#974). The experiment has been
    running since before *now*, so the start written is nearer the real one
    than the date it replaces.
    """
    start_date = _as_utc(experiment.start_date)
    if start_date is not None and start_date >= now:
        experiment.start_date = now - timedelta(microseconds=1)
    experiment.end_date = now


def _resolve_enum_member(enum_cls, value: Any):
    """
    Resolve *value* to a member of *enum_cls* by value or by name.

    Callers hand us any of three spellings for the same thing: the enum member
    itself (``ExperimentStatus.DRAFT``), its *value* (``"draft"`` — what the
    REST API and the dashboard send) or its *name* (``"DRAFT"`` — what the
    database stores).  Matching is case-insensitive and value-first, because
    the two differ for members like ``ExperimentType.MULTIVARIATE`` whose
    value is ``"mv"``.

    Args:
        enum_cls: The enum class to resolve against.
        value: Member, value or name.

    Returns:
        The matching member, or ``None`` when nothing matches.
    """
    if isinstance(value, enum_cls):
        return value
    if value is None:
        return None

    text = str(getattr(value, "value", value)).strip().lower()
    if not text:
        return None

    for member in enum_cls:
        if str(member.value).lower() == text or member.name.lower() == text:
            return member
    return None


def resolve_experiment_status(value: Any) -> Optional[ExperimentStatus]:
    """Return the ``ExperimentStatus`` for *value*, or ``None`` if unknown.

    ``Experiment.status`` is a SQLAlchemy ``Enum(ExperimentStatus)``, which
    stores enum *names* (``DRAFT``).  Filtering with the raw string the client
    sent (``"draft"``) passes straight through to SQL and matches no rows, so
    every status filter must go through this helper first.
    """
    return _resolve_enum_member(ExperimentStatus, value)


def resolve_experiment_type(value: Any) -> Optional[ExperimentType]:
    """Return the ``ExperimentType`` for *value*, or ``None`` if unknown."""
    return _resolve_enum_member(ExperimentType, value)


#: The refusal of a start whose start date would not be before the end date
#: (#953). A fixed sentence: the route answers it as the 400's ``detail``.
END_DATE_PASSED = (
    "Cannot start experiment: its end date has passed. Set a later end date, "
    "or clear it, with PUT /api/v1/experiments/{id}/schedule, then start it again."
)


class EndDatePassedError(ValueError):
    """Starting would stamp a start date at or after the stored end date.

    ``check_experiment_dates`` (``end_date > start_date``) would refuse the
    row; the start is refused before anything is written instead (#953).
    """

    def __init__(self) -> None:
        super().__init__(END_DATE_PASSED)


class AnalysisConfigError(ValueError):
    """A create/update would leave an analysis config in a state it refuses.

    ``field`` is the request field at fault; the API answers 422 naming it.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


#: How each ``metric_type`` a request may send is stored. This is the one
#: place the request schema's ``MetricType`` meets the model's; a value with
#: no entry is refused, never stored as something else (#558).
#: ``test_experiment_metric_types`` holds the keys to the schema's values.
METRIC_TYPE_STORED_AS: Dict[str, MetricType] = {
    "conversion": MetricType.CONVERSION,
    "revenue": MetricType.REVENUE,
    "count": MetricType.COUNT,
    "duration": MetricType.DURATION,
    "custom": MetricType.CUSTOM,
}

#: The refusal for a metric type with no entry above. Fixed text: it lists
#: the accepted values and does not repeat the one sent.
UNKNOWN_METRIC_TYPE_MESSAGE = "metric_type must be one of: " + ", ".join(
    METRIC_TYPE_STORED_AS
)


def stored_metric_type(value: Any) -> MetricType:
    """The model ``MetricType`` a metric sent with *value* is stored as.

    *value* is a model member, a schema member, or its value (any case). A
    missing value is ``conversion``, the schema's default.

    Raises:
        AnalysisConfigError: *value* has no entry in ``METRIC_TYPE_STORED_AS``.
            The update route answers it with 422 on ``metrics``.
    """
    if value is None:
        return MetricType.CONVERSION
    if isinstance(value, MetricType):
        return value
    key = str(getattr(value, "value", value)).strip().lower()
    stored = METRIC_TYPE_STORED_AS.get(key)
    if stored is None:
        raise AnalysisConfigError("metrics", UNKNOWN_METRIC_TYPE_MESSAGE)
    return stored


def _with_stored_metric_types(
    metrics_data: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """*metrics_data* with each ``metric_type`` resolved for storage.

    Create and update call this before they add or change any row.
    """
    return [
        {**metric, "metric_type": stored_metric_type(metric.get("metric_type"))}
        for metric in metrics_data
    ]


BAYESIAN_CONFIG_CLEAR_MESSAGE = (
    "bayesian_config cannot be cleared while bayesian_enabled is true; "
    "send bayesian_enabled: false to disable"
)


# The JSONB analysis-configuration columns and the schema each one holds.
_ANALYSIS_CONFIG_SCHEMAS: Dict[str, type] = {
    "bayesian_config": BayesianConfig,
    "sequential_testing_config": SequentialTestingConfigInput,
    "variance_reduction_config": VarianceReductionConfig,
}


def _normalise_analysis_configs(
    data: Dict[str, Any], experiment: Optional[Experiment] = None
) -> Dict[str, Any]:
    """Prepare the analysis-configuration fields of a create/update for storage.

    Each JSONB config is stored as ``model_dump(mode="json")`` of its schema, so
    enums become their values and every default is written out.  Turning
    Bayesian analysis on with no config -- neither in *data* nor already stored
    on *experiment* -- stores the default ``BayesianConfig``, because the
    analysis only runs for an experiment that has both.  An explicit
    ``bayesian_enabled: null`` on an update is dropped rather than written to a
    non-nullable column.

    On an update (*experiment* given), an explicit ``bayesian_config: null``
    while Bayesian analysis stays on raises :class:`AnalysisConfigError`
    rather than silently replacing the stored config with the defaults.
    Clearing it together with ``bayesian_enabled: false`` is allowed.

    Raises:
        AnalysisConfigError: the update would clear the config of an enabled
            experiment.
    """
    for field, schema in _ANALYSIS_CONFIG_SCHEMAS.items():
        value = data.get(field)
        if isinstance(value, BaseModel):
            data[field] = value.model_dump(mode="json")
        elif isinstance(value, dict):
            data[field] = schema.model_validate(value).model_dump(mode="json")

    # The correction method is stored as its value ('benjamini_hochberg'),
    # never as an enum's repr (#580).
    method = data.get("correction_method")
    if method is not None:
        data["correction_method"] = getattr(method, "value", method)

    if "bayesian_enabled" in data and data["bayesian_enabled"] is None:
        del data["bayesian_enabled"]

    enabled = data.get(
        "bayesian_enabled",
        bool(getattr(experiment, "bayesian_enabled", False)),
    )
    config = (
        data["bayesian_config"]
        if "bayesian_config" in data
        else getattr(experiment, "bayesian_config", None)
    )
    clears_config = "bayesian_config" in data and data["bayesian_config"] is None
    if enabled and clears_config and experiment is not None:
        raise AnalysisConfigError("bayesian_config", BAYESIAN_CONFIG_CLEAR_MESSAGE)
    if enabled and not config:
        data["bayesian_config"] = BayesianConfig().model_dump(mode="json")
    return data


# --- each experiment's partial rollout buckets on its own id (#533) ------------
#
# The rules engine admits a user to a rule with a partial ``rollout_percentage``
# when ``md5("<user_id>:<rule id>") % 100`` is below it. A dashboard-shaped
# rule with no top-level ``id`` gets the shared id ``"dashboard"``, so every
# such experiment admitted the same users. An experiment's rule is therefore
# given the experiment's own id when that id starts to decide who is admitted:
#
# * on DRAFT -> ACTIVE (``start_experiment`` and the scheduler; never on a
#   resume from PAUSED, so an experiment already running keeps the users it
#   admits on ``"dashboard"``);
# * on a PAUSED edit that turns a rule admitting everyone it matches into one
#   admitting part of them (people already assigned keep their variant: an
#   existing assignment is returned before targeting is evaluated);
# * and a later ``PUT`` whose rule carries no id keeps the stored one.
#
# Only stored dashboard rules are changed; flags never bucket on a rule id.


def stamp_rollout_rule_id(
    experiment: Experiment, previous_status: Optional[ExperimentStatus]
) -> bool:
    """Give a starting experiment's partial-rollout rule the experiment's id.

    Called on every write of ``ExperimentStatus.ACTIVE``, with the status the
    experiment had before it. Changes ``targeting_rules`` only when the
    experiment is leaving DRAFT and its stored dashboard rule admits part of
    the users it matches (:func:`buckets_on_rule_id`) with no id of its own.

    Returns:
        Whether ``targeting_rules`` was changed. The caller commits.
    """
    if previous_status != ExperimentStatus.DRAFT:
        return False
    rules = experiment.targeting_rules
    if not buckets_on_rule_id(rules) or stored_rule_id(rules) is not None:
        return False
    # A new dict, so the JSONB column is seen as changed.
    experiment.targeting_rules = {**rules, "id": str(experiment.id)}
    return True


def rules_with_rule_id(experiment: Experiment, new_rules: Any) -> Any:
    """The ``targeting_rules`` an update stores, given the ones it sent.

    A dashboard rule sent without an ``id`` keeps the stored rule's id, so
    saving the rules again never changes which users a partial rollout
    admits. With no stored id, a PAUSED experiment whose stored rule admitted
    everyone it matched, and whose new rule admits only part of them, gets the
    experiment's id now: it already ran, so this edit is the moment the id
    starts to decide. Anything else is returned unchanged.
    """
    if (
        not isinstance(new_rules, dict)
        or not _is_dashboard_rules_shape(new_rules)
        or not new_rules.get("groups")
        or stored_rule_id(new_rules) is not None
    ):
        return new_rules
    stored = experiment.targeting_rules
    inherited = stored_rule_id(stored)
    if inherited is not None:
        return {**new_rules, "id": inherited}
    if (
        experiment.status == ExperimentStatus.PAUSED
        and buckets_on_rule_id(new_rules)
        and not buckets_on_rule_id(stored)
    ):
        return {**new_rules, "id": str(experiment.id)}
    return new_rules


def rules_for_clone(source: Experiment) -> Any:
    """The ``targeting_rules`` a clone of ``source`` starts with.

    The stored rules as they are, except a top-level ``id`` equal to the
    source's own id: that is the id :func:`stamp_rollout_rule_id` gave the
    source, and a clone keeping it would admit exactly the source's users
    for ever (the stamp never replaces an id). Without it, the clone is
    stamped with its own id when it first starts. Any other id was chosen by
    a caller and is kept, like every other stored value.
    """
    rules = source.targeting_rules
    if stored_rule_id(rules) == str(source.id):
        return {key: value for key, value in rules.items() if key != "id"}
    return rules


# The experiment's analysis settings, which a clone starts with (#255). Every
# one is copied as stored (deep-copied, so the clone never shares a JSON value
# with its source). ``bayesian_decision`` is not among them: it is the source's
# result, not a setting. The unit test
# ``test_every_experiment_column_is_classified_for_clone`` fails on a new
# column until it is listed here or named as one a clone does not copy.
CLONED_ANALYSIS_FIELDS = (
    "optimization_type",
    "sequential_testing_enabled",
    "sequential_testing_method",
    "sequential_testing_config",
    "variance_reduction_config",
    "bayesian_enabled",
    "bayesian_config",
    # #580: a clone is a draft, so its own settings can still be changed.
    "correction_method",
    "confidence_level",
)


class ExperimentService:
    """
    Service for managing experiments, including creation, retrieval, updates, and status changes.

    This service provides the business logic for experiment operations, including:
    - Creating new experiments with variants and metrics
    - Retrieving experiments with filtering options
    - Updating experiment details and components
    - Managing experiment lifecycle (start, pause, complete, archive)
    - Access control and permission handling
    """

    def __init__(self, db: Session):
        """Initialize with a database session."""
        self.db = db

    def get_experiment_by_id(
        self, experiment_id: Union[str, UUID]
    ) -> Optional[Dict[str, Any]]:
        """
        Get an experiment by ID with its variants and metrics.

        Args:
            experiment_id: The unique identifier of the experiment

        Returns:
            Dict containing the experiment data or None if not found
        """
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
            return None

        return self._experiment_to_dict(experiment)

    def get_experiments(
        self,
        skip: int = 0,
        limit: int = 100,
        status: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = "created_at",
        sort_order: Optional[str] = "desc",
    ) -> List[Dict[str, Any]]:
        """
        Get all experiments with optional status filter.

        Args:
            skip: Number of records to skip for pagination
            limit: Maximum number of records to return
            status: Optional status filter
            search: Optional search term to filter experiments
            sort_by: Field to sort by (created_at, updated_at, name, status)
            sort_order: Sort order (asc, desc)

        Returns:
            List of experiment dictionaries
        """
        query = self.db.query(Experiment).options(
            joinedload(Experiment.variants), joinedload(Experiment.metric_definitions)
        )

        if status:
            status_enum = resolve_experiment_status(status)
            if status_enum is None:
                logger.warning(
                    "Unknown experiment status filter %r; returning no rows", status
                )
                return []
            query = query.filter(Experiment.status == status_enum)

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Experiment.name.ilike(search_pattern),
                    Experiment.description.ilike(search_pattern),
                    Experiment.tags.contains([search]),  # For JSON array search
                )
            )

        # Apply sorting
        if sort_by and hasattr(Experiment, sort_by):
            sort_field = getattr(Experiment, sort_by)
            if sort_order and sort_order.lower() == "asc":
                query = query.order_by(sort_field.asc())
            else:
                query = query.order_by(sort_field.desc())

        experiments = query.offset(skip).limit(limit).all()
        return [self._experiment_to_dict(exp) for exp in experiments]

    def get_experiments_by_owner(
        self,
        owner_id: Union[str, UUID],
        skip: int = 0,
        limit: int = 100,
        status: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = "created_at",
        sort_order: Optional[str] = "desc",
    ) -> List[Dict[str, Any]]:
        """
        Get experiments owned by a specific user.

        Args:
            owner_id: User ID of the experiment owner
            skip: Number of records to skip for pagination
            limit: Maximum number of records to return
            status: Optional status filter
            search: Optional search term to filter experiments
            sort_by: Field to sort by (created_at, updated_at, name, status)
            sort_order: Sort order (asc, desc)

        Returns:
            List of experiment dictionaries
        """
        query = (
            self.db.query(Experiment)
            .options(
                joinedload(Experiment.variants),
                joinedload(Experiment.metric_definitions),
            )
            .filter(Experiment.owner_id == owner_id)
        )

        if status:
            status_enum = resolve_experiment_status(status)
            if status_enum is None:
                logger.warning(
                    "Unknown experiment status filter %r; returning no rows", status
                )
                return []
            query = query.filter(Experiment.status == status_enum)

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Experiment.name.ilike(search_pattern),
                    Experiment.description.ilike(search_pattern),
                    Experiment.tags.contains([search]),  # For JSON array search
                )
            )

        # Apply sorting
        if sort_by and hasattr(Experiment, sort_by):
            sort_field = getattr(Experiment, sort_by)
            if sort_order and sort_order.lower() == "asc":
                query = query.order_by(sort_field.asc())
            else:
                query = query.order_by(sort_field.desc())

        experiments = query.offset(skip).limit(limit).all()
        return [self._experiment_to_dict(exp) for exp in experiments]

    def count_experiments_by_owner(
        self,
        owner_id: Union[str, UUID],
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> int:
        """
        Count experiments owned by a specific user.

        Args:
            owner_id: User ID of the experiment owner
            status: Optional status filter
            search: Optional search term to filter experiments

        Returns:
            Count of experiments matching the criteria
        """
        query = self.db.query(func.count(Experiment.id)).filter(
            Experiment.owner_id == owner_id
        )

        if status:
            status_enum = resolve_experiment_status(status)
            if status_enum is None:
                logger.warning(
                    "Unknown experiment status filter %r; counting zero rows", status
                )
                return 0
            query = query.filter(Experiment.status == status_enum)

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Experiment.name.ilike(search_pattern),
                    Experiment.description.ilike(search_pattern),
                    Experiment.tags.contains([search]),  # For JSON array search
                )
            )

        return query.scalar()

    @staticmethod
    def generate_key(name: str) -> str:
        """Derive a unique, URL/SDK-safe experiment key from a name.

        Lower-cases the name, replaces runs of non-alphanumerics with "-", and
        appends a short random suffix so two experiments with the same name
        never collide.  Example: "Button Color Test" -> "button-color-test-3f9a2c".
        """
        import re as _re
        import uuid as _uuid

        slug = _re.sub(r"[^a-z0-9]+", "-", (name or "experiment").lower()).strip("-")[
            :80
        ]
        return f"{slug or 'experiment'}-{_uuid.uuid4().hex[:6]}"

    def _insert_with_generated_key(self, experiment: Experiment) -> None:
        """Insert *experiment* under a generated key, retrying a taken one.

        Each attempt flushes inside a savepoint, so a unique conflict on the
        key index rolls back only that attempt and the session stays usable.
        Any other refusal, or a conflict on the last attempt, is raised as is.
        """
        for attempt in range(1, KEY_GENERATION_ATTEMPTS + 1):
            experiment.key = self.generate_key(experiment.name)
            try:
                with self.db.begin_nested():
                    self.db.add(experiment)
                    self.db.flush()  # Flush to get the experiment ID
                return
            except IntegrityError as exc:
                if attempt == KEY_GENERATION_ATTEMPTS or not (
                    is_experiment_key_conflict(exc)
                ):
                    raise
                logger.info(
                    "Generated experiment key %r is taken; generating another "
                    "(attempt %d of %d)",
                    experiment.key,
                    attempt,
                    KEY_GENERATION_ATTEMPTS,
                )

    def create_experiment(
        self,
        obj_in: Union[ExperimentCreate, Dict[str, Any]],
        user_id: UUID,
    ) -> Dict[str, Any]:
        """
        Create a new experiment with variants and metrics.

        Args:
            obj_in: Experiment creation data schema
            user_id: ID of the user creating the experiment

        Returns:
            Dictionary containing the created experiment data
        """
        # Extract data from the schema, excluding unset values
        obj_data = jsonable_encoder(obj_in, exclude_unset=True)

        # Extract nested objects
        variants_data = obj_data.pop("variants", [])
        metrics_data = _with_stored_metric_types(obj_data.pop("metrics", None) or [])

        # Set default values and owner
        # Handle string status values by converting to enum ("draft" or "DRAFT")
        if "status" in obj_data and isinstance(obj_data["status"], str):
            resolved_status = resolve_experiment_status(obj_data["status"])
            if resolved_status is None:
                logger.warning(
                    "Unknown experiment status %r; defaulting to DRAFT",
                    obj_data["status"],
                )
                resolved_status = ExperimentStatus.DRAFT
            obj_data["status"] = resolved_status
        else:
            obj_data["status"] = ExperimentStatus.DRAFT

        # Handle string experiment type values by converting to enum.  Values
        # and names differ ("mv" -> MULTIVARIATE), so resolve by both.
        if "experiment_type" in obj_data and isinstance(
            obj_data["experiment_type"], str
        ):
            resolved_type = resolve_experiment_type(obj_data["experiment_type"])
            if resolved_type is None:
                logger.warning(
                    "Unknown experiment type %r; defaulting to A_B",
                    obj_data["experiment_type"],
                )
                resolved_type = ExperimentType.A_B
            obj_data["experiment_type"] = resolved_type
        else:
            obj_data["experiment_type"] = ExperimentType.A_B

        obj_data["owner_id"] = str(user_id)
        _normalise_analysis_configs(obj_data)

        # Create experiment
        experiment = Experiment(**obj_data)
        if experiment.key:
            # A key the caller chose: a conflict is theirs to hear about, so
            # it propagates unchanged (the endpoint answers 409 naming it).
            self.db.add(experiment)
            self.db.flush()  # Flush to get the experiment ID
        else:
            self._insert_with_generated_key(experiment)

        # Create variants
        for variant_data in variants_data:
            variant = Variant(**variant_data, experiment_id=experiment.id)
            self.db.add(variant)

        # Create metrics
        for metric_data in metrics_data:
            metric = Metric(**metric_data, experiment_id=experiment.id)
            self.db.add(metric)

        self.db.commit()
        self.db.refresh(experiment)

        logger.info(f"Created experiment {experiment.id}: {experiment.name}")
        return self._experiment_to_dict(experiment)

    def update_experiment(
        self,
        experiment: Experiment,
        experiment_in: Union[ExperimentUpdate, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Update an existing experiment.

        Args:
            experiment: Experiment model object to update
            experiment_in: Update data schema or dictionary

        Returns:
            Dictionary containing the updated experiment data
        """
        # Convert to dict if it's a schema
        if isinstance(experiment_in, dict):
            update_data = experiment_in
        else:
            update_data = experiment_in.model_dump(exclude_unset=True)

        # Handle string status values by converting to enum ("draft" or "DRAFT")
        if "status" in update_data and isinstance(update_data["status"], str):
            resolved_status = resolve_experiment_status(update_data["status"])
            if resolved_status is None:
                # If conversion fails, keep the existing status
                logger.warning(
                    "Unknown experiment status %r; leaving status unchanged",
                    update_data["status"],
                )
                del update_data["status"]
            else:
                update_data["status"] = resolved_status

        # Handle string experiment type values by converting to enum.  Values
        # and names differ ("mv" -> MULTIVARIATE), so resolve by both.
        if "experiment_type" in update_data and isinstance(
            update_data["experiment_type"], str
        ):
            resolved_type = resolve_experiment_type(update_data["experiment_type"])
            if resolved_type is None:
                # If conversion fails, keep the existing type
                logger.warning(
                    "Unknown experiment type %r; leaving type unchanged",
                    update_data["experiment_type"],
                )
                del update_data["experiment_type"]
            else:
                update_data["experiment_type"] = resolved_type

        _normalise_analysis_configs(update_data, experiment)

        # A rule sent without an id keeps the one its partial rollout buckets
        # on (#533). Before any attribute changes: it reads the stored rules
        # and status.
        if "targeting_rules" in update_data:
            update_data["targeting_rules"] = rules_with_rule_id(
                experiment, update_data["targeting_rules"]
            )

        # Extract nested objects if present
        variants_data = update_data.pop("variants", None)
        metrics_data = update_data.pop("metrics", None)
        if metrics_data is not None:
            # Resolved before any attribute or row below is changed.
            metrics_data = _with_stored_metric_types(metrics_data)

        # Update experiment attributes
        for field in update_data:
            if hasattr(experiment, field):
                setattr(experiment, field, update_data[field])

        # Handle variants update if provided
        if variants_data is not None:
            # Remove existing variants
            for variant in experiment.variants:
                self.db.delete(variant)
            # Delete before inserting, as for metrics below.
            self.db.flush()

            # Create new variants
            for variant_data in variants_data:
                variant = Variant(**variant_data, experiment_id=experiment.id)
                self.db.add(variant)

        # Handle metrics update if provided
        if metrics_data is not None:
            # Remove existing metrics
            for metric in experiment.metric_definitions:
                self.db.delete(metric)
            # The unit of work inserts before it deletes, so without this a
            # new metric keeping an old one's name meets the unique index on
            # (experiment, name) while the old row is still there (#557).
            self.db.flush()

            # Create new metrics
            for metric_data in metrics_data:
                metric = Metric(**metric_data, experiment_id=experiment.id)
                self.db.add(metric)

        # Update timestamp
        experiment.updated_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(experiment)

        logger.info(f"Updated experiment {experiment.id}: {experiment.name}")
        return self._experiment_to_dict(experiment)

    def start_experiment(self, experiment: Experiment) -> Dict[str, Any]:
        """
        Start an experiment by changing its status to ACTIVE.

        The start date is set to now when the experiment has none, and when
        it is a draft whose scheduled start is still ahead: a draft started by
        hand before its scheduled start records when it actually started
        (#974). A start date that has passed is kept, and so is a paused
        experiment's (when it first started).

        Args:
            experiment: Experiment model object to start

        Returns:
            Dictionary containing the updated experiment data

        Raises:
            EndDatePassedError: If the start date would be set to now and the
                end date is not after now. Nothing is written.
            ValueError: If the experiment cannot be started
        """
        # Validate experiment status
        if experiment.status not in [ExperimentStatus.DRAFT, ExperimentStatus.PAUSED]:
            raise ValueError(
                f"Cannot start experiment with status: {experiment.status}"
            )

        # Validate experiment has required components
        if not self._validate_experiment_for_start(experiment):
            raise ValueError("Experiment does not meet requirements to start")

        # A start date already stored is kept, and the row already holds it
        # before its end date, unless it is a draft's scheduled start that is
        # still ahead: the draft starts now, so that is its start date (#974).
        # A start date set to now must be before the end date (#953); the
        # same ``now`` is checked and written.
        now = datetime.now(timezone.utc)
        start_date = _as_utc(experiment.start_date)
        if (
            experiment.status == ExperimentStatus.DRAFT
            and start_date is not None
            and start_date > now
        ):
            start_date = None
        if start_date is None:
            end_date = _as_utc(experiment.end_date)
            if end_date is not None and end_date <= now:
                raise EndDatePassedError()

        # Update status and start date. Only a first start stamps the rule
        # id; a resume from PAUSED keeps the users the rule admits (#533).
        stamp_rollout_rule_id(experiment, experiment.status)
        experiment.status = ExperimentStatus.ACTIVE
        if start_date is None:
            experiment.start_date = now.isoformat()

        experiment.updated_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(experiment)

        logger.info(f"Started experiment {experiment.id}: {experiment.name}")
        return self._experiment_to_dict(experiment)

    def pause_experiment(self, experiment: Experiment) -> Dict[str, Any]:
        """
        Pause an active experiment.

        Args:
            experiment: Experiment model object to pause

        Returns:
            Dictionary containing the updated experiment data

        Raises:
            ValueError: If the experiment cannot be paused
        """
        if experiment.status != ExperimentStatus.ACTIVE:
            raise ValueError(
                f"Cannot pause experiment with status: {experiment.status}"
            )

        experiment.status = ExperimentStatus.PAUSED
        experiment.updated_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(experiment)

        logger.info(f"Paused experiment {experiment.id}: {experiment.name}")
        return self._experiment_to_dict(experiment)

    def complete_experiment(self, experiment: Experiment) -> Dict[str, Any]:
        """
        Mark an experiment as completed.

        Args:
            experiment: Experiment model object to complete

        Returns:
            Dictionary containing the updated experiment data

        Raises:
            ValueError: If the experiment cannot be completed
        """
        if experiment.status not in [ExperimentStatus.ACTIVE, ExperimentStatus.PAUSED]:
            raise ValueError(
                f"Cannot complete experiment with status: {experiment.status}"
            )

        experiment.status = ExperimentStatus.COMPLETED
        record_end_date(experiment, datetime.now(timezone.utc))
        experiment.updated_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(experiment)

        logger.info(f"Completed experiment {experiment.id}: {experiment.name}")
        return self._experiment_to_dict(experiment)

    def archive_experiment(self, experiment: Experiment) -> Dict[str, Any]:
        """
        Archive an experiment.

        Args:
            experiment: Experiment model object to archive

        Returns:
            Dictionary containing the updated experiment data
        """
        experiment.status = ExperimentStatus.ARCHIVED
        experiment.updated_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(experiment)

        logger.info(f"Archived experiment {experiment.id}: {experiment.name}")
        return self._experiment_to_dict(experiment)

    def update_experiment_schedule(
        self,
        experiment: Experiment,
        schedule: Dict[str, Any],
        fields_set: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        Update experiment scheduling configuration.

        A field the request omitted is left unchanged, on either status
        (#482); an explicit null clears it.

        On a DRAFT experiment, ``start_date`` and ``end_date`` are the times
        to activate and complete it at; a null one is no longer scheduled.

        On a PAUSED experiment, ``start_date`` is the time to resume at and is
        stored as ``resume_at``; the experiment's own ``start_date`` is never
        moved. A null ``start_date`` cancels a scheduled resume.

        ``time_zone`` is not read here: ``ScheduleConfig`` has already read
        any date without an offset in that zone and validated the name (#483).

        Args:
            experiment: Experiment model to update
            schedule: Scheduling configuration containing start_date, end_date, and time_zone
            fields_set: The fields the request actually carried. Defaults to
                the keys of ``schedule``.

        Returns:
            Dictionary containing the updated experiment data

        Raises:
            ValueError: If scheduling parameters are invalid
        """
        # Validate experiment status
        if experiment.status not in [ExperimentStatus.DRAFT, ExperimentStatus.PAUSED]:
            raise ValueError(
                f"Cannot schedule experiment with status {experiment.status.value}. "
                f"Experiment must be in DRAFT or PAUSED status."
            )

        fields = set(schedule) if fields_set is None else set(fields_set)
        if experiment.status == ExperimentStatus.PAUSED:
            self._schedule_resume(experiment, schedule, fields)
        else:
            self._schedule_draft(experiment, schedule, fields)

        experiment.updated_at = datetime.now(timezone.utc)

        # Save changes
        self.db.commit()
        self.db.refresh(experiment)

        logger.info(
            f"Updated schedule for experiment {experiment.id}: "
            f"start={experiment.start_date}, end={experiment.end_date}"
        )

        return self._experiment_to_dict(experiment)

    @staticmethod
    def _schedule_draft(
        experiment: Experiment,
        schedule: Dict[str, Any],
        fields_set: Set[str],
    ) -> None:
        """Apply PUT /schedule to a DRAFT experiment (#482).

        ``start_date`` and ``end_date`` present are written (null clears the
        scheduled activation or completion); omitted, the stored value is
        kept. The values the experiment would end up with -- the request's
        where it carries them, the stored ones otherwise -- are checked before
        anything is written, so a refusal leaves the row as it was:

        * ``end_date`` must be later than ``start_date``;
        * and at least one hour later.
        """
        start_date = (
            _as_utc(schedule.get("start_date"))
            if "start_date" in fields_set
            else _as_utc(experiment.start_date)
        )
        end_date = (
            _as_utc(schedule.get("end_date"))
            if "end_date" in fields_set
            else _as_utc(experiment.end_date)
        )

        if start_date is not None and end_date is not None:
            if end_date <= start_date:
                raise ValueError("End date must be after start date")

            # Minimum duration check
            min_duration = timedelta(hours=1)
            if end_date - start_date < min_duration:
                raise ValueError(f"Experiment must run for at least {min_duration}")

        if "start_date" in fields_set:
            experiment.start_date = schedule.get("start_date")
        if "end_date" in fields_set:
            experiment.end_date = schedule.get("end_date")

    @staticmethod
    def _schedule_resume(
        experiment: Experiment,
        schedule: Dict[str, Any],
        fields_set: Set[str],
    ) -> None:
        """Apply PUT /schedule to a PAUSED experiment (#436).

        ``start_date`` present and not null schedules a resume at that time;
        present and null cancels one; omitted leaves it as it is. ``end_date``
        present is written (null clears it); omitted leaves it as it is. The
        stored ``start_date`` is never touched.

        The values the experiment would end up with are checked before
        anything is written, so a refusal is a ValueError (400) and never a
        database constraint error:

        * ``end_date`` must be later than the stored ``start_date``;
        * with a resume scheduled, ``end_date`` must be at least one hour
          after it (the stored ``end_date`` when the request omits it).
        """
        resume_at = (
            _as_utc(schedule.get("start_date"))
            if "start_date" in fields_set
            else _as_utc(experiment.resume_at)
        )
        end_date = (
            _as_utc(schedule.get("end_date"))
            if "end_date" in fields_set
            else _as_utc(experiment.end_date)
        )
        start_date = _as_utc(experiment.start_date)

        if end_date is not None and start_date is not None and end_date <= start_date:
            raise ValueError("End date must be after the experiment's start date")

        if end_date is not None and resume_at is not None:
            min_duration = timedelta(hours=1)
            if end_date - resume_at < min_duration:
                raise ValueError(
                    f"End date must be at least {min_duration} after the resume time"
                )

        if "start_date" in fields_set:
            experiment.resume_at = resume_at
        if "end_date" in fields_set:
            experiment.end_date = schedule.get("end_date")

    def delete_experiment(self, experiment: Experiment) -> None:
        """
        Delete an experiment and all related data.

        Args:
            experiment: Experiment model object to delete
        """
        # Get experiment ID for logging
        experiment_id = str(experiment.id)
        experiment_name = experiment.name

        # Delete the experiment (cascades to variants and metrics)
        self.db.delete(experiment)
        self.db.commit()

        logger.info(f"Deleted experiment {experiment_id}: {experiment_name}")

    def clone_experiment(
        self, experiment: Experiment, user_id: Union[str, UUID]
    ) -> Dict[str, Any]:
        """
        Create a clone of an existing experiment.

        Args:
            experiment: Experiment model object to clone
            user_id: ID of the user creating the clone

        Returns:
            Dictionary containing the cloned experiment data
        """
        # Create new experiment object with copied data
        new_experiment = Experiment(
            name=f"Copy of {experiment.name}"[:NAME_MAX],
            description=experiment.description,
            hypothesis=experiment.hypothesis,
            experiment_type=experiment.experiment_type,
            # Copied as stored and not validated, on purpose: stored rules are
            # not re-judged. Only the rule id the server stamped from the
            # source's own id is dropped, so the clone gets its own (#533).
            targeting_rules=rules_for_clone(experiment),
            status=ExperimentStatus.DRAFT,
            owner_id=str(user_id),
            tags=experiment.tags,
            **{
                field: copy.deepcopy(getattr(experiment, field))
                for field in CLONED_ANALYSIS_FIELDS
            },
        )

        # A key of its own, generated as create does and with the same bounded
        # retry: the tracking API finds an experiment by key, so a clone with
        # none could never be assigned (#609).
        self._insert_with_generated_key(new_experiment)

        # Clone variants
        for variant in experiment.variants:
            new_variant = Variant(
                name=variant.name,
                description=variant.description,
                is_control=variant.is_control,
                traffic_allocation=variant.traffic_allocation,
                configuration=variant.configuration,
                experiment_id=new_experiment.id,
            )
            self.db.add(new_variant)

        # Clone metrics (copy every definition column; Metric has no event_type)
        for metric in experiment.metric_definitions:
            new_metric = Metric(
                name=metric.name,
                description=metric.description,
                event_name=metric.event_name,
                metric_type=metric.metric_type,
                is_primary=metric.is_primary,
                aggregation_method=metric.aggregation_method,
                minimum_sample_size=metric.minimum_sample_size,
                expected_effect=metric.expected_effect,
                event_value_path=metric.event_value_path,
                lower_is_better=metric.lower_is_better,
                experiment_id=new_experiment.id,
            )
            self.db.add(new_metric)

        self.db.commit()
        self.db.refresh(new_experiment)

        logger.info(
            f"Cloned experiment {experiment.id} to {new_experiment.id}: {new_experiment.name}"
        )
        return self._experiment_to_dict(new_experiment)

    def search_experiments(
        self,
        search_term: str,
        owner_id: Optional[Union[str, UUID]] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Search experiments by name, description, or tags.

        Args:
            search_term: Text to search for
            owner_id: Optional filter by owner
            status: Optional filter by status
            skip: Number of records to skip for pagination
            limit: Maximum number of records to return

        Returns:
            List of matching experiment dictionaries
        """
        # Basic search query
        query = self.db.query(Experiment).options(
            joinedload(Experiment.variants), joinedload(Experiment.metric_definitions)
        )

        # Add search conditions
        if search_term:
            search_pattern = f"%{search_term}%"
            query = query.filter(
                or_(
                    Experiment.name.ilike(search_pattern),
                    Experiment.description.ilike(search_pattern),
                    Experiment.tags.contains([search_term]),  # For JSON array search
                )
            )

        # Add optional filters
        if owner_id:
            query = query.filter(Experiment.owner_id == owner_id)

        if status:
            status_enum = resolve_experiment_status(status)
            if status_enum is None:
                logger.warning(
                    "Unknown experiment status filter %r; returning no rows", status
                )
                return []
            query = query.filter(Experiment.status == status_enum)

        # Execute query with pagination
        experiments = query.offset(skip).limit(limit).all()
        return [self._experiment_to_dict(exp) for exp in experiments]

    def count_search_results(
        self,
        search_term: str,
        owner_id: Optional[Union[str, UUID]] = None,
        status: Optional[str] = None,
    ) -> int:
        """
        Count search results for pagination.

        Args:
            search_term: Text to search for
            owner_id: Optional filter by owner
            status: Optional filter by status

        Returns:
            Count of matching experiments
        """
        # Basic count query
        query = self.db.query(func.count(Experiment.id))

        # Add search conditions
        if search_term:
            search_pattern = f"%{search_term}%"
            query = query.filter(
                or_(
                    Experiment.name.ilike(search_pattern),
                    Experiment.description.ilike(search_pattern),
                    Experiment.tags.contains([search_term]),  # For JSON array search
                )
            )

        # Add optional filters
        if owner_id:
            query = query.filter(Experiment.owner_id == owner_id)

        if status:
            status_enum = resolve_experiment_status(status)
            if status_enum is None:
                logger.warning(
                    "Unknown experiment status filter %r; counting zero rows", status
                )
                return 0
            query = query.filter(Experiment.status == status_enum)

        return query.scalar()

    def _validate_experiment_for_start(self, experiment: Experiment) -> bool:
        """
        Validate that an experiment meets requirements to start.

        Args:
            experiment: Experiment to validate

        Returns:
            Boolean indicating if experiment can be started
        """
        # Check if experiment has variants
        if not experiment.variants or len(experiment.variants) < 2:
            logger.warning(
                f"Experiment {experiment.id} does not have enough variants to start"
            )
            return False

        # Check if experiment has at least one control variant
        has_control = any(variant.is_control for variant in experiment.variants)
        if not has_control:
            logger.warning(
                f"Experiment {experiment.id} does not have a control variant"
            )
            return False

        # Check if experiment has metrics
        if not experiment.metric_definitions or len(experiment.metric_definitions) < 1:
            logger.warning(
                f"Experiment {experiment.id} does not have any metrics defined"
            )
            return False

        # A split URL experiment needs a configuration whose URL variants
        # match its variants (#393). The checks against other experiments are
        # start's alone.
        if getattr(experiment, "experiment_type", None) == ExperimentType.SPLIT_URL:
            if not experiment.split_url_config:
                logger.warning(f"Experiment {experiment.id} has no split_url_config")
                return False
            if (
                split_url_config_problem(
                    experiment.split_url_config, experiment.variants
                )
                is not None
            ):
                logger.warning(
                    f"Experiment {experiment.id} has a split_url_config that is "
                    "not valid"
                )
                return False

        # All checks passed
        return True

    def _parse_datetime(self, date_str: str, time_zone: str = "UTC") -> datetime:
        """
        Parse datetime string with time zone awareness.

        Args:
            date_str: Date string in ISO format
            time_zone: Time zone name (default: UTC)

        Returns:
            Timezone-aware datetime object

        Raises:
            ValueError: If the date string is invalid or time zone is unknown
        """
        try:
            # Parse the date string
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))

            # Convert to UTC for storage
            if time_zone != "UTC":
                import pytz

                try:
                    tz = pytz.timezone(time_zone)
                    dt = tz.localize(dt.replace(tzinfo=None))
                    dt = dt.astimezone(pytz.UTC)
                except Exception as e:
                    logger.warning(
                        f"Unknown time zone: {time_zone}, using UTC. Error: {e!s}"
                    )

            return dt
        except ValueError as e:
            logger.error(f"Error parsing date: {date_str}, {e!s}")
            raise ValueError(f"Invalid date format: {date_str}")

    def to_response_dict(self, experiment: Experiment) -> Dict[str, Any]:
        """Serialise an Experiment for ExperimentResponse (metrics come from
        the metric_definitions relationship, not the JSONB `metrics` column)."""
        return self._experiment_to_dict(experiment)

    def _experiment_to_dict(self, experiment: Experiment) -> Dict[str, Any]:
        """
        Convert an experiment model to a dictionary for API responses.

        Args:
            experiment: Experiment model object

        Returns:
            Dictionary representation of the experiment
        """
        # Convert the experiment to a dictionary
        result = {
            "id": str(experiment.id),
            "name": experiment.name,
            "description": experiment.description,
            "hypothesis": experiment.hypothesis,
            "status": (
                experiment.status.value
                if hasattr(experiment.status, "value")
                else experiment.status
            ),
            "experiment_type": experiment.experiment_type,
            "targeting_rules": experiment.targeting_rules,
            "owner_id": str(experiment.owner_id) if experiment.owner_id else None,
            "created_at": (
                experiment.created_at.isoformat()
                if hasattr(experiment.created_at, "isoformat")
                else experiment.created_at
            ),
            "updated_at": (
                experiment.updated_at.isoformat()
                if hasattr(experiment.updated_at, "isoformat")
                else experiment.updated_at
            ),
            "start_date": experiment.start_date,
            "end_date": experiment.end_date,
            "resume_at": experiment.resume_at,
            "tags": experiment.tags or [],
            "key": experiment.key,
            "experiment_metadata": experiment.experiment_metadata or {},
            "split_url_config": experiment.split_url_config,
            # Issue #197: every column ExperimentResponse declares with a
            # default must be copied here, or the response reports the
            # default instead of the stored value (a bandit read as "fixed").
            "optimization_type": experiment.optimization_type or "fixed",
            "sequential_testing_enabled": bool(experiment.sequential_testing_enabled),
            "sequential_testing_method": experiment.sequential_testing_method,
            "sequential_testing_config": experiment.sequential_testing_config,
            "variance_reduction_config": experiment.variance_reduction_config,
            "mutual_exclusion_group_id": (
                str(experiment.mutual_exclusion_group_id)
                if experiment.mutual_exclusion_group_id
                else None
            ),
            # Issue #216: Bayesian analysis; bayesian_decision is response-only.
            "bayesian_enabled": bool(experiment.bayesian_enabled),
            "bayesian_config": experiment.bayesian_config,
            "bayesian_decision": experiment.bayesian_decision,
            # #580: the stored analysis settings (see Issue #197 above).
            "correction_method": experiment.correction_method,
            "confidence_level": experiment.confidence_level,
        }

        # Add variants if loaded
        if hasattr(experiment, "variants") and experiment.variants is not None:
            result["variants"] = [
                {
                    "id": str(v.id),
                    "name": v.name,
                    "description": v.description,
                    "is_control": v.is_control,
                    "traffic_allocation": v.traffic_allocation,
                    "configuration": v.configuration,
                    "experiment_id": str(v.experiment_id),
                    "created_at": (
                        v.created_at.isoformat()
                        if hasattr(v.created_at, "isoformat")
                        else v.created_at
                    ),
                    "updated_at": (
                        v.updated_at.isoformat()
                        if hasattr(v.updated_at, "isoformat")
                        else v.updated_at
                    ),
                }
                for v in experiment.variants
            ]
        else:
            result["variants"] = []

        # Add metrics if loaded
        if (
            hasattr(experiment, "metric_definitions")
            and experiment.metric_definitions is not None
        ):
            result["metrics"] = [
                {
                    "id": str(m.id),
                    "name": m.name,
                    "description": m.description,
                    "event_name": m.event_name,
                    "metric_type": m.metric_type.value
                    if hasattr(m.metric_type, "value")
                    else m.metric_type,
                    "is_primary": m.is_primary,
                    "aggregation_method": m.aggregation_method,
                    "minimum_sample_size": m.minimum_sample_size,
                    "expected_effect": m.expected_effect,
                    "event_value_path": m.event_value_path,
                    "lower_is_better": m.lower_is_better,
                    "experiment_id": str(m.experiment_id),
                    "created_at": (
                        m.created_at.isoformat()
                        if hasattr(m.created_at, "isoformat")
                        else m.created_at
                    ),
                    "updated_at": (
                        m.updated_at.isoformat()
                        if hasattr(m.updated_at, "isoformat")
                        else m.updated_at
                    ),
                }
                for m in experiment.metric_definitions
            ]
        else:
            result["metrics"] = []

        return result

    def count_experiments(
        self, status: Optional[str] = None, search: Optional[str] = None
    ) -> int:
        """
        Count all experiments with optional filters.

        Args:
            status: Optional status filter
            search: Optional search term to filter experiments

        Returns:
            Count of experiments matching the criteria
        """
        query = self.db.query(func.count(Experiment.id))

        if status:
            status_enum = resolve_experiment_status(status)
            if status_enum is None:
                logger.warning(
                    "Unknown experiment status filter %r; counting zero rows", status
                )
                return 0
            query = query.filter(Experiment.status == status_enum)

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Experiment.name.ilike(search_pattern),
                    Experiment.description.ilike(search_pattern),
                    Experiment.tags.contains([search]),  # For JSON array search
                )
            )

        return query.scalar()
