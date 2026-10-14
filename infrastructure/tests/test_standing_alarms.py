"""Every standing alarm watches something a deployed resource publishes (#390, #205).

The monitoring stack's alarms sat in INSUFFICIENT_DATA for ever: an API
Gateway 5xx alarm with no API Gateway, Lambda alarms on a function no stack
deploys, a throttling alarm on a literal table name, three alarms on a
Prometheus namespace nothing publishes to, an ERROR filter on a log group
nothing writes to, and CPU alarms naming ``AuroraCluster`` and ``Redis`` --
identifiers no deployed resource has. And nothing alarmed when no API task was
healthy: the 5xx alarms count the tasks' own 5xx, and with no task the load
balancer answers 503 itself.

Everything here runs on a real ``app.synth()`` of every environment, both
profiles (``EXPERIMENTLY_PROFILE``, parametrized, never skipped) and both
values of ``api_live_target_group``, and asserts exact values:

* **No healthy API task.** A HealthyHostCount alarm per target group --
  Maximum, 60 s, 3 of 3, below 1, missing data breaching, NO action -- and one
  composite, exactly ``ALARM(blue) AND ALARM(green)``, that emails the topic.
  The idle colour's alarm is in ALARM by design between deployments, so
  neither per-colour alarm may join the deployment group (whose every alarm
  ``scripts/refuse_alarm_active.py`` requires OK before a deploy) and neither
  may email. The composite and both per-colour alarms are byte-identical
  whichever colour is live, so ``-c api_live_target_group`` replaces nothing.
* **Every other standing alarm emails the topic.** The exemption list is
  exactly the two per-colour alarms.
* **Aurora.** ``DBClusterIdentifier`` resolves through an SSM parameter to the
  database stack's own ``AWS::RDS::DBCluster``, with ``Role=WRITER``; no
  export, no import; the monitoring stack depends on the database stack; the
  cluster is not renamed.
* **Redis.** One alarm per node, and the node ids are the replication group's
  id with ``-001`` .. ``-00N``, N the group's own node count.
* **Namespaces.** No alarm or metric filter uses a namespace or log group
  nothing publishes to, and the error-log filter is on the log group the
  API's task definition writes to.
* **The error-log filter** counts the lines whose JSON ``level`` is
  ``error`` or ``critical`` (#811): the API's lines are JSON with a lower-case
  level, and CloudWatch matches case-sensitively, so the ``"ERROR"`` term it
  used to match counted none of them. ``backend/tests/unit/core/test_logger.py``
  pins the logger's half: the level a rendered line carries.
"""

from __future__ import annotations

import json
import re
import runpy

import aws_cdk as cdk
import pytest

from .test_app_profiles import CDK_DIR, _app_environment

pytestmark = pytest.mark.regression

ENVIRONMENTS = ("dev", "staging", "prod", "demo")
PROFILES = ("full", "core")
LIVE = ("blue", "green")
COLOURS = ("blue", "green")

#: Namespaces and log groups nothing in a deployment publishes to (#205).
#: Formatted with the environment.
DEAD_TARGETS = (
    "AWS/ApiGateway",
    "AWS/Lambda",
    "ExperimentationPlatform/Prometheus",
    "/experimentation/{env}/application",
)
ALARM_TYPES = ("AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm")


def _synth(environment: str, profile: str, live: str) -> dict[str, dict]:
    """``{stack name: template}`` for ``app.py``, as ``cdk synth -c api_live_target_group`` builds it."""
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
    return {
        "templates": {s.stack_name: s.template for s in assembly.stacks},
        "dependencies": {
            s.stack_name: {d.stack_name for d in s.dependencies if hasattr(d, "stack_name")}
            for s in assembly.stacks
        },
    }


CASES = [(e, p, v) for e in ENVIRONMENTS for p in PROFILES for v in LIVE]


def _ids(case) -> str:
    return "-".join(case)


@pytest.fixture(scope="module")
def synths():
    return {case: _synth(*case) for case in CASES}


def _stack(synth: dict, short: str, env: str) -> dict:
    return synth["templates"][f"experimentation-{short}-{env}"]


def _of_type(template: dict, rtype: str) -> dict:
    return {
        lid: r for lid, r in template.get("Resources", {}).items() if r["Type"] == rtype
    }


def _one(template: dict, prefix: str, rtype: str) -> str:
    hits = [
        lid
        for lid, r in template["Resources"].items()
        if r["Type"] == rtype and re.fullmatch(rf"{prefix}[0-9A-F]{{8}}", lid)
    ]
    assert len(hits) == 1, f"expected one {rtype} {prefix}<hash>, found {hits}"
    return hits[0]


def _alarms_by_name(template: dict) -> dict[str, tuple[str, dict]]:
    return {
        r["Properties"]["AlarmName"]: (lid, r["Properties"])
        for rtype in ALARM_TYPES
        for lid, r in _of_type(template, rtype).items()
    }


