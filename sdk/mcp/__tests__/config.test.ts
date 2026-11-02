/**
 * V-S2 and V-OPT: the configuration the server refuses at start-up, and the
 * plain-HTTP rule with its opt-in. Each case spawns the built server with a
 * real, explicit environment (no proxy variable unless the case is about
 * one), sends `initialize`, and reads what happened: a refusal is one stderr
 * line naming the variable, exit code 2, nothing on stdout, and no value
 * echoed that might be a secret; an acceptance answers `initialize`.
 */

import { ARGUMENTS_REFUSED } from "../src/config";
import { call, initialize, json, newKey, refused, start, startStub } from "./helpers";

const KEY = newKey();

function env(url: string | undefined, extra: Record<string, string> = {}): Record<string, string> {
  const out: Record<string, string> = { CONTROLVAR_API_KEY: KEY, ...extra };
  if (url !== undefined) out.CONTROLVAR_API_URL = url;
  return out;
}

async function expectRefused(
  environment: Record<string, string>,
  naming: string,
  options: { args?: string[]; secrets?: string[]; notShown?: string[]; text?: string } = {},
) {
  const { proc, exit, lines } = await refused(environment, options.args ?? [], options.secrets ?? []);
  expect(exit.code).toBe(2);
  expect(proc.stdoutLines).toEqual([]); // initialize never answered
  expect(lines).toHaveLength(1);
  expect(lines[0]).toContain(naming);
  if (options.text !== undefined) expect(lines[0]).toBe(options.text);
  for (const value of options.notShown ?? []) expect(lines[0]).not.toContain(value);
  return lines[0];
}

async function expectAccepted(environment: Record<string, string>) {
  const proc = start(environment);
  const answer = await initialize(proc);
  expect(answer.result?.serverInfo?.name).toBe("controlvar");
  return proc;
}

describe("V-S2: arguments", () => {
  test.each([["check"], ["--help"], ["--version"], [newKey()]])("the argument %s is refused and not echoed", async (arg) => {
    await expectRefused(env("http://localhost:8000"), "CONTROLVAR_API_URL", {
      args: [arg],
      secrets: arg.startsWith("eptk_") ? [arg] : [],
      notShown: [arg],
      text: ARGUMENTS_REFUSED,
    });
  });
});

describe("V-S2: CONTROLVAR_API_URL", () => {
  test("missing", async () => {
    await expectRefused(env(undefined), "CONTROLVAR_API_URL is not set");
  });

  test.each([[""], ["   "]])("empty: %j", async (value) => {
    await expectRefused(env(value), "CONTROLVAR_API_URL is not set");
  });

  test.each([["localhost:8000"], ["ftp://controlvar.example.com"], ["not a url"], ["//controlvar.example.com"]])(
    "not http(s): %s",
    async (value) => {
      const line = await expectRefused(env(value), "CONTROLVAR_API_URL");
      expect(line).toContain("not an http:// or https:// address");
    },
  );

  test.each([
    ["https://user:hunter2-password@controlvar.example.com", "hunter2-password"],
    ["https://some-user@controlvar.example.com", "some-user"],
    ["https://controlvar.example.com/?token=query-canary", "query-canary"],
    ["https://controlvar.example.com/#fragment-canary", "fragment-canary"],
  ])("user info, query or fragment, never echoed: %s", async (value, canary) => {
    const line = await expectRefused(env(value), "CONTROLVAR_API_URL", { notShown: [canary, value] });
    expect(line).toContain("user name, password, query or fragment");
  });

  test("a URL with user info and no http scheme is not echoed either", async () => {
    await expectRefused(env("ftp://user:pw-canary@h"), "CONTROLVAR_API_URL", { notShown: ["pw-canary"] });
  });

  test.each([["https://h/prefix"], ["https://h/api/v1"], ["https://h//"]])("a path is refused: %s", async (value) => {
    const line = await expectRefused(env(value), "CONTROLVAR_API_URL");
    expect(line).toContain("which has a path");
  });

  test.each([["https://h"], ["https://h/"]])("an origin is accepted: %s", async (value) => {
    await expectAccepted(env(value));
  });
});

describe("V-S2: the host check and every request use the same parsed URL", () => {
  test.each([["/?"], ["/#"], ["@"]])("http://localhost:<port>%s reaches /api/v1/... at that origin", async (form) => {
    const stub = await startStub((_request, response) => json(response, 200, { items: [], total: 0, skip: 0, limit: 50 }));
    const url = form === "@" ? `http://@localhost:${stub.port}` : `http://localhost:${stub.port}${form}`;
    const proc = await expectAccepted(env(url));
    const result = await call(proc, "list_feature_flags");
    expect(result.isError).toBe(false);
    expect(stub.requests.map((r) => r.url.split("?")[0])).toEqual(["/api/v1/feature-flags/"]);
  });
});

