# AWS CDK Deployment

The platform's AWS infrastructure is defined as code using **AWS CDK v2**, in **Python**: the app is `infrastructure/cdk/app.py`, and `infrastructure/cdk/cdk.json` runs it with `python3 app.py`. `cdk deploy --all` provisions the API, the dashboard and the data stores they need in your AWS account.

**The Fargate stack runs two services behind one Application Load Balancer**: the API, and the dashboard as its own ECS service (`experimentation-dashboard-<env>`). The HTTPS listener sends `/api/*`, `/health`, `/health/*` and `/metrics` to the API and everything else to the dashboard. `cdk deploy` starts the dashboard on the `experimentation-platform/web:bootstrap` image, which you push before the first deploy ([deployment guide §1.3](../deployment/deployment-guide.md)). After that, the Deploy workflow rolls each release onto the dashboard: it registers a dashboard task definition with the release's image, rolls the dashboard service once the API is serving the same release, and smoke-tests it through the public URL.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| AWS Account | Any | [Sign up](https://aws.amazon.com/) |
| Python | 3.11+ | The CDK app is Python |
| AWS CDK | v2 | `npm install -g aws-cdk` |
| Docker | | For the bootstrap images the [deployment guide](../deployment/deployment-guide.md) pushes |

---

## One-Time Bootstrap

CDK bootstrap provisions the S3 bucket and IAM roles that CDK needs to deploy assets. Run this once per AWS account/region combination:

```bash
cdk bootstrap aws://YOUR_ACCOUNT_ID/YOUR_REGION
```

Example:

```bash
cdk bootstrap aws://123456789012/us-west-2
```

You can find your account ID with:

```bash
aws sts get-caller-identity --query Account --output text
```

---

## Required Environment Variables

`infrastructure/cdk/app.py` reads these from the environment. `cdk synth`, `cdk diff`, `cdk deploy` and `cdk destroy` fail without the ones marked required, `ALARM_EMAIL` is required for `staging` and `prod`, and the three secret ARNs are required in every environment (below).

**One region.** The Deploy, Rollback and Database Migration workflows act in
`us-west-2` (`AWS_REGION` at the top of each), so deploy the stacks, the ECR
repositories, the secrets and the certificate there too. To use another region,
change it in all three workflows and use it everywhere below.

```bash
export CDK_DEFAULT_ACCOUNT=123456789012
export CDK_DEFAULT_REGION=us-west-2
```

`ENVIRONMENT` is `dev` (the default), `staging`, `prod` or `demo`:

```bash
export ENVIRONMENT=prod
```

Required: the ACM certificate for the load balancer's HTTPS listeners:

```bash
export CERTIFICATE_ARN=arn:aws:acm:us-west-2:123456789012:certificate/your-certificate-id
```

Required: the absolute `https://` origin users reach the platform at (one host, `app.<domain>`):

```bash
export PUBLIC_BASE_URL=https://app.example.com

export ALARM_EMAIL=ops@your-domain.com
```

**`ALARM_EMAIL`** is the one address every CloudWatch alarm emails, through the
SNS topic `experimentation-alerts-<env>`. That includes the API's two 5xx
alarms: while either is in ALARM, CodeDeploy rolls a deployment back by itself,
and this email is how you hear about it.

- **Required for `staging` and `prod`.** Without it, synth stops with a
  `ValueError` naming `ALARM_EMAIL` and saying how to set it.
- **Optional for `dev` and `demo`.** Leave it unset and the topic gets no
  subscriber at all, not a placeholder.
- **Refused, in any environment,** when it is longer than 254 characters,
  contains whitespace, has other than exactly one `@`, has a domain with no dot,
  or is at `example.com`, `example.net` or `example.org`, or a subdomain of one
  (in any case).
- **Needed every time the app is synthesised.** That includes `cdk diff` and
  `cdk destroy`, not only `cdk deploy`. Changing the value replaces the
  subscription, which then has to be confirmed again.

