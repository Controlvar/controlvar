# React SDK

`@controlvar/react-sdk` (v0.1.0) provides a context provider, hooks, a higher-order
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
`controlvar-react-sdk-0.1.0.tgz`:

```bash
git clone https://github.com/Controlvar/controlvar.git
cd controlvar/sdk/react
npm ci && npm run build && npm pack
```

Then install that file in your app:

```bash
npm install /path/to/controlvar/sdk/react/controlvar-react-sdk-0.1.0.tgz
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
| `config.onExposure` | `(exposure: Exposure) => void` | No | Called once each time the server enrols the user in an experiment and the answer is not cached; see [Exposure hook](#exposure-hook) |
| `user.userId` | `string` | Yes | Stable identifier used for bucketing |
| `user.attributes` | `Record<string, unknown>` | No | Sent as `context` on experiment assignment and on flag evaluation (targeting rules); see [Targeting context](#targeting-context) |
| `bootstrap` | `ExperimentationBootstrap` | No | Answers the server already has for this user, shown on the first render with no request; see [Start from the server's answers](#start-from-the-servers-answers-bootstrap) |

The client is recreated only when `apiKey`, `baseUrl`, `timeoutMs` or `cacheTtlMs` change (a new
client starts with empty caches), not when `onExposure` does; hooks re-run when `userId` or the attributes change. All hooks
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
| `isEnabled` | `boolean` | Server decision for this user (`false` while loading or on error; when the bootstrap has this flag, its answer from the first render, kept if the request fails) |
| `variant` | `string \| null` | `null` when off, otherwise `'on'` |
| `config` | `unknown \| null` | Always `null`: the evaluate endpoint returns `config: null` for every flag |
| `loading` | `boolean` | `true` until the first response; `false` from the first render when the bootstrap has the flag |
| `error` | `Error \| null` | Set when the request failed: the flag is reported off, or keeps its bootstrapped answer |
| `reason` | `string \| undefined` | Why the server decided: `targeting_rule`, `rollout`, `inactive` or `error`; `undefined` while loading, on error, or from servers that do not send it |

A flag change reaches an open page when a hook re-mounts after `cacheTtlMs`, or on reload; the SDK
neither polls nor streams. See [Turning a Flag Off](../feature-flags/kill-switch.md).

### `useVariant(flagKey): string | null`

Shorthand for `useFeatureFlag(flagKey).variant`.

### `useMultipleFlags(flagKeys): Record<string, FeatureFlagEvaluation | null>`

Evaluates several flags in parallel. Each entry is `null` until its evaluation completes, unless
the bootstrap has that flag: then it starts from the bootstrap's answer.

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
| `variantKey` | `string` | Assigned variant name (`'control'` while loading or on error; when the bootstrap has this experiment, its variant from the first render, kept if the request fails) |
| `variantName` | `string` | Same value as `variantKey` (`'Control'` as the default) |
| `variantId` | `string \| null` | Variant UUID, `null` until assigned |
| `isControl` | `boolean` | `true` for the control variant (and for the default) |
| `configuration` | `Record<string, unknown> \| null` | The variant's `configuration` JSON from the experiment definition |
| `loading` | `boolean` | `true` until the first response; `false` from the first render when the bootstrap has the experiment |
| `error` | `Error \| null` | Set when assignment failed (404 when no experiment has the key, or for a draft with no control variant): the control defaults are returned, or the bootstrapped assignment is kept; an experiment that is not ACTIVE is not an error |
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
one entry per experiment and one per flag the client answered for the user in the last 24 hours,
whether or not the answer is still cached; an experiment whose latest answer says it is a draft,
paused, completed or archived (`reason`) gets no entry. An experiment whose assignment is later
answered 404 loses its entry at once; any other failure (a 5xx, a network error) keeps it. This is
what makes a single `track('purchase', …)` count as a conversion for every experiment the user is
in. With nothing to
attribute it to, the event is sent once with no key: the server stores it as history, which counts
in holdout results and as CUPED history, never in an experiment's results. The keys live in the
client's memory (up to 10,000 users) and survive `clearCache()`; a page reload starts with none, and
the page's hooks record them again as they answer.

Pass `experimentKey` for an event that is the outcome of one experiment's surface (a click in the
sorted list). Leave it out for an outcome of the whole visit (a purchase), which counts for every
experiment the user is in.

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

In the App Router, import `ServerClient` from `@controlvar/react-sdk/ssr` in an async server
component and call the same methods there. Use the `/ssr` subpath rather than the package root: the
root also loads the provider, which a React Server Component cannot import. The `/ssr` subpath
resolves for a CommonJS `require`, for a Node ES module `import`, and for TypeScript under
`node10`, `node16`, `nodenext` and `bundler` module resolution.

### `ServerClient` API

| Method | Signature | Description |
|--------|-----------|-------------|
| `evaluateFeatureFlag` | `(flagKey, user) => Promise<FeatureFlagEvaluation>` | One flag (`user.attributes` sent as `context`); disabled evaluation on failure |
| `getAll` | `(flagKeys, user) => Promise<Record<string, FeatureFlagEvaluation>>` | Several flags in parallel |
| `assignExperiment` | `(experimentKey, user) => Promise<ExperimentAssignment>` | Sticky assignment; control defaults on failure |
| `bootstrap` | `(keys, user) => Promise<ExperimentationBootstrap>` | One request per key, in parallel; a failed key is left out. Never throws; reads and fills no cache. Each experiment key records an exposure. See [Start from the server's answers](#start-from-the-servers-answers-bootstrap) |
| `clearCache` | `() => void` | Drop cached results (per instance) |

---

## Start from the server's answers (bootstrap)

Without a bootstrap, a server-rendered page shows every flag off and every experiment's control
variant, then switches when the browser's requests answer: a flicker. With one, it shows the
answers the server already has, and the browser shows the same.

1. On the server, call `ServerClient.bootstrap()` with the keys the page renders and the page's
   user. It asks the API for each key, in parallel, and returns plain JSON, safe to pass as a prop.
2. Pass it to the provider as `bootstrap`, with the same user, attributes included.

A hook whose key is in the bootstrap starts with that answer (`loading: false`), on the server and
in the browser, with no request. It then asks the API as it always does; if the answer has changed
since the page was rendered (the flag was turned off, say), it shows the new one.

The keys below are your own flag and experiment keys; the examples use `new_search` and
`checkout_flow`. `CONTROLVAR_API_KEY` and `CONTROLVAR_API_URL` are read on the server only.

Pages Router:

```tsx
// pages/checkout.tsx
import type { GetServerSideProps } from 'next';
import {
  ExperimentationProvider,
  ServerClient,
  useExperiment,
  useFeatureFlag,
  type ExperimentationBootstrap,
} from '@controlvar/react-sdk';

