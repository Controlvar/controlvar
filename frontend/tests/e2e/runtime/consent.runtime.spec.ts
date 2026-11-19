import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { BrowserContext, Page } from "@playwright/test";
import { expect, test } from "./runtime.fixture";
import { EDITOR, newTicket, routeSite, SITE, storageKeys, type Site } from "./editor";
import { answering, calls, experiment, previewAnswer, sitePage, VISUAL_PATH } from "./visual";

/**
 * consent() and revokeConsent() on a real page (#1545). A tag with
 * data-require-consent reads, stores and sends nothing before consent();
 * after it, the keys the script keeps are exactly the ones the closed key
 * list (tests/sdk-contract/visual-storage-keys.json) marks written.
 * revokeConsent() deletes the cookie and every controlvar_ key, sends nothing
 * more from the page, and the next page follows the tag's consent mode.
 */

interface StorageKey {
  name: string;
  storage: "cookie" | "localStorage" | "sessionStorage";
  written: boolean;
}

const CONTRACT = join(__dirname, "..", "..", "..", "..", "tests", "sdk-contract", "visual-storage-keys.json");
const KEYS: StorageKey[] = JSON.parse(readFileSync(CONTRACT, "utf8")).keys;
const AREA = { cookie: "cookie", localStorage: "local", sessionStorage: "session" } as const;
const WRITTEN = KEYS.filter((key) => key.written).map((key) => `${AREA[key.storage]}:${key.name}`).sort();

const BODY = `<h1 id="t">Original</h1><a id="next" href="/a/b/other">Next</a>`;
const HEADLINE = [{ op: "set_text", selector: "#t", text: "Changed" }];
const TOKEN = "p".repeat(40) + "abc";

type Visual = { consent(): unknown; revokeConsent(): unknown; track(event: string, options?: unknown): unknown };

function visual(page: Page, call: "consent" | "revokeConsent" | "track", ...args: unknown[]) {
  return page.evaluate(
    ([name, rest]) => {
      const api = (window as unknown as { controlvar: { visual: Visual } }).controlvar.visual;
      return (api[name as "track"] as (...a: unknown[]) => unknown)(...(rest as unknown[])) ?? null;
    },
    [call, args] as const,
  );
}

/** Every Controlvar cookie and storage key, as "cookie:name", "local:name" or "session:name". */
async function controlvarKeys(page: Page, context: BrowserContext, cookieName = "controlvar_uid"): Promise<string[]> {
  const cookies = (await context.cookies(SITE))
    .filter((cookie) => cookie.name === cookieName || cookie.name.startsWith("controlvar_"))
    .map((cookie) => `cookie:${cookie.name}`);
  return [...cookies, ...(await storageKeys(page))].sort();
}

