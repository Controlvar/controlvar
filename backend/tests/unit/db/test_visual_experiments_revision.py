"""``4955571d232c``, read without a database (#1545, EM45-10).

* Its downgrade calls ``drop_column``, so ``test_documented_downgrade_targets``
  sees it, and every documented downgrade target below it must name it with
  what it drops. That test only sees ``drop_column`` calls: written as
  ``drop_table`` alone or as ``op.execute("ALTER TABLE ... DROP COLUMN")``, the
  downgrade would cross every documented target unannounced.
* It extends the core head it sits on, ``c448_balance_within``, and its id
  fits ``alembic_version.version_num`` (32 characters).
* The model and the migration share the two checks' names and SQL, as
  ``segments.kind`` does: a check the model admits and the migration does not
  (or the reverse) would differ only between a fresh database and an upgraded
  one, and autogenerate compares neither.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from backend.app.models import visual_token
from backend.tests.unit.docs.test_documented_downgrade_targets import (
    _script,
    drops_a_column,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "4955571d232c"


def _path() -> Path:
    return Path(_script().get_revision(REVISION).path)


def _module():
    spec = importlib.util.spec_from_file_location("visual_revision", _path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_downgrade_drops_columns_as_the_downgrade_docs_test_reads_it():
    assert drops_a_column(str(_path())) is True


def test_it_extends_the_core_head_it_sits_on_and_fits_the_version_table():
    revision = _script().get_revision(REVISION)
    # Kept literal on purpose. Written on 7b6b4b4fe246 and re-pointed onto
    # c448_balance_within (#448's first migration, which merged first). A
    # re-point changes this line, every CORE_HEAD pin, the
    # "c448_balance_within" key of NAMED_DOWNGRADES in
    # test_db_migrate_target_graph.py, the three sentences that give this
    # revision's down_revision (migrations.md, the rollback runbook,
    # db-migrate.yml's help text) and the migration's own docstring, together.
    assert revision.down_revision == "c448_balance_within"
    assert len(REVISION) <= 32


def test_the_model_and_the_migration_share_the_checks():
    module = _module()
    assert module._KIND_CHECK == visual_token.VISUAL_TOKEN_KIND_CHECK
    assert module._KIND_CHECK_SQL == visual_token.VISUAL_TOKEN_KIND_CHECK_SQL
    assert module._REASON_CHECK == visual_token.VISUAL_TOKEN_REASON_CHECK
    assert module._REASON_CHECK_SQL == visual_token.VISUAL_TOKEN_REASON_CHECK_SQL


def test_the_checks_admit_every_designed_kind_and_reason():
    """editor_claim (C6's fallback) and scheduled (FD-S (b)) are admitted now,
    so neither needs a migration later (EM45-9)."""
    for kind in visual_token.VISUAL_TOKEN_KINDS:
        assert f"'{kind}'" in visual_token.VISUAL_TOKEN_KIND_CHECK_SQL
    for reason in visual_token.VISUAL_TOKEN_REVOKED_REASONS:
        assert f"'{reason}'" in visual_token.VISUAL_TOKEN_REASON_CHECK_SQL
    assert set(visual_token.VISUAL_TOKEN_KINDS) == {
        "editor_ticket",
        "editor_session",
        "editor_claim",
        "preview",
    }
    assert set(visual_token.VISUAL_TOKEN_REVOKED_REASONS) == {
        "reopened",
        "started",
        "ticket_reused",
        "signed_out",
        "scheduled",
    }
