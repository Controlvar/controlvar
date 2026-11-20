"""Every generated statement, pinned by its sha256: one dialect's fix moves no other.

The goldens (``test_golden_sql.py``) compare the builder with files the same
change can regenerate, so they cannot show that a change meant for one
dialect left the others alone.  This file can: each literal below was
written once, from the text before the change, and is never regenerated.

* The four production dialects x five kinds, built by
  ``test_golden_sql.build_case``.  The Athena, BigQuery and Snowflake literals
  are ``sha256`` of ``golden/{athena,bigquery,snowflake}_*.sql`` at main
  c60470ed, before the Redshift fix of #826 (those goldens equal the built
  statements there); the Redshift literals are those of the fix's goldens.
* The DuckDB token set the semantics tests run, for the three kinds it can
  build (it has no ``epoch_seconds``, so no preview), from the fixed mapping
  below passed to the builders.  Its literals were computed at main c60470ed.

A statement whose literal no longer matches has changed.  If that is meant,
read the new text against its warehouse's SQL reference and replace the
literal in the same change, saying why.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from modules.backend.app.core.warehouse_identifiers import SourceFilter
from modules.backend.app.services.warehouse_query_builder import (
    SQL_DIALECTS,
    AnalysisWindow,
    AssignmentMapping,
    MetricMapping,
    build_diagnostics_query,
    build_metric_query,
)
from modules.backend.tests.unit.warehouse.duckdb_adapter import DUCKDB_SQL
from modules.backend.tests.unit.warehouse.test_golden_sql import KINDS, build_case

pytestmark = pytest.mark.unit

PINNED = {
    ("athena", "diagnostics"): (
        "cf0d3efc9f4662f4887839279e71225ad32a6d9ec5c3984abbfacd6988e162f2"
    ),
    ("athena", "metric_mean"): (
        "ff8386c7f14797a5b129036625272b9ecaa1f3f213a10f43622b6f726bbf2cd0"
    ),
    ("athena", "metric_proportion"): (
        "f92b7e77753a05d1c4f7afdef400b43c17fb72c96dfd502981bbf21b6e072685"
    ),
    ("athena", "preview_assignment"): (
        "0d1a2709a6845663d01538c05fc092c71ba88d4f7d344d80699de5ea336cc2f9"
    ),
    ("athena", "preview_metric"): (
        "e3aeeab5dcee313b94e65015c1436989e46aff64912ad6539d209861df82009c"
    ),
    ("bigquery", "diagnostics"): (
        "abe67e0e3369afeec73e61094062bf39cb46f8c9543bfd46f7058377e36dbe1a"
    ),
    ("bigquery", "metric_mean"): (
        "b6b3eb8350b40fc0ed7d459f76a7fe694c11b82828e1a4a457ef2aa6a93109fc"
    ),
    ("bigquery", "metric_proportion"): (
        "d1adc4b2fbe35c1f6efb1f1cdce1995d8e3301a0e393ea79307d739c254e6957"
    ),
    ("bigquery", "preview_assignment"): (
        "9d749bd6dbe0e107703152b39a467c988b7865c0fffcba1b0acd531bf7b98948"
    ),
    ("bigquery", "preview_metric"): (
        "2e08df166111d06ae57093a27fdd9f602b7b1ae9c365d987f683b51ce45303d7"
    ),
    ("snowflake", "diagnostics"): (
        "c8f5a96d4121a229f6344ddabecf202c3259c44dc50b98f12a795dee1dca4609"
    ),
    ("snowflake", "metric_mean"): (
        "80c22621f5e4deb63ef52df8671efbd6b7931fa528348cdad926de5e42c46722"
    ),
    ("snowflake", "metric_proportion"): (
        "50a516808040a4d98c18217559d57deb681c0d7d9dd1e693ad3d5ab7599d5b68"
    ),
    ("snowflake", "preview_assignment"): (
        "13404090c32631b37f17a936d4b816a72d9fe868d7d0b777d616fac305573636"
    ),
    ("snowflake", "preview_metric"): (
        "bbc074119c04ab8d74d030a7f6cd31f4ceec0557606f7886a82ce400a3d6bf6b"
    ),
    # Redshift: SUBSTRING, and DATEADD on the TIMESTAMP form (#826).  The
    # metric preview has neither token and is unchanged from main.
    ("redshift", "diagnostics"): (
        "bbce27681fd17ee477bed6eaf4d6202acefbf9d21a830d5febbdc3019a2ef759"
    ),
    ("redshift", "metric_mean"): (
        "221f88a5ec5ff61d1d8cf6ff39a34b4c42fb1f704866802a49905f0affa9398a"
    ),
    ("redshift", "metric_proportion"): (
        "3c44fdedccf5d28b6450f764514b34505e634e5850d08eb71504177a4bcf41c4"
    ),
    ("redshift", "preview_assignment"): (
        "b277b19fd82fcf2a1759e5698cee4b650c596101fb2d9c9709864c91bcc5c930"
    ),
    ("redshift", "preview_metric"): (
        "10a9d36e3d6a466e713298e98e901bc70850619b911028fae7119198536f6468"
    ),
}

#: The DuckDB set's fixed mapping, passed to the builders as it is.
DUCKDB_KEY = "checkout-v2"
DUCKDB_WINDOW = AnalysisWindow(
    datetime(2026, 9, 1, tzinfo=timezone.utc),
    datetime(2026, 9, 27, 12, 30, tzinfo=timezone.utc),
)
DUCKDB_ASSIGNMENT = AssignmentMapping(
    ("main", "exposures"),
    "user_id",
    "experiment_key",
    "variant",
    "exposed_at",
    (SourceFilter("platform", "in", ("web", "ios")),),
)


def _duckdb_metric(mean: bool) -> MetricMapping:
    return MetricMapping(
        ("main", "orders"),
        "user_id",
        "event_at",
        "mean" if mean else "proportion",
        "amount" if mean else None,
        168,
        500.0 if mean else None,
        (
            SourceFilter("status", "eq", "paid"),
            SourceFilter("is_test", "ne", True),
            SourceFilter("amount", "is_not_null"),
        ),
    )


DUCKDB_PINNED = {
    "metric_mean": "02d6684865be5afa838fdc1f01ecac88c53f0b75cc9df84fd47eef0d363520bd",
    "metric_proportion": (
        "885630e52ae6491cc966932aed9bb065668450c0239aef40a45813f20146d796"
    ),
    "diagnostics": "5c1a1a82c13a68fa18733373bc5e48ff6ddfb281cc7973e0f1bbeef7e18c20cb",
}


def build_duckdb(kind: str):
    if kind == "diagnostics":
        return build_diagnostics_query(
            DUCKDB_SQL, DUCKDB_ASSIGNMENT, DUCKDB_KEY, DUCKDB_WINDOW
        )
    return build_metric_query(
        DUCKDB_SQL,
        DUCKDB_ASSIGNMENT,
        _duckdb_metric(kind == "metric_mean"),
        DUCKDB_KEY,
        DUCKDB_WINDOW,
    )


def _sha256(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def test_the_pins_cover_every_dialect_and_kind() -> None:
    assert set(PINNED) == {(d, k) for d in SQL_DIALECTS for k in KINDS}
    assert set(DUCKDB_PINNED) == {k for k in KINDS if not k.startswith("preview")}


@pytest.mark.parametrize(("dialect", "kind"), sorted(PINNED))
def test_each_statement_is_the_pinned_text(dialect: str, kind: str) -> None:
    assert _sha256(build_case(dialect, kind).sql) == PINNED[(dialect, kind)]


@pytest.mark.parametrize("kind", sorted(DUCKDB_PINNED))
def test_each_duckdb_statement_is_the_pinned_text(kind: str) -> None:
    assert _sha256(build_duckdb(kind).sql) == DUCKDB_PINNED[kind]
