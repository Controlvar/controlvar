/**
 * @jest-environment node
 */
import type { ChildProcess } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, get, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { listenOnFreePort, startStaticServer } from "../../../tests/e2e/free-port";

/**
 * #1888: the Hosted UI journey's two servers (its API stub and
 * static-server.mjs) listened on fixed ports, 55550 and 55549, inside Linux's
 * ephemeral range. A Browser E2E run found 55550 in use and failed with
 * `listen EADDRINUSE` on both attempts. They now take a port the OS hands out
 * (tests/e2e/free-port.ts), and a variable that names one still wins.
 *
 * The journey itself needs a build and a browser, so it is not run here: the
 * last test checks that it takes both ports from these helpers.
 */

const VARIABLE = "FREE_PORT_TEST_PORT";
const OLD_STUB_PORT = 55550;
const SPEC = join(__dirname, "..", "..", "..", "tests", "e2e", "hosted-ui-sign-in.journey.spec.ts");

const opened: Server[] = [];
const children: ChildProcess[] = [];
let exportDir = "";

/** A server of this test's own, on `port` (0: one the OS hands out). */
async function listen(port: number): Promise<Server> {
  const server = createServer((_req, res) => res.end("stub"));
  opened.push(server);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => resolve());
  });
  return server;
}

/** Hold `port`, unless something else already does: either way it is in use, which is the case under test. */
async function occupy(port: number): Promise<void> {
  await listen(port).catch((err: NodeJS.ErrnoException) => {
    if (err.code !== "EADDRINUSE") throw err;
  });
}

function portOf(server: Server): number {
  return (server.address() as AddressInfo).port;
}

function getText(url: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    get(url, (res) => {
      let body = "";
      res.on("data", (chunk: Buffer) => (body += chunk.toString("utf8")));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body }));
    }).on("error", reject);
  });
}

beforeAll(() => {
  // A stand-in for frontend/out: static-server.mjs needs an index.html.
  exportDir = mkdtempSync(join(tmpdir(), "free-port-"));
  writeFileSync(join(exportDir, "index.html"), "<p>export under test</p>");
  writeFileSync(join(exportDir, "routes-manifest.json"), JSON.stringify({ dynamicRoutes: [] }));
});

afterEach(async () => {
  jest.restoreAllMocks();
  delete process.env[VARIABLE];
  for (const child of children.splice(0)) {
    if (child.exitCode === null && child.signalCode === null) {
      const exited = new Promise((resolve) => child.once("exit", resolve));
      child.kill("SIGKILL");
      await exited;
    }
  }
  for (const server of opened.splice(0)) {
    if (server.listening) await new Promise((resolve) => server.close(resolve));
  }
});

afterAll(() => {
  if (exportDir) rmSync(exportDir, { recursive: true, force: true });
});

/** static-server.mjs's arguments over the stand-in export; its output (passed through) is kept out of the test log. */
function staticArgs(): string[] {
  jest.spyOn(process.stdout, "write").mockImplementation(() => true);
  jest.spyOn(process.stderr, "write").mockImplementation(() => true);
  return ["--dir", exportDir, "--manifest", join(exportDir, "routes-manifest.json"), "--api", "http://127.0.0.1:1"];
}

describe("free-port: the servers a spec starts take a port the OS hands out (#1888)", () => {
  it("the stub starts while 55550, the port it used to take, is in use", async () => {
    await occupy(OLD_STUB_PORT);
    const stub = createServer((_req, res) => res.end("stub"));
    opened.push(stub);
    const port = await listenOnFreePort(stub, VARIABLE);
    expect(port).toBeGreaterThan(0);
    expect(port).not.toBe(OLD_STUB_PORT);
    expect(await getText(`http://127.0.0.1:${port}/`)).toEqual({ status: 200, body: "stub" });
  });

  it("two stubs at once get two ports", async () => {
    const first = createServer();
    const second = createServer();
    opened.push(first, second);
    const ports = [await listenOnFreePort(first, VARIABLE), await listenOnFreePort(second, VARIABLE)];
    expect(ports[0]).not.toBe(ports[1]);
  });

  it("a port the variable names wins, and one in use is refused naming the variable and the port", async () => {
    const taken = await listen(0);
    process.env[VARIABLE] = String(portOf(taken));
    const refused = createServer();
    opened.push(refused);
    await expect(listenOnFreePort(refused, VARIABLE)).rejects.toThrow(
      `cannot listen on 127.0.0.1:${portOf(taken)} (${VARIABLE}=${portOf(taken)}): listen EADDRINUSE`,
    );

    const wanted = portOf(taken);
    await new Promise((resolve) => taken.close(resolve));
    const fixed = createServer();
    opened.push(fixed);
    expect(await listenOnFreePort(fixed, VARIABLE)).toBe(wanted);
  });

  it("static-server.mjs announces the port the OS gave it, and serves there", async () => {
    const served = await startStaticServer(staticArgs(), VARIABLE);
    children.push(served.child);
    expect(served.port).toBeGreaterThan(0);
    expect(await getText(`http://127.0.0.1:${served.port}/`)).toEqual({
      status: 200,
      body: "<p>export under test</p>",
    });
  });

  it("static-server.mjs on a port in use fails naming the variable and the port", async () => {
    const taken = await listen(0);
    process.env[VARIABLE] = String(portOf(taken));
    await expect(startStaticServer(staticArgs(), VARIABLE)).rejects.toThrow(
      new RegExp(`^cannot listen on 127\\.0\\.0\\.1:${portOf(taken)} \\(${VARIABLE}=${portOf(taken)}\\): static-server exited \\(code 1\\).*EADDRINUSE`, "s"),
    );
  });

  it("the Hosted UI journey takes both ports from these helpers, and binds none itself", () => {
    const spec = readFileSync(SPEC, "utf8");
    expect(spec).toMatch(/listenOnFreePort\(stub, "HOSTED_UI_E2E_API_PORT"\)/);
    expect(spec).toMatch(/startStaticServer\([^;]*"HOSTED_UI_E2E_PORT"\)/);
    // No listen call and no fallback port of its own.
    expect(spec).not.toMatch(/\.listen\(/);
    expect(spec).not.toMatch(/HOSTED_UI_E2E_\w*PORT\s*\?\?/);
  });
});
