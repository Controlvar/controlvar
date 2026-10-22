"""Account states for the fake ``aws``, from what the AWS guide's end-to-end run
measured (2026-10-21, a new account, release 0.36.1; its sweep before the first
write and after the teardown).

* ``new_account()`` is that account before its first write: in each region the
  default VPC (four subnets in us-west-2, six in us-east-1) with its internet
  gateway, network ACL, route table and default group, and the eleven AWS-managed
  KMS aliases that have no key yet; IAM's roles for Organizations, IAM Identity
  Center (an administrator and a read-only permission set), Support and Trusted
  Advisor, and Identity Center's SAML provider. Nothing else.
* ``installed_account()`` adds what an install makes, at least one item of every
  type the install creates (the nine stacks' resource types, CDKToolkit's, and
  what AWS makes on first use), so a sweep of it shows a new line in each of
  ``CREATED_FILES``.
* ``uninstalled_account()`` is the new account plus exactly the residue the run
  measured after its teardown: five service-linked roles, three DynamoDB SYSTEM
  backups, four task-definition revisions being deleted and their three INACTIVE
  families, four AWS-managed KMS keys and their aliases, the database's key
  pending deletion, the alarm subscription never confirmed (it outlives its
  topic) and ten tagging-index entries of deleted resources.

IDs are made here (``_id``); none is the run's. The account is 111111111111.
Timestamps are ISO 8601 in UTC, ending in Z, as the fake expects.
"""

from __future__ import annotations

import copy
import hashlib

ACCOUNT = "111111111111"
REGIONS = ("us-west-2", "us-east-1")
CREATED = "2026-10-21T00:33:12Z"
LONG_AGO = "2026-09-29T17:02:41Z"

#: The AWS-managed aliases a new account lists before any key exists (the run's
#: before sweep, both regions).
NEW_ACCOUNT_ALIASES = (
    "dynamodb",
    "ebs",
    "elasticfilesystem",
    "es",
    "glue",
    "kinesisvideo",
    "rds",
    "redshift",
    "s3",
    "ssm",
    "xray",
)

#: The AWS-managed keys an install makes on first use (the run's after sweep):
#: alias, then KMS's description.
FIRST_USE_KEYS = (
    (
        "acm",
        "Default key that protects my ACM private keys when no other key is defined",
    ),
    (
        "lambda",
        "Default key that protects my Lambda functions when no other key is defined",
    ),
    ("s3", "Default key that protects my S3 objects when no other key is defined"),
    (
        "secretsmanager",
        "Default key that protects my Secrets Manager data when no other key is defined",
    ),
)

#: The service-linked roles an install makes, which its teardown leaves in place,
#: as path and name.
SERVICE_ROLES = (
    ("/aws-service-role/ecs.amazonaws.com/", "AWSServiceRoleForECS"),
    (
        "/aws-service-role/elasticloadbalancing.amazonaws.com/",
        "AWSServiceRoleForElasticLoadBalancing",
    ),
    ("/aws-service-role/rds.amazonaws.com/", "AWSServiceRoleForRDS"),
    ("/aws-service-role/elasticache.amazonaws.com/", "AWSServiceRoleForElastiCache"),
    (
        "/aws-service-role/ecs.application-autoscaling.amazonaws.com/",
        "AWSServiceRoleForApplicationAutoScaling_ECSService",
    ),
)

#: The infrastructure/cdk names of a staging install (lib/names.sh holds the
#: installer's copy; these build the fake's resources).
STACKS = tuple(
    f"experimentation-{s}-staging"
    for s in (
        "dynamodb",
        "auth",
        "vpc",
        "redis",
        "database",
        "compute",
        "monitoring",
        "fargate",
        "migrations",
    )
)
TABLES = tuple(
    f"experimentation-{t}-staging" for t in ("events", "feature-flags", "overrides")
)
FAMILIES = tuple(
    f"experimentation-{f}-staging" for f in ("backend", "dashboard", "migrate")
)
TOPIC = "experimentation-alerts-staging"
DB_KEY_DESCRIPTION = (
    "KMS key for experimentation-database-staging PostgreSQL encryption"
)

