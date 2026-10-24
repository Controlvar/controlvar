"""The fake ``aws``'s operations for what uninstall reads and deletes (fake_ops_install.py
has install's, fake_aws.py the sweep's).

``register(fake)`` adds them to the fake's table, as fake_ops_install.py does. Each delete
changes the account's state the way the service does, as far as uninstall depends on it,
and refuses what the service refuses: a repository that still holds images without
``--force``, a certificate a load balancer still uses, a log group or secret that is not
there. The AWS guide's end-to-end run deleted each of these once (its phase 9 logs); the
rest is the services' documented behaviour.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

if __package__:
    from . import made as _made
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import made as _made  # type: ignore[no-redef]

#: The CodeDeploy statuses a deployment can be in.
DEPLOYMENT_STATUSES = (
    "Created",
    "Queued",
    "InProgress",
    "Baking",
    "Succeeded",
    "Failed",
    "Stopped",
    "Ready",
)

#: What the operations registered here are, as (service, operation).
REGISTERED: set[tuple[str, str]] = set()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _later(days: int) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _live(c, name: str):
    for stack in c.here.get("stacks", []):
        if stack["StackName"] == name and stack["StackStatus"] != "DELETE_COMPLETE":
            return stack
    return None


def register(fake) -> None:
    ServiceError, Usage = fake.ServiceError, fake.Usage

    def op(service, name, **kw):
        REGISTERED.add((service, name))
        return fake.op(service, name, **kw)

    # --- CodeDeploy -------------------------------------------------------------------
    @op(
        "deploy",
        "list-deployments",
        values=("--application-name", "--deployment-group-name"),
        lists=("--include-only-statuses",),
    )
    def _deployments(c) -> dict:
        application = c.arg("--application-name")
        if application is not None and application not in c.items(
            "codedeploy_applications"
        ):
            raise ServiceError(
                "ApplicationDoesNotExistException",
                f"No application found for name: {application}",
            )
        wanted = c.arg("--include-only-statuses") or list(DEPLOYMENT_STATUSES)
        for status in wanted:
            if status not in DEPLOYMENT_STATUSES:
                raise Usage(f"Invalid choice for --include-only-statuses: {status}")
        return {
            "deployments": [
                d["id"] for d in c.items("deployments") if d["status"] in wanted
            ]
        }

    # --- Secrets Manager --------------------------------------------------------------
    @op(
        "secretsmanager",
        "delete-secret",
        values=("--secret-id", "--recovery-window-in-days"),
        flags=("--force-delete-without-recovery",),
        required=("--secret-id",),
    )
    def _delete_secret(c) -> dict:
        wanted = c.arg("--secret-id")
        secrets = c.here.get("secrets", [])
        for secret in secrets:
            if wanted in (secret["Name"], secret["ARN"]):
                if c.arg("--force-delete-without-recovery", False):
                    secrets.remove(secret)
                    c.here.get("secret_values", {}).pop(secret["ARN"], None)
                    deleted = _now()
                else:
                    deleted = secret.setdefault("DeletedDate", _later(30))
                return {
                    "ARN": secret["ARN"],
                    "Name": secret["Name"],
                    "DeletionDate": deleted,
                }
        raise ServiceError(
            "ResourceNotFoundException",
            "Secrets Manager can't find the specified secret.",
        )

    # --- ECR ----------------------------------------------------------------------------
    def _repository(c, name: str):
        for repo in c.here.get("repositories", []):
            if repo["repositoryName"] == name:
                return repo
        return None

    def _not_found(c, name: str) -> Exception:
        return ServiceError(
            "RepositoryNotFoundException",
            f"The repository with name '{name}' does not exist in the registry with id '{c.account}'",
        )

    @op("ecr", "describe-repositories", lists=("--repository-names",))
    def _describe_repositories(c) -> dict:
        names = c.arg("--repository-names")
        found = c.items("repositories")
        if names is not None:
            for name in names:
                if _repository(c, name) is None:
                    raise _not_found(c, name)
            found = [r for r in found if r["repositoryName"] in names]
        return {"repositories": found}

    @op(
        "ecr",
        "list-tags-for-resource",
        values=("--resource-arn",),
        required=("--resource-arn",),
    )
    def _repository_tags(c) -> dict:
        arn = c.arg("--resource-arn")
        for repo in c.here.get("repositories", []):
            if repo["repositoryArn"] == arn:
                return {
                    "tags": list(
                        c.here.get("repository_tags", {}).get(
                            repo["repositoryName"], []
                        )
                    )
                }
        raise _not_found(c, arn.rsplit("/", 1)[-1])

    @op(
        "ecr",
        "delete-repository",
        values=("--repository-name",),
        flags=("--force",),
        required=("--repository-name",),
    )
    def _delete_repository(c) -> dict:
        name = c.arg("--repository-name")
        repo = _repository(c, name)
        if repo is None:
            raise _not_found(c, name)
        if c.here.get("images", {}).get(name) and not c.arg("--force", False):
            raise ServiceError(
                "RepositoryNotEmptyException",
                f"The repository with name '{name}' in registry with id '{c.account}' cannot be deleted because it still contains images",
            )
        c.here["repositories"].remove(repo)
        c.here.get("images", {}).pop(name, None)
        c.here.get("repository_tags", {}).pop(name, None)
        return {"repository": repo}

    # --- ACM ----------------------------------------------------------------------------
    def _certificate(c, arn: str) -> dict:
        for cert in c.here.get("certificates", []):
            if cert["CertificateArn"] == arn:
                return cert
        raise ServiceError(
            "ResourceNotFoundException", f"Could not find certificate {arn}."
        )

    @op(
        "acm",
        "list-tags-for-certificate",
        values=("--certificate-arn",),
        required=("--certificate-arn",),
    )
    def _certificate_tags(c) -> dict:
        return {
            "Tags": list(_certificate(c, c.arg("--certificate-arn")).get("Tags", []))
        }

    @op(
        "acm",
        "delete-certificate",
        values=("--certificate-arn",),
        required=("--certificate-arn",),
    )
    def _delete_certificate(c) -> dict:
        cert = _certificate(c, c.arg("--certificate-arn"))
        # The load balancer's listener uses it until the fargate stack is gone.
        if _live(c, "experimentation-fargate-staging") is not None:
            raise ServiceError(
                "ResourceInUseException",
                f"Certificate {cert['CertificateArn']} in account {c.account} is in use.",
            )
        c.here["certificates"].remove(cert)
        return {}

    # --- CloudWatch Logs ------------------------------------------------------------------
    @op(
        "logs",
        "delete-log-group",
        values=("--log-group-name",),
        required=("--log-group-name",),
    )
    def _delete_log_group(c) -> dict:
        name = c.arg("--log-group-name")
        groups = c.here.get("log_groups", [])
        for group in groups:
            if group["logGroupName"] == name:
                groups.remove(group)
                return {}
        raise ServiceError(
            "ResourceNotFoundException", "The specified log group does not exist."
        )

    # --- ECS task definitions -------------------------------------------------------------
    def _revision(c, arn: str) -> dict:
        tail = arn.rsplit("/", 1)[-1]
        family, _, revision = tail.partition(":")
        for td in c.here.get("task_definitions", []):
            if td["family"] == family and str(td["revision"]) == revision:
                return td
        raise ServiceError(
            "ClientException", "The specified task definition does not exist."
        )

    def _shown(c, td: dict) -> dict:
        body = {
            k: v for k, v in td.items() if k not in ("family", "revision", "status")
        }
        return {
            "taskDefinitionArn": f"arn:aws:ecs:{c.region}:{c.account}:task-definition/{td['family']}:{td['revision']}",
            "family": td["family"],
            "revision": td["revision"],
            "status": td["status"],
            **body,
        }

    @op(
        "ecs",
        "deregister-task-definition",
        values=("--task-definition",),
        required=("--task-definition",),
    )
    def _deregister(c) -> dict:
        td = _revision(c, c.arg("--task-definition"))
        if td["status"] == "DELETE_IN_PROGRESS":
            raise ServiceError(
                "ClientException", "The specified task definition is being deleted."
            )
        td["status"] = "INACTIVE"
        return {"taskDefinition": _shown(c, td)}

    @op(
        "ecs",
        "delete-task-definitions",
        lists=("--task-definitions",),
        required=("--task-definitions",),
    )
    def _delete_tds(c) -> dict:
        done, failures = [], []
        for arn in c.arg("--task-definitions"):
            try:
                td = _revision(c, arn)
            except ServiceError:
                failures.append({"arn": arn, "reason": "TASK_DEFINITION_NOT_FOUND"})
                continue
            if td["status"] == "ACTIVE":
                failures.append(
                    {
                        "arn": arn,
                        "reason": "The specified task definition must be INACTIVE before deletion.",
                    }
                )
                continue
            # Deletion is asynchronous: the revision is DELETE_IN_PROGRESS until ECS is done.
            td["status"] = "DELETE_IN_PROGRESS"
            done.append(_shown(c, td))
        return {"taskDefinitions": done, "failures": failures}

    # --- CloudFormation: the CDK's setup stack ------------------------------------------------
    @op(
        "cloudformation",
        "delete-stack",
        values=("--stack-name",),
        required=("--stack-name",),
    )
    def _delete_stack(c) -> dict:
        name = c.arg("--stack-name")
        stack = _live(c, name)
        if stack is None:
            return {}  # deleting a stack that is not there succeeds
        failed = c.state.get("cdk", {}).get("delete_stack_fail", {}).get(name)
        if failed:
            stack["StackStatus"] = "DELETE_FAILED"
            stack["events"] = [
                {
                    "StackId": stack["StackId"],
                    "EventId": "t1",
                    "StackName": name,
                    "LogicalResourceId": failed.get("logical", "StagingBucket"),
                    "PhysicalResourceId": failed.get("physical", "resource-1"),
                    "ResourceType": failed.get("type", "AWS::S3::Bucket"),
                    "ResourceStatus": "DELETE_FAILED",
                    "ResourceStatusReason": failed["reason"],
                    "Timestamp": "2026-10-21T06:01:30Z",
                }
            ]
            return {}
        _made.remove_made(c.state, name)
        c.here["tag_index"] = [
            a for a in c.here.get("tag_index", []) if a != stack["StackId"]
        ]
        stack["StackStatus"] = "DELETE_COMPLETE"
        stack["DeletionTime"] = _now()
        stack.pop("Outputs", None)
        return {}

    # --- S3: the CDK's bucket -------------------------------------------------------------
    def _bucket(c, name: str) -> dict:
        for bucket in c.world.get("buckets", []):
            if bucket["Name"] == name:
                return bucket
        raise ServiceError("NoSuchBucket", "The specified bucket does not exist")

    def _objects(c, name: str) -> list:
        return c.world.setdefault("bucket_objects", {}).setdefault(name, [])

    @op("s3api", "list-object-versions", values=("--bucket",), required=("--bucket",))
    def _versions(c) -> dict:
        name = c.arg("--bucket")
        _bucket(c, name)
        response = {
            "IsTruncated": False,
            "KeyMarker": "",
            "VersionIdMarker": "",
            "Name": name,
            "Prefix": "",
            "MaxKeys": 1000,
        }
        versions = list(_objects(c, name))
        if versions:  # S3 leaves an empty list out
            response["Versions"] = versions
        return response

    @op(
        "s3api",
        "delete-objects",
        values=("--bucket", "--delete"),
        required=("--bucket", "--delete"),
    )
    def _delete_objects(c) -> dict:
        name = c.arg("--bucket")
        _bucket(c, name)
        try:
            batch = json.loads(_value(c.arg("--delete")))
        except (OSError, ValueError) as error:
            raise Usage(f"Error parsing parameter '--delete': {error}") from error
        wanted = [(o["Key"], o.get("VersionId")) for o in batch["Objects"]]
        if not wanted or len(wanted) > 1000:
            raise ServiceError(
                "MalformedXML", "The XML you provided was not well-formed"
            )
        objects = _objects(c, name)
        deleted = []
        for key, version in wanted:
            for item in list(objects):
                if item["Key"] == key and item["VersionId"] == version:
                    objects.remove(item)
                    deleted.append({"Key": key, "VersionId": version})
        return {} if batch.get("Quiet") else {"Deleted": deleted}

    @op("s3api", "head-bucket", values=("--bucket",), required=("--bucket",))
    def _head_bucket(c) -> dict:
        try:
            _bucket(c, c.arg("--bucket"))
        except ServiceError as error:
            raise ServiceError("404", "Not Found") from error
        return {"BucketRegion": "us-west-2", "AccessPointAlias": False}

    @op("s3api", "delete-bucket", values=("--bucket",), required=("--bucket",))
    def _delete_bucket(c) -> dict:
        name = c.arg("--bucket")
        bucket = _bucket(c, name)
        if _objects(c, name):
            raise ServiceError(
                "BucketNotEmpty", "The bucket you tried to delete is not empty"
            )
        c.world["buckets"].remove(bucket)
        c.world.get("bucket_objects", {}).pop(name, None)
        return {}


def _value(arg: str) -> str:
    """An option's value as the CLI reads it: ``file://<path>`` is the file's text."""
    if arg.startswith("file://"):
        with open(arg[len("file://") :], encoding="utf-8") as handle:
            return handle.read()
    return arg
