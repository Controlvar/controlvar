"""Two environments in one account neither collide nor leave billed residue.

#139 and #142, on a real ``app.synth()`` of every environment ``app.py``
accepts. Four properties, each asserted as exact values:

* **Names.** No account-scoped physical name is the same in two environments.
  Before this, dev and staging collided on 25 names plus the three Glue jobs
  and crawler -- the ECS cluster (``experimentation-dev`` in every
  environment, #142), the CodeDeploy application, the migration task family,
  fourteen alarms and dashboards, seven SSM parameters -- so the second
  environment deployed into an account failed on the first of them. A
  completeness guard classifies every CloudFormation resource type the app
  synthesises, so a new type cannot slip past the name scan unexamined.
* **Retention.** The exact DeletionPolicy of every resource that carries one,
  per environment: RETAIN/SNAPSHOT in prod, Delete everywhere else. A staging
  teardown left sixteen resources behind, eight of them named, so the next
  staging deploy failed on the names.
* **Sizing.** The exact Aurora instance and NAT gateway count per environment
  (T24, DECISIONS D7).
* **Teardown.** The stacks that must be destroyed before the cluster rename can
  reach an existing environment are *derived from the synthesised import
  graph* here; ``test_teardown_instructions.py`` holds the self-hosting CDK
  page's commands to that graph, so this file reads no documentation.
"""

from __future__ import annotations

import json
import re
import runpy
import shutil

import pytest

from .test_app_profiles import CDK_DIR, REPO_ROOT, _app_environment
from .test_dashboard_service import _synth

ENVIRONMENTS = ("dev", "staging", "prod", "demo")
MODULES_PRESENT = (REPO_ROOT / "modules" / "infrastructure" / "cdk" / "stacks").is_dir()

#: Where each resource type keeps a physical name that must be unique in an
#: account (and region). Stricter than AWS in places -- a Cognito pool name or
#: an ECS service name need not be unique -- because an environment-scoped name
#: costs nothing and a shared one has to be reasoned about.
NAME_PROPERTIES: dict[str, tuple[str, ...]] = {
    "AWS::CloudWatch::Alarm": ("AlarmName",),
    "AWS::CloudWatch::CompositeAlarm": ("AlarmName",),
    "AWS::CloudWatch::Dashboard": ("DashboardName",),
    "AWS::CodeDeploy::Application": ("ApplicationName",),
    "AWS::CodeDeploy::DeploymentGroup": ("DeploymentGroupName",),
    "AWS::Cognito::UserPool": ("UserPoolName",),
    "AWS::DynamoDB::Table": ("TableName",),
    "AWS::EC2::SecurityGroup": ("GroupName",),
    "AWS::ECS::Cluster": ("ClusterName",),
    "AWS::ECS::Service": ("ServiceName",),
    "AWS::ECS::TaskDefinition": ("Family",),
    "AWS::ElastiCache::ReplicationGroup": ("ReplicationGroupId",),
    "AWS::ElastiCache::SubnetGroup": ("CacheSubnetGroupName",),
    "AWS::ElasticLoadBalancingV2::LoadBalancer": ("Name",),
    "AWS::ElasticLoadBalancingV2::TargetGroup": ("Name",),
    "AWS::Events::Rule": ("Name",),
    "AWS::Glue::Crawler": ("Name",),
    "AWS::Glue::Database": ("DatabaseInput.Name",),
    "AWS::Glue::Job": ("Name",),
    "AWS::IAM::ManagedPolicy": ("ManagedPolicyName",),
    "AWS::IAM::Role": ("RoleName",),
    "AWS::KMS::Alias": ("AliasName",),
    "AWS::KMS::Key": (),  # addressed by generated id; its alias is the name
    "AWS::Kinesis::Stream": ("Name",),
    "AWS::KinesisFirehose::DeliveryStream": ("DeliveryStreamName",),
    "AWS::Lambda::Function": ("FunctionName",),
    "AWS::Logs::LogGroup": ("LogGroupName",),
    "AWS::OpenSearchService::Domain": ("DomainName",),
    "AWS::RDS::DBCluster": ("DBClusterIdentifier",),
    "AWS::RDS::DBClusterParameterGroup": ("DBClusterParameterGroupName",),
    "AWS::RDS::DBInstance": ("DBInstanceIdentifier",),
    "AWS::RDS::DBParameterGroup": ("DBParameterGroupName",),
    "AWS::RDS::DBSubnetGroup": ("DBSubnetGroupName",),
    "AWS::S3::Bucket": ("BucketName",),
    "AWS::SNS::Topic": ("TopicName",),
    "AWS::SSM::Parameter": ("Name",),
    "AWS::SecretsManager::Secret": ("Name",),
}

