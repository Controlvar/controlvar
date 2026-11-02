# Event tracking service
# backend/app/services/event_service.py
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event, EventType, normalize_event_timestamp
from backend.app.models.event_dedup_key import EventDedupKey
from backend.app.models.experiment import Experiment, Variant
from backend.app.models.feature_flag import FeatureFlag
from backend.app.schemas.tracking import EventCreate, UntaggedEventCreate
from backend.app.services.event_dedup import dedup_key

logger = logging.getLogger(__name__)

# Keys accepted by track_event()/track_events_batch() (schema field names and
# ORM column names are both understood so callers can pass either shape).
_METADATA_KEYS = ("event_metadata", "properties", "metadata")
_TIMESTAMP_KEYS = ("timestamp", "created_at")

#: The id columns an event stores, each with the table its foreign key names.
_REFERENCE_MODELS = (
    ("experiment_id", Experiment),
    ("feature_flag_id", FeatureFlag),
    ("variant_id", Variant),
)


def _to_uuid(value: Union[str, UUID, None]) -> Optional[UUID]:
    """Coerce ``value`` to a UUID (or None). Raises ValueError when invalid."""
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _to_iso_timestamp(value: Union[str, datetime, None]) -> str:
    """Serialise a timestamp for the ``events.created_at`` string column.

    Always UTC in one fixed format (``normalize_event_timestamp``), so a
    client timestamp carrying an offset compares correctly against a window
    boundary; ``None`` or ``""`` means now.  Raises ``ValueError`` for a
    string that is not ISO-8601.
    """
    if value is None or value == "":
        value = datetime.now(timezone.utc)
    return normalize_event_timestamp(value)


