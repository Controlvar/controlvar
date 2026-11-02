/**
 * The six tools against a stub API, through the built server and the real
 * fetch: what each one sends (Q3's routes, Q6's credential, the query
 * names), what it answers, and how every failure reads (Q8, Q11, Q15, Q18,
 * Q19, C5). The secret and stdout checks of helpers.ts run after each test.
 */

import { ServerResponse } from "node:http";
import { Api, ApiError, MIN_PLATFORM_VERSION, TIMEOUT_SECONDS } from "../src/http";
import { call, closedPort, initialize, json, newKey, Recorded, start, startStub, Stub } from "./helpers";

const EXPERIMENT_ID = "3f1c2b7a-9d4e-4b8a-8c3f-5e6d7a8b9c0d";

async function serve(handler: (request: Recorded, response: ServerResponse) => void) {
  const stub = await startStub(handler);
  const key = newKey();
  const proc = start({ CONTROLVAR_API_URL: stub.origin, CONTROLVAR_API_KEY: key });
  await initialize(proc);
  return { stub, proc, key };
}

function flag(key: string, extra: Record<string, unknown> = {}) {
  return {
    id: `id-${key}`,
    key,
    name: `Flag ${key}`,
    description: "d",
    status: "ACTIVE",
    rollout_percentage: 100,
    value_type: "boolean",
    tags: ["t"],
    targeting_rules: null,
    on_value: true,
    off_value: false,
    default_value: false,
    created_at: "2026-10-01T00:00:00",
    updated_at: "2026-10-02T00:00:00",
    ...extra,
  };
}

function path(request: Recorded): string {
  return request.url.split("?")[0];
}

function query(request: Recorded): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(request.url.split("?")[1] ?? ""));
}

describe("what each tool sends and answers", () => {
  test("list_feature_flags: the query, the projection, and only the configured key", async () => {
    const { stub, proc, key } = await serve((_request, response) =>
      json(response, 200, { items: [flag("a"), flag("b")], total: 7, skip: 2, limit: 2 }),
    );
    const result = await call(proc, "list_feature_flags", { search: "a", status: "ACTIVE", limit: 2, skip: 2 });
    expect(result.isError).toBe(false);
    expect(JSON.parse(result.text)).toEqual({
      total: 7,
      skip: 2,
      limit: 2,
      items: ["a", "b"].map((k) => ({
        key: k,
        name: `Flag ${k}`,
        status: "ACTIVE",
        rollout_percentage: 100,
        value_type: "boolean",
        tags: ["t"],
        updated_at: "2026-10-02T00:00:00",
      })),
    });
    expect(stub.requests).toHaveLength(1);
    const [request] = stub.requests;
    expect([request.method, path(request)]).toEqual(["GET", "/api/v1/feature-flags/"]);
    expect(query(request)).toEqual({ skip: "2", limit: "2", status: "ACTIVE", search: "a" });
    expect(request.headers["x-api-key"]).toBe(key);
    expect(request.headers.authorization).toBeUndefined();
    expect(request.headers.cookie).toBeUndefined();
    expect(request.headers.accept).toBe("application/json");
    expect(request.headers["user-agent"]).toMatch(/^controlvar-mcp\/\d+\.\d+\.\d+/);
    expect(request.headers["x-request-id"]).toMatch(/^[0-9a-f-]{36}$/);
  });

  test("list_feature_flags: the defaults are limit 50 and skip 0", async () => {
    const { stub, proc } = await serve((_request, response) => json(response, 200, { items: [], total: 0, skip: 0, limit: 50 }));
    await call(proc, "list_feature_flags");
    expect(query(stub.requests[0])).toEqual({ skip: "0", limit: "50" });
  });

  test("list_feature_flags: a limit over 100 is refused before any request", async () => {
    const { stub, proc } = await serve((_request, response) => json(response, 200, {}));
    const result = await call(proc, "list_feature_flags", { limit: 101 });
    expect(result.isError).toBe(true);
    expect(stub.requests).toEqual([]);
  });

  test("list_experiments: the status goes as status_filter, exactly", async () => {
    const item = {
      id: EXPERIMENT_ID,
      key: "exp",
      name: "Exp",
      status: "active",
      experiment_type: "a_b",
      start_date: null,
      end_date: null,
      updated_at: "2026-10-02T00:00:00",
      hypothesis: "h",
      variants: [],
    };
    const { stub, proc } = await serve((_request, response) => json(response, 200, { items: [item], total: 1, skip: 0, limit: 50 }));
    const result = await call(proc, "list_experiments", { status: "active", search: "Exp" });
    expect(result.isError).toBe(false);
    const { hypothesis: _h, variants: _v, ...listed } = item;
    expect(JSON.parse(result.text)).toEqual({ total: 1, skip: 0, limit: 50, items: [listed] });
    expect(path(stub.requests[0])).toBe("/api/v1/experiments/");
    expect(query(stub.requests[0])).toEqual({ skip: "0", limit: "50", status_filter: "active", search: "Exp" });
  });

  test("evaluate_feature_flag: a POST with the user and context in the body, config dropped", async () => {
    const { stub, proc } = await serve((_request, response) =>
      json(response, 200, { key: "beta", enabled: true, value: true, value_type: "boolean", config: null, reason: "rollout" }),
    );
    const result = await call(proc, "evaluate_feature_flag", {
      flag_key: "beta",
      user_id: "user-123",
      context: { plan: "pro", country: "DE" },
    });
    expect(JSON.parse(result.text)).toEqual({ key: "beta", enabled: true, value: true, value_type: "boolean", reason: "rollout" });
    const [request] = stub.requests;
    expect([request.method, request.url]).toEqual(["POST", "/api/v1/feature-flags/evaluate/beta"]);
    expect(request.headers["content-type"]).toBe("application/json");
    expect(JSON.parse(request.body)).toEqual({ user_id: "user-123", context: { plan: "pro", country: "DE" } });
  });

  test("get_experiment and get_experiment_results: the answer as served, on the checked routes", async () => {
    const { stub, proc } = await serve((request, response) =>
      json(response, 200, { route: request.url, srm: { detected: false }, sample_size_adequate: true }),
    );
    const detail = await call(proc, "get_experiment", { experiment_id: EXPERIMENT_ID });
    const results = await call(proc, "get_experiment_results", { experiment_id: EXPERIMENT_ID });
    expect(JSON.parse(detail.text).route).toBe(`/api/v1/experiments/${EXPERIMENT_ID}`);
    expect(JSON.parse(results.text).route).toBe(`/api/v1/experiments/${EXPERIMENT_ID}/results`);
    expect(stub.requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      `GET /api/v1/experiments/${EXPERIMENT_ID}`,
      `GET /api/v1/experiments/${EXPERIMENT_ID}/results`,
    ]);
  });
});

