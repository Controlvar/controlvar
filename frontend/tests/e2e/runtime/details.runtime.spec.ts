import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { BrowserContext, Page } from "@playwright/test";
import { routeSite, SITE, type Site } from "./editor";
import { expect, test } from "./runtime.fixture";
import { answering, calls, sitePage, VISUAL_PATH } from "./visual";

/**
 * The website details the packed script sends with the call for a page's
 * changes (#1545), in the browser: the referring site, the campaign values and the landing
 * page, what the visit keeps in localStorage, and data-collect. The API is
 * stubbed (routeSite). The device type on a phone and a tablet is in
 * phone.runtime.spec.ts and tablet.runtime.spec.ts.
 */

const VECTORS = JSON.parse(
  readFileSync(join(__dirname, "..", "..", "..", "..", "tests", "sdk-contract", "visual-visitor-vectors.json"), "utf8"),
);
const BODY = '<h1 id="t">Original</h1>';
const SEARCH_REFERRER = "https://www.google.com/search?q=cv-secret-7731";
const WITH_LANDING = "device,browser,language,visitor,referrer,utm,landing";

/** Init script: document.referrer is `url`, as after a click on a search result. */
function referredBy(url: string) {
  Object.defineProperty(Document.prototype, "referrer", { configurable: true, get: () => url });
}

/** The context of the last call for the page's changes. */
function lastContext(site: Site): Record<string, unknown> {
  const asks = calls(site, VISUAL_PATH);
  const body = asks[asks.length - 1].body as { context?: Record<string, unknown> };
  return body.context ?? {};
}

async function asked(site: Site, count = 1): Promise<void> {
  await expect.poll(() => calls(site, VISUAL_PATH).length, { message: "the call for the page" }).toBe(count);
}

async function onePage(page: Page, visualPackage: Parameters<typeof routeSite>[1], attributes: Record<string, string> = {}) {
  return routeSite(page, visualPackage, {
    pages: { "/a": sitePage(BODY, attributes), "/a/b/page": sitePage(BODY, attributes), "/a/b/second": sitePage(BODY, attributes), "/utm": sitePage(BODY, attributes) },
    answer: answering({ experiments: [] }),
  });
}

/** Every cookie and every localStorage and sessionStorage entry of the site, as "name=value". */
async function everythingStored(page: Page, context: BrowserContext): Promise<string[]> {
  const cookies = (await context.cookies(SITE)).map((cookie) => `cookie ${cookie.name}=${cookie.value}`);
  const storage = await page.evaluate(() => {
    const out: string[] = [];
    for (const [area, store] of [["local", localStorage], ["session", sessionStorage]] as const) {
      for (let i = 0; i < store.length; i += 1) {
        const key = store.key(i)!;
        out.push(`${area} ${key}=${store.getItem(key)}`);
      }
    }
    return out;
  });
  return [...cookies, ...storage];
}