class EventService:
    """
    Service for handling experiment events, tracking and processing.

    This service provides functionalities for:
    - Tracking user interactions with experiments
    - Recording conversion events and metrics
    - Storing event data for analysis
    - Managing event queues and processing
    """

    def __init__(self, db: Session):
        """Initialize with a database session."""
        self.db = db

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _as_dict(
        event_data: Union[EventCreate, UntaggedEventCreate, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Accept either a Pydantic model or a plain mapping."""
        if hasattr(event_data, "model_dump"):
            return event_data.model_dump()
        return dict(event_data)

    @classmethod
    def build_event(
        cls,
        event_data: Union[EventCreate, UntaggedEventCreate, Mapping[str, Any]],
        *,
        allow_untagged: bool = False,
    ) -> Event:
        """
        Build an ``Event`` ORM object from schema-shaped or column-shaped data.

        Maps the tracking schema (``timestamp``, ``metadata``/``properties``)
        onto the ORM columns (``created_at``, ``event_metadata``) and coerces
        identifiers to UUIDs.  Raises ``ValueError`` for missing ``user_id``,
        an invalid identifier, or when neither ``experiment_id`` nor
        ``feature_flag_id`` is provided and ``allow_untagged`` is False.

        ``allow_untagged`` is True only for ``/tracking/track`` and
        ``/tracking/batch`` (#217): an event with neither id is stored as
        history and counts in no experiment's results.
        """
        data = cls._as_dict(event_data)

        user_id = data.get("user_id")
        if not user_id:
            raise ValueError("user_id is required to track an event")

        experiment_id = _to_uuid(data.get("experiment_id"))
        feature_flag_id = _to_uuid(data.get("feature_flag_id"))
        if experiment_id is None and feature_flag_id is None and not allow_untagged:
            raise ValueError("Either experiment_id or feature_flag_id must be provided")

        metadata = next(
            (data[k] for k in _METADATA_KEYS if data.get(k) is not None), None
        )
        timestamp = next((data[k] for k in _TIMESTAMP_KEYS if data.get(k)), None)

        return Event(
            event_type=data.get("event_type") or EventType.CUSTOM.value,
            event_name=data.get("event_name"),
            user_id=str(user_id),
            experiment_id=experiment_id,
            feature_flag_id=feature_flag_id,
            variant_id=_to_uuid(data.get("variant_id")),
            value=data.get("value"),
            event_metadata=metadata,
            created_at=_to_iso_timestamp(timestamp),
        )

    @staticmethod
    def _serialize(event: Event) -> Dict[str, Any]:
        """Dictionary form of an event for API responses."""
        return {
            "id": str(event.id),
            "user_id": event.user_id,
            "event_type": event.event_type,
            "event_name": event.event_name,
            "experiment_id": str(event.experiment_id) if event.experiment_id else None,
            "feature_flag_id": str(event.feature_flag_id)
            if event.feature_flag_id
            else None,
            "variant_id": str(event.variant_id) if event.variant_id else None,
            "value": event.value,
            "timestamp": event.created_at,
            "properties": event.event_metadata,
        }

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def unknown_id_fields(
        self,
        event_data: Union[EventCreate, UntaggedEventCreate, Mapping[str, Any]],
    ) -> List[str]:
        """The id fields of an event that name no stored row of their table.

        ``/tracking/events`` takes ids from the caller and asks this before
        :meth:`track_event`, so an id naming nothing answers 404 instead of
        reaching the database, which refuses the row (#400). The event is
        built first, so anything :meth:`build_event` refuses (an id that is
        not a UUID among it) still raises ``ValueError``. Fields are listed
        in the order of ``_REFERENCE_MODELS``.

        Only existence is checked, which is all the foreign keys check: no
        owner, workspace or status rule, and no rule that the variant belongs
        to the experiment.
        """
        event = self.build_event(event_data)
        unknown = []
        for field, model in _REFERENCE_MODELS:
            value = getattr(event, field)
            if value is None:
                continue
            if self.db.query(model.id).filter(model.id == value).first() is None:
                unknown.append(field)
        return unknown

    def track_event(
        self,
        event_data: Union[EventCreate, UntaggedEventCreate, Mapping[str, Any]],
        *,
        allow_untagged: bool = False,
    ) -> Event:
        """Persist a single event. Accepts an ``EventCreate`` or a mapping.

        ``allow_untagged`` is passed through to :meth:`build_event`.
        """
        try:
            event = self.build_event(event_data, allow_untagged=allow_untagged)
            self.db.add(event)
            self.db.commit()
            self.db.refresh(event)
            return event
        except Exception as e:
            # The type only: a database error's text repeats the row's
            # parameters, the user id among them.
            logger.error(f"Error tracking event: {type(e).__name__}")
            self.db.rollback()
            raise

    def track_event_once(
        self,
        event_data: Union[EventCreate, UntaggedEventCreate, Mapping[str, Any]],
        event_id: Optional[str],
        *,
        allow_untagged: bool,
    ) -> Tuple[UUID, bool, Optional[Event]]:
        """Store an event unless one with the same key is already stored (#1543).

        Returns ``(id, created, event)``:

        * ``event_id`` None: :meth:`track_event`, unchanged, and
          ``(event.id, True, event)``;
        * the key (``event_dedup.dedup_key`` over the built event's
          experiment, flag and event name, and ``event_id``) claimed now: the
          event is stored with the id the claim recorded, in the same
          transaction, and ``(id, True, event)``;
        * the key already claimed: nothing is written and
          ``(stored id, False, None)``, the stored id being the event that
          claimed it.

        The claim is ``INSERT ... ON CONFLICT DO NOTHING RETURNING``. A claim
        on a key that an uncommitted transaction holds waits for it: when that
        one commits this is a duplicate, when it rolls back this one stores.
        So a duplicate or a race raises nothing and logs nothing.
        ``allow_untagged`` is passed through to :meth:`build_event`.
        """
        if event_id is None:
            event = self.track_event(event_data, allow_untagged=allow_untagged)
            return event.id, True, event
        try:
            event = self.build_event(event_data, allow_untagged=allow_untagged)
            key = dedup_key(
                event.experiment_id,
                event.feature_flag_id,
                event.event_name or "",
                event_id,
            )
            keys = EventDedupKey.__table__
            claim_id = uuid.uuid4()
            claimed = self.db.execute(
                pg_insert(keys)
                .values(user_id=event.user_id, dedup_key=key, stored_event_id=claim_id)
                .on_conflict_do_nothing(
                    index_elements=[keys.c.user_id, keys.c.dedup_key]
                )
                .returning(keys.c.stored_event_id)
            ).scalar_one_or_none()
            if claimed is not None:
                event.id = claimed
                self.db.add(event)
                self.db.commit()
                self.db.refresh(event)
                return event.id, True, event
            stored_id = self.db.execute(
                select(keys.c.stored_event_id).where(
                    keys.c.user_id == event.user_id, keys.c.dedup_key == key
                )
            ).scalar_one()
            self.db.rollback()
            return stored_id, False, None
        except Exception as e:
            # The type only, as track_event: a database error's text repeats
            # the row's parameters, the user id among them.
            logger.error(f"Error tracking event: {type(e).__name__}")
            self.db.rollback()
            raise

    def track_conversion(
        self,
        user_id: str,
        event_name: str,
        experiment_id: Optional[Union[str, UUID]] = None,
        properties: Optional[Dict[str, Any]] = None,
        feature_flag_id: Optional[Union[str, UUID]] = None,
        value: Optional[float] = None,
    ) -> Event:
        """
        Track a conversion event for a user.

        When ``experiment_id`` is given, the user's most recent assignment in
        that experiment is looked up so the event carries the variant.
        """
        variant_id = None
        if experiment_id:
            assignment = (
                self.db.query(Assignment)
                .filter(
                    Assignment.user_id == user_id,
                    Assignment.experiment_id == _to_uuid(experiment_id),
                )
                .order_by(desc(Assignment.created_at))
                .first()
            )
            if assignment:
                variant_id = assignment.variant_id

        return self.track_event(
            {
                "user_id": user_id,
                "event_type": EventType.CONVERSION.value,
                "event_name": event_name,
                "experiment_id": experiment_id,
                "feature_flag_id": feature_flag_id,
                "variant_id": variant_id,
                "value": value,
                "properties": properties or {},
            }
        )

    def track_exposure(
        self,
        user_id: str,
        experiment_id: Union[str, UUID],
        variant_id: Union[str, UUID],
        properties: Optional[Dict[str, Any]] = None,
    ) -> Event:
        """Track an exposure event when a user is exposed to a variant."""
        return self.track_event(
            {
                "user_id": user_id,
                "event_type": EventType.EXPOSURE.value,
                "event_name": "variant_exposure",
                "experiment_id": experiment_id,
                "variant_id": variant_id,
                "properties": properties or {},
            }
        )

    def track_events_batch(
        self, events_data: Iterable[Union[EventCreate, Mapping[str, Any]]]
    ) -> List[Event]:
        """Track multiple events in a single transaction (all or nothing)."""
        try:
            events = [self.build_event(event_data) for event_data in events_data]
            self.db.add_all(events)
            self.db.commit()
            for event in events:
                self.db.refresh(event)
            return events
        except Exception as e:
            # The type only: a database error's text repeats the row's
            # parameters, the user id among them.
            logger.error(f"Error tracking events batch: {type(e).__name__}")
            self.db.rollback()
            raise

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_events_by_experiment(
        self,
        experiment_id: Union[str, UUID],
        event_type: Optional[str] = None,
        event_name: Optional[str] = None,
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get events for an experiment, newest first, with optional filters."""
        query = self.db.query(Event).filter(
            Event.experiment_id == _to_uuid(experiment_id)
        )

        if event_type:
            query = query.filter(Event.event_type == event_type)
        if event_name:
            query = query.filter(Event.event_name == event_name)
        if start_date:
            query = query.filter(Event.created_at >= _to_iso_timestamp(start_date))
        if end_date:
            query = query.filter(Event.created_at <= _to_iso_timestamp(end_date))

        events = query.order_by(desc(Event.created_at)).offset(skip).limit(limit).all()
        return [self._serialize(event) for event in events]

    def count_events_by_experiment(
        self,
        experiment_id: Union[str, UUID],
        event_type: Optional[str] = None,
        event_name: Optional[str] = None,
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
    ) -> int:
        """Count events for an experiment with optional filters."""
        query = self.db.query(func.count(Event.id)).filter(
            Event.experiment_id == _to_uuid(experiment_id)
        )

        if event_type:
            query = query.filter(Event.event_type == event_type)
        if event_name:
            query = query.filter(Event.event_name == event_name)
        if start_date:
            query = query.filter(Event.created_at >= _to_iso_timestamp(start_date))
        if end_date:
            query = query.filter(Event.created_at <= _to_iso_timestamp(end_date))

        return query.scalar() or 0

    def get_events_by_user(
        self,
        user_id: str,
        experiment_id: Optional[Union[str, UUID]] = None,
        event_type: Optional[str] = None,
        event_name: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get events for a user, newest first, with optional filters."""
        query = self.db.query(Event).filter(Event.user_id == user_id)

        if experiment_id:
            query = query.filter(Event.experiment_id == _to_uuid(experiment_id))
        if event_type:
            query = query.filter(Event.event_type == event_type)
        if event_name:
            query = query.filter(Event.event_name == event_name)

        events = query.order_by(desc(Event.created_at)).offset(skip).limit(limit).all()
        return [self._serialize(event) for event in events]

    # ------------------------------------------------------------------
    # Deletes
    # ------------------------------------------------------------------

    def delete_events_by_experiment(self, experiment_id: Union[str, UUID]) -> int:
        """Delete all events associated with an experiment; returns the count."""
        exp_id = _to_uuid(experiment_id)
        count = (
            self.db.query(func.count(Event.id))
            .filter(Event.experiment_id == exp_id)
            .scalar()
            or 0
        )

        self.db.query(Event).filter(Event.experiment_id == exp_id).delete(
            synchronize_session=False
        )
        self.db.commit()

        logger.info(f"Deleted {count} events for experiment {experiment_id}")
        return count

    def purge_old_events(self, days_to_keep: int = 90) -> int:
        """Delete events older than ``days_to_keep`` days; returns the count."""
        cutoff = _to_iso_timestamp(
            datetime.now(timezone.utc) - timedelta(days=days_to_keep)
        )

        count = (
            self.db.query(func.count(Event.id))
            .filter(Event.created_at < cutoff)
            .scalar()
            or 0
        )

        self.db.query(Event).filter(Event.created_at < cutoff).delete(
            synchronize_session=False
        )
        self.db.commit()

        logger.info(f"Purged {count} events older than {days_to_keep} days")
        return count