**The subscription does nothing until it is confirmed.** After the first
`cdk deploy` of the monitoring stack, SNS emails a confirmation link to the
address. Until it is confirmed, the alarms fire and nobody is told, and the
link does not last for ever, so confirm it the same day. Do not just click it:
a subscription confirmed that way can be cancelled by the unsubscribe link in
any alarm email, by anyone the email reaches. Instead, copy the `Token=` value
out of the link and confirm it with `--authenticate-on-unsubscribe true`, after
which unsubscribing takes an authenticated AWS call:

```bash
TOPIC_ARN=$(aws sns list-topics --output text \
  --query "Topics[?ends_with(TopicArn, ':experimentation-alerts-$ENVIRONMENT')].TopicArn")
aws sns confirm-subscription --topic-arn "$TOPIC_ARN" \
  --token "$TOKEN_FROM_THE_LINK" --authenticate-on-unsubscribe true
aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
  --query "Subscriptions[].[Protocol,Endpoint,SubscriptionArn]" --output text
```

The last command must show the `email` subscription with a real ARN, not
`PendingConfirmation`.

Required in **every** environment: the complete ARN of each secret the task
definitions read. The secrets themselves must exist first
([Secrets Management](../deployment/secrets-management.md)); read each ARN by
name, read-only, in the region the stacks deploy to (the third only for the
full profile):

```bash
export JWT_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "/$ENVIRONMENT/experimentation/jwt-secret" --query ARN --output text)
export FIRST_SUPERUSER_PASSWORD_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "/$ENVIRONMENT/experimentation/first-superuser-password" --query ARN --output text)
export AUDIT_HMAC_KEY_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "/$ENVIRONMENT/experimentation/audit-hmac-key" --query ARN --output text)
```

- **`JWT_SECRET_ARN`**, **`FIRST_SUPERUSER_PASSWORD_SECRET_ARN`**: always.
  **`AUDIT_HMAC_KEY_SECRET_ARN`**: the full profile only (checked on core too
  if you set it).
- **Each must be the complete ARN**,
  `arn:aws:secretsmanager:<region>:<account>:secret:/<env>/experimentation/<name>-<suffix>`,
  in the stack's own account and region. Synth refuses a missing value, a
  partial ARN (one that stops at the name: ECS cannot resolve it), an ARN for a
  different environment (`/prod/...` in staging), or one variable's secret given
  in another. The refusal
  names the variable and the command above, and never prints the value.
- **Recreating a secret changes its ARN.** Update the input and run
  `cdk deploy` again, or the next task start fails. Changing a secret's value
  keeps its ARN.

The application's own secret *values* (database password, JWT secret and the
rest) are not read from your shell. The task definition takes them from Secrets
Manager when each task starts; see the list in the
[Deployment README](../deployment/README.md). The API image is pulled from
the ECR repository `experimentation-platform/backend`, which the CDK does not
create.

---

## Deploy All Stacks

```bash
cd infrastructure/cdk
pip install -r requirements.txt
cdk deploy --all
```

### Point the hostname at the load balancer

**No stack creates a DNS record.** After `cdk deploy` the load balancer is up,
but the host in `PUBLIC_BASE_URL` (`app.<domain>`) does not resolve until you
add an alias record for it in your Route 53 hosted zone. Until then `curl`
reports `000`, and the Deploy workflow's smoke test cannot reach the platform.
Do this once per environment, after the first `cdk deploy` of the Fargate stack.

Add a record for every hostname the platform answers on: the host in
`PUBLIC_BASE_URL` always, and any other name on the certificate that clients
use (the commands below also add `api.<domain>`; leave that entry out if you
do not use it).

Set the hosted zone's name and the two hostnames:

```{.bash skip reason="aws: reads the hosted zone and the load balancer of a deployed environment"}
ZONE_NAME=example.com
APP_HOST=${PUBLIC_BASE_URL#https://}
API_HOST=api.example.com
```

