using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using Controlvar;
using Controlvar.Models;
using Xunit;

namespace Controlvar.Tests;

/// <summary>
/// Tests for <see cref="ExperimentationClient"/> against the backend contract: request URL,
/// method, headers and JSON body of every call, response mapping, per-user caching with TTL,
/// the track fan-out rule, and the "never throws" guarantees.
/// </summary>
public class ExperimentationClientTests
{
    // -----------------------------------------------------------------------
    // Recording mock handler
    // -----------------------------------------------------------------------

    /// <summary>Everything worth asserting about one request, captured before the content is disposed.</summary>
    public sealed class RecordedRequest
    {
        public string Method { get; set; } = "";
        public string Url { get; set; } = "";
        public string Path { get; set; } = "";
        public string Query { get; set; } = "";
        public Dictionary<string, string> Headers { get; } = new(StringComparer.OrdinalIgnoreCase);
        public string? ContentType { get; set; }
        public string? Body { get; set; }

        public JsonElement Json => JsonDocument.Parse(Body ?? "{}").RootElement.Clone();
    }

    private sealed class MockHandler : HttpMessageHandler
    {
        private readonly object _lock = new();
        private readonly Queue<(HttpStatusCode status, string body)> _responses = new();
        private (HttpStatusCode status, string body)? _sticky;
        private Exception? _error;

        public List<RecordedRequest> Requests { get; } = new();
        public int CallCount => Requests.Count;

        /// <summary>Queue one response (consumed in order).</summary>
        public void Enqueue(HttpStatusCode status, string body)
        {
            lock (_lock) _responses.Enqueue((status, body));
        }

        /// <summary>Answer every request with this response until changed.</summary>
        public void Respond(HttpStatusCode status, string body)
        {
            lock (_lock)
            {
                _error = null;
                _sticky = (status, body);
            }
        }

        /// <summary>Throw this exception for every request until changed.</summary>
        public void Fail(Exception error)
        {
            lock (_lock) _error = error;
        }

        public void Reset()
        {
            lock (_lock)
            {
                Requests.Clear();
                _responses.Clear();
                _sticky = null;
                _error = null;
            }
        }

        public RecordedRequest Last => Requests[Requests.Count - 1];

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var recorded = new RecordedRequest
            {
                Method = request.Method.Method,
                Url = request.RequestUri!.AbsoluteUri,
                Path = request.RequestUri!.AbsolutePath,
                Query = request.RequestUri!.Query
            };
            foreach (var header in request.Headers)
                recorded.Headers[header.Key] = string.Join(",", header.Value);
            if (request.Content != null)
            {
                recorded.ContentType = request.Content.Headers.ContentType?.MediaType;
                recorded.Body = await request.Content.ReadAsStringAsync().ConfigureAwait(false);
            }

            (HttpStatusCode status, string body) reply;
            lock (_lock)
            {
                Requests.Add(recorded);
                if (_error != null)
                    throw _error;
                if (_responses.Count > 0)
                    reply = _responses.Dequeue();
                else if (_sticky.HasValue)
                    reply = _sticky.Value;
                else
                    reply = (HttpStatusCode.NotFound, "{\"detail\":\"not found\"}");
            }

