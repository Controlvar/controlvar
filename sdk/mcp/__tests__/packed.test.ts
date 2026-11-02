/**
 * Q25's pull-request leg: the package as npm would publish it installs and
 * runs on its own, and installs no development dependency.
 *
 * The release does `npm ci`, the tests, `npm shrinkwrap`, then `npm pack`.
 * This does the same to a copy of the files the package ships (the working
 * tree's lock is never renamed): shrinkwrap, pack, then install the tarball
 * into an empty directory with `--ignore-scripts` and run the installed bin
 * with one `initialize` line on stdin. It needs the registry, as a
 * consumer's install does.
 */

import { spawnSync, SpawnSyncReturns } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { newKey, PACKAGE_ROOT } from "./helpers";

const PACKAGE = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"));

function run(command: string, args: string[], cwd: string): SpawnSyncReturns<string> {
  const result = spawnSync(command, args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} exited ${result.status}: ${result.stderr}${result.stdout}`);
  }
  return result;
}

/** Every package name installed anywhere under a node_modules directory. */
function installedNames(nodeModules: string, found = new Set<string>()): Set<string> {
  if (!existsSync(nodeModules)) return found;
  for (const entry of readdirSync(nodeModules)) {
    if (entry.startsWith(".")) continue;
    const dirs = entry.startsWith("@")
      ? readdirSync(join(nodeModules, entry)).map((name) => join(nodeModules, entry, name))
      : [join(nodeModules, entry)];
    for (const dir of dirs) {
      if (!statSync(dir).isDirectory()) continue;
      const manifest = join(dir, "package.json");
      if (existsSync(manifest)) found.add(JSON.parse(readFileSync(manifest, "utf8")).name);
      installedNames(join(dir, "node_modules"), found);
    }
  }
  return found;
}

describe("Q25: the packed package installs alone and runs", () => {
  let work: string;
  beforeAll(() => {
    work = mkdtempSync(join(tmpdir(), "controlvar-mcp-pack-"));
  });
  afterAll(() => {
    rmSync(work, { recursive: true, force: true });
  });

  test("shrinkwrap, pack, install with --ignore-scripts, and answer initialize", () => {
    // What the release packs from: the shipped files plus the lock.
    const copy = join(work, "package");
    for (const name of ["package.json", "package-lock.json", "README.md", "LICENSE", "dist"]) {
      expect(existsSync(join(PACKAGE_ROOT, name))).toBe(true);
      cpSync(join(PACKAGE_ROOT, name), join(copy, name), { recursive: true });
    }
    run("npm", ["shrinkwrap"], copy);
    expect(existsSync(join(copy, "npm-shrinkwrap.json"))).toBe(true);

    const listing = JSON.parse(run("npm", ["pack", "--dry-run", "--json"], copy).stdout)[0];
    const files: string[] = listing.files.map((file: { path: string }) => file.path);
    expect(files).toContain("npm-shrinkwrap.json");
    expect(files).toContain("dist/index.js");
    expect(files).toContain("README.md");
    expect(files.filter((path) => path.startsWith("src/") || path.startsWith("__tests__/"))).toEqual([]);

    // The shrinkwrap's root versions are the lock's.
    const lock = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package-lock.json"), "utf8")).packages;
    const shrinkwrap = JSON.parse(readFileSync(join(copy, "npm-shrinkwrap.json"), "utf8")).packages;
    for (const name of Object.keys(PACKAGE.dependencies)) {
      expect(shrinkwrap[`node_modules/${name}`].version).toBe(lock[`node_modules/${name}`].version);
      expect(shrinkwrap[`node_modules/${name}`].version).toBe(PACKAGE.dependencies[name]);
    }

    const out = join(work, "out");
    mkdirSync(out);
    run("npm", ["pack", "--pack-destination", out], copy);
    const tarballs = readdirSync(out).filter((name) => name.endsWith(".tgz"));
    expect(tarballs).toHaveLength(1);

    const consumer = join(work, "consumer");
    mkdirSync(consumer);
    run("npm", ["init", "-y"], consumer);
    run("npm", ["install", "--ignore-scripts", "--no-audit", "--no-fund", join(out, tarballs[0])], consumer);

    const installed = installedNames(join(consumer, "node_modules"));
    expect(installed.has(PACKAGE.name)).toBe(true);
    for (const name of Object.keys(PACKAGE.devDependencies)) {
      if (installed.has(name)) throw new Error(`${name} is installed in a consumer's tree`);
    }

    const bin = join(consumer, "node_modules", ".bin", Object.keys(PACKAGE.bin)[0]);
    if (!existsSync(bin)) throw new Error(`the installed package has no ${Object.keys(PACKAGE.bin)[0]}`);
    const initialize = JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "q25", version: "0" } },
    });
    const started = spawnSync(bin, [], {
      input: `${initialize}\n`,
      encoding: "utf8",
      timeout: 30_000,
      env: {
        PATH: `${dirname(process.execPath)}:/usr/bin:/bin`,
        CONTROLVAR_API_URL: "http://localhost:8000",
        CONTROLVAR_API_KEY: newKey(),
      },
    });
    if (started.error || started.status !== 0) {
      throw new Error(`initialize failed: ${started.error?.message ?? ""} ${started.stderr}`);
    }
    const answer = JSON.parse(started.stdout.split("\n")[0]);
    expect(answer.result.serverInfo).toEqual({ name: "controlvar", version: PACKAGE.version });
  }, 300_000);
});
