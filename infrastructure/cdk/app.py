#!/usr/bin/env python3

import importlib.util
import os
from pathlib import Path

from aws_cdk import App, Environment

from stacks.vpc_stack import VpcStack
from stacks.compute_stack import ComputeStack
from stacks.monitoring_stack import MonitoringStack
from stacks.enhanced_database_stack import EnhancedDatabaseStack
from stacks.dynamodb_tables_stack import DynamoDBTablesStack
from stacks.elasticache_redis_stack import (
    ElastiCacheRedisStack,
)  # Import the Redis stack
from stacks.authentication_stack import AuthenticationStack
from stacks.fargate_service_stack import FargateServiceStack
from stacks.migration_task_stack import MigrationTaskStack
from stacks.environments import nat_gateway_count
from stacks.secret_arns import resolve_secret_arns

# ---------------------------------------------------------------------------
# The modules' stacks (modules/infrastructure/cdk/stacks, issue #89)
# ---------------------------------------------------------------------------
# The real-time counters table (P2-B), the analytics data lake (Kinesis +
# Firehose + OpenSearch) and the Glue ETL jobs (P3-A) belong to the counters
# and etl modules: their stacks live under modules/, which a core checkout
# does not have.
#
# Which profile this checkout deploys is decided the same way the application
# decides it (backend/app/modules_loader.py): by whether the modules are
# THERE.  A full checkout has modules/infrastructure/cdk/stacks and gets the
# three stacks; a core checkout has no modules/ and gets neither the stacks
# nor the API routes that would need them.  It was an environment variable
# (ENABLE_MODULE_STACKS) that nothing in the repository ever set, so
# `cdk deploy --all` on a full checkout silently produced no counters table
# and no Kinesis/OpenSearch/Glue while the API went on reporting `counters`
# and `etl` installed and the bandit scheduler retried DynamoDB every tick.
#
# EXPERIMENTLY_PROFILE -- the same variable the images and the API use --
# overrides the default in either direction: `core` deploys the core set from
# a full checkout, `full` insists on the module stacks and fails if they are
# absent rather than quietly dropping them.
#
# The stacks are loaded by file path so that `cdk synth` never imports the
# `modules` package (whose __init__ pulls in the backend application and its
# dependencies).
_MODULES_STACKS_DIR = Path(__file__).resolve().parents[2] / "modules" / "infrastructure" / "cdk" / "stacks"

_PROFILE = os.environ.get("EXPERIMENTLY_PROFILE", "").strip().lower()
if _PROFILE not in ("", "core", "full"):
    raise SystemExit(
        f"EXPERIMENTLY_PROFILE must be 'core' or 'full', not {_PROFILE!r}"
    )
_MODULES_PRESENT = _MODULES_STACKS_DIR.is_dir()
if _PROFILE == "full" and not _MODULES_PRESENT:
    raise SystemExit(
        f"EXPERIMENTLY_PROFILE=full but {_MODULES_STACKS_DIR} does not exist: "
        "this is a core checkout (modules/ is absent)."
    )
ENABLE_MODULE_STACKS = _MODULES_PRESENT and _PROFILE != "core"


