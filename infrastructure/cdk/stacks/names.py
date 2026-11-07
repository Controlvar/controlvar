"""Identifiers more than one tree has to agree on.

The ECR repository is the case that motivated this. The deployment guide told
operators to create `experimentation-backend` and `experimentation-migrations`
before the first deployment, and **nothing** used either name -- every
producer and consumer says `experimentation-platform/backend`. Following the
documentation exactly produced a first deploy that died at `docker push` with
"name unknown" (#211). The name was written down in five documents and two
stacks with nothing tying any of them together.

A name belongs here when more than one tree has to spell it the same way.
`infrastructure/tests/test_ecr_repository_agrees.py` asserts that the CDK, the
deploy workflow and every deployment document still agree about this one, so a
rename that misses a caller fails a test rather than a deployment.

The per-environment names below (#139, #142) are here because the stacks and
the deploy workflows both spell them. Every one carries the environment: two
environments in one account must never claim the same physical name, which
`infrastructure/tests/test_environments_do_not_collide.py` asserts over every
resource type the app synthesises.
"""

from __future__ import annotations

#: The one container repository this project pushes to and runs from. The API
#: service and the migration task deliberately share it -- migrations run the
#: same image with a different command, so a second repository would be a
#: second thing to keep in step for no gain.
#:
#: Nothing in the CDK *creates* it. That was tried and reverted: a registry is
#: account-scoped while these stacks are per-environment, so a fixed name in a
#: per-environment stack means only one environment per account can deploy
#: (a demo environment is `ENVIRONMENT=demo cdk deploy --all`), and
#: `RemovalPolicy.RETAIN` plus an explicit name makes `cdk deploy` fail
#: outright wherever the repository already exists -- including for the
#: operator who hit #211 and created it by hand to unblock themselves.
#: Creating it belongs in a once-per-account stack, not here.
BACKEND_ECR_REPOSITORY = "experimentation-platform/backend"

#: The dashboard's repository (#69): `frontend/Dockerfile`'s static nginx
#: image. Imported by name for the same reasons as the backend's, and created
#: by hand once per account, with a `bootstrap` image pushed to it, before the
#: first `cdk deploy` that includes the dashboard service
#: (docs/deployment/deployment-guide.md section 1.3).
DASHBOARD_ECR_REPOSITORY = "experimentation-platform/web"

#: The CDK context key naming the API target group that is LIVE -- the one
#: CodeDeploy's most recent deployment left serving traffic -- and its
#: default. The HTTPS listener's API path rules forward to it.
#:
#: CloudFormation cannot know which of blue and green that is: CodeDeploy
#: swaps them on every deployment, outside CloudFormation. After an odd number
#: of deployments green is live and blue is empty, and a `cdk deploy` that
#: wrote the rules against blue would send every API request to an empty
#: target group. `scripts/check_live_target_group.py` reads the live one from
#: the running environment (read-only) and refuses a mismatch with this value.
#: The script carries its own copy of both; a unit test asserts they agree.
API_LIVE_TARGET_GROUP_CONTEXT = "api_live_target_group"
API_LIVE_TARGET_GROUP_DEFAULT = "blue"


# --- Per-environment names (#139, #142) --------------------------------------


def ecs_cluster_name(env_name: str) -> str:
    """The ECS cluster, ``experimentation-<env>``.

    It was ``experimentation-dev`` in every environment: the compute stack read
    the CDK context key ``env``, which nothing sets, instead of ``ENVIRONMENT``
    (#142). Staging and prod therefore both built a cluster named for dev, and
    the second of them to deploy in an account failed on the name.
    """
    return f"experimentation-{env_name}"


def codedeploy_application_name(env_name: str) -> str:
    """The CodeDeploy application, ``experimentation-platform-<env>``.

    It was ``experimentation-platform`` everywhere: an account-scoped name, so a
    second environment in the same account could not be created at all (#139).
    """
    return f"experimentation-platform-{env_name}"


def migration_task_family(env_name: str) -> str:
    """The migration task definition family, ``experimentation-migrate-<env>``.

    One family shared by every environment meant ``--task-definition
    experimentation-migrate`` resolved to whichever environment registered a
    revision last -- staging's migration could run prod's task definition.
    """
    return f"experimentation-migrate-{env_name}"


def glue_names(env_name: str) -> dict[str, str]:
    """The ``etl`` module's Glue names, keyed by the API setting that reads each.

    Glue job, crawler and database names are account-scoped, and these were
    literal (``experimentation-events-etl`` ...), so dev and staging in one
    account collided on all four. The API finds them through the settings named
    by the keys (``modules/backend/app/settings.py``); the Fargate stack sets
    those variables on the task from this same function, so the stack that
    creates a name and the service that calls it cannot disagree. The database
    uses an underscore: Athena does not accept a hyphen in a database name.
    """
    return {
        "GLUE_ETL_JOB_NAME": f"experimentation-events-etl-{env_name}",
        "GLUE_METRICS_JOB_NAME": f"experimentation-metrics-etl-{env_name}",
        "GLUE_DATABASE": f"experimentation_{env_name}",
        "GLUE_CRAWLER_NAME": f"experimentation-crawler-{env_name}",
    }


