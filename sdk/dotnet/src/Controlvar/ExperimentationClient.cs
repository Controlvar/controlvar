using System.Text.Json;
using System.Text.Json.Serialization;
using Controlvar.Exceptions;
using Controlvar.Models;

namespace Controlvar;

/// <summary>
/// High-level SDK client for evaluating feature flags, getting experiment assignments,
/// and tracking analytics events.
///
/// <para><b>The server decides.</b> <see cref="EvaluateFlagAsync"/> calls
/// <c>GET /api/v1/feature-flags/evaluate/{key}?user_id=…&amp;context=…</c> and <see cref="GetAssignmentAsync"/>
/// calls <c>POST /api/v1/tracking/assign</c>; no bucketing happens in the SDK. The typed getters
/// (<see cref="GetStringValueAsync"/>, <see cref="GetNumberValueAsync"/>,
/// <see cref="GetJsonValueAsync"/>) read the value a flag serves from the same request and
/// cache as <see cref="EvaluateFlagAsync"/>.</para>
///
/// <para><b>Caching.</b> Successful results are cached per user + key for
/// <see cref="SdkConfig.CacheTtlSeconds"/>; failures are never cached. On a network or HTTP
/// failure the cached value is returned when present, otherwise the flag is reported disabled /
/// the assignment is <c>null</c>. None of the public methods throw.</para>
///
/// <para><b>Tracking.</b> An event carrying <c>ExperimentKey</c>/<c>FeatureFlagKey</c> goes to
/// <c>POST /api/v1/tracking/track</c>; an event without keys is fanned out through
/// <c>POST /api/v1/tracking/batch</c> to every cached assignment and evaluated flag of that user
/// (nothing cached → nothing sent).</para>
///
/// <para>Instances are thread-safe and intended to be shared (one per process is typical).</para>
/// </summary>
public class ExperimentationClient : IDisposable
{
    /// <summary>Maximum events per <c>POST /api/v1/tracking/batch</c> request.</summary>
    public const int BatchLimit = 100;

    private readonly ExperimentationHttpClient _httpClient;
    private readonly UserKeyCache<FlagEvaluationResult> _flagCache;
    private readonly UserKeyCache<Assignment> _assignmentCache;
    private readonly Action<Exposure>? _onExposure;
    private readonly Action<ExperimentationException, string>? _onError;
    private bool _disposed;

    /// <summary>
    /// Creates a new client.
    /// </summary>
    /// <param name="config">Required SDK configuration.</param>
    /// <param name="httpClient">
    /// Optional pre-configured <see cref="System.Net.Http.HttpClient"/> for testability.
    /// If not supplied the SDK creates and manages its own instance.
    /// </param>
    public ExperimentationClient(SdkConfig config, System.Net.Http.HttpClient? httpClient = null)
        : this(config ?? throw new ArgumentNullException(nameof(config)), new ExperimentationHttpClient(config, httpClient))
    {
    }

    // Internal constructor for testing with a pre-built http wrapper.
    internal ExperimentationClient(SdkConfig config, ExperimentationHttpClient httpClient)
    {
        _httpClient = httpClient;
        var ttl = TimeSpan.FromSeconds(config.CacheTtlSeconds);
        _flagCache = new UserKeyCache<FlagEvaluationResult>(config.MaxCacheSize, ttl);
        _assignmentCache = new UserKeyCache<Assignment>(config.MaxCacheSize, ttl);
        _onExposure = config.OnExposure;
        _onError = config.OnError;
    }

    // -----------------------------------------------------------------
    // Feature flags
    // -----------------------------------------------------------------

