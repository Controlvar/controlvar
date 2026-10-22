"""The fake ``aws``'s operations for what install reads and writes (fake_aws.py has the
sweep's, the shapes, the faults and the logs).

``register(fake)`` adds them to the fake's table; it is given the fake module rather
than importing it, so the fake run as a script and the fake imported by the tests
register into their own table. Each write changes the account's state the way the
service does, as far as install depends on it, and refuses what the service refuses:
a second repository, secret or record of a name, a DELETE that does not match. What
the AWS guide's end-to-end run measured is modelled from its logs; the rest is the
services' documented behaviour.
"""

from __future__ import annotations

import datetime
import hashlib
import json

#: The run's migration task log (7.3), its database host masked, with the
#: administrator's address and the schema's state filled in by the bootstrap model.
MIGRATION_LOG = (
    "[entrypoint] waiting for PostgreSQL at <aurora host>:5432 (timeout 120s)",
    "[entrypoint] PostgreSQL is ready",
    "[entrypoint] RUN_MIGRATIONS=false: skipping bootstrap",
    "[entrypoint] starting: python -m backend.app.db.bootstrap",
    "INFO {fresh}",
    "INFO Running the core profile",
    "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.",
    "INFO  [alembic.runtime.migration] Will assume transactional DDL.",
    "INFO  [alembic.runtime.migration] Running stamp_revision  -> 4c2c09a2b3e8",
    "INFO  [__main__] created the first administrator {admin} (role ADMIN)",
    "Database bootstrap complete ({state}) — schema 'experimentation' at <aurora host>:5432/experimentation",
)


def _id(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:length]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _value(arg: str) -> str:
    """An option's value as the CLI reads it: ``file://<path>`` is the file's text."""
    if arg.startswith("file://"):
        with open(arg[len("file://") :], encoding="utf-8") as handle:
            return handle.read()
    return arg


def _shorthand(text: str) -> dict:
    """``Key=a,Value=b`` as the CLI's shorthand reads it (no nesting)."""
    out = {}
    for part in text.split(","):
        key, _, value = part.partition("=")
        out[key] = value
    return out