def counters_table_name(env_name: str) -> str:
    """The ``counters`` module's DynamoDB table, ``experiment-counters-<env>`` (#392).

    The counters stack creates the table under this name, and the Fargate
    stack gives the API task the same name (``DYNAMODB_COUNTERS_TABLE``) and
    its access to it, both from this function. A plain string rather than the
    table's token: a token would become a CloudFormation export, and an
    imported export pins the counters stack in place.
    """
    return f"experiment-counters-{env_name}"


#: The target groups the API's CodeDeploy deployment group swaps between, in
#: the order the stack builds its 5xx alarms (#148).
API_TARGET_GROUP_COLOURS = ("blue", "green")


def api_5xx_alarm_name(env_name: str, colour: str) -> str:
    """The API's 5xx alarm on one target group, ``experimentation-api-5xx-<colour>-<env>``.

    The deployment group names both (#148), and ``deploy.yml`` names them in
    its summary and copy; ``test_workflow_names_exist.py`` looks each up in a
    synth. Account-scoped, so it carries the environment.
    """
    if colour not in API_TARGET_GROUP_COLOURS:
        raise ValueError(f"colour must be one of {API_TARGET_GROUP_COLOURS}: {colour!r}")
    return f"experimentation-api-5xx-{colour}-{env_name}"


def api_healthy_alarm_name(env_name: str, colour: str) -> str:
    """The API's healthy-task alarm on one target group, ``experimentation-api-healthy-<colour>-<env>``.

    In ALARM while that target group has had no healthy target for three
    minutes. The target group that is not live has no targets between
    deployments, so its alarm is in ALARM by design: neither alarm has an
    action, and neither may join the deployment group, every alarm of which
    ``scripts/refuse_alarm_active.py`` requires to be OK before a deploy.
    """
    if colour not in API_TARGET_GROUP_COLOURS:
        raise ValueError(f"colour must be one of {API_TARGET_GROUP_COLOURS}: {colour!r}")
    return f"experimentation-api-healthy-{colour}-{env_name}"


def api_no_healthy_task_alarm_name(env_name: str) -> str:
    """The composite that emails when NEITHER target group has a healthy API task."""
    return f"experimentation-api-no-healthy-task-{env_name}"


def api_error_logs_alarm_name(env_name: str) -> str:
    """The alarm on error- and critical-level lines in the API's log group."""
    return f"experimentation-api-error-logs-{env_name}"


def api_audit_write_failed_alarm_name(env_name: str) -> str:
    """The alarm on one audit-log entry that could not be written (#834).

    ``docs/api/audit-logging.md`` and the AWS CDK deployment page name it; a
    backend docs test asserts both pages spell what this returns.
    """
    return f"experimentation-api-audit-write-failed-{env_name}"


# --- The API's log-line metrics (#205, #811, #834) -----------------------------
# The fargate stack's metric filters publish these from the API's log group;
# the monitoring stack's application dashboard graphs them. Both read them
# from here so the dashboard cannot name a metric no filter publishes.


def api_log_metric_namespace(env_name: str) -> str:
    """``Experimently/<env>``: the namespace of the log-line metrics."""
    return f"Experimently/{env_name}"


#: Error- and critical-level lines from the API's tasks.
API_ERROR_LOG_LINES_METRIC = "ApiErrorLogLines"

#: Lines saying an audit-log entry could not be written.
AUDIT_WRITE_FAILURES_METRIC = "AuditWriteFailures"

#: The logger that writes the "Failed to create audit log" lines:
#: ``logging.getLogger(__name__)`` in ``backend/app/services/audit_service.py``.
#: The audit-write filter matches this logger name and the line's level, not
#: the message text. ``backend/tests/unit/services/test_audit_write_failed_line.py``
#: renders the real lines and checks them against this constant.
AUDIT_SERVICE_LOGGER = "backend.app.services.audit_service"

#: The text every such line starts with: what the alarm's description and the
#: audit-logging page tell an operator to search the log group for.
AUDIT_WRITE_FAILED_SEARCH = "Failed to create audit log"


#: The SSM parameter the database stack writes the Aurora cluster's identifier
#: to (``{"Ref": <cluster>}``) and the monitoring stack's AuroraHighCPU alarm
#: reads at deploy time. A parameter rather than a CloudFormation export: an
#: export pins the database stack (CloudFormation refuses to change or remove
#: an export another stack imports), and the identifier is generated by
#: CloudFormation, so nothing else can spell it. ``app.py`` makes the
#: monitoring stack depend on the database stack, so the parameter exists
#: before the alarm is created.
AURORA_CLUSTER_IDENTIFIER_PARAMETER = (
    "/experimentation/{env}/database/aurora-cluster-identifier"
)


def aurora_cluster_identifier_parameter(env_name: str) -> str:
    """``/experimentation/<env>/database/aurora-cluster-identifier``."""
    return AURORA_CLUSTER_IDENTIFIER_PARAMETER.format(env=env_name)
