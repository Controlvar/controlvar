"""A stateful fake ``aws`` for the AWS installer beta (deploy/aws-installer/).

It answers as the AWS CLI v2 does, from one JSON account state. The ``aws`` on
PATH is a shim (``world.py``) that runs ``python -I -B fake_aws.py <state> <shapes>
<arguments...>``, so the call's environment is exactly what its caller passed:

* **Each tool is its fake.** The shim's folder is first on PATH; anything the
  harness does not fake reaches the test session's failing ``aws`` (exit 97,
  backend/tests/no_real_aws.py) or is not found.

* **Regions.** Each region has its own resources (``state["regions"][<region>]``);
  IAM, Route 53, S3's bucket list, CloudFront and the caller identity are global
  (``state["global"]``). The region is ``--region``, else ``AWS_REGION``, else
  ``AWS_DEFAULT_REGION``, else the profile's ``region`` in ``AWS_CONFIG_FILE``, as
  the CLI resolves it; with none, exit 253 and the CLI's message. A region the
  state does not hold is not modelled (exit 99).
* **Responses** are built from the state in the shape botocore's model gives the
  operation, and checked against it before anything prints (``shapes.validate``,
  on the document ``shapes.extract`` wrote). Keys are put in the model's member
  order, as the CLI's parsers do.
* **Output** is what the CLI prints for that response: a paginated operation
  prints the paginator's result keys only (botocore's ``build_full_result``),
  unless a paging option is given by hand (the paginator's input token or limit
  key), when the CLI makes one request and prints that page as the service sent
  it, next-page token included; ``--query`` is applied with jmespath,
  ``--output json`` is the CLI's JSON (an
  empty object prints nothing), and ``--output text`` is the CLI's text format:
  a row per line, columns joined by tabs, ``None`` for null, ``True``/``False``.
  A timestamp a JSON-protocol service sends as epoch seconds prints in the local
  time zone; one an XML-protocol service sends prints in UTC.
  scripts/check_installer_cli.py proves this against the real CLI on a local stub
  for every shape the beta reads (gate H6).
* **Faults** (``state["faults"]``, a list, the first that matches applies): each
  names a ``mode`` and optionally the ``service``, ``op`` and ``region`` it
  matches, the ``call`` number (1-based, over every call) or ``nth`` (the nth call
  it matches that no earlier fault took). Modes: ``AccessDenied`` (the service's
  own code, exit 254), ``EMPTY`` (exit 0, nothing printed), ``NOQUERY``
  (``--query`` ignored: the whole response printed) and ``error`` (its own
  ``code``, ``message`` and ``exit``).
* **Errors** print as the CLI prints a service's error, in one of the two forms
  CLIs are measured to print: ``aws: [ERROR]: An error occurred (Code) when
  calling the Op operation: message`` (aws-cli 2.38.1, the default here), or the
  same without ``aws: [ERROR]: `` (aws-cli 2.28.24, the run's; ``state
  ["error_style"] = "plain"``). Code that reads an error must read the code in
  parentheses, never the start of the line.
* **Logs.** Every call is one JSON line in ``calls.log`` beside the state: its
  number, service, operation, region, arguments, exit status, the fault applied
  and the NAMES of the variables in its environment (never their values). A call
  the fake does not model, a flag it does not know and a response the model
  refuses each exit 99 and add a line to ``unknown.log``, which a test expects to
  stay empty.

What it models is what the AWS guide's end-to-end run measured (2026-10-21: its
sweeps and step logs) and the CLI's documented behaviour; it never answers from
the beta's own input. Fixtures
use account 111111111111.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import sys
from pathlib import Path

if __package__:  # imported: by the tests and scripts/check_installer_cli.py
    from . import shapes as _shapes
else:  # run as the fake `aws`: this folder's copy, never an installed package's
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import shapes as _shapes  # type: ignore[no-redef]

EXIT_OK = 0
EXIT_USAGE = 252
EXIT_CONFIG = 253
EXIT_SERVICE = 254
EXIT_UNMODELLED = 99

#: Services whose state is the account's, not a region's.
GLOBAL_SERVICES = {"iam", "route53", "cloudfront", "sts"}
#: s3api's bucket list is global; nothing else of s3api is modelled.
GLOBAL_OPERATIONS = {("s3api", "list-buckets")}

#: Options every operation takes (each with one value).
GLOBAL_OPTIONS = ("--region", "--query", "--output")

#: The code each protocol family's services answer an access denial with.
ACCESS_DENIED = {
    "ec2": "UnauthorizedOperation",
    "query": "AccessDenied",
    "rest-xml": "AccessDenied",
    "json": "AccessDeniedException",
    "rest-json": "AccessDeniedException",
    "smithy-rpc-v2-cbor": "AccessDeniedException",
}
#: Services whose denial code is their own.
ACCESS_DENIED_BY_SERVICE = {"sns": "AuthorizationError"}

#: Protocols whose timestamps arrive as epoch seconds, which the CLI renders in
#: the local time zone; the XML protocols send ISO 8601 in UTC.
EPOCH_PROTOCOLS = {"json", "rest-json", "smithy-rpc-v2-cbor"}


class ServiceError(Exception):
    """A modelled error answer: the CLI prints it and exits 254."""

    def __init__(self, code: str, message: str, status: int = EXIT_SERVICE):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


class Unmodelled(Exception):
    """A call the fake does not answer (exit 99, a line in unknown.log)."""


class Usage(Exception):
    """A call the CLI itself refuses before any request (exit 252)."""


# --- the operations -----------------------------------------------------------------

OPS: dict[tuple[str, str], tuple] = {}


def op(service: str, name: str, values=(), flags=(), required=()):
    """Register a handler. ``values`` take one argument, ``flags`` none;
    ``required`` must be given, as the CLI requires them."""

    def register(handler):
        OPS[(service, name)] = (handler, set(values), set(flags), set(required))
        return handler

    return register


def modelled_operations() -> list[tuple[str, str]]:
    return sorted(OPS)


class Call:
    """One invocation: its arguments, region and the parts of the state it reads."""

    def __init__(self, state: dict, service: str, name: str, args: dict, region: str):
        self.state, self.service, self.name, self.args = state, service, name, args
        self.region, self.account = region, state["account"]

    @property
    def here(self) -> dict:
        return self.state["regions"][self.region]

    @property
    def world(self) -> dict:
        return self.state["global"]

    def items(self, key: str) -> list:
        return list(self.here.get(key, []))

    def arg(self, name: str, default=None):
        return self.args.get(name, default)


# cloudformation -----------------------------------------------------------------------
@op("cloudformation", "list-stacks")
def _list_stacks(c: Call) -> dict:
    keep = ("StackId", "StackName", "TemplateDescription", "CreationTime")
    later = ("LastUpdatedTime", "DeletionTime", "StackStatus", "StackStatusReason")
    summaries = []
    for stack in c.items("stacks"):
        summary = {k: stack[k] for k in keep + later if k in stack}
        summary["DriftInformation"] = {"StackDriftStatus": "NOT_CHECKED"}
        summaries.append(summary)
    return {"StackSummaries": summaries}


# ec2 --------------------------------------------------------------------------------
for _name, _key, _collection in (
    ("describe-vpcs", "Vpcs", "vpcs"),
    ("describe-subnets", "Subnets", "subnets"),
    ("describe-route-tables", "RouteTables", "route_tables"),
    ("describe-internet-gateways", "InternetGateways", "internet_gateways"),
    ("describe-nat-gateways", "NatGateways", "nat_gateways"),
    ("describe-addresses", "Addresses", "addresses"),
    ("describe-network-interfaces", "NetworkInterfaces", "network_interfaces"),
    ("describe-security-groups", "SecurityGroups", "security_groups"),
    ("describe-vpc-endpoints", "VpcEndpoints", "vpc_endpoints"),
    ("describe-network-acls", "NetworkAcls", "network_acls"),
    ("describe-instances", "Reservations", "reservations"),
    ("describe-volumes", "Volumes", "volumes"),
    ("describe-flow-logs", "FlowLogs", "flow_logs"),
):

    def _ec2(c: Call, key=_key, collection=_collection) -> dict:
        return {key: c.items(collection)}

    op("ec2", _name)(_ec2)


# elbv2 ------------------------------------------------------------------------------
@op("elbv2", "describe-load-balancers")
def _load_balancers(c: Call) -> dict:
    return {"LoadBalancers": c.items("load_balancers")}


@op("elbv2", "describe-target-groups")
def _target_groups(c: Call) -> dict:
    return {"TargetGroups": c.items("target_groups")}


# ecs --------------------------------------------------------------------------------
@op("ecs", "list-clusters")
def _list_clusters(c: Call) -> dict:
    return {"clusterArns": [k["clusterArn"] for k in c.items("clusters")]}


@op("ecs", "list-services", values=("--cluster",))
def _list_services(c: Call) -> dict:
    wanted = c.arg("--cluster", "default")
    for cluster in c.items("clusters"):
        if wanted in (cluster["clusterArn"], cluster["clusterName"]):
            return {"serviceArns": list(cluster.get("serviceArns", []))}
    raise ServiceError("ClusterNotFoundException", "Cluster not found.")


def _revision_arn(c: Call, td: dict) -> str:
    return (
        f"arn:aws:ecs:{c.region}:{c.account}:task-definition/"
        f"{td['family']}:{td['revision']}"
    )


@op("ecs", "list-task-definition-families", values=("--status", "--family-prefix"))
def _families(c: Call) -> dict:
    status = c.arg("--status", "ACTIVE")
    if status not in ("ACTIVE", "INACTIVE", "ALL"):
        raise Usage(f"Invalid choice for --status: {status}")
    prefix = c.arg("--family-prefix", "")  # a real prefix here, as the run measured
    by_family: dict[str, set] = {}
    for td in c.items("task_definitions"):
        by_family.setdefault(td["family"], set()).add(td["status"])
    out = []
    for family, statuses in sorted(by_family.items()):
        if not family.startswith(prefix):
            continue
        active = "ACTIVE" in statuses
        if status == "ALL" or (status == "ACTIVE") == active:
            out.append(family)
    return {"families": out}


@op("ecs", "list-task-definitions", values=("--status", "--family-prefix"))
def _task_definitions(c: Call) -> dict:
    status = c.arg("--status", "ACTIVE")
    if status not in ("ACTIVE", "INACTIVE", "DELETE_IN_PROGRESS"):
        raise Usage(f"Invalid choice for --status: {status}")
    # Measured by the run: --family-prefix takes a FULL family name.
    # "experimentation-" matched nothing while three families existed.
    family = c.arg("--family-prefix")
    revisions = sorted(
        (td for td in c.items("task_definitions") if td["status"] == status),
        key=lambda td: (td["family"], td["revision"]),
    )
    return {
        "taskDefinitionArns": [
            _revision_arn(c, td)
            for td in revisions
            if family is None or td["family"] == family
        ]
    }


# ecr --------------------------------------------------------------------------------
@op("ecr", "describe-repositories")
def _repositories(c: Call) -> dict:
    return {"repositories": c.items("repositories")}


# rds --------------------------------------------------------------------------------
for _name, _key, _collection in (
    ("describe-db-clusters", "DBClusters", "db_clusters"),
    ("describe-db-instances", "DBInstances", "db_instances"),
    ("describe-db-cluster-snapshots", "DBClusterSnapshots", "db_cluster_snapshots"),
    ("describe-db-snapshots", "DBSnapshots", "db_snapshots"),
    (
        "describe-db-cluster-automated-backups",
        "DBClusterAutomatedBackups",
        "db_cluster_automated_backups",
    ),
    ("describe-db-subnet-groups", "DBSubnetGroups", "db_subnet_groups"),
    (
        "describe-db-cluster-parameter-groups",
        "DBClusterParameterGroups",
        "db_cluster_parameter_groups",
    ),
    ("describe-db-parameter-groups", "DBParameterGroups", "db_parameter_groups"),
):

    def _rds(c: Call, key=_key, collection=_collection) -> dict:
        return {key: c.items(collection)}

    op("rds", _name)(_rds)


# elasticache ------------------------------------------------------------------------
for _name, _key, _collection in (
    ("describe-replication-groups", "ReplicationGroups", "replication_groups"),
    ("describe-cache-clusters", "CacheClusters", "cache_clusters"),
    ("describe-snapshots", "Snapshots", "cache_snapshots"),
    ("describe-cache-subnet-groups", "CacheSubnetGroups", "cache_subnet_groups"),
):

    def _elasticache(c: Call, key=_key, collection=_collection) -> dict:
        return {key: c.items(collection)}

    op("elasticache", _name)(_elasticache)


# secretsmanager ---------------------------------------------------------------------
@op("secretsmanager", "list-secrets", flags=("--include-planned-deletion",))
def _secrets(c: Call) -> dict:
    planned = c.arg("--include-planned-deletion", False)
    return {
        "SecretList": [
            s for s in c.items("secrets") if planned or "DeletedDate" not in s
        ]
    }


# kms --------------------------------------------------------------------------------
@op("kms", "list-aliases")
def _aliases(c: Call) -> dict:
    return {"Aliases": c.items("kms_aliases"), "Truncated": False}


@op("kms", "list-keys")
def _keys(c: Call) -> dict:
    return {
        "Keys": [
            {"KeyId": k["KeyId"], "KeyArn": k["Arn"]} for k in c.items("kms_keys")
        ],
        "Truncated": False,
    }


@op("kms", "describe-key", values=("--key-id",), required=("--key-id",))
def _describe_key(c: Call) -> dict:
    wanted = c.arg("--key-id")
    for key in c.items("kms_keys"):
        if wanted in (key["KeyId"], key["Arn"]):
            return {"KeyMetadata": key}
    raise ServiceError(
        "NotFoundException",
        f"Key 'arn:aws:kms:{c.region}:{c.account}:key/{wanted}' does not exist",
    )


# logs, cloudwatch, sns, ssm -----------------------------------------------------------
@op("logs", "describe-log-groups")
def _log_groups(c: Call) -> dict:
    return {"logGroups": c.items("log_groups")}


@op("cloudwatch", "describe-alarms")
def _alarms(c: Call) -> dict:
    return {
        "MetricAlarms": c.items("metric_alarms"),
        "CompositeAlarms": c.items("composite_alarms"),
    }


@op("cloudwatch", "list-dashboards")
def _dashboards(c: Call) -> dict:
    return {"DashboardEntries": c.items("dashboards")}


@op("sns", "list-topics")
def _topics(c: Call) -> dict:
    return {"Topics": [{"TopicArn": t} for t in c.items("topics")]}


@op("sns", "list-subscriptions")
def _subscriptions(c: Call) -> dict:
    return {"Subscriptions": c.items("subscriptions")}


@op("ssm", "describe-parameters")
def _parameters(c: Call) -> dict:
    return {"Parameters": c.items("parameters")}


# dynamodb, cognito, lambda, codedeploy, application-autoscaling -----------------------
@op("dynamodb", "list-tables")
def _tables(c: Call) -> dict:
    return {"TableNames": sorted(c.items("tables"))}


@op("dynamodb", "list-backups", values=("--backup-type",))
def _backups(c: Call) -> dict:
    kind = c.arg("--backup-type", "USER")
    if kind not in ("USER", "SYSTEM", "AWS_BACKUP", "ALL"):
        raise Usage(f"Invalid choice for --backup-type: {kind}")
    return {
        "BackupSummaries": [
            b for b in c.items("backups") if kind == "ALL" or b["BackupType"] == kind
        ]
    }


@op(
    "cognito-idp",
    "list-user-pools",
    values=("--max-results",),
    required=("--max-results",),
)
def _user_pools(c: Call) -> dict:
    try:
        limit = int(c.arg("--max-results"))
    except ValueError as error:
        raise Usage(f"invalid literal for --max-results: {error}") from error
    if not 1 <= limit <= 60:
        raise ServiceError(
            "InvalidParameterException",
            "1 validation error detected: Value at 'maxResults' failed to satisfy"
            " constraint: Member must have value less than or equal to 60",
        )
    # An account with more pools than the page holds: the service answers with a
    # token for the next page (``user_pools_next_token`` in the state).
    response = {"UserPools": c.items("user_pools")}
    if c.here.get("user_pools_next_token"):
        response["NextToken"] = c.here["user_pools_next_token"]
    return response


@op("lambda", "list-functions")
def _functions(c: Call) -> dict:
    return {"Functions": c.items("functions")}


@op("deploy", "list-applications")
def _applications(c: Call) -> dict:
    return {"applications": sorted(c.items("codedeploy_applications"))}


@op(
    "application-autoscaling",
    "describe-scalable-targets",
    values=("--service-namespace",),
    required=("--service-namespace",),
)
def _scalable_targets(c: Call) -> dict:
    namespace = c.arg("--service-namespace")
    return {
        "ScalableTargets": [
            t for t in c.items("scalable_targets") if t["ServiceNamespace"] == namespace
        ]
    }


# acm, events, backup, guardduty, config, tagging ---------------------------------------
@op("acm", "list-certificates", values=("--includes",))
def _certificates(c: Call) -> dict:
    # Without --includes ACM lists only RSA_2048 certificates (ListCertificates).
    types = {"RSA_2048"}
    includes = c.arg("--includes")
    if includes is not None:
        name, _, listed = includes.partition("=")
        if name != "keyTypes" or not listed:
            raise Unmodelled(f"--includes {includes}")
        types = set(listed.split(","))
    return {
        "CertificateSummaryList": [
            s for s in c.items("certificates") if s["KeyAlgorithm"] in types
        ]
    }


@op("events", "list-rules")
def _rules(c: Call) -> dict:
    return {"Rules": c.items("rules")}


@op("backup", "list-backup-vaults")
def _vaults(c: Call) -> dict:
    return {"BackupVaultList": c.items("backup_vaults")}


@op(
    "backup",
    "list-recovery-points-by-backup-vault",
    values=("--backup-vault-name",),
    required=("--backup-vault-name",),
)
def _recovery_points(c: Call) -> dict:
    name = c.arg("--backup-vault-name")
    if name not in {v["BackupVaultName"] for v in c.items("backup_vaults")}:
        raise ServiceError(
            "ResourceNotFoundException", f"Backup vault {name} does not exist."
        )
    return {"RecoveryPoints": list(c.here.get("recovery_points", {}).get(name, []))}


@op("guardduty", "list-detectors")
def _detectors(c: Call) -> dict:
    return {"DetectorIds": c.items("detectors")}


@op("configservice", "describe-configuration-recorders")
def _recorders(c: Call) -> dict:
    return {"ConfigurationRecorders": c.items("config_recorders")}


@op("resourcegroupstaggingapi", "get-resources")
def _tagged(c: Call) -> dict:
    # The tagging index is its own list: it trails the services, and still listed
    # deleted resources after the run's teardown.
    return {
        "ResourceTagMappingList": [
            {
                "ResourceARN": arn,
                "Tags": [{"Key": "Name", "Value": arn.rsplit("/", 1)[-1]}],
            }
            for arn in c.items("tag_index")
        ]
    }


# iam ----------------------------------------------------------------------------------
@op("iam", "list-roles")
def _roles(c: Call) -> dict:
    return {"Roles": list(c.world.get("roles", [])), "IsTruncated": False}


@op("iam", "list-policies", values=("--scope",))
def _policies(c: Call) -> dict:
    if c.arg("--scope") != "Local":
        raise Unmodelled("list-policies without --scope Local")
    return {"Policies": list(c.world.get("policies", [])), "IsTruncated": False}


@op("iam", "list-instance-profiles")
def _instance_profiles(c: Call) -> dict:
    return {
        "InstanceProfiles": list(c.world.get("instance_profiles", [])),
        "IsTruncated": False,
    }


@op("iam", "list-users")
def _users(c: Call) -> dict:
    return {"Users": list(c.world.get("users", [])), "IsTruncated": False}


@op("iam", "list-open-id-connect-providers")
def _oidc(c: Call) -> dict:
    return {
        "OpenIDConnectProviderList": [
            {"Arn": arn} for arn in c.world.get("oidc_providers", [])
        ]
    }


@op("iam", "list-saml-providers")
def _saml(c: Call) -> dict:
    return {"SAMLProviderList": list(c.world.get("saml_providers", []))}


# route53, s3api, cloudfront, sts --------------------------------------------------------
@op("route53", "list-hosted-zones")
def _zones(c: Call) -> dict:
    return {
        "HostedZones": list(c.world.get("hosted_zones", [])),
        "Marker": "",
        "IsTruncated": False,
        "MaxItems": "100",
    }


@op("route53", "list-health-checks")
def _health_checks(c: Call) -> dict:
    return {
        "HealthChecks": list(c.world.get("health_checks", [])),
        "Marker": "",
        "IsTruncated": False,
        "MaxItems": "100",
    }


@op("s3api", "list-buckets")
def _buckets(c: Call) -> dict:
    return {
        "Buckets": list(c.world.get("buckets", [])),
        "Owner": {"ID": c.world["canonical_user_id"]},
    }


@op("cloudfront", "list-distributions")
def _distributions(c: Call) -> dict:
    items = list(c.world.get("distributions", []))
    listing = {
        "Marker": "",
        "MaxItems": 100,
        "IsTruncated": False,
        "Quantity": len(items),
    }
    if items:  # CloudFront leaves Items out of an empty list
        listing["Items"] = items
    return {"DistributionList": listing}


@op("sts", "get-caller-identity")
def _identity(c: Call) -> dict:
    who = c.world["identity"]
    return {"UserId": who["UserId"], "Account": c.account, "Arn": who["Arn"]}


# --- the CLI's output -------------------------------------------------------------------


def full_result(response: dict, paginator: dict | None) -> dict:
    """What the CLI formats for a paginated operation: the paginator's result
    keys and non-aggregate keys only (botocore ``PageIterator.build_full_result``;
    one page here). A result key whose value is absent is left out."""
    import jmespath

    if paginator is None:
        return response
    keys = paginator.get("result_key")
    keys = keys if isinstance(keys, list) else [keys]
    out: dict = {}
    for expression in keys + list(paginator.get("non_aggregate_keys", [])):
        value = jmespath.search(expression, response)
        if value is not None:
            _set_path(out, expression, value)
    return out


def text_page(response: dict, paginator: dict | None) -> dict:
    """What the CLI's text output formats for one page of a paginated operation:
    the non-aggregate keys, then every result key, set even when absent (awscli
    ``TextFormatter``), so an absent list prints as ``None``."""
    import jmespath

    if paginator is None:
        return response
    out: dict = {}
    for expression in list(paginator.get("non_aggregate_keys", [])):
        value = jmespath.search(expression, response)
        if value is not None:
            _set_path(out, expression, value)
    keys = paginator.get("result_key")
    for expression in keys if isinstance(keys, list) else [keys]:
        _set_path(out, expression, jmespath.search(expression, response))
    return out


def _set_path(target: dict, expression: str, value) -> None:
    *parents, last = expression.split(".")
    for part in parents:
        target = target.setdefault(part, {})
    target[last] = value


def render_json(value) -> str:
    """awscli's JSON formatter: an empty object prints nothing."""
    if value == {}:
        return ""
    return json.dumps(value, indent=4, ensure_ascii=False) + "\n"