describe("Q15: get_feature_flag is an exact match", () => {
  test("decoys before the target do not answer", async () => {
    const { stub, proc } = await serve((_request, response) =>
      json(response, 200, {
        items: [flag("mcp_flag_v2"), flag("mcpXflag"), flag("other", { name: "mcp_flag" }), flag("mcp_flag")],
        total: 4,
        skip: 0,
        limit: 100,
      }),
    );
    const result = await call(proc, "get_feature_flag", { flag_key: "mcp_flag" });
    expect(result.isError).toBe(false);
    expect(JSON.parse(result.text).key).toBe("mcp_flag");
    expect(query(stub.requests[0])).toEqual({ skip: "0", limit: "100", search: "mcp_flag" });
    expect(stub.requests).toHaveLength(1);
  });

  test("no exact match among all the matches: not found, in the API's words", async () => {
    const { proc } = await serve((_request, response) =>
      json(response, 200, { items: [flag("mcp_flag_v2")], total: 1, skip: 0, limit: 100 }),
    );
    const result = await call(proc, "get_feature_flag", { flag_key: "mcp_flag" });
    expect(result).toMatchObject({ isError: true, text: "Feature flag with key 'mcp_flag' not found" });
  });

  test("more than 100 matches and no exact one: it fails closed and never walks pages", async () => {
    const items = Array.from({ length: 100 }, (_, i) => flag(`mcp_flag_${i}`));
    const { stub, proc } = await serve((_request, response) => json(response, 200, { items, total: 150, skip: 0, limit: 100 }));
    const result = await call(proc, "get_feature_flag", { flag_key: "mcp_flag" });
    expect(result.isError).toBe(true);
    expect(result.text).toContain("More than 100 feature flags match 'mcp_flag'");
    expect(stub.requests).toHaveLength(1);
  });
});

