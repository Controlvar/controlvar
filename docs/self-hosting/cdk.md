# AWS CDK Deployment

To have a script run this page's steps for you, see [Install on AWS with one command (beta)](aws-install.md).

The platform's AWS infrastructure is defined as code using **AWS CDK v2**, in **Python**: the app is `infrastructure/cdk/app.py`, and `infrastructure/cdk/cdk.json` runs it with `python3 app.py`. `cdk deploy --all` provisions the API, the dashboard and the data stores they need in your AWS account.

**The Fargate stack runs two services behind one Application Load Balancer**: the API, and the dashboard as its own ECS service (`experimentation-dashboard-<env>`). The HTTPS listener sends `/api/*`, `/health`, `/health/*` and `/metrics` to the API and everything else to the dashboard. `cdk deploy` starts the dashboard on the `experimentation-platform/web:bootstrap` image, which you push before the first deploy ([deployment guide §1.3](../deployment/deployment-guide.md)). After that, the Deploy workflow rolls each release onto the dashboard: it registers a dashboard task definition with the release's image, rolls the dashboard service once the API is serving the same release, and smoke-tests it through the public URL.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| AWS Account | Any | [Sign up](https://aws.amazon.com/) |
| Python | 3.11+ | The CDK app is Python; its requirements go in a virtual environment ([Deploy All Stacks](#deploy-all-stacks)) |
| Node.js | 22+ | The AWS CDK CLI and the app's `jsii` runtime run on it |
| AWS CDK CLI | 2.1144.0 | `npm install -g aws-cdk@2.1144.0` |
| AWS CLI | v2 | Every `aws` command below |
| jq | | The commands below build and read JSON with it |
| cosign and crane | | To verify the signed release images and copy them to ECR ([deployment guide §1.3](../deployment/deployment-guide.md#13-ecr-repositories-and-the-bootstrap-images)) |
| Docker | | Only to build the images from source instead ([deployment guide §1.3](../deployment/deployment-guide.md#13-ecr-repositories-and-the-bootstrap-images)) |

A `staging` deployment of the core profile was deployed and torn down on a
new AWS account on 2026-10-21 by following this page, with AWS CDK CLI
2.1144.0, AWS CLI 2.28, Node.js 22, jq 1.7.1, cosign 3.1.3 and crane 0.22.1,
its commands run by macOS's bash 3.2.

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

On a new account, measured on 2026-10-21, the bootstrap took 53 seconds.

---

## Required Environment Variables

`infrastructure/cdk/app.py` reads these from the environment. `cdk synth`, `cdk diff`, `cdk deploy` and `cdk destroy` fail without the ones marked required, `ALARM_EMAIL` is required for `staging` and `prod`, and the three secret ARNs are required in every environment (below). Keep every one of them exported for every later `cdk` command, `cdk destroy` included: the app reads them each time it is synthesised.

**One region.** The Deploy, Rollback and Database Migration workflows act in
`us-west-2` (`AWS_REGION` at the top of each), so deploy the stacks, the ECR
repositories, the secrets and the certificate there too. To use another region,
change it in all three workflows and use it everywhere below.

Set the region in your shell. The CDK CLI works out the account from your
credentials and the region from `AWS_REGION`, else your profile's region, else
`us-east-1`, and gives them to the app as `CDK_DEFAULT_ACCOUNT` and
`CDK_DEFAULT_REGION` in place of any you export:

```bash
export AWS_REGION=us-west-2
```

`ENVIRONMENT` is `dev` (the default), `staging`, `prod` or `demo`. For an install, set `ENVIRONMENT=staging`, or `prod` for the redundant production shape.

```bash
export ENVIRONMENT=staging
```

Choose the profile, `core` or `full`
([Which stacks you get](#which-stacks-you-get-core-or-full)). A clone of this
repository is a full checkout, so unless you choose `core`, `cdk deploy --all`
deploys the full profile, whose stacks also need the audit key's secret
(below). For the core profile:

```bash
export EXPERIMENTLY_PROFILE=core
```

Required: the ACM certificate for the load balancer's HTTPS listeners:

```bash
export CERTIFICATE_ARN=arn:aws:acm:us-west-2:123456789012:certificate/your-certificate-id
```

To get one, you need a public Route 53 hosted zone for your domain that the
internet resolves (for a subdomain, delegated from its parent zone). Request
the certificate for the host in `PUBLIC_BASE_URL` (below), with DNS
validation:

```bash
ZONE_NAME=example.com
APP_HOST=app.example.com
CERTIFICATE_ARN=$(aws acm request-certificate --region us-west-2 \
  --domain-name "$APP_HOST" --validation-method DNS \
  --query CertificateArn --output text)
export CERTIFICATE_ARN
```

Write its validation record into the zone. ACM takes a few seconds to say
what the record is, so the loop waits for it:

```bash
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$ZONE_NAME" \
  --query "HostedZones[?Name=='$ZONE_NAME.' && Config.PrivateZone==\`false\`].Id" --output text)
ZONE_ID=${ZONE_ID#/hostedzone/}
for i in $(seq 1 30); do
  aws acm describe-certificate --certificate-arn "$CERTIFICATE_ARN" --region us-west-2 \
    --query 'Certificate.DomainValidationOptions' --output json > cert-validation.json
  if jq -e 'length > 0 and all(.[]; .ResourceRecord != null)' cert-validation.json > /dev/null; then break; fi
  sleep 10
done
jq '{Changes: [map(.ResourceRecord) | unique_by(.Name)[] | {Action: "UPSERT",
    ResourceRecordSet: {Name: .Name, Type: "CNAME", TTL: 300, ResourceRecords: [{Value: .Value}]}}]}' \
  cert-validation.json > cert-validation-change.json
CHANGE_ID=$(aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --change-batch file://cert-validation-change.json --query ChangeInfo.Id --output text)
aws route53 wait resource-record-sets-changed --id "$CHANGE_ID"
```

Then wait for ACM to issue it. `aws acm wait certificate-validated` gives up
after five minutes; this loop waits up to 45 minutes:

```bash
for i in $(seq 1 90); do
  CERTIFICATE_STATUS=$(aws acm describe-certificate --certificate-arn "$CERTIFICATE_ARN" \
    --region us-west-2 --query Certificate.Status --output text)
  if [ "$CERTIFICATE_STATUS" != PENDING_VALIDATION ]; then break; fi
  sleep 30
done
echo "$CERTIFICATE_STATUS"
```

It must print `ISSUED`. On a new account, measured on 2026-10-21, the
certificate was issued about 15 seconds after its validation record was in
the zone. ACM keeps trying for up to 72 hours before it gives up.

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
create. The dashboard's is pulled from `experimentation-platform/web`, which
it does not create either:
[deployment guide §1.3](../deployment/deployment-guide.md#13-ecr-repositories-and-the-bootstrap-images)
creates both repositories and puts their `bootstrap` images in them, copied
from a signed release or built from source.

---

## Deploy All Stacks

Deploy from a checkout of the release whose images you put in the
repositories, with the CDK app's requirements in a virtual environment:

```bash
git clone https://github.com/Controlvar/controlvar.git
cd controlvar
git switch --detach vX.Y.Z
cd infrastructure/cdk
python3 -m venv venv
. venv/bin/activate
python3 -m pip install -r requirements.txt
cdk deploy --all --require-approval never
```

- **`--require-approval never`** deploys without asking. Without it the CLI
  asks before each stack that adds IAM or security-group rules, and without a
  terminal it stops there instead.
- **The CLI records your account's availability zones, under your account ID,
  in `infrastructure/cdk/cdk.context.json`**, a tracked file. Do not commit
  that change.

On a new account, measured on 2026-10-21, `cdk deploy --all` of the nine
`staging` stacks of the core profile took 23 min 43 s: the database stack
7 min 32 s, Redis 5 min 23 s, Fargate 4 min 21 s, the VPC 2 min 27 s, and each
of the other five under a minute.

With an IAM Identity Center profile (`aws sso login`), the access token lasts
an hour, and the AWS CLI renews it while your access-portal session lasts:
measured on 2026-10-21, a command run after the first token had expired
needed no new sign-in, and the deploy and the teardown each ran on one
sign-in.

On a new account, measured on 2026-10-21, the Fargate On-Demand vCPU quota
was 6, which a `staging` deployment fits. The API scales out to ten tasks of
one vCPU each, so ask for a higher quota in Service Quotas (`L-3032A538`)
before production load.

### Point the hostname at the load balancer

**No stack creates a DNS record.** After `cdk deploy` the load balancer is up,
but the host in `PUBLIC_BASE_URL` (`app.<domain>`) does not resolve until you
add an alias record for it in your Route 53 hosted zone. Until then `curl`
reports `000`, and the Deploy workflow's smoke test cannot reach the platform.
Do this once per environment, after the first `cdk deploy` of the Fargate stack.

Add the record for the host in `PUBLIC_BASE_URL`: it is the one name the API
serves. Measured on 2026-10-21, a second name on the same load balancer,
`api.<domain>`, answered `/api/v1/experiments/` with
`400 {"detail":"Invalid host header"}`, while its `/health` answered `200`.

Set the hosted zone's name and the hostname:

```{.bash skip reason="aws: reads the hosted zone and the load balancer of a deployed environment"}
ZONE_NAME=example.com
APP_HOST=${PUBLIC_BASE_URL#https://}
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

Create, or update, the alias A record, and wait for Route 53 to apply the
change:

```{.bash skip reason="aws: changes records in the hosted zone"}
cat > alias.json <<EOF
{"Changes": [
  {"Action": "UPSERT", "ResourceRecordSet": {"Name": "$APP_HOST", "Type": "A",
    "AliasTarget": {"HostedZoneId": "$ALB_ZONE", "DNSName": "$ALB_DNS", "EvaluateTargetHealth": false}}}
]}
EOF
CHANGE_ID=$(aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --change-batch file://alias.json --query ChangeInfo.Id --output text)
aws route53 wait resource-record-sets-changed --id "$CHANGE_ID"
```

Check that the hostname reaches the platform:

```{.bash skip reason="aws: needs a deployed environment and its DNS records"}
curl -s -o /dev/null -w '%{http_code}\n' "https://$APP_HOST/health"
```

It prints `200`. `000` means the name does not resolve yet, or resolves to
something other than this load balancer: check the record and the zone id, and
allow for DNS caches that still hold an earlier answer.

`UPSERT` makes the commands safe to run again. Run them again whenever the load
balancer is replaced (for example after destroying and redeploying the Fargate
stack), because its DNS name changes with it.

This record and the certificate's validation record are the two you made in
the zone. `cdk destroy` does not remove these records: the stacks did not create them. When you tear an environment down, delete them (the same change with `"Action": "DELETE"`). Route 53 refuses to delete a hosted zone that still has them. If you made the hosted zone for this environment and delegated it from a parent zone, delete the zone's NS records in the parent zone first, then the zone.

### `cdk deploy` does not create the schema

The API tasks never run database migrations: their task definition sets
`RUN_MIGRATIONS=false` and `SEED=` (empty). The schema is written only by the
migration task, which the **Deploy** workflow runs before it shifts traffic
(and the **Database Migration** workflow when run by hand). So after the first
`cdk deploy` of an environment the API runs against an empty database until the
schema is created:

- `/health` answers 200 through the hostname once its alias record exists
  (above), because it checks that the database answers, and the load
  balancer's health checks pass;
- a sign-in, an SDK evaluation and a tracked event answer 500, and the API
  logs errors. This is expected until the schema exists;
- a request without credentials answers 401, as it does once the schema
  exists, so that answer does not show the schema is there (measured on
  2026-10-21 with `GET /api/v1/experiments/`).

Create the schema soon after the first `cdk deploy`: run the first Deploy, or,
without the Deploy workflow, the migration task by hand
([below](#creating-the-schema-without-the-deploy-workflow)). Requests that reach the
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

### Creating the schema without the Deploy workflow

From the repository root of the same checkout, register a revision of the
migration task that names the `bootstrap` image by its digest, then run it
with the bootstrap command. These are the Deploy workflow's two steps
([Running Migrations in Production](migrations.md#running-migrations-in-production)),
with the task's network read from the Fargate stack.

Name the first administrator when you create the schema: add `FIRST_SUPERUSER`, set to your own address, to the bootstrap task's overrides. It is read only while the database has no users; without it the address is `admin@example.com`. After you sign in, add a second administrator, so the install does not depend on one account.

Put your address in place of `<your address>`; the commands copy it into the
overrides:

```bash
FIRST_ADMIN_EMAIL='<your address>'
DIGEST=$(aws ecr describe-images --repository-name experimentation-platform/backend \
  --image-ids imageTag=bootstrap --query 'imageDetails[0].imageDigest' --output text)
REGISTRY=$(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-west-2.amazonaws.com
ARN=$(bash scripts/register_task_definition.sh "experimentation-migrate-$ENVIRONMENT" \
  "$REGISTRY/experimentation-platform/backend@$DIGEST")
SUBNETS=$(aws cloudformation describe-stacks --stack-name "experimentation-fargate-$ENVIRONMENT" \
  --query "Stacks[0].Outputs[?OutputKey=='TaskSubnets'].OutputValue" --output text)
SG=$(aws cloudformation describe-stacks --stack-name "experimentation-fargate-$ENVIRONMENT" \
  --query "Stacks[0].Outputs[?OutputKey=='TaskSecurityGroup'].OutputValue" --output text)
NETWORK=$(jq -cn --arg subnets "$SUBNETS" --arg sg "$SG" \
  '{awsvpcConfiguration: {subnets: ($subnets | split(",")), securityGroups: [$sg], assignPublicIp: "DISABLED"}}')
OVERRIDES=$(jq -cn --arg admin "$FIRST_ADMIN_EMAIL" \
  '{containerOverrides: [{name: "backend", command: ["python", "-m", "backend.app.db.bootstrap"],
    environment: [{name: "FIRST_SUPERUSER", value: $admin}]}]}')
bash scripts/run_migration_task.sh "experimentation-$ENVIRONMENT" "$ARN" "$NETWORK" "$OVERRIDES" \
  "/ecs/experimentation-migrate-$ENVIRONMENT"
```

The script prints the task's last 100 log lines, and exits 0 only if the task
did. Among the lines are
`created the first administrator <your address> (role ADMIN)` and
`Database bootstrap complete (created)`.

Then sign in at `PUBLIC_BASE_URL` with that address and the password in the
`/<env>/experimentation/first-superuser-password` secret. On a new account,
measured on 2026-10-21, the task took 70 seconds, and its address signed in
as an `ADMIN` superuser.

### Users' addresses

Run `cdk deploy` for the Fargate stack (`experimentation-fargate-<env>`) from a
release's checkout before you deploy that release's image: the stack is what
sets `FORWARDED_ALLOW_IPS` on the API task. The Deploy workflow refuses an API
task definition that does not set it, and says which stack to deploy.

The API task sets `FORWARDED_ALLOW_IPS` to the VPC's range, `10.0.0.0/16`, so
the API believes the `X-Forwarded-For` header the load balancer adds, counts
its rate limits per user address and writes that address on its access lines.
This CloudWatch Logs Insights query on `/ecs/experimentation-backend-<env>`
counts the API requests from each address:

```text
fields event
| filter logger = "uvicorn.access" and event like "/api/"
| parse event /^(?<client>\S+):\d+ - /
| stats count(*) as requests by client
| sort requests desc
```

Your users' addresses mean it works; `10.0.x.x` on every line is the load
balancer's.

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

- `EXPERIMENTLY_PROFILE=core cdk deploy --all`: core stacks only, from a full checkout
- `EXPERIMENTLY_PROFILE=full cdk deploy --all`: fail if the module stacks are absent

With the core profile from a full checkout, measured on 2026-10-21, the line
reads:

```text
[cdk] profile: core (modules/infrastructure/cdk/stacks present, EXPERIMENTLY_PROFILE=core)
```

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

Measured on 2026-10-21 on a new account, for a `staging` deployment of the
core profile at v0.36.1, the nine `cdk destroy` commands of a core deployment
below took 26 min 23 s, the database stack 10 min 33 s of it, Redis
5 min 18 s, Fargate 5 min 5 s and the VPC 2 min 58 s, and no stack failed to
delete, the API's CodeDeploy-controlled service included. What they left, and
how to remove that, is in the sections below.

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
command, so it needs every variable the deploy had: `AWS_REGION`,
`ENVIRONMENT`, the profile, `CERTIFICATE_ARN`, `PUBLIC_BASE_URL`,
`ALARM_EMAIL` for `staging` or `prod`, and the secret ARNs. In a script, add
`--force`, which deletes without asking.

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

The glue, analytics and counters stacks exist only in a full deployment; skip
their lines in a core one, a full checkout deployed with the core profile
included.

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
| DynamoDB tables `experimentation-{assignments,experiments}-prod`, in a production environment deployed before #605 | no stack creates them any more, and nothing reads them | prod | provisioned capacity and storage | `aws dynamodb create-backup` if you want a copy, then `aws dynamodb delete-table` |
| Log groups `/ecs/experimentation-{backend,dashboard,migrate}-prod` | RETAIN | prod | log storage | `aws logs delete-log-group` |
| Data-lake, Athena-results and Glue-scripts buckets | RETAIN | prod (full) | storage, every object version | empty (all versions), then `aws s3 rb` |
| Kinesis stream and OpenSearch domain | RETAIN | prod (full) | per shard-hour; per instance-hour | `aws kinesis delete-stream`; `aws opensearch delete-domain` |
| `pre-migration-*` and `pre-deploy-*` Aurora cluster snapshots | taken by the deploy and migrate workflows, not by CloudFormation | every environment they ran against | snapshot storage | `aws rds delete-db-cluster-snapshot`. Outside prod the KMS key they are encrypted with is deleted with the stack, so they cannot be restored afterwards |
| The alias A record for `app.<domain>` and the certificate's validation record | created by hand ([Required Environment Variables](#required-environment-variables), [Point the hostname at the load balancer](#point-the-hostname-at-the-load-balancer)) | every environment | nothing for the records; the hosted zone is billed per month | the same `change-resource-record-sets` with `"Action": "DELETE"` ([below](#removing-what-is-left)); a hosted zone you made for this environment goes after them, its NS records in the parent zone first |
| The ACM certificate | requested by hand before the first deploy ([Required Environment Variables](#required-environment-variables)) | every environment | nothing | `aws acm delete-certificate --certificate-arn "$CERTIFICATE_ARN" --region us-west-2`, after `cdk destroy`: ACM refuses while a load balancer uses it |
| The secrets under `/<env>/experimentation/` | created by hand before the first deploy ([Secrets Management](../deployment/secrets-management.md)) | every environment | per secret per month | `aws secretsmanager delete-secret --force-delete-without-recovery`: without the flag a secret waits 30 days, and a new secret of the same name is refused until then |
| ECR repositories `experimentation-platform/backend` and `experimentation-platform/web`, and their images | created by hand once per account ([Deployment Guide](../deployment/deployment-guide.md)) | shared by all environments | image storage | `aws ecr delete-repository --force`, once no environment needs them |
| The CDK bootstrap stack `CDKToolkit`: its `cdk-*-assets-<account>-<region>` bucket and `cdk-*-container-assets-*` repository | `cdk bootstrap`, once per account and region | shared by all environments | storage | last, and only where no other CDK app uses it: empty the bucket, delete the stack, then delete the bucket, which the stack keeps ([below](#removing-what-is-left)) |
| Log groups AWS services create at run time: `/aws/lambda/<function>` for the functions without a managed log group, `/aws/ecs/containerinsights/<cluster>/performance`, `/aws-glue/*` | the services themselves, not CloudFormation | every environment | log storage | `aws logs delete-log-group` |
| Task-definition revisions of `experimentation-{backend,dashboard,migrate}-<env>` | CloudFormation deregisters its own and leaves them INACTIVE; one registered by hand or by Deploy stays ACTIVE | every environment | nothing | deregister the ACTIVE ones, then `aws ecs delete-task-definitions` ([below](#removing-what-is-left)) |
| A system backup of each of `experimentation-{events,feature-flags,overrides}-<env>` | DynamoDB, for a deleted table that had point-in-time recovery on, as these do | dev, staging, demo | nothing | none: DynamoDB deletes it after 35 days |
| An alarm subscription that was never confirmed | the SNS topic's `ALARM_EMAIL` subscription, left `PendingConfirmation` | every environment with `ALARM_EMAIL` | nothing | none: measured on 2026-10-21, `aws sns list-subscriptions` still listed it after its topic was deleted, with no ARN to unsubscribe |
| The service-linked roles `AWSServiceRoleForECS`, `AWSServiceRoleForElasticLoadBalancing`, `AWSServiceRoleForRDS`, `AWSServiceRoleForElastiCache` and `AWSServiceRoleForApplicationAutoScaling_ECSService`, and the AWS managed KMS keys `aws/acm`, `aws/lambda`, `aws/s3` and `aws/secretsmanager` | AWS, the first time the account uses each service | the account | nothing | none: they serve the account's later use, and an AWS managed key cannot be deleted |

The KMS key outside prod is scheduled for deletion after the 30-day waiting
period, and its alias is removed at once, so it does not block a redeploy.

The database credentials secret
(`experimentation-database-<env>-aurora-credentials`) was deleted with its
stack, not left in a recovery window, measured on 2026-10-21. If the next
deploy of the same environment fails with "a secret with this name is already
scheduled for deletion", remove it with
`aws secretsmanager delete-secret --secret-id <name> --force-delete-without-recovery`.

Aurora's automated backups went with the cluster, measured on 2026-10-21: the
RDS API deletes them with a cluster unless an AWS Backup policy keeps them.

### Removing what is left

Delete the alias and validation records with the files that made them, from
the directory you made them in, with `"Action": "DELETE"` in place of
`"UPSERT"`. The block looks the zone up again from `ZONE_NAME`, set as in
[Point the hostname at the load balancer](#point-the-hostname-at-the-load-balancer):

```bash
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$ZONE_NAME" \
  --query "HostedZones[?Name=='$ZONE_NAME.' && Config.PrivateZone==\`false\`].Id" --output text)
ZONE_ID=${ZONE_ID#/hostedzone/}
sed 's/"UPSERT"/"DELETE"/' alias.json > alias-delete.json
sed 's/"UPSERT"/"DELETE"/' cert-validation-change.json > cert-validation-delete.json
for CHANGE in alias-delete.json cert-validation-delete.json; do
  CHANGE_ID=$(aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
    --change-batch "file://$CHANGE" --query ChangeInfo.Id --output text)
  aws route53 wait resource-record-sets-changed --id "$CHANGE_ID"
done
```

List the task-definition revisions by name, not with `--family-prefix`, which
takes a whole family name: measured on 2026-10-21,
`--family-prefix experimentation-` listed none of them. Deregister the ones
still ACTIVE, then delete the INACTIVE ones, which ECS does in the background:

```bash
QUERY="taskDefinitionArns[?contains(@, ':task-definition/experimentation-') && contains(@, '-$ENVIRONMENT:')]"
for TD in $(aws ecs list-task-definitions --status ACTIVE --query "$QUERY" --output text); do
  aws ecs deregister-task-definition --task-definition "$TD" --query taskDefinition.taskDefinitionArn --output text
done
for TD in $(aws ecs list-task-definitions --status INACTIVE --query "$QUERY" --output text); do
  aws ecs delete-task-definitions --task-definitions "$TD" --query failures --output text
done
```

Delete `CDKToolkit` last, after every environment's stacks are gone, and only in
an account and region that no other CDK app uses: the bootstrap stack and its
bucket serve every CDK app there, and CloudFormation deletes the stacks through
the roles it holds. The stack keeps its bucket when it is deleted. Empty the
bucket of every object version, delete markers included (the S3 console's Empty
action does this), then delete the `CDKToolkit` stack, then delete the bucket.
If you are not sure that no other CDK app uses the account and region, leave
`CDKToolkit` in place: it costs pennies of storage. The
[AWS installer (beta)](aws-install.md) never deletes `CDKToolkit`, whether it
made the stack or the account had it before, so this is where to remove it after
an uninstall.

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