Look up the zone, and the load balancer by its name. The Fargate stack names
it `experimentation-<env>`, so this finds the right one in an account with
more than one load balancer; do not take the first internet-facing one. Both
commands must print exactly one value:

```{.bash skip reason="aws: reads the hosted zone and the load balancer of a deployed environment"}
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$ZONE_NAME" \
  --query "HostedZones[?Name=='$ZONE_NAME.' && Config.PrivateZone==\`false\`].Id" --output text)
ZONE_ID=${ZONE_ID#/hostedzone/}
read -r ALB_DNS ALB_ZONE < <(aws elbv2 describe-load-balancers \
  --names "experimentation-$ENVIRONMENT" \
  --query 'LoadBalancers[0].[DNSName,CanonicalHostedZoneId]' --output text)
printf 'zone %s\nalb  %s %s\n' "$ZONE_ID" "$ALB_DNS" "$ALB_ZONE"
```

`ALB_DNS` is also the `ALBDnsName` output of `experimentation-fargate-<env>`.
The alias records below take `ALB_ZONE` as their `HostedZoneId`, not
`ZONE_ID`.

Create, or update, an alias A record for each hostname, and wait for Route 53
to apply the change:

```{.bash skip reason="aws: changes records in the hosted zone"}
cat > alias.json <<EOF
{"Changes": [
  {"Action": "UPSERT", "ResourceRecordSet": {"Name": "$APP_HOST", "Type": "A",
    "AliasTarget": {"HostedZoneId": "$ALB_ZONE", "DNSName": "$ALB_DNS", "EvaluateTargetHealth": false}}},
  {"Action": "UPSERT", "ResourceRecordSet": {"Name": "$API_HOST", "Type": "A",
    "AliasTarget": {"HostedZoneId": "$ALB_ZONE", "DNSName": "$ALB_DNS", "EvaluateTargetHealth": false}}}
]}
EOF
CHANGE_ID=$(aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --change-batch file://alias.json --query ChangeInfo.Id --output text)
aws route53 wait resource-record-sets-changed --id "$CHANGE_ID"
```

Check that each hostname reaches the platform:

```{.bash skip reason="aws: needs a deployed environment and its DNS records"}
curl -s -o /dev/null -w '%{http_code}\n' "https://$APP_HOST/health"
curl -s -o /dev/null -w '%{http_code}\n' "https://$API_HOST/health"
```

Each prints `200`. `000` means the name does not resolve yet, or resolves to
something other than this load balancer: check the record and the zone id, and
allow for DNS caches that still hold an earlier answer.

`UPSERT` makes the commands safe to run again. Run them again whenever the load
balancer is replaced (for example after destroying and redeploying the Fargate
stack), because its DNS name changes with it.

`cdk destroy` does not remove these records: the stacks did not create them.
When you tear an environment down, delete them (the same change with
`"Action": "DELETE"`), or they are deleted with the hosted zone if you delete
the zone itself.

### `cdk deploy` does not create the schema

The API tasks never run database migrations: their task definition sets
`RUN_MIGRATIONS=false` and `SEED=` (empty). The schema is written only by the
migration task, which the **Deploy** workflow runs before it shifts traffic
(and the **Database Migration** workflow when run by hand). So after the first
`cdk deploy` of an environment the API runs against an empty database until the
first Deploy:

- `/health` answers 200 through the hostname once its alias record exists
  (above), because it checks that the database answers, and the load
  balancer's health checks pass;
- real requests answer 500, and the API logs errors. This is expected until
  the first Deploy creates the schema.