    /// <summary>
    /// Evaluates a feature flag for a specific user via
    /// <c>GET /api/v1/feature-flags/evaluate/{flagKey}?user_id={userId}&amp;context={attributes}</c>.
    /// </summary>
    /// <param name="flagKey">The feature flag key.</param>
    /// <param name="userId">The user ID.</param>
    /// <param name="attributes">
    /// Optional user attributes, sent as <c>context</c> for the flag's targeting rules: compact
    /// JSON, serialised with the options the assignment body is serialised with, then
    /// percent-encoded. Nothing is sent for <c>null</c> or an empty dictionary. The cache key is
    /// user + flag key only, so a cached answer is returned whatever the attributes; call
    /// <see cref="ClearCache"/> after changing a user's attributes. Attributes that cannot be
    /// serialised send no request and give the disabled result, which is not cached.
    /// </param>
    /// <returns>
    /// The server's decision (<c>Key</c>, <c>Enabled</c>, <c>Config</c>). Never throws: on failure
    /// the cached value is returned when present, otherwise a disabled result
    /// (<see cref="FlagEvaluationResult.Disabled"/>). A 404 means the flag key is unknown; a flag that is not ACTIVE is a normal answer with <c>Enabled</c> false.
    /// </returns>
    public async Task<FlagEvaluationResult> EvaluateFlagAsync(
        string flagKey,
        string userId,
        Dictionary<string, object>? attributes = null)
    {
        var cached = _flagCache.Get(userId, flagKey);
        if (cached != null)
            return cached;

        try
        {
            return await FetchFlagAsync(flagKey, userId, attributes).ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Never throw — failures are not cached; report the flag as disabled.
            return FlagEvaluationResult.Disabled(flagKey);
        }
    }

    /// <summary>
    /// The request of <see cref="EvaluateFlagAsync"/> and the typed getters for an answer that is
    /// not cached: it caches a successful answer and returns it, and throws what the request
    /// failed with (which <see cref="EvaluateFlagAsync"/> hides and the getters report).
    /// </summary>
    private async Task<FlagEvaluationResult> FetchFlagAsync(
        string flagKey,
        string userId,
        Dictionary<string, object>? attributes)
    {
        var path = $"/api/v1/feature-flags/evaluate/{Uri.EscapeDataString(flagKey)}";
        var query = new Dictionary<string, string> { ["user_id"] = userId };
        var context = ContextParam(attributes);
        if (context != null)
            query["context"] = context;
        var result = await _httpClient.GetAsync<FlagEvaluationResult>(path, query).ConfigureAwait(false);
        if (string.IsNullOrEmpty(result.Key))
            result.Key = flagKey;

        _flagCache.Set(userId, flagKey, result);
        return result;
    }

    // -----------------------------------------------------------------
    // Flag values
    // -----------------------------------------------------------------

    /// <summary>
    /// The string a string flag serves this user: its on value when the flag is on for them, its
    /// off value otherwise. Evaluated as <see cref="EvaluateFlagAsync"/> evaluates, with the same
    /// request, attributes and cache: reading a value records no exposure.
    /// </summary>
    /// <param name="flagKey">The feature flag key.</param>
    /// <param name="userId">The user ID.</param>
    /// <param name="defaultValue">What to return when there is no string value to return.</param>
    /// <param name="attributes">Optional user attributes, sent as <see cref="EvaluateFlagAsync"/> sends them.</param>
    /// <returns>
    /// The flag's string value, or <paramref name="defaultValue"/>. Never throws: it returns
    /// <paramref name="defaultValue"/>, and passes the reason to <see cref="SdkConfig.OnError"/>
    /// with the operation <c>"value"</c>, when the request fails, the flag is unknown (404), the
    /// server sends no value (a server without flag values), the flag serves another type
    /// (<see cref="FlagValueException.TypeMismatch"/>), the value is not of its own type
    /// (<see cref="FlagValueException.InvalidResponse"/>), or <paramref name="userId"/> or
    /// <paramref name="flagKey"/> is <c>null</c> or empty (then nothing is sent;
    /// <see cref="FlagValueException.InvalidResponse"/>).
    /// </returns>
    public async Task<string> GetStringValueAsync(
        string flagKey,
        string userId,
        string defaultValue,
        Dictionary<string, object>? attributes = null)
    {
        var value = await TypedValueAsync(flagKey, userId, attributes, "string", nameof(GetStringValueAsync)).ConfigureAwait(false);
        return value.HasValue ? value.Value.GetString()! : defaultValue;
    }

