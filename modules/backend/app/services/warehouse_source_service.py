"""Warehouse sources: what is stored, how it is checked, what it renders to.

A source is saved with the names its author typed, after each has passed the
connection's per-dialect identifier gate
(:mod:`modules.backend.app.core.warehouse_identifiers`).  It is not used in a
query until it has been **validated**: the table's columns are read from the
warehouse's own metadata (no query touches data), every mapped column and
filter column is matched to one of them -- exactly, or else by the one column
whose name differs only in case, which is then stored in its canonical case --
and the time and value columns must have a supported type.  Editing a source
clears its validation.

Stored shapes (JSONB):

* ``table_reference``: ``{"parts": ["DB", "SCHEMA", "TABLE"]}``;
* ``column_mapping``: ``{role: {"name": ..., "type": ... or null}}``;
* ``filters``: ``[{"column", "operator", "value"}]``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from modules.backend.app.core.warehouse_identifiers import (
    IDENTIFIER_RULES,
    IdentifierRules,
    SourceFilter,
    WarehouseQueryRefused,
    parse_table_reference,
    render_filters,
    validate_column,
)
from modules.backend.app.models.warehouse_source import WarehouseSource
from modules.backend.app.services.warehouse_clients import WarehouseClient
from modules.backend.app.services.warehouse_query_builder import (
    AssignmentMapping,
    MetricMapping,
)

ASSIGNMENT_ROLES: Tuple[str, ...] = (
    "unit_id",
    "experiment_key",
    "variant",
    "exposed_at",
)
METRIC_ROLES: Tuple[str, ...] = ("unit_id", "event_at", "value")
TIME_ROLES = frozenset({"exposed_at", "event_at"})


class SourceRefused(ValueError):
    """A source was refused (422).  ``code`` is from a fixed set; ``field`` names
    the part.  The message never repeats a submitted value."""

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


def rules_for(warehouse_type: str) -> IdentifierRules:
    return IDENTIFIER_RULES[warehouse_type]


def _filter_values(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


def filters_from_json(filters: Iterable[Mapping[str, Any]]) -> Tuple[SourceFilter, ...]:
    return tuple(
        SourceFilter(
            column=f["column"],
            operator=f["operator"],
            value=_filter_values(f.get("value")),
        )
        for f in filters
    )


def check_definition(
    rules: IdentifierRules,
    table: str,
    columns: Mapping[str, Optional[str]],
    filters: Sequence[Mapping[str, Any]],
) -> Tuple[str, ...]:
    """Apply the per-dialect gate to a submitted source; return the table parts.

    Raises :class:`~modules.backend.app.core.warehouse_identifiers.WarehouseQueryRefused`
    naming the refused part.  Nothing is sent anywhere.
    """
    parts = parse_table_reference(rules, table)
    for role, name in columns.items():
        if name is not None:
            validate_column(rules, name, f"columns.{role}")
    render_filters(rules, filters_from_json(filters), "filters")
    return parts


def lower_is_better(source: WarehouseSource) -> Optional[bool]:
    """A metric source's direction (#1386); None for an assignment source,
    which has none, like its other metric-only fields."""
    if source.kind != "metric":
        return None
    return source.lower_is_better is True


def stored_definition(source: WarehouseSource) -> Dict[str, Any]:
    """The full definition, as the audit log records it.

    The audit trail is core, so it outlives a downgrade of the modules
    branch: a source's latest entry still says whether it was marked lower
    is better after ``modules_0003_lower_is_better`` has dropped the column.
    """
    return {
        "connection_id": str(source.connection_id),
        "kind": source.kind,
        "name": source.name,
        "table_reference": source.table_reference,
        "column_mapping": source.column_mapping,
        "filters": source.filters,
        "metric_type": source.metric_type,
        "conversion_window_hours": source.conversion_window_hours,
        "cap_value": source.cap_value,
        "lower_is_better": lower_is_better(source),
        "validated_at": source.validated_at.isoformat()
        if source.validated_at
        else None,
    }


def table_parts(source: WarehouseSource) -> Tuple[str, ...]:
    return tuple(source.table_reference["parts"])


def column(source: WarehouseSource, role: str) -> Optional[str]:
    entry = (source.column_mapping or {}).get(role)
    return entry["name"] if entry else None


def assignment_mapping(source: WarehouseSource) -> AssignmentMapping:
    return AssignmentMapping(
        table=table_parts(source),
        unit_id=column(source, "unit_id"),  # type: ignore[arg-type]
        experiment_key=column(source, "experiment_key"),  # type: ignore[arg-type]
        variant=column(source, "variant"),  # type: ignore[arg-type]
        exposed_at=column(source, "exposed_at"),  # type: ignore[arg-type]
        filters=filters_from_json(source.filters or []),
    )


def metric_mapping(source: WarehouseSource) -> MetricMapping:
    return MetricMapping(
        table=table_parts(source),
        unit_id=column(source, "unit_id"),  # type: ignore[arg-type]
        event_at=column(source, "event_at"),  # type: ignore[arg-type]
        metric_type=source.metric_type,  # type: ignore[arg-type]
        value=column(source, "value"),
        conversion_window_hours=int(source.conversion_window_hours),
        cap_value=source.cap_value,
        filters=filters_from_json(source.filters or []),
    )


def _resolve(wanted: str, available: Mapping[str, str], field: str) -> Tuple[str, str]:
    """``(canonical name, type)`` of the column ``wanted`` names."""
    if wanted in available:
        return wanted, available[wanted]
    folded = [name for name in available if name.lower() == wanted.lower()]
    if len(folded) == 1:
        return folded[0], available[folded[0]]
    raise SourceRefused(
        "identifier_not_found",
        field,
        f"{field} names a column the table does not have",
    )


def resolve_columns(
    source: WarehouseSource,
    client: WarehouseClient,
    columns: Sequence[Tuple[str, str]],
) -> Tuple[Dict[str, Dict[str, Optional[str]]], List[Dict[str, Any]]]:
    """Match the mapping and the filter columns to the table's columns.

    Returns the new ``column_mapping`` and ``filters`` in canonical case.
    Raises :class:`SourceRefused` (``identifier_not_found``,
    ``unsupported_column_type``).
    """
    rules = client.dialect.identifiers
    available: Dict[str, str] = {}
    for name, kind in columns:
        # A name the dialect's gate would refuse is never stored.
        try:
            validate_column(rules, name, "column")
        except WarehouseQueryRefused:
            continue
        available[name] = kind
    mapping: Dict[str, Dict[str, Optional[str]]] = {}
    for role, entry in (source.column_mapping or {}).items():
        if not entry:
            continue
        name, kind = _resolve(entry["name"], available, f"columns.{role}")
        if role in TIME_ROLES and not client.is_time_type(kind):
            raise SourceRefused(
                "unsupported_column_type",
                f"columns.{role}",
                f"columns.{role} must be a timestamp column",
            )
        if role == "value" and not client.is_numeric_type(kind):
            raise SourceRefused(
                "unsupported_column_type",
                "columns.value",
                "columns.value must be a numeric column",
            )
        mapping[role] = {"name": name, "type": kind}
    filters: List[Dict[str, Any]] = []
    for index, flt in enumerate(source.filters or []):
        name, _ = _resolve(flt["column"], available, f"filters[{index}].column")
        filters.append({**flt, "column": name})
    return mapping, filters


def mark_validated(
    source: WarehouseSource,
    mapping: Dict[str, Dict[str, Optional[str]]],
    filters: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> None:
    source.column_mapping = mapping
    source.filters = filters
    source.validated_at = now or datetime.now(timezone.utc)


__all__ = [
    "ASSIGNMENT_ROLES",
    "METRIC_ROLES",
    "SourceRefused",
    "assignment_mapping",
    "check_definition",
    "filters_from_json",
    "lower_is_better",
    "mark_validated",
    "metric_mapping",
    "resolve_columns",
    "rules_for",
    "stored_definition",
    "table_parts",
]