Run the first Deploy soon after the first `cdk deploy`. Requests that reach the
API in between, from scanners as much as from people, can put one of the API's
5xx alarms into ALARM, and Deploy refuses to start while one is. The way
through is Deploy's existing break-glass
([rollback runbook, "Fix forward while an alarm is firing"](../deployment/rollback-runbook.md#fix-forward-while-an-alarm-is-firing));
whether to use it is decided by a person at that moment.

Rolling the API back runs the older release against the newer schema, which
works only for backward-compatible migrations; for one that is not, undo the
migration while the release that contains it is still serving, before rolling
the API back
([rollback runbook, Database Rollback Procedure](../deployment/rollback-runbook.md#database-rollback-procedure)).
After the API has been rolled back, the emergency route is a point-in-time
restore with `scripts/restore_repoint.sh`
([Disaster Recovery, Scenario 4](../deployment/disaster-recovery.md#scenario-4-full-aurora-database-cluster-failure)). See
[On AWS, the API does not migrate](migrations.md#on-aws-the-api-does-not-migrate)
for why, and for an environment whose Fargate stack was deployed before this
setting existed. Deploy refuses to deploy an API revision that lacks the
setting, and says to `cdk deploy` the Fargate stack from a current checkout
first.

### Which stacks you get: core or full

The CDK app deploys the stacks your checkout has, the same way the API loads
the modules it finds. A **core** checkout has no `modules/` directory and gets
the core stacks below. A **full** checkout also has
`modules/infrastructure/cdk/stacks`, and `cdk deploy --all` then adds three
more:

| Stack | Module | What it is |
|-------|--------|------------|
| `experimentation-dynamodb-counters-<env>` | `counters` | The real-time counter table the bandit scheduler reads |
| `experimentation-analytics-<env>` | `etl` | Kinesis stream, Firehose delivery, OpenSearch domain, data-lake bucket |
| `experimentation-glue-etl-<env>` | `etl` | Glue crawler and ETL jobs over the data-lake bucket |

The first line the app prints says which profile it picked:

```
[cdk] profile: full (modules/infrastructure/cdk/stacks present)
```

`EXPERIMENTLY_PROFILE` — the same variable the container images and the API
use — overrides the choice:

```bash
EXPERIMENTLY_PROFILE=core cdk deploy --all
EXPERIMENTLY_PROFILE=full cdk deploy --all
```

- `EXPERIMENTLY_PROFILE=core cdk deploy --all`: core stacks only, from a full checkout
- `EXPERIMENTLY_PROFILE=full cdk deploy --all`: fail if the module stacks are absent

Two things follow from the profile, so a core deployment is consistent rather
than half-configured:

- The monitoring stack's Kinesis widget and its iterator-age alarm are created
  only alongside the analytics stack, and they watch the stream that stack
  actually creates. A core deployment has neither.
- Without the `counters` stack there is no counter table: the bandit scheduler
  falls back to PostgreSQL for its statistics, which is the core behaviour.

---

## Individual Stack Deployment

Deploy specific stacks during development or when updating a single component:

`cdk deploy` takes a stack **id**, which `cdk list` prints for your
environment -- not a class name:

Deploy only the API service (faster for code changes):

```bash
cdk deploy experimentation-fargate-dev
```

Deploy only monitoring resources:

```bash
cdk deploy experimentation-monitoring-dev
```

**Redeploying `experimentation-fargate-<env>` on a running environment** needs
two pins, printed by two read-only checks run from the repository root:

```bash
python3 scripts/check_live_target_group.py --env <env>
python3 scripts/check_dashboard_image.py --env <env>
cdk deploy experimentation-fargate-<env> \
  -c api_live_target_group=<blue|green> -c dashboard_image_tag=sha256:<hex>
```

- `python3 scripts/check_live_target_group.py --env <env>`: prints `api_live_target_group`
- `python3 scripts/check_dashboard_image.py --env <env>`: prints `dashboard_image_tag`

Without them the deploy undoes what the release workflow did, and every probe
stays green: the API's routes can point at the empty one of its blue and green
target groups, and a change to the dashboard's task definition
puts it back on the `web:bootstrap` placeholder image. The [Deployment Guide, section 1.6](../deployment/deployment-guide.md#16-the-stacks)
has the full sequence, including `backend_image_tag` and what to do when the
dashboard is still on `:bootstrap`.

---

## What Gets Deployed

### experimentation-vpc-<env>

- VPC with public and private subnets across 2 availability zones
- NAT gateways for outbound internet access from private subnets: two in `prod` (one per availability zone), one in every other environment

The VPC is `10.0.0.0/16` across two availability zones, with a public, a
private and an isolated `/24` in each: `10.0.0.0/24` and `10.0.1.0/24` public,
`10.0.2.0/24` and `10.0.3.0/24` private, `10.0.4.0/24` and `10.0.5.0/24`
isolated. It has gateway endpoints for S3 and DynamoDB, and interface
endpoints, with private DNS, for Secrets Manager, ECR (API and Docker) and
CloudWatch Logs. AWS bills each interface endpoint by the hour, used or not. The load
balancer is the only resource open to the internet: its security group admits
TCP 80 and 443 from anywhere, and no other security group rule admits the
whole internet. It reaches the API tasks on 8000 and the dashboard tasks on
8080, and nothing else. Aurora admits PostgreSQL (5432) from the API task
group, which the migration task also runs in.

The VPC stack also creates a bastion security group, which admits nobody
unless you deploy with `-c bastion_ssh_cidr=<cidr>`.

### experimentation-database-<env> and experimentation-redis-<env>

- **Aurora PostgreSQL** cluster: writer + 1 reader on `db.r5.large` for `prod`, a single `db.t3.medium` in every other environment
- **ElastiCache Redis** replication group: 3 nodes on `cache.r6g.large` for `prod` (automatic failover, Multi-AZ); a single node elsewhere, `cache.t4g.small` for `staging` and `cache.t4g.medium` otherwise, with no replica and so no failover
- Subnet groups and parameter groups
- Redis snapshots kept 7 days in `prod`, 3 in `staging`, 1 otherwise

### experimentation-compute-<env> and experimentation-fargate-<env>

- **ECS Fargate** cluster
- ECS task definition for the API container (1 vCPU, 2 GB memory)
- ECS service for the API with 3 tasks in `prod`, 2 in `dev` and `staging` and 1 in `demo`, auto-scaling from that count up to 10
- **Application Load Balancer** with an HTTPS listener and an HTTP-to-HTTPS redirect, in front of the API and the dashboard: `/api/*`, `/health`, `/health/*` and `/metrics` go to the API, everything else to the dashboard
- The dashboard's own ECS service, `experimentation-dashboard-<env>` (rolling deployment with the circuit breaker; 1 task in `staging`, 2 in `prod`), started on `experimentation-platform/web:bootstrap`
- AWS CodeDeploy deployment group for blue/green deployments
- IAM task role for the API: the `CloudWatchLogsFullAccess` managed policy,
  `secretsmanager:GetSecretValue` on the secrets the task reads, and the
  `ssmmessages` and `logs` actions ECS Exec uses. The full profile adds
  `dynamodb:Query` and `dynamodb:UpdateItem` on the
  `experiment-counters-<env>` table alone, and sets `DYNAMODB_COUNTERS_TABLE`
  and `AWS_DEFAULT_REGION` on the API container. It also adds, for the ETL
  routes, `glue:StartJobRun` and `glue:GetJobRun` on this environment's two
  Glue jobs, `glue:StartCrawler` and `glue:GetCrawler` on its crawler, and
  `glue:GetTable` and `glue:BatchCreatePartition` on the catalog, the
  `experimentation_<env>` database and its tables, and sets the four `GLUE_*`
  names on the API container. There is no Kinesis, Cognito, Athena, S3 or other
  DynamoDB permission

### experimentation-dynamodb-<env> and experimentation-dynamodb-counters-<env>

- Core: the events, feature-flags and overrides tables
- Full profile only (the `counters` module): the **experiment-counters**
  DynamoDB table with on-demand billing and a GSI for querying by experiment

### experimentation-analytics-<env> (full profile only — the `etl` module)

- **Kinesis Data Stream** (name generated by CloudFormation; it used to be
  `exp-events-<random>`, regenerated on every synth -- see #96)
- **Firehose delivery stream** into the data-lake bucket
- **OpenSearch Service** domain

### experimentation-monitoring-<env> {#experimentation-monitoring}

- CloudWatch dashboards `experimentation-platform-<env>` and
  `experimentation-application-metrics-<env>` (some of their widgets graph
  metrics nothing publishes, #424)
- CloudWatch alarms: `AuroraHighCPU-<env>` on the Aurora writer, and one
  `RedisHighCPU-00N-<env>` per Redis node. The Aurora alarm reads the cluster's
  identifier from the SSM parameter
  `/experimentation/<env>/database/aurora-cluster-identifier`, which the
  database stack writes, so this stack depends on
  `experimentation-database-<env>`.
- SNS topic for alarm notifications, `experimentation-alerts-<env>`, with one
  email subscriber: `ALARM_EMAIL` (none in `dev` or `demo` without it). The
  fargate stack's alarms publish to it too: the two API 5xx alarms, which roll
  a deployment back, `experimentation-api-no-healthy-task-<env>`, which fires
  when no API task is healthy, and `experimentation-api-error-logs-<env>`,
  which fires on 10 or more lines in 5 minutes whose JSON `level` is `error`
  or `critical`.
  That is why `experimentation-fargate-<env>` depends on this stack.
- With the `etl` module: a Kinesis widget and an iterator-age alarm on that
  module's event stream. A core deployment gets neither, rather than an alarm
  on a stream that does not exist.

### What is not deployed

- **CloudFront.** No stack creates a distribution. The split-URL module ships a
  construct for one (`modules/infrastructure/constructs/split_url_distribution.py`),
  but `app.py` does not use it; see [Split URL testing](../api/split-url.md).

---

## Blue/Green Deployment for Zero-Downtime Updates

The API service uses blue/green deployment through AWS CodeDeploy, and the
**Deploy** workflow is what drives it ([deployment guide, section 3](../deployment/deployment-guide.md#3-every-deploy)).
The API's revision moves through CodeDeploy, not through `cdk deploy`: the
service has a CODE_DEPLOY deployment controller, and ECS refuses a
task-definition change through UpdateService on such a service. For each
deploy:

1. The workflow registers a new task definition and creates a CodeDeploy deployment
2. CodeDeploy starts the new tasks in the target group that is not live, and reports `Ready`
3. When every new target is healthy, the workflow approves the shift
4. The canary sends 10% of traffic to the new tasks, waits 15 minutes ([why 15](../deployment/README.md#deploy)), then sends the rest
5. The old tasks are kept for an hour, so Rollback can put them back, and then terminated

Alarms watch the API's canary and the hour after it. The Fargate stack creates
two CloudWatch alarms, `experimentation-api-5xx-blue-<env>` and
`experimentation-api-5xx-green-<env>`, one per target group, and attaches both
to the deployment group. While either is in ALARM, CodeDeploy stops the
deployment and rolls the API back by itself; it also rolls back a deployment
that fails (new tasks that never become healthy) or is stopped. The alarms
watch the API's target 5xx only: a release that answers wrongly with a 2xx,
the load balancer's own 502 and 504, and the dashboard are not covered. See the
[rollback runbook](../deployment/rollback-runbook.md#method-3-what-codedeploy-rolls-back-by-itself-and-when).

---

## Preview Changes with cdk diff

Before deploying, preview what will change. On an environment that is
already running, pass exactly the pins the deploy will pass
([Deployment Guide, section 1.6](../deployment/deployment-guide.md#16-the-stacks)
shows how to read them), or the diff shows the very changes those pins exist to
prevent. Write the diff to a file rather than piping it:

```bash
cdk diff "experimentation-fargate-$ENVIRONMENT" \
  -c backend_image_tag="$BACKEND_TAG" -c api_live_target_group="$API_LIVE" \
  -c dashboard_image_tag="$DASHBOARD_DIGEST" > "cdk-diff-$ENVIRONMENT.txt" 2>&1
```

Do not deploy a diff with any line that names
`AWS::EC2::SecurityGroupEgress` or `AWS::EC2::SecurityGroupIngress`: section
1.6 of the Deployment Guide gives the stop rule and why.

---

## Destroying an environment

The teardown has not been rehearsed on AWS as of v0.31.1: this section comes
from the stacks' templates and AWS's documentation.

Only `prod` keeps its data when its stacks are destroyed. Every other
environment (`dev`, `staging`, `demo`) is disposable: its database, tables,
buckets, stream, search domain, user pool, key and log groups are deleted with
it, so the next deploy of the same environment starts clean instead of failing
on the names the last one left behind. The rule is one line,
`retains_data(env) == (env == "prod")`, in
`infrastructure/cdk/stacks/environments.py`.

Before you destroy `prod`, export anything you need that the retained resources
below do not already keep: `pg_dump` the database, export audit logs
(`GET /api/v1/compliance/export`, full profile only), archive CloudWatch logs.

### Tearing an environment down

`cdk destroy --all` destroys the stacks in dependency order. By hand, the same
order is: a stack that imports from another, or depends on it, goes first --
so monitoring and the Glue stack go before analytics, and fargate before
compute and monitoring. `cdk destroy` synthesises the app like any other
command, so a `staging` or `prod` teardown needs `ALARM_EMAIL` set too.

```bash
cdk destroy experimentation-migrations-<env>
cdk destroy experimentation-fargate-<env>
cdk destroy experimentation-glue-etl-<env>
cdk destroy experimentation-monitoring-<env>
cdk destroy experimentation-analytics-<env>
cdk destroy experimentation-compute-<env>
cdk destroy experimentation-redis-<env>
cdk destroy experimentation-database-<env>
cdk destroy experimentation-dynamodb-counters-<env>
cdk destroy experimentation-dynamodb-<env>
cdk destroy experimentation-auth-<env>
cdk destroy experimentation-vpc-<env>
```

The glue, analytics and counters stacks exist only in a full checkout; skip
their lines in a core one.

The buckets outside `prod` are emptied before they are deleted:
`auto_delete_objects=True` adds a custom resource backed by a CDK-provided
Lambda (`Custom::S3AutoDeleteObjectsCustomResourceProvider`, with its own IAM
role) that, when the stack is deleted, denies new writes to the bucket and
deletes every object version and delete marker. It acts only on a bucket
carrying the `aws-cdk:auto-delete-objects` tag.

### What `cdk destroy` leaves behind and bills

Everything below survives `cdk destroy --all`. The first group is kept on
purpose, in `prod` only; the rest is created outside the stacks' templates and
is yours to remove.

| What | Where it comes from | Environments | Billed while it exists | How to remove it |
|------|---------------------|--------------|------------------------|------------------|
| The final snapshot CloudFormation takes of the Aurora cluster | the cluster's SNAPSHOT policy | prod | snapshot storage | `aws rds delete-db-cluster-snapshot` |
| KMS key (the alias is deleted) | RETAIN; the final snapshot is encrypted with it | prod | per key per month | `aws kms schedule-key-deletion` -- only after the snapshot is gone |
| Cognito user pool `experimentation-platform-users-prod` | RETAIN | prod | per monthly active user | `aws cognito-idp delete-user-pool` |
| DynamoDB tables `experimentation-{events,feature-flags,overrides}-prod`, `experiment-counters-prod` | RETAIN | prod | capacity (the core tables are provisioned in prod) and storage | `aws dynamodb delete-table` |
| Log groups `/ecs/experimentation-{backend,dashboard,migrate}-prod` | RETAIN | prod | log storage | `aws logs delete-log-group` |
| Data-lake, Athena-results and Glue-scripts buckets | RETAIN | prod (full) | storage, every object version | empty (all versions), then `aws s3 rb` |
| Kinesis stream and OpenSearch domain | RETAIN | prod (full) | per shard-hour; per instance-hour | `aws kinesis delete-stream`; `aws opensearch delete-domain` |
| `pre-migration-*` and `pre-deploy-*` Aurora cluster snapshots | taken by the deploy and migrate workflows, not by CloudFormation | every environment they ran against | snapshot storage | `aws rds delete-db-cluster-snapshot`. Outside prod the KMS key they are encrypted with is deleted with the stack, so they cannot be restored afterwards |
| The alias A records for `app.<domain>` (and `api.<domain>`) | created by hand after the first deploy ([Point the hostname at the load balancer](#point-the-hostname-at-the-load-balancer)) | every environment | nothing for the records; the hosted zone is billed per month | the same `change-resource-record-sets` with `"Action": "DELETE"`, or delete the hosted zone |
| The secrets under `/<env>/experimentation/` | created by hand before the first deploy ([Secrets Management](../deployment/secrets-management.md)) | every environment | per secret per month | `aws secretsmanager delete-secret` |
| ECR repositories `experimentation-platform/backend` and `experimentation-platform/web`, and their images | created by hand once per account ([Deployment Guide](../deployment/deployment-guide.md)) | shared by all environments | image storage | `aws ecr delete-repository --force`, once no environment needs them |
| The CDK bootstrap stack `CDKToolkit`: its `cdk-*-assets-<account>-<region>` bucket and `cdk-*-container-assets-*` repository | `cdk bootstrap`, once per account and region | shared by all environments | storage | `aws cloudformation delete-stack --stack-name CDKToolkit`, after emptying the bucket |
| Log groups AWS services create at run time: `/aws/lambda/<function>` for the functions without a managed log group, `/aws/ecs/containerinsights/<cluster>/performance`, `/aws-glue/*` | the services themselves, not CloudFormation | every environment | log storage | `aws logs delete-log-group` |

Two things are deleted but not immediately:

- **The database credentials secret** (`experimentation-database-<env>-aurora-credentials`)
  may be scheduled for deletion with a recovery window rather than removed. If
  the next deploy of the same environment fails with "a secret with this name is
  already scheduled for deletion", remove it with
  `aws secretsmanager delete-secret --secret-id <name> --force-delete-without-recovery`.
- **The KMS key outside prod** is scheduled for deletion after the 30-day
  waiting period, and its alias is removed at once, so it does not block a
  redeploy.

### Moving an environment deployed before the rename

This version names the ECS cluster from `ENVIRONMENT` (`experimentation-<env>`;
it was `experimentation-dev` in every environment, #142) and renames the
CodeDeploy application to `experimentation-platform-<env>` (#139). **An
environment deployed by an earlier version cannot be updated in place:**
`cdk deploy --all` fails on the compute stack.

The fargate stack is the only importer of that export -- the analytics stack
imports nothing from compute and is not touched. So, for each environment
deployed before this change:

```bash
cdk destroy --exclusively experimentation-fargate-<env>
aws logs delete-log-group --log-group-name /ecs/experimentation-backend-<env>
aws logs delete-log-group --log-group-name /ecs/experimentation-dashboard-<env>
cdk deploy experimentation-compute-<env>
cdk deploy experimentation-fargate-<env>
cdk deploy --all
```

- `--exclusively` matters: without it the CDK CLI also destroys the stacks that
  depend on fargate, which is the migrations stack.
- Run the two `delete-log-group` lines: a recreated fargate stack would
  otherwise fail on the names. Skip the dashboard line if that log group does
  not exist.
- Destroying fargate stops the API and the dashboard until the last step
  finishes; the database, Redis and the data stores are not touched.
