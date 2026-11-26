import { devices } from "@playwright/test";
import { routeSite, SITE } from "./editor";
import { expect, test } from "./runtime.fixture";
import { answering, calls, sitePage, VISUAL_PATH } from "./visual";

/**
 * The device type the packed script sends from an iPad that reports a Mac
 * (#1545), as iPadOS Safari does by default: Playwright's iPad Pro 11
 * profile with that user agent, in Chromium. Chromium's emulation exposes
 * navigator.userAgentData and reports one touch point, so an init script
 * deletes userAgentData and sets the touch points: five make it a tablet,
 * none leave the same user agent a desktop. The API is stubbed (routeSite).
 */

const IPADOS = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15";

test.use({ ...devices["iPad Pro 11"], userAgent: IPADOS, browserName: "chromium" });

function withoutUserAgentData() {
  delete (Navigator.prototype as unknown as { userAgentData?: unknown }).userAgentData;
}

function touchPoints(points: number) {
  Object.defineProperty(Navigator.prototype, "maxTouchPoints", { configurable: true, get: () => points });
}

test("five touch points make it a tablet; none make the same user agent a desktop", async ({
  page,
  context,
  visualPackage,
  browserName,
  browser,
}) => {
  expect(browserName).toBe("chromium");
  expect(browser.browserType().name()).toBe("chromium");
  const seen: string[] = [];
  for (const [points, tab] of [[5, page], [0, await context.newPage()]] as const) {
    await tab.addInitScript(withoutUserAgentData);
    await tab.addInitScript(touchPoints, points);
    const site = await routeSite(tab, visualPackage, {
      pages: { "/a": sitePage('<h1 id="t">Original</h1>') },
      answer: answering({ experiments: [] }),
    });
    await tab.goto(`${SITE}/a`);
    expect(await tab.evaluate(() => navigator.maxTouchPoints), "the touch points").toBe(points);
    expect(await tab.evaluate(() => "userAgentData" in navigator), "userAgentData is gone").toBe(false);
    expect(await tab.evaluate(() => navigator.userAgent)).toBe(IPADOS);
    await expect.poll(() => calls(site, VISUAL_PATH).length, { message: "the call for the page" }).toBe(1);
    const sent = (calls(site, VISUAL_PATH)[0].body as { context: Record<string, unknown> }).context;
    seen.push(`${points} touch points: ${sent.web_device}`);
  }
  expect(`iPadOS: ${seen[0].split(": ")[1]}`).toBe("iPadOS: tablet");
  expect(seen).toEqual(["5 touch points: tablet", "0 touch points: desktop"]);
});