interface Props { visitorId: string; bootstrap: ExperimentationBootstrap }

export const getServerSideProps: GetServerSideProps<Props> = async ({ req, res }) => {
  let visitorId = req.cookies.visitor_id;
  if (!visitorId) {
    visitorId = crypto.randomUUID();
    res.setHeader('Set-Cookie', `visitor_id=${visitorId}; Path=/; Max-Age=31536000; SameSite=Lax`);
  }
  const client = new ServerClient({
    apiKey: process.env.CONTROLVAR_API_KEY!,
    baseUrl: process.env.CONTROLVAR_API_URL!,
  });
  const bootstrap = await client.bootstrap(
    { flags: ['new_search'], experiments: ['checkout_flow'] },
    { userId: visitorId },
  );
  // A prop: Next.js writes it into the page. Never into a <script> tag of your own.
  return { props: { visitorId, bootstrap } };
};

export default function CheckoutPage({ visitorId, bootstrap }: Props) {
  return (
    <ExperimentationProvider
      config={{
        apiKey: process.env.NEXT_PUBLIC_CONTROLVAR_API_KEY!,
        baseUrl: process.env.NEXT_PUBLIC_CONTROLVAR_API_URL ?? 'http://localhost:8000',
      }}
      user={{ userId: visitorId }}
      bootstrap={bootstrap}
    >
      <Checkout />
    </ExperimentationProvider>
  );
}