def _load_modules_stack(module_name: str, class_name: str):
    """Import ``<class_name>`` from ``modules/infrastructure/cdk/stacks/<module_name>.py``."""
    path = _MODULES_STACKS_DIR / f"{module_name}.py"
    if not path.exists():
        raise SystemExit(
            f"{path} does not exist, but {_MODULES_STACKS_DIR} does: the "
            "modules' CDK stacks are incomplete."
        )
    spec = importlib.util.spec_from_file_location(f"modules_stacks.{module_name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


print(
    f"[cdk] profile: {'full' if ENABLE_MODULE_STACKS else 'core'} "
    f"(modules/infrastructure/cdk/stacks {'present' if _MODULES_PRESENT else 'absent'}"
    f"{', EXPERIMENTLY_PROFILE=' + _PROFILE if _PROFILE else ''})"
)

if ENABLE_MODULE_STACKS:
    AnalyticsStack = _load_modules_stack("analytics_stack", "AnalyticsStack")
    DynamoDBCountersStack = _load_modules_stack("dynamodb_counters_stack", "DynamoDBCountersStack")
    GlueETLStack = _load_modules_stack("glue_etl_stack", "GlueETLStack")

# Environment determination
VALID_ENVIRONMENTS = ["dev", "staging", "prod", "demo"]
env_name = os.environ.get("ENVIRONMENT", "dev")
if env_name not in VALID_ENVIRONMENTS:
    raise ValueError(
        f"Invalid ENVIRONMENT '{env_name}'. Must be one of: {', '.join(VALID_ENVIRONMENTS)}"
    )

# Demo environment uses smaller instance sizing to reduce cost
is_demo = env_name == "demo"
db_instance_size = "db.t3.small" if is_demo else "db.t3.medium"
cache_node_type = "cache.t3.micro" if is_demo else "cache.t3.small"

# ECS tasks per service, per environment. `fargate_desired_count` used to be
# computed here (1 for demo, 2 otherwise) and passed nowhere, while the stack
# hard-coded 3 -- so every environment ran 3 API tasks.
#
#   API:       staging 2 (DECISIONS D13: the minimum that proves multi-task
#              behaviour, #66); demo 1 and dev 2 as this file already
#              intended; prod 3, unchanged -- nothing has decided to reduce it.
#   dashboard: staging 1 (D13; a rolling deploy at min 100% / max 200% has no
#              gap even at one task); demo and dev 1. prod 2 is the team's
#              recommendation (one per AZ) and the founder's decision to make:
#              a default here, never a required setting.
API_DESIRED_COUNT = {"dev": 2, "staging": 2, "prod": 3, "demo": 1}
DASHBOARD_DESIRED_COUNT = {"dev": 1, "staging": 1, "prod": 2, "demo": 1}

# Define CDK environment (account and region)
# Account/region come from the environment only: CDK sets CDK_DEFAULT_ACCOUNT
# from the active credentials; AWS_ACCOUNT_ID is the fallback an operator can
# export. No account id is hard-coded in the repository.
account = os.environ.get("CDK_DEFAULT_ACCOUNT") or os.environ.get("AWS_ACCOUNT_ID")
region = os.environ.get("CDK_DEFAULT_REGION") or os.environ.get("AWS_REGION", "us-west-2")
if not account:
    raise SystemExit(
        "AWS account id not set: export AWS_ACCOUNT_ID (or let the CDK CLI set "
        "CDK_DEFAULT_ACCOUNT from your credentials) before synthesizing."
    )
env = Environment(account=account, region=region)

# The three secrets the API and migration task definitions read, by their
# COMPLETE ARN (#636): JWT_SECRET_ARN, FIRST_SUPERUSER_PASSWORD_SECRET_ARN and,
# for the full profile, AUDIT_HMAC_KEY_SECRET_ARN. Required in every
# environment; a partial ARN (no suffix) is what ECS cannot resolve, so there
# is no fallback to the secret's name. stacks/secret_arns.py has what it
# refuses and the read-only command that prints each value.
secret_arns = resolve_secret_arns(
    os.environ,
    env_name=env_name,
    account=account,
    region=region,
    include_modules=ENABLE_MODULE_STACKS,
)

app = App()

# The absolute origin users reach the dashboard and the API at -- one origin,
# e.g. https://app.example.com (DECISIONS D14).
# Required at synth by FargateServiceStack -- the application refuses to start
# in staging/production without knowing what hostname it answers on, because
# the Host header is attacker-controlled (#220). Both the service and the
# migration task get it: they build the same settings, and a migration that
# cannot import its settings fails a deployment after the image is rolling.
# With `-c cognito_hosted_ui=true` the auth stack registers the dashboard's
# sign-in callback and sign-out pages on it too.
public_base_url = os.environ.get("PUBLIC_BASE_URL", None)

# Create the authentication stack. Dashboard sign-in through the Cognito
# Hosted UI is off unless `-c cognito_hosted_ui=true` (#708); off, this stack
# and the API task are what they were before that key existed.
auth_stack = AuthenticationStack(
    app,
    f"experimentation-auth-{env_name}",
    environment=env_name,
    public_base_url=public_base_url,
    env=env,
)

# Create the networking stack (VPC, subnets, etc.)
vpc_stack = VpcStack(
    app,
    f"experimentation-vpc-{env_name}",
    environment=env_name,
    # Two in prod (one per AZ), one elsewhere (DECISIONS D7).
    nat_gateways=nat_gateway_count(env_name),
    env=env,
)

# Create the DynamoDB tables stack
dynamodb_stack = DynamoDBTablesStack(
    app, f"experimentation-dynamodb-{env_name}", environment=env_name, env=env
)

# P2-B: Real-time counters DynamoDB table (counters module)
if ENABLE_MODULE_STACKS:
    dynamodb_counters_stack = DynamoDBCountersStack(
        app,
        f"experimentation-dynamodb-counters-{env_name}",
        environment=env_name,
        env=env,
    )

# Create the enhanced database stack (with improved Aurora PostgreSQL)
database_stack = EnhancedDatabaseStack(
    app,
    f"experimentation-database-{env_name}",
    vpc=vpc_stack.vpc,
    environment=env_name,
    env=env,
)
database_stack.add_dependency(vpc_stack)

# Create the ElastiCache Redis stack
redis_stack = ElastiCacheRedisStack(
    app,
    f"experimentation-redis-{env_name}",
    vpc=vpc_stack.vpc,
    environment=env_name,
    env=env,
)
redis_stack.add_dependency(vpc_stack)

# Create the compute stack (ECS, Lambda)
compute_stack = ComputeStack(
    app,
    f"experimentation-compute-{env_name}",
    vpc=vpc_stack.vpc,
    env_name=env_name,
    env=env,
)
compute_stack.add_dependency(vpc_stack)
compute_stack.add_dependency(database_stack)
compute_stack.add_dependency(dynamodb_stack)
compute_stack.add_dependency(redis_stack)  # Add dependency on Redis stack

# Create the analytics stack (Kinesis, OpenSearch) -- etl module
analytics_stack = None
if ENABLE_MODULE_STACKS:
    analytics_stack = AnalyticsStack(
        app,
        f"experimentation-analytics-{env_name}",
        vpc=vpc_stack.vpc,
        env_name=env_name,
        env=env,
    )
    analytics_stack.add_dependency(vpc_stack)
    analytics_stack.add_dependency(dynamodb_stack)

# Create the monitoring stack (CloudWatch, Alarms)
#
# The Kinesis widget and the iterator-age alarm describe the analytics stack's
# event stream, so they are only created when that stack is. In a core
# deployment there is no stream: the alarm would sit in INSUFFICIENT_DATA for
# ever and the dashboard would show an empty graph. The stream NAME comes from
# the analytics stack rather than being spelled out again here -- it names
# itself `exp-events-<id>`, never the `experimentation-events` this stack used
# to watch.
monitoring_stack = MonitoringStack(
    app,
    f"experimentation-monitoring-{env_name}",
    vpc=vpc_stack.vpc,
    events_stream_name=(
        analytics_stack.events_stream.stream_name if analytics_stack else None
    ),
    env_name=env_name,
    # ALARM_EMAIL: the one address every alarm emails, the rollback alarms
    # included (DECISIONS D21). Required for staging and prod, optional for dev
    # and demo; stacks/alarm_email.py has what it refuses.
    alarm_email=os.environ.get("ALARM_EMAIL"),
    # The per-node Redis CPU alarms (#390): the replication group's id is a
    # literal, so this is a plain string and no cross-stack import.
    redis_replication_group_id=redis_stack.replication_group_id,
    env=env,
)
monitoring_stack.add_dependency(vpc_stack)
# AuroraHighCPU reads the cluster identifier from the SSM parameter the
# database stack creates (#390), resolved when this stack deploys, so the
# parameter has to exist first. Not an export: nothing pins the database stack.
monitoring_stack.add_dependency(database_stack)
if analytics_stack is not None:
    monitoring_stack.add_dependency(analytics_stack)

# ---------------------------------------------------------------------------
# EP-019: Production Deployment — ECS Fargate + ALB + Blue/Green + Migrations
# ---------------------------------------------------------------------------

# Optional: supply an ACM certificate ARN via the CERTIFICATE_ARN environment
# variable. Without this the HTTPS listeners cannot be created; the parameter
# defaults to None which produces a listener without a certificate (suitable
# only for development/testing stacks where certificate validation is not
# required).
certificate_arn = os.environ.get("CERTIFICATE_ARN", None)

# Fargate service stack: ALB, blue/green CodeDeploy, auto-scaling
fargate_stack = FargateServiceStack(
    app,
    f"experimentation-fargate-{env_name}",
    vpc=vpc_stack.vpc,
    ecs_cluster=compute_stack.ecs_cluster,
    ecs_security_group=compute_stack.ecs_security_group,
    env_name=env_name,
    certificate_arn=certificate_arn,
    public_base_url=public_base_url,
    # The profile decides which secrets the task definition has to inject:
    # AUDIT_HMAC_KEY is read only by the modules, and naming a secret that was
    # never created stops ECS from starting the task at all.
    include_modules=ENABLE_MODULE_STACKS,
    secret_arns=secret_arns,
    api_desired_count=API_DESIRED_COUNT[env_name],
    dashboard_desired_count=DASHBOARD_DESIRED_COUNT[env_name],
    # Where Aurora is and how to log in to it (#78, #146): this environment's
    # writer endpoint, and the secret Aurora generated its master credentials
    # into -- not a hand-made copy that can drift from what the cluster has.
    # The security group is so the stack can admit the tasks on 5432.
    db_host=database_stack.writer_host,
    db_credentials=database_stack.db_credentials,
    db_security_group=database_stack.rds_security_group,
    # Where Redis is (#147): the replication group's primary endpoint. The task
    # speaks TLS to it (REDIS_SSL, set in the stack); there is no secret.
    redis_host=redis_stack.primary_host,
    redis_port=redis_stack.primary_port,
    # The API's two 5xx alarms roll a deployment back by themselves; they
    # announce it on the monitoring topic (DECISIONS D21). Monitoring depends
    # only on vpc, database and (with the etl module) analytics, none of which
    # depends on this stack, so there is no cycle. The no-healthy-task
    # composite and the error-log alarm (#390, #205) email through it too.
    alarm_topic=monitoring_stack.alerts_topic,
    # None unless `-c cognito_hosted_ui=true`. When on, the API task is given
    # AUTH_PROVIDER=cognito and the pool, client and Hosted UI domain by
    # reference to the auth stack, whose exports this stack then imports
    # (which pins them: see stacks/authentication_stack.py).
    cognito_hosted_ui=auth_stack.hosted_ui,
    env=env,
)
fargate_stack.add_dependency(compute_stack)
fargate_stack.add_dependency(monitoring_stack)
fargate_stack.add_dependency(database_stack)
fargate_stack.add_dependency(redis_stack)
if ENABLE_MODULE_STACKS:
    # The API task is given the counters table's name and access to it (#392);
    # the table is created first. By name, not by reference, so this is the
    # only thing ordering the two stacks.
    fargate_stack.add_dependency(dynamodb_counters_stack)

# Migration task stack: one-shot Fargate task for Alembic migrations
migration_stack = MigrationTaskStack(
    app,
    f"experimentation-migrations-{env_name}",
    ecs_cluster=compute_stack.ecs_cluster,
    env_name=env_name,
    # The migration container connects to the same Aurora cluster the service
    # does, with the same credentials; it was given no host at all and so
    # tried localhost. It runs in the ECS tasks' security group, which is the
    # one the fargate stack admits to Aurora.
    db_host=database_stack.writer_host,
    db_credentials=database_stack.db_credentials,
    ecs_security_group=compute_stack.ecs_security_group,
    public_base_url=public_base_url,
    include_modules=ENABLE_MODULE_STACKS,
    secret_arns=secret_arns,
    env=env,
)
migration_stack.add_dependency(fargate_stack)
migration_stack.add_dependency(database_stack)


# P3-A: ETL & Glue Jobs for S3 Data Lake -- etl module (needs the analytics
# stack's data lake bucket, so the two are enabled together)
if ENABLE_MODULE_STACKS:
    glue_etl_stack = GlueETLStack(
        app,
        f"experimentation-glue-etl-{env_name}",
        data_lake_bucket=analytics_stack.data_lake_bucket,
        env_name=env_name,
        env=env,
    )
    glue_etl_stack.add_dependency(analytics_stack)


app.synth()
