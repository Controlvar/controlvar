# @controlvar/react-native-sdk

**Beta.** This is a 0.x release: pin the exact version you test against.

Feature flags, A/B experiment assignment and event tracking for React Native apps: a context
provider, three hooks and a plain client, with an in-memory cache and an AsyncStorage-backed
offline fallback. Flags and assignments are decided **by the server** (sticky per user +
experiment); the SDK caches the answers per user + key and never buckets locally.

Full documentation: [the React Native SDK page](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/react-native.md).

The user's attributes are sent as `context` on flag evaluation and on experiment assignment, so
targeting rules match as in the JavaScript SDK.

## Install

To install it, follow [the React Native SDK page](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/react-native.md#installation).

- Its peer dependencies are `react` >= 16.8 and `react-native` >= 0.65; your app provides them.
- `@react-native-async-storage/async-storage` `>=1.21.0 <4` is a peer dependency too, and must
  be one of your app's own dependencies, because React Native links a native module only from
  the app's own dependencies. Majors 1, 2 and 3 are supported.
- iOS: `cd ios && pod install`, for AsyncStorage's native module.
- It ships TypeScript source, which Metro compiles; the type declarations of `md5`, which it
  imports, come with it.

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
  const { enabled, loading } = useFlag('dark-mode');
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

## Exposure hook

`onExposure`, a function you give `ExperimentationClient` when you create it, is called once each
time the server enrols the user in an experiment and the answer did not come from the client's
memory cache or from AsyncStorage: the moment the server records the exposure. It receives an
`Exposure`: `experimentKey`, `userId`, `variantId` (`null` when the server sends none) and
`variantName`.

```ts
const client = new ExperimentationClient({
  apiKey: 'YOUR_API_KEY',
  baseUrl: 'http://localhost:8000',
  onExposure: (exposure) => console.log('exposure', exposure.experimentKey, exposure.variantName),
  onError: (error, operation) => {
    if (operation === 'exposure') console.warn('onExposure failed', error);
  },
});
```

It is not called for an answer from the client's memory cache or from AsyncStorage, a user the
experiment did not enrol (`assigned: false`), a call that fails or a feature flag. With the default
`offlineFallback`, the answer stays in AsyncStorage for the cache lifetime, so neither
`clearCache()` nor a new client calls it again until that answer expires; with
`offlineFallback: false`, or after `clearStorage()`, both do. If it throws, or returns a Promise
that rejects, the answer is unchanged and `onError` receives the error with the operation
`'exposure'`.

Full details: [Exposure hook](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/react-native.md#exposure-hook).

## Testing with Jest

React Native's Jest preset does not transform code under `node_modules`, and this package is
TypeScript source, so add it to the preset's `transformIgnorePatterns`:

```json
{
  "preset": "react-native",
  "transformIgnorePatterns": [
    "node_modules/(?!((jest-)?react-native|@react-native(-community)?|@controlvar/react-native-sdk)/)"
  ]
}
```

Map AsyncStorage to an in-memory mock as well: [the Jest setup](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/react-native.md#jest-setup) on the SDK page shows one.

## Development

In a clone of this repository, in `sdk/react-native`: `npm ci`, then `npm test` (Jest, with
`fetch` and AsyncStorage mocked), `npm run typecheck` (the source) and `npm run typecheck:tests`
(the tests and mocks).

The SDK Unit Tests workflow runs all three on every pull request that changes `sdk/react-native`.
It also installs the packed package into a TypeScript React Native app on each AsyncStorage major,
which must keep its own AsyncStorage and type-check the package
(`scripts/check_react_native_app.py`).