#: Types with no physical name an account could hold twice, and why.
NO_ACCOUNT_SCOPED_NAME: dict[str, str] = {
    "AWS::ApplicationAutoScaling::ScalableTarget": "identified by the resource it scales",
    "AWS::ApplicationAutoScaling::ScalingPolicy": "PolicyName is scoped to its scalable target",
    "AWS::Cognito::UserPoolClient": "ClientName is scoped to its user pool",
    "AWS::EC2::EIP": "no name",
    "AWS::EC2::InternetGateway": "no name (a Name tag is not unique)",
    "AWS::EC2::NatGateway": "no name (a Name tag is not unique)",
    "AWS::EC2::NetworkAcl": "no name",
    "AWS::EC2::NetworkAclEntry": "no name",
    "AWS::EC2::Route": "no name",
    "AWS::EC2::RouteTable": "no name",
    "AWS::EC2::SecurityGroupEgress": "no name",
    "AWS::EC2::SecurityGroupIngress": "no name",
    "AWS::EC2::Subnet": "no name",
    "AWS::EC2::SubnetNetworkAclAssociation": "no name",
    "AWS::EC2::SubnetRouteTableAssociation": "no name",
    "AWS::EC2::VPC": "no name",
    "AWS::EC2::VPCEndpoint": "no name",
    "AWS::EC2::VPCGatewayAttachment": "no name",
    "AWS::ElasticLoadBalancingV2::Listener": "no name",
    "AWS::ElasticLoadBalancingV2::ListenerRule": "no name",
    "AWS::IAM::Policy": "an inline policy: PolicyName is scoped to its role",
    "AWS::Lambda::EventSourceMapping": "no name",
    "AWS::Lambda::Permission": "no name",
    "AWS::Logs::MetricFilter": "FilterName is scoped to its log group",
    "AWS::S3::BucketPolicy": "no name",
    "AWS::SNS::Subscription": "no name",
    "AWS::SecretsManager::SecretTargetAttachment": "no name",
    "AWS::CDK::Metadata": "no name",
    "Custom::OpenSearchAccessPolicy": "a custom resource: no name",
    "Custom::S3AutoDeleteObjects": "a custom resource: no name",
}

#: Pairs of environments allowed to share a physical name. Empty, and meant
#: to stay that way.
SHARED_NAME_ALLOW_LIST: frozenset[tuple[str, str]] = frozenset()

#: CloudFormation pseudo parameters: the SAME value in every environment of
#: one account, so a name built only from literals and these is a literal.
PSEUDO_PARAMETERS = {
    "AWS::AccountId",
    "AWS::Region",
    "AWS::Partition",
    "AWS::URLSuffix",
}


class Synth:
    """One environment's assembly, reduced to what these tests read."""

    def __init__(self, env: str, assembly=None):
        if assembly is None:
            assembly = _synth(env)
        self.env = env
        self.templates = {s.stack_name: s.template for s in assembly.stacks}
        #: ``stack name -> (account, region)`` as the assembly resolved them.
        self.locations = {
            s.stack_name: (s.environment.account, s.environment.region)
            for s in assembly.stacks
        }
        self.dependencies = {
            s.stack_name: {
                d.stack_name for d in s.dependencies if hasattr(d, "stack_name")
            }
            for s in assembly.stacks
        }

    def short(self, stack_name: str) -> str:
        """``experimentation-fargate-staging`` -> ``experimentation-fargate``."""
        suffix = f"-{self.env}"
        assert stack_name.endswith(suffix), stack_name
        return stack_name[: -len(suffix)]

    def resources(self):
        for stack_name, template in sorted(self.templates.items()):
            for logical_id, resource in sorted(template.get("Resources", {}).items()):
                yield stack_name, logical_id, resource


@pytest.fixture(scope="module")
def synths() -> dict[str, Synth]:
    return {env: Synth(env) for env in ENVIRONMENTS}


