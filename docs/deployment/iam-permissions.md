# IAM permissions for deploying

Three identities touch an AWS account that runs Controlvar, and they need
different things. Keep them separate: the one that deploys releases every week
should not be able to rewrite the account.

| Identity | Who uses it | When | What it needs |
|---|---|---|---|
| **Bootstrap** | a human, once per account and region | `cdk bootstrap` | create the CDK toolkit stack (below) |
| **`cdk deploy`** | a human standing up or changing an environment | `ENVIRONMENT=<env> cdk deploy …` | `sts:AssumeRole` on the bootstrap's `cdk-*` roles, and a few one-off extras |
| **Workflow role** | GitHub Actions, through OIDC | Deploy, Rollback, Database Migration | exactly the generated list below |

Nothing in this repository creates any of them. They are set up by a person
with IAM rights in the account, before the first deploy
([deployment guide, section 1](deployment-guide.md#1-before-the-first-deploy)).

## 1. The bootstrap identity

`cdk bootstrap aws://<account>/<region>` creates the `CDKToolkit`
CloudFormation stack: an S3 bucket and an ECR repository for assets, SSM
parameter `/cdk-bootstrap/hnb659fds/version`, and the IAM roles
`cdk-hnb659fds-{deploy,cfn-exec,file-publishing,image-publishing,lookup}-role-<account>-<region>`.
It creates them with its caller's own permissions, IAM roles included, so run
it once, as an administrator of the account. Nothing afterwards should use it.

## 2. The `cdk deploy` identity

With a modern bootstrap, `cdk deploy` does not use its caller's permissions to
create resources: it assumes the bootstrap roles, and CloudFormation acts as
`cdk-hnb659fds-cfn-exec-role-…`. The caller needs:

- `sts:AssumeRole` on `arn:aws:iam::<account>:role/cdk-hnb659fds-*`;
- for the one-off steps the deployment guide has them do by hand:
  `ecr:CreateRepository` and push rights on `experimentation-platform/backend`
  and `experimentation-platform/web`, `secretsmanager:CreateSecret` on
  `/<env>/experimentation/*`, and `acm:RequestCertificate` /
  `acm:DescribeCertificate` (plus `route53:ChangeResourceRecordSets` if DNS is
  in Route 53).

Check it before relying on it:

```bash
aws iam simulate-principal-policy \
  --policy-source-arn <the identity's ARN> \
  --action-names sts:AssumeRole \
  --resource-arns "arn:aws:iam::<account>:role/cdk-hnb659fds-deploy-role-<account>-<region>"
```

## 3. The workflow role (GitHub OIDC)

One role **per environment**, each trusted only by jobs bound to that GitHub
environment, and each holding the same permissions policy.

### Trust policy

[`infrastructure/cdk/github-actions-trust-policy.json`](https://github.com/Controlvar/controlvar/blob/main/infrastructure/cdk/github-actions-trust-policy.json)
is the template. Replace `<AWS_ACCOUNT_ID>` and `<ENVIRONMENT>` (`staging` or
`prod`) and create one role from each copy. It pins the token's `sub` claim
exactly -- `StringEquals`, no wildcard:

```
repo:Controlvar@341280474/controlvar@1367480368:environment:<ENVIRONMENT>
```

That is GitHub's **immutable subject**: the owner and the repository by name
*and* numeric id. On 2026-10-02 (UTC) a job of this repository bound to the
`staging` environment received
`repo:getexperimently@328439352/experimently@1367480368:environment:staging`
([run](https://github.com/Controlvar/controlvar/actions/runs/36972395116)).
On 2026-10-17 (UTC), after the repository moved, a job bound to the `staging`
environment received `repo:Controlvar@341280474/controlvar@1367480368:environment:staging`
([run](https://github.com/Controlvar/controlvar/actions/runs/39047349118)).
A fork or a copy of this repository has another prefix: the OIDC subject probe
below prints yours. The `:environment:<name>` suffix is what a job that declares
`environment:` receives, which is the only kind of job in these workflows that
assumes the role.

**Before writing the policy, decode a real token.** Before the first staging deploy, run a
throwaway job bound to the `staging` environment that prints only the
*payload* of its OIDC token's `sub` claim, and the trust policy is written
from what it printed. The **OIDC subject probe** workflow
(`.github/workflows/oidc-sub-probe.yml`; Actions → OIDC subject probe → Run
workflow, from `main`) is that job: it prints `sub`, `aud` and `environment`
and never the token. Protect the environment first (see
[GitHub side](#github-side)). A `sub` that does not match fails `AssumeRoleWithWebIdentity`
with "Not authorized", which is the safe direction.

The account also needs the GitHub OIDC provider
(`token.actions.githubusercontent.com`, audience `sts.amazonaws.com`), once.

### Session length

Each environment role needs a **`MaxSessionDuration` of 10800 seconds (3
hours)**. The workflows ask for a session as long as the job may run:
`role-duration-seconds` is 9600 in `deploy.yml` (a 160-minute job) and 5400 in
`db-migrate.yml` and `rollback.yml` (90-minute jobs). IAM's default maximum is
one hour, and a request above the role's maximum fails at the "Configure AWS
credentials" step, before anything has changed.

Create the role with `--max-session-duration 10800`. For a role that already
exists:

```bash
aws iam update-role --role-name <ROLE_NAME> --max-session-duration 10800
```

### Permissions policy

[`infrastructure/cdk/github-actions-deploy-policy.json`](https://github.com/Controlvar/controlvar/blob/main/infrastructure/cdk/github-actions-deploy-policy.json),
generated -- like the table below -- by `scripts/iam_actions.py` from every
`aws <service> <verb>` in `deploy.yml`, `rollback.yml`, `db-migrate.yml`, the
local actions under `.github/actions/`, the scripts those run, and any script
listed in `STAGED_SCRIPTS` there (one the role will run that no workflow names
yet, so the policy can be re-applied before the deploy that needs it). A unit test
fails when a workflow gains a call the committed copies do not grant, so the
list cannot fall behind the workflows. `iam:PassRole` is granted only for
passing roles to ECS tasks.

Resources are `*`, except `rds:AddTagsToResource`, which is granted only on
cluster snapshots (below).

`ecs:UpdateService` is on `*` too, and that is a deliberate choice, not an
oversight. It is there for `scripts/ecs_rolling_rollout.sh`, which rolls the
dashboard service out to a new revision, but the action can also change any
service's desired count, load balancers and deployment controller, the API's
included. It adds no reach the role lacks: the role already holds
`ecs:RunTask`, `ecs:RegisterTaskDefinition` and `iam:PassRole` to ECS tasks on
`*`, which together run any image with any task role in the account, so that
grant dominates it.

Six of the actions are not visible in the workflow text and come from AWS's
documentation of what the call needs: `iam:PassRole` (registering and running
a task definition that names roles), `codedeploy:GetDeploymentConfig` /
`codedeploy:RegisterApplicationRevision` / `codedeploy:GetApplicationRevision`
(creating a deployment from an AppSpec, below),
`rds:AddTagsToResource` (a cluster snapshot that copies the cluster's tags,
below), and `codedeploy:UpdateDeploymentGroup` (creating one with
`--override-alarm-configuration`, below). Confirm the whole list with
`aws iam simulate-principal-policy` before the first staging deploy.

`codedeploy:UpdateDeploymentGroup` is there because of a flag, not a verb
(#148). The API's deployment group has two 5xx alarms, and CodeDeploy stops
every deployment to the group while one of them is in ALARM. The release
being rolled back is usually what holds it there, so Rollback creates its
deployment with the group's alarms overridden (`--override-alarm-configuration`),
always, and Deploy does the same only under its break-glass input
([Fix forward while an alarm is firing](rollback-runbook.md#fix-forward-while-an-alarm-is-firing)).
Overriding alarms on `CreateDeployment` needs the `UpdateDeploymentGroup`
permission, which `scripts/iam_actions.py` grants for that flag; there is no
narrower action. The role can already create a deployment of any AppSpec, and
it cannot change the group's service role, because `iam:PassRole` is granted
only to ECS tasks.

**A role created before an update to this policy must have it re-applied.** The
policy is applied by hand, so a role keeps whatever it was given. The
forward-deploy traffic shift (#143) added four read actions:
`cloudformation:DescribeStackResources`, `elasticloadbalancing:DescribeListeners`,
`elasticloadbalancing:DescribeRules` and `elasticloadbalancing:DescribeTargetHealth`.
A role without them fails the deploy red at the traffic shift. The run stops
with "Could not tell whether the API is serving" (`AccessDenied` on
`describe-target-health` once the deployment is `Ready`), and it never approves
the shift. Nothing shifts, and CodeDeploy stops the unapproved deployment when
its 30-minute approval wait ends. Re-apply
`infrastructure/cdk/github-actions-deploy-policy.json` to every environment's
role before its next deploy.

The dashboard rollout (#69) adds `ecs:UpdateService` and `ecs:ListTasks`: Deploy
and Rollback both run `scripts/ecs_rolling_rollout.sh`, which points the
dashboard's service at a new revision and, when a rollout fails, lists its
stopped tasks.
Roles created from a policy before the dashboard rollout lack `ecs:UpdateService`
and `ecs:ListTasks`; re-apply this policy in every account before the first
deploy of a release that includes the dashboard rollout. A role
without them stops at the dashboard's `update-service` with "ecs:UpdateService
was denied", after the API has already shifted, and Deploy then refuses a re-run
until that CodeDeploy deployment is no longer active (about an hour).

The API's alarms (#148) add `codedeploy:UpdateDeploymentGroup` and a
`codedeploy:GetDeployment` read in Deploy. Roles created from a policy before
the alarms lack `codedeploy:UpdateDeploymentGroup`; re-apply this policy in
every account before the first `cdk deploy` that adds the alarms. Without it,
Rollback's `create-deployment` is expected to be refused with `AccessDenied` in
the middle of an incident, and so is a Deploy with the alarm override; that has
not been rehearsed on AWS as of v0.31.1. The deployment group's own read of its alarms
(`cloudwatch:DescribeAlarms`) is on CodeDeploy's service role, which the CDK
creates; the workflow role's grant of the same action, below, is a separate
one.

Deploy's alarm pre-flight (#297, `scripts/refuse_alarm_active.py`) adds two
reads: `codedeploy:GetDeploymentGroup`, for the alarm names the group polls,
and `cloudwatch:DescribeAlarms`, for their states. Roles created from a policy
before the pre-flight lack `codedeploy:GetDeploymentGroup` and
`cloudwatch:DescribeAlarms`; re-apply this policy in every account before the
first deploy of a release that includes the pre-flight. The failure without
them is loud and safe: every deploy stops at "Could not read the API's
alarms", before anything is built, snapshotted or migrated.

The pre-migration snapshot (#744) adds `rds:AddTagsToResource`, which no
command names: the CDK's Aurora cluster copies its tags to each snapshot, and
AWS authorizes that copy as a tagging call on the new snapshot.
`scripts/iam_actions.py` grants it from its `IMPLIED` table, only on cluster
snapshots (`arn:aws:rds:*:*:cluster-snapshot:*`). Roles created from a policy
before #744 lack `rds:AddTagsToResource`; re-apply this policy in every
account before the next deploy. The failure without it is loud and safe:
Deploy's "Snapshot the database before migrating" step (and Database
Migration's "Take a snapshot first") stops with "not authorized to perform:
rds:AddTagsToResource on resource: arn:aws:rds:...:cluster-snapshot:...",
and nothing is snapshotted, migrated or shifted.

`codedeploy:GetApplicationRevision` (#754) was added after the staging
rollback rehearsal on 2026-10-03 (UTC), where its absence stopped Rollback
([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-5963917501)). Roles created from a
policy before #754 lack it; re-apply this policy in every account before the
next rollback. The failure without it comes after the in-flight deployment has
been stopped: Rollback's "Roll back via CodeDeploy" step stops with "not
authorized to perform: codedeploy:GetApplicationRevision", and no rollback
deployment is created.

<!-- BEGIN GENERATED by scripts/iam_actions.py; do not edit by hand -->
| Action | Needed by |
|---|---|
| `cloudformation:DescribeStackResources` | `scripts/check_live_target_group.py: aws cloudformation describe-stack-resources` |
| `cloudformation:DescribeStacks` | `.github/actions/stack-outputs/action.yml: aws cloudformation describe-stacks` |
| `cloudwatch:DescribeAlarms` | `scripts/refuse_alarm_active.py: aws cloudwatch describe-alarms` |
| `codedeploy:ContinueDeployment` | `.github/workflows/rollback.yml: aws deploy continue-deployment`<br>`scripts/shift_traffic.py: aws deploy continue-deployment` |
| `codedeploy:CreateDeployment` | `.github/workflows/deploy.yml: aws deploy create-deployment`<br>`.github/workflows/rollback.yml: aws deploy create-deployment` |
| `codedeploy:GetApplicationRevision` | `.github/workflows/deploy.yml: aws deploy create-deployment`<br>`.github/workflows/rollback.yml: aws deploy create-deployment` |
| `codedeploy:GetDeployment` | `.github/workflows/deploy.yml: aws deploy get-deployment`<br>`.github/workflows/rollback.yml: aws deploy get-deployment`<br>`scripts/refuse_active_deployment.py: aws deploy get-deployment`<br>`scripts/shift_traffic.py: aws deploy get-deployment` |
| `codedeploy:GetDeploymentConfig` | `.github/workflows/deploy.yml: aws deploy create-deployment`<br>`.github/workflows/rollback.yml: aws deploy create-deployment` |
| `codedeploy:GetDeploymentGroup` | `scripts/refuse_alarm_active.py: aws deploy get-deployment-group` |
| `codedeploy:ListDeployments` | `.github/workflows/rollback.yml: aws deploy list-deployments`<br>`scripts/refuse_active_deployment.py: aws deploy list-deployments` |
| `codedeploy:RegisterApplicationRevision` | `.github/workflows/deploy.yml: aws deploy create-deployment`<br>`.github/workflows/rollback.yml: aws deploy create-deployment` |
| `codedeploy:StopDeployment` | `.github/workflows/rollback.yml: aws deploy stop-deployment` |
| `codedeploy:UpdateDeploymentGroup` | `.github/workflows/deploy.yml: aws deploy create-deployment --override-alarm-configuration`<br>`.github/workflows/rollback.yml: aws deploy create-deployment --override-alarm-configuration` |
| `ecr:BatchCheckLayerAvailability` | `.github/workflows/deploy.yml: docker pull`<br>`.github/workflows/deploy.yml: docker push` |
| `ecr:BatchGetImage` | `.github/workflows/deploy.yml: docker pull` |
| `ecr:CompleteLayerUpload` | `.github/workflows/deploy.yml: docker push` |
| `ecr:DescribeImages` | `.github/workflows/deploy.yml: aws ecr describe-images` |
| `ecr:DescribeRepositories` | `.github/workflows/deploy.yml: aws ecr describe-repositories` |
| `ecr:GetAuthorizationToken` | `.github/workflows/deploy.yml: uses aws-actions/amazon-ecr-login` |
| `ecr:GetDownloadUrlForLayer` | `.github/workflows/deploy.yml: docker pull` |
| `ecr:InitiateLayerUpload` | `.github/workflows/deploy.yml: docker push` |
| `ecr:PutImage` | `.github/workflows/deploy.yml: docker push` |
| `ecr:UploadLayerPart` | `.github/workflows/deploy.yml: docker push` |
| `ecs:DescribeClusters` | `.github/workflows/deploy.yml: aws ecs describe-clusters` |
| `ecs:DescribeServices` | `.github/workflows/db-migrate.yml: aws ecs describe-services`<br>`.github/workflows/deploy.yml: aws ecs describe-services`<br>`.github/workflows/rollback.yml: aws ecs describe-services`<br>`scripts/check_dashboard_image.py: aws ecs describe-services`<br>`scripts/check_live_target_group.py: aws ecs describe-services`<br>`scripts/ecs_rolling_rollout.sh: aws ecs describe-services`<br>`scripts/shift_traffic.py: aws ecs describe-services` |
| `ecs:DescribeTaskDefinition` | `.github/workflows/db-migrate.yml: aws ecs describe-task-definition`<br>`.github/workflows/deploy.yml: aws ecs describe-task-definition`<br>`.github/workflows/rollback.yml: aws ecs describe-task-definition`<br>`scripts/check_dashboard_image.py: aws ecs describe-task-definition`<br>`scripts/check_task_secrets.py: aws ecs describe-task-definition`<br>`scripts/ecs_rolling_rollout.sh: aws ecs describe-task-definition`<br>`scripts/refuse_migrating_api_revision.py: aws ecs describe-task-definition`<br>`scripts/register_task_definition.sh: aws ecs describe-task-definition` |
| `ecs:DescribeTasks` | `scripts/ecs_rolling_rollout.sh: aws ecs describe-tasks`<br>`scripts/run_migration_task.sh: aws ecs describe-tasks` |
| `ecs:ListTasks` | `scripts/ecs_rolling_rollout.sh: aws ecs list-tasks` |
| `ecs:RegisterTaskDefinition` | `scripts/register_task_definition.sh: aws ecs register-task-definition` |
| `ecs:RunTask` | `scripts/run_migration_task.sh: aws ecs run-task` |
| `ecs:UpdateService` | `scripts/ecs_rolling_rollout.sh: aws ecs update-service` |
| `elasticloadbalancing:DescribeListeners` | `scripts/check_live_target_group.py: aws elbv2 describe-listeners` |
| `elasticloadbalancing:DescribeRules` | `scripts/check_live_target_group.py: aws elbv2 describe-rules` |
| `elasticloadbalancing:DescribeTargetHealth` | `scripts/shift_traffic.py: aws elbv2 describe-target-health` |
| `iam:PassRole` | `scripts/register_task_definition.sh: aws ecs register-task-definition`<br>`scripts/run_migration_task.sh: aws ecs run-task` |
| `logs:GetLogEvents` | `scripts/run_migration_task.sh: aws logs get-log-events` |
| `rds:AddTagsToResource` | `.github/workflows/db-migrate.yml: aws rds create-db-cluster-snapshot`<br>`.github/workflows/deploy.yml: aws rds create-db-cluster-snapshot` |
| `rds:CreateDBClusterSnapshot` | `.github/workflows/db-migrate.yml: aws rds create-db-cluster-snapshot`<br>`.github/workflows/deploy.yml: aws rds create-db-cluster-snapshot` |
| `rds:DescribeDBClusterSnapshots` | `.github/workflows/db-migrate.yml: aws rds wait db-cluster-snapshot-available`<br>`.github/workflows/deploy.yml: aws rds wait db-cluster-snapshot-available` |
| `secretsmanager:DescribeSecret` | `scripts/check_task_secrets.py: aws secretsmanager describe-secret` |
<!-- END GENERATED -->

### GitHub side

Each environment's protection, its `AWS_ACCOUNT_ID` and `AWS_ROLE_ARN`
secrets and its `PUBLIC_BASE_URL` variable are
[section 1.5 of the deployment guide](deployment-guide.md#15-github-repository-administrator).

`SLACK_BOT_TOKEN` may be a repository secret; it is optional.