    /// <summary>
    /// The number a number flag serves this user, as a <see cref="double"/>: an integer as saved
    /// (<c>10</c> is <c>10.0</c>) or a decimal. Evaluated as <see cref="GetStringValueAsync"/> is.
    /// </summary>
    /// <param name="flagKey">The feature flag key.</param>
    /// <param name="userId">The user ID.</param>
    /// <param name="defaultValue">What to return when there is no number value to return.</param>
    /// <param name="attributes">Optional user attributes, sent as <see cref="EvaluateFlagAsync"/> sends them.</param>
    /// <returns>
    /// The flag's number value, or <paramref name="defaultValue"/> in the cases
    /// <see cref="GetStringValueAsync"/> names; a number beyond ±(2^53−1), which no server sends,
    /// is one of them (<see cref="FlagValueException.InvalidResponse"/>). Never throws.
    /// </returns>
    public async Task<double> GetNumberValueAsync(
        string flagKey,
        string userId,
        double defaultValue,
        Dictionary<string, object>? attributes = null)
    {
        var value = await TypedValueAsync(flagKey, userId, attributes, "number", nameof(GetNumberValueAsync)).ConfigureAwait(false);
        return value.HasValue ? value.Value.GetDouble() : defaultValue;
    }

    /// <summary>
    /// The JSON object or array a json flag serves this user, as a <see cref="JsonElement"/>
    /// (read-only, so nothing done with it reaches the answer the client keeps). Evaluated as
    /// <see cref="GetStringValueAsync"/> is.
    /// </summary>
    /// <param name="flagKey">The feature flag key.</param>
    /// <param name="userId">The user ID.</param>
    /// <param name="defaultValue">What to return when there is no JSON value to return.</param>
    /// <param name="attributes">Optional user attributes, sent as <see cref="EvaluateFlagAsync"/> sends them.</param>
    /// <returns>
    /// The flag's JSON value (its <see cref="JsonElement.ValueKind"/> is
    /// <see cref="JsonValueKind.Object"/> or <see cref="JsonValueKind.Array"/>), or
    /// <paramref name="defaultValue"/> in the cases <see cref="GetStringValueAsync"/> names. Never
    /// throws.
    /// </returns>
    public async Task<JsonElement> GetJsonValueAsync(
        string flagKey,
        string userId,
        JsonElement defaultValue,
        Dictionary<string, object>? attributes = null)
    {
        var value = await TypedValueAsync(flagKey, userId, attributes, "json", nameof(GetJsonValueAsync)).ConfigureAwait(false);
        return value ?? defaultValue;
    }

    // -----------------------------------------------------------------
    // Experiments
    // -----------------------------------------------------------------

    /// <summary>
    /// Retrieves the user's (sticky) experiment assignment via <c>POST /api/v1/tracking/assign</c>.
    /// The server records the exposure; <paramref name="attributes"/> are sent as <c>context</c>
    /// for targeting rules.
    /// </summary>
    /// <param name="experimentKey">The experiment key.</param>
    /// <param name="userId">The user ID.</param>
    /// <param name="attributes">Optional user attributes (sent as <c>context</c>).</param>
    /// <returns>
    /// The <see cref="Assignment"/>, or <c>null</c> when no experiment has the key or a draft has
    /// no control variant (404), or the request failed and nothing is cached. An experiment that
    /// is not ACTIVE answers its control variant. Never throws.
    /// </returns>
    /// <remarks>
    /// When the server answers <c>"assigned": true</c> and no unexpired answer for this user and
    /// experiment was cached, <see cref="SdkConfig.OnExposure"/> is called before the returned
    /// task completes. A cached answer calls nothing, and nothing the function throws reaches
    /// the caller or changes the answer.
    /// </remarks>
    public async Task<Assignment?> GetAssignmentAsync(
        string experimentKey,
        string userId,
        Dictionary<string, object>? attributes = null)
    {
        var cached = _assignmentCache.Get(userId, experimentKey);
        if (cached != null)
            return cached;

        Assignment? assignment;
        Exposure? exposure = null;
        try
        {
            var body = new AssignRequest
            {
                ExperimentKey = experimentKey,
                UserId = userId,
                Context = attributes
            };
            // Read as a JSON element first: the answer becomes the Assignment, and its `assigned`,
            // which Assignment does not carry, says whether the server recorded an exposure.
            var answer = await _httpClient.PostAsync<JsonElement>("/api/v1/tracking/assign", body).ConfigureAwait(false);
            assignment = JsonSerializer.Deserialize<Assignment>(answer, ExperimentationHttpClient.JsonOptions);
            if (assignment == null)
                return null; // a JSON null body, a failure as before
            if (string.IsNullOrEmpty(assignment.ExperimentKey))
                assignment.ExperimentKey = experimentKey;
            if (string.IsNullOrEmpty(assignment.UserId))
                assignment.UserId = userId;

            // Fill is Set that also says whether the slot held no live answer. Two calls that
            // asked for the same new user and experiment at once each store an answer here; only
            // the first is told the slot was empty.
            bool filled = _assignmentCache.Fill(userId, experimentKey, assignment);
            if (filled && _onExposure != null && SaysAssigned(answer))
                exposure = new Exposure(assignment.ExperimentKey, assignment.UserId, assignment.VariantId, assignment.VariantName);
        }
        catch (Exception)
        {
            return null;
        }

        if (exposure != null)
        {
            // Last, outside the request's try block, with no cache lock held. What the function
            // throws is dropped (OnError hears only the typed getters' fallbacks): the answer
            // below is returned whatever it did.
            ExposureHook.Call(_onExposure!, exposure);
        }
        return assignment;
    }