def _get(properties: dict, path: str):
    value = properties
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _literal(value) -> str | None:
    """The value as the same string in every environment, or ``None``.

    A plain string is literal. So is an ``Fn::Join`` / ``Fn::Sub`` built only
    from strings and pseudo parameters -- ``foo-${AWS::Region}`` is ``foo-`` in
    every environment of a region. Anything referring to a resource (``Ref``,
    ``Fn::GetAtt``, ``Fn::ImportValue``) takes that resource's unique value.
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((function, argument),) = value.items()
    if function == "Ref":
        return f"${{{argument}}}" if argument in PSEUDO_PARAMETERS else None
    if function == "Fn::Join":
        separator, parts = argument
        rendered = [_literal(part) for part in parts]
        return None if None in rendered else separator.join(rendered)
    if function == "Fn::Sub" and isinstance(argument, str):
        references = set(re.findall(r"\$\{([^}]+)\}", argument))
        return argument if references <= PSEUDO_PARAMETERS else None
    return None


def literal_names(synth: Synth) -> dict[tuple[str, str], list[str]]:
    """``(type, literal name) -> [stack/logical id]`` for every named resource."""
    found: dict[tuple[str, str], list[str]] = {}
    for stack_name, logical_id, resource in synth.resources():
        for path in NAME_PROPERTIES.get(resource["Type"], ()):
            name = _literal(_get(resource.get("Properties") or {}, path))
            if name is not None:
                found.setdefault((resource["Type"], name), []).append(
                    f"{stack_name}/{logical_id}"
                )
    return found


# --- names ------------------------------------------------------------------


def test_every_resource_type_is_classified(synths):
    """A type the scan does not know is a type whose name nobody checked."""
    seen = {
        resource["Type"]
        for synth in synths.values()
        for _, _, resource in synth.resources()
    }
    unclassified = sorted(seen - NAME_PROPERTIES.keys() - NO_ACCOUNT_SCOPED_NAME.keys())
    assert not unclassified, (
        f"{unclassified} is not classified: add it to NAME_PROPERTIES with the "
        "property holding its physical name, or to NO_ACCOUNT_SCOPED_NAME with "
        "the reason it has none"
    )
    both = sorted(NAME_PROPERTIES.keys() & NO_ACCOUNT_SCOPED_NAME.keys())
    assert not both, f"{both} is classified both ways"


def test_the_name_scan_reads_the_names_that_matter(synths):
    """Not vacuous: the scan finds the names the deploy workflows address."""
    names = literal_names(synths["staging"])
    expected = {
        ("AWS::ECS::Cluster", "experimentation-staging"),
        ("AWS::CodeDeploy::Application", "experimentation-platform-staging"),
        ("AWS::ECS::TaskDefinition", "experimentation-migrate-staging"),
        ("AWS::ECS::TaskDefinition", "experimentation-backend-staging"),
        ("AWS::Logs::LogGroup", "/ecs/experimentation-backend-staging"),
        ("AWS::SSM::Parameter", "/experimentation/staging/vpc/id"),
        ("AWS::SNS::Topic", "experimentation-alerts-staging"),
        ("AWS::CloudWatch::CompositeAlarm", "experimentation-api-no-healthy-task-staging"),
        ("AWS::SSM::Parameter", "/experimentation/staging/database/aurora-cluster-identifier"),
    }
    if MODULES_PRESENT:
        expected |= {
            ("AWS::Glue::Job", "experimentation-events-etl-staging"),
            ("AWS::Glue::Database", "experimentation_staging"),
        }
    assert expected <= names.keys(), sorted(expected - names.keys())
    assert len(names) >= 50, len(names)


@pytest.mark.regression
@pytest.mark.parametrize(
    "first, second",
    [(a, b) for i, a in enumerate(ENVIRONMENTS) for b in ENVIRONMENTS[i + 1 :]],
)
def test_two_environments_share_no_physical_name(synths, first, second):
    """#139: dev and staging collided on 25 names and three Glue names."""
    if (first, second) in SHARED_NAME_ALLOW_LIST:
        pytest.skip("allow-listed")
    a, b = literal_names(synths[first]), literal_names(synths[second])
    shared = sorted(a.keys() & b.keys())
    assert not shared, "\n".join(
        f"{rtype} {name!r} is identical in {first} ({a[(rtype, name)]}) and "
        f"{second}; an account can hold only one"
        for rtype, name in shared
    )


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_cluster_is_named_for_its_environment(synths, env):
    """#142: the cluster was `experimentation-dev` in every environment."""
    clusters = [
        r["Properties"].get("ClusterName")
        for _, _, r in synths[env].resources()
        if r["Type"] == "AWS::ECS::Cluster"
    ]
    assert clusters == [f"experimentation-{env}"], (
        f"{env} cluster is {clusters}, expected experimentation-{env}"
    )


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_no_lambda_is_told_it_runs_in_another_environment(synths, env):
    """The same context key made the analytics Lambda say ENVIRONMENT=dev."""
    told = {
        f"{stack}/{lid}": r["Properties"]["Environment"]["Variables"]["ENVIRONMENT"]
        for stack, lid, r in synths[env].resources()
        if r["Type"] == "AWS::Lambda::Function"
        and "ENVIRONMENT"
        in ((r["Properties"].get("Environment") or {}).get("Variables") or {})
    }
    expected_stacks = {f"experimentation-compute-{env}"}
    if MODULES_PRESENT:
        expected_stacks.add(f"experimentation-analytics-{env}")
    assert {key.split("/")[0] for key in told} == expected_stacks, told
    assert set(told.values()) == {env}, told


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_api_is_given_the_glue_names_the_glue_stack_creates(synths, env):
    """Renaming the Glue jobs must not strand the API on the old names."""
    if not MODULES_PRESENT:
        pytest.skip("core checkout: no etl module")
    created = {
        name
        for (rtype, name) in literal_names(synths[env])
        if rtype in ("AWS::Glue::Job", "AWS::Glue::Crawler", "AWS::Glue::Database")
    }
    fargate = synths[env].templates[f"experimentation-fargate-{env}"]["Resources"]
    (backend,) = [
        c
        for r in fargate.values()
        if r["Type"] == "AWS::ECS::TaskDefinition"
        and r["Properties"]["Family"] == f"experimentation-backend-{env}"
        for c in r["Properties"]["ContainerDefinitions"]
    ]
    told = {
        e["Name"]: e["Value"]
        for e in backend["Environment"]
        if e["Name"].startswith("GLUE_")
    }
    assert set(told) == {
        "GLUE_ETL_JOB_NAME",
        "GLUE_METRICS_JOB_NAME",
        "GLUE_DATABASE",
        "GLUE_CRAWLER_NAME",
    }, told
    assert set(told.values()) == created, (told, created)


# --- the counters table (#392) ------------------------------------------------
#
# The API task was given neither the counters table's name nor any access to
# it: DYNAMODB_COUNTERS_TABLE fell back to a name no stack creates, and the task
# role carried no dynamodb action at all. Every assertion below compares with
# what the counters stack actually SYNTHESISED -- its TableName, its account and
# region -- not with stacks/names.py, so the producer and the consumer are each
# checked against the other rather than both against one helper.


def _backend_task_definition(synth: Synth) -> dict:
    fargate = synth.templates[f"experimentation-fargate-{synth.env}"]["Resources"]
    (task_definition,) = [
        r
        for r in fargate.values()
        if r["Type"] == "AWS::ECS::TaskDefinition"
        and r["Properties"]["Family"] == f"experimentation-backend-{synth.env}"
    ]
    return task_definition


def _backend_environment(synth: Synth) -> list[tuple[str, object]]:
    (backend,) = _backend_task_definition(synth)["Properties"]["ContainerDefinitions"]
    return [(e["Name"], e["Value"]) for e in backend.get("Environment", [])]


def _counters_table(synth: Synth) -> tuple[str, str, str]:
    """``(TableName, account, region)`` of the table the counters stack creates."""
    stack = f"experimentation-dynamodb-counters-{synth.env}"
    tables = [
        r["Properties"]["TableName"]
        for r in synth.templates[stack]["Resources"].values()
        if r["Type"] == "AWS::DynamoDB::Table"
    ]
    assert len(tables) == 1 and isinstance(tables[0], str), tables
    account, region = synth.locations[stack]
    return tables[0], account, region


