import type { Page } from "@playwright/test";
import { expect, test, type VisualPackage } from "./runtime.fixture";
import { routeSite, SITE } from "./editor";
import { answering, calls, experiment, shown, sitePage, VISUAL_PATH } from "./visual";

/**
 * The hide (#1545): while the script waits for the page's changes, <html> is
 * hidden with visibility (the page paints nothing and takes no click or Tab),
 * it shows as soon as the changes are applied, never later than
 * data-timeout-ms, and whatever goes wrong. Each test is a fresh navigation
 * in a new page, so the browser's paint holding (a same-origin navigation
 * feature) cannot be what hides anything here.
 */

const BODY = '<h1 id="t">Original</h1><button id="b" style="width:200px;height:60px">Buy</button><a id="l" href="#x">x</a>';
const CHANGE = [{ op: "set_text", selector: "#t", text: "Changed" }];

/** The page's timers run only when the test says so (install, then pauseAt). */
async function stopClock(page: Page): Promise<void> {
  const start = new Date("2026-01-01T00:00:00Z");
  await page.clock.install({ time: start });
  await page.clock.pauseAt(new Date(start.getTime() + 1000));
}

test.describe("the timeout", () => {
  for (const [attribute, timeout] of [
    [null, 1000],
    ["10000", 4000],
  ] as const) {
    test(`shows the page at ${timeout} ms when the API never answers (data-timeout-ms ${attribute ?? "absent"})`, async ({ page, visualPackage }) => {
      await stopClock(page);
      const never = new Promise<void>(() => undefined);
      const site = await routeSite(page, visualPackage, {
        pages: { "/": sitePage(BODY, { "data-timeout-ms": attribute }) },
        answer: answering({ experiments: [experiment("never", CHANGE)], hold: never }),
      });
      await page.goto(`${SITE}/`, { waitUntil: "domcontentloaded" });
      await expect.poll(() => calls(site, VISUAL_PATH).length).toBe(1);
      await page.clock.runFor(timeout - 1);
      expect(await page.evaluate(() => getComputedStyle(document.documentElement).visibility)).toBe("hidden");
      await page.clock.runFor(1);
      expect(await page.evaluate(() => getComputedStyle(document.documentElement).visibility)).toBe("visible");
      await expect(page.locator("#t")).toHaveText("Original");
    });
  }
});

test("an answer that comes after the timeout is still applied, so the variant counted is the one shown", async ({ page, visualPackage }) => {
  await stopClock(page);
  let release: () => void = () => undefined;
  const hold = new Promise<void>((resolve) => (release = resolve));
  const site = await routeSite(page, visualPackage, {
    pages: { "/": sitePage(BODY) },
    answer: answering({ experiments: [experiment("late", CHANGE)], hold }),
  });
  await page.goto(`${SITE}/`, { waitUntil: "domcontentloaded" });
  await expect.poll(() => calls(site, VISUAL_PATH).length).toBe(1);
  await page.clock.runFor(1000);
  expect(await shown(page)).toBe(true);
  await expect(page.locator("#t")).toHaveText("Original");
  release();
  await expect(page.locator("#t")).toHaveText("Changed");
});

test.describe("failures", () => {
  test("a script that never loads leaves the page shown", async ({ page, visualPackage }) => {
    await routeSite(page, visualPackage, { pages: { "/": sitePage(BODY) } });
    await page.route(`${SITE}/controlvar-visual.js`, (route) => route.abort());
    await page.goto(`${SITE}/`);
    expect(await shown(page)).toBe(true);
    await expect(page.locator("#t")).toBeVisible();
  });

  test("a script that throws while it applies still shows the page, with no timer needed", async ({ page, visualPackage }) => {
    await stopClock(page);
    let release: () => void = () => undefined;
    const hold = new Promise<void>((resolve) => (release = resolve));
    const site = await routeSite(page, visualPackage, {
      // A later script breaks JSON.stringify before the answer arrives.
      pages: { "/": sitePage(`${BODY}<script>JSON.stringify = function () { throw new Error("broken by the page"); };</script>`) },
      answer: answering({ experiments: [experiment("throws", CHANGE)], hold }),
    });
    await page.goto(`${SITE}/`);
    await expect.poll(() => calls(site, VISUAL_PATH).length).toBe(1);
    expect(await shown(page)).toBe(false);
    release();
    await expect.poll(() => shown(page), { message: "shown after the answer, with the clock stopped" }).toBe(true);
  });
});

