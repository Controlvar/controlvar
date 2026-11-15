"""The identifier and literal gate refuses everything outside its patterns.

Each tamper below is refused by the gate before any statement exists, in
every dialect, for every place a caller's value can reach: a table part, a
column, a filter literal and the experiment key.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules.backend.app.core import warehouse_identifiers as gate
from modules.backend.app.core.warehouse_identifiers import (
    IDENTIFIER_RULES,
    SourceFilter,
    WarehouseQueryRefused,
)
from modules.backend.app.services.warehouse_query_builder import (
    SQL_DIALECTS,
    AnalysisWindow,
    AssignmentMapping,
    MetricMapping,
    build_metric_query,
)

pytestmark = pytest.mark.unit

#: A valid table and column per dialect; each tamper is spliced into them.
VALID = {
    "snowflake": (("ANALYTICS", "PUBLIC", "ORDERS"), "USER_ID"),
    "bigquery": (("acme-prod", "analytics", "orders"), "user_id"),
    "athena": (("analytics", "orders"), "user_id"),
    "redshift": (("analytics", "orders"), "user_id"),
}


def test_every_dialect_has_identifier_rules_and_the_reverse():
    """A dialect the builder can render has a gate, and a gate has a dialect:
    the parametrisations here and in the builder's tests cover the same set."""
    assert set(IDENTIFIER_RULES) == set(SQL_DIALECTS)
    assert set(VALID) == set(IDENTIFIER_RULES)
    for name, dialect in SQL_DIALECTS.items():
        assert dialect.identifiers is IDENTIFIER_RULES[name]


#: (name, text appended to a valid identifier) -- each must be refused.
IDENTIFIER_TAMPERS = [
    ("double_quote", '"'),
    ("backtick", "`"),
    ("apostrophe", "'"),
    ("semicolon", ";"),
    ("line_comment", "--"),
    ("block_comment", "/*"),
    ("line_separator", "\u2028"),
    ("paragraph_separator", "\u2029"),
    ("nul", "\x00"),
    ("newline_inside", "\nX"),
    ("carriage_return", "\r"),
    ("trailing_newline", "\n"),
    ("trailing_space", " "),
    ("dot", ".x"),
    ("paren", "()"),
    ("non_ascii_letter", "é"),
]


@pytest.mark.parametrize("dialect", sorted(IDENTIFIER_RULES))
@pytest.mark.parametrize(("tamper", "text"), IDENTIFIER_TAMPERS)
def test_identifier_tampers_refused_in_every_table_part(dialect, tamper, text):
    rules = IDENTIFIER_RULES[dialect]
    table, _ = VALID[dialect]
    for i in range(len(table)):
        parts = list(table)
        parts[i] = parts[i] + text
        with pytest.raises(WarehouseQueryRefused) as refused:
            gate.validate_table_parts(rules, parts)
        assert refused.value.code == "invalid_identifier"
        with pytest.raises(WarehouseQueryRefused):
            gate.render_table(rules, parts)


@pytest.mark.parametrize("dialect", sorted(IDENTIFIER_RULES))
@pytest.mark.parametrize(("tamper", "text"), IDENTIFIER_TAMPERS)
def test_identifier_tampers_refused_in_columns(dialect, tamper, text):
    rules = IDENTIFIER_RULES[dialect]
    _, column = VALID[dialect]
    for bad in (column + text, text + column):
        with pytest.raises(WarehouseQueryRefused) as refused:
            gate.render_column(rules, bad, "columns.unit_id")
        assert refused.value.code == "invalid_identifier"
        assert refused.value.field == "columns.unit_id"


@pytest.mark.parametrize("dialect", sorted(IDENTIFIER_RULES))
def test_leading_space_and_empty_part_refused(dialect):
    rules = IDENTIFIER_RULES[dialect]
    table, column = VALID[dialect]
    for i in range(len(table)):
        for bad in (" " + table[i], ""):
            parts = list(table)
            parts[i] = bad
            with pytest.raises(WarehouseQueryRefused):
                gate.validate_table_parts(rules, parts)
    for bad in (" " + column, ""):
        with pytest.raises(WarehouseQueryRefused):
            gate.validate_column(rules, bad, "c")


@pytest.mark.parametrize("dialect", sorted(IDENTIFIER_RULES))
def test_table_reference_part_count_is_exact(dialect):
    rules = IDENTIFIER_RULES[dialect]
    table, _ = VALID[dialect]
    good = ".".join(table)
    assert gate.parse_table_reference(rules, good) == table
    for bad in (
        good + ".extra",
        ".".join(table[1:]),
        good + ".",
        "." + good,
        good.replace(".", "..", 1),
    ):
        with pytest.raises(WarehouseQueryRefused) as refused:
            gate.parse_table_reference(rules, bad)
        assert refused.value.code == "invalid_identifier"


