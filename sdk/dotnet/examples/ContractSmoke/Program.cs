// Contract smoke for the .NET SDK.
//
// Run from the repo root against a seeded backend (see backend/scripts/seed_sdk_contract.py):
//
//     CONTROLVAR_API_KEY=... dotnet run --project sdk/dotnet/examples/ContractSmoke
//
// Environment: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY
// (required), CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY (default
// sdk_contract_flag), CONTRACT_TARGETED_FLAG_KEY (default sdk_contract_targeted),
// CONTRACT_USER_ID (default smoke-<guid>).
//
// Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr and
// exits 1. Only the SDK's public API is used. (`dotnet run` itself may print restore/build
// messages before the program starts; the live runner reads the last stdout line.)

using System.Collections.Concurrent;
using System.Text.Json;
using Controlvar;
using Controlvar.Models;

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
    var apiUrl = Env("CONTROLVAR_API_URL", Env("EXPERIMENTLY_API_URL", "http://localhost:8000"));
    var apiKey = Env("CONTROLVAR_API_KEY", Env("EXPERIMENTLY_API_KEY", ""));
    if (string.IsNullOrWhiteSpace(apiKey))
        return Fail("CONTROLVAR_API_KEY is required");
    var experimentKey = Env("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab");
    var flagKey = Env("CONTRACT_FLAG_KEY", "sdk_contract_flag");
    var targetedKey = Env("CONTRACT_TARGETED_FLAG_KEY", "sdk_contract_targeted");
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
        return Fail($"assignment failed for {experimentKey} (unknown experiment key, bad API key, or API unreachable)");
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

    // 4c. The attributes reach flag evaluation (#1520, #1719). The seeded targeted flag is at 0%
    //     with one rule, plan equals pro, at 100%. A NEW client (so the fan-out above is untouched)
    //     asks for it with plan=pro, then, after ClearCache() (the cache key is user + flag), with
    //     plan=free. FlagEvaluationResult carries no reason, so the server's reason is read from
    //     the answer to the client's own request: the client is given an HttpClient (the public
    //     constructor's optional argument, with the SDK's default timeout) whose handler only
    //     reads the targeted flag's answers. The runner requires on with targeting_rule, then off.
    var reasons = new ConcurrentQueue<string?>();
    var targetedPath = $"/api/v1/feature-flags/evaluate/{Uri.EscapeDataString(targetedKey)}";
    FlagEvaluationResult pro;
    FlagEvaluationResult free;
    using (var recording = new HttpClient(new ReasonRecorder(targetedPath, reasons)) { Timeout = TimeSpan.FromSeconds(config.TimeoutSeconds) })
    using (var targeting = new ExperimentationClient(config, recording))
    {
        pro = await targeting.EvaluateFlagAsync(targetedKey, userId, new Dictionary<string, object> { ["plan"] = "pro" });
        if (!targeting.GetEvaluatedFlags(userId).Contains(targetedKey))
            return Fail($"flag evaluation failed for {targetedKey} (unknown flag key, bad key, or API unreachable)");
        targeting.ClearCache();
        free = await targeting.EvaluateFlagAsync(targetedKey, userId, new Dictionary<string, object> { ["plan"] = "free" });
    }
    var answered = reasons.ToArray();
    if (answered.Length != 2)
        return Fail($"the targeted flag was asked {answered.Length} time(s), expected 2");

    // 5. The exposure hook (#1499): a NEW client with OnExposure assigns the same user twice, with
    //    no ClearCache() in between. The server records an exposure for the first request
    //    (assigned: true) and the second answer comes from the cache, so OnExposure is called
    //    once. The runner checks the count, the experiment key and the variant; the user id is
    //    checked here, the one place that knows it.
    var exposures = new ConcurrentQueue<Exposure>();
    using (var hooked = new ExperimentationClient(new SdkConfig(apiUrl, apiKey) { OnExposure = exposures.Enqueue }))
    {
        await hooked.GetAssignmentAsync(experimentKey, userId, attributes);
        await hooked.GetAssignmentAsync(experimentKey, userId, attributes);
    }
    foreach (var e in exposures)
    {
        if (e.UserId != userId)
            return Fail($"OnExposure received user \"{e.UserId}\", expected \"{userId}\"");
    }

    // 5b. A function that throws: the client must catch it (an exception that escaped would end
    //     this smoke through its catch, exit 1) and answer the same variant.
    int thrown = 0;
    Assignment? fromThrowing;
    using (var failing = new ExperimentationClient(new SdkConfig(apiUrl, apiKey)
    {
        OnExposure = _ =>
        {
            Interlocked.Increment(ref thrown);
            throw new InvalidOperationException("the analytics client is not ready");
        }
    }))
    {
        fromThrowing = await failing.GetAssignmentAsync(experimentKey, userId, attributes);
    }
    if (fromThrowing == null || fromThrowing.VariantName != first.VariantName)
        return Fail($"the client whose OnExposure throws answered {fromThrowing?.VariantName ?? "null"}, not {first.VariantName}");
    if (thrown != 1)
        return Fail($"the OnExposure that throws was called {thrown} times, expected 1");

    var seen = exposures.ToArray();
    var payload = seen.Length == 0
        ? null
        : new
        {
            experiment_key = seen[0].ExperimentKey,
            variant_name = seen[0].VariantName,
            variant_id = seen[0].VariantId,
            user_id = seen[0].UserId
        };

    var line = JsonSerializer.Serialize(new
    {
        sdk = "dotnet",
        assign = new { variant_name = first.VariantName, is_control = first.IsControl, sticky },
        flag = new { enabled = flag.Enabled },
        track = new { ok = trackOk },
        fanout = new { ok = fanoutOk && batchOk },
        targeted = new { pro = pro.Enabled, free = free.Enabled, reason = answered[0] },
        exposure = new { calls = seen.Length, payload }
    });
    Console.Out.WriteLine(line);
    return 0;
}

// The handler of step 4c's HttpClient: passes every request on unchanged and, for the targeted
// flag's evaluations, records the `reason` of the server's answer (null when it has none).
sealed class ReasonRecorder : DelegatingHandler
{
    private readonly string _path;
    private readonly ConcurrentQueue<string?> _reasons;

    public ReasonRecorder(string path, ConcurrentQueue<string?> reasons)
        : base(new HttpClientHandler())
    {
        _path = path;
        _reasons = reasons;
    }

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var response = await base.SendAsync(request, cancellationToken).ConfigureAwait(false);
        if (request.RequestUri?.AbsolutePath == _path)
        {
            string? reason = null;
            try
            {
                var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
                using var answer = JsonDocument.Parse(body);
                if (answer.RootElement.ValueKind == JsonValueKind.Object
                    && answer.RootElement.TryGetProperty("reason", out var value)
                    && value.ValueKind == JsonValueKind.String)
                    reason = value.GetString();
            }
            catch (JsonException)
            {
                // Not JSON: no reason.
            }
            _reasons.Enqueue(reason);
        }
        return response;
    }
}
