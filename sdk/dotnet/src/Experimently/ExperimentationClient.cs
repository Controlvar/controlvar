using System.Text.Json.Serialization;
using Experimently.Models;

namespace Experimently;

/// <summary>
/// High-level SDK client for evaluating feature flags, getting experiment assignments,
/// and tracking analytics events.
///
/// <para><b>The server decides.</b> <see cref="EvaluateFlagAsync"/> calls
/// <c>GET /api/v1/feature-flags/evaluate/{key}?user_id=…</c> and <see cref="GetAssignmentAsync"/>
/// calls <c>POST /api/v1/tracking/assign</c>; no bucketing happens in the SDK.</para>
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
    }

    // -----------------------------------------------------------------
    // Feature flags
    // -----------------------------------------------------------------

    /// <summary>
    /// Evaluates a feature flag for a specific user via
    /// <c>GET /api/v1/feature-flags/evaluate/{flagKey}?user_id={userId}</c>.
    /// </summary>
    /// <param name="flagKey">The feature flag key.</param>
    /// <param name="userId">The user ID.</param>
    /// <param name="attributes">
    /// Accepted for source compatibility and not sent: the evaluate endpoint takes a context, which this SDK does not send yet.
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
            var path = $"/api/v1/feature-flags/evaluate/{Uri.EscapeDataString(flagKey)}";
            var query = new Dictionary<string, string> { ["user_id"] = userId };
            var result = await _httpClient.GetAsync<FlagEvaluationResult>(path, query).ConfigureAwait(false);
            if (string.IsNullOrEmpty(result.Key))
                result.Key = flagKey;

            _flagCache.Set(userId, flagKey, result);
            return result;
        }
        catch (Exception)
        {
            // Never throw — failures are not cached; report the flag as disabled.
            return FlagEvaluationResult.Disabled(flagKey);
        }
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
    /// The <see cref="Assignment"/>, or <c>null</c> when the experiment is unknown / not ACTIVE
    /// (404) or the request failed and nothing is cached. Never throws.
    /// </returns>
    public async Task<Assignment?> GetAssignmentAsync(
        string experimentKey,
        string userId,
        Dictionary<string, object>? attributes = null)
    {
        var cached = _assignmentCache.Get(userId, experimentKey);
        if (cached != null)
            return cached;

        try
        {
            var body = new AssignRequest
            {
                ExperimentKey = experimentKey,
                UserId = userId,
                Context = attributes
            };
            var assignment = await _httpClient.PostAsync<Assignment>("/api/v1/tracking/assign", body).ConfigureAwait(false);
            if (string.IsNullOrEmpty(assignment.ExperimentKey))
                assignment.ExperimentKey = experimentKey;
            if (string.IsNullOrEmpty(assignment.UserId))
                assignment.UserId = userId;

            _assignmentCache.Set(userId, experimentKey, assignment);
            return assignment;
        }
        catch (Exception)
        {
            return null;
        }
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
