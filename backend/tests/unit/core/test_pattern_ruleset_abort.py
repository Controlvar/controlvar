"""
A pattern condition that cannot be evaluated abandons the whole ruleset.

"Cannot be evaluated" is a pattern RE2 refuses, or a value that is too long or
not encodable. The outcome, in every evaluator:

* a feature flag evaluates **disabled with reason ``error``** -- not the
  global rollout, not a ``default_rule`` -- and writes no error-log row;
* a user is **not eligible** for an experiment (control);
* a user is **not a member** of a segment.

The shapes below are the ones where treating the condition (or the rule) as
"no match" would *grant* access: a ``NOT`` group, a 0% exclusion rule in front
of a 100% rollout, and a broad ``default_rule`` behind a narrow rule.
``[unterminated`` is refused by both engines, so these fail on the previous
code; ``(?<=@)`` is refused only by RE2.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.core import pattern_match
from backend.app.core.pattern_match import MAX_REGEX_INPUT, PatternUnevaluable
from backend.app.core.rules_engine import evaluate_rule
from backend.app.core.targeting_adapter import (
    match_targeting_rule,
    normalise_targeting_rules,
)
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.schemas.targeting_rule import TargetingRules
from backend.app.services import feature_flag_service as ffs
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.audience_service import AudienceService
from backend.app.services.rules_evaluation_service import RulesEvaluationService

pytestmark = pytest.mark.unit

UNPARSEABLE = "[unterminated"
RE2_ONLY_REFUSAL = r"(?<=@)competitor\.com$"

COMPETITOR = {"email": "bob@competitor.com", "country": "US"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    pattern_match.clear_compiled_patterns()
    yield
    pattern_match.clear_compiled_patterns()


def _regex(pattern: str, attribute: str = "email") -> dict:
    return {"attribute": attribute, "operator": "regex", "value": pattern}


def nested_not(pattern: str) -> dict:
    """Dashboard shape: NOT(email ~ pattern) AND country == US."""
    return {
        "logical_operator": "AND",
        "groups": [
            {"logical_operator": "NOT", "conditions": [_regex(pattern)]},
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "US"}
                ],
            },
        ],
    }


def zero_percent_exclusion(pattern: str) -> dict:
    """Dashboard shape: matching users get 0%; everyone else the global rollout."""
    return {
        "logical_operator": "AND",
        "rollout_percentage": 0,
        "groups": [{"logical_operator": "AND", "conditions": [_regex(pattern)]}],
    }


def narrow_rule_broad_default(pattern: str) -> dict:
    """Native shape: a narrow rule, then a default_rule that matches everyone."""
    return {
        "rules": [
            {
                "id": "competitors-off",
                "priority": 0,
                "rollout_percentage": 0,
                "rule": {
                    "operator": "and",
                    "conditions": [
                        {
                            "attribute": "email",
                            "operator": "match_regex",
                            "value": pattern,
                        }
                    ],
                },
            }
        ],
        "default_rule": {
            "id": "everyone",
            "rollout_percentage": 100,
            "rule": {"operator": "and", "conditions": []},
        },
    }


# -- the engine ---------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("pattern", [UNPARSEABLE, RE2_ONLY_REFUSAL])
def test_the_engine_raises_through_a_nested_not(pattern):
    rules = normalise_targeting_rules(nested_not(pattern))
    with pytest.raises(PatternUnevaluable):
        evaluate_rule(rules.rules[0], COMPETITOR)
    with pytest.raises(PatternUnevaluable):
        match_targeting_rule(rules, COMPETITOR)


@pytest.mark.regression
def test_the_engine_raises_for_a_lone_surrogate():
    rules = normalise_targeting_rules(nested_not(r"@competitor\.com$"))
    with pytest.raises(PatternUnevaluable):
        match_targeting_rule(rules, {"email": "bob@\ud800", "country": "US"})


# -- feature flags ------------------------------------------------------------


@pytest.fixture
def metrics(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(ffs, "MetricsService", fake)
    return fake


def _flag(targeting_rules, rollout=100):
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        key="pattern-abort",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=rollout,
        targeting_rules=targeting_rules,
    )


def _evaluate(flag, context=COMPETITOR, user_id="user-1"):
    return ffs.FeatureFlagService(db=MagicMock()).evaluate_flag_detailed(
        flag, user_id, context
    )


DISABLED_BY_ERROR = {"enabled": False, "reason": "error", "rule_id": None}


@pytest.mark.regression
@pytest.mark.parametrize("pattern", [UNPARSEABLE, RE2_ONLY_REFUSAL])
def test_flag_nested_not_is_disabled(metrics, pattern):
    assert _evaluate(_flag(nested_not(pattern))) == DISABLED_BY_ERROR
    metrics.log_error.assert_not_called()


@pytest.mark.regression
@pytest.mark.parametrize("pattern", [UNPARSEABLE, RE2_ONLY_REFUSAL])
def test_flag_zero_percent_rule_does_not_fall_through_to_rollout(metrics, pattern):
    assert _evaluate(_flag(zero_percent_exclusion(pattern))) == DISABLED_BY_ERROR
    metrics.log_error.assert_not_called()


@pytest.mark.regression
@pytest.mark.parametrize("pattern", [UNPARSEABLE, RE2_ONLY_REFUSAL])
def test_flag_broad_default_rule_is_not_returned(metrics, pattern):
    assert _evaluate(_flag(narrow_rule_broad_default(pattern))) == DISABLED_BY_ERROR
    metrics.log_error.assert_not_called()


@pytest.mark.regression
def test_flag_value_over_the_limit_is_disabled(metrics):
    context = {"email": "x" * MAX_REGEX_INPUT + "@competitor.com"}
    flag = _flag(zero_percent_exclusion(r"@competitor\.com$"))
    assert _evaluate(flag, context) == DISABLED_BY_ERROR
    metrics.log_error.assert_not_called()


def test_flag_valid_pattern_still_decides(metrics):
    flag = _flag(zero_percent_exclusion(r"@competitor\.com$"))
    assert _evaluate(flag) == {
        "enabled": False,
        "reason": "targeting_rule",
        "rule_id": "dashboard",
    }
    other = _evaluate(flag, {"email": "ann@example.com"})
    assert other == {"enabled": True, "reason": "rollout", "rule_id": None}


def test_flag_abort_is_logged_without_the_pattern(metrics, monkeypatch):
    seen = []
    monkeypatch.setattr(
        ffs, "report_unevaluable", lambda exc, where: seen.append((str(exc), where))
    )
    _evaluate(_flag(zero_percent_exclusion(UNPARSEABLE)))
    assert seen == [("pattern condition could not be evaluated", "flag:pattern-abort")]


# -- experiment targeting -----------------------------------------------------


def _native(dashboard_or_native: dict) -> TargetingRules:
    return normalise_targeting_rules(dashboard_or_native)


@pytest.mark.regression
@pytest.mark.parametrize(
    "rules",
    [
        nested_not(UNPARSEABLE),
        nested_not(RE2_ONLY_REFUSAL),
        narrow_rule_broad_default(UNPARSEABLE),
        narrow_rule_broad_default(RE2_ONLY_REFUSAL),
    ],
    ids=["nested-not", "nested-not-re2", "default-rule", "default-rule-re2"],
)
def test_experiment_targeting_is_ineligible(rules):
    service = RulesEvaluationService()
    matched, metrics = service.evaluate_rules_with_validation(
        _native(rules), COMPETITOR
    )
    assert matched is None
    assert metrics is not None and metrics.matched is False
    assert "competitor" not in (metrics.error or "")

    result = service.evaluate(_native(rules), COMPETITOR, skip_cache=True)
    assert result.matched is False

    outcome = AssignmentService(MagicMock())._evaluate_experiment_targeting(
        SimpleNamespace(targeting_rules=rules), COMPETITOR
    )
    assert outcome["eligible"] is False


# -- segments -----------------------------------------------------------------

# Segment rules are checked when saved (#440), so an unparseable pattern
# cannot be stored; a context value longer than MAX_REGEX_INPUT is what makes
# a pattern unevaluable at evaluation time. Without the abort, the NOT group
# would turn the unevaluable condition into a match.
SEGMENT_NESTED_NOT = {
    "logical_operator": "AND",
    "groups": [
        {"conditions": [{"attribute": "country", "operator": "equals", "value": "US"}]},
        {
            "logical_operator": "NOT",
            "conditions": [
                {
                    "attribute": "email",
                    "operator": "regex",
                    "value": r"@competitor\.com$",
                }
            ],
        },
    ],
}

LONG_COMPETITOR = {"email": "x" * MAX_REGEX_INPUT + "@competitor.com", "country": "US"}


@pytest.mark.regression
def test_segment_membership_is_not_granted(monkeypatch):
    segment = SimpleNamespace(
        id="seg-1", name="Not competitors", kind="rules", rules=SEGMENT_NESTED_NOT
    )
    monkeypatch.setattr(
        AudienceService, "get_segment", staticmethod(lambda db, sid: segment)
    )
    result = AudienceService.evaluate_membership(MagicMock(), "seg-1", LONG_COMPETITOR)
    assert result.is_member is False
    assert result.matched_rules == []


@pytest.mark.regression
def test_segment_preview_does_not_count_the_user():
    db = MagicMock()
    rows = db.query.return_value.filter.return_value.limit.return_value
    rows.all.return_value = [(LONG_COMPETITOR,)]
    preview = AudienceService.preview_audience_size(db, SEGMENT_NESTED_NOT, 10)
    assert preview.sample_size == 1
    assert preview.matched == 0


@pytest.mark.regression
def test_experiment_abandonment_is_reported_with_the_experiment_id(monkeypatch):
    from backend.app.services import rules_evaluation_service as res

    seen = []
    monkeypatch.setattr(
        res, "report_unevaluable", lambda exc, where: seen.append((str(exc), where))
    )
    outcome = AssignmentService(MagicMock())._evaluate_experiment_targeting(
        SimpleNamespace(id="exp-42", targeting_rules=nested_not(UNPARSEABLE)),
        COMPETITOR,
    )
    assert outcome["eligible"] is False
    assert seen == [("pattern condition could not be evaluated", "experiment:exp-42")]


def test_cached_evaluation_abandonment_is_reported(monkeypatch):
    from backend.app.services import rules_evaluation_service as res

    seen = []
    monkeypatch.setattr(
        res, "report_unevaluable", lambda exc, where: seen.append(where)
    )
    result = RulesEvaluationService().evaluate(
        _native(nested_not(UNPARSEABLE)), COMPETITOR, skip_cache=True
    )
    assert result.matched is False
    assert seen == ["targeting rules"]
