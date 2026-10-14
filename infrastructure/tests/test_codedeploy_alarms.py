"""The API's CodeDeploy deployment group rolls back on its 5xx alarms (#148).

Before this, nothing in the CDK suite pinned the deployment group at all: a
change that dropped its alarms, or kept the alarms and dropped the rollback,
passed every test. Everything here runs on a real ``app.synth()``, as exact
values, for every environment the deploy workflows reach, both profiles and
both values of ``api_live_target_group``:

* **Both colours** (QA P1a-c). CodeDeploy swaps blue and green on every
  deployment, so an alarm on one target group would watch the wrong one half
  the time. The group's alarms are exactly the two in this stack, and together
  their metrics name exactly the blue and green target groups -- never the
  dashboard's, and never a load-balancer-wide metric (which would include the
  dashboard and the load balancer's own 5xx).
* **The configuration** (P2, P3). Alarms enabled, a failed alarm read never
  ignored, and the three rollback events, STOP_ON_ALARM included. The tamper
  for P3 is ``deployment_in_alarm=False``: omitting the argument changes
  nothing (CDK defaults it on when alarms exist), while False keeps the alarms
  stopping deployments without rolling them back.
* **The alarm** (P4, P5). A copy-against-copy pin of the metric math and every
  parameter. It does not verify what CloudWatch will do with the expression;
  nothing offline can. What it does catch is an expression that names a metric
  it does not define: CDK only WARNS about that, so the synth's messages are
  asserted empty (PE condition 9b).
* **The service role** (P8) reads the alarms explicitly.
* **The dashboard has no alarm** (P13): a static nginx image answers 404, not
  5xx, and ECS deployment alarms there would be a second automatic rollback
  racing the dashboard rollout's PRIMARY verdict.
* **Names** (P14): per environment, so two environments in one account never
  collide (test_environments_do_not_collide.py checks every AlarmName too).
* **Nothing is replaced** when ``api_live_target_group`` flips: the same
  logical ids under blue and green.
* **The canary is 15 minutes** (#212, D47): the group's deployment config is
  exactly ``CodeDeployDefault.ECSCanary10Percent15Minutes``, the alarm window
  fits inside the canary that config gives, and deploy.yml's shift deadline is
  that canary plus the same 1500 s allowance for everything else.
"""

from __future__ import annotations

import json
import re
import runpy

import aws_cdk as cdk
import pytest
import yaml

from .test_app_profiles import CDK_DIR, REPO_ROOT, _app_environment

#: The environments the deploy workflows reach, plus dev (the default).
ENVIRONMENTS = ("dev", "staging", "prod")
PROFILES = ("full", "core")
LIVE = ("blue", "green")

EXPRESSION = "IF(FILL(e,0) >= 5, FILL(e,0)/r, 0)"
EVENTS = sorted(
    ["DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_REQUEST", "DEPLOYMENT_STOP_ON_ALARM"]
)
#: The one deployment config the API's group may use (#212, D47).
CANARY_CONFIG = "CodeDeployDefault.ECSCanary10Percent15Minutes"
#: How long each predefined config holds traffic at the canary step. Explicit,
#: so the canary length is derived from the synthesised name and a config not
#: listed here fails rather than being guessed at. An alarm that needs longer
#: than the canary to fire can never stop a deployment inside it.
CANARY_SECONDS_BY_CONFIG = {
    "CodeDeployDefault.ECSCanary10Percent15Minutes": 900,
    "CodeDeployDefault.ECSCanary10Percent5Minutes": 300,
    "CodeDeployDefault.ECSAllAtOnce": 0,
}
#: deploy.yml's CODEDEPLOY_DEADLINE_SECONDS minus the canary: starting the
#: replacement tasks, the approval and the final shift. Three staging runs used
#: 165-188 s of it. Equality, so the deadline neither shrinks below the canary
#: nor drifts away from it.
SHIFT_ALLOWANCE_SECONDS = 1500


def _synth(environment: str, profile: str, live: str):
    """``app.py`` synthesised the way the CLI would, with ``-c api_live_target_group``."""
    original = cdk.App.__init__

    def with_context(self, **kwargs):
        merged = dict(kwargs.pop("context", None) or {})
        merged["api_live_target_group"] = live
        original(self, context=merged, **kwargs)

    cdk.App.__init__ = with_context
    try:
        with _app_environment(
            CDK_DIR, ENVIRONMENT=environment, EXPERIMENTLY_PROFILE=profile
        ):
            namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    finally:
        cdk.App.__init__ = original
    assembly = namespace["app"].synth()
    (stack,) = [s for s in assembly.stacks if "-fargate-" in s.stack_name]
    return stack


