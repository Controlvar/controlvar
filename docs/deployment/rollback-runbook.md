# Rollback Runbook — Controlvar

**Audience:** On-call Engineers
**Measured:** on 2026-10-11 (UTC), on a staging account, the Rollback job took 5 min
56 s once approved: the API was verified on the previous revision after 3 min
27 s, and the dashboard 2 min 27 s later
([run](https://github.com/Controlvar/controlvar/actions/runs/38098724258)).

Every command below is written for either environment. Set this first, in the
shell you will paste into. The line names `prod`; to rehearse, or to roll back
staging, change it to `export ENV=staging`:

```bash
export ENV=prod
```

**Rehearse it.** Before prod is ever relied on, do it in staging: two gated
deploys of two tags, then roll back to the first
([deployment guide, section 2](deployment-guide.md#2-the-first-deploys-and-the-rollback-rehearsal)).

---

## Decision Tree: When to Rollback

Use this tree to decide your response within the first 2 minutes of an issue. When in doubt, roll back.

```
Post-deployment issue detected
          |
          v
Did CodeDeploy already roll the API back?
  (the run ended "Rolled back by an alarm", or get-deployment shows
   Stopped with ALARM_ACTIVE)
          |
    YES --+--→ AN ALARM ROLLED THE API BACK (that section below; no Rollback)
          |
    NO    v
Is CodeDeploy rolling back right now?
  (an active deployment's creator is codeDeployRollback)
          |
    YES --+--→ WAIT for it; Rollback refuses while it is active
          |
    NO    v
Is any of the following true?
  - Health check /health returning non-200
  - 5xx error rate > 5% (vs. pre-deploy baseline)
  - p99 API latency > 5000ms
  - Data corruption detected (missing rows, wrong values)
  - ECS tasks crash-looping (stopped reason in logs)
          |
    NO ---+--- YES
          |         |
          |         v
          |   Is there a workaround (feature flag at 0%)?
          |         |
          |   YES --+-- NO ──→ ROLLBACK IMMEDIATELY (go to Method 1)
          |         |
          |         v
          |   Can a hotfix be written and tested in < 30 min?
          |         |
          |   YES --+-- NO ──→ ROLLBACK IMMEDIATELY (go to Method 1)
          |         |
          |         v
          |   Is it a database schema issue?
          |         |
          |   YES ──→ DATABASE FIRST, THEN APPLICATION (DB Rollback section, then Method 1)
          |   NO  ──→ HOTFIX (deploy new tag following standard procedure)
          |
          |   Only the dashboard is broken, and the API is fine?
          |   YES ──→ DASHBOARD ONLY (Method 2, the dashboard block)
          |
          v
   Monitor for 5 more minutes; page if it worsens
```

**Rollback triggers — no deliberation required:**

While a deploy's CodeDeploy deployment is active (its canary, and the hour
after its traffic shift), the deployment group's two 5xx alarms act on the
5xx row by themselves: at least 5 target 5xx and at least 5% of a target
group's requests in a minute, for 2 minutes of 3, and CodeDeploy rolls the API
back. The rest of the table, and everything after that hour, is yours.

| Condition | Threshold | Action |
|-----------|-----------|--------|
| Health check failure | `/health` returning non-200 | Immediate rollback |
| 5xx error rate spike | > 5% of requests over 2 min | Immediate rollback (during a deployment, the alarms do it first) |
| p99 API latency | > 5000ms sustained for 2 min | Immediate rollback |
| Data corruption | Any confirmed case | Immediate rollback + escalate |
| ECS tasks crash-looping | Tasks not stabilizing after 5 min | Immediate rollback |

---

## Method 1: GitHub Actions Manual Rollback (Preferred)

This is the preferred method. It is audited and sends Slack notifications when `SLACK_BOT_TOKEN` is set. It does **not** run smoke tests; Step 5 of Method 2 and the post-rollback checklist are by hand.

The deploy that went wrong printed the target for you: its run summary ends
with `Rollback: Actions → Rollback → environment=<env>, task_definition_arn=experimentation-backend-<env>:<n>, dashboard_task_definition_arn=experimentation-dashboard-<env>:<m>`.
Use that, and skip Step 1. It carries both revisions, the API's and the
dashboard's, so both go back. The dashboard part is left out when the dashboard
was not on a release before that deploy (the bootstrap image, or a tag a `cdk
deploy` registered): there is then no released dashboard to go back to, and the
summary says so.

The workflow always rolls the **API** back. It has no dashboard-only mode: it
first stops any CodeDeploy deployment in flight with auto-rollback, which
reverts an API that shifted within the last hour. To put back the dashboard
alone, use Method 2's dashboard block.

It refuses the API half, fails, and says why, in two cases:

- **The API is already on the target** and no deployment is in flight: the
  target is the PRIMARY task set and the `/api/*` rule forwards to it. There
  is nothing to roll back, so it creates no deployment and stops nothing. If
  you gave a dashboard revision, the dashboard half still runs, and the run
  says what it did.
- **CodeDeploy is already rolling back**: an in-flight deployment was created
  by CodeDeploy itself (`creator` is `codeDeployRollback`). Stopping it would
  put back the release it is rolling away from, so the run stops nothing and
  rolls nothing back, not even the dashboard. Rollback refuses for as long as
  that deployment is active, which can be up to an hour. For the dashboard
  alone, use Method 2's dashboard block. The run also stops nothing when it
  cannot read an in-flight deployment's creator.

It creates its CodeDeploy deployment with the deployment group's alarms
overridden (`--override-alarm-configuration`), always: the
release being rolled back is usually what holds a 5xx alarm in ALARM, and
CodeDeploy stops every deployment to the group while one is.

A rollback is reported done only once the run's own deployment has been
approved and the target is the PRIMARY task set. A deployment the run never
saw waiting for approval is not counted, and the run then fails and says so.

**If the API went back and the dashboard did not**, the run says so (its
verdict is `API BACK, DASHBOARD NOT`), and the system is in the
newer-dashboard, older-API state. Put the dashboard back with
Method 2's dashboard block. Do not dispatch Rollback again while the rollback's
own CodeDeploy deployment is active (about an hour after its shift): its stop
step would stop that deployment with auto-rollback and put the API back on the
release you rolled back from.

### Reading the result

`scripts/rollback_verdict.py` chooses the run's verdict from a fixed order of rules, the first
that matches winning, and the run succeeds only when the verdict is
`ROLLED BACK`. When the run's own steps did not finish (its verify step did not
succeed), the summary first reads what the API is serving at the end of the
run, with `scripts/api_serving.py` (read-only, the same check the stop step
uses), and lists the deployment group's active deployments once; the headline
quotes what the read said. The summary's table also says what the stop step
stopped and how its wait ended.

A headline that names a deployment that may still be active ends with
"Do not dispatch Rollback again while deployment `<id>` is active": a new
Rollback stops any deployment that is not CodeDeploy's own rollback, with
auto-rollback, which reverts what it shifted, and it refuses while CodeDeploy's
own rollback is active. Check one with
`aws deploy get-deployment --deployment-id <id> --query deploymentInfo.status`.

| Verdict | What it means | What to do |
|---|---|---|
| `ROLLED BACK` | This run's deployment was approved and verified, and the dashboard was rolled back or not asked for. | Nothing more. The deployment the headline names stays active for about an hour while CodeDeploy keeps the replaced task set. |
| `API BACK, DASHBOARD NOT` | The API is on the target (verified, or read at the end of the run), and the dashboard was asked for and was not rolled back, so the API and dashboard are on different releases. | Put the dashboard back with Method 2's dashboard block. Fix what the failed step names before the next deploy or rollback. |
| `API BACK, RUN FAILED` | The API is on the target, read at the end of the run, most often because stopping the bad deployment with auto-rollback put it back, and a later step then failed (the step's error says which). | Nothing more for the API. Fix what the failed step names before the next deploy or rollback. |
| `NOT FINISHED` | This run stopped a deployment, and its wait for the deployment group to have no active deployment timed out (about 5 minutes) or could not read the group, so it created no rollback deployment. The headline quotes what the API was serving at the end. | Wait until the deployments it names have finished, then read what is serving: `python3 scripts/api_serving.py experimentation-$ENV experimentation-backend-$ENV <target>` (exit 0 means on the target). |
| `STILL MOVING` | The API is not yet on the target, and a deployment is active or the `/api/*` rule still splits traffic between blue and green. | Wait, then read it again with the command above. If the rule still splits traffic and no deployment is active, the listener is stuck and needs a person: call the next person in the table at the end of this page, and do not dispatch anything until they have looked. |
| `NOT ROLLED BACK` | The API is not on the target, and no deployment is active. The quoted read says why: `NOT YET` (another revision is PRIMARY) or `WRONG` (the target is PRIMARY but the `/api/*` rule forwards elsewhere). | Act on the quoted read. Nothing is active, so a new Rollback is not stopping anything in flight. |
| `OUTCOME UNKNOWN` | This run could not tell: the read failed (`UNKNOWN`, exit 2), the active deployments could not be listed, the summary got a value it does not recognise, or the verdict script did not run. | Before acting, read what is serving with the command above, and list the deployment group's active deployments: `aws deploy list-deployments --application-name experimentation-platform-$ENV --deployment-group-name experimentation-$ENV --include-only-statuses Created Queued InProgress Baking Ready --query deployments --output text`. |
| `REFUSED` | The run refused, and says why: the API was already on the target, CodeDeploy's own rollback is active, or an in-flight deployment could not be classified. | Follow the reason in the line. |
| `NOTHING CHANGED` | The run stopped nothing and created no deployment: the target revision was not resolved, or a step failed before anything changed. The API is as it was. | Fix what the failed step names. |

### The rolled-back API runs against the current schema

Rolling back puts back an older API; it does not put back the database. The
API tasks do not run migrations when they start (`RUN_MIGRATIONS=false` in
their task definition), so the older release starts against the schema the
newer one migrated and serves it. That is correct only when every migration
since the target release is backward-compatible, which is the rule migrations
here follow ([Deployment Guide](deployment-guide.md#backward-compatible-migrations)).
When one is not -- it dropped or renamed something the target release reads --
rolling the API back is not enough, and the order matters: undo the migration
while the release that ran it is still serving, then roll the API back
([Database Rollback Procedure](#database-rollback-procedure)).

Check the target's environment before you roll back to it:

```bash
aws ecs describe-task-definition --task-definition "$TARGET_TD" \
  --query "taskDefinition.containerDefinitions[?name=='backend'] | [0].environment[?name=='RUN_MIGRATIONS'] | [0].value" \
  --output text
```

It prints `false` for a revision that does not migrate on start. One that
prints `None` (no such variable) or prints `true` runs the older
release's migrations on start, and against a newer schema it refuses to
start. Register a copy of it with
`RUN_MIGRATIONS=false` added to the `backend` container's environment, and roll
back to the copy. Revisions the Deploy workflow registers after the Fargate
stack has been deployed from a current checkout carry the setting already, and
Deploy refuses to create a deployment for one that does not
([Deploy refused an API revision that would run migrations on start](#deploy-refused-an-api-revision-that-would-run-migrations-on-start)).
Rollback does not check its target: that is this command.

### Rolling back past segment targeting

Segment targeting (`in_segment` and `not_in_segment`) arrived in v0.22.0.

- **Before rolling the API back past that release**, remove segment
  conditions from flags and experiments, or disable the flags that use them,
  and pause the experiments that use them.
- **After an automatic rollback past it** (an alarm in the canary, when the
  old and the new release serve side by side, or after the traffic shift),
  disable segment-targeted flags and pause segment-targeted experiments at
  once. Turn them back on when the API is again on a release with segment
  targeting and the SDKs have refreshed their rulesets (every 30 seconds by
  default).

`GET /api/v1/segments/{segment_id}/experiments` lists the flags and
experiments whose rules mention a segment.

### Step 1: Find the Previous Task Definition ARN

> **Two producers write to this family, and only one of them is runnable.**
> `cdk deploy` registers revisions too, and those carry the CDK's `bootstrap`
> image tag rather than a released build (#82). **Never pick a revision by subtracting 1** —
> read the image off a candidate before you deploy it.

Option A: List recent task definitions for the family (newest first) WITH the image each one carries, so a CloudFormation-registered revision is visible rather than a number in a list:

```bash
for arn in $(aws ecs list-task-definitions \
      --family-prefix experimentation-backend-$ENV \
      --sort DESC --max-results 10 --query 'taskDefinitionArns' --output text); do
  image=$(aws ecs describe-task-definition --task-definition "$arn" \
    --query "taskDefinition.containerDefinitions[?name=='backend'].image" --output text)
  echo "$arn  $image"
done
```

It prints one line per revision, newest first, for example:

```text
arn:...:experimentation-backend-$ENV:45  ...backend:bootstrap   <- CloudFormation; NOT a rollback target
arn:...:experimentation-backend-$ENV:44  ...backend@sha256:9f2c…   <- current (bad): a deploy registered it, by digest
arn:...:experimentation-backend-$ENV:43  ...backend@sha256:41ab…   <- target (good): the last known-good release
```

Option B: Ask which revision is actually serving traffic. NOT `services[0].taskDefinition`: on a service with a CodeDeploy deployment controller that field is "specified when the service is created with CreateService, and it can be modified with UpdateService" -- and UpdateService is the one call ECS refuses on such a service. So it names the revision CloudFormation created when the stack was first deployed, for the life of the service, and is never the running one. The PRIMARY task set is:

```bash
aws ecs describe-services \
  --cluster "experimentation-$ENV" \
  --services experimentation-backend-$ENV \
  --query "services[0].taskSets[?status=='PRIMARY'].taskDefinition" \
  --output text
```

It returns an ARN such as `arn:...:task-definition/experimentation-backend-$ENV:44`. Then use Option A to choose the released revision below it.

Option C: Review service events to identify what was running before this deployment:

```bash
aws ecs describe-services \
  --cluster "experimentation-$ENV" \
  --services experimentation-backend-$ENV \
  --query 'services[0].events[:5]'
```

The dashboard has the same listing for its own family and container. Only a
revision whose image is a digest (`web@sha256:...`) is a release: a deploy
registered it. A `:bootstrap` or version-tag image is a revision CloudFormation
registered, and Rollback refuses it. The second command shows what the
dashboard is serving: its PRIMARY *deployment* (it has no task sets).

```bash
for arn in $(aws ecs list-task-definitions \
      --family-prefix experimentation-dashboard-$ENV \
      --sort DESC --max-results 10 --query 'taskDefinitionArns' --output text); do
  image=$(aws ecs describe-task-definition --task-definition "$arn" \
    --query "taskDefinition.containerDefinitions[?name=='dashboard'].image" --output text)
  echo "$arn  $image"
done

aws ecs describe-services \
  --cluster "experimentation-$ENV" \
  --services experimentation-dashboard-$ENV \
  --query "services[0].deployments[?status=='PRIMARY'].[taskDefinition,rolloutState]" \
  --output text
```

### Step 2: Trigger the Rollback Workflow

1. Navigate to **GitHub → Actions → "Rollback"** (file: `rollback.yml`)
2. Click **Run workflow**, from `main` (any other ref is refused)
3. Fill in the required inputs:
   - **Environment:** `$ENV` (`staging` or `prod`). A revision of the other
     environment's family is refused with "Re-run with environment=…"
   - **Reason for rollback:** Brief description, e.g., `"Error rate 8% after v1.2.3 deploy, p99 latency 5200ms"`
   - **Previous task definition ARN:** The ARN from Step 1 (the last known-good revision)
   - **dashboard_task_definition_arn** (optional): the dashboard revision from
     Step 1, `experimentation-dashboard-$ENV:<n>`. Leave it empty to leave the
     dashboard as it is; the run summary then says what the dashboard is
     serving. A dashboard revision in the API field, or an API revision in the
     dashboard field, is refused by name ("… is a dashboard revision; it goes
     in dashboard_task_definition_arn, not task_definition_arn", and the
     reverse)
4. Click **Run workflow**

The workflow will:
- Check the target revision is ACTIVE, in the right family, has a container
  named `backend`, and is not a CloudFormation-registered `:bootstrap` revision
- Stop any CodeDeploy deployment still in flight (during an incident the bad
  deploy usually is, and CodeDeploy refuses a second one), after reading who
  created each one; if any is CodeDeploy's own rollback, or its creator cannot
  be read, it stops nothing and fails
- After stopping one, wait up to about 5 minutes until the deployment group
  has no active deployment. Stopping with auto-rollback makes CodeDeploy create
  its own revert of what was stopped, which usually finishes in a few seconds.
  If one is still active when the wait ends, the run creates nothing and fails
  with `Deployment group still busy`, naming it
- Create a **CodeDeploy** deployment naming that revision, all-at-once rather
  than the canary the forward path uses
- **Approve the traffic shift** (`aws deploy continue-deployment`) — without
  this the deployment parks for 30 minutes and is then stopped, which
  auto-rollback turns back into the revision you were rolling away from
- Wait until the target revision is the **PRIMARY task set** and every desired
  task is running
- If a dashboard revision was given (it was checked before anything changed:
  ACTIVE, in the dashboard family of this environment, one `dashboard`
  container, a digest image): roll the dashboard's service onto it, and refuse,
  without changing it, if the dashboard's PRIMARY deployment is no longer the
  revision the check saw (another Deploy, Rollback or `cdk deploy` changed it)
- Notify `#deployments` with the result, success or failure

It does **not** run smoke tests; `/health` is Step 5 below, by hand.

### Step 3: Monitor Progress

Watch the GitHub Actions run. Simultaneously run:

Watch the PRIMARY task set, which is what actually moves.

NOT `services[0].taskDefinition`: on a CODE_DEPLOY service that field is set by CreateService and changed only by UpdateService -- the call ECS refuses here -- so it names the revision CloudFormation created and never changes. Watching it during a rollback shows nothing happening and reads as a failure:

```bash
watch -n 5 'aws ecs describe-services \
  --cluster "experimentation-$ENV" \
  --services experimentation-backend-$ENV \
  --output json \
  --query "services[0].taskSets[?status==\`PRIMARY\`] | [0].{Serving:taskDefinition,Running:runningCount,Desired:computedDesiredCount,Pending:pendingCount}"'
```

Read the PRIMARY task set's counts, not the service's
`runningCount`: for the hour after a shift the replaced task set keeps
running beside the new one, so the service counts both (on staging on
2026-10-04 (UTC) it counted 4 tasks of 2 desired after a rollback that had worked:
[#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-5982176639)). This is a
**blue/green** deployment, not a rolling update: a second (green) task set is
provisioned alongside the current one and traffic moves to it in one shift, so
`Serving` changes from the old revision to the new one at once rather than
tasks being replaced one at a time.

### The dashboard is the opposite case

The dashboard's service (`experimentation-dashboard-$ENV`) uses the ECS
**rolling** deployment controller, not CodeDeploy. Everything said above about
the API is reversed for it:

- `aws ecs update-service --task-definition` **is** the right call; there is no
  CodeDeploy deployment and no traffic shift to approve.
- What it is serving is its PRIMARY **deployment**,
  `deployments[?status=='PRIMARY']`, not `taskSets` (it has none).
  `services[0].taskDefinition` does move for it.
- `aws ecs wait services-stable` reports **success after the deployment circuit
  breaker has rolled a failed revision back**: one deployment, every task
  running, just not the revision you asked for. Wait on the PRIMARY
  deployment's `taskDefinition` and `rolloutState` instead, as Method 2's
  dashboard block does.

---

## Method 2: AWS CLI Direct Rollback (Emergency)

Use this method when GitHub Actions is unavailable or you need to act faster than the workflow allows. This method is faster but does not automatically run smoke tests — you must run them manually after.

**Step 1.** Set the target task definition ARN (the last known-good revision):

```bash
PREV_TASK_DEF="arn:aws:ecs:us-west-2:ACCOUNT_ID:task-definition/experimentation-backend-$ENV:43"
```

**Step 2.** Get the running task definition, if you need it. Use the PRIMARY
task set, not `services[0].taskDefinition` -- see the note in Method 1 Step 1:
on a CodeDeploy-controlled service that field never moves off the revision
CloudFormation created.

```bash
CURRENT=$(aws ecs describe-services \
  --cluster "experimentation-$ENV" \
  --services experimentation-backend-$ENV \
  --query "services[0].taskSets[?status=='PRIMARY'].taskDefinition" \
  --output text)
echo "Running task def: $CURRENT"
```

Do NOT decrement the revision number to find the target. CloudFormation
registers into this family too, and its revisions carry the `bootstrap` image
tag; pick the target with Method 1 Step 1's listing, which prints the image
beside each revision.

**Step 2b. Stop the deployment in flight first.** CodeDeploy accepts one
deployment per deployment group at a time, so Step 3 is refused while the bad
deploy's deployment is still active. List what is active:

```bash
aws deploy list-deployments \
  --application-name experimentation-platform-$ENV \
  --deployment-group-name "experimentation-$ENV" \
  --include-only-statuses Created Queued InProgress Baking Ready \
  --query deployments --output text
```

For each id it prints, read who created it:

```bash
aws deploy get-deployment --deployment-id d-XXXXXXXXX \
  --query 'deploymentInfo.{status:status,creator:creator}'
```

If the creator is `codeDeployRollback`, do not stop it: CodeDeploy is already
putting the API back ([An alarm rolled the API back](#an-alarm-rolled-the-api-back)).
Otherwise stop it, with auto-rollback:

```bash
aws deploy stop-deployment --deployment-id d-XXXXXXXXX --auto-rollback-enabled
```

On a staging account on 2026-10-03 (UTC), stopping the in-flight deployment
returned the API both times it was tried
([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-5971647359)).
Run the list again until it prints nothing, then go on.

**Step 3.** Deploy the previous task definition through CodeDeploy.

NOT `aws ecs update-service --task-definition`. This service has a CodeDeploy
deployment controller, and ECS refuses a task-definition change through
UpdateService on one: "Unable to update task definition on services with a
CODE_DEPLOY deployment controller". Going back is the same call as going
forward, with an older revision.

```bash
APPSPEC=$(jq -cn --arg td "$PREV_TASK_DEF" '{
  version: 1,
  Resources: [{ TargetService: {
    Type: "AWS::ECS::Service",
    Properties: {
      TaskDefinition: $td,
      LoadBalancerInfo: { ContainerName: "backend", ContainerPort: 8000 }
    }
  }}]
}')
```

`--deployment-config-name` is all-at-once, NOT the deployment group's
CANARY_10_PERCENT_15_MINUTES.

```bash
DEPLOYMENT_ID=$(aws deploy create-deployment \
  --application-name experimentation-platform-$ENV \
  --deployment-group-name "experimentation-$ENV" \
  --deployment-config-name CodeDeployDefault.ECSAllAtOnce \
  --description "manual rollback" \
  --revision "$(jq -cn --arg c "$APPSPEC" '{revisionType:"AppSpecContent",appSpecContent:{content:$c}}')" \
  --query deploymentId --output text)
echo "deployment: $DEPLOYMENT_ID"
```

**Step 3b. APPROVE THE TRAFFIC SHIFT. Do not skip this.**

The deployment group sets `deployment_approval_wait_time` = 30 minutes. When
the green task set is provisioned CodeDeploy goes to status `Ready` and WAITS.
If ContinueDeployment is not called it stops the deployment, and the group's
`auto_rollback(stopped_deployment=True)` then puts back the revision you are
rolling away from.

```bash
while [ "$(aws deploy get-deployment --deployment-id "$DEPLOYMENT_ID" \
             --query deploymentInfo.status --output text)" != "Ready" ]; do
  sleep 5
done
aws deploy continue-deployment --deployment-id "$DEPLOYMENT_ID" \
  --deployment-wait-type READY_WAIT
```

**Step 4.** Wait for the ROLLBACK, not for the service.

`aws ecs wait services-stable` returns almost immediately here: during a
blue/green deployment the old task set is serving the whole time, so the
service is stable and the waiter says nothing about whether the rollback took.
Watch the PRIMARY task set instead: that is what moves, and it
tells you traffic has shifted without waiting for CodeDeploy to terminate the
old task set (the group keeps it for an hour).

```bash
until [ "$(aws ecs describe-services --cluster "experimentation-$ENV" \
             --services experimentation-backend-$ENV \
             --query "services[0].taskSets[?status=='PRIMARY'].taskDefinition" \
             --output text)" = "$PREV_TASK_DEF" ]; do
  sleep 5
done

echo "Rollback complete. Running verification..."
```

**Step 5.** Verify health:

```bash
curl -sf "https://app.<domain>/health" && echo "Health check PASSED" || echo "Health check FAILED"
```

**The dashboard alone.** This is the only dashboard-only path. The Rollback
workflow always rolls the API back too, and stops any in-flight API deployment
with auto-rollback; and within about an hour of a deploy's traffic shift,
Deploy refuses a re-run while that CodeDeploy deployment is still active. Pick
the target from Method 1 Step 1's dashboard listing (a digest image). For a
rolling service, pointing it at the revision IS the rollback. Then wait on the
PRIMARY deployment, not `wait services-stable` (see "The dashboard is the
opposite case"): if the PRIMARY turns back to the revision you replaced, the
circuit breaker rejected the target, so stop the loop and read the service
events.

```bash
DASH_TASK_DEF="arn:aws:ecs:us-west-2:ACCOUNT_ID:task-definition/experimentation-dashboard-$ENV:6"

aws ecs update-service \
  --cluster "experimentation-$ENV" \
  --service experimentation-dashboard-$ENV \
  --task-definition "$DASH_TASK_DEF" \
  --query "service.deployments[?status=='PRIMARY'].id" --output text

until [ "$(aws ecs describe-services --cluster "experimentation-$ENV" \
             --services experimentation-dashboard-$ENV \
             --query "services[0].deployments[?status=='PRIMARY'].[taskDefinition,rolloutState] | [0]" \
             --output text)" = "$(printf '%s\tCOMPLETED' "$DASH_TASK_DEF")" ]; do
  sleep 10
done
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' "https://app.<domain>/"
```

---

## Method 3: What CodeDeploy rolls back by itself, and when

**Alarms watch the API while a deployment is active.** The deployment group
has two alarms, `experimentation-api-5xx-blue-$ENV` and
`experimentation-api-5xx-green-$ENV`, one per target group
([#148](https://github.com/Controlvar/controlvar/issues/148)). While
either is in ALARM during a deployment -- its canary, and the hour after its
traffic shift -- CodeDeploy stops the deployment and rolls the API back to the
previous revision by itself: see
[An alarm rolled the API back](#an-alarm-rolled-the-api-back). A minute counts
against the API when its target group answers at least 5 target 5xx and at
least 5% of its requests with a 5xx; two such minutes of three fire the alarm.
On a staging account on 2026-10-03 (UTC), CodeDeploy stopped and reverted an API deployment
when an alarm went into ALARM
([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-5971647359)),
and on 2026-10-11 (UTC) an alarm did the same in the hour after the traffic shift
([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-6157399595)).

What the alarms do not cover, so **Method 1 is the response**:

- a release that answers wrongly with a 2xx, or with fewer errors than that;
- the load balancer's own 502 and 504 (a crashed or timed-out task): they have
  no target-group dimension, so no alarm watches them;
- the dashboard: an alarm rolls back the API only;
- anything after the hour CodeDeploy keeps the previous task set, when the
  deployment is no longer active and no alarm acts on it.

**Rollback before a fix-forward.** Within the hour after a deploy's traffic
shift, the deploy workflow refuses a new release while that deployment is
active. To ship a fix, run Rollback first (Method 1), then deploy the fix.
Rollback's own deployment is then the active one, and the next forward deploy
is refused until it is no longer active: about an hour, because the deployment
group's 60-minute termination wait applies to every deployment in the group,
Rollback's included. On a staging account on 2026-10-11 (UTC), the Rollback run's
deployment stayed active for about an hour after its traffic shift
([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-6157399595)).

**When CodeDeploy does roll back by itself:**
- one of the two 5xx alarms is in ALARM while the deployment is active (above);
- the new task set's tasks never become healthy, so the deployment fails
  before any traffic moves (the deploy workflow also refuses to approve the
  shift until every target is healthy);
- the deployment is never approved, so CodeDeploy stops it when its 30-minute
  approval wait ends. Nothing had shifted.

---

## An alarm rolled the API back

The deploy run ended **"Rolled back by an alarm"** (or, after the run went
green, `get-deployment` shows the deployment `Stopped` with `ALARM_ACTIVE`).
CodeDeploy has stopped the deployment and is moving the API back to the
revision that served before it. There is nothing to roll back for the API.

1. **Confirm it.** The status, who created the deployment, why it stopped, and
   CodeDeploy's rollback deployment:

    ```{.bash skip reason="aws: reads a real CodeDeploy deployment"}
    aws deploy get-deployment --deployment-id d-XXXXXXXXX --query 'deploymentInfo.{status:status,creator:creator,error:errorInformation,rollback:rollbackInfo}'
    ```

2. **See what the alarm saw.** Its history, then the API's log
   (`/ecs/experimentation-backend-$ENV`) for the same minutes:

    ```{.bash skip reason="aws: reads the real alarms' history"}
    aws cloudwatch describe-alarm-history --alarm-name "experimentation-api-5xx-green-$ENV" --history-item-type StateUpdate --max-records 10
    ```

    The blue alarm is `experimentation-api-5xx-blue-$ENV`; the run named the
    one that fired.

3. **Check the API is back**: the revision serving before the deploy is the
   PRIMARY task set again (Step 1, Option B), or, with the run summary's
   "API serving before this run" value:
   `python3 scripts/api_serving.py experimentation-$ENV experimentation-backend-$ENV <that ARN>`
   exits 0.
4. **The dashboard.** If the alarm fired in the canary, the run stopped before
   the dashboard and it was not changed. If it fired in the hour after the
   shift, the dashboard is on the new release in front of the old API: put it
   back with Method 2's dashboard block, using the summary's "Dashboard serving
   before this run" value.
5. **Do not dispatch Rollback while CodeDeploy's rollback is active.** Its
   deployment's `creator` is `codeDeployRollback`.
6. **The migration stays applied.** The database is at the new release's
   heads, with the previous revision serving on it. That is safe only for a
   [backward-compatible migration](deployment-guide.md#backward-compatible-migrations);
   otherwise see the Database Rollback Procedure below.
7. **Do not redeploy the same tag.** It will meet the same alarm. Fix it and
   cut a new release, and note the bad release in the incident record.
8. **A false alarm** -- a good release rolled back -- is possible: the alarm
   also watches the release that was serving before (it is on the other target
   group), and it counts every 5xx the API answers, including a failing
   dependency's. Compare the alarm's timeline with the release's and the
   dependency's, write down what you find, and change the alarm (in
   `infrastructure/cdk/stacks/fargate_service_stack.py`) before redeploying.
9. **Segment targeting.** If the release that served before the deploy has no
   segment targeting, disable segment-targeted flags and pause
   segment-targeted experiments now
   ([Rolling back past segment targeting](#rolling-back-past-segment-targeting)).

## Fix forward while an alarm is firing

CodeDeploy stops **every** deployment to the group while either 5xx alarm is
in ALARM, a fix included. That is right when the release being replaced is the
one failing, and Rollback (Method 1) handles it: Rollback always overrides the
alarms for its own deployment. It is wrong in three cases:

- the bug is in the release you would roll back to as well, so rolling back
  does not help and only a new release fixes it;
- a dependency outage holds the alarm in ALARM, and the fix is a configuration
  change that has to ship during it;
- the environment has no schema yet. `cdk deploy` does not create it: the
  first Deploy's migration does, and until then the API answers real requests
  with 500 ([Deployment Guide, section 1.6](deployment-guide.md#16-the-stacks)).
  Those 500s can put an alarm into ALARM, and Deploy then refuses to start.
  Rollback has nothing to go back to. Whether to deploy unwatched is
  decided by a person at that moment.

For those, Deploy has a break-glass: tick **`override_alarms`** and give
**`override_alarms_reason`**. The run's name then ends in `ALARMS OVERRIDDEN`,
which is what the environment's reviewer sees when asked to approve it: the
reviewer is the check. Deploy refuses the tick-box without a reason, and a
reason without the tick-box.

What it gives up: **no alarm watches that deployment**, in its canary or in
the hour after its shift, and nothing rolls it back by itself. The run summary
and the Slack message say so, with the reason. If the fix is bad, roll back
with Method 1 within that hour. The override is on that one deployment only,
so the next deploy is watched again with no action from anyone.

Deploy checks first. Before it builds anything, and again just before the
snapshot and the migration, it reads the alarms the deployment group polls and
refuses while one of them is in ALARM ("An alarm is already firing", naming
the alarm and since when). It also refuses, whatever else is set, when the
group watches no alarm, has its alarms disabled, or does not watch both of the
stack's alarms: deploy the Fargate stack first. `INSUFFICIENT_DATA` does not
refuse. The check is advisory: an alarm that goes into ALARM after it passes
still stops the deployment, after the migration, and the run ends with the
"Migrated, not deployed" warning. With the break-glass ticked, an alarm in
ALARM is printed as a warning instead, and the deploy goes on unwatched.

`aws cloudwatch disable-alarm-actions` and `aws cloudwatch set-alarm-state` do
not unblock a deploy: expected, and not rehearsed on AWS as of v0.31.1. CodeDeploy reads the alarms' state, not their actions,
and a state set by hand lasts only until the next evaluation: on staging on
2026-10-11 (UTC) the alarm was back to OK within two minutes
([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-6157399595)).

To see which alarm is firing, and since when:

```{.bash skip reason="aws: reads the real alarms' state"}
aws cloudwatch describe-alarms --alarm-names "experimentation-api-5xx-blue-$ENV" "experimentation-api-5xx-green-$ENV" --query 'MetricAlarms[].{name:AlarmName,state:StateValue,since:StateUpdatedTimestamp,reason:StateReason}'
```

## Deploy refused an API revision that would run migrations on start

The deploy run ended with **"This API revision would run migrations on
start"**. Deploy registers the API's revision by copying the newest revision of
the family and replacing its image, then reads back what ECS stored. It found
the `backend` container without `RUN_MIGRATIONS=false` -- the variable missing,
or set to anything but exactly `false` -- and stopped before creating the
CodeDeploy deployment.

**What has happened.** The snapshot was taken and the migration was applied;
the database is at the new release's heads. No deployment was created, so the
revision that served before the run still serves, on the migrated schema. The
run also ends with the "Migrated, not deployed" warning, which says the same.

**Why.** A revision without `RUN_MIGRATIONS=false` runs the database bootstrap
each time a task starts, and a bootstrap refuses a database a newer release
migrated.

**Fix.** Deploy the Fargate stack of this environment,
`experimentation-fargate-<env>`, from a checkout that contains #499 (0.14.0 or
later), pinned to what is live exactly as
[Deployment Guide, section 1.6](deployment-guide.md#16-the-stacks) says. That
registers a revision with the setting and does not change what is serving: the
API service is under CodeDeploy, so a change to its task definition only adds a
revision. Then run Deploy again with the same tag. Its migration finds the
database already at the release's heads and changes nothing.

**Meanwhile.** The revision that is serving was probably registered from the
same older base, so it runs migrations on start too. Its running tasks keep
serving, but one that starts now -- a replacement, a scale-out -- meets a
schema newer than its release and refuses to start. Check it with the command
in [The rolled-back API runs against the current schema](#the-rolled-back-api-runs-against-the-current-schema),
and do not leave the environment in this state longer than the fix takes.

---

## Database Rollback Procedure

**Only use this section if a database migration caused the issue.** Database rollback is higher risk and requires Engineering Lead approval if data loss is possible.

**Undo the migration before you roll the API back.** The Database Migration
workflow has no image input: it runs `alembic` with the image of the API's
PRIMARY task set. So the workflow downgrade works only while the release that
contains the migration is PRIMARY. Run Step 2 first, and only then roll the API
back with Methods 1–3.

**After an API rollback or an alarm rollback, there is no supported downgrade
through the workflow.** The image now serving does not contain the migration's
file, so `alembic` cannot locate the database's revision. The workflow fails at
"Show the current revision", after it has already taken a snapshot, and changes
nothing. Do not dispatch it to try. The documented recovery from that state is
the point-in-time restore in Step 3. Call the Engineering Lead now.

### Step 1: Confirm Migration is the Root Cause

Before touching the database, confirm all of the following:

- Error logs contain schema-related errors (e.g., `column "X" does not exist`, `relation "Y" does not exist`, `UndefinedColumn`, `ProgrammingError`)
- The failure correlates with the migration that ran during this deployment
- The API has not been rolled back: the release that ran the migration is still PRIMARY. If Rollback or an alarm has already rolled it back, go to Step 3 and call the Engineering Lead

Check migration logs from the ECS migration task:

```bash
aws logs filter-log-events \
  --log-group-name /ecs/experimentation-migrate-$ENV \
  --filter-pattern '"alembic"' \
  --start-time $(date -u -v-1H +%s000 2>/dev/null || date -u --date='1 hour ago' +%s000)
```

The current revision is printed by the Database Migration workflow's
"Show the current revision" step, which runs `alembic current` in the VPC.

### Step 2: Run Migration Downgrade via GitHub Actions

1. Navigate to **GitHub → Actions → "Database Migration"** (file: `db-migrate.yml`)
2. Fill in the required inputs:
   - **Environment:** `$ENV` (`staging` or `prod`)
   - **Direction:** `downgrade`
   - **Target:** the revision id to end at. To undo the migration a release added, open that migration file (`backend/app/db/migrations/versions/` for core, `modules/backend/app/db/migrations/versions/` for a module, which exists only in a full-profile image) and use its `down_revision`, e.g. `37dcb2969766` to undo `4c2c09a2b3e8`. That downgrade changes no table: `safety_rollback_records.safety_config_id` stays nullable and its foreign key stays ON DELETE SET NULL, which the release you roll back to runs against unchanged. Going one further, to `d29a479daafe`, also undoes `37dcb2969766`, which drops the table `segment_members`, the members of every id-list segment (nothing rebuilds them), and the column `segments.kind`, so it refuses while `segment_members` has any row: it prints the row count and changes nothing. The workflow cannot pass the override (`-x allow_member_loss=true`), so with id lists stored either remove their members first or run the downgrade by hand with the override, with the Engineering Lead's approval at the moment it runs. Going further, to `806901fb7735`, also undoes `d29a479daafe`, which drops the table `holdout_population` with every recorded holdout membership (nothing rebuilds it) and three `global_holdouts` columns: when that table has rows, a downgrade across `d29a479daafe` needs the Engineering Lead's approval at the moment it runs. Going further still, to `1ab99332f0ba`, also undoes `806901fb7735`, which drops every experiment's stored correction method and confidence level, and an upgrade afterwards restores the defaults (Benjamini-Hochberg, 0.95), not the choices: run it only with someone who has agreed to lose them. Not `-1`: a full install has two heads, and the workflow refuses relative steps, `head` and `base`. A core id at or below `a7b8c9d0e1f2` also unapplies the modules branch.
3. Click **Run workflow**

Once the downgrade has completed, roll the API back (Method 1). Not before: after the API is rolled back, the workflow can no longer read this migration.

Rolling back across `d29a479daafe` while a global holdout is active invalidates that holdout's measurement: the older release buckets users with the old salt and records nobody. After the next upgrade, deactivate that holdout and create a new one (`PUT /api/v1/holdout/{id}` with `is_active: false`, then `POST /api/v1/holdout`).

### Step 3: Emergency — Point-in-time restore to a new cluster

**This causes data loss for the period after the time you restore to. Call the Engineering Lead before proceeding.**

Only take this path if:
- The API has already been rolled back (by Rollback or by an alarm), so the workflow cannot downgrade, or the downgrade in Step 2 failed
- The migration is not backward-compatible, so the release you rolled back to cannot run against the migrated schema
- The migration caused data corruption or irreversible data loss
- The Engineering Lead has explicitly approved this path

**STOP. A restore to a NEW cluster is not picked up by a redeploy.**

Both backend task definitions take `POSTGRES_SERVER` from the database STACK's writer endpoint, and `POSTGRES_USER` and `POSTGRES_PASSWORD` from the generated secret of the stack, as CloudFormation imports. A cluster restored beside the stack is not that endpoint, so there is no connection string in Secrets Manager to update, and a new deployment would bring the tasks back pointing at the original cluster -- while this runbook reported success. The restored cluster also keeps the master password of the snapshot, which is the one in the secret only if it has not been rotated since.

There is no restore that keeps the original cluster: both Aurora restore
operations, point-in-time and from a snapshot, create a new cluster. The
decided way to point the application at it is `scripts/restore_repoint.sh`: it
renames the restored cluster onto the stack's identifier and the original out
of it, so the endpoint the tasks carry reaches the restored database with
nothing redeployed, and it checks the password and the endpoint before the API
starts. It was rehearsed on a staging account on 2026-10-11 (UTC): a restore, a
cutover, and a cutover undone ([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-6157399822)).
[Restore from PITR](disaster-recovery.md#restore-from-pitr-preferred)
in the Disaster Recovery Plan gives it step by step, with what it cannot do.

The cluster's identifier is generated by CloudFormation; the stack publishes it:

```bash
CLUSTER=$(aws cloudformation describe-stacks --stack-name "experimentation-database-$ENV" \
  --query "Stacks[0].Outputs[?OutputKey=='ClusterIdentifier'].OutputValue" --output text)
```

Find when the most recent pre-deployment snapshot was taken; the restore time
must be before the migration. A deploy takes a snapshot before migrating, named
`pre-deploy-<env>-<tag>-<time>`; a manual migration's is named
`pre-migration-...`:

```bash
aws rds describe-db-cluster-snapshots \
  --db-cluster-identifier "$CLUSTER" \
  --query 'sort_by(DBClusterSnapshots, &SnapshotCreateTime)[-5:].{ID:DBClusterSnapshotIdentifier,Time:SnapshotCreateTime,Status:Status}'
```

Aurora restores to any second within the cluster's backup retention period,
up to its `LatestRestorableTime` (usually within 5 minutes of the present).
The stack sets 35 days in prod and staging and 1 day elsewhere. Check what the
cluster keeps:

```bash
aws rds describe-db-clusters --db-cluster-identifier "$CLUSTER" \
  --query 'DBClusters[].BackupRetentionPeriod'
```

Restore to a new cluster at a time before the migration, and point the
application at it, with Steps 1 to 4 of
[Restore from PITR](disaster-recovery.md#restore-from-pitr-preferred), restoring
to the time you found above. The cutover there starts the API again on the
revision it already serves, with the task count and scaling it had. Before the
next deploy, change the release: a deploy of the release that contains the
migration runs it again.

**RTO: on 2026-10-11 (UTC), on a staging database holding little data, the restore took 11 min 41 s and the cutover took the API down for 12 min 45 s ([#295](https://github.com/Controlvar/controlvar/issues/295#issuecomment-6157399822)); a larger database restores more slowly. The recovery point is the time you restore to.**

---

## Restart the API on the revision it is serving

Use this when the API's tasks have to start again on the release they already run: after a
secret they read at start has changed ([Secrets Management](secrets-management.md#restart-the-api-on-its-current-release)),
or after a restore, once the tasks would come back pointing at the right database. ECS reads
each secret into a task when the task starts, so only new tasks see a new value.

The preferred way is the **Deploy** workflow, run with the tag already serving;
[Secrets Management](secrets-management.md#with-the-deploy-workflow) says what it does and what
it asks for. The commands below are for when the workflow is not available. They need an AWS
role allowed to read the service and the task definitions, register a task definition, and
create and continue a CodeDeploy deployment, and, for the last step, a checkout of this
repository.

NOT `aws ecs update-service --force-new-deployment`, the usual way to restart an ECS service.
This service has a CodeDeploy deployment controller, and on such a service UpdateService
changes only the desired count, the deployment configuration, the health check grace period,
task placement and tag settings: new tasks come from a CodeDeploy deployment.

**Step 1. Is a deployment still active?** CodeDeploy accepts one deployment per deployment group
at a time. A deploy's deployment stays active for about an hour after its traffic shift, while
the group keeps the old task set, so a restart inside that hour is refused. Nothing printed
means you can go on:

```{.bash skip reason="aws: reads the real deployment group"}
aws deploy list-deployments \
  --application-name experimentation-platform-$ENV \
  --deployment-group-name "experimentation-$ENV" \
  --include-only-statuses Created Queued InProgress Baking Ready \
  --query deployments --output text
```

If a deployment is listed, wait for it to end (`aws deploy get-deployment --deployment-id <id>`
shows its status), then start again here. Do not stop it by hand to make room: stopping it with
auto-rollback moves traffic back to the task set kept from before that deploy, whose tasks
started before the change and still hold the old value. If the restart cannot wait, the one
faster way to start new tasks is [Method 1](#method-1-github-actions-manual-rollback-preferred)
to the previous revision: it stops that deployment and creates its own, whose new tasks read the
current values. That runs the previous release (read
[The rolled-back API runs against the current schema](#the-rolled-back-api-runs-against-the-current-schema)
first), and its own deployment is then active for its hour in turn.

**Step 2. Read the revision serving**, from the PRIMARY task set (not
`services[0].taskDefinition`, which on this service never moves). It must print exactly one ARN;
two, or none, means a deployment is in progress: go back to Step 1.

```{.bash skip reason="aws: reads the real service"}
CURRENT=$(aws ecs describe-services \
  --cluster "experimentation-$ENV" \
  --services experimentation-backend-$ENV \
  --query "services[0].taskSets[?status=='PRIMARY'].taskDefinition" \
  --output text)
printf 'serving: %s\n' "$CURRENT"
```

**Step 3. Register a copy of it as a new revision.** Same image, same settings, a new ARN, so
the check in Step 6 cannot pass before the new tasks are serving:

```{.bash skip reason="aws: registers a real task definition revision"}
CURRENT_TD=$(aws ecs describe-task-definition --task-definition "$CURRENT" \
  --query taskDefinition --output json)
COPY=$(jq -c 'del(.taskDefinitionArn, .revision, .status, .requiresAttributes,
  .compatibilities, .registeredAt, .registeredBy, .deregisteredAt)' <<<"$CURRENT_TD")
NEW_ARN=$(aws ecs register-task-definition --cli-input-json "$COPY" \
  --query taskDefinition.taskDefinitionArn --output text)
printf 'restarting on: %s\n' "$NEW_ARN"
```

**Step 4. Create the deployment, all at once.** `--deployment-config-name` overrides the group's
CANARY_10_PERCENT_15_MINUTES: the revision is the one already serving, and the canary would
keep 90% of traffic on the old tasks for another fifteen minutes.

```{.bash skip reason="aws: creates a real CodeDeploy deployment"}
APPSPEC=$(jq -cn --arg td "$NEW_ARN" '{
  version: 1,
  Resources: [{ TargetService: {
    Type: "AWS::ECS::Service",
    Properties: {
      TaskDefinition: $td,
      LoadBalancerInfo: { ContainerName: "backend", ContainerPort: 8000 }
    }
  }}]
}')

DEPLOYMENT_ID=$(aws deploy create-deployment \
  --application-name experimentation-platform-$ENV \
  --deployment-group-name "experimentation-$ENV" \
  --deployment-config-name CodeDeployDefault.ECSAllAtOnce \
  --description "restart on the revision serving" \
  --revision "$(jq -cn --arg c "$APPSPEC" '{revisionType:"AppSpecContent",appSpecContent:{content:$c}}')" \
  --query deploymentId --output text)
printf 'deployment: %s\n' "$DEPLOYMENT_ID"
```

**Step 5. APPROVE THE TRAFFIC SHIFT. Do not skip this.** The deployment group waits up to 30
minutes for the approval once the new task set is up (status `Ready`), then stops the deployment,
and auto-rollback discards the new tasks: a restart that is never approved never happens. If the
loop prints `Failed` or `Stopped`, stop here; the old tasks are still serving, and
`aws deploy get-deployment --deployment-id "$DEPLOYMENT_ID"` says why.

```{.bash skip reason="aws: reads and approves a real CodeDeploy deployment"}
STATUS=""
until [ "$STATUS" = "Ready" ] || [ "$STATUS" = "Failed" ] || [ "$STATUS" = "Stopped" ]; do
  sleep 5
  STATUS=$(aws deploy get-deployment --deployment-id "$DEPLOYMENT_ID" \
             --query deploymentInfo.status --output text)
done
printf 'deployment %s: %s\n' "$DEPLOYMENT_ID" "$STATUS"
```

Then, only if it printed `Ready`:

```{.bash skip reason="aws: approves a real CodeDeploy deployment"}
aws deploy continue-deployment --deployment-id "$DEPLOYMENT_ID" \
  --deployment-wait-type READY_WAIT
```

**Step 6. Confirm the new revision is serving**, from the repository root.
`scripts/api_serving.py` exits 0 when the new revision is the PRIMARY task set and the
HTTPS listener's `/api/*` rule forwards to that task set's target group, and 1 while the shift
is still under way. The loop ends on any other answer, or when the deployment fails or is
stopped:

```{.bash skip reason="aws: reads the real service, deployment and load balancer"}
STATUS=""
RC=1
while [ "$RC" -eq 1 ] && [ "$STATUS" != "Failed" ] && [ "$STATUS" != "Stopped" ]; do
  sleep 10
  RC=0
  python3 scripts/api_serving.py "experimentation-$ENV" "experimentation-backend-$ENV" "$NEW_ARN" || RC=$?
  STATUS=$(aws deploy get-deployment --deployment-id "$DEPLOYMENT_ID" \
             --query deploymentInfo.status --output text)
done
printf 'api_serving exit %s; deployment %s: %s\n' "$RC" "$DEPLOYMENT_ID" "$STATUS"
```

Exit 0 means the restart is done: every request now reaches the new tasks. Exit 3 means the
new tasks are PRIMARY but the `/api/*` rule forwards elsewhere: read
[The API route and the PRIMARY task set disagree](deployment-guide.md#the-api-route-and-the-primary-task-set-disagree).
Exit 2 means the script could not tell; it prints why. The deployment itself reads `Succeeded`
only after the group terminates the old task set, about an hour after the shift; until then
an alarm, or a stop with auto-rollback, can still move traffic back to the old tasks.

---

## Post-Rollback Checklist

Complete every item before closing the incident. Do not declare the incident resolved until all boxes are checked.

### Immediate Verification (within 5 minutes of rollback)

- [ ] `GET /health` returns `{"status": "healthy"}` with HTTP 200
- [ ] 5xx error rate returned to < 0.1% baseline
- [ ] p99 API latency returned to < 500ms
- [ ] The API's PRIMARY task set runs its desired count (2 in staging, 3 in
      prod) with 0 pending, on the revision you rolled back to. Not the
      service's running count: until the replaced task set is removed, an
      hour after the shift, that counts both task sets
- [ ] ECS deployment status is `PRIMARY` with a single active deployment
- [ ] The dashboard's running count equals its desired count (1 in staging,
      2 in prod), on the revision you meant, `rolloutState` `COMPLETED`
- [ ] `GET https://app.<domain>/` returns 200 with `text/html`

Quick health verification commands:

```bash
curl -sf "https://app.<domain>/health" | python3 -m json.tool
aws ecs describe-services \
  --cluster "experimentation-$ENV" \
  --services experimentation-backend-$ENV \
  --output json \
  --query "services[0].taskSets[?status=='PRIMARY'] | [0].{Serving:taskDefinition,Running:runningCount,Desired:computedDesiredCount,Pending:pendingCount}"
aws ecs describe-services \
  --cluster "experimentation-$ENV" \
  --services experimentation-dashboard-$ENV \
  --query "services[0].deployments[?status=='PRIMARY'].{td:taskDefinition,state:rolloutState,running:runningCount,desired:desiredCount}"
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' "https://app.<domain>/"
```

### Incident Documentation (within 30 minutes of rollback)

- [ ] Bad image tagged so nobody mistakes it for a good one (the deploy pushes
      `:<tag>-<profile>`):

```bash
aws ecr put-image \
  --repository-name experimentation-platform/backend \
  --image-tag "bad-v1.2.3-full-do-not-deploy" \
  --image-manifest "$(aws ecr batch-get-image \
    --repository-name experimentation-platform/backend \
    --image-ids imageTag=v1.2.3-full \
    --query 'images[0].imageManifest' --output text)"
```

- [ ] The release notes of the bad release say so, and its git tag stays.

See also [Responding to an incident on your deployment](../security/incident-response.md).