def render_text(value) -> str:
    """awscli's text formatter (awscli/text.py), line for line."""
    out: list[str] = []
    _text(value, out)
    return "".join(out)


def _text(item, out, identifier=None, scalar_keys=None) -> None:
    if isinstance(item, dict):
        _text_dict(scalar_keys, item, identifier, out)
    elif isinstance(item, list):
        _text_list(item, identifier, out)
    else:
        out.append(f"{item}\n")


def _text_list(item, identifier, out) -> None:
    if not item:
        return
    if any(isinstance(el, dict) for el in item):
        keys = sorted(
            {
                k
                for el in item
                if isinstance(el, dict)
                for k, v in el.items()
                if not isinstance(v, (dict, list))
            }
        )
        for element in item:
            _text(element, out, identifier=identifier, scalar_keys=keys)
    elif any(isinstance(el, list) for el in item):
        scalars = [el for el in item if not isinstance(el, (list, dict))]
        others = [el for el in item if isinstance(el, (list, dict))]
        if scalars:
            _text_scalars(scalars, identifier, out)
        for element in others:
            _text(element, out, identifier=identifier)
    else:
        _text_scalars(item, identifier, out)


def _text_scalars(elements, identifier, out) -> None:
    if identifier is not None:
        for element in elements:
            out.append(f"{identifier.upper()}\t{element}\n")
    else:
        out.append("\t".join(str(el) for el in elements) + "\n")


