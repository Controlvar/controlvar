from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    Tags,
    aws_ec2 as ec2,
    aws_elasticache as elasticache,
    aws_ssm as ssm,
)
from constructs import Construct

from stacks.environments import redis_node_count


class ElastiCacheRedisStack(Stack):
    """
    CDK Stack for creating an ElastiCache Redis cluster.

    Features:
    - Environment-based scaling
    - Subnet group configuration
    - Security group restrictions
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        environment: str = "dev",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 1. CONFIGURE SUBNET GROUP
        # Create a subnet group for Redis using private subnets
        subnet_ids = vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        ).subnet_ids

        # ElastiCache refuses a name ending in a hyphen, and for prod and demo
        # the 40-character cut ends in one. Keep the cut: without it staging's
        # and dev's names change, and a new name replaces the subnet group and
        # the Redis that uses it. test_physical_names_are_valid.py pins all four.
        redis_subnet_group = elasticache.CfnSubnetGroup(
            self,
            "RedisSubnetGroup",
            description=f"Subnet group for {construct_id} Redis cluster",
            subnet_ids=subnet_ids,
            cache_subnet_group_name=f"{construct_id}-redis-subnet-group".lower()
            .replace("_", "-")[:40]
            .rstrip("-"),
        )

        # 2. CONFIGURE SECURITY GROUP
        # Create a security group for Redis
        redis_security_group = ec2.SecurityGroup(
            self,
            "RedisSecurityGroup",
            vpc=vpc,
            description=f"Security group for {construct_id} Redis cluster",
            allow_all_outbound=False,
        )

        # Allow access from VPC CIDR
        redis_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(6379),
            description="Allow Redis access from within VPC",
        )

        # 3. ENVIRONMENT-SPECIFIC CONFIGURATIONS
        # Different settings based on environment
        # prod: a primary and two replicas; one node elsewhere
        # (stacks/environments.py, which the monitoring stack's per-node CPU
        # alarms read too).
        cluster_size = redis_node_count(environment)
        instance_type = self._get_instance_type_for_env(environment)

        # 4. CREATE REDIS REPLICATION GROUP
        # The id is a literal, so its nodes' ids are too: ElastiCache names
        # them <id>-001 to <id>-00N. The monitoring stack is given this plain
        # string by app.py -- no cross-stack import -- to alarm on each node.
        self.replication_group_id = f"{construct_id}-redis".lower().replace("_", "-")[:40]
        self.redis_cluster = elasticache.CfnReplicationGroup(
            self,
            "RedisCluster",
            replication_group_id=self.replication_group_id,
            replication_group_description=f"Redis cluster for {construct_id}",
            engine="redis",
            engine_version="7.0",
            port=6379,
            cache_node_type=instance_type,
            num_cache_clusters=cluster_size,
            automatic_failover_enabled=cluster_size > 1,
            auto_minor_version_upgrade=True,
            multi_az_enabled=cluster_size > 1,
            security_group_ids=[redis_security_group.security_group_id],
            cache_subnet_group_name=redis_subnet_group.ref,
            snapshot_retention_limit=self._get_snapshot_retention_for_env(environment),
            snapshot_window="02:00-03:00",  # UTC
            preferred_maintenance_window="sun:04:00-sun:05:00",  # UTC
            at_rest_encryption_enabled=True,
            transit_encryption_enabled=True,
        )

        # Where the API task connects (#147): the PRIMARY endpoint, the one that
        # takes writes. The rate limiter INCRs and the caches SET, so a reader
        # endpoint would fail every write. The port is a string attribute.
        self.primary_host = self.redis_cluster.attr_primary_end_point_address
        self.primary_port = self.redis_cluster.attr_primary_end_point_port

        # Add tags
        Tags.of(self.redis_cluster).add("Name", f"{construct_id}-redis")
        Tags.of(self.redis_cluster).add("Environment", environment)
        Tags.of(self.redis_cluster).add("Service", "experimently")

        # 5. STORE CONNECTION INFORMATION IN SSM
        ssm.StringParameter(
            self,
            "RedisEndpointParam",
            parameter_name=f"/experimentation/{environment}/redis/endpoint",
            string_value=self.redis_cluster.attr_primary_end_point_address,
            description=f"Redis primary endpoint for {construct_id}",
        )

        ssm.StringParameter(
            self,
            "RedisPortParam",
            parameter_name=f"/experimentation/{environment}/redis/port",
            string_value=self.redis_cluster.attr_primary_end_point_port,
            description=f"Redis port for {construct_id}",
        )

        if cluster_size > 1:
            ssm.StringParameter(
                self,
                "RedisReaderEndpointParam",
                parameter_name=f"/experimentation/{environment}/redis/reader-endpoint",
                string_value=self.redis_cluster.attr_reader_end_point_address,
                description=f"Redis reader endpoint for {construct_id}",
            )

        # 6. CREATE CLOUDFORMATION OUTPUTS
        CfnOutput(
            self,
            "RedisEndpoint",
            value=self.redis_cluster.attr_primary_end_point_address,
            description="Redis primary endpoint",
            export_name=f"{self.stack_name}-RedisEndpoint",
        )

        CfnOutput(
            self,
            "RedisPort",
            value=self.redis_cluster.attr_primary_end_point_port,
            description="Redis port",
            export_name=f"{self.stack_name}-RedisPort",
        )

        if cluster_size > 1:
            CfnOutput(
                self,
                "RedisReaderEndpoint",
                value=self.redis_cluster.attr_reader_end_point_address,
                description="Redis reader endpoint",
                export_name=f"{self.stack_name}-RedisReaderEndpoint",
            )

    def _get_instance_type_for_env(self, environment: str) -> str:
        """
        Get the appropriate Redis instance type based on environment.
        """
        if environment == "prod":
            return "cache.r6g.large"  # Memory optimized instances for production
        elif environment == "staging":
            # One small node (Stream I, I-1d): staging exists to rehearse
            # deploys, not Redis failover, and 2x cache.m6g.large was about
            # 40% of its hourly cost. Pinned by
            # infrastructure/tests/test_redis_sizing.py.
            return "cache.t4g.small"
        else:  # dev, test, etc.
            return "cache.t4g.medium"  # Burstable instances for dev/test

    def _get_snapshot_retention_for_env(self, environment: str) -> int:
        """
        Get the appropriate Redis snapshot retention period based on environment.
        """
        if environment == "prod":
            return 7  # 7 days retention in production
        elif environment == "staging":
            return 3  # 3 days retention in staging
        else:  # dev, test, etc.
            return 1  # 1 day retention in dev/test
