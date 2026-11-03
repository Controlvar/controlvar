namespace Controlvar.Models;

/// <summary>
/// One exposure the server recorded, as given to <see cref="SdkConfig.OnExposure"/>: the
/// experiment, the user and the variant the user was enrolled in.
/// </summary>
/// <remarks>
/// It holds four strings and nothing else: not the user's attributes and not the variant's
/// configuration. It is immutable, and the client builds a new one for each call, so nothing
/// the function does with it reaches the <see cref="Assignment"/> the client keeps. The
/// positional order is the order of <see cref="Assignment"/>'s properties. The constructor is
/// public so that a test of your function can build one too.
/// </remarks>
/// <param name="ExperimentKey">The key of the experiment.</param>
/// <param name="UserId">The user the server enrolled.</param>
/// <param name="VariantId">The id of the variant, or <c>null</c> when the server sends none.</param>
/// <param name="VariantName">The name of the variant, e.g. <c>treatment</c>.</param>
public sealed record Exposure(string ExperimentKey, string UserId, string? VariantId, string VariantName);
