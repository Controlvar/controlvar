# React Native SDK

`@getexperimently/react-native-sdk` (v0.1) provides feature flag evaluation, A/B experiment
assignment and event tracking for React Native apps: a context provider, three hooks and a plain
client, with an in-memory cache and an AsyncStorage-backed offline fallback.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

> **Beta: flag targeting on user attributes does not work in this SDK yet.** Flag
> evaluation sends only the user id (`GET /api/v1/feature-flags/evaluate/{key}?user_id=…`),
> not the user's attributes, so a flag targeting rule on an attribute (app version, OS,
> country, plan) never matches here, and the flag's rollout percentage alone decides. Experiment
> assignment is not affected: `getAssignment` (and `useExperiment`, with the provider's `attributes`) sends the attributes as `context`, so experiment
> targeting works. Until this is fixed ([#438](https://github.com/getexperimently/experimently/issues/438)), an app that needs attribute targeting on a
> flag can call the evaluate endpoint itself with a `context` query parameter (a URL-encoded
> JSON object of the attributes).

Source: `sdk/react-native`. Example app: `sdk/react-native/example/App.tsx`.

CI runs this SDK's contract smoke against a real API: `react-native` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Installation

**Not yet published.** `@getexperimently/react-native-sdk` is not on npm yet, so both the npm
and the Yarn command below fail today. Pack it from a clone of this repository (it ships TypeScript
source, so there is no build step) and install the tarball instead; installing that tarball into a
React Native app has not been tested.

With npm:

```bash
npm install @getexperimently/react-native-sdk @react-native-async-storage/async-storage md5
```

With Yarn:

```bash
yarn add @getexperimently/react-native-sdk @react-native-async-storage/async-storage md5
```

From a clone of this repository instead, `npm pack` writes
`getexperimently-react-native-sdk-0.1.0.tgz`:

```bash
git clone https://github.com/getexperimently/experimently.git
cd experimently/sdk/react-native
npm pack
```

Then, in your app, install the tarball with the two peer dependencies:

```bash
npm install /path/to/experimently/sdk/react-native/getexperimently-react-native-sdk-0.1.0.tgz @react-native-async-storage/async-storage md5
```

- **iOS**: `cd ios && pod install` (required by `@react-native-async-storage/async-storage`).
- **Android**: no additional setup (minSdk 21).
- **Expo**: `npx expo install @react-native-async-storage/async-storage` (Expo SDK 49+).
- `md5` is only needed for the exported `hashUser` compatibility utility.

---

## Quick start

```tsx
import {
  ExperimentationProvider,
  ExperimentationClient,
  useFlag,
  useExperiment,
  useExperimentationClient,
} from '@getexperimently/react-native-sdk';

// Create the client once, at app start-up.
const client = new ExperimentationClient({
  apiKey: 'YOUR_API_KEY',
  baseUrl: 'https://api.getexperimently.com', // origin only; the SDK appends /api/v1/...
});

export default function App() {
  return (
    <ExperimentationProvider client={client} userId="user-123" attributes={{ plan: 'pro', country: 'US' }}>
      <Home />
    </ExperimentationProvider>
  );
}

function Home() {
  const { enabled, config, loading } = useFlag('dark-mode');
  const { variant, configuration } = useExperiment('checkout-experiment');
  const { client, userId } = useExperimentationClient();

  if (loading) return <ActivityIndicator />;
  return (
    <View style={enabled ? darkStyles : lightStyles}>
      {variant === 'treatment' ? <NewCheckout copy={configuration?.cta} /> : <OldCheckout />}
      <Button
        title="Buy"
        onPress={() => client.track('purchase', userId, { sku: 'A1' }, { value: 49.99, experimentKey: 'checkout-experiment' })}
      />
    </View>
  );
}
```

---

## Provider

```tsx
<ExperimentationProvider client={client} userId="user-123" attributes={{ plan: 'pro' }}>
  {children}
</ExperimentationProvider>
```

| Prop | Type | Required | Description |
|------|------|----------|-------------|
| `client` | `ExperimentationClient` | Yes | The client instance (create it once) |
| `userId` | `string` | Yes | Stable identifier used by the server for bucketing |
| `attributes` | `Record<string, unknown>` | No | Sent as `context` on experiment assignment (targeting rules); not sent on flag evaluation |

The context value is memoised on `client`, `userId` and `attributes`. Hooks re-run when `client`,
`userId` or the key they were given changes; `useExperiment` deliberately ignores `attributes`
changes because assignment is sticky per user.

### Client configuration (`SdkConfig`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `apiKey` | `string` | — | API key, sent as `X-API-Key` |
| `baseUrl` | `string` | — | Backend origin (`https://api.example.com`); trailing slashes are stripped |
| `timeoutMs` | `number` | `5000` | Per-request timeout (`AbortController`) |
| `cacheTtlMs` | `number` | `300000` | How long a successful evaluation/assignment is reused per user + key (memory and AsyncStorage) |
| `offlineFallback` | `boolean` | `true` | Persist results to AsyncStorage and serve them (even past their TTL) when the API is unreachable |
| `onError` | `(error, operation) => void` | — | Called for every swallowed failure; `operation` is `'evaluateFlag' \| 'getAssignment' \| 'getAllFlags' \| 'track' \| 'trackBatch'` |

---

## Hooks

### `useFlag(flagKey): FlagState`

Evaluates one flag for the provider's user via
`GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…`. A second `attributes` argument is
accepted for signature compatibility but not sent.

| Field | Type | Description |
|-------|------|-------------|
| `key` | `string` | The key you asked for |
| `enabled` | `boolean` | Server decision for this user (`false` while loading or on failure) |
| `value` | `boolean` | Alias of `enabled` (kept from 0.1) |
| `config` | `unknown \| null` | Always `null`: the evaluate endpoint returns `config: null` for every flag |
| `loading` | `boolean` | `true` until the first response |
| `error` | `Error \| null` | Only set if the client promise rejects (the client itself never throws, so this is normally `null` — failures come back as `enabled: false`) |

### `useExperiment(experimentKey): ExperimentState`

Assigns the provider's user via `POST /api/v1/tracking/assign` (sticky on the server; the
provider's `attributes` are sent as `context`) and returns the variant plus its configuration.

| Field | Type | Description |
|-------|------|-------------|
| `experimentKey` | `string` | The key you asked for |
| `variant` | `string \| null` | Assigned variant name; `null` while loading or when assignment failed |
| `variantName` | `string \| null` | Same value as `variant` |
| `variantId` | `string \| null` | Variant UUID, `null` until assigned |
| `isControl` | `boolean` | `true` for the control variant (`false` by default) |
| `configuration` | `Record<string, unknown> \| null` | The variant's `configuration` JSON from the experiment definition |
| `loading` | `boolean` | `true` until the first response |
| `error` | `Error \| null` | Only set if the client promise rejects; a 404 (experiment not ACTIVE) yields `variant: null` with `error: null` |

### `useExperimentationClient(): { client, userId, attributes }`

Escape hatch to the underlying client, e.g. for imperative tracking or `client.clearCache()`.
`useExperimentationContext` is the same hook. Both throw
`useExperimentationContext must be used inside an <ExperimentationProvider>` outside a provider.

---

## Direct Client API

```typescript
import { ExperimentationClient } from '@getexperimently/react-native-sdk';

const client = new ExperimentationClient({ apiKey: 'YOUR_API_KEY', baseUrl: 'https://api.getexperimently.com' });

const { key, enabled, config } = await client.evaluateFlag('dark-mode', 'user-123');
const on = await client.isFeatureEnabled('dark-mode', 'user-123');          // boolean
const all = await client.getAllFlags('user-123');                            // { [flagKey]: boolean }

const assignment = await client.getAssignment('checkout-experiment', 'user-123', { plan: 'pro' });
// { experimentKey, userId, variantId, variantName, isControl, configuration } | null
const variant = await client.getVariant('checkout-experiment', 'user-123'); // string | null

await client.track('purchase', 'user-123', { sku: 'A1' }, { value: 49.99, experimentKey: 'checkout-experiment' });
await client.track('page_view', 'user-123', { screen: 'home' });             // no key → fan-out (see below)
const result = await client.trackBatch([
  { eventName: 'purchase', userId: 'user-123', value: 12.5, experimentKey: 'checkout-experiment' },
  { eventName: 'page_view', userId: 'user-123' },
]);
```

| Method | Signature | Returns | On failure |
|--------|-----------|---------|------------|
| `evaluateFlag` | `(flagKey, userId) => Promise<FlagEvaluation>` | `{ key, enabled, config }` | `{ key, enabled: false, config: null }` (or the last-known AsyncStorage value) |
| `isFeatureEnabled` | `(flagKey, userId) => Promise<boolean>` | `enabled` | `false` |
| `getAllFlags` | `(userId) => Promise<Record<string, boolean>>` | `{ flagKey: enabled }`; not cached, not part of the fan-out | `{}` |
| `getAssignment` | `(experimentKey, userId, attributes?) => Promise<Assignment \| null>` | `{ experimentKey, userId, variantId, variantName, isControl, configuration }` | `null` (or the last-known AsyncStorage value) |
| `getVariant` | `(experimentKey, userId, attributes?) => Promise<string \| null>` | `variantName` | `null` |
| `track` | `(eventName, userId, properties?, options?) => Promise<void>` | — | Never rejects; reported to `onError` |
| `trackBatch` | `(events: TrackEvent[]) => Promise<BatchResult>` | `{ successCount, failureCount, errors }` | Never rejects; a failed chunk counts all its events as failures |
| `getAssignments` | `(userId) => Assignment[]` | Cached, unexpired assignments | — |
| `getEvaluatedFlags` | `(userId) => string[]` | Keys of cached, successfully evaluated flags | — |
| `clearCache` | `() => void` | Drops the in-memory caches (AsyncStorage untouched) | — |
| `clearStorage` | `() => Promise<void>` | Removes every `ep_sdk_*` key from AsyncStorage | — |

`TrackOptions`: `value?` (number), `experimentKey?`, `featureFlagKey?`, `eventType?` (defaults
to the event name), `timestamp?` (`Date` or string, sent as ISO-8601). `properties` is sent as
`metadata`. `TrackEvent` = `TrackOptions & { eventName, userId, properties? }`.

**Fan-out rule.** With `experimentKey` and/or `featureFlagKey` the SDK sends one
`POST /api/v1/tracking/track`. Without a key it sends `POST /api/v1/tracking/batch` (chunked at
100) with one entry per experiment the user has been assigned to in this client (`experiment_key`)
plus one per flag evaluated for the user (`feature_flag_key`), both taken from the in-memory cache.
If nothing is cached, nothing is sent. `trackBatch` applies the same expansion to keyless entries.

Concurrent calls for the same user + key share one in-flight request, so mounting several
components that use the same experiment produces one assignment (and one exposure event).

---

## Evaluation Order

For `evaluateFlag` and `getAssignment`:

1. **In-memory cache** — returns the cached result if it has not expired (`cacheTtlMs`).
2. **AsyncStorage** (if `offlineFallback`) — an unexpired persisted entry is served without a
   network call (e.g. right after an app restart) and promoted to memory.
3. **Server** — `GET /api/v1/feature-flags/evaluate/…` / `POST /api/v1/tracking/assign`; the
   result is cached in memory and, if `offlineFallback`, persisted with its expiry.
4. **Offline fallback** — on a network error or non-2xx response, the last-known AsyncStorage
   value is returned even if its TTL has passed (it is *not* written back to memory, so the next
   call retries the server).
5. **Safe default** — `{ key, enabled: false, config: null }` / `null`. Failures are never cached.

Every swallowed failure is passed to `config.onError(error, operation)`; HTTP failures are
`ApiError` instances with a `status` field (401 bad key, 404 an experiment unknown or not ACTIVE, or an unknown flag key, 422
invalid event, 429 rate limited).

### AsyncStorage layout

Entries are stored as `{ value, expiresAt }` under `ep_sdk_flag:<userId>:<flagKey>` and
`ep_sdk_asgn:<userId>:<experimentKey>` (both parts URL-encoded). Set `offlineFallback: false` to
skip persistence entirely; `client.clearStorage()` removes only these keys.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.
`baseUrl` is the origin only; the SDK appends the paths below.

| SDK call | Method and path | Body / query | 200 response |
|---|---|---|---|
| `getAssignment`, `getVariant`, `useExperiment` | `POST /api/v1/tracking/assign` | `{"experiment_key","user_id","context"?: object}` | `{"experiment_key","user_id","variant_id","variant_name","is_control","configuration"}` |
| `evaluateFlag`, `isFeatureEnabled`, `useFlag` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<id>` | — | `{"key","enabled","config"}` |
| `getAllFlags` | `GET /api/v1/feature-flags/user/{user_id}` | — | `{"<flag_key>": bool, ...}` |
| `track` with a key | `POST /api/v1/tracking/track` | `{"event_type","event_name","user_id","experiment_key"?,"feature_flag_key"?,"value"?,"metadata"?,"timestamp"?}` | stored event (ignored) |
| `track` without a key, `trackBatch` | `POST /api/v1/tracking/batch` | `{"events":[<track body>...]}` (max 100) | `{"success_count","failure_count","errors"}` |

Errors: 401 bad key; 404 an experiment unknown or not ACTIVE, or an unknown flag key; 422 invalid event; 429
rate limited (`Retry-After` header).

---

## Hash Algorithm (compatibility utility only)

`hashUser(userId, flagKey)` is still exported so the cross-SDK golden-vector tests
(`tests/sdk-contract/golden-vectors.json`) and custom integrations can verify parity, but
**nothing in the SDK calls it to pick a variant** — the server decides.

```
MD5("{userId}:{flagKey}") → first 4 bytes as little-endian uint32 → ÷ 2^32 (4294967296)
```

```typescript
import { hashUser } from '@getexperimently/react-native-sdk';
hashUser('user-123', 'my-flag'); // ≈ 0.6927449859
```

---

## Testing

### Jest setup

The SDK ships an in-memory Jest mock for AsyncStorage. Map it in your Jest config (or copy it to
your own `__mocks__`):

```json
{
  "moduleNameMapper": {
    "@react-native-async-storage/async-storage": "<rootDir>/node_modules/@getexperimently/react-native-sdk/__mocks__/@react-native-async-storage/async-storage"
  }
}
```

### Mocking the client

Stub the methods with the real return shapes:

```typescript
import type { ExperimentationClient } from '@getexperimently/react-native-sdk';

const mockClient = {
  evaluateFlag: jest.fn().mockResolvedValue({ key: 'my-flag', enabled: true, config: null }),
  getAssignment: jest.fn().mockResolvedValue({
    experimentKey: 'exp', userId: 'u1', variantId: 'v2', variantName: 'treatment', isControl: false, configuration: null,
  }),
  track: jest.fn().mockResolvedValue(undefined),
  clearCache: jest.fn(),
} as unknown as ExperimentationClient;
```

### Testing hooks

```tsx
import { render, waitFor } from '@testing-library/react-native';
import { ExperimentationProvider, useFlag } from '@getexperimently/react-native-sdk';

function Probe() {
  const { enabled, loading } = useFlag('my-flag');
  if (loading) return <Text>Loading</Text>;
  return <Text testID="result">{enabled ? 'on' : 'off'}</Text>;
}

test('useFlag reflects the server decision', async () => {
  const { getByTestId } = render(
    <ExperimentationProvider client={mockClient} userId="u1">
      <Probe />
    </ExperimentationProvider>
  );
  await waitFor(() => expect(getByTestId('result').props.children).toBe('on'));
});
```

### Running the SDK's own tests

`npx jest` runs the client, hook and hash tests, with `fetch` mocked. `npx tsc --noEmit`
type-checks `src/`, and `npm run typecheck:tests` type-checks the tests too:

```bash
cd sdk/react-native && npm install
npx jest
npx tsc --noEmit
npm run typecheck:tests
```

The contract smoke, `examples/contract_smoke.test.ts`, runs under Jest in Node, with no device
runtime; the live runner runs it with `python tests/sdk-contract/live/run_live_contract.py --sdk react-native --strict`.

---

## Troubleshooting

**`useExperimentationContext must be used inside an <ExperimentationProvider>`** — wrap the
component in the provider; check for a duplicate copy of React in the bundle.

**Flags always come back disabled / variant is always `null`** — pass an `onError` handler and look
at the `ApiError.status`: 401 means a bad `apiKey`, 404 means an unknown key or an experiment that is not
ACTIVE (a flag that is not ACTIVE is not an error: it comes back disabled), a plain `Error` means the network/timeout. Check that `baseUrl` is the origin
only (no `/api/v1`). With `offlineFallback: true` the last-known value is served during outages.

**Type errors with `md5`** — `npm install --save-dev @types/md5`.
