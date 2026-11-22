import type { BrowserContext, Page } from "@playwright/test";
import { expect, test } from "./runtime.fixture";
import { routeSite, SITE, storageKeys } from "./editor";
import {
  answering,
  BATCH_PATH,
  calls,
  experiment,
  PREVIEW_PATH,
  previewAnswer,
  reports,
  shown,
  sitePage,
  VISUAL_PATH,
} from "./visual";

/**
 * A preview link on a real page (#1545): the page shows one variant, the
 * address keeps no token, every other experiment is off, nothing is recorded,
 * and the script's banner, in a closed shadow root beside <body>, stays
 * usable whatever the variant changes. Every page is a fresh navigation.
 */

const TOKEN = "p".repeat(40) + "abc";
const BODY = `<h1 id="t">Original</h1><p id="other">Other</p>`;
const HEADLINE = [{ op: "set_text", selector: "#t", text: "Changed" }];

/** Keeps every shadow root the page makes, closed ones included, on window.__roots. */
function keepShadowRoots() {
  const native = Element.prototype.attachShadow;
  const roots: ShadowRoot[] = [];
  (window as unknown as { __roots: ShadowRoot[] }).__roots = roots;
  Element.prototype.attachShadow = function (this: Element, init: ShadowRootInit) {
    const root = native.call(this, init);
    roots.push(root);
    return root;
  };
}

interface ExitButton {
  /** The button's centre, in the page's viewport. */
  x: number;
  y: number;
  visible: boolean;
  inViewport: boolean;
  /** document.elementFromPoint at the centre is the banner's host, and the root's own elementFromPoint is the button. */
  onTop: boolean;
  text: string;
}

async function exitButton(page: Page): Promise<ExitButton | null> {
  return page.evaluate(() => {
    const root = (window as unknown as { __roots: ShadowRoot[] }).__roots[0];
    const host = document.querySelector("controlvar-ui");
    const button = root ? root.querySelector("button") : null;
    if (!root || !host || !button) return null;
    const rect = button.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    return {
      x,
      y,
      visible: button.checkVisibility({ opacityProperty: true, visibilityProperty: true }),
      inViewport: rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.left >= 0 && rect.bottom <= innerHeight && rect.right <= innerWidth,
      onTop: document.elementFromPoint(x, y) === host && root.elementFromPoint(x, y) === button,
      text: button.textContent ?? "",
    };
  });
}

async function bannerText(page: Page): Promise<string> {
  return page.evaluate(() => (window as unknown as { __roots: ShadowRoot[] }).__roots[0]?.textContent ?? "");
}

async function controlvarCookies(context: BrowserContext): Promise<string[]> {
  return (await context.cookies(SITE)).filter((cookie) => cookie.name.startsWith("controlvar_")).map((cookie) => cookie.name);
}

