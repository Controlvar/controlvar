using System.Net;
using System.Text;
using System.Text.Json;
using Controlvar;
using Controlvar.Exceptions;
using Xunit;

namespace Controlvar.Tests;

/// <summary>
/// The typed getters against <c>tests/sdk-contract/value-vectors.json</c> (#1810): for each answer
/// of <c>GET /api/v1/feature-flags/evaluate/{key}</c> in the file, what
/// <see cref="ExperimentationClient.GetStringValueAsync"/>,
/// <see cref="ExperimentationClient.GetNumberValueAsync"/> or
/// <see cref="ExperimentationClient.GetJsonValueAsync"/> returns and what it passes to
/// <see cref="SdkConfig.OnError"/>. The served half is generated from the server's own answer
/// builder (drift-checked in the backend suite); the malformed half is answers no server sends.
/// </summary>
/// <remarks>
/// <para>Each answer's body is served by a stub <see cref="HttpMessageHandler"/> as the text the
/// file holds, so the client's own HTTP call and JSON deserialisation read it, as they read a
/// server's.</para>
/// <para>One test, not one per vector: it counts the vectors it answered and checked, and
/// requires 49, so an empty or truncated file fails here instead of running nothing. The file is
/// found by walking up from the test assembly's directory; the test fails when it is not
/// there.</para>
/// </remarks>
public class ValueVectorsTests
{
    /// <summary>Pinned: 35 served + 14 malformed (backend/tests/unit/services/test_sdk_value_vectors.py).</summary>
    private const int Expected = 49;

    private const string FlagKey = "value-vector";
    private const string UserId = "value-vector-user";

    /// <summary>Serves one status and body to every request, and counts the requests.</summary>
    private sealed class StubApi : HttpMessageHandler
    {
        private readonly HttpStatusCode _status;
        private readonly string _body;
        private int _requests;

        public StubApi(int status, string body)
        {
            _status = (HttpStatusCode)status;
            _body = body;
        }

