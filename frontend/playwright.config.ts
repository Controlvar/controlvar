import { existsSync } from "node:fs";
import { join } from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { API_URL, BASE_URL, IS_EXTERNAL_BASE_URL, PORT } from "./tests/e2e/env";

/**
 * Playwright configuration for the Controlvar dashboard.
 *
 * Four projects:
 *   - `journeys` — the fail-hard PR journeys (`*.journey.spec.ts`), the
 *     required `browser-e2e` gate. Run them with `--project=journeys`.
 *   - `sso` — sign in with SSO from the dashboard (`*.sso.spec.ts`, C2b). It
 *     needs its own API process and the fake OIDC provider, which the
 *     `browser-e2e` job starts as steps; see tests/e2e/sso-sign-in.sso.spec.ts.
 *   - `extended` — everything else under tests/e2e (accessibility and the
 *     older specs); nightly territory, not a PR gate, but a failure there
 *     fails the night.
 *   - `visual` — `visual-regression.spec.ts` only. No baseline image has been
 *     committed (#943), so every test in it fails on the first comparison and
 *     no workflow runs this project. `npm run test:visual` (and
 *     `test:visual:update`, which writes the baselines) select it by file name.
 *
 * The web server is chosen automatically so nobody has to remember a flag:
 *   - a dashboard already answering on the base URL is reused (local dev);
 *   - otherwise, if `frontend/out` holds a static export (CI, after
 *     `npm run build`), it is served by tests/e2e/static-server.mjs exactly the
 *     way nginx serves it in the image — including the `/api` proxy, so the
 *     dashboard and the API share one origin and the browser sends no CORS
 *     preflight (build that export with NEXT_PUBLIC_API_URL="");
 *   - otherwise `npm run dev` is started.
 *
 * Environment variables:
 *   PLAYWRIGHT_BASE_URL    dashboard origin        (default http://localhost:3100)
 *   PLAYWRIGHT_API_URL     backend origin          (default http://localhost:8000)
 *   PLAYWRIGHT_PORT        port for the managed server (default 3100)
 *   PLAYWRIGHT_WEB_SERVER  auto | dev | export | none   (default auto)
 *   PLAYWRIGHT_HEADLESS    "false" to watch the browser
 */

const HEADLESS = process.env.PLAYWRIGHT_HEADLESS !== "false";
const hasExport = existsSync(join(__dirname, "out", "index.html"));

type ServerMode = "auto" | "dev" | "export" | "none";
const requestedMode = (process.env.PLAYWRIGHT_WEB_SERVER as ServerMode) ?? "auto";
const mode: ServerMode =
  requestedMode !== "auto"
    ? requestedMode
    : IS_EXTERNAL_BASE_URL
      ? "none" // someone pointed us at their own stack
      : hasExport
        ? "export"
        : "dev";

const webServer =
  mode === "none"
    ? undefined
    : {
        command:
          mode === "export"
            ? `node tests/e2e/static-server.mjs --port ${PORT} --api ${API_URL}`
            : `npm run dev -- --port ${PORT}`,
        url: BASE_URL,
        // Locally, a dev server already on the port is the stack under test.
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        stdout: "pipe" as const,
        stderr: "pipe" as const,
      };

const use = {
  baseURL: BASE_URL,
  headless: HEADLESS,
  screenshot: "only-on-failure" as const,
  video: "retain-on-failure" as const,
  trace: "retain-on-failure" as const,
  actionTimeout: 15_000,
  navigationTimeout: 30_000,
};

export default defineConfig({
  testDir: "./tests/e2e",
  // Journeys walk one backend through real state transitions; serialise them.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  // One retry absorbs network jitter in CI; a journey that fails twice fails the PR.
  retries: process.env.CI ? 1 : 0,
  // Generous: a journey that trips the backend's 10/min login limit waits the
  // window out (see LoginPage.submitResilient) before it can continue.
  timeout: 120_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [["html", { outputFolder: "playwright-report", open: "never" }], ["github"], ["list"]]
    : [["html", { outputFolder: "playwright-report", open: "never" }], ["list"]],

  use,

  projects: [
    {
      name: "journeys",
      testMatch: /\.journey\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "sso",
      testMatch: /\.sso\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "extended",
      testMatch: /\.spec\.ts$/,
      // The visual spec has its own project below, until its baselines exist.
      testIgnore: [/\.journey\.spec\.ts$/, /\.sso\.spec\.ts$/, /visual-regression\.spec\.ts$/],
      use: { ...devices["Desktop Chrome"] },
    },
    {
      // Not run by any workflow. Move it back into `extended` (delete the
      // ignore above and this project) in the change that commits the images.
      name: "visual",
      testMatch: /visual-regression\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  webServer,
  outputDir: "playwright-results",
});