def _render(value, account: str, region: str) -> str | None:
    """An IAM resource as the ARN CloudFormation would produce, or ``None``.

    Strings, and ``Fn::Join`` over strings and the partition/account/region
    pseudo parameters -- what ``Stack.format_arn`` emits. Anything else (a
    resource reference, an import) is not a name this test can check, and
    renders as ``None`` so it fails the comparison.
    """
    pseudo = {"AWS::Partition": "aws", "AWS::AccountId": account, "AWS::Region": region}
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"Ref"}:
        return pseudo.get(value["Ref"])
    if isinstance(value, dict) and set(value) == {"Fn::Join"}:
        separator, parts = value["Fn::Join"]
        rendered = [_render(part, account, region) for part in parts]
        return None if None in rendered else separator.join(rendered)
    return None


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _task_role_statements(synth: Synth) -> list[dict]:
    """Every policy statement attached to the API task role, found via TaskRoleArn."""
    task_role_arn = _backend_task_definition(synth)["Properties"]["TaskRoleArn"]
    role_id, attribute = task_role_arn["Fn::GetAtt"]
    assert attribute == "Arn", task_role_arn
    fargate = synth.templates[f"experimentation-fargate-{synth.env}"]["Resources"]
    role = fargate[role_id]
    assert role["Type"] == "AWS::IAM::Role", role["Type"]
    statements = [
        statement
        for policy in role["Properties"].get("Policies", [])
        for statement in _as_list(policy["PolicyDocument"]["Statement"])
    ]
    for resource in fargate.values():
        if resource["Type"] == "AWS::IAM::Policy" and {"Ref": role_id} in resource[
            "Properties"
        ].get("Roles", []):
            statements += _as_list(resource["Properties"]["PolicyDocument"]["Statement"])
    return statements


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_api_is_given_the_counters_table_the_counters_stack_creates(synths, env):
    """#392: the API task was told no table name, so it used one nothing creates."""
    if not MODULES_PRESENT:
        pytest.skip("core checkout: no counters module")
    synth = synths[env]
    table_name, _, table_region = _counters_table(synth)
    _, fargate_region = synth.locations[f"experimentation-fargate-{env}"]
    told = _backend_environment(synth)

    names = [value for name, value in told if name == "DYNAMODB_COUNTERS_TABLE"]
    assert names == [table_name], (
        f"{env}: the API task is told DYNAMODB_COUNTERS_TABLE={names}; the "
        f"counters stack creates {table_name!r}"
    )
    # The service builds its client with no region, and botocore reads the
    # default region from AWS_DEFAULT_REGION only.
    regions = [value for name, value in told if name == "AWS_DEFAULT_REGION"]
    assert regions == [fargate_region] == [table_region], (
        f"{env}: AWS_DEFAULT_REGION={regions}; the stacks are in "
        f"{fargate_region} (fargate) and {table_region} (counters)"
    )
    # AWS_REGION is read explicitly by other settings; this change must not set it.
    assert "AWS_REGION" not in {name for name, _ in told}, told


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_api_task_role_may_query_and_update_the_counters_table_only(synths, env):
    """#392: the task role carried no dynamodb action at all.

    Exactly the two calls DynamoDBCounterService makes, on exactly the table the
    counters stack creates: not its index, not a wildcard, not another role.
    """
    if not MODULES_PRESENT:
        pytest.skip("core checkout: no counters module")
    synth = synths[env]
    table_name, account, region = _counters_table(synth)

    dynamodb = [
        statement
        for statement in _task_role_statements(synth)
        if any(
            str(action).lower().startswith("dynamodb:")
            for action in _as_list(statement.get("Action", []))
        )
    ]
    assert len(dynamodb) == 1, f"{env}: task role dynamodb statements: {dynamodb}"
    (statement,) = dynamodb
    assert statement["Effect"] == "Allow", statement
    assert set(_as_list(statement["Action"])) == {
        "dynamodb:Query",
        "dynamodb:UpdateItem",
    }, statement["Action"]

    resources = _as_list(statement["Resource"])
    assert len(resources) == 1, f"{env}: expected the table alone, got {resources}"
    assert _render(resources[0], account, region) == (
        f"arn:aws:dynamodb:{region}:{account}:table/{table_name}"
    ), resources[0]


@pytest.fixture(scope="module")
def core_synths(tmp_path_factory) -> dict[tuple[str, str], Synth]:
    """Every environment's core assembly, by both routes to the core profile.

    ``copy``: a checkout with no ``modules/`` beside the CDK app (what a core
    checkout is). ``variable``: this checkout with ``EXPERIMENTLY_PROFILE=core``.
    """
    root = tmp_path_factory.mktemp("core-counters")
    core_dir = root / "infrastructure" / "cdk"
    shutil.copytree(CDK_DIR, core_dir)
    routes = {"copy": (core_dir, {}), "variable": (CDK_DIR, {"EXPERIMENTLY_PROFILE": "core"})}
    found = {}
    for route, (cdk_dir, overrides) in routes.items():
        for env in ENVIRONMENTS:
            with _app_environment(cdk_dir, ENVIRONMENT=env, **overrides):
                namespace = runpy.run_path(str(cdk_dir / "app.py"), run_name="__main__")
            assert not namespace["ENABLE_MODULE_STACKS"], (route, env)
            found[(route, env)] = Synth(env, namespace["app"].synth())
    return found


