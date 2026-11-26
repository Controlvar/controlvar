import { randomBytes } from "node:crypto";
import { devices, type APIRequestContext, type Browser, type Page } from "@playwright/test";
import { API_URL } from "../env";
import { TEST_USERS } from "../fixtures/auth.fixture";
import { SITE } from "./editor";
import { expect, test, type VisualPackage } from "./runtime.fixture";
import { shown, sitePage, VISUAL_PATH } from "./visual";

/**
 * A phones-only visual experiment against the real API (#1545): its audience
 * is matched on the device type the packed script sends. As in
 * real-api.runtime.spec.ts, every request the script sends to SITE/api/ is
 * passed on to the API under test (PLAYWRIGHT_API_URL) with route.fetch, so
 * the browser never calls the API's own origin. Each test makes its own
 * experiment through the experiment routes, as the seeded developer, with a
 * key with no scopes, and reads its report once
 * (GET /api/v1/export/reports/experiments/{id}): assignments and exposures
 * (the report's event total; nothing else records an event here).
 *
 * The phone is each test's own page, Playwright's iPhone 15 profile in
 * Chromium, with navigator.userAgentData deleted, as Safari has none: the
 * device type then comes from the user agent alone. The desktop is a Desktop
 * Chrome context of the same browser.
 */

const RUN = randomBytes(4).toString("hex");
const DEVELOPER = TEST_USERS.developer;
const ORIGINAL = '<p id="a">Original</p><p id="b">Original</p><p id="c">Original</p>';
const CHANGES = ["#a", "#b", "#c"].map((selector) => ({ op: "set_text", selector, text: "Changed" }));
const PHONES_ONLY = {
  logical_operator: "AND",
  groups: [{ logical_operator: "AND", conditions: [{ attribute: "web_device", operator: "equals", value: "phone" }] }],
};
const { defaultBrowserType: _desktopBrowser, ...DESKTOP } = devices["Desktop Chrome"];

test.use({ ...devices["iPhone 15"], browserName: "chromium" });

let api: APIRequestContext;
let bearer: Record<string, string>;
let pageKey: string;

test.beforeAll(async ({ playwright }) => {
  api = await playwright.request.newContext({ baseURL: API_URL });
  const signIn = await api.post("/api/v1/auth/token", {
    form: { username: DEVELOPER.username, password: DEVELOPER.password },
  });
  expect(signIn.status(), `sign in at ${API_URL}`).toBe(200);
  bearer = { Authorization: `Bearer ${(await signIn.json()).access_token}` };
  const key = await api.post("/api/v1/api-keys", { data: { name: `page script details ${RUN}` }, headers: bearer });
  expect(key.status(), "the page's key").toBe(201);
  pageKey = (await key.json()).key;
});

test.afterAll(async () => {
  await api?.dispose();
});

interface Made {
  id: string;
  key: string;
  path: string;
}

/** A running phones-only visual experiment on its own page: Control 0 %, B 100 % with three changes. */
async function phonesOnly(arm: string): Promise<Made> {
  const path = `/details-${RUN}/${arm}`;
  const page = { match: "exact", url: `${SITE}${path}` };
  const created = await api.post("/api/v1/experiments/", {
    headers: bearer,
    data: {
      name: `Page script details ${arm} ${RUN}`,
      experiment_type: "a_b",
      targeting_rules: PHONES_ONLY,
      visual_config: { version: 1, editor_url: page.url, pages: [page], goals: [] },
      variants: [
        { name: "Control", is_control: true, traffic_allocation: 0 },
        { name: "B", is_control: false, traffic_allocation: 100, visual_changes: { version: 1, changes: CHANGES } },
      ],
      metrics: [{ name: "Buy", event_name: `buy_details_${RUN}`, metric_type: "conversion", is_primary: true }],
    },
  });
  expect(created.status(), `create ${arm}: ${await created.text()}`).toBe(201);
  const { id, key } = await created.json();
  const read = await api.get(`/api/v1/experiments/${id}`, { headers: bearer });
  expect(read.status(), `read ${arm}`).toBe(200);
  const started = await api.post(`/api/v1/experiments/${id}/start`, {
    headers: bearer,
    data: { design_etag: (await read.json()).design_etag },
  });
  expect(started.status(), `start ${arm}: ${await started.text()}`).toBe(200);
  return { id, key, path };
}

/** The experiment's report, read once: its assignments and exposures. */
async function report(made: Made): Promise<{ assignments: number; exposures: number }> {
  const response = await api.get(`/api/v1/export/reports/experiments/${made.id}?format=json`, { headers: bearer });
  expect(response.status(), "the experiment's report").toBe(200);
  const rows = (await response.json()).experiments as { experiment_id: string; total_assignments: number; total_events: number }[];
  expect(rows.map((row) => row.experiment_id)).toEqual([made.id]);
  return { assignments: rows[0].total_assignments, exposures: rows[0].total_events };
}

interface Answered {
  /** The context the script sent. */
  context: Record<string, unknown>;
  /** This experiment's entry in the API's answer. */
  entry: { assigned: boolean; reason: string } | undefined;
}

interface View {
  page: Page;
  answered: Answered[];
  /** Requests the browser sent to the API's own origin. */
  direct: string[];
}

