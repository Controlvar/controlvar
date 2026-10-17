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
from stacks.names import aurora_cluster_identifier_parameter


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

        # Create a CloudWatch Dashboard
        dashboard = cloudwatch.Dashboard(
            self,
            "ExperimentationDashboard",
            dashboard_name="experimentation-platform" + suffix,
        )

        # Add API Gateway metrics
        api_widget = cloudwatch.GraphWidget(
            title="API Gateway",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="Count",
                    dimensions_map={"ApiName": "ExperimentationApi"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="Latency",
                    dimensions_map={"ApiName": "ExperimentationApi"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
            ],
        )

        # Add Lambda metrics
        lambda_widget = cloudwatch.GraphWidget(
            title="Lambda Functions",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Invocations",
                    dimensions_map={"FunctionName": "AssignmentLambda"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Invocations",
                    dimensions_map={"FunctionName": "EventProcessorLambda"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Invocations",
                    dimensions_map={"FunctionName": "FeatureFlagLambda"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Duration",
                    dimensions_map={"FunctionName": "AssignmentLambda"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Duration",
                    dimensions_map={"FunctionName": "EventProcessorLambda"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Duration",
                    dimensions_map={"FunctionName": "FeatureFlagLambda"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
            ],
        )

        # Add DynamoDB metrics
        dynamodb_widget = cloudwatch.GraphWidget(
            title="DynamoDB",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ConsumedReadCapacityUnits",
                    dimensions_map={"TableName": "AssignmentsTable"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ConsumedWriteCapacityUnits",
                    dimensions_map={"TableName": "AssignmentsTable"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ConsumedReadCapacityUnits",
                    dimensions_map={"TableName": "EventsTable"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ConsumedWriteCapacityUnits",
                    dimensions_map={"TableName": "EventsTable"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
            ],
        )

        # Add Kinesis metrics -- only when this deployment has a stream.
        kinesis_widget = None
        if events_stream_name is not None:
            stream_dimensions = {"StreamName": events_stream_name}
            kinesis_widget = cloudwatch.GraphWidget(
                title="Kinesis",
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

        # Add RDS metrics
        rds_widget = cloudwatch.GraphWidget(
            title="RDS Aurora",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/RDS",
                    metric_name="CPUUtilization",
                    dimensions_map={"DBClusterIdentifier": "AuroraCluster"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/RDS",
                    metric_name="DatabaseConnections",
                    dimensions_map={"DBClusterIdentifier": "AuroraCluster"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/RDS",
                    metric_name="FreeableMemory",
                    dimensions_map={"DBClusterIdentifier": "AuroraCluster"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/RDS",
                    metric_name="ReadLatency",
                    dimensions_map={"DBClusterIdentifier": "AuroraCluster"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/RDS",
                    metric_name="WriteLatency",
                    dimensions_map={"DBClusterIdentifier": "AuroraCluster"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
            ],
        )

        # Add Redis metrics
        redis_widget = cloudwatch.GraphWidget(
            title="ElastiCache Redis",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name="CPUUtilization",
                    dimensions_map={"CacheClusterId": "Redis"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name="CurrConnections",
                    dimensions_map={"CacheClusterId": "Redis"},
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name="CacheHits",
                    dimensions_map={"CacheClusterId": "Redis"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name="CacheMisses",
                    dimensions_map={"CacheClusterId": "Redis"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
            ],
        )

        # Add all widgets to the dashboard
        dashboard.add_widgets(
            *[
                widget
                for widget in (
                    api_widget,
                    lambda_widget,
                    dynamodb_widget,
                    kinesis_widget,
                    rds_widget,
                    redis_widget,
                )
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
        # counts error- and critical-level lines. The dashboards are
        # unchanged (#424).
        #
        # Aurora's writer CPU. CloudWatch publishes Aurora cluster metrics
        # under the cluster's identifier, which CloudFormation generates; the
        # alarm named the literal "AuroraCluster". The identifier comes from
        # the SSM parameter the database stack writes (stacks/names.py), read
        # when this stack deploys -- not from an export, which would pin the
        # database stack in place. Role=WRITER: the cluster-level series
        # without it mixes the writer with prod's reader. Missing data is
        # breaching: a writer that publishes nothing is not a healthy one.
        aurora_cluster_identifier = ssm.StringParameter.value_for_string_parameter(
            self, aurora_cluster_identifier_parameter(env_name)
        )
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

        # Redis CPU, one alarm per node. ElastiCache publishes host and engine
        # metrics per node (CacheClusterId), and a replication group's nodes
        # are <group id>-001 .. -00N; the alarm named the literal "Redis". The
        # group id is the literal the Redis stack sets (app.py passes it as a
        # plain string -- no import) and N comes from the function that sizes
        # the group. EngineCPUUtilization is the Redis engine thread's CPU,
        # the one that saturates; host CPUUtilization averages every core.
        for index in range(1, redis_node_count(env_name) + 1):
            member = f"{redis_replication_group_id}-{index:03d}"
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

        # Create a dashboard for application-specific metrics
        app_dashboard = cloudwatch.Dashboard(
            self,
            "ApplicationDashboard",
            dashboard_name="experimentation-application-metrics" + suffix,
        )

        # Add application-specific widgets (these would be custom metrics published by your application)
        app_widget = cloudwatch.GraphWidget(
            title="Experiment Metrics",
            left=[
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform",
                    metric_name="ActiveExperiments",
                    statistic="Maximum",
                    period=Duration.minutes(5),
                ),
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform",
                    metric_name="ExperimentCreationRate",
                    statistic="Sum",
                    period=Duration.hours(1),
                ),
            ],
            right=[
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform",
                    metric_name="FeatureFlagEvaluations",
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform",
                    metric_name="ExperimentAssignments",
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
            ],
        )

        event_widget = cloudwatch.GraphWidget(
            title="Event Processing",
            left=[
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform",
                    metric_name="EventsProcessed",
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform",
                    metric_name="EventProcessingLatency",
                    statistic="Average",
                    period=Duration.minutes(1),
                ),
            ],
            right=[
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform",
                    metric_name="EventProcessingErrors",
                    statistic="Sum",
                    period=Duration.minutes(5),
                )
            ],
        )

        # Add widgets to application dashboard
        app_dashboard.add_widgets(app_widget, event_widget)

        # ---------------------------------------------------------------
        # EP-013: Prometheus / application-level metrics section
        # These widgets surface metrics emitted by the FastAPI app via
        # backend/app/core/metrics.py and scraped into CloudWatch via the
        # CloudWatch agent or a Prometheus remote-write adapter.
        # ---------------------------------------------------------------

        # Request rate and error rate (HTTP metrics from the FastAPI app)
        prometheus_http_widget = cloudwatch.GraphWidget(
            title="HTTP Request Rate & Error Rate (app)",
            left=[
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform/Prometheus",
                    metric_name="http_requests_total",
                    dimensions_map={"method": "GET"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                    label="GET requests/min",
                ),
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform/Prometheus",
                    metric_name="http_requests_total",
                    dimensions_map={"method": "POST"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                    label="POST requests/min",
                ),
            ],
            right=[
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform/Prometheus",
                    metric_name="http_requests_total",
                    dimensions_map={"status_code": "500"},
                    statistic="Sum",
                    period=Duration.minutes(1),
                    label="5xx errors/min",
                ),
            ],
        )

        # Latency widget (p50 / p95 / p99)
        prometheus_latency_widget = cloudwatch.GraphWidget(
            title="Request Latency (app p50/p95/p99)",
            left=[
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform/Prometheus",
                    metric_name="http_request_duration_seconds",
                    statistic="p50",
                    period=Duration.minutes(1),
                    label="p50 latency (s)",
                ),
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform/Prometheus",
                    metric_name="http_request_duration_seconds",
                    statistic="p95",
                    period=Duration.minutes(1),
                    label="p95 latency (s)",
                ),
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform/Prometheus",
                    metric_name="http_request_duration_seconds",
                    statistic="p99",
                    period=Duration.minutes(1),
                    label="p99 latency (s)",
                ),
            ],
        )

        # Active experiments gauge
        active_experiments_widget = cloudwatch.GraphWidget(
            title="Active Experiments",
            left=[
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform/Prometheus",
                    metric_name="active_experiments_gauge",
                    statistic="Maximum",
                    period=Duration.minutes(5),
                    label="Active experiments",
                ),
            ],
        )

        # Cache hit ratio (cache hits / (hits + misses))
        cache_hit_rate_widget = cloudwatch.GraphWidget(
            title="Cache Hit Ratio",
            left=[
                cloudwatch.MathExpression(
                    expression="hits / (hits + misses) * 100",
                    using_metrics={
                        "hits": cloudwatch.Metric(
                            namespace="ExperimentationPlatform/Prometheus",
                            metric_name="cache_hits_total",
                            statistic="Sum",
                            period=Duration.minutes(1),
                        ),
                        "misses": cloudwatch.Metric(
                            namespace="ExperimentationPlatform/Prometheus",
                            metric_name="cache_misses_total",
                            statistic="Sum",
                            period=Duration.minutes(1),
                        ),
                    },
                    label="Cache hit ratio (%)",
                    period=Duration.minutes(1),
                ),
            ],
        )

        # Feature flag evaluations
        flag_eval_widget = cloudwatch.GraphWidget(
            title="Feature Flag Evaluations",
            left=[
                cloudwatch.Metric(
                    namespace="ExperimentationPlatform/Prometheus",
                    metric_name="feature_flag_evaluations_total",
                    statistic="Sum",
                    period=Duration.minutes(1),
                    label="Flag evaluations/min",
                ),
            ],
        )

        # Add EP-013 widgets to the application dashboard
        app_dashboard.add_widgets(
            prometheus_http_widget,
            prometheus_latency_widget,
            active_experiments_widget,
            cache_hit_rate_widget,
            flag_eval_widget,
        )
