# @controlvar/edge-sdk

SDK for edge runtimes, with adapters for Cloudflare Workers, Vercel Edge Middleware and Deno Deploy;
declares Node >= 18. Zero runtime dependencies, no Node.js built-ins. Its tests and contract smoke
run it under Node, with the adapters' KV and request objects stubbed.

Flag evaluation and experiment assignment are **decided by the server**; results are cached per
user + key in memory (and optionally in Cloudflare KV / Deno KV; a flag call with attributes is not
stored; see Attributes on flag evaluation). Nothing is bucketed locally.

Full documentation: [`docs/sdk/edge.md`](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/edge.md).

## Installation

**Not yet published.** `@controlvar/edge-sdk` is not on npm, so build it from source in a
clone of this repository and install the packed tarball in your app:

```bash
cd sdk/edge
npm ci && npm run build && npm pack
```

`npm pack` writes `controlvar-edge-sdk-0.2.0.tgz`; `npm install <path to that file>` adds it.

## Quick start

```ts
import { EdgeExperimentationClient } from '@controlvar/edge-sdk';

const client = new EdgeExperimentationClient({ apiKey: env.EP_API_KEY, baseUrl: 'http://localhost:8000' });

const assignment = await client.getAssignment('checkout_flow', userId, { country: 'US' }); // null on failure
const { enabled, config } = await client.evaluateFlag('new-checkout', userId, { plan: 'pro' });
await client.track('purchase', userId, { sku: 'A1' }, { value: 49.99, experimentKey: 'checkout_flow' });
await client.track('page_view', userId); // no key → fans out to every cached assignment + flag
```

Adapters: `@controlvar/edge-sdk/cloudflare` (`withExperimentation`, KV store),
`/vercel` (`createEdgeMiddleware`), `/deno` (`createDenoHandler`, Deno KV store).

## Attributes on flag evaluation

Flag evaluation sends the user's attributes as `context`, so a flag's targeting rules match against
them. `evaluateFlag`, `isFeatureEnabled` and `getAllFlags` take them as an optional last argument.
The flag calls are
`GET /api/v1/feature-flags/evaluate/{key}?user_id=…&context=…` and
`GET /api/v1/feature-flags/user/{user_id}?context=…`, where `context` is the attributes as compact
JSON, percent-encoded: `{ plan: 'pro' }` travels as `context=%7B%22plan%22%3A%22pro%22%7D`. With no
attributes or an empty object, no `context` is sent and the request carries the user id alone.
If the attributes cannot be written as JSON (a BigInt, or an object that refers to itself), nothing
is sent, the call returns the disabled answer (`{}` for `getAllFlags`), and nothing is
logged.

The SDK keeps one cached answer per user and flag, together with the attributes it was asked with.
A later call is answered from the cache only if its attributes are exactly the same; otherwise the
SDK asks the server, and the new answer replaces the cached one. So two calls that differ only in
attributes get two answers. The same attributes written in a different key order may cost one extra
request; they never return another call's answer. Calls with attributes neither read nor write the
shared store; only calls without attributes use it. `evaluateFlagSync` and `getCachedFlag` return the
last answer cached for this user and flag, whatever attributes it was asked with; call `evaluateFlag`
with the attributes when the answer must match them.

The change is in this repository ([#1720](https://github.com/Controlvar/controlvar/issues/1720)); it
reaches a registry at this SDK's first publish, so a build from a clone of `main` has it and a copy
installed earlier does not.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json`, `Accept: application/json`.

| SDK call | Method and path | 200 response |
|---|---|---|
| `evaluateFlag`, `isFeatureEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=…` (`context`: the attributes as JSON, left out when there are none) | `{key, enabled, config}` |
| `getAllFlags` | `GET /api/v1/feature-flags/user/{user_id}?context=…` (same) | `{"<flag_key>": bool}` |
| `getAssignment`, `getVariant` | `POST /api/v1/tracking/assign` `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}` |
| `track` with a key | `POST /api/v1/tracking/track` | ignored |
| `track` without keys | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100), one entry per cached assignment and evaluated flag; with nothing cached, nothing is sent | ignored |
| `trackBatch` | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100) | `{success_count, failure_count, errors}` |

Errors: 401 bad key, 404 an unknown experiment or flag key, or a draft experiment with no control variant (→ disabled / `null`, never cached), 429 rate limited.
An experiment that is not ACTIVE is not an error: it answers its control variant.

This SDK reads only the variant from an answer: an `assigned: false` answer (a user the
experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as the
control variant, and an event tracked without a key after the user's in-memory cache entries have
expired (`cacheTtlMs`, 60 s by default) is not sent, so pass `experimentKey` on a conversion that
can come later.

## Contract smoke

```bash
cd sdk/edge && npm run build --silent && node examples/contract_smoke.mjs
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY`,
`CONTRACT_EXPERIMENT_KEY` (`sdk_contract_ab`), `CONTRACT_FLAG_KEY` (`sdk_contract_flag`), `CONTRACT_USER_ID`.

CI runs this SDK's contract smoke against a real API: `edge` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

## Development

```bash
npm install
npm test
npm run build
```

- `npm test`: Jest, fetch mocked
- `npm run build`: ESM + .d.ts → dist/
