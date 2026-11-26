---
title: Flag environments, production, staging and development
description: "A flag has its own settings in production, staging and development, and every API key is answered those of its environment: configure, test and promote a flag before it reaches your users."
---

# Flag Environments (beta)

A flag has settings in three environments, `production`, `staging` and `development`: whether it
is on, its rollout percentage and its targeting rules. Every API key belongs to one of them and is
answered that environment's settings, so a change can be made and tested in staging before any
production user gets it. Environments are in beta: their routes and the dashboard may change.

The commands on this page run as written against the stack from the
[Quick Start](../getting-started/quick-start.md), in one terminal, top to bottom. Each one uses the
shell variables set by the ones before it.

## What an environment is

- The environments are `production`, `staging` and `development`, and there are no others.
  They are lower case in the API and capitalised in the dashboard: Production, Staging and
  Development.
- **Production's settings are the flag's own.** They are what the flag's page and
  `PUT /api/v1/feature-flags/{id}` have always changed, and every API key made before environments
  existed is a production key.
- In staging and development a flag is off until someone configures it there: with no settings it
  is off, at 0%, with no rules.
- A flag's key, name, description, `value_type`, `on_value` and `off_value` are the flag's, and are the same in every
  environment.
- An archived flag is off in every environment, whatever its settings there.
- A user who is inside a rollout percentage in one environment is inside the same percentage in
  every environment.
- The environment of a flag is not the deployment setting `ENVIRONMENT`: an installation whose
  `ENVIRONMENT` is `development` still answers its flags from production, until a key says
  otherwise.

Log in, then list the environments:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/environments -H "Authorization: Bearer $TOKEN" | jq -c '[.[].key]'
```
<!-- expect: ["production","staging","development"] -->

It prints `["production","staging","development"]`. Any signed-in user can read the list.

## Keys and environments

A key belongs to one environment, chosen when it is created and never changed: to move a key, create
a new one and delete the old. A key made without an `environment` is a production key.

This creates a flag, then a key for staging and one for production, and prints the `environment`
of each key from the create response:

```{.bash exec}
FLAG=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"key": "checkout-banner", "name": "Checkout banner", "rollout_percentage": 100}')
FLAG_ID=$(jq -r .id <<<"$FLAG")

curl -s -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"is_active": true}' | jq -c '{status, rollout_percentage}'

STAGING=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "checkout staging", "environment": "staging"}')
STAGING_KEY=$(jq -r .key <<<"$STAGING")
jq -c '{name, environment}' <<<"$STAGING"

PRODUCTION=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "checkout production"}')
PRODUCTION_KEY=$(jq -r .key <<<"$PRODUCTION")
jq -c '{name, environment}' <<<"$PRODUCTION"
```
<!-- expect: {"status":"active","rollout_percentage":100} -->
<!-- expect: {"name":"checkout staging","environment":"staging"} -->
<!-- expect: {"name":"checkout production","environment":"production"} -->

The flag is on in production. The create response, and the list of keys, name each key's
`environment`; check it before you hand a key out. `environment` takes `production`, `staging` or
`development`, and anything else, `null` included, answers `422`.

An evaluation answers the settings of the environment of the key that asks. The staging key has no
settings for the flag yet, so it is answered off, and the production key is answered on:

```{.bash exec}
curl -s "localhost:8000/api/v1/feature-flags/evaluate/checkout-banner?user_id=u-1" \
  -H "X-API-Key: $STAGING_KEY" | jq -c '{enabled, reason}'

curl -s "localhost:8000/api/v1/feature-flags/evaluate/checkout-banner?user_id=u-1" \
  -H "X-API-Key: $PRODUCTION_KEY" | jq -c '{enabled, reason}'