describe("Q18: an input cannot change the route", () => {
  const badKeys = [".", "..", "a/..", "a/b", "x?y", "x#y", "%2e%2e", "a%2Fb", "-a", "_a", "a\\..", "a\u0000b", "", "a".repeat(101)];
  test.each(badKeys.map((k) => [k]))("flag_key %j is refused with no request", async (flagKey) => {
    const { stub, proc } = await serve((_request, response) => json(response, 200, {}));
    for (const tool of ["get_feature_flag", "evaluate_feature_flag"]) {
      const result = await call(proc, tool, { flag_key: flagKey, user_id: "u" });
      expect(result.isError).toBe(true);
    }
    expect(stub.requests).toEqual([]);
  });

  const badIds = ["..", "../flags", `${EXPERIMENT_ID}/x`, `${EXPERIMENT_ID}?a=b`, `{${EXPERIMENT_ID}}`, "not-a-uuid", "%2e%2e"];
  test.each(badIds.map((id) => [id]))("experiment_id %j is refused with no request", async (id) => {
    const { stub, proc } = await serve((_request, response) => json(response, 200, {}));
    for (const tool of ["get_experiment", "get_experiment_results"]) {
      const result = await call(proc, tool, { experiment_id: id });
      expect(result.isError).toBe(true);
    }
    expect(stub.requests).toEqual([]);
  });

  test("U+0000 and a lone surrogate are refused in free text, and in context", async () => {
    const { stub, proc } = await serve((_request, response) => json(response, 200, {}));
    expect((await call(proc, "list_feature_flags", { search: "a\u0000b" })).isError).toBe(true);
    expect((await call(proc, "list_experiments", { search: "a\uD800b" })).isError).toBe(true);
    expect((await call(proc, "evaluate_feature_flag", { flag_key: "f", user_id: "u\u0000" })).isError).toBe(true);
    expect((await call(proc, "evaluate_feature_flag", { flag_key: "f", user_id: "u", context: { a: ["\uDC00"] } })).isError).toBe(true);
    expect(stub.requests).toEqual([]);
  });

  test("an accepted key with every allowed character stays one path segment", async () => {
    const { stub, proc } = await serve((_request, response) =>
      json(response, 200, { key: "A.b_c-1", enabled: false, value: false, value_type: "boolean", reason: "inactive" }),
    );
    await call(proc, "evaluate_feature_flag", { flag_key: "A.b_c-1", user_id: "u" });
    expect(stub.requests[0].url).toBe("/api/v1/feature-flags/evaluate/A.b_c-1");
  });
});

describe("Q8 (PKG): a refused key is a tool error, never an empty answer, and the server stays up", () => {
  const cases: Array<[string, number, string, string]> = [
    [
      "an instance older than the read scope (401 Not authenticated)",
      401,
      "Not authenticated",
      `did not accept the key in CONTROLVAR_API_KEY (401: Not authenticated). It may have been deleted or have expired, or its owner's account may be deactivated; or the instance is older than Controlvar ${MIN_PLATFORM_VERSION}`,
    ],
    ["a deleted or expired key (401 Invalid API Key)", 401, "Invalid API Key", "(401: Invalid API Key)"],
    [
      "a key without the scope (403)",
      403,
      "This API key does not have the 'mcp:read' scope. Create a key with the 'mcp:read' scope to read flags, experiments and results with it.",
      "Listing feature flags failed: This API key does not have the 'mcp:read' scope. Create a key with the 'mcp:read' scope to read flags, experiments and results with it. (403).",
    ],
    [
      "a role refused (403)",
      403,
      "You don't have permission to list feature flags",
      "Listing feature flags failed: You don't have permission to list feature flags (403).",
    ],
  ];
  test.each(cases)("%s", async (_name, status, detail, expected) => {
    let first = true;
    const { proc } = await serve((_request, response) => {
      if (first) {
        first = false;
        json(response, status, { detail });
      } else {
        json(response, 200, { items: [], total: 0, skip: 0, limit: 50 });
      }
    });
    const result = await call(proc, "list_feature_flags");
    expect(result.isError).toBe(true);
    expect(result.text).toContain(expected);
    expect(result.text).not.toContain("items");
    const next = await call(proc, "list_feature_flags");
    expect(next.isError).toBe(false);
  });
});

