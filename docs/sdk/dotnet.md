# .NET SDK

`Experimently.SDK` (v0.2) provides feature flag evaluation, experiment assignment and
event tracking for .NET applications. It targets `netstandard2.1` and `net6.0`, uses
`System.Text.Json` and `HttpClient`, and has no other dependencies.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

Source: `sdk/dotnet`.

---

## Requirements

- .NET 6.0+ runtime, or any `netstandard2.1`-compatible runtime (.NET Core 3.0+, Mono 6.4+)
- .NET 6+ SDK to build, test or run the examples

---

## Installation

**Not yet published.** `Experimently.SDK` is not on NuGet yet, so the command below fails today.
Reference the project from a clone of this repository instead, as shown after it
(`dotnet add reference /path/to/experimently/sdk/dotnet/src/Experimently/Experimently.csproj`
adds the same line; this has not been tested here).

```bash
dotnet add package Experimently.SDK
```

Or reference the project directly from a checkout:

```xml
<ProjectReference Include="../../sdk/dotnet/src/Experimently/Experimently.csproj" />
```

---

## Quick Start

```csharp
using Experimently;

var config = new SdkConfig(
    baseUrl: Environment.GetEnvironmentVariable("EXPERIMENTLY_API_URL") ?? "http://localhost:8000",
    apiKey:  Environment.GetEnvironmentVariable("EXPERIMENTLY_API_KEY")!);

using var client = new ExperimentationClient(config);

var flag = await client.EvaluateFlagAsync("new-checkout", "user-123");
if (flag.Enabled)
{
    ShowNewCheckout();
}

var assignment = await client.GetAssignmentAsync("checkout-cta-copy", "user-123",
    new Dictionary<string, object> { ["plan"] = "pro" });
var headline = assignment?.Configuration?.GetProperty("headline").GetString() ?? "Buy now";

await client.TrackAsync("purchase", "user-123",
    properties: new Dictionary<string, object> { ["sku"] = "pro-plan" },
    experimentKey: "checkout-cta-copy", value: 99.99);
```

---

## Configuration

```csharp
var config = new SdkConfig("http://localhost:8000", "your-api-key")   // origin only; the SDK appends /api/v1/...
{
    CacheTtlSeconds = 300,   // Seconds a successful result is reused (default 300)
    TimeoutSeconds  = 10,    // Timeout of the SDK-created HttpClient (default 10)
    MaxCacheSize    = 1000,  // Maximum entries per cache, oldest evicted (default 1000)
};

using var client = new ExperimentationClient(config);
// or, with your own HttpClient (IHttpClientFactory, tests):
using var client2 = new ExperimentationClient(config, httpClient);
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `baseUrl` (ctor) | `string` | *(required)* | Backend origin, e.g. `https://api.example.com`; a trailing `/` is trimmed |
| `apiKey` (ctor) | `string` | *(required)* | API key sent as `X-API-Key` on every request |
| `CacheTtlSeconds` | `int` | `300` | How long a successful evaluation/assignment is reused per user + key |
| `TimeoutSeconds` | `int` | `10` | `HttpClient.Timeout` for the client the SDK creates (an injected `HttpClient` keeps its own) |
| `MaxCacheSize` | `int` | `1000` | Maximum entries in each of the two caches (evaluations, assignments) |

`SdkConfig` throws `ArgumentException` for an empty `baseUrl`/`apiKey`. `ExperimentationClient`
is thread-safe and meant to be shared (one instance per process). Dispose it to release the
`HttpClient` it created; an injected `HttpClient` is never mutated or disposed.

---

## Feature Flag Evaluation

### `Task<FlagEvaluationResult> EvaluateFlagAsync(string flagKey, string userId, Dictionary<string, object>? attributes = null)`

Calls `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…` (both values percent-encoded) and
returns a `FlagEvaluationResult`. `attributes` is accepted for source compatibility but not sent:
the evaluate endpoint takes a `context`, which this SDK does not send yet.

| Property | Type | Description |
|----------|------|-------------|
| `Key` | `string` | The flag key you asked for |
| `Enabled` | `bool` | Server decision for this user (`false` on any failure) |
| `Config` | `JsonElement?` | Always `null`: the evaluate endpoint returns `config: null` for every flag |

```csharp
var flag = await client.EvaluateFlagAsync("dark-mode", "user-456");
if (flag.Enabled)
{
    EnableDarkMode();
}
```

Never throws. A flag that is not ACTIVE is not a failure: it answers `Enabled: false`. On a
network/HTTP failure (a 404 is an unknown flag key) a cached evaluation is returned when one exists; otherwise a disabled result
(`FlagEvaluationResult.Disabled(flagKey)`). Failures are never cached, so the next call retries.

---

## Experiment Assignment

### `Task<Assignment?> GetAssignmentAsync(string experimentKey, string userId, Dictionary<string, object>? attributes = null)`