        public int Requests => Volatile.Read(ref _requests);
        public string? LastPathAndQuery { get; private set; }

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Interlocked.Increment(ref _requests);
            LastPathAndQuery = request.RequestUri!.PathAndQuery;
            return Task.FromResult(new HttpResponseMessage(_status)
            {
                Content = new StringContent(_body, Encoding.UTF8, "application/json")
            });
        }
    }

    /// <summary>
    /// <c>tests/sdk-contract/value-vectors.json</c>, from the first directory above the test
    /// assembly's that holds it (sdk/dotnet/tests/&lt;project&gt;/bin/&lt;config&gt;/&lt;framework&gt;/
    /// is six levels below the repository root). Throws, failing the test, when none does.
    /// </summary>
    private static string VectorsPath()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory != null; directory = directory.Parent)
        {
            var candidate = Path.Combine(directory.FullName, "tests", "sdk-contract", "value-vectors.json");
            if (File.Exists(candidate))
                return candidate;
        }
        throw new FileNotFoundException(
            "tests/sdk-contract/value-vectors.json is in no directory above " + AppContext.BaseDirectory);
    }

    [Fact]
    public async Task TheTypedGettersAnswerEachValueVectorAsTheContractSays()
    {
        using var document = JsonDocument.Parse(File.ReadAllText(VectorsPath()));
        var problems = new List<string>();
        int answered = 0;
        int asserted = 0;
        int requests = 0;

        var vectors = new List<JsonElement>();
        foreach (var half in new[] { "served", "malformed" })
        {
            if (document.RootElement.TryGetProperty(half, out var list) && list.ValueKind == JsonValueKind.Array)
                vectors.AddRange(list.EnumerateArray());
            else
                problems.Add($"the file has no \"{half}\" array");
        }

        foreach (var vector in vectors)
        {
            string name = vector.GetProperty("name").GetString()!;
            var response = vector.GetProperty("response");
            // The body's text exactly as the file holds it: the client reads these bytes.
            var api = new StubApi(response.GetProperty("status").GetInt32(), response.GetProperty("body").GetRawText());

            var errors = new List<ExperimentationException>();
            var operations = new List<string>();
            var config = new SdkConfig("http://test-api", "test-api-key")
            {
                OnError = (error, operation) =>
                {
                    errors.Add(error);
                    operations.Add(operation);
                }
            };

            string getter = vector.GetProperty("getter").GetString()!;
            var fallbackElement = vector.GetProperty("default");
            var expect = vector.GetProperty("expect");
            bool expectDefault = expect.ValueKind == JsonValueKind.String && expect.GetString() == "default";
            string got;
            using (var http = new HttpClient(api))
            using (var client = new ExperimentationClient(config, http))
            {
                switch (getter)
                {
                    case "string":
                    {
                        // A new string, so "the default" is checked by identity.
                        string fallback = new string(fallbackElement.GetString()!.ToCharArray());
                        string value = await client.GetStringValueAsync(FlagKey, UserId, fallback);
                        got = value == null ? "null" : "\"" + value + "\"";
                        bool same = expectDefault
                            ? ReferenceEquals(value, fallback)
                            : expect.ValueKind == JsonValueKind.String && expect.GetString() == value;
                        if (!same)
                            problems.Add($"{name}: got {got}, expected {Describe(expect)}");
                        break;
                    }
                    case "number":
                    {
                        double fallback = fallbackElement.GetDouble();
                        double value = await client.GetNumberValueAsync(FlagKey, UserId, fallback);
                        got = value.ToString("R", System.Globalization.CultureInfo.InvariantCulture);
                        bool same = expectDefault
                            ? value.Equals(fallback)
                            : expect.ValueKind == JsonValueKind.Number && value.Equals(expect.GetDouble());
                        if (!same)
                            problems.Add($"{name}: got {got}, expected {Describe(expect)}");
                        break;
                    }
                    case "json":
                    {
                        var fallback = fallbackElement.Clone();
                        var value = await client.GetJsonValueAsync(FlagKey, UserId, fallback);
                        got = value.ValueKind == JsonValueKind.Undefined ? "undefined" : value.GetRawText();
                        bool same = expectDefault
                            ? JsonEqual(value, fallback)
                            : (value.ValueKind == JsonValueKind.Object || value.ValueKind == JsonValueKind.Array)
                              && JsonEqual(value, expect);
                        if (!same)
                            problems.Add($"{name}: got {got}, expected {Describe(expect)}");
                        break;
                    }
                    default:
                        problems.Add($"{name}: unknown getter {getter}");
                        continue;
                }
            }
            answered += 1;
            requests += api.Requests;
            if (api.Requests != 1)
                problems.Add($"{name}: {api.Requests} requests, expected 1");
            else if (api.LastPathAndQuery != $"/api/v1/feature-flags/evaluate/{FlagKey}?user_id={UserId}")
                problems.Add($"{name}: asked {api.LastPathAndQuery}");

            int reports = operations.Count(operation => operation == "value");
            if (reports != operations.Count)
                problems.Add($"{name}: reported as {string.Join(", ", operations)}");
            int expectedReports = vector.GetProperty("reported").GetBoolean() ? 1 : 0;
            if (reports != expectedReports)
                problems.Add($"{name}: reported {reports}, expected {expectedReports} (got {got})");
            // A null code is not compared: this SDK passes a 404 or a 503 on as the transport's
            // ApiException, whose StatusCode is the HTTP status.
            var code = vector.GetProperty("error_code");
            if (code.ValueKind == JsonValueKind.String)
            {
                string? gotCode = errors.Count > 0 && errors[0] is FlagValueException flagValue ? flagValue.Code : null;
                if (gotCode != code.GetString())
                    problems.Add($"{name}: code {gotCode ?? "null"}, expected {code.GetString()}");
            }
            asserted += 1;
        }

        Assert.True(problems.Count == 0, string.Join("\n", problems));
        Assert.Equal(
            $"answered {Expected} of {Expected}, asserted {Expected}",
            $"answered {answered} of {Expected}, asserted {asserted}");
        Assert.Equal(Expected, requests);
    }

    private static string Describe(JsonElement expect) =>
        expect.ValueKind == JsonValueKind.String && expect.GetString() == "default" ? "the default" : expect.GetRawText();

    /// <summary>JSON equality: the same kinds, strings, numbers (as doubles), members and items.</summary>
    private static bool JsonEqual(JsonElement a, JsonElement b)
    {
        if (a.ValueKind != b.ValueKind)
            return false;
        switch (a.ValueKind)
        {
            case JsonValueKind.Object:
            {
                var left = a.EnumerateObject().ToList();
                var right = b.EnumerateObject().ToList();
                if (left.Count != right.Count)
                    return false;
                foreach (var member in left)
                {
                    if (!b.TryGetProperty(member.Name, out var other) || !JsonEqual(member.Value, other))
                        return false;
                }
                return true;
            }
            case JsonValueKind.Array:
            {
                var left = a.EnumerateArray().ToList();
                var right = b.EnumerateArray().ToList();
                return left.Count == right.Count && left.Zip(right, JsonEqual).All(same => same);
            }
            case JsonValueKind.String:
                return a.GetString() == b.GetString();
            case JsonValueKind.Number:
                return a.GetDouble().Equals(b.GetDouble());
            default:
                return true; // true, false, null, undefined: the kind is the value
        }
    }
}
