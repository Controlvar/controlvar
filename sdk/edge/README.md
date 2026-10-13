# @getexperimently/edge-sdk

Edge-compatible SDK for Cloudflare Workers, Vercel Edge Functions, Deno Deploy, Node >= 18 and any
WinterCG runtime. Zero runtime dependencies, no Node.js built-ins.

Flag evaluation and experiment assignment are **decided by the server**; results are cached per
user + key in memory (and optionally in Cloudflare KV / Deno KV). Nothing is bucketed locally.

Full documentation: [`docs/sdk/edge.md`](../../docs/sdk/edge.md).

## Quick start

```ts
import { EdgeExperimentationClient } from '@getexperimently/edge-sdk';

const client = new EdgeExperimentationClient({ apiKey: env.EP_API_KEY, baseUrl: 'http://localhost:8000' });

const assignment = await client.getAssignment('checkout_flow', userId, { country: 'US' }); // null on failure
const { enabled, config } = await client.evaluateFlag('new-checkout', userId);
await client.track('purchase', userId, { sku: 'A1' }, { value: 49.99, experimentKey: 'checkout_flow' });
await client.track('page_view', userId); // no key → fans out to every cached assignment + flag
```

Adapters: `@getexperimently/edge-sdk/cloudflare` (`withExperimentation`, KV store),
`/vercel` (`createEdgeMiddleware`), `/deno` (`createDenoHandler`, Deno KV store).

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json`, `Accept: application/json`.

| SDK call | Method and path | 200 response |
|---|---|---|
| `evaluateFlag`, `isFeatureEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | `{key, enabled, config}` |
| `getAllFlags` | `GET /api/v1/feature-flags/user/{user_id}` | `{"<flag_key>": bool}` |
| `getAssignment`, `getVariant` | `POST /api/v1/tracking/assign` `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}` |
| `track` with a key | `POST /api/v1/tracking/track` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100) | `{success_count, failure_count, errors}` |

Errors: 401 bad key, 404 an experiment unknown or not ACTIVE, or an unknown flag key (→ disabled / `null`, never cached), 429 rate limited.

## Contract smoke

```bash
cd sdk/edge && npm run build --silent && node examples/contract_smoke.mjs
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY`,
`CONTRACT_EXPERIMENT_KEY` (`sdk_contract_ab`), `CONTRACT_FLAG_KEY` (`sdk_contract_flag`), `CONTRACT_USER_ID`.

CI runs this SDK's contract smoke against a real API: `edge` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

## Development

```bash
npm install
npm test
npm run build
```

- `npm test`: Jest, fetch mocked
- `npm run build`: ESM + .d.ts → dist/