test.describe("when it works", () => {
  test("no frame shows the original text: the page shows once the change is applied", async ({ page, visualPackage }) => {
    await page.addInitScript(() => {
      const frames: { visible: boolean; text: string | null }[] = [];
      (window as unknown as { __frames: typeof frames }).__frames = frames;
      const sample = () => {
        const element = document.getElementById("t");
        frames.push({
          visible: getComputedStyle(document.documentElement).visibility !== "hidden",
          text: element ? element.textContent : null,
        });
        if (frames.length < 2000) requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    });
    let release: () => void = () => undefined;
    const hold = new Promise<void>((resolve) => (release = resolve));
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY) },
      answer: answering({ experiments: [experiment("headline", CHANGE)], hold }),
    });
    await page.goto(`${SITE}/`, { waitUntil: "domcontentloaded" });
    await expect.poll(() => calls(site, VISUAL_PATH).length).toBe(1);
    setTimeout(release, 300);
    await expect(page.locator("#t")).toHaveText("Changed");
    await expect.poll(() => shown(page)).toBe(true);
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const frames = await page.evaluate(() => (window as unknown as { __frames: { visible: boolean; text: string | null }[] }).__frames);
    const visible = frames.filter((frame) => frame.visible);
    expect(visible.filter((frame) => frame.text === "Original"), "visible frames with the original text").toHaveLength(0);
    expect(visible.filter((frame) => frame.text === "Changed").length, "visible frames with the change").toBeGreaterThan(0);
  });

  /**
   * A page whose answer is held until the returned function releases it. The
   * page records when the script's hide went on <html> (its style attribute
   * gets a value) and when it came off (the attribute is empty again),
   * whatever property the hide uses.
   */
  async function heldPage(page: Page, visualPackage: VisualPackage) {
    await page.addInitScript(() => {
      const state = { hiddenAt: -1, shownAt: -1, clicks: 0 };
      (window as unknown as { __hide: typeof state }).__hide = state;
      new MutationObserver(() => {
        const style = document.documentElement.getAttribute("style");
        if (style && state.hiddenAt < 0) state.hiddenAt = performance.now();
        if (!style && state.hiddenAt >= 0 && state.shownAt < 0) state.shownAt = performance.now();
      }).observe(document, { attributes: true, attributeFilter: ["style"], subtree: true });
      document.addEventListener("click", (event) => {
        if ((event.target as Element).id === "b") state.clicks += 1;
      }, true);
    });
    let release: () => void = () => undefined;
    const hold = new Promise<void>((resolve) => (release = resolve));
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY, { "data-timeout-ms": "4000" }) },
      answer: answering({ experiments: [experiment("headline", CHANGE)], hold }),
    });
    await page.goto(`${SITE}/`, { waitUntil: "domcontentloaded" });
    await expect.poll(() => calls(site, VISUAL_PATH).length).toBe(1);
    const state = () => page.evaluate(() => (window as unknown as { __hide: { hiddenAt: number; shownAt: number; clicks: number } }).__hide);
    expect((await state()).hiddenAt, "the hide is on").toBeGreaterThanOrEqual(0);
    expect((await state()).shownAt, "the hide is still on").toBe(-1);
    return async () => {
      release();
      await expect.poll(async () => (await state()).shownAt, { message: "the hide came off" }).toBeGreaterThan(0);
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      return state();
    };
  }

  test("while hidden, a click or Tab reaches none of the page's controls", async ({ page, visualPackage }) => {
    const show = await heldPage(page, visualPackage);
    const box = await page.locator("#b").boundingBox();
    expect(box, "the button is laid out while hidden").not.toBeNull();
    await page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.keyboard.press("Tab");
    const focused = await page.evaluate(() => (document.activeElement ? document.activeElement.tagName : null));
    const hide = await show();
    expect(hide.clicks, "button handler").toBe(0);
    expect(focused === "BODY" || focused === null, `Tab focused ${focused}`).toBe(true);
    await page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await expect.poll(() => page.evaluate(() => (window as unknown as { __hide: { clicks: number } }).__hide.clicks), "the same click once shown").toBe(1);
  });

  test("while hidden the page paints nothing: every paint, and the largest, comes after it shows", async ({ page, visualPackage }) => {
    // No input in this test: the browser stops reporting the largest paint after the first click or key.
    const show = await heldPage(page, visualPackage);
    const hide = await show();
    const paints = await page.evaluate(() => performance.getEntriesByType("paint").map((entry) => ({ name: entry.name, start: entry.startTime })));
    const largest = await page.evaluate(
      () =>
        new Promise<number | null>((resolve) => {
          new PerformanceObserver((list) => {
            const entries = list.getEntries();
            if (entries.length) resolve(entries[0].startTime);
          }).observe({ type: "largest-contentful-paint", buffered: true });
          setTimeout(() => resolve(null), 2000);
        }),
    );
    expect(hide.shownAt).toBeGreaterThan(0);
    expect(paints.map((paint) => paint.name).sort()).toEqual(["first-contentful-paint", "first-paint"]);
    for (const paint of paints) {
      expect(paint.start, `${paint.name} ${paint.start} < shown ${hide.shownAt}`).toBeGreaterThanOrEqual(hide.shownAt);
    }
    expect(largest, "largest-contentful-paint").not.toBeNull();
    expect(largest as number, `largest-contentful-paint ${largest} < shown ${hide.shownAt}`).toBeGreaterThanOrEqual(hide.shownAt);
  });
});
