using Controlvar.Models;

namespace Controlvar;

/// <summary>
/// Configuration for the ExperimentationClient SDK.
/// </summary>
public class SdkConfig
{
    private readonly Action<Exposure>? _onExposure;

    /// <summary>The origin of the Controlvar API (e.g. "https://api.example.com"); the SDK appends "/api/v1/...".</summary>
    public string BaseUrl { get; init; }

    /// <summary>The API key, sent as the <c>X-API-Key</c> header on every request.</summary>
    public string ApiKey { get; init; }

    /// <summary>How long a successful flag evaluation / experiment assignment is cached per user + key, in seconds. Default: 300 (5 minutes).</summary>
    public int CacheTtlSeconds { get; init; } = 300;

    /// <summary>HTTP request timeout in seconds. Default: 10.</summary>
    public int TimeoutSeconds { get; init; } = 10;

    /// <summary>Maximum number of entries in each in-memory cache (evaluations, assignments). Default: 1000.</summary>
    public int MaxCacheSize { get; init; } = 1000;

    /// <summary>
    /// A function the client calls once each time the server enrols the user in an experiment and
    /// the answer did not come from the client's cache: the moment the server records the
    /// exposure. Default: <c>null</c>, none.
    /// </summary>
    /// <remarks>
    /// <para><see cref="ExperimentationClient.GetAssignmentAsync"/> calls it when it stores an
    /// answer from <c>POST /api/v1/tracking/assign</c> that says <c>"assigned": true</c> for a
    /// user and experiment with no unexpired cached answer in that client. It is not called for a
    /// cached answer, for the second of two calls that asked for the same new user and experiment
    /// at the same moment, for an <c>"assigned": false</c> answer or one without
    /// <c>assigned</c>, for a call that fails, or for a feature flag. It is called again after the
    /// cache lifetime, after <see cref="ExperimentationClient.ClearCache"/>, after eviction from a
    /// full cache, and in every new client.</para>
    /// <para>It runs inside <c>GetAssignmentAsync</c>, after the answer is cached and before its
    /// task completes, usually on a thread-pool thread (do not assume a UI thread), with no lock
    /// of the client held, so its running time is added to that call and the client cannot time
    /// it out. It can run on several threads at once. Hand the exposure to a queue, such as a
    /// <c>System.Threading.Channels</c> channel, that another task sends from.</para>
    /// <para>If it throws, the client catches the exception and reports nothing: the answer is
    /// unchanged. Each function combined into the delegate is called in turn, and one that throws
    /// does not stop the next.</para>
    /// </remarks>
    /// <exception cref="ArgumentException">Thrown when it is set to an async lambda or
    /// <c>async</c> method, alone or combined with others: it would run as <c>async void</c>, and
    /// an exception after its first <c>await</c> would end the process.</exception>
    public Action<Exposure>? OnExposure
    {
        get => _onExposure;
        init
        {
            if (value != null)
                ExposureHook.RefuseAsync(value, nameof(OnExposure));
            _onExposure = value;
        }
    }

    /// <summary>
    /// Initialises a new SdkConfig.
    /// </summary>
    /// <param name="baseUrl">Required. The API base URL (must not be null or empty).</param>
    /// <param name="apiKey">Required. The API key (must not be null or empty).</param>
    /// <exception cref="ArgumentException">Thrown if baseUrl or apiKey is null or empty.</exception>
    public SdkConfig(string baseUrl, string apiKey)
    {
        if (string.IsNullOrWhiteSpace(baseUrl))
            throw new ArgumentException("BaseUrl is required and must not be empty.", nameof(baseUrl));
        if (string.IsNullOrWhiteSpace(apiKey))
            throw new ArgumentException("ApiKey is required and must not be empty.", nameof(apiKey));

        BaseUrl = baseUrl.TrimEnd('/');
        ApiKey = apiKey;
    }
}
