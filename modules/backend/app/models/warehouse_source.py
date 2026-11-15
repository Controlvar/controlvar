"""A table or view in a customer's warehouse, mapped for analysis (#312).

A source names one table or view on a connection and says which of its
columns play which part:

* an **assignment** source: ``unit_id``, ``experiment_key``, ``variant`` and
  ``exposed_at``;
* a **metric** source: ``unit_id``, ``event_at`` and, for a mean metric,
  ``value``; with the metric type, its conversion window and an optional cap.

Only names that were checked against the warehouse's own metadata are stored
(``table_reference`` and ``column_mapping`` hold the canonical names and the
column types), and filters are structured values, never SQL text.  The
source is deleted with its connection.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declared_attr

from backend.app.core.database_config import get_schema_name
from backend.app.models.base import Base, BaseModel

SOURCE_KINDS: tuple[str, ...] = ("assignment", "metric")
METRIC_TYPES: tuple[str, ...] = ("proportion", "mean")

DEFAULT_CONVERSION_WINDOW_HOURS = 168
MAX_CONVERSION_WINDOW_HOURS = 8760


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class WarehouseSource(Base, BaseModel):
    """A mapped table or view: where exposures, or a metric's events, live."""

    __tablename__ = "warehouse_sources"

    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.warehouse_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind = Column(String(20), nullable=False)
    name = Column(String(200), nullable=False)
    #: The validated table reference: its parts, in canonical case.
    table_reference = Column(JSONB, nullable=False)
    #: Role -> the validated column name and its warehouse type.
    column_mapping = Column(JSONB, nullable=False)
    #: Structured filters ``{column, operator, value}``; never SQL text.
    filters = Column(JSONB, nullable=False, default=list)
    metric_type = Column(String(20), nullable=True)
    conversion_window_hours = Column(Integer, nullable=True)
    cap_value = Column(Double, nullable=True)
    #: Whether a decrease is the improvement for this metric (#1386).  False on
    #: every source until something sets it, which is how every analysis
    #: before this column read a metric.  The server default is load-bearing:
    #: the previous release's INSERTs do not name the column
    #: (``modules_0003_lower_is_better``).
    lower_is_better = Column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    #: When the table and columns were last checked against the warehouse.
    validated_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="SET NULL"),
        nullable=True,
    )

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint(
                "connection_id",
                "kind",
                "name",
                name="uq_warehouse_sources_connection_kind_name",
            ),
            CheckConstraint(
                _in("kind", SOURCE_KINDS), name="ck_warehouse_sources_kind"
            ),
            # JSONB would otherwise store Python None as the JSON value null.
            CheckConstraint(
                "jsonb_typeof(table_reference) = 'object'",
                name="ck_warehouse_sources_table_reference_object",
            ),
            CheckConstraint(
                "jsonb_typeof(column_mapping) = 'object'",
                name="ck_warehouse_sources_column_mapping_object",
            ),
            CheckConstraint(
                "jsonb_typeof(filters) = 'array'",
                name="ck_warehouse_sources_filters_array",
            ),
            CheckConstraint(
                f"metric_type IS NULL OR {_in('metric_type', METRIC_TYPES)}",
                name="ck_warehouse_sources_metric_type",
            ),
            # A metric source has a type and a window; an assignment source
            # has neither.
            CheckConstraint(
                "(kind = 'metric') = (metric_type IS NOT NULL)",
                name="ck_warehouse_sources_metric_has_type",
            ),
            CheckConstraint(
                "(kind = 'metric') = (conversion_window_hours IS NOT NULL)",
                name="ck_warehouse_sources_metric_has_window",
            ),
            CheckConstraint(
                "conversion_window_hours IS NULL OR conversion_window_hours "
                f"BETWEEN 1 AND {MAX_CONVERSION_WINDOW_HOURS}",
                name="ck_warehouse_sources_window_range",
            ),
            CheckConstraint(
                "cap_value IS NULL OR (cap_value > 0 AND metric_type = 'mean')",
                name="ck_warehouse_sources_cap_value",
            ),
            {"schema": get_schema_name()},
        )

    def __repr__(self) -> str:
        return f"<WarehouseSource id={self.id} kind={self.kind!r}>"
