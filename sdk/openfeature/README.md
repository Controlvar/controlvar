# @controlvar/openfeature-provider

**Beta.** This is a 0.x release: pin the exact version you test against.

[OpenFeature](https://openfeature.dev) provider for Controlvar
(`@openfeature/server-sdk`, Node >= 18). Every evaluation is delegated to
[`@controlvar/js-sdk`](https://github.com/Controlvar/controlvar/blob/main/sdk/js/README.md) and **decided by the server**; successful
answers are cached per user + flag. Nothing is bucketed locally and no flag definitions are downloaded.

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
const ctx = { targetingKey: 'user-123' };                       // targetingKey → user_id (required)
const enabled = await client.getBooleanValue('dark-mode', false, ctx);        // → enabled
// Only boolean flags carry a value: string, number and object calls return your default.

// Experiments and tracking are not OpenFeature concepts: use the JS SDK client underneath.
const assignment = await provider.client.getAssignment('checkout_flow', { userId: 'user-123', attributes: { plan: 'pro' } });
await provider.client.track('user-123', 'purchase', { value: 49.99, experimentKey: 'checkout_flow' });
await provider.client.track('user-123', 'page_view'); // no key → fanned out by the JS SDK's rule
```

## Options

`apiKey` (required unless `client` is given), `baseUrl` (`http://localhost:8000`), `cacheTtlMs`
(300000), `timeout` (5000 ms), `fetch`, `client` (reuse an existing JS SDK client and its cache).

## Resolution

| Call | Value | Reason |
|---|---|---|
| boolean | `enabled` | `TARGETING_MATCH` / `CACHED`, or `DISABLED` |
| string, number, object | the caller's default | `DEFAULT`, or `DISABLED` when the flag is off |

Only boolean flags carry a value: the evaluate endpoint returns `config: null` for every flag.

Errors never throw: 404 → `FLAG_NOT_FOUND`, bad body → `PARSE_ERROR`, missing `targetingKey` →
`TARGETING_KEY_MISSING`, anything else → `GENERAL`; the default is returned with reason `ERROR` and
nothing is cached. Context attributes are **not** sent: the evaluate call carries `user_id` alone,
so the server evaluates the flag's targeting rules against an empty context.
`provider.hashUser(userId, flagKey)` exposes the cross-SDK MD5 hash as a compatibility utility only.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json`, `Accept: application/json`.

| Call | Method and path | 200 response |
|---|---|---|
| every `resolve*Evaluation` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | `{key, enabled, config}` |
| `provider.client.getAssignment` / `getVariant` | `POST /api/v1/tracking/assign` `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}` |
| `provider.client.track` with a key | `POST /api/v1/tracking/track` | ignored |
| `provider.client.track` without a key, `trackBatch` | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100) | `{success_count, failure_count, errors}` |

Errors: 401 bad key, 404 an unknown experiment or flag key, or a draft experiment with no control variant (never cached), 422 invalid event, 429 rate limited.
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
