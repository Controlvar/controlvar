"""ECS task definition for running database migrations."""
from aws_cdk import (
    Stack,
    CfnOutput,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from stacks.database_access import require_database
from stacks.environments import api_app_env, data_removal_policy
from stacks.names import BACKEND_ECR_REPOSITORY, migration_task_family
from stacks.secret_arns import require_secret_arns

# --- The command the task runs -------------------------------------------
# backend/Dockerfile sets WORKDIR /app and copies the repository layout under
# it (`COPY backend/ /app/backend/`, and `modules/ /app/modules/` for the full
# profile), so a path that works from a checkout works here unchanged.  The
# config used to be named "app/db/alembic.ini", which resolves to
# /app/app/db/alembic.ini and does not exist.
IMAGE_WORKDIR = "/app"
ALEMBIC_CONFIG = "backend/app/db/alembic.ini"

#: **Not** raw ``alembic upgrade heads``.  The historical migration chain
#: cannot be replayed from an empty database -- rehearsed in the built full
#: image against a real PostgreSQL, ``alembic -c backend/app/db/alembic.ini
#: upgrade heads`` gets two revisions in and dies:
#:
#:     INFO  [alembic.runtime.migration] Running upgrade  -> 84a772608a6e
#:     INFO  [alembic.runtime.migration] Running upgrade 84a772608a6e -> ba93ceb4d658
#:     psycopg2.errors.DuplicateTable: relation "permissions" already exists
#:
#: `deploy.yml` runs this task *before* the service's CodeDeploy deployment, so
#: nothing has created the schema yet and the FIRST deploy of an environment
#: meets exactly that empty database.  No deployment has ever been made from
#: this repository, so that is the next one.
#:
#: ``bootstrap`` is what `backend/docker-entrypoint.sh` already runs, and it
#: handles both states: schema from the models plus stamped heads on a fresh
#: database, `alembic upgrade heads` on an existing one.  Verified in the full
#: image against a fresh database -- 50 tables, and both heads
#: (`b8c9d0e1f2a3` and `modules_0001_rbac`) recorded in `alembic_version`.
#: It needs FIRST_SUPERUSER_PASSWORD, which `secrets=` below already supplies.
#:
#: ``db-migrate.yml`` deliberately keeps raw alembic: `current`, and
#: `upgrade`/`downgrade <target>`, are targeted operations on a database that
#: already exists, and bootstrap cannot express a target.  ALEMBIC_CONFIG above
#: is still what that workflow passes to `-c`.
#:
#: backend/tests/unit/infrastructure/test_migration_task_command.py checks that
#: this command, the db-migrate workflow and the deploy workflow all still
#: agree with the image layout.
MIGRATION_COMMAND = [
    "python",
    "-m",
    "backend.app.db.bootstrap",
]

# --- The image this task definition carries ------------------------------
# NOT the image a migration runs. Every migration runs a revision a workflow
# registered for it: `.github/workflows/deploy.yml` registers a revision of
# this family naming the image it just built, BY DIGEST, and runs that
# revision's ARN; `db-migrate.yml` does the same with the image the API is
# serving (`scripts/register_task_definition.sh`, #138). That is how the
# migration and the API end up on the same build and the same **profile** --
# which matters, because the two profiles do not read the same
# `alembic_version`: a core image cannot resolve the `modules_*` row a full
# bootstrap records, and alembic reads every row before it does anything.
#
# This tag used to be `latest`, and the deploy pushed `:latest` so that the
# family's newest revision would pull "the image being deployed". It pulled
# whatever ANY environment or profile had built last: one ECR repository
# serves them all, and ECS resolves a tag at every task start (#71, #138).
# Nothing pushes `:latest` any more. The tag here is the CDK's `bootstrap`, the
# same one the API service's CloudFormation revision names: a tag the pipeline
# never writes, so this revision cannot silently follow a later build.
# infrastructure/tests/test_pipeline_owns_the_image.py asserts that no task
# definition the app synthesises names `latest`.
#
# When the pair *is* mismatched, `backend/app/db/migrations/env.py` answers the
# way `db/bootstrap.py` always did rather than with a traceback: when there is
# nothing for this build to apply it logs a WARNING naming the foreign
# revisions, leaves the rows alone and exits 0; when this build's own
# migrations are *not* all applied it refuses with a message that says which
# case it is -- a full database met by a core image (run the full image of
# that release), or a database a newer release migrated (run that release, or
# restore the pre-upgrade backup) -- and never tells the operator to edit
# `<schema>.alembic_version` (#238). Either way the
# task never half-applies a chain it cannot plan, and the deploy job fails with
# a sentence instead of "Can't locate revision identified by ...".
IMAGE_TAG = "bootstrap"


class MigrationTaskStack(Stack):
    """
    One-shot ECS task definition for Alembic database migrations.

    This stack does NOT create a long-running ECS service. Instead it
    registers a Fargate task definition that can be invoked on-demand
    (e.g. from a CI/CD pipeline or a CodePipeline action) to run
    :data:`MIGRATION_COMMAND`:

        python -m backend.app.db.bootstrap

    Usage in a deployment pipeline: register a revision of
    ``experimentation-migrate-<env>`` naming the image by digest
    (``scripts/register_task_definition.sh``), then run THAT revision's ARN
    (``scripts/run_migration_task.sh``) -- never the bare family, which
    resolves to whatever registered last. The subnets and security group come
    from the Fargate stack's ``TaskSubnets`` / ``TaskSecurityGroup`` outputs.

    The task exits (succeeds or fails) after the migration completes, making
    it easy to detect failures and gate subsequent deployment steps.

    ``db_host`` is the Aurora writer endpoint and ``db_credentials`` the secret
    Aurora generated its master credentials into (``app.py`` passes the
    database stack's ``writer_host`` and ``db_credentials``); synth refuses
    the stack without either (#78). ``ecs_security_group`` is the group the
    task must be run in -- the one the fargate stack admits to Aurora on
    5432 -- and is published as the ``MigrationSecurityGroupId`` output for
    ``run-task``'s ``securityGroups``. ``include_modules`` is the deployment's
    profile and decides whether the full profile's ``AUDIT_HMAC_KEY`` is
    injected.

    :data:`MIGRATION_COMMAND` is the container's **CMD**, and the image's
    ENTRYPOINT runs before it; ``RUN_MIGRATIONS=false`` in the environment is
    what stops that entry point from bootstrapping the schema on its way past.
    Callers that override the command (``.github/workflows/db-migrate.yml``,
    ``deploy.yml``) inherit that environment, which is why a *downgrade*
    override is now only a downgrade.

    This task is the **only** thing that writes the schema on AWS (#298). The
    API task definition (``fargate_service_stack.py``) also sets
    ``RUN_MIGRATIONS=false`` and ``SEED=``, so no API task bootstraps the
    database when it starts: an older API revision started after a newer
    migration -- a replacement, a scale-out, a rollback -- then serves the
    newer schema instead of refusing it, which is correct only for
    backward-compatible migrations. It also means ``cdk deploy`` alone leaves a
    new environment without a schema (``/health`` 200, real requests 500) until
    the first Deploy runs this task.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        ecs_cluster,
        env_name: str = "prod",
        db_host: str = None,
        public_base_url: str = None,
        include_modules: bool = False,
        secret_arns=None,
        db_credentials=None,
        ecs_security_group=None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_database("MigrationTaskStack", db_host, db_credentials)
        secret_arns = require_secret_arns(
            "MigrationTaskStack", secret_arns, include_modules
        )
        if ecs_security_group is None:
            raise ValueError(
                "MigrationTaskStack requires ecs_security_group: the group the "
                "task runs in, which must be the one admitted to Aurora on 5432."
            )

        self.env_name = env_name
        self.ecs_cluster = ecs_cluster
        self.include_modules = include_modules

        # --- ECR Repository ---
        # Reuses the same backend image as the main Fargate service.
        # Migrations run from the same Docker image using a different entrypoint.
        ecr_repo = ecr.Repository.from_repository_name(
            self,
            "BackendECR",
            repository_name=BACKEND_ECR_REPOSITORY,
        )

        # --- Secrets from Secrets Manager ---
        # Not "only the DB password": `python -m alembic -c
        # backend/app/db/alembic.ini ...` imports `backend.app.core.config`
        # (and, on the full profile, `modules.register(hooks)`) before it plans
        # a single revision, and those settings classes VALIDATE every hardened
        # field in staging/production whether or not this task reads it. A
        # missing SECRET_KEY or FIRST_SUPERUSER_PASSWORD is a ValidationError at
        # import, which is every alembic command failing -- see the comments in
        # stacks/fargate_service_stack.py, which names the same secrets.
        #
        # Redis is genuinely not needed: a migration never connects to it, and
        # no Redis setting has such a validator. (Nor does the API task name a
        # Redis secret any more: it gets REDIS_HOST/REDIS_PORT from the Redis
        # stack, #147.)
        #
        # The database credentials are not looked up by name here: they come
        # from `db_credentials`, the secret Aurora was created with (#78).
        #
        # By COMPLETE ARN, the synth inputs app.py validates (stacks/
        # secret_arns.py, #636): the partial ARN a by-name import rendered
        # is one Secrets Manager does not resolve.
        jwt_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "JwtSecret",
            secret_arns["JWT_SECRET_ARN"],
        )
        superuser_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "SuperuserPasswordSecret",
            secret_arns["FIRST_SUPERUSER_PASSWORD_SECRET_ARN"],
        )
        required_secrets = [jwt_secret, superuser_secret]

        audit_secret = None
        if include_modules:
            audit_secret = secretsmanager.Secret.from_secret_complete_arn(
                self,
                "AuditHmacSecret",
                secret_arns["AUDIT_HMAC_KEY_SECRET_ARN"],
            )
            required_secrets.append(audit_secret)

        # --- IAM Task Execution Role ---
        # The execution role is used by the ECS agent to pull the image and
        # inject secrets before the container starts.
        execution_role = iam.Role(
            self,
            "MigrationExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="ECS task execution role for database migrations",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )
        # Allow the execution role to retrieve only the secrets it injects
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret.secret_arn for secret in required_secrets],
            )
        )

        # --- CloudWatch Log Group ---
        # Separate log group from the main service for easy filtering.
        log_group = logs.LogGroup(
            self,
            "MigrationLogs",
            log_group_name=f"/ecs/experimentation-migrate-{env_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            # Migration history survives a teardown in prod. Elsewhere a
            # retained, NAMED log group makes the next deploy of the same
            # environment fail on the name (stacks/environments.py).
            removal_policy=data_removal_policy(env_name),
        )

        # --- ECS Task Definition ---
        # Smaller resource allocation than the main service — migrations are
        # short-lived and not performance-critical.
        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "MigrationTaskDef",
            # One family per environment (#139). It was one family for all of
            # them, so `run-task --task-definition experimentation-migrate`
            # ran whichever environment had registered a revision last.
            family=migration_task_family(env_name),
            cpu=512,            # 0.5 vCPU
            memory_limit_mib=1024,  # 1 GB
            execution_role=execution_role,
            # No separate task_role needed — the migration container only reads
            # from Secrets Manager (handled by the execution role) and connects
            # to PostgreSQL. If the application code ever requires AWS API
            # calls during migrations, add a task_role here.
        )

        self.task_definition.add_container(
            "backend",
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, tag=IMAGE_TAG),
            # `command` is the container's CMD, NOT its entry point: the image's
            # ENTRYPOINT (backend/docker-entrypoint.sh) still runs first and
            # only `exec "$@"`s this at the end. RUN_MIGRATIONS=false below is
            # what keeps that from being a bug -- see the environment.
            command=list(MIGRATION_COMMAND),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="migrate",
                log_group=log_group,
            ),
            environment={
                # The API task's value, from the same rule: `prod` in prod,
                # `staging` everywhere else (stacks/environments.py).
                "APP_ENV": api_app_env(env_name),
                # The migration task builds the SAME settings the API does, so
                # it refuses to start without this for the same reason -- and a
                # migration that cannot import its settings is a deployment that
                # fails after the image is already rolling.
                "PUBLIC_BASE_URL": public_base_url,
                # Where the database IS. Without it every setting that names a
                # host falls back to `localhost`: the entry point's `pg_isready`
                # loop spent DB_WAIT_TIMEOUT=120s against the container itself
                # and exited 1, and had it got past that, alembic would have
                # "migrated" a database that is not there.
                "POSTGRES_SERVER": db_host,
                "POSTGRES_DB": "experimentation",
                "POSTGRES_SCHEMA": "experimentation",
                "POSTGRES_PORT": "5432",
                # The entry point's own bootstrap (prune + `upgrade heads` +
                # reconcile + ensure_first_superuser) must NOT run here: it
                # would apply a migration before this task's command is
                # reached, which for `db-migrate.yml`'s downgrade override
                # means upgrading to heads and then undoing exactly that. The
                # task runs one alembic command -- the one in `command` -- and
                # nothing else. The entry point still waits for the database
                # and still execs the command.
                "RUN_MIGRATIONS": "false",
                "SEED": "",
                # Ensure Python output is flushed immediately so that
                # CloudWatch Logs captures the full migration output even if
                # the task exits quickly.
                "PYTHONUNBUFFERED": "1",
            },
            secrets={
                # The same secret the API service reads, and the one Aurora's
                # master credentials were generated into.
                "POSTGRES_USER": ecs.Secret.from_secrets_manager(
                    db_credentials, field="username"
                ),
                "POSTGRES_PASSWORD": ecs.Secret.from_secrets_manager(
                    db_credentials, field="password"
                ),
                "SECRET_KEY": ecs.Secret.from_secrets_manager(jwt_secret),
                "FIRST_SUPERUSER_PASSWORD": ecs.Secret.from_secrets_manager(
                    superuser_secret
                ),
                **(
                    {
                        "AUDIT_HMAC_KEY": ecs.Secret.from_secrets_manager(
                            audit_secret
                        )
                    }
                    if audit_secret is not None
                    else {}
                ),
            },
            essential=True,
        )

        # --- CloudFormation Outputs ---
        CfnOutput(
            self,
            "MigrationTaskDefinitionArn",
            value=self.task_definition.task_definition_arn,
            description="ARN of the Alembic migration ECS task definition",
            export_name=f"{self.stack_name}-MigrationTaskDefArn",
        )
        CfnOutput(
            self,
            "MigrationTaskFamily",
            value=self.task_definition.family,
            description="Family name of the migration ECS task definition (use :LATEST for most recent)",
            export_name=f"{self.stack_name}-MigrationTaskFamily",
        )
        # The group `aws ecs run-task` must put this task in: the ECS
        # tasks' group, which is the one admitted to Aurora on 5432
        # (fargate_service_stack.py, TasksToDatabaseIngress). A task run in
        # any other group is not admitted to Aurora.
        CfnOutput(
            self,
            "MigrationSecurityGroupId",
            value=ecs_security_group.security_group_id,
            description="Security group to run the migration task in (admitted to Aurora)",
        )
        CfnOutput(
            self,
            "MigrationLogGroup",
            value=log_group.log_group_name,
            description="CloudWatch log group for migration task output",
            export_name=f"{self.stack_name}-MigrationLogGroup",
        )
