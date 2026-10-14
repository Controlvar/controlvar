using System.Text.Json;
using System.Text.Json.Serialization;

namespace Experimently.Models;

/// <summary>
/// The server's decision for one feature flag and one user
/// (<c>GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…</c>).
/// </summary>
public class FlagEvaluationResult
{
    /// <summary>The evaluated flag key.</summary>
    [JsonPropertyName("key")]
    public string Key { get; set; } = "";

    /// <summary>Whether the flag is enabled for this user (decided by the server).</summary>
    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; }

    /// <summary>
    /// <c>null</c> today: the evaluate route returns <c>config: null</c> for every flag. It
    /// holds the JSON value if the server ever sends one.
    /// </summary>
    [JsonPropertyName("config")]
    public JsonElement? Config { get; set; }

    /// <summary>The idiomatic "unknown" result returned when a request fails and nothing is cached.</summary>
    public static FlagEvaluationResult Disabled(string flagKey) =>
        new() { Key = flagKey, Enabled = false, Config = null };
}
