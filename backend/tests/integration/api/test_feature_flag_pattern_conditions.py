"""
Regex (``regex``) conditions through the SDK evaluate endpoint.

When a pattern condition cannot be evaluated -- RE2 refuses the pattern, or the
context value cannot be evaluated -- the flag answers ``enabled: false`` with
``reason: "error"``. It does not fall through to the global rollout, and no
error-log row is written (the safety monitor divides error-log rows by
evaluations; a stored rule is not a client error).
"""

import json

import pytest

from backend.app.core import pattern_match
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.services import feature_flag_service
from backend.tests.integration.helpers import unique_flag_key


def exclusion_rules(pattern: str) -> dict:
    """Users whose email matches get 0%; everyone else the global 100%."""
    return {
        "logical_operator": "AND",
        "rollout_percentage": 0,
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "email", "operator": "regex", "value": pattern}
                ],
            }
        ],
    }


@pytest.fixture
def make_pattern_flag(db_session, make_feature_flag):
    created = []

    def make(pattern: str):
        flag = make_feature_flag(
            key=unique_flag_key("pattern"),
            name="Pattern condition",
            status=FeatureFlagStatus.ACTIVE,
            rollout_percentage=100,
            targeting_rules=exclusion_rules(pattern),
        )
        created.append(flag.id)
        return flag

    pattern_match.clear_compiled_patterns()
    yield make
    db_session.rollback()
    for flag_id in created:
        db_session.query(RawMetric).filter(
            RawMetric.feature_flag_id == flag_id
        ).delete()
        db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag_id).delete()
        db_session.query(FeatureFlag).filter(FeatureFlag.id == flag_id).delete()
    db_session.commit()


class _Recorder:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message, *args, **kwargs):
        self.errors.append(message % args if args else message)

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message % args if args else message)

    def debug(self, *args, **kwargs):
        pass

    info = debug


@pytest.fixture
def logs(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(feature_flag_service, "logger", recorder)
    monkeypatch.setattr(pattern_match, "logger", recorder)
    pattern_match._reported.clear()
    return recorder


def _evaluate(client, flag, context):
    return client.get(
        f"/api/v1/feature-flags/evaluate/{flag.key}",
        params={"user_id": "pattern-user", "context": json.dumps(context)},
    )


@pytest.mark.regression
def test_refused_pattern_disables_the_flag(admin_client, db_session, make_pattern_flag):
    flag = make_pattern_flag("[@competitor\\.com$")
    resp = _evaluate(admin_client, flag, {"email": "bob@competitor.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "key": flag.key,
        "enabled": False,
        "value": False,
        "value_type": "boolean",
        "config": None,
        "reason": "error",
    }
    db_session.expire_all()
    assert (
        db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).count()
        == 0
    )


def test_valid_pattern_still_excludes(admin_client, make_pattern_flag):
    flag = make_pattern_flag("@competitor\\.com$")
    excluded = _evaluate(admin_client, flag, {"email": "bob@competitor.com"})
    assert excluded.json()["enabled"] is False
    assert excluded.json()["reason"] == "targeting_rule"
    included = _evaluate(admin_client, flag, {"email": "ann@example.com"})
    assert included.json()["enabled"] is True
    assert included.json()["reason"] == "rollout"


@pytest.mark.regression
def test_lone_surrogate_in_context_disables_the_flag(
    admin_client, make_pattern_flag, logs
):
    """The value cannot be encoded for matching: the ruleset is abandoned.

    Asserted on what distinguishes that outcome from a generic evaluation
    error (which would also answer ``reason: "error"``): the pattern module
    reports the abandoned ruleset, and the flag service logs no evaluation
    error.
    """
    flag = make_pattern_flag("@competitor\\.com$")
    resp = _evaluate(admin_client, flag, {"email": "bob@competitor.com\ud800"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    assert resp.json()["reason"] == "error"
    assert [w for w in logs.warnings if "reason=input_encoding" in w], logs.warnings
    assert not [e for e in logs.errors if e.startswith("Error evaluating flag")], (
        logs.errors
    )


def test_the_upgrade_check_lists_the_stored_pattern(make_pattern_flag, capsys):
    from backend.scripts import check_regex_rules

    refused = make_pattern_flag("(?<=@)competitor\\.com")
    ascii_class = make_pattern_flag("^\\w+@competitor")
    assert check_regex_rules.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert (
        f"feature_flag\t{refused.id}\t{refused.key}\tgroups[0].conditions[0].value"
        "\tREFUSED\tinvalid perl operator: (?<="
    ) in lines
    assert (
        f"feature_flag\t{ascii_class.id}\t{ascii_class.key}"
        "\tgroups[0].conditions[0].value\tASCII_CLASS\tASCII only: \\w"
    ) in lines
