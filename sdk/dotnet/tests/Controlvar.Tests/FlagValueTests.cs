using System.Net;
using System.Text;
using System.Text.Json;
using Controlvar;
using Controlvar.Exceptions;
using Controlvar.Models;
using Xunit;

namespace Controlvar.Tests;

/// <summary>
/// The typed getters (#1810): <see cref="ExperimentationClient.GetStringValueAsync"/>,
/// <see cref="ExperimentationClient.GetNumberValueAsync"/> and
/// <see cref="ExperimentationClient.GetJsonValueAsync"/>, <see cref="FlagEvaluationResult.Value"/>
/// and <see cref="FlagEvaluationResult.ValueType"/>, and <see cref="SdkConfig.OnError"/>, beyond
/// what <see cref="ValueVectorsTests"/> answers: the answer's mapping, numbers, the cache they share
/// with <see cref="ExperimentationClient.EvaluateFlagAsync"/>, the cases that send nothing, every
/// failure they never throw, and the error hook's refusals and isolation.
/// </summary>
public class FlagValueTests
{
    // -----------------------------------------------------------------------
    // Fixtures
    // -----------------------------------------------------------------------

    /// <summary>
    /// A fake API: answers every request with <see cref="Respond"/>'s status and body (by default
    /// a 404), or throws <see cref="Fail"/>'s exception after recording the request.
    /// </summary>
    private sealed class FakeApi : HttpMessageHandler
    {
        private readonly object _lock = new();
        private readonly List<string> _requests = new();
        private readonly Queue<(HttpStatusCode, string)> _queued = new();
        private (HttpStatusCode, string) _reply = (HttpStatusCode.NotFound, "{\"detail\":\"not found\"}");
        private Exception? _error;

        public void Respond(HttpStatusCode status, string body)
        {
            lock (_lock)
            {
                _error = null;
                _reply = (status, body);
            }
        }

        /// <summary>Answers the next request with this, before falling back to <see cref="Respond"/>'s.</summary>
        public void Then(HttpStatusCode status, string body)
        {
            lock (_lock) _queued.Enqueue((status, body));
        }

        public void Fail(Exception error)
        {
            lock (_lock) _error = error;
        }

        /// <summary>"METHOD /path?query" of each request, in order.</summary>
        public IReadOnlyList<string> Requests
        {
            get { lock (_lock) return _requests.ToArray(); }
        }

