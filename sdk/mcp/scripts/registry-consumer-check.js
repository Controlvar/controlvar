#!/usr/bin/env node
// Installs the packed MCP server the way a consumer does from npm, and checks
// what landed on disk:
//
//   node scripts/registry-consumer-check.js <tgz> <work> <source-lock>
//
// npm installs a package's npm-shrinkwrap.json only when the registry's
// metadata for that version says `_hasShrinkwrap: true`; an install of a
// local tarball path never reads the file. So the install goes through a stub
// registry on 127.0.0.1 that serves exactly two things, this package's
// metadata (with `_hasShrinkwrap: true`) and its tarball, and answers 404 to
// anything else; every other package comes from npm's own registry. The npm
// cache is a new, empty directory under <work>, so the tarball has to be
// fetched from the stub.
//
// Then, each with its own message:
//   1. the stub served both the metadata and the tarball;
//   2. nothing is hoisted: the only package at the top of the consumer's
//      node_modules is this one, the sign that its shrinkwrap was honoured;
//   3. the directories on disk under the package, path -> version, are
//      exactly the packed shrinkwrap's entries (a package the shrinkwrap pins
//      that did not install on this platform fails too);
//   4. no installed package is one the source lock has only as a development
//      dependency;
//   5. `npm ls --all` exits 0;
//   6. the installed bin answers one `initialize` line with
//      serverInfo {name: "controlvar", version: <the package's version>}.
//
// <work> must be new or empty. <source-lock> is the package-lock.json the
// tests ran with. Called by __tests__/packed.test.ts and by the release
// (sdk-release.yml, "The installed MCP server answers initialize, with no dev
// dependency"). Standard library and npm only; not shipped.

"use strict";

const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const PREFIX = "registry-consumer-check";
const SHOWN = 20;

function usage() {
  fs.writeSync(2, `${PREFIX}: usage: registry-consumer-check.js <tgz> <work> <source-lock>\n`);
  process.exit(2);
}

/** Run a command; resolve with its exit code and output, never reject on a non-zero exit. */
function run(command, args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { ...options, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.on("error", reject);
    child.on("close", (code, signal) => resolve({ code: code ?? signal, stdout, stderr }));
    child.stdin.end(options.input ?? "");
  });
}

