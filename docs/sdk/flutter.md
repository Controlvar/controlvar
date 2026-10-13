# Flutter SDK

`experimently` (v0.2) provides feature flag evaluation, A/B experiment assignment and
event tracking for Flutter applications on iOS, Android, Web, macOS, Windows and Linux. A
Flutter-free entry point (`experimently_core.dart`) exposes the same client to plain Dart
programs.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed on the device.

> **Beta: flag targeting on user attributes does not work in this SDK yet.** Flag
> evaluation sends only the user id (`GET /api/v1/feature-flags/evaluate/{key}?user_id=…`),
> not the user's attributes, so a flag targeting rule on an attribute (app version, OS,
> country, plan) never matches here, and the flag's rollout percentage alone decides. Experiment
> assignment is not affected: `getAssignment` sends the attributes as `context`, so experiment
> targeting works. Until this is fixed ([#438](https://github.com/getexperimently/experimently/issues/438)), an app that needs attribute targeting on a
> flag can call the evaluate endpoint itself with a `context` query parameter (a URL-encoded
> JSON object of the attributes).

Source: `sdk/flutter`.

---

## Requirements

- Dart 2.17+ (null safety), Flutter 3.0+
- Dependencies: `http`, `crypto`, `shared_preferences`

---

## Installation

```yaml
dependencies:
  flutter:
    sdk: flutter
  experimently:
    git:
      url: https://github.com/getexperimently/experimently
      path: sdk/flutter
```

```bash
flutter pub get
```

Two library entry points are available:

| Import | Use from | Adds |
|--------|----------|------|
| `package:experimently/experimently.dart` | Flutter apps | everything below plus `SharedPreferencesOfflineStore` |
| `package:experimently/experimently_core.dart` | plain Dart (servers, CLIs, the contract smoke) | the client, models, cache, HTTP client, `InMemoryOfflineStore`, `hashUser` — no `package:flutter` code |

---

## Quick Start

```dart
import 'package:experimently/experimently.dart';

final client = ExperimentationClient(
  config: const SdkConfig(
    apiKey: 'your-api-key',            // sent as X-API-Key
    baseUrl: 'http://localhost:8000',  // origin only; the SDK appends /api/v1/...
  ),
  offlineStore: SharedPreferencesOfflineStore(),
);

// Once, before any other call. Opens the offline store; makes no network request.
await client.init();

final flag = await client.evaluateFlag('new-checkout', 'user-123');
if (flag.enabled) {
  showNewCheckout();
}

final assignment = await client.getAssignment('checkout-cta-copy', 'user-123',
    attributes: {'plan': 'pro'});
final headline = (assignment?.configuration?['headline'] as String?) ?? 'Buy now';

await client.track('purchase', 'user-123',
    experimentKey: 'checkout-cta-copy', value: 99.99, properties: {'sku': 'pro-plan'});
```

---

## Configuration

```dart
const config = SdkConfig(
  apiKey: 'your-api-key',                // Required — sent as X-API-Key
  baseUrl: 'https://api.example.com',    // Required — origin only
  timeout: Duration(seconds: 5),         // Per-request HTTP timeout (default 5 s)
  cacheTtl: Duration(minutes: 5),        // How long a successful result is reused (default 5 min)
  offlineFallback: true,                 // Persist results to the OfflineStore (default true)
);
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `apiKey` | `String` | *(required)* | API key sent as `X-API-Key` |
| `baseUrl` | `String` | *(required)* | Backend origin, e.g. `https://api.example.com`; a trailing `/` is stripped |
| `timeout` | `Duration` | `5s` | HTTP request timeout |
| `cacheTtl` | `Duration` | `5 min` | How long a successful evaluation/assignment is reused |
| `offlineFallback` | `bool` | `true` | Write successful results to the `OfflineStore` and serve them when the API is unreachable |

`ExperimentationClient` constructor parameters:

| Parameter | Description |
|-----------|-------------|
| `config` | `SdkConfig` (required) |
| `httpClient` | `ApiHttpClient`; build one with a `package:http` `Client` to inject `MockClient` in tests |
| `offlineStore` | `SharedPreferencesOfflineStore()` for Flutter apps; defaults to `InMemoryOfflineStore()` (session-scoped) |

---

## Feature Flag Evaluation

### `evaluateFlag(String flagKey, String userId): Future<EvalResult>`

Calls `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…` and returns the `EvalResult` below. An
optional `attributes:` named argument is accepted but not sent (see the beta note at the top of
this page).

| Property | Type | Description |
|----------|------|-------------|
| `key` | `String` | The flag key you asked for |
| `enabled` | `bool` | Server decision for this user (`false` on any failure) |
| `config` | `dynamic` | Always `null`: the evaluate endpoint returns `config: null` for every flag |
| `configMap` | `Map<String, dynamic>?` | Always `null`, like `config` |

```dart
final flag = await client.evaluateFlag('dark-mode', 'user-456');
if (flag.enabled) {
  enableDarkMode();
}
```

Never throws. On a network/HTTP failure the cached evaluation is returned when one exists,
otherwise the persisted offline value (if `offlineFallback`), otherwise `EvalResult.disabled(key)`.
Failures are never cached, so the next call retries. A 404 (an unknown flag key) is a
definitive answer: the flag is reported disabled and any persisted offline value is removed.
Concurrent calls for the same user + key share a single request.

### `isEnabled(String flagKey, String userId): Future<bool>`

Shorthand for `(await evaluateFlag(...)).enabled`.

---

## Experiment Assignment

### `getAssignment(String experimentKey, String userId, {Map<String, dynamic>? attributes}): Future<Assignment?>`

Calls `POST /api/v1/tracking/assign` with `{experiment_key, user_id, context: attributes}`. The
server buckets the user, keeps the assignment sticky and records the exposure.

| Property | Type | Description |
|----------|------|-------------|
| `experimentKey` | `String` | The experiment key |
| `userId` | `String` | The user the assignment belongs to |
| `variantId` | `String?` | UUID of the assigned variant |
| `variantName` | `String` | Assigned variant name (e.g. `"control"`, `"treatment"`) |
| `isControl` | `bool` | `true` for the control variant |
| `configuration` | `Map<String, dynamic>?` | The variant's `configuration` JSON from the experiment definition |

```dart
final assignment = await client.getAssignment('checkout-cta-copy', 'user-123',
    attributes: {'plan': 'pro', 'country': 'US'});

switch (assignment?.variantName) {
  case 'treatment-a': renderShortCta(); break;
  case 'treatment-b': renderUrgencyCta(); break;
  default:            renderOriginalCta();   // control, or null on failure
}
```

Never throws. Returns `null` on a 404 (experiment not ACTIVE / unknown) or when the request failed
and neither the cache nor the offline store has a value. Failures are never cached.
`variantKey` remains as a deprecated alias of `variantName`.

### `getVariant(String experimentKey, String userId, {attributes}): Future<String?>`

Shorthand for `(await getAssignment(...))?.variantName`.

---

## Event Tracking

### `track(String eventName, String userId, {properties, experimentKey, featureFlagKey, value, eventType, timestamp}): Future<bool>`

Never throws. Returns `true` when every request succeeded (or nothing had to be sent), `false`
otherwise.

```dart
await client.track('purchase', 'user-123', experimentKey: 'checkout-cta-copy', value: 99.99);
await client.track('search', 'user-123', featureFlagKey: 'new-search', properties: {'q': 'shoes'});
await client.track('page_view', 'user-123', properties: {'page': '/products'});   // no key: fanned out
```

**Fan-out rule.** With `experimentKey` and/or `featureFlagKey` the SDK sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch`
containing one entry per experiment the user has been assigned to through this client plus one
per flag evaluated for the user (from the in-memory cache). If nothing is cached, nothing is sent.
This is what makes a single `track('purchase', …)` count as a conversion for every experiment the
user is in.

Conversions are matched to metrics by **event name**: an experiment metric whose `event_name` is
`purchase` counts every `purchase` event, whatever `event_type` was sent. `properties` is sent as
`metadata`; `eventType` defaults to `eventName`; `timestamp` is sent as ISO-8601 UTC when given.

Because the fan-out reads the in-memory cache, it only sees assignments and flags evaluated
through the **same** client instance within `cacheTtl`. Pass the key explicitly otherwise.

### `trackEvent(TrackEvent event): Future<bool>`

Same as `track` with a prebuilt `TrackEvent(userId, eventName, {eventType, experimentKey,
featureFlagKey, value, properties, timestamp})`.

### `trackBatch(List<TrackEvent> events): Future<bool>`

Sends events with `POST /api/v1/tracking/batch`, at most 100 per request (longer lists are
chunked). Keyed events are sent as-is; events without a key are fanned out like `track`. Never
throws; returns `false` if any batch request failed.

```dart
final ok = await client.trackBatch([
  TrackEvent(userId: 'user-123', eventName: 'purchase', experimentKey: 'checkout-cta-copy', value: 99.99),
  TrackEvent(userId: 'user-123', eventName: 'search', featureFlagKey: 'new-search'),
]);
```

---

## Cache and offline store

| Method | Description |
|--------|-------------|
| `getAssignments(String userId): List<Assignment>` | Cached (successful, unexpired) assignments for the user |
| `getEvaluatedFlags(String userId): List<String>` | Keys of flags successfully evaluated (and still cached) for the user |
| `clearCache(): void` | Drop every cached evaluation and assignment (the offline store is kept) |
| `clearOfflineCache(): Future<void>` | Remove everything the SDK persisted to the offline store |
| `close(): Future<void>` | Clear the cache and close the HTTP connection pool |

Lookup order for `evaluateFlag` / `getAssignment`:

1. **In-memory cache** — per user + key, expires after `cacheTtl`.
2. **API call** — the server decides; the result is cached and, with `offlineFallback: true`,
   written to the `OfflineStore`.
3. **Offline store** — on a network error or non-404 HTTP error, the last persisted result.
4. **Safe default** — `EvalResult.disabled(key)` / `null`.

`OfflineStore` is an interface (`init`, `setFlag`/`getFlag`/`removeFlag`,
`setAssignment`/`getAssignment`/`removeAssignment`, `clear`). `SharedPreferencesOfflineStore`
persists JSON under `ep_sdk_flag_`/`ep_sdk_asgn_` keys across app launches;
`InMemoryOfflineStore` (the default) lasts for the process. The Dart VM is single-isolate, so the
client needs no locking; use one client per isolate.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFlag`, `isEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `getAssignment`, `getVariant` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `track` with a key, `trackEvent` | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

These SDK paths share a per-IP rate-limit ceiling of `SDK_RATE_LIMIT_PER_MINUTE` requests
(default 6000) on the backend; a `429` is surfaced as a failure (disabled flag / `null` / `false`).

---

## Error Handling

The public methods never throw on network or HTTP errors. The single exception is calling any
method before `init()`, which throws `StateError` (a programming error). Only `ApiHttpClient`
throws:

| Exception | When |
|-----------|------|
| `ApiException` (`statusCode`, `message`, `body`, `isNotFound`) | Non-2xx responses (`statusCode` 401/404/422/429/5xx) and network errors or timeouts (`statusCode == 0`) |

---

## Consistent Hash Utility

`hashUser(String userId, String key): double` implements the cross-SDK formula —
`MD5("{userId}:{key}")`, first 4 bytes as little-endian uint32, divided by 2^32 — and is pinned by
the golden-vector tests in `tests/sdk-contract/`:

```dart
import 'package:experimently/experimently.dart';

hashUser('user-123', 'my-flag');   // 0.6927449859213084
```

It is exported as a utility only. Since assignment moved to the server, nothing in the SDK uses it
to decide a variant.

---

## Flutter Widget Integration

```dart
class FeatureFlagWidget extends StatefulWidget {
  const FeatureFlagWidget({super.key, required this.client});
  final ExperimentationClient client;

  @override
  State<FeatureFlagWidget> createState() => _FeatureFlagWidgetState();
}

class _FeatureFlagWidgetState extends State<FeatureFlagWidget> {
  bool _enabled = false;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _evaluate();
  }

  Future<void> _evaluate() async {
    final flag = await widget.client.evaluateFlag('new-ui', 'user-123');   // never throws
    if (mounted) setState(() { _enabled = flag.enabled; _loading = false; });
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const CircularProgressIndicator();
    return Text(_enabled ? 'New UI' : 'Old UI');
  }
}
```

A complete example app is in `sdk/flutter/example/lib/main.dart`.

---

## Testing your own code

Inject a `package:http` `MockClient` (no code generation needed); the SDK's own tests in
`sdk/flutter/test/client_test.dart` use this pattern to record requests and serve canned responses:

```dart
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:experimently/experimently_core.dart';

final apiHttp = ApiHttpClient(
  apiKey: 'test',
  baseUrl: 'https://example.com',
  timeout: const Duration(seconds: 2),
  httpClient: MockClient((request) async =>
      http.Response('{"key":"new-ui","enabled":true,"config":null}', 200)),
);
final client = ExperimentationClient(
  config: const SdkConfig(apiKey: 'test', baseUrl: 'https://example.com', offlineFallback: false),
  httpClient: apiHttp,
);
await client.init();
expect(await client.isEnabled('new-ui', 'user-1'), isTrue);
```

---

## Contract smoke

Runs the four contract steps (sticky assignment, flag evaluation, keyed track, key-less fan-out
plus a 2-event batch) against a live backend and prints one JSON line:

```bash
cd sdk/flutter && flutter pub get
EXPERIMENTLY_API_KEY=<key> dart run example/contract_smoke.dart
```

It prints:

```text
{"sdk":"flutter","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

The live runner (`tests/sdk-contract/live/run_live_contract.py`) invokes exactly
`cd sdk/flutter && dart run example/contract_smoke.dart`. Env: `EXPERIMENTLY_API_URL` (default
`http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required), `CONTRACT_EXPERIMENT_KEY` (default
`sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default `sdk_contract_flag`), `CONTRACT_USER_ID` (default
random `smoke-<uuid>`). The smoke imports only `experimently_core.dart`, so no
`package:flutter` code is loaded — but the package depends on the `flutter` SDK, so dependency
resolution needs Flutter's bundled `dart` (run `flutter pub get` first so `dart run` prints
nothing but the JSON line).

CI does not run this SDK's contract smoke against a real API: `flutter` is not in the
`sdks:` list of the SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).
Run `python tests/sdk-contract/live/run_live_contract.py --sdk flutter --strict`
on a machine with the Flutter SDK.

---

## Development

```bash
cd sdk/flutter
flutter pub get
flutter test
flutter analyze
```

- `flutter test`: client (MockClient), hash golden vectors, SharedPreferences store

CI runs `flutter pub get` and `flutter test` on Flutter 3.19.6 in the SDK Unit Tests workflow
([sdk-unit-tests.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/sdk-unit-tests.yml)), daily and on a pull request that changes `sdk/flutter/`. No workflow runs
`flutter analyze`.
