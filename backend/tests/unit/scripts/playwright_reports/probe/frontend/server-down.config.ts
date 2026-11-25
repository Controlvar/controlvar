import { defineConfig } from "@playwright/test";

/** The probe's run when the web server does not start: its command exits 3. */
export default defineConfig({
  testDir: "./tests/e2e",
  workers: 1,
  reporter: [["list"]],
  projects: [{ name: "page-runtime", testMatch: /outcomes\.runtime\.spec\.ts$/ }],
  webServer: {
    command: 'node -e "process.exit(3)"',
    url: "http://127.0.0.1:1/",
    reuseExistingServer: false,
    timeout: 10_000,
  },
});
