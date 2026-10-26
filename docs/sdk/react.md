# React SDK

`@controlvar/react-sdk` (v1.1) provides a context provider, hooks, a higher-order
component and an SSR client for using experiments and feature flags from React and Next.js.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

Source: `sdk/react`. A complete working integration is the ShopLab demo storefront in
`demo/shoplab`.

CI runs this SDK's contract smoke against a real API: `react` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Installation

**Not yet published.** `@controlvar/react-sdk` is not on npm, so build it from source in a
clone of this repository and install the packed tarball (the blocks below).

Its peer dependencies are `react` >= 17 and `react-dom` >= 17.

To install from source, clone the repository and build a package; `npm pack` writes
`controlvar-react-sdk-1.1.0.tgz`:

```bash
git clone https://github.com/Controlvar/controlvar.git
cd controlvar/sdk/react
npm ci && npm run build && npm pack
```

Then install that file in your app:

```bash
npm install /path/to/controlvar/sdk/react/controlvar-react-sdk-1.1.0.tgz
```

To consume the SDK from source inside this monorepo (what `demo/shoplab` does), alias
`@controlvar/react-sdk` to `sdk/react/src` in `tsconfig.json` `paths` and in
your bundler, and alias `react`/`react-dom` to your app's copies to avoid a duplicate React.

---

## Provider Setup

If you set `NEXT_PUBLIC_EXPERIMENTLY_API_KEY` or `EXPERIMENTLY_API_KEY` from an earlier version of these pages, set `NEXT_PUBLIC_CONTROLVAR_API_KEY` and `CONTROLVAR_API_KEY` instead.

```tsx
import { ExperimentationProvider } from '@controlvar/react-sdk';

function App() {
  return (
    <ExperimentationProvider
      config={{
        apiKey: process.env.NEXT_PUBLIC_CONTROLVAR_API_KEY!,
        baseUrl: process.env.NEXT_PUBLIC_CONTROLVAR_API_URL ?? 'http://localhost:8000',
      }}
      user={{ userId: currentUser.id, attributes: { country: currentUser.country, plan: currentUser.plan } }}
    >
      <YourApplication />
    </ExperimentationProvider>
  );
}
```

### Provider Props

| Prop | Type | Required | Description |
|------|------|----------|-------------|
| `config.apiKey` | `string` | Yes | API key (sent as `X-API-Key`) |
| `config.baseUrl` | `string` | Yes | Backend origin, e.g. `https://api.example.com`; the SDK appends `/api/v1/...` |
| `config.timeoutMs` | `number` | No | Per-request timeout, default `5000` |
| `config.cacheTtlMs` | `number` | No | How long a successful evaluation/assignment is reused, default `300000` (5 min) |
| `user.userId` | `string` | Yes | Stable identifier used for bucketing |
| `user.attributes` | `Record<string, unknown>` | No | Sent as `context` on experiment assignment and on flag evaluation (targeting rules); see [Targeting context](#targeting-context) |

The client is recreated only when `apiKey`, `baseUrl`, `timeoutMs` or `cacheTtlMs` change (a new
client starts with empty caches); hooks re-run when `userId` or the attributes change. All hooks
return safe defaults while loading and on a network or server error, and do not throw then;
outside an `<ExperimentationProvider>` they throw.

### Targeting context

`user.attributes` is what the platform's targeting rules evaluate against. The SDK sends it as
`context` in the `POST /api/v1/tracking/assign` body and, when it is a non-empty object, as
`context=<url-encoded JSON>` on `GET /api/v1/feature-flags/evaluate/{flagKey}` — so a flag whose
dashboard rule says `os_version semver_gte 17.0.0 AND tier equals premium` turns on only for users
whose attributes match. Top-level keys are also reachable under `user.` / `device.` / `app.`
aliases in rules (`country` matches `user.country`), and nested objects flatten to dotted keys
(`{ app: { version: '3.2.1' } }` answers `app.version`).

Attributes are assumed to be **stable per user**: evaluations and assignments are cached by
user + key only, so after changing a user's attributes call `client.clearCache()` (via
`useExperimentation()`), or re-mount the provider with a `key` tied to the user. The provider does
re-run hooks when the attributes change, but they will be served from the cache until it is
cleared or the TTL elapses.

---

## Hooks Reference

### `useFeatureFlag(flagKey): FeatureFlagEvaluation`

Evaluates one flag for the current user via
`GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…[&context=<url-encoded JSON>]`
(`context` is `user.attributes`, omitted when empty).

