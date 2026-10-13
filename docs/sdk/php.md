# PHP SDK

`experimently/sdk` provides feature flag evaluation, experiment
assignment and event tracking for PHP 8.1+ applications. It depends only on `ext-curl` and
`ext-json`.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

Source: `sdk/php`.

---

## Requirements

- PHP >= 8.1
- `ext-json`, `ext-curl`
- Composer for installation and PHPUnit only — the SDK has no runtime dependencies

---

## Installation

**Not yet published.** `experimently/sdk` is not on Packagist, so build it from source: point
Composer at `sdk/php` in a clone of this repository with a path repository (this has not been
tested here).

```bash
git clone https://github.com/getexperimently/experimently.git
```

Then, in your project, add the path repository and require the package from it:

```bash
composer config repositories.experimently path /path/to/experimently/sdk/php
composer require experimently/sdk:@dev
```

Without composer, `require` the files under `sdk/php/src/` directly (`examples/contract_smoke.php`
shows the order).

---

## Quick Start

```php
<?php

use Experimently\ExperimentationClient;
use Experimently\SdkConfig;

$client = new ExperimentationClient(new SdkConfig(
    baseUrl: getenv('EXPERIMENTLY_API_URL') ?: 'http://localhost:8000',
    apiKey:  getenv('EXPERIMENTLY_API_KEY'),
));

if ($client->isFeatureEnabled('new-checkout', 'user-123')) {
    showNewCheckout();
}

$assignment = $client->getAssignment('checkout-cta-copy', 'user-123', ['plan' => 'pro']);
$headline   = $assignment?->configuration['headline'] ?? 'Buy now';

$client->track('purchase', 'user-123', ['sku' => 'pro-plan'], 'checkout-cta-copy', null, 99.99);
```

---

## Configuration

```php
$config = new SdkConfig(
    baseUrl: 'http://localhost:8000',  // Required — origin only; the SDK appends /api/v1/...
    apiKey: 'your-api-key',            // Required — sent as X-API-Key
    cacheTtl: 300,                     // Seconds a successful result is reused (default 300)
    timeout: 10,                       // HTTP connect/request timeout in seconds (default 10)
    maxCacheSize: 1000,                // Maximum cached entries, oldest evicted (default 1000)
);
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `baseUrl` | `string` | *(required)* | Backend origin, e.g. `https://api.example.com` |
| `apiKey` | `string` | *(required)* | API key sent as `X-API-Key` |
| `cacheTtl` | `int` | `300` | How long a successful evaluation/assignment is reused (seconds) |
| `timeout` | `int` | `10` | cURL connect and total timeout (seconds) |
| `maxCacheSize` | `int` | `1000` | Maximum cache entries |

`SdkConfig` validates its arguments and throws `InvalidArgumentException` for an empty
`baseUrl`/`apiKey` or a non-positive timeout/size.

---

## Feature Flag Evaluation

### `evaluateFlag(string $flagKey, string $userId): FlagEvaluation`

Calls `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…` and returns a `FlagEvaluation`.
The request carries the user id alone, so the server evaluates the flag's targeting rules against
an empty context.

| Property | Type | Description |
|----------|------|-------------|
| `key` | `string` | The flag key you asked for |
| `enabled` | `bool` | Server decision for this user (`false` on any failure) |
| `config` | `mixed`, `null` | Always `null`: the evaluate endpoint returns `config: null` for every flag |

```php
$flag = $client->evaluateFlag('dark-mode', 'user-456');
if ($flag->enabled) {
    enableDarkMode();
}
```

A flag that is not ACTIVE is not a failure: it answers `enabled: false`. A cached evaluation is
returned before any request is made; on a network/HTTP failure (a 404 is an unknown flag key) the
flag is reported as disabled. Failures are never cached, so the next call retries. `FlagEvaluation::toArray()` gives `['key', 'enabled', 'config']`.

### `isFeatureEnabled(string $flagKey, string $userId): bool`

Shorthand for `evaluateFlag(...)->enabled`.

---

## Experiment Assignment

### `getAssignment(string $experimentKey, string $userId, array $attributes = []): ?Assignment`