def _topic_action(synth: dict, env: str, stack_name: str):
    """The alerts topic as ``stack_name`` names it: a Ref at home, an import elsewhere."""
    monitoring = _stack(synth, "monitoring", env)
    (topic,) = _of_type(monitoring, "AWS::SNS::Topic")
    if stack_name == f"experimentation-monitoring-{env}":
        return {"Ref": topic}
    (export,) = [
        o["Export"]["Name"]
        for o in monitoring.get("Outputs", {}).values()
        if o.get("Value") == {"Ref": topic} and "Export" in o
    ]
    return {"Fn::ImportValue": export}


# --- no healthy API task (#390) -----------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_each_colour_has_a_healthy_task_alarm_with_no_action(synths, case):
    env, _, _ = case
    fargate = _stack(synths[case], "fargate", env)
    alb = _one(fargate, "BackendALB", "AWS::ElasticLoadBalancingV2::LoadBalancer")
    alarms = _alarms_by_name(fargate)
    for colour in COLOURS:
        name = f"experimentation-api-healthy-{colour}-{env}"
        assert name in alarms, sorted(alarms)
        _, props = alarms[name]
        target_group = _one(
            fargate,
            f"{colour.capitalize()}TargetGroup",
            "AWS::ElasticLoadBalancingV2::TargetGroup",
        )
        assert {k: v for k, v in props.items() if k != "AlarmDescription"} == {
            "AlarmName": name,
            "Namespace": "AWS/ApplicationELB",
            "MetricName": "HealthyHostCount",
            "Dimensions": [
                {"Name": "LoadBalancer", "Value": {"Fn::GetAtt": [alb, "LoadBalancerFullName"]}},
                {
                    "Name": "TargetGroup",
                    "Value": {"Fn::GetAtt": [target_group, "TargetGroupFullName"]},
                },
            ],
            "Statistic": "Maximum",
            "Period": 60,
            "EvaluationPeriods": 3,
            "DatapointsToAlarm": 3,
            "ComparisonOperator": "LessThanThreshold",
            "Threshold": 1,
            # The idle target group publishes nothing: that must count as "no
            # healthy target", or the composite can never fire.
            "TreatMissingData": "breaching",
        }, name


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_composite_is_exactly_blue_and_green_and_emails_the_topic(synths, case):
    env, _, _ = case
    fargate = _stack(synths[case], "fargate", env)
    alarms = _alarms_by_name(fargate)
    blue, _ = alarms[f"experimentation-api-healthy-blue-{env}"]
    green, _ = alarms[f"experimentation-api-healthy-green-{env}"]
    name = f"experimentation-api-no-healthy-task-{env}"
    composites = {
        r["Properties"]["AlarmName"]: r["Properties"]
        for r in _of_type(fargate, "AWS::CloudWatch::CompositeAlarm").values()
    }
    assert list(composites) == [name], sorted(composites)
    props = composites[name]
    separator, parts = props["AlarmRule"]["Fn::Join"]
    assert separator == ""
    rule = "".join(
        part if isinstance(part, str) else "{%s}" % part["Fn::GetAtt"][0]
        for part in parts
    )
    # AND, not OR: the idle colour is in ALARM all the time, so OR would email
    # permanently; a single colour would miss every deployment that swaps.
    assert rule == '(ALARM("{%s}") AND ALARM("{%s}"))' % (blue, green), rule
    assert [p["Fn::GetAtt"] for p in parts if not isinstance(p, str)] == [
        [blue, "Arn"],
        [green, "Arn"],
    ]
    assert props["AlarmActions"] == [
        _topic_action(synths[case], env, f"experimentation-fargate-{env}")
    ]
    assert "OKActions" not in props and "InsufficientDataActions" not in props


