using System.Globalization;
using System.Text.Json.Serialization;

namespace Controlvar.Models;

/// <summary>
/// An analytics event. <see cref="ExperimentationClient.TrackAsync(TrackEvent)"/> sends it as the
/// body of <c>POST /api/v1/tracking/track</c> (or as one entry of <c>POST /api/v1/tracking/batch</c>).
/// </summary>
public class TrackEvent
{
    /// <summary>The name of the event (e.g. "purchase"). Experiment metrics match on this name.</summary>
    public string EventName { get; set; } = "";

    /// <summary>The user ID who triggered this event.</summary>
    public string UserId { get; set; } = "";

    /// <summary>Optional event type; defaults to <see cref="EventName"/> on the wire.</summary>
    public string? EventType { get; set; }

    /// <summary>
    /// Attribute the event to one experiment. When neither this nor <see cref="FeatureFlagKey"/>
    /// is set, the client fans the event out to every cached assignment and flag of the user.
    /// </summary>
    public string? ExperimentKey { get; set; }

    /// <summary>Attribute the event to one feature flag.</summary>
    public string? FeatureFlagKey { get; set; }

    /// <summary>Optional numeric value (revenue, duration, …).</summary>
    public double? Value { get; set; }

    /// <summary>Optional key-value properties; sent as <c>metadata</c>.</summary>
    public Dictionary<string, object>? Properties { get; set; }

    /// <summary>Optional event time; sent as ISO-8601 (UTC). The server stamps the event when omitted.</summary>
    public DateTimeOffset? Timestamp { get; set; }

    /// <summary><c>true</c> when the event is attributed to an experiment or a feature flag.</summary>
    [JsonIgnore]
    public bool HasKey => ExperimentKey != null || FeatureFlagKey != null;

    /// <summary>Returns a copy attributed to the given experiment and/or flag.</summary>
    public TrackEvent Attributed(string? experimentKey = null, string? featureFlagKey = null) => new()
    {
        EventName = EventName,
        UserId = UserId,
        EventType = EventType,
        ExperimentKey = experimentKey,
        FeatureFlagKey = featureFlagKey,
        Value = Value,
        Properties = Properties,
        Timestamp = Timestamp
    };

    /// <summary>Builds the wire representation (<c>event_type</c> defaults to the event name).</summary>
    internal TrackBody ToBody() => new()
    {
        EventType = EventType ?? EventName,
        EventName = EventName,
        UserId = UserId,
        ExperimentKey = ExperimentKey,
        FeatureFlagKey = FeatureFlagKey,
        Value = Value,
        Metadata = Properties,
        Timestamp = Timestamp?.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture)
    };
}

/// <summary>
/// Body of <c>POST /api/v1/tracking/track</c> and of each <c>POST /api/v1/tracking/batch</c> entry.
/// Null members are omitted when serialised.
/// </summary>
internal sealed class TrackBody
{
    [JsonPropertyName("event_type")]
    public string EventType { get; set; } = "";

    [JsonPropertyName("event_name")]
    public string EventName { get; set; } = "";

    [JsonPropertyName("user_id")]
    public string UserId { get; set; } = "";

    [JsonPropertyName("experiment_key")]
    public string? ExperimentKey { get; set; }

    [JsonPropertyName("feature_flag_key")]
    public string? FeatureFlagKey { get; set; }

    [JsonPropertyName("value")]
    public double? Value { get; set; }

    [JsonPropertyName("metadata")]
    public Dictionary<string, object>? Metadata { get; set; }

    [JsonPropertyName("timestamp")]
    public string? Timestamp { get; set; }
}