CASES = [(e, p, "blue") for e in ENVIRONMENTS for p in PROFILES] + [
    (e, "full", "green") for e in ENVIRONMENTS
]


@pytest.fixture(scope="module")
def stacks():
    return {case: _synth(*case) for case in CASES}


def _ids(case):
    return "-".join(case)


def _of_type(resources: dict, rtype: str) -> dict:
    return {lid: r for lid, r in resources.items() if r["Type"] == rtype}


def _one(resources: dict, prefix: str, rtype: str) -> str:
    hits = [
        lid
        for lid, r in resources.items()
        if r["Type"] == rtype and re.fullmatch(rf"{prefix}[0-9A-F]{{8}}", lid)
    ]
    assert len(hits) == 1, f"expected one {rtype} {prefix}<hash>, found {hits}"
    return hits[0]


def _group(resources: dict) -> dict:
    (group,) = _of_type(resources, "AWS::CodeDeploy::DeploymentGroup").values()
    return group["Properties"]


def _canary_seconds(resources: dict) -> int:
    name = _group(resources)["DeploymentConfigName"]
    assert name in CANARY_SECONDS_BY_CONFIG, (
        f"unknown deployment config {name!r}: add its canary length to "
        "CANARY_SECONDS_BY_CONFIG"
    )
    return CANARY_SECONDS_BY_CONFIG[name]


def _group_alarms(resources: dict) -> dict[str, dict]:
    """The alarms the deployment group names, by logical id (each must be a Ref)."""
    alarms = _of_type(resources, "AWS::CloudWatch::Alarm")
    named = {}
    for entry in _group(resources)["AlarmConfiguration"]["Alarms"]:
        name = entry["Name"]
        assert isinstance(name, dict) and set(name) == {"Ref"}, (
            f"the deployment group names an alarm that is not a Ref to an alarm "
            f"in this stack: {name!r}"
        )
        assert name["Ref"] in alarms, (
            f"{name['Ref']} is not an alarm in this stack ({sorted(alarms)})"
        )
        named[name["Ref"]] = alarms[name["Ref"]]["Properties"]
    return named


def _metric_stats(alarm: dict) -> list[dict]:
    if "Metrics" in alarm:
        return [m["MetricStat"] for m in alarm["Metrics"] if "MetricStat" in m]
    return [
        {
            "Metric": {
                "Namespace": alarm.get("Namespace"),
                "MetricName": alarm.get("MetricName"),
                "Dimensions": alarm.get("Dimensions", []),
            }
        }
    ]