```tsx
import { useFeatureFlag } from '@controlvar/react-sdk';

function SearchPage() {
  const { isEnabled, variant, config, loading, error } = useFeatureFlag('new_search');
  if (loading) return <Spinner />;
  return isEnabled ? <NewSearch /> : <LegacySearch />;
}
```

| Field | Type | Description |
|-------|------|-------------|
| `flagKey` | `string` | The key you asked for |
| `isEnabled` | `boolean` | Server decision for this user (`false` while loading or on error) |
| `variant` | `string \| null` | `null` when off, otherwise `'on'` |
| `config` | `unknown \| null` | Always `null`: the evaluate endpoint returns `config: null` for every flag |
| `loading` | `boolean` | `true` until the first response |
| `error` | `Error \| null` | Set when the request failed (flag is reported off) |
| `reason` | `string \| undefined` | Why the server decided: `targeting_rule`, `rollout`, `inactive` or `error`; `undefined` while loading, on error, or from servers that do not send it |

### `useVariant(flagKey): string | null`

Shorthand for `useFeatureFlag(flagKey).variant`.

### `useMultipleFlags(flagKeys): Record<string, FeatureFlagEvaluation | null>`

Evaluates several flags in parallel. Each entry is `null` until its evaluation completes.

```tsx
const flags = useMultipleFlags(['new_search', 'free_shipping_banner']);
const showBanner = flags['free_shipping_banner']?.isEnabled ?? false;
```

### `useExperiment(experimentKey): ExperimentAssignment`

Assigns the current user via `POST /api/v1/tracking/assign` (sticky on the server) and returns the
variant plus its configuration.

```tsx
import { useExperiment } from '@controlvar/react-sdk';

function HeroBanner() {
  const { variantKey, configuration, isControl, loading } = useExperiment('hero_banner');
  const headline = (configuration?.headline as string) ?? 'Gear up for the season';
  return <Hero headline={headline} video={variantKey === 'video_hero'} />;
}
```

| Field | Type | Description |
|-------|------|-------------|
| `experimentKey` | `string` | The key you asked for |
| `variantKey` | `string` | Assigned variant name (`'control'` while loading or on error) |
| `variantName` | `string` | Same value as `variantKey` (`'Control'` as the default) |
| `variantId` | `string \| null` | Variant UUID, `null` until assigned |
| `isControl` | `boolean` | `true` for the control variant (and for the default) |
| `configuration` | `Record<string, unknown> \| null` | The variant's `configuration` JSON from the experiment definition |
| `loading` | `boolean` | `true` until the first response |
| `error` | `Error \| null` | Set when assignment failed (404 when no experiment has the key, or for a draft with no control variant); an experiment that is not ACTIVE is not an error |
| `assigned` | `boolean \| undefined` | `false` when the server did not enrol the user and returned the control variant (no exposure recorded); `true` for a real assignment and for servers that predate the field; `undefined` while loading or on error |
| `reason` | `string \| undefined` | `'assigned'`, `'holdout'`, `'mutual_exclusion'` or `'targeting'`, or the status of an experiment that is not ACTIVE (`'draft'`, `'paused'`, `'completed'` or `'archived'`); `undefined` while loading, on error, or when the server does not send one |

When you export exposures to a warehouse, log only assignments with `assigned: true`; the others
were never in the experiment.

### `useTrackEvent(): (eventName, properties?, options?) => void`

Returns a stable function that records events. Tracking is fire-and-forget and never throws.

```tsx
const track = useTrackEvent();

track('add_to_cart', { product_id, quantity: 1 }, { experimentKey: 'pdp_buy_button', value: 49 });
track('search', { query, results: 12 }, { featureFlagKey: 'new_search' });
track('page_view', { page: '/products' });   // no key: fanned out, see below
```

`TrackEventOptions`: `experimentKey?`, `featureFlagKey?`, `value?` (numeric value, e.g. revenue),
`eventType?` (defaults to `eventName`), `timestamp?` (`Date`, sent as ISO-8601).

**Fan-out rule.** With `experimentKey` and/or `featureFlagKey` the SDK sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch` containing
one entry per experiment the user has been assigned to in this client plus one per flag evaluated
for the user (from the cache). If nothing is cached, nothing is sent. This is what makes a single
`track('purchase', …)` count as a conversion for every experiment the user is in.

Conversions are matched to metrics by **event name**: an experiment metric whose `event_name` is
`purchase` counts every `purchase` event, whatever `event_type` was sent.

### `useExperimentation(): { client, user }`

Escape hatch to the underlying `ExperimentationClient` (for example `client.getAssignments(user.userId)`
to build a debug overlay). `useExperimentationContext` is an alias.

---

## `withExperimentation` Higher-Order Component

Injects `flagEvaluation: FeatureFlagEvaluation` for one flag into a component.

```tsx
import { withExperimentation, type FeatureFlagEvaluation } from '@controlvar/react-sdk';