test.describe("the website details of a page view", () => {
  test("the referring site's host, the landing path and the campaign; never the referrer's query, gclid or email", async ({
    page,
    visualPackage,
  }) => {
    await page.addInitScript(referredBy, SEARCH_REFERRER);
    const site = await onePage(page, visualPackage, { "data-collect": WITH_LANDING });
    await page.goto(`${SITE}/a/b/page?email=a%40b.c&gclid=XYZ&utm_source=news`);
    expect(await page.evaluate(() => document.referrer), "the page's referrer").toContain("cv-secret-7731");
    await asked(site);
    const sent = JSON.stringify(site.api.map((call) => call.body));
    const found = ["cv-secret-7731", "gclid", "email"].filter((canary) => sent.includes(canary)).map((canary) => `found '${canary}'`);
    expect(found).toEqual([]);
    expect(lastContext(site)).toMatchObject({
      web_referrer: "www.google.com",
      web_landing_path: "/a/b/page",
      web_utm_source: "news",
    });
  });

  test("each campaign query of the server's vectors gives the browser's own reading of it, kept as the server keeps it", async ({
    page,
    visualPackage,
  }) => {
    const queries = VECTORS.utm_queries as { id: string; query: string; param: string; value: string; kept: boolean }[];
    expect(queries).toHaveLength(10);
    const site = await onePage(page, visualPackage);
    const wrong: string[] = [];
    for (let index = 0; index < queries.length; index += 1) {
      const query = queries[index];
      await page.goto(`${SITE}/utm?${query.query}`);
      await asked(site, index + 1);
      const context = lastContext(site);
      const campaign = Object.fromEntries(Object.entries(context).filter(([key]) => key.startsWith("web_utm_")));
      const expected = query.kept ? { [`web_${query.param}`]: query.value } : {};
      if (JSON.stringify(campaign) !== JSON.stringify(expected)) {
        wrong.push(`${query.id}: ${JSON.stringify(campaign)} != ${JSON.stringify(expected)}`);
      }
      // The next query starts a visit of its own.
      await page.evaluate(() => localStorage.removeItem("controlvar_visit"));
    }
    expect(wrong).toEqual([]);
  });

  test("the visit keeps its start, its last page view and its cleaned campaign and landing page, and nothing from the address", async ({
    page,
    context,
    visualPackage,
  }) => {
    await page.addInitScript(referredBy, SEARCH_REFERRER);
    const site = await onePage(page, visualPackage, { "data-collect": WITH_LANDING });
    await page.goto(`${SITE}/a/b/page?email=a%40b.c&gclid=XYZ&utm_source=news&utm_campaign=a@b.c`);
    await asked(site);
    const stored = await everythingStored(page, context);
    const found = stored.flatMap((entry) =>
      ["cv-secret-7731", "gclid", "XYZ", "email", "@", "%40", "google"]
        .filter((canary) => entry.includes(canary))
        .map((canary) => `found '${canary}' in ${entry.split("=")[0].split(" ")[1]}`),
    );
    expect(found).toEqual([]);
    const visit = JSON.parse((await page.evaluate(() => localStorage.getItem("controlvar_visit")))!);
    expect(Object.keys(visit).sort()).toEqual(["landing", "last_seen", "started", "utm"]);
    expect(visit).toMatchObject({ utm: { utm_source: "news" }, landing: "/a/b/page" });

    // A later page of the visit, and another tab: the first page's campaign, never their own.
    await page.goto(`${SITE}/a/b/second?utm_source=later&utm_campaign=later`);
    await asked(site, 2);
    expect(lastContext(site)).toMatchObject({ web_utm_source: "news", web_landing_path: "/a/b/page" });
    expect(lastContext(site)).not.toHaveProperty("web_utm_campaign");
    const tab = await context.newPage();
    const tabSite = await onePage(tab, visualPackage, { "data-collect": WITH_LANDING });
    await tab.goto(`${SITE}/a?utm_source=tab`);
    await asked(tabSite);
    expect(lastContext(tabSite)).toMatchObject({ web_utm_source: "news", web_landing_path: "/a/b/page" });
    const visitAfter = JSON.parse((await tab.evaluate(() => localStorage.getItem("controlvar_visit")))!);
    expect(visitAfter.started, "one visit across both tabs").toBe(visit.started);
  });
});

test.describe("data-collect", () => {
  test("none sends no website detail and stores no visit", async ({ page, visualPackage }) => {
    await page.addInitScript(referredBy, SEARCH_REFERRER);
    const site = await onePage(page, visualPackage, { "data-collect": "none" });
    await page.goto(`${SITE}/a/b/page?utm_source=news`);
    await asked(site);
    const body = calls(site, VISUAL_PATH)[0].body as Record<string, unknown>;
    expect(JSON.stringify(body)).not.toContain("web_");
    const keys = await page.evaluate(() => [localStorage.getItem("controlvar_visit"), localStorage.getItem("controlvar_seen")]);
    expect(keys).toEqual([null, null]);
  });

  test("a value it does not know gets one console warning, and the values it knows still apply", async ({ page, visualPackage }) => {
    const warnings: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "warning" && message.text().startsWith("Controlvar: data-collect")) warnings.push(message.text());
    });
    const site = await onePage(page, visualPackage, { "data-collect": "device,colour,Browser" });
    await page.goto(`${SITE}/a`);
    await asked(site);
    expect(warnings).toHaveLength(1);
    expect(Object.keys(lastContext(site))).toEqual(["web_device"]);
  });
});