@pytest.mark.parametrize(
    "env, profile", [(e, p) for e in ENVIRONMENTS for p in PROFILES]
)
def test_the_new_alarms_do_not_move_with_the_live_colour(synths, env, profile):
    """``-c api_live_target_group=green`` changes none of them -- no replacement."""
    new = {}
    for live in LIVE:
        fargate = _stack(synths[(env, profile, live)], "fargate", env)
        new[live] = {
            lid: r
            for lid, r in fargate["Resources"].items()
            if r["Type"] in (*ALARM_TYPES, "AWS::Logs::MetricFilter")
            and not r["Properties"].get("AlarmName", "").startswith("experimentation-api-5xx-")
        }
    assert len(new["blue"]) == 5, sorted(new["blue"])
    assert json.dumps(new["blue"], sort_keys=True) == json.dumps(
        new["green"], sort_keys=True
    )


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_deployment_group_rolls_back_on_the_5xx_alarms_only(synths, case):
    """A healthy-task alarm in the group would stop every deploy: the idle colour's is always ALARM."""
    env, _, _ = case
    fargate = _stack(synths[case], "fargate", env)
    (group,) = _of_type(fargate, "AWS::CodeDeploy::DeploymentGroup").values()
    names = {a["Name"]["Ref"] for a in group["Properties"]["AlarmConfiguration"]["Alarms"]}
    alarms = _alarms_by_name(fargate)
    assert names == {
        alarms[f"experimentation-api-5xx-{colour}-{env}"][0] for colour in COLOURS
    }, names


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_every_standing_alarm_emails_the_topic_but_the_per_colour_two(synths, case):
    env, _, _ = case
    synth = synths[case]
    exempt = {f"experimentation-api-healthy-{colour}-{env}" for colour in COLOURS}
    seen_exempt = set()
    count = 0
    for stack_name, template in synth["templates"].items():
        for name, (_, props) in _alarms_by_name(template).items():
            count += 1
            if name in exempt:
                seen_exempt.add(name)
                for key in ("AlarmActions", "OKActions", "InsufficientDataActions"):
                    assert key not in props, (name, key)
                continue
            assert props.get("AlarmActions") == [
                _topic_action(synth, env, stack_name)
            ], (stack_name, name, props.get("AlarmActions"))
    assert seen_exempt == exempt, seen_exempt
    assert count >= 8, count


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_standing_alarms_are_exactly_these(synths, case):
    """The whole app's alarm set, so a removed alarm cannot come back unnoticed."""
    env, profile, _ = case
    expected = {
        f"experimentation-api-5xx-blue-{env}",
        f"experimentation-api-5xx-green-{env}",
        f"experimentation-api-healthy-blue-{env}",
        f"experimentation-api-healthy-green-{env}",
        f"experimentation-api-no-healthy-task-{env}",
        f"experimentation-api-error-logs-{env}",
        f"AuroraHighCPU-{env}",
    } | {f"RedisHighCPU-{i:03d}-{env}" for i in range(1, (3 if env == "prod" else 1) + 1)}
    if profile == "full":
        expected.add(f"KinesisProcessingDelay-{env}")
    found = {
        name
        for template in synths[case]["templates"].values()
        for name in _alarms_by_name(template)
    }
    assert found == expected, (sorted(found - expected), sorted(expected - found))


# --- Aurora -------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_aurora_cpu_names_the_real_cluster_through_the_ssm_parameter(synths, case):
    env, _, _ = case
    synth = synths[case]
    monitoring = _stack(synth, "monitoring", env)
    database = _stack(synth, "database", env)
    (_, props) = _alarms_by_name(monitoring)[f"AuroraHighCPU-{env}"]
    assert props["Namespace"] == "AWS/RDS" and props["MetricName"] == "CPUUtilization"
    assert props["TreatMissingData"] == "breaching"
    dimensions = props["Dimensions"]
    assert [d["Name"] for d in dimensions] == ["DBClusterIdentifier", "Role"]
    assert dimensions[1]["Value"] == "WRITER"
    reference = dimensions[0]["Value"]
    assert set(reference) == {"Ref"}, reference
    parameter = monitoring["Parameters"][reference["Ref"]]
    assert parameter["Type"] == "AWS::SSM::Parameter::Value<String>", parameter
    name = parameter["Default"]
    assert name == f"/experimentation/{env}/database/aurora-cluster-identifier"

    (cluster,) = _of_type(database, "AWS::RDS::DBCluster")
    written = [
        r["Properties"]["Value"]
        for r in _of_type(database, "AWS::SSM::Parameter").values()
        if r["Properties"]["Name"] == name
    ]
    assert written == [{"Ref": cluster}], written
    # Never renamed: a DBClusterIdentifier change replaces the cluster.
    assert "DBClusterIdentifier" not in database["Resources"][cluster]["Properties"]


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_no_export_ties_monitoring_to_the_database(synths, case):
    env, _, _ = case
    synth = synths[case]
    database = _stack(synth, "database", env)
    (cluster,) = _of_type(database, "AWS::RDS::DBCluster")
    # The cluster's identifier is an output (the deploy workflows read it with
    # describe-stacks) but never an export: CloudFormation refuses to change
    # an export another stack imports, which would pin the cluster.
    exported = [
        name
        for name, o in database.get("Outputs", {}).items()
        if "Export" in o and o["Value"] == {"Ref": cluster}
    ]
    assert not exported, exported
    # And the monitoring stack imports nothing from the database stack.
    monitoring = json.dumps(_stack(synth, "monitoring", env))
    assert f"experimentation-database-{env}" not in monitoring
    # What orders the parameter before the alarm is the stack dependency.
    assert f"experimentation-database-{env}" in synth["dependencies"][
        f"experimentation-monitoring-{env}"
    ], synth["dependencies"][f"experimentation-monitoring-{env}"]