interface Props { flagEvaluation: FeatureFlagEvaluation | null; title: string }

function Layout({ flagEvaluation, title }: Props) {
  return <div className={flagEvaluation?.isEnabled ? 'layout-v2' : 'layout-v1'}>{title}</div>;
}

export default withExperimentation(Layout, 'new_product_layout');
// <LayoutWithFlag title="..." />  — no flagEvaluation prop needed
```

---

## SSR / Next.js Support

`ServerClient` makes the same requests from Node and **never throws**: failures come back as a
disabled evaluation or the control defaults with `error` set.

```tsx
// pages/checkout.tsx
import { ServerClient } from '@controlvar/react-sdk';
import type { GetServerSideProps } from 'next';

export const getServerSideProps: GetServerSideProps = async ({ req }) => {
  const client = new ServerClient({
    apiKey: process.env.CONTROLVAR_API_KEY!,
    baseUrl: process.env.CONTROLVAR_API_URL!,
  });
  const user = { userId: req.cookies['visitor_id'] ?? 'anonymous' };

  const [checkout, flags] = await Promise.all([
    client.assignExperiment('checkout_flow', user),
    client.getAll(['new_search', 'free_shipping_banner'], user),
  ]);

  return { props: { variantKey: checkout.variantKey, flags } };
};
```

In the App Router call the same methods from an async server component.

### `ServerClient` API

| Method | Signature | Description |
|--------|-----------|-------------|
| `evaluateFeatureFlag` | `(flagKey, user) => Promise<FeatureFlagEvaluation>` | One flag (`user.attributes` sent as `context`); disabled evaluation on failure |
| `getAll` | `(flagKeys, user) => Promise<Record<string, FeatureFlagEvaluation>>` | Several flags in parallel |
| `assignExperiment` | `(experimentKey, user) => Promise<ExperimentAssignment>` | Sticky assignment; control defaults on failure |
| `clearCache` | `() => void` | Drop cached results (per instance) |

---

## Backend endpoints used

Every request carries `X-API-Key` and `Content-Type: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `useFeatureFlag`, `useVariant`, `useMultipleFlags`, `ServerClient.evaluateFeatureFlag`/`getAll` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=<url-encoded JSON>` | `context` = `user.attributes` (omitted when empty) | `{key, enabled, config, reason}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `useExperiment`, `ServerClient.assignExperiment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `useTrackEvent` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `useTrackEvent` without keys | `POST /api/v1/tracking/batch` | `{events: [ ...track bodies ]}` (max 100 per request) | ignored |

These SDK paths share a per-IP rate-limit ceiling of `SDK_RATE_LIMIT_PER_MINUTE` requests
(default 6000) on the backend.

---

## TypeScript Types

```typescript
import type {
  SdkConfig,
  UserContext,
  FeatureFlagEvaluation,
  ExperimentAssignment,
  TrackEventOptions,
} from '@controlvar/react-sdk';

interface FeatureFlagEvaluation {
  flagKey: string;
  variant: string | null;
  isEnabled: boolean;
  config: unknown | null;
  loading: boolean;
  error: Error | null;
  reason?: string; // 'targeting_rule' | 'rollout' | 'inactive' | 'error'; undefined when not sent
}

interface ExperimentAssignment {
  experimentKey: string;
  variantKey: string;
  variantName: string;
  variantId: string | null;
  isControl: boolean;
  configuration: Record<string, unknown> | null;
  loading: boolean;
  error: Error | null;
  assigned?: boolean; // false when the server returned control without enrolling the user
  reason?: string;    // 'assigned' | 'holdout' | 'mutual_exclusion' | 'targeting', or the status of an experiment that is not ACTIVE
}

interface TrackEventOptions {
  experimentKey?: string;
  featureFlagKey?: string;
  value?: number;
  eventType?: string;
  timestamp?: Date;
}
```

---

## Error Handling

- `useFeatureFlag` / `useMultipleFlags` report the flag as off with `error` set.
- `useExperiment` returns the control defaults (`variantKey: 'control'`, `isControl: true`,
  `configuration: null`) with `error` set.
- `useTrackEvent` swallows network failures.
- Failed evaluations and assignments are never cached, so the next render retries.
- Concurrent calls for the same user + key share one in-flight request, so mounting several
  components that use the same experiment produces one assignment (and one exposure event).

---

## Development

```bash
cd sdk/react && npm install
npx jest
npm run build
```

- `npx jest`: unit tests, fetch is mocked
- `npm run build`: tsc --strict