def test_dialect_patterns_are_the_per_dialect_ones():
    sf, bq, at = (
        IDENTIFIER_RULES["snowflake"],
        IDENTIFIER_RULES["bigquery"],
        IDENTIFIER_RULES["athena"],
    )
    # Snowflake admits $ but not -, BigQuery's project admits - but not
    # upper case, Athena admits only lower case.
    assert gate.render_column(sf, "A$B", "c") == '"A$B"'
    with pytest.raises(WarehouseQueryRefused):
        gate.render_column(sf, "A-B", "c")
    assert (
        gate.render_table(bq, ("acme-prod", "Data_Set", "T1"))
        == "`acme-prod`.`Data_Set`.`T1`"
    )
    for bad_project in ("Acme-prod", "acme", "acme-prod-", "1acme-prod"):
        with pytest.raises(WarehouseQueryRefused):
            gate.render_table(bq, (bad_project, "d", "t"))
    with pytest.raises(WarehouseQueryRefused):
        gate.render_column(bq, "a$b", "c")
    assert gate.render_table(at, ("db", "t_1")) == '"db"."t_1"'
    with pytest.raises(WarehouseQueryRefused):
        gate.render_table(at, ("DB", "t"))
    # Length limits.
    assert gate.validate_column(sf, "A" * 255, "c")
    with pytest.raises(WarehouseQueryRefused):
        gate.validate_column(sf, "A" * 256, "c")
    with pytest.raises(WarehouseQueryRefused):
        gate.validate_column(at, "a" * 256, "c")


def test_identifiers_are_quoted_by_us():
    assert (
        gate.render_table(IDENTIFIER_RULES["snowflake"], ("DB", "SCH", "T"))
        == '"DB"."SCH"."T"'
    )
    assert gate.render_table(IDENTIFIER_RULES["athena"], ("db", "t")) == '"db"."t"'
    assert (
        gate.render_table(IDENTIFIER_RULES["redshift"], ("analytics", "orders"))
        == '"analytics"."orders"'
    )
    assert (
        gate.render_column(IDENTIFIER_RULES["redshift"], "User_Id", "c") == '"User_Id"'
    )


def test_redshift_names_are_schema_and_table_in_the_connections_database():
    """Two parts, named schema and table; a database part is refused (422)."""
    rules = IDENTIFIER_RULES["redshift"]
    assert gate.parse_table_reference(rules, "analytics.orders") == (
        "analytics",
        "orders",
    )
    with pytest.raises(WarehouseQueryRefused) as refused:
        gate.parse_table_reference(rules, "dev.analytics.orders")
    assert (refused.value.code, refused.value.field) == ("invalid_identifier", "table")
    assert str(refused.value) == (
        "table must have exactly 2 dot-separated parts: schema.table"
    )


def test_redshift_identifier_rules():
    """A letter or _, then letters, digits, _ and $; at most 127 characters."""
    rules = IDENTIFIER_RULES["redshift"]
    for good in ("a", "_x", "Orders_2026", "a$b", "A" * 127):
        assert gate.validate_column(rules, good, "c") == good
    for bad in ("1abc", "a-b", "a b", "$a", "A" * 128, "é", "a.b"):
        with pytest.raises(WarehouseQueryRefused):
            gate.validate_column(rules, bad, "c")
        with pytest.raises(WarehouseQueryRefused):
            gate.validate_table_parts(rules, (bad, "t"))


def test_a_source_on_a_redshift_connection_is_checked_by_redshifts_rules():
    """The source service looks a connection's rules up by its type: a
    Redshift connection's are found (a KeyError there was a 500)."""
    from modules.backend.app.services import warehouse_source_service as sources

    rules = sources.rules_for("redshift")
    assert rules is IDENTIFIER_RULES["redshift"]
    columns = {"unit_id": "user_id", "event_at": "event_at", "value": None}
    assert sources.check_definition(rules, "analytics.events", columns, []) == (
        "analytics",
        "events",
    )
    with pytest.raises(WarehouseQueryRefused) as refused:
        sources.check_definition(rules, "dev.analytics.events", columns, [])
    assert "schema.table" in str(refused.value)


LITERAL_TAMPERS = [
    ("apostrophe", "paid'"),
    ("double_quote", 'paid"'),
    ("backtick", "paid`"),
    ("semicolon", "paid;"),
    ("block_comment", "paid/*"),
    ("backslash", "paid\\"),
    ("asterisk", "a*b"),
    ("line_separator", "paid\u2028"),
    ("nul", "paid\x00"),
    ("newline", "pa\nid"),
    ("carriage_return", "paid\r"),
    ("trailing_newline", "paid\n"),
    ("tab", "pa\tid"),
    ("too_long", "a" * 257),
    ("empty", ""),
    ("non_ascii", "café"),
]


