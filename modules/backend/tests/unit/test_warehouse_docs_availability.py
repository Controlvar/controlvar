"""The warehouse analysis page says which warehouses are available, and no others.

Enabling a connector (``ENABLED_CONNECTORS``) is a public claim, and the page
that says which warehouses are available has to change in the same pull
request.  The page's other statements are pinned in core,
``backend/tests/unit/docs/test_warehouse_docs_claims.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from modules.backend.app.warehouse.connectors import (
    ENABLED_CONNECTORS,
    KNOWN_CONNECTORS,
)
from modules.backend.app.warehouse.errors import (
    MESSAGES,
    WarehouseError,
    WarehouseErrorCode,
)

pytestmark = pytest.mark.unit

PAGE = Path(__file__).resolve().parents[4] / "docs" / "api" / "warehouse-analytics.md"


NAMES = {"bigquery": "BigQuery", "snowflake": "Snowflake", "athena": "Amazon Athena"}


def _text() -> str:
    return re.sub(r"\s+", " ", PAGE.read_text(encoding="utf-8"))


def test_the_page_says_no_warehouse_is_available_exactly_while_none_is():
    says_none = "no warehouse is available yet" in _text().lower()
    assert says_none == (not ENABLED_CONNECTORS)


@pytest.mark.parametrize("warehouse_type", KNOWN_CONNECTORS)
def test_the_page_says_a_warehouse_is_available_exactly_while_it_is(warehouse_type):
    says_available = f"{NAMES[warehouse_type]} is available" in _text()
    assert says_available == (warehouse_type in ENABLED_CONNECTORS)


# -- the Troubleshooting section (#1809) --------------------------------------

_POINTER = re.compile(
    r"See (?P<section>[A-Z][A-Za-z ]{2,40}) in the warehouse analysis docs\."
)


def _headings() -> list[str]:
    return [
        line.lstrip("#").strip()
        for line in PAGE.read_text(encoding="utf-8").splitlines()
        if re.match(r"#{1,6} ", line)
    ]


def _troubleshooting_rows() -> list[list[str]]:
    """The cells of each body row of the table under ``## Troubleshooting``."""
    rows: list[list[str]] = []
    in_section = False
    for line in PAGE.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip() == "Troubleshooting"
        elif in_section and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells[0] != "Code" and not set(cells[0]) <= {"-", ":"}:
                rows.append(cells)
    return rows


@pytest.mark.regression
def test_the_unrecognised_error_text_names_a_section_the_page_has():
    """#1809: the text said "See Troubleshooting." and the page had no such heading."""
    match = _POINTER.search(MESSAGES[WarehouseErrorCode.UNRECOGNISED_WAREHOUSE_ERROR])
    assert match, "the text must say: See <Section> in the warehouse analysis docs."
    assert match.group("section") in _headings()


def test_the_troubleshooting_table_has_one_row_per_error_code_with_its_status():
    rows = _troubleshooting_rows()
    codes = [row[0].strip("`") for row in rows]
    assert sorted(codes) == sorted(code.value for code in WarehouseErrorCode)
    assert set(MESSAGES) == set(WarehouseErrorCode)
    for row in rows:
        code = WarehouseErrorCode(row[0].strip("`"))
        assert row[1] == str(WarehouseError(code).status_code), code.value