function tail(text) {
  return text.length > 4000 ? `...${text.slice(-4000)}` : text;
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

/** The name an entry of a lockfile's `packages` installs as. */
function entryName(key, entry) {
  if (entry && typeof entry.name === "string") return entry.name;
  return key.slice(key.lastIndexOf("node_modules/") + "node_modules/".length);
}

/** Each package directory directly inside `<dir>/node_modules`, as [name, path]. */
function packageDirs(dir) {
  const modules = path.join(dir, "node_modules");
  if (!fs.existsSync(modules)) return [];
  const found = [];
  for (const entry of fs.readdirSync(modules).sort()) {
    if (entry.startsWith(".")) continue;
    if (entry.startsWith("@")) {
      for (const child of fs.readdirSync(path.join(modules, entry)).sort()) {
        found.push([`${entry}/${child}`, path.join(modules, entry, child)]);
      }
    } else {
      found.push([entry, path.join(modules, entry)]);
    }
  }
  return found;
}

/** Walk the packages on disk below `dir`: key (as a lockfile spells it) -> package.json. */
function walk(dir, prefix, into, problems) {
  for (const [name, location] of packageDirs(dir)) {
    const key = `${prefix}node_modules/${name}`;
    const manifest = path.join(location, "package.json");
    if (!fs.existsSync(manifest)) {
      problems.push(`${key} is a directory with no package.json`);
      continue;
    }
    into.set(key, readJson(manifest));
    walk(location, `${key}/`, into, problems);
  }
  return into;
}

function listed(problems) {
  const shown = problems.slice(0, SHOWN);
  if (problems.length > SHOWN) shown.push(`... and ${problems.length - SHOWN} more`);
  return shown;
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length !== 3) usage();
  const [tgz, work, sourceLock] = args.map((arg) => path.resolve(arg));

  if (fs.existsSync(work) && fs.readdirSync(work).length > 0) {
    throw new Error(`${work} must be a new or empty directory`);
  }
  const packed = path.join(work, "packed");
  const consumer = path.join(work, "consumer");
  const cache = path.join(work, "npm-cache");
  for (const dir of [packed, consumer, cache]) fs.mkdirSync(dir, { recursive: true });

  // The packed manifest and shrinkwrap, read from the tarball's bytes.
  const extracted = await run("tar", ["-xzf", tgz, "-C", packed, "package/package.json", "package/npm-shrinkwrap.json"], {});
  if (extracted.code !== 0) {
    throw new Error(`could not read package.json and npm-shrinkwrap.json from ${tgz}: ${extracted.stderr.trim()}`);
  }
  const manifest = readJson(path.join(packed, "package", "package.json"));
  const shrinkwrap = (readJson(path.join(packed, "package", "npm-shrinkwrap.json")).packages) || {};
  const { name, version } = manifest;
  const binName = manifest.bin && typeof manifest.bin === "object" ? Object.keys(manifest.bin)[0] : undefined;

  // The development-only names of the source lock, across the whole closure.
  const lockPackages = readJson(sourceLock).packages || {};
  const devNames = new Set();
  const productionNames = new Set();
  for (const [key, entry] of Object.entries(lockPackages)) {
    if (key === "") continue;
    (entry.dev === true ? devNames : productionNames).add(entryName(key, entry));
  }
  const devOnly = new Set([...devNames].filter((n) => !productionNames.has(n)));

  // The stub registry: this package's metadata and its tarball, 404 otherwise.
  const bytes = fs.readFileSync(tgz);
  const tarballPath = `/${name}/-/${path.basename(tgz)}`;
  const metadataPaths = new Set([`/${name}`, `/${name.replace("/", "%2f")}`].map((p) => p.toLowerCase()));
  const served = { metadata: 0, tarball: 0 };
  const server = http.createServer((request, response) => {
    const url = (request.url || "").split("?")[0];
    if (request.method === "GET" && metadataPaths.has(url.toLowerCase())) {
      served.metadata += 1;
      const { port } = server.address();
      const document = {
        _id: name,
        name,
        "dist-tags": { latest: version },
        versions: {
          [version]: {
            ...manifest,
            _id: `${name}@${version}`,
            _hasShrinkwrap: true,
            dist: {
              tarball: `http://127.0.0.1:${port}${tarballPath}`,
              integrity: `sha512-${crypto.createHash("sha512").update(bytes).digest("base64")}`,
              shasum: crypto.createHash("sha1").update(bytes).digest("hex"),
            },
          },
        },
      };
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify(document));
      return;
    }
    if (request.method === "GET" && url === tarballPath) {
      served.tarball += 1;
      response.writeHead(200, { "content-type": "application/octet-stream" });
      response.end(bytes);
      return;
    }
    response.writeHead(404, { "content-type": "application/json" });
    response.end('{"error":"not found"}');
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const registry = `http://127.0.0.1:${server.address().port}/`;

  // npm with a new, empty cache of its own.
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (/^npm_config_cache$/i.test(key)) delete env[key];
  }
  env.npm_config_cache = cache;

  const problems = [];
  const counts = {};
  try {
    const init = await run("npm", ["init", "-y"], { cwd: consumer, env });
    if (init.code !== 0) throw new Error(`npm init -y exited ${init.code}: ${tail(init.stderr)}`);
    const install = await run(
      "npm",
      [
        "install",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        `--@controlvar:registry=${registry}`,
        `${name}@${version}`,
      ],
      { cwd: consumer, env },
    );

    // 1. Both requests reached the stub.
    if (served.metadata === 0) problems.push(`the stub served no packument: ${name} was not installed through the registry`);
    if (served.tarball === 0) problems.push(`the stub served no tarball: ${name} was not installed from the bytes under test`);
    if (install.code !== 0) {
      problems.push(`npm install exited ${install.code}: ${tail(install.stderr + install.stdout)}`);
      return problems;
    }

    // 2. Nothing hoisted.
    const top = packageDirs(consumer).map(([n]) => n);
    const hoisted = top.filter((n) => n !== name);
    counts.hoisted = hoisted.length;
    if (!top.includes(name)) problems.push(`${name} is not at the top of the consumer's node_modules`);
    if (hoisted.length > 0) {
      problems.push(
        `${hoisted.length} dependencies are hoisted: the shrinkwrap was not honoured (${hoisted.slice(0, 5).join(", ")}${hoisted.length > 5 ? ", ..." : ""})`,
      );
    }

    // 3. On disk under the package, exactly the shrinkwrap's entries.
    const packageDir = path.join(consumer, "node_modules", ...name.split("/"));
    const below = walk(packageDir, "", new Map(), problems);
    counts.onDisk = below.size;
    const pinned = Object.entries(shrinkwrap).filter(([key]) => key !== "");
    counts.pinned = pinned.length;
    const treeProblems = [];
    for (const [key, entry] of pinned) {
      const installed = below.get(key);
      if (installed === undefined) treeProblems.push(`the shrinkwrap pins ${key} ${entry.version}; it is not installed`);
      else if (installed.version !== entry.version) {
        treeProblems.push(`${key} is installed at ${installed.version}; the shrinkwrap pins ${entry.version}`);
      }
    }
    for (const key of below.keys()) {
      if (!Object.prototype.hasOwnProperty.call(shrinkwrap, key)) treeProblems.push(`${key} is installed and is not in the shrinkwrap`);
    }
    problems.push(...listed(treeProblems));

    // 4. No development-only package anywhere in the consumer's tree.
    const everything = walk(consumer, "", new Map(), []);
    const direct = new Set(Object.keys(manifest.devDependencies || {}));
    const devFound = [...new Set([...everything.values()].map((m) => m.name).filter((n) => devOnly.has(n)))].sort(
      (a, b) => Number(direct.has(b)) - Number(direct.has(a)) || a.localeCompare(b),
    );
    counts.dev = devFound.length;
    problems.push(
      ...listed(devFound.map((n) => `${n} is installed in a consumer's tree; the lock has it only as a development dependency`)),
    );

    // 5. npm ls --all.
    const ls = await run("npm", ["ls", "--all"], { cwd: consumer, env });
    if (ls.code !== 0) problems.push(`npm ls --all exited ${ls.code}: ${tail(ls.stderr + ls.stdout)}`);

    // 6. The bin answers initialize.
    if (binName === undefined) {
      problems.push("the packed package.json names no bin");
    } else {
      const bin = path.join(consumer, "node_modules", ".bin", binName);
      if (!fs.existsSync(bin)) {
        problems.push(`the installed package has no ${binName}`);
      } else {
        const initialize = JSON.stringify({
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: PREFIX, version: "0" } },
        });
        const started = await run(bin, [], {
          cwd: consumer,
          timeout: 30000,
          input: `${initialize}\n`,
          env: {
            PATH: `${path.dirname(process.execPath)}:/usr/bin:/bin`,
            CONTROLVAR_API_URL: "http://localhost:8000",
            CONTROLVAR_API_KEY: "registry-consumer-check-placeholder",
          },
        });
        let info;
        try {
          info = JSON.parse(started.stdout.split("\n")[0]).result.serverInfo;
        } catch {
          info = undefined;
        }
        if (
          !info ||
          Object.keys(info).sort().join(",") !== "name,version" ||
          info.name !== "controlvar" ||
          info.version !== version
        ) {
          problems.push(
            `initialize failed: the installed server exited ${started.code} and answered serverInfo ` +
              `${JSON.stringify(info)}: ${tail(started.stderr)}`,
          );
        } else {
          counts.serverInfo = JSON.stringify(info);
        }
      }
    }
  } finally {
    server.closeAllConnections();
    server.close();
  }

  if (problems.length === 0) {
    process.stdout.write(
      `${PREFIX}: ${name}@${version} installed through the stub registry ` +
        `(metadata served ${served.metadata}, tarball served ${served.tarball}); ` +
        `${counts.hoisted} hoisted; ${counts.onDisk} packages on disk under it, ` +
        `the shrinkwrap pins ${counts.pinned}; ${counts.dev} development-only packages; ` +
        `npm ls --all exit 0; initialize answered serverInfo ${counts.serverInfo}\n`,
    );
  }
  return problems;
}

// exitCode, not exit(): nothing is left running once main() settles, and an
// exit() could cut off output still queued for a pipe.
main().then(
  (problems) => {
    for (const problem of problems) process.stderr.write(`${PREFIX}: ${problem}\n`);
    process.exitCode = problems.length === 0 ? 0 : 1;
  },
  (error) => {
    process.stderr.write(`${PREFIX}: ${error.message}\n`);
    process.exitCode = 1;
  },
);
