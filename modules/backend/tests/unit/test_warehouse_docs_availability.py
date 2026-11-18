"""The warehouse analysis page says which warehouses are available, and no others.

Enabling a connector (``ENABLED_CONNECTORS``) is a public claim, and the page
that says which warehouses are available has to change in the same pull
request.  The page's other statements are pinned in core,
``backend/tests/unit/docs/test_warehouse_docs_claims.py``.
"""

from __future__ import annotations

import json
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


NAMES = {
    "bigquery": "BigQuery",
    "snowflake": "Snowflake",
    "athena": "Amazon Athena",
    "redshift": "Amazon Redshift",
}


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


# -- the Amazon Redshift setup's two JSON policies (#826) ------------------------------


def _redshift_setup_json() -> list[object]:
    """The parsed ``json`` fences of ``## Setting up Amazon Redshift``."""
    section: list[str] = []
    in_section = False
    for line in PAGE.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip() == "Setting up Amazon Redshift"
        elif in_section:
            section.append(line)
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in section:
        stripped = line.strip()
        if current is None and stripped == "```json":
            current = []
        elif current is not None and stripped == "```":
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    assert current is None, "an unclosed json fence"
    return [json.loads("\n".join(block)) for block in blocks]


def test_the_redshift_setup_json_parses_and_keeps_what_the_run_proved():
    """The trust policy needs the external ID; the permissions policy lets a
    role touch only its own statements and signs in to exactly what it names.

    Both mirror the policies the green live run (wl-redshift-20261118T011715Z-6b77b53f)
    used, with its resource names replaced by placeholders.
    """
    trust, permissions = _redshift_setup_json()

    (assume,) = trust["Statement"]
    assert assume["Effect"] == "Allow" and assume["Action"] == "sts:AssumeRole"
    assert list(assume["Condition"]["StringEquals"]) == ["sts:ExternalId"]

    by_sid = {s["Sid"]: s for s in permissions["Statement"]}
    assert list(by_sid) == [
        "RunInTheWarehouse",
        "OwnStatementsOnly",
        "ServerlessSignIn",
        "ClusterSignIn",
    ]
    own = by_sid["OwnStatementsOnly"]
    assert own["Action"] == [
        "redshift-data:DescribeStatement",
        "redshift-data:GetStatementResult",
        "redshift-data:CancelStatement",
    ]
    assert own["Condition"] == {
        "StringEquals": {"redshift-data:statement-owner-iam-userid": "${aws:userid}"}
    }
    assert by_sid["RunInTheWarehouse"]["Action"] == [
        "redshift-data:BatchExecuteStatement",
        "redshift-data:DescribeTable",
    ]
    # Everything but the statement-owner statement names its resources.
    for sid, statement in by_sid.items():
        if sid == "OwnStatementsOnly":
            continue
        resources = statement["Resource"]
        for resource in [resources] if isinstance(resources, str) else resources:
            assert "*" not in resource, (sid, resource)
    actions = {
        a
        for s in permissions["Statement"]
        for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }
    assert not any("ListStatements" in a for a in actions), actions