# --- Redis --------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_redis_cpu_alarms_are_the_replication_groups_nodes(synths, case):
    env, _, _ = case
    synth = synths[case]
    (group,) = _of_type(_stack(synth, "redis", env), "AWS::ElastiCache::ReplicationGroup").values()
    group_id = group["Properties"]["ReplicationGroupId"]
    nodes = group["Properties"]["NumCacheClusters"]
    assert isinstance(group_id, str) and nodes == (3 if env == "prod" else 1)
    members = {f"{group_id}-{i:03d}" for i in range(1, nodes + 1)}

    alarms = {
        name: props
        for name, (_, props) in _alarms_by_name(_stack(synth, "monitoring", env)).items()
        if props.get("Namespace") == "AWS/ElastiCache"
    }
    watched = set()
    for name, props in alarms.items():
        assert props["MetricName"] == "EngineCPUUtilization", name
        assert props["TreatMissingData"] == "breaching", name
        (dimension,) = props["Dimensions"]
        assert dimension["Name"] == "CacheClusterId", name
        watched.add(dimension["Value"])
    assert watched == members, (watched, members)
    assert len(alarms) == nodes


# --- namespaces and log groups (#205) -----------------------------------------


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_no_alarm_or_filter_watches_what_nothing_publishes(synths, case):
    env, _, _ = case
    dead = [target.format(env=env) for target in DEAD_TARGETS]
    for stack_name, template in synths[case]["templates"].items():
        for lid, r in template.get("Resources", {}).items():
            if r["Type"] in (*ALARM_TYPES, "AWS::Logs::MetricFilter"):
                text = json.dumps(r)
                for target in dead:
                    assert target not in text, (stack_name, lid, target)


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_error_filter_is_on_the_api_tasks_log_group(synths, case):
    env, _, _ = case
    synth = synths[case]
    filters = {
        (stack_name, lid): r["Properties"]
        for stack_name, template in synth["templates"].items()
        for lid, r in _of_type(template, "AWS::Logs::MetricFilter").items()
    }
    assert list(filters) == [
        (f"experimentation-fargate-{env}", "BackendLogGroupApiErrorLogsB9EE50D9")
    ], sorted(filters)
    (props,) = filters.values()

    fargate = _stack(synth, "fargate", env)
    (backend,) = [
        c
        for r in _of_type(fargate, "AWS::ECS::TaskDefinition").values()
        if r["Properties"]["Family"] == f"experimentation-backend-{env}"
        for c in r["Properties"]["ContainerDefinitions"]
    ]
    assert backend["LogConfiguration"]["LogDriver"] == "awslogs"
    task_group = backend["LogConfiguration"]["Options"]["awslogs-group"]
    assert props["LogGroupName"] == task_group == {"Ref": "BackendLogGroupDA10F1B2"}
    assert (
        fargate["Resources"]["BackendLogGroupDA10F1B2"]["Properties"]["LogGroupName"]
        == f"/ecs/experimentation-backend-{env}"
    )
    # The API's JSON lines carry a lower-case level, and CloudWatch matches
    # case-sensitively (#811). test_logger.py renders the logger's half.
    assert (
        props["FilterPattern"]
        == '{ ($.level = "error") || ($.level = "critical") }'
    )
    (transformation,) = props["MetricTransformations"]
    assert transformation["MetricNamespace"] == f"Experimently/{env}"

    alarms = _alarms_by_name(fargate)
    _, alarm = alarms[f"experimentation-api-error-logs-{env}"]
    assert (alarm["Namespace"], alarm["MetricName"]) == (
        transformation["MetricNamespace"],
        transformation["MetricName"],
    )
    # The description is what the alarm email says it counts.
    assert alarm["AlarmDescription"] == (
        "At least 10 log lines from the API's tasks in 5 minutes whose "
        'JSON "level" is "error" or "critical"'
    )


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_error_log_alarm_fires_on_10_lines_in_5_minutes(synths, case):
    """The AWS CDK Deployment page: experimentation-api-error-logs-<env> fires on
    10 or more error or critical lines in 5 minutes, and emails the topic."""
    env, _, _ = case
    synth = synths[case]
    fargate = _stack(synth, "fargate", env)
    _, alarm = _alarms_by_name(fargate)[f"experimentation-api-error-logs-{env}"]
    assert alarm["Threshold"] == 10
    assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert alarm["EvaluationPeriods"] == 1
    assert alarm["Period"] == 300
    assert alarm["Statistic"] == "Sum"
    assert alarm["MetricName"] == "ApiErrorLogLines"
    assert alarm["AlarmActions"] == [
        _topic_action(synth, env, f"experimentation-fargate-{env}")
    ], alarm["AlarmActions"]
