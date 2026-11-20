import type { Page } from "@playwright/test";
import { routeSite, SITE } from "./editor";
import { fixtureFiles as exported, PIXEL } from "./fixtures";
import { expect, test, type VisualPackage } from "./runtime.fixture";
import { answering, experiment, reports } from "./visual";

/**
 * Hydration keeps the changes (#1545). Four sites a framework renders on the
 * server, each hydrated with its framework's default error handling: Next's
 * Pages Router and App Router (React 19.3), a React 18.3.1 page, and a React
 * 19.3 page whose targets sit in two sibling Suspense boundaries
 * (tests/e2e/runtime/fixtures). For every operation, 25 changes are applied
 * before hydration; 1 s after hydration all 25 are present, no frame sampled
 * after the page shows has an original value, and the page errors and
 * console errors are exactly those measured. Then the bound: a hydration
 * after it leaves the originals, and the page reports five of them (the cap).
 * Every page is a fresh navigation, so paint holding cannot hold its frames.
 */

const FIXTURES = ["pages-router", "app-router", "react18", "react19-suspense"] as const;
type Fixture = (typeof FIXTURES)[number];
const N = 25;
const START = new Date("2026-11-16T12:00:00Z");

const OPS = {
  set_text: (n: number) => ({ op: "set_text", selector: `#t-${n}`, text: `CHANGED server text ${n}` }),
  set_attribute: (n: number) => ({ op: "set_attribute", selector: `#t-${n}`, name: "title", value: "B" }),
  set_image: (n: number) => ({ op: "set_image", selector: `#i-${n}`, src: "/b.png", alt: "B" }),
  set_style: (n: number) => ({ op: "set_style", selector: `#t-${n}`, styles: { color: "rgb(255, 0, 0)" } }),
  hide: (n: number) => ({ op: "hide", selector: `#t-${n}` }),
} as const;
type Op = keyof typeof OPS;

/**
 * [page errors, console errors] per page view, as measured with Playwright's
 * Chromium (2026-11-16). React 19 reported one hydration error per boundary
 * it rendered again (the root, or each of the two Suspense boundaries);
 * React 18.3.1 one per mismatch it met (25 for 25 text changes, 50 for 25
 * image changes) and one more for the root. set_image removes the <source>
 * elements, so it counts as a text change does; attribute and style changes
 * cost nothing.
 */
const MEASURED: Record<Fixture, Record<Op, [number, number]>> = {
  "pages-router": { set_text: [1, 0], set_attribute: [0, 0], set_image: [1, 0], set_style: [0, 0], hide: [0, 0] },
  "app-router": { set_text: [1, 0], set_attribute: [0, 0], set_image: [1, 0], set_style: [0, 0], hide: [0, 0] },
  react18: { set_text: [26, 0], set_attribute: [0, 0], set_image: [51, 0], set_style: [0, 0], hide: [0, 0] },
  "react19-suspense": { set_text: [2, 0], set_attribute: [0, 0], set_image: [2, 0], set_style: [0, 0], hide: [0, 0] },
};

/**
 * Which targets do not show *op*'s change, by number. Runs in the page, as a
 * string, so the frame sampler below can use it too.
 */
const MISSING = `(op, n) => {
  const out = [];
  for (let i = 1; i <= n; i += 1) {
    const t = document.getElementById("t-" + i);
    const img = document.getElementById("i-" + i);
    let ok = false;
    if (op === "set_text") ok = !!t && t.textContent === "CHANGED server text " + i;
    else if (op === "set_attribute") ok = !!t && t.getAttribute("title") === "B";
    else if (op === "set_image") ok = !!img && /\\/b\\.png$/.test(img.getAttribute("src") || "") && img.parentElement.querySelectorAll("source").length === 0;
    else if (op === "set_style") ok = !!t && t.style.color === "rgb(255, 0, 0)";
    else if (op === "hide") ok = !!t && getComputedStyle(t).display === "none";
    if (!ok) out.push(i);
  }
  return out;
}`;

function missing(page: Page, op: Op): Promise<number[]> {
  return page.evaluate(([code, name, n]) => (0, eval)(code)(name, n), [MISSING, op, N] as const);
}

/** From the first frame after <html> shows: each frame that has a target without the change. */
function sampleFrames([code, op, n]: readonly [string, string, number]) {
  const check = (0, eval)(code) as (op: string, n: number) => number[];
  const state = { frames: 0, originals: [] as number[][] };
  (window as unknown as { __sampled: typeof state }).__sampled = state;
  const sample = () => {
    if (document.documentElement && getComputedStyle(document.documentElement).visibility !== "hidden" && document.getElementById("hydrated")) {
      state.frames += 1;
      const without = check(op, n);
      if (without.length > 0) state.originals.push(without);
    }
    if (state.frames < 300) requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
}

async function open(page: Page, visualPackage: VisualPackage, fixture: Fixture, op: Op, extraBody = "") {
  const files = exported(fixture);
  const index = files["/index.html"].body.toString("utf8").replace("</body>", `${extraBody}</body>`);
  const changes = Array.from({ length: N }, (_, index) => OPS[op](index + 1));
  const site = await routeSite(page, visualPackage, {
    pages: { "/": index },
    files,
    answer: answering({ experiments: [experiment("hydration", changes)] }),
  });
  // The scripts that hydrate the page wait for release(), so a spec can see
  // every change applied before React hydrates. Registered after routeSite's
  // route, so this one answers first.
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => (release = resolve));
  const hydrating = fixture === "react18" || fixture === "react19-suspense" ? `${SITE}/client.js` : `${SITE}/_next/**`;
  await page.route(hydrating, async (route) => {
    await released;
    const served = files[new URL(route.request().url()).pathname];
    if (served) await route.fulfill({ body: served.body, contentType: served.contentType });
    else await route.fulfill({ status: 404, body: "" });
  });
  return { site, release };
}