def _text_dict(scalar_keys, item, identifier, out) -> None:
    scalars, others = [], []
    if scalar_keys is None:
        for key, value in sorted(item.items()):
            if isinstance(value, (dict, list)):
                others.append((key, value))
            else:
                scalars.append(str(value))
    else:
        for key in scalar_keys:
            scalars.append(str(item.get(key, "")))
        for key in sorted(set(item) - set(scalar_keys)):
            others.append((key, item[key]))
    if scalars:
        if identifier is not None:
            scalars.insert(0, identifier.upper())
        out.append("\t".join(scalars) + "\n")
    for new_identifier, value in others:
        _text(value, out, identifier=new_identifier)


def render_timestamps(value, ref: dict, shapes: dict, epoch: bool):
    """Each timestamp as the CLI prints it (cli_timestamp_format iso8601): from an
    epoch (JSON protocols) in the local time zone, from ISO 8601 (XML) in UTC."""
    shape = shapes[ref["shape"]]
    kind = shape["type"]
    if kind == "structure" and isinstance(value, dict):
        members = shape.get("members", {})
        return {
            k: render_timestamps(v, members[k], shapes, epoch) if k in members else v
            for k, v in value.items()
        }
    if kind == "list" and isinstance(value, list):
        return [render_timestamps(v, shape["member"], shapes, epoch) for v in value]
    if kind == "map" and isinstance(value, dict):
        return {
            k: render_timestamps(v, shape["value"], shapes, epoch)
            for k, v in value.items()
        }
    if kind == "timestamp" and isinstance(value, str):
        moment = parse_timestamp(value)
        if epoch:
            return (
                datetime.datetime.fromtimestamp(moment.timestamp())
                .astimezone()
                .isoformat()
            )
        return moment.isoformat()
    return value


