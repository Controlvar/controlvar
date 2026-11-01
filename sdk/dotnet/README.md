# Controlvar.SDK

**Beta.** This is a 0.x release: pin the exact version you test against.

The .NET/C# SDK for Controlvar: feature flag evaluation, experiment assignment and analytics
event tracking.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

Full documentation: [the .NET SDK page](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/dotnet.md).

**Target frameworks**: `net8.0` and `netstandard2.1`
**Package ID**: `Controlvar.SDK`
**Dependencies**: none beyond `System.Text.Json` (part of .NET on `net8.0`; System.Text.Json 8.0.6 from NuGet on `netstandard2.1`)

---

## Install

To install it, follow [the .NET SDK page](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/dotnet.md#installation).

.NET 8 and later use the `net8.0` build, and .NET Core 3.0 to .NET 7 use the `netstandard2.1`
build. On .NET Core 3.0 to .NET 5, System.Text.Json 8 and its dependency System.Text.Encodings.Web
warn at build time that they do not support that target framework and have not been tested with
it. .NET Framework cannot use this package: it does not implement .NET Standard 2.1.

---

## Quick Start

```csharp
using Controlvar;

var config = new SdkConfig(
    Environment.GetEnvironmentVariable("CONTROLVAR_API_URL") ?? "http://localhost:8000",
    Environment.GetEnvironmentVariable("CONTROLVAR_API_KEY")!);
using var client = new ExperimentationClient(config);

// Feature flag — GET /api/v1/feature-flags/evaluate/{key}?user_id=…
var flag = await client.EvaluateFlagAsync("new-checkout-ui", "user-42");
if (flag.Enabled)
{
    // show the feature (flag.Config is always null: the evaluate endpoint returns config: null)
}

// Experiment — POST /api/v1/tracking/assign (sticky on the server, records the exposure)
var assignment = await client.GetAssignmentAsync("checkout-cta-test", "user-42",
    new Dictionary<string, object> { ["plan"] = "pro", ["country"] = "US" });
var variant = assignment?.VariantName ?? "control";                // null on failure

// Events — never throw
await client.TrackAsync("purchase", "user-42",
    properties: new Dictionary<string, object> { ["sku"] = "pro" },
    experimentKey: "checkout-cta-test", value: 49.99);            // one POST /api/v1/tracking/track
await client.TrackAsync("page_view", "user-42");                   // no key: fanned out (see below)
```

---

## Configuration

```csharp
var config = new SdkConfig("http://localhost:8000", "your-api-key")   // origin only; the SDK appends /api/v1/...
{
    CacheTtlSeconds = 300,   // seconds a successful evaluation / assignment is reused (default 300)
    TimeoutSeconds  = 10,    // HTTP timeout for the SDK-created HttpClient (default 10)
    MaxCacheSize    = 1000   // max entries per cache (evaluations, assignments); oldest evicted (default 1000)
};
```

`SdkConfig` throws `ArgumentException` for an empty `baseUrl`/`apiKey` and trims a trailing `/`.

You can inject your own `HttpClient` (connection pooling, `IHttpClientFactory`, tests):

```csharp
using var client = new ExperimentationClient(config, httpClient);   // never mutated; its own Timeout applies
```

---

## API Reference

### `ExperimentationClient`

| Method | Backend call | Returns |
|--------|--------------|---------|
| `EvaluateFlagAsync(flagKey, userId, attributes?)` | `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…` (the user id alone; `attributes` are not sent) | `FlagEvaluationResult { Key, Enabled, Config }`. Never throws; disabled on failure. |
| `GetAssignmentAsync(experimentKey, userId, attributes?)` | `POST /api/v1/tracking/assign` (`attributes` sent as `context`) | `Assignment? { ExperimentKey, UserId, VariantId, VariantName, IsControl, Configuration }`. Never throws; `null` on failure. |
| `TrackAsync(eventName, userId, properties?, experimentKey?, featureFlagKey?, value?, eventType?, timestamp?)` | `POST /api/v1/tracking/track` with a key, else `POST /api/v1/tracking/batch` fan-out | `bool` — `false` on any error. Never throws. |
| `TrackAsync(TrackEvent)` | same rules | `bool` |
| `TrackBatchAsync(IEnumerable<TrackEvent>)` | `POST /api/v1/tracking/batch`, ≤ 100 events per request | `bool` — `false` if any chunk failed. Never throws. |
| `GetAssignments(userId)` | — | Cached (unexpired) assignments of the user |
| `GetEvaluatedFlags(userId)` | — | Keys of flags cached for the user |
| `ClearCache()` | — | Drops both caches |
| `Dispose()` | — | Releases the SDK-created `HttpClient` |

`Config` and `Configuration` are `System.Text.Json.JsonElement?`. `Configuration` is the raw JSON
the server returned for the variant (`null` when absent); `Config` is always `null`, because the
evaluate endpoint returns `config: null` for every flag; a variant's configuration reads as `assignment.Configuration?.GetProperty("headline").GetString()`.

### Caching and failures

Successful evaluations and assignments are cached per **user + key** for `CacheTtlSeconds`;
failures (network error, 401, 404 for an unknown experiment or flag key or a draft experiment with no
control variant, 429, 5xx, undecodable body) are never cached, so the next call retries. On a failure the SDK returns a disabled
`FlagEvaluationResult` / `null` assignment. Both caches are guarded by locks; one client can be
shared across threads.

### Track fan-out rule

With `experimentKey` and/or `featureFlagKey` the SDK sends one `POST /api/v1/tracking/track`.
Without a key it sends one `POST /api/v1/tracking/batch` containing one entry per experiment the
user has been assigned to through this client plus one per flag evaluated for the user (from the
cache). If nothing is cached, nothing is sent and `true` is returned. `event_type` defaults to
the event name; `properties` are sent as `metadata`; `timestamp` is sent as ISO-8601 UTC when set.

This SDK reads only the variant from an answer: an `assigned: false` answer (a user the
experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as the
control variant, and an event tracked without a key after the user's cache entries have expired
(`CacheTtlSeconds`, 300 by default) is not sent, so pass `experimentKey` on a conversion that can
come later.

### `FeatureFlagEvaluator` (static)

| Method | Description |
|--------|-------------|
| `HashUser(userId, flagKey)` | Cross-SDK MD5 bucket in `[0.0, 1.0)`. Utility only — nothing in the SDK uses it to decide a variant. |

---

## Backend endpoints used

Every request carries `X-API-Key` and `Accept: application/json`; POSTs carry
`Content-Type: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `EvaluateFlagAsync` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `GetAssignmentAsync` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `TrackAsync` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `TrackAsync` without keys, `TrackBatchAsync` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored (status only) |

---

## Error Handling

The public `ExperimentationClient` methods never throw on network or HTTP errors. Only
`ExperimentationHttpClient` (the low-level wrapper) throws:

| Scenario | Exception |
|----------|-----------|
| HTTP 401 | `AuthException` |
| Other 4xx / 5xx (404 unknown key, 422 validation, 429 rate limited) or undecodable body | `ApiException` (`.StatusCode`, `.ResponseBody`) |
| Network / timeout | `NetworkException` |

---

## Consistent Hashing

All SDKs share one hash formula; it is exported for debugging and parity tests only:

```
MD5("{userId}:{flagKey}") → first 4 bytes as little-endian uint32 → / 2^32
FeatureFlagEvaluator.HashUser("user-123", "my-flag") == 0.6927449859213084
```

---

## Development

In a clone of this repository, `sdk/dotnet/`. The tests target `net8.0` and `net10.0`, so they
need the .NET 10 SDK and the .NET 8 runtime.

### Running Tests

```bash
cd sdk/dotnet
dotnet test
dotnet test --filter "FullyQualifiedName~HashCompatibilityTests"
```

- `dotnet test`: whole solution, in three legs: `tests/Controlvar.Tests` on `net8.0` and on
  `net10.0` (against the SDK's `net8.0` build), and `tests/Controlvar.Tests.NetStandard` on
  `net8.0` against the SDK's `netstandard2.1` build. The three run the same tests.

**Test coverage** (CI runs `dotnet test` on the solution):

| Test class | Coverage |
|------------|----------|
| `ExperimentationClientTests` | Request URL/method/headers/body per endpoint, response mapping, per-user cache + TTL, 404 → failure value, track fan-out + chunking, never-throws |
| `ExperimentationHttpClientTests` | URL building/encoding, headers, JSON body, 401/404/422/500 mapping, network errors |
| `CacheTests` | TTL, eviction, thread safety |
| `HashCompatibilityTests` | Cross-SDK hash vector |
| `TargetLegTests` | Which build of the SDK each leg loaded, and on which runtime |

All HTTP is faked with an `HttpMessageHandler` injected through `HttpClient`; no network access.

Standalone check of the hash without .NET: `python3 sdk/dotnet/verify_hash.py`.

### Contract smoke

Runs the four contract steps (sticky assignment, flag evaluation, keyed track, key-less fan-out
plus a 2-event batch) against a live backend and prints one JSON line:

```bash
CONTROLVAR_API_KEY=<key> dotnet run --project sdk/dotnet/examples/ContractSmoke
```

It prints:

```text
{"sdk":"dotnet","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<guid>`). On failure one line is
written to stderr and the exit code is 1.

CI runs this SDK's contract smoke against a real API: `dotnet` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml). Run it
yourself with `python tests/sdk-contract/live/run_live_contract.py --sdk dotnet --strict` on a
machine with the .NET 10 SDK and the .NET 8 runtime.

### Project Structure

```
sdk/dotnet/
  Controlvar.sln
  src/Controlvar/            # SDK source (netstandard2.1 + net8.0)
    SdkConfig.cs
    ExperimentationClient.cs              # public API (evaluate / assign / track / batch / cache helpers)
    ExperimentationHttpClient.cs          # X-API-Key, JSON, error mapping
    Cache.cs                              # SdkCache<T> + UserKeyCache<T> (per user + key, TTL)
    FeatureFlagEvaluator.cs               # HashUser utility
    IsExternalInit.cs                     # `init` accessor polyfill for netstandard2.1
    Models/                               # FlagEvaluationResult, Assignment, TrackEvent
    Exceptions/                           # ExperimentationException, NetworkException, ApiException, AuthException
  tests/Controlvar.Tests/    # xUnit (net8.0 + net10.0), fake HttpMessageHandler
  tests/Controlvar.Tests.NetStandard/     # the same tests against the netstandard2.1 build
  examples/BasicUsage/                    # Walk-through app (net8.0)
  examples/ContractSmoke/                 # Contract smoke (see above)
  verify_hash.py                          # Cross-SDK hash parity script
```

---

## License

MIT.
