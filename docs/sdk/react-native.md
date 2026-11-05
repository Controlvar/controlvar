# React Native SDK

`@controlvar/react-native-sdk` (v0.1) provides feature flag evaluation, A/B experiment
assignment and event tracking for React Native apps: a context provider, three hooks and a plain
client, with an in-memory cache and an AsyncStorage-backed offline fallback.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

Flag evaluation and experiment assignment both send the user's attributes, so targeting rules match as in the JavaScript SDK: flag
evaluation sends them as the `context` query parameter (a URL-encoded JSON object; nothing is
sent for missing or empty attributes), and experiment assignment sends them as `context` in the
request body.

Source: `sdk/react-native`. Example app: `sdk/react-native/example/App.tsx`.

CI runs this SDK's contract smoke against a real API: `react-native` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Installation

**Not yet published.** `@controlvar/react-native-sdk` is not on npm, so build it from
source: pack it from a clone of this repository (it ships TypeScript source, so there is no build
step) and install the tarball. The SDK Unit Tests workflow packs it and type-checks it in a
TypeScript React Native app on every pull request that changes it; no CI job runs it on a device.
`npm pack` writes `controlvar-react-native-sdk-0.1.0.tgz`:

```bash
git clone https://github.com/Controlvar/controlvar.git
cd controlvar/sdk/react-native
npm pack
```

Then, in your app, install the tarball:

```bash
npm install /path/to/controlvar/sdk/react-native/controlvar-react-native-sdk-0.1.0.tgz
```

Its peer dependencies are `react` (>= 16.8), `react-native` (>= 0.65) and
`@react-native-async-storage/async-storage` (`>=1.21.0 <4`: majors 1, 2 and 3 are supported).
React Native links a native module only from your app's own dependencies, so AsyncStorage must be
one of them. If your app does not have it yet, add it:

```bash
npm install @react-native-async-storage/async-storage
```

An app that already has AsyncStorage keeps its version. Do not put a version or range of
AsyncStorage on that line (such as `@latest`): npm saves it, which moves an app on 1.x or 2.x to
3.x. Your app's own AsyncStorage already satisfies the peer.

- **iOS**: `cd ios && pod install` (required by `@react-native-async-storage/async-storage`).
- **Android**: no additional setup (minSdk 21).
- `md5` is a dependency of the package, imported when the package loads (its entry point
  re-exports `hashUser`), so it must resolve even if you never call `hashUser`. Its type
  declarations, `@types/md5`, are a dependency too, so an app's `tsc` finds them.

---

## Quick start

