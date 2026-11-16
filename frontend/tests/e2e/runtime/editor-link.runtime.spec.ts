import type { Page } from "@playwright/test";
import {
  BASE64URL_43,
  CLAIM_PATH,
  claimSettled,
  claimsIn,
  EDITOR,
  flushToFrame,
  FRAME_SELECTOR,
  frameAt,
  html,
  newTicket,
  OTHER_EDITOR_PAGE,
  PANEL_URL,
  routeSite,
  sayHello,
  SITE,
  snippet,
  storageKeys,
} from "./editor";
import { expect, test } from "./runtime.fixture";

/**
 * The visual editor's link (#1545): a page opened with
 * `#controlvar-editor=<43 base64url characters>`. The script removes the
 * fragment from the address, sends one claim for it, opens the editor's panel
 * in a frame, and hands the claim to the panel once the claim request has
 * settled and the panel has said hello. The claim route and the panel are
 * stubs here.
 */

const PATH = "/a/b?x=1";

async function open(page: Page, url: string): Promise<void> {
  await page.goto(url, { waitUntil: "load" });
}

test.describe("an exact editor link", () => {
  test("is removed from the address, claimed once, and handed to the panel after hello", async ({
    page,
    visualPackage,
  }) => {
    const ticket = newTicket();
    const site = await routeSite(page, visualPackage, {
      pages: { "/a/b": html(`<meta name="referrer" content="unsafe-url">${snippet()}`) },
    });
    await open(page, `${SITE}${PATH}#controlvar-editor=${ticket}`);

    expect(page.url(), "address after the first run").toBe(`${SITE}${PATH}`);
    expect(site.api.map((call) => call.path), "API requests").toEqual([CLAIM_PATH]);
    const body = site.api[0].body as Record<string, string>;
    expect(Object.keys(body).sort(), "claim body keys").toEqual(["claim", "ticket"]);
    expect(body.ticket).toBe(ticket);
    expect(body.claim).toMatch(BASE64URL_43);
    expect(body.claim).not.toBe(ticket);
    expect(site.api[0].headers.referer, "claim request referer").toBeUndefined();

    const frame = await frameAt(page, EDITOR);
    const attributes = await page.evaluate((selector) => {
      const element = document.querySelector(selector) as HTMLIFrameElement;
      return { policy: element.referrerPolicy, src: element.getAttribute("src") };
    }, FRAME_SELECTOR);
    expect(attributes.policy, "the panel frame's referrerPolicy").toBe("no-referrer");
    expect(attributes.src).toBe(`${EDITOR}/visual-editor/panel`);
    expect(site.panelRequests.map((headers) => headers.referer ?? null), "panel request referer").toEqual([null]);

    await sayHello(frame);
    await expect.poll(async () => (await claimsIn(frame)).length, { message: "claims handed over" }).toBe(1);
    const [handed] = await claimsIn(frame);
    expect(handed.origin).toBe(SITE);
    expect(handed.data).toEqual({ cv: 1, type: "claim", claim: body.claim });

    await sayHello(frame);
    await flushToFrame(page, frame);
    expect((await claimsIn(frame)).length, "claims after a second hello").toBe(1);
    expect(await storageKeys(page)).toEqual(["session:controlvar_editor_open"]);
  });

  test("waits for the claim request to settle when hello comes first", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, { pages: { "/a/b": html(snippet()) }, holdClaim: true });
    await open(page, `${SITE}${PATH}#controlvar-editor=${newTicket()}`);
    const frame = await frameAt(page, EDITOR);

    await sayHello(frame);
    await flushToFrame(page, frame);
    expect((await claimsIn(frame)).length, "claims before the claim request settled").toBe(0);

    site.releaseClaim();
    await expect.poll(async () => (await claimsIn(frame)).length, { message: "claims after it settled" }).toBe(1);
  });

  test("waits for hello when the claim request settles first", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, { pages: { "/a/b": html(snippet()) }, holdClaim: true });
    await open(page, `${SITE}${PATH}#controlvar-editor=${newTicket()}`);
    const frame = await frameAt(page, EDITOR);

    // Settled once the panel has loaded, so a claim posted now would reach it.
    site.releaseClaim();
    await claimSettled(page);
    await flushToFrame(page, frame);
    expect((await claimsIn(frame)).length, "claims before hello").toBe(0);

    await sayHello(frame);
    await expect.poll(async () => (await claimsIn(frame)).length, { message: "claims after hello" }).toBe(1);
  });

  test("hands over only to a hello from the panel frame at the editor origin", async ({ page, visualPackage }) => {
    await routeSite(page, visualPackage, { pages: { "/a/b": html(snippet()) } });
    await open(page, `${SITE}${PATH}#controlvar-editor=${newTicket()}`);
    const frame = await frameAt(page, PANEL_URL);
    await claimSettled(page);

    // The right message from the page itself: the wrong source and origin.
    await page.evaluate(() => window.postMessage({ cv: 1, type: "hello" }, "*"));
    // The right message and origin from another frame on the editor's origin.
    await page.evaluate((src) => {
      const other = document.createElement("iframe");
      other.src = src;
      document.body.appendChild(other);
    }, OTHER_EDITOR_PAGE);
    const other = await frameAt(page, OTHER_EDITOR_PAGE);
    await sayHello(other);
    // The right source, the wrong message.
    await frame.evaluate(() => parent.postMessage({ cv: 2, type: "hello" }, "*"));
    await frame.evaluate(() => parent.postMessage({ cv: 1, type: "ready" }, "*"));
    await flushToFrame(page, frame);
    expect((await claimsIn(other)).length, "claims in the other frame").toBe(0);
    expect((await claimsIn(frame)).length, "claims after hellos that do not count").toBe(0);

    await sayHello(frame);
    await expect.poll(async () => (await claimsIn(frame)).length, { message: "claims after the panel's hello" }).toBe(1);
  });

  test("says so on the page when the panel does not answer within 5 seconds", async ({ page, visualPackage }) => {
    // The page's clock stands still from the start; only runFor moves it.
    await page.clock.install({ time: new Date("2026-01-01T00:00:00Z") });
    await page.clock.pauseAt(new Date("2026-01-01T00:00:01Z"));
    await routeSite(page, visualPackage, { pages: { "/a/b": html(snippet()) } });
    await open(page, `${SITE}${PATH}#controlvar-editor=${newTicket()}`);
    await frameAt(page, EDITOR);
    const message = page.getByRole("alert");

    await page.clock.runFor(4999);
    await expect(message).toHaveCount(0);
    await page.clock.runFor(1);
    await expect(message).toHaveText(/The visual editor did not open: its panel did not answer within 5 seconds\./);
  });
});

