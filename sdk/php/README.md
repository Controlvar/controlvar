# Controlvar PHP SDK

PHP 8.1+ SDK for the Controlvar A/B testing and feature flag platform.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for the current request/process. Nothing is bucketed
locally.

## Requirements

- PHP >= 8.1 (readonly properties)
- `ext-json`, `ext-curl` (built-in)
- Composer only for installation and PHPUnit — the SDK itself has no dependencies

## Installation

**Not yet published.** `controlvar/sdk` is not on Packagist, so build it from source: point
Composer at `sdk/php` in a clone of this repository with a path repository (this has not been
tested here).

```bash
git clone https://github.com/Controlvar/controlvar.git
```

Then, in your project, add the path repository and require the package from it:

```bash
composer config repositories.controlvar path /path/to/controlvar/sdk/php
composer require controlvar/sdk:@dev
```

Without composer, `require` the files under `src/` directly (see `examples/contract_smoke.php`).

## Quick Start

```php
use Controlvar\ExperimentationClient;
use Controlvar\SdkConfig;

$client = new ExperimentationClient(new SdkConfig(
    baseUrl: getenv('CONTROLVAR_API_URL') ?: 'http://localhost:8000',  // origin only
    apiKey:  getenv('CONTROLVAR_API_KEY'),                             // sent as X-API-Key
));

// Feature flag (GET /api/v1/feature-flags/evaluate/{key}?user_id=…)
$flag = $client->evaluateFlag('dark-mode', 'user-123');
$flag->enabled;   // bool
$flag->config;    // always null: the evaluate endpoint returns config: null
$client->isFeatureEnabled('dark-mode', 'user-123');   // bool

// Experiment assignment (POST /api/v1/tracking/assign — sticky on the server)
$assignment = $client->getAssignment('checkout-flow', 'user-123', ['country' => 'US']);
if ($assignment !== null) {
    $assignment->variantName;     // "control" / "treatment"
    $assignment->isControl;       // bool
    $assignment->configuration;   // array|null
}

// Events (never throw)
$client->track('purchase', 'user-123', ['sku' => 'pro'], 'checkout-flow', null, 49.0);
$client->track('page_view', 'user-123', ['page' => '/']);   // no key: fanned out, see below
```

## Configuration

| Parameter      | Type   | Default | Description                                                     |
|----------------|--------|---------|-----------------------------------------------------------------|
| `baseUrl`      | string | —       | Backend origin (required); the SDK appends `/api/v1/...`        |
| `apiKey`       | string | —       | API key (required), sent as `X-API-Key`                          |
| `cacheTtl`     | int    | 300     | Seconds a successful evaluation/assignment is reused             |
| `timeout`      | int    | 10      | HTTP connect/request timeout in seconds                          |
| `maxCacheSize` | int    | 1000    | Maximum cached entries (oldest evicted)                          |

## API

### `evaluateFlag(string $flagKey, string $userId): FlagEvaluation`

`GET /api/v1/feature-flags/evaluate/{flagKey}?user_id={userId}`: the user id alone, so the server
evaluates the flag's targeting rules against an empty context. Returns a `FlagEvaluation` with
`key`, `enabled` and `config`. A cached evaluation is returned before any request is made; on any
network/HTTP failure (transport error, 401, 404 for an unknown flag key, 429, ...) the flag is
reported as **disabled** (`config: null`). Failures are never cached. Never throws on
network/HTTP errors.

### `isFeatureEnabled(string $flagKey, string $userId): bool`

`evaluateFlag(...)->enabled`.

### `getAssignment(string $experimentKey, string $userId, array $attributes = []): ?Assignment`

`POST /api/v1/tracking/assign` with `{experiment_key, user_id, context: $attributes}`. The server
buckets the user, keeps the assignment sticky and records the exposure. Returns an `Assignment`
with `experimentKey`, `variantId`, `variantName`, `isControl` and `configuration`, or **`null`**
on any network/HTTP failure (including 404 when the experiment is not ACTIVE); a cached
assignment, when present, is returned instead. Failures are never cached. Never throws on
network/HTTP errors.