@pytest.mark.parametrize("route", ["copy", "variable"])
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_core_api_is_given_no_counters_table(core_synths, route, env):
    """A core deployment has no counters table, so its API gets none of #392."""
    synth = core_synths[(route, env)]
    assert not [s for s in synth.templates if "dynamodb-counters" in s], sorted(
        synth.templates
    )
    told = {name for name, _ in _backend_environment(synth)}
    assert not told & {"DYNAMODB_COUNTERS_TABLE", "AWS_DEFAULT_REGION"}, told
    fargate = f"experimentation-fargate-{env}"
    assert "dynamodb" not in json.dumps(synth.templates[fargate]).lower()
    assert not [d for d in synth.dependencies[fargate] if "counters" in d], (
        synth.dependencies[fargate]
    )



# --- the etl module's Glue access (#487) --------------------------------------
#
# The API task's role carried no glue action, so every ETL route was refused,
# and the Glue client looked in settings.AWS_REGION (us-east-1 by default)
# rather than the region the stacks deploy to. The grant is three statements,
# each on this environment's objects alone. Every expected name is read from
# what the glue-etl stack SYNTHESISED -- its job, crawler and database names,
# its account and region -- not from stacks/names.py, so the producer and the
# consumer are each checked against the other.

ETL_SERVICE = REPO_ROOT / "modules" / "backend" / "app" / "services" / "etl_service.py"

#: Attributes of a Glue client that are not operations, and so need no grant.
#: Anything else reached on ``self._glue()`` must be a Glue operation.
GLUE_CLIENT_NON_OPERATIONS = frozenset({"exceptions"})


def _glue_created(synth: Synth) -> dict:
    """What the glue-etl stack creates: names, plus its account and region."""
    stack = f"experimentation-glue-etl-{synth.env}"
    resources = list(synth.templates[stack]["Resources"].values())

    def names(rtype: str, path: str) -> list[str]:
        found = [_get(r["Properties"], path) for r in resources if r["Type"] == rtype]
        assert all(isinstance(name, str) for name in found), (rtype, found)
        return found

    jobs = names("AWS::Glue::Job", "Name")
    (crawler,) = names("AWS::Glue::Crawler", "Name")
    (database,) = names("AWS::Glue::Database", "DatabaseInput.Name")
    (crawled,) = names("AWS::Glue::Crawler", "DatabaseName")
    assert crawled == database, (crawled, database)
    assert len(jobs) == 2, jobs
    account, region = synth.locations[stack]
    return {
        "jobs": jobs,
        "crawler": crawler,
        "database": database,
        "account": account,
        "region": region,
    }


def _expected_glue_statements(synth: Synth) -> set[tuple[frozenset, frozenset]]:
    """G1-G3 as ``(actions, rendered resources)`` pairs, from the glue-etl template."""
    created = _glue_created(synth)
    arn = f"arn:aws:glue:{created['region']}:{created['account']}"
    database = created["database"]
    return {
        (
            frozenset({"glue:StartJobRun", "glue:GetJobRun"}),
            frozenset(f"{arn}:job/{job}" for job in created["jobs"]),
        ),
        (
            frozenset({"glue:StartCrawler", "glue:GetCrawler"}),
            frozenset({f"{arn}:crawler/{created['crawler']}"}),
        ),
        (
            frozenset({"glue:GetTable", "glue:BatchCreatePartition"}),
            frozenset(
                {
                    f"{arn}:catalog",
                    f"{arn}:database/{database}",
                    f"{arn}:table/{database}/*",
                }
            ),
        ),
    }


def _actions(statement: dict) -> list[str]:
    return [str(a) for a in _as_list(statement.get("Action", []))]


def _glue_statements(synth: Synth) -> list[dict]:
    return [
        statement
        for statement in _task_role_statements(synth)
        if any(a.lower().startswith("glue:") for a in _actions(statement))
    ]


def _rendered(synth: Synth, statement: dict) -> list[str | None]:
    account, region = synth.locations[f"experimentation-fargate-{synth.env}"]
    return [_render(r, account, region) for r in _as_list(statement.get("Resource", []))]


