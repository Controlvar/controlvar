/**
 * The server's surface, as a client sees it.
 *
 * Q1: tool names, titles, descriptions, input schemas, annotations and the
 *     `instructions` are exactly the committed tools.json (which the backend's
 *     tests and the docs read), and the environment variables it lists are
 *     the ones the server reads.
 * Q13: every protocol version a client may send is answered, and
 *     `serverInfo.version` is package.json's.
 * Q12 (static half): no source file writes to stdout itself.
 * Q20: the process ends when the client closes stdin.
 */

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { ENV_NAMES } from "../src/config";
import { call, initialize, json, newKey, PACKAGE_ROOT, start, startStub } from "./helpers";

const TOOLS_JSON = JSON.parse(readFileSync(join(PACKAGE_ROOT, "tools.json"), "utf8"));
const PACKAGE = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"));

/** The first path at which two JSON values differ, or null. */
function firstDifference(served: unknown, pinned: unknown, path: string): string | null {
  if (typeof served !== typeof pinned || Array.isArray(served) !== Array.isArray(pinned)) return path;
  if (Array.isArray(served) && Array.isArray(pinned)) {
    if (served.length !== pinned.length) return `${path}.length`;
    for (let i = 0; i < served.length; i += 1) {
      const found = firstDifference(served[i], pinned[i], `${path}[${i}]`);
      if (found) return found;
    }
    return null;
  }
  if (served && pinned && typeof served === "object") {
    const a = served as Record<string, unknown>;
    const b = pinned as Record<string, unknown>;
    for (const key of new Set([...Object.keys(a), ...Object.keys(b)])) {
      if (!(key in a) || !(key in b)) return `${path}.${key}`;
      const found = firstDifference(a[key], b[key], `${path}.${key}`);
      if (found) return found;
    }
    return null;
  }
  return served === pinned ? null : path;
}

function server() {
  return start({ CONTROLVAR_API_URL: "https://controlvar.example.com", CONTROLVAR_API_KEY: newKey() });
}

describe("Q1: tools/list and instructions are exactly tools.json", () => {
  test("tools/list", async () => {
    const proc = server();
    await initialize(proc);
    const served = (await proc.request("tools/list", {})).result.tools;
    const pinnedNames = TOOLS_JSON.tools.map((tool: any) => tool.name);
    expect(served.map((tool: any) => tool.name)).toEqual(pinnedNames);
    for (let i = 0; i < served.length; i += 1) {
      const where = firstDifference(served[i], TOOLS_JSON.tools[i], served[i].name);
      if (where) throw new Error(`tools/list differs from tools.json at ${where}`);
    }
  });

  test("instructions and the server's name", async () => {
    const proc = server();
    const answer = await initialize(proc);
    expect(answer.result.instructions).toBe(TOOLS_JSON.instructions);
    expect(answer.result.serverInfo.name).toBe(TOOLS_JSON.server.name);
    expect(TOOLS_JSON.server.name).toBe("controlvar");
  });

  test("the environment variables are the ones the server reads", () => {
    expect(TOOLS_JSON.env).toEqual([...ENV_NAMES]);
    expect(TOOLS_JSON.env).toEqual(["CONTROLVAR_API_URL", "CONTROLVAR_API_KEY", "CONTROLVAR_ALLOW_INSECURE_HTTP"]);
  });

  test("the six tools and their annotations, whatever tools.json says", () => {
    const annotations = Object.fromEntries(TOOLS_JSON.tools.map((tool: any) => [tool.name, tool.annotations]));
    const readOnly = { readOnlyHint: true, openWorldHint: false };
    expect(annotations).toEqual({
      list_feature_flags: readOnly,
      get_feature_flag: readOnly,
      evaluate_feature_flag: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false,
      },
      list_experiments: readOnly,
      get_experiment: readOnly,
      get_experiment_results: readOnly,
    });
  });
});

describe("Q5 (PKG half): no other tool can be called", () => {
  test.each([["create_experiment"], ["toggle_feature_flag"], ["delete_flag"]])("%s is unknown and sends nothing", async (name) => {
    const stub = await startStub((_request, response) => json(response, 200, {}));
    const proc = start({ CONTROLVAR_API_URL: stub.origin, CONTROLVAR_API_KEY: newKey() });
    await initialize(proc);
    const result = await call(proc, name, { flag_id: "x" });
    expect(result.isError).toBe(true);
    expect(result.text).toContain(`Tool ${name} not found`);
    expect(stub.requests).toEqual([]);
  });
});

describe("Q13: protocol versions and the reported version", () => {
  test.each([["2024-11-05"], ["2025-03-26"], ["2025-06-18"], ["2025-11-25"]])("initialize at %s", async (version) => {
    const proc = server();
    const answer = await initialize(proc, version);
    expect(answer.result.protocolVersion).toBe(version);
    expect(answer.result.serverInfo.version).toBe(PACKAGE.version);
    expect(answer.result.capabilities.tools).toBeDefined();
  });
});

describe("Q12 (static half): only the transport writes to stdout", () => {
  test("no source file calls console or writes to process.stdout", () => {
    const sources = readdirSync(join(PACKAGE_ROOT, "src")).filter((name) => name.endsWith(".ts"));
    expect(sources.length).toBeGreaterThan(0);
    for (const name of sources) {
      const source = readFileSync(join(PACKAGE_ROOT, "src", name), "utf8");
      expect({ name, found: source.match(/\bconsole\.\w+|process\.stdout\.write/g) }).toEqual({ name, found: null });
    }
  });
});

describe("Q20: the process ends when the client closes stdin", () => {
  test("gone within 10 seconds", async () => {
    // A liveness bound, not a performance one: the process must end at all.
    const proc = server();
    await initialize(proc);
    proc.child.stdin.end();
    let timer: NodeJS.Timeout | undefined;
    const exit = await Promise.race([
      proc.exited,
      new Promise<null>((resolve) => {
        timer = setTimeout(() => resolve(null), 10_000);
      }),
    ]);
    clearTimeout(timer);
    if (exit === null) throw new Error("server still running 10 s after stdin closed");
    expect(exit.code).toBe(0);
  });
});
