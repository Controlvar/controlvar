import { defineConfig } from "@playwright/test";

/**
 * The probe behind the Playwright JSON reports beside regenerate.py. No test
 * opens a page, so no browser is downloaded or launched. The files are named
 * as the dashboard's page-runtime specs are, under the same testDir, so a
 * report reads as one of frontend/playwright.config.ts's would.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 1,
  reporter: [["list"]],
  projects: [
    { name: "page-runtime", testMatch: /outcomes\.runtime\.spec\.ts$/ },
    // The same file in a second project, for the report of a run of both.
    { name: "extended", testMatch: /outcomes\.runtime\.spec\.ts$/ },
    { name: "load-error", testMatch: /load-error\.runtime\.spec\.ts$/ },
    { name: "duplicate", testMatch: /duplicate\.runtime\.spec\.ts$/ },
  ],
});
