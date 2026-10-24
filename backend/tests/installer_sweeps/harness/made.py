"""What each stack the fakes deploy made, so that deleting the stack removes it.

The fake ``cdk deploy`` adds the nine stacks' resources to the account's state at once
(accounts.Builder.nine_stacks), and the fake ``cdk bootstrap`` adds CDKToolkit's. Here
each item they add is filed under the stack that makes it (``state["cdk"]["made"]``), by
its collection in the state as infrastructure/cdk lays the stacks out (a few by name), and
``remove_made`` removes a stack's items the way CloudFormation does when the stack is
deleted: a customer key scheduled for deletion, a table leaving AWS's SYSTEM backup, a
task-definition revision deregistered, the rest gone. What no stack makes stays: the
service-linked roles, AWS's managed keys, an unconfirmed alarm subscription (it outlives
its topic), most tagging-index entries (the index trails the services) and CDKToolkit's
bucket (its template keeps it). Used by fake_tools.py (cdk destroy) and
fake_ops_uninstall.py (cloudformation delete-stack).
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__:
    from . import accounts as _accounts
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import accounts as _accounts  # type: ignore[no-redef]

#: The log group ECS makes for the cluster's Container Insights (no stack holds it).
CONTAINER_INSIGHTS = "/aws/ecs/containerinsights/experimentation-staging/performance"

#: Which of the nine stacks makes each kind of resource the fake deploy adds, by its
#: collection in the state, as infrastructure/cdk lays them out (a few go by name:
#: _stack_of). What no stack makes is AWS's own and stays: the service-linked roles, its
#: managed keys, the alarm subscription (it outlives its topic) and most tagging-index
#: entries (the index trails the services).
STACK_OF = {
    **dict.fromkeys(
        (
            "vpcs",
            "subnets",
            "route_tables",
            "internet_gateways",
            "nat_gateways",
            "addresses",
            "network_interfaces",
            "security_groups",
            "vpc_endpoints",
            "network_acls",
        ),
        "vpc",
    ),
    **dict.fromkeys(
        (
            "db_clusters",
            "db_instances",
            "db_subnet_groups",
            "db_cluster_parameter_groups",
            "db_parameter_groups",
        ),
        "database",
    ),
    **dict.fromkeys(
        ("replication_groups", "cache_clusters", "cache_subnet_groups"), "redis"
    ),
    "tables": "dynamodb",
    "user_pools": "auth",
    "clusters": "compute",
    "functions": "compute",
    **dict.fromkeys(
        ("metric_alarms", "composite_alarms", "dashboards", "topics"), "monitoring"
    ),
    **dict.fromkeys(
        (
            "load_balancers",
            "target_groups",
            "scalable_targets",
            "codedeploy_applications",
            "services",
        ),
        "fargate",
    ),
}
#: The field that names an item of each collection (strings name themselves).
KEY_FIELDS = {
    "vpcs": "VpcId",
    "subnets": "SubnetId",
    "route_tables": "RouteTableId",
    "internet_gateways": "InternetGatewayId",
    "nat_gateways": "NatGatewayId",
    "addresses": "AllocationId",
    "network_interfaces": "NetworkInterfaceId",
    "security_groups": "GroupId",
    "vpc_endpoints": "VpcEndpointId",
    "network_acls": "NetworkAclId",
    "load_balancers": "LoadBalancerArn",
    "target_groups": "TargetGroupArn",
    "clusters": "clusterArn",
    "db_clusters": "DBClusterIdentifier",
    "db_instances": "DBInstanceIdentifier",
    "db_subnet_groups": "DBSubnetGroupName",
    "db_cluster_parameter_groups": "DBClusterParameterGroupName",
    "db_parameter_groups": "DBParameterGroupName",
    "replication_groups": "ReplicationGroupId",
    "cache_clusters": "CacheClusterId",
    "cache_subnet_groups": "CacheSubnetGroupName",
    "secrets": "ARN",
    "kms_keys": "KeyId",
    "kms_aliases": "AliasName",
    "log_groups": "logGroupName",
    "metric_alarms": "AlarmName",
    "composite_alarms": "AlarmName",
    "dashboards": "DashboardName",
    "parameters": "Name",
    "user_pools": "Id",
    "functions": "FunctionName",
    "scalable_targets": "ResourceId",
    "roles": "RoleName",
    "services": "serviceArn",
    "stacks": "StackId",
    "repositories": "repositoryName",
    "buckets": "Name",
}


def key(collection: str, item) -> str:
    if isinstance(item, str):
        return item
    if collection == "task_definitions":
        return f"{item['family']}:{item['revision']}"
    if collection == "subscriptions":
        return f"{item['TopicArn']} {item['Endpoint']}"
    return str(item[KEY_FIELDS[collection]])


def stack_of(where: str, collection: str, item) -> str | None:
    """The short name of the stack that made ``item``, or None for what AWS keeps."""
    if collection == "stacks":
        return (
            item["StackName"].removeprefix("experimentation-").removesuffix("-staging")
        )
    if collection in STACK_OF:
        return STACK_OF[collection]
    if collection == "kms_keys":
        return "database" if item.get("KeyManager") == "CUSTOMER" else None
    if collection == "kms_aliases":
        return None if item["AliasName"].startswith("alias/aws/") else "database"
    if collection == "secrets":
        return "database"
    if collection == "parameters":
        return "vpc"
    if collection == "log_groups":
        name = item["logGroupName"]
        if name.startswith("/aws/lambda/"):
            return "compute"
        return "migrations" if name.endswith("-migrate-staging") else "fargate"
    if collection == "task_definitions":
        return (
            "migrations" if item["family"].endswith("-migrate-staging") else "fargate"
        )
    if collection == "roles":
        for part in ("auth", "compute", "fargate"):
            if item["RoleName"].startswith(f"experimentation-{part}-staging-"):
                return part
        return None  # the service-linked roles
    if collection == "tag_index":
        if ":cloudformation:" in item:
            return (
                item.split("stack/", 1)[1]
                .split("/", 1)[0]
                .removeprefix("experimentation-")
                .removesuffix("-staging")
            )
        return "database" if ":secretsmanager:" in item else None
    return None


def live_stack(region: dict, name: str) -> dict | None:
    for stack in region.get("stacks", []):
        if stack["StackName"] == name and stack["StackStatus"] != "DELETE_COMPLETE":
            return stack
    return None


def attribute(state: dict, before: dict, stack: str | None = None) -> None:
    """What the deploy just added, each item filed under the stack that made it
    (``state["cdk"]["made"]``), as a later ``cdk destroy`` of that stack removes it. A
    resource added again exactly as it already is (an AWS-kept key's alias, a tagging
    entry) is the same one, not a second; a task-definition revision takes the family's
    next number, as ECS gives it, and the deploy makes only the first revision of each
    family (the second migration revision is install's own, registered by hand)."""
    made = state.setdefault("cdk", {}).setdefault("made", {})
    places = [("global", state["global"], before["global"])] + [
        (r, state["regions"][r], before["regions"].get(r, {})) for r in state["regions"]
    ]
    for where, now, then in places:
        for collection, items in list(now.items()):
            if not isinstance(items, list):
                continue
            old = list(then.get(collection, []))
            new = (
                list(items[len(old) :])
                if items[: len(old)] == old
                else [item for item in items if item not in old]
            )
            kept = [item for item in items if item not in new]
            added = []
            for item in new:
                if item in kept or item in added:
                    continue  # the same resource again
                added.append(item)
            if collection == "task_definitions":
                seen, renumbered = set(), []
                for td in added:
                    if td["family"] in seen:
                        continue
                    seen.add(td["family"])
                    td["revision"] = (
                        max(
                            [
                                o["revision"]
                                for o in kept
                                if o["family"] == td["family"]
                            ],
                            default=0,
                        )
                        + 1
                    )
                    if td["family"].endswith("-migrate-staging"):
                        state["cdk"]["migrate_revision"] = td["revision"]
                    renumbered.append(td)
                added = renumbered
            if collection == "kms_keys":
                for kms in added:
                    if any(o["KeyId"] == kms["KeyId"] for o in kept):
                        old_id = kms["KeyId"]
                        kms["KeyId"] = old_id[:-4] + f"{len(kept):04x}"
                        kms["Arn"] = kms["Arn"].replace(old_id, kms["KeyId"])
                        for alias in now.get("kms_aliases", []):
                            if alias.get(
                                "TargetKeyId"
                            ) == old_id and alias not in then.get("kms_aliases", []):
                                alias["TargetKeyId"] = kms["KeyId"]
            items[:] = kept + added
            for item in added:
                owner = stack_of(where, collection, item) if stack is None else stack
                if collection == "buckets" and stack is not None:
                    owner = None  # kept when its stack goes (DeletionPolicy Retain)
                if owner is not None:
                    made.setdefault(owner, []).append(
                        [where, collection, key(collection, item)]
                    )


def remove_made(state: dict, name: str) -> None:
    """Everything stack ``name`` made, gone as CloudFormation removes it: a customer key is
    scheduled for deletion (its alias goes), a table leaves AWS's SYSTEM backup, a
    task-definition revision is deregistered (INACTIVE), the rest is deleted."""
    short = name.removeprefix("experimentation-").removesuffix("-staging")
    region = state["regions"]["us-west-2"]
    for where, collection, wanted in (
        state.get("cdk", {}).get("made", {}).pop(short, [])
    ):
        home = state["global"] if where == "global" else state["regions"][where]
        items = home.get(collection, [])
        for item in list(items):
            if key(collection, item) != wanted:
                continue
            if collection == "stacks":
                continue  # the stack's own record: marked DELETE_COMPLETE by its caller
            if collection == "kms_keys":
                item["KeyState"] = "PendingDeletion"
                continue
            if collection == "task_definitions":
                if item["status"] == "ACTIVE":
                    item["status"] = "INACTIVE"
                continue
            if collection == "tables":
                backup = {
                    "TableName": item,
                    "TableArn": f"arn:aws:dynamodb:us-west-2:{state['account']}:table/{item}",
                    "BackupArn": f"arn:aws:dynamodb:us-west-2:{state['account']}:table/{item}/backup/01761026000000-{_accounts._id(item, 8)}",
                    "BackupName": f"{item}$DeletedTableBackup",
                    "BackupCreationDateTime": "2026-10-21T05:52:00Z",
                    "BackupStatus": "AVAILABLE",
                    "BackupType": "SYSTEM",
                    "BackupSizeBytes": 0,
                }
                if backup not in region.setdefault("backups", []):
                    region["backups"].append(backup)
            items.remove(item)
