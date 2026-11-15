# @controlvar/react-sdk

**Beta.** This is a 0.x release: pin the exact version you test against.

React hooks and clients for the Controlvar public API. Feature flags and
experiment assignments are decided **by the server** (sticky per user); the SDK caches the
answers per user + key and never buckets locally.

Full documentation: [the React SDK page](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/react.md).

## Install

To install it, follow [the React SDK page](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/react.md#installation).

Its peer dependencies are `react` >= 17 and `react-dom` >= 17; your app provides them.

## Provider setup

```tsx
import { ExperimentationProvider } from '@controlvar/react-sdk';

<ExperimentationProvider
  config={{ apiKey: process.env.NEXT_PUBLIC_CONTROLVAR_API_KEY!, baseUrl: 'http://localhost:8000' }}
  user={{ userId: visitorId, attributes: { device: 'mobile', country: 'US', returning: false } }}
>
  <App />
</ExperimentationProvider>
```

`SdkConfig`: `apiKey`, `baseUrl`, optional `timeoutMs` (default 5000), `cacheTtlMs` (default 300 000)
and `onExposure` (see Exposure hook below).
`user.attributes` is sent as `context` on experiment assignment **and** on flag evaluation
(`&context=<url-encoded JSON>`, omitted when empty), so flag targeting rules evaluate against it
(`country` also matches `user.country`; nested objects flatten to dotted keys such as `app.version`).
Attributes are assumed stable per user — caches are keyed by user + key, so call
`client.clearCache()` (from `useExperimentation()`) or re-mount the provider after changing them.

## Hooks

### `useFeatureFlag(flagKey): FeatureFlagEvaluation`

```tsx
const { isEnabled, variant, config, loading, error } = useFeatureFlag('shoplab_new_search');
if (loading) return <Spinner />;
return isEnabled ? <NewSearch /> : <LegacySearch />;
```

`variant` is `null` when off and `'on'` otherwise, and `config` is always `null`: the evaluate
endpoint returns `config: null` for every flag. On error `error` is set and the flag is reported off,
or, if it started from the Provider's `bootstrap`, keeps that answer. `reason`
(`'targeting_rule' | 'rollout' | 'inactive' | 'error'`) says why the server decided; it is
`undefined` while loading, on error, or when the server does not send one.

### `useVariant(flagKey): string | null` — just the variant string (or `null` while loading / off / error).

### `useMultipleFlags(flagKeys): Record<string, FeatureFlagEvaluation | null>`

```tsx
const flags = useMultipleFlags(['shoplab_new_search', 'shoplab_free_shipping_banner']);
const showBanner = flags['shoplab_free_shipping_banner']?.isEnabled ?? false;  // null while loading
```

### `useExperiment(experimentKey): ExperimentAssignment`

```tsx
const { variantKey, configuration, isControl, loading } = useExperiment('shoplab_hero_banner');
const headline = (configuration?.headline as string) ?? 'Gear up for the season';
```

While loading or on error you get the control defaults:
`variantKey: 'control', variantName: 'Control', variantId: null, isControl: true, configuration: null`.
With a `bootstrap` that has the experiment, it starts from that assignment instead, and keeps it,
with `error` set, if its request fails.

A resolved assignment also carries `assigned` and `reason`. `assigned: false` means the server
did not enrol the user — `reason` is `'holdout'`, `'mutual_exclusion'` or `'targeting'` — and
returned the experiment's control variant so you render the default experience (no exposure is
recorded). Servers that predate the field are reported as `assigned: true` with no `reason`.
**When you export exposures to a warehouse, log only assignments with `assigned: true`**; the
others were never in the experiment.

An experiment that is not ACTIVE answers every user with its control variant, `assigned: false` and
`reason` set to its status (`'draft'`, `'paused'`, `'completed'` or `'archived'`).

### `useTrackEvent(): (eventName, properties?, options?) => void`

```tsx
const track = useTrackEvent();
track('add_to_cart', { product_id, quantity: 1 }, { experimentKey: 'shoplab_pdp_buy_button', value: 49 });
track('search', { query, results: 12 }, { featureFlagKey: 'shoplab_new_search' });
track('page_view', { page: '/products' });           // no key -> fanned out, see below
```

