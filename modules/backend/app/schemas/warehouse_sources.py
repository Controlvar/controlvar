"""Request bodies for warehouse sources: a table or view plus a column mapping.

A source names one of the customer's tables or views and maps its columns;
there is no field that takes SQL.  Every string field carries a maximum length
and a pattern, and ``modules/backend/tests/unit/warehouse/test_request_schemas.py``
pins the exact set of (field, max_length, pattern) across every warehouse
request schema, so a new free-text field fails that test until it is listed
with its bounds.

The patterns here are the dialect-independent outer bound: a source's
connection decides the dialect, and the per-dialect identifier gate
(:mod:`modules.backend.app.core.warehouse_identifiers`) applies the strict
per-dialect pattern with ``re.fullmatch`` before anything is rendered.
Pydantic applies ``pattern`` as a full match of the whole value; a trailing
newline does not satisfy ``$``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

#: Printable text with no control character (U+0000-U+001F, U+007F) and no
#: line or paragraph separator (U+2028, U+2029, written as escapes so the
#: pattern shows them).  Pydantic's regex engine reads the \uXXXX escapes.
NAME_PATTERN = r"^[^\x00-\x1f\x7f\u2028\u2029]+$"
#: The union of the three dialects' column characters; each dialect's own
#: pattern is stricter and is applied by the identifier gate.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9_$]+$"
#: Two or three parts; only the first (a BigQuery project) may hold "-".
TABLE_PATTERN = r"^[A-Za-z0-9_$-]+(\.[A-Za-z0-9_$]+){1,2}$"
LITERAL_PATTERN = r"^[A-Za-z0-9 _.:@/+-]+$"

SourceName = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=NAME_PATTERN)
]
Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=1024, pattern=IDENTIFIER_PATTERN)
]
TableReference = Annotated[
    str, StringConstraints(min_length=3, max_length=2080, pattern=TABLE_PATTERN)
]
StringLiteral = Annotated[
    str, StringConstraints(min_length=1, max_length=256, pattern=LITERAL_PATTERN)
]
#: A signed 64-bit integer, -2**63 to 2**63 - 1 inclusive.
#:
#: The bounds are enforced here but described as ``format: int64`` rather than
#: ``minimum``/``maximum``.  A bound of that size is not a number a double can
#: hold: FastAPI's OpenAPI model types ``maximum`` as ``float``, which turned
#: 2**63 - 1 into 2**63 in the document, and release-please's JSON updater
#: then printed that as 9223372036854776000 in the committed snapshots.
#: ``int64`` is OpenAPI's name for exactly this range, and it survives both.
IntLiteral = Annotated[
    StrictInt,
    Field(ge=-(2**63), le=2**63 - 1),
    WithJsonSchema({"type": "integer", "format": "int64"}),
]

ScalarLiteral = Union[StrictBool, IntLiteral, StringLiteral]

_FORBID = ConfigDict(extra="forbid")


class SourceFilterIn(BaseModel):
    """One structured filter: ``{column, operator, value}``."""

    model_config = _FORBID

    column: Identifier
    operator: Literal["eq", "ne", "in", "not_in", "is_null", "is_not_null"]
    value: Optional[
        Union[
            ScalarLiteral,
            Annotated[list[ScalarLiteral], Field(min_length=1, max_length=50)],
        ]
    ] = None

    @model_validator(mode="after")
    def _value_fits_operator(self) -> "SourceFilterIn":
        if self.operator in ("is_null", "is_not_null"):
            if self.value is not None:
                raise ValueError(f"{self.operator} takes no value")
        elif self.operator in ("in", "not_in"):
            if not isinstance(self.value, list):
                raise ValueError(f"{self.operator} takes a list of 1 to 50 values")
            if len({type(v) for v in self.value}) != 1:
                raise ValueError(f"{self.operator} values must all have one type")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError(f"{self.operator} takes one value")
        return self


Filters = Annotated[list[SourceFilterIn], Field(max_length=5)]


class AssignmentColumns(BaseModel):
    model_config = _FORBID

    unit_id: Identifier
    experiment_key: Identifier
    variant: Identifier
    exposed_at: Identifier


class MetricColumns(BaseModel):
    model_config = _FORBID

    unit_id: Identifier
    event_at: Identifier
    value: Optional[Identifier] = None


class _AssignmentSourceBody(BaseModel):
    model_config = _FORBID

    name: SourceName
    table: TableReference
    columns: AssignmentColumns
    filters: Filters = []


class AssignmentSourceCreate(_AssignmentSourceBody):
    """``POST /sources`` with ``kind: assignment``."""

    connection_id: uuid.UUID
    kind: Literal["assignment"]


class AssignmentSourceUpdate(_AssignmentSourceBody):
    """``PUT /sources/{id}`` for an assignment source (the connection is fixed)."""

    kind: Literal["assignment"]


class _MetricSourceBody(BaseModel):
    model_config = _FORBID

    name: SourceName
    table: TableReference
    columns: MetricColumns
    metric_type: Literal["proportion", "mean"]
    conversion_window_hours: Annotated[StrictInt, Field(ge=1, le=8760)] = 168
    #: Strict: a JSON number only.  An integer (500) is accepted as a float,
    #: which is what JSON numbers are; a boolean or a string ("500") is refused
    #: rather than coerced.
    cap_value: Optional[
        Annotated[float, Field(strict=True, gt=0, le=1e15, allow_inf_nan=False)]
    ] = None
    #: Strict: a JSON boolean only ("true", 1 and null are refused).  The body
    #: replaces the whole definition, so a PUT that leaves it out sets false,
    #: as an omitted cap_value clears the cap (#1386).
    lower_is_better: StrictBool = Field(
        default=False,
        description=(
            "True for a metric where a decrease is the improvement, such as an "
            "error rate or churn: an analysis started afterwards names a winner "
            "only for a significant decrease. Omitted means false, on a PUT as "
            "on a POST."
        ),
    )
    filters: Filters = []

    @model_validator(mode="after")
    def _value_fits_type(self) -> "_MetricSourceBody":
        if self.metric_type == "mean":
            if self.columns.value is None:
                raise ValueError("a mean metric needs columns.value")
        else:
            if self.columns.value is not None:
                raise ValueError("a proportion metric takes no columns.value")
            if self.cap_value is not None:
                raise ValueError("cap_value applies to mean metrics only")
        return self


class MetricSourceCreate(_MetricSourceBody):
    """``POST /sources`` with ``kind: metric``."""

    connection_id: uuid.UUID
    kind: Literal["metric"]


class MetricSourceUpdate(_MetricSourceBody):
    """``PUT /sources/{id}`` for a metric source (the connection is fixed)."""

    kind: Literal["metric"]
