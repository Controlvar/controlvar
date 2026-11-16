import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
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

export const test = launchTest.extend<object, { visualPackage: VisualPackage }>({
  visualPackage: [
    // Playwright reads a fixture's dependencies from this pattern; it has none.
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      await use(loadVisualPackage());
    },
    { scope: "worker" },
  ],
});

export { expect };
