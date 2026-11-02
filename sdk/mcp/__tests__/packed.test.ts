/**
 * Q25's pull-request leg: the package as npm would publish it installs and
 * runs on its own, and installs no development dependency.
 *
 * The release does `npm ci`, the tests, `npm shrinkwrap`, strips the
 * shrinkwrap's development entries, then `npm pack`. This does the same to a
 * copy of the package directory without node_modules (the working tree's
 * lock is never renamed), with the same strip script. Then the release's
 * consumer check installs the tarball through a stub registry whose metadata
 * says `_hasShrinkwrap: true`, as we expect npm's registry to mark a version
 * that ships a shrinkwrap (checked with `npm view` after the first publish),
 * so the shipped file is what decides the tree: exactly the
 * lock's production entries, nested under the package, no development
 * package, `npm ls --all` clean, and the installed bin answering one
 * `initialize` line. A local tarball install would not read the shrinkwrap
 * at all. It needs npm's registry for the dependencies, as a consumer's
 * install does.
 */

import { spawnSync, SpawnSyncReturns } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PACKAGE_ROOT } from "./helpers";

const PACKAGE = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"));
const LOCK = join(PACKAGE_ROOT, "package-lock.json");

function run(command: string, args: string[], cwd: string): SpawnSyncReturns<string> {
  const result = spawnSync(command, args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} exited ${result.status}: ${result.stderr}${result.stdout}`);
  }
  return result;
}

describe("Q25: the packed package installs alone and runs", () => {
  let work: string;
  beforeAll(() => {
    work = mkdtempSync(join(tmpdir(), "controlvar-mcp-pack-"));
  });
  afterAll(() => {
    rmSync(work, { recursive: true, force: true });
  });

  test("shrinkwrap, strip, pack, install through a registry, and answer initialize", () => {
    // What the release packs from: the package directory, without
    // node_modules, so `files` decides what ships.
    const copy = join(work, "package");
    for (const name of ["package.json", "package-lock.json", "README.md", "LICENSE", "dist", "scripts", "src"]) {
      expect(existsSync(join(PACKAGE_ROOT, name))).toBe(true);
    }
    for (const name of readdirSync(PACKAGE_ROOT)) {
      if (name === "node_modules" || name === "npm-shrinkwrap.json") continue;
      cpSync(join(PACKAGE_ROOT, name), join(copy, name), { recursive: true });
    }
    run("npm", ["shrinkwrap"], copy);
    expect(existsSync(join(copy, "npm-shrinkwrap.json"))).toBe(true);
    const stripped = run(
      process.execPath,
      [join(PACKAGE_ROOT, "scripts", "strip-shrinkwrap-dev.js"), join(copy, "npm-shrinkwrap.json"), LOCK],
      copy,
    ).stdout.trim();
    console.log(`strip-shrinkwrap-dev: ${stripped}`);
    expect(stripped).toMatch(/^removed [1-9]\d* dev entries; [1-9]\d* remain$/);

    const listing = JSON.parse(run("npm", ["pack", "--dry-run", "--json"], copy).stdout)[0];
    const files: string[] = listing.files.map((file: { path: string }) => file.path);
    expect(files).toContain("npm-shrinkwrap.json");
    expect(files).toContain("dist/index.js");
    expect(files).toContain("README.md");
    expect(
      files.filter((path) => path.startsWith("src/") || path.startsWith("__tests__/") || path.startsWith("scripts/")),
    ).toEqual([]);

    // The shrinkwrap's root versions are the lock's.
    const lock = JSON.parse(readFileSync(LOCK, "utf8")).packages;
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

    const checked = run(
      process.execPath,
      [join(PACKAGE_ROOT, "scripts", "registry-consumer-check.js"), join(out, tarballs[0]), join(work, "registry"), LOCK],
      work,
    ).stdout.trim();
    console.log(checked);
    expect(checked).toContain(`${PACKAGE.name}@${PACKAGE.version} installed through the stub registry`);
  }, 300_000);
});
