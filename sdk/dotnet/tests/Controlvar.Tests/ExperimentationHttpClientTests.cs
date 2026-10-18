using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using Controlvar;
using Controlvar.Exceptions;
using Controlvar.Models;
using Xunit;

namespace Controlvar.Tests;

/// <summary>
/// Tests for <see cref="ExperimentationHttpClient"/> using a mock HTTP message handler:
/// URL building, headers, JSON body, and HTTP error mapping.
/// </summary>
public class ExperimentationHttpClientTests
{
    // -----------------------------------------------------------------------
    // Mock handler
    // -----------------------------------------------------------------------

    /// <summary>Returns a pre-canned HTTP response and records the request (body read before disposal).</summary>
    private sealed class MockHttpMessageHandler : HttpMessageHandler
    {
        private readonly HttpResponseMessage _response;
        public HttpRequestMessage? LastRequest { get; private set; }
        public string? LastBody { get; private set; }
        public string? LastContentType { get; private set; }

        public MockHttpMessageHandler(HttpResponseMessage response) =>
            _response = response;

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            LastRequest = request;
            if (request.Content != null)
            {
                LastContentType = request.Content.Headers.ContentType?.MediaType;
                LastBody = await request.Content.ReadAsStringAsync().ConfigureAwait(false);
            }
            return _response;
        }
    }

    private static (ExperimentationHttpClient client, MockHttpMessageHandler handler) BuildClient(
        HttpStatusCode statusCode,
        string? body = null,
        string contentType = "application/json")
    {
        var response = new HttpResponseMessage(statusCode);
        if (body != null)
            response.Content = new StringContent(body, Encoding.UTF8, contentType);

        var handler = new MockHttpMessageHandler(response);
        // No BaseAddress: the wrapper resolves paths against SdkConfig.BaseUrl.
        var httpClient = new HttpClient(handler);
        var config = new SdkConfig("http://test-api", "test-api-key");
        var client = new ExperimentationHttpClient(config, httpClient);

        return (client, handler);
    }

    private const string FlagJson = "{\"key\":\"my-flag\",\"enabled\":true,\"config\":null}";

    // -----------------------------------------------------------------------
    // URL building
    // -----------------------------------------------------------------------

    [Fact]
    public void BuildUri_ResolvesPathAgainstOrigin_WithOrWithoutLeadingSlash()
    {
        var (client, _) = BuildClient(HttpStatusCode.OK, FlagJson);

        Assert.Equal("http://test-api/api/v1/tracking/assign", client.BuildUri("/api/v1/tracking/assign").AbsoluteUri);
        Assert.Equal("http://test-api/api/v1/tracking/assign", client.BuildUri("api/v1/tracking/assign").AbsoluteUri);
    }

    [Fact]
    public void BuildUri_EncodesQueryValues()
    {
        var (client, _) = BuildClient(HttpStatusCode.OK, FlagJson);

        var uri = client.BuildUri("/api/v1/feature-flags/evaluate/f",
            new Dictionary<string, string> { ["user_id"] = "user a&b=c" });

        Assert.Equal("/api/v1/feature-flags/evaluate/f", uri.AbsolutePath);
        Assert.Equal("?user_id=user%20a%26b%3Dc", uri.Query);
    }

    [Fact]
    public void SdkConfig_TrimsTrailingSlashFromBaseUrl()
    {
        var config = new SdkConfig("http://test-api///", "k");
        Assert.Equal("http://test-api", config.BaseUrl);
    }

    // -----------------------------------------------------------------------
    // Successful GET
    // -----------------------------------------------------------------------

    [Fact]
    public async Task GetAsync_SuccessfulResponse_DeserializesJson()
    {
        var (client, _) = BuildClient(HttpStatusCode.OK, FlagJson);

        var result = await client.GetAsync<FlagEvaluationResult>("/api/v1/feature-flags/evaluate/my-flag");

        Assert.NotNull(result);
        Assert.Equal("my-flag", result.Key);
        Assert.True(result.Enabled);
        Assert.Null(result.Config);
    }

    [Fact]
    public async Task GetAsync_SendsApiKeyAndAcceptHeaders()
    {
        var (client, handler) = BuildClient(HttpStatusCode.OK, FlagJson);

        await client.GetAsync<FlagEvaluationResult>("/api/v1/feature-flags/evaluate/my-flag",
            new Dictionary<string, string> { ["user_id"] = "user-1" });

        Assert.NotNull(handler.LastRequest);
        Assert.Equal(HttpMethod.Get, handler.LastRequest!.Method);
        Assert.Equal("http://test-api/api/v1/feature-flags/evaluate/my-flag?user_id=user-1",
            handler.LastRequest.RequestUri!.AbsoluteUri);
        Assert.True(handler.LastRequest.Headers.TryGetValues("X-API-Key", out var apiKeys));
        Assert.Equal("test-api-key", Assert.Single(apiKeys!));
        Assert.Contains(handler.LastRequest.Headers.Accept, a => a.MediaType == "application/json");
        Assert.Null(handler.LastRequest.Headers.Authorization);
    }

    // -----------------------------------------------------------------------
    // Successful POST
    // -----------------------------------------------------------------------

    [Fact]
    public async Task PostAsync_SendsJsonBodyWithContentType_AndDeserializesResponse()
    {
        const string responseJson =
            "{\"experiment_key\":\"exp-1\",\"user_id\":\"user-1\",\"variant_id\":\"v-1\",\"variant_name\":\"treatment\",\"is_control\":false,\"configuration\":null}";
        var (client, handler) = BuildClient(HttpStatusCode.OK, responseJson);

        var result = await client.PostAsync<Assignment>("/api/v1/tracking/assign",
            new Dictionary<string, object> { ["experiment_key"] = "exp-1", ["user_id"] = "user-1" });

        Assert.NotNull(result);
        Assert.Equal("exp-1", result.ExperimentKey);
        Assert.Equal("treatment", result.VariantName);

        Assert.Equal(HttpMethod.Post, handler.LastRequest!.Method);
        Assert.Equal("application/json", handler.LastContentType);
        Assert.True(handler.LastRequest.Headers.TryGetValues("X-API-Key", out _));
        var body = JsonDocument.Parse(handler.LastBody!).RootElement;
        Assert.Equal("exp-1", body.GetProperty("experiment_key").GetString());
        Assert.Equal("user-1", body.GetProperty("user_id").GetString());
    }

    [Fact]
    public async Task PostAsync_NonGeneric_IgnoresResponseBody()
    {
        var (client, handler) = BuildClient(HttpStatusCode.Created, "not json at all");

        var ex = await Record.ExceptionAsync(() => client.PostAsync("/api/v1/tracking/track", new { a = 1 }));

        Assert.Null(ex);
        Assert.Equal("{\"a\":1}", handler.LastBody);
    }

    [Fact]
    public async Task PostAsync_NonGeneric_EmptyResponseBodyIsFine()
    {
        var (client, _) = BuildClient(HttpStatusCode.NoContent);

        var ex = await Record.ExceptionAsync(() => client.PostAsync("/api/v1/tracking/track", new { a = 1 }));

        Assert.Null(ex);
    }

    [Fact]
    public async Task PostAsync_OmitsNullMembers()
    {
        var (client, handler) = BuildClient(HttpStatusCode.OK, "{}");

        await client.PostAsync("/api/v1/tracking/track",
            new TrackEvent { UserId = "u", EventName = "e", ExperimentKey = "x" });

        // TrackEvent itself is not the wire shape (the client sends TrackBody), but this proves
        // the shared serializer options drop null members.
        var body = JsonDocument.Parse(handler.LastBody!).RootElement;
        Assert.False(body.TryGetProperty("FeatureFlagKey", out _));
        Assert.False(body.TryGetProperty("Value", out _));
        Assert.Equal("x", body.GetProperty("ExperimentKey").GetString());
    }

    // -----------------------------------------------------------------------
    // HTTP 401 → AuthException
    // -----------------------------------------------------------------------

    [Fact]
    public async Task GetAsync_Http401_ThrowsAuthException()
    {
        var (client, _) = BuildClient(HttpStatusCode.Unauthorized, "{\"detail\": \"Unauthorized\"}");
        await Assert.ThrowsAsync<AuthException>(() => client.GetAsync<FlagEvaluationResult>("/api/v1/feature-flags/evaluate/x"));
    }

    [Fact]
    public async Task PostAsync_Http401_ThrowsAuthException()
    {
        var (client, _) = BuildClient(HttpStatusCode.Unauthorized);
        await Assert.ThrowsAsync<AuthException>(() => client.PostAsync<Assignment>("/api/v1/tracking/assign", new { }));
    }

    [Fact]
    public async Task PostAsync_NonGeneric_Http401_ThrowsAuthException()
    {
        var (client, _) = BuildClient(HttpStatusCode.Unauthorized);
        await Assert.ThrowsAsync<AuthException>(() => client.PostAsync("/api/v1/tracking/track", new { }));
    }

    // -----------------------------------------------------------------------
    // HTTP 404 / 422 / 500 → ApiException with correct StatusCode
    // -----------------------------------------------------------------------

    [Fact]
    public async Task GetAsync_Http404_ThrowsApiExceptionWithStatus404()
    {
        var (client, _) = BuildClient(HttpStatusCode.NotFound, "{\"detail\": \"Flag not found\"}");

        var ex = await Assert.ThrowsAsync<ApiException>(() => client.GetAsync<FlagEvaluationResult>("/api/v1/feature-flags/evaluate/ghost"));
        Assert.Equal(404, ex.StatusCode);
    }

    [Fact]
    public async Task PostAsync_Http422_ThrowsApiExceptionWithStatus422()
    {
        var (client, _) = BuildClient(HttpStatusCode.UnprocessableEntity, "{\"detail\": \"missing key\"}");

        var ex = await Assert.ThrowsAsync<ApiException>(() => client.PostAsync("/api/v1/tracking/track", new { }));
        Assert.Equal(422, ex.StatusCode);
    }

    [Fact]
    public async Task GetAsync_Http500_ThrowsApiException()
    {
        var (client, _) = BuildClient(HttpStatusCode.InternalServerError, "{\"detail\": \"Internal error\"}");

        var ex = await Assert.ThrowsAsync<ApiException>(() => client.GetAsync<FlagEvaluationResult>("/api/v1/feature-flags/evaluate/err"));
        Assert.Equal(500, ex.StatusCode);
    }

    [Fact]
    public async Task GetAsync_MalformedBody_ThrowsApiException()
    {
        var (client, _) = BuildClient(HttpStatusCode.OK, "not json");

        await Assert.ThrowsAsync<ApiException>(() => client.GetAsync<FlagEvaluationResult>("/api/v1/feature-flags/evaluate/x"));
    }

    // -----------------------------------------------------------------------
    // Network error → NetworkException
    // -----------------------------------------------------------------------

    private sealed class ThrowingHandler : HttpMessageHandler
    {
        private readonly Exception _ex;
        public ThrowingHandler(Exception ex) => _ex = ex;
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
            => throw _ex;
    }

    [Fact]
    public async Task GetAsync_NetworkError_ThrowsNetworkException()
    {
        var httpClient = new HttpClient(new ThrowingHandler(new HttpRequestException("Connection refused")));
        var config = new SdkConfig("http://unreachable", "key");
        var client = new ExperimentationHttpClient(config, httpClient);

        await Assert.ThrowsAsync<NetworkException>(() => client.GetAsync<FlagEvaluationResult>("/api/v1/feature-flags/evaluate/x"));
    }

    [Fact]
    public async Task PostAsync_Timeout_ThrowsNetworkException()
    {
        var httpClient = new HttpClient(new ThrowingHandler(new TaskCanceledException("timed out")));
        var config = new SdkConfig("http://unreachable", "key");
        var client = new ExperimentationHttpClient(config, httpClient);

        await Assert.ThrowsAsync<NetworkException>(() => client.PostAsync("/api/v1/tracking/track", new { }));
    }

    // -----------------------------------------------------------------------
    // ApiException carries response body
    // -----------------------------------------------------------------------

    [Fact]
    public async Task GetAsync_HttpError_ApiExceptionContainsResponseBody()
    {
        const string errorBody = "{\"detail\": \"Feature flag not found\"}";
        var (client, _) = BuildClient(HttpStatusCode.NotFound, errorBody);

        var ex = await Assert.ThrowsAsync<ApiException>(() => client.GetAsync<FlagEvaluationResult>("/api/v1/feature-flags/evaluate/missing"));
        Assert.Equal(errorBody, ex.ResponseBody);
    }

    // -----------------------------------------------------------------------
    // Injected HttpClient is not mutated
    // -----------------------------------------------------------------------

    [Fact]
    public async Task InjectedHttpClient_DefaultHeadersAreLeftAlone()
    {
        var handler = new MockHttpMessageHandler(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(FlagJson, Encoding.UTF8, "application/json")
        });
        var httpClient = new HttpClient(handler);
        var client = new ExperimentationHttpClient(new SdkConfig("http://test-api", "k"), httpClient);

        await client.GetAsync<FlagEvaluationResult>("/api/v1/feature-flags/evaluate/f");

        Assert.False(httpClient.DefaultRequestHeaders.Contains("X-API-Key"));
        Assert.True(handler.LastRequest!.Headers.Contains("X-API-Key"));
    }
}
