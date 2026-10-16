// BasicUsage — demonstrates the Controlvar .NET SDK
//
// To run:
//   cd sdk/dotnet/examples/BasicUsage
//   EXPERIMENTLY_API_URL=http://localhost:8000 EXPERIMENTLY_API_KEY=<key> dotnet run
//
// Flags and experiments are decided by the server; the SDK caches the answers per user + key.

using Controlvar;
using Controlvar.Models;

// -----------------------------------------------------------------
// 1. Configure the SDK
// -----------------------------------------------------------------
var baseUrl = Environment.GetEnvironmentVariable("EXPERIMENTLY_API_URL") ?? "http://localhost:8000";
var apiKey  = Environment.GetEnvironmentVariable("EXPERIMENTLY_API_KEY")  ?? "your-api-key-here";

var config = new SdkConfig(baseUrl, apiKey)
{
    CacheTtlSeconds = 300,   // reuse a successful evaluation / assignment for 5 minutes
    TimeoutSeconds  = 10,    // HTTP timeout
    MaxCacheSize    = 1000   // max in-memory cached entries
};

using var client = new ExperimentationClient(config);

// -----------------------------------------------------------------
// 2. Evaluate a feature flag (GET /api/v1/feature-flags/evaluate/{key}?user_id=…)
// -----------------------------------------------------------------
Console.WriteLine("=== Feature Flag Evaluation ===");

var userId = "user-42";
var flagResult = await client.EvaluateFlagAsync("new-checkout-ui", userId);

Console.WriteLine($"Flag '{flagResult.Key}' for user '{userId}':");
Console.WriteLine($"  Enabled  : {flagResult.Enabled}");
Console.WriteLine($"  Config   : {(flagResult.Config.HasValue ? flagResult.Config.Value.ToString() : "(none)")}");

// -----------------------------------------------------------------
// 3. Get an experiment assignment (POST /api/v1/tracking/assign, sticky)
// -----------------------------------------------------------------
Console.WriteLine();
Console.WriteLine("=== Experiment Assignment ===");

var assignment = await client.GetAssignmentAsync(
    experimentKey: "checkout-cta-test",
    userId: userId,
    attributes: new Dictionary<string, object> { { "country", "US" }, { "plan", "pro" } });

if (assignment != null)
{
    Console.WriteLine($"Experiment '{assignment.ExperimentKey}' for user '{userId}':");
    Console.WriteLine($"  Variant       : {assignment.VariantName} (id {assignment.VariantId})");
    Console.WriteLine($"  IsControl     : {assignment.IsControl}");
    Console.WriteLine($"  Configuration : {(assignment.Configuration.HasValue ? assignment.Configuration.Value.ToString() : "(none)")}");
}
else
{
    Console.WriteLine("Not assigned (experiment not ACTIVE, or API unreachable with nothing cached).");
}

// -----------------------------------------------------------------
// 4. Track events (never throws)
// -----------------------------------------------------------------
Console.WriteLine();
Console.WriteLine("=== Event Tracking ===");

// With an experiment key: one POST /api/v1/tracking/track.
bool tracked = await client.TrackAsync(
    eventName: "checkout_started",
    userId: userId,
    properties: new Dictionary<string, object> { { "cart_total", 129.99 }, { "item_count", 3 } },
    experimentKey: "checkout-cta-test",
    value: 129.99);
Console.WriteLine($"Event 'checkout_started' tracked: {tracked}");

// Without a key: fanned out (POST /api/v1/tracking/batch) to every experiment the user was
// assigned to and every flag evaluated for them through this client.
bool fannedOut = await client.TrackAsync("page_view", userId,
    properties: new Dictionary<string, object> { { "page", "/checkout" } });
Console.WriteLine($"Event 'page_view' fanned out: {fannedOut}");

// -----------------------------------------------------------------
// 5. Hash parity demo — no network needed (not used for bucketing any more)
// -----------------------------------------------------------------
Console.WriteLine();
Console.WriteLine("=== Hash Parity ===");

double hashResult = FeatureFlagEvaluator.HashUser("user-123", "my-flag");
double expected   = 0.6927449859213084;
bool   matches    = Math.Abs(hashResult - expected) < 1e-10;

Console.WriteLine($"HashUser(\"user-123\", \"my-flag\") = {hashResult}");
Console.WriteLine($"Expected                            = {expected}");
Console.WriteLine($"Cross-SDK parity: {(matches ? "PASS" : "FAIL")}");
