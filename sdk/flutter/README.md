# Experimently Flutter / Dart SDK

`experimently` (v0.2) provides feature flag evaluation, A/B experiment assignment and event
tracking for Flutter applications (iOS, Android, Web, macOS, Windows, Linux) and, through the
Flutter-free `experimently_core.dart` entry point, for plain Dart programs.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed on the device.

> **Flag evaluation sends only the user id.** `GET /api/v1/feature-flags/evaluate/{key}?user_id=…`
> carries none of the user's attributes, so the server evaluates a flag's targeting rules against
> an empty context. Experiment assignment sends them: `getAssignment` sends the attributes as
> `context`. To evaluate a flag with attributes, call the evaluate endpoint with a `context`
> query parameter (a URL-encoded JSON object of the attributes).

- **Server-decided** — `GET /api/v1/feature-flags/evaluate/{key}` and `POST /api/v1/tracking/assign`
- **Per-user TTL cache** — successful results are reused for `cacheTtl` (default 5 minutes);
  failures are never cached; concurrent calls for the same user + key share one request
- **Offline fallback** — the last known server result is persisted (SharedPreferences in Flutter,
  in-memory elsewhere) and served when the API is unreachable
- **Never throws** — `evaluateFlag`, `getAssignment`, `track` and `trackBatch` degrade gracefully
- **Null-safe Dart 2.17+**, `http` + `crypto` + `shared_preferences` only

## Installation

**Not yet published.** The `experimently` package is not on pub.dev, so build it from source: this
dependency takes `sdk/flutter` from this repository.

```yaml
dependencies:
  experimently:
    git:
      url: https://github.com/getexperimently/experimently
      path: sdk/flutter
```

```bash
flutter pub get
```

## Quick Start

```dart
import 'package:experimently/experimently.dart';

final client = ExperimentationClient(
  config: const SdkConfig(
    apiKey: 'your-api-key',              // sent as X-API-Key
    baseUrl: 'http://localhost:8000',    // origin only; the SDK appends /api/v1/...
  ),
  offlineStore: SharedPreferencesOfflineStore(),   // Flutter apps; omit for plain Dart
);
await client.init();   // opens the offline store; makes no network request

// Feature flag (GET /api/v1/feature-flags/evaluate/{key}?user_id=…)
final flag = await client.evaluateFlag('dark-mode', 'user-123');
flag.enabled;   // bool — false on any failure with nothing cached
flag.config;    // always null: the evaluate endpoint returns config: null
await client.isEnabled('dark-mode', 'user-123');   // bool shorthand

// Experiment assignment (POST /api/v1/tracking/assign — sticky on the server)
final assignment = await client.getAssignment('checkout-flow', 'user-123',
    attributes: {'country': 'US', 'plan': 'pro'});
if (assignment != null) {
  assignment.variantName;    // 'control' / 'treatment'
  assignment.isControl;      // true for the control variant
  assignment.configuration;  // Map<String, dynamic>? — the variant's configuration
}

// Events (never throw)
await client.track('purchase', 'user-123', experimentKey: 'checkout-flow', value: 49.0);
await client.track('page_view', 'user-123', properties: {'page': '/'});   // no key: fanned out

await client.close();
```

## Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `apiKey` | `String` | *(required)* | API key sent as `X-API-Key` |
| `baseUrl` | `String` | *(required)* | Backend origin, e.g. `https://api.example.com` |
| `timeout` | `Duration` | `5s` | Per-request HTTP timeout |
| `cacheTtl` | `Duration` | `5 min` | How long a successful evaluation/assignment is reused |
| `offlineFallback` | `bool` | `true` | Persist successful results to the `OfflineStore` and serve them when the API fails |

`ExperimentationClient` also takes an optional `httpClient` (`ApiHttpClient`, which itself accepts
a `package:http` `Client` — inject `MockClient` in tests) and an `offlineStore`
(`SharedPreferencesOfflineStore` for Flutter, `InMemoryOfflineStore` by default).

## API

| Method | Returns | Failure value |
|--------|---------|---------------|
| `evaluateFlag(flagKey, userId)` | `EvalResult{key, enabled, config}` | cached/offline value, else `EvalResult.disabled(key)` |
| `isEnabled(flagKey, userId)` | `bool` | `false` |
| `getAssignment(experimentKey, userId, {attributes})` | `Assignment?{experimentKey, userId, variantId, variantName, isControl, configuration}` | cached/offline value, else `null` |
| `getVariant(experimentKey, userId)` | `String?` (`variantName`) | `null` |
| `track(eventName, userId, {properties, experimentKey, featureFlagKey, value, eventType, timestamp})` | `bool` | `false` |
| `trackEvent(TrackEvent)` / `trackBatch(List<TrackEvent>)` | `bool` | `false` |
| `getAssignments(userId)` / `getEvaluatedFlags(userId)` | cached assignments / flag keys | — |
| `clearCache()` / `clearOfflineCache()` / `close()` | — | — |

A 404 (an experiment unknown or not ACTIVE, or an unknown flag key) is a definitive answer: it returns the failure
value and also removes any persisted offline entry, so a stale value is never served for it.
`evaluateFlag`, `getAssignment`, `trackEvent` and `trackBatch` (and `isEnabled`, `getVariant` and
`track`, which call them) throw `StateError` when called before `init()` — that is the only
exception the public API raises.

**Fan-out rule.** With `experimentKey` and/or `featureFlagKey`, `track` sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch`
containing one entry per experiment the user was assigned to through this client plus one per
flag evaluated for the user (from the in-memory cache). If nothing is cached, nothing is sent.
`event_type` defaults to the event name; `properties` is sent as `metadata`. `trackBatch`
chunks requests at 100 events.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFlag`, `isEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `getAssignment`, `getVariant` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

## Hash Utility

`hashUser(userId, key)` implements the cross-SDK formula `MD5("{userId}:{key}")` → first 4 bytes
as little-endian uint32 / 2^32 and is pinned by the golden-vector tests in `tests/sdk-contract/`:

```dart
hashUser('user-123', 'my-flag');   // 0.6927449859213084
```

It is exported as a utility only — nothing in the SDK uses it to decide a variant.

## Contract smoke

```bash
cd sdk/flutter && flutter pub get
EXPERIMENTLY_API_KEY=<key> dart run example/contract_smoke.dart
```

It prints:

```text
{"sdk":"flutter","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The smoke imports only
`experimently_core.dart` (no `package:flutter` code is loaded), but dependency resolution
still needs the Flutter SDK because the package depends on `flutter`/`shared_preferences`, so use
Flutter's bundled `dart` and run `flutter pub get` first.

CI does not run this SDK's contract smoke against a real API: `flutter` is not in the
`sdks:` list of the SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml). Run
`python tests/sdk-contract/live/run_live_contract.py --sdk flutter --strict` on a machine with the
Flutter SDK.

## Running Tests

```bash
cd sdk/flutter
flutter pub get
flutter test
```

- `flutter test`: the client (HTTP faked with package:http MockClient), hash vectors, SharedPreferences store

CI runs `flutter pub get` and `flutter test` on Flutter 3.19.6 in the SDK Unit Tests workflow. No
CI job runs the contract smoke; run it on a machine with the Flutter SDK.
