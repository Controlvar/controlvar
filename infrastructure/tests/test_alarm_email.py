"""``ALARM_EMAIL``: who the alarms email, and the rollback alarms announcing themselves.

DECISIONS D21. The monitoring topic ``experimentation-alerts-<env>`` had one
subscriber, the literal ``alerts@example.com``, so every monitoring alarm told
nobody. The API's two 5xx alarms, which make CodeDeploy roll a deployment back
by itself, had no action at all, so an automatic rollback was silent.

Pinned here, on a real ``app.synth()`` where it matters:

* the rule (``stacks/alarm_email.py``): each refusal, and the acceptance of a
  real address;
* staging and prod refuse to synthesise without the setting, and the refusal
  names ``ALARM_EMAIL`` and says how to set it;
* staging and prod have exactly one EMAIL subscription, to the given address;
* both ``experimentation-api-5xx-{blue,green}-<env>`` alarms have the topic,
  and only the topic, as their ALARM action, and no OK or INSUFFICIENT_DATA
  action;
* dev and demo without the setting have no subscription at all.
"""

from __future__ import annotations

import runpy
import sys

import aws_cdk as cdk
import pytest

from .test_app_profiles import CDK_DIR, TEST_ALARM_EMAIL, _app_environment

REQUIRED = ("staging", "prod")
OPTIONAL = ("dev", "demo")


def _resolve():
    sys.path.insert(0, str(CDK_DIR))
    try:
        from stacks.alarm_email import resolve_alarm_email
    finally:
        sys.path.remove(str(CDK_DIR))
    return resolve_alarm_email


def _run_app(environment: str, **overrides):
    """Run ``app.py`` for ``environment`` without synthesising; the namespace."""
    synth = cdk.App.synth
    cdk.App.synth = lambda self, **kwargs: None  # type: ignore[method-assign]
    try:
        with _app_environment(CDK_DIR, ENVIRONMENT=environment, **overrides):
            return runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    finally:
        cdk.App.synth = synth  # type: ignore[method-assign]


def _assembly(environment: str, **overrides):
    with _app_environment(CDK_DIR, ENVIRONMENT=environment, **overrides):
        namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    return namespace["app"].synth()


def _stack(assembly, kind: str) -> dict:
    (stack,) = [s for s in assembly.stacks if f"-{kind}-" in s.stack_name]
    return stack.template


def _of_type(template: dict, rtype: str) -> dict:
    return {
        lid: r for lid, r in template.get("Resources", {}).items() if r["Type"] == rtype
    }


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------

REFUSED = {
    "blank": "   ",
    "empty": "",
    "too long": "a" * 243 + "@example.org.uk",  # 258 characters
    "exactly 255": "a" * 240 + "@controlvar.com",
    "whitespace inside": "ops team@controlvar.com",
    "leading whitespace": " hello@controlvar.com",
    "trailing newline": "hello@controlvar.com\n",
    "tab": "hello@controlvar.com\t",
    "no @": "hello.controlvar.com",
    "two @": "hello@ops@controlvar.com",
    "nothing before @": "@controlvar.com",
    "nothing after @": "hello@",
    "domain with no dot": "hello@localhost",
    "example.com": "alerts@example.com",
    "example.net": "alerts@example.net",
    "example.org": "alerts@example.org",
    "upper-case example.com": "alerts@EXAMPLE.COM",
    "mixed-case example.org": "alerts@Example.Org",
    "subdomain of example.com": "alerts@mail.example.com",
    "deep subdomain of example.net": "alerts@a.b.EXAMPLE.net",
    "example.com with a trailing dot": "alerts@example.com.",
}


@pytest.mark.parametrize("environment", REQUIRED + OPTIONAL)
@pytest.mark.parametrize("value", REFUSED.values(), ids=REFUSED.keys())
def test_a_bad_value_is_refused_in_every_environment(value, environment):
    """Refused wherever it is given; blank is refused only where it is required."""
    resolve = _resolve()
    if not value.strip() and environment in OPTIONAL:
        assert resolve(value, environment) is None
        return
    with pytest.raises(ValueError) as excinfo:
        resolve(value, environment)
    message = str(excinfo.value)
    assert "ALARM_EMAIL" in message, message
    assert "export ALARM_EMAIL=" in message, message


@pytest.mark.parametrize(
    "value",
    [
        "hello@controlvar.com",
        "Hello@Controlvar.com",
        TEST_ALARM_EMAIL,
        # Only the documentation domains themselves, not look-alikes.
        "ops@notexample.com",
        "ops@example.company",
        "ops@example.com.au",
        # Exactly 254 characters is the limit, and allowed.
        "a" * 239 + "@controlvar.com",
    ],
)
@pytest.mark.parametrize("environment", REQUIRED + OPTIONAL)
def test_a_real_address_is_accepted_unchanged(value, environment):
    assert len(value) <= 254
    assert _resolve()(value, environment) == value


