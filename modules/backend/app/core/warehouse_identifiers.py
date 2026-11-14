"""Identifiers and literals that may appear in a generated warehouse query.

A warehouse analysis query is built by us from a table reference, a column
mapping and structured filters (see
``modules.backend.app.services.warehouse_query_builder``).  Nothing a caller
sends is ever pasted into SQL as text.  Every value that reaches the
statement passes through this module first, and the rule for each kind of
value is the same: it must **fully match** a strict per-dialect pattern, or it
is refused.  Nothing is escaped.

* **Identifiers** (table parts, column names) fully match their dialect's
  part pattern and are then quoted by us: double quotes for Snowflake, Athena
  and Amazon Redshift, backticks for BigQuery.  None of the patterns admits a
  quote character, so a quoted identifier cannot be closed early.
* **String literals** in filters fully match ``[A-Za-z0-9 _.:@/+-]{1,256}``
  and are wrapped in single quotes by us.  The class has no quote, backslash,
  semicolon, asterisk, control character or line separator.
* **Integers** render with ``str(int)``; **booleans** as ``TRUE``/``FALSE``.
* **Timestamps** are rendered from timezone-aware ``datetime`` values,
  converted to UTC, with the offset written into the literal.
* **The experiment key** fully matches ``[A-Za-z0-9][A-Za-z0-9_.-]{0,99}``.

Every pattern here is applied with :func:`re.fullmatch` and nothing else.
``re.match``/``re.search`` with a ``$`` anchor accept a trailing newline
(``"ORDERS\\n"``); ``fullmatch`` does not.  A test walks this module's syntax
tree and fails on any other ``re`` matching call.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Sequence, Union

#: One literal value in a filter.  ``bool`` is listed before ``int`` because
#: it is a subclass of it and must render as TRUE/FALSE, not 1/0.
FilterLiteral = Union[str, bool, int]

MAX_FILTERS: Final = 5
MAX_IN_VALUES: Final = 50
#: The range a filter integer may carry: a signed 64-bit value, the widest
#: integer type all four warehouses share (Redshift's BIGINT is 64-bit).
MIN_INT_LITERAL: Final = -(2**63)
MAX_INT_LITERAL: Final = 2**63 - 1

OPERATORS: Final = frozenset({"eq", "ne", "in", "not_in", "is_null", "is_not_null"})

_STRING_LITERAL: Final = re.compile(r"[A-Za-z0-9 _.:@/+-]{1,256}")
_EXPERIMENT_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")

# Per-dialect part patterns (plan SPEC 3). Written without anchors because
# they are only ever applied with fullmatch.
_SNOWFLAKE_PART: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")
_BIGQUERY_PROJECT: Final = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
_BIGQUERY_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,1023}")
_ATHENA_PART: Final = re.compile(r"[a-z0-9_]{1,255}")
#: A Redshift identifier: a letter or _, then letters, digits, _ and $ (at
#: most 127, Redshift's limit).  Redshift reads a quoted identifier in lower
#: case unless the cluster enables case-sensitive identifiers; a source's
#: names are matched to the table's own (DescribeTable) when it is validated.
_REDSHIFT_PART: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,126}")


class WarehouseQueryRefused(ValueError):
    """A value was refused before any query was built or sent.

    ``code`` is one of a fixed set (``invalid_identifier``,
    ``invalid_literal``, ``too_many_filters``, ``internal``); ``field`` names
    the part that was refused.  The message never repeats the refused value.
    """

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True)
class IdentifierRules:
    """How one dialect names a table and a column, and how it quotes them."""

    name: str
    #: One pattern per part of a table reference, in order; the count is
    #: exact (Snowflake and BigQuery 3, Athena and Redshift 2).
    table_parts: tuple[re.Pattern[str], ...]
    table_part_names: tuple[str, ...]
    column: re.Pattern[str]
    quote_open: str
    quote_close: str


SNOWFLAKE: Final = IdentifierRules(
    name="snowflake",
    table_parts=(_SNOWFLAKE_PART, _SNOWFLAKE_PART, _SNOWFLAKE_PART),
    table_part_names=("database", "schema", "table"),
    column=_SNOWFLAKE_PART,
    quote_open='"',
    quote_close='"',
)
BIGQUERY: Final = IdentifierRules(
    name="bigquery",
    table_parts=(_BIGQUERY_PROJECT, _BIGQUERY_NAME, _BIGQUERY_NAME),
    table_part_names=("project", "dataset", "table"),
    column=_BIGQUERY_NAME,
    quote_open="`",
    quote_close="`",
)
ATHENA: Final = IdentifierRules(
    name="athena",
    table_parts=(_ATHENA_PART, _ATHENA_PART),
    table_part_names=("database", "table"),
    column=_ATHENA_PART,
    quote_open='"',
    quote_close='"',
)
#: Amazon Redshift: ``schema.table``, in the connection's own database.
REDSHIFT: Final = IdentifierRules(
    name="redshift",
    table_parts=(_REDSHIFT_PART, _REDSHIFT_PART),
    table_part_names=("schema", "table"),
    column=_REDSHIFT_PART,
    quote_open='"',
    quote_close='"',
)

#: The dialects a connection can have.  The DuckDB rules the tests use are
#: defined in the tests, not here.
IDENTIFIER_RULES: Final = {r.name: r for r in (SNOWFLAKE, BIGQUERY, ATHENA, REDSHIFT)}


def _fully_matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _refuse_identifier(field: str) -> WarehouseQueryRefused:
    return WarehouseQueryRefused(
        "invalid_identifier",
        field,
        f"{field} is not a valid identifier for this warehouse",
    )


def parse_table_reference(rules: IdentifierRules, text: object) -> tuple[str, ...]:
    """Split ``text`` on dots and check every part; return the parts.

    The number of parts is exact.  A part that does not fully match its
    pattern (an empty part, a quote, a space, a newline, ...) is refused and
    named.
    """
    if not isinstance(text, str):
        raise _refuse_identifier("table")
    return validate_table_parts(rules, tuple(text.split(".")))


def validate_table_parts(
    rules: IdentifierRules, parts: Sequence[object]
) -> tuple[str, ...]:
    """Check an already split table reference; return it as a tuple."""
    expected = len(rules.table_parts)
    if len(parts) != expected:
        raise WarehouseQueryRefused(
            "invalid_identifier",
            "table",
            f"table must have exactly {expected} dot-separated parts: "
            + ".".join(rules.table_part_names),
        )
    checked: list[str] = []
    for pattern, part_name, part in zip(
        rules.table_parts, rules.table_part_names, parts
    ):
        if not _fully_matches(pattern, part):
            raise _refuse_identifier(f"table {part_name}")
        checked.append(part)  # type: ignore[arg-type]
    return tuple(checked)


def validate_column(rules: IdentifierRules, name: object, field: str) -> str:
    """Return ``name`` if it fully matches the dialect's column pattern."""
    if not _fully_matches(rules.column, name):
        raise _refuse_identifier(field)
    return name  # type: ignore[return-value]