    // -----------------------------------------------------------------
    // Tracking
    // -----------------------------------------------------------------

    /// <summary>
    /// Sends an analytics tracking event. Never throws.
    /// </summary>
    /// <param name="eventName">The event name (experiment metrics match on this name).</param>
    /// <param name="userId">The user who triggered the event.</param>
    /// <param name="properties">Optional event properties (sent as <c>metadata</c>).</param>
    /// <param name="experimentKey">Attribute the event to one experiment.</param>
    /// <param name="featureFlagKey">Attribute the event to one feature flag.</param>
    /// <param name="value">Optional numeric value (revenue, duration, …).</param>
    /// <param name="eventType">Optional event type; defaults to <paramref name="eventName"/>.</param>
    /// <param name="timestamp">Optional event time; sent as ISO-8601 UTC.</param>
    /// <returns>
    /// <c>true</c> when every request succeeded (or there was nothing to send); <c>false</c> on any
    /// network / HTTP error.
    /// </returns>
    /// <remarks>
    /// With <paramref name="experimentKey"/> and/or <paramref name="featureFlagKey"/> one
    /// <c>POST /api/v1/tracking/track</c> is sent. Without keys, one
    /// <c>POST /api/v1/tracking/batch</c> is sent with one entry per cached assignment and one per
    /// cached evaluated flag of the user; if nothing is cached, nothing is sent.
    /// </remarks>
    public Task<bool> TrackAsync(
        string eventName,
        string userId,
        Dictionary<string, object>? properties = null,
        string? experimentKey = null,
        string? featureFlagKey = null,
        double? value = null,
        string? eventType = null,
        DateTimeOffset? timestamp = null)
    {
        return TrackAsync(new TrackEvent
        {
            EventName = eventName,
            UserId = userId,
            Properties = properties,
            ExperimentKey = experimentKey,
            FeatureFlagKey = featureFlagKey,
            Value = value,
            EventType = eventType,
            Timestamp = timestamp
        });
    }

