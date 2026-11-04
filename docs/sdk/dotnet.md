# .NET SDK

`Controlvar.SDK` (v0.1.0) provides feature flag evaluation, experiment assignment and
event tracking for .NET applications. It targets `net8.0` and `netstandard2.1`, uses
`System.Text.Json` and `HttpClient`, and has no other dependencies.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

Source: `sdk/dotnet`.

---

## Requirements

- The library targets `net8.0` and `netstandard2.1`. .NET 8 and later use the `net8.0` build,
  and .NET Core 3.0 to .NET 7 use the `netstandard2.1` build. On .NET Core 3.0 to .NET 5,
  System.Text.Json 8 and its dependency System.Text.Encodings.Web warn at build time that they do
  not support that target framework and have not been tested with it.
- .NET Framework cannot use it: .NET Framework does not implement .NET Standard 2.1.
- To build it from source and run its tests: the .NET 10 SDK and the .NET 8 runtime (see
  Development)

---

## Installation

**Not yet published.** `Controlvar.SDK` has no release on NuGet, so build it from source: reference
the project in `sdk/dotnet/src/Controlvar` of a clone of this repository from your own project
(this is what the SDK's `examples/` and `tests/` do):

```xml
<ProjectReference Include="../../sdk/dotnet/src/Controlvar/Controlvar.csproj" />
```

---

## Quick Start

If you set `EXPERIMENTLY_API_KEY` or `EXPERIMENTLY_API_URL` from an earlier version of these pages, set `CONTROLVAR_API_KEY` and `CONTROLVAR_API_URL` instead.

```csharp
using Controlvar;

var config = new SdkConfig(
    baseUrl: Environment.GetEnvironmentVariable("CONTROLVAR_API_URL") ?? "http://localhost:8000",
    apiKey:  Environment.GetEnvironmentVariable("CONTROLVAR_API_KEY")!);

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
| `OnExposure` | `Action<Exposure>?` | `null` | Called once each time the server enrols the user and the answer is not cached; see [Exposure hook](#exposure-hook) |

`SdkConfig` throws `ArgumentException` for an empty `baseUrl`/`apiKey`. `ExperimentationClient`
is thread-safe and meant to be shared (one instance per process). Dispose it to release the
`HttpClient` it created; an injected `HttpClient` is never mutated or disposed.

---

## Feature Flag Evaluation

### `Task<FlagEvaluationResult> EvaluateFlagAsync(string flagKey, string userId, Dictionary<string, object>? attributes = null)`

Calls `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…&context=…` and returns a
`FlagEvaluationResult`. `attributes` is sent as `context` on flag evaluation, so the flag's
targeting rules are evaluated against them: compact JSON from the same `System.Text.Json` options
as the assignment body (characters such as `+`, `&` and non-ASCII letters become `\uXXXX`
escapes), then
percent-encoded, as the key and the user id are. With `null` or an empty dictionary no `context`
is sent and the request carries the user id alone.

Attributes are assumed **stable per user**: an evaluation is cached by user + flag key only, so
within `CacheTtlSeconds` a second call for the same user and flag returns the first answer,
whatever its attributes. Call `ClearCache()` after changing a user's attributes.

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

Never throws. A flag that is not ACTIVE is not a failure: it answers `Enabled: false`. A cached
evaluation is returned before any request is made; on a network/HTTP failure (a 404 is an unknown
flag key) the result is disabled (`FlagEvaluationResult.Disabled(flagKey)`). Failures are never
cached, so the next call retries.

---

## Experiment Assignment

### `Task<Assignment?> GetAssignmentAsync(string experimentKey, string userId, Dictionary<string, object>? attributes = null)`

Calls `POST /api/v1/tracking/assign` with `{experiment_key, user_id, context: attributes}`. The
server buckets the user, keeps the assignment sticky and records the exposure. `context` is
omitted when `attributes` is `null`. To send each exposure to your own analytics tool as well,
see [Exposure hook](#exposure-hook).

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

Never throws. Returns `null` on any failure (network error, 401, 404 when no experiment has the
key or a draft has no control variant, 429); a cached assignment is returned when one exists.
Failures are never cached. An experiment that is not ACTIVE is not a failure: it answers its
control variant.

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

This SDK returns only the variant from an answer: an `assigned: false` answer (a user the
experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as the
control variant, and an event tracked without a key after the user's cache entries have expired
(`CacheTtlSeconds`, 300 by default) is not sent, so pass `experimentKey` on a conversion that can
come later.

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

## Exposure hook

`OnExposure` is a function you give `SdkConfig` when you create it. The client calls it once each
time the server enrols the user in an experiment and the answer did not come from the client's
cache: the moment the server records the exposure. Use it to send the exposure to your analytics
tool or warehouse.

This code writes each exposure to a channel, and a task of its own sends them on:

```csharp
using System.Threading.Channels;
using Controlvar;
using Controlvar.Models;

var exposures = Channel.CreateBounded<Exposure>(10_000);

var config = new SdkConfig("http://localhost:8000", Environment.GetEnvironmentVariable("CONTROLVAR_API_KEY")!)
{
    OnExposure = e => exposures.Writer.TryWrite(e),   // never waits: drops the exposure when the channel is full
};

_ = Task.Run(async () =>
{
    await foreach (var e in exposures.Reader.ReadAllAsync())
    {
        // Your analytics tool's call goes here: it runs here, not inside GetAssignmentAsync.
        Console.WriteLine($"exposure {e.ExperimentKey} {e.VariantName} {e.UserId}");
    }
});

using var client = new ExperimentationClient(config);
```

In ASP.NET Core, read the channel in a `BackgroundService`.

It receives an `Exposure`, a record in `Controlvar.Models`, new for each call:

| Property | Type | Description |
|----------|------|-------------|
| `ExperimentKey` | `string` | The experiment key |
| `UserId` | `string` | The user the server enrolled |
| `VariantId` | `string?` | The variant's id, `null` when the server sends none |
| `VariantName` | `string` | The variant's name |

Nothing else: not the user's attributes and not the variant's configuration. Its positional
order is `Assignment`'s: `new Exposure(experimentKey, userId, variantId, variantName)`.

It is not called for:

- an answer from the client's cache;
- a user the experiment did not enrol (`assigned: false`, with `reason` `"holdout"`,
  `"mutual_exclusion"` or `"targeting"`), or an experiment that is not running (`"draft"`,
  `"paused"`, `"completed"` or `"archived"`): the server records no exposure for them, and the
  client returns the control variant;
- a call that fails, including one that times out after the server recorded the exposure;
- a feature flag.

This SDK's `Assignment` does not say whether the user was enrolled; `OnExposure` is not called for
a user who was not. It is not called when the server does not send `assigned`; every Controlvar
server release sends it.

It is called again, and the server records another exposure, after the cache lifetime
(`CacheTtlSeconds`, 300 by default), after `ClearCache()`, after the entry is evicted from the
cache (`MaxCacheSize`, 1000 entries per cache by default), and in every new client, such as each
process on a server. So your tool can receive several exposures for one user and experiment: count
users, not events. It never receives one the server did not record.

It is called when your code asks for the assignment, whether or not you then show the variant, so
ask for it where the variant is shown.

It runs inside `GetAssignmentAsync`, after the answer is cached and before its `Task` completes,
usually on a thread-pool thread; do not assume a UI thread, and marshal to it yourself to touch
UI. Its running time is added to that call, with no timeout. Calls for other users and experiments
are not held up. Write the exposure to a channel, as above.

It can run on several threads at once: make it safe for that. Two calls for the same new user and
experiment at the same moment send two requests, and the server records two exposures;
`OnExposure` is called once.

If it throws, the client catches the exception and reports nothing: `GetAssignmentAsync` returns
the answer, and a failing function looks like one that is never called. Catch and log inside your
function if you need to know. When several functions are combined into one delegate (`+=`), each
is called in turn, and one that throws does not stop the next.

An async lambda or `async` method is refused with `ArgumentException` when you set `OnExposure`,
alone or combined with other functions: it would run as `async void`, and an exception after its
first `await` would end the process. A synchronous lambda that itself calls an `async void` method
is not detected. `OnExposure = null` sets none.

In a test, the `/assign` answer your `HttpMessageHandler` returns must include `"assigned": true`,
or `OnExposure` is not called.

---

## Backend endpoints used

Every request carries `X-API-Key` and `Accept: application/json`; POSTs carry
`Content-Type: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `EvaluateFlagAsync` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=…` | `context`: the attributes as URL-encoded JSON, only when there are any | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `GetAssignmentAsync` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration, assigned, reason}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
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
| `Controlvar.Exceptions.ExperimentationException` | `Exception` | Base class |
| `Controlvar.Exceptions.NetworkException` | `ExperimentationException` | `HttpRequestException`, timeouts (`TaskCanceledException`) |
| `Controlvar.Exceptions.ApiException` (`StatusCode`, `ResponseBody`) | `ExperimentationException` | Other 4xx/5xx (404: an unknown experiment or flag key, or a draft experiment with no control variant; 422 validation; 429 rate limited) or an undecodable body |
| `Controlvar.Exceptions.AuthException` | `ExperimentationException` | HTTP 401 — invalid API key |

---

## Consistent Hash Utility

`FeatureFlagEvaluator.HashUser(string userId, string flagKey)` implements the cross-SDK formula —
`MD5("{userId}:{flagKey}")`, first 4 bytes as little-endian uint32, divided by 2^32. The SDK's own
`HashCompatibilityTests` pin one vector; the cross-SDK golden vectors in `tests/sdk-contract/`
are not run against this SDK:

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
builder.Services.AddHttpClient("controlvar", http => http.Timeout = TimeSpan.FromSeconds(5));
builder.Services.AddSingleton(sp =>
{
    var cfg = builder.Configuration.GetSection("Controlvar");
    var http = sp.GetRequiredService<IHttpClientFactory>().CreateClient("controlvar");
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
  "Controlvar": { "BaseUrl": "https://api.example.com", "ApiKey": "your-api-key", "CacheTtlSeconds": 300 }
}
```

---

## Testing your own code

Inject an `HttpClient` built on a fake `HttpMessageHandler` — the SDK's own tests do exactly
this (`sdk/dotnet/tests/Controlvar.Tests/ExperimentationClientTests.cs`):

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
plus a 2-event batch) and the exposure hook against a live backend and prints one JSON line:

```bash
CONTROLVAR_API_KEY=<key> dotnet run --project sdk/dotnet/examples/ContractSmoke
```

It prints:

```text
{"sdk":"dotnet","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true},"exposure":{"calls":1,"payload":{"experiment_key":"sdk_contract_ab","variant_name":"control","variant_id":"<variant id>","user_id":"smoke-<guid>"}}}
```

After the four steps a new client with `OnExposure` assigns the user twice, and the smoke reports
how often `OnExposure` was called and what it received (`exposure`); it exits 1 if `OnExposure`
received another user's id. Last, a client whose `OnExposure` throws assigns once and must answer
the same variant.

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<guid>`). The program writes only
that line to stdout (on first run `dotnet run` may print restore/build messages first; the live
runner reads the last stdout line) and, on failure, one line to stderr with exit code 1. This is
the command in `tests/sdk-contract/live/run_live_contract.py`.

CI runs this SDK's contract smoke against a real API: `dotnet` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Development

```bash
cd sdk/dotnet
dotnet build
dotnet test
dotnet run --project examples/BasicUsage
```

- `dotnet build`: netstandard2.1 + net8.0
- `dotnet test`: xUnit (HTTP faked, no network), in three legs: `tests/Controlvar.Tests` on
  `net8.0` and on `net10.0` against the `net8.0` build, and `tests/Controlvar.Tests.NetStandard`
  on `net8.0` against the `netstandard2.1` build. The tests need the .NET 10 SDK and the .NET 8
  runtime.

CI runs `dotnet test sdk/dotnet/Controlvar.sln` with the .NET 10 SDK and the .NET 8 runtime in
the SDK Unit Tests workflow
([sdk-unit-tests.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/sdk-unit-tests.yml)), daily and on a pull request that changes `sdk/dotnet/`.
