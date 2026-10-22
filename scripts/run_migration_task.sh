#!/usr/bin/env bash
# Run one migration task by its registered revision ARN, wait for it, print its
# log, and exit with its verdict.
#
#   scripts/run_migration_task.sh <cluster> <task definition ARN> \
#       <network configuration JSON> <overrides JSON> <log group>
#
# Used by deploy.yml and db-migrate.yml.
#
# An ARN with a revision, never a family (QA 1c): a bare family resolves to
# its newest ACTIVE revision, which is whatever registered last -- a
# CloudFormation `bootstrap` revision after a `cdk deploy`, or another run's.
# `run-task`'s overrides cannot change the image (containerOverrides has no
# image field), so the image is chosen by registering a revision, and that
# revision is what runs.
#
# The wait is a loop with its own deadline, not `aws ecs wait tasks-stopped`,
# whose limit (100 polls of 6 s) is shorter than a large migration and whose
# timeout reads as a failure. A migration that outlives the deadline is still
# running: re-running it would start a second one against the same database.
#
# Exit status: 0 the task exited 0; 1 anything else.
set -euo pipefail

if [ "$#" -ne 5 ]; then
  echo "usage: $0 <cluster> <task definition ARN> <network JSON> <overrides JSON> <log group>" >&2
  exit 2
fi
CLUSTER="$1"
TASK_DEFINITION="$2"
NETWORK="$3"
OVERRIDES="$4"
LOG_GROUP="$5"
TIMEOUT="${MIGRATION_TIMEOUT_SECONDS:-1800}"

if ! [[ "$TASK_DEFINITION" =~ ^arn:aws[a-z-]*:ecs:[a-z0-9-]+:[0-9]{12}:task-definition/[A-Za-z0-9_-]+:[0-9]+$ ]]; then
  echo "::error::Refusing to run '${TASK_DEFINITION##*/}': not a registered revision ARN. A family resolves to whatever registered last." >&2
  exit 1
fi

OUT="$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TASK_DEFINITION" \
         --launch-type FARGATE --network-configuration "$NETWORK" \
         --overrides "$OVERRIDES" --output json)"
TASK="$(jq -r '.tasks[0].taskArn // empty' <<<"$OUT")"
if [ -z "$TASK" ]; then
  # Each failure's reason and detail, not its `arn`, which names the account.
  echo "::error::ECS did not start the migration task: $(jq -c '[.failures[]? | {reason, detail}]' <<<"$OUT")"
  exit 1
fi
TASK_ID="${TASK##*/}"
STREAM="migrate/backend/${TASK_ID}"
echo "migration task $TASK_ID started from ${TASK_DEFINITION##*/} (log: $LOG_GROUP $STREAM)"

deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  status="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" \
              --query 'tasks[0].lastStatus' --output text)"
  [ "$status" = "STOPPED" ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "::error title=Migration still running::Migration task $TASK_ID is still running after $(( TIMEOUT / 60 )) minutes (status $status). Do not re-run it: that would start a second migration against the same database. Watch $LOG_GROUP, stream $STREAM."
    exit 1
  fi
  sleep 10
done

echo "--- $LOG_GROUP $STREAM (last 100 lines) ---"
aws logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "$STREAM" \
  --limit 100 --query 'events[].message' --output text || echo "(the log could not be read)"
echo "---"

EXIT_CODE="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" \
               --query "tasks[0].containers[?name=='backend'].exitCode | [0]" --output text)"
if [ -z "$EXIT_CODE" ] || [ "$EXIT_CODE" = "None" ] || [ "$EXIT_CODE" = "null" ]; then
  REASON="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" \
              --query 'tasks[0].stoppedReason' --output text)"
  # A pull failure's reason names the image with its registry host, which
  # carries the account ID; an annotation is public.
  REASON="$(sed -E 's/(^|[^0-9A-Za-z])[0-9]{12}([^0-9A-Za-z]|$)/\1<account>\2/g' <<<"$REASON")"
  echo "::error title=Migration did not run::Migration task stopped before the container ran: $REASON"
  exit 1
fi
if [ "$EXIT_CODE" != "0" ]; then
  echo "::error title=Migration failed::Migration task $TASK_ID exited $EXIT_CODE; its last lines are above ($LOG_GROUP, $STREAM)."
  exit 1
fi
aws ecs list-clusters > /dev/null || true
echo "migration task $TASK_ID exited 0"