Calls `POST /api/v1/tracking/assign` with `{experiment_key, user_id, context: $attributes}`. The
server buckets the user, keeps the assignment sticky and records the exposure.

| Property | Type | Description |
|----------|------|-------------|
| `experimentKey` | `string` | The experiment key |
| `variantId` | `?string` | UUID of the assigned variant |
| `variantName` | `string` | Assigned variant name (e.g. `"control"`, `"treatment"`) |
| `isControl` | `bool` | `true` for the control variant |
| `configuration` | `?array` | The variant's `configuration` JSON from the experiment definition |

```php
$assignment = $client->getAssignment('checkout-cta-copy', 'user-123', ['plan' => 'pro', 'country' => 'US']);

match ($assignment?->variantName) {
    'treatment-a' => renderShortCta(),
    'treatment-b' => renderUrgencyCta(),
    default       => renderOriginalCta(),   // control, or null on failure
};
```

Returns `null` on any failure (network error, 401, 404 when the experiment is not ACTIVE); a
cached assignment is returned when one exists. Failures are never cached. `Assignment::toArray()`
uses the API field names (`experiment_key`, `variant_id`, ...).

---

## Event Tracking

### `track(string $eventName, string $userId, array $properties = [], ?string $experimentKey = null, ?string $featureFlagKey = null, ?float $value = null, ?string $eventType = null): bool`

Never throws. Returns `true` when every request succeeded, `false` otherwise.

```php
$client->track('purchase', 'user-123', ['sku' => 'pro-plan'], 'checkout-cta-copy', null, 99.99);
$client->track('search', 'user-123', ['q' => 'shoes'], null, 'new-search');
$client->track('page_view', 'user-123', ['page' => '/products']);   // no key: fanned out
```

**Fan-out rule.** With `$experimentKey` and/or `$featureFlagKey` the SDK sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch`
containing one entry per experiment the user has been assigned to through this client plus one
per flag evaluated for the user (from the cache). If nothing is cached, nothing is sent. This is
what makes a single `track('purchase', …)` count as a conversion for every experiment the user is
in.

Conversions are matched to metrics by **event name**: an experiment metric whose `event_name` is
`purchase` counts every `purchase` event, whatever `event_type` was sent. `$properties` is sent as
`metadata`; `$eventType` defaults to `$eventName`; the timestamp is now (UTC, ISO-8601).

Because PHP's cache lives for one request/process, the key-less fan-out only sees assignments and
flags evaluated earlier in the **same** request. Pass the key explicitly when tracking from a
different request (for example a webhook or a queue worker).

### `trackBatch(array $events): BatchResult`

Sends up to 100 events per `POST /api/v1/tracking/batch` (longer lists are chunked). Each event
is an array with `event_name` and `user_id`; optional `experiment_key`, `feature_flag_key`,
`properties`, `value`, `event_type`, `timestamp` (ISO-8601 string or `DateTimeInterface`). An
event with no key is stored as history and counts in no experiment's results; tag outcome events.

```php
$result = $client->trackBatch([
    ['event_name' => 'purchase', 'user_id' => 'user-123', 'experiment_key' => 'checkout-cta-copy', 'value' => 99.99],
    ['event_name' => 'search',   'user_id' => 'user-123', 'feature_flag_key' => 'new-search'],
]);
$result->isOk();          // true when failureCount === 0
$result->successCount;    // 2
$result->errors;          // null, or a list of error arrays
```

Malformed events are counted as failures without being sent. Never throws.

---

## Cache helpers

| Method | Description |
|--------|-------------|
| `getAssignments(string $userId): Assignment[]` | Cached (successful, unexpired) assignments for the user |
| `getEvaluatedFlags(string $userId): string[]` | Keys of flags successfully evaluated (and still cached) for the user |
| `clearCache(): void` | Drop every cached evaluation and assignment |

The cache is an in-memory array scoped to the `ExperimentationClient` instance and lives as long
as the instance does, so it is reused across requests only where one instance serves several of
them.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFlag`, `isFeatureEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `getAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | `{success_count, failure_count, errors}` |

