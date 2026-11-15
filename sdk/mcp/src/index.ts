#!/usr/bin/env node
/**
 * The program an MCP client starts: it reads the environment, refuses a
 * configuration that can never work (one line on stderr, exit code 2, no
 * answer to `initialize`), and otherwise serves the six tools over stdio.
 *
 * stdout carries the protocol's JSON-RPC and nothing else; everything for a
 * person goes to stderr. The process ends when the client closes stdin.
 */

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { readConfig } from "./config.js";
import { createServer } from "./server.js";

function main(): void {
  const result = readConfig(process.argv.slice(2), process.env, process.execArgv);
  if (!result.ok) {
    process.stderr.write(`${result.message}\n`);
    // exitCode, not exit(): stderr to a pipe may still be writing.
    process.exitCode = 2;
    return;
  }
  const { origin, apiKey, warnings } = result.config;
  for (const warning of warnings) process.stderr.write(`${warning}\n`);

  const server = createServer({
    origin,
    apiKey,
    fetch: (input, init) => fetch(input, init),
    log: (line) => process.stderr.write(`${line}\n`),
  });
  // A client that went away leaves a broken pipe; there is no one to answer.
  process.stdout.on("error", () => process.exit(0));
  server.connect(new StdioServerTransport()).catch((error: unknown) => {
    process.stderr.write(`controlvar-mcp: ${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}

main();
