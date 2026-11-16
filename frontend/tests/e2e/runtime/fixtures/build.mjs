// Builds the page-runtime project's framework fixtures (#1545) with the
// dashboard's own next and react: `node tests/e2e/runtime/fixtures/build.mjs`
// from frontend/. Each fixture's static export lands in its own out/
// (gitignored); the specs fail, saying so, when it is missing.
import { spawnSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = join(here, "..", "..", "..", "..");
const next = join(frontend, "node_modules", "next", "dist", "bin", "next");

export const FIXTURES = ["app-router"];

for (const name of FIXTURES) {
  const dir = join(here, name);
  rmSync(join(dir, "out"), { recursive: true, force: true });
  rmSync(join(dir, ".next"), { recursive: true, force: true });
  const result = spawnSync(process.execPath, [next, "build", dir], {
    cwd: frontend,
    stdio: "inherit",
    env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1" },
  });
  if (result.status !== 0) {
    throw new Error(`next build ${name} exited ${result.status}`);
  }
  if (!existsSync(join(dir, "out", "index.html"))) {
    throw new Error(`next build ${name} wrote no out/index.html`);
  }
}