def _target_group_of(stat: dict) -> str | None:
    for dim in stat["Metric"].get("Dimensions", []):
        if dim["Name"] == "TargetGroup":
            value = dim["Value"]
            assert "Fn::GetAtt" in value, value
            assert value["Fn::GetAtt"][1] == "TargetGroupFullName", value
            return value["Fn::GetAtt"][0]
    return None


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_both_target_groups_and_nothing_else_are_watched(stacks, case):
    """P1a/P1b/P1c: exactly two alarms, together watching exactly blue and green."""
    resources = stacks[case].template["Resources"]
    lb = "AWS::ElasticLoadBalancingV2::TargetGroup"
    blue = _one(resources, "BlueTargetGroup", lb)
    green = _one(resources, "GreenTargetGroup", lb)
    dashboard = _one(resources, "DashboardTargetGroup", lb)

    alarms = _group_alarms(resources)
    assert len(alarms) == 2, f"expected exactly two alarms, got {sorted(alarms)}"
    watched = set()
    for lid, alarm in alarms.items():
        groups = set()
        for stat in _metric_stats(alarm):
            metric = stat["Metric"]
            group = _target_group_of(stat)
            assert group is not None, (
                f"{lid} has a metric with no TargetGroup dimension "
                f"({metric.get('MetricName')}): it would count the whole load "
                "balancer, the dashboard included"
            )
            assert group != dashboard, f"{lid} watches the dashboard's target group"
            assert metric["Namespace"] == "AWS/ApplicationELB", metric
            assert metric["MetricName"] in (
                "HTTPCode_Target_5XX_Count",
                "RequestCount",
            ), metric
            groups.add(group)
        assert len(groups) == 1, f"{lid} mixes target groups: {groups}"
        watched |= groups
    assert watched == {blue, green}, (
        f"the alarms watch {sorted(watched)}; expected exactly blue ({blue}) and "
        f"green ({green})"
    )


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_group_stops_and_rolls_back_on_an_alarm(stacks, case):
    """P2 and P3, by equality."""
    group = _group(stacks[case].template["Resources"])
    config = dict(group["AlarmConfiguration"])
    assert config.pop("IgnorePollAlarmFailure", False) is False, (
        "a failed alarm read must stop the deployment, not be ignored"
    )
    assert config.pop("Enabled") is True
    assert set(config) == {"Alarms"}, config
    rollback = group["AutoRollbackConfiguration"]
    assert rollback["Enabled"] is True
    assert sorted(rollback["Events"]) == EVENTS, (
        f"rollback events {sorted(rollback['Events'])}; expected {EVENTS}. "
        "Without DEPLOYMENT_STOP_ON_ALARM an alarm stops the deployment and "
        "leaves the group half-shifted."
    )
    assert set(rollback) == {"Enabled", "Events"}, rollback


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_each_alarm_is_exactly_the_specified_one(stacks, case):
    """P4 and P5: a pin, not a behaviour test -- CloudWatch is not simulated."""
    environment = case[0]
    resources = stacks[case].template["Resources"]
    alarms = _group_alarms(resources)
    by_name = {a["AlarmName"]: a for a in alarms.values()}
    assert sorted(by_name) == sorted(
        f"experimentation-api-5xx-{colour}-{environment}" for colour in LIVE
    ), sorted(by_name)
    for alarm in alarms.values():
        expressions = [m for m in alarm["Metrics"] if "Expression" in m]
        assert len(expressions) == 1, alarm["Metrics"]
        (expression,) = expressions
        assert expression["Expression"] == EXPRESSION
        assert expression["ReturnData"] is True
        stats = {m["Id"]: m for m in alarm["Metrics"] if "MetricStat" in m}
        assert set(stats) == {"e", "r"}, sorted(stats)
        assert stats["e"]["MetricStat"]["Metric"]["MetricName"] == (
            "HTTPCode_Target_5XX_Count"
        )
        assert stats["r"]["MetricStat"]["Metric"]["MetricName"] == "RequestCount"
        for metric in stats.values():
            assert metric["ReturnData"] is False
            assert metric["MetricStat"]["Stat"] == "Sum"
            assert metric["MetricStat"]["Period"] == 60
        assert alarm["Threshold"] == 0.05
        assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
        assert alarm["EvaluationPeriods"] == 3
        assert alarm["DatapointsToAlarm"] == 2
        assert alarm["TreatMissingData"] == "notBreaching", (
            "anything but notBreaching puts an idle target group -- the one not "
            "live -- in ALARM, and every deployment is stopped"
        )
        window = 60 * alarm["EvaluationPeriods"]
        canary = _canary_seconds(resources)
        assert window <= canary, (
            f"evaluation window {window}s exceeds the {canary}s canary"
        )


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_api_canary_is_fifteen_minutes(stacks, case):
    """#212 (D47): every API deployment group uses the 15-minute canary."""
    group = _group(stacks[case].template["Resources"])
    assert group["DeploymentConfigName"] == CANARY_CONFIG


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_deploy_expects_the_synthesised_deployment_config(stacks, case):
    """#795 (PE C7 on #212): deploy.yml's pre-flight warns when the group's
    deploymentConfigName differs from CODEDEPLOY_DEPLOYMENT_CONFIG in the
    deploy job's env, so that value must be the one the stack synthesises, as
    the alarm names passed as --expect are (test_workflow_names_exist)."""
    deploy = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    (job,) = [j for j in deploy["jobs"].values() if "environment" in j]
    expected = job["env"]["CODEDEPLOY_DEPLOYMENT_CONFIG"]
    group = _group(stacks[case].template["Resources"])
    assert expected == group["DeploymentConfigName"], (
        f"deploy.yml expects {expected!r}; the {_ids(case)} synth gives the group "
        f"{group['DeploymentConfigName']!r}"
    )


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_shift_deadline_is_the_canary_plus_the_allowance(stacks, case):
    """deploy.yml's shift deadline runs from create-deployment and covers the
    canary in full (scripts/shift_traffic.py). A longer canary with the old
    deadline leaves too little for the tasks to start and the shift to finish:
    a false red after an approved shift."""
    deploy = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    deadline = int(deploy["env"]["CODEDEPLOY_DEADLINE_SECONDS"])
    canary = _canary_seconds(stacks[case].template["Resources"])
    assert deadline - canary == SHIFT_ALLOWANCE_SECONDS, (
        f"CODEDEPLOY_DEADLINE_SECONDS {deadline} - the {canary}s canary == "
        f"{deadline - canary}, expected {SHIFT_ALLOWANCE_SECONDS}"
    )


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_synth_has_no_warnings(stacks, case):
    """PE 9b: CDK only warns about a math identifier it does not know.

    Rename `r` in the expression and this is the message that appears:
    "Math expression ... references unknown identifiers: q". The stack is
    otherwise warning-free, so any message fails; the first assertion names the
    one this exists for.
    """
    messages = [
        f"{m.level}: {m.id}: {m.entry.data}" for m in stacks[case].messages
    ]
    assert not [m for m in messages if "unknown identifiers" in m], messages
    assert messages == [], messages


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_codedeploy_role_reads_the_alarms(stacks, case):
    """P8: the managed policy exactly, and the explicit alarm read."""
    resources = stacks[case].template["Resources"]
    role_id = _one(resources, "CodeDeployRole", "AWS::IAM::Role")
    role = resources[role_id]["Properties"]
    assert role["ManagedPolicyArns"] == [
        {
            "Fn::Join": [
                "",
                [
                    "arn:",
                    {"Ref": "AWS::Partition"},
                    ":iam::aws:policy/AWSCodeDeployRoleForECS",
                ],
            ]
        }
    ]
    assert _group(resources)["ServiceRoleArn"] == {"Fn::GetAtt": [role_id, "Arn"]}
    statements = [
        statement
        for policy in _of_type(resources, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in policy["Properties"]["Roles"]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    assert statements == [
        {"Action": "cloudwatch:DescribeAlarms", "Effect": "Allow", "Resource": "*"}
    ], statements


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_dashboard_has_no_alarm(stacks, case):
    """P13: the dashboard's rollback is its circuit breaker, and only that."""
    resources = stacks[case].template["Resources"]
    service = _one(resources, "DashboardService", "AWS::ECS::Service")
    assert resources[service]["Properties"]["DeploymentConfiguration"] == {
        "Alarms": {"AlarmNames": [], "Enable": False, "Rollback": False},
        "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
        "MaximumPercent": 200,
        "MinimumHealthyPercent": 100,
    }


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_deploy_summarys_bake_is_the_groups_termination_wait(stacks, case):
    """deploy.yml's summary says "alarms watch the API until about <shift +
    BAKE_MINUTES>"; that is the group's TerminationWaitTimeInMinutes."""
    deploy = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    group = _group(stacks[case].template["Resources"])
    wait = group["BlueGreenDeploymentConfiguration"][
        "TerminateBlueInstancesOnDeploymentSuccess"
    ]["TerminationWaitTimeInMinutes"]
    assert int(deploy["env"]["BAKE_MINUTES"]) == wait


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_flipping_the_live_group_changes_no_logical_id(stacks, environment):
    """A cdk deploy after an odd number of deployments must not replace anything
    this change added; the alarms are colour-agnostic by construction.

    The templates as a whole do differ between the two: the two ``ApiPaths``
    listener rules forward to the live group, and ``BackendService``'s
    DependsOn follows them. Nothing else may (test_alb_egress_to_tasks.py pins
    that exact difference). The load balancer's egress rule to the tasks used
    to follow the live group too, and a green-pinned deploy would have
    deleted it (#801), so the comparison includes the ingress and egress
    rules alongside the resource types #148 added."""
    kinds = (
        "AWS::CloudWatch::Alarm",
        "AWS::CodeDeploy::DeploymentGroup",
        "AWS::EC2::SecurityGroupEgress",
        "AWS::EC2::SecurityGroupIngress",
        "AWS::IAM::Policy",
        "AWS::IAM::Role",
    )

    def touched(live: str) -> dict:
        resources = stacks[(environment, "full", live)].template["Resources"]
        return {
            lid: json.dumps(r, sort_keys=True)
            for lid, r in resources.items()
            if r["Type"] in kinds
        }

    blue, green = touched("blue"), touched("green")
    assert sorted(blue) == sorted(green)
    assert len([lid for lid in blue if lid.startswith("Api5xx")]) == 2, sorted(blue)
    for lid in blue:
        assert blue[lid] == green[lid], lid


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_an_unapproved_deployment_is_stopped_after_30_minutes(stacks, case):
    """The rollback runbook: the group waits 30 minutes for the traffic shift's
    approval, then stops the deployment, and a stopped deployment rolls back."""
    group = _group(stacks[case].template["Resources"])
    ready = group["BlueGreenDeploymentConfiguration"]["DeploymentReadyOption"]
    assert ready == {"ActionOnTimeout": "STOP_DEPLOYMENT", "WaitTimeInMinutes": 30}, (
        ready
    )
    assert "DEPLOYMENT_STOP_ON_REQUEST" in group["AutoRollbackConfiguration"]["Events"]