            return new HttpResponseMessage(reply.status)
            {
                Content = new StringContent(reply.body, Encoding.UTF8, "application/json")
            };
        }
    }

    // -----------------------------------------------------------------------
    // Fixtures
    // -----------------------------------------------------------------------

    private const string AssignJson =
        "{\"experiment_key\":\"exp-1\",\"user_id\":\"user-1\",\"variant_id\":\"11111111-2222-3333-4444-555555555555\"," +
        "\"variant_name\":\"treatment\",\"is_control\":false,\"configuration\":{\"headline\":\"Go\",\"limit\":3}}";

    private const string FlagJson =
        "{\"key\":\"new-ui\",\"enabled\":true,\"config\":{\"variant\":\"blue\",\"limit\":5}}";

    private static (ExperimentationClient client, MockHandler handler) BuildClient(
        int cacheTtl = 300,
        int cacheSize = 100,
        string apiKey = "test-key")
    {
        var handler = new MockHandler();
        // No BaseAddress on purpose: the SDK must resolve paths against SdkConfig.BaseUrl.
        var httpClient = new HttpClient(handler);
        var config = new SdkConfig("http://test-api/", apiKey)
        {
            CacheTtlSeconds = cacheTtl,
            MaxCacheSize = cacheSize
        };
        var client = new ExperimentationClient(config, httpClient);
        return (client, handler);
    }

    private sealed class ThrowingHandler : HttpMessageHandler
    {
        private readonly Exception _ex;
        public ThrowingHandler(Exception ex) => _ex = ex;
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
            => throw _ex;
    }

    // -----------------------------------------------------------------------
    // EvaluateFlagAsync — request shape
    // -----------------------------------------------------------------------

    [Fact]
    public async Task EvaluateFlagAsync_SendsGetWithEncodedKeyUserIdQueryAndHeaders()
    {
        var (client, handler) = BuildClient(apiKey: "secret-key");
        handler.Respond(HttpStatusCode.OK, FlagJson);

        await client.EvaluateFlagAsync("new ui", "user a&b");

        Assert.Equal(1, handler.CallCount);
        var request = handler.Last;
        Assert.Equal("GET", request.Method);
        Assert.Equal("http://test-api/api/v1/feature-flags/evaluate/new%20ui?user_id=user%20a%26b", request.Url);
        Assert.Equal("/api/v1/feature-flags/evaluate/new%20ui", request.Path);
        Assert.Equal("?user_id=user%20a%26b", request.Query);
        Assert.Equal("secret-key", request.Headers["X-API-Key"]);
        Assert.Equal("application/json", request.Headers["Accept"]);
        Assert.Null(request.Body);
    }

    [Fact]
    public async Task EvaluateFlagAsync_MapsKeyEnabledAndConfig()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, FlagJson);

        var result = await client.EvaluateFlagAsync("new-ui", "user-1");

        Assert.Equal("new-ui", result.Key);
        Assert.True(result.Enabled);
        Assert.NotNull(result.Config);
        Assert.Equal("blue", result.Config!.Value.GetProperty("variant").GetString());
        Assert.Equal(5, result.Config!.Value.GetProperty("limit").GetInt32());
    }

    [Fact]
    public async Task EvaluateFlagAsync_DisabledFlagWithNullConfig()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "{\"key\":\"off\",\"enabled\":false,\"config\":null}");

        var result = await client.EvaluateFlagAsync("off", "user-1");

        Assert.False(result.Enabled);
        Assert.Null(result.Config);
    }

    [Fact]
    public async Task EvaluateFlagAsync_ScalarConfigIsExposedAsIs()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "{\"key\":\"k\",\"enabled\":true,\"config\":\"blue\"}");

        var result = await client.EvaluateFlagAsync("k", "user-1");

        Assert.True(result.Enabled);
        Assert.Equal(JsonValueKind.String, result.Config!.Value.ValueKind);
        Assert.Equal("blue", result.Config!.Value.GetString());
    }

    [Fact]
    public async Task EvaluateFlagAsync_MissingKeyInResponseFallsBackToRequestedKey()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "{\"enabled\":true}");

        var result = await client.EvaluateFlagAsync("requested", "user-1");

        Assert.Equal("requested", result.Key);
        Assert.True(result.Enabled);
    }

    // -----------------------------------------------------------------------
    // EvaluateFlagAsync — caching
    // -----------------------------------------------------------------------

    [Fact]
    public async Task EvaluateFlagAsync_SecondCallIsServedFromCache()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, FlagJson);

        await client.EvaluateFlagAsync("new-ui", "user-1");
        handler.Respond(HttpStatusCode.InternalServerError, "gone");
        var result = await client.EvaluateFlagAsync("new-ui", "user-1");

        Assert.True(result.Enabled);
        Assert.Equal(1, handler.CallCount);
        var flags = client.GetEvaluatedFlags("user-1");
        Assert.Single(flags);
        Assert.Equal("new-ui", flags[0]);
    }

    [Fact]
    public async Task EvaluateFlagAsync_CacheIsPerUser()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, FlagJson);

        await client.EvaluateFlagAsync("new-ui", "user-A");
        await client.EvaluateFlagAsync("new-ui", "user-B");

        Assert.Equal(2, handler.CallCount);
        var flags = client.GetEvaluatedFlags("user-B");
        Assert.Single(flags);
        Assert.Equal("new-ui", flags[0]);
    }

    [Fact]
    public async Task EvaluateFlagAsync_CacheExpiresAfterTtl()
    {
        var (client, handler) = BuildClient(cacheTtl: 1);
        handler.Respond(HttpStatusCode.OK, FlagJson);

        await client.EvaluateFlagAsync("new-ui", "user-1");
        await Task.Delay(1200);
        handler.Respond(HttpStatusCode.OK, "{\"key\":\"new-ui\",\"enabled\":false,\"config\":null}");
        var result = await client.EvaluateFlagAsync("new-ui", "user-1");

        Assert.False(result.Enabled);
        Assert.Equal(2, handler.CallCount);
    }

    [Fact]
    public async Task EvaluateFlagAsync_ClearCacheForcesRefetch()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, FlagJson);

        await client.EvaluateFlagAsync("new-ui", "user-1");
        client.ClearCache();
        await client.EvaluateFlagAsync("new-ui", "user-1");

        Assert.Equal(2, handler.CallCount);
        Assert.Empty(client.GetEvaluatedFlags("nobody"));
    }

    // -----------------------------------------------------------------------
    // EvaluateFlagAsync — failures never throw and are never cached
    // -----------------------------------------------------------------------

    [Fact]
    public async Task EvaluateFlagAsync_404ReturnsDisabledAndIsNotCached()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.NotFound, "{\"detail\":\"Feature flag with key 'ghost' not found\"}");

        var result = await client.EvaluateFlagAsync("ghost", "user-1");

        Assert.False(result.Enabled);
        Assert.Equal("ghost", result.Key);
        Assert.Null(result.Config);
        Assert.Empty(client.GetEvaluatedFlags("user-1"));

        await client.EvaluateFlagAsync("ghost", "user-1");
        Assert.Equal(2, handler.CallCount);
    }

    [Fact]
    public async Task EvaluateFlagAsync_OnServerErrorReturnsDisabled()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.InternalServerError, "{\"detail\":\"boom\"}");

        var result = await client.EvaluateFlagAsync("broken-flag", "user-1");

        Assert.False(result.Enabled);
    }

    [Fact]
    public async Task EvaluateFlagAsync_OnAuthErrorReturnsDisabled()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.Unauthorized, "{\"detail\":\"unauthorized\"}");

        var result = await client.EvaluateFlagAsync("any-flag", "user-1");

        Assert.False(result.Enabled);
    }

    [Fact]
    public async Task EvaluateFlagAsync_OnNetworkErrorReturnsDisabled()
    {
        var httpClient = new HttpClient(new ThrowingHandler(new HttpRequestException("Connection refused")));
        var client = new ExperimentationClient(new SdkConfig("http://test", "key"), httpClient);

        var result = await client.EvaluateFlagAsync("any-flag", "user-1");

        Assert.False(result.Enabled);
        Assert.Empty(client.GetEvaluatedFlags("user-1"));
    }

    [Fact]
    public async Task EvaluateFlagAsync_OnMalformedBodyReturnsDisabled()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "not json");

        var result = await client.EvaluateFlagAsync("bad", "user-1");

        Assert.False(result.Enabled);
    }

    // -----------------------------------------------------------------------
    // GetAssignmentAsync — request shape
    // -----------------------------------------------------------------------

    [Fact]
    public async Task GetAssignmentAsync_PostsExperimentKeyUserIdAndContext()
    {
        var (client, handler) = BuildClient(apiKey: "secret-key");
        handler.Respond(HttpStatusCode.OK, AssignJson);

        await client.GetAssignmentAsync("exp-1", "user-1",
            new Dictionary<string, object> { { "plan", "pro" }, { "age", 30 }, { "beta", true } });

        Assert.Equal(1, handler.CallCount);
        var request = handler.Last;
        Assert.Equal("POST", request.Method);
        Assert.Equal("http://test-api/api/v1/tracking/assign", request.Url);
        Assert.Equal("secret-key", request.Headers["X-API-Key"]);
        Assert.Equal("application/json", request.Headers["Accept"]);
        Assert.Equal("application/json", request.ContentType);

        var body = request.Json;
        Assert.Equal("exp-1", body.GetProperty("experiment_key").GetString());
        Assert.Equal("user-1", body.GetProperty("user_id").GetString());
        var context = body.GetProperty("context");
        Assert.Equal("pro", context.GetProperty("plan").GetString());
        Assert.Equal(30, context.GetProperty("age").GetInt32());
        Assert.True(context.GetProperty("beta").GetBoolean());
    }

    [Fact]
    public async Task GetAssignmentAsync_OmitsContextWithoutAttributes()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, AssignJson);

        await client.GetAssignmentAsync("exp-1", "user-1");

        var body = handler.Last.Json;
        Assert.False(body.TryGetProperty("context", out _));
        Assert.Equal("exp-1", body.GetProperty("experiment_key").GetString());
        Assert.Equal("user-1", body.GetProperty("user_id").GetString());
    }

    [Fact]
    public async Task GetAssignmentAsync_MapsResponse()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, AssignJson);

        var assignment = await client.GetAssignmentAsync("exp-1", "user-1");

        Assert.NotNull(assignment);
        Assert.Equal("exp-1", assignment!.ExperimentKey);
        Assert.Equal("user-1", assignment.UserId);
        Assert.Equal("11111111-2222-3333-4444-555555555555", assignment.VariantId);
        Assert.Equal("treatment", assignment.VariantName);
        Assert.False(assignment.IsControl);
        Assert.NotNull(assignment.Configuration);
        Assert.Equal("Go", assignment.Configuration!.Value.GetProperty("headline").GetString());
        Assert.Equal(3, assignment.Configuration!.Value.GetProperty("limit").GetInt32());
    }

    [Fact]
    public async Task GetAssignmentAsync_ControlWithNullConfiguration()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK,
            "{\"experiment_key\":\"exp-1\",\"user_id\":\"u\",\"variant_id\":\"v\",\"variant_name\":\"control\",\"is_control\":true,\"configuration\":null}");

        var assignment = await client.GetAssignmentAsync("exp-1", "u");

        Assert.NotNull(assignment);
        Assert.True(assignment!.IsControl);
        Assert.Equal("control", assignment.VariantName);
        Assert.Null(assignment.Configuration);
    }

    // -----------------------------------------------------------------------
    // GetAssignmentAsync — caching and failures
    // -----------------------------------------------------------------------

    [Fact]
    public async Task GetAssignmentAsync_IsStickyFromCache()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, AssignJson);

        var first = await client.GetAssignmentAsync("exp-1", "user-1");
        handler.Respond(HttpStatusCode.InternalServerError, "gone");
        var second = await client.GetAssignmentAsync("exp-1", "user-1");

        Assert.NotNull(second);
        Assert.Equal(first!.VariantName, second!.VariantName);
        Assert.Equal(1, handler.CallCount);
        var cached = client.GetAssignments("user-1");
        Assert.Single(cached);
        Assert.Equal("exp-1", cached[0].ExperimentKey);
    }

    [Fact]
    public async Task GetAssignmentAsync_CacheIsPerUser()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, AssignJson);

        await client.GetAssignmentAsync("exp-1", "user-A");
        await client.GetAssignmentAsync("exp-1", "user-B");
        await client.GetAssignmentAsync("exp-1", "user-A");

        Assert.Equal(2, handler.CallCount);
        Assert.Single(client.GetAssignments("user-A"));
        Assert.Single(client.GetAssignments("user-B"));
        Assert.Empty(client.GetAssignments("user-C"));
    }

    [Fact]
    public async Task GetAssignmentAsync_404ReturnsNullAndIsNotCached()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.NotFound, "{\"detail\":\"No experiment has the key 'ghost-exp'.\"}");

        var result = await client.GetAssignmentAsync("ghost-exp", "user-1");

        Assert.Null(result);
        Assert.Empty(client.GetAssignments("user-1"));

        await client.GetAssignmentAsync("ghost-exp", "user-1");
        Assert.Equal(2, handler.CallCount);
    }

    [Fact]
    public async Task GetAssignmentAsync_OnServerErrorReturnsNull()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.InternalServerError, "{\"detail\":\"boom\"}");

        Assert.Null(await client.GetAssignmentAsync("exp-1", "user-1"));
    }

    [Fact]
    public async Task GetAssignmentAsync_OnNetworkErrorReturnsNull()
    {
        var httpClient = new HttpClient(new ThrowingHandler(new HttpRequestException("Connection refused")));
        var client = new ExperimentationClient(new SdkConfig("http://test", "key"), httpClient);

        Assert.Null(await client.GetAssignmentAsync("exp-1", "user-1"));
    }

    // -----------------------------------------------------------------------
    // TrackAsync with a key
    // -----------------------------------------------------------------------

    [Fact]
    public async Task TrackAsync_WithExperimentKey_PostsToTrackingTrack()
    {
        var (client, handler) = BuildClient(apiKey: "secret-key");
        handler.Respond(HttpStatusCode.OK, "{\"id\":\"evt-1\"}");

        bool ok = await client.TrackAsync(
            "purchase",
            "user-1",
            properties: new Dictionary<string, object> { { "sku", "pro" }, { "qty", 2 } },
            experimentKey: "exp-1",
            value: 12.5,
            timestamp: new DateTimeOffset(2023, 11, 14, 22, 13, 20, TimeSpan.Zero));

        Assert.True(ok);
        Assert.Equal(1, handler.CallCount);
        var request = handler.Last;
        Assert.Equal("POST", request.Method);
        Assert.Equal("http://test-api/api/v1/tracking/track", request.Url);
        Assert.Equal("secret-key", request.Headers["X-API-Key"]);
        Assert.Equal("application/json", request.ContentType);

        var body = request.Json;
        Assert.Equal("purchase", body.GetProperty("event_type").GetString());
        Assert.Equal("purchase", body.GetProperty("event_name").GetString());
        Assert.Equal("user-1", body.GetProperty("user_id").GetString());
        Assert.Equal("exp-1", body.GetProperty("experiment_key").GetString());
        Assert.False(body.TryGetProperty("feature_flag_key", out _));
        Assert.Equal(12.5, body.GetProperty("value").GetDouble());
        Assert.Equal("pro", body.GetProperty("metadata").GetProperty("sku").GetString());
        Assert.Equal(2, body.GetProperty("metadata").GetProperty("qty").GetInt32());
        Assert.Equal("2023-11-14T22:13:20.000Z", body.GetProperty("timestamp").GetString());
    }

    [Fact]
    public async Task TrackAsync_WithFeatureFlagKeyAndCustomEventType()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "{}");

        await client.TrackAsync("search", "user-1", featureFlagKey: "new-search", eventType: "interaction");

        var body = handler.Last.Json;
        Assert.Equal("interaction", body.GetProperty("event_type").GetString());
        Assert.Equal("search", body.GetProperty("event_name").GetString());
        Assert.Equal("new-search", body.GetProperty("feature_flag_key").GetString());
        Assert.False(body.TryGetProperty("experiment_key", out _));
        Assert.False(body.TryGetProperty("value", out _));
        Assert.False(body.TryGetProperty("metadata", out _));
        Assert.False(body.TryGetProperty("timestamp", out _));
    }

    [Fact]
    public async Task TrackAsync_WithBothKeys_SendsBothInOneTrackRequest()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "{}");

        bool ok = await client.TrackAsync("click", "user-1", experimentKey: "exp-1", featureFlagKey: "new-ui");

        Assert.True(ok);
        Assert.Equal(1, handler.CallCount);
        Assert.Equal("/api/v1/tracking/track", handler.Last.Path);
        var body = handler.Last.Json;
        Assert.Equal("exp-1", body.GetProperty("experiment_key").GetString());
        Assert.Equal("new-ui", body.GetProperty("feature_flag_key").GetString());
    }

    [Fact]
    public async Task TrackAsync_AcceptsPrebuiltTrackEvent()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "{}");

        bool ok = await client.TrackAsync(new TrackEvent { UserId = "user-1", EventName = "signup", ExperimentKey = "exp-1" });

        Assert.True(ok);
        Assert.Equal("exp-1", handler.Last.Json.GetProperty("experiment_key").GetString());
    }

    // -----------------------------------------------------------------------
    // TrackAsync without a key (fan-out)
    // -----------------------------------------------------------------------

    [Fact]
    public async Task TrackAsync_WithoutKeyAndNothingCached_SendsNothing()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "{}");

        bool ok = await client.TrackAsync("page_view", "user-1");

        Assert.True(ok);
        Assert.Equal(0, handler.CallCount);
    }

    [Fact]
    public async Task TrackAsync_WithoutKey_FansOutToCachedAssignmentsAndFlagsViaBatch()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, AssignJson);
        await client.GetAssignmentAsync("exp-1", "user-1");
        handler.Respond(HttpStatusCode.OK, FlagJson);
        await client.EvaluateFlagAsync("new-ui", "user-1");
        await client.EvaluateFlagAsync("new-ui", "someone-else");
        handler.Reset();
        handler.Respond(HttpStatusCode.OK, "{\"success_count\":2,\"failure_count\":0,\"errors\":null}");

        bool ok = await client.TrackAsync("page_view", "user-1",
            properties: new Dictionary<string, object> { { "page", "/" } }, value: 1);

        Assert.True(ok);
        Assert.Equal(1, handler.CallCount);
        Assert.Equal("POST", handler.Last.Method);
        Assert.Equal("http://test-api/api/v1/tracking/batch", handler.Last.Url);

        var events = handler.Last.Json.GetProperty("events");
        Assert.Equal(2, events.GetArrayLength());
        Assert.Equal("exp-1", events[0].GetProperty("experiment_key").GetString());
        Assert.False(events[0].TryGetProperty("feature_flag_key", out _));
        Assert.Equal("new-ui", events[1].GetProperty("feature_flag_key").GetString());
        Assert.False(events[1].TryGetProperty("experiment_key", out _));
        foreach (var evt in events.EnumerateArray())
        {
            Assert.Equal("page_view", evt.GetProperty("event_type").GetString());
            Assert.Equal("page_view", evt.GetProperty("event_name").GetString());
            Assert.Equal("user-1", evt.GetProperty("user_id").GetString());
            Assert.Equal(1.0, evt.GetProperty("value").GetDouble());
            Assert.Equal("/", evt.GetProperty("metadata").GetProperty("page").GetString());
        }
    }

    [Fact]
    public async Task TrackAsync_WithoutKey_IgnoresExpiredCacheEntries()
    {
        var (client, handler) = BuildClient(cacheTtl: 1);
        handler.Respond(HttpStatusCode.OK, FlagJson);
        await client.EvaluateFlagAsync("new-ui", "user-1");
        await Task.Delay(1200);
        handler.Reset();
        handler.Respond(HttpStatusCode.OK, "{}");

        await client.TrackAsync("page_view", "user-1");

        Assert.Equal(0, handler.CallCount);
    }

    // -----------------------------------------------------------------------
    // TrackAsync never throws
    // -----------------------------------------------------------------------

    [Fact]
    public async Task TrackAsync_OnApiError_ReturnsFalse()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.InternalServerError, "{\"detail\":\"error\"}");

        Assert.False(await client.TrackAsync("event", "user-1", experimentKey: "x"));
    }

    [Fact]
    public async Task TrackAsync_On422_ReturnsFalse()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.UnprocessableEntity, "{\"detail\":\"invalid\"}");

        Assert.False(await client.TrackAsync("event", "user-1", featureFlagKey: "f"));
        Assert.Equal(1, handler.CallCount);
    }

    [Fact]
    public async Task TrackAsync_OnNetworkError_ReturnsFalse()
    {
        var httpClient = new HttpClient(new ThrowingHandler(new HttpRequestException("Connection refused")));
        var client = new ExperimentationClient(new SdkConfig("http://test", "key"), httpClient);

        Assert.False(await client.TrackAsync("event", "user-1", experimentKey: "x"));
    }

    [Fact]
    public async Task TrackAsync_OnTimeout_ReturnsFalse()
    {
        var httpClient = new HttpClient(new ThrowingHandler(new TaskCanceledException("timed out")));
        var client = new ExperimentationClient(new SdkConfig("http://test", "key"), httpClient);

        Assert.False(await client.TrackAsync("event", "user-1", featureFlagKey: "f"));
    }

    // -----------------------------------------------------------------------
    // TrackBatchAsync
    // -----------------------------------------------------------------------

    [Fact]
    public async Task TrackBatchAsync_SendsOneBatchRequest()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "{\"success_count\":2,\"failure_count\":0,\"errors\":null}");

        bool ok = await client.TrackBatchAsync(new[]
        {
            new TrackEvent { UserId = "u", EventName = "add_to_cart", ExperimentKey = "exp-1", Value = 1 },
            new TrackEvent { UserId = "u", EventName = "checkout", FeatureFlagKey = "new-ui" }
        });

        Assert.True(ok);
        Assert.Equal(1, handler.CallCount);
        Assert.Equal("/api/v1/tracking/batch", handler.Last.Path);
        var events = handler.Last.Json.GetProperty("events");
        Assert.Equal(2, events.GetArrayLength());
        Assert.Equal("exp-1", events[0].GetProperty("experiment_key").GetString());
        Assert.Equal("new-ui", events[1].GetProperty("feature_flag_key").GetString());
    }

    [Fact]
    public async Task TrackBatchAsync_EmptyAndUnkeyedWithoutCache_SendNothing()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, "{}");

        Assert.True(await client.TrackBatchAsync(Array.Empty<TrackEvent>()));
        Assert.True(await client.TrackBatchAsync(new[] { new TrackEvent { UserId = "u", EventName = "page_view" } }));
        Assert.Equal(0, handler.CallCount);
    }

    [Fact]
    public async Task TrackBatchAsync_FansOutUnkeyedEventsAndChunksBy100()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.OK, AssignJson);
        await client.GetAssignmentAsync("exp-1", "u");
        handler.Reset();
        handler.Respond(HttpStatusCode.OK, "{}");

        var events = new List<TrackEvent>();
        for (int i = 0; i < 149; i++)
            events.Add(new TrackEvent { UserId = "u", EventName = $"e{i}", ExperimentKey = "exp-1" });
        events.Add(new TrackEvent { UserId = "u", EventName = "page_view" }); // fans out to exp-1 → 150 total

        bool ok = await client.TrackBatchAsync(events);

        Assert.True(ok);
        Assert.Equal(2, handler.CallCount);
        Assert.Equal(100, handler.Requests[0].Json.GetProperty("events").GetArrayLength());
        var second = handler.Requests[1].Json.GetProperty("events");
        Assert.Equal(50, second.GetArrayLength());
        Assert.Equal("page_view", second[49].GetProperty("event_name").GetString());
        Assert.Equal("exp-1", second[49].GetProperty("experiment_key").GetString());
    }

    [Fact]
    public async Task TrackBatchAsync_ReportsFailure()
    {
        var (client, handler) = BuildClient();
        handler.Respond(HttpStatusCode.TooManyRequests, "{\"detail\":\"rate limited\"}");

        Assert.False(await client.TrackBatchAsync(new[]
        {
            new TrackEvent { UserId = "u", EventName = "e", ExperimentKey = "x" }
        }));
    }

    // -----------------------------------------------------------------------
    // Dispose
    // -----------------------------------------------------------------------

    [Fact]
    public void Dispose_DoesNotThrow()
    {
        var (client, _) = BuildClient();
        var ex = Record.Exception(() => client.Dispose());
        Assert.Null(ex);
    }

    [Fact]
    public void Dispose_CalledTwice_DoesNotThrow()
    {
        var (client, _) = BuildClient();
        client.Dispose();
        var ex = Record.Exception(() => client.Dispose());
        Assert.Null(ex);
    }

    [Fact]
    public void Constructor_NullConfig_Throws()
    {
        Assert.Throws<ArgumentNullException>(() => new ExperimentationClient(null!));
    }

    // -----------------------------------------------------------------------
    // TrackEvent model
    // -----------------------------------------------------------------------

    [Fact]
    public void TrackEvent_HasKeyAndAttributedCopies()
    {
        var evt = new TrackEvent { UserId = "u", EventName = "e", Value = 2.5 };
        Assert.False(evt.HasKey);

        var byExperiment = evt.Attributed(experimentKey: "x");
        Assert.True(byExperiment.HasKey);
        Assert.Equal("x", byExperiment.ExperimentKey);
        Assert.Null(byExperiment.FeatureFlagKey);
        Assert.Equal(2.5, byExperiment.Value);

        var byFlag = evt.Attributed(featureFlagKey: "f");
        Assert.Equal("f", byFlag.FeatureFlagKey);
        Assert.Null(byFlag.ExperimentKey);
        Assert.Null(evt.ExperimentKey);
    }
}
