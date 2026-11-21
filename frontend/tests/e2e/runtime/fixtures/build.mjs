// Builds the page-runtime project's framework fixtures (#1545):
// `node tests/e2e/runtime/fixtures/build.mjs` from frontend/, after
// sdk/visual's `npm ci` (its devDependencies hold esbuild and React 18.3.1).
// Each fixture's page lands in its own out/ (gitignored); the specs fail,
// saying so, when it is missing.
//   FIXTURES: Next sites (App Router, Pages Router), static exports built
//     with the dashboard's own next and react (19.3).
//   STANDALONE: a page rendered with react-dom/server and hydrated with
//     hydrateRoot (standalone/build.mjs): React 18.3.1 from sdk/visual, and
//     the dashboard's React 19.3 with two sibling Suspense boundaries.
import { spawnSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = join(here, "..", "..", "..", "..");
const sdkVisual = join(frontend, "..", "sdk", "visual");
const next = join(frontend, "node_modules", "next", "dist", "bin", "next");

export const FIXTURES = ["app-router", "pages-router"];
export const STANDALONE = { react18: [sdkVisual, "0"], "react19-suspense": [frontend, "1"] };

function run(name, args, env) {
  const result = spawnSync(process.execPath, args, { cwd: frontend, stdio: "inherit", env: { ...process.env, ...env } });
  if (result.status !== 0) {
    throw new Error(`building ${name} exited ${result.status}`);
  }
  if (!existsSync(join(here, name, "out", "index.html"))) {
    throw new Error(`building ${name} wrote no out/index.html`);
  }
}

for (const name of FIXTURES) {
  const dir = join(here, name);
  rmSync(join(dir, "out"), { recursive: true, force: true });
  rmSync(join(dir, ".next"), { recursive: true, force: true });
  run(name, [next, "build", dir], { NEXT_TELEMETRY_DISABLED: "1" });
}
for (const [name, [reactRoot, split]] of Object.entries(STANDALONE)) {
  run(name, [join(here, "standalone", "build.mjs"), name, reactRoot, split], { NODE_ENV: "production" });
}
