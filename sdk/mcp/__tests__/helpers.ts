/**
 * Test helpers: the BUILT server (dist/index.js) as a child process spoken to
 * in raw newline-delimited JSON-RPC, and a stub API on 127.0.0.1.
 *
 * Every process is started with an explicit environment holding only what
 * the test sets, so no proxy variable or setting of the machine running the
 * tests reaches it. After each test, every process it started is checked:
 * each stdout line is JSON-RPC (Q12), and no secret it was given (the key,
 * an argument, a refused value) appears, whole or as any 9-character
 * substring, in its stdout or stderr (Q10).
 */

import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { createServer, IncomingHttpHeaders, IncomingMessage, Server, ServerResponse } from "node:http";
import { join } from "node:path";

export const PACKAGE_ROOT = join(__dirname, "..");
export const BIN = join(PACKAGE_ROOT, "dist", "index.js");

/** A key in the API's own format: `eptk_` and 32 hexadecimal characters. */
export function newKey(): string {
  return `eptk_${randomBytes(16).toString("hex")}`;
}

export interface Exit {
  code: number | null;
  signal: NodeJS.Signals | null;
}

export interface Proc {
  child: ChildProcessWithoutNullStreams;
  /** Complete stdout lines, in order. */
  stdoutLines: string[];
  stderr(): string;
  /** Values that must never appear in this process's output. */
  secrets: string[];
  /** Every tool result text this process answered, for the secret scan. */
  results: string[];
  exited: Promise<Exit>;
  send(message: unknown): void;
  request(method: string, params?: unknown, timeoutMs?: number): Promise<any>;
}

const started: Proc[] = [];
const stubs: Stub[] = [];

export function start(env: Record<string, string>, args: string[] = [], secrets: string[] = []): Proc {
  for (const name of Object.keys(env)) {
    if (/proxy/i.test(name) && name !== "NODE_USE_ENV_PROXY") {
      throw new Error(`a test passed the proxy variable ${name}`);
    }
  }
  const child = spawn(process.execPath, [BIN, ...args], { env, stdio: ["pipe", "pipe", "pipe"] });
  const stdoutLines: string[] = [];
  let stdoutBuffer = "";
  let stderrText = "";
  const waiting = new Map<number, (message: any) => void>();
  child.stdin.on("error", () => undefined); // a refused start closes stdin first
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    stderrText += chunk;
  });
  child.stdout.on("data", (chunk: string) => {
    stdoutBuffer += chunk;
    let newline: number;
    while ((newline = stdoutBuffer.indexOf("\n")) >= 0) {
      const line = stdoutBuffer.slice(0, newline);
      stdoutBuffer = stdoutBuffer.slice(newline + 1);
      stdoutLines.push(line);
      let message: any;
      try {
        message = JSON.parse(line);
      } catch {
        continue; // reported by the Q12 check after the test
      }
      if (message && typeof message.id === "number" && waiting.has(message.id)) {
        waiting.get(message.id)!(message);
        waiting.delete(message.id);
      }
    }
  });
  const exited = new Promise<Exit>((resolve) => {
    child.on("close", (code, signal) => {
      if (stdoutBuffer) stdoutLines.push(stdoutBuffer);
      resolve({ code, signal });
    });
  });
  let nextId = 1;
  const proc: Proc = {
    child,
    stdoutLines,
    stderr: () => stderrText,
    secrets: [...secrets],
    results: [],
    exited,
    send(message) {
      if (child.stdin.writable) child.stdin.write(`${JSON.stringify(message)}\n`);
    },
    request(method, params, timeoutMs = 10_000) {
      const id = nextId++;
      const answer = new Promise<any>((resolve, reject) => {
        const timer = setTimeout(() => {
          waiting.delete(id);
          reject(new Error(`no answer to ${method} within ${timeoutMs} ms; stderr: ${stderrText}`));
        }, timeoutMs);
        waiting.set(id, (message) => {
          clearTimeout(timer);
          resolve(message);
        });
      });
      proc.send({ jsonrpc: "2.0", id, method, ...(params === undefined ? {} : { params }) });
      return answer;
    },
  };
  if (env.CONTROLVAR_API_KEY) proc.secrets.push(env.CONTROLVAR_API_KEY);
  started.push(proc);
  return proc;
}

export async function initialize(proc: Proc, protocolVersion = "2025-06-18"): Promise<any> {
  const answer = await proc.request("initialize", {
    protocolVersion,
    capabilities: {},
    clientInfo: { name: "controlvar-mcp-tests", version: "0" },
  });
  proc.send({ jsonrpc: "2.0", method: "notifications/initialized" });
  return answer;
}

export interface ToolResult {
  isError: boolean;
  text: string;
  raw: any;
}

