// Contract smoke for the .NET SDK.
//
// Run from the repo root against a seeded backend (see backend/scripts/seed_sdk_contract.py):
//
//     EXPERIMENTLY_API_KEY=... dotnet run --project sdk/dotnet/examples/ContractSmoke
//
// Environment: EXPERIMENTLY_API_URL (default http://localhost:8000), EXPERIMENTLY_API_KEY
// (required), CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY (default
// sdk_contract_flag), CONTRACT_USER_ID (default smoke-<guid>).
//
// Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr and
// exits 1. Only the SDK's public API is used. (`dotnet run` itself may print restore/build
// messages before the program starts; the live runner reads the last stdout line.)

using System.Text.Json;
using Experimently;
using Experimently.Models;

try
{
    return await RunAsync();
}
catch (Exception ex)
{
    return Fail($"{ex.GetType().Name}: {ex.Message}");
}

static string Env(string name, string defaultValue)
{
    var value = Environment.GetEnvironmentVariable(name);
    return string.IsNullOrEmpty(value) ? defaultValue : value;
}

static int Fail(string message)
{
    Console.Error.WriteLine("dotnet contract smoke: " + message.Replace('\n', ' ').Replace('\r', ' '));
    return 1;
}

static async Task<int> RunAsync()
{
    var apiUrl = Env("EXPERIMENTLY_API_URL", "http://localhost:8000");
    var apiKey = Env("EXPERIMENTLY_API_KEY", "");
    if (string.IsNullOrWhiteSpace(apiKey))
        return Fail("EXPERIMENTLY_API_KEY is required");
    var experimentKey = Env("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab");
    var flagKey = Env("CONTRACT_FLAG_KEY", "sdk_contract_flag");
    var userId = Env("CONTRACT_USER_ID", "smoke-" + Guid.NewGuid().ToString("D"));

    var config = new SdkConfig(apiUrl, apiKey);
    var attributes = new Dictionary<string, object> { ["source"] = "contract_smoke", ["sdk"] = "dotnet" };

    // `client` keeps the assignment and flag cached (needed for the fan-out step); `fresh` starts
    // with an empty cache so the second assignment is a real server round-trip and proves the
    // server itself is sticky.
    using var client = new ExperimentationClient(config);
    using var fresh = new ExperimentationClient(config);

    // 1. Assign twice → identical (sticky on the server).
    var first = await client.GetAssignmentAsync(experimentKey, userId, attributes);
    if (first == null)
        return Fail($"assignment failed for {experimentKey} (not ACTIVE, bad key, or API unreachable)");
    var second = await fresh.GetAssignmentAsync(experimentKey, userId, attributes);
    if (second == null)
        return Fail($"second assignment failed for {experimentKey}");
    if (first.VariantName != "control" && first.VariantName != "treatment")
        return Fail($"unexpected variant_name '{first.VariantName}' for {experimentKey}");
    bool sticky = first.VariantName == second.VariantName && first.VariantId == second.VariantId;
    if (!sticky)
        return Fail($"assignment not sticky: {first.VariantName}/{first.VariantId} vs {second.VariantName}/{second.VariantId}");

    // 2. Evaluate the flag → Enabled is a bool (seeded flag is 100% on). EvaluateFlagAsync never
    //    throws, so a failed request is detected by the flag not having been cached.
    var flag = await client.EvaluateFlagAsync(flagKey, userId);
    if (!client.GetEvaluatedFlags(userId).Contains(flagKey))
        return Fail($"flag evaluation failed for {flagKey} (unknown flag key, bad key, or API unreachable)");

    // 3. Track `purchase` with a value and the experiment key → POST /api/v1/tracking/track.
    bool trackOk = await client.TrackAsync(
        "purchase",
        userId,
        properties: new Dictionary<string, object> { ["sdk"] = "dotnet", ["currency"] = "USD" },
        experimentKey: experimentKey,
        value: 12.5);
    if (!trackOk)
        return Fail("track purchase failed");

    // 4. Track `page_view` without a key → fans out to the cached assignment + flag via
    //    POST /api/v1/tracking/batch.
    if (client.GetAssignments(userId).Count != 1)
        return Fail("expected one cached assignment before fan-out");
    bool fanoutOk = await client.TrackAsync(
        "page_view",
        userId,
        properties: new Dictionary<string, object> { ["page"] = "/smoke" });
    if (!fanoutOk)
        return Fail("fan-out track failed");

    // The SDK exposes a batch call, so also send a 2-event batch.
    bool batchOk = await client.TrackBatchAsync(new[]
    {
        new TrackEvent { UserId = userId, EventName = "add_to_cart", ExperimentKey = experimentKey, Value = 1 },
        new TrackEvent { UserId = userId, EventName = "checkout", FeatureFlagKey = flagKey }
    });
    if (!batchOk)
        return Fail("batch track failed");

    var line = JsonSerializer.Serialize(new
    {
        sdk = "dotnet",
        assign = new { variant_name = first.VariantName, is_control = first.IsControl, sticky },
        flag = new { enabled = flag.Enabled },
        track = new { ok = trackOk },
        fanout = new { ok = fanoutOk && batchOk }
    });
    Console.Out.WriteLine(line);
    return 0;
}
