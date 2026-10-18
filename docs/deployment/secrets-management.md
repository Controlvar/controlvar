# Secrets Management — Controlvar

**Audience:** DevOps, Security Engineers

---

## Overview

On AWS, the API and migration tasks take their secrets from AWS Secrets Manager. Each task
definition names its secrets in its `secrets` key, and ECS reads their values into the
container's environment when a task starts.

- The secrets a human creates are under `/<env>/experimentation/` (below). The database's
  user and password are in the secret the database stack generates.
- Each task's execution role may read exactly the secrets its task definition names, by their
  complete ARNs ([below](#iam-permissions-for-secret-access)).
- No stack schedules the rotation of a secret:
  [Emergency Secret Rotation](#emergency-secret-rotation) rotates one by hand.

---

## Required Secrets (Create Before First Deploy)

Create all of the following before running the first production deployment. These commands must be run by an IAM identity with `secretsmanager:CreateSecret` permission.

The secrets a human creates are:

| Secret | Profile |
|--------|---------|
| `/<env>/experimentation/jwt-secret` | both |
| `/<env>/experimentation/first-superuser-password` | both |
| `/<env>/experimentation/audit-hmac-key` | `full` only |

Nothing else: the database credentials and the Redis connection come from their
stacks (below).

### Database credentials: nothing to create

The database stack generates
Aurora's master credentials into its own secret,
`experimentation-database-<env>-aurora-credentials` (JSON with `username` and
`password` fields; the ARN is the stack's `SecretArn` output), and creates the
cluster with them. Both backend task definitions read `POSTGRES_USER` and
`POSTGRES_PASSWORD` from that secret's two fields, and `POSTGRES_SERVER` from
the stack's writer endpoint, so the credentials the tasks present are by
construction the ones the cluster has.

The generated password may contain any printable character except
`" @ / \`. The application percent-encodes `POSTGRES_USER` and
`POSTGRES_PASSWORD` when it builds the connection URL, so no escaping is needed
there.

**A `DATABASE_URI` supplied directly is used exactly as given, so it must
already be percent-encoded.** If you set `DATABASE_URI` (or
`SQLALCHEMY_DATABASE_URI`) yourself instead of the `POSTGRES_*` variables,
encode the user and password first -- a `#`, `?`, `%`, `:`, `/`, `@` or space
left raw either breaks the URL or silently sends a different password:

```bash
python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$PASSWORD"
```

(`quote`, not `quote_plus`: the driver decodes `+` literally, so a space
encoded as `+` would come back as `+`.)

### JWT Signing Secret

The JWT secret must be at least 32 characters.

```bash
JWT_SECRET=$(openssl rand -base64 48)

aws secretsmanager create-secret \
  --name /prod/experimentation/jwt-secret \
  --description "JWT token signing secret for the experimentation platform API" \
  --secret-string "$JWT_SECRET" \
  --tags '[{"Key":"Environment","Value":"production"},{"Key":"Service","Value":"experimently"}]'
```

### Redis: nothing to create

The API task definition takes its Redis connection from the Redis stack
(`experimentation-redis-<env>`), as plain environment, not secrets:

| Variable | Value |
|----------|-------|
| `REDIS_HOST` | the replication group's **primary** endpoint address |
| `REDIS_PORT` | the primary endpoint's port |
| `REDIS_SSL` | `true` |

The replication group has in-transit encryption on, so it refuses a plaintext
connection; `REDIS_SSL=true` makes every Redis client the application builds
connect over TLS (`ssl=True`). It defaults to `false`, for the plaintext
`redis:7` used locally and in CI. The replication group has no AUTH token, so
there is no password to store: `REDIS_PASSWORD` stays unset.

Redis is optional to the application: without it the rate limiter falls back
to per-task memory (and retries Redis every 30 s) and the caches are skipped, and `/health/ready` still
answers 200 unless `REDIS_REQUIRED=true`. So a Redis the tasks cannot reach (a
TLS failure, say) does **not** fail a deployment by itself. Two things catch it:

- set `REDIS_REQUIRED=true` in staging, so readiness -- the ALB health check --
  fails without Redis; or
- read `checks.redis.status` from the `/health/ready` body, which is
  `healthy` or `unhealthy` in every environment (production shows the status
  only, not the error).

### First Superuser Password

The bootstrap creates the first administrator with this password. It has no
usable default: `FIRST_SUPERUSER_PASSWORD` falls back to `admin`, which the
production settings reject outright — without this secret the container exits
before uvicorn binds, on **either** profile.

```bash
SUPERUSER_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

aws secretsmanager create-secret \
  --name /prod/experimentation/first-superuser-password \
  --description "Password for the first administrator account (FIRST_SUPERUSER_PASSWORD)" \
  --secret-string "$SUPERUSER_PASSWORD" \
  --tags '[{"Key":"Environment","Value":"production"},{"Key":"Service","Value":"experimently"}]'
```

### Compliance Audit HMAC Key (`profile: full` only)

Signs the SOC 2 / ISO 27001 compliance audit log (HMAC-SHA256). Only module
code reads it, and `modules.register(hooks)` builds those settings as its first
step: a **full** deployment without this secret fails the registration, so the
API refuses to start and every `alembic` command fails with it. A `core`
deployment never reads it and does not need the secret at all.

```bash
AUDIT_HMAC_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

aws secretsmanager create-secret \
  --name /prod/experimentation/audit-hmac-key \
  --description "HMAC-SHA256 key signing the compliance audit log (AUDIT_HMAC_KEY)" \
  --secret-string "$AUDIT_HMAC_KEY" \
  --tags '[{"Key":"Environment","Value":"production"},{"Key":"Service","Value":"experimently"}]'
```

Rotating it does not invalidate old rows, but signatures made with the previous
key no longer verify — re-verify or re-sign before rotating.

### Verify All Secrets Exist

```bash
aws secretsmanager list-secrets \
  --filters Key=name,Values=/prod/experimentation/ \
  --query 'SecretList[*].{Name:Name,ARN:ARN,LastChanged:LastChangedDate}' \
  --output table
```

Deploy checks the secrets by the references ECS will use, before it builds
anything ("Every secret the task definitions reference exists"). It reads
every `valueFrom` in the API's, the migration's and the dashboard's task
definitions, refuses any that is not a complete ARN in the environment's
account and region (a partial ARN, one that stops at the name, is refused
before any Secrets Manager call), requires the API and migration task
definitions to reference `jwt-secret` and `first-superuser-password`, plus
`audit-hmac-key` for the `full` profile, and then runs `describe-secret` on
each complete ARN. Its log names each secret by name only, never by ARN. The
database credentials are not under this prefix: they are the database stack's
`experimentation-database-<env>-aurora-credentials`. Nor is Redis: see
[Redis: nothing to create](#redis-nothing-to-create).

### Give the CDK each secret's complete ARN

The task definitions name each secret by its **complete ARN**, which ends in a
six-character suffix Secrets Manager adds when the secret is created
(`.../secret:/<env>/experimentation/jwt-secret-<suffix>`). The suffix cannot be
worked out from the name, so `cdk synth`, `cdk diff`, `cdk deploy` and
`cdk destroy` take the three ARNs from the environment, in **every**
environment (`dev` and `demo` too):

| Variable | Secret | Needed |
|----------|--------|--------|
| `JWT_SECRET_ARN` | `/<env>/experimentation/jwt-secret` | always |
| `FIRST_SUPERUSER_PASSWORD_SECRET_ARN` | `/<env>/experimentation/first-superuser-password` | always |
| `AUDIT_HMAC_KEY_SECRET_ARN` | `/<env>/experimentation/audit-hmac-key` | `profile: full` only |

Read them (read-only) and export them in the shell that runs the CDK, with
the region the stacks deploy to. The third is for `profile: full` only:

```bash
ENV=staging
export JWT_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "/$ENV/experimentation/jwt-secret" --query ARN --output text)
export FIRST_SUPERUSER_PASSWORD_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "/$ENV/experimentation/first-superuser-password" --query ARN --output text)
export AUDIT_HMAC_KEY_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "/$ENV/experimentation/audit-hmac-key" --query ARN --output text)
```

Synth refuses a value that is missing, that stops at the name (a *partial*
ARN, which ECS cannot resolve), that is for a different environment
(`/prod/...` in staging), that is in a region or account other than the
stack's, or that names another of the three secrets.
The refusal names the variable and prints the command above; it never prints
the value, because an ARN carries the account ID. For the same reason, keep
these values out of issues, pull requests and chat.

**Recreating a secret changes its ARN.** Deleting and re-creating
`/<env>/experimentation/jwt-secret` gives it a new suffix: update the input and
run `cdk deploy` again, or the next task start fails with
`ResourceNotFoundException`. Changing a secret's *value*
(`put-secret-value`, or rotation) keeps its ARN and needs nothing here.
---

## How Secrets Are Injected at Runtime

The ECS task definition references secrets by their complete ARN. At container start, ECS fetches the current secret value from Secrets Manager and sets it as an environment variable.

The API task definition in `infrastructure/cdk/stacks/fargate_service_stack.py`, and the
migration task in `migration_task_stack.py`, import each secret by its complete ARN, from the
synth inputs [above](#give-the-cdk-each-secrets-complete-arn), and map it to a variable:

| Variable | From |
|----------|------|
| `SECRET_KEY` | `/<env>/experimentation/jwt-secret` |
| `FIRST_SUPERUSER_PASSWORD` | `/<env>/experimentation/first-superuser-password` |
| `AUDIT_HMAC_KEY` | `/<env>/experimentation/audit-hmac-key` (`profile: full` only) |
| `POSTGRES_USER`, `POSTGRES_PASSWORD` | the `username` and `password` fields of the database stack's secret |

`REDIS_HOST`, `REDIS_PORT` and `REDIS_SSL` are plain environment, not secrets.
The application reads them as settings (`backend/app/core/config.py`) from that environment.

---

## Secret Rotation

No stack schedules the rotation of a secret. ECS reads a secret into a task when the task
starts, so a rotation reaches the API only after a restart:
[Restart the API on its current release](#restart-the-api-on-its-current-release). Not
`aws ecs update-service --force-new-deployment`: the API's service is controlled by CodeDeploy,
which that call is not for (below).

---

## Emergency Secret Rotation

Use these procedures when a secret is known or suspected to be compromised.

Each one ends by restarting the API, which CodeDeploy refuses for about an hour after any deploy
([A deployment is still active](#a-deployment-is-still-active)). Check that first, with the
Rollback Runbook's [Step 1](rollback-runbook.md#restart-the-api-on-the-revision-it-is-serving),
before you change the secret: from the change on, any task that starts uses the new value while
the rest keep the old one.

### Rotate JWT Secret Immediately

Rotating the JWT secret invalidates every token the API signed itself: local sign-in tokens and
the tokens it issues after an SSO sign-in. It does **not** take effect at once. Tasks that started before the change keep
accepting tokens signed with the old secret until the restart's traffic shift has completed;
[How long the old value keeps working](#how-long-the-old-value-keeps-working) says how long that
is. After the shift, users of those tokens must sign in again.

Step 1: Generate a new JWT secret:

```bash
NEW_JWT_SECRET=$(openssl rand -base64 48)
```

Step 2: Update the secret value in Secrets Manager:

```bash
aws secretsmanager put-secret-value \
  --secret-id /prod/experimentation/jwt-secret \
  --secret-string "$NEW_JWT_SECRET"
```

Step 3: Restart the API so new tasks read the new value:
[Restart the API on its current release](#restart-the-api-on-its-current-release). The rotation
is done when that procedure confirms the new revision is serving, not when the secret is
updated.

### Rotate Database Password Immediately

Step 1: Generate a new password. `token_urlsafe` uses only letters, digits, `-` and `_`, none of which Aurora refuses in a master password:

```bash
NEW_DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
```

Step 2: Set it on the cluster FIRST:

```bash
aws rds modify-db-cluster \
  --db-cluster-identifier <the cluster in experimentation-database-prod> \
  --master-user-password "$NEW_DB_PASSWORD" \
  --apply-immediately
```

Step 3: Put the same password in the database stack's secret (the tasks read its `username` and `password` fields):

```bash
aws secretsmanager put-secret-value \
  --secret-id experimentation-database-prod-aurora-credentials \
  --secret-string "$(python3 -c 'import json, sys; print(json.dumps({"username": "postgres", "password": sys.argv[1]}))' "$NEW_DB_PASSWORD")"
```

Step 4: Restart the API so the tasks reconnect with the new credentials:
[Restart the API on its current release](#restart-the-api-on-its-current-release). Until its
traffic shift completes, the running tasks keep the old password: connections they already hold
keep working, and any new connection they open is refused.

### Rotate Compromised API Key (Application-Level)

For application-level API keys managed by the platform itself:

Step 1: Revoke the compromised key. Only its SHA-256 hash is stored, so hash the leaked key first:

```bash
KEY_HASH=$(printf '%s' "$LEAKED_KEY" | shasum -a 256 | cut -d' ' -f1)
```

Then, connected to Aurora, deactivate that row (every request with the key then gets 401). The
`RETURNING` clause prints what steps 2 and 4 need: the key's owner (`user_id`), its `scopes`
(empty when it has none, otherwise a comma-separated list such as `sdk:ruleset`) and when it was
last used:

```sql
UPDATE experimentation.api_keys SET is_active = false WHERE key = '<KEY_HASH>' RETURNING id, name, user_id, scopes, last_used_at;
```

Step 2: Issue a replacement, owned by the person who owned the old key. A key belongs to whoever
creates it: `POST /api/v1/api-keys` always creates the key for the caller, so a key an ADMIN
creates on someone else's behalf is the ADMIN's own, and it stops working when that ADMIN's
account is deactivated. So the owner (`user_id` from step 1) signs in as themselves
([Authentication](../api/auth.md)) and creates the replacement with the `scopes` step 1 printed,
as a JSON list:

```bash
curl -X POST \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Replacement key", "scopes": ["sdk:ruleset"]}' \
  https://app.example.com/api/v1/api-keys
```

Leave `scopes` out when step 1 printed none. A server SDK that evaluates flags locally uses a key
with the `sdk:ruleset` scope: its replacement needs the same scope, because
`GET /api/v1/sdk/ruleset` answers 403 to a key without it. Only users who can change feature
flags (ADMIN, DEVELOPER, or a superuser) can create a key with that scope; for any other owner the
API answers 403 and creates nothing, so someone with one of those roles creates the key, and
it is theirs. An owner with a superuser account can instead use **Admin → API Keys** (the admin
area is for superusers) and tick **Server-side local evaluation (sdk:ruleset)**.

Step 3: Put the new key where the service that used the old one reads it (a secrets manager or
that service's deployment settings, not Slack or email), and remove the old key from wherever it
was copied.

Step 4: Find out whether the key was used. What the platform records about a key's use:

- **`last_used_at`** on the key's row is when the key last authenticated a request, in UTC. The API writes
  it at most once a minute per key, so it can trail the last request by up to a minute. Step 1
  printed it, and an inactive key keeps it. An ADMIN can read it with
  `GET /api/v1/api-keys?all=true&include_inactive=true`, and a superuser in the **Last used**
  column of **Admin → API Keys** with **All users (admin)** and **Include inactive** ticked. A
  time later than the last request your own service made with the key means someone else used it.
  Deleting a key removes its row and this time with it, so read it before you delete the key.
- **The API's request log**, the CloudWatch log group `/ecs/experimentation-backend-$ENV`, has an
  access line for each request with the peer address, the method, the path, the status and a
  request id. No line says which key made a request: the API logs no key, no part of one and no key
  id (the one exception is a warning that names the key's id when the `last_used_at` write fails),
  so the log cannot be searched for a key. It can show whether a route that only an `sdk:ruleset`
  key may call was called while the key was out. This looks for the one that returns every flag's
  targeting rules; set `START_MS` to when the key may first have been copied (this form works on
  macOS and Linux):

```bash
START_MS=$(python3 -c 'import time; print(int((time.time() - 7 * 86400) * 1000))')
aws logs filter-log-events \
  --log-group-name "/ecs/experimentation-backend-$ENV" \
  --filter-pattern '"/api/v1/sdk/ruleset"' \
  --start-time "$START_MS" \
  --query 'events[].message' --output text
```

The key's owner, or an ADMIN, can instead delete it through the API with `DELETE /api/v1/api-keys/{id}`, or from **Admin → API Keys** in the dashboard. Neither the plaintext nor its `eptk_xxxx` prefix is stored, so identify the key by its name, or get its `id` from the `RETURNING` clause above. `KEY_HASH` is the hex SHA-256 of the key, which is what the platform stores (`hash_api_key` in `backend/app/core/security.py`).

If the key may have been exposed through a vulnerability in Controlvar itself, report it privately as [SECURITY.md](https://github.com/Controlvar/controlvar/blob/main/SECURITY.md) describes; see [Responding to an incident on your deployment](../security/incident-response.md).

---

## Restart the API on its current release

ECS reads each secret into a task when the task starts. A changed value reaches the API only
through new tasks, and the API's service is controlled by CodeDeploy, so new tasks come from a
CodeDeploy deployment. Not `aws ecs update-service --force-new-deployment`, the usual way to
restart an ECS service: on a CodeDeploy-controlled service, UpdateService may change only the
desired count, the deployment configuration, the health check grace period, task placement and
tag settings, and a forced new deployment is not among them.

### With the Deploy workflow

The preferred way: run **Deploy** again with the release that is already serving.

1. Find that release. The API's revision names its image by digest; the repository gives the
   tag (`vX.Y.Z-<profile>`) for it:

    ```{.bash skip reason="aws: reads the real service, task definition and repository"}
    CURRENT=$(aws ecs describe-services \
      --cluster "experimentation-$ENV" \
      --services "experimentation-backend-$ENV" \
      --query "services[0].taskSets[?status=='PRIMARY'].taskDefinition" \
      --output text)
    IMAGE=$(aws ecs describe-task-definition --task-definition "$CURRENT" \
      --query "taskDefinition.containerDefinitions[?name=='backend'].image | [0]" \
      --output text)
    aws ecr describe-images --repository-name experimentation-platform/backend \
      --image-ids imageDigest="${IMAGE#*@}" \
      --query "imageDetails[0].imageTags" --output text
    ```

2. **Actions → Deploy → Run workflow** from `main`, with `environment`, `version` the tag's
   `vX.Y.Z` part and `profile` its profile part.
3. The environment's required reviewer approves the run, as for any deploy.

With a release already deployed, the run (the [deployment guide](deployment-guide.md#3-every-deploy)
has every step):

- refuses, before changing anything, while an earlier CodeDeploy deployment is still active or
  an API alarm is firing;
- reuses the image already in the repository (`already exists (...): reusing it`) instead of
  building one;
- takes a database snapshot and runs the migration task, which has nothing new to apply;
- registers a new API revision naming the same image and creates a CodeDeploy deployment with
  the deployment group's canary;
- approves the traffic shift itself once every new task is healthy: 10% of traffic moves to the
  new tasks, then the rest fifteen minutes later;
- succeeds only when the new revision is the PRIMARY task set and the `/api/*` rule forwards to
  its target group, then sends a smoke request and rolls the dashboard onto a new revision of
  its own unchanged image.

### By hand

When the workflow is not available:
[Restart the API on the revision it is serving](rollback-runbook.md#restart-the-api-on-the-revision-it-is-serving)
in the Rollback Runbook.

### A deployment is still active

CodeDeploy accepts one deployment per deployment group at a time, and every deploy's deployment
stays active for about an hour after its traffic shift while the group keeps the old task set.
Inside that hour Deploy refuses with `A deployment is still active`, naming the deployment and
roughly how long it has left, and a deployment created by hand is refused too. Wait for it to
end and restart then. Stopping it does not help: it moves traffic back to the task set kept from
before that deploy, whose tasks still hold the old value. The Rollback Runbook's
[Step 1](rollback-runbook.md#restart-the-api-on-the-revision-it-is-serving) gives the one faster
path and what it costs.

### How long the old value keeps working

Tasks keep the value they started with. Until the traffic shift completes, some or all requests
still reach tasks that started before the change:

- **With Deploy**: from the change until the end of the canary, which is the approval of the
  run, the snapshot and the migration, starting the new tasks, and fifteen minutes with 90% of
  traffic still on the old ones: at least the canary's fifteen minutes, plus however long the
  approval and the steps before it take.
- **By hand**: until the all-at-once shift, a few minutes after `continue-deployment`.
- **During that time**, a task that starts in the serving set (a scale-out, or a replacement for
  a task that stopped) reads the new value. For the JWT secret, a token one task signed can be
  refused by another until the shift.
- **For an hour after the shift** the old tasks get no traffic but are kept. An alarm, or a stop
  with auto-rollback, in that hour moves traffic back to them, and with it the old value.

---

## IAM Permissions for Secret Access

The CDK stacks grant `secretsmanager:GetSecretValue` to each task's execution role on exactly the
complete ARNs its task definition names (the synth inputs above), and nothing wider.

---

## Staging Secrets

Staging uses separate secrets under `/staging/experimentation/` with the same structure.

Create staging secrets (same structure, different values). The database credentials are generated by experimentation-database-staging:

```bash
aws secretsmanager create-secret \
  --name /staging/experimentation/jwt-secret \
  --secret-string "$(openssl rand -base64 32)"
```

Never copy production secrets to staging. Never use staging secrets in production.