@pytest.mark.parametrize("environment", REQUIRED)
@pytest.mark.parametrize("value", [None, "", "  "], ids=["unset", "empty", "blank"])
def test_staging_and_prod_require_it(environment, value):
    with pytest.raises(ValueError) as excinfo:
        _resolve()(value, environment)
    message = str(excinfo.value)
    assert f"ALARM_EMAIL is required in {environment}" in message, message
    assert "export ALARM_EMAIL=" in message, message
    assert "confirm-subscription --authenticate-on-unsubscribe true" in message


@pytest.mark.parametrize("environment", OPTIONAL)
def test_dev_and_demo_do_not(environment):
    assert _resolve()(None, environment) is None


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


@pytest.mark.parametrize("environment", REQUIRED)
@pytest.mark.parametrize("value", [None, "", "alerts@example.com"])
def test_the_app_refuses_to_synthesise_staging_or_prod_without_it(environment, value):
    """What an operator sees from ``cdk synth`` with the setting missing or a placeholder."""
    with pytest.raises(ValueError) as excinfo:
        _run_app(environment, ALARM_EMAIL=value)
    assert "ALARM_EMAIL" in str(excinfo.value)
    assert "export ALARM_EMAIL=" in str(excinfo.value)


@pytest.fixture(scope="module", params=REQUIRED)
def required(request):
    return request.param, _assembly(request.param)


@pytest.mark.regression
def test_staging_and_prod_email_exactly_the_given_address(required):
    """One EMAIL subscription on the alerts topic, to ALARM_EMAIL -- not the placeholder."""
    environment, assembly = required
    monitoring = _stack(assembly, "monitoring")
    (topic_id,) = _of_type(monitoring, "AWS::SNS::Topic")
    subscriptions = [
        s["Properties"] for s in _of_type(monitoring, "AWS::SNS::Subscription").values()
    ]
    assert subscriptions == [
        {
            "Protocol": "email",
            "TopicArn": {"Ref": topic_id},
            "Endpoint": TEST_ALARM_EMAIL,
        }
    ], subscriptions
    # Nowhere in the whole app is the placeholder left.
    for stack in assembly.stacks:
        assert "alerts@example.com" not in str(stack.template), stack.stack_name
    # And no other stack subscribes anyone to anything.
    others = [
        s.stack_name
        for s in assembly.stacks
        if s.stack_name != f"experimentation-monitoring-{environment}"
        and _of_type(s.template, "AWS::SNS::Subscription")
    ]
    assert not others, others


@pytest.mark.regression
def test_both_rollback_alarms_announce_themselves_on_the_topic(required):
    """The two alarms CodeDeploy rolls back on had no action: a silent rollback."""
    environment, assembly = required
    monitoring = _stack(assembly, "monitoring")
    fargate = _stack(assembly, "fargate")

    alarms = {
        r["Properties"]["AlarmName"]: r["Properties"]
        for r in _of_type(fargate, "AWS::CloudWatch::Alarm").values()
    }
    actions = {}
    for colour in ("blue", "green"):
        name = f"experimentation-api-5xx-{colour}-{environment}"
        assert name in alarms, sorted(alarms)
        properties = alarms[name]
        assert "OKActions" not in properties, (name, properties)
        assert "InsufficientDataActions" not in properties, (name, properties)
        assert len(properties.get("AlarmActions") or []) == 1, (
            f"{name} has no single ALARM action: an automatic rollback on it "
            f"tells nobody ({properties.get('AlarmActions')})"
        )
        actions[name] = properties["AlarmActions"][0]

    # The one action is the monitoring stack's alerts topic, imported by its
    # export -- not some other topic or ARN.
    (topic_id,) = _of_type(monitoring, "AWS::SNS::Topic")
    exports = [
        output["Export"]["Name"]
        for output in (monitoring.get("Outputs") or {}).values()
        if output.get("Value") == {"Ref": topic_id} and "Export" in output
    ]
    assert len(exports) == 1, monitoring.get("Outputs")
    for name, action in actions.items():
        assert action == {"Fn::ImportValue": exports[0]}, (name, action)


@pytest.mark.parametrize("environment", OPTIONAL)
def test_dev_and_demo_without_it_subscribe_nobody(environment):
    """Absent means no subscription, not a placeholder -- and the rest still synthesises."""
    assembly = _assembly(environment, ALARM_EMAIL=None)
    for stack in assembly.stacks:
        assert not _of_type(stack.template, "AWS::SNS::Subscription"), stack.stack_name
    monitoring = _stack(assembly, "monitoring")
    assert len(_of_type(monitoring, "AWS::SNS::Topic")) == 1
    # The rollback alarms still publish to the topic; it simply has no reader.
    fargate = _stack(assembly, "fargate")
    for alarm in _of_type(fargate, "AWS::CloudWatch::Alarm").values():
        if alarm["Properties"]["AlarmName"].startswith("experimentation-api-5xx-"):
            assert len(alarm["Properties"].get("AlarmActions") or []) == 1, alarm