test.describe("an editor link and consent", () => {
  test("before consent, the claim is the only request and the editor's key the only key", async ({
    page,
    context,
    visualPackage,
  }) => {
    await context.addCookies([{ name: "site_session", value: "1", url: SITE }]);
    const site = await routeSite(page, visualPackage, {
      pages: { "/a/b": html(snippet({ "data-require-consent": true })) },
    });
    await open(page, `${SITE}${PATH}#controlvar-editor=${newTicket()}`);
    await expect.poll(() => site.api.length, { message: "claim requests" }).toBe(1);
    const frame = await frameAt(page, EDITOR);
    await sayHello(frame);
    await expect.poll(async () => (await claimsIn(frame)).length, { message: "claims handed over" }).toBe(1);

    expect(site.api.length, "requests").toBe(1);
    expect(site.api[0].path).toBe(CLAIM_PATH);
    expect(Object.keys(site.api[0].body as object).sort(), "body keys").toEqual(["claim", "ticket"]);
    expect(site.api[0].headers.cookie, "claim request cookie").toBeUndefined();
    expect(await storageKeys(page), "storage keys").toEqual(["session:controlvar_editor_open"]);
    const cookies = (await context.cookies(SITE)).map((cookie) => cookie.name);
    expect(cookies, "cookies").toEqual(["site_session"]);
  });

  test("with the backoff key set, the claim is still sent", async ({ page, visualPackage }) => {
    await page.addInitScript((origin) => {
      if (location.origin === origin) {
        sessionStorage.setItem("controlvar_backoff", String(Date.now() + 300_000));
      }
    }, SITE);
    const site = await routeSite(page, visualPackage, {
      pages: { "/a/b": html(snippet({ "data-require-consent": true })) },
    });
    await open(page, `${SITE}${PATH}#controlvar-editor=${newTicket()}`);
    await expect
      .poll(() => site.api.filter((call) => call.path === CLAIM_PATH).length, { message: "backoff: claim requests" })
      .toBe(1);
    const frame = await frameAt(page, EDITOR);
    await sayHello(frame);
    await expect.poll(async () => (await claimsIn(frame)).length, { message: "backoff: claims handed over" }).toBe(1);
    expect(site.api.length, "backoff: requests").toBe(1);
  });

  test("without the link, before consent: no request, no key and no frame", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/a/b": html(snippet({ "data-require-consent": true })) },
    });
    await page.goto(`${SITE}${PATH}`, { waitUntil: "networkidle" });
    expect(site.api.length, "requests").toBe(0);
    expect(await storageKeys(page), "storage keys").toEqual([]);
    await expect(page.locator(FRAME_SELECTOR)).toHaveCount(0);
  });
});