```
<!-- expect: {"enabled":false,"reason":"inactive"} -->
<!-- expect: {"enabled":true,"reason":"rollout"} -->

The routes that answer a key's environment are the flag evaluation routes,
`GET` and `POST /api/v1/feature-flags/evaluate/{flag_key}` and
`GET /api/v1/feature-flags/user/{user_id}`, and the ruleset, `GET /api/v1/sdk/ruleset`, which has a
version of its own in each environment. An SDK that evaluates locally with a staging key evaluates
staging's rules. An answer has the fields it always had.

Pass a staging key to an SDK as its API key setting (`apiKey` in the JavaScript SDK, `api_key` in
the Python SDK), as you would a production key: nothing else in your setup changes.

Experiments and event tracking run in production only. The routes that assign users and record
events (for example `/tracking/assign`, `/assign/batch`, `/alias`, `/track`, `/events`, `/batch`
and `/visual`) refuse a staging or development key with `403`, and write nothing. Error and evaluation reports are
not refused; see below:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/track \
  -H "X-API-Key: $STAGING_KEY" \
  -H 'content-type: application/json' \
  -d '{"event_type": "conversion", "event_name": "purchase", "user_id": "u-1"}' | jq -r .detail
```
<!-- expect: This API key belongs to the staging environment. Experiments and event tracking run in production only; use a production key. -->

It prints `This API key belongs to the staging environment. Experiments and event tracking run in production only; use a production key.`

A staging or development key cannot carry the `mcp:read` scope, because the routes it opens read
production only. An error report sent with such a key is stored and is in no safety number, and an
evaluation count sent with one answers `accepted: 0` and is not recorded. See
[Safety monitoring](safety.md#which-errors-count).

In the dashboard, **Admin → API Keys → Create API Key** and **Your API keys** both offer an
**Environment**, Production first, and both lists show each key's environment. Each checks the
create response, and a key whose response does not name the environment asked for is revoked and an
error is shown; if the revoke fails, the error says which key is left.

## Changing a flag in an environment

`GET /api/v1/feature-flags/{flag_id}/environments` lists the flag's settings in each environment,
and `PUT /api/v1/feature-flags/{flag_id}/environments/{environment}` changes them in one. The body
takes `is_active`, `rollout_percentage` and `targeting_rules`, each optional: only the fields sent
change, and `"targeting_rules": null` clears the rules. Anyone signed in can read; changing needs
the permission to change flags: the ADMIN and DEVELOPER roles, and in the full profile anyone whose
custom role or direct grant gives `feature_flag (update)`. A `PUT` to `production` is the change
`PUT /api/v1/feature-flags/{flag_id}` makes.

This turns the flag on in staging at 100%. The development settings are as they were, and the
production key is still answered on:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID/environments/staging \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"is_active": true, "rollout_percentage": 100}' | jq -c '{environment, status, rollout_percentage, configured}'

curl -s "localhost:8000/api/v1/feature-flags/evaluate/checkout-banner?user_id=u-1" \
  -H "X-API-Key: $STAGING_KEY" | jq -c '{enabled, reason}'

curl -s localhost:8000/api/v1/feature-flags/$FLAG_ID/environments/development \
  -H "Authorization: Bearer $TOKEN" | jq -c '{environment, status, configured}'
```
<!-- expect: {"environment":"staging","status":"active","rollout_percentage":100,"configured":true} -->
<!-- expect: {"enabled":true,"reason":"rollout"} -->
<!-- expect: {"environment":"development","status":"inactive","configured":false} -->

`configured` is `false` for a flag nobody has configured in that environment. A name that is not
an environment answers the standard `422`:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID/environments/qa \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"is_active": true}' | jq -r '.detail[0].msg'
```
<!-- expect: Input should be 'production', 'staging' or 'development' -->

