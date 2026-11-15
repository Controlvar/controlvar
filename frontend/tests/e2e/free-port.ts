import { spawn, type ChildProcess } from "node:child_process";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";

/**
 * Ports for the servers a spec starts itself (#1888).
 *
 * A fixed port can already be in use on a CI runner. The Hosted UI journey's
 * stub listened on 127.0.0.1:55550, inside Linux's default ephemeral range
 * (32768-60999), and one Browser E2E run failed with `listen EADDRINUSE` on
 * both attempts. So each server here listens on port 0, the OS hands it a
 * free port, and the spec uses the port it got.
 *
 * A variable that names a port still wins, for a run that needs it fixed.
 * When that port is in use, the error names the variable and the port.
 */

const HOST = "127.0.0.1";
const STATIC_SERVER = join(__dirname, "static-server.mjs");

/** static-server.mjs's first line once it is listening: `... on http://127.0.0.1:<port> (...`. */
const ANNOUNCED = /static-server: serving .* on http:\/\/127\.0\.0\.1:(\d+) /;

/** How long static-server.mjs gets to say it is listening before it is killed. */
const START_MS = 30_000;

/** The port `variable` names, or 0 (the OS picks a free one) when it is unset or empty. */
export function requestedPort(variable: string): number {
  const raw = process.env[variable];
  if (raw === undefined || raw.trim() === "") return 0;
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${variable}=${raw} is not a port (1-65535); unset it to let the OS choose one`);
  }
  return port;
}

function cannotListen(variable: string, port: number, detail: string): Error {
  const where = port === 0 ? `a free port on ${HOST} (${variable} unset)` : `${HOST}:${port} (${variable}=${port})`;
  return new Error(`cannot listen on ${where}: ${detail}`);
}

/** Listen on the port `variable` names, else on one the OS hands out; resolves with the port it got. */
export function listenOnFreePort(server: Server, variable: string): Promise<number> {
  const port = requestedPort(variable);
  return new Promise((resolve, reject) => {
    const failed = (err: Error) => reject(cannotListen(variable, port, err.message));
    server.once("error", failed);
    server.listen(port, HOST, () => {
      server.off("error", failed);
      resolve((server.address() as AddressInfo).port);
    });
  });
}

/**
 * Start tests/e2e/static-server.mjs with `args` on the port `variable` names,
 * else on one the OS hands out. Resolves once it is listening, with the port
 * it announced; rejects, with its output, if it exits or stays silent first.
 * Its output is passed through to this process's.
 */
export function startStaticServer(args: string[], variable: string): Promise<{ child: ChildProcess; port: number }> {
  const port = requestedPort(variable);
  const child = spawn(process.execPath, [STATIC_SERVER, ...args, "--host", HOST, "--port", String(port)], {
    stdio: ["ignore", "pipe", "pipe"],
  });
  return new Promise((resolve, reject) => {
    let output = "";
    let settled = false;
    const fail = (detail: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
      reject(cannotListen(variable, port, `${detail}; static-server said: ${output.trim().slice(-1000) || "nothing"}`));
    };
    const timer = setTimeout(() => fail(`static-server did not say it was listening within ${START_MS} ms`), START_MS);
    child.stdout?.on("data", (chunk: Buffer) => {
      process.stdout.write(chunk);
      if (settled) return;
      output += chunk.toString("utf8");
      const announced = ANNOUNCED.exec(output);
      if (!announced) return;
      const got = Number(announced[1]);
      // Port 0 would mean it printed the port it was asked for, not the one it got.
      if (got === 0) return fail("static-server announced port 0, not the port it is listening on");
      settled = true;
      clearTimeout(timer);
      resolve({ child, port: got });
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      process.stderr.write(chunk);
      if (!settled) output += chunk.toString("utf8");
    });
    child.once("error", (err) => fail(`static-server did not start: ${err.message}`));
    child.once("exit", (code, signal) => fail(`static-server exited (${signal ?? `code ${code}`})`));
  });
}