`TrackEventOptions`: `experimentKey?`, `featureFlagKey?`, `value?`, `eventType?` (defaults to
`eventName`), `timestamp?` (Date, sent as ISO-8601). Tracking is fire-and-forget and never throws.

### `useExperimentation(): { client, user }`

Escape hatch to the underlying `ExperimentationClient`, e.g. `client.getAssignments(user.userId)`
for a debug overlay. `useExperimentationContext` is an alias.

### `withExperimentation(Component, flagKey)`

HOC that injects `flagEvaluation: FeatureFlagEvaluation` into `Component`.

## Tracking fan-out rule

`trackEvent` (and `useTrackEvent`) sends:

- **with** `experimentKey` and/or `featureFlagKey` → one `POST /api/v1/tracking/track` with
  `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata, timestamp?}`;
- **without** keys → one `POST /api/v1/tracking/batch` containing one entry per experiment
  (`experiment_key`) **plus** one per flag (`feature_flag_key`) the client answered for the user in
  the last 24 hours, whether or not the answer is still cached. An experiment whose latest answer
  has `reason` `'draft'`, `'paused'`, `'completed'` or `'archived'` gets no entry; `assigned: false`
  answers for `'holdout'`, `'mutual_exclusion'` or `'targeting'` keep theirs;
- **without** keys and with nothing to attribute it to → one `POST /api/v1/tracking/track` with no
  key, which the server stores as history (it counts in holdout results and as CUPED history, never
  in an experiment's results).

The keys live in the client's memory, for up to 10,000 users; `clearCache()` keeps them. A page
reload starts with none, and the page's hooks record them again as they answer.

Pass `experimentKey` for an event that is the outcome of one experiment's surface (a click in the
sorted list). Leave it out for an outcome of the whole visit (a purchase), which counts for every
experiment the user is in.

## Clients

`ExperimentationClient` (browser) — `evaluateFeatureFlag(user, key)`, `evaluateFeatureFlagDetailed`,
`assignExperiment(user, key)`, `trackEvent(userId, name, props?, options?)`, `getAssignments(userId)`,
`getEvaluatedFlags(userId)`, `clearCache()`. Evaluate/assign throw on failure (the hooks turn that into
`error`); concurrent calls for the same user + key share one request; failures are never cached.

`ServerClient` (Node/SSR) — same requests, `(key, user)` argument order, **never throws**:

```ts
import { ServerClient } from '@controlvar/react-sdk';

export async function getServerSideProps({ req }) {
  const client = new ServerClient({ apiKey: process.env.CONTROLVAR_API_KEY!, baseUrl: process.env.CONTROLVAR_API_URL! });
  const user = { userId: req.cookies.visitor_id };
  const flags = await client.getAll(['shoplab_new_search', 'shoplab_free_shipping_banner'], user);
  const hero = await client.assignExperiment('shoplab_hero_banner', user);  // control defaults + error on failure
  return { props: { flags, hero } };
}
```

`ServerClient.bootstrap(keys, user)` returns the answers in the form the Provider's `bootstrap`
takes (see Bootstrap below).

## Bootstrap

Pass the Provider a `bootstrap`, the answers the server already has for the page's user, and the
page's first render shows them with no request instead of every flag off and the control variant:

```tsx
// pages/checkout.tsx
export const getServerSideProps = async ({ req, res }) => {
  const client = new ServerClient({ apiKey: process.env.CONTROLVAR_API_KEY!, baseUrl: process.env.CONTROLVAR_API_URL! });
  let visitorId = req.cookies.visitor_id;
  if (!visitorId) {  // a first visit has no cookie yet: give the visitor an id
    visitorId = crypto.randomUUID();
    res.setHeader('Set-Cookie', `visitor_id=${visitorId}; Path=/; Max-Age=31536000; SameSite=Lax`);
  }
  const bootstrap = await client.bootstrap({ flags: ['new_search'], experiments: ['checkout_flow'] }, { userId: visitorId });
  return { props: { visitorId, bootstrap } };  // a prop, never a <script> tag of your own
};

export default function CheckoutPage({ visitorId, bootstrap }) {
  return (
    <ExperimentationProvider config={{ apiKey, baseUrl }} user={{ userId: visitorId }} bootstrap={bootstrap}>
      <Checkout />
    </ExperimentationProvider>
  );
}
```