function Checkout() {
  const search = useFeatureFlag('new_search');
  const { variantKey } = useExperiment('checkout_flow');
  return (
    <main>
      {search.isEnabled && <input type="search" aria-label="Search products" />}
      <button type="button">{variantKey === 'treatment' ? 'Buy now' : 'Checkout'}</button>
    </main>
  );
}
```

App Router (Next.js 16: `proxy.ts`, which was `middleware.ts` before 16). A server component
cannot set a cookie, so the proxy gives each visitor an id:

```ts
// proxy.ts
import { NextResponse, type NextRequest } from 'next/server';

export function proxy(request: NextRequest) {
  const existing = request.cookies.get('visitor_id')?.value;
  const visitorId = existing ?? crypto.randomUUID();
  const headers = new Headers(request.headers);
  headers.set('x-visitor-id', visitorId);
  const response = NextResponse.next({ request: { headers } });
  if (!existing) {
    response.cookies.set('visitor_id', visitorId, { path: '/', maxAge: 60 * 60 * 24 * 365, sameSite: 'lax' });
  }
  return response;
}
```

```tsx
// app/checkout/page.tsx (a server component)
import { headers } from 'next/headers';
import { ServerClient } from '@controlvar/react-sdk/ssr';
import { CheckoutClient } from './checkout-client';

export default async function CheckoutPage() {
  const visitorId = (await headers()).get('x-visitor-id')!;
  const client = new ServerClient({ apiKey: process.env.CONTROLVAR_API_KEY!, baseUrl: process.env.CONTROLVAR_API_URL! });
  const bootstrap = await client.bootstrap({ flags: ['new_search'], experiments: ['checkout_flow'] }, { userId: visitorId });
  // A prop of a client component: Next.js writes it into the page. Never into a <script> tag of your own.
  return <CheckoutClient visitorId={visitorId} bootstrap={bootstrap} />;
}
```

```tsx
// app/checkout/checkout-client.tsx
'use client';
import { ExperimentationProvider, useExperiment, useFeatureFlag, type ExperimentationBootstrap } from '@controlvar/react-sdk';

export function CheckoutClient({ visitorId, bootstrap }: { visitorId: string; bootstrap: ExperimentationBootstrap }) {
  return (
    <ExperimentationProvider
      config={{ apiKey: process.env.NEXT_PUBLIC_CONTROLVAR_API_KEY!, baseUrl: process.env.NEXT_PUBLIC_CONTROLVAR_API_URL ?? 'http://localhost:8000' }}
      user={{ userId: visitorId }}
      bootstrap={bootstrap}
    >
      <Checkout />
    </ExperimentationProvider>
  );
}

