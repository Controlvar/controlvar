using System.Collections.Concurrent;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;
using Controlvar;
using Controlvar.Exceptions;
using Controlvar.Models;
using Xunit;

namespace Controlvar.Tests;

/// <summary>
/// <see cref="SdkConfig.OnExposure"/> (#1499): called once each time the client stores an
/// <c>"assigned": true</c> answer from <c>POST /api/v1/tracking/assign</c> into an empty or
/// expired cache slot, after the answer is stored, outside the request's error path, and never
/// able to change or fail the answer. docs/sdk/dotnet.md's "Exposure hook" section quotes these
/// test names (backend/tests/unit/docs/test_dotnet_exposure_hook.py).
/// </summary>
public class ExposureTests
{
    // -----------------------------------------------------------------------
    // Fixtures
    // -----------------------------------------------------------------------

    /// <summary>The refusal UX-D4 words, as a caller sees it.</summary>
    private const string AsyncRefusal =
        "OnExposure must not be async: an async lambda or method runs as async void, and an " +
        "exception after its first await would end the process. Write the exposure to a queue " +
        "instead, such as a Channel.";

    /// <summary>
    /// An <c>/assign</c> answer with VariantAssignmentResponse's field names. <paramref name="assigned"/>
    /// is the raw JSON value of <c>assigned</c>, or <c>null</c> to leave the field out.
    /// </summary>
    private static string Answer(
        string experimentKey = "exp-1",
        string userId = "u-1",
        string? assigned = "true",
        string variantName = "treatment",
        bool isControl = false,
        string? variantId = "v-2",
        string? reason = null)
    {
        var fields = new List<string>
        {
            $"\"experiment_key\":\"{experimentKey}\"",
            $"\"user_id\":\"{userId}\"",
        };
        if (variantId != null)
            fields.Add($"\"variant_id\":\"{variantId}\"");
        fields.Add($"\"variant_name\":\"{variantName}\"");
        fields.Add($"\"is_control\":{(isControl ? "true" : "false")}");
        fields.Add("\"configuration\":null");
        if (assigned != null)
            fields.Add($"\"assigned\":{assigned}");
        if (reason != null)
            fields.Add($"\"reason\":\"{reason}\"");
        return "{" + string.Join(",", fields) + "}";
    }

    /// <summary>
    /// A fake API. By default it answers <c>/assign</c> with <c>"assigned": true</c> for the
    /// experiment and user the request names; <see cref="Body"/> and <see cref="Status"/> replace
    /// that, and <see cref="Error"/> is thrown after the request is recorded (a request the server
    /// received). <see cref="HoldUntil"/> holds every request until that many have arrived.
    /// </summary>
    private sealed class FakeApi : HttpMessageHandler
    {
        private readonly object _lock = new();
        private readonly List<string> _requests = new();
        private TaskCompletionSource<bool>? _barrier;
        private int _barrierCount;
        private int _arrived;

        public HttpStatusCode Status { get; set; } = HttpStatusCode.OK;
        public string? Body { get; set; }
        public Exception? Error { get; set; }

        public IReadOnlyList<string> Requests
        {
            get { lock (_lock) return _requests.ToArray(); }
        }

        public int Count
        {
            get { lock (_lock) return _requests.Count; }
        }

        public void HoldUntil(int requests)
        {
            lock (_lock)
            {
                _barrierCount = requests;
                _barrier = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            }
        }

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            string body = request.Content == null
                ? ""
                : await request.Content.ReadAsStringAsync().ConfigureAwait(false);

            TaskCompletionSource<bool>? barrier;
            lock (_lock)
            {
                _requests.Add($"{request.Method.Method} {request.RequestUri!.AbsolutePath}");
                barrier = _barrier;
                if (barrier != null && ++_arrived >= _barrierCount)
                    barrier.TrySetResult(true);
            }

