from aws_cdk import (
    Stack,
    Duration,
    Token,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
    aws_ssm as ssm,
)
from constructs import Construct

from stacks.alarm_email import resolve_alarm_email
from stacks.environments import redis_node_count
from stacks.names import (
    API_ERROR_LOG_LINES_METRIC,
    AUDIT_WRITE_FAILURES_METRIC,
    api_log_metric_namespace,
    api_service_name,
    aurora_cluster_identifier_parameter,
    ecs_cluster_name,
)


class MonitoringStack(Stack):
    """Dashboard, alarms and SNS topic for the platform.

    ``events_stream_name`` is the Kinesis stream the analytics stack creates
    (``modules/infrastructure/cdk/stacks/analytics_stack.py``, the ``etl``
    module).  It is ``None`` in a core deployment, where there is no such
    stream: the Kinesis widget and the iterator-age alarm are then not created
    at all, rather than graphing nothing and holding an alarm in
    INSUFFICIENT_DATA for ever.  The name is passed in rather than written out
    here because the stream names itself ``exp-events-<id>``, not the
    ``experimentation-events`` this stack used to watch.

    ``alarm_email`` is ``ALARM_EMAIL`` (``app.py`` reads it), the topic's one
    email subscriber; ``stacks/alarm_email.py`` has what is required where and
    what is refused.

    ``redis_replication_group_id`` is the Redis stack's replication group id,
    a literal string (``redis_stack.replication_group_id``): the per-node CPU
    alarms are built from it. Required -- there is no default a real
    deployment's group could have. The Aurora alarm reads the cluster's
    identifier from the database stack's SSM parameter
    (``stacks/names.py``), so ``app.py`` makes this stack depend on the
    database stack.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc,
        *,
        redis_replication_group_id: str,
        events_stream_name: str | None = None,
        env_name: str = "dev",
        alarm_email: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if (
            not isinstance(redis_replication_group_id, str)
            or not redis_replication_group_id
            or Token.is_unresolved(redis_replication_group_id)
        ):
            raise ValueError(
                "MonitoringStack requires redis_replication_group_id: the Redis "
                "stack's literal replication group id "
                "(redis_stack.replication_group_id), from which the per-node "
                "CPU alarms are named."
            )

        # Every name below is account-scoped (alarms, dashboards, the topic),
        # and every one was literal: dev and staging in one
        # account collided on fourteen of them (#139). Each now ends in the
        # environment.
        suffix = f"-{env_name}"

        # Create an SNS topic for alerts
        self.alerts_topic = sns.Topic(
            self,
            "AlertsTopic",
            display_name="Controlvar Alerts",
            topic_name="experimentation-alerts" + suffix,
        )

        # The one subscriber: ALARM_EMAIL (stacks/alarm_email.py, DECISIONS
        # D21). Required in staging and prod; in dev and demo, no value means
        # no subscription rather than a placeholder that mails nobody.
        address = resolve_alarm_email(alarm_email, env_name)
        if address is not None:
            self.alerts_topic.add_subscription(
                sns_subscriptions.EmailSubscription(address)
            )

        # Aurora's cluster identifier. CloudWatch publishes Aurora cluster
        # metrics under the cluster's identifier, which CloudFormation
        # generates; the alarm and the dashboard named the literal
        # "AuroraCluster". The identifier comes from the SSM parameter the
        # database stack writes (stacks/names.py), read when this stack
        # deploys -- not from an export, which would pin the database stack in
        # place. Role=WRITER: the cluster-level series without it mixes the
        # writer with prod's reader.
        aurora_cluster_identifier = ssm.StringParameter.value_for_string_parameter(
            self, aurora_cluster_identifier_parameter(env_name)
        )
        aurora_writer = {
            "DBClusterIdentifier": aurora_cluster_identifier,
            "Role": "WRITER",
        }

        # The Redis replication group's nodes. ElastiCache publishes host and
        # engine metrics per node (CacheClusterId), and a replication group's
        # nodes are <group id>-001 .. -00N; the alarm and the dashboard named
        # the literal "Redis". The group id is the literal the Redis stack
        # sets (app.py passes it as a plain string -- no import) and N comes
        # from the function that sizes the group.
        redis_members = [
            f"{redis_replication_group_id}-{index:03d}"
            for index in range(1, redis_node_count(env_name) + 1)
        ]

        # --- experimentation-platform-<env> (#424) ---------------------------
        # Every widget graphs a metric a deployed resource publishes, by
        # dimensions that name a resource the app creates. The API Gateway,
        # Lambda and DynamoDB widgets this dashboard used to carry graphed
        # an API Gateway, three Lambdas and two tables no stack deploys, and
        # the Aurora and Redis widgets named identifiers no resource has.
        # infrastructure/tests/test_dashboards.py parses every widget.
        dashboard = cloudwatch.Dashboard(
            self,
            "ExperimentationDashboard",
            dashboard_name="experimentation-platform" + suffix,
        )

        # The event stream -- only when this deployment has one.
        kinesis_widget = None
        if events_stream_name is not None:
            stream_dimensions = {"StreamName": events_stream_name}
            kinesis_widget = cloudwatch.GraphWidget(
                title="Kinesis event stream",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/Kinesis",
                        metric_name="IncomingRecords",
                        dimensions_map=stream_dimensions,
                        statistic="Sum",
                        period=Duration.minutes(1),
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/Kinesis",
                        metric_name="IncomingBytes",
                        dimensions_map=stream_dimensions,
                        statistic="Sum",
                        period=Duration.minutes(1),
                    ),
                ],
                right=[
                    cloudwatch.Metric(
                        namespace="AWS/Kinesis",
                        metric_name="GetRecords.IteratorAgeMilliseconds",
                        dimensions_map=stream_dimensions,
                        statistic="Maximum",
                        period=Duration.minutes(1),
                    )
                ],
            )

        aurora_widget = cloudwatch.GraphWidget(
            title="Aurora writer",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/RDS",
                    metric_name=name,
                    dimensions_map=aurora_writer,
                    statistic=statistic,
                    period=Duration.minutes(1),
                )
                for name, statistic in (
                    ("CPUUtilization", "Average"),
                    ("DatabaseConnections", "Average"),
                )
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/RDS",
                    metric_name=name,
                    dimensions_map=aurora_writer,
                    statistic="Average",
                    period=Duration.minutes(1),
                )
                for name in ("FreeableMemory", "ReadLatency", "WriteLatency")
            ],
        )

        # One series per node and metric.
        redis_widget = cloudwatch.GraphWidget(
            title="Redis nodes",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name=name,
                    dimensions_map={"CacheClusterId": member},
                    statistic="Average",
                    period=Duration.minutes(1),
                    label=f"{name} {member}",
                )
                for name in ("EngineCPUUtilization", "CurrConnections")
                for member in redis_members
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name=name,
                    dimensions_map={"CacheClusterId": member},
                    statistic="Sum",
                    period=Duration.minutes(1),
                    label=f"{name} {member}",
                )
                for name in ("CacheHits", "CacheMisses")
                for member in redis_members
            ],
        )

        dashboard.add_widgets(
            *[
                widget
                for widget in (kinesis_widget, aurora_widget, redis_widget)
                if widget is not None
            ]
        )

        # --- Alarms (#390, #205) ---------------------------------------------
        # Every alarm here watches a metric a deployed resource publishes. The
        # ones this stack used to carry did not, and sat in INSUFFICIENT_DATA
        # for ever: an API Gateway 5xx alarm (no API Gateway is deployed; the
        # API's 5xx alarms are the fargate stack's, on the load balancer), two
        # Lambda alarms on `AssignmentLambda` (a Lambda no stack ever
        # deployed, and whose code has since been deleted, #480), a
        # throttling alarm on the literal table `AssignmentsTable` (the table
        # is experimentation-assignments-<env>, which nothing uses), three alarms on the
        # `ExperimentationPlatform/Prometheus` namespace (nothing publishes
        # to it), and an ERROR filter on `/experimentation/<env>/application`,
        # a log group nothing writes to -- the API's tasks log to
        # /ecs/experimentation-backend-<env>, where the fargate stack now
        # counts error- and critical-level lines. The dashboards went the
        # same way (#424).
        #
        # Aurora's writer CPU, on the identifier above. Missing data is
        # breaching: a writer that publishes nothing is not a healthy one.
        aurora_cpu_alarm = cloudwatch.Alarm(
            self,
            "AuroraCpuAlarm",
            metric=cloudwatch.Metric(
                namespace="AWS/RDS",
                metric_name="CPUUtilization",
                dimensions_map={
                    "DBClusterIdentifier": aurora_cluster_identifier,
                    "Role": "WRITER",
                },
                statistic="Average",
                period=Duration.minutes(5),
            ),
            evaluation_periods=3,
            threshold=80,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            alarm_description=(
                "The Aurora writer's CPU is at 80% or more, or the writer "
                "publishes no CPU metric"
            ),
            alarm_name="AuroraHighCPU" + suffix,
        )

        aurora_cpu_alarm.add_alarm_action(
            cloudwatch_actions.SnsAction(self.alerts_topic)
        )

        # Kinesis iterator age alarm (potential processing backlog) -- only
        # when the analytics stack created the stream it watches.
        if events_stream_name is not None:
            iterator_age_alarm = cloudwatch.Alarm(
                self,
                "KinesisIteratorAgeAlarm",
                metric=cloudwatch.Metric(
                    namespace="AWS/Kinesis",
                    metric_name="GetRecords.IteratorAgeMilliseconds",
                    dimensions_map={"StreamName": events_stream_name},
                    statistic="Maximum",
                    period=Duration.minutes(5),
                ),
                evaluation_periods=3,
                threshold=300000,  # 5 minutes
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                alarm_description="Kinesis stream processing is falling behind",
                alarm_name="KinesisProcessingDelay" + suffix,
            )

            iterator_age_alarm.add_alarm_action(
                cloudwatch_actions.SnsAction(self.alerts_topic)
            )

        # Redis CPU, one alarm per node (the members above).
        # EngineCPUUtilization is the Redis engine thread's CPU, the one that
        # saturates; host CPUUtilization averages every core.
        for index, member in enumerate(redis_members, start=1):
            redis_cpu_alarm = cloudwatch.Alarm(
                self,
                f"RedisCpuAlarm{index:03d}",
                metric=cloudwatch.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name="EngineCPUUtilization",
                    dimensions_map={"CacheClusterId": member},
                    statistic="Average",
                    period=Duration.minutes(5),
                ),
                evaluation_periods=3,
                threshold=80,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
                alarm_description=(
                    f"Redis node {member}: engine CPU is at 80% or more, or "
                    "the node publishes no CPU metric"
                ),
                alarm_name=f"RedisHighCPU-{index:03d}" + suffix,
            )

            redis_cpu_alarm.add_alarm_action(
                cloudwatch_actions.SnsAction(self.alerts_topic)
            )

        # --- experimentation-application-metrics-<env> (#424) ----------------
        # The API's service and its log lines. Every widget this dashboard
        # used to carry graphed the `ExperimentationPlatform` and
        # `ExperimentationPlatform/Prometheus` namespaces, which nothing
        # publishes: the API's Prometheus metrics are served on /metrics and
        # nothing collects them.
        #
        # The service and cluster by name (stacks/names.py): the fargate stack
        # depends on this one, so it cannot hand this stack the service's
        # token. Container Insights is on for the cluster (compute_stack.py).
        # The log-line metrics are the fargate stack's two metric filters on
        # the API's log group, by the names those filters publish.
        app_dashboard = cloudwatch.Dashboard(
            self,
            "ApplicationDashboard",
            dashboard_name="experimentation-application-metrics" + suffix,
        )
        api_service = {
            "ClusterName": ecs_cluster_name(env_name),
            "ServiceName": api_service_name(env_name),
        }
        log_metrics = api_log_metric_namespace(env_name)
        app_dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="API service: CPU and memory",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/ECS",
                        metric_name=name,
                        dimensions_map=api_service,
                        statistic="Average",
                        period=Duration.minutes(1),
                    )
                    for name in ("CPUUtilization", "MemoryUtilization")
                ],
            ),
            cloudwatch.GraphWidget(
                title="API tasks running",
                left=[
                    cloudwatch.Metric(
                        namespace="ECS/ContainerInsights",
                        metric_name="RunningTaskCount",
                        dimensions_map=api_service,
                        statistic="Average",
                        period=Duration.minutes(1),
                    )
                ],
            ),
            cloudwatch.GraphWidget(
                title="API log lines at level error or critical",
                left=[
                    cloudwatch.Metric(
                        namespace=log_metrics,
                        metric_name=API_ERROR_LOG_LINES_METRIC,
                        statistic="Sum",
                        period=Duration.minutes(5),
                    )
                ],
            ),
            cloudwatch.GraphWidget(
                title="Audit-log entries not written",
                left=[
                    cloudwatch.Metric(
                        namespace=log_metrics,
                        metric_name=AUDIT_WRITE_FAILURES_METRIC,
                        statistic="Sum",
                        period=Duration.minutes(5),
                    )
                ],
            ),
        )
