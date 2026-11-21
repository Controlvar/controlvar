import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, SITE_ORIGIN, test } from "./runtime.fixture";

/**
 * The packed script is the one in the tree (#1545): a page that loads the
 * tarball's classic script with `<script src>` gets `window.controlvar.visual`,
 * whose version is the one sdk/visual/package.json carries in this checkout.
 * A stale tarball, a build that took its version from anywhere else, or a
 * script that fails to run as a classic script fails it.
 */

const SOURCE_MANIFEST = resolve(__dirname, "../../../../sdk/visual/package.json");

const PAGE = `<!doctype html>
<html><head><meta charset="utf-8"><title>fixture site</title>
<script src="/controlvar-visual.js"></script>
</head><body><p id="t">Original</p></body></html>`;

test("the packed script loads with a script tag and reports the version in sdk/visual/package.json", async ({
  page,
  visualPackage,
}) => {
  const expected = JSON.parse(readFileSync(SOURCE_MANIFEST, "utf8")).version;
  expect(visualPackage.version, "the packed package.json").toBe(expected);

  await page.route(`${SITE_ORIGIN}/**`, async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === "/") {
      await route.fulfill({ contentType: "text/html; charset=utf-8", body: PAGE });
    } else if (pathname === "/controlvar-visual.js") {
      await route.fulfill({ contentType: "text/javascript; charset=utf-8", body: visualPackage.classicScript });
    } else {
      await route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
    }
  });
  await page.goto(`${SITE_ORIGIN}/`, { waitUntil: "load" });

  const version = await page.evaluate(
    () => (window as unknown as { controlvar?: { visual?: { version?: unknown } } }).controlvar?.visual?.version,
  );
  expect(version, "window.controlvar.visual.version").toBe(expected);
});