def parse_timestamp(text: str) -> datetime.datetime:
    """The state's canonical timestamps: ISO 8601 in UTC, ending in Z."""
    if not text.endswith("Z"):
        raise ValueError(f"timestamp {text!r} must be UTC and end in Z")
    return datetime.datetime.fromisoformat(text[:-1]).replace(
        tzinfo=datetime.timezone.utc
    )


# --- one call ---------------------------------------------------------------------------


def parse_argv(argv: list[str]):
    """``(service, operation, options)`` for ``aws <service> <operation> [options]``.

    Options are ``--name value`` or a bare ``--flag``; which is which comes from the
    operation's registration, so this only splits.
    """
    if len(argv) < 2 or argv[0].startswith("-") or argv[1].startswith("-"):
        raise Unmodelled("not aws <service> <operation> ...")
    return argv[0], argv[1], argv[2:]


def options(rest: list[str], values: set, flags: set) -> dict:
    out: dict = {}
    i = 0
    while i < len(rest):
        name = rest[i]
        if name in flags:
            out[name] = True
            i += 1
        elif name in values or name in GLOBAL_OPTIONS:
            if i + 1 >= len(rest):
                raise Usage(f"argument {name}: expected one argument")
            if name in out:
                raise Unmodelled(f"{name} given twice")
            out[name] = rest[i + 1]
            i += 2
        else:
            raise Unmodelled(f"option {name}")
    return out