It prints `Input should be 'production', 'staging' or 'development'`. The routes, their errors and
their permissions are in [API endpoints](../api/endpoints.md#flag-environments-beta).

## In the dashboard

The flag list and a flag's page have an **Environment** switch: Development, Staging and
Production. Production is marked **Live** beside it, and for anyone who may change flags its page says
`You are editing Production. Changes affect live users.` The environment is in the page's address,
as `?env=staging`, so a reload or a copied link opens the same one.

- A save in Staging or Development reads **Save changes in Staging** (or Development), and changes
  that environment only. A save in Production changes the flag as it always has.
- A flag with no settings in Staging or Development shows **Off**, with
  `Not configured in Staging`.
- **Compare environments** shows the flag's status, rollout and targeting rules in Development,
  Staging and Production side by side.
- A flag's values, and archiving or deleting it, apply in every environment, so the dashboard offers
  them in Production only. New flags are created in Production.

## Promotion

Promotion copies a flag's settings from one environment into another:
`POST /api/v1/feature-flags/{flag_id}/environments/{target}/promote`, with the body
`{"from": "<environment>"}`. It needs the permission that changing a flag needs. It copies the
status, the rollout percentage and the targeting rules, all together. It does not copy the flag's
`value_type`, `on_value` or `off_value`, its rollout schedules or their stage state, or its safety
settings. A source nobody has configured copies off.
A promotion into the environment it copies from answers `422`, and a promotion of an archived flag
leaves it archived.

This copies staging's settings into development:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/feature-flags/$FLAG_ID/environments/development/promote \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"from": "staging"}' | jq -c '{environment, status, rollout_percentage, configured}'
```
<!-- expect: {"environment":"development","status":"active","rollout_percentage":100,"configured":true} -->

In the dashboard, a flag in Development offers **Promote to Staging**, and a flag in Staging offers
**Promote to Production**; Production offers nothing. The dialog lists **Status**,
**Rollout percentage** and **Targeting rules** as they are in the target now and as they will be,
and says what is not copied. A promotion into Production has a second step before it is sent.

## What runs in production only

Rollout schedules, safety monitoring and experiments run in Production only.

- A rollout schedule changes the flag's production rollout percentage and nothing in staging or
  development. See [Gradual rollouts](rollouts.md#environments).
- Safety monitoring counts production's errors and evaluations, and a rollback changes production
  only. See [Safety monitoring](safety.md#environments).
- Experiments and event tracking take production keys only, as above.

Turning a flag off follows its environment too: see [Turning a flag off](kill-switch.md#environments).

Other flag routes read a flag's settings in staging and development as well. Archiving a flag with
bulk toggle turns it off there too, and unarchiving it leaves it off. A flag that is on in staging
or development is not [stale](stale-flags.md). A segment that a flag's staging or development rules
use is in use, and is listed with the environments that use it.

## Upgrading to 0.49.0

**Environments (beta).** A flag now has a production, a staging and a development configuration,
and every API key belongs to one environment. Upgrading changes nothing: your flags, rollout
schedules and safety settings are your production environment, every existing API key is a
production key, and SDKs get the same answers with no change. In staging and development every flag
is off until you configure or promote it. Safety monitoring, rollout schedules and experiments run
in production only. Nothing to do.

### A task still running 0.48.0

While a task still runs 0.48.0 against the upgraded database, during a rolling upgrade and after
any rollback of the API alone below 0.49.0, that task has no concept of environments. It treats a
staging or development key as a production key. It serves that key production's configuration. It
counts that key's server-side error reports, its failed evaluations and its evaluation counts in
production's safety numbers (error reports sent with a client-side key are stored but not counted,
as today), which can trigger or dilute a production rollback. It writes that key's assignments and
events into production experiments. After a rollback this lasts until someone acts; the rollback
runbook's 0.49.0 step removes those keys.

A key created through a task still running 0.48.0 is a production key, even when the request asks
for `staging` or `development`: 0.48.0 ignores the field, and its response carries no
`environment`. Check `environment` in the create response. The dashboard checks it, revokes a key
whose response does not match, and shows an error.

A flag that such a task archives keeps its staging and development settings as they were. They
answer off while the flag is archived, because an archived flag is off everywhere, and they answer
as before once the flag is unarchived, which can turn the flag back on in staging or development.
Check them with `GET /api/v1/feature-flags/{flag_id}/environments` after unarchiving.

To roll back, follow
[Rolling back past flag environments](../deployment/rollback-runbook.md#rolling-back-past-flag-environments).
[Migrations](../self-hosting/migrations.md#t2011_flag_environments-adds-each-flags-staging-and-development-settings)
says what the database downgrade loses and how the upgrade waits for its locks.
