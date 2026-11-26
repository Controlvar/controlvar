import { devices } from "@playwright/test";
import { routeSite, SITE } from "./editor";
import { expect, test } from "./runtime.fixture";
import { answering, calls, sitePage, VISUAL_PATH } from "./visual";

/**
 * The device type the packed script sends from an iPhone (#1545), by the
 * user agent alone: Playwright's iPhone 15 profile, in Chromium. Chromium's
 * emulation exposes navigator.userAgentData, which Safari does not have, so
 * an init script deletes it first, and the row takes the path a real iPhone
 * takes. The API is stubbed (routeSite).
 */

test.use({ ...devices["iPhone 15"], browserName: "chromium" });

function withoutUserAgentData() {
  delete (Navigator.prototype as unknown as { userAgentData?: unknown }).userAgentData;
}

test("an iPhone without userAgentData is a phone", async ({ page, visualPackage, browserName, browser }) => {
  expect(browserName).toBe("chromium");
  expect(browser.browserType().name()).toBe("chromium");
  await page.addInitScript(withoutUserAgentData);
  const site = await routeSite(page, visualPackage, {
    pages: { "/a": sitePage('<h1 id="t">Original</h1>') },
    answer: answering({ experiments: [] }),
  });
  await page.goto(`${SITE}/a`);
  expect(await page.evaluate(() => "userAgentData" in navigator), "userAgentData is gone").toBe(false);
  expect(await page.evaluate(() => navigator.userAgent)).toContain("iPhone");
  await expect.poll(() => calls(site, VISUAL_PATH).length, { message: "the call for the page" }).toBe(1);
  const context = (calls(site, VISUAL_PATH)[0].body as { context: Record<string, unknown> }).context;
  expect(`iPhone without uaData: ${context.web_device}`).toBe("iPhone without uaData: phone");
});