### `track(string $eventName, string $userId, array $properties = [], ?string $experimentKey = null, ?string $featureFlagKey = null, ?float $value = null, ?string $eventType = null): bool`

Never throws; returns `true` when every request succeeded.

- With `$experimentKey` and/or `$featureFlagKey`: one `POST /api/v1/tracking/track`.
- Without a key: one `POST /api/v1/tracking/batch` containing one entry per experiment the user
  has been assigned to through this client plus one per flag evaluated for the user (both from the
  cache). If nothing is cached for the user, nothing is sent and `true` is returned.

`$properties` is sent as `metadata`; `$eventType` defaults to `$eventName`; the timestamp is now
(UTC).

### `trackBatch(array $events): BatchResult`

`POST /api/v1/tracking/batch` in chunks of at most 100 events. Each event is an array with
`event_name` and `user_id`; optional `experiment_key`, `feature_flag_key`, `properties`, `value`,
`event_type`, `timestamp`. An event with no key is stored as history and counts in no experiment's
results; tag outcome events. Malformed
events are counted as failures without being sent. Returns a `BatchResult` (`successCount`,
`failureCount`, `errors`, `isOk()`). Never throws.

### Cache helpers

`getAssignments(string $userId): Assignment[]`, `getEvaluatedFlags(string $userId): string[]`,
`clearCache(): void`.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFlag`, `isFeatureEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `getAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | `{success_count, failure_count, errors}` |

## Error Handling

`evaluateFlag()`, `getAssignment()`, `track()` and `trackBatch()` never throw — they return a
disabled flag / `null` / `false` / a `BatchResult` with failures. Only `HttpClient` throws:

| Exception                  | Extends                    | When                                          |
|----------------------------|----------------------------|-----------------------------------------------|
| `ExperimentationException` | `\RuntimeException`        | Base class for all SDK errors                 |
| `NetworkException`         | `ExperimentationException` | cURL transport errors, timeouts               |
| `ApiException`             | `ExperimentationException` | HTTP 4xx/5xx (`getStatusCode()`)              |
| `AuthException`            | `ApiException`             | HTTP 401 — invalid API key                    |

## Hash Utility

`FeatureFlagEvaluator::hashUser($userId, $key)` implements the cross-SDK formula
`MD5("{userId}:{key}")` → first 4 bytes as little-endian uint32 / 2^32. The SDK's own
`HashCompatibilityTest` pins one vector; the cross-SDK golden vectors in `tests/sdk-contract/` are
not run against this SDK:

```php
FeatureFlagEvaluator::hashUser('user-123', 'my-flag');   // 0.6927449859213084
```

It is exported as a utility only — nothing in the SDK uses it to decide a variant.

## Contract smoke

```bash
CONTROLVAR_API_KEY=<key> php sdk/php/examples/contract_smoke.php
```

It prints one line:

```json
{"sdk":"php","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). Works without
`composer install` (it `require`s `src/` directly when `vendor/` is absent).

CI runs this SDK's contract smoke against a real API: `php` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml). Run it
yourself with `python tests/sdk-contract/live/run_live_contract.py --sdk php --strict` on a machine
with PHP 8.1+.

## Running Tests

`./vendor/bin/phpunit` runs the PHPUnit 10 suite, with HTTP faked by `tests/FakeHttpClient.php`:

```bash
cd sdk/php
composer install
./vendor/bin/phpunit
```

`php test_standalone.php` needs no Composer: it checks the hash vector, the types, the cache and
the client's request shapes:

```bash
php test_standalone.php
```

CI runs the PHPUnit suite (client, cache, HTTP client, hash tests) on PHP 8.2 in the SDK Unit Tests
workflow; no workflow runs `test_standalone.php`.

## License

MIT