            if (barrier != null)
            {
                // Bounds a hang; asserts no timing. A barrier never released answers 500, so
                // the assignment is null and the test fails loudly, never green.
                var first = await Task.WhenAny(barrier.Task, Task.Delay(TimeSpan.FromSeconds(5))).ConfigureAwait(false);
                if (first != barrier.Task)
                    return Reply(HttpStatusCode.InternalServerError, "{\"detail\":\"the barrier was never released\"}");
            }

            if (Error != null)
                throw Error;
            return Reply(Status, Body ?? Echo(body));
        }

        private static string Echo(string requestBody)
        {
            using var document = JsonDocument.Parse(requestBody);
            var root = document.RootElement;
            return Answer(root.GetProperty("experiment_key").GetString()!, root.GetProperty("user_id").GetString()!);
        }

        private static HttpResponseMessage Reply(HttpStatusCode status, string body) =>
            new(status) { Content = new StringContent(body, Encoding.UTF8, "application/json") };
    }

    private static ExperimentationClient NewClient(
        FakeApi api, Action<Exposure>? onExposure, int cacheTtl = 300, int cacheSize = 100) =>
        new(
            new SdkConfig("http://test-api", "test-key")
            {
                CacheTtlSeconds = cacheTtl,
                MaxCacheSize = cacheSize,
                OnExposure = onExposure
            },
            new HttpClient(api));

    private static void Sync(Exposure exposure)
    {
    }

    private static async void AsyncVoid(Exposure exposure)
    {
        await Task.Yield();
    }

    /// <summary>A thrown value whose <c>Message</c> itself throws.</summary>
    private sealed class UnreadableException : Exception
    {
        public override string Message => throw new InvalidOperationException("Message cannot be read");
    }

    // -----------------------------------------------------------------------
    // What it receives
    // -----------------------------------------------------------------------

    [Fact]
    public async Task IsCalledOnceForAnAssignedTrueAnswerWithTheFourValues()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        var assignment = await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.NotNull(assignment);
        Assert.Equal("treatment", assignment!.VariantName);
        var exposure = Assert.Single(calls);
        Assert.Equal(new Exposure("exp-1", "u-1", "v-2", "treatment"), exposure);
        Assert.Equal("exp-1", exposure.ExperimentKey);
        Assert.Equal("u-1", exposure.UserId);
        Assert.Equal("v-2", exposure.VariantId);
        Assert.Equal("treatment", exposure.VariantName);
        Assert.Equal(1, api.Count);
    }

    [Fact]
    public async Task PassesANullVariantIdWhenTheServerSendsNone()
    {
        var api = new FakeApi { Body = Answer(variantId: null) };
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        await client.GetAssignmentAsync("exp-1", "u-1");

        var exposure = Assert.Single(calls);
        Assert.Null(exposure.VariantId);
        Assert.Equal(new Exposure("exp-1", "u-1", null, "treatment"), exposure);
    }

    [Fact]
    public void ThePositionalOrderIsTheAssignmentOrder()
    {
        // Experiment key, user id, variant id, variant name: Assignment's order. Two swapped
        // strings would compile and be a silent bug in an integrator's own tests.
        var exposure = new Exposure("k", "u", "v", "n");

        Assert.Equal("k", exposure.ExperimentKey);
        Assert.Equal("u", exposure.UserId);
        Assert.Equal("v", exposure.VariantId);
        Assert.Equal("n", exposure.VariantName);
        var (experimentKey, userId, variantId, variantName) = exposure;
        Assert.Equal(new[] { "k", "u", "v", "n" }, new[] { experimentKey, userId, variantId, variantName });
        var constructor = Assert.Single(typeof(Exposure).GetConstructors());
        Assert.Equal(
            new[] { "ExperimentKey", "UserId", "VariantId", "VariantName" },
            constructor.GetParameters().Select(p => p.Name).ToArray());
    }

    [Fact]
    public void HasExactlyFourInitOnlyPublicProperties()
    {
        Assert.True(typeof(Exposure).IsSealed);
        var properties = typeof(Exposure).GetProperties(BindingFlags.Public | BindingFlags.Instance);
        Assert.Equal(
            new[] { "ExperimentKey", "UserId", "VariantId", "VariantName" },
            properties.Select(p => p.Name).OrderBy(n => n, StringComparer.Ordinal).ToArray());
        foreach (var property in properties)
        {
            Assert.Equal(typeof(string), property.PropertyType);
            var setter = property.SetMethod;
            Assert.NotNull(setter);
            Assert.Contains(
                setter!.ReturnParameter.GetRequiredCustomModifiers(),
                modifier => modifier.FullName == "System.Runtime.CompilerServices.IsExternalInit");
        }
    }

    [Fact]
    public async Task ReceivesANewObjectEachTime()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        await client.GetAssignmentAsync("exp-1", "u-1");
        client.ClearCache();
        await client.GetAssignmentAsync("exp-1", "u-1");

        var both = calls.ToArray();
        Assert.Equal(2, both.Length);
        Assert.NotSame(both[0], both[1]);
        Assert.Equal(both[0], both[1]);
    }

    // -----------------------------------------------------------------------
    // When it is called, and when not
    // -----------------------------------------------------------------------

    [Fact]
    public async Task IsNotCalledForACachedAnswerHoweverOftenItIsAskedFor()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        for (int i = 0; i < 5; i++)
            Assert.Equal("treatment", (await client.GetAssignmentAsync("exp-1", "u-1"))!.VariantName);

        Assert.Equal(1, api.Count);
        Assert.Single(calls);
    }

    [Fact]
    public async Task IsCalledOncePerUserAndExperiment()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        foreach (var (key, user) in new[] { ("exp-1", "u-1"), ("exp-1", "u-2"), ("exp-2", "u-1"), ("exp-1", "u-1"), ("exp-2", "u-1") })
            await client.GetAssignmentAsync(key, user);

        Assert.Equal(3, api.Count);
        Assert.Equal(
            new[] { "exp-1/u-1", "exp-1/u-2", "exp-2/u-1" },
            calls.Select(e => $"{e.ExperimentKey}/{e.UserId}").ToArray());
    }

    [Fact]
    public async Task ConcurrentFirstCallsMakeTwoRequestsAndOneCall()
    {
        var api = new FakeApi();
        api.HoldUntil(2);
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        var first = Task.Run(() => client.GetAssignmentAsync("exp-1", "u-1"));
        var second = Task.Run(() => client.GetAssignmentAsync("exp-1", "u-1"));
        var answers = await Task.WhenAll(first, second);

        // The positive control: both calls were in flight at once, so each sent a request and
        // the server recorded two exposures.
        Assert.Equal(2, api.Count);
        Assert.All(answers, answer => Assert.Equal("treatment", answer?.VariantName));
        Assert.Single(calls);
    }

    [Theory]
    [InlineData("holdout")]
    [InlineData("mutual_exclusion")]
    [InlineData("targeting")]
    [InlineData("draft")]
    [InlineData("paused")]
    [InlineData("completed")]
    [InlineData("archived")]
    public async Task IsNotCalledForAnAssignedFalseAnswerAndTheAnswerIsUnchanged(string reason)
    {
        var api = new FakeApi
        {
            Body = Answer(assigned: "false", reason: reason, variantName: "control", isControl: true)
        };
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        var assignment = await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.NotNull(assignment);
        Assert.Equal("control", assignment!.VariantName);
        Assert.True(assignment.IsControl);
        Assert.Equal(1, api.Count);
        Assert.Empty(calls);
    }

    [Theory]
    [InlineData("absent")]
    [InlineData("null")]
    [InlineData("\"true\"")]
    [InlineData("1")]
    public async Task IsNotCalledWhenAssignedIsNotTheLiteralTrueAndTheAnswerIsStillReturned(string assigned)
    {
        var api = new FakeApi { Body = Answer(assigned: assigned == "absent" ? null : assigned) };
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        var assignment = await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.NotNull(assignment);
        Assert.Equal("treatment", assignment!.VariantName);
        Assert.Empty(calls);
    }

    [Theory]
    [InlineData("404")]
    [InlineData("500")]
    [InlineData("401")]
    [InlineData("invalid JSON")]
    [InlineData("empty body")]
    [InlineData("JSON null")]
    [InlineData("network error")]
    [InlineData("timeout after the server received it")]
    public async Task IsNotCalledWhenTheCallFails(string failure)
    {
        var api = new FakeApi();
        switch (failure)
        {
            case "404": api.Status = HttpStatusCode.NotFound; api.Body = "{\"detail\":\"not found\"}"; break;
            case "500": api.Status = HttpStatusCode.InternalServerError; api.Body = "{\"detail\":\"boom\"}"; break;
            case "401": api.Status = HttpStatusCode.Unauthorized; api.Body = "{\"detail\":\"unauthorized\"}"; break;
            case "invalid JSON": api.Body = "not json"; break;
            case "empty body": api.Body = ""; break;
            case "JSON null": api.Body = "null"; break;
            case "network error": api.Error = new HttpRequestException("Connection refused"); break;
            case "timeout after the server received it": api.Error = new TaskCanceledException("timed out"); break;
            default: throw new ArgumentOutOfRangeException(nameof(failure), failure, null);
        }
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        Assert.Null(await client.GetAssignmentAsync("exp-1", "u-1"));
        Assert.Equal(1, api.Count);
        Assert.Empty(calls);
    }

    [Fact]
    public async Task IsNeverCalledForAFlag()
    {
        var api = new FakeApi { Body = "{\"key\":\"new-ui\",\"enabled\":true,\"config\":null,\"assigned\":true}" };
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        Assert.True((await client.EvaluateFlagAsync("new-ui", "u-1")).Enabled);
        Assert.True((await client.EvaluateFlagAsync("new-ui", "u-2")).Enabled);

        Assert.Equal(2, api.Count);
        Assert.Empty(calls);
    }

    [Fact]
    public async Task IsCalledAgainAfterTheCacheLifetime()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue, cacheTtl: 1);

        await client.GetAssignmentAsync("exp-1", "u-1");
        await Task.Delay(1200); // past the one-second lifetime; asserts no timing
        await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.Equal(2, api.Count);
        Assert.Equal(2, calls.Count);
    }

    [Fact]
    public async Task IsCalledAgainAfterClearCache()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        await client.GetAssignmentAsync("exp-1", "u-1");
        client.ClearCache();
        await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.Equal(2, api.Count);
        Assert.Equal(2, calls.Count);
    }

    [Fact]
    public async Task IsCalledAgainAfterEvictionFromAFullCache()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue, cacheSize: 1);

        await client.GetAssignmentAsync("exp-1", "u-1");
        await client.GetAssignmentAsync("exp-2", "u-1"); // evicts exp-1 from the one-entry cache
        await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.Equal(3, api.Count);
        Assert.Equal(new[] { "exp-1", "exp-2", "exp-1" }, calls.Select(e => e.ExperimentKey).ToArray());
    }

    [Fact]
    public async Task IsCalledAgainInANewClient()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();

        await NewClient(api, calls.Enqueue).GetAssignmentAsync("exp-1", "u-1");
        await NewClient(api, calls.Enqueue).GetAssignmentAsync("exp-1", "u-1");

        Assert.Equal(2, api.Count);
        Assert.Equal(2, calls.Count);
    }

    [Fact]
    public async Task SendsNothingBecauseOfTheHook()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();
        var client = NewClient(api, calls.Enqueue);

        await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.Single(calls);
        Assert.Equal(new[] { "POST /api/v1/tracking/assign" }, api.Requests);
    }

    // -----------------------------------------------------------------------
    // Where it runs
    // -----------------------------------------------------------------------

    [Fact]
    public async Task RunsAfterTheAnswerIsCachedSoTheSameKeyIsAnsweredFromTheCache()
    {
        var api = new FakeApi();
        ExperimentationClient? client = null;
        int depth = 0;
        bool answeredAtOnce = false;
        string? innerVariant = null;
        client = NewClient(api, exposure =>
        {
            // Re-enters once at most, so a hook called before the store cannot recurse.
            if (Interlocked.Increment(ref depth) != 1)
                return;
            var inner = client!.GetAssignmentAsync("exp-1", "u-1");
            answeredAtOnce = inner.IsCompleted;
            if (answeredAtOnce)
                innerVariant = inner.GetAwaiter().GetResult()?.VariantName;
        });

        var assignment = await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.Equal("treatment", assignment?.VariantName);
        Assert.True(answeredAtOnce, "the same key, asked for inside OnExposure, was not answered from the cache");
        Assert.Equal("treatment", innerVariant);
        Assert.Equal(1, api.Count);
    }

    [Fact]
    public async Task HoldsNoLockOfTheClientAndRunsInsideTheCall()
    {
        var api = new FakeApi();
        var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        using var release = new ManualResetEventSlim(false);
        var client = NewClient(api, exposure =>
        {
            if (exposure.ExperimentKey != "exp-1")
                return;
            entered.TrySetResult(true);
            release.Wait(TimeSpan.FromSeconds(10));
        });
        await client.GetAssignmentAsync("exp-2", "u-1"); // cached before anything waits

        var first = Task.Run(() => client.GetAssignmentAsync("exp-1", "u-1"));
        try
        {
            Assert.Same(entered.Task, await Task.WhenAny(entered.Task, Task.Delay(TimeSpan.FromSeconds(5))));
            var other = Task.Run(() => client.GetAssignmentAsync("exp-2", "u-1"));
            Assert.True(
                other == await Task.WhenAny(other, Task.Delay(TimeSpan.FromSeconds(5))),
                "a cached call for another experiment waited for OnExposure: it ran under a lock of the client");
            Assert.Equal("treatment", (await other)?.VariantName);
            Assert.False(first.IsCompleted, "GetAssignmentAsync completed while OnExposure was still running");
        }
        finally
        {
            release.Set();
        }

        Assert.Equal("treatment", (await first)?.VariantName);
        Assert.Equal(2, api.Count);
    }

    // -----------------------------------------------------------------------
    // If it throws
    // -----------------------------------------------------------------------

    [Theory]
    [InlineData("an InvalidOperationException")]
    [InlineData("the SDK's own ApiException")]
    [InlineData("an exception whose Message throws")]
    public async Task AThrowingFunctionLeavesTheAnswerUnchangedAndCached(string thrown)
    {
        Exception error = thrown switch
        {
            "an InvalidOperationException" => new InvalidOperationException("hook boom"),
            "the SDK's own ApiException" => new ApiException(500, "hook boom"),
            _ => new UnreadableException(),
        };
        var api = new FakeApi();
        int ran = 0;
        var client = NewClient(api, exposure =>
        {
            Interlocked.Increment(ref ran);
            throw error;
        });

        var first = await client.GetAssignmentAsync("exp-1", "u-1");
        var second = await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.NotNull(first);
        Assert.Equal("treatment", first!.VariantName);
        Assert.Same(first, second);
        Assert.Equal(1, api.Count);
        Assert.Equal(1, ran);
    }

    [Fact]
    public async Task EachCombinedFunctionIsCalledAndOneThatThrowsDoesNotStopTheNext()
    {
        var api = new FakeApi();
        var calls = new ConcurrentQueue<Exposure>();
        Action<Exposure> combined = exposure => throw new InvalidOperationException("first boom");
        combined += calls.Enqueue;
        var client = NewClient(api, combined);

        var assignment = await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.Equal("treatment", assignment?.VariantName);
        Assert.Equal(new Exposure("exp-1", "u-1", "v-2", "treatment"), Assert.Single(calls));
    }

    // -----------------------------------------------------------------------
    // What is refused
    // -----------------------------------------------------------------------

    [Fact]
    public void AnAsyncLambdaIsRefusedWhenOnExposureIsSet()
    {
        // No client is created: the init accessor itself refuses it.
        var error = Assert.Throws<ArgumentException>(() => new SdkConfig("http://test-api", "test-key")
        {
            OnExposure = async exposure => { await Task.Yield(); }
        });

        Assert.Equal("OnExposure", error.ParamName);
        Assert.StartsWith(AsyncRefusal, error.Message);
    }

    [Fact]
    public void AnAsyncMethodIsRefusedWhenOnExposureIsSet()
    {
        var error = Assert.Throws<ArgumentException>(() => new SdkConfig("http://test-api", "test-key")
        {
            OnExposure = AsyncVoid
        });

        Assert.Equal("OnExposure", error.ParamName);
        Assert.StartsWith(AsyncRefusal, error.Message);
    }

    [Fact]
    public void AnAsyncFunctionCombinedWithOthersIsRefused()
    {
        // Async first: Delegate.Method names only the LAST function (Sync), so a check of
        // Method alone would accept this. Every function in GetInvocationList() is checked.
        Action<Exposure> asyncFirst = async exposure => { await Task.Yield(); };
        asyncFirst += Sync;
        Action<Exposure> asyncLast = Sync;
        asyncLast += AsyncVoid;

        foreach (var combined in new[] { asyncFirst, asyncLast })
        {
            var error = Assert.Throws<ArgumentException>(() => new SdkConfig("http://test-api", "test-key")
            {
                OnExposure = combined
            });
            Assert.Equal("OnExposure", error.ParamName);
            Assert.StartsWith(AsyncRefusal, error.Message);
        }
    }

    [Fact]
    public void SynchronousFunctionsAndNullAreAccepted()
    {
        var calls = new ConcurrentQueue<Exposure>();
        Action<Exposure> both = Sync;
        both += calls.Enqueue;

        Assert.NotNull(new SdkConfig("http://test-api", "test-key") { OnExposure = exposure => { } }.OnExposure);
        Assert.NotNull(new SdkConfig("http://test-api", "test-key") { OnExposure = Sync }.OnExposure);
        Assert.Same(both, new SdkConfig("http://test-api", "test-key") { OnExposure = both }.OnExposure);
        Assert.Null(new SdkConfig("http://test-api", "test-key") { OnExposure = null }.OnExposure);
        Assert.Null(new SdkConfig("http://test-api", "test-key").OnExposure);
    }

    [Fact]
    public void ASynchronousLambdaThatCallsAnAsyncVoidMethodIsNotDetected()
    {
        // The blind spot the docs state: the lambda itself is synchronous.
        var config = new SdkConfig("http://test-api", "test-key") { OnExposure = exposure => AsyncVoid(exposure) };

        Assert.NotNull(config.OnExposure);
    }

    // -----------------------------------------------------------------------
    // The docs page's example
    // -----------------------------------------------------------------------

    [Fact]
    public async Task TheChannelExampleOnTheDocsPageReceivesTheExposure()
    {
        var exposures = Channel.CreateBounded<Exposure>(10_000);
        var api = new FakeApi();
        var config = new SdkConfig("http://test-api", "test-key")
        {
            OnExposure = e => exposures.Writer.TryWrite(e),   // never waits: drops the exposure when the channel is full
        };
        var client = new ExperimentationClient(config, new HttpClient(api));

        await client.GetAssignmentAsync("exp-1", "u-1");

        Assert.True(exposures.Reader.TryRead(out var exposure));
        Assert.Equal(new Exposure("exp-1", "u-1", "v-2", "treatment"), exposure);
        Assert.False(exposures.Reader.TryRead(out _));
    }
}