        public int Count
        {
            get { lock (_lock) return _requests.Count; }
        }

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            (HttpStatusCode status, string body) reply;
            lock (_lock)
            {
                _requests.Add($"{request.Method.Method} {request.RequestUri!.PathAndQuery}");
                if (_error != null)
                    throw _error;
                reply = _queued.Count > 0 ? _queued.Dequeue() : _reply;
            }
            return Task.FromResult(new HttpResponseMessage(reply.status)
            {
                Content = new StringContent(reply.body, Encoding.UTF8, "application/json")
            });
        }
    }

    /// <summary>The errors and operations <see cref="SdkConfig.OnError"/> was given.</summary>
    private sealed class Reports
    {
        private readonly object _lock = new();

        public List<ExperimentationException> Errors { get; } = new();
        public List<string> Operations { get; } = new();

        public void Record(ExperimentationException error, string operation)
        {
            lock (_lock)
            {
                Errors.Add(error);
                Operations.Add(operation);
            }
        }
    }

    /// <summary>An evaluate answer. <paramref name="value"/> is raw JSON, or null to leave the field out.</summary>
    private static string Answer(string? value, string? valueType, bool enabled = true, string key = "f")
    {
        var fields = new List<string> { $"\"key\":\"{key}\"", $"\"enabled\":{(enabled ? "true" : "false")}" };
        if (value != null)
            fields.Add($"\"value\":{value}");
        if (valueType != null)
            fields.Add($"\"value_type\":{valueType}");
        fields.Add("\"config\":null");
        fields.Add("\"reason\":\"rollout\"");
        return "{" + string.Join(",", fields) + "}";
    }

    private static ExperimentationClient Client(FakeApi api, Reports? reports = null, Action<Exposure>? onExposure = null)
    {
        var config = new SdkConfig("http://test-api", "test-key")
        {
            OnError = reports == null ? null : reports.Record,
            OnExposure = onExposure
        };
        return new ExperimentationClient(config, new HttpClient(api));
    }

    private static JsonElement Json(string raw) => JsonDocument.Parse(raw).RootElement.Clone();

    private static readonly JsonElement JsonDefault = Json("{\"fb\":true}");

    // -----------------------------------------------------------------------
    // The answer's value and type
    // -----------------------------------------------------------------------

    [Fact]
    public async Task EvaluateFlagAsyncCarriesTheValueAndItsType()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("{\"color\":\"blue\"}", "\"json\""));
        using var client = Client(api);

        var result = await client.EvaluateFlagAsync("f", "u");

        Assert.True(result.Enabled);
        Assert.Equal("json", result.ValueType);
        Assert.True(result.Value.HasValue);
        Assert.Equal("blue", result.Value!.Value.GetProperty("color").GetString());
    }

    [Fact]
    public async Task AServerFromBeforeFlagValuesGivesNoValueAndNoType()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer(null, null));
        using var client = Client(api);

        var result = await client.EvaluateFlagAsync("f", "u");

        Assert.True(result.Enabled);
        Assert.Null(result.Value);
        Assert.Null(result.ValueType);
    }

    [Theory]
    [InlineData("7")]
    [InlineData("{\"a\":1}")]
    [InlineData("[\"string\"]")]
    [InlineData("true")]
    public async Task AValueTypeThatIsNotAStringIsNoTypeAndTheFlagStillAnswers(string valueType)
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"x\"", valueType));
        var reports = new Reports();
        using var client = Client(api, reports);

        var result = await client.EvaluateFlagAsync("f", "u");

        Assert.True(result.Enabled);
        Assert.Null(result.ValueType);
        Assert.Equal("x", result.Value!.Value.GetString());
        // With no type the value's own JSON type is the served type, as in the JavaScript SDK.
        Assert.Equal("x", await client.GetStringValueAsync("f", "u", "fb"));
        Assert.Empty(reports.Errors);
    }

    [Fact]
    public async Task AJsonNullValueIsNoValue()
    {
        // On every leg, including the SDK's netstandard2.1 build; this leg loads the
        // framework's System.Text.Json, not the NuGet copy older runtimes use.
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("null", "\"string\""));
        var reports = new Reports();
        using var client = Client(api, reports);

        var result = await client.EvaluateFlagAsync("f", "u");

        Assert.True(result.Enabled);
        Assert.False(result.Value.HasValue);
        Assert.Equal("string", result.ValueType);
        Assert.Equal("fb", await client.GetStringValueAsync("f", "u", "fb"));
        var error = Assert.IsType<FlagValueException>(Assert.Single(reports.Errors));
        Assert.Equal(FlagValueException.InvalidResponse, error.Code);
    }

    [Fact]
    public void TheDisabledResultHasNoValue()
    {
        var result = FlagEvaluationResult.Disabled("f");

        Assert.Null(result.Value);
        Assert.Null(result.ValueType);
    }

    // -----------------------------------------------------------------------
    // Off, and values that look empty
    // -----------------------------------------------------------------------

    [Fact]
    public async Task AFlagThatIsOffServesItsOffValue()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"off-copy\"", "\"string\"", enabled: false));
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.Equal("off-copy", await client.GetStringValueAsync("f", "u", "fb"));
        Assert.Empty(reports.Errors);
    }

    [Fact]
    public async Task AnEmptyStringZeroAndAnEmptyObjectOrArrayAreValues()
    {
        var api = new FakeApi();
        var reports = new Reports();
        using var client = Client(api, reports);

        api.Respond(HttpStatusCode.OK, Answer("\"\"", "\"string\"", key: "s"));
        Assert.Equal("", await client.GetStringValueAsync("s", "u", "fb"));
        api.Respond(HttpStatusCode.OK, Answer("0", "\"number\"", key: "n"));
        Assert.Equal(0.0, await client.GetNumberValueAsync("n", "u", -1));
        api.Respond(HttpStatusCode.OK, Answer("{}", "\"json\"", key: "o"));
        Assert.Equal(JsonValueKind.Object, (await client.GetJsonValueAsync("o", "u", JsonDefault)).ValueKind);
        Assert.Empty((await client.GetJsonValueAsync("o", "u", JsonDefault)).EnumerateObject());
        api.Respond(HttpStatusCode.OK, Answer("[]", "\"json\"", key: "a"));
        Assert.Equal(JsonValueKind.Array, (await client.GetJsonValueAsync("a", "u", JsonDefault)).ValueKind);
        Assert.Equal(0, (await client.GetJsonValueAsync("a", "u", JsonDefault)).GetArrayLength());

        Assert.Empty(reports.Errors);
    }

    // -----------------------------------------------------------------------
    // Numbers
    // -----------------------------------------------------------------------

    [Fact]
    public async Task AnIntegerIsReadAsADouble()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("10", "\"number\""));
        using var client = Client(api);

        double value = await client.GetNumberValueAsync("f", "u", -1);

        Assert.Equal(10.0, value);
    }

    [Fact]
    public async Task ADecimalIsKept()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("2.5", "\"number\""));
        using var client = Client(api);

        Assert.Equal(2.5, await client.GetNumberValueAsync("f", "u", -1));
    }

    [Theory]
    [InlineData("9007199254740991", 9007199254740991.0)]
    [InlineData("-9007199254740991", -9007199254740991.0)]
    [InlineData("-0.5", -0.5)]
    [InlineData("1e3", 1000.0)]
    public async Task ANumberWithinTwoToTheFiftyThirdMinusOneIsAValue(string raw, double expected)
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer(raw, "\"number\""));
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.Equal(expected, await client.GetNumberValueAsync("f", "u", -1));
        Assert.Empty(reports.Errors);
    }

    [Theory]
    [InlineData("9007199254740992")]
    [InlineData("-9007199254740992")]
    [InlineData("9007199254740993")]
    [InlineData("1e300")]
    [InlineData("1e400")]
    [InlineData("-1e400")]
    public async Task ANumberBeyondTwoToTheFiftyThirdMinusOneIsRefused(string raw)
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer(raw, "\"number\""));
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.Equal(-1.0, await client.GetNumberValueAsync("f", "u", -1));
        var error = Assert.IsType<FlagValueException>(Assert.Single(reports.Errors));
        Assert.Equal(FlagValueException.InvalidResponse, error.Code);
    }

    // -----------------------------------------------------------------------
    // What is reported, and the cases that send nothing
    // -----------------------------------------------------------------------

    [Fact]
    public async Task TheReasonNamesTheFlagTheServedTypeAndTheGetter()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.Equal(-1.0, await client.GetNumberValueAsync("f", "u", -1));

        var error = Assert.IsType<FlagValueException>(Assert.Single(reports.Errors));
        Assert.Equal(FlagValueException.TypeMismatch, error.Code);
        Assert.Equal("TYPE_MISMATCH", error.Code);
        Assert.Equal("Flag 'f' serves a string; GetNumberValueAsync returned your default", error.Message);
        Assert.Equal("value", Assert.Single(reports.Operations));
    }

    [Fact]
    public async Task EachFallbackCallIsReportedOnceAndTheAnswerIsAskedForOnce()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.Equal(-1.0, await client.GetNumberValueAsync("f", "u", -1));
        Assert.Single(reports.Errors);
        Assert.Equal(JsonDefault.GetRawText(), (await client.GetJsonValueAsync("f", "u", JsonDefault)).GetRawText());

        Assert.Equal(2, reports.Errors.Count);
        Assert.Equal(1, api.Count);
    }

    [Fact]
    public async Task AnUnknownFlagReportsTheApiExceptionWithItsStatus()
    {
        var api = new FakeApi(); // 404 by default
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.Equal("fb", await client.GetStringValueAsync("f", "u", "fb"));

        var error = Assert.IsType<ApiException>(Assert.Single(reports.Errors));
        Assert.Equal(404, error.StatusCode);
        Assert.Equal("value", Assert.Single(reports.Operations));
    }

    [Fact]
    public async Task AFailedRequestReportsTheNetworkException()
    {
        var api = new FakeApi();
        api.Fail(new HttpRequestException("connection refused"));
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.Equal("fb", await client.GetStringValueAsync("f", "u", "fb"));

        Assert.IsType<NetworkException>(Assert.Single(reports.Errors));
    }

    [Theory]
    [InlineData(null, "f")]
    [InlineData("", "f")]
    [InlineData("u", "")]
    [InlineData("u", null)]
    public async Task ANullOrEmptyUserIdOrFlagKeySendsNothingAndReportsInvalidResponse(string? userId, string? flagKey)
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.Equal("fb", await client.GetStringValueAsync(flagKey!, userId!, "fb"));
        Assert.Equal(-1.0, await client.GetNumberValueAsync(flagKey!, userId!, -1));
        Assert.Equal(JsonDefault.GetRawText(), (await client.GetJsonValueAsync(flagKey!, userId!, JsonDefault)).GetRawText());

        Assert.Equal(0, api.Count);
        Assert.Equal(3, reports.Errors.Count);
        foreach (var error in reports.Errors)
        {
            var flagValue = Assert.IsType<FlagValueException>(error);
            Assert.Equal(FlagValueException.InvalidResponse, flagValue.Code);
        }
    }

    // -----------------------------------------------------------------------
    // One request, one cache, no exposure
    // -----------------------------------------------------------------------

    [Fact]
    public async Task EvaluateFlagAsyncThenAGetterAsksTheServerOnce()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        using var client = Client(api);

        Assert.True((await client.EvaluateFlagAsync("f", "u")).Enabled);
        Assert.Equal("on-copy", await client.GetStringValueAsync("f", "u", "fb"));

        Assert.Equal(1, api.Count);
    }

    [Fact]
    public async Task AGetterThenEvaluateFlagAsyncAsksTheServerOnce()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("2.5", "\"number\""));
        using var client = Client(api);

        Assert.Equal(2.5, await client.GetNumberValueAsync("f", "u", -1));
        var result = await client.EvaluateFlagAsync("f", "u");

        Assert.True(result.Enabled);
        Assert.Equal(2.5, result.Value!.Value.GetDouble());
        Assert.Equal(1, api.Count);
        Assert.Equal(new[] { "f" }, client.GetEvaluatedFlags("u"));
    }

    [Fact]
    public async Task AFallbackIsNotCachedSoTheNextCallAsksAgain()
    {
        var api = new FakeApi();
        api.Then(HttpStatusCode.ServiceUnavailable, "{\"detail\":\"Service Unavailable\"}");
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        using var client = Client(api);

        Assert.Equal("fb", await client.GetStringValueAsync("f", "u", "fb"));
        Assert.Equal("on-copy", await client.GetStringValueAsync("f", "u", "fb"));

        Assert.Equal(2, api.Count);
    }

    [Fact]
    public async Task TheGettersSendTheAttributesAsContextAsEvaluateFlagAsyncDoes()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        using var client = Client(api);
        var attributes = new Dictionary<string, object> { ["plan"] = "pro" };

        await client.GetStringValueAsync("f", "u", "fb", attributes);
        client.ClearCache();
        await client.EvaluateFlagAsync("f", "u", attributes);

        Assert.Equal(2, api.Count);
        Assert.Equal("GET /api/v1/feature-flags/evaluate/f?user_id=u&context=%7B%22plan%22%3A%22pro%22%7D", api.Requests[0]);
        Assert.Equal(api.Requests[1], api.Requests[0]);
    }

    [Fact]
    public async Task ReadingAValueRecordsNoExposure()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        var exposures = new List<Exposure>();
        using var client = Client(api, onExposure: exposures.Add);

        await client.GetStringValueAsync("f", "u", "fb");
        await client.GetNumberValueAsync("f", "u", -1);
        await client.GetJsonValueAsync("f", "u", JsonDefault);

        Assert.Empty(exposures);
        Assert.Equal(new[] { "GET /api/v1/feature-flags/evaluate/f?user_id=u" }, api.Requests);
    }

    // -----------------------------------------------------------------------
    // Never throws
    // -----------------------------------------------------------------------

    [Theory]
    [InlineData("network")]
    [InlineData("timeout")]
    [InlineData("401")]
    [InlineData("404")]
    [InlineData("503")]
    [InlineData("not json")]
    [InlineData("a JSON null")]
    public async Task NeverThrowsAndReportsEachFallback(string failure)
    {
        var api = new FakeApi();
        switch (failure)
        {
            case "network": api.Fail(new HttpRequestException("connection refused")); break;
            case "timeout": api.Fail(new TaskCanceledException("timed out")); break;
            case "401": api.Respond(HttpStatusCode.Unauthorized, "{\"detail\":\"no\"}"); break;
            case "404": api.Respond(HttpStatusCode.NotFound, "{\"detail\":\"not found\"}"); break;
            case "503": api.Respond(HttpStatusCode.ServiceUnavailable, "{\"detail\":\"Service Unavailable\"}"); break;
            case "not json": api.Respond(HttpStatusCode.OK, "<html>"); break;
            default: api.Respond(HttpStatusCode.OK, "null"); break;
        }
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.Equal("fb", await client.GetStringValueAsync("f", "u", "fb"));
        Assert.Equal(-1.0, await client.GetNumberValueAsync("f", "u", -1));
        Assert.Equal(JsonDefault.GetRawText(), (await client.GetJsonValueAsync("f", "u", JsonDefault)).GetRawText());

        Assert.Equal(3, reports.Errors.Count);
        Assert.All(reports.Errors, error => Assert.False(error is FlagValueException, failure));
        Assert.Equal(3, api.Count);
    }

    [Fact]
    public async Task ABaseUrlThatIsNoUrlGivesTheDefaultAndIsReportedWithItsCause()
    {
        var reports = new Reports();
        var config = new SdkConfig("not a url", "test-key") { OnError = reports.Record };
        using var client = new ExperimentationClient(config, new HttpClient(new FakeApi()));

        Assert.Equal("fb", await client.GetStringValueAsync("f", "u", "fb"));

        var error = Assert.Single(reports.Errors);
        Assert.Equal(typeof(ExperimentationException), error.GetType());
        Assert.IsType<UriFormatException>(error.InnerException);
        Assert.Equal(
            "Could not evaluate flag 'f' (System.UriFormatException); GetStringValueAsync returned your default",
            error.Message);
    }

    [Fact]
    public async Task AttributesThatCannotBeSerialisedSendNothingAndAreReported()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        var reports = new Reports();
        using var client = Client(api, reports);
        var cyclic = new Dictionary<string, object>();
        cyclic["self"] = cyclic; // System.Text.Json refuses a cycle with a JsonException

        Assert.Equal("fb", await client.GetStringValueAsync("f", "u", "fb", cyclic));

        Assert.Equal(0, api.Count);
        var error = Assert.Single(reports.Errors);
        Assert.IsType<JsonException>(error.InnerException);
    }

    [Fact]
    public async Task WithNoOnErrorAFallbackIsStillTheDefault()
    {
        var api = new FakeApi();
        api.Fail(new HttpRequestException("connection refused"));
        using var client = Client(api);

        Assert.Equal("fb", await client.GetStringValueAsync("f", "u", "fb"));
    }

    // -----------------------------------------------------------------------
    // OnError
    // -----------------------------------------------------------------------

    /// <summary>The refusal, as a caller sees it.</summary>
    private const string AsyncRefusal =
        "OnError must not be async: an async lambda or method runs as async void, and an " +
        "exception after its first await would end the process. Write the error to a queue " +
        "instead, such as a Channel.";

    [Fact]
    public void AnAsyncLambdaIsRefusedWhenOnErrorIsSet()
    {
        var error = Assert.Throws<ArgumentException>(() => new SdkConfig("http://test-api", "test-key")
        {
            OnError = async (e, operation) => await Task.Yield()
        });

        Assert.Equal("OnError", error.ParamName);
        Assert.StartsWith(AsyncRefusal, error.Message);
    }

    private static async void AnAsyncVoidMethod(ExperimentationException error, string operation) =>
        await Task.Yield();

    private static void ASynchronousMethod(ExperimentationException error, string operation)
    {
    }

    [Fact]
    public void AnAsyncMethodIsRefusedWhenOnErrorIsSet()
    {
        var error = Assert.Throws<ArgumentException>(() => new SdkConfig("http://test-api", "test-key")
        {
            OnError = AnAsyncVoidMethod
        });

        Assert.Equal("OnError", error.ParamName);
    }

    [Fact]
    public void AnAsyncFunctionCombinedWithOthersIsRefusedForOnError()
    {
        Action<ExperimentationException, string> combined = ASynchronousMethod;
        combined += AnAsyncVoidMethod;
        combined += ASynchronousMethod;

        var error = Assert.Throws<ArgumentException>(() => new SdkConfig("http://test-api", "test-key")
        {
            OnError = combined
        });

        Assert.Equal("OnError", error.ParamName);
    }

    [Fact]
    public void SynchronousOnErrorFunctionsAndNullAreAccepted()
    {
        Action<ExperimentationException, string> combined = ASynchronousMethod;
        combined += (e, operation) => { };

        Assert.NotNull(new SdkConfig("http://test-api", "test-key") { OnError = combined }.OnError);
        Assert.Null(new SdkConfig("http://test-api", "test-key") { OnError = null }.OnError);
        Assert.Null(new SdkConfig("http://test-api", "test-key").OnError);
    }

    [Theory]
    [InlineData("InvalidOperationException")]
    [InlineData("NullReferenceException")]
    [InlineData("ExperimentationException")]
    public async Task AThrowingOnErrorChangesNoAnswer(string thrown)
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        int calls = 0;
        var config = new SdkConfig("http://test-api", "test-key")
        {
            OnError = (error, operation) =>
            {
                calls++;
                throw thrown switch
                {
                    "InvalidOperationException" => new InvalidOperationException("the logger is not ready"),
                    "NullReferenceException" => new NullReferenceException(),
                    _ => (Exception)new ExperimentationException("rethrown"),
                };
            }
        };
        using var client = new ExperimentationClient(config, new HttpClient(api));

        Assert.Equal(-1.0, await client.GetNumberValueAsync("f", "u", -1));
        Assert.Equal("on-copy", await client.GetStringValueAsync("f", "u", "fb"));

        Assert.Equal(1, calls);
        Assert.Equal(1, api.Count);
    }

    [Fact]
    public async Task EachCombinedOnErrorFunctionIsCalledAndOneThatThrowsDoesNotStopTheNext()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.OK, Answer("\"on-copy\"", "\"string\""));
        var calls = new List<string>();
        Action<ExperimentationException, string> combined = (error, operation) =>
        {
            calls.Add("first");
            throw new InvalidOperationException("the first one fails");
        };
        combined += (error, operation) => calls.Add("second " + operation);
        var config = new SdkConfig("http://test-api", "test-key") { OnError = combined };
        using var client = new ExperimentationClient(config, new HttpClient(api));

        Assert.Equal(-1.0, await client.GetNumberValueAsync("f", "u", -1));

        Assert.Equal(new[] { "first", "second value" }, calls);
    }

    [Fact]
    public async Task OnErrorRunsBeforeTheGettersTaskCompletes()
    {
        var api = new FakeApi();
        var reports = new Reports();
        using var client = Client(api, reports);

        var task = client.GetStringValueAsync("f", "u", "fb");
        Assert.Equal("fb", await task);

        Assert.Single(reports.Errors);
    }

    [Fact]
    public async Task OnErrorHearsTheTypedGettersOnly()
    {
        var api = new FakeApi();
        api.Respond(HttpStatusCode.ServiceUnavailable, "{\"detail\":\"Service Unavailable\"}");
        var reports = new Reports();
        using var client = Client(api, reports);

        Assert.False((await client.EvaluateFlagAsync("f", "u")).Enabled);
        Assert.Null(await client.GetAssignmentAsync("e", "u"));
        Assert.False(await client.TrackAsync("purchase", "u", experimentKey: "e"));
        Assert.Empty(reports.Errors);

        Assert.Equal("fb", await client.GetStringValueAsync("f", "u", "fb"));
        Assert.Single(reports.Errors);
    }
}
