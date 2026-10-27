# shellcheck shell=bash
# shellcheck disable=SC2034  # every variable here is read by the files that source this one
# lib/names.sh: the names infrastructure/cdk gives what the beta installs (staging's shape,
# the core profile), each beside the source line that makes it. Sourced by controlvar-aws;
# defines variables only. Nothing else under deploy/aws-installer/ spells one of these names,
# and infrastructure/tests/test_installer_names.py compares each with a real staging-core
# synth.
#
# lib/sweep.sh's kept list reads some of them. Each value is exact: no pattern character
# (* ? [) may appear in one (test_installer_sweep_static.py).

# The nine stacks: app.py's construct ids (experimentation-<part>-<env>), in the order
# the AWS guide's run listed them.
NAMES_STACKS=(
    "experimentation-auth-staging"
    "experimentation-vpc-staging"
    "experimentation-dynamodb-staging"
    "experimentation-database-staging"
    "experimentation-redis-staging"
    "experimentation-compute-staging"
    "experimentation-monitoring-staging"
    "experimentation-fargate-staging"
    "experimentation-migrations-staging"
)

# The same nine in the order uninstall destroys them, each before every stack it imports
# from or depends on (the AWS guide's teardown order, cdk.md's "Tearing an environment
# down"; infrastructure/tests/test_installer_names.py holds it to the synth's import
# graph and dependencies).
NAMES_DESTROY_ORDER=(
    "experimentation-migrations-staging"
    "experimentation-fargate-staging"
    "experimentation-monitoring-staging"
    "experimentation-compute-staging"
    "experimentation-redis-staging"
    "experimentation-database-staging"
    "experimentation-dynamodb-staging"
    "experimentation-auth-staging"
    "experimentation-vpc-staging"
)

# The network's stack: app.py's experimentation-vpc-<env>.
NAMES_VPC_STACK="experimentation-vpc-staging"

# What every stack id of this app starts with: the stack scan and the cdk list check read
# any stack that does, so a stack this list does not name is still seen.
NAMES_STACK_PREFIX="experimentation-"

# The stack the service watcher reads: app.py's experimentation-fargate-<env>.
NAMES_FARGATE_STACK="experimentation-fargate-staging"

# The variable app.py reads for the profile (its former-name spelling is kept on purpose:
# releases and self-hosters set it), and the first line app.py prints for the core profile
# from a folder with no modules/, after its "[cdk] " prefix (left out here: a bracket is a
# pattern character).
NAMES_PROFILE_VARIABLE="EXPERIMENTLY_PROFILE"
NAMES_PROFILE_LINE="profile: core (modules/infrastructure/cdk/stacks absent, $NAMES_PROFILE_VARIABLE=core)"

# The ECS cluster (names.py's ecs_cluster_name) and the load balancer
# (fargate_service_stack.py, load_balancer_name): both experimentation-<env>.
NAMES_CLUSTER="experimentation-staging"
NAMES_LOAD_BALANCER="experimentation-staging"

# The API's ECS service: fargate_service_stack.py, service_name.
NAMES_API_SERVICE="experimentation-backend-staging"

# The CodeDeploy application (names.py's codedeploy_application_name) and its deployment
# group (fargate_service_stack.py, deployment_group_name).
NAMES_CODEDEPLOY_APPLICATION="experimentation-platform-staging"
NAMES_DEPLOYMENT_GROUP="experimentation-staging"

# The fargate stack's outputs the install reads: TaskSubnets, TaskSecurityGroup and
# ALBDnsName (fargate_service_stack.py, its CloudFormation outputs), and the three others
# it also makes.
NAMES_FARGATE_OUTPUTS=(
    "TaskSubnets"
    "TaskSecurityGroup"
    "ALBDnsName"
    "ECSServiceArn"
    "CodeDeployAppName"
    "DeploymentGroupName"
)

# The database key's description: enhanced_database_stack.py, "KMS key for
# {construct_id} PostgreSQL encryption", the construct id from app.py.
NAMES_DB_KEY_DESCRIPTION="KMS key for experimentation-database-staging PostgreSQL encryption"

# The three DynamoDB tables: dynamodb_tables_stack.py.
NAMES_TABLES=(
    "experimentation-events-staging"
    "experimentation-feature-flags-staging"
    "experimentation-overrides-staging"
)

# The three task-definition families: fargate_service_stack.py (the API),
# dashboard_service.py (the dashboard), names.py's migration_task_family (the migration).
NAMES_FAMILIES=(
    "experimentation-backend-staging"
    "experimentation-dashboard-staging"
    "experimentation-migrate-staging"
)
NAMES_MIGRATE_FAMILY="experimentation-migrate-staging"

# Their log groups: fargate_service_stack.py, dashboard_service.py and
# migration_task_stack.py, /ecs/<family>.
NAMES_LOG_GROUPS=(
    "/ecs/experimentation-backend-staging"
    "/ecs/experimentation-dashboard-staging"
    "/ecs/experimentation-migrate-staging"
)
NAMES_MIGRATE_LOG_GROUP="/ecs/experimentation-migrate-staging"

# The log group ECS makes for the cluster's Container Insights (compute_stack.py turns
# them on), /aws/ecs/containerinsights/<cluster>/performance: no stack holds it, so
# uninstall deletes it (the AWS guide's run did, by hand).
NAMES_CONTAINER_INSIGHTS_LOG_GROUP="/aws/ecs/containerinsights/experimentation-staging/performance"

# The database's credentials secret: enhanced_database_stack.py's
# "{construct_id}-aurora-credentials". CloudFormation may leave it scheduled for deletion.
NAMES_DB_SECRET="experimentation-database-staging-aurora-credentials"

# The container the migration overrides and the API's revision runs:
# migration_task_stack.py's and fargate_service_stack.py's container name.
NAMES_BACKEND_CONTAINER="backend"

# The two secrets the core profile reads: secret_arns.py's secret_name, /<env>/experimentation/
# <name>, for JWT_SECRET_ARN and FIRST_SUPERUSER_PASSWORD_SECRET_ARN.
NAMES_JWT_SECRET="/staging/experimentation/jwt-secret"
NAMES_PASSWORD_SECRET="/staging/experimentation/first-superuser-password"

# The two image repositories: names.py's BACKEND_ECR_REPOSITORY and DASHBOARD_ECR_REPOSITORY.
NAMES_BACKEND_REPOSITORY="experimentation-platform/backend"
NAMES_WEB_REPOSITORY="experimentation-platform/web"

# The alarm topic: monitoring_stack.py.
NAMES_ALARM_TOPIC="experimentation-alerts-staging"

# The cache's replication group: elasticache_redis_stack.py's f"{construct_id}-redis", the
# construct id from app.py. It has one node, <group>-001 (num_cache_clusters 1), whose
# automatic backups AWS names automatic.<group>-001-<yyyy-mm-dd-hh-mm> and removes by
# itself some minutes after the group is deleted (lib/sweep.sh's kept list).
NAMES_REDIS_GROUP="experimentation-redis-staging-redis"