    /// <summary>Sends a prebuilt <see cref="TrackEvent"/> (same rules as the other overload). Never throws.</summary>
    public async Task<bool> TrackAsync(TrackEvent trackEvent)
    {
        if (trackEvent == null)
            return false;

        if (trackEvent.HasKey)
        {
            try
            {
                await _httpClient.PostAsync("/api/v1/tracking/track", trackEvent.ToBody()).ConfigureAwait(false);
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        return await SendBatchesAsync(FanOut(trackEvent)).ConfigureAwait(false);
    }

    /// <summary>
    /// Sends several events with <c>POST /api/v1/tracking/batch</c> (chunked by
    /// <see cref="BatchLimit"/>). Keyed events are sent as-is; events without keys are fanned out to
    /// the user's cached assignments and flags. Never throws.
    /// </summary>
    /// <returns><c>true</c> when every batch request succeeded (or nothing had to be sent).</returns>
    public async Task<bool> TrackBatchAsync(IEnumerable<TrackEvent> events)
    {
        if (events == null)
            return true;

        var expanded = new List<TrackEvent>();
        foreach (var trackEvent in events)
        {
            if (trackEvent == null)
                continue;
            if (trackEvent.HasKey)
                expanded.Add(trackEvent);
            else
                expanded.AddRange(FanOut(trackEvent));
        }

        return await SendBatchesAsync(expanded).ConfigureAwait(false);
    }

    // -----------------------------------------------------------------
    // Cache access
    // -----------------------------------------------------------------

    /// <summary>Cached (successful, unexpired) assignments of the user, in the order they were made.</summary>
    public IReadOnlyList<Assignment> GetAssignments(string userId)
    {
        var result = new List<Assignment>();
        foreach (var entry in _assignmentCache.Entries(userId))
            result.Add(entry.Value);
        return result;
    }

    /// <summary>Keys of flags successfully evaluated (and still cached) for the user.</summary>
    public IReadOnlyList<string> GetEvaluatedFlags(string userId)
    {
        var result = new List<string>();
        foreach (var entry in _flagCache.Entries(userId))
            result.Add(entry.Key);
        return result;
    }

    /// <summary>Drops every cached evaluation and assignment.</summary>
    public void ClearCache()
    {
        _flagCache.Clear();
        _assignmentCache.Clear();
    }

    // -----------------------------------------------------------------
    // Private helpers
    // -----------------------------------------------------------------

    private sealed class AssignRequest
    {
        [JsonPropertyName("experiment_key")]
        public string ExperimentKey { get; set; } = "";

        [JsonPropertyName("user_id")]
        public string UserId { get; set; } = "";

        [JsonPropertyName("context")]
        public Dictionary<string, object>? Context { get; set; }
    }

    private sealed class BatchRequest
    {
        [JsonPropertyName("events")]
        public List<TrackBody> Events { get; set; } = new();
    }

    /// <summary>
    /// The <c>context</c> query value of a flag evaluation: <paramref name="attributes"/> as
    /// compact JSON, serialised with the options of the assignment body, or <c>null</c> when there
    /// are none (<c>null</c> or empty), so the URL is then the one sent without them.
    /// <see cref="ExperimentationHttpClient.BuildUri"/> percent-encodes it. Throws what
    /// <see cref="JsonSerializer"/> throws for attributes it cannot serialise.
    /// </summary>
    private static string? ContextParam(Dictionary<string, object>? attributes)
    {
        if (attributes == null || attributes.Count == 0)
            return null;
        return JsonSerializer.Serialize(attributes, ExperimentationHttpClient.JsonOptions);
    }

    /// <summary>2^53−1: the largest integer every SDK's numbers hold exactly.</summary>
    private const double MaxSafeInteger = 9007199254740991;

    /// <summary>
    /// The typed getters' one body, as the JavaScript SDK's: the value the flag serves this user
    /// when it is of <paramref name="type"/>, and otherwise <c>null</c>, with the reason passed to
    /// <see cref="SdkConfig.OnError"/> once. The order of the checks is the contract
    /// (<c>tests/sdk-contract/value-vectors.json</c>): no value; a served type other than
    /// <paramref name="type"/> (<c>value_type</c> when the answer has one, else the value's own
    /// JSON type); a value that is not of <paramref name="type"/>. Never throws.
    /// </summary>
    private async Task<JsonElement?> TypedValueAsync(
        string flagKey,
        string userId,
        Dictionary<string, object>? attributes,
        string type,
        string method)
    {
        ExperimentationException reason;
        try
        {
            if (string.IsNullOrEmpty(userId))
                throw new FlagValueException(
                    $"userId is null or empty; {method} sent nothing and returned your default",
                    FlagValueException.InvalidResponse);
            if (string.IsNullOrEmpty(flagKey))
                throw new FlagValueException(
                    $"flagKey is null or empty; {method} sent nothing and returned your default",
                    FlagValueException.InvalidResponse);

            var evaluation = _flagCache.Get(userId, flagKey)
                ?? await FetchFlagAsync(flagKey, userId, attributes).ConfigureAwait(false);
            var flag = $"Flag '{flagKey}'";
            if (evaluation.Value is not { } value
                || value.ValueKind == JsonValueKind.Null
                || value.ValueKind == JsonValueKind.Undefined)
                throw new FlagValueException(
                    $"{flag} came back without a value (from a server without flag values, or one that " +
                    $"cannot serve this flag's stored value); {method} returned your default",
                    FlagValueException.InvalidResponse);
            string served = evaluation.ValueType ?? JsonTypeOf(value);
            if (served != type)
                throw new FlagValueException(
                    $"{flag} serves {DescribeType(served)}; {method} returned your default",
                    FlagValueException.TypeMismatch);
            if (!IsValueOfType(value, type))
                throw new FlagValueException(
                    $"{flag} serves {DescribeType(type)}, but the server's value is not one; {method} " +
                    "returned your default",
                    FlagValueException.InvalidResponse);
            return value;
        }
        catch (ExperimentationException e)
        {
            reason = e;
        }
        catch (Exception e)
        {
            // Not the SDK's own kind (a base URL that is no URL, attributes that cannot be
            // serialised): reported as one, with what was thrown as its cause.
            reason = new ExperimentationException(
                $"Could not evaluate flag '{flagKey}' ({e.GetType().FullName}); {method} returned your default", e);
        }

        if (_onError != null)
            ExposureHook.CallOnError(_onError, reason, "value");
        return null;
    }

    /// <summary>A JSON value's type as the server names value types.</summary>
    private static string JsonTypeOf(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.String => "string",
        JsonValueKind.True or JsonValueKind.False => "boolean",
        JsonValueKind.Number => "number",
        JsonValueKind.Object or JsonValueKind.Array => "json",
        _ => value.ValueKind.ToString(),
    };

    /// <summary>A value type as the typed getters' messages name it.</summary>
    private static string DescribeType(string type) => type switch
    {
        "boolean" => "a boolean",
        "string" => "a string",
        "number" => "a number",
        "json" => "a JSON value",
        _ => $"a value of type \"{(type.Length > 64 ? type.Substring(0, 64) : type)}\"",
    };

    /// <summary>
    /// Whether <paramref name="value"/> is a value of <paramref name="type"/> as the server serves
    /// one: a string; a number that is finite and within ±(2^53−1); a JSON object or array.
    /// </summary>
    private static bool IsValueOfType(JsonElement value, string type) => type switch
    {
        "string" => value.ValueKind == JsonValueKind.String,
        "number" => value.ValueKind == JsonValueKind.Number
            && value.TryGetDouble(out double number)
            && !double.IsNaN(number)
            && !double.IsInfinity(number)
            && Math.Abs(number) <= MaxSafeInteger,
        "json" => value.ValueKind == JsonValueKind.Object || value.ValueKind == JsonValueKind.Array,
        _ => false,
    };

    /// <summary>
    /// True only when the <c>/assign</c> answer's <c>assigned</c> is the JSON literal
    /// <c>true</c>. A missing field, <c>null</c>, <c>"true"</c> or <c>1</c> is not: the server
    /// recorded an exposure only for <c>true</c>.
    /// </summary>
    private static bool SaysAssigned(JsonElement answer) =>
        answer.ValueKind == JsonValueKind.Object
        && answer.TryGetProperty("assigned", out var assigned)
        && assigned.ValueKind == JsonValueKind.True;

    /// <summary>One entry per cached assignment (experiment_key) plus one per cached flag (feature_flag_key).</summary>
    private List<TrackEvent> FanOut(TrackEvent trackEvent)
    {
        var events = new List<TrackEvent>();
        foreach (var assignment in GetAssignments(trackEvent.UserId))
            events.Add(trackEvent.Attributed(experimentKey: assignment.ExperimentKey));
        foreach (var flagKey in GetEvaluatedFlags(trackEvent.UserId))
            events.Add(trackEvent.Attributed(featureFlagKey: flagKey));
        return events;
    }

    private async Task<bool> SendBatchesAsync(List<TrackEvent> events)
    {
        if (events.Count == 0)
            return true;

        bool ok = true;
        for (int index = 0; index < events.Count; index += BatchLimit)
        {
            var chunk = new BatchRequest();
            int end = Math.Min(index + BatchLimit, events.Count);
            for (int i = index; i < end; i++)
                chunk.Events.Add(events[i].ToBody());

            try
            {
                await _httpClient.PostAsync("/api/v1/tracking/batch", chunk).ConfigureAwait(false);
            }
            catch (Exception)
            {
                ok = false;
            }
        }
        return ok;
    }

    /// <summary>Drops the caches and releases the HTTP client (only when the SDK created it). Safe to call twice.</summary>
    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        ClearCache();
        _httpClient.Dispose();
        GC.SuppressFinalize(this);
    }
}