Calls `POST /api/v1/tracking/assign` with `{experiment_key, user_id, context: attributes}`. The
server buckets the user, keeps the assignment sticky and records the exposure. `context` is
omitted when `attributes` is `null`.

| Property | Type | Description |
|----------|------|-------------|
| `ExperimentKey` | `string` | The experiment key |
| `UserId` | `string` | The user that was assigned |
| `VariantId` | `string?` | UUID of the assigned variant |
| `VariantName` | `string` | Assigned variant name (e.g. `"control"`, `"treatment"`) |
| `IsControl` | `bool` | `true` for the control variant |
| `Configuration` | `JsonElement?` | The variant's `configuration` JSON from the experiment definition, `null` when absent |

```csharp
var assignment = await client.GetAssignmentAsync("checkout-cta-copy", "user-123",
    new Dictionary<string, object> { ["plan"] = "pro", ["country"] = "US" });

var view = assignment?.VariantName switch
{
    "treatment-a" => "Views/Checkout/ShortCta",
    "treatment-b" => "Views/Checkout/UrgencyCta",
    _             => "Views/Checkout/Original",   // control, or null on failure
};
```

Never throws. Returns `null` on any failure (network error, 401, 404 when the experiment is not
ACTIVE, 429); a cached assignment is returned when one exists. Failures are never cached.

---

## Event Tracking

### `Task<bool> TrackAsync(string eventName, string userId, Dictionary<string, object>? properties = null, string? experimentKey = null, string? featureFlagKey = null, double? value = null, string? eventType = null, DateTimeOffset? timestamp = null)`

Never throws. Returns `true` when every request succeeded (or nothing had to be sent), `false`
otherwise. A `TrackAsync(TrackEvent)` overload takes a prebuilt event with the same fields.

```csharp
await client.TrackAsync("purchase", "user-123", new Dictionary<string, object> { ["sku"] = "pro-plan" },
    experimentKey: "checkout-cta-copy", value: 99.99);
await client.TrackAsync("search", "user-123", featureFlagKey: "new-search");
await client.TrackAsync("page_view", "user-123", new Dictionary<string, object> { ["page"] = "/products" });   // no key: fanned out
```

**Fan-out rule.** With `experimentKey` and/or `featureFlagKey` the SDK sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch`
containing one entry per experiment the user has been assigned to through this client plus one
per flag evaluated for the user (from the cache). If nothing is cached, nothing is sent and
`true` is returned. This is what makes a single `TrackAsync("purchase", …)` count as a conversion
for every experiment the user is in.

Conversions are matched to metrics by **event name**: an experiment metric whose `event_name` is
`purchase` counts every `purchase` event, whatever `event_type` was sent. `properties` is sent as
`metadata`; `eventType` defaults to `eventName`; `timestamp` is sent as ISO-8601 UTC when given
(the server stamps the event otherwise).

Because the cache lives in the `ExperimentationClient` instance, the key-less fan-out only sees
assignments and flags evaluated through that instance within `CacheTtlSeconds`. Pass the key
explicitly when tracking from another process (a queue worker, a webhook handler).

### `Task<bool> TrackBatchAsync(IEnumerable<TrackEvent> events)`

Sends up to 100 events per `POST /api/v1/tracking/batch` (longer lists are chunked). Events with
a key are sent as-is; events without a key are fanned out like `TrackAsync`. Returns `false` if
any chunk failed. Never throws.

```csharp
bool ok = await client.TrackBatchAsync(new[]
{
    new TrackEvent { UserId = "user-123", EventName = "purchase", ExperimentKey = "checkout-cta-copy", Value = 99.99 },
    new TrackEvent { UserId = "user-123", EventName = "search",   FeatureFlagKey = "new-search" },
});
```

---

## Cache helpers

| Method | Description |
|--------|-------------|
| `IReadOnlyList<Assignment> GetAssignments(string userId)` | Cached (successful, unexpired) assignments for the user, oldest first |
| `IReadOnlyList<string> GetEvaluatedFlags(string userId)` | Keys of flags successfully evaluated (and still cached) for the user |
| `void ClearCache()` | Drop every cached evaluation and assignment |

Both caches are in-memory, keyed by user + key, TTL-expiring, capped at `MaxCacheSize` (oldest
entry evicted) and guarded by locks.

---

## Backend endpoints used

Every request carries `X-API-Key` and `Accept: application/json`; POSTs carry
`Content-Type: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `EvaluateFlagAsync` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `GetAssignmentAsync` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `TrackAsync` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `TrackAsync` without keys, `TrackBatchAsync` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored (status only) |

These SDK paths share a per-IP rate-limit ceiling of `SDK_RATE_LIMIT_PER_MINUTE` requests
(default 6000) on the backend; a `429` is surfaced as a failure (disabled flag / `null` / `false`).

