from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    Token,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_elasticloadbalancingv2 as elbv2,
    aws_codedeploy as codedeploy,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    aws_secretsmanager as secretsmanager,
    aws_logs as logs,
)
from constructs import Construct

from stacks.dashboard_service import DashboardService
from stacks.database_access import require_database
from stacks.environments import api_app_env, data_removal_policy
from stacks.names import (
    API_LIVE_TARGET_GROUP_CONTEXT,
    API_LIVE_TARGET_GROUP_DEFAULT,
    API_TARGET_GROUP_COLOURS,
    BACKEND_ECR_REPOSITORY,
    api_5xx_alarm_name,
    api_error_logs_alarm_name,
    api_healthy_alarm_name,
    api_no_healthy_task_alarm_name,
    codedeploy_application_name,
    counters_table_name,
    glue_names,
)
from stacks.secret_arns import require_secret_arns

#: The API 5xx alarms' metric math (#148). `e` is HTTPCode_Target_5XX_Count
#: and `r` is RequestCount, one-minute sums on one target group. The value is
#: the error rate once there are at least 5 errors, and 0 below that; the alarm
#: fires at a rate of 5% or more. Each target 5xx is also a request, so `r` is
#: at least 5 whenever the division happens.
#:
#: Nothing offline validates this string: CDK only warns about an identifier
#: it does not know (infrastructure/tests/test_codedeploy_alarms.py fails on
#: that warning), and CloudWatch checks it at the first PutMetricAlarm, i.e.
#: the first `cdk deploy`. If CloudWatch rejects it, the fallback is
#: count-only: API_5XX_FALLBACK_EXPRESSION with API_5XX_FALLBACK_THRESHOLD.
API_5XX_EXPRESSION = "IF(FILL(e,0) >= 5, FILL(e,0)/r, 0)"
API_5XX_RATE_THRESHOLD = 0.05
API_5XX_FALLBACK_EXPRESSION = "FILL(e,0)"
API_5XX_FALLBACK_THRESHOLD = 5

#: HTTPS listener rules that send the API's paths to its live target group.
#: Everything else falls to the listener's default action, the dashboard.
#:
#: The API's only routes outside `/api/v1` are `/health`, `/health/live`,
#: `/health/ready` and `/metrics` (backend/app/core/health.py); WebSockets
#: live under `/api/v1/ws`. ALB quotas: at most five condition values per
#: rule and at most three match evaluations per condition, so the second rule
#: is at the three-value limit. `/health*` is not a shorter spelling of it: it
#: also matches `/healthz` (the dashboard's static liveness path) and anything
#: else that merely starts with `/health`. ALB path patterns are
#: case-sensitive.
API_PATH_RULES = (
    (10, ("/api/*",)),
    (11, ("/health", "/health/*", "/metrics")),
)