describe("Q19: misconfiguration and failures are loud, with exactly one request per call", () => {
  async function one(handler: (request: Recorded, response: ServerResponse) => void, tool = "list_feature_flags", args: Record<string, unknown> = {}) {
    const { stub, proc } = await serve(handler);
    const result = await call(proc, tool, args);
    expect(stub.requests).toHaveLength(1);
    expect(result.isError).toBe(true);
    return { result, stub, proc };
  }

  test("a web page with 200 (the dashboard's address) is E9", async () => {
    const { result, stub } = await one((_request, response) => {
      response.writeHead(200, { "content-type": "text/html" });
      response.end("<!doctype html><html></html>");
    });
    expect(result.text).toBe(
      `${stub.origin} answered, but not as the Controlvar API (it sent a web page). Set CONTROLVAR_API_URL to the address that serves /api/v1: for the Quick Start, http://localhost:8000.`,
    );
  });

  test("429 with Retry-After is E18", async () => {
    const { result } = await one((_request, response) => json(response, 429, { detail: "Too many" }, { "retry-after": "17" }));
    expect(result.text).toBe("The Controlvar API is limiting requests from this address (429). Try again in 17 seconds.");
  });

  test("500 with X-Request-ID is E19, naming that id", async () => {
    const { result, stub } = await one((_request, response) =>
      json(response, 500, { detail: "boom" }, { "x-request-id": "req-from-the-api-42" }),
    );
    expect(result.text).toBe(
      `The Controlvar API at ${stub.origin} failed while listing feature flags (500). Request ID: req-from-the-api-42; your administrator can find it in the API's logs.`,
    );
  });

  test("400 Invalid host header is E11", async () => {
    const { result, stub } = await one((_request, response) => json(response, 400, { detail: "Invalid host header" }));
    expect(result.text).toContain(`The Controlvar API at ${stub.origin} refused that host name (400: Invalid host header).`);
  });

  test("404 for a flag passes the API's detail through", async () => {
    const { result } = await one(
      (_request, response) => json(response, 404, { detail: "Feature flag with key 'nope' not found" }),
      "evaluate_feature_flag",
      { flag_key: "nope", user_id: "u" },
    );
    expect(result.text).toBe("Evaluating a feature flag failed: Feature flag with key 'nope' not found (404).");
  });

  test("400 for a draft's results passes the API's detail through", async () => {
    const { result } = await one(
      (_request, response) => json(response, 400, { detail: "Cannot get results for experiments in DRAFT status" }),
      "get_experiment_results",
      { experiment_id: EXPERIMENT_ID },
    );
    expect(result.text).toBe("Reading experiment results failed: Cannot get results for experiments in DRAFT status (400).");
  });

  test("422 joins each message with its field", async () => {
    const { result } = await one(
      (_request, response) =>
        json(response, 422, {
          detail: [
            { loc: ["body", "context"], msg: "context has too many keys", type: "value_error" },
            { loc: ["body", "user_id"], msg: "too long", type: "value_error" },
          ],
        }),
      "evaluate_feature_flag",
      { flag_key: "f", user_id: "u" },
    );
    expect(result.text).toBe("Evaluating a feature flag failed: context: context has too many keys; user_id: too long (422).");
  });

  test("connection refused is E5", async () => {
    const port = await closedPort();
    const proc = start({ CONTROLVAR_API_URL: `http://127.0.0.1:${port}`, CONTROLVAR_API_KEY: newKey() });
    await initialize(proc);
    const result = await call(proc, "list_feature_flags");
    expect(result).toMatchObject({
      isError: true,
      text: `Could not reach the Controlvar API at http://127.0.0.1:${port}: connection refused. Check that the instance is running and that CONTROLVAR_API_URL is its address.`,
    });
  });

  test("an API that never answers is E8, after the timeout, with no retry", async () => {
    let held = 0;
    const stub: Stub = await startStub(() => {
      held += 1; // never answered
    });
    const lines: string[] = [];
    const api = new Api({
      origin: stub.origin,
      apiKey: newKey(),
      version: "0.0.0",
      fetch: (input, init) => fetch(input, init),
      log: (line) => lines.push(line),
      timeoutMs: 300,
    });
    const failure = await api
      .request({ tool: "t", operation: "listing feature flags", method: "GET", template: "/x", path: "/x" })
      .catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as Error).message).toMatch(
      new RegExp(`^Could not reach the Controlvar API at ${stub.origin}: did not answer within ${TIMEOUT_SECONDS} seconds \\(request [0-9a-f-]{36}\\)\\.`),
    );
    expect(held).toBe(1);
    expect(lines).toHaveLength(1);
  });

  test("the timeout is 60 seconds, and a request given no timeout of its own uses it", async () => {
    // The test above runs at 300 ms and reads the constant back, so it would
    // pass with any value; this pins the value and the default.
    expect(TIMEOUT_SECONDS).toBe(60);
    const timeout = jest.spyOn(AbortSignal, "timeout");
    try {
      const api = new Api({
        origin: "http://127.0.0.1:9",
        apiKey: newKey(),
        version: "0.0.0",
        fetch: async () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
        log: () => undefined,
      });
      await api.request({ tool: "t", operation: "listing feature flags", method: "GET", template: "/x", path: "/x" });
      expect(timeout).toHaveBeenCalledTimes(1);
      expect(timeout).toHaveBeenCalledWith(60_000);
    } finally {
      timeout.mockRestore();
    }
  });
});