function countErrors(page: Page) {
  const counts = { page: [] as string[], console: [] as string[] };
  page.on("pageerror", (error) => counts.page.push(error.message.slice(0, 120)));
  page.on("console", (message) => {
    if (message.type() === "error") counts.console.push(message.text().slice(0, 120));
  });
  return counts;
}

for (const fixture of FIXTURES) {
  for (const op of Object.keys(OPS) as Op[]) {
    test(`${fixture}: ${op} on 25 targets is kept through hydration`, async ({ page, visualPackage }) => {
      const errors = countErrors(page);
      await page.addInitScript(sampleFrames, [MISSING, op, N] as const);
      const { site, release } = await open(page, visualPackage, fixture, op);
      await page.goto(`${SITE}/`, { waitUntil: "commit" });
      await expect.poll(() => missing(page, op), { message: "targets without the change before hydration" }).toEqual([]);
      expect(await page.locator("#hydrated").textContent(), "before the release").toBe("server");
      release();
      await expect(page.locator("#hydrated")).toHaveText("hydrated");
      await page.waitForTimeout(1000);
      expect(await missing(page, op), "targets without the change 1 s after hydration").toEqual([]);
      const sampled = await page.evaluate(() => (window as unknown as { __sampled: { frames: number; originals: number[][] } }).__sampled);
      expect(sampled.frames, "frames sampled after the page showed").toBeGreaterThan(0);
      expect(sampled.originals, "frames with an original value").toEqual([]);
      expect(reports(site), "reports").toEqual([]);
      const first = { page: errors.page.slice(0, 2), console: errors.console.slice(0, 2) };
      expect([errors.page.length, errors.console.length], `[page errors, console errors], the first: ${JSON.stringify(first)}`).toEqual(
        MEASURED[fixture][op],
      );
    });
  }
}

test.describe("the bound", () => {
  const hidden = (page: Page) =>
    page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
    });

  async function heldHydration(page: Page, visualPackage: VisualPackage, extraBody = "") {
    await page.addInitScript(() => {
      (window as unknown as { __holdHydration: boolean }).__holdHydration = true;
    });
    await page.clock.install({ time: START });
    const { site, release } = await open(page, visualPackage, "react19-suspense", "set_text", extraBody);
    release();
    return site;
  }

  const hydrate = async (page: Page) => {
    await page.evaluate(() => (window as unknown as { __hydrate(): void }).__hydrate());
    await expect(page.locator("#hydrated")).toHaveText("hydrated");
  };

  test("hydration at load + 4 s keeps every change", async ({ page, visualPackage }) => {
    const site = await heldHydration(page, visualPackage);
    await page.goto(`${SITE}/`, { waitUntil: "load" });
    await expect(page.locator("#t-1")).toHaveText("CHANGED server text 1");
    await page.clock.runFor(4000);
    await hydrate(page);
    expect(await missing(page, "set_text"), "targets without the change").toEqual([]);
    await hidden(page);
    expect(reports(site), "reports").toEqual([]);
  });

  test("hydration at load + 6 s shows the originals, and the page reports five of them", async ({ page, visualPackage }) => {
    const site = await heldHydration(page, visualPackage);
    await page.goto(`${SITE}/`, { waitUntil: "load" });
    await expect(page.locator("#t-1")).toHaveText("CHANGED server text 1");
    await page.clock.runFor(6000);
    await hydrate(page);
    expect(await missing(page, "set_text"), "targets without the change").toHaveLength(N);
    await hidden(page);
    await expect.poll(() => reports(site).length, { message: "replaced_after_bound reports" }).toBe(5);
    expect(new Set(reports(site).map((report) => report.metadata.reason))).toEqual(new Set(["replaced_after_bound"]));
  });

  test("load held to 8 s by a slow image, hydration at 11 s: the same", async ({ page, visualPackage }) => {
    const site = await heldHydration(page, visualPackage, '<img src="/slow.png" alt="">');
    let release: () => void = () => undefined;
    const slow = new Promise<void>((resolve) => (release = resolve));
    await page.route(`${SITE}/slow.png`, async (route) => {
      await slow;
      await route.fulfill({ body: PIXEL, contentType: "image/png" });
    });
    await page.goto(`${SITE}/`, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#t-1")).toHaveText("CHANGED server text 1");
    await page.clock.runFor(8000);
    release();
    await page.waitForLoadState("load");
    await page.clock.runFor(3000);
    await hydrate(page);
    expect(await missing(page, "set_text"), "targets without the change").toHaveLength(N);
    await hidden(page);
    await expect.poll(() => reports(site).length, { message: "cap arm: reports" }).toBe(5);
  });
});