def _role_statements(synth: Synth, arn_property: str) -> list[dict]:
    """Every policy statement attached to one of the backend task definition's roles."""
    role_arn = _backend_task_definition(synth)["Properties"][arn_property]
    role_id, attribute = role_arn["Fn::GetAtt"]
    assert attribute == "Arn", role_arn
    fargate = synth.templates[f"experimentation-fargate-{synth.env}"]["Resources"]
    statements = [
        statement
        for policy in fargate[role_id]["Properties"].get("Policies", [])
        for statement in _as_list(policy["PolicyDocument"]["Statement"])
    ]
    for resource in fargate.values():
        if resource["Type"] == "AWS::IAM::Policy" and {"Ref": role_id} in resource[
            "Properties"
        ].get("Roles", []):
            statements += _as_list(resource["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _glue_calls_in_etl_service() -> set[str]:
    """The IAM actions ``etl_service.py`` needs, read from its source.

    Every ``self._glue().<attribute>`` is mapped through botocore's offline
    Glue model to ``glue:<Operation>``. ``exceptions`` is the one attribute
    that is not an operation; any other name that is not one fails here, so a
    typo or an unmodelled attribute cannot drop out of the comparison. A call
    through a client held in a variable is not seen by this walk: the
    recording fake in test_etl_service_calls.py sees every call however the
    client is reached.
    """
    import ast

    import botocore.session
    from botocore import xform_name

    model = botocore.session.get_session().get_service_model("glue")
    operations = {xform_name(op): op for op in model.operation_names}
    source = ETL_SERVICE.read_text()
    tree = ast.parse(source)

    found: set[str] = set()
    unmapped: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "_glue"
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "self"
        ):
            if node.attr in operations:
                found.add(f"glue:{operations[node.attr]}")
            elif node.attr not in GLUE_CLIENT_NON_OPERATIONS:
                unmapped.add(node.attr)
    assert not unmapped, (
        f"self._glue().{sorted(unmapped)} is neither a Glue operation nor in "
        f"{sorted(GLUE_CLIENT_NON_OPERATIONS)}"
    )

    # The client is built in one place, so every call goes through _glue().
    builders = [
        function.name
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "boto3"
    ]
    assert builders == ["_glue"], f"boto3 is called in {builders}, not only in _glue"
    assert source.count("boto3.client(") == 1, "boto3.client( outside _glue"
    return found


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_api_task_role_may_use_this_environments_glue_objects(synths, env):
    """#487: the task role carried no glue action, so every ETL route was refused.

    Exactly three statements -- the jobs, the crawler, the catalog objects --
    compared as a set of (actions, rendered resources) pairs, so neither their
    order nor the policy minimiser's merging can hide a change.
    """
    if not MODULES_PRESENT:
        pytest.skip("core checkout: no etl module")
    synth = synths[env]
    statements = _glue_statements(synth)
    for statement in statements:
        assert set(statement) == {"Action", "Effect", "Resource"}, statement
        assert statement["Effect"] == "Allow", statement
    granted = {
        (frozenset(_actions(s)), frozenset(_rendered(synth, s))) for s in statements
    }
    assert granted == _expected_glue_statements(synth), (
        f"{env}: task role glue statements {statements}"
    )
    assert len(statements) == 3, statements


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_glue_grant_is_the_calls_the_etl_service_makes(synths, env):
    """No grant the service does not use, and no call the role does not grant."""
    if not MODULES_PRESENT:
        pytest.skip("core checkout: no etl module")
    granted = {a for s in _glue_statements(synths[env]) for a in _actions(s)}
    calls = _glue_calls_in_etl_service()
    assert granted == calls, (
        f"{env}: granted but not called {sorted(granted - calls)}; "
        f"called but not granted {sorted(calls - granted)}"
    )


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_glue_grant_names_exact_arns(synths, env):
    """The exact names keep each environment's ETL routes on that environment's Glue objects.

    No bare ``*``, no ``job/*`` or ``crawler/*``, nothing that does not render
    to a plain ARN. The one wildcard is the tables of this environment's
    database.
    """
    if not MODULES_PRESENT:
        pytest.skip("core checkout: no etl module")
    synth = synths[env]
    database = _glue_created(synth)["database"]
    statements = _glue_statements(synth)
    assert statements, f"{env}: no glue statements to check"
    for statement in statements:
        for action in _actions(statement):
            assert "*" not in action, statement
        for arn in _rendered(synth, statement):
            assert arn is not None, statement
            assert arn != "*" and arn.startswith("arn:aws:glue:"), arn
            resource = arn.split(":", 5)[5]
            if "*" in resource:
                assert resource == f"table/{database}/*", arn
            assert resource == "catalog" or env in resource, arn


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_glue_grant_names_what_the_api_is_told(synths, env):
    """The exact names keep each environment's ETL routes on that environment's Glue objects.

    The job, crawler and database in the granted ARNs are the values of the
    task's ``GLUE_*`` variables.
    """
    if not MODULES_PRESENT:
        pytest.skip("core checkout: no etl module")
    synth = synths[env]
    told = dict(_backend_environment(synth))
    named: dict[str, set[str]] = {"job": set(), "crawler": set(), "database": set()}
    for statement in _glue_statements(synth):
        for arn in _rendered(synth, statement):
            kind, _, name = arn.split(":", 5)[5].partition("/")
            if kind in named:
                named[kind].add(name)
            elif kind == "table":
                named["database"].add(name.split("/")[0])
    assert named == {
        "job": {told["GLUE_ETL_JOB_NAME"], told["GLUE_METRICS_JOB_NAME"]},
        "crawler": {told["GLUE_CRAWLER_NAME"]},
        "database": {told["GLUE_DATABASE"]},
    }, (named, told)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_task_role_has_no_athena_s3_or_sts_and_the_execution_role_no_glue(
    synths, env
):
    """Athena, S3 and STS are not granted to the API task's role (T54/D25)."""
    synth = synths[env]
    task_actions = {
        a.lower() for s in _role_statements(synth, "TaskRoleArn") for a in _actions(s)
    }
    assert task_actions, "the task role's statements were not found"
    assert "*" not in task_actions, task_actions
    assert not {
        a for a in task_actions if a.startswith(("athena:", "s3:", "sts:", "glue:*"))
    }, task_actions
    execution_actions = {
        a.lower()
        for s in _role_statements(synth, "ExecutionRoleArn")
        for a in _actions(s)
    }
    assert execution_actions, "the execution role's statements were not found"
    assert not {a for a in execution_actions if a.startswith("glue:")}, (
        execution_actions
    )


@pytest.mark.parametrize("route", ["copy", "variable"])
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_core_api_is_given_no_glue_access(core_synths, route, env):
    """A core deployment has no Glue, so its API gets no Glue name and no grant."""
    synth = core_synths[(route, env)]
    told = {name for name, _ in _backend_environment(synth)}
    assert not {name for name in told if name.startswith("GLUE_")}, told
    actions = {
        a.lower()
        for arn_property in ("TaskRoleArn", "ExecutionRoleArn")
        for s in _role_statements(synth, arn_property)
        for a in _actions(s)
    }
    assert actions, "no role statements found"
    assert not {a for a in actions if a.startswith("glue:")}, actions
    fargate = f"experimentation-fargate-{env}"
    assert '"glue:' not in json.dumps(synth.templates[fargate]).lower()


# --- retention ----------------------------------------------------------------

#: Every resource that carries a DeletionPolicy, by (stack, type, logical id),
#: and what prod does with it. Outside prod every one of them is "Delete".
_CORE_POLICIES = {
    ("experimentation-auth", "AWS::Cognito::UserPool", "ExperimentationUserPool8B5B85D6"): "Retain",
    ("experimentation-compute", "AWS::Logs::LogGroup", "DatabaseLambdaLogs0B6EB9BE"): "Delete",
    ("experimentation-database", "AWS::RDS::DBCluster", "AuroraCluster23D869C0"): "Snapshot",
    ("experimentation-database", "AWS::RDS::DBInstance", "AuroraClusterInstance19E8278EB"): "Delete",
    ("experimentation-database", "AWS::SecretsManager::Secret", "DBCredentialsCBF39AE9"): "Delete",
    ("experimentation-database", "AWS::KMS::Key", "DatabaseEncryptionKey10487C37"): "Retain",
    ("experimentation-dynamodb", "AWS::DynamoDB::Table", "EventsTableD24865E5"): "Retain",
    ("experimentation-dynamodb", "AWS::DynamoDB::Table", "FeatureFlagsTable00AD5587"): "Retain",
    ("experimentation-dynamodb", "AWS::DynamoDB::Table", "OverridesTable7BDFBA35"): "Retain",
    ("experimentation-fargate", "AWS::Logs::LogGroup", "BackendLogGroupDA10F1B2"): "Retain",
    ("experimentation-fargate", "AWS::Logs::LogGroup", "DashboardLogGroupE8E0E1A2"): "Retain",
    ("experimentation-migrations", "AWS::Logs::LogGroup", "MigrationLogs670D4322"): "Retain",
}
_MODULE_POLICIES = {
    ("experimentation-analytics", "AWS::S3::Bucket", "DataLakeBucket0256EA8E"): "Retain",
    ("experimentation-analytics", "AWS::Kinesis::Stream", "EventsStream287662BE"): "Retain",
    ("experimentation-analytics", "AWS::OpenSearchService::Domain", "ExperimentationDomain3B6A1339"): "Retain",
    ("experimentation-analytics", "Custom::OpenSearchAccessPolicy", "ExperimentationDomainAccessPolicyCF0D0276"): "Delete",
    ("experimentation-dynamodb-counters", "AWS::DynamoDB::Table", "ExperimentCountersTableE64AC51F"): "Retain",
    ("experimentation-glue-etl", "AWS::S3::Bucket", "AthenaResultsBucket879938FA"): "Retain",
    ("experimentation-glue-etl", "AWS::S3::Bucket", "GlueScriptsBucketCD60B14C"): "Retain",
}
#: Present only where the buckets are destroyed: the custom resources that
#: empty them first. Their provider -- the CDK's
#: Custom::S3AutoDeleteObjectsCustomResourceProvider Lambda and its role -- is
#: what `auto_delete_objects=True` adds to the stack.
_AUTO_DELETE = {
    ("experimentation-analytics", "Custom::S3AutoDeleteObjects", "DataLakeBucketAutoDeleteObjectsCustomResourceBA68F53A"),
    ("experimentation-glue-etl", "Custom::S3AutoDeleteObjects", "AthenaResultsBucketAutoDeleteObjectsCustomResourceD2206C34"),
    ("experimentation-glue-etl", "Custom::S3AutoDeleteObjects", "GlueScriptsBucketAutoDeleteObjectsCustomResource5EF6A9DF"),
}


def _expected_policies(env: str) -> dict[tuple[str, str, str], str]:
    prod = dict(_CORE_POLICIES)
    if MODULES_PRESENT:
        prod.update(_MODULE_POLICIES)
    if env == "prod":
        # prod's second Aurora instance (T24): a reader, deleted with the cluster.
        prod[("experimentation-database", "AWS::RDS::DBInstance", "AuroraClusterInstance2FE2217C4")] = "Delete"
        return prod
    expected = {key: "Delete" for key in prod}
    if MODULES_PRESENT:
        expected.update({key: "Delete" for key in _AUTO_DELETE})
    return expected


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_retention_is_chosen_per_environment(synths, env):
    """The exact DeletionPolicy table: prod keeps its data, nothing else does.

    A staging teardown left 16 resources behind (#139): Cognito, six named
    DynamoDB tables, Kinesis, three buckets, OpenSearch, the KMS key, an Aurora
    snapshot and two named log groups. The named ones then made the next
    staging deploy fail.
    """
    synth = synths[env]
    actual = {
        (synth.short(stack), r["Type"], lid): r["DeletionPolicy"]
        for stack, lid, r in synth.resources()
        if "DeletionPolicy" in r
    }
    expected = _expected_policies(env)
    wrong = {
        key: (actual.get(key), policy)
        for key, policy in expected.items()
        if actual.get(key) != policy
    }
    extra = sorted(actual.keys() - expected.keys())
    assert not wrong and not extra, (
        f"{env}: (actual, expected) {wrong}; unexpected {extra}"
    )
    # UpdateReplacePolicy follows: a replacement must not orphan what a
    # deletion would have destroyed, or keep what it would have kept.
    for stack, lid, r in synth.resources():
        if "DeletionPolicy" in r:
            assert r.get("UpdateReplacePolicy") == r["DeletionPolicy"], (stack, lid)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_a_destroyed_bucket_is_emptied_first(synths, env):
    """CloudFormation deletes only an empty bucket; the data lake never is."""
    if not MODULES_PRESENT:
        pytest.skip("core checkout: no buckets")
    synth = synths[env]
    buckets = {
        (stack, lid): r
        for stack, lid, r in synth.resources()
        if r["Type"] == "AWS::S3::Bucket"
    }
    emptied = {
        (stack, r["Properties"]["BucketName"]["Ref"])
        for stack, lid, r in synth.resources()
        if r["Type"] == "Custom::S3AutoDeleteObjects"
    }
    tagged = {
        key
        for key, r in buckets.items()
        if {"Key": "aws-cdk:auto-delete-objects", "Value": "true"}
        in ((r.get("Properties") or {}).get("Tags") or [])
    }
    assert len(buckets) == 3, sorted(buckets)
    if env == "prod":
        assert not emptied and not tagged, (emptied, tagged)
    else:
        assert emptied == set(buckets) == tagged, (sorted(buckets), emptied, tagged)


# --- sizing -------------------------------------------------------------------

AURORA_INSTANCES = {"dev": 1, "staging": 1, "prod": 2, "demo": 1}
NAT_GATEWAYS = {"dev": 1, "staging": 1, "prod": 2, "demo": 1}


def _count(synth: Synth, rtype: str) -> int:
    return sum(1 for _, _, r in synth.resources() if r["Type"] == rtype)


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_aurora_instance_count_per_environment(synths, env):
    """T24 (EM condition 7): staging runs one instance, prod two."""
    assert _count(synths[env], "AWS::RDS::DBInstance") == AURORA_INSTANCES[env]


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_nat_gateway_count_per_environment(synths, env):
    """DECISIONS D7: one NAT gateway outside prod."""
    assert _count(synths[env], "AWS::EC2::NatGateway") == NAT_GATEWAYS[env]


# --- outputs B3 reads -----------------------------------------------------------


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_stacks_say_what_a_workflow_needs_to_address(synths, env):
    """The cluster identifier, and where a one-off task must run."""
    synth = synths[env]
    database = synth.templates[f"experimentation-database-{env}"]
    (cluster_id,) = [
        lid for lid, r in database["Resources"].items() if r["Type"] == "AWS::RDS::DBCluster"
    ]
    assert database["Outputs"]["ClusterIdentifier"]["Value"] == {"Ref": cluster_id}

    compute = synth.templates[f"experimentation-compute-{env}"]
    (ecs_sg,) = [
        lid
        for lid, r in compute["Resources"].items()
        if r["Type"] == "AWS::EC2::SecurityGroup"
    ]
    outputs = synth.templates[f"experimentation-fargate-{env}"]["Outputs"]
    sg = outputs["TaskSecurityGroup"]["Value"]["Fn::ImportValue"]
    assert sg.startswith(f"experimentation-compute-{env}:") and ecs_sg in sg, sg

    separator, parts = outputs["TaskSubnets"]["Value"]["Fn::Join"]
    imports = [p["Fn::ImportValue"] for p in parts if isinstance(p, dict)]
    assert len(imports) == 2 and all("PrivateSubnet" in i for i in imports), parts

    # The service runs in exactly those subnets and that group.
    (service,) = [
        r
        for r in synth.templates[f"experimentation-fargate-{env}"]["Resources"].values()
        if r["Type"] == "AWS::ECS::Service"
        and r["Properties"].get("ServiceName") == f"experimentation-backend-{env}"
    ]
    network = service["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert [s["Fn::ImportValue"] for s in network["Subnets"]] == imports
    assert [g["Fn::ImportValue"] for g in network["SecurityGroups"]] == [sg]


# --- teardown -----------------------------------------------------------------


def _exports(synth: Synth) -> dict[str, str]:
    return {
        output["Export"]["Name"]: stack
        for stack, template in synth.templates.items()
        for output in template.get("Outputs", {}).values()
        if "Export" in output and isinstance(output["Export"]["Name"], str)
    }


def _imports(value, found: set[str]) -> set[str]:
    if isinstance(value, dict):
        if "Fn::ImportValue" in value and isinstance(value["Fn::ImportValue"], str):
            found.add(value["Fn::ImportValue"])
        for item in value.values():
            _imports(item, found)
    elif isinstance(value, list):
        for item in value:
            _imports(item, found)
    return found


def import_graph(synth: Synth) -> dict[str, set[str]]:
    """``export name -> {importing stack}`` over the whole app."""
    graph: dict[str, set[str]] = {name: set() for name in _exports(synth)}
    for stack, template in synth.templates.items():
        for name in _imports(template.get("Resources", {}), set()) | _imports(
            template.get("Outputs", {}), set()
        ):
            graph.setdefault(name, set()).add(stack)
    return graph


def cluster_export_importers(synth: Synth) -> set[str]:
    """The stacks importing the compute stack's ECS cluster ``Ref``."""
    compute = f"experimentation-compute-{synth.env}"
    (cluster,) = [
        lid
        for lid, r in synth.templates[compute]["Resources"].items()
        if r["Type"] == "AWS::ECS::Cluster"
    ]
    exported = [
        output["Export"]["Name"]
        for output in synth.templates[compute].get("Outputs", {}).values()
        if output["Value"] == {"Ref": cluster} and "Export" in output
    ]
    assert len(exported) == 1, exported
    return {synth.short(s) for s in import_graph(synth)[exported[0]]}
