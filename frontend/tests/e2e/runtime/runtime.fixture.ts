import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { Browser } from "@playwright/test";
import { expect, test as launchTest } from "../fixtures/launch.fixture";

/**
 * The `page-runtime` project's fixture (#1545): the launch fixture (V112:
 * the browser runs with storage partitioning and paint holding on) plus the
 * packed sdk/visual package.
 *
 * The specs load the script a release would ship: the files inside sdk/visual's
 * `npm pack` tarball, never sdk/visual/dist. VISUAL_PACKAGE_DIR names the
 * tarball's `package/` directory, unpacked. Browser E2E sets it; locally:
 *
 *   (cd sdk/visual && npm ci && npm run build && npm pack --pack-destination /tmp/pack)
 *   tar -xzf /tmp/pack/controlvar-visual-*.tgz -C /tmp/pack
 *   VISUAL_PACKAGE_DIR=/tmp/pack/package npx playwright test --project=page-runtime
 *
 * Without it, or with a directory that holds no packed script, every test
 * that asks for `visualPackage` fails with the reason. It is read when a test
 * asks for it, never when the file is loaded, so `--list` still lists.
 */

/** The fixture site's origin. Specs route it with `page.route`; nothing listens there. */
export const SITE_ORIGIN = `http://localhost:${Number(process.env.VISUAL_SITE_PORT ?? 4100)}`;

export interface VisualPackage {
  /** The unpacked `package/` directory. */
  dir: string;
  /** The version in the packed package.json. */
  version: string;
  /** The packed classic script, dist/controlvar-visual.js, as text. */
  classicScript: string;
}

function loadVisualPackage(): VisualPackage {
  const dir = process.env.VISUAL_PACKAGE_DIR;
  if (!dir) {
    throw new Error(
      "VISUAL_PACKAGE_DIR is not set: pack sdk/visual (npm ci, npm run build, npm pack), unpack the " +
        "tarball, and set it to the package/ directory (tests/e2e/runtime/runtime.fixture.ts says how)",
    );
  }
  const manifest = join(dir, "package.json");
  const classic = join(dir, "dist", "controlvar-visual.js");
  for (const file of [manifest, classic]) {
    if (!existsSync(file)) {
      throw new Error(`${file} does not exist: VISUAL_PACKAGE_DIR is not an unpacked sdk/visual tarball`);
    }
  }
  return {
    dir,
    version: JSON.parse(readFileSync(manifest, "utf8")).version,
    classicScript: readFileSync(classic, "utf8"),
  };
}

const SENTINEL_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>sentinel</title>
<script src="/controlvar-visual.js" data-api-url="${SITE_ORIGIN}" data-api-key="test-website-key"></script>
</head><body><p id="sentinel">Original</p></body></html>`;

const SENTINEL_ANSWER = {
  experiments: [
    {
      experiment_key: "sentinel",
      variant_id: "v1",
      variant_name: "B",
      assigned: true,
      reason: "assigned",
      changes: { version: 1, changes: [{ op: "set_text", selector: "#sentinel", text: "Changed" }] },
      goals: [],
    },
  ],
  pages: { host: "localhost", rules: [{ match: "exact", path: "/sentinel" }], max_age: 60 },
};

/**
 * After every test, in a fresh browser context: the packed script loads, its
 * version is the packed package.json's, and it applies a sentinel change. A
 * test whose own page shows nothing (a script that never loaded, say) cannot
 * pass while the script is broken.
 */
async function positiveControl(browser: Browser, visualPackage: VisualPackage): Promise<void> {
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await page.route(`${SITE_ORIGIN}/**`, async (route) => {
      const { pathname } = new URL(route.request().url());
      if (pathname === "/controlvar-visual.js") {
        await route.fulfill({ contentType: "text/javascript", body: visualPackage.classicScript });
      } else if (pathname === "/sentinel") {
        await route.fulfill({ contentType: "text/html", body: SENTINEL_PAGE });
      } else if (pathname === "/api/v1/tracking/visual") {
        await route.fulfill({ contentType: "application/json", body: JSON.stringify(SENTINEL_ANSWER) });
      } else {
        await route.fulfill({ status: 404, body: "" });
      }
    });
    await page.goto(`${SITE_ORIGIN}/sentinel`);
    await expect(page.locator("#sentinel"), "sentinel").toHaveText("Changed");
    const version = await page.evaluate(
      () => (window as unknown as { controlvar?: { visual?: { version?: string } } }).controlvar?.visual?.version,
    );
    expect(version, "the packed script's version").toBe(visualPackage.version);
  } finally {
    await context.close();
  }
}

export const test = launchTest.extend<{ positiveControl: void }, { visualPackage: VisualPackage }>({
  visualPackage: [
    // Playwright reads a fixture's dependencies from this pattern; it has none.
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      await use(loadVisualPackage());
    },
    { scope: "worker" },
  ],
  positiveControl: [
    async ({ browser, visualPackage }, use) => {
      await use();
      await positiveControl(browser, visualPackage);
    },
    { auto: true },
  ],
});

export { expect };
