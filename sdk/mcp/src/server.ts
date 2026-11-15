/**
 * `createServer` builds the MCP server with its six tools, over any fetch,
 * so the tests can hand it a fake one. `index.ts` gives it the real fetch and
 * the stdio transport.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Api, FetchLike } from "./http.js";
import { INSTRUCTIONS, registerTools } from "./tools.js";

/** The server's name, the same as the manifest's at GET /api/v1/mcp/manifest. */
export const SERVER_NAME = "controlvar";

/** This package's version, from its own package.json (one directory above dist/ and src/). */
export function packageVersion(): string {
  const manifest = JSON.parse(readFileSync(join(__dirname, "..", "package.json"), "utf8")) as {
    version?: unknown;
  };
  if (typeof manifest.version !== "string") throw new Error("package.json has no version");
  return manifest.version;
}

export interface ServerOptions {
  origin: string;
  apiKey: string;
  fetch: FetchLike;
  log: (line: string) => void;
  version?: string;
  timeoutMs?: number;
}

export function createServer(options: ServerOptions): McpServer {
  const version = options.version ?? packageVersion();
  const server = new McpServer({ name: SERVER_NAME, version }, { instructions: INSTRUCTIONS });
  const api = new Api({
    origin: options.origin,
    apiKey: options.apiKey,
    version,
    fetch: options.fetch,
    log: options.log,
    timeoutMs: options.timeoutMs,
  });
  registerTools(server, api);
  return server;
}