def resolve_region(args: dict, env: dict) -> str | None:
    for value in (
        args.get("--region"),
        env.get("AWS_REGION"),
        env.get("AWS_DEFAULT_REGION"),
    ):
        if value:
            return value
    return _profile_region(env)


def _profile_region(env: dict) -> str | None:
    """The ``region`` of the profile in ``AWS_CONFIG_FILE`` (``default`` without a
    profile), as the CLI reads it."""
    import configparser

    path = env.get("AWS_CONFIG_FILE")
    if not path or not os.path.isfile(path):
        return None
    parser = configparser.RawConfigParser()
    try:
        parser.read(path)
    except configparser.Error:
        return None
    profile = env.get("AWS_PROFILE") or "default"
    section = "default" if profile == "default" else f"profile {profile}"
    if parser.has_section(section) and parser.has_option(section, "region"):
        return parser.get(section, "region").strip() or None
    return None


def _fault(state: dict, service: str, name: str, region: str | None, number: int):
    hits = state.setdefault("fault_hits", {})
    for index, fault in enumerate(state.get("faults", [])):
        if "call" in fault and fault["call"] != number:
            continue
        if any(
            key in fault and fault[key] != value
            for key, value in (("service", service), ("op", name), ("region", region))
        ):
            continue
        hits[str(index)] = hits.get(str(index), 0) + 1
        if "nth" in fault and fault["nth"] != hits[str(index)]:
            continue
        return fault
    return None