#: The sweep files an install adds lines to (G1's mid sweep, where every type the
#: install creates shows a new line): each
#: type ``installed_account`` makes, with the infrastructure/cdk resource type
#: or the AWS action it stands for.
CREATED_FILES = {
    "us-west-2/cfn-stacks.txt": "the nine stacks and CDKToolkit",
    "us-west-2/ec2-vpcs.txt": "AWS::EC2::VPC",
    "us-west-2/ec2-subnets.txt": "AWS::EC2::Subnet",
    "us-west-2/ec2-route-tables.txt": "AWS::EC2::RouteTable",
    "us-west-2/ec2-internet-gateways.txt": "AWS::EC2::InternetGateway",
    "us-west-2/ec2-nat-gateways.txt": "AWS::EC2::NatGateway",
    "us-west-2/ec2-addresses.txt": "AWS::EC2::EIP",
    "us-west-2/ec2-network-interfaces.txt": "the interfaces the NAT gateway, endpoints and tasks hold",
    "us-west-2/ec2-security-groups.txt": "AWS::EC2::SecurityGroup",
    "us-west-2/ec2-vpc-endpoints.txt": "AWS::EC2::VPCEndpoint",
    "us-west-2/ec2-network-acls.txt": "AWS::EC2::NetworkAcl",
    "us-west-2/elbv2-load-balancers.txt": "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "us-west-2/elbv2-target-groups.txt": "AWS::ElasticLoadBalancingV2::TargetGroup",
    "us-west-2/ecs-clusters.txt": "AWS::ECS::Cluster",
    "us-west-2/ecs-services.txt": "AWS::ECS::Service",
    "us-west-2/ecs-td-families-active.txt": "AWS::ECS::TaskDefinition",
    "us-west-2/ecr-repositories.txt": "the two image repositories and CDKToolkit's",
    "us-west-2/rds-clusters.txt": "AWS::RDS::DBCluster",
    "us-west-2/rds-instances.txt": "AWS::RDS::DBInstance",
    "us-west-2/rds-subnet-groups.txt": "AWS::RDS::DBSubnetGroup",
    "us-west-2/rds-cluster-parameter-groups.txt": "AWS::RDS::DBClusterParameterGroup",
    "us-west-2/rds-parameter-groups.txt": "AWS::RDS::DBParameterGroup",
    "us-west-2/elasticache-replication-groups.txt": "AWS::ElastiCache::ReplicationGroup",
    "us-west-2/elasticache-cache-clusters.txt": "the replication group's node",
    "us-west-2/elasticache-subnet-groups.txt": "AWS::ElastiCache::SubnetGroup",
    "us-west-2/secrets.txt": "the two secrets and AWS::SecretsManager::Secret",
    "us-west-2/kms-aliases.txt": "AWS::KMS::Alias, and AWS's aliases for its first-use keys",
    "us-west-2/kms-keys.txt": "AWS::KMS::Key, and AWS's first-use keys",
    "us-west-2/logs-groups.txt": "AWS::Logs::LogGroup",
    "us-west-2/cloudwatch-alarms.txt": "AWS::CloudWatch::Alarm, AWS::CloudWatch::CompositeAlarm",
    "us-west-2/cloudwatch-dashboards.txt": "AWS::CloudWatch::Dashboard",
    "us-west-2/sns-topics.txt": "AWS::SNS::Topic",
    "us-west-2/sns-subscriptions.txt": "AWS::SNS::Subscription",
    "us-west-2/ssm-parameters.txt": "AWS::SSM::Parameter",
    "us-west-2/dynamodb-tables.txt": "AWS::DynamoDB::Table",
    "us-west-2/cognito-user-pools.txt": "AWS::Cognito::UserPool",
    "us-west-2/lambda-functions.txt": "AWS::Lambda::Function",
    "us-west-2/codedeploy-applications.txt": "AWS::CodeDeploy::Application",
    "us-west-2/autoscaling-ecs.txt": "AWS::ApplicationAutoScaling::ScalableTarget",
    "us-west-2/acm-certificates.txt": "the certificate",
    "us-west-2/tagged-resources.txt": "the tagged resources",
    "global/iam-roles.txt": "AWS::IAM::Role, CDKToolkit's roles and the service-linked roles",
    "global/route53-hosted-zones.txt": "the validation CNAME and the alias in the reader's zone",
    "global/s3-buckets.txt": "CDKToolkit's bucket",
}


def _id(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:length]