```tsx
import { ActivityIndicator, Button, View } from 'react-native';
import {
  ExperimentationProvider,
  ExperimentationClient,
  useFlag,
  useExperiment,
  useExperimentationClient,
} from '@controlvar/react-native-sdk';
// darkStyles, lightStyles, NewCheckout and OldCheckout below are your app's own.

// Create the client once, at app start-up.
const client = new ExperimentationClient({
  apiKey: 'YOUR_API_KEY',
  baseUrl: 'http://localhost:8000', // your backend's origin; the SDK appends /api/v1/...
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
| `attributes` | `Record<string, unknown>` | No | Sent as `context` on flag evaluation and on experiment assignment (targeting rules) |

The context value is memoised on `client`, `userId` and `attributes`. Hooks re-run when `client`,
`userId` or the key they were given changes; neither `useExperiment` nor `useFlag` re-runs when
`attributes` changes (assignment is sticky per user, and an inline object is new on every render).

**After the user's attributes change,** the SDK keys its memory cache, its AsyncStorage entries
and its in-flight requests by user + key, as the JavaScript SDK does, so an answer already stored
for that user and flag is served until it expires, with the attributes it was asked with. To ask
again with the new attributes, call `client.clearCache()` and `await client.clearStorage()`, then
mount the components that read flags again (a new `key` on the provider does it). `clearStorage()`
also removes the stored assignments, so the next `useExperiment` asks again; the variant is the
same because assignment is sticky, and the server records another exposure. With a client
constructed with `offlineFallback: false`, which keeps nothing in AsyncStorage, `clearCache()` plus
a remount is enough; either one alone is not, because a mounted hook does not ask again and a
remount is answered from the memory cache.

### Client configuration (`SdkConfig`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `apiKey` | `string` | — | API key, sent as `X-API-Key` |
| `baseUrl` | `string` | — | Backend origin (`https://api.example.com`); trailing slashes are stripped |
| `timeoutMs` | `number` | `5000` | Per-request timeout (`AbortController`) |
| `cacheTtlMs` | `number` | `300000` | How long a successful evaluation/assignment is reused per user + key (memory and AsyncStorage) |
| `offlineFallback` | `boolean` | `true` | Persist results to AsyncStorage and serve them (even past their TTL) when the request fails, except a flag evaluation that answers 404 (the flag was deleted), which removes the stored answer |
| `onError` | `(error, operation) => void` | — | Called for every swallowed failure; `operation` is `'evaluateFlag' \| 'getAssignment' \| 'getAllFlags' \| 'track' \| 'trackBatch' \| 'exposure'` |
| `onExposure` | `(exposure: Exposure) => void` | — | Called once each time the server enrols the user in an experiment and the answer is not cached; see [Exposure hook](#exposure-hook) |

---

## Hooks

### `useFlag(flagKey, attributes?): FlagState`

Evaluates one flag for the provider's user via
`GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…&context=…`. It sends its `attributes`
argument as `context`; when it has none, the provider's `attributes`. The two are not merged.

| Field | Type | Description |
|-------|------|-------------|
| `key` | `string` | The key you asked for |
| `enabled` | `boolean` | Server decision for this user (`false` while loading or on failure) |
| `value` | `boolean` | Alias of `enabled` (kept from 0.1) |
| `config` | `unknown \| null` | Always `null`: the evaluate endpoint returns `config: null` for every flag |
| `loading` | `boolean` | `true` until the first response |
| `error` | `Error \| null` | Only set if the client promise rejects (the client itself never throws, so this is normally `null` — failures come back as `enabled: false`) |

A component already on screen keeps the answer it rendered until it mounts again; the SDK neither
polls nor streams. See [Turning a Flag Off](../feature-flags/kill-switch.md).

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
| `error` | `Error \| null` | Only set if the client promise rejects; a 404 (no experiment has the key) yields `variant: null` with `error: null`, and an experiment that is not ACTIVE yields its control variant |

### `useExperimentationClient(): { client, userId, attributes }`

Escape hatch to the underlying client, e.g. for imperative tracking or `client.clearCache()`.
`useExperimentationContext` is the same hook. Both throw
`useExperimentationContext must be used inside an <ExperimentationProvider>` outside a provider.

---

## Direct Client API

```typescript
import { ExperimentationClient } from '@controlvar/react-native-sdk';

const client = new ExperimentationClient({ apiKey: 'YOUR_API_KEY', baseUrl: 'http://localhost:8000' });

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
| `evaluateFlag` | `(flagKey, userId, attributes?) => Promise<FlagEvaluation>` | `{ key, enabled, config }` | `{ key, enabled: false, config: null }` (or the last-known AsyncStorage value). Attributes that cannot be sent (`JSON.stringify` throws, or the server answers 422 for a context over 8,192 bytes or nested more than 10 levels) are reported to `onError` and answered the same way |
| `isFeatureEnabled` | `(flagKey, userId, attributes?) => Promise<boolean>` | `enabled` | `false` |
| `getAllFlags` | `(userId, attributes?) => Promise<Record<string, boolean>>` | `{ flagKey: enabled }`; not cached, not part of the fan-out | `{}` |
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

This SDK's `Assignment` does not say whether the user was enrolled: an `assigned: false` answer (a
user the experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as
the control variant, and an event tracked without a key after the user's in-memory cache entries have
expired (`cacheTtlMs`, 5 minutes by default) is not sent, so pass `experimentKey` on a conversion
that can come later.

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
4. **Offline fallback** — on a network error or a non-2xx response other than a flag evaluation's 404,
   the last-known AsyncStorage value is returned even if its TTL has passed (it is *not* written
   back to memory, so the next call retries the server). A flag evaluation that answers 404 means
   the flag was deleted: the stored answer is removed and the flag is answered as off.
5. **Safe default** — `{ key, enabled: false, config: null }` / `null`. Failures are never cached.

Every swallowed failure is passed to `config.onError(error, operation)`; HTTP failures are
`ApiError` instances with a `status` field (401 bad key, 404 an unknown experiment or flag key, or a draft experiment with no
control variant, 422 invalid event, 429 rate limited).

### AsyncStorage layout

Entries are stored as `{ value, expiresAt }` under `ep_sdk_flag:<userId>:<flagKey>` and
`ep_sdk_asgn:<userId>:<experimentKey>` (both parts URL-encoded). Set `offlineFallback: false` to
skip persistence entirely; `client.clearStorage()` removes only these keys.

---

## Exposure hook

`onExposure` is a function you give the client when you create it. The client calls it once each
time the server enrols the user in an experiment and the answer did not come from the client's
memory cache or from AsyncStorage: the moment the server records the exposure. Use it to send the
exposure to your analytics tool or warehouse.

```typescript
const client = new ExperimentationClient({
  apiKey: 'YOUR_API_KEY',
  baseUrl: 'http://localhost:8000',
  onExposure: (exposure) => {
    // your analytics tool's call goes here
    console.log('exposure', exposure.experimentKey, exposure.variantName, exposure.userId);
  },
  onError: (error, operation) => {
    if (operation === 'exposure') console.warn('onExposure failed', error);
  },
});
```

It receives an `Exposure`, a new object each time: `experimentKey`, `userId`, `variantId` (`null`
when the server sends none) and `variantName`. Nothing else: not the user's attributes and not the
variant's configuration. `Exposure` is exported from `@controlvar/react-native-sdk` as a type.

It is not called for:

- an answer from the client's memory cache;
- an answer read from AsyncStorage: an unexpired stored answer (after an app restart, or in a new
  client) or the offline fallback, served when the request fails;
- a user the experiment did not enrol (`assigned: false`, with `reason` `'holdout'`,
  `'mutual_exclusion'` or `'targeting'`), or an experiment that is not running (`'draft'`,
  `'paused'`, `'completed'` or `'archived'`): the server records no exposure for them;
- a call that fails;
- a feature flag.

This SDK's `Assignment` does not say whether the user was enrolled; the hook is not called for a
user who was not. It is not called when the server does not send `assigned`; every Controlvar
server release sends it.

Calls for the same user and experiment that are made while the first is still waiting share its
request, so together they call it once, and so do components that mount `useExperiment` for the
same experiment. A call made after `clearCache()` while a request is still in flight makes a second
request, and the server records a second exposure, but the hook is called once, for the answer
stored first.

With the default `offlineFallback: true`, the answer is kept in AsyncStorage for the cache lifetime
(`cacheTtlMs`, 5 minutes by default), so on one device neither `clearCache()` nor a new client
calls it again until that stored answer expires. It is called again, and the server records
another exposure, after the cache lifetime, and, with `offlineFallback: false` or after
`await clearStorage()`, after `clearCache()` and in every new client. So your tool can receive
several exposures for one user and experiment: count users, not events. It never receives one the
server did not record.

It is called when your code asks for the assignment, whether or not you then show the variant, so
ask for it where the variant is shown.

It runs before `getAssignment` resolves, and before every call waiting on the same request, so
keep it quick: hand the exposure to your analytics library, which sends it later. The client does
not wait for a Promise it returns.

If it throws, or returns a Promise that rejects, the answer is unchanged and `onError` receives the
error with the operation `'exposure'`: what your function threw, or an `Error` wrapping it when it
is not one.

The constructor throws `ExperimentationClient: onExposure must be a function` for any other value;
`null` and `undefined` set none.

In a test, a mocked `/assign` answer must include `"assigned": true`, or the hook is not called.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.
`baseUrl` is the origin only; the SDK appends the paths below.

| SDK call | Method and path | Body / query | 200 response |
|---|---|---|---|
| `getAssignment`, `getVariant`, `useExperiment` | `POST /api/v1/tracking/assign` | `{"experiment_key","user_id","context"?: object}` | `{"experiment_key","user_id","variant_id","variant_name","is_control","configuration","assigned","reason"}` |
| `evaluateFlag`, `isFeatureEnabled`, `useFlag` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<id>&context=<url-encoded JSON>` (no `context` for missing or empty attributes) | — | `{"key","enabled","config"}` |
| `getAllFlags` | `GET /api/v1/feature-flags/user/{user_id}?context=<url-encoded JSON>` (no `context` for missing or empty attributes) | — | `{"<flag_key>": bool, ...}` |
| `track` with a key | `POST /api/v1/tracking/track` | `{"event_type","event_name","user_id","experiment_key"?,"feature_flag_key"?,"value"?,"metadata"?,"timestamp"?}` | stored event (ignored) |
| `track` without a key, `trackBatch` | `POST /api/v1/tracking/batch` | `{"events":[<track body>...]}` (max 100) | `{"success_count","failure_count","errors"}` |

Errors: 401 bad key; 404 an unknown experiment or flag key, or a draft experiment with no control variant; 422 an invalid
event, or a targeting context over 8,192 bytes or 10 levels deep; 429 rate limited (`Retry-After` header).

---

## Hash Algorithm (compatibility utility only)

`hashUser(userId, flagKey)` is still exported so the cross-SDK golden-vector tests
(`tests/sdk-contract/golden-vectors.json`) and custom integrations can verify parity, but
**nothing in the SDK calls it to pick a variant** — the server decides.

```
MD5("{userId}:{flagKey}") → first 4 bytes as little-endian uint32 → ÷ 2^32 (4294967296)
```

```typescript
import { hashUser } from '@controlvar/react-native-sdk';
hashUser('user-123', 'my-flag'); // ≈ 0.6927449859
```

---

## Testing

### Jest setup

React Native's Jest preset does not transform code under `node_modules`, and the package is
TypeScript source, so add it to the preset's `transformIgnorePatterns` in your Jest config:

```json
{
  "preset": "react-native",
  "transformIgnorePatterns": [
    "node_modules/(?!((jest-)?react-native|@react-native(-community)?|@controlvar/react-native-sdk)/)"
  ]
}
```

This repository has an in-memory Jest mock for AsyncStorage,
`sdk/react-native/__mocks__/@react-native-async-storage/async-storage.ts`. The package does not
ship it (its `files` list is `src` only), so copy it into your project and map it in your Jest
config, as the SDK's own Jest config does:

```json
{
  "moduleNameMapper": {
    "@react-native-async-storage/async-storage": "<rootDir>/__mocks__/@react-native-async-storage/async-storage.ts"
  }
}
```

### Mocking the client

Stub the methods with the real return shapes:

```typescript
import type { ExperimentationClient } from '@controlvar/react-native-sdk';

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
import { ExperimentationProvider, useFlag } from '@controlvar/react-native-sdk';

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

`npx jest` runs the client, hook, hash and storage tests, with `fetch` mocked (the storage tests
run against the AsyncStorage 1.x and 3.x shapes). `npx tsc --noEmit` type-checks `src/`, and
`npm run typecheck:tests` type-checks the tests too. The SDK Unit Tests workflow runs all three:

```bash
cd sdk/react-native && npm install
npx jest
npx tsc --noEmit
npm run typecheck:tests
```

The contract smoke, `examples/contract_smoke.test.ts`, runs under Jest in Node, with no device
runtime; the live runner runs it with `python tests/sdk-contract/live/run_live_contract.py --sdk react-native --strict`.
Besides assigning, evaluating and tracking, it evaluates the seed's `sdk_contract_targeted` flag
(0% rollout, one rule: `plan` equals `pro`) with the attributes `{ plan: 'pro' }` and then
`{ plan: 'free' }`, and the runner requires the first on, by that rule, and the second off.

---

## Troubleshooting

**`useExperimentationContext must be used inside an <ExperimentationProvider>`** — wrap the
component in the provider; check for a duplicate copy of React in the bundle.

**Flags always come back disabled / variant is always `null`** — pass an `onError` handler and look
at the `ApiError.status`: 401 means a bad `apiKey`, 404 means an unknown key or a draft experiment with no
control variant (a flag that is not ACTIVE comes back disabled, and an experiment that is not ACTIVE answers its
control variant: neither is an error), a plain `Error` means the network/timeout. Check that `baseUrl` is the origin
only (no `/api/v1`). With `offlineFallback: true` the last-known value is served during outages.
An `ApiError` message gives the request path without its query.

**A flag with an attribute rule is off for a user who matches it** — check that the provider (or
`useFlag`'s second argument) carries the attribute, and that the answer was not cached before the
attributes changed (see "After the user's attributes change" under Provider): the answer stored
for that user and flag is served until it expires, whatever attributes the new call sends. Call
`clearCache()` and `await clearStorage()`, then remount; with a client constructed with
`offlineFallback: false`, `clearCache()` plus a remount is enough.

**Type errors in `node_modules/@controlvar/react-native-sdk/src`** — the package is TypeScript
source, so your app's `tsc` checks it with your app's AsyncStorage. Check that AsyncStorage is a
major the peer range allows (1, 2 or 3), and that `@types/md5`, which comes with the package, is
installed.