test.describe("a fragment that is not an exact editor link", () => {
  const cases: [string, (ticket: string) => string][] = [
    ["42-character fragment", (ticket) => ticket.slice(0, 42)],
    ["44-character fragment", (ticket) => `${ticket}A`],
    ["fragment with +", (ticket) => `${ticket.slice(0, 42)}+`],
    ["fragment with =", (ticket) => `${ticket.slice(0, 42)}=`],
    ["fragment with a suffix", (ticket) => `${ticket}&x=1`],
  ];
  for (const [name, value] of cases) {
    test(`is left alone: ${name}`, async ({ page, visualPackage }) => {
      // Before consent nothing else sends either, so any request is the link's.
      const site = await routeSite(page, visualPackage, {
        pages: { "/a/b": html(snippet({ "data-require-consent": true })) },
      });
      const url = `${SITE}${PATH}#controlvar-editor=${value(newTicket())}`;
      await page.goto(url, { waitUntil: "networkidle" });
      expect(site.api.length, `${name}: requests`).toBe(0);
      expect(page.url(), `${name}: address`).toBe(url);
      await expect(page.locator(FRAME_SELECTOR)).toHaveCount(0);
      expect(await storageKeys(page)).toEqual([]);
    });
  }
});

test("a tag without data-editor-origin removes and claims the link, and opens no frame", async ({
  page,
  visualPackage,
}) => {
  const messages: string[] = [];
  page.on("console", (message) => messages.push(`${message.type()}: ${message.text()}`));
  const site = await routeSite(page, visualPackage, {
    pages: { "/a/b": html(snippet({ "data-editor-origin": null })) },
  });
  await page.goto(`${SITE}${PATH}#controlvar-editor=${newTicket()}`, { waitUntil: "networkidle" });
  expect(page.url()).toBe(`${SITE}${PATH}`);
  expect(site.api.map((call) => call.path)).toEqual([CLAIM_PATH]);
  await expect(page.locator(FRAME_SELECTOR)).toHaveCount(0);
  expect(await storageKeys(page)).toEqual([]);
  expect(messages).toEqual([
    "warning: Controlvar: this page was opened with a visual editor link, but the script tag has no data-editor-origin, so the editor stays closed.",
  ]);
});
