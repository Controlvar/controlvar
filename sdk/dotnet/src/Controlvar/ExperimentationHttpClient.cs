using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Controlvar.Exceptions;

namespace Controlvar;

/// <summary>
/// Wraps <see cref="System.Net.Http.HttpClient"/> with authentication, serialisation,
/// and error mapping for the Controlvar API.
///
/// Every request carries <c>X-API-Key: &lt;key&gt;</c>, <c>Accept: application/json</c> and (for
/// POST) <c>Content-Type: application/json</c>. Paths are resolved against
/// <see cref="SdkConfig.BaseUrl"/> (the API origin), so an injected <see cref="HttpClient"/> does
/// not need a <see cref="HttpClient.BaseAddress"/>.
/// </summary>
public class ExperimentationHttpClient : IDisposable
{
    private readonly SdkConfig _config;
    private readonly HttpClient _httpClient;
    private readonly bool _ownsHttpClient;
    private bool _disposed;

    /// <summary>Serializer options shared by all requests: snake_case is set per property via attributes; nulls are omitted.</summary>
    internal static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    /// <summary>
    /// Creates a new HTTP client wrapper.
    /// </summary>
    /// <param name="config">SDK configuration including base URL and API key.</param>
    /// <param name="httpClient">
    /// Optional pre-configured <see cref="HttpClient"/> (for testing or connection pooling). If not
    /// supplied a new instance is created with <see cref="SdkConfig.TimeoutSeconds"/> as its timeout
    /// (and disposed when this object is disposed). Injected clients are never mutated.
    /// </param>
    public ExperimentationHttpClient(SdkConfig config, HttpClient? httpClient = null)
    {
        _config = config ?? throw new ArgumentNullException(nameof(config));

        if (httpClient != null)
        {
            _httpClient = httpClient;
            _ownsHttpClient = false;
        }
        else
        {
            _httpClient = new HttpClient
            {
                Timeout = TimeSpan.FromSeconds(config.TimeoutSeconds)
            };
            _ownsHttpClient = true;
        }
    }