@pytest.mark.parametrize(("tamper", "value"), LITERAL_TAMPERS)
def test_literal_outside_allowlist_refused(tamper, value):
    with pytest.raises(WarehouseQueryRefused) as refused:
        gate.render_literal(value, "filters[0].value")
    assert refused.value.code == "invalid_literal"
    # ...and the refusal does not repeat the value.
    if value:
        assert value not in str(refused.value)


def test_literals_render_without_escaping():
    assert gate.render_literal("paid", "v") == "'paid'"
    assert gate.render_literal("a-b_c.d:e@f/g+h i", "v") == "'a-b_c.d:e@f/g+h i'"
    # "--" is inside the quotes, where it is text, and the class admits no
    # quote to leave them.
    assert gate.render_literal("a--b", "v") == "'a--b'"
    assert gate.render_literal(True, "v") == "TRUE"
    assert gate.render_literal(False, "v") == "FALSE"
    assert gate.render_literal(-42, "v") == "-42"
    assert gate.render_literal(2**63 - 1, "v") == str(2**63 - 1)
    with pytest.raises(WarehouseQueryRefused):
        gate.render_literal(2**63, "v")
    # The int64 minimum is a valid value; one below it is not.
    assert gate.render_literal(-(2**63), "v") == str(-(2**63))
    with pytest.raises(WarehouseQueryRefused):
        gate.render_literal(-(2**63) - 1, "v")
    for wrong_type in (1.5, None, b"paid", ["paid"]):
        with pytest.raises(WarehouseQueryRefused):
            gate.render_literal(wrong_type, "v")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "-leading",
        "has space",
        "quote'",
        "semi;colon",
        "nl\n",
        "a" * 101,
        "comment--",
        "\u2028x",
    ],
)
def test_experiment_key_revalidated(key):
    if key == "comment--":
        # Allowed characters ("-" is in the class); still a single literal.
        assert gate.render_experiment_key(key) == "'comment--'"
        return
    with pytest.raises(WarehouseQueryRefused):
        gate.render_experiment_key(key)


def test_filters_are_structured():
    rules = IDENTIFIER_RULES["athena"]
    render = gate.render_filter
    assert render(rules, SourceFilter("s", "eq", "paid"), "f") == "\"s\" = 'paid'"
    assert render(rules, SourceFilter("s", "ne", 3), "f") == '"s" <> 3'
    assert (
        render(rules, SourceFilter("s", "in", ("a", "b")), "f") == "\"s\" IN ('a', 'b')"
    )
    assert (
        render(rules, SourceFilter("s", "not_in", (1, 2)), "f") == '"s" NOT IN (1, 2)'
    )
    assert render(rules, SourceFilter("s", "is_null"), "f") == '"s" IS NULL'
    assert render(rules, SourceFilter("s", "is_not_null"), "f") == '"s" IS NOT NULL'
    refused = [
        SourceFilter("s", "like", "a%"),
        SourceFilter("s", "eq", None),
        SourceFilter("s", "eq", ("a",)),
        SourceFilter("s", "in", ()),
        SourceFilter("s", "in", tuple(str(i) for i in range(51))),
        SourceFilter("s", "in", ("a", 1)),
        SourceFilter("s", "in", "a"),
        SourceFilter("s", "is_null", "a"),
        SourceFilter("s;", "eq", "a"),
    ]
    for flt in refused:
        with pytest.raises(WarehouseQueryRefused):
            render(rules, flt, "f")
    assert render(rules, SourceFilter("s", "in", tuple(str(i) for i in range(50))), "f")


def test_too_many_filters():
    rules = IDENTIFIER_RULES["athena"]
    five = [SourceFilter("s", "eq", "a")] * 5
    assert len(gate.render_filters(rules, five, "filters")) == 5
    with pytest.raises(WarehouseQueryRefused) as refused:
        gate.render_filters(rules, five + five[:1], "filters")
    assert refused.value.code == "too_many_filters"


def test_timestamps_need_a_zone_and_render_in_utc():
    with pytest.raises(WarehouseQueryRefused):
        gate.utc_text(datetime(2026, 9, 1), "window_start")
    with pytest.raises(WarehouseQueryRefused):
        gate.utc_text("2026-09-01 00:00:00", "window_start")
    plus5 = timezone(timedelta(hours=5))
    assert (
        gate.utc_text(datetime(2026, 9, 1, 5, tzinfo=plus5), "w")
        == "2026-09-01 00:00:00"
    )
    assert (
        gate.utc_text(datetime(2026, 9, 1, 0, 0, 0, 1500, tzinfo=timezone.utc), "w")
        == "2026-09-01 00:00:00.001500"
    )


