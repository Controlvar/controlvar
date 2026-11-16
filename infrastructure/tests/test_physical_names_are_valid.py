"""Every literal physical name the CDK app synthesises meets its AWS naming rule.

``cdk deploy`` for prod and demo failed at ``experimentation-redis-<env>``. The
Redis subnet group name is cut to 40 characters, and for those two environments
the cut ended in a hyphen (``experimentation-redis-prod-redis-subnet-``), which
ElastiCache refuses. The cut dates from the first Redis stack. ``cdk synth``
and this suite both accepted the name, because nothing compared a name with
the rule AWS applies to it.

So, on a real ``app.synth()`` of all four environments:

* Every literal name is checked against its resource type's rule in ``RULES``,
  or declared not to be a physical name in ``NOT_A_NAME``, with the reason. A
  literal name is a string property whose key ends in ``Name``, ``Id``,
  ``Identifier`` or ``Family``, at any depth, plus every path in
  ``NAME_PROPERTIES``. Anything in neither table fails: a name nobody wrote a
  rule for is a name nobody checked (fail closed).
* Each rule refuses a name AWS refuses and accepts one AWS accepts
  (``EXAMPLES``), so a rule that accepts everything fails here instead of
  passing in silence.
* The four Redis subnet group names are pinned. Staging's and dev's must not
  change: a new ``CacheSubnetGroupName`` replaces the subnet group and the
  replication group that names it, i.e. that environment's Redis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from .test_environments_do_not_collide import (
    ENVIRONMENTS,
    MODULES_PRESENT,
    NAME_PROPERTIES,
    Synth,
    _get,
    _literal,
)


@dataclass(frozen=True)
class Rule:
    """A length range, the characters allowed, and what else AWS refuses."""

    max_length: int
    pattern: str
    refused: tuple[tuple[str, str], ...] = ()
    min_length: int = 1

    def violations(self, name: str) -> list[str]:
        found = []
        if not self.min_length <= len(name) <= self.max_length:
            found.append(
                f"is {len(name)} characters; the limit is "
                f"{self.min_length}-{self.max_length}"
            )
        if not re.fullmatch(self.pattern, name):
            found.append(f"does not match {self.pattern}")
        found += [why for regex, why in self.refused if re.search(regex, name)]
        return found


# Where botocore's service model gives a constraint (min, max, pattern), the
# rule is that constraint. Where it gives none, the rule is the API reference's
# wording, named beside it.

#: ElastiCache's own words, from the refused production deploy: "Identifiers
#: must begin with a letter; must contain only ASCII letters, digits, and
#: hyphens; and must not end with a hyphen or contain two consecutive hyphens."
#: A replication group id is at most 40 characters, a subnet group name 255.
_ELASTICACHE_REFUSED = (
    (r"-$", "ends with a hyphen"),
    (r"--", "contains two consecutive hyphens"),
)
ELASTICACHE_REPLICATION_GROUP = Rule(40, r"[A-Za-z][A-Za-z0-9-]*", _ELASTICACHE_REFUSED)
ELASTICACHE_SUBNET_GROUP = Rule(255, r"[A-Za-z][A-Za-z0-9-]*", _ELASTICACHE_REFUSED)
#: The API reference: at most 32 alphanumerics or hyphens, not beginning or
#: ending with a hyphen, not beginning with "internal-".
ALB = Rule(
    32,
    r"[A-Za-z0-9-]+",
    (
        (r"^-|-$", "begins or ends with a hyphen"),
        (r"^internal-", "begins with internal-"),
    ),
)
#: The API reference: up to 255 letters, numbers, underscores and hyphens
#: (cluster, service, task definition family and container alike).
ECS = Rule(255, r"[A-Za-z0-9_-]+")
#: The API reference: alphanumerics, "-" and "_", up to 255.
DASHBOARD = Rule(255, r"[A-Za-z0-9_-]+")
#: botocore: 1-255; the API reference: no ASCII control characters.
ALARM = Rule(255, r"[^\x00-\x1f\x7f]+")
CODEDEPLOY = Rule(100, r"[A-Za-z0-9+=,.@_-]+")
COGNITO = Rule(128, r"[\w\s+=,.@-]+")
DYNAMODB = Rule(255, r"[A-Za-z0-9_.-]+", min_length=3)
EVENTS_RULE = Rule(64, r"[.\-_A-Za-z0-9]+")
#: botocore's single-line string: no control character but tab.
GLUE_NAME = Rule(255, r"[^\x00-\x08\x0a-\x1f]+")
#: Glue lowercases a database name, and Athena reads only lowercase letters,
#: digits and underscores.
GLUE_DATABASE = Rule(255, r"[a-z0-9_]+")
IAM_ROLE = Rule(64, r"[\w+=,.@-]+")
IAM_POLICY = Rule(128, r"[\w+=,.@-]+")
KMS_ALIAS = Rule(
    256,
    r"[A-Za-z0-9:/_-]+",
    (
        (r"^(?!alias/)", "does not begin with alias/"),
        (r"^alias/aws/", "begins with alias/aws/, which AWS keeps for its own keys"),
    ),
)
#: botocore allows 140 for an ARN; a bare function name is at most 64.
LAMBDA = Rule(64, r"[A-Za-z0-9_-]+")
LOG_GROUP = Rule(
    512, r"[.\-_/#A-Za-z0-9]+", ((r"^aws/", "begins with aws/, which is reserved"),)
)
#: botocore: 1-256, ``\p{Print}``; narrowed here to printable ASCII.
SCALING_POLICY = Rule(256, r"[\x20-\x7e]+")
#: The API reference: ASCII letters, numbers and /_+=.@-, up to 512.
SECRET = Rule(512, r"[A-Za-z0-9/_+=.@-]+")
#: The API reference: letters, numbers, "_" and "-", up to 256, ".fifo" last
#: for a FIFO topic; a display name is at most 100 characters.
SNS_TOPIC = Rule(256, r"[A-Za-z0-9_-]+(\.fifo)?")
SNS_DISPLAY_NAME = Rule(100, r"[^\x00-\x08\x0a-\x1f\x7f]+")
#: The API reference: letters, numbers and _.-/, a hierarchy begins with "/"
#: and has at most 15 levels, and no name begins with "aws" or "ssm". 1011 is
#: the limit with the parameter's ARN included, so this bound is loose.
SSM_PARAMETER = Rule(
    1011,
    r"[A-Za-z0-9_.\-/]+",
    (
        (r"^[^/].*/", "has a / but does not begin with one"),
        (r"(?i)^/?(aws|ssm)", "begins with aws or ssm, which are reserved"),
        (r"^(/[^/]*){16}", "has more than 15 levels"),
    ),
)
#: Aurora PostgreSQL's database name: 1-63 letters, digits or underscores,
#: beginning with a letter. (Words the engine reserves are not checked.)
AURORA_POSTGRES_DATABASE = Rule(63, r"[A-Za-z][A-Za-z0-9_]*")

#: ``type -> {property path -> rule}``. A list in the path is written ``[]``.
RULES: dict[str, dict[str, Rule]] = {
    "AWS::ApplicationAutoScaling::ScalingPolicy": {"PolicyName": SCALING_POLICY},
    "AWS::CloudWatch::Alarm": {"AlarmName": ALARM},
    "AWS::CloudWatch::CompositeAlarm": {"AlarmName": ALARM},
    "AWS::CloudWatch::Dashboard": {"DashboardName": DASHBOARD},
    "AWS::CodeDeploy::Application": {"ApplicationName": CODEDEPLOY},
    "AWS::CodeDeploy::DeploymentGroup": {"DeploymentGroupName": CODEDEPLOY},
    "AWS::Cognito::UserPool": {"UserPoolName": COGNITO},
    "AWS::Cognito::UserPoolClient": {"ClientName": COGNITO},
    "AWS::DynamoDB::Table": {
        "TableName": DYNAMODB,
        "GlobalSecondaryIndexes[].IndexName": DYNAMODB,
    },
    "AWS::ECS::Cluster": {"ClusterName": ECS},
    "AWS::ECS::Service": {"ServiceName": ECS, "LoadBalancers[].ContainerName": ECS},
    "AWS::ECS::TaskDefinition": {"Family": ECS, "ContainerDefinitions[].Name": ECS},
    "AWS::ElastiCache::ReplicationGroup": {
        "ReplicationGroupId": ELASTICACHE_REPLICATION_GROUP
    },
    "AWS::ElastiCache::SubnetGroup": {"CacheSubnetGroupName": ELASTICACHE_SUBNET_GROUP},
    "AWS::ElasticLoadBalancingV2::LoadBalancer": {"Name": ALB},
    "AWS::Events::Rule": {"Name": EVENTS_RULE},
    "AWS::Glue::Crawler": {"Name": GLUE_NAME, "DatabaseName": GLUE_DATABASE},
    "AWS::Glue::Database": {"DatabaseInput.Name": GLUE_DATABASE},
    "AWS::Glue::Job": {"Name": GLUE_NAME},
    "AWS::IAM::Policy": {"PolicyName": IAM_POLICY},
    "AWS::IAM::Role": {"RoleName": IAM_ROLE, "Policies[].PolicyName": IAM_POLICY},
    "AWS::KMS::Alias": {"AliasName": KMS_ALIAS},
    "AWS::Lambda::Function": {"FunctionName": LAMBDA},
    "AWS::Logs::LogGroup": {"LogGroupName": LOG_GROUP},
    "AWS::RDS::DBCluster": {"DatabaseName": AURORA_POSTGRES_DATABASE},
    "AWS::SNS::Topic": {"TopicName": SNS_TOPIC, "DisplayName": SNS_DISPLAY_NAME},
    "AWS::SSM::Parameter": {"Name": SSM_PARAMETER},
    "AWS::SecretsManager::Secret": {"Name": SECRET},
}

#: ``type -> {property path -> why it is not a physical name}``.
NOT_A_NAME: dict[str, dict[str, str]] = {
    "AWS::CloudWatch::Alarm": {
        "MetricName": "the metric the alarm watches",
        "Dimensions[].Name": "a metric dimension",
        "Metrics[].Id": "a metric math id inside the alarm",
        "Metrics[].MetricStat.Metric.MetricName": "the metric the alarm watches",
        "Metrics[].MetricStat.Metric.Dimensions[].Name": "a metric dimension",
    },
    "AWS::CodeDeploy::DeploymentGroup": {
        "DeploymentConfigName": "selects one of CodeDeploy's own configurations",
    },
    "AWS::Cognito::UserPool": {
        "AccountRecoverySetting.RecoveryMechanisms[].Name": "an enumerated value",
        "Schema[].Name": "a user attribute",
        "SmsConfiguration.ExternalId": "a value the SMS role's trust policy checks",
    },
    "AWS::DynamoDB::Table": {
        "AttributeDefinitions[].AttributeName": "an item attribute",
        "KeySchema[].AttributeName": "an item attribute",
        "GlobalSecondaryIndexes[].KeySchema[].AttributeName": "an item attribute",
        "TimeToLiveSpecification.AttributeName": "an item attribute",
    },
    "AWS::EC2::VPCEndpoint": {"ServiceName": "the AWS service the endpoint reaches"},
    "AWS::ECS::Cluster": {"ClusterSettings[].Name": "an enumerated setting"},
    "AWS::ECS::TaskDefinition": {
        "ContainerDefinitions[].Environment[].Name": "an environment variable",
        "ContainerDefinitions[].Secrets[].Name": "an environment variable",
    },
    "AWS::Events::Rule": {"Targets[].Id": "a target id inside the rule"},
    "AWS::Glue::Database": {"CatalogId": "the account id"},
    "AWS::Glue::Job": {"Command.Name": "an enumerated job type"},
    "AWS::IAM::Role": {
        "AssumeRolePolicyDocument.Statement[].Condition.StringEquals.sts:ExternalId": (
            "a value the trust policy checks"
        ),
    },
    "AWS::Kinesis::Stream": {
        "StreamEncryption.Fn::If[].KeyId": "the AWS managed key it encrypts with",
    },
    "AWS::Logs::MetricFilter": {
        "MetricTransformations[].MetricName": "the metric the filter publishes",
    },
    "AWS::RDS::DBClusterParameterGroup": {"Family": "the engine family"},
    "AWS::RDS::DBParameterGroup": {"Family": "the engine family"},
    "AWS::S3::Bucket": {
        "LifecycleConfiguration.Rules[].Id": "a lifecycle rule id inside the bucket",
    },
}

#: ``rule -> (names AWS refuses, a name it accepts)``. Every refusal a rule
#: carries is exercised by at least one refused name.
EXAMPLES: dict[str, tuple[Rule, tuple[str, ...], str]] = {
    "ELASTICACHE_REPLICATION_GROUP": (
        ELASTICACHE_REPLICATION_GROUP,
        ("r" * 41, "redis-", "re--dis", "1redis", "re_dis"),
        "experimentation-redis-prod-redis",
    ),
    "ELASTICACHE_SUBNET_GROUP": (
        ELASTICACHE_SUBNET_GROUP,
        (
            "experimentation-redis-prod-redis-subnet-",
            "redis--subnet",
            "1redis",
            "redis_subnet",
            "r" * 256,
        ),
        "experimentation-redis-prod-redis-subnet",
    ),
    "ALB": (
        ALB,
        ("a" * 33, "-alb", "alb-", "internal-alb", "a_lb"),
        "experimentation-staging",
    ),
    "ECS": (ECS, ("a.b", "a b", "a" * 256), "experimentation-backend-prod"),
    "DASHBOARD": (DASHBOARD, ("a.b", "a" * 256), "experimentation-platform-prod"),
    "ALARM": (ALARM, ("a\nb", "a" * 256, ""), "AuroraHighCPU-prod"),
    "CODEDEPLOY": (CODEDEPLOY, ("a b", "a" * 101), "experimentation-platform-prod"),
    "COGNITO": (COGNITO, ("a/b", "a" * 129), "experimentation-platform-users-prod"),
    "DYNAMODB": (DYNAMODB, ("ab", "a b", "a" * 256), "experimentation-events-prod"),
    "EVENTS_RULE": (EVENTS_RULE, ("a b", "a" * 65), "experimentation-daily-etl-prod"),
    "GLUE_NAME": (GLUE_NAME, ("a\nb", "a" * 256), "experimentation-events-etl-prod"),
    "GLUE_DATABASE": (
        GLUE_DATABASE,
        ("Experimentation", "a-b"),
        "experimentation_prod",
    ),
    "IAM_ROLE": (IAM_ROLE, ("a" * 65, "a/b"), "experimentation-glue-role-prod"),
    "IAM_POLICY": (
        IAM_POLICY,
        ("a" * 129, "a b"),
        "CodeDeployRoleDefaultPolicyC10C0991",
    ),
    "KMS_ALIAS": (
        KMS_ALIAS,
        ("postgres-key", "alias/aws/rds", "alias/a b"),
        "alias/experimentation-database-prod-postgres-key",
    ),
    "LAMBDA": (LAMBDA, ("a" * 65, "a.b"), "experimentation-etl-trigger-prod"),
    "LOG_GROUP": (
        LOG_GROUP,
        ("aws/ecs/backend", "a b", "a" * 513),
        "/ecs/experimentation-backend-prod",
    ),
    "SCALING_POLICY": (SCALING_POLICY, ("a\tb", "a" * 257), "BackendServiceCPUScaling"),
    "SECRET": (
        SECRET,
        ("a b", "a" * 513),
        "experimentation-database-prod-aurora-credentials",
    ),
    "SNS_TOPIC": (SNS_TOPIC, ("a.b", "a" * 257), "experimentation-alerts-prod"),
    "SNS_DISPLAY_NAME": (SNS_DISPLAY_NAME, ("a" * 101, "a\nb"), "Controlvar Alerts"),
    "SSM_PARAMETER": (
        SSM_PARAMETER,
        ("a/b", "/aws/x", "ssm-x", "/a" * 16, "/a b", "/" + "a" * 1011),
        "/experimentation/prod/redis/endpoint",
    ),
    "AURORA_POSTGRES_DATABASE": (
        AURORA_POSTGRES_DATABASE,
        ("1db", "my-db", "a" * 64),
        "experimentation",
    ),
}

#: The Redis subnet group name in each environment. Staging's and dev's are
#: unchanged by the hyphen fix; prod's and demo's are the 40-character cut
#: without its trailing hyphen.
REDIS_SUBNET_GROUP_NAMES = {
    "dev": "experimentation-redis-dev-redis-subnet-g",
    "staging": "experimentation-redis-staging-redis-subn",
    "prod": "experimentation-redis-prod-redis-subnet",
    "demo": "experimentation-redis-demo-redis-subnet",
}

#: The rule paths only a module stack (the Glue ETL stack) holds a literal name
#: at. A core checkout reads every other rule path, and none of these.
MODULE_RULE_PATHS = frozenset(
    {
        ("AWS::Events::Rule", "Name"),
        ("AWS::Glue::Crawler", "DatabaseName"),
        ("AWS::Glue::Crawler", "Name"),
        ("AWS::Glue::Database", "DatabaseInput.Name"),
        ("AWS::Glue::Job", "Name"),
        ("AWS::IAM::Role", "RoleName"),
        ("AWS::Lambda::Function", "FunctionName"),
    }
)

_NAME_LIKE_KEY = re.compile(r"(Name|Id|Identifier|Family)$")


def _name_like(value, path: str = ""):
    """``(path, value)`` for every key that looks like a name, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else key
            if _NAME_LIKE_KEY.search(key):
                yield here, item
            yield from _name_like(item, here)
    elif isinstance(value, list):
        for item in value:
            yield from _name_like(item, f"{path}[]")


