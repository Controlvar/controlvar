/**
 * scripts/strip-shrinkwrap-dev.js, which the release and packed.test.ts both
 * run on the shrinkwrap before packing. On the real lock it leaves exactly
 * the lock's production entries. On lockfiles written here it refuses a
 * format it does not read, and refuses (writing nothing) a result that is
 * not the lock's production closure.
 */

import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PACKAGE_ROOT } from "./helpers";

const STRIP = join(PACKAGE_ROOT, "scripts", "strip-shrinkwrap-dev.js");
const LOCK_TEXT = readFileSync(join(PACKAGE_ROOT, "package-lock.json"), "utf8");

type Entry = Record<string, unknown>;
type Lockfile = { lockfileVersion?: unknown; dependencies?: unknown; packages: Record<string, Entry> };

let work: string;
beforeEach(() => {
  work = mkdtempSync(join(tmpdir(), "controlvar-mcp-strip-"));
});
afterEach(() => {
  rmSync(work, { recursive: true, force: true });
});

function strip(shrinkwrap: Lockfile, lock: Lockfile) {
  const shrinkwrapPath = join(work, "npm-shrinkwrap.json");
  const lockPath = join(work, "package-lock.json");
  const before = `${JSON.stringify(shrinkwrap, null, 2)}\n`;
  writeFileSync(shrinkwrapPath, before);
  writeFileSync(lockPath, JSON.stringify(lock));
  const result = spawnSync(process.execPath, [STRIP, shrinkwrapPath, lockPath], { encoding: "utf8" });
  return { result, before, after: readFileSync(shrinkwrapPath, "utf8") };
}

function small(): Lockfile {
  return {
    lockfileVersion: 3,
    packages: {
      "": { name: "@controlvar/mcp-server", version: "0.1.0" },
      "node_modules/qs": { version: "6.16.1", resolved: "https://registry.npmjs.org/qs/-/qs-6.16.1.tgz", integrity: "sha512-q" },
      "node_modules/jest": { version: "30.5.2", resolved: "https://registry.npmjs.org/jest/-/jest-30.5.2.tgz", integrity: "sha512-j", dev: true },
    },
  };
}

test("on the real lock, exactly the lock's production entries are left, at the lock's versions", () => {
  const lock = JSON.parse(LOCK_TEXT) as Lockfile;
  const { result, after } = strip(lock, lock);
  expect(result.status).toBe(0);
  const production = Object.keys(lock.packages).filter((key) => lock.packages[key].dev !== true);
  const dev = Object.keys(lock.packages).filter((key) => lock.packages[key].dev === true);
  expect(dev.length).toBeGreaterThan(0);
  expect(result.stdout).toBe(`removed ${dev.length} dev entries; ${production.length - 1} remain\n`);
  const packages = (JSON.parse(after) as Lockfile).packages;
  expect(Object.keys(packages).sort()).toEqual(production.sort());
  for (const key of production) expect(packages[key]).toEqual(lock.packages[key]);
});

type Case = [string, (file: Lockfile) => Lockfile, string];

const UNREAD: Case[] = [
  ["lockfileVersion 2", (file) => ({ ...file, lockfileVersion: 2 }), "only lockfileVersion 3 is read"],
  ["a top-level dependencies section", (file) => ({ ...file, dependencies: {} }), 'top-level "dependencies"'],
  ["no root entry", (file) => ({ ...file, packages: { "node_modules/qs": file.packages["node_modules/qs"] } }), "root entry"],
];

test.each(UNREAD)("refuses %s, and writes nothing", (_name, change, refusal) => {
  const { result, before, after } = strip(change(small()), small());
  expect(result.status).toBe(1);
  expect(result.stderr).toContain(refusal);
  expect(after).toBe(before);
});

const NOT_THE_CLOSURE: Case[] = [
  [
    "a production entry missing (as if the strip had deleted node_modules/qs)",
    (file: Lockfile) => {
      delete file.packages["node_modules/qs"];
      return file;
    },
    "missing node_modules/qs",
  ],
  [
    "an entry the lock does not have",
    (file: Lockfile) => {
      file.packages["node_modules/extra"] = { version: "1.0.0" };
      return file;
    },
    "unexpected node_modules/extra",
  ],
  [
    "another version",
    (file: Lockfile) => {
      file.packages["node_modules/qs"].version = "6.17.1";
      return file;
    },
    'node_modules/qs: npm-shrinkwrap.json has version "6.17.1", the lock "6.16.1"',
  ],
  [
    "another integrity",
    (file: Lockfile) => {
      file.packages["node_modules/qs"].integrity = "sha512-other";
      return file;
    },
    "node_modules/qs: npm-shrinkwrap.json has integrity",
  ],
];

test.each(NOT_THE_CLOSURE)("refuses a result that is not the lock's production closure: %s", (_name, change, refusal) => {
  const { result, before, after } = strip(change(small()), small());
  expect(result.status).toBe(1);
  expect(result.stderr).toContain(refusal);
  expect(after).toBe(before);
});
