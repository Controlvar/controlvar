"""Connectors ship disabled until verified, and recorded fixtures say where they came from."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.backend.app.models.warehouse_connection import WAREHOUSE_TYPES
from modules.backend.app.warehouse import connectors
from modules.backend.app.warehouse.connectors import (
    ENABLED_CONNECTORS,
    KNOWN_CONNECTORS,
    ConnectorDisabled,
    is_enabled,
    require_enabled,
)

pytestmark = pytest.mark.unit

RECORDED = Path(__file__).parent / "recorded"

#: Where each vendor's documentation lives; a recorded file's URL must be there.
VENDOR_DOC_ROOTS = {
    "bigquery": ("https://docs.cloud.google.com/", "https://developers.google.com/"),
    "snowflake": ("https://docs.snowflake.com/",),
}


#: The connectors whose real check has passed, each enabled by its own pull
#: request carrying the run id (Snowflake: wl-snowflake-20261004T225624Z-3787b33c;
#: BigQuery: wl-bigquery-20261005T150837Z-7782e712).
SHIPPED_ENABLED = frozenset({"snowflake", "bigquery"})
#: Every other connector ships disabled.
SHIPPED_DISABLED = ("athena", "redshift")


def test_enabled_connectors_exact():
    """Only a connector whose real check has passed is enabled (a flip PR changes this)."""
    assert ENABLED_CONNECTORS == SHIPPED_ENABLED
    assert isinstance(ENABLED_CONNECTORS, frozenset)
    assert set(KNOWN_CONNECTORS) == set(WAREHOUSE_TYPES)
    assert set(SHIPPED_DISABLED) == set(KNOWN_CONNECTORS) - SHIPPED_ENABLED


@pytest.mark.parametrize("warehouse_type", sorted(SHIPPED_ENABLED))
def test_verified_connectors_are_enabled_as_shipped(warehouse_type):
    assert require_enabled(warehouse_type) == warehouse_type
    assert is_enabled(warehouse_type)


@pytest.mark.parametrize("warehouse_type", SHIPPED_DISABLED)
def test_disabled_connector_refused_with_422(warehouse_type):
    with pytest.raises(ConnectorDisabled) as err:
        require_enabled(warehouse_type)
    assert err.value.status_code == 422
    body = err.value.to_body()
    assert body["code"] == "connector_disabled"
    assert body["message"].endswith("isn't available on this deployment yet.")
    assert not is_enabled(warehouse_type)


def test_disabled_copy_names_the_warehouse():
    assert ConnectorDisabled("bigquery").message == (
        "BigQuery isn't available on this deployment yet."
    )
    assert ConnectorDisabled("athena").message == (
        "Amazon Athena isn't available on this deployment yet."
    )
    assert ConnectorDisabled("redshift").message == (
        "Amazon Redshift isn't available on this deployment yet."
    )


@pytest.mark.parametrize("value", ["BigQuery", "bigquery ", "postgres", "", None, 1])
def test_unknown_types_refused(value):
    with pytest.raises(ConnectorDisabled):
        require_enabled(value, frozenset(KNOWN_CONNECTORS))


def test_an_enabled_connector_is_allowed():
    assert require_enabled("bigquery", frozenset({"bigquery"})) == "bigquery"
    assert not is_enabled("snowflake", frozenset({"bigquery"}))


def test_there_is_no_setting_that_enables_a_connector():
    """Enabling is a code change, not configuration: nothing here reads settings."""
    source = Path(connectors.__file__).read_text(encoding="utf-8")
    assert "settings" not in source and "environ" not in source


def _recorded_files():
    return sorted(RECORDED.rglob("*.json"))


def test_there_are_recorded_fixtures():
    assert len(_recorded_files()) >= 10


@pytest.mark.parametrize(
    "path", _recorded_files(), ids=lambda p: str(p.relative_to(RECORDED))
)
def test_recorded_fixtures_declare_provenance(path):
    """Each recorded response names its vendor-doc row and URL, or a real run id."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"_provenance", "status", "body"}
    provenance = data["_provenance"]
    real_run = provenance.get("real_run_id")
    if real_run:
        assert isinstance(real_run, str) and real_run.strip()
        return
    assert provenance.get("source") == "vendor documentation"
    assert isinstance(provenance.get("vendor_docs_row"), str)
    assert provenance["vendor_docs_row"].strip()
    vendor = path.relative_to(RECORDED).parts[0]
    assert provenance.get("url", "").startswith(VENDOR_DOC_ROOTS[vendor])
    assert isinstance(provenance.get("note"), str) and provenance["note"].strip()