def _denied(spec: dict, operation: dict, service: str, state: dict) -> ServiceError:
    protocol = spec["metadata"].get("protocol", "json")
    code = ACCESS_DENIED_BY_SERVICE.get(
        service, ACCESS_DENIED.get(protocol, "AccessDenied")
    )
    action = f"{spec['metadata'].get('signingName', service)}:{operation['name']}"
    who = state["global"]["identity"]["Arn"]
    return ServiceError(
        code,
        f"User: {who} is not authorized to perform: {action} because no "
        "identity-based policy allows the action",
    )


def raw_response(state: dict, argv: list[str], env: dict) -> dict:
    """The response the service would send for ``argv`` against ``state``: the
    handler's own answer, before the CLI's reduction, query or formatting. What
    scripts/check_installer_cli.py serves the real CLI; no fault applies."""
    service, name, rest = parse_argv(argv)
    handler, values, flags, _ = OPS[(service, name)]
    args = options(rest, values, flags)
    return handler(Call(state, service, name, args, resolve_region(args, env)))


class Answer:
    """What one call prints and exits with, and what the log records of it."""

    def __init__(self) -> None:
        self.out = self.err = ""
        self.status = EXIT_OK
        self.region: str | None = None
        self.fault: str | None = None
        self.operation = "unknown"  # the model's spelling, for error messages


