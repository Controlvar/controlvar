"""G5: a targeted flag serves the users the docs say, and no one else.

Use case #5 (D210), targeting by attributes. O-TARGET
(``ground_truth.targeting``) is two flags and nineteen users, each user's answer
written from a docs sentence and importing nothing from the product. Every row
goes through the server's public ``FeatureFlagService.evaluate_flag_detailed``
(no database: the metrics writes are stubbed) and must come back as the row's
``(enabled, reason)``, so a server change that moves a documented answer fails
here, on the row the docs give it: text compared without case fails d7,
versions compared as text fail s3, a boolean no longer read as ``"true"`` fails
d10.

The ruleset the SDKs evaluate from is what the rows' ``local`` legs assume:
``sdk_ruleset.build_flag_entry`` marks the documented example ``local``, with
its one rule at 100%, and the semantic-version flag ``remote``.

The table's own check refuses what no server answer could make right; each
refusal has a test with a planted bad row. That each row's sentence is still on
its page is ``backend/tests/unit/docs/test_targeting_example_docs.py``'s.
"""

from __future__ import annotations

import copy
import dataclasses
import uuid
from unittest.mock import MagicMock

import pytest

from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services import sdk_ruleset
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.metrics_service import MetricsService
from backend.tests.unit.ground_truth.library import targeting

pytestmark = pytest.mark.unit

DOC_KEY = "o-target-doc"
SEM_KEY = "o-target-sem"


def _flag(key: str, rules: dict) -> FeatureFlag:
    return FeatureFlag(
        id=uuid.uuid5(uuid.NAMESPACE_URL, key),
        key=key,
        name=key,
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=targeting.ROLLOUT_PERCENTAGE,
        # A copy, so nothing the server does to its rules can edit the oracle.
        targeting_rules=copy.deepcopy(rules),
    )


FLAGS = {
    row.id: (DOC_KEY, targeting.DOCUMENTED_RULES) for row in targeting.DOC_ROWS
} | {row.id: (SEM_KEY, targeting.SEMVER_RULES) for row in targeting.SEM_ROWS}


@pytest.fixture
def service(monkeypatch) -> FeatureFlagService:
    """The server's evaluator with its metrics writes stubbed (no database)."""
    monkeypatch.setattr(
        MetricsService, "record_flag_evaluation", staticmethod(lambda **_: None)
    )
    monkeypatch.setattr(MetricsService, "log_error", staticmethod(lambda **_: None))
    return FeatureFlagService(db=MagicMock())


@pytest.mark.parametrize("row", targeting.ROWS, ids=lambda row: row.id)
def test_the_server_gives_every_row_the_docs_answer(service, row):
    key, rules = FLAGS[row.id]
    answer = service.evaluate_flag_detailed(
        _flag(key, rules), f"uc-g5-{row.id}", copy.deepcopy(row.context)
    )
    found = (answer["enabled"], answer["reason"])
    assert found == (row.enabled, row.reason), (
        f"{row.id}: the server answered {found}, the table says "
        f"({row.enabled}, {row.reason!r}); the docs: {row.basis_file}: "
        f"{row.basis_quote}"
    )


def test_the_documented_example_is_evaluated_locally_with_one_rule_at_100():
    entry = sdk_ruleset.build_flag_entry(_flag(DOC_KEY, targeting.DOCUMENTED_RULES))
    assert entry["evaluation"] == "local", entry
    assert entry["rollout_percentage"] == targeting.ROLLOUT_PERCENTAGE, entry
    assert [rule["rollout_percentage"] for rule in entry["rules"]] == [100], entry
    assert entry["default_rule"] is None, entry


def test_the_semantic_version_flag_is_evaluated_by_the_server():
    entry = sdk_ruleset.build_flag_entry(_flag(SEM_KEY, targeting.SEMVER_RULES))
    assert entry == {"key": SEM_KEY, "active": True, "evaluation": "remote"}


def test_the_table_passes_its_own_check():
    targeting.check(targeting.DOC_ROWS, targeting.SEM_ROWS)
    assert [row.id for row in targeting.ROWS] == [
        *(f"d{n}" for n in range(1, 12)),
        *(f"s{n}" for n in range(1, 9)),
    ]


def _planted(rows: tuple, row_id: str, **changes) -> tuple:
    return tuple(
        dataclasses.replace(row, **changes) if row.id == row_id else row for row in rows
    )


def test_a_row_id_used_twice_is_refused():
    doubled = (*targeting.DOC_ROWS, dataclasses.replace(targeting.DOC_ROWS[2]))
    with pytest.raises(ValueError, match="row id d3 is used twice"):
        targeting.check(doubled, targeting.SEM_ROWS)


def test_a_reason_outside_the_two_is_refused():
    planted = _planted(targeting.DOC_ROWS, "d4", reason="inactive")
    with pytest.raises(ValueError, match="d4: reason 'inactive' is not one of"):
        targeting.check(planted, targeting.SEM_ROWS)


def test_a_row_enabled_by_the_rollout_is_refused():
    planted = _planted(targeting.DOC_ROWS, "d3", enabled=True, reason="rollout")
    with pytest.raises(ValueError, match="d3: enabled with reason rollout"):
        targeting.check(planted, targeting.SEM_ROWS)


def test_a_semantic_version_row_answered_locally_is_refused():
    planted = _planted(targeting.SEM_ROWS, "s2", local="local")
    with pytest.raises(ValueError, match="s2: a semantic-version row is answered"):
        targeting.check(targeting.DOC_ROWS, planted)
