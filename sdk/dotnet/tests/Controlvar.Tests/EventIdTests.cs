using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Controlvar;
using Controlvar.Models;
using Xunit;

namespace Controlvar.Tests;

/// <summary>
/// The <c>event_id</c> every event carries (#1543): made by the SDK when the caller gave none,
/// the caller's own when it did, one per event (shared by the entries it fans out to), and the
/// same on every send of an event the caller gave an id.
/// </summary>
public class EventIdTests
{
    private static readonly Regex UuidV4 =
        new Regex("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$");

    private const string AssignJson =
        "{\"experiment_key\":\"exp-1\",\"user_id\":\"user-1\",\"variant_id\":\"11111111-2222-3333-4444-555555555555\"," +
        "\"variant_name\":\"treatment\",\"is_control\":false,\"configuration\":{}}";

    private const string FlagJson = "{\"key\":\"new-ui\",\"enabled\":true,\"config\":{}}";

    private sealed class Recorder : HttpMessageHandler
    {
        public List<(string Path, JsonElement Body)> Requests { get; } = new List<(string, JsonElement)>();
        public HttpStatusCode Status { get; set; } = HttpStatusCode.OK;
        public string Reply { get; set; } = "{}";

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var text = request.Content == null ? "{}" : await request.Content.ReadAsStringAsync().ConfigureAwait(false);
            Requests.Add((request.RequestUri!.AbsolutePath, JsonDocument.Parse(text).RootElement.Clone()));
            return new HttpResponseMessage(Status) { Content = new StringContent(Reply, Encoding.UTF8, "application/json") };
        }
    }

    private static (ExperimentationClient client, Recorder handler) Build()
    {
        var handler = new Recorder();
        var client = new ExperimentationClient(new SdkConfig("http://test-api/", "key"), new HttpClient(handler));
        return (client, handler);
    }

    /// <summary>Caches one assignment and one flag for user-1, then forgets the requests that took.</summary>
    private static async Task<(ExperimentationClient client, Recorder handler)> BuildWithCache()
    {
        var (client, handler) = Build();
        handler.Reply = AssignJson;
        await client.GetAssignmentAsync("exp-1", "user-1");
        handler.Reply = FlagJson;
        await client.EvaluateFlagAsync("new-ui", "user-1");
        handler.Requests.Clear();
        handler.Reply = "{}";
        return (client, handler);
    }

    private static string IdOf(JsonElement body) => body.GetProperty("event_id").GetString()!;

    [Fact]
    public async Task Track_WithAKey_SendsAGeneratedLowercaseUuidV4AsEventId()
    {
        var (client, handler) = Build();

        Assert.True(await client.TrackAsync("purchase", "user-1", experimentKey: "exp-1"));

        Assert.Single(handler.Requests);
        Assert.Equal("/api/v1/tracking/track", handler.Requests[0].Path);
        Assert.Matches(UuidV4, IdOf(handler.Requests[0].Body));
    }

    [Fact]
    public async Task TwoEvents_GetTwoDifferentIds()
    {
        var (client, handler) = Build();

        await client.TrackAsync("purchase", "user-1", experimentKey: "exp-1");
        await client.TrackAsync("purchase", "user-1", experimentKey: "exp-1");

        Assert.Equal(2, handler.Requests.Count);
        Assert.NotEqual(IdOf(handler.Requests[0].Body), IdOf(handler.Requests[1].Body));
    }

    [Fact]
    public async Task TheCallersEventId_Wins_OnTheOverloadAndOnATrackEvent()
    {
        var (client, handler) = Build();

        await client.TrackAsync("purchase", "user-1", experimentKey: "exp-1", eventId: "order-77");
        await client.TrackAsync(new TrackEvent
        {
            EventName = "purchase", UserId = "user-1", FeatureFlagKey = "new-ui", EventId = "order-78"
        });

        Assert.Equal("order-77", IdOf(handler.Requests[0].Body));
        Assert.Equal("order-78", IdOf(handler.Requests[1].Body));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    public async Task NullAndAnEmptyString_AreNoIdOfTheCaller_TheSdkMakesOne(string? given)
    {
        var (client, handler) = Build();

        await client.TrackAsync("purchase", "user-1", experimentKey: "exp-1", eventId: given);

        Assert.Matches(UuidV4, IdOf(handler.Requests[0].Body));
    }

    [Fact]
    public async Task AKeyLessTrack_SendsOneIdOnEveryFanOutEntry()
    {
        var (client, handler) = await BuildWithCache();

        await client.TrackAsync("page_view", "user-1");
        await client.TrackAsync("page_view", "user-1");

        Assert.Equal(2, handler.Requests.Count);
        var first = handler.Requests[0].Body.GetProperty("events");
        var second = handler.Requests[1].Body.GetProperty("events");
        Assert.Equal(2, first.GetArrayLength());
        Assert.Matches(UuidV4, IdOf(first[0]));
        Assert.Equal(IdOf(first[0]), IdOf(first[1]));
        Assert.Equal(IdOf(second[0]), IdOf(second[1]));
        Assert.NotEqual(IdOf(first[0]), IdOf(second[0]));
    }

    [Fact]
    public async Task AKeyLessTrack_WithTheCallersId_SendsItOnEveryFanOutEntry()
    {
        var (client, handler) = await BuildWithCache();

        await client.TrackAsync("page_view", "user-1", eventId: "view-9");

        var events = handler.Requests[0].Body.GetProperty("events");
        Assert.Equal("view-9", IdOf(events[0]));
        Assert.Equal("view-9", IdOf(events[1]));
    }

    [Fact]
    public async Task TrackBatch_EachEventHasItsOwnId_SharedByTheEntriesItFansOutTo()
    {
        var (client, handler) = await BuildWithCache();

        await client.TrackBatchAsync(new[]
        {
            new TrackEvent { UserId = "user-1", EventName = "add_to_cart", ExperimentKey = "exp-1" },
            new TrackEvent { UserId = "user-1", EventName = "checkout", FeatureFlagKey = "new-ui", EventId = "co-1" },
            new TrackEvent { UserId = "user-1", EventName = "page_view" }
        });

        var events = handler.Requests[0].Body.GetProperty("events");
        Assert.Equal(4, events.GetArrayLength());
        Assert.Matches(UuidV4, IdOf(events[0]));
        Assert.Equal("co-1", IdOf(events[1]));
        Assert.Matches(UuidV4, IdOf(events[2]));
        Assert.Equal(IdOf(events[2]), IdOf(events[3]));
        Assert.NotEqual(IdOf(events[0]), IdOf(events[2]));
    }

    [Fact]
    public async Task TheCallersTrackEvent_IsNotChanged_ByASend()
    {
        var (client, _) = Build();
        var evt = new TrackEvent { UserId = "user-1", EventName = "purchase", ExperimentKey = "exp-1" };

        await client.TrackAsync(evt);

        Assert.Null(evt.EventId);
    }

    [Fact]
    public async Task ARequestThatFails_IsNotSentAgainBySdk_AndTheSameIdGoesOutWhenTheCallerSendsAgain()
    {
        var (client, handler) = Build();
        handler.Status = HttpStatusCode.InternalServerError;

        Assert.False(await client.TrackAsync("purchase", "user-1", experimentKey: "exp-1", eventId: "order-5"));
        Assert.Single(handler.Requests);

        handler.Status = HttpStatusCode.OK;
        Assert.True(await client.TrackAsync("purchase", "user-1", experimentKey: "exp-1", eventId: "order-5"));

        Assert.Equal(2, handler.Requests.Count);
        Assert.Equal("order-5", IdOf(handler.Requests[0].Body));
        Assert.Equal("order-5", IdOf(handler.Requests[1].Body));
    }
}
