#!/usr/bin/env node
// Writes tools.json: what the built server answers to `initialize` (its
// `instructions`) and `tools/list`, plus the environment variables it reads.
// Run `npm run tools-json` after changing a tool, and commit the result; the
// package's tests fail until the two agree. tools.json is committed source,
// never build output, because the backend's tests and the docs read it from
// the tree.
//
// The server is started with a placeholder address and key. It makes no
// request: initialize and tools/list are answered locally.

"use strict";

const { spawn } = require("node:child_process");
const { writeFileSync } = require("node:fs");
const { join } = require("node:path");

const root = join(__dirname, "..");
const { ENV_NAMES } = require(join(root, "dist", "config.js"));

const child = spawn(process.execPath, [join(root, "dist", "index.js")], {
  env: {
    CONTROLVAR_API_URL: "https://controlvar.example.com",
    CONTROLVAR_API_KEY: "tools-json-placeholder",
  },
  stdio: ["pipe", "pipe", "inherit"],
});

const answers = new Map();
let buffer = "";
child.stdout.setEncoding("utf8");
child.stdout.on("data", (chunk) => {
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    if (!line.trim()) continue;
    const message = JSON.parse(line);
    answers.set(message.id, message);
  }
});

function send(message) {
  child.stdin.write(`${JSON.stringify(message)}\n`);
}

send({
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "write-tools-json", version: "0" },
  },
});
send({ jsonrpc: "2.0", method: "notifications/initialized" });
send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
child.stdin.end();

child.on("close", (code) => {
  const initialize = answers.get(1);
  const list = answers.get(2);
  if (code !== 0 || !initialize?.result || !list?.result) {
    process.stderr.write(`the server did not answer initialize and tools/list (exit ${code})\n`);
    process.exit(1);
  }
  const document = {
    server: { name: initialize.result.serverInfo.name },
    instructions: initialize.result.instructions,
    env: [...ENV_NAMES],
    tools: list.result.tools,
  };
  writeFileSync(join(root, "tools.json"), `${JSON.stringify(document, null, 2)}\n`);
  process.stdout.write(`wrote tools.json: ${document.tools.length} tools\n`);
});
