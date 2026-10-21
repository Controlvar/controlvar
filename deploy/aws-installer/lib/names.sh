# shellcheck shell=bash
# shellcheck disable=SC2034  # every variable here is read by the files that source this one
# lib/names.sh: the names infrastructure/cdk gives what the beta installs (staging's shape,
# the core profile), each beside the source line that makes it. Sourced by controlvar-aws;
# defines variables only. Nothing else under deploy/aws-installer/ spells one of these names.
#
# lib/sweep.sh's kept list reads them. Each value is exact: no pattern character (* ? [) may
# appear in one (test_installer_sweep_static.py).

# The database key's description: enhanced_database_stack.py:71, "KMS key for
# {construct_id} PostgreSQL encryption", the construct id from app.py:178.
NAMES_DB_KEY_DESCRIPTION="KMS key for experimentation-database-staging PostgreSQL encryption"

# The three DynamoDB tables: dynamodb_tables_stack.py:79, :153 and :208.
NAMES_TABLES=(
    "experimentation-events-staging"
    "experimentation-feature-flags-staging"
    "experimentation-overrides-staging"
)

# The three task-definition families: fargate_service_stack.py:368 (the API),
# dashboard_service.py:121 (the dashboard), names.py:92 (the migration task).
NAMES_FAMILIES=(
    "experimentation-backend-staging"
    "experimentation-dashboard-staging"
    "experimentation-migrate-staging"
)

# The alarm topic: monitoring_stack.py:79.
NAMES_ALARM_TOPIC="experimentation-alerts-staging"