class FargateServiceStack(Stack):
    """
    ECS Fargate service with blue/green CodeDeploy deployment.

    Provides:
    - ALB with HTTPS listener and HTTP->HTTPS redirect
    - Blue/green target groups for zero-downtime deployments
    - ECS Fargate service with CodeDeploy controller
    - Auto-scaling based on CPU and memory utilization
    - Secrets Manager integration for sensitive configuration

    Prerequisites:
    - An ACM certificate must exist for the HTTPS listener. Set the
      CERTIFICATE_ARN environment variable or pass certificate_arn to the
      constructor. Without it this stack refuses to build -- at ``cdk synth``,
      not at deploy, which is what this note used to say: both listeners are
      HTTPS and CDK validates that at the end of synthesis. A synth-only check
      may pass any well-formed ARN; nothing resolves it until a deployment.
    - The Secrets Manager secrets listed in docs/deployment/README.md must
      exist. ECS cannot start a task whose task definition names a secret that
      is not there, and the application cannot start without their values.
      The database credentials are NOT among them: they come from the secret
      the database stack generated for Aurora (``db_credentials``).

    ``db_host``, ``db_credentials`` and ``db_security_group`` are the database
    stack's writer endpoint, generated credentials secret and security group
    (#78). Synth refuses to build the stack without them; the security group is
    what this stack opens to the ECS tasks on 5432.

    ``redis_host`` and ``redis_port`` are the Redis stack's primary endpoint
    (#147). Synth refuses to build the stack without them: the application
    falls back to ``localhost`` and "degrades gracefully", so a missing host is
    an outage of rate limiting and caching that no probe reports.

    ``include_modules`` is the deployment's profile (``app.py`` passes
    ``ENABLE_MODULE_STACKS``). ``True`` adds the full profile's own secret,
    ``AUDIT_HMAC_KEY``; see the comment beside it.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc,
        ecs_cluster,
        ecs_security_group,
        env_name: str = "prod",
        certificate_arn: str = None,
        public_base_url: str = None,
        include_modules: bool = False,
        secret_arns=None,
        api_desired_count: int = 3,
        dashboard_desired_count: int = 1,
        db_host: str = None,
        db_credentials=None,
        db_security_group=None,
        redis_host: str = None,
        redis_port: str = None,
        alarm_topic=None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_database("FargateServiceStack", db_host, db_credentials)
        secret_arns = require_secret_arns(
            "FargateServiceStack", secret_arns, include_modules
        )
        if alarm_topic is None:
            raise ValueError(
                "FargateServiceStack requires alarm_topic: the monitoring "
                "stack's topic (monitoring_stack.alerts_topic). The API's two "
                "5xx alarms roll a deployment back by themselves, and without "
                "an action on the topic that rollback tells nobody."
            )
        if db_security_group is None:
            raise ValueError(
                "FargateServiceStack requires db_security_group: Aurora's "
                "security group (database_stack.rds_security_group), which "
                "has to admit the ECS tasks on 5432 or every connection times "
                "out."
            )
        if not redis_host or not redis_port:
            raise ValueError(
                "FargateServiceStack requires redis_host and redis_port: the "
                "Redis stack's primary endpoint (redis_stack.primary_host, "
                "redis_stack.primary_port). Without them the application "
                "connects to localhost, finds nothing, and silently runs "
                "without rate-limit storage or caching (#147)."
            )
        # PUBLIC_BASE_URL is the URL users reach this service at, e.g.
        # https://api.example.com. It is required at SYNTH, like CERTIFICATE_ARN and
        # for a related reason: the application refuses to start in staging or
        # production without knowing what hostname it answers on, because the Host
        # header is attacker-controlled and absolute URLs the app emits -- the OIDC
        # redirect_uri above all -- were built from it (#220).
        #
        # Deliberately NOT defaulting to the load balancer's DNS name. That would
        # synthesise happily and hand the deployment an allow-list naming a host no
        # user ever sends, refusing 100% of traffic while every health check stayed
        # green, because the probes are exempt. A loud failure here beats an outage
        # that monitoring calls fine.
        if not public_base_url:
            raise ValueError(
                "FargateServiceStack requires the public base URL: set the "
                "PUBLIC_BASE_URL environment variable (or pass public_base_url) "
                "to the absolute https:// origin users reach this service at, "
                "e.g. https://api.example.com. The application refuses to start "
                "in staging/production without it."
            )


        self.env_name = env_name
        self.include_modules = include_modules

        # --- ECR Repository ---
        self.ecr_repo = ecr.Repository.from_repository_name(
            self,
            "BackendECR",
            repository_name=BACKEND_ECR_REPOSITORY,
        )

        # --- Secrets from Secrets Manager ---
        # These secrets must be created manually (or by another stack) before
        # deploying this stack. The secret paths follow the convention:
        #   /<env>/experimentation/<secret-name>
        #
        # The database password is not one of them (#78, #146). It was
        # `/<env>/experimentation/db-password`, a secret a human created by
        # hand and nothing ever set on the cluster, so the task would have
        # presented a password Aurora had never heard of. The credentials now
        # come from `db_credentials`, the secret Aurora was created with.
        #
        # Each is imported by its COMPLETE ARN, the synth input app.py validates
        # (stacks/secret_arns.py, #636). Importing by name rendered a
        # partial ARN into valueFrom, which Secrets Manager does not resolve,
        # so no task would have started.
        jwt_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "JwtSecret", secret_arns["JWT_SECRET_ARN"]
        )
        # Every secret below is one the container REFUSES TO START without in a
        # hardened environment, and the task definition is the only thing that
        # can supply it: the image ships no .env file (.dockerignore keeps
        # .env.* out of the build context).
        #
        # FIRST_SUPERUSER_PASSWORD defaults to "admin", which
        # `Settings.validate_superuser_password` rejects in staging/production
        # -- so `import backend.app.core.config` raised ValidationError and
        # uvicorn never bound, on BOTH profiles.
        superuser_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "SuperuserPasswordSecret",
            secret_arns["FIRST_SUPERUSER_PASSWORD_SECRET_ARN"],
        )
        # There is no Redis secret (#147). The one this used to inject held a
        # connection URL in a variable nothing in the application reads. The
        # connection is
        # REDIS_HOST/REDIS_PORT/REDIS_SSL in the environment below, taken from
        # the Redis stack, and the replication group has no AUTH token.
        required_secrets = [jwt_secret, superuser_secret]

        # AUDIT_HMAC_KEY is the full profile's: `modules.register(hooks)` builds
        # ModulesSettings as its first step and its validator rejects the dev
        # default in staging/production, so a full image without this secret
        # fails the registration -- which `abort_if_modules_broken()` turns into
        # a refusal to start, and which kills every alembic command too
        # (migrations/env.py calls require_modules_or_absent()).
        #
        # Only for the full profile: a core image never reads it, and naming a
        # secret that does not exist stops ECS from starting the task at all.
        audit_secret = None
        if include_modules:
            audit_secret = secretsmanager.Secret.from_secret_complete_arn(
                self,
                "AuditHmacSecret",
                secret_arns["AUDIT_HMAC_KEY_SECRET_ARN"],
            )
            required_secrets.append(audit_secret)

        # --- IAM Task Role ---
        # The task role is assumed by the application code running inside the
        # container. It has least-privilege access to only the secrets it needs.
        task_role = iam.Role(
            self,
            "ECSTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="IAM role for ECS Fargate tasks",
        )
        task_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchLogsFullAccess")
        )
        # Least-privilege Secrets Manager access — only the required secrets
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret.secret_arn for secret in required_secrets],
            )
        )

        # --- IAM Task Execution Role ---
        # The execution role is used by the ECS agent to pull the container
        # image from ECR and inject secrets as environment variables at startup.
        execution_role = iam.Role(
            self,
            "ECSExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )
        # The execution role also needs GetSecretValue so that ECS can inject
        # the secrets as environment variables before the container starts.
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret.secret_arn for secret in required_secrets],
            )
        )

        # --- CloudWatch Log Group ---
        log_group = logs.LogGroup(
            self,
            "BackendLogGroup",
            log_group_name=f"/ecs/experimentation-backend-{env_name}",
            retention=logs.RetentionDays.THREE_MONTHS,
            # Named, so a retained copy blocks the next deploy of the same
            # environment. Kept in prod only (stacks/environments.py).
            removal_policy=data_removal_policy(env_name),
        )

        # --- The image this task definition carries ---------------------------
        # The pipeline owns what actually runs.
        # `.github/workflows/deploy.yml` reads this family's newest revision,
        # rewrites the backend container's image to the one it just built (by
        # digest), registers that as a new revision and hands it to
        # CodeDeploy. CloudFormation never gets to choose: ECS
        # refuses a task-definition change on a service with a CODE_DEPLOY
        # controller outright -- "Unable to update task definition on services
        # with a CODE_DEPLOY deployment controller".
        #
        # So the tag below is a *bootstrap* image: the one thing that has to
        # exist before an environment can be stood up at all, because
        # CloudFormation cannot create an ECS service without a task
        # definition, and a task definition cannot name no image.
        #
        # It is deliberately not `latest`, and the reason is narrower than it
        # first looks -- this comment was rewritten after reading the ECS API
        # model rather than reasoning from the template.
        #
        # What is NOT true: that a `cdk deploy` can roll the running service
        # back. ECS refuses a task-definition change through `UpdateService`
        # on a CODE_DEPLOY service, and CodeDeploy deployments name a revision
        # ARN outright, so nothing here decides what serves traffic.
        #
        # What IS true: this revision is the one `Service.taskDefinition`
        # keeps pointing at forever. That field is "specified when the service
        # is created with CreateService, and it can be modified with
        # UpdateService" -- and UpdateService is the call just ruled out -- so
        # for the life of the service it names the revision CloudFormation
        # created, not the one serving traffic (that is
        # `taskSets[?status=='PRIMARY'].taskDefinition`). Anything that reads
        # the service to find "the current task definition" therefore lands
        # here.
        #
        # `:latest` made that a live image: the deploy workflow moved the tag
        # on every build (it no longer pushes it at all, #71), including builds that failed verification or were
        # rolled back, so the revision everyone mistakes for current pointed
        # at an arbitrary later build. A tag the pipeline never writes cannot
        # drift, so the mistake becomes visible instead of plausible.
        #
        # `-c backend_image_tag=<tag>` overrides it, for pinning a `cdk deploy`
        # on a running environment to the image already in service.
        #
        # migration_task_stack.py names `bootstrap` too: the deploy registers
        # its own migration revision by digest, and nothing pushes `:latest`.
        image_tag = self.node.try_get_context("backend_image_tag")
        if image_tag is None:
            image_tag = "bootstrap"
        if not isinstance(image_tag, str) or not image_tag.strip():
            # `-c backend_image_tag=` supplies "", and cdk.json can supply a
            # number. Both used to fall through an `or` to "bootstrap", so an
            # operator who thought they had pinned production got the drifted
            # revision they were trying to avoid, silently.
            raise ValueError(
                "backend_image_tag context must be a non-empty string; got "
                f"{image_tag!r}"
            )
        if image_tag == "latest":
            raise ValueError(
                "backend_image_tag must not be `latest`: any build can move "
                "that tag, which is #82"
            )
        self.backend_image_tag = image_tag

        # --- ECS Task Definition ---
        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "BackendTaskDef",
            family=f"experimentation-backend-{env_name}",
            cpu=1024,  # 1 vCPU
            memory_limit_mib=2048,  # 2 GB
            task_role=task_role,
            execution_role=execution_role,
        )

        self.container = self.task_definition.add_container(
            "backend",
            image=ecs.ContainerImage.from_ecr_repository(
                self.ecr_repo, tag=self.backend_image_tag
            ),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="backend",
                log_group=log_group,
            ),
            environment={
                # `prod` in prod, `staging` everywhere else: no environment runs
                # the API with development settings (stacks/environments.py).
                "APP_ENV": api_app_env(env_name),
                "PUBLIC_BASE_URL": public_base_url,
                "LOG_LEVEL": "INFO",
                "PYTHONUNBUFFERED": "1",
                # This environment's Aurora WRITER endpoint (#78), imported
                # from the database stack. Without it the application falls
                # back to localhost and `/health` -- the ALB's health check,
                # which runs the database check -- never passes.
                "POSTGRES_SERVER": db_host,
                "POSTGRES_DB": "experimentation",
                "POSTGRES_SCHEMA": "experimentation",
                "POSTGRES_PORT": "5432",
                # This environment's ElastiCache primary endpoint (#147). The
                # replication group has in-transit encryption on and refuses a
                # plaintext connection, so every client the application builds
                # must speak TLS: REDIS_SSL is passed as `ssl=` to all of them
                # (backend/tests/unit/core/test_redis_connection.py).
                "REDIS_HOST": redis_host,
                "REDIS_PORT": redis_port,
                "REDIS_SSL": "true",
                # The API never writes the schema (#298). The entry point's
                # default is RUN_MIGRATIONS=true, which bootstraps the database
                # on every task start; here the deploy's migration task
                # (migration_task_stack.py, run by deploy.yml before the
                # CodeDeploy deployment) is the only thing that does. Two
                # reasons:
                #   * an OLD image refuses to start against a schema a newer
                #     release has migrated, so under the default every task an
                #     older revision starts -- a replacement, a scale-out, a
                #     rollback -- exits 1 instead of serving;
                #   * several tasks starting at once would each run the
                #     bootstrap, where one writer is enough.
                # Consequence: `cdk deploy` alone leaves a new environment
                # without a schema (`/health` answers 200, real requests 500)
                # until the first Deploy runs the migration task.
                # SEED is empty for the same reason: nothing but a migration
                # task writes to this database at start-up.
                # infrastructure/tests/test_api_task_does_not_migrate.py pins
                # both values exactly.
                "RUN_MIGRATIONS": "false",
                "SEED": "",
            },
            secrets={
                # Both halves from the secret Aurora generated its master
                # credentials into; ECS grants the execution role read access
                # to it. The generated password can hold any printable
                # character except the four the database stack excludes, so
                # the application percent-encodes it (backend/app/db/url.py).
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
                    {"AUDIT_HMAC_KEY": ecs.Secret.from_secrets_manager(audit_secret)}
                    if audit_secret is not None
                    else {}
                ),
            },
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(60),
            ),
            essential=True,
        )

        self.container.add_port_mappings(
            ecs.PortMapping(container_port=8000, protocol=ecs.Protocol.TCP)
        )

        # --- Application Load Balancer ---
        # The ALB is internet-facing and placed in the public subnets. ECS tasks
        # run in private subnets and receive traffic only through the ALB.
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "BackendALB",
            vpc=vpc,
            internet_facing=True,
            load_balancer_name=f"experimentation-{env_name}",
            # ALB is placed in public subnets by default when internet_facing=True
        )

        # HTTP (port 80) -> HTTPS (port 443) permanent redirect
        self.alb.add_redirect(
            source_port=80,
            source_protocol=elbv2.ApplicationProtocol.HTTP,
            target_port=443,
            target_protocol=elbv2.ApplicationProtocol.HTTPS,
        )

        # --- Blue Target Group (current / stable version) ---
        self.blue_target_group = elbv2.ApplicationTargetGroup(
            self,
            "BlueTargetGroup",
            vpc=vpc,
            port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/health",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
                healthy_http_codes="200",
            ),
            deregistration_delay=Duration.seconds(30),
        )

        # --- Green Target Group (new version during blue/green deployment) ---
        self.green_target_group = elbv2.ApplicationTargetGroup(
            self,
            "GreenTargetGroup",
            vpc=vpc,
            port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/health",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
                healthy_http_codes="200",
            ),
            deregistration_delay=Duration.seconds(30),
        )

        # --- HTTPS Production Listener (port 443) ---
        # An ACM certificate ARN is required. Provision one via AWS Certificate
        # Manager and pass it as the `certificate_arn` constructor parameter or
        # the CERTIFICATE_ARN environment variable; the format is
        # `arn:aws:acm:<region>:<account>:certificate/<uuid>`.
        #
        # Required at SYNTH, not at deploy -- which is what the note here used
        # to say. Both listeners below are HTTPS, and CDK validates at the end
        # of synthesis that an HTTPS listener has a certificate:
        #
        #   ValidationFailedWithErrors: [.../HttpsListener] HTTPS Listener
        #   needs at least one certificate (call addCertificates)
        #
        # So `cdk synth` fails without one, and the guard below says that in
        # those words rather than leaving the reader to map CDK's message back
        # to a missing environment variable. A synth-only check (CI) can pass
        # any well-formed ARN: nothing resolves it until deploy.
        #
        # Deliberately NOT falling back to an HTTP listener when the ARN is
        # absent. That would make `cdk synth` succeed and hand anyone who
        # forgot the variable a load balancer that terminates in plaintext --
        # trading a loud failure for a quiet downgrade, on the public edge of
        # the platform.
        if not certificate_arn:
            raise ValueError(
                "FargateServiceStack requires an ACM certificate: set the "
                "CERTIFICATE_ARN environment variable (or pass certificate_arn) "
                "to an arn:aws:acm:<region>:<account>:certificate/<uuid>. Both "
                "the production and the CodeDeploy test listener are HTTPS, and "
                "cdk synth fails validation without one."
            )

        # --- The dashboard (#69): the HTTPS listener's default action ---
        self.dashboard = DashboardService(
            self,
            "Dashboard",
            vpc=vpc,
            cluster=ecs_cluster,
            env_name=env_name,
            desired_count=dashboard_desired_count,
        )

        https_listener_kwargs = dict(
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            default_target_groups=[self.dashboard.target_group],
            open=True,
        )
        if certificate_arn:
            https_listener_kwargs["certificates"] = [
                elbv2.ListenerCertificate.from_arn(certificate_arn)
            ]

        self.https_listener = self.alb.add_listener(
            "HttpsListener",
            **https_listener_kwargs,
        )

        # --- The API's paths, to the API's LIVE target group ---
        # Which of blue and green is live is decided by CodeDeploy, outside
        # CloudFormation, and swaps on every deployment. These rules therefore
        # forward to the target group the `api_live_target_group` context
        # names (default `blue`, right for a new environment), never to one
        # hard-coded here. Writing them against the wrong one sends every API
        # request to an empty target group while `/` and the dashboard's
        # probes stay green -- so run scripts/check_live_target_group.py
        # before every `cdk deploy` of this stack on a running environment;
        # it reads the live one (read-only) and refuses a mismatch.
        #
        # Whether a CodeDeploy traffic shift rewrites these rules, or only the
        # default action, is not documented by AWS (Stream C OPEN 1). It is a
        # Stream I gate before production; ECS-native blue/green
        # (productionListenerRule) or separate api./app. hosts (D14) are the
        # fallbacks.
        live = self.node.try_get_context(API_LIVE_TARGET_GROUP_CONTEXT)
        if live is None:
            live = API_LIVE_TARGET_GROUP_DEFAULT
        live_target_groups = {
            "blue": self.blue_target_group,
            "green": self.green_target_group,
        }
        if live not in live_target_groups:
            raise ValueError(
                f"{API_LIVE_TARGET_GROUP_CONTEXT} context must be 'blue' or "
                f"'green'; got {live!r}. scripts/check_live_target_group.py "
                "prints the value to pass."
            )
        self.api_live_target_group_name = live
        for priority, paths in API_PATH_RULES:
            self.https_listener.add_target_groups(
                f"ApiPaths{priority}",
                priority=priority,
                conditions=[elbv2.ListenerCondition.path_patterns(list(paths))],
                target_groups=[live_target_groups[live]],
            )

        # --- Test Listener (port 8443) ---
        # CodeDeploy uses this listener to route traffic to the green (new)
        # version during deployment so that health checks can pass before
        # shifting production traffic. Access to the test port is restricted
        # (open=False) so that it is not publicly accessible.
        test_listener_kwargs = dict(
            port=8443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            default_target_groups=[self.green_target_group],
            open=False,
        )
        if certificate_arn:
            test_listener_kwargs["certificates"] = [
                elbv2.ListenerCertificate.from_arn(certificate_arn)
            ]

        self.test_listener = self.alb.add_listener(
            "TestListener",
            **test_listener_kwargs,
        )

        # Imported immutably, and this is the whole fix for the dependency
        # cycle. Passing the ComputeStack's SecurityGroup *object* to the
        # service made `attach_to_application_target_group` below reach back into
        # it: CDK's ApplicationListener.registerConnectable calls
        # `connections.allowFrom(loadBalancer, ...)`, and
        # `determineRuleScope` puts BOTH halves of the rule pair under the
        # initiating group -- the ECS one, in the compute stack --
        # each referencing the ALB's group, which lives here. That
        # is compute -> fargate, against the fargate -> compute dependency
        # app.py already declares, and `cdk synth` has refused to complete
        # since EP-019 first added this stack.
        #
        # `mutable=False` makes CDK decline to write rules onto the
        # imported group; the rule it would have written is created
        # explicitly below, in this stack, where it belongs.
        #
        # A local rather than inline in the service, so the egress rule below
        # can name the same peer object. The construct id and path are
        # unchanged, so every logical id derived from it is unchanged too.
        imported_ecs_sg = ec2.SecurityGroup.from_security_group_id(
            self,
            "ImportedEcsSecurityGroup",
            ecs_security_group.security_group_id,
            mutable=False,
        )

        # --- ECS Fargate Service (CodeDeploy deployment controller) ---
        # Using CODE_DEPLOY controller disables rolling updates managed by ECS
        # and hands deployment control entirely to CodeDeploy, enabling the
        # blue/green strategy defined below.
        self.fargate_service = ecs.FargateService(
            self,
            "BackendService",
            service_name=f"experimentation-backend-{env_name}",
            cluster=ecs_cluster,
            task_definition=self.task_definition,
            # Per environment, from app.py (DECISIONS D13: staging runs 2).
            desired_count=api_desired_count,
            min_healthy_percent=100,
            max_healthy_percent=200,
            security_groups=[imported_ecs_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            deployment_controller=ecs.DeploymentController(
                type=ecs.DeploymentControllerType.CODE_DEPLOY
            ),
            health_check_grace_period=Duration.seconds(60),
            # ECS Exec enables interactive debugging sessions on running tasks
            # via `aws ecs execute-command`. Disable in high-security environments.
            enable_execute_command=True,
        )

        # Attach the service to the blue target group. CodeDeploy will manage
        # shifting traffic between blue and green during deployments.
        self.fargate_service.attach_to_application_target_group(self.blue_target_group)

        # The ingress CDK would have added implicitly, written explicitly and on
        # this side of the stack boundary. With the group imported
        # `mutable=False` above, CDK silently declines to create this rule --
        # no warning, no annotation. `compute_stack.py` gives the ECS group no
        # ingress of its own (its old 0.0.0.0/0 rule on 8000 was removed), so
        # without this nothing lets the load balancer in: every `/api/v1` and
        # `/health` request through it fails while the dashboard at `/` keeps
        # working. So the real rule is stated here: this ALB, to the task
        # port, and nothing else.
        ec2.CfnSecurityGroupIngress(
            self,
            "AlbToTasksIngress",
            group_id=ecs_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8000,
            to_port=8000,
            source_security_group_id=self.alb.connections.security_groups[
                0
            ].security_group_id,
            description="Load balancer to target",
        )

        # The other half of that pair: the load balancer's own egress to the
        # task port. Its group has no allow-all outbound rule, so without this
        # the ALB cannot open a connection to the tasks at all (#801).
        #
        # Explicit because the implicit one is not always there.
        # `attach_to_application_target_group` above writes it only through
        # the blue target group's listener, and the blue group sits behind a
        # listener rule only while `api_live_target_group` is blue. A synth
        # with green live dropped the rule, and a green-pinned `cdk diff`
        # against staging planned to destroy it.
        #
        # Why this call and not a hand-written CfnSecurityGroupEgress: CDK
        # derives the rule's construct id from the peer's uniqueId and the
        # port only (`to <peer>:8000`, under the ALB's group), and skips a
        # second rule with the same id regardless of its description. So
        # naming the same `imported_ecs_sg` object yields the logical id the
        # deployed stacks already carry. On blue, the implicit rule from
        # `attach_to_application_target_group` runs first and this call is
        # the one skipped; on green this call writes it. A resource under a
        # new logical id would instead sit beside the deployed rule on blue,
        # and be created before the old one is removed on a deployed stack
        # (expected to fail as a duplicate; not run, nothing here deploys).
        #
        # Because the skip is by id and not by content, the description has
        # to be the literal "Load balancer to target" -- the implicit rule's
        # -- or the rule's properties would change with the live colour.
        # infrastructure/tests/test_alb_egress_to_tasks.py pins the logical
        # id, the properties and the description for every environment,
        # profile and colour.
        self.alb.connections.allow_to(
            imported_ecs_sg, ec2.Port.tcp(8000), "Load balancer to target"
        )

        # The tasks to Aurora, on the same pattern as the rule above and
        # written here for the same kind of reason: this is the one stack that
        # knows both groups. The database stack cannot name the ECS group
        # (compute depends on database), and Aurora's group was created with
        # no ingress at all, so without this every connection from a task
        # times out (#78). The migration task runs in the same ECS group
        # (migration_task_stack.py outputs it), so this one rule admits both.
        ec2.CfnSecurityGroupIngress(
            self,
            "TasksToDatabaseIngress",
            group_id=db_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=5432,
            to_port=5432,
            source_security_group_id=ecs_security_group.security_group_id,
            description="ECS tasks to Aurora PostgreSQL",
        )

        # --- CodeDeploy IAM Role ---
        codedeploy_role = iam.Role(
            self,
            "CodeDeployRole",
            assumed_by=iam.ServicePrincipal("codedeploy.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AWSCodeDeployRoleForECS"
                )
            ],
        )
        # CodeDeploy reads the deployment group's alarms on every poll. The
        # managed policy above is not relied on for that: its contents are not
        # pinned here. A failed read stops the deployment, because
        # `ignore_poll_alarms_failure` stays False below (#148).
        codedeploy_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:DescribeAlarms"],
                resources=["*"],
            )
        )

        # --- The API's 5xx alarms (#148) ---
        # One per target group, because CodeDeploy swaps blue and green on
        # every deployment: an alarm on one group would watch the wrong one
        # half the time. Both are attached to the deployment group below, so
        # both are polled for the whole deployment, the ORIGINAL target
        # group's included: a live release already answering 5xx at the
        # threshold stops (and rolls back) a new deploy too. The runbook's
        # "Fix forward while an alarm is firing" is the way through that.
        #
        # The metrics are built by hand, not with `tg.metrics.*`: those need
        # the target group attached to a load balancer, and the group that is
        # not live is attached to none (synth raises
        # TargetGroupNeedsAttachedLoad when api_live_target_group=green).
        #
        # Target 5xx only: HTTPCode_Target_5XX_Count has a TargetGroup
        # dimension, and the load balancer's own 502/503/504 do not, so a
        # crashed or timed-out task is NOT covered. Every 5xx the application
        # answers counts, its deliberate ones included: 501 on a module route
        # under the core profile, and 503 from /health/ready for a caller that
        # is not the load balancer's health check. Health checks are not
        # requests and do not count.
        #
        # A breaching minute needs at least 5 target 5xx AND at least 5% of
        # that group's requests (API_5XX_EXPRESSION). A minute with no data is
        # NOT_BREACHING, so an idle target group never alarms. Two breaching
        # minutes of three. These are starting values, to be measured against
        # real traffic.
        self.api_5xx_alarms = []
        for colour, target_group in zip(
            API_TARGET_GROUP_COLOURS,
            (self.blue_target_group, self.green_target_group),
        ):
            dimensions = {
                "LoadBalancer": self.alb.load_balancer_full_name,
                "TargetGroup": target_group.target_group_full_name,
            }
            errors = cloudwatch.Metric(
                namespace="AWS/ApplicationELB",
                metric_name="HTTPCode_Target_5XX_Count",
                dimensions_map=dimensions,
                statistic="Sum",
                period=Duration.seconds(60),
            )
            requests = cloudwatch.Metric(
                namespace="AWS/ApplicationELB",
                metric_name="RequestCount",
                dimensions_map=dimensions,
                statistic="Sum",
                period=Duration.seconds(60),
            )
            alarm = cloudwatch.Alarm(
                self,
                f"Api5xx{colour.capitalize()}",
                alarm_name=api_5xx_alarm_name(env_name, colour),
                alarm_description=(
                    f"The API's {colour} target group answers 5xx: at least "
                    "5 target 5xx and at least 5% of its requests in a "
                    "minute, for 2 of 3 minutes. While this is in ALARM, "
                    "CodeDeploy stops and rolls back any API deployment "
                    "(docs/deployment/rollback-runbook.md)."
                ),
                metric=cloudwatch.MathExpression(
                    expression=API_5XX_EXPRESSION,
                    using_metrics={"e": errors, "r": requests},
                    period=Duration.seconds(60),
                    label=f"{colour} target 5xx rate",
                ),
                threshold=API_5XX_RATE_THRESHOLD,
                comparison_operator=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
                ),
                evaluation_periods=3,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            # Announce it (DECISIONS D21): while in ALARM this rolls a
            # deployment back by itself, and without an action nobody is told.
            # ALARM only -- no OK or INSUFFICIENT_DATA action.
            alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))
            self.api_5xx_alarms.append(alarm)

        # --- No healthy API task (#390) ---
        # The 5xx alarms above cannot see this: with no task behind the load
        # balancer it answers 503 itself, the target groups report no target
        # 5xx, and missing data is not breaching. So each target group gets a
        # HealthyHostCount alarm -- in ALARM after three one-minute periods
        # with no healthy target, missing data included -- and one composite
        # emails when BOTH are in ALARM at once.
        #
        # Between deployments one of blue and green is empty, so its alarm is
        # in ALARM by design, all the time. That is why the per-colour alarms
        # have NO action and are NOT in the deployment group:
        # scripts/refuse_alarm_active.py refuses a deploy while any group
        # alarm is in ALARM, and the idle colour's would refuse every one. The
        # composite is what emails; during a canary both colours have healthy
        # targets, so it stays OK. In an environment running one API task, a
        # task replacement leaves no healthy target for a few minutes, and the
        # composite emails for it -- correctly: nothing was serving.
        #
        # Built by hand for the reason given above the 5xx alarms: the
        # target group that is not live is attached to no load balancer, and
        # `tg.metrics.*` refuses it at synth.
        self.api_healthy_alarms = []
        for colour, target_group in zip(
            API_TARGET_GROUP_COLOURS,
            (self.blue_target_group, self.green_target_group),
        ):
            healthy = cloudwatch.Alarm(
                self,
                f"ApiHealthy{colour.capitalize()}",
                alarm_name=api_healthy_alarm_name(env_name, colour),
                alarm_description=(
                    f"The API's {colour} target group has had no healthy "
                    "target for 3 minutes. Between deployments the target "
                    "group that is not live is empty, so one of the two "
                    "healthy alarms is always in ALARM; that is expected. "
                    f"{api_no_healthy_task_alarm_name(env_name)} is the alarm "
                    "that emails, when both are."
                ),
                metric=cloudwatch.Metric(
                    namespace="AWS/ApplicationELB",
                    metric_name="HealthyHostCount",
                    dimensions_map={
                        "LoadBalancer": self.alb.load_balancer_full_name,
                        "TargetGroup": target_group.target_group_full_name,
                    },
                    statistic="Maximum",
                    period=Duration.seconds(60),
                ),
                threshold=1,
                comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=3,
                datapoints_to_alarm=3,
                treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            )
            self.api_healthy_alarms.append(healthy)

        self.api_no_healthy_task_alarm = cloudwatch.CompositeAlarm(
            self,
            "ApiNoHealthyTask",
            composite_alarm_name=api_no_healthy_task_alarm_name(env_name),
            alarm_description=(
                "No API task is healthy: neither the blue nor the green "
                "target group has had a healthy target for 3 minutes. Every "
                "API request is answered 503 by the load balancer. See "
                "docs/deployment/disaster-recovery.md, Scenario 2."
            ),
            alarm_rule=cloudwatch.AlarmRule.all_of(
                *(
                    cloudwatch.AlarmRule.from_alarm(alarm, cloudwatch.AlarmState.ALARM)
                    for alarm in self.api_healthy_alarms
                )
            ),
        )
        self.api_no_healthy_task_alarm.add_alarm_action(
            cloudwatch_actions.SnsAction(alarm_topic)
        )

        # --- error- and critical-level lines in the API's logs (#205, #811) ---
        # On the log group the task definition's awslogs driver writes to.
        # The monitoring stack's old filter watched
        # /experimentation/<env>/application, which nothing writes to.
        #
        # The API writes one JSON object per line (configure_logging with
        # json_logs=True, backend/app/core/logger.py), and its "level" field
        # is lower case: "error", "critical". CloudWatch filter patterns are
        # case-sensitive, so the term "ERROR" this filter used to match
        # counted none of those lines. Lines that are not JSON, or whose
        # level is upper case (the bootstrap's and alembic's output, and
        # anything logged before configure_logging runs), are not counted.
        # backend/tests/unit/core/test_logger.py renders lines through
        # configure_logging and checks the two values this pattern names.
        error_logs = log_group.add_metric_filter(
            "ApiErrorLogs",
            filter_pattern=logs.FilterPattern.any(
                logs.FilterPattern.string_value("$.level", "=", "error"),
                logs.FilterPattern.string_value("$.level", "=", "critical"),
            ),
            metric_name="ApiErrorLogLines",
            metric_namespace=f"Experimently/{env_name}",
            default_value=0,
        )
        error_logs_alarm = cloudwatch.Alarm(
            self,
            "ApiErrorLogsAlarm",
            alarm_name=api_error_logs_alarm_name(env_name),
            alarm_description=(
                "At least 10 log lines from the API's tasks in 5 minutes whose "
                'JSON "level" is "error" or "critical"'
            ),
            metric=error_logs.metric(statistic="Sum", period=Duration.minutes(5)),
            threshold=10,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        error_logs_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        # --- CodeDeploy Application ---
        self.codedeploy_app = codedeploy.EcsApplication(
            self,
            "CodeDeployApp",
            # Account-scoped, so it carries the environment (#139).
            application_name=codedeploy_application_name(env_name),
        )

        # --- CodeDeploy Deployment Group ---
        # Uses CANARY_10_PERCENT_15_MINUTES: once the shift is approved, 10% of
        # the API's traffic goes to the replacement target group for 15
        # minutes, then the rest. Both 5xx alarms above, blue's and green's,
        # are in the group's AlarmConfiguration, which applies to the whole
        # deployment, not to a phase of it (the synth tests pin both). CDK's
        # documentation says the hour after the shift (termination_wait_time)
        # is monitored too; that, and what happens to an alarm already in
        # ALARM before the approval, is not yet observed in a real account.
        # While either is in ALARM, CodeDeploy stops the deployment
        # (STOP_ON_ALARM, from `deployment_in_alarm`) and rolls it back
        # (`stopped_deployment`). Nothing else judges the canary: a release
        # that answers wrongly with a 2xx, or with fewer errors than the alarm
        # needs, goes to 100%. The alarms need 2 of 3 one-minute periods plus
        # the metric delay. Fifteen minutes rather than five (D47): at launch
        # traffic, 10% of a few requests a minute often does not reach the
        # alarm's thresholds inside five minutes, so a broken release went to
        # 100% before the alarm could act. Revisit once production has real
        # traffic. The forward deploy's shift deadline covers it
        # (CODEDEPLOY_DEADLINE_SECONDS in deploy.yml; the synth tests pin the
        # two together).
        self.deployment_group = codedeploy.EcsDeploymentGroup(
            self,
            "DeploymentGroup",
            application=self.codedeploy_app,
            deployment_group_name=f"experimentation-{env_name}",
            service=self.fargate_service,
            blue_green_deployment_config=codedeploy.EcsBlueGreenDeploymentConfig(
                listener=self.https_listener,
                test_listener=self.test_listener,
                blue_target_group=self.blue_target_group,
                green_target_group=self.green_target_group,
                # Allow up to 30 minutes of manual approval before auto-rolling
                # back, giving operators time to validate the deployment via the
                # test listener before committing the traffic shift.
                deployment_approval_wait_time=Duration.minutes(30),
                # Keep the blue (old) task set alive for 1 hour after a
                # successful deployment so that a quick rollback is possible
                # without a full re-deployment.  The property is
                # `termination_wait_time` (a Duration): the name this used --
                # `terminate_blue_instances_on_deployment_success=
                # codedeploy.InstanceTerminationWaitTime.after(...)` -- is from
                # the EC2/on-premises deployment group and does not exist in
                # aws_cdk.aws_codedeploy at all, so `cdk synth` raised
                # AttributeError before it produced a single template.
                termination_wait_time=Duration.hours(1),
            ),
            deployment_config=codedeploy.EcsDeploymentConfig.CANARY_10_PERCENT_15_MINUTES,
            role=codedeploy_role,
            # Both colours, for the reason given above the alarms. A failed
            # alarm read stops the deployment (fail closed); never ignored.
            alarms=self.api_5xx_alarms,
            ignore_poll_alarms_failure=False,
            auto_rollback=codedeploy.AutoRollbackConfig(
                failed_deployment=True,
                stopped_deployment=True,
                # Explicit, not left to the default: CDK turns it on when
                # alarms exist, and False would leave an alarm stopping a
                # deployment without rolling it back -- a half-shifted group.
                deployment_in_alarm=True,
            ),
        )

        # --- Application Auto-Scaling ---
        # Scale between the environment's task count and 10. The floor IS the
        # desired count: a fixed floor of 3 would make Application Auto
        # Scaling hold staging at 3 whatever `desired_count` says. Scale-out
        # is aggressive (30 s cooldown) to respond quickly to traffic spikes;
        # scale-in is conservative (60 s) to avoid thrashing.
        scaling = self.fargate_service.auto_scale_task_count(
            min_capacity=api_desired_count,
            max_capacity=10,
        )

        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=70,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(30),
        )

        scaling.scale_on_memory_utilization(
            "MemoryScaling",
            target_utilization_percent=80,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(30),
        )

        # --- The etl module's Glue names, and the task role's Glue access ---
        # The Glue stack names its jobs, crawler and database per environment
        # (stacks/names.py); the API calls them through these settings, whose
        # defaults are the old account-wide names. Set from the same function
        # so the two cannot drift. Core deployments have no Glue at all.
        #
        # The grant (#487) is the six calls ETLService makes, each on this
        # environment's objects alone: the two jobs, the crawler, and the
        # catalog, database and tables under that database (GetTable and
        # BatchCreatePartition are authorised against all three). No wildcard
        # but the tables of this environment's database.
        # modules/backend/tests/unit/services/test_etl_service_calls.py fails
        # if the service starts making any other call. The client finds its
        # region in AWS_DEFAULT_REGION, set below with the counters table.
        if include_modules:
            glue = glue_names(env_name)
            for variable, value in glue.items():
                self.container.add_environment(variable, value)
            database = glue["GLUE_DATABASE"]
            task_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["glue:StartJobRun", "glue:GetJobRun"],
                    resources=[
                        self.format_arn(
                            service="glue",
                            resource="job",
                            resource_name=glue[variable],
                        )
                        for variable in ("GLUE_ETL_JOB_NAME", "GLUE_METRICS_JOB_NAME")
                    ],
                )
            )
            task_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["glue:StartCrawler", "glue:GetCrawler"],
                    resources=[
                        self.format_arn(
                            service="glue",
                            resource="crawler",
                            resource_name=glue["GLUE_CRAWLER_NAME"],
                        )
                    ],
                )
            )
            task_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["glue:GetTable", "glue:BatchCreatePartition"],
                    resources=[
                        self.format_arn(service="glue", resource="catalog"),
                        self.format_arn(
                            service="glue", resource="database", resource_name=database
                        ),
                        self.format_arn(
                            service="glue",
                            resource="table",
                            resource_name=f"{database}/*",
                        ),
                    ],
                )
            )

        # --- The counters module's DynamoDB table (#392) ---
        # The counters stack creates `counters_table_name(env)`; the API reads
        # the name from DYNAMODB_COUNTERS_TABLE, whose default is a name no
        # stack creates. The same function supplies both, as a plain string:
        # the table's token would become an export that pins the counters
        # stack. The grant is the two calls DynamoDBCounterService makes
        # (UpdateItem and Query, on the table itself, not its index);
        # modules/backend/tests/unit/services/test_counter_service_calls.py
        # fails if the service starts making any other.
        #
        # AWS_DEFAULT_REGION, not AWS_REGION: the service builds its client
        # with no region, and botocore takes the default region from
        # AWS_DEFAULT_REGION only. AWS_REGION is read explicitly by other
        # settings (Cognito among them), which this must not move. Core
        # deployments have no counters table, so they get none of this.
        if include_modules:
            if Token.is_unresolved(self.region):
                raise ValueError(
                    "the counters table needs a concrete region: give the "
                    "stack env=Environment(region=...)"
                )
            table_name = counters_table_name(env_name)
            self.container.add_environment("DYNAMODB_COUNTERS_TABLE", table_name)
            self.container.add_environment("AWS_DEFAULT_REGION", self.region)
            task_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["dynamodb:Query", "dynamodb:UpdateItem"],
                    resources=[
                        self.format_arn(
                            service="dynamodb",
                            resource="table",
                            resource_name=table_name,
                        )
                    ],
                )
            )

        # --- CloudFormation Outputs ---
        # Where the API's tasks run: the subnets and security group a one-off
        # task (the migration) must be given to reach what the service
        # reaches. Plain outputs, not exports -- a workflow reads them with
        # `describe-stacks`, and an export would be one more thing that pins
        # this stack in place.
        CfnOutput(
            self,
            "TaskSubnets",
            value=",".join(
                vpc.select_subnets(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ).subnet_ids
            ),
            description="Subnets the API tasks run in (comma-separated)",
        )
        CfnOutput(
            self,
            "TaskSecurityGroup",
            value=ecs_security_group.security_group_id,
            description="Security group the API tasks run in",
        )
        CfnOutput(
            self,
            "ALBDnsName",
            value=self.alb.load_balancer_dns_name,
            description="DNS name of the Application Load Balancer",
            export_name=f"{self.stack_name}-ALBDnsName",
        )
        CfnOutput(
            self,
            "ECSServiceArn",
            value=self.fargate_service.service_arn,
            description="ARN of the ECS Fargate service",
            export_name=f"{self.stack_name}-ECSServiceArn",
        )
        CfnOutput(
            self,
            "CodeDeployAppName",
            value=self.codedeploy_app.application_name,
            description="CodeDeploy application name",
            export_name=f"{self.stack_name}-CodeDeployApp",
        )
        CfnOutput(
            self,
            "DeploymentGroupName",
            value=self.deployment_group.deployment_group_name,
            description="CodeDeploy deployment group name",
            export_name=f"{self.stack_name}-DeploymentGroup",
        )