describe("C5: redirects are never followed", () => {
  test.each([
    [301, "list_feature_flags", {}],
    [302, "get_experiment", { experiment_id: EXPERIMENT_ID }],
    [307, "evaluate_feature_flag", { flag_key: "f", user_id: "u" }],
    [308, "evaluate_feature_flag", { flag_key: "f", user_id: "u" }],
  ])("%i: the target receives nothing, and the answer names its origin", async (status, tool, args) => {
    const target = await startStub((_request, response) => json(response, 200, { items: [], total: 0 }));
    const { stub, proc } = await serve((_request, response) => {
      response.writeHead(status, { location: `${target.origin}/api/v1/feature-flags/` });
      response.end();
    });
    const result = await call(proc, tool, args);
    expect(target.requests).toEqual([]);
    expect(stub.requests).toHaveLength(1);
    expect(result).toMatchObject({
      isError: true,
      text: `${stub.origin} answered with a redirect to ${target.origin}. The MCP server does not follow redirects, so your key goes only to the address you set. If ${target.origin} is your instance, set CONTROLVAR_API_URL to it.`,
    });
  });
});

describe("Q11: no query string or body value in an error or a log line", () => {
  test("a search canary against a 500 is named nowhere; the path is", async () => {
    const canary = "mcp-QS-canary";
    const { proc } = await serve((_request, response) => json(response, 500, { detail: "boom" }));
    const result = await call(proc, "list_feature_flags", { search: canary });
    expect(result.isError).toBe(true);
    expect(result.text).not.toContain(canary);
    expect(proc.stderr()).toContain("GET /api/v1/feature-flags/ 500");
    expect(proc.stderr()).not.toContain(canary);
  });

  test("an evaluate user_id and context never reach stderr", async () => {
    const { proc } = await serve((_request, response) => json(response, 500, { detail: "boom" }));
    await call(proc, "evaluate_feature_flag", { flag_key: "f", user_id: "user-id-canary-77", context: { email: "ctx-canary@example.com" } });
    expect(proc.stderr()).toContain("POST /api/v1/feature-flags/evaluate/{flag_key} 500");
    expect(proc.stderr()).not.toContain("user-id-canary-77");
    expect(proc.stderr()).not.toContain("ctx-canary");
  });
});

describe("Q10 (PKG): the key is in no output, whatever happens", () => {
  test("every tool, success and failure, leaves no part of the key anywhere", async () => {
    let status = 200;
    const { proc } = await serve((request, response) => {
      if (status !== 200) return json(response, status, { detail: "no" });
      if (request.url.startsWith("/api/v1/feature-flags/evaluate/")) {
        return json(response, 200, { key: "f", enabled: true, value: true, value_type: "boolean", reason: "rollout" });
      }
      return json(response, 200, { items: [flag("f")], total: 1, skip: 0, limit: 50 });
    });
    for (status of [200, 401, 403, 500]) {
      await call(proc, "list_feature_flags");
      await call(proc, "get_feature_flag", { flag_key: "f" });
      await call(proc, "evaluate_feature_flag", { flag_key: "f", user_id: "u" });
      await call(proc, "list_experiments");
      await call(proc, "get_experiment", { experiment_id: EXPERIMENT_ID });
      await call(proc, "get_experiment_results", { experiment_id: EXPERIMENT_ID });
    }
    // The scan itself runs after the test (helpers.ts), over stdout, stderr
    // and every tool result; this asserts it had something to scan.
    expect(proc.results.length).toBe(24);
    expect(proc.stderr().split("\n").filter(Boolean).length).toBe(24);
  });
});
