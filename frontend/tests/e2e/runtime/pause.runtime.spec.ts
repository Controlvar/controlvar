import type { Page } from "@playwright/test";
import { routeSite, SITE, storageKeys } from "./editor";
import { expect, test } from "./runtime.fixture";
import { answering, calls, experiment, PREVIEW_PATH, previewAnswer, sitePage, VISUAL_PATH } from "./visual";

/**
 * The 5-minute pause (#2173). A call the page's own navigation cuts off before
 * its answer comes starts no pause in the tab, so each later page asks as
 * usual; a call that fails while the page is open still starts one. One tab
 * opens five pages in turn, each left once its load event has fired and its
 * call has reached the API, which answers none of them before the page goes.
 */

const TOKEN = "p".repeat(40) + "abc";
const BODY = `<h1 id="t">Original</h1>`;
const HEADLINE = [{ op: "set_text", selector: "#t", text: "Changed" }];
const PAGES = Object.fromEntries([1, 2, 3, 4, 5].map((n) => [`/p/${n}`, sitePage(BODY)]));

/** The count once it reaches `want`, or what it is 2 s later if it does not. */
async function arrived(count: () => number, want: number): Promise<number> {
  await expect
    .poll(count, { timeout: 2000 })
    .toBeGreaterThanOrEqual(want)
    .catch(() => undefined);
  return count();
}

/** Opens the five pages in one tab; after each load, the number of calls to `path` the API has had. */
async function fivePages(page: Page, count: () => number, first: string): Promise<number[]> {
  const seen: number[] = [];
  for (const n of [1, 2, 3, 4, 5]) {
    await page.goto(n === 1 ? first : `${SITE}/p/${n}`, { waitUntil: "load" });
    seen.push(await arrived(count, n));
  }
  return seen;
}

test.describe("a page left before its call is answered", () => {
  test("a preview tab asks for the variant on each of five pages and keeps no pause", async ({ page, visualPackage }) => {
    let release: () => void = () => undefined;
    const holdPreview = new Promise<void>((resolve) => (release = resolve));
    const site = await routeSite(page, visualPackage, {
      pages: PAGES,
      answer: answering({ experiments: [], preview: previewAnswer("headline", HEADLINE), holdPreview }),
    });
    try {
      const seen = await fivePages(page, () => calls(site, PREVIEW_PATH).length, `${SITE}/p/1#controlvar-preview=${TOKEN}`);
      expect(
        { calls: seen, keys: await storageKeys(page) },
        "preview calls the API has had after each page, and the storage keys after the fifth",
      ).toEqual({ calls: [1, 2, 3, 4, 5], keys: ["session:controlvar_preview"] });
      expect(calls(site, VISUAL_PATH), "calls for other experiments").toEqual([]);
    } finally {
      release();
      await page.unrouteAll({ behavior: "ignoreErrors" });
    }
  });

  test("an ordinary tab asks for its changes on each of five pages and keeps no pause", async ({ page, visualPackage }) => {
    let release: () => void = () => undefined;
    const hold = new Promise<void>((resolve) => (release = resolve));
    const site = await routeSite(page, visualPackage, {
      pages: PAGES,
      answer: answering({ experiments: [experiment("headline", HEADLINE)], hold }),
    });
    try {
      const seen = await fivePages(page, () => calls(site, VISUAL_PATH).length, `${SITE}/p/1`);
      const paused = (await storageKeys(page)).includes("session:controlvar_backoff");
      expect({ calls: seen, paused }, "calls the API has had after each page, and the pause after the fifth").toEqual({
        calls: [1, 2, 3, 4, 5],
        paused: false,
      });
      expect(calls(site, VISUAL_PATH).map((call) => (call.body as { path: string }).path)).toEqual(["/p/1", "/p/2", "/p/3", "/p/4", "/p/5"]);
    } finally {
      release();
      await page.unrouteAll({ behavior: "ignoreErrors" });
    }
  });

  test("a call that fails while the page is open still pauses the tab for 5 minutes: the next page makes no call", async ({
    page,
    visualPackage,
  }) => {
    await routeSite(page, visualPackage, { pages: PAGES, answer: answering({ experiments: [] }) });
    // Registered after routeSite's, so it answers first: the call fails as a lost network does.
    let failed = 0;
    await page.route(`${SITE}${VISUAL_PATH}`, async (route) => {
      failed += 1;
      await route.abort("failed");
    });
    await page.goto(`${SITE}/p/1`, { waitUntil: "load" });
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem("controlvar_backoff"))).not.toBeNull();
    const left = await page.evaluate(() => Number(sessionStorage.getItem("controlvar_backoff")) - Date.now());
    expect(left, "the pause, in ms").toBeGreaterThan(4 * 60 * 1000);
    expect(left, "the pause, in ms").toBeLessThanOrEqual(5 * 60 * 1000);
    await page.goto(`${SITE}/p/2`, { waitUntil: "load" });
    expect(failed, "calls").toBe(1);
    expect(await storageKeys(page), "storage keys").toContain("session:controlvar_backoff");
  });
});
