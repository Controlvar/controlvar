# @getexperimently/openfeature-provider

**0.x beta.** The API may change between 0.x minor versions; pin the exact version
you test against.

[OpenFeature](https://openfeature.dev) provider for Experimently
(`@openfeature/server-sdk`, Node >= 18). Every evaluation is delegated to
[`@getexperimently/js-sdk`](https://github.com/getexperimently/experimently/blob/main/sdk/js/README.md) and **decided by the server**; successful
answers are cached per user + flag. Nothing is bucketed locally and no flag definitions are downloaded.

Full documentation: [`docs/sdk/openfeature.md`](https://github.com/getexperimently/experimently/blob/main/docs/sdk/openfeature.md).

## Install

**Not yet published.** `@getexperimently/openfeature-provider` is not on npm yet, so this fails
today (`@getexperimently/js-sdk` and `@openfeature/server-sdk` are on npm):

```bash
npm install @openfeature/server-sdk @getexperimently/openfeature-provider
```

Build it from a clone of this repository instead (the provider's build also builds `sdk/js`):

```bash
git clone https://github.com/getexperimently/experimently.git
npm --prefix experimently/sdk/js ci
npm --prefix experimently/sdk/openfeature ci
npm --prefix experimently/sdk/openfeature run build
```

`@getexperimently/js-sdk` is installed with it; `@openfeature/server-sdk` is a peer dependency your app provides.

## Quick start

```ts
import { OpenFeature } from '@openfeature/server-sdk';
import { ExperimentationProvider } from '@getexperimently/openfeature-provider';

const provider = new ExperimentationProvider({ apiKey: 'eptk_...', baseUrl: 'http://localhost:8000' });
await OpenFeature.setProviderAndWait(provider);

const client = OpenFeature.getClient();
const ctx = { targetingKey: 'user-123' };                       // targetingKey → user_id (required)
const enabled = await client.getBooleanValue('dark-mode', false, ctx);        // → enabled
// Only boolean flags carry a value: string, number and object calls return your default.

// Experiments and tracking are not OpenFeature concepts: use the JS SDK client underneath.
const assignment = await provider.client.getAssignment('checkout_flow', { userId: 'user-123', attributes: { plan: 'pro' } });
await provider.client.track('user-123', 'purchase', { value: 49.99, experimentKey: 'checkout_flow' });
await provider.client.track('user-123', 'page_view'); // no key → fans out to cached assignments + flags
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
nothing is cached. Context attributes are **not** sent: the evaluate endpoint takes a `context`, which this provider
does not send yet.
`provider.hashUser(userId, flagKey)` exposes the cross-SDK MD5 hash as a compatibility utility only.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json`, `Accept: application/json`.

| Call | Method and path | 200 response |
|---|---|---|
| every `resolve*Evaluation` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | `{key, enabled, config}` |
| `provider.client.getAssignment` / `getVariant` | `POST /api/v1/tracking/assign` `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}` |
| `provider.client.track` with a key | `POST /api/v1/tracking/track` | ignored |
| `provider.client.track` without a key, `trackBatch` | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100) | `{success_count, failure_count, errors}` |

Errors: 401 bad key, 404 an experiment unknown or not ACTIVE, or an unknown flag key (never cached), 422 invalid event, 429 rate limited.

## Contract smoke

```bash
cd sdk/openfeature && npm run build --silent && node examples/contract_smoke.mjs
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (`sdk_contract_ab`), `CONTRACT_FLAG_KEY` (`sdk_contract_flag`), `CONTRACT_USER_ID`.

CI runs this SDK's contract smoke against a real API: `openfeature` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

## Development

```bash
npm install
npm test
npm run build
```

- `npm test`: Jest, fetch mocked; builds ../js first
- `npm run build`: tsc → dist/ (CommonJS + .d.ts)
