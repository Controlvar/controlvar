using System.Text.Json;
using System.Text.Json.Serialization;

namespace Controlvar.Models;

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

    /// <summary>
    /// The value the flag serves this user: its on value when <see cref="Enabled"/> is true, its
    /// off value otherwise. <c>null</c> when the answer has none: a server from before flag values,
    /// or one that cannot serve the flag's stored value. The typed getters
    /// (<see cref="ExperimentationClient.GetStringValueAsync"/> and the others) read it, with the
    /// checks this property does not make.
    /// </summary>
    [JsonPropertyName("value")]
    public JsonElement? Value { get; set; }

    /// <summary>
    /// The type of <see cref="Value"/> as the server names it: <c>"boolean"</c>,
    /// <c>"string"</c>, <c>"number"</c> or <c>"json"</c>. <c>null</c> when the answer has none, or
    /// has one that is not a JSON string (then the answer is still read, and the typed getters
    /// take the value's own JSON type).
    /// </summary>
    [JsonPropertyName("value_type")]
    [JsonConverter(typeof(StringOrNoneConverter))]
    public string? ValueType { get; set; }

    /// <summary>The idiomatic "unknown" result returned when a request fails and nothing is cached.</summary>
    public static FlagEvaluationResult Disabled(string flagKey) =>
        new() { Key = flagKey, Enabled = false, Config = null };

    /// <summary>
    /// Reads a JSON string as itself and any other JSON value as <c>null</c>, so an answer whose
    /// <c>value_type</c> is not a string is not refused whole (which would turn the flag off).
    /// </summary>
    private sealed class StringOrNoneConverter : JsonConverter<string?>
    {
        public override string? Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
        {
            if (reader.TokenType == JsonTokenType.String)
                return reader.GetString();
            reader.Skip();
            return null;
        }

        public override void Write(Utf8JsonWriter writer, string? value, JsonSerializerOptions options)
        {
            if (value == null)
                writer.WriteNullValue();
            else
                writer.WriteStringValue(value);
        }
    }
}