function withoutUserAgentData() {
  delete (Navigator.prototype as unknown as { userAgentData?: unknown }).userAgentData;
}

/** A desktop: a page in a new Desktop Chrome context of the same browser. */
async function desktopPage(browser: Browser): Promise<Page> {
  const context = await browser.newContext(DESKTOP);
  return context.newPage();
}

/** A page view of *made*'s page on *page*: the test's iPhone or a desktop. */
async function view(page: Page, visualPackage: VisualPackage, made: Made, device: "phone" | "desktop", userId?: string): Promise<View> {
  if (device === "phone") await page.addInitScript(withoutUserAgentData);
  const answered: Answered[] = [];
  const direct: string[] = [];
  const apiOrigin = new URL(API_URL).origin;
  page.on("request", (request) => {
    if (new URL(request.url()).origin === apiOrigin) direct.push(request.url());
  });
  await page.route(`${SITE}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) {
      const response = await route.fetch({ url: `${API_URL}${url.pathname}${url.search}` });
      if (url.pathname === VISUAL_PATH) {
        const sent = request.postDataJSON() as { context?: Record<string, unknown> };
        const body = (await response.json()) as { experiments: { experiment_key: string; assigned: boolean; reason: string }[] };
        answered.push({ context: sent.context ?? {}, entry: body.experiments.find((entry) => entry.experiment_key === made.key) });
      }
      await route.fulfill({ response });
      return;
    }
    if (url.pathname === "/controlvar-visual.js") {
      await route.fulfill({ contentType: "text/javascript; charset=utf-8", body: visualPackage.classicScript });
      return;
    }
    if (url.pathname === made.path) {
      const attributes: Record<string, string> = { "data-api-key": pageKey };
      if (userId) attributes["data-user-id"] = userId;
      await route.fulfill({ contentType: "text/html; charset=utf-8", body: sitePage(ORIGINAL, attributes) });
      return;
    }
    await route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
  });
  await page.goto(`${SITE}${made.path}`, { waitUntil: "load" });
  if (device === "phone") expect(await page.evaluate(() => "userAgentData" in navigator), "userAgentData is gone").toBe(false);
  await expect.poll(() => answered.length, { message: `${device}: the API's answer` }).toBe(1);
  await expect.poll(() => shown(page), { message: `${device}: the page is shown` }).toBe(true);
  return { page, answered, direct };
}

/** How many of the three elements show the change. */
async function applied(page: Page): Promise<number> {
  return page.locator("#a, #b, #c").evaluateAll((elements) => elements.filter((element) => element.textContent === "Changed").length);
}

test.describe("a phones-only visual experiment against the real API", () => {
  test("an iPhone page view is assigned and changed; a desktop one is not, and the report counts the phone only", async ({
    page,
    browser,
    browserName,
    visualPackage,
  }) => {
    expect(browserName).toBe("chromium");
    const made = await phonesOnly("audience");

    const phone = await view(page, visualPackage, made, "phone");
    expect(`phone: reason "${phone.answered[0].entry?.reason}"`).toBe('phone: reason "assigned"');
    expect(phone.answered[0].context.web_device, "the phone's device type").toBe("phone");
    expect(phone.answered[0].entry?.assigned).toBe(true);
    await expect.poll(() => applied(phone.page), { message: "phone: applied" }).toBe(3);

    const desktop = await view(await desktopPage(browser), visualPackage, made, "desktop");
    expect(desktop.answered[0].context.web_device, "the desktop's device type").toBe("desktop");
    expect(desktop.answered[0].entry).toEqual(expect.objectContaining({ assigned: false, reason: "targeting" }));
    expect(`desktop: applied ${await applied(desktop.page)}`).toBe("desktop: applied 0");

    expect(await report(made), "the report").toEqual({ assignments: 1, exposures: 1 });
    expect([...phone.direct, ...desktop.direct], "requests from the browser to the API's origin").toEqual([]);
    await desktop.page.context().close();
  });

  test("a visitor the site names keeps their variant on a desktop after joining on a phone", async ({
    page,
    browser,
    browserName,
    visualPackage,
  }) => {
    expect(browserName).toBe("chromium");
    const made = await phonesOnly("signed-in");
    const userId = `member-${RUN}`;

    const phone = await view(page, visualPackage, made, "phone", userId);
    expect(phone.answered[0].entry?.assigned, "phone: assigned").toBe(true);
    await expect.poll(() => applied(phone.page), { message: "phone: applied" }).toBe(3);

    const desktop = await view(await desktopPage(browser), visualPackage, made, "desktop", userId);
    await expect.poll(async () => `desktop: applied ${await applied(desktop.page)} != 3`, { message: "desktop: applied" }).toBe(
      "desktop: applied 3 != 3",
    );
    expect(desktop.answered[0].context.web_device, "the desktop's device type").toBe("desktop");
    expect(desktop.answered[0].entry?.assigned, "desktop: assigned").toBe(true);

    expect(await report(made), "the report").toEqual({ assignments: 1, exposures: 2 });
    expect([...phone.direct, ...desktop.direct], "requests from the browser to the API's origin").toEqual([]);
    await desktop.page.context().close();
  });
});