test.describe("a preview link", () => {
  test("shows its variant with the banner, other experiments off, the address clean and nothing recorded", async ({
    page,
    context,
    visualPackage,
  }) => {
    await page.addInitScript(keepShadowRoots);
    // This visitor was assigned to an experiment on an earlier page: track() could name it.
    const assigned = JSON.stringify({ other: Date.parse("2026-11-18T12:00:00Z") });
    await page.addInitScript((value) => localStorage.setItem("controlvar_assigned", value), assigned);
    let release: () => void = () => undefined;
    const holdPreview = new Promise<void>((resolve) => (release = resolve));
    const site = await routeSite(page, visualPackage, {
      pages: { "/a/b": sitePage(BODY, { "data-timeout-ms": "4000" }), "/a/c": sitePage(BODY) },
      answer: answering({
        experiments: [experiment("other", [{ op: "set_text", selector: "#other", text: "Other changed" }])],
        preview: previewAnswer("headline", HEADLINE),
        holdPreview,
      }),
    });
    await page.goto(`${SITE}/a/b?q=1#controlvar-preview=${TOKEN}`, { waitUntil: "domcontentloaded" });
    await expect.poll(() => calls(site, PREVIEW_PATH).length, { message: "preview reads" }).toBe(1);
    // While the page waits for the variant it is hidden, and the banner shows.
    expect(await shown(page), "the page while it waits").toBe(false);
    const host = page.locator("controlvar-ui");
    expect(await host.evaluate((element) => getComputedStyle(element).visibility), "the banner while the page is hidden").toBe("visible");
    release();
    await expect(page.locator("#t")).toHaveText("Changed");
    await expect.poll(() => shown(page)).toBe(true);
    expect(page.url()).toBe(`${SITE}/a/b?q=1`);
    expect(await bannerText(page)).toContain("Variant B, as visitors in it see this page. Nothing is recorded.");
    expect(await host.evaluate((element) => element.parentNode === document.documentElement && element.nextElementSibling === document.body)).toBe(true);

    await page.evaluate(() => (window as unknown as { controlvar: { visual: { track(e: string): void } } }).controlvar.visual.track("buy"));
    await page.goto(`${SITE}/a/c`, { waitUntil: "load" });
    await expect(page.locator("#t"), "a later page in the tab").toHaveText("Changed");

    const [first] = calls(site, PREVIEW_PATH);
    expect(first.body).toEqual({ token: TOKEN, host: "localhost:4100", path: "/a/b" });
    expect(calls(site, PREVIEW_PATH).length, "preview reads").toBe(2);
    expect(calls(site, VISUAL_PATH).length, "calls for other experiments").toBe(0);
    expect(calls(site, BATCH_PATH).length, "events").toBe(0);
    await expect(page.locator("#other")).toHaveText("Other");
    expect(await controlvarCookies(context), "cookies").toEqual([]);
    expect(await storageKeys(page), "storage keys").toEqual(["local:controlvar_assigned", "session:controlvar_preview"]);
    expect(await page.evaluate(() => localStorage.getItem("controlvar_assigned")), "assignments kept").toBe(assigned);
  });

  test("[Exit preview] puts the page back, takes the banner away and ends the preview for the tab", async ({ page, visualPackage }) => {
    await page.addInitScript(keepShadowRoots);
    const site = await routeSite(page, visualPackage, {
      pages: { "/a/b": sitePage(BODY) },
      answer: answering({ experiments: [], preview: previewAnswer("headline", HEADLINE) }),
    });
    await page.goto(`${SITE}/a/b#controlvar-preview=${TOKEN}`, { waitUntil: "load" });
    await expect(page.locator("#t")).toHaveText("Changed");
    const button = await exitButton(page);
    expect(button, "the banner's button").not.toBeNull();
    expect(button!.text).toBe("Exit preview");
    await page.mouse.click(button!.x, button!.y);
    await expect(page.locator("#t")).toHaveText("Original");
    await expect(page.locator("controlvar-ui")).toHaveCount(0);
    expect(await storageKeys(page)).toEqual([]);
    await page.goto(`${SITE}/a/b`, { waitUntil: "load" });
    await expect.poll(() => calls(site, VISUAL_PATH).length, { message: "the next page is an ordinary page view" }).toBe(1);
    expect(calls(site, PREVIEW_PATH).length).toBe(1);
  });

  test("a link that no longer works says so on the banner, changes nothing and is forgotten", async ({ page, visualPackage }) => {
    await page.addInitScript(keepShadowRoots);
    await routeSite(page, visualPackage, { pages: { "/a/b": sitePage(BODY) }, answer: answering({ experiments: [] }) });
    await page.goto(`${SITE}/a/b#controlvar-preview=${TOKEN}`, { waitUntil: "load" });
    await expect.poll(() => bannerText(page)).toContain("This preview link has expired, was revoked, or does not exist.");
    await expect(page.locator("#t")).toHaveText("Original");
    expect(await storageKeys(page)).toEqual([]);
  });

  test("the banner comes back when the page removes it", async ({ page, visualPackage }) => {
    await page.addInitScript(keepShadowRoots);
    await routeSite(page, visualPackage, {
      pages: { "/a/b": sitePage(BODY) },
      answer: answering({ experiments: [], preview: previewAnswer("headline", HEADLINE) }),
    });
    await page.goto(`${SITE}/a/b#controlvar-preview=${TOKEN}`, { waitUntil: "load" });
    await page.evaluate(() => document.querySelector("controlvar-ui")!.remove());
    await expect(page.locator("controlvar-ui")).toHaveCount(1);
    const button = await exitButton(page);
    expect(button && button.visible && button.onTop, "the banner's button, back").toBe(true);
  });
});

/** Every style the variant could use to hide something, as the changes S1 accepts. */
const HIDING: { name: string; change: (selector: string) => Record<string, unknown> }[] = [
  { name: "hide", change: (selector) => ({ op: "hide", selector }) },
  { name: "display:none", change: (selector) => ({ op: "set_style", selector, styles: { display: "none" } }) },
  { name: "opacity:0", change: (selector) => ({ op: "set_style", selector, styles: { opacity: "0" } }) },
  { name: "visibility:hidden", change: (selector) => ({ op: "set_style", selector, styles: { visibility: "hidden" } }) },
  { name: "color:transparent", change: (selector) => ({ op: "set_style", selector, styles: { color: "transparent" } }) },
  { name: "font-size:0", change: (selector) => ({ op: "set_style", selector, styles: { "font-size": "0" } }) },
  { name: "height:0", change: (selector) => ({ op: "set_style", selector, styles: { height: "0" } }) },
];

/** The banner's host by its name, a selector that matches only it another way, the body, and <html>. */
const TARGETS = ["controlvar-ui", ":root > :not(head):not(body)", "body", "html"];

test.describe("no change reaches the banner", () => {
  for (const target of TARGETS) {
    test(`a variant that hides ${target} in seven ways leaves [Exit preview] visible, on top and in view`, async ({ page, visualPackage }) => {
      await page.addInitScript(keepShadowRoots);
      for (const hiding of HIDING) {
        const label = `${target} ${hiding.name}`;
        const site = await routeSite(page, visualPackage, {
          pages: { "/a/b": sitePage(BODY) },
          answer: answering({ experiments: [], preview: previewAnswer("headline", [hiding.change(target)]) }),
        });
        await page.goto(`${SITE}/a/b?${encodeURIComponent(label)}#controlvar-preview=${TOKEN}`, { waitUntil: "load" });
        await expect.poll(() => shown(page), { message: `${label}: shown` }).toBe(true);
        await expect.poll(() => bannerText(page), { message: `${label}: the answer` }).toContain("Variant B");
        const button = await exitButton(page);
        expect(button, `${label}: the banner's button`).not.toBeNull();
        const outcome = button!.visible && button!.inViewport && button!.onTop ? "banner usable" : "banner hidden";
        expect(`${label}: ${outcome}`).toBe(`${label}: banner usable`);
        const own = () => reports(site).filter((report) => report.error_type === "own_ui").length;
        await expect.poll(own, { message: `${label}: own_ui reports` }).toBe(target === "body" ? 0 : 1);
        expect(reports(site).length, `${label}: reports`).toBe(own());
        await page.unrouteAll({ behavior: "wait" });
      }
    });
  }
});