async function clearSite(page: Page, context: BrowserContext): Promise<void> {
  await page.evaluate(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
  await context.clearCookies();
}

test.describe("consent()", () => {
  test("before it, nothing is read, stored or sent; after it, the keys are exactly the ones the script writes", async ({
    page,
    context,
    visualPackage,
  }) => {
    let refuse = false;
    const ok = answering({ experiments: [experiment("headline", HEADLINE)], preview: previewAnswer("headline", HEADLINE) });
    const site = await routeSite(page, visualPackage, {
      pages: {
        "/a/b/page": sitePage(BODY, { "data-require-consent": true }),
        "/a/b/editor": sitePage(BODY, { "data-require-consent": true, "data-editor-origin": EDITOR }),
      },
      answer: (call) => (refuse && call.path === VISUAL_PATH ? { status: 500 } : ok(call)),
    });
    const seen = new Set<string>();

    // An ordinary page view.
    await page.goto(`${SITE}/a/b/page`, { waitUntil: "load" });
    await visual(page, "track", "buy");
    expect(site.api, "requests before consent").toEqual([]);
    expect(await controlvarKeys(page, context), "storage before consent").toEqual([]);
    await visual(page, "consent");
    await expect(page.locator("#t")).toHaveText("Changed");
    const ordinary = await controlvarKeys(page, context);
    expect(ordinary, "after consent").toEqual(["cookie:controlvar_uid", "local:controlvar_assigned", "local:controlvar_pages"]);
    ordinary.forEach((key) => seen.add(key));

    // A refused call: the silence.
    await clearSite(page, context);
    refuse = true;
    await page.goto(`${SITE}/a/b/page?refused`, { waitUntil: "load" });
    expect(await controlvarKeys(page, context), "refused: storage before consent").toEqual([]);
    await visual(page, "consent");
    await expect.poll(() => storageKeys(page), { message: "refused: after consent" }).toContain("session:controlvar_backoff");
    (await controlvarKeys(page, context)).forEach((key) => seen.add(key));
    refuse = false;

    // A preview link.
    await clearSite(page, context);
    await page.goto(`${SITE}/a/b/page?preview#controlvar-preview=${TOKEN}`, { waitUntil: "load" });
    expect(await controlvarKeys(page, context), "preview: storage before consent").toEqual([]);
    await visual(page, "consent");
    await expect(page.locator("#t")).toHaveText("Changed");
    expect(await controlvarKeys(page, context), "preview: after consent").toEqual(["session:controlvar_preview"]);
    (await controlvarKeys(page, context)).forEach((key) => seen.add(key));

    // An editor link: its one key, before consent too (the exception).
    await clearSite(page, context);
    await page.goto(`${SITE}/a/b/editor#controlvar-editor=${newTicket()}`, { waitUntil: "load" });
    expect(await controlvarKeys(page, context), "editor link: before consent").toEqual(["session:controlvar_editor_open"]);
    (await controlvarKeys(page, context)).forEach((key) => seen.add(key));

    expect(Array.from(seen).sort(), "every key seen").toEqual(WRITTEN);
  });
});

/** A page at /a/b/page with every key of the closed list already set, and the site's own keys beside them. */
async function seeded(page: Page, context: BrowserContext): Promise<void> {
  await context.addCookies([
    { name: "controlvar_uid", value: "old", url: SITE },
    { name: "site_session", value: "kept", url: SITE },
  ]);
  await page.addInitScript(
    ([origin, keys]) => {
      if (location.origin !== origin || sessionStorage.getItem("seeded") !== null) return;
      sessionStorage.setItem("seeded", "1");
      for (const key of keys as { name: string; storage: string }[]) {
        if (key.storage === "localStorage") localStorage.setItem(key.name, "1");
        if (key.storage === "sessionStorage") sessionStorage.setItem(key.name, "1");
      }
      localStorage.setItem("site_cart", "kept");
    },
    [SITE, KEYS] as const,
  );
}

const SITE_KEYS = { cookies: ["site_session"], storage: ["local:site_cart", "session:seeded"] };

async function siteKeys(page: Page, context: BrowserContext) {
  const cookies = (await context.cookies(SITE)).map((cookie) => cookie.name).sort();
  const storage = await page.evaluate(() => {
    const keys: string[] = [];
    for (const [area, storage] of [
      ["local", localStorage],
      ["session", sessionStorage],
    ] as const) {
      for (let i = 0; i < storage.length; i += 1) keys.push(`${area}:${storage.key(i)}`);
    }
    return keys.sort();
  });
  return { cookies, storage };
}

for (const mode of ["waits for consent", "does not wait"] as const) {
  const waits = mode === "waits for consent";
  test(`revokeConsent() on a tag that ${mode}: the cookie and every key go, nothing more is sent, and the next page follows the tag`, async ({
    page,
    context,
    visualPackage,
  }) => {
    await page.clock.install({ time: new Date("2026-11-19T12:00:00Z") });
    await seeded(page, context);
    const attributes: Record<string, string | true> = { "data-cookie-name": "shop_visitor" };
    if (waits) attributes["data-require-consent"] = true;
    const site: Site = await routeSite(page, visualPackage, {
      pages: { "/a/b/page": sitePage(BODY, attributes), "/a/b/other": sitePage(BODY, attributes) },
      answer: answering({ experiments: [experiment("headline", HEADLINE)] }),
    });
    await page.goto(`${SITE}/a/b/page`, { waitUntil: "load" });
    if (waits) await visual(page, "consent");
    await expect(page.locator("#t")).toHaveText("Changed");
    await visual(page, "track", "buy");
    await expect.poll(() => site.api.length, { message: "the call and the event" }).toBe(2);
    expect((await context.cookies(SITE)).map((cookie) => cookie.name)).toContain("shop_visitor");

    expect(await visual(page, "revokeConsent"), "revokeConsent() returns nothing").toBeNull();
    expect(await visual(page, "revokeConsent"), "a second call").toBeNull();
    const after = await siteKeys(page, context);
    expect(after.cookies.includes("shop_visitor") ? "shop_visitor still set" : "deleted").toBe("deleted");
    expect(after.cookies.includes("controlvar_uid") ? "controlvar_uid still set" : "deleted").toBe("deleted");
    expect(after.cookies).toEqual(SITE_KEYS.cookies);
    expect(after.storage, "storage after revoke").toEqual(SITE_KEYS.storage);
    await expect(page.locator("#t"), "a change already shown stays").toHaveText("Changed");

    const before = site.api.length;
    await visual(page, "track", "buy");
    await page.evaluate(() => history.pushState(null, "", "/a/b/elsewhere"));
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide")));
    await page.clock.runFor(60_000);
    expect(site.api.length - before, "requests after revoke").toBe(0);
    expect(await siteKeys(page, context), "storage after the page went on").toEqual(after);

    await page.goto(`${SITE}/a/b/other`, { waitUntil: "load" });
    await page.clock.runFor(2_000);
    expect(calls(site, VISUAL_PATH).length - 1, "calls on the next page").toBe(waits ? 0 : 1);
  });
}
