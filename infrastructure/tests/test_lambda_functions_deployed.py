"""Which Lambda functions the CDK deploys, and that none serves requests (#125, #1142).

The stacks deploy two placeholders whose inline code returns 200 and the Glue
ETL trigger, which a daily EventBridge rule starts; the code under
backend/lambda/ is deployed by nothing. This synthesises prod (full profile).
Split from ``test_lambda_functions_doc.py``, which holds the pages to this list
and is the only one of the two that reads documentation.
"""

from __future__ import annotations

import re

import pytest

from .test_dashboard_service import _synth

pytestmark = pytest.mark.regression

#: The functions the stacks define, by logical-id prefix, and whether each is a
#: placeholder (inline code that only returns 200).
EXPECTED = {
    "DatabaseAccessLambda": True,
    "AnalyticsLambda": True,
    "ETLTriggerLambda": False,
}
#: CDK's own singleton providers (bucket auto-delete, log retention), which
#: are not application functions.
CDK_PROVIDER = re.compile(r"^(AWS[0-9a-f]{32}|CustomS3AutoDeleteObjects|LogRetention)")


@pytest.fixture(scope="module")
def functions() -> dict:
    """{logical-id prefix: properties} for every application Lambda in prod."""
    found = {}
    for stack in _synth("prod").stacks:
        for lid, r in stack.template.get("Resources", {}).items():
            if r["Type"] != "AWS::Lambda::Function" or CDK_PROVIDER.match(lid):
                continue
            prefix = re.sub(r"[0-9A-F]{8}$", "", lid)
            assert prefix not in found, f"two functions named {prefix}"
            found[prefix] = r["Properties"]
    return found


def test_exactly_these_functions_are_deployed(functions):
    assert set(functions) == set(EXPECTED), sorted(functions)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_placeholders_are_placeholders(functions, name):
    code = functions[name]["Code"]
    inline = code.get("ZipFile", "")
    if EXPECTED[name]:
        assert "'statusCode': 200" in inline, (name, inline)
        # Nothing but a return: no import, no client, no loop.
        assert "import" not in inline and "boto3" not in inline, (name, inline)
    else:
        assert "glue.start_job_run" in inline, (name, inline[:200])


def test_nothing_deploys_backend_lambda(functions):
    for name, props in functions.items():
        assert "S3Key" not in props["Code"], f"{name} is deployed from an asset"


def test_the_etl_trigger_runs_from_a_daily_eventbridge_rule():
    rules = {}
    for stack in _synth("prod").stacks:
        for lid, r in stack.template.get("Resources", {}).items():
            if r["Type"] == "AWS::Events::Rule":
                rules[(stack.stack_name, lid)] = r["Properties"]
    assert len(rules) == 1, sorted(rules)
    (((stack_name, _), rule),) = rules.items()
    assert stack_name == "experimentation-glue-etl-prod"
    assert rule["ScheduleExpression"] == "cron(0 2 * * ? *)"
    (target,) = rule["Targets"]
    assert "ETLTriggerLambda" in str(target["Arn"])
