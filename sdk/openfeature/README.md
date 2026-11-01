# @controlvar/openfeature-provider

**Beta.** This is a 0.x release: pin the exact version you test against.

[OpenFeature](https://openfeature.dev) provider for Controlvar
(`@openfeature/server-sdk`, Node >= 18). Every evaluation is delegated to
[`@controlvar/js-sdk`](https://github.com/Controlvar/controlvar/blob/main/sdk/js/README.md) and **decided by the server**; successful
answers are cached per user + flag. Nothing is bucketed locally and no flag definitions are downloaded,
unless you pass a `client` built with `evaluation: 'local'`.
Boolean, string, number and object calls are answered, and the evaluation context's attributes
are sent with each evaluation.

Full documentation: [`docs/sdk/openfeature.md`](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/openfeature.md).

## Install

**Not yet published.** `@controlvar/openfeature-provider` is not on npm, so build it from
source in a clone of this repository (the provider's build also builds `sdk/js`):

```bash
git clone https://github.com/Controlvar/controlvar.git
npm --prefix controlvar/sdk/js ci
npm --prefix controlvar/sdk/openfeature ci
npm --prefix controlvar/sdk/openfeature run build
```

Then, in your app, install both directories:
`npm install @openfeature/server-sdk /path/to/controlvar/sdk/js /path/to/controlvar/sdk/openfeature`.
From source the provider's manifest names `@controlvar/js-sdk` as `file:../js`, so install
that directory too; `@openfeature/server-sdk` is a peer dependency your app provides.

## Quick start

```ts
import { OpenFeature } from '@openfeature/server-sdk';
import { ExperimentationProvider } from '@controlvar/openfeature-provider';

const provider = new ExperimentationProvider({ apiKey: 'eptk_...', baseUrl: 'http://localhost:8000' });
await OpenFeature.setProviderAndWait(provider);

const client = OpenFeature.getClient();
const ctx = { targetingKey: 'user-123', plan: 'pro' };   // targetingKey → user_id (required); the rest → the user's attributes
const enabled = await client.getBooleanValue('dark-mode', false, ctx);                    // → enabled
const label = await client.getStringValue('checkout-button-label', 'Buy now', ctx);       // → the flag's string value
const limit = await client.getNumberValue('upload-limit-mb', 10, ctx);                    // → the flag's number value
const theme = await client.getObjectValue('checkout-theme', { color: 'blue' }, ctx);      // → the flag's JSON value

// Experiments and tracking are not OpenFeature concepts: use the JS SDK client underneath.
const assignment = await provider.client.getAssignment('checkout_flow', { userId: 'user-123', attributes: { plan: 'pro' } });
await provider.client.track('user-123', 'purchase', { value: 49.99, experimentKey: 'checkout_flow' });
await provider.client.track('user-123', 'page_view'); // no key → fanned out by the JS SDK's rule
```

## Options

`apiKey` (required unless `client` is given), `baseUrl` (`http://localhost:8000`), `cacheTtlMs`
(300000), `timeout` (5000 ms), `fetch`, `client` (reuse an existing JS SDK client and its cache).

## Context

`targetingKey` is the user id (`user_id`) and is required. Every other key of the evaluation
context is passed to the JS SDK as the user's attributes, which it sends on the evaluate call as
`context` (URL-encoded JSON), so the flag's targeting rules are evaluated against them. A `Date`,
at any depth, is sent as its ISO 8601 string. A context with only `targetingKey` sends no
`context`.

The JS SDK caches each answer per user + flag for `cacheTtlMs`. The attributes are not part of
that key: within `cacheTtlMs`, a second call for the same `targetingKey` and flag gets the first
answer (reason `CACHED`) whatever its attributes, so a changed attribute is seen once that answer
expires. `cacheTtlMs: 0` caches nothing; `provider.onClose()` or `provider.client.clearCache()`
drops every cached answer. A `client` you pass with `evaluation: 'local'` answers each call it
can from its ruleset, using that call's attributes, and such an answer is never reported `CACHED`.

## Resolution

Every call reads the value the flag serves: its on value when the flag is on for the user, its off
value otherwise. A boolean call reads a boolean flag (its value is `enabled`), a string or number
call a string or number flag, and an object call a JSON flag (an object or an array), returned as
a new copy each time. There is no coercion: a call of another type than the flag's gets your
default. A server without flag values sends no `value_type`; there a boolean call answers
`enabled`, and a string, number or object call gets your default.

| The server's answer | Value | Reason | Variant |
|---|---|---|---|
| `reason: "targeting_rule"`, on / off | the on value / the off value | `TARGETING_MATCH` | `"on"` / `"off"` |
| `reason: "rollout"`, on / off | the on value / the off value | `SPLIT` | `"on"` / `"off"` |
| `reason: "inactive"` | the off value, not your default | `DISABLED` | `"off"` |
| answered from the JS SDK's cache | as cached | `CACHED` | as cached |
| `reason: "error"` | your default | `ERROR`, code `GENERAL` | none |
| the flag serves another type than the call | your default | `ERROR`, code `TYPE_MISMATCH` | none |
| a string, number or object call, and no `value` (a server without flag values) | your default | `ERROR`, code `GENERAL` | none |
| a string, number or object call, and a `value` that is not of the flag's type | your default | `ERROR`, code `PARSE_ERROR` | none |

A server reason this provider does not know, or none, answers the value with reason `UNKNOWN`.

Errors never throw: 404 → `FLAG_NOT_FOUND`, a 2xx whose body is not JSON → `PARSE_ERROR`, missing
`targetingKey` → `TARGETING_KEY_MISSING`, anything else (401, 422, 5xx, timeout, network) →
`GENERAL`; the default is returned with reason `ERROR`, and a failed request is never cached. A
2xx whose JSON is not an evaluation is read as an answer with no `value_type`, `value`, `reason`
or `enabled`: a string, number or object call gets your default with `GENERAL`, and a boolean
call answers `false` with reason `UNKNOWN`. The answers in the table are successful requests,
which the JS SDK caches like any other.
`provider.hashUser(userId, flagKey)` exposes the cross-SDK MD5 hash as a compatibility utility only.

`provider.client`, the JS SDK client, has typed getters of its own: `getStringValue(flagKey, user,
defaultValue)`, `getNumberValue` and `getJsonValue` take the user before the default, while
OpenFeature's `client.getStringValue(flagKey, defaultValue, context)` takes the default first.
Swapping the user and the default on `provider.client` is a TypeScript error.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json`, `Accept: application/json`.

| Call | Method and path | 200 response |
|---|---|---|
| every `resolve*Evaluation` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…[&context=…]` | `{key, enabled, value, value_type, config, reason}` |
| `provider.client.getAssignment` / `getVariant` | `POST /api/v1/tracking/assign` `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}` |
| `provider.client.track` with a key | `POST /api/v1/tracking/track` | ignored |
| `provider.client.track` without a key | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100), one entry per experiment and flag it reaches; with none, `POST /api/v1/tracking/track` with no key | ignored |
| `provider.client.trackBatch` | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100) | `{success_count, failure_count, errors}` |

Errors: 401 bad key, 404 an unknown experiment or flag key, or a draft experiment with no control variant (never cached), 422 an invalid event or a `context` the server refuses, 429 rate limited.
An experiment that is not ACTIVE is not an error: `getAssignment` resolves to its control variant with `assigned: false`.

## Contract smoke

```bash
cd sdk/openfeature && npm run build --silent && node examples/contract_smoke.mjs
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (`sdk_contract_ab`), `CONTRACT_FLAG_KEY` (`sdk_contract_flag`), `CONTRACT_USER_ID`.

CI runs this SDK's contract smoke against a real API: `openfeature` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

## Development

```bash
npm install
npm test
npm run build
```

- `npm test`: Jest, fetch mocked; builds ../js first
- `npm run build`: tsc → dist/ (CommonJS + .d.ts)
