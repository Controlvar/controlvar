using System.Text.Json;
using System.Text.Json.Serialization;

namespace Controlvar.Models;

/// <summary>
/// A user's assignment to an experiment variant (<c>POST /api/v1/tracking/assign</c>).
/// The assignment is sticky on the server: the same user always receives the same variant.
/// </summary>
public class Assignment
{
    /// <summary>The key of the experiment this assignment belongs to.</summary>
    [JsonPropertyName("experiment_key")]
    public string ExperimentKey { get; set; } = "";

    /// <summary>The user ID that was assigned.</summary>
    [JsonPropertyName("user_id")]
    public string UserId { get; set; } = "";

    /// <summary>UUID of the assigned variant.</summary>
    [JsonPropertyName("variant_id")]
    public string? VariantId { get; set; }

    /// <summary>Name of the assigned variant, e.g. <c>control</c> or <c>treatment</c>.</summary>
    [JsonPropertyName("variant_name")]
    public string VariantName { get; set; } = "";

    /// <summary><c>true</c> when the user landed in the control variant.</summary>
    [JsonPropertyName("is_control")]
    public bool IsControl { get; set; }

    /// <summary>
    /// The variant's <c>configuration</c> JSON from the experiment definition, or <c>null</c>.
    /// For an object payload use e.g. <c>assignment.Configuration?.GetProperty("headline").GetString()</c>.
    /// </summary>
    [JsonPropertyName("configuration")]
    public JsonElement? Configuration { get; set; }
}