export async function call(proc: Proc, name: string, args: Record<string, unknown> = {}): Promise<ToolResult> {
  const answer = await proc.request("tools/call", { name, arguments: args });
  if (answer.error) {
    const text = String(answer.error.message);
    proc.results.push(text);
    return { isError: true, text, raw: answer };
  }
  const content = answer.result?.content ?? [];
  const text = content.map((item: any) => item.text ?? "").join("");
  proc.results.push(text);
  return { isError: answer.result?.isError === true, text, raw: answer };
}

/**
 * Start a process, send initialize, and wait for it to exit (a refused
 * configuration). A process still running after 5 seconds accepted it.
 */
export async function refused(env: Record<string, string>, args: string[] = [], secrets: string[] = []) {
  const proc = start(env, args, secrets);
  proc.send({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "0" } },
  });
  let timer: NodeJS.Timeout | undefined;
  const exit = await Promise.race([
    proc.exited,
    new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), 5_000);
    }),
  ]);
  clearTimeout(timer);
  if (exit === null) {
    const what = args.length ? "an argument" : "the configuration";
    throw new Error(
      `${what} was accepted: the server did not exit (initialize answered: ${proc.stdoutLines.length > 0})`,
    );
  }
  return { proc, exit, lines: proc.stderr().replace(/\n$/, "").split("\n") };
}

// ---------------------------------------------------------------------------
// A stub API
// ---------------------------------------------------------------------------

export interface Recorded {
  method: string;
  url: string;
  headers: IncomingHttpHeaders;
  body: string;
}

export interface Stub {
  origin: string;
  port: number;
  requests: Recorded[];
  close(): Promise<void>;
}

export type Handler = (request: Recorded, response: ServerResponse) => void;

/** Listen on 127.0.0.1 at a port above 55000 (the range these tests keep to). */
async function listenAbove55000(server: Server): Promise<number> {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const port = 55001 + Math.floor(Math.random() * 9998);
    const ok = await new Promise<boolean>((resolve) => {
      const onError = () => {
        server.removeListener("listening", onListening);
        resolve(false);
      };
      const onListening = () => {
        server.removeListener("error", onError);
        resolve(true);
      };
      server.once("error", onError);
      server.once("listening", onListening);
      server.listen(port, "127.0.0.1");
    });
    if (ok) return port;
  }
  throw new Error("no free port above 55000");
}

export async function startStub(handler: Handler): Promise<Stub> {
  const requests: Recorded[] = [];
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk: string) => {
      body += chunk;
    });
    req.on("end", () => {
      const recorded = { method: req.method ?? "", url: req.url ?? "", headers: req.headers, body };
      requests.push(recorded);
      handler(recorded, res);
    });
  });
  const port = await listenAbove55000(server);
  const stub: Stub = {
    origin: `http://127.0.0.1:${port}`,
    port,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
  stubs.push(stub);
  return stub;
}

/** A port above 55000 that nothing listens on (bound, then released). */
export async function closedPort(): Promise<number> {
  const server = createServer();
  const port = await listenAbove55000(server);
  await new Promise<void>((resolve) => server.close(() => resolve()));
  return port;
}

export function json(response: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}) {
  response.writeHead(status, { "content-type": "application/json", ...headers });
  response.end(JSON.stringify(body));
}

// ---------------------------------------------------------------------------
// The checks every test gets (Q10, Q12)
// ---------------------------------------------------------------------------

/** Where a secret, or any 9-character substring of it, appears; never the value. */
export function secretLocation(secret: string, outputs: Record<string, string[]>): string | null {
  const pieces = secret.length >= 9 ? [] as string[] : [secret];
  for (let i = 0; i + 9 <= secret.length; i += 1) pieces.push(secret.slice(i, i + 9));
  for (const [where, lines] of Object.entries(outputs)) {
    for (let index = 0; index < lines.length; index += 1) {
      if (pieces.some((piece) => lines[index].includes(piece))) return `${where} line ${index + 1}`;
    }
  }
  return null;
}

export function checkOutputs(proc: Proc): void {
  proc.stdoutLines.forEach((line, index) => {
    let message: any;
    try {
      message = JSON.parse(line);
    } catch {
      throw new Error(`stdout line ${index + 1} is not JSON-RPC: ${JSON.stringify(line.slice(0, 60))}`);
    }
    if (!message || message.jsonrpc !== "2.0") {
      throw new Error(`stdout line ${index + 1} is not JSON-RPC: ${JSON.stringify(line.slice(0, 60))}`);
    }
  });
  for (const secret of proc.secrets) {
    const found = secretLocation(secret, {
      stdout: proc.stdoutLines,
      stderr: proc.stderr().split("\n"),
      "tool result": proc.results,
    });
    if (found) throw new Error(`a secret the server was given appears in ${found}`);
  }
}

afterEach(async () => {
  const procs = started.splice(0);
  for (const proc of procs) {
    if (proc.child.exitCode === null && proc.child.signalCode === null) proc.child.kill();
    await proc.exited;
  }
  for (const stub of stubs.splice(0)) await stub.close();
  for (const proc of procs) checkOutputs(proc);
});