---

## Error Handling

The public `ExperimentationClient` methods never throw on network or HTTP errors. Only the
low-level `ExperimentationHttpClient` throws:

| Exception | Extends | When |
|-----------|---------|------|
| `Experimently.Exceptions.ExperimentationException` | `Exception` | Base class |
| `Experimently.Exceptions.NetworkException` | `ExperimentationException` | `HttpRequestException`, timeouts (`TaskCanceledException`) |
| `Experimently.Exceptions.ApiException` (`StatusCode`, `ResponseBody`) | `ExperimentationException` | Other 4xx/5xx (404 not ACTIVE, 422 validation, 429 rate limited) or an undecodable body |
| `Experimently.Exceptions.AuthException` | `ExperimentationException` | HTTP 401 — invalid API key |

---

## Consistent Hash Utility

`FeatureFlagEvaluator.HashUser(string userId, string flagKey)` implements the cross-SDK formula —
`MD5("{userId}:{flagKey}")`, first 4 bytes as little-endian uint32, divided by 2^32 — and is
pinned by the golden-vector tests in `tests/sdk-contract/`:

```csharp
FeatureFlagEvaluator.HashUser("user-123", "my-flag");   // 0.6927449859213084
```

It is exported as a utility only. Since assignment moved to the server, nothing in the SDK uses it
to decide a variant.

---

## ASP.NET Core Integration

Register one client as a singleton; the SDK has no DI extension of its own, so use the standard
`IServiceCollection` APIs:

```csharp
// Program.cs
builder.Services.AddHttpClient("experimently", http => http.Timeout = TimeSpan.FromSeconds(5));
builder.Services.AddSingleton(sp =>
{
    var cfg = builder.Configuration.GetSection("Experimently");
    var http = sp.GetRequiredService<IHttpClientFactory>().CreateClient("experimently");
    return new ExperimentationClient(
        new SdkConfig(cfg["BaseUrl"]!, cfg["ApiKey"]!) { CacheTtlSeconds = cfg.GetValue("CacheTtlSeconds", 300) },
        http);
});
```

```csharp
[ApiController]
[Route("api/[controller]")]
public class CheckoutController : ControllerBase
{
    private readonly ExperimentationClient _experiments;
    public CheckoutController(ExperimentationClient experiments) => _experiments = experiments;

    [HttpGet]
    public async Task<IActionResult> Get()
    {
        var userId = User.FindFirst("sub")?.Value ?? "anonymous";
        var newFlow = (await _experiments.EvaluateFlagAsync("new-checkout", userId)).Enabled;
        var assignment = await _experiments.GetAssignmentAsync("checkout-cta-test", userId);
        await _experiments.TrackAsync("checkout_view", userId);   // fans out to the assignment + flag above
        return Ok(new { newFlow, cta = assignment?.VariantName ?? "control" });
    }
}
```

```json
{
  "Experimently": { "BaseUrl": "https://api.example.com", "ApiKey": "your-api-key", "CacheTtlSeconds": 300 }
}
```

---

## Testing your own code

Inject an `HttpClient` built on a fake `HttpMessageHandler` — the SDK's own tests do exactly
this (`sdk/dotnet/tests/Experimently.Tests/ExperimentationClientTests.cs`):

```csharp
sealed class CannedHandler : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct) =>
        Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent("{\"key\":\"new-checkout\",\"enabled\":true,\"config\":null}", Encoding.UTF8, "application/json")
        });
}

var client = new ExperimentationClient(new SdkConfig("http://test", "k"), new HttpClient(new CannedHandler()));
Assert.True((await client.EvaluateFlagAsync("new-checkout", "user-1")).Enabled);
```

---

## Contract smoke

Runs the four contract steps (sticky assignment, flag evaluation, keyed track, key-less fan-out
plus a 2-event batch) against a live backend and prints one JSON line:

```bash
EXPERIMENTLY_API_KEY=<key> dotnet run --project sdk/dotnet/examples/ContractSmoke
```

It prints:

```text
{"sdk":"dotnet","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<guid>`). The program writes only
that line to stdout (on first run `dotnet run` may print restore/build messages first; the live
runner reads the last stdout line) and, on failure, one line to stderr with exit code 1. This is
the command in `tests/sdk-contract/live/run_live_contract.py`.

CI runs this SDK's contract smoke against a real API: `dotnet` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Development

```bash
cd sdk/dotnet
dotnet build
dotnet test
dotnet run --project examples/BasicUsage
```

- `dotnet build`: netstandard2.1 + net6.0
- `dotnet test`: xUnit (HTTP faked, no network)

CI runs `dotnet test sdk/dotnet/Experimently.sln` on .NET 6 in the SDK Unit Tests workflow
([sdk-unit-tests.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/sdk-unit-tests.yml)), daily and on a pull request that changes `sdk/dotnet/`.