function Checkout() {
  const search = useFeatureFlag('new_search');
  const { variantKey } = useExperiment('checkout_flow');
  return (
    <main>
      {search.isEnabled && <input type="search" aria-label="Search products" />}
      <button type="button">{variantKey === 'treatment' ? 'Buy now' : 'Checkout'}</button>
    </main>
  );
}
```

The proxy runs on every route unless you give it a `matcher`; if you do, include every page that
reads `x-visitor-id`.

Pass the bootstrap to the provider as a prop, as both examples do: Next.js writes a page's props
into the page itself. Do not write it into a `<script>` tag of your own: a variant's
`configuration` is text your team wrote, and it would go into the page as it is.

### What the bootstrap records

- `bootstrap()` assigns each experiment you name, and the server records an exposure for its
  variant then, before the page is sent. **Name only the experiments the page renders**: one you
  name and do not render still records an exposure. The browser sends no assignment for a
  bootstrapped experiment no hook renders.
- After hydration the hook asks again, and the server records a second exposure for the same user
  and variant (assignment is sticky). Count users, not exposure events.
- Each bootstrapped flag is evaluated twice per page view, on the server and in the browser, and
  counts twice in the flag's evaluation count, the denominator of its error rate for safety
  monitoring.
- The provider's `onExposure` is called once, after hydration; `ServerClient`'s, if you set one,
  when `bootstrap()` assigns. Set it in one of the two.

### When the bootstrap is not used

| Case | What happens |
|---|---|
| A key the bootstrap does not have | That hook starts loading and asks the API, as without a bootstrap. |
| `bootstrap()` could not get an answer for a key (an error, a timeout, a 429: its requests share the per-IP SDK rate limit) | The key is left out, so it loads as without a bootstrap. To see why, call `evaluateFeatureFlag` or `assignExperiment` for it: they return the error. |
| `bootstrap.userId` is not the provider's `user.userId` | The whole bootstrap is ignored, with one warning in the browser console. |
| `bootstrap` is not an object with a text `userId`, or its `flags` or `experiments` is there and is not an object | The whole bootstrap is ignored, with one warning. |
| An entry is not a response the API gives (a flag without a `true` or `false` `enabled`, an experiment without a text `variant_name`) | That key loads as without a bootstrap, with one warning for the provider. |
| The hook's own request fails | The hook keeps the bootstrapped answer and sets `error`. |
| The provider's `user` changes after the first render | Hooks start loading and ask again. A bootstrap still passed is then another user's: it is ignored, with its warning. |
| A new `bootstrap` prop after the first render | Hooks already on the page keep their answers; hooks that mount later read the new one. |
| `bootstrap` is `null` or left out | No bootstrap: every hook starts loading, and there is no warning. |

Each warning starts with `@controlvar/react-sdk: bootstrap`, names no user, key or value, and is
printed once per provider, in the browser only (never during a server render), production builds
included.

A bootstrap has no expiry: it is the answer at the moment the page was rendered, and each hook
replaces it with the API's answer once the browser has one.

**Keep a bootstrapped page out of shared caches.** It carries one visitor's id and answers. Served
from a CDN or a proxy to someone else, it shows them the first visitor's variants, and the browser
tracks their events as that visitor. Keep its `Cache-Control` private. A page that a shared cache
serves anyway shows its bootstrapped answers until the hooks' requests answer; if those fail, the
answers stay, with `error` set.

### Build one without `ServerClient`

The format is the API's own responses, so any server can build it: each `flags` entry is the body
of `GET /api/v1/feature-flags/evaluate/{key}?user_id=…`, and each `experiments` entry the body of
`POST /api/v1/tracking/assign`, keyed by the key you asked for. For a visitor assigned to
`treatment`, `bootstrap({ flags: ['new_search'], experiments: ['checkout_flow'] }, { userId })`
returns:

```json
{
  "userId": "2b6f0cc9-5a3e-4c1d-9e7b-6f8a1d2c3b4e",
  "flags": {
    "new_search": {
      "key": "new_search",
      "enabled": true,
      "value": true,
      "value_type": "boolean",
      "config": null,
      "reason": "rollout"
    }
  },
  "experiments": {
    "checkout_flow": {
      "experiment_key": "checkout_flow",
      "user_id": "2b6f0cc9-5a3e-4c1d-9e7b-6f8a1d2c3b4e",
      "variant_id": "8d1e4f2a-7b3c-4e5d-a6f7-0b9c8d7e6f5a",
      "variant_name": "treatment",
      "is_control": false,
      "configuration": { "button_text": "Buy now" },
      "assigned": true,
      "reason": "assigned"
    }
  }
}
```

The fields are those a live server sends; CI's SDK Live Contract job compares them with the
bootstrap it builds against a real API on every pull request that is not docs-only.

---

## Exposure hook

`config.onExposure` is a function you give the client. The client calls it once each time the
server enrols the user in an experiment and the answer did not come from the client's cache: the
moment the server records the exposure. Use it to send the exposure to your analytics tool or
warehouse.

```tsx
<ExperimentationProvider
  config={{
    apiKey: process.env.NEXT_PUBLIC_CONTROLVAR_API_KEY!,
    baseUrl: process.env.NEXT_PUBLIC_CONTROLVAR_API_URL ?? 'http://localhost:8000',
    onExposure: (exposure) => {
      try {
        // your analytics tool's call goes here
        console.log('exposure', exposure.experimentKey, exposure.variantName, exposure.userId);
      } catch (err) {
        console.error('onExposure failed', err); // this SDK reports nothing itself
      }
    },
  }}
  user={{ userId: currentUser.id }}
>
  <YourApplication />