def respond(
    state: dict, shapes: dict, argv: list[str], env: dict, answer: Answer
) -> None:
    """Fill ``answer`` for one call; ``state`` is updated. Raises ServiceError,
    Usage or Unmodelled for the answers that are not a success."""
    service, name, rest = parse_argv(argv)
    if (service, name) not in OPS:
        raise Unmodelled("operation not modelled")
    handler, values, flags, required = OPS[(service, name)]
    spec = shapes.get(service)
    if spec is None or name not in spec["operations"]:
        raise Unmodelled("no output shape for this operation")
    operation = spec["operations"][name]
    answer.operation = operation["name"]
    args = options(rest, values, flags)
    for flag in sorted(required):
        if flag not in args:
            raise Usage(f"the following arguments are required: {flag}")
    output = args.get("--output", "json")
    if output not in ("json", "text"):
        raise Unmodelled(f"--output {output}")
    answer.region = resolve_region(args, env)
    if answer.region is None:
        answer.err = (
            "You must specify a region. You can also configure your region by "
            'running "aws configure".\n'
        )
        answer.status = EXIT_CONFIG
        return
    is_global = service in GLOBAL_SERVICES or (service, name) in GLOBAL_OPERATIONS
    if not is_global and answer.region not in state["regions"]:
        raise Unmodelled(f"region {answer.region} is not in the state")
    compiled = None
    if "--query" in args:
        import jmespath

        try:
            compiled = jmespath.compile(args["--query"])
        except jmespath.exceptions.JMESPathError as error:
            raise Usage(f"Bad value for --query {args['--query']}: {error}") from error

    fault = _fault(state, service, name, answer.region, state["calls"])
    answer.fault = fault["mode"] if fault else None
    if answer.fault == "AccessDenied":
        raise _denied(spec, operation, service, state)
    if answer.fault == "error":
        if "stderr" in fault:  # a failure the CLI reports in its own words
            answer.err, answer.status = fault["stderr"], fault.get("exit", 255)
            return
        raise ServiceError(
            fault["code"],
            fault.get("message", fault["code"]),
            fault.get("exit", EXIT_SERVICE),
        )
    if answer.fault == "EMPTY":
        return
    if answer.fault not in (None, "NOQUERY"):
        raise Unmodelled(f"fault mode {answer.fault}")

    response = handler(Call(state, service, name, args, answer.region))
    ref = operation["output"]
    if ref is None:
        raise Unmodelled("an operation with no output shape")
    errors = _shapes.validate(response, ref, spec["shapes"])
    if errors:
        raise Unmodelled("the model refuses the response: " + "; ".join(errors[:5]))
    response = _shapes.member_order(response, ref, spec["shapes"])
    epoch = spec["metadata"].get("protocol") in EPOCH_PROTOCOLS
    response = render_timestamps(response, ref, spec["shapes"], epoch)
    paginator = operation["paginator"]
    if paginator is not None and set(paginator["manual_flags"]) & set(args):
        # A paging option given by hand turns the CLI's paging off: one request,
        # the page printed as the service sent it, next-page token included.
        paginator = None
    query = compiled if answer.fault != "NOQUERY" else None
    if output == "text":
        page = text_page(response, paginator)
        answer.out = render_text(page if query is None else query.search(page))
    else:
        full = full_result(response, paginator)
        answer.out = render_json(full if query is None else query.search(full))


