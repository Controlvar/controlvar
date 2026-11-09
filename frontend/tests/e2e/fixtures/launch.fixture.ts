import { test as base, expect, type Browser } from "@playwright/test";
import { launchOptions, PAINT_HOLDING, STORAGE_PARTITIONING } from "../launch";

/**
 * V112's first half (#1545): the browser the `launch` project's specs run in
 * disables neither paint holding nor third-party storage partitioning, and
 * its only `--disable-features=` argument is the override's.
 *
 * An automatic fixture at worker scope, so it reads the browser this test
 * process launched for its specs, once, before the first test runs. A
 * config-wide `globalSetup` would launch a browser of its own and check that
 * one instead. Specs of the `launch` project import `test` and `expect` from
 * this file.
 *
 * The command line comes from CDP `SystemInfo.getInfo`. On the Chromium
 * Headless Shell that Playwright 1.64.0 installs (156.0.8078.4),
 * `Browser.getBrowserCommandLine` answers "Command line not returned because
 * --enable-automation not set" and `chrome://version` does not load
 * (ERR_INVALID_URL). An empty command line, or one with no
 * `--disable-features=` argument (the override always passes one), fails
 * here instead of passing with nothing to look at.
 *
 * Last, the command line must carry exactly one `--disable-features=`
 * argument, the override's. Chromium applies only the last such argument, so
 * when `ignoreDefaultArgs` stops matching (a Playwright bump that changes the
 * default list), Playwright's default stays in front of the override's, the
 * override's list still wins, and both features stay on: the checks above
 * and the canary pair all pass. This check is the one that fails then, and
 * also when something adds a `--disable-features=` of its own.
 */

/** The value of every `--disable-features=` argument, in command-line order. */
function disableFeaturesValues(commandLine: string): string[] {
  return commandLine
    .split(/\s+/)
    .map((arg) => arg.replace(/^"|"$/g, ""))
    .filter((arg) => arg.startsWith("--disable-features="))
    .map((arg) => arg.slice("--disable-features=".length));
}

async function browserCommandLine(browser: Browser): Promise<string> {
  const session = await browser.newBrowserCDPSession();
  try {
    const { commandLine } = await session.send("SystemInfo.getInfo");
    return commandLine;
  } finally {
    await session.detach();
  }
}

export const test = base.extend<object, { launchFeatures: void }>({
  launchFeatures: [
    async ({ browser }, use) => {
      const commandLine = await browserCommandLine(browser);
      const values = disableFeaturesValues(commandLine);
      if (values.length === 0) {
        throw new Error(
          `the browser's command line has no --disable-features= argument: ${JSON.stringify(commandLine.slice(0, 300))}`,
        );
      }
      for (const feature of [PAINT_HOLDING, STORAGE_PARTITIONING]) {
        const hit = values.find((value) => value.split(",").includes(feature));
        if (hit !== undefined) {
          throw new Error(`${feature} disabled (--disable-features=${hit})`);
        }
      }
      const expected = (launchOptions.args ?? []).filter((arg) => arg.startsWith("--disable-features="));
      if (values.length !== 1 || `--disable-features=${values[0]}` !== expected[0]) {
        throw new Error(`the override no longer replaces the default list: ${JSON.stringify(values)}`);
      }
      await use();
    },
    { scope: "worker", auto: true },
  ],
});

export { expect };