def _tags(raw) -> list[dict]:
    """--tags as JSON or as one or more ``Key=a,Value=b``."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    tags = []
    for item in raw:
        item = _value(item)
        if item.lstrip().startswith("["):
            tags.extend(json.loads(item))
        else:
            tags.append(_shorthand(item))
    return [{"Key": t["Key"], "Value": t["Value"]} for t in tags]


def register(fake) -> None:
    op, ServiceError, Usage = fake.op, fake.ServiceError, fake.Usage

    # --- account and regions ----------------------------------------------------------
    @op("ec2", "describe-regions")
    def _regions(c) -> dict:
        names = c.state.get("enabled_regions") or list(c.state["regions"])
        return {
            "Regions": [
                {
                    "Endpoint": f"ec2.{r}.amazonaws.com",
                    "RegionName": r,
                    "OptInStatus": "opt-in-not-required",
                }
                for r in names
            ]
        }

    @op(
        "service-quotas",
        "get-service-quota",
        values=("--service-code", "--quota-code"),
        required=("--service-code", "--quota-code"),
    )
    def _quota(c) -> dict:
        quota = c.here.get("quotas", {}).get(c.arg("--quota-code"))
        if quota is None or not quota.get("applied", True):
            raise ServiceError(
                "NoSuchResourceException",
                f"The request failed because the specified service quota {c.arg('--quota-code')} does not exist.",
            )
        return {"Quota": _quota_shape(c, quota)}

    @op(
        "service-quotas",
        "get-aws-default-service-quota",
        values=("--service-code", "--quota-code"),
        required=("--service-code", "--quota-code"),
    )
    def _default_quota(c) -> dict:
        quota = c.here.get("quotas", {}).get(c.arg("--quota-code"))
        if quota is None:
            raise ServiceError("NoSuchResourceException", "The quota does not exist.")
        return {
            "Quota": _quota_shape(
                c, {**quota, "Value": quota.get("default", quota["Value"])}
            )
        }

    def _quota_shape(c, quota: dict) -> dict:
        return {
            "ServiceCode": c.arg("--service-code"),
            "QuotaCode": c.arg("--quota-code"),
            "QuotaName": quota.get("name", c.arg("--quota-code")),
            "Value": float(quota["Value"]),
            "Adjustable": True,
            "GlobalQuota": False,
        }

    @op(
        "rds",
        "describe-orderable-db-instance-options",
        values=("--engine", "--engine-version", "--db-instance-class"),
        required=("--engine",),
    )
    def _orderable(c) -> dict:
        found = [
            o
            for o in c.here.get("orderable", [])
            if o["Engine"] == c.arg("--engine")
            and c.arg("--engine-version", o["EngineVersion"]) == o["EngineVersion"]
            and c.arg("--db-instance-class", o["DBInstanceClass"])
            == o["DBInstanceClass"]
        ]
        return {"OrderableDBInstanceOptions": found}

    @op(
        "elasticache",
        "describe-cache-engine-versions",
        values=("--engine", "--engine-version"),
    )
    def _engine_versions(c) -> dict:
        return {
            "CacheEngineVersions": [
                v
                for v in c.here.get("cache_engine_versions", [])
                if v["Engine"] == c.arg("--engine", v["Engine"])
                and v["EngineVersion"] == c.arg("--engine-version", v["EngineVersion"])
            ]
        }

    @op(
        "elasticache",
        "describe-reserved-cache-nodes-offerings",
        values=("--cache-node-type",),
    )
    def _offerings(c) -> dict:
        return {
            "ReservedCacheNodesOfferings": [
                o
                for o in c.here.get("reserved_offerings", [])
                if o["CacheNodeType"] == c.arg("--cache-node-type", o["CacheNodeType"])
            ]
        }

    # --- IAM ---------------------------------------------------------------------------
    def _role(c, name: str):
        for role in c.world.get("roles", []):
            if role["RoleName"] == name:
                return role
        return None

    @op("iam", "get-role", values=("--role-name",), required=("--role-name",))
    def _get_role(c) -> dict:
        role = _role(c, c.arg("--role-name"))
        if role is None:
            raise ServiceError(
                "NoSuchEntity",
                f"The role with name {c.arg('--role-name')} cannot be found.",
            )
        return {"Role": role}

    @op(
        "iam",
        "simulate-principal-policy",
        values=("--policy-source-arn",),
        lists=("--action-names", "--resource-arns"),
        required=("--policy-source-arn", "--action-names"),
    )
    def _simulate(c) -> dict:
        denied = set(c.world.get("denied_actions", []))
        return {
            "EvaluationResults": [
                {
                    "EvalActionName": action,
                    "EvalResourceName": "*",
                    "EvalDecision": "implicitDeny" if action in denied else "allowed",
                }
                for action in c.arg("--action-names")
            ],
            "IsTruncated": False,
        }

    @op(
        "iam",
        "create-service-linked-role",
        values=("--aws-service-name",),
        required=("--aws-service-name",),
    )
    def _create_slr(c) -> dict:
        service = c.arg("--aws-service-name")
        if service != "ecs.amazonaws.com":
            raise fake.Unmodelled(f"a service-linked role for {service}")
        name = "AWSServiceRoleForECS"
        if _role(c, name) is not None:
            raise ServiceError(
                "InvalidInput",
                f"Service role name {name} has been taken in this account, please try a different suffix.",
            )
        role = _make_role(c, "/aws-service-role/ecs.amazonaws.com/", name)
        c.world.setdefault("roles", []).append(role)
        return {"Role": role}

    def _make_role(c, path: str, name: str) -> dict:
        return {
            "Path": path,
            "RoleName": name,
            "RoleId": f"AROA{_id(name, 17).upper()}",
            "Arn": f"arn:aws:iam::{c.account}:role{path}{name}",
            "CreateDate": _now(),
            "MaxSessionDuration": 3600,
        }

    # --- CloudFormation -------------------------------------------------------------------
    def _stack(c, name: str):
        for stack in c.here.get("stacks", []):
            if (
                name in (stack["StackName"], stack["StackId"])
                and stack["StackStatus"] != "DELETE_COMPLETE"
            ):
                return stack
        return None

    @op("cloudformation", "describe-stacks", values=("--stack-name",))
    def _describe_stacks(c) -> dict:
        name = c.arg("--stack-name")
        if name is None:
            stacks = [
                s for s in c.items("stacks") if s["StackStatus"] != "DELETE_COMPLETE"
            ]
        else:
            stack = _stack(c, name)
            if stack is None:
                raise ServiceError(
                    "ValidationError", f"Stack with id {name} does not exist"
                )
            stacks = [stack]
        keep = (
            "StackId",
            "StackName",
            "CreationTime",
            "StackStatus",
            "StackStatusReason",
            "Outputs",
            "RoleARN",
            "Tags",
        )
        return {"Stacks": [{k: s[k] for k in keep if k in s} for s in stacks]}

    @op(
        "cloudformation",
        "describe-stack-events",
        values=("--stack-name",),
        required=("--stack-name",),
    )
    def _stack_events(c) -> dict:
        stack = _stack(c, c.arg("--stack-name"))
        if stack is None:
            raise ServiceError(
                "ValidationError", f"Stack [{c.arg('--stack-name')}] does not exist"
            )
        return {"StackEvents": list(stack.get("events", []))}

    # --- Route 53 -----------------------------------------------------------------------
    def _zone(c, zone_id: str):
        wanted = zone_id.rsplit("/", 1)[-1]
        for zone in c.world.get("hosted_zones", []):
            if zone["Id"].rsplit("/", 1)[-1] == wanted:
                return zone
        raise ServiceError(
            "NoSuchHostedZone", f"No hosted zone found with ID: {wanted}"
        )

    def _zone_records(zone: dict) -> list:
        if "records" not in zone:
            name = zone["Name"]
            zone["records"] = [
                {
                    "Name": name,
                    "Type": "NS",
                    "TTL": 172800,
                    "ResourceRecords": [
                        {"Value": ns + "."} for ns in zone.get("NameServers", [])
                    ],
                },
                {
                    "Name": name,
                    "Type": "SOA",
                    "TTL": 900,
                    "ResourceRecords": [
                        {
                            "Value": f"{zone.get('NameServers', ['ns'])[0]}. awsdns-hostmaster.amazon.com. 1 7200 900 1209600 86400"
                        }
                    ],
                },
            ]
        return zone["records"]

    @op("route53", "get-hosted-zone", values=("--id",), required=("--id",))
    def _get_zone(c) -> dict:
        zone = _zone(c, c.arg("--id"))
        shown = {
            k: v
            for k, v in zone.items()
            if k not in ("records", "NameServers", "public_ns")
        }
        return {
            "HostedZone": shown,
            "DelegationSet": {"NameServers": list(zone.get("NameServers", []))},
        }

    @op(
        "route53",
        "list-resource-record-sets",
        values=("--hosted-zone-id",),
        required=("--hosted-zone-id",),
    )
    def _records(c) -> dict:
        zone = _zone(c, c.arg("--hosted-zone-id"))
        return {
            "ResourceRecordSets": list(_zone_records(zone)),
            "IsTruncated": False,
            "MaxItems": "300",
        }

    @op(
        "route53",
        "change-resource-record-sets",
        values=("--hosted-zone-id", "--change-batch"),
        required=("--hosted-zone-id", "--change-batch"),
    )
    def _change(c) -> dict:
        zone = _zone(c, c.arg("--hosted-zone-id"))
        try:
            batch = json.loads(_value(c.arg("--change-batch")))
        except (OSError, ValueError) as error:
            raise Usage(f"Error parsing parameter '--change-batch': {error}") from error
        records = _zone_records(zone)
        for change in batch["Changes"]:
            rrset = change["ResourceRecordSet"]
            key = (rrset["Name"], rrset["Type"])
            present = [r for r in records if (r["Name"], r["Type"]) == key]
            action = change["Action"]
            if action == "CREATE":
                if present:
                    raise ServiceError(
                        "InvalidChangeBatch",
                        f"[Tried to create resource record set [name='{key[0]}', type='{key[1]}'] but it already exists]",
                    )
                records.append(rrset)
            elif action == "DELETE":
                if not present or present[0] != rrset:
                    raise ServiceError(
                        "InvalidChangeBatch",
                        f"[Tried to delete resource record set [name='{key[0]}', type='{key[1]}'] but the values provided do not match the current values]",
                    )
                records.remove(present[0])
            elif action == "UPSERT":
                for r in present:
                    records.remove(r)
                records.append(rrset)
            else:
                raise Usage(f"Invalid Action {action}")
        zone["ResourceRecordSetCount"] = len(records)
        change_id = f"/change/C{_id(json.dumps(batch, sort_keys=True), 13).upper()}"
        c.world.setdefault("changes", {})[change_id] = "INSYNC"
        return {
            "ChangeInfo": {"Id": change_id, "Status": "PENDING", "SubmittedAt": _now()}
        }

    @op("route53", "get-change", values=("--id",), required=("--id",))
    def _get_change(c) -> dict:
        wanted = c.arg("--id")
        changes = c.world.get("changes", {})
        for change_id, status in changes.items():
            if wanted in (change_id, change_id.rsplit("/", 1)[-1]):
                return {
                    "ChangeInfo": {
                        "Id": change_id,
                        "Status": status,
                        "SubmittedAt": _now(),
                    }
                }
        raise ServiceError(
            "NoSuchChange",
            f"A change with the specified change ID does not exist: {wanted}",
        )

    # --- ECR ------------------------------------------------------------------------------
    def _repository(c, name: str):
        for repo in c.here.get("repositories", []):
            if repo["repositoryName"] == name:
                return repo
        return None

    @op(
        "ecr",
        "create-repository",
        values=("--repository-name", "--image-scanning-configuration"),
        lists=("--tags",),
        required=("--repository-name",),
    )
    def _create_repository(c) -> dict:
        name = c.arg("--repository-name")
        if _repository(c, name) is not None:
            raise ServiceError(
                "RepositoryAlreadyExistsException",
                f"The repository with name '{name}' already exists in the registry with id '{c.account}'",
            )
        scan = _shorthand(c.arg("--image-scanning-configuration", "scanOnPush=false"))
        repo = {
            "repositoryArn": f"arn:aws:ecr:{c.region}:{c.account}:repository/{name}",
            "registryId": c.account,
            "repositoryName": name,
            "repositoryUri": f"{c.account}.dkr.ecr.{c.region}.amazonaws.com/{name}",
            "createdAt": _now(),
            "imageTagMutability": "MUTABLE",
            "imageScanningConfiguration": {
                "scanOnPush": scan.get("scanOnPush") == "true"
            },
            "encryptionConfiguration": {"encryptionType": "AES256"},
        }
        c.here.setdefault("repositories", []).append(repo)
        c.here.setdefault("repository_tags", {})[name] = _tags(c.arg("--tags"))
        return {"repository": repo}

    @op(
        "ecr",
        "describe-images",
        values=("--repository-name", "--image-ids"),
        required=("--repository-name",),
    )
    def _describe_images(c) -> dict:
        name = c.arg("--repository-name")
        repo = _repository(c, name)
        if repo is None:
            raise ServiceError(
                "RepositoryNotFoundException",
                f"The repository with name '{name}' does not exist in the registry with id '{c.account}'",
            )
        images = c.here.get("images", {}).get(name, [])
        wanted = c.arg("--image-ids")
        if wanted is not None:
            tag = _shorthand(wanted).get("imageTag")
            images = [i for i in images if tag in i.get("imageTags", [])]
            if not images:
                raise ServiceError(
                    "ImageNotFoundException",
                    f"The image with imageId {{imageDigest:'null', imageTag:'{tag}'}} does not exist within the repository with name '{name}' in the registry with id '{c.account}'",
                )
        return {"imageDetails": images}

    # --- Secrets Manager ---------------------------------------------------------------------
    def _secret(c, wanted: str):
        for secret in c.here.get("secrets", []):
            if wanted in (secret["Name"], secret["ARN"]):
                return secret
        return None

    @op(
        "secretsmanager",
        "create-secret",
        values=("--name", "--description", "--secret-string"),
        lists=("--tags",),
        required=("--name",),
    )
    def _create_secret(c) -> dict:
        name = c.arg("--name")
        existing = _secret(c, name)
        if existing is not None:
            if "DeletedDate" in existing:
                raise ServiceError(
                    "InvalidRequestException",
                    "You can't create this secret because a secret with this name is already scheduled for deletion.",
                )
            raise ServiceError(
                "ResourceExistsException",
                f"The operation failed because the secret {name} already exists.",
            )
        value = _value(c.arg("--secret-string", ""))
        counter = c.state.setdefault("secret_counter", 0) + 1
        c.state["secret_counter"] = counter
        arn = f"arn:aws:secretsmanager:{c.region}:{c.account}:secret:{name}-{_id(name + str(counter), 6)}"
        version = _id(arn, 32)
        secret = {
            "ARN": arn,
            "Name": name,
            "Description": c.arg("--description", ""),
            "CreatedDate": _now(),
            "Tags": _tags(c.arg("--tags")),
        }
        c.here.setdefault("secrets", []).append(secret)
        c.here.setdefault("secret_values", {})[arn] = value
        return {
            "ARN": arn,
            "Name": name,
            "VersionId": f"{version[:8]}-{version[8:12]}-{version[12:16]}-{version[16:20]}-{version[20:32]}",
        }

    @op(
        "secretsmanager",
        "describe-secret",
        values=("--secret-id",),
        required=("--secret-id",),
    )
    def _describe_secret(c) -> dict:
        secret = _secret(c, c.arg("--secret-id"))
        if secret is None:
            raise ServiceError(
                "ResourceNotFoundException",
                "Secrets Manager can't find the specified secret.",
            )
        return dict(secret)

    @op(
        "secretsmanager",
        "get-secret-value",
        values=("--secret-id",),
        required=("--secret-id",),
    )
    def _get_secret_value(c) -> dict:
        secret = _secret(c, c.arg("--secret-id"))
        if secret is None or "DeletedDate" in secret:
            raise ServiceError(
                "ResourceNotFoundException",
                "Secrets Manager can't find the specified secret.",
            )
        return {
            "ARN": secret["ARN"],
            "Name": secret["Name"],
            "VersionId": "00000000-0000-4000-8000-000000000001",
            "SecretString": c.here.get("secret_values", {}).get(secret["ARN"], ""),
            "VersionStages": ["AWSCURRENT"],
            "CreatedDate": secret["CreatedDate"],
        }

    @op(
        "secretsmanager",
        "list-secrets",
        flags=("--include-planned-deletion",),
        values=("--filters",),
    )
    def _list_secrets(c) -> dict:
        planned = c.arg("--include-planned-deletion", False)
        found = [s for s in c.items("secrets") if planned or "DeletedDate" not in s]
        filters = c.arg("--filters")
        if filters is not None:
            spec = _shorthand(filters)
            if spec.get("Key") != "name":
                raise fake.Unmodelled(f"--filters {filters}")
            found = [s for s in found if s["Name"].startswith(spec.get("Values", ""))]
        shown = [
            {k: v for k, v in s.items() if k != "Tags"} | {"Tags": s.get("Tags", [])}
            for s in found
        ]
        return {"SecretList": shown}

    # --- ACM -------------------------------------------------------------------------------
    def _certificate(c, arn: str):
        for cert in c.here.get("certificates", []):
            if cert["CertificateArn"] == arn:
                return cert
        raise ServiceError(
            "ResourceNotFoundException", f"Could not find certificate {arn}."
        )

    @op(
        "acm",
        "request-certificate",
        values=("--domain-name", "--validation-method", "--idempotency-token"),
        lists=("--tags", "--subject-alternative-names"),
        required=("--domain-name",),
    )
    def _request_certificate(c) -> dict:
        domain = c.arg("--domain-name")
        token = c.arg("--idempotency-token")
        for cert in c.here.get("certificates", []):
            if token is not None and cert.get("IdempotencyToken") == token:
                return {"CertificateArn": cert["CertificateArn"]}
        arn = f"arn:aws:acm:{c.region}:{c.account}:certificate/{_uuid(domain + str(token))}"
        names = [domain] + list(c.arg("--subject-alternative-names") or [])
        record = {
            "Name": f"_{_id('validation' + domain, 32)}.{domain}.",
            "Type": "CNAME",
            "Value": f"_{_id('target' + domain, 32)}.xlfgrmvvlj.acm-validations.aws.",
        }
        # state["acm"]: how ACM ends a validation, for a test (issues_as FAILED or
        # VALIDATION_TIMED_OUT with a failure_reason, or never_validates).
        settings = c.state.get("acm", {})
        cert = {
            "CertificateArn": arn,
            "DomainName": domain,
            "SubjectAlternativeNameSummaries": names,
            "Status": "PENDING_VALIDATION",
            "Type": "AMAZON_ISSUED",
            "KeyAlgorithm": "RSA_2048",
            "InUse": False,
            "IdempotencyToken": token,
            "ValidationRecord": record,
            "Tags": _tags(c.arg("--tags")),
            "CreatedAt": _now(),
            "issues_as": settings.get("issues_as", "ISSUED"),
            "failure_reason": settings.get("failure_reason", "CAA_ERROR"),
            "never_validates": settings.get("never_validates", False),
        }
        c.here.setdefault("certificates", []).append(cert)
        return {"CertificateArn": arn}

    def _uuid(seed: str) -> str:
        h = _id(seed, 32)
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    @op(
        "acm",
        "describe-certificate",
        values=("--certificate-arn",),
        required=("--certificate-arn",),
    )
    def _describe_certificate(c) -> dict:
        cert = _certificate(c, c.arg("--certificate-arn"))
        record = cert.get("ValidationRecord")
        if (
            cert["Status"] == "PENDING_VALIDATION"
            and record is not None
            and not cert.get("never_validates")
            and _validated(c, record)
        ):
            cert["Status"] = cert.get("issues_as", "ISSUED")
            if cert["Status"] != "ISSUED":
                cert["FailureReason"] = cert.get("failure_reason", "CAA_ERROR")
        options = {
            "DomainName": cert["DomainName"],
            "ValidationDomain": cert["DomainName"],
            "ValidationStatus": "SUCCESS"
            if cert["Status"] == "ISSUED"
            else "PENDING_VALIDATION",
            "ValidationMethod": "DNS",
        }
        if record is not None:
            options["ResourceRecord"] = dict(record)
        shown = {
            "CertificateArn": cert["CertificateArn"],
            "DomainName": cert["DomainName"],
            "SubjectAlternativeNames": cert["SubjectAlternativeNameSummaries"],
            "DomainValidationOptions": [options],
            "Status": cert["Status"],
            "Type": cert["Type"],
            "KeyAlgorithm": cert["KeyAlgorithm"],
            "InUseBy": [],
        }
        if "FailureReason" in cert:
            shown["FailureReason"] = cert["FailureReason"]
        return {"Certificate": shown}

    def _validated(c, record: dict) -> bool:
        """The validation CNAME is in a zone of this account with ACM's value."""
        for zone in c.world.get("hosted_zones", []):
            for r in zone.get("records", []):
                if (
                    r["Name"] == record["Name"]
                    and r["Type"] == "CNAME"
                    and r.get("ResourceRecords", [{}])[0].get("Value")
                    == record["Value"]
                ):
                    return True
        return False

    # --- ECS ---------------------------------------------------------------------------------
    def _revision_arn(c, td: dict) -> str:
        return f"arn:aws:ecs:{c.region}:{c.account}:task-definition/{td['family']}:{td['revision']}"

    def _task_definition(c, wanted: str) -> dict:
        tds = c.here.get("task_definitions", [])
        if ":" in wanted.rsplit("/", 1)[-1]:
            family, revision = wanted.rsplit("/", 1)[-1].split(":")
            for td in tds:
                if td["family"] == family and str(td["revision"]) == revision:
                    return td
        else:
            active = [
                td for td in tds if td["family"] == wanted and td["status"] == "ACTIVE"
            ]
            if active:
                return max(active, key=lambda td: td["revision"])
        raise ServiceError("ClientException", "Unable to describe task definition.")

    def _td_shape(c, td: dict) -> dict:
        body = {
            k: v for k, v in td.items() if k not in ("family", "revision", "status")
        }
        return {
            "taskDefinitionArn": _revision_arn(c, td),
            "family": td["family"],
            "revision": td["revision"],
            "status": td["status"],
            **body,
        }

    @op(
        "ecs",
        "describe-task-definition",
        values=("--task-definition",),
        required=("--task-definition",),
    )
    def _describe_td(c) -> dict:
        return {
            "taskDefinition": _td_shape(
                c, _task_definition(c, c.arg("--task-definition"))
            )
        }

    @op(
        "ecs",
        "register-task-definition",
        values=("--cli-input-json",),
        required=("--cli-input-json",),
    )
    def _register_td(c) -> dict:
        body = json.loads(_value(c.arg("--cli-input-json")))
        family = body.pop("family")
        tds = c.here.setdefault("task_definitions", [])
        revision = (
            max([td["revision"] for td in tds if td["family"] == family], default=0) + 1
        )
        td = {"family": family, "revision": revision, "status": "ACTIVE", **body}
        tds.append(td)
        return {"taskDefinition": _td_shape(c, td)}

    def _cluster(c, wanted: str) -> dict:
        for cluster in c.here.get("clusters", []):
            if wanted in (cluster["clusterArn"], cluster["clusterName"]):
                return cluster
        raise ServiceError("ClusterNotFoundException", "Cluster not found.")

    @op(
        "ecs",
        "run-task",
        values=(
            "--cluster",
            "--task-definition",
            "--launch-type",
            "--network-configuration",
            "--overrides",
        ),
        required=("--task-definition",),
    )
    def _run_task(c) -> dict:
        cluster = _cluster(c, c.arg("--cluster", "default"))
        td = _task_definition(c, c.arg("--task-definition"))
        overrides = json.loads(c.arg("--overrides", "{}"))
        env = {}
        for override in overrides.get("containerOverrides", []):
            for entry in override.get("environment", []):
                env[entry["name"]] = entry["value"]
        task_id = _id(f"task{c.state.get('calls')}", 32)
        arn = f"arn:aws:ecs:{c.region}:{c.account}:task/{cluster['clusterName']}/{task_id}"
        app = c.state.setdefault("app", {})
        exit_code = app.get("migration_exit", 0)
        lines = []
        if exit_code == 0:
            admin = env.get("FIRST_SUPERUSER", "admin@example.com").lower()
            fresh = not app.get("schema", False)
            # schema_absent: a task that logs success while the API's database stays
            # without its schema (the API then answers as N5 measured: /health 200,
            # anything that needs the schema 500).
            app["schema"] = not app.get("schema_absent", False)
            app.setdefault("admin", admin)
            for line in MIGRATION_LOG:
                lines.append(
                    line.format(
                        admin=app["admin"],
                        state="created" if fresh else "upgraded",
                        fresh="Fresh database: creating schema experimentation from models and stamping heads"
                        if fresh
                        else "Database at its head: nothing to migrate",
                    )
                )
        task = {
            "taskArn": arn,
            "clusterArn": cluster["clusterArn"],
            "taskDefinitionArn": _revision_arn(c, td),
            "lastStatus": "STOPPED",
            "desiredStatus": "STOPPED",
            "stoppedReason": "Essential container in task exited",
            "containers": [
                {"name": "backend", "exitCode": exit_code, "lastStatus": "STOPPED"}
            ],
            "overrides_seen": overrides,
            "env_seen": env,
        }
        c.here.setdefault("tasks", []).append(task)
        c.here.setdefault("log_events", {})[f"migrate/backend/{task_id}"] = lines
        shown = {k: v for k, v in task.items() if not k.endswith("_seen")}
        return {"tasks": [{**shown, "lastStatus": "PROVISIONING"}], "failures": []}

    @op(
        "ecs",
        "describe-tasks",
        values=("--cluster",),
        lists=("--tasks",),
        required=("--tasks",),
    )
    def _describe_tasks(c) -> dict:
        wanted = set(c.arg("--tasks"))
        tasks = [
            {k: v for k, v in t.items() if not k.endswith("_seen")}
            for t in c.here.get("tasks", [])
            if t["taskArn"] in wanted or t["taskArn"].rsplit("/", 1)[-1] in wanted
        ]
        return {"tasks": tasks, "failures": []}

    @op("ecs", "list-tasks", values=("--cluster", "--desired-status"))
    def _list_tasks(c) -> dict:
        _cluster(c, c.arg("--cluster", "default"))
        status = c.arg("--desired-status")
        return {
            "taskArns": [
                t["taskArn"]
                for t in c.here.get("tasks", [])
                if status is None or t["desiredStatus"] == status
            ]
        }

    @op(
        "ecs",
        "describe-services",
        values=("--cluster",),
        lists=("--services",),
        required=("--services",),
    )
    def _describe_services(c) -> dict:
        _cluster(c, c.arg("--cluster", "default"))
        found = []
        for name in c.arg("--services"):
            for service in c.here.get("services", []):
                if service["serviceName"] == name:
                    found.append(service)
        return {"services": found, "failures": []}

    # --- logs, load balancers, SNS -------------------------------------------------------------
    @op(
        "logs",
        "get-log-events",
        values=("--log-group-name", "--log-stream-name", "--limit"),
        required=("--log-stream-name",),
    )
    def _log_events(c) -> dict:
        lines = c.here.get("log_events", {}).get(c.arg("--log-stream-name"))
        if lines is None:
            raise ServiceError(
                "ResourceNotFoundException", "The specified log stream does not exist."
            )
        limit = int(c.arg("--limit", "10000"))
        events = [
            {
                "timestamp": 1761019500000 + n,
                "message": line,
                "ingestionTime": 1761019501000 + n,
            }
            for n, line in enumerate(lines[-limit:])
        ]
        return {"events": events, "nextForwardToken": "f/0", "nextBackwardToken": "b/0"}

    @op("elbv2", "describe-load-balancers", lists=("--names",))
    def _load_balancers(c) -> dict:
        found = c.items("load_balancers")
        names = c.arg("--names")
        if names is not None:
            found = [lb for lb in found if lb["LoadBalancerName"] in names]
            if not found:
                raise ServiceError(
                    "LoadBalancerNotFound", "One or more load balancers not found"
                )
        return {"LoadBalancers": found}

    @op(
        "sns",
        "list-subscriptions-by-topic",
        values=("--topic-arn",),
        required=("--topic-arn",),
    )
    def _subscriptions_by_topic(c) -> dict:
        arn = c.arg("--topic-arn")
        if arn not in c.items("topics"):
            raise ServiceError("NotFound", "Topic does not exist")
        return {
            "Subscriptions": [
                s for s in c.items("subscriptions") if s["TopicArn"] == arn
            ]
        }