@pytest.mark.parametrize("dialect", sorted(SQL_DIALECTS))
def test_the_builder_refuses_before_producing_a_statement(dialect):
    """A tamper in any mapped column stops the build; nothing is returned."""
    d = SQL_DIALECTS[dialect]
    table, column = VALID[dialect]
    window = AnalysisWindow(
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    good_a = AssignmentMapping(table, column, column, column, column)
    good_m = MetricMapping(table, column, column, "proportion")
    assert build_metric_query(d, good_a, good_m, "key", window).sql
    bad = column + "\n"
    for a, m in (
        (AssignmentMapping(table, bad, column, column, column), good_m),
        (AssignmentMapping(table, column, column, bad, column), good_m),
        (good_a, MetricMapping(table, column, bad, "proportion")),
        (good_a, MetricMapping(table, column, column, "mean", value=bad)),
        (good_a, MetricMapping(table[:-1] + (bad,), column, column, "proportion")),
        (
            good_a,
            MetricMapping(
                table,
                column,
                column,
                "proportion",
                filters=(SourceFilter(column, "eq", "x'"),),
            ),
        ),
    ):
        with pytest.raises(WarehouseQueryRefused):
            build_metric_query(d, a, m, "key", window)


def test_metric_definition_is_checked():
    d = SQL_DIALECTS["athena"]
    window = AnalysisWindow(
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    a = AssignmentMapping(("d", "t"), "u", "k", "v", "e")
    for m in (
        MetricMapping(("d", "t"), "u", "e", "mean"),  # no value column
        MetricMapping(("d", "t"), "u", "e", "proportion", value="x"),
        MetricMapping(("d", "t"), "u", "e", "proportion", cap_value=5.0),
        MetricMapping(("d", "t"), "u", "e", "mean", value="x", cap_value=0.0),
        MetricMapping(("d", "t"), "u", "e", "mean", value="x", cap_value=float("inf")),
        MetricMapping(("d", "t"), "u", "e", "mean", value="x", cap_value=2e15),
        MetricMapping(("d", "t"), "u", "e", "proportion", conversion_window_hours=0),
        MetricMapping(("d", "t"), "u", "e", "proportion", conversion_window_hours=8761),
        MetricMapping(("d", "t"), "u", "e", "ratio"),  # type: ignore[arg-type]
    ):
        with pytest.raises(WarehouseQueryRefused):
            build_metric_query(d, a, m, "key", window)
    with pytest.raises(WarehouseQueryRefused):
        build_metric_query(
            d,
            a,
            MetricMapping(("d", "t"), "u", "e", "proportion"),
            "key",
            AnalysisWindow(window.end, window.start),
        )


_PATTERN_USERS = [
    Path(gate.__file__),
    Path(gate.__file__).parents[1] / "services" / "warehouse_sufficient_stats.py",
    Path(gate.__file__).parents[1] / "services" / "warehouse_query_builder.py",
]


@pytest.mark.parametrize("path", _PATTERN_USERS, ids=lambda p: p.name)
def test_patterns_are_applied_only_with_fullmatch(path):
    """No re.match/re.search/.match()/.search() -- they accept "X\\n" with $."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        f"{path.name}:{node.lineno} .{node.func.attr}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"match", "search"}
    ]
    assert offenders == []


def test_no_pattern_in_the_gate_carries_an_anchor():
    """Anchors would only matter to match/search; fullmatch needs none."""
    for name, value in vars(gate).items():
        if isinstance(value, re.Pattern):
            assert not value.pattern.startswith("^") and not value.pattern.endswith(
                "$"
            ), name


def test_float_literal_must_be_positive():
    assert gate.render_float_literal(2.5, "cap_value") == "2.5"
    assert gate.render_float_literal(500, "cap_value") == "500.0"
    assert gate.render_float_literal(1e-05, "cap_value") == "1e-05"
    for not_positive in (0, 0.0, -0.0, -1.5, -(10**6)):
        with pytest.raises(WarehouseQueryRefused) as refused:
            gate.render_float_literal(not_positive, "cap_value")
        assert str(refused.value) == "cap_value must be above 0"
    for not_a_number in (float("nan"), float("inf"), True, "2.5", None):
        with pytest.raises(WarehouseQueryRefused) as refused:
            gate.render_float_literal(not_a_number, "cap_value")
        assert str(refused.value) == "cap_value must be a number"


def test_no_source_file_holds_an_invisible_separator():
    """U+2028/U+2029 are written as escapes, so a reader sees them."""
    root = Path(gate.__file__).parents[1]
    paths = [
        root / "core" / "warehouse_identifiers.py",
        root / "schemas" / "warehouse_sources.py",
        root / "services" / "warehouse_query_builder.py",
        root / "services" / "warehouse_sufficient_stats.py",
        *Path(__file__).parent.glob("*.py"),
    ]
    offenders = [
        p.name
        for p in paths
        if {"\u2028", "\u2029"} & set(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
