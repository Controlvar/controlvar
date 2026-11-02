#!/usr/bin/env node
// Removes the development entries from the MCP server's npm-shrinkwrap.json,
// in place, before it is packed:
//
//   node scripts/strip-shrinkwrap-dev.js <npm-shrinkwrap.json> <lock-copy>
//
// `npm shrinkwrap` keeps every entry of the lock, development ones included.
// An install from a registry whose metadata marks the version
// `_hasShrinkwrap` installs every entry of the shipped file, and a default
// install filters none of them out; an install of a local tarball ignores the
// file. So the shipped file holds the lock's production entries and nothing
// else, at the lock's versions.
//
// <lock-copy> is the package-lock.json the tests ran with, kept before
// `npm shrinkwrap` renamed it. The script refuses anything but a
// lockfileVersion 3 file with no top-level `dependencies`, deletes every
// `packages` entry marked `dev: true` and nothing else (the root entry is
// left as it is), and writes the file only if what is left is exactly the
// lock's non-development entries, each with the lock's version, resolved URL
// and integrity. It prints `removed N dev entries; M remain`.
//
// Called by the release (sdk-release.yml, "Shrinkwrap the MCP server") and by
// __tests__/packed.test.ts. Standard library only; not shipped (package.json's
// `files` names dist, README.md and npm-shrinkwrap.json).

"use strict";

const { readFileSync, writeFileSync, writeSync } = require("node:fs");

function fail(message) {
  // writeSync: an exit() straight after an asynchronous write to a pipe can
  // lose the message.
  writeSync(2, `strip-shrinkwrap-dev: ${message}\n`);
  process.exit(1);
}

function readLockfile(path) {
  let document;
  try {
    document = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    fail(`${path}: ${error.message}`);
  }
  if (document === null || typeof document !== "object" || Array.isArray(document)) {
    fail(`${path} is not a JSON object`);
  }
  if (document.lockfileVersion !== 3) {
    fail(`${path} is lockfileVersion ${JSON.stringify(document.lockfileVersion)}; only lockfileVersion 3 is read`);
  }
  if (Object.prototype.hasOwnProperty.call(document, "dependencies")) {
    fail(`${path} has a top-level "dependencies" section, which this script does not edit; only "packages" is read`);
  }
  const packages = document.packages;
  if (packages === null || typeof packages !== "object" || Array.isArray(packages) || !packages[""]) {
    fail(`${path} has no "packages" section with a root entry`);
  }
  return document;
}

const args = process.argv.slice(2);
if (args.length !== 2) {
  fail("usage: strip-shrinkwrap-dev.js <npm-shrinkwrap.json> <lock-copy>");
}
const [shrinkwrapPath, lockPath] = args;
const shrinkwrap = readLockfile(shrinkwrapPath);
const lock = readLockfile(lockPath);

let removed = 0;
for (const [key, entry] of Object.entries(shrinkwrap.packages)) {
  if (key !== "" && entry !== null && typeof entry === "object" && entry.dev === true) {
    delete shrinkwrap.packages[key];
    removed += 1;
  }
}

// What is left must be exactly the lock's production entries.
const problems = [];
const left = shrinkwrap.packages;
const wanted = Object.fromEntries(Object.entries(lock.packages).filter(([, entry]) => entry.dev !== true));
for (const [key, entry] of Object.entries(left)) {
  if (Object.prototype.hasOwnProperty.call(entry, "dev")) problems.push(`${key || '""'} still carries dev`);
}
for (const key of Object.keys(wanted).sort()) {
  if (!Object.prototype.hasOwnProperty.call(left, key)) problems.push(`missing ${key}`);
}
for (const key of Object.keys(left).sort()) {
  if (!Object.prototype.hasOwnProperty.call(wanted, key)) {
    problems.push(`unexpected ${key}`);
    continue;
  }
  for (const field of ["version", "resolved", "integrity"]) {
    if (left[key][field] !== wanted[key][field]) {
      problems.push(
        `${key || '""'}: npm-shrinkwrap.json has ${field} ${JSON.stringify(left[key][field])}, ` +
          `the lock ${JSON.stringify(wanted[key][field])}`,
      );
    }
  }
}
if (problems.length > 0) {
  fail(`the stripped shrinkwrap is not the lock's production closure, so it was not written:\n  ${problems.join("\n  ")}`);
}

writeFileSync(shrinkwrapPath, `${JSON.stringify(shrinkwrap, null, 2)}\n`);
process.stdout.write(`removed ${removed} dev entries; ${Object.keys(left).length - 1} remain\n`);