describe("V-S2: CONTROLVAR_API_KEY", () => {
  test("missing", async () => {
    await expectRefused({ CONTROLVAR_API_URL: "http://localhost:8000" }, "CONTROLVAR_API_KEY is not set");
  });

  test("blank", async () => {
    await expectRefused({ CONTROLVAR_API_URL: "http://localhost:8000", CONTROLVAR_API_KEY: "  " }, "CONTROLVAR_API_KEY is not set");
  });

  test("a sign-in token is named as one, and not echoed", async () => {
    // Starts like a sign-in token (a JWT's encoded header) and is not one.
    const token = "eyJ-not-a-real-sign-in-token-canary";
    const line = await expectRefused(
      { CONTROLVAR_API_URL: "http://localhost:8000", CONTROLVAR_API_KEY: token },
      "CONTROLVAR_API_KEY",
      { notShown: [token] },
    );
    expect(line).toContain("looks like a sign-in token");
  });

  test.each([
    ["too long", `eptk_${"a".repeat(196)}`],
    ["a space inside", "eptk_0123456789 abcdef0123456789abcdef"],
    ["not ASCII", "eptk_0123456789abcdef0123456789abcdé"],
  ])("refused, never echoed: %s", async (_name, value) => {
    const line = await expectRefused(
      { CONTROLVAR_API_URL: "http://localhost:8000", CONTROLVAR_API_KEY: value },
      "CONTROLVAR_API_KEY",
    );
    expect(line).toContain("is not an API key");
  });

  test("a key that does not start eptk_ is accepted (the seed scripts store any value)", async () => {
    await expectAccepted({ CONTROLVAR_API_URL: "http://localhost:8000", CONTROLVAR_API_KEY: "seeded-demo-key-0001" });
  });
});

describe("V-OPT: plain HTTP without the opt-in", () => {
  test.each([
    ["http://localhost:8000"],
    ["http://127.0.0.1:8000"],
    ["http://[::1]:8000"],
    ["http://LOCALHOST:8000"],
    ["https://controlvar.example.com"],
  ])("accepted with no warning: %s", async (url) => {
    const proc = await expectAccepted(env(url));
    expect(proc.stderr()).toBe("");
  });

  test.each([
    ["http://controlvar.example.com"],
    ["http://10.0.0.5:8000"],
    ["http://127.0.0.2:8000"],
    ["http://0.0.0.0:8000"],
    ["http://foo.localhost:8000"],
    ["http://localhost.example.com"],
    ["http://localhost.:8000"],
  ])("refused with E2d: %s", async (url) => {
    const line = await expectRefused(env(url), "CONTROLVAR_API_URL");
    expect(line).toContain("a plain http:// address on another computer");
    expect(line).toContain("CONTROLVAR_ALLOW_INSECURE_HTTP=1");
  });

  test("NODE_USE_ENV_PROXY=1 makes localhost another computer: E2d, naming the variable", async () => {
    const line = await expectRefused(env("http://localhost:8000", { NODE_USE_ENV_PROXY: "1" }), "CONTROLVAR_API_URL");
    expect(line).toContain("NODE_USE_ENV_PROXY");
  });
});

describe("V-OPT: CONTROLVAR_ALLOW_INSECURE_HTTP with any value but 1", () => {
  const canary = "insecure-value-canary-0123456789";
  test.each([[""], ["0"], ["true"], ["TRUE"], ["yes"], ["on"], [" 1"], ["1 "], ["01"], [canary]])(
    "%j is refused with E2e, for an https:// address too",
    async (value) => {
      for (const url of ["http://10.0.0.5:8000", "https://controlvar.example.com"]) {
        // The canary is a secret to the scan after the test: no part of it
        // may be echoed. (The short values occur in the message's own words.)
        const line = await expectRefused(env(url, { CONTROLVAR_ALLOW_INSECURE_HTTP: value }), "CONTROLVAR_ALLOW_INSECURE_HTTP", {
          secrets: value === canary ? [canary] : [],
          notShown: value === canary ? [canary] : [],
        });
        expect(line).toContain("has a value this server does not accept");
      }
    },
  );
});

describe("V-OPT: CONTROLVAR_ALLOW_INSECURE_HTTP=1", () => {
  const optIn = { CONTROLVAR_ALLOW_INSECURE_HTTP: "1" };

  test("http://10.0.0.5:8000 answers initialize, with exactly one W1 line at every start", async () => {
    for (let start = 1; start <= 2; start += 1) {
      const proc = await expectAccepted(env("http://10.0.0.5:8000", optIn));
      const lines = proc.stderr().replace(/\n$/, "").split("\n");
      expect(lines).toHaveLength(1);
      expect(lines[0]).toMatch(/^Warning: CONTROLVAR_ALLOW_INSECURE_HTTP=1: /);
      expect(lines[0]).toContain("10.0.0.5:8000");
    }
  });

  test.each([["https://controlvar.example.com"], ["http://localhost:8000"]])("no warning for %s", async (url) => {
    const proc = await expectAccepted(env(url, optIn));
    expect(proc.stderr()).toBe("");
  });

  test("with NODE_USE_ENV_PROXY set, localhost is accepted with a W1 line naming the variable", async () => {
    const proc = await expectAccepted(env("http://localhost:8000", { ...optIn, NODE_USE_ENV_PROXY: "1" }));
    const lines = proc.stderr().replace(/\n$/, "").split("\n");
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain("NODE_USE_ENV_PROXY");
    expect(lines[0]).toContain("localhost:8000");
  });

  test("it lifts no other rule: user info", async () => {
    const line = await expectRefused(env("http://u:pw-canary-123@10.0.0.5", optIn), "CONTROLVAR_API_URL", {
      notShown: ["pw-canary-123"],
    });
    expect(line).toContain("user name, password, query or fragment");
  });

  test("it lifts no other rule: a path", async () => {
    const line = await expectRefused(env("http://10.0.0.5/prefix", optIn), "CONTROLVAR_API_URL");
    expect(line).toContain("which has a path");
  });
});