These SDK paths share a per-IP rate-limit ceiling of `SDK_RATE_LIMIT_PER_MINUTE` requests
(default 6000) on the backend; a `429` is surfaced as a failure (disabled flag / `null` / `false`).

---

## Error Handling

The public methods never throw on network or HTTP errors. Only `HttpClient` throws:

| Exception | Extends | When |
|-----------|---------|------|
| `Experimently\Errors\ExperimentationException` | `\RuntimeException` | Base class |
| `Experimently\Errors\NetworkException` | `ExperimentationException` | cURL transport errors, timeouts |
| `Experimently\Errors\ApiException` (`getStatusCode()`) | `ExperimentationException` | Other 4xx/5xx (404: an experiment unknown or not ACTIVE, or an unknown flag key; 422 validation; 429 rate limited) |
| `Experimently\Errors\AuthException` | `ApiException` | HTTP 401 — invalid API key |

---

## Consistent Hash Utility

`FeatureFlagEvaluator::hashUser(string $userId, string $key): float` implements the cross-SDK
formula — `MD5("{userId}:{key}")`, first 4 bytes as little-endian uint32, divided by 2^32. The
SDK's own `HashCompatibilityTest` pins one vector; the cross-SDK golden vectors in
`tests/sdk-contract/` are not run against this SDK:

```php
FeatureFlagEvaluator::hashUser('user-123', 'my-flag');   // 0.6927449859213084
```

It is exported as a utility only. Since assignment moved to the server, nothing in the SDK uses it
to decide a variant.

---

## Laravel Integration

```php
// app/Providers/ExperimentlyServiceProvider.php
use Experimently\ExperimentationClient;
use Experimently\SdkConfig;

public function register(): void
{
    $this->app->singleton(ExperimentationClient::class, fn () => new ExperimentationClient(new SdkConfig(
        baseUrl: config('services.experimently.url'),
        apiKey: config('services.experimently.key'),
        cacheTtl: 60,
    )));
}
```

```php
// In a controller
public function show(ExperimentationClient $experiments, Request $request)
{
    $userId  = (string) ($request->user()?->id ?? $request->cookie('visitor_id'));
    $variant = $experiments->getAssignment('checkout-cta-copy', $userId, ['plan' => $request->user()?->plan])
        ?->variantName ?? 'control';

    return view('checkout', ['variant' => $variant]);
}
```

---

## Testing your own code

Inject a fake `HttpClient` (the SDK's own tests use `sdk/php/tests/FakeHttpClient.php`, which
records requests and serves canned responses), or a PHPUnit mock of `ExperimentationClient`:

```php
$http = new FakeHttpClient();
$http->on('GET', '/api/v1/feature-flags/evaluate/new-checkout?user_id=user-1',
          ['key' => 'new-checkout', 'enabled' => true, 'config' => null]);

$client = new ExperimentationClient($config, $http);
self::assertTrue($client->isFeatureEnabled('new-checkout', 'user-1'));
self::assertSame('GET', $http->requests[0]['method']);
```

---

## Contract smoke

Runs the four contract steps (sticky assignment, flag evaluation, keyed track, key-less fan-out
plus a 2-event batch) against a live backend and prints one JSON line:

```bash
EXPERIMENTLY_API_KEY=<key> php sdk/php/examples/contract_smoke.php
```

It prints:

```json
{"sdk":"php","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). No `composer install`
is needed — the script `require`s `src/` directly when `vendor/` is absent.

CI runs this SDK's contract smoke against a real API: `php` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Development

`./vendor/bin/phpunit` runs the PHPUnit 10 suite (client, cache, HTTP client and hash tests), with
HTTP faked. `php test_standalone.php` needs no Composer: it checks the hash vector, the types, the
cache and the client's request shapes.

```bash
cd sdk/php
composer install
./vendor/bin/phpunit
php test_standalone.php
```

CI runs `composer install` and `vendor/bin/phpunit` on PHP 8.2 in the SDK Unit Tests
workflow ([sdk-unit-tests.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/sdk-unit-tests.yml)), daily and on a pull request that changes `sdk/php/`. No workflow runs
`php test_standalone.php`.