</ExperimentationProvider>
```

It receives an `Exposure`, a new object each time: `experimentKey`, `userId`, `variantId` (`null`
when the server sends none) and `variantName`. Nothing else: not the user's attributes and not the
variant's configuration.

It is not called for:

- an answer from the client's cache;
- a user the experiment did not enrol (`assigned: false`, with `reason` `'holdout'`,
  `'mutual_exclusion'` or `'targeting'`), or an experiment that is not running (`'draft'`,
  `'paused'`, `'completed'` or `'archived'`): the server records no exposure for them;
- a call that fails;
- a feature flag.

Calls for the same user and experiment that are made while the first is still waiting share its
request, so together they call it once.

`useExperiment` asks for the assignment in the browser, after the first render, so a server render
makes no request and calls nothing. A server render followed by hydration calls it once, in the
browser, and so does React's StrictMode, which runs effects twice in development.

Changing `onExposure` between renders does not create a new client, so nothing is asked again; the
client calls the function from the latest render.

It is called again, and the server records another exposure, after the cache lifetime
(`cacheTtlMs`, 5 minutes by default), after `clearCache()`, and in every new client: each page
load, and each change of `apiKey`, `baseUrl`, `timeoutMs` or `cacheTtlMs`. So your tool can receive
several exposures for one user and experiment: count users, not events. It never receives one the
server did not record.

It runs before the assignment resolves, and before every call waiting on the same request, so keep
it quick: hand the exposure to your analytics library, which sends it later. The client does not
wait for a Promise it returns.

If it throws, or returns a Promise that rejects, the answer is unchanged, and nothing reports the
error: this SDK has no `onError`. Catch inside your function, as above, if you need to know.

`ServerClient` takes `onExposure` in its config too, and calls it for the assignments it makes, in
the same way. It is a separate client with its own cache: if you assign a user with `ServerClient`
on the server and with `useExperiment` in the browser, the server records two exposures, and each
client that has `onExposure` calls its own. Set `onExposure` where your analytics library runs: on
the Provider for a browser library, on `ServerClient` for a server one. A `ServerClient` created in
each request, as in the `getServerSideProps` example above, starts with an empty cache, so it calls
`onExposure` on every request that assigns, and the server records an exposure on each.

The Provider and `ServerClient` throw `onExposure must be a function` for any other value; `null`
and `undefined` set none.

In a test, a mocked `/assign` answer must include `"assigned": true`, or the hook is not called.
This SDK treats an answer without `assigned` as enrolled for `ExperimentAssignment.assigned`, but
does not call the hook for it.

---

## Backend endpoints used

Every request carries `X-API-Key` and `Content-Type: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `useFeatureFlag`, `useVariant`, `useMultipleFlags`, `ServerClient.evaluateFeatureFlag`/`getAll`/`bootstrap` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=<url-encoded JSON>` | `context` = `user.attributes` (omitted when empty) | `{key, enabled, config, reason}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `useExperiment`, `ServerClient.assignExperiment`/`bootstrap` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration, assigned, reason}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
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
  Exposure,
  ExperimentationBootstrap,
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

interface Exposure {          // what onExposure receives; also exported from '@controlvar/react-sdk/ssr'
  readonly experimentKey: string;
  readonly userId: string;
  readonly variantId: string | null;
  readonly variantName: string;
}

interface ExperimentationBootstrap {   // what ServerClient.bootstrap() returns; also exported from '@controlvar/react-sdk/ssr'
  userId: string;
  flags?: Record<string, FeatureFlagEvaluateResponse>;     // body of GET …/feature-flags/evaluate/{key}
  experiments?: Record<string, ExperimentAssignResponse>; // body of POST …/tracking/assign
}
```

---

## Error Handling

- `useFeatureFlag` / `useMultipleFlags` report the flag as off with `error` set, or keep a
  bootstrapped answer with `error` set.
- `useExperiment` returns the control defaults (`variantKey: 'control'`, `isControl: true`,
  `configuration: null`) with `error` set, or keeps a bootstrapped assignment with `error` set.
- `ServerClient.bootstrap()` never throws: a key it could not get is left out.
- `useTrackEvent` swallows network failures.
- A failing `onExposure` changes no answer and is reported nowhere (see [Exposure hook](#exposure-hook)).
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