    /// <summary>Sends a GET request and deserialises the JSON response.</summary>
    /// <typeparam name="T">The expected response type.</typeparam>
    /// <param name="path">
    /// API path relative to the origin, e.g. <c>/api/v1/feature-flags/evaluate/my-flag</c>. Path
    /// segments must already be percent-encoded (<see cref="Uri.EscapeDataString"/>).
    /// </param>
    /// <param name="query">Optional query parameters; keys and values are percent-encoded here.</param>
    /// <returns>The deserialised response body.</returns>
    /// <exception cref="AuthException">Thrown for HTTP 401.</exception>
    /// <exception cref="ApiException">Thrown for other non-success HTTP codes or an undecodable body.</exception>
    /// <exception cref="NetworkException">Thrown for connectivity / timeout errors.</exception>
    public async Task<T> GetAsync<T>(string path, IReadOnlyDictionary<string, string>? query = null)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, BuildUri(path, query));
        var response = await SendAsync(request, "GET", path).ConfigureAwait(false);
        return await HandleResponseAsync<T>(response).ConfigureAwait(false);
    }

    /// <summary>Sends a POST request with a JSON body and deserialises the JSON response.</summary>
    /// <typeparam name="T">The expected response type.</typeparam>
    /// <param name="path">API path relative to the origin, e.g. <c>/api/v1/tracking/assign</c>.</param>
    /// <param name="body">The request body (will be serialised to JSON).</param>
    /// <returns>The deserialised response body.</returns>
    /// <exception cref="AuthException">Thrown for HTTP 401.</exception>
    /// <exception cref="ApiException">Thrown for other non-success HTTP codes or an undecodable body.</exception>
    /// <exception cref="NetworkException">Thrown for connectivity / timeout errors.</exception>
    public async Task<T> PostAsync<T>(string path, object body)
    {
        var response = await PostRawAsync(path, body).ConfigureAwait(false);
        return await HandleResponseAsync<T>(response).ConfigureAwait(false);
    }

    /// <summary>
    /// Sends a POST request with a JSON body and ignores the response body (used for tracking).
    /// Still throws on network errors and non-success HTTP codes.
    /// </summary>
    /// <param name="path">API path relative to the origin, e.g. <c>/api/v1/tracking/track</c>.</param>
    /// <param name="body">The request body (will be serialised to JSON).</param>
    /// <exception cref="AuthException">Thrown for HTTP 401.</exception>
    /// <exception cref="ApiException">Thrown for other non-success HTTP codes.</exception>
    /// <exception cref="NetworkException">Thrown for connectivity / timeout errors.</exception>
    public async Task PostAsync(string path, object body)
    {
        var response = await PostRawAsync(path, body).ConfigureAwait(false);
        await EnsureSuccessAsync(response).ConfigureAwait(false);
    }

    /// <summary>Resolves an API path (and optional query) against the configured origin.</summary>
    public Uri BuildUri(string path, IReadOnlyDictionary<string, string>? query = null)
    {
        var url = new StringBuilder(_config.BaseUrl);
        url.Append('/').Append(path.TrimStart('/'));

        if (query != null && query.Count > 0)
        {
            var separator = path.Contains('?') ? '&' : '?';
            foreach (var pair in query)
            {
                url.Append(separator)
                   .Append(Uri.EscapeDataString(pair.Key))
                   .Append('=')
                   .Append(Uri.EscapeDataString(pair.Value));
                separator = '&';
            }
        }

        return new Uri(url.ToString(), UriKind.Absolute);
    }

    // -----------------------------------------------------------------
    // Private helpers
    // -----------------------------------------------------------------

    private async Task<HttpResponseMessage> PostRawAsync(string path, object body)
    {
        string json = JsonSerializer.Serialize(body, body.GetType(), JsonOptions);
        using var request = new HttpRequestMessage(HttpMethod.Post, BuildUri(path))
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };
        return await SendAsync(request, "POST", path).ConfigureAwait(false);
    }

    private async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, string method, string path)
    {
        request.Headers.Add("X-API-Key", _config.ApiKey);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));

        try
        {
            return await _httpClient.SendAsync(request).ConfigureAwait(false);
        }
        catch (HttpRequestException ex)
        {
            throw new NetworkException($"Network error calling {method} {path}: {ex.Message}", ex);
        }
        catch (TaskCanceledException ex)
        {
            throw new NetworkException($"Request timed out calling {method} {path}: {ex.Message}", ex);
        }
    }

    private static async Task<string> EnsureSuccessAsync(HttpResponseMessage response)
    {
        string body = response.Content == null
            ? ""
            : await response.Content.ReadAsStringAsync().ConfigureAwait(false);

        if (response.StatusCode == HttpStatusCode.Unauthorized)
            throw new AuthException("Authentication failed (HTTP 401). Check your API key.");

        if (!response.IsSuccessStatusCode)
            throw new ApiException(
                (int)response.StatusCode,
                $"API request failed with status {(int)response.StatusCode} {response.ReasonPhrase}.",
                responseBody: body);

        return body;
    }

    private static async Task<T> HandleResponseAsync<T>(HttpResponseMessage response)
    {
        string body = await EnsureSuccessAsync(response).ConfigureAwait(false);

        if (typeof(T) == typeof(bool))
        {
            // Allow endpoints that return an empty 200/204 body to resolve as true.
            if (string.IsNullOrWhiteSpace(body))
                return (T)(object)true;
        }

        try
        {
            var result = JsonSerializer.Deserialize<T>(body, JsonOptions);
            if (result == null)
                throw new ApiException(
                    (int)response.StatusCode,
                    "API returned a null response body.",
                    responseBody: body);
            return result;
        }
        catch (JsonException ex)
        {
            throw new ApiException(
                (int)response.StatusCode,
                $"Failed to deserialise API response: {ex.Message}",
                ex,
                responseBody: body);
        }
    }

    /// <summary>Disposes the underlying <see cref="HttpClient"/> if this wrapper created it; injected clients are left alone.</summary>
    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        if (_ownsHttpClient)
            _httpClient.Dispose();
    }
}