def _key_id(seed: str) -> str:
    h = _id(seed, 32)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class Builder:
    """One account's state, built up resource by resource."""

    def __init__(self, account: str = ACCOUNT) -> None:
        sso = _id("sso-admin", 16)
        self.account = account
        self.state: dict = {
            "account": account,
            "regions": {r: {} for r in REGIONS},
            "global": {
                "identity": {
                    "UserId": f"AROA{_id('caller', 17).upper()}:reader",
                    "Arn": f"arn:aws:sts::{account}:assumed-role/"
                    f"AWSReservedSSO_AdministratorAccess_{sso}/reader",
                },
                "canonical_user_id": _id("canonical", 64),
            },
            "faults": [],
        }

    # -- plumbing --------------------------------------------------------------------
    def add(self, region: str | None, collection: str, item) -> None:
        where = (
            self.state["global"] if region is None else self.state["regions"][region]
        )
        where.setdefault(collection, []).append(item)

    def arn(self, service: str, region: str, resource: str) -> str:
        return f"arn:aws:{service}:{region}:{self.account}:{resource}"

    def tag(self, region: str, arn: str) -> None:
        self.add(region, "tag_index", arn)

    # -- what a new account holds --------------------------------------------------------
    def default_network(self, region: str, subnets: int) -> None:
        vpc = f"vpc-{_id(region + 'default-vpc', 17)}"
        self.add(region, "vpcs", self.vpc(vpc, "172.31.0.0/16", default=True))
        for n in range(subnets):
            self.add(
                region,
                "subnets",
                self.subnet(
                    region, f"default-{n}", vpc, f"172.31.{16 * n}.0/20", default=True
                ),
            )
        igw = f"igw-{_id(region + 'default-igw', 17)}"
        self.add(
            region,
            "internet_gateways",
            {
                "Attachments": [{"State": "available", "VpcId": vpc}],
                "InternetGatewayId": igw,
                "OwnerId": self.account,
                "Tags": [],
            },
        )
        self.add(
            region,
            "network_acls",
            self.network_acl(region, "default", vpc, default=True),
        )
        self.add(
            region, "route_tables", self.route_table(region, "default", vpc, main=True)
        )
        self.add(
            region,
            "security_groups",
            self.security_group(
                region, "default", vpc, "default", "default VPC security group"
            ),
        )

    def vpc(self, vpc_id: str, cidr: str, default: bool) -> dict:
        return {
            "OwnerId": self.account,
            "InstanceTenancy": "default",
            "CidrBlockAssociationSet": [
                {
                    "AssociationId": f"vpc-cidr-assoc-{_id(vpc_id, 17)}",
                    "CidrBlock": cidr,
                    "CidrBlockState": {"State": "associated"},
                }
            ],
            "IsDefault": default,
            "VpcId": vpc_id,
            "State": "available",
            "CidrBlock": cidr,
            "DhcpOptionsId": f"dopt-{_id('dhcp', 17)}",
        }

    def subnet(
        self, region: str, seed: str, vpc: str, cidr: str, default: bool
    ) -> dict:
        subnet_id = f"subnet-{_id(region + seed, 17)}"
        zone = f"{region}{'abcdef'[int(_id(seed, 2), 16) % 4]}"
        return {
            "AvailabilityZone": zone,
            "AvailableIpAddressCount": 4091,
            "CidrBlock": cidr,
            "DefaultForAz": default,
            "MapPublicIpOnLaunch": default,
            "State": "available",
            "SubnetId": subnet_id,
            "VpcId": vpc,
            "OwnerId": self.account,
            "AssignIpv6AddressOnCreation": False,
            "Ipv6CidrBlockAssociationSet": [],
            "SubnetArn": self.arn("ec2", region, f"subnet/{subnet_id}"),
        }

    def network_acl(self, region: str, seed: str, vpc: str, default: bool) -> dict:
        return {
            "Associations": [],
            "Entries": [
                {
                    "CidrBlock": "0.0.0.0/0",
                    "Egress": egress,
                    "Protocol": "-1",
                    "RuleAction": "allow",
                    "RuleNumber": 100,
                }
                for egress in (True, False)
            ],
            "IsDefault": default,
            "NetworkAclId": f"acl-{_id(region + seed + 'acl', 17)}",
            "Tags": [],
            "VpcId": vpc,
            "OwnerId": self.account,
        }

    def route_table(self, region: str, seed: str, vpc: str, main: bool) -> dict:
        table = f"rtb-{_id(region + seed + 'rtb', 17)}"
        return {
            "Associations": [
                {
                    "Main": main,
                    "RouteTableAssociationId": f"rtbassoc-{_id(table, 17)}",
                    "RouteTableId": table,
                    "AssociationState": {"State": "associated"},
                }
            ],
            "PropagatingVgws": [],
            "RouteTableId": table,
            "Routes": [
                {
                    "DestinationCidrBlock": "172.31.0.0/16",
                    "GatewayId": "local",
                    "Origin": "CreateRouteTable",
                    "State": "active",
                }
            ],
            "Tags": [],
            "VpcId": vpc,
            "OwnerId": self.account,
        }

    def security_group(
        self, region: str, seed: str, vpc: str, name: str, description: str
    ) -> dict:
        group = f"sg-{_id(region + seed + 'sg', 17)}"
        return {
            "GroupId": group,
            "IpPermissionsEgress": [
                {
                    "IpProtocol": "-1",
                    "UserIdGroupPairs": [],
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    "Ipv6Ranges": [],
                    "PrefixListIds": [],
                }
            ],
            "VpcId": vpc,
            "SecurityGroupArn": self.arn("ec2", region, f"security-group/{group}"),
            "OwnerId": self.account,
            "GroupName": name,
            "Description": description,
            "IpPermissions": [],
        }

    def aws_alias(self, region: str, name: str, key_id: str | None = None) -> dict:
        alias = {
            "AliasName": f"alias/aws/{name}",
            "AliasArn": self.arn("kms", region, f"alias/aws/{name}"),
        }
        if key_id is not None:
            alias.update(
                TargetKeyId=key_id, CreationDate=CREATED, LastUpdatedDate=CREATED
            )
        return alias

    def kms_key(
        self, region: str, key_id: str, manager: str, state: str, description: str
    ) -> dict:
        key = {
            "AWSAccountId": self.account,
            "KeyId": key_id,
            "Arn": self.arn("kms", region, f"key/{key_id}"),
            "CreationDate": CREATED,
            "Enabled": state == "Enabled",
            "Description": description,
            "KeyUsage": "ENCRYPT_DECRYPT",
            "KeyState": state,
            "Origin": "AWS_KMS",
            "KeyManager": manager,
            "CustomerMasterKeySpec": "SYMMETRIC_DEFAULT",
            "KeySpec": "SYMMETRIC_DEFAULT",
            "EncryptionAlgorithms": ["SYMMETRIC_DEFAULT"],
            "MultiRegion": False,
        }
        if state == "PendingDeletion":
            key.update(
                DeletionDate="2026-11-19T06:00:28Z", PendingDeletionWindowInDays=30
            )
        return key

    def role(self, path: str, name: str, created: str = LONG_AGO) -> dict:
        return {
            "Path": path,
            "RoleName": name,
            "RoleId": f"AROA{_id(name, 17).upper()}",
            "Arn": f"arn:aws:iam::{self.account}:role{path}{name}",
            "CreateDate": created,
            "MaxSessionDuration": 3600,
        }

    def new_account(self) -> Builder:
        self.default_network("us-west-2", 4)
        self.default_network("us-east-1", 6)
        for region in REGIONS:
            for name in NEW_ACCOUNT_ALIASES:
                self.add(region, "kms_aliases", self.aws_alias(region, name))
        admin, viewer = _id("sso-admin", 16), _id("sso-readonly", 16)
        for path, name in (
            ("/", "OrganizationAccountAccessRole"),
            (
                "/aws-reserved/sso.amazonaws.com/us-west-2/",
                f"AWSReservedSSO_AdministratorAccess_{admin}",
            ),
            (
                "/aws-reserved/sso.amazonaws.com/us-west-2/",
                f"AWSReservedSSO_ReadOnlyAccess_{viewer}",
            ),
            (
                "/aws-service-role/organizations.amazonaws.com/",
                "AWSServiceRoleForOrganizations",
            ),
            ("/aws-service-role/sso.amazonaws.com/", "AWSServiceRoleForSSO"),
            ("/aws-service-role/support.amazonaws.com/", "AWSServiceRoleForSupport"),
            (
                "/aws-service-role/trustedadvisor.amazonaws.com/",
                "AWSServiceRoleForTrustedAdvisor",
            ),
        ):
            self.add(None, "roles", self.role(path, name))
        self.add(
            None,
            "saml_providers",
            {
                "Arn": f"arn:aws:iam::{self.account}:saml-provider/"
                f"AWSSSO_{_id('saml', 16)}_DO_NOT_DELETE",
                "ValidUntil": "2126-09-29T17:02:41Z",
                "CreateDate": LONG_AGO,
            },
        )
        return self

    # -- what an install makes -----------------------------------------------------------
    def first_use_keys(self, region: str) -> None:
        """AWS's own keys for S3, Secrets Manager, Lambda and ACM, made on first use;
        the s3 alias the account already listed now names its key."""
        aliases = self.state["regions"][region].setdefault("kms_aliases", [])
        for name, description in FIRST_USE_KEYS:
            key_id = _key_id(region + name)
            self.add(
                region,
                "kms_keys",
                self.kms_key(region, key_id, "AWS", "Enabled", description),
            )
            aliases[:] = [a for a in aliases if a["AliasName"] != f"alias/aws/{name}"]
            aliases.append(self.aws_alias(region, name, key_id))

    def installed(self, zone: str = "example.org.") -> Builder:
        r = "us-west-2"
        vpc = f"vpc-{_id('install-vpc', 17)}"
        for n, stack in enumerate(("CDKToolkit",) + STACKS):
            stack_id = self.arn("cloudformation", r, f"stack/{stack}/{_key_id(stack)}")
            self.add(
                r,
                "stacks",
                {
                    "StackId": stack_id,
                    "StackName": stack,
                    "CreationTime": f"2026-10-21T03:{26 + n:02d}:40Z",
                    "StackStatus": "CREATE_COMPLETE",
                },
            )
            self.tag(r, stack_id)
        self.add(r, "vpcs", self.vpc(vpc, "10.0.0.0/16", default=False))
        for n in range(6):
            self.add(
                r,
                "subnets",
                self.subnet(
                    r, f"install-{n}", vpc, f"10.0.{n * 32}.0/19", default=False
                ),
            )
            self.add(
                r, "route_tables", self.route_table(r, f"install-{n}", vpc, main=False)
            )
        self.add(r, "route_tables", self.route_table(r, "install-main", vpc, main=True))
        self.add(
            r,
            "internet_gateways",
            {
                "Attachments": [{"State": "available", "VpcId": vpc}],
                "InternetGatewayId": f"igw-{_id('install-igw', 17)}",
                "OwnerId": self.account,
                "Tags": [{"Key": "Name", "Value": "experimentation-vpc-staging/Vpc"}],
            },
        )
        nat_eni = f"eni-{_id('nat-eni', 17)}"
        self.add(
            r,
            "nat_gateways",
            {
                "CreateTime": "2026-10-21T03:28:00Z",
                "NatGatewayId": f"nat-{_id('nat', 17)}",
                "State": "available",
                "SubnetId": f"subnet-{_id(r + 'install-0', 17)}",
                "VpcId": vpc,
                "ConnectivityType": "public",
                "NatGatewayAddresses": [
                    {
                        "AllocationId": f"eipalloc-{_id('eip', 17)}",
                        "NetworkInterfaceId": nat_eni,
                    }
                ],
            },
        )
        self.add(
            r,
            "addresses",
            {
                "AllocationId": f"eipalloc-{_id('eip', 17)}",
                "PublicIp": "198.51.100.23",
                "Domain": "vpc",
                "NetworkInterfaceId": nat_eni,
                "NetworkBorderGroup": r,
            },
        )
        for n, kind in enumerate(
            ("nat_gateway", "vpc_endpoint", "network_load_balancer", "interface")
        ):
            self.add(
                r,
                "network_interfaces",
                {
                    "NetworkInterfaceId": nat_eni
                    if n == 0
                    else f"eni-{_id(f'eni{n}', 17)}",
                    "InterfaceType": "interface"
                    if kind == "network_load_balancer"
                    else kind,
                    "Status": "in-use",
                    "VpcId": vpc,
                    "SubnetId": f"subnet-{_id(r + 'install-0', 17)}",
                    "OwnerId": self.account,
                    "RequesterManaged": n < 2,
                    "SourceDestCheck": n != 0,
                    "PrivateIpAddress": f"10.0.0.{10 + n}",
                    "Groups": [],
                    "TagSet": [],
                },
            )
        for name in ("vpc-default", "database", "redis", "compute", "alb", "api"):
            self.add(
                r,
                "security_groups",
                self.security_group(
                    r,
                    "install-" + name,
                    vpc,
                    f"experimentation-{name}-staging-sg",
                    f"{name} group",
                ),
            )
        for n, service in enumerate(
            ("ecr.api", "ecr.dkr", "logs", "secretsmanager", "sts", "s3")
        ):
            endpoint = f"vpce-{_id('vpce' + service, 17)}"
            self.add(
                r,
                "vpc_endpoints",
                {
                    "VpcEndpointId": endpoint,
                    "VpcEndpointType": "Gateway" if service == "s3" else "Interface",
                    "VpcId": vpc,
                    "ServiceName": f"com.amazonaws.{r}.{service}",
                    "State": "available",
                    "OwnerId": self.account,
                },
            )
            self.tag(r, self.arn("ec2", r, f"vpc-endpoint/{endpoint}"))
        for n in range(3):
            self.add(
                r,
                "network_acls",
                self.network_acl(r, f"install-{n}", vpc, default=n == 0),
            )
        lb = "experimentation-staging"
        self.add(
            r,
            "load_balancers",
            {
                "LoadBalancerArn": self.arn(
                    "elasticloadbalancing", r, f"loadbalancer/app/{lb}/{_id(lb, 16)}"
                ),
                "DNSName": f"{lb}-123456789.{r}.elb.amazonaws.com",
                "CanonicalHostedZoneId": "Z1H1FL5HABSF5",
                "CreatedTime": "2026-10-21T03:40:00Z",
                "LoadBalancerName": lb,
                "Scheme": "internet-facing",
                "VpcId": vpc,
                "State": {"Code": "active"},
                "Type": "application",
                "IpAddressType": "ipv4",
            },
        )
        for name, port in (
            ("api-blue", 8000),
            ("api-green", 8000),
            ("dashboard", 3000),
        ):
            group = f"exp-staging-{name}"
            self.add(
                r,
                "target_groups",
                {
                    "TargetGroupArn": self.arn(
                        "elasticloadbalancing",
                        r,
                        f"targetgroup/{group}/{_id(group, 16)}",
                    ),
                    "TargetGroupName": group,
                    "Protocol": "HTTP",
                    "Port": port,
                    "VpcId": vpc,
                    "TargetType": "ip",
                },
            )
        cluster = self.arn("ecs", r, "cluster/experimentation-staging")
        self.add(
            r,
            "clusters",
            {
                "clusterArn": cluster,
                "clusterName": "experimentation-staging",
                "serviceArns": [
                    self.arn("ecs", r, f"service/experimentation-staging/{family}")
                    for family in FAMILIES[:2]
                ],
            },
        )
        self.tag(r, cluster)
        for family in FAMILIES:
            self.add(
                r,
                "task_definitions",
                {"family": family, "revision": 1, "status": "ACTIVE"},
            )
        self.add(
            r,
            "task_definitions",
            {"family": FAMILIES[2], "revision": 2, "status": "ACTIVE"},
        )
        for name in (
            "experimentation-platform/backend",  # names.py:38
            "experimentation-platform/web",  # names.py:45
            f"cdk-hnb659fds-container-assets-{self.account}-{r}",
        ):
            self.add(
                r,
                "repositories",
                {
                    "repositoryArn": self.arn("ecr", r, f"repository/{name}"),
                    "registryId": self.account,
                    "repositoryName": name,
                    "repositoryUri": f"{self.account}.dkr.ecr.{r}.amazonaws.com/{name}",
                    "createdAt": "2026-10-21T02:37:00Z",
                    "imageTagMutability": "MUTABLE",
                    "imageScanningConfiguration": {"scanOnPush": False},
                    "encryptionConfiguration": {"encryptionType": "AES256"},
                },
            )
        db = "experimentation-database-staging-auroracluster-1a2b3c4d5e6f"
        self.add(
            r,
            "db_clusters",
            {
                "DBClusterIdentifier": db,
                "Status": "available",
                "Engine": "aurora-postgresql",
                "EngineVersion": "15.17",
                "Port": 5432,
                "MultiAZ": False,
                "StorageEncrypted": True,
                "DBClusterArn": self.arn("rds", r, f"cluster:{db}"),
            },
        )
        self.add(
            r,
            "db_instances",
            {
                "DBInstanceIdentifier": "experimentation-database-staging-writer-7g8h9i0j",
                "DBInstanceClass": "db.t3.medium",
                "Engine": "aurora-postgresql",
                "DBInstanceStatus": "available",
                "DBClusterIdentifier": db,
            },
        )
        self.add(
            r,
            "db_subnet_groups",
            {
                "DBSubnetGroupName": "experimentation-database-staging-subnets-abc123",
                "DBSubnetGroupDescription": "Subnets for the database",
                "VpcId": vpc,
                "SubnetGroupStatus": "Complete",
            },
        )
        self.add(
            r,
            "db_cluster_parameter_groups",
            {
                "DBClusterParameterGroupName": "default.aurora-postgresql15",
                "DBParameterGroupFamily": "aurora-postgresql15",
                "Description": "Default cluster parameter group for aurora-postgresql15",
            },
        )
        self.add(
            r,
            "db_cluster_parameter_groups",
            {
                "DBClusterParameterGroupName": "experimentation-database-staging-clusterparams-def456",
                "DBParameterGroupFamily": "aurora-postgresql15",
                "Description": "Cluster parameter group for the database",
            },
        )
        self.add(
            r,
            "db_parameter_groups",
            {
                "DBParameterGroupName": "experimentation-database-staging-instanceparams-ghi789",
                "DBParameterGroupFamily": "aurora-postgresql15",
                "Description": "Instance parameter group for the database",
            },
        )
        redis = "experimentation-redis-staging"
        self.add(
            r,
            "replication_groups",
            {
                "ReplicationGroupId": redis,
                "Description": "Redis for the API",
                "Status": "available",
                "MemberClusters": [f"{redis}-001"],
                "AtRestEncryptionEnabled": True,
                "TransitEncryptionEnabled": True,
            },
        )
        self.add(
            r,
            "cache_clusters",
            {
                "CacheClusterId": f"{redis}-001",
                "CacheNodeType": "cache.t4g.small",
                "Engine": "redis",
                "EngineVersion": "7.0.7",
                "CacheClusterStatus": "available",
                "NumCacheNodes": 1,
                "ReplicationGroupId": redis,
            },
        )
        self.add(
            r,
            "cache_subnet_groups",
            {
                "CacheSubnetGroupName": f"{redis}-subnets",
                "CacheSubnetGroupDescription": "Subnets for Redis",
                "VpcId": vpc,
            },
        )
        for name in (
            "/staging/experimentation/jwt-secret",
            "/staging/experimentation/first-superuser-password",
            "experimentation-database-staging-credentials",
        ):
            secret = self.arn("secretsmanager", r, f"secret:{name}-{_id(name, 6)}")
            self.add(
                r,
                "secrets",
                {"ARN": secret, "Name": name, "CreatedDate": "2026-10-21T02:37:30Z"},
            )
            self.tag(r, secret)
        db_key = _key_id("database-key")
        self.add(
            r,
            "kms_keys",
            self.kms_key(r, db_key, "CUSTOMER", "Enabled", DB_KEY_DESCRIPTION),
        )
        self.add(
            r,
            "kms_aliases",
            {
                "AliasName": "alias/experimentation-database-staging-postgres-key",
                "AliasArn": self.arn(
                    "kms", r, "alias/experimentation-database-staging-postgres-key"
                ),
                "TargetKeyId": db_key,
                "CreationDate": CREATED,
            },
        )
        self.tag(r, self.arn("kms", r, f"key/{db_key}"))
        self.first_use_keys(r)
        for group in (
            "/ecs/experimentation-backend-staging",
            "/ecs/experimentation-dashboard-staging",
            "/ecs/experimentation-migrate-staging",
            "/aws/lambda/experimentation-compute-staging-processor",
        ):
            self.add(
                r,
                "log_groups",
                {
                    "logGroupName": group,
                    "creationTime": 1761017400000,
                    "metricFilterCount": 0,
                    "arn": self.arn("logs", r, f"log-group:{group}:*"),
                    "storedBytes": 0,
                },
            )
        for name in ("api-5xx-blue", "api-5xx-green", "api-latency", "database-cpu"):
            self.add(
                r,
                "metric_alarms",
                {
                    "AlarmName": f"experimentation-{name}-staging",
                    "AlarmArn": self.arn(
                        "cloudwatch", r, f"alarm:experimentation-{name}-staging"
                    ),
                    "StateValue": "OK",
                    "StateUpdatedTimestamp": "2026-10-21T03:51:00Z",
                    "ActionsEnabled": True,
                },
            )
        self.add(
            r,
            "composite_alarms",
            {
                "AlarmName": "experimentation-api-health-staging",
                "AlarmArn": self.arn(
                    "cloudwatch", r, "alarm:experimentation-api-health-staging"
                ),
                "StateValue": "OK",
                "ActionsEnabled": True,
            },
        )
        for name in ("experimentation-staging", "experimentation-api-staging"):
            self.add(
                r,
                "dashboards",
                {
                    "DashboardName": name,
                    "DashboardArn": f"arn:aws:cloudwatch::{self.account}:dashboard/{name}",
                    "LastModified": "2026-10-21T03:50:00Z",
                    "Size": 2048,
                },
            )
        topic = self.arn("sns", r, TOPIC)
        self.add(r, "topics", topic)
        self.add(
            r,
            "subscriptions",
            {
                "SubscriptionArn": "PendingConfirmation",
                "Owner": self.account,
                "Protocol": "email",
                "Endpoint": "alerts@example.org",
                "TopicArn": topic,
            },
        )
        for name in (
            "/cdk-bootstrap/hnb659fds/version",
            "/staging/experimentation/vpc-id",
        ):
            self.add(
                r,
                "parameters",
                {
                    "Name": name,
                    "ARN": self.arn("ssm", r, f"parameter{name}"),
                    "Type": "String",
                    "LastModifiedDate": "2026-10-21T03:27:00Z",
                    "Version": 1,
                    "Tier": "Standard",
                    "DataType": "text",
                },
            )
        self.state["regions"][r].setdefault("tables", []).extend(TABLES)
        pool = f"{r}_{_id('pool', 9)}"
        self.add(
            r,
            "user_pools",
            {
                "Id": pool,
                "Name": "experimentation-users-staging",
                "LastModifiedDate": "2026-10-21T03:28:00Z",
                "CreationDate": "2026-10-21T03:28:00Z",
            },
        )
        self.tag(r, self.arn("cognito-idp", r, f"userpool/{pool}"))
        self.add(
            r,
            "functions",
            {
                "FunctionName": "experimentation-compute-staging-processor",
                "FunctionArn": self.arn(
                    "lambda", r, "function:experimentation-compute-staging-processor"
                ),
                "Runtime": "python3.11",
                "Handler": "index.handler",
                "MemorySize": 256,
                "Timeout": 30,
            },
        )
        self.state["regions"][r].setdefault("codedeploy_applications", []).append(
            "experimentation-platform-staging"
        )
        self.add(
            r,
            "scalable_targets",
            {
                "ServiceNamespace": "ecs",
                "ResourceId": "service/experimentation-staging/experimentation-backend-staging",
                "ScalableDimension": "ecs:service:DesiredCount",
                "MinCapacity": 2,
                "MaxCapacity": 10,
                "RoleARN": f"arn:aws:iam::{self.account}:role/aws-service-role/"
                "ecs.application-autoscaling.amazonaws.com/"
                "AWSServiceRoleForApplicationAutoScaling_ECSService",
                "CreationTime": "2026-10-21T03:45:00Z",
            },
        )
        host = zone.rstrip(".")
        self.add(
            r,
            "certificates",
            {
                "CertificateArn": self.arn(
                    "acm", r, f"certificate/{_key_id('certificate')}"
                ),
                "DomainName": f"install-test.{host}",
                "Status": "ISSUED",
                "Type": "AMAZON_ISSUED",
                "KeyAlgorithm": "RSA_2048",
                "InUse": True,
            },
        )
        # global: the stacks' roles, CDKToolkit's and the service-linked roles
        for name in (
            "auth-staging-SmsRole",
            "compute-staging-FunctionRole",
            "fargate-staging-TaskRole",
        ):
            self.add(
                None,
                "roles",
                self.role(
                    "/", f"experimentation-{name}-{_id(name, 12).upper()}", CREATED
                ),
            )
        for kind in (
            "cfn-exec",
            "deploy",
            "file-publishing",
            "image-publishing",
            "lookup",
        ):
            self.add(
                None,
                "roles",
                self.role(
                    "/", f"cdk-hnb659fds-{kind}-role-{self.account}-{r}", CREATED
                ),
            )
        for path, name in SERVICE_ROLES:
            self.add(None, "roles", self.role(path, name, CREATED))
        self.add(
            None,
            "buckets",
            {
                "Name": f"cdk-hnb659fds-assets-{self.account}-{r}",
                "CreationDate": "2026-10-21T00:56:00Z",
            },
        )
        zones = self.state["global"].setdefault("hosted_zones", [])
        for z in zones:
            if z["Name"] == zone:
                z["ResourceRecordSetCount"] += 2
        return self

    def reader_zone(self, zone: str = "example.org.", records: int = 9) -> Builder:
        """The reader's own public zone, with ``records`` record sets."""
        self.add(
            None,
            "hosted_zones",
            {
                "Id": f"/hostedzone/Z{_id(zone, 20).upper()}",
                "Name": zone,
                "CallerReference": _id(zone + "ref", 36),
                "Config": {"PrivateZone": False},
                "ResourceRecordSetCount": records,
            },
        )
        return self

    # -- what an uninstall leaves (the run's measured residue) -------------------------
    def residue(self) -> Builder:
        r = "us-west-2"
        for path, name in SERVICE_ROLES:
            self.add(None, "roles", self.role(path, name, CREATED))
        for table in TABLES:
            self.add(
                r,
                "backups",
                {
                    "TableName": table,
                    "TableArn": self.arn("dynamodb", r, f"table/{table}"),
                    "BackupArn": self.arn(
                        "dynamodb",
                        r,
                        f"table/{table}/backup/01761026000000-{_id(table, 8)}",
                    ),
                    "BackupName": f"{table}$DeletedTableBackup",
                    "BackupCreationDateTime": "2026-10-21T05:52:00Z",
                    "BackupStatus": "AVAILABLE",
                    "BackupType": "SYSTEM",
                    "BackupSizeBytes": 0,
                },
            )
        for family, revision in (
            (FAMILIES[0], 1),
            (FAMILIES[1], 1),
            (FAMILIES[2], 1),
            (FAMILIES[2], 2),
        ):
            self.add(
                r,
                "task_definitions",
                {
                    "family": family,
                    "revision": revision,
                    "status": "DELETE_IN_PROGRESS",
                },
            )
        self.first_use_keys(r)
        db_key = _key_id("database-key")
        self.add(
            r,
            "kms_keys",
            self.kms_key(r, db_key, "CUSTOMER", "PendingDeletion", DB_KEY_DESCRIPTION),
        )
        self.add(
            r,
            "subscriptions",
            {
                "SubscriptionArn": "PendingConfirmation",
                "Owner": self.account,
                "Protocol": "email",
                "Endpoint": "alerts@example.org",
                "TopicArn": self.arn("sns", r, TOPIC),
            },
        )
        for arn in (
            self.arn("cognito-idp", r, f"userpool/{r}_{_id('pool', 9)}"),
            self.arn("ec2", r, f"natgateway/nat-{_id('nat', 17)}"),
            *(
                self.arn("ec2", r, f"vpc-endpoint/vpce-{_id('vpce' + s, 17)}")
                for s in ("ecr.api", "ecr.dkr", "logs", "secretsmanager", "sts", "s3")
            ),
            self.arn("ecs", r, "cluster/experimentation-staging"),
            self.arn("kms", r, f"key/{db_key}"),
        ):
            self.tag(r, arn)
        return self


def new_account() -> dict:
    return Builder().new_account().reader_zone().state


def installed_account() -> dict:
    return Builder().new_account().reader_zone().installed().state


def uninstalled_account() -> dict:
    return Builder().new_account().reader_zone().residue().state


def copy_of(state: dict) -> dict:
    return copy.deepcopy(state)
