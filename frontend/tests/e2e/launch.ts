import type { LaunchOptions } from "@playwright/test";

/**
 * The launch override for browser gates that must run the browser users run
 * (#1545, V112).
 *
 * Playwright's default Chromium launch passes one `--disable-features=<list>`
 * argument, and two of the features in that list are on in users' Chrome:
 * third-party storage partitioning (a frame under another site gets its own
 * storage) and paint holding (a same-origin navigation keeps showing the old
 * page until the new one has something to paint). `ignoreDefaultArgs` drops
 * the default argument and `args` passes the same list without those two.
 * Every other feature Playwright disables stays disabled.
 *
 * `ignoreDefaultArgs` removes an argument only when it is exactly equal to
 * the string given, so DEFAULT must be playwright-core's value byte for byte.
 * A wrong string matches nothing: the default argument stays and the one in
 * `args` follows it. Measured on Chromium Headless Shell 156, the canary pair
 * then passes: Chromium uses the later argument, so storage partitioning is
 * on, and every other disabled feature comes from the stale list.
 * tests/e2e/fixtures/launch.fixture.ts fails on that command line anyway,
 * because the default argument still names both: a bump that changes the
 * list fails V112 until DEFAULT is updated (measured with 1.63's list).
 *
 * Only the `launch` project (playwright.config.ts) uses this today; the other
 * projects keep Playwright's default launch.
 */

// Intent: this is playwright-core 1.64.0's default; V112 fails if a bump changes it.
const DEFAULT =
  "AvoidUnnecessaryBeforeUnloadCheckSync,DestroyProfileOnBrowserClose,DialMediaRouteProvider," +
  "GlobalMediaControls,HttpsUpgrades,LensOverlay,MediaRouter,PaintHolding," +
  "ThirdPartyStoragePartitioning,BlockOriginHeaderModificationOnRedirect,Translate," +
  "AutoDeElevate,OptimizationHints,NetworkTimeServiceQuerying,AimEnabled," +
  "msForceBrowserSignIn,msEdgeUpdateLaunchServicesPreferredVersion";

export const PAINT_HOLDING = "PaintHolding";
export const STORAGE_PARTITIONING = "ThirdPartyStoragePartitioning";

const kept = DEFAULT.split(",")
  .filter((feature) => feature !== PAINT_HOLDING && feature !== STORAGE_PARTITIONING)
  .join(",");

export const launchOptions: LaunchOptions = {
  ignoreDefaultArgs: [`--disable-features=${DEFAULT}`],
  args: [`--disable-features=${kept}`],
};
