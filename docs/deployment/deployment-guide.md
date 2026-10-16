# Deployment Guide — Controlvar

**Audience:** whoever stands up an environment, and whoever deploys to it.
**Environments:** `staging` and `prod` -- one path for both. `<env>` below is
one of those two words, and `ENV=staging` (or `prod`) in a shell.
**Region:** `us-west-2`, the region the workflows act in
([cdk.md](../self-hosting/cdk.md#required-environment-variables)).

---

## Table of Contents

1. [Before the first deploy](#1-before-the-first-deploy) -- the ordered checklist
2. [The first deploys, and the rollback rehearsal](#2-the-first-deploys-and-the-rollback-rehearsal)
3. [Every deploy](#3-every-deploy)
4. [Pre-Deployment Checklist](#4-pre-deployment-checklist)
5. [Post-Deployment Verification](#5-post-deployment-verification)
6. [Deployment Architecture](#6-deployment-architecture)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Before the first deploy

Do these **in this order**, once per environment. Each step says who does it,
how to check it read-only, and where to stop. Several are irreversible or
billable, and each of those needs a person to say yes at the moment it happens.

### 1.0 Decisions (founder)

Which AWS account (one per environment), which region (one: `us-west-2` unless
you change all three workflows), which profile (`full` or `core`), the domain
(`app.<domain>`), a budget alarm and who owns the bill, and when a staging
environment is torn down. Nothing below should start without them.

### 1.1 IAM (account administrator)

Three identities, described with their exact permissions in
[IAM Permissions](iam-permissions.md): the **bootstrap** identity (once), the
**`cdk deploy`** identity, and one **workflow role** per environment.

- The workflow role's trust policy pins the OIDC subject exactly. **Decode a
  real token's `sub` first** (a throwaway job bound to the `staging`
  environment printing only the payload's `sub`), and write the trust policy
  from what it printed, not from memory.
- Check: `aws iam simulate-principal-policy` for each identity (the IAM page
  gives the command). **Stop here if** any action is denied.

### 1.2 Certificate and domain

An ACM certificate in the region the stacks deploy to (`us-west-2`), covering
`app.<domain>`, with status `ISSUED`, before any `cdk deploy`. `PUBLIC_BASE_URL=https://app.<domain>`.

```bash
aws acm describe-certificate --certificate-arn "$CERTIFICATE_ARN" \
  --region us-west-2 --query Certificate.Status
```

It must print `"ISSUED"`. If it prints anything else, stop here.

Nothing in the CDK creates a DNS record. After the first `cdk deploy`, add an
alias A record for `app.<domain>` (and `api.<domain>` if you use it) pointing at
the load balancer `experimentation-<env>`; until then the hostname does not
resolve and `curl` reports `000`.
[AWS CDK Deployment, "Point the hostname at the load balancer"](../self-hosting/cdk.md#point-the-hostname-at-the-load-balancer)
has the commands and the check.

### 1.3 ECR repositories and the bootstrap images

**Two** repositories, created once per account and region; nothing in the CDK
creates them (a registry is account-scoped, the stacks are per-environment):

```bash
aws ecr create-repository \
  --repository-name experimentation-platform/backend \
  --region us-west-2 \
  --image-scanning-configuration scanOnPush=true

aws ecr create-repository \
  --repository-name experimentation-platform/web \
  --region us-west-2 \
  --image-scanning-configuration scanOnPush=true
```

Then push one image to each, tagged `bootstrap`, which the stacks' task
definitions name until the first deploy. `bootstrap` is a tag the Deploy workflow never writes, so the revision
CloudFormation registers cannot drift to a later build. Build from the
repository root with the profile you will deploy:

```bash
ECR_REGISTRY=<account>.dkr.ecr.us-west-2.amazonaws.com
aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"
docker build --target <core|full> -f backend/Dockerfile \
  -t "$ECR_REGISTRY/experimentation-platform/backend:bootstrap" .
docker push "$ECR_REGISTRY/experimentation-platform/backend:bootstrap"
docker build -f frontend/Dockerfile --build-arg EXPERIMENTLY_PROFILE=<core|full> \
  -t "$ECR_REGISTRY/experimentation-platform/web:bootstrap" .
docker push "$ECR_REGISTRY/experimentation-platform/web:bootstrap"
```

Both repositories survive a teardown (they are not in any stack).

### 1.4 Secrets

`/<env>/experimentation/jwt-secret` and `/<env>/experimentation/first-superuser-password`,
plus `/<env>/experimentation/audit-hmac-key` on `full`
([Secrets Management](secrets-management.md)). They must exist before the
Fargate and migrations stacks: ECS will not start a task whose definition
names a missing secret. The database and Redis need nothing from you -- the
stacks wire them.

The task definitions name each secret by its **complete ARN** (it ends in a
six-character suffix Secrets Manager chose), and the CDK takes the three ARNs
from your shell. Read them by name, then check each one by the ARN itself:
that is the string ECS will look up, and a check by name passes even when the
ARN is wrong. The third secret is for `full` only; on `core`, leave
`AUDIT_HMAC_KEY_SECRET_ARN` unset and the loop skips it.

```bash
ENV=staging
export JWT_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "/$ENV/experimentation/jwt-secret" --query ARN --output text)
export FIRST_SUPERUSER_PASSWORD_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "/$ENV/experimentation/first-superuser-password" --query ARN --output text)
export AUDIT_HMAC_KEY_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "/$ENV/experimentation/audit-hmac-key" --query ARN --output text)
for arn in "$JWT_SECRET_ARN" "$FIRST_SUPERUSER_PASSWORD_SECRET_ARN" ${AUDIT_HMAC_KEY_SECRET_ARN:+"$AUDIT_HMAC_KEY_SECRET_ARN"}; do
  aws secretsmanager describe-secret --secret-id "$arn" --query Name --output text
done
```

Keep the three
exported for every `cdk synth`, `cdk diff`, `cdk deploy` and `cdk destroy`
(section 1.6), and keep them out of issues, pull requests and chat: an ARN
carries the account ID. **Recreating a secret changes its ARN**: update the
input and run `cdk deploy` again ([Secrets Management](secrets-management.md#give-the-cdk-each-secrets-complete-arn)).

### 1.5 GitHub (repository administrator)

For each environment, **protection first**, then configuration:

1. Under **Settings → Environments**, give `<env>` a **required reviewer** and
   limit its **deployment branches** to `main`. Check it before going on:
   `gh api repos/<owner>/<repo>/environments/<env> --jq '.protection_rules, .deployment_branch_policy'`.
2. Secrets `AWS_ACCOUNT_ID` and `AWS_ROLE_ARN`; variable `PUBLIC_BASE_URL`.
   Setting them arms the workflows for that environment:
   `gh secret set AWS_ACCOUNT_ID --env <env>` (12 digits; it prompts for the
   value), `gh secret set AWS_ROLE_ARN --env <env>`, and
   `gh variable set PUBLIC_BASE_URL --env <env> --body https://app.<domain>`.
   The account ID is a **secret**, not a variable, because workflow logs are
   public: a step's log header prints its variables before anything can mask
   them. The workflows read `secrets.AWS_ACCOUNT_ID` only, so an
   `AWS_ACCOUNT_ID` variable left over from an older setup is ignored (and the
   run refuses as "not configured") -- move it to a secret, then delete the
   variable: `gh variable delete AWS_ACCOUNT_ID --env <env>`.

### 1.6 The stacks

Bootstrap once per account and region, then deploy the stacks:

```bash
cdk bootstrap aws://<account>/us-west-2

cd infrastructure/cdk
export ENVIRONMENT=staging CDK_DEFAULT_REGION=us-west-2
export CERTIFICATE_ARN=... PUBLIC_BASE_URL=https://app.<domain>
export ALARM_EMAIL=ops@your-domain.com
cdk deploy --all --require-approval never
```

`JWT_SECRET_ARN`, `FIRST_SUPERUSER_PASSWORD_SECRET_ARN` and (on `full`)
`AUDIT_HMAC_KEY_SECRET_ARN` are **required in every environment**: the
complete ARN of each secret, exported in this same shell as section 1.4 shows. Synth refuses one that is
missing, partial (no suffix), for a different environment (`/prod/...` in
staging), region or account, or in the wrong variable, and says how to read the right value.

`ALARM_EMAIL` is **required** for `staging` and `prod`: the one address every
alarm emails, including the two API 5xx alarms that roll a deployment back by
themselves. Synth refuses a missing value, a malformed one, or one at
`example.com`/`.net`/`.org`. Keep it exported for every later `cdk diff`,
`cdk deploy` and `cdk destroy` too.

**Confirm the subscription before the first release deploy.** SNS emails the
address a confirmation link. Until it is confirmed no alarm reaches anyone. Do
not click the link. Confirm the link's `Token=` value with
`aws sns confirm-subscription --topic-arn "$TOPIC_ARN" --token "$TOKEN_FROM_THE_LINK" --authenticate-on-unsubscribe true`,
then check the subscription is no longer `PendingConfirmation`
([AWS CDK Deployment](../self-hosting/cdk.md#required-environment-variables)
has the commands).

**`cdk deploy` does not create the database schema.** The API tasks run with
`RUN_MIGRATIONS=false` and `SEED=` (empty); only the migration task, which the
Deploy workflow runs before it shifts traffic, writes the schema. Until the
first Deploy (section 2) the API runs against an empty database: `/health`
answers 200 (through the hostname, once its alias record exists; section 1.2)
and the load balancer's health checks pass, while real requests
answer 500 and the API logs errors. Expect that, and run the first Deploy soon
after the stacks are up. If a request in that window has put
`experimentation-api-5xx-blue-<env>` or `-green-<env>` into ALARM, Deploy
refuses to start; the way through is Deploy's break-glass
([Rollback Runbook, "Fix forward while an alarm is firing"](rollback-runbook.md#fix-forward-while-an-alarm-is-firing)),
and a person decides at that moment whether to use it. Docker Compose and the
Helm chart are unchanged: compose runs one API container that migrates on
start, and the chart runs the migration in an init container
([Database Migrations](../self-hosting/migrations.md#on-aws-the-api-does-not-migrate)).

`cdk deploy --all` is for standing an environment up. On one that is already
running, deploy `experimentation-fargate-<env>` on its own, pinned to what is
live. Both pins go on **every** such deploy, because releases reach the API and
the dashboard outside CloudFormation and a `cdk deploy` undoes them silently:

- **The API's live target group.** CodeDeploy swaps the API's blue and green
  target groups outside CloudFormation, and a `cdk deploy` that names the empty
  one sends every API request to it while every probe stays green.
- **The dashboard's running digest.** The dashboard service rolls under the ECS
  deployment controller. Each deploy registers a task definition revision for
  it, by image digest, that CloudFormation never saw, and rolls the service
  onto it; before the first deploy it runs `:bootstrap` and the check says so. A `cdk deploy` that
  changes the dashboard's task definition in any way re-points the service at CloudFormation's own
  revision, whose image is `web:<dashboard_image_tag>` -- `web:bootstrap` when
  the value is not given. The dashboard silently goes back to the placeholder,
  and every probe stays green.

Two read-only checks print the values to pass, and refuse a value that does not
match what is running. Run them from the repository root: the first prints
`api_live_target_group`, the second `dashboard_image_tag`. The commands after
them print the image of the backend revision that is serving:

```bash
python3 scripts/check_live_target_group.py --env "$ENVIRONMENT"
python3 scripts/check_dashboard_image.py --env "$ENVIRONMENT"
RUNNING_TD=$(aws ecs describe-services --cluster "experimentation-$ENVIRONMENT" \
  --services "experimentation-backend-$ENVIRONMENT" \
  --query "services[0].taskSets[?status=='PRIMARY'].taskDefinition" --output text)
test -n "$RUNNING_TD" || { echo "no PRIMARY task set"; exit 1; }
aws ecs describe-task-definition --task-definition "$RUNNING_TD" \
  --query "taskDefinition.containerDefinitions[?name=='backend'].image" --output text
```

Set the three values you will pass: `BACKEND_TAG` is the backend image's tag
(or `bootstrap`), `API_LIVE` is what the first check printed (`blue` or
`green`), and `DASHBOARD_DIGEST` is what the second printed (`sha256:` and the
hex digest). Re-run both checks with them (each exits non-zero on a mismatch),
then preview the deploy with the same pins, writing the diff to a file:

```bash
BACKEND_TAG=bootstrap
API_LIVE=blue
DASHBOARD_DIGEST=sha256:0000000000000000000000000000000000000000000000000000000000000000
python3 scripts/check_live_target_group.py --env "$ENVIRONMENT" --expect "$API_LIVE" &&
  python3 scripts/check_dashboard_image.py --env "$ENVIRONMENT" --expect "$DASHBOARD_DIGEST" &&
  cdk diff "experimentation-fargate-$ENVIRONMENT" \
    -c backend_image_tag="$BACKEND_TAG" -c api_live_target_group="$API_LIVE" \
    -c dashboard_image_tag="$DASHBOARD_DIGEST" > "cdk-diff-$ENVIRONMENT.txt" 2>&1 &&
  grep -cE 'AWS::EC2::SecurityGroup(Egress|Ingress)' "cdk-diff-$ENVIRONMENT.txt"
```

Replace the three example values with yours before running anything. Each step
runs only if the one before it succeeded, so a check that refuses a value stops
the diff, and a diff that fails prints no count at all, which is not a `0`.
Read the whole file, not only the count. Do not pipe the diff into another command: a
pipe reports the last command's exit status, so a failed diff can look like a
clean one.

**Stop if the diff touches an ingress or egress rule.** Any line marked `[-]`,
`[+]` or `[~]` that names `AWS::EC2::SecurityGroupEgress` or
`AWS::EC2::SecurityGroupIngress` means stop: the `grep -cE` above must print
`0`. The load balancer reaches the API tasks over tcp 8000 and the dashboard
tasks over tcp 8080. Losing the 8000 rule breaks every `/api/v1` and `/health`
request through the load balancer, while the dashboard at `/` keeps working, so
the dashboard looking fine proves nothing. Losing the 8080 rule breaks the
dashboard at `/`. A count other than `0` is not something to fix at the
console: do not deploy, re-run both checks, and open an issue with the diff
file attached.

When the count is `0`, deploy with exactly the same pins:

```bash
cdk deploy "experimentation-fargate-$ENVIRONMENT" --require-approval never \
  -c backend_image_tag="$BACKEND_TAG" -c api_live_target_group="$API_LIVE" \
  -c dashboard_image_tag="$DASHBOARD_DIGEST"
```

Every deploy's run summary also prints the digest the dashboard is running
after it, as `-c dashboard_image_tag=sha256:<hex>` with the matching `--expect`
check. Re-run the check anyway before a `cdk deploy`: a Rollback or another
deploy since then moves it.

The dashboard pin is the **digest** the check prints (`sha256:<hex>`), not an
image tag: a tag can be moved to other bytes, and the digest synthesises the
image as `experimentation-platform/web@sha256:<hex>`, the exact image already
running. If the check reports that the dashboard runs `:bootstrap` -- no release
has reached it yet -- there is nothing to pin: leave `dashboard_image_tag` out
of both `cdk diff` and `cdk deploy`, and skip its `--expect`. An empty
`DASHBOARD_DIGEST` is not the same thing: synth refuses
`-c dashboard_image_tag=` with no value. If it reports any other tag, there is no digest to pin
either: `-c dashboard_image_tag=<that tag>` names the same reference, which may
since have been moved to other bytes.

Check: `aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE --query "StackSummaries[?contains(StackName, 'experimentation-')].StackName"`.
**Stop here if** a stack rolled back: the first missing secret or image is the
usual cause (section 7).

---

## 2. The first deploys, and the rollback rehearsal

In **staging**, before prod is ever deployed:

1. **Deploy tag A** (section 3). The first deploy's migration builds the
   schema from nothing (`backend.app.db.bootstrap`); until it has, the API
   answers real requests with 500 (section 1.6).
2. **Smoke through the public origin**: `curl -s https://app.<domain>/api/v1/experiments/`
   answers `401 {"detail":"Not authenticated"}`.
3. **Deploy tag B.**
4. **Roll back to A** with the line tag B's run summary printed
   ([Rollback Runbook](rollback-runbook.md)).
5. **Force an alarm during a canary.** Deploy tag B again once tag A's hour has
   passed, and while its canary runs set the green or blue alarm (whichever
   watches the new task set's target group) to ALARM by hand. Do the same once
   in the hour after a shift. The state set by hand lasts until the alarm's
   next evaluation; on staging on 2026-10-11 (UTC) the alarm was back to OK within
   two minutes ([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-6157399595)):

    ```{.bash skip reason="aws: forces a real alarm into ALARM in the staging account"}
    aws cloudwatch set-alarm-state --alarm-name experimentation-api-5xx-green-staging --state-value ALARM --state-reason "rehearsal: forced during the canary"
    ```

6. Tear down, and compare what is left with
   [what `cdk destroy` leaves behind](../self-hosting/cdk.md#what-cdk-destroy-leaves-behind-and-bills).

Tag B cannot be deployed within the hour after tag A's traffic shift: tag A's
CodeDeploy deployment is still active while the old task set is kept, and the
deploy refuses until it is not (section 3).

**What has run on staging:**

- On 2026-10-02 (UTC), tag A deployed
  ([run](https://github.com/Controlvar/controlvar/actions/runs/37070072132)), and tag B, dispatched inside the
  hour after A's shift, was refused before it changed anything
  ([run](https://github.com/Controlvar/controlvar/actions/runs/37074588665)).
- On 2026-10-04 (UTC), v0.18.0 deployed and the Rollback workflow put the API and the
  dashboard back on the previous release
  ([run](https://github.com/Controlvar/controlvar/actions/runs/37213010211),
  [run](https://github.com/Controlvar/controlvar/actions/runs/37217043185)).
- On 2026-10-11 (UTC), the Rollback workflow did it again
  ([run](https://github.com/Controlvar/controlvar/actions/runs/38098724258)), and a 5xx alarm forced into
  ALARM in the hour after a deploy's shift stopped that deployment, and
  CodeDeploy moved the API back by itself while the dashboard stayed on the new
  release ([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-6157399595)).

---

## 3. Every deploy

> **What has run against a real AWS account.** Deploy has run to success on a
> staging account, most recently v0.31.0 on 2026-10-13 (UTC)
> ([run](https://github.com/Controlvar/controlvar/actions/runs/38423251771)), and the Rollback workflow
> has run there end to end ([section 2](#2-the-first-deploys-and-the-rollback-rehearsal)).
> Deploy to staging before prod, and read the run and its summary closely.

1. **A release tag.** release-please cuts them; the Release Gate runs on every
   push to `main`, so the tag's commit has a result. For a tag cut before that:
   `gh workflow run release-gate.yml --ref vX.Y.Z`, then wait for it.
2. **Actions → Deploy → Run workflow**, "Use workflow from" **`main`** (any other
   ref is refused: the workflow's own tooling comes from it), then
   `environment`, `version` (the tag), `profile`.
3. **Preflight**, with no AWS credentials: the dispatch ref, the profile
   acknowledgement, that `version` matches `vX.Y.Z[-pre]`, exists, and is on
   `main`, and that its commit passed the Release Gate (the latest completed
   run wins; a run still in progress refuses).
4. **Approve** when the environment asks. Nothing in AWS has changed yet.
5. The deploy job then, in order: refuses an unconfigured environment, the
   wrong AWS account, missing stacks, a profile the stacks were not deployed
   for, missing secrets or either ECR repository, a CodeDeploy deployment of
   this environment that is still active, and a dashboard service that is
   missing, mid-rollout or not on the ECS rolling controller -- all before any
   change -- and then builds `:<tag>-<profile>` from the tag (or reuses it if an
   earlier deploy of the same release into this account already pushed it),
   checks the image's labels name the tag's commit, does the same for the
   dashboard image (`experimentation-platform/web:<tag>-<profile>`, the profile
   passed as a build argument), and only then
   snapshots the database (`pre-deploy-<env>-<tag>-<time>`), registers and
   runs the migration by digest, registers the API revision by digest, refuses
   it if what ECS stored does not set `RUN_MIGRATIONS=false` on the `backend`
   container, and creates the CodeDeploy deployment.
6. **The traffic shift.** When CodeDeploy reports the deployment `Ready`, the
   run checks that every target in the new task set's target group is
   `healthy` and that their number is the task set's desired count. Then it
   approves the shift (`continue-deployment --deployment-wait-type
   READY_WAIT`). The canary sends 10% of traffic, waits 15 minutes
   ([why 15](README.md#deploy)), then sends the rest. The step succeeds when
   the new revision is the PRIMARY task set **and** `scripts/check_live_target_group.py` confirms that the `/api/*`
   rule forwards to that task set's target group (`scripts/api_serving.py`).
   A rule still split between blue and green means the shift is in progress,
   and the step waits. It gives up after `CODEDEPLOY_DEADLINE_SECONDS` (40
   minutes: the canary's 15 and 25 for starting the tasks, the approval and
   the final shift) and **never stops the deployment**. CodeDeploy does, if
   one of the deployment group's alarms goes into ALARM: it stops the deployment and
   rolls the API back by itself, and the step ends "Rolled back by an alarm"
   (`result=alarm`), naming the alarm. There is nothing to roll back then; read
   [An alarm rolled the API back](rollback-runbook.md#an-alarm-rolled-the-api-back).
7. **The smoke request** (`GET ${PUBLIC_BASE_URL}/api/v1/experiments/` →
   `401 {"detail":"Not authenticated"}`). A failed smoke test does **not**
   roll back; the alarms may, while the deployment is active. If the API is
   still on the new release, the run summary gives the line:
   `Rollback: Actions → Rollback → environment=<env>, task_definition_arn=experimentation-backend-<env>:<n>`.
8. **The dashboard**, only after the API answered. The run registers the
   dashboard's task definition with the new image's digest, checks again that
   the API is still serving this run's revision (a Rollback run may have
   started meanwhile), and rolls the dashboard's service onto the new
   revision, refusing if the dashboard is no longer on the revision it
   recorded before anything changed. It waits on the PRIMARY deployment until
   it is the new revision, `COMPLETED`, with every task running: a rollout the
   circuit breaker rolled back is red, not "stable". Then
   `GET ${PUBLIC_BASE_URL}/` must answer 200 with `text/html`. The **last**
   check of the run is the API check once more.
9. **The summary** names the live API target group and the dashboard's running
   digest, with the values for the next `cdk deploy` of
   `experimentation-fargate-<env>`: `-c api_live_target_group=<blue|green>` and
   `-c dashboard_image_tag=sha256:<hex>` ([section 1.6](#16-the-stacks)), and a
   rollback line carrying both revisions.

The deploy job's timeout is 160 minutes: every wait in it is a named bound
(snapshot, migration, traffic shift, dashboard rollout, the two smoke tests)
and the sum leaves room for two uncached image builds.

**Alarms watch the API until the deployment ends**
([#148](https://github.com/Controlvar/controlvar/issues/148)). The deployment group has
two alarms, `experimentation-api-5xx-blue-<env>` and
`experimentation-api-5xx-green-<env>`, one per target group, because
CodeDeploy swaps the two on every deployment. Both are polled for the whole
deployment: before the approval, in the canary, and in the hour after the
shift while CodeDeploy keeps the previous task set. While either is in ALARM,
CodeDeploy stops the deployment and rolls the API back to the previous
revision by itself. On 2026-10-03 (UTC) CodeDeploy stopped and reverted an API deployment
on staging when an alarm went into ALARM
([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-5971647359)),
and on 2026-10-11 (UTC) an alarm forced in the hour after the shift did the same
([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-6157399595)). An alarm in the time before the approval has not been rehearsed
on AWS as of v0.31.1. An alarm fires on a minute with at least 5 target 5xx
**and** at least 5% of that target group's requests, for 2 minutes of 3; a
minute with no requests counts as fine. What they do not catch:

- a release that answers wrongly with a 2xx, or with fewer errors than that;
- the load balancer's own 502 and 504 (a task that crashed or timed out);
- the dashboard: an alarm rolls back the API only, and a rollback in the hour
  after the shift leaves the dashboard on the new release;
- anything after that hour, when the response is the **Rollback** workflow.

Two more things follow from the alarms watching both target groups. A live
release already answering 5xx at the threshold (its own bug, or a failing
dependency) stops every new deploy too, the fix included: see
[Fix forward while an alarm is firing](rollback-runbook.md#fix-forward-while-an-alarm-is-firing).
And every 5xx the API answers counts, its deliberate ones included: `501` on a
module's route under the core profile, and `503` from `/health/ready` for a
caller other than the load balancer. A rollback in the hour after the shift is not announced in the
`#deployments` channel; the run summary gives the time the alarms stop watching and the
command that shows whether one happened.

**One deploy per environment per hour.** For an hour after the shift,
CodeDeploy keeps the previous task set, and the deployment stays active that
whole time. It accepts no second deployment meanwhile. So a deploy dispatched
within that hour is refused at once, before anything is built, naming the
deployment and roughly how long it has left. A deployment that was never
approved stays active until its 30-minute approval wait ends. The deploy never
stops a deployment itself. Rollback does, on purpose.

**Shipping a fix within that hour means Rollback first.** A fix-forward deploy is
refused while the bad release's deployment is active. Run **Rollback**, which
stops that deployment and puts the previous revision back, and deploy the fix
afterwards. If an alarm already rolled the release back, do not run Rollback
for it: the API is already going back, and Rollback refuses while CodeDeploy's
own rollback is active
([An alarm rolled the API back](rollback-runbook.md#an-alarm-rolled-the-api-back)). Rollback's own CodeDeploy deployment is then the active one, and
the next forward deploy is refused until it is no longer active: about another
hour, because the 60-minute termination wait is a setting of the deployment
group (the synthesised template's
`TerminateBlueInstancesOnDeploymentSuccess.TerminationWaitTimeInMinutes: 60`)
and `create-deployment` has no parameter that overrides it. Rollback's
all-at-once `--deployment-config-name` changes how traffic moves, not how long
the old task set is kept. On staging on 2026-10-11 (UTC), the Rollback run's
deployment stayed active for about an hour after its traffic shift
([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-6157399595)).

One deploy or manual migration per environment at a time.

### Backward-compatible migrations

A deploy migrates **before** it shifts traffic, so for a while -- and for as
long as a failed or stopped deploy leaves it so -- the previous release runs
against the new schema. So write every migration so that the previous release
can run against it: add columns and tables (nullable, or with defaults), and
never drop or rename, in the same release, one the previous release reads. Drop
in a later release, once nothing running reads it.

A rollback relies on the same rule. The API tasks do not migrate on start, so
a rolled-back release runs against the newer schema as it is; it works only if
every migration since that release is backward-compatible. For one that is
not, the order matters: undo the migration while the release that contains it
is still serving, and only then roll the API back
([Rollback Runbook, Database Rollback Procedure](rollback-runbook.md#database-rollback-procedure),
[Step 2](rollback-runbook.md#step-2-run-migration-downgrade-via-github-actions)). Once the API has been rolled back, the workflow can no longer
undo it. The emergency route is then a point-in-time restore (the same
procedure, Step 3).

---

## 4. Pre-Deployment Checklist

### Mandatory Checks

- [ ] The tag's commit passed the Release Gate; the Deploy workflow refuses it otherwise
- [ ] For prod: the same tag deployed to staging and smoke-tested

### Database Migration Checks (if migration is included)

- [ ] Migration applied in staging — no errors, no data loss
- [ ] Migration is [backward-compatible](#backward-compatible-migrations)
- [ ] The downgrade tested in staging (Actions → Database Migration, `downgrade`, target: the new migration's `down_revision` id)

---

## 5. Post-Deployment Verification

The block sets `ENV=staging`; for production, change it to `ENV=prod`.
`/health` is the readiness check: look at `checks.database.status` in what it
prints. The unauthenticated request to `/api/v1/experiments/` answers
`401 {"detail":"Not authenticated"}`. The last two commands are the dashboard's:
`GET /` answers 200 `text/html`, and its PRIMARY deployment names the new
revision, `COMPLETED`:

```bash
ENV=staging
BASE=https://app.<domain>

curl -s "$BASE/health"
curl -s "$BASE/api/v1/experiments/"

aws ecs describe-services --cluster "experimentation-$ENV" \
  --services "experimentation-backend-$ENV" \
  --output json \
  --query "services[0].taskSets[?status=='PRIMARY'] | [0].{Serving:taskDefinition,Running:runningCount,Desired:computedDesiredCount,Pending:pendingCount}"

curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' "$BASE/"
aws ecs describe-services --cluster "experimentation-$ENV" \
  --services "experimentation-dashboard-$ENV" \
  --query "services[0].deployments[?status=='PRIMARY'].{td:taskDefinition,state:rolloutState,running:runningCount,desired:desiredCount}"
```

Expected: `Running == Desired` and `Pending` 0, and `Serving` the revision the
run summary named. These are the API's PRIMARY task set's own counts; the
service's `runningCount` also counts the replaced task set for the hour after
a shift, so it reads higher than `Desired` then. The dashboard's one PRIMARY
deployment on the new dashboard revision, `COMPLETED`, running equal to
desired.

---

## 6. Deployment Architecture

```
Actions → Deploy (from main; environment = staging | prod)
  preflight   no credentials: ref, profile, tag on main, Release Gate
  deploy      one approval, then:
                refusals (account, stacks, profile, secrets, ECR)
                build :<tag>-<profile> from ./release (or reuse it)
                snapshot  →  migration (by digest)  →  API revision (by digest)
                CodeDeploy blue/green  →  healthy targets  →  approve  →
                canary (alarms watched)  →  PRIMARY + /api/* rule  →  smoke  →
                API check  →  dashboard rollout (by digest)  →  dashboard smoke  →
                API check  →  summary
              then, after the run has ended:
                the hour CodeDeploy keeps the old task set (alarms watched)
      |
      v
ECS cluster experimentation-<env>
  service experimentation-backend-<env>     image .../backend@sha256:...
  service experimentation-dashboard-<env>   image .../web@sha256:...
  secrets  /<env>/experimentation/*  and the Aurora-generated credentials
  Aurora (identifier: stack output)  ·  Redis over TLS (REDIS_SSL=true)
```

---

## 7. Troubleshooting

### "Deployment stuck in Pending"

```bash
ENV=staging
aws ecs describe-tasks --cluster "experimentation-$ENV" \
  --tasks $(aws ecs list-tasks --cluster "experimentation-$ENV" --query 'taskArns[0]' --output text) \
  --query 'tasks[0].{Status:lastStatus,StopCode:stopCode,StopReason:stoppedReason}'
```

### "Migration task failed"

The run prints the task's last 100 log lines. The full log is CloudWatch log
group `/ecs/experimentation-migrate-<env>`, stream `migrate/backend/<task id>`.
A migration that failed half-way needs its downgrade before a re-deploy
([Rollback Runbook](rollback-runbook.md)).

### "A deployment is still active"

The previous deploy's CodeDeploy deployment is still active: it is inside the
hour CodeDeploy keeps the old task set, or it is waiting for an approval that
the deploy never sent. The refusal names it and estimates how long it has
left. Wait and re-run. If the release it deployed is bad, run **Rollback**,
which stops it and puts the older revision back. Do not stop it by hand with
`--auto-rollback-enabled` unless rolling that release back is what you want.
If the refusal says it is CodeDeploy's own rollback of an earlier deployment,
an alarm (or a failed deployment) is already putting the API back: wait for
it, and do not use Rollback, which refuses while it is active.

### "Rolled back by an alarm"

One of the deployment group's two 5xx alarms went into ALARM, and CodeDeploy
stopped the deployment and rolled the API back by itself. Read
`errorInformation` first, then follow
[An alarm rolled the API back](rollback-runbook.md#an-alarm-rolled-the-api-back).
If it says "Stopped by an alarm" instead, the alarm was already firing before
the traffic shift and nothing shifted:
[Fix forward while an alarm is firing](rollback-runbook.md#fix-forward-while-an-alarm-is-firing).

### "This API revision would run migrations on start"

The API revision this run registered, copied from the family's newest one,
does not set `RUN_MIGRATIONS=false`, most likely because the Fargate stack was
last deployed from a checkout older than 0.14.0. No deployment was created and the previous revision
keeps serving; the migration **has** been applied. Deploy the Fargate stack from
a current checkout, pinned (section 1.6), then deploy again:
[Rollback Runbook](rollback-runbook.md#deploy-refused-an-api-revision-that-would-run-migrations-on-start).

### "Traffic shift not approved"

The new task set's targets never all became healthy before the deadline, so
the deploy did not approve the shift. Nothing moved, and the previous revision
keeps serving. CodeDeploy stops the deployment when its 30-minute approval wait
ends. The migration **has** been applied. Read the API's log
(`/ecs/experimentation-backend-<env>`) for why the new tasks fail their
health check.

### "Traffic shift not confirmed"

The deploy approved the shift, and when its deadline passed the new revision
was not yet serving through the `/api/*` rule. **The deployment was not
stopped**, because stopping it could roll back a release that is succeeding.
Watch it with `aws deploy get-deployment --deployment-id <id>` and
`python3 scripts/api_serving.py experimentation-<env> experimentation-backend-<env> <new revision ARN>`
(exit 0 means it is serving). If the release is bad, use the rollback line.

### The API route and the PRIMARY task set disagree

The new revision is the PRIMARY task set, but the HTTPS listener's `/api/*`
rule forwards to the *other* target group, with no split. The API route is not
where the new tasks are. When the old task set is terminated, an hour after
the shift, every API request gets a 503.

Until then the old task set keeps answering through the rule, so users see
the *previous* release, not the new one.

First check `aws deploy get-deployment --deployment-id <id>`. If it is
`Stopped` with `ALARM_ACTIVE`, this is not that question: an alarm stopped the
deployment and CodeDeploy is already rolling the API back, putting the rule
back first. Do not dispatch Rollback; read
[An alarm rolled the API back](rollback-runbook.md#an-alarm-rolled-the-api-back).
The deploy re-reads the deployment for about a minute before it reports this,
for that reason. Otherwise:

1. Roll back within the hour, with the rollback line in the run summary. Then
   confirm with `python3 scripts/check_live_target_group.py --env <env>` that
   the rule names the group the previous revision runs in.
2. Before any further deploy, record the observation.

### The dashboard did not roll out

These all come after the API has shifted, so the API is new and the dashboard
is not (or its state is in question). Three things hold for each:

- **Deploy refuses a re-run** until the CodeDeploy deployment of this run is no
  longer active, about an hour after its traffic shift.
- **The rollback line in the run summary reverts the API too**, not only the
  dashboard.
- **The dashboard alone**, inside that hour, is the
  [rollback runbook](rollback-runbook.md)'s Method 2, dashboard block.

The titles, and what each means:

- **"Dashboard not rolled out"** -- the API check before the rollout failed, so
  the dashboard was not touched. The line above it is the check's own: another
  revision is PRIMARY or a traffic shift is in progress (exit 1), the check
  could not tell because an AWS call failed or it crashed (exit 2), or the route and the tasks
  disagree (exit 3). Find out what changed the API (usually a Rollback run)
  before anything else.
- **"… rolled back by ECS"** -- the new dashboard's tasks did not become
  healthy, and the deployment circuit breaker put the previous revision back.
  The task log is `/ecs/experimentation-dashboard-<env>`.
- **"… changed during this run"** -- the dashboard's PRIMARY deployment is
  neither this run's revision nor the one before it: a Rollback run, another
  deploy or a `cdk deploy` changed it. This run did not change it back.
- **"… rollout did not finish"** -- the deadline passed with ECS still working
  on it. The run stopped waiting and stopped nothing; watch the service.
- **"Workflow role cannot update …"** -- the role predates the dashboard
  rollout; re-apply the policy ([IAM permissions](iam-permissions.md)).
- **"Dashboard smoke test failed"** -- ECS reports the new revision serving,
  but `GET /` did not answer 200 `text/html`: check the HTTPS listener's
  default action in `experimentation-fargate-<env>`.
- **"API changed after the dashboard rolled out"** -- the last API check
  failed: the dashboard is new and the API may not be. Check both before
  anything else (`scripts/api_serving.py`, and the dashboard describe in
  section 5).
- **"API rolled back by an alarm, dashboard not"** -- the exception to the three
  points above. An API check failed because an alarm stopped the deployment
  after its shift, and CodeDeploy is moving the API back by itself. The
  deployment is stopped, so a re-run is not refused for the hour, and there is
  nothing to roll back for the API: do not use the rollback line. If the
  dashboard is on this run's revision, put it back to match with Method 2's
  dashboard block, and read
  [An alarm rolled the API back](rollback-runbook.md#an-alarm-rolled-the-api-back).

### "Smoke test failing after deployment"

The smoke test rolls nothing back. The API's alarms may, while the run's
CodeDeploy deployment is active: if the API is already back on the previous
revision, read
[An alarm rolled the API back](rollback-runbook.md#an-alarm-rolled-the-api-back).
Otherwise decide, and use the rollback line in the run summary. The API's log
is `/ecs/experimentation-backend-<env>`.