def quote_identifier(
    rules: IdentifierRules, part: str, pattern: re.Pattern[str]
) -> str:
    """Quote one validated identifier part.

    The part is checked again here, so a caller that skipped validation still
    cannot render an unchecked name, and the quote character is asserted
    absent (no pattern admits it; this is the second line).
    """
    if not _fully_matches(pattern, part):
        raise _refuse_identifier("identifier")
    if rules.quote_open in part or rules.quote_close in part:
        raise _refuse_identifier("identifier")
    return f"{rules.quote_open}{part}{rules.quote_close}"


def render_table(rules: IdentifierRules, parts: Sequence[str]) -> str:
    """``"DB"."SCH"."T"`` / `` `p`.`d`.`t` `` / ``"db"."t"``."""
    checked = validate_table_parts(rules, parts)
    return ".".join(
        quote_identifier(rules, part, pattern)
        for part, pattern in zip(checked, rules.table_parts)
    )


def render_column(rules: IdentifierRules, name: str, field: str) -> str:
    return quote_identifier(rules, validate_column(rules, name, field), rules.column)


def validate_experiment_key(key: object) -> str:
    if not _fully_matches(_EXPERIMENT_KEY, key):
        raise WarehouseQueryRefused(
            "invalid_literal", "experiment_key", "experiment_key is not a valid key"
        )
    return key  # type: ignore[return-value]


def render_experiment_key(key: object) -> str:
    return f"'{validate_experiment_key(key)}'"