Each hook whose key the bootstrap has starts from its answer (`loading: false`), then asks the API
as it always does and shows a changed answer. If that request fails, it keeps the bootstrapped
answer, with `error` set. `bootstrap()` never throws and leaves out a key it could not get; each
experiment it names records an exposure on the server, and the hook's request after hydration
records another, so name only the experiments the page renders. A bootstrap for another user, or
one that is not what `bootstrap()` returns, is ignored, with one warning in the browser console.

Full details, with the App Router: [Start from the server's answers](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/react.md#start-from-the-servers-answers-bootstrap).

## Exposure hook

`onExposure`, in the Provider's `config` or in `ServerClient`'s, is a function the client calls once
each time the server enrols the user in an experiment and the answer did not come from the client's
cache: the moment the server records the exposure. It receives an `Exposure`: `experimentKey`,
`userId`, `variantId` (`null` when the server sends none) and `variantName`.

```tsx
<ExperimentationProvider
  config={{ apiKey, baseUrl, onExposure: (e) => console.log('exposure', e.experimentKey, e.variantName) }}
  user={{ userId: visitorId }}
>
  <App />
</ExperimentationProvider>
```

It is not called for an answer from the client's cache, a user the experiment did not enrol
(`assigned: false`), a call that fails or a feature flag. A Provider rendered on the server calls nothing:
`useExperiment` asks for the assignment in the browser, after the first render. If it throws, or
returns a Promise that rejects, the answer is unchanged, and nothing reports the error: this SDK has
no `onError`.

Full details: [Exposure hook](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/react.md#exposure-hook).

## Backend endpoints used

Every request carries `X-API-Key: <key>` and `Content-Type: application/json`.

| SDK call | Method & path | Body / query | Response used |
|---|---|---|---|
| `evaluateFeatureFlag*`, `useFeatureFlag`, `useVariant`, `useMultipleFlags`, `ServerClient.getAll` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=<url-encoded JSON>` | `context` = `user.attributes` (omitted when empty) | `{key, enabled, config, reason}` (off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key) |
| `assignExperiment`, `useExperiment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration, assigned, reason}` (404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false`) |
| `trackEvent` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `trackEvent` without keys | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (≤ 100 per request) | ignored |

## Development

In a clone of this repository:

```bash
cd sdk/react && npm ci
npm test
npm run build
```

- `npm test`: unit tests (fetch is mocked)
- `npm run build`: tsc, strict

The unit tests run in `.github/workflows/sdk-unit-tests.yml` on every pull request (react is one of
the always-on core SDKs).

Consuming from source (as the ShopLab demo does): alias `@controlvar/react-sdk`
to `sdk/react/src` in your bundler and tsconfig.

### Contract smoke

```bash
cd sdk/react && npm run build --silent && node examples/contract_smoke.mjs
```

It prints:

```text
{"sdk":"react","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true},"exposure":{"calls":1,"payload":{"experiment_key":"sdk_contract_ab","variant_name":"control","variant_id":"…","user_id":"smoke-…"}}}
```

Drives the SSR entry point (`ServerClient`) plus `ExperimentationClient` for tracking under plain
node — no DOM, no React render. `exposure` is what the `onExposure` of a new `ServerClient`
received over two assignments of the smoke's user. Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`),
`CONTROLVAR_API_KEY` (required), `CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`),
`CONTRACT_FLAG_KEY` (default `sdk_contract_flag`), `CONTRACT_USER_ID` (default random
`smoke-<uuid>`). Repo-wide runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk react`.

`trackEvent` never throws, so the smoke wraps `fetch` and fails when a tracking call did not come
back 2xx — which is what catches a path the backend does not serve.
