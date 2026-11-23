import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { contentType } from "./editor";

/**
 * The built pages of a page-runtime framework fixture (#1545,
 * tests/e2e/runtime/fixtures), by the path each is served at, plus the
 * images the shared page loads. A missing build fails the test that asks.
 */
export const FIXTURES_DIR = join(__dirname, "fixtures");

/** A 1x1 PNG, for every image the pages load. */
export const PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
  "base64",
);

export function fixtureFiles(fixture: string): Record<string, { body: Buffer; contentType: string }> {
  const out = join(FIXTURES_DIR, fixture, "out");
  if (!existsSync(join(out, "index.html"))) {
    throw new Error(`${out}/index.html does not exist: build the fixtures first (from frontend/: node tests/e2e/runtime/fixtures/build.mjs)`);
  }
  const files: Record<string, { body: Buffer; contentType: string }> = {};
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else {
        const served = `/${relative(out, path).split("\\").join("/")}`;
        files[served] = { body: readFileSync(path), contentType: contentType(served) };
      }
    }
  };
  walk(out);
  for (const image of ["/a.png", "/b.png", "/a.webp"]) files[image] = { body: PIXEL, contentType: "image/png" };
  return files;
}