def render_literal(value: object, field: str) -> str:
    """Render one filter literal, or refuse it.  Strings are never escaped."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if type(value) is int:
        if not MIN_INT_LITERAL <= value <= MAX_INT_LITERAL:
            raise WarehouseQueryRefused(
                "invalid_literal", field, f"{field} is outside the 64-bit range"
            )
        return str(value)
    if _fully_matches(_STRING_LITERAL, value):
        return f"'{value}'"
    raise WarehouseQueryRefused(
        "invalid_literal",
        field,
        f"{field} may contain only letters, digits, spaces and _ . : @ / + -"
        " (1 to 256 characters)",
    )


def render_float_literal(value: object, field: str) -> str:
    """A positive finite number, as a literal every dialect reads as a number."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise WarehouseQueryRefused(
            "invalid_literal", field, f"{field} must be a number"
        )
    if not value > 0:
        raise WarehouseQueryRefused(
            "invalid_literal", field, f"{field} must be above 0"
        )
    text = repr(float(value))
    if not _fully_matches(_FLOAT_TEXT, text):
        raise WarehouseQueryRefused(
            "invalid_literal", field, f"{field} must be a number"
        )
    return text


_FLOAT_TEXT: Final = re.compile(r"[0-9]+(\.[0-9]+)?(e[+-][0-9]{1,3})?")


def utc_text(value: object, field: str) -> str:
    """``YYYY-MM-DD HH:MM:SS[.ffffff]`` in UTC, from an aware datetime.

    A naive datetime is refused: the caller resolves a naive value to UTC
    (the ``_parse_dt`` rule) before it gets here, so the builder never has to
    guess a zone.
    """
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise WarehouseQueryRefused(
            "invalid_literal", field, f"{field} must be a timezone-aware datetime"
        )
    utc = value.astimezone(timezone.utc)
    text = utc.strftime("%Y-%m-%d %H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}"
    return text


@dataclass(frozen=True)
class SourceFilter:
    """One structured filter: a column, an operator from a fixed set, a value."""

    column: str
    operator: str
    value: Union[None, FilterLiteral, tuple[FilterLiteral, ...]] = None


def render_filter(rules: IdentifierRules, flt: SourceFilter, field: str) -> str:
    """Render one filter as ``<quoted column> <op> <literal(s)>``."""
    column = render_column(rules, flt.column, f"{field}.column")
    op = flt.operator
    if op not in OPERATORS:
        raise WarehouseQueryRefused(
            "invalid_literal", f"{field}.operator", f"{field}.operator is not supported"
        )
    if op in ("is_null", "is_not_null"):
        if flt.value is not None:
            raise WarehouseQueryRefused(
                "invalid_literal",
                f"{field}.value",
                f"{field}.value must be empty for {op}",
            )
        return f"{column} IS NULL" if op == "is_null" else f"{column} IS NOT NULL"
    if op in ("eq", "ne"):
        if isinstance(flt.value, (tuple, list)) or flt.value is None:
            raise WarehouseQueryRefused(
                "invalid_literal", f"{field}.value", f"{field}.value must be one value"
            )
        symbol = "=" if op == "eq" else "<>"
        return f"{column} {symbol} {render_literal(flt.value, f'{field}.value')}"
    values = flt.value
    if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= MAX_IN_VALUES:
        raise WarehouseQueryRefused(
            "invalid_literal",
            f"{field}.value",
            f"{field}.value must be a list of 1 to {MAX_IN_VALUES} values",
        )
    kinds = {bool if isinstance(v, bool) else type(v) for v in values}
    if len(kinds) != 1:
        raise WarehouseQueryRefused(
            "invalid_literal",
            f"{field}.value",
            f"{field}.value must hold values of one type",
        )
    rendered = ", ".join(render_literal(v, f"{field}.value") for v in values)
    keyword = "IN" if op == "in" else "NOT IN"
    return f"{column} {keyword} ({rendered})"


def render_filters(
    rules: IdentifierRules, filters: Sequence[SourceFilter], field: str
) -> list[str]:
    if len(filters) > MAX_FILTERS:
        raise WarehouseQueryRefused(
            "too_many_filters", field, f"at most {MAX_FILTERS} filters are allowed"
        )
    return [render_filter(rules, f, f"{field}[{i}]") for i, f in enumerate(filters)]