def literal_names(synth: Synth) -> list[tuple[str, str, str, str]]:
    """``(stack/logical id, type, path, name)`` for every literal name."""
    found = []
    for stack, logical_id, resource in synth.resources():
        properties = resource.get("Properties") or {}
        pairs = {(path, _literal(value)) for path, value in _name_like(properties)}
        pairs |= {
            (path, _literal(_get(properties, path)))
            for path in NAME_PROPERTIES.get(resource["Type"], ())
        }
        found += [
            (f"{stack}/{logical_id}", resource["Type"], path, name)
            for path, name in sorted(p for p in pairs if p[1] is not None)
        ]
    return found


@pytest.fixture(scope="module")
def names() -> dict[str, list[tuple[str, str, str, str]]]:
    return {env: literal_names(Synth(env)) for env in ENVIRONMENTS}


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_every_literal_name_has_a_rule(names, env):
    """Fail closed: each literal name is checked, or declared not to be a name."""
    unclassified = sorted(
        {
            (rtype, path, name)
            for _, rtype, path, name in names[env]
            if path not in RULES.get(rtype, {})
            and path not in NOT_A_NAME.get(rtype, {})
        }
    )
    assert not unclassified, (
        f"{env}: no rule for {unclassified}. Add each to RULES with the rule AWS "
        "applies to it, or to NOT_A_NAME with the reason it is not a name"
    )
    both = sorted(
        (rtype, path)
        for rtype, paths in RULES.items()
        for path in paths
        if path in NOT_A_NAME.get(rtype, {})
    )
    assert not both, f"{both} is in RULES and NOT_A_NAME"


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_every_literal_name_meets_its_aws_rule(names, env):
    """prod's and demo's Redis subnet group name ended in a hyphen.

    ``experimentation-redis-prod-redis-subnet-``: ElastiCache refused it, and
    ``cdk deploy`` stopped at ``experimentation-redis-prod``.
    """
    wrong = [
        f"{where} {path}={name!r} {why}"
        for where, rtype, path, name in names[env]
        if path in RULES.get(rtype, {})
        for why in RULES[rtype][path].violations(name)
    ]
    assert not wrong, f"{env}:\n" + "\n".join(wrong)


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_redis_subnet_group_name_is_pinned(names, env):
    """The fix strips the hyphen and keeps the cut.

    Without ``[:40]`` staging's name becomes
    ``experimentation-redis-staging-redis-subnet-group``: valid, and a
    replacement of staging's Redis.
    """
    found = [
        name
        for _, rtype, path, name in names[env]
        if (rtype, path) == ("AWS::ElastiCache::SubnetGroup", "CacheSubnetGroupName")
    ]
    assert found == [REDIS_SUBNET_GROUP_NAMES[env]], (
        f"{env}: the Redis subnet group is named {found}, expected "
        f"{REDIS_SUBNET_GROUP_NAMES[env]!r}"
    )