#: The two forms CLIs print a service's error in (measured: aws-cli 2.38.1 on the
#: CI runner prints the prefix, 2.28.24 does not).
ERROR_STYLES = {"prefixed": "aws: [ERROR]: ", "plain": ""}


def service_error_text(
    code: str, operation: str, message: str, style: str | None
) -> str:
    """What the CLI writes to stderr for a service's error, in ``style``
    (``prefixed`` when unset)."""
    prefix = ERROR_STYLES[style or "prefixed"]
    return (
        f"\n{prefix}An error occurred ({code}) when calling the "
        f"{operation} operation: {message}\n"
    )


@contextlib.contextmanager
def _locked(state_path: Path):
    with open(f"{state_path}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main(state_path: Path, shapes_path: Path, argv: list[str], env: dict) -> int:
    """One call. The shim on PATH passes the state and shapes files as the first
    two arguments, so the call's environment is exactly the one its caller gave."""
    calls = state_path.parent / "calls.log"
    unknown = state_path.parent / "unknown.log"
    answer = Answer()
    with _locked(state_path):
        state = json.loads(state_path.read_text())
        state["calls"] = state.get("calls", 0) + 1
        try:
            respond(state, json.loads(shapes_path.read_text()), argv, env, answer)
        except ServiceError as error:
            answer.err = service_error_text(
                error.code, answer.operation, error.message, state.get("error_style")
            )
            answer.status = error.status
            answer.fault = answer.fault or "error"
        except Usage as error:
            answer.err = f"\naws: error: {error}\n"
            answer.status = EXIT_USAGE
        except Unmodelled as error:
            answer.err = f"fake aws: {error}: aws {' '.join(argv)}\n"
            answer.status = EXIT_UNMODELLED
            with open(unknown, "a") as handle:
                handle.write(f"{error}: aws {json.dumps(argv)}\n")
        record = {
            "n": state["calls"],
            "service": argv[0] if argv else None,
            "op": argv[1] if len(argv) > 1 else None,
            "region": answer.region,
            "argv": argv,
            "exit": answer.status,
            "fault": answer.fault,
            "env": sorted(env),
        }
        with open(calls, "a") as handle:
            handle.write(json.dumps(record) + "\n")
        state_path.write_text(json.dumps(state, indent=1, sort_keys=True))
    sys.stdout.write(answer.out)
    sys.stderr.write(answer.err)
    return answer.status


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:], dict(os.environ)))