def test_the_scan_reads_every_name_a_rule_is_written_for(names):
    """Not vacuous: each rule path holds a literal name in some environment.

    Exact in both profiles. A core checkout reads every rule path except
    MODULE_RULE_PATHS, and none of those.
    """
    rule_paths = {(rtype, path) for rtype, paths in RULES.items() for path in paths}
    assert MODULE_RULE_PATHS <= rule_paths, sorted(MODULE_RULE_PATHS - rule_paths)
    expected = rule_paths if MODULES_PRESENT else rule_paths - MODULE_RULE_PATHS
    checked = {
        (rtype, path)
        for env in ENVIRONMENTS
        for _, rtype, path, _ in names[env]
        if path in RULES.get(rtype, {})
    }
    assert checked == expected, (
        f"no literal name at {sorted(expected - checked)}; read in a core "
        f"checkout but listed in MODULE_RULE_PATHS: {sorted(checked - expected)}"
    )


def test_every_rule_has_examples():
    """A rule in RULES with no examples is a rule nobody saw refuse anything."""
    used = {rule for paths in RULES.values() for rule in paths.values()}
    examined = {rule for rule, _, _ in EXAMPLES.values()}
    assert used == examined, (used - examined, examined - used)


@pytest.mark.parametrize(
    "rule, name",
    [
        pytest.param(rule, name, id=f"{key}-{index}")
        for key, (rule, refused, _) in EXAMPLES.items()
        for index, name in enumerate(refused)
    ],
)
def test_each_rule_refuses_what_aws_refuses(rule, name):
    assert rule.violations(name), f"{name!r} was accepted"


@pytest.mark.parametrize(
    "rule, name",
    [pytest.param(rule, name, id=key) for key, (rule, _, name) in EXAMPLES.items()],
)
def test_each_rule_accepts_what_aws_accepts(rule, name):
    assert not rule.violations(name)


def test_every_refusal_is_exercised():
    """Each refusal a rule carries fires on at least one of its refused names."""
    unexercised = [
        (key, why)
        for key, (rule, refused, _) in EXAMPLES.items()
        for regex, why in rule.refused
        if not any(re.search(regex, name) for name in refused)
    ]
    assert not unexercised, unexercised
