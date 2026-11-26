import { randomBytes } from "node:crypto";
import type { APIRequestContext, Page } from "@playwright/test";
import { API_URL } from "../env";
import { TEST_USERS } from "../fixtures/auth.fixture";
import { SITE } from "./editor";
import { exportWindowStart, readExport, type Counts } from "./export-read";
import { expect, test, type VisualPackage } from "./runtime.fixture";
import { BATCH_PATH, PREVIEW_PATH, shown, sitePage, VISUAL_PATH } from "./visual";

/**
 * Preview links and track() against the real API (#1545), as
 * real-api.runtime.spec.ts does it: every request the script sends to the
 * site's /api/ is passed on to the API under test with route.fetch. The tests
 * run in order; each records what its experiment's totals should be, and the
 * last one reads the export once (export-read.ts) for each experiment's
 * assignments and its event rows.
 *
 * Preview links can be made only for pages on an origin the API accepts
 * requests from, so the preview tests' site is http://localhost:3300, which
 * the API accepts in development with no CORS setting (DEFAULT_CORS_ORIGINS)
 * and which is not a dashboard origin. Nothing listens there: page.route
 * answers every request to it.
 */

const RUN = randomBytes(4).toString("hex");
const PREVIEW_SITE = "http://localhost:3300";
const ORIGINAL = '<h1 id="headline">Original</h1>';
const HEADLINE = { op: "set_text", selector: "#headline", text: "Changed" };
const DEVELOPER = TEST_USERS.developer;
const PREVIEW_LOADS = 50;
const GOAL_BODY = '<h1 id="headline">Original</h1><button id="buy">Buy</button>';

interface Forwarded {
  path: string;
  body: unknown;
  status: number;
}

interface Made {
  id: string;
  key: string;
  variants: { id: string; name: string }[];
}

let api: APIRequestContext;
let bearer: Record<string, string>;
let pageKey: string;
let since: string;
/** Each test's arm, its experiment and the totals it expects. */
const arms: { arm: string; id: string; report: Counts }[] = [];

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ playwright }) => {
  since = exportWindowStart();
  arms.length = 0;
  api = await playwright.request.newContext({ baseURL: API_URL });
  const signIn = await api.post("/api/v1/auth/token", {
    form: { username: DEVELOPER.username, password: DEVELOPER.password },
  });
  expect(signIn.status(), `sign in at ${API_URL}`).toBe(200);
  bearer = { Authorization: `Bearer ${(await signIn.json()).access_token}` };
  const key = await api.post("/api/v1/api-keys", { data: { name: `page script previews ${RUN}` }, headers: bearer });
  expect(key.status(), "the page's key").toBe(201);
  pageKey = (await key.json()).key;
});

test.afterAll(async () => {
  await api?.dispose();
});

/**
 * A running visual experiment on *pageUrl*: Control and B, with the traffic
 * given, B holding the headline change; *goals* and the other metrics' event
 * names when given.
 */
async function running(name: string, pageUrl: string, control: number, metric: string, goals: unknown[] = [], others: string[] = []): Promise<Made> {
  const page = { match: "exact", url: pageUrl };
  const created = await api.post("/api/v1/experiments/", {
    headers: bearer,
    data: {
      name: `${name} ${RUN}`,
      experiment_type: "a_b",
      visual_config: { version: 1, editor_url: page.url, pages: [page], goals },
      variants: [
        { name: "Control", is_control: true, traffic_allocation: control },
        { name: "B", is_control: false, traffic_allocation: 100 - control, visual_changes: { version: 1, changes: [HEADLINE] } },
      ],
      metrics: [
        { name: "Buy", event_name: metric, metric_type: "conversion", is_primary: true },
        ...others.map((other, index) => ({ name: `Other ${index + 1}`, event_name: other, metric_type: "conversion", is_primary: false })),
      ],
    },
  });
  expect(created.status(), `create ${name}: ${await created.text()}`).toBe(201);
  const { id, key } = await created.json();
  const read = await api.get(`/api/v1/experiments/${id}`, { headers: bearer });
  expect(read.status(), `read ${name}`).toBe(200);
  const detail = await read.json();
  const started = await api.post(`/api/v1/experiments/${id}/start`, { headers: bearer, data: { design_etag: detail.design_etag } });
  expect(started.status(), `start ${name}: ${await started.text()}`).toBe(200);
  return { id, key, variants: detail.variants };
}

/** The URLs at *origin* that page.route answered, for a test that checks none went to the network. */
const answered = new Map<string, string[]>();

/** *origin*'s pages (each with the snippet first in <head>), the packed script, and its /api/ passed on to the API. */
async function site(page: Page, visualPackage: VisualPackage, origin: string, paths: string[], body = ORIGINAL): Promise<Forwarded[]> {
  const forwarded: Forwarded[] = [];
  const routed: string[] = [];
  answered.set(origin, routed);
  await page.route(`${origin}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    routed.push(request.url());
    if (url.pathname.startsWith("/api/")) {
      let body: unknown = request.postData();
      try {
        body = request.postDataJSON();
      } catch {
        // not JSON: kept as text
      }
      const response = await route.fetch({ url: `${API_URL}${url.pathname}${url.search}` });
      forwarded.push({ path: url.pathname, body, status: response.status() });
      await route.fulfill({ response });
      return;
    }
    if (url.pathname === "/controlvar-visual.js") {
      await route.fulfill({ contentType: "text/javascript; charset=utf-8", body: visualPackage.classicScript });
      return;
    }
    if (paths.includes(url.pathname)) {
      const html = sitePage(body, { "data-api-url": origin, "data-api-key": pageKey });
      await route.fulfill({ contentType: "text/html; charset=utf-8", body: html });
      return;
    }
    await route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
  });
  return forwarded;
}

const statuses = (calls: Forwarded[], path: string) => calls.filter((call) => call.path === path).map((call) => call.status);

test("a preview link shows its variant 50 times and records nothing; a changed or revoked link gets 404, and a visitor still gets their own variant", async ({
  page,
  browser,
  visualPackage,
}) => {
  test.setTimeout(120_000);
  const path = `/preview-${RUN}`;
  // Nobody is assigned B (0 %): only the preview shows it.
  const made = await running("Preview", `${PREVIEW_SITE}${path}`, 100, `buy_${RUN}`);
  const b = made.variants.find((variant) => variant.name === "B")!;
  const link = await api.post(`/api/v1/experiments/${made.id}/visual/preview-links`, { headers: bearer, data: { variant_id: b.id } });
  expect(link.status(), `preview link: ${await link.text()}`).toBe(201);
  const token = new URL((await link.json()).url).hash.replace("#controlvar-preview=", "");
  expect(token).toMatch(/^[A-Za-z0-9_-]{43}$/);

  const forwarded = await site(page, visualPackage, PREVIEW_SITE, [path]);
  for (let load = 1; load <= PREVIEW_LOADS; load += 1) {
    await page.goto(`${PREVIEW_SITE}${path}?load=${load}#controlvar-preview=${token}`, { waitUntil: "load" });
    await expect(page.locator("#headline"), `load ${load}`).toHaveText("Changed");
  }
  await expect.poll(() => statuses(forwarded, PREVIEW_PATH).length, { message: "preview reads answered" }).toBe(PREVIEW_LOADS);
  expect(new Set(statuses(forwarded, PREVIEW_PATH)), "preview read statuses").toEqual(new Set([200]));
  expect(forwarded.map((call) => call.path).filter((p) => p !== PREVIEW_PATH), "other requests").toEqual([]);

  const changed = token.slice(0, -1) + (token.endsWith("A") ? "B" : "A");
  await page.goto(`${PREVIEW_SITE}${path}?changed#controlvar-preview=${changed}`, { waitUntil: "load" });
  await expect.poll(() => statuses(forwarded, PREVIEW_PATH).slice(PREVIEW_LOADS), { message: "a changed token" }).toEqual([404]);
  await expect(page.locator("#headline")).toHaveText("Original");

  const revoked = await api.delete(`/api/v1/experiments/${made.id}/visual/preview-links`, { headers: bearer });
  expect(revoked.status(), "revoke the links").toBe(204);
  await page.goto(`${PREVIEW_SITE}${path}?revoked#controlvar-preview=${token}`, { waitUntil: "load" });
  await expect.poll(() => statuses(forwarded, PREVIEW_PATH).slice(PREVIEW_LOADS), { message: "a revoked link" }).toEqual([404, 404]);
  await expect(page.locator("#headline")).toHaveText("Original");

  // A visitor in another browser context: an ordinary page view, Control for everyone.
  const visitor = await browser.newContext();
  try {
    const visitorPage = await visitor.newPage();
    const visits = await site(visitorPage, visualPackage, PREVIEW_SITE, [path]);
    await visitorPage.goto(`${PREVIEW_SITE}${path}`, { waitUntil: "load" });
    await expect.poll(() => statuses(visits, VISUAL_PATH), { message: "the visitor's call" }).toEqual([200]);
    await expect(visitorPage.locator("#headline")).toHaveText("Original");
  } finally {
    await visitor.close();
  }

  arms.push({ arm: "preview", id: made.id, report: { assignments: 1, events: 1 } });
});

test("an event sent on another page counts for the experiment the visitor saw first", async ({ page, visualPackage }) => {
  const home = `/home-${RUN}`;
  const checkout = `/checkout-${RUN}`;
  const event = `purchase_${RUN}`;
  const made = await running("Conversion elsewhere", `${SITE}${home}`, 0, event);
  const forwarded = await site(page, visualPackage, SITE, [home, checkout]);

  await page.goto(`${SITE}${home}`, { waitUntil: "load" });
  await expect(page.locator("#headline")).toHaveText("Changed");
  await page.goto(`${SITE}${checkout}`, { waitUntil: "load" });
  await page.evaluate((name) => {
    (window as unknown as { controlvar: { visual: { track(e: string, o: unknown): void } } }).controlvar.visual.track(name, { value: 25 });
  }, event);
  await expect.poll(() => statuses(forwarded, BATCH_PATH), { message: "the event's call" }).toEqual([200]);
  expect(statuses(forwarded, VISUAL_PATH), "calls: the checkout page is not one of the experiment's").toEqual([200]);
  const [batch] = forwarded.filter((call) => call.path === BATCH_PATH);
  expect(batch.body).toMatchObject({ events: [{ event_type: event, experiment_key: made.key, value: 25 }] });

  arms.push({ arm: "event on another page", id: made.id, report: { assignments: 1, events: 2 } });
});

test("a preview tab records no goal: 20 clicks and 5 page views of the goals' pages send nothing", async ({ page, visualPackage }) => {
  test.setTimeout(120_000);
  const path = `/preview-goals-${RUN}`;
  const thanks = `${path}/thanks`;
  const click = `click_preview_${RUN}`;
  const view = `page_preview_${RUN}`;
  const goals = [
    { event_name: click, kind: "click", selector: "#buy", page: { match: "exact", url: `${PREVIEW_SITE}${path}` } },
    { event_name: view, kind: "page_view", page: { match: "exact", url: `${PREVIEW_SITE}${thanks}` } },
  ];
  // Everyone gets Control (100 %); only the preview shows B.
  const made = await running("Preview goals", `${PREVIEW_SITE}${path}`, 100, click, goals, [view]);
  const b = made.variants.find((variant) => variant.name === "B")!;
  const link = await api.post(`/api/v1/experiments/${made.id}/visual/preview-links`, { headers: bearer, data: { variant_id: b.id } });
  expect(link.status(), `preview link: ${await link.text()}`).toBe(201);
  const token = new URL((await link.json()).url).hash.replace("#controlvar-preview=", "");
  const seen: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).origin === PREVIEW_SITE) seen.push(request.url());
  });
  const forwarded = await site(page, visualPackage, PREVIEW_SITE, [path, thanks], GOAL_BODY);

  // An ordinary page view first: the visitor is assigned Control, and its goals are kept in this tab's site storage.
  await page.goto(`${PREVIEW_SITE}${path}`, { waitUntil: "load" });
  await expect.poll(() => statuses(forwarded, VISUAL_PATH), { message: "the visitor's call" }).toEqual([200]);
  await expect.poll(() => page.evaluate(() => localStorage.getItem("controlvar_goals") !== null), { message: "the goals kept" }).toBe(true);
  for (let load = 1; load <= 20; load += 1) {
    await page.goto(`${PREVIEW_SITE}${path}?load=${load}#controlvar-preview=${token}`, { waitUntil: "load" });
    await expect(page.locator("#headline"), `preview load ${load}`).toHaveText("Changed");
    await page.click("#buy");
  }
  for (let load = 1; load <= 5; load += 1) {
    await page.goto(`${PREVIEW_SITE}${thanks}?load=${load}`, { waitUntil: "load" });
    // Each read is answered before the next page, as on the loads above.
    await expect.poll(() => statuses(forwarded, PREVIEW_PATH).length, { message: `thanks load ${load}: the preview read` }).toBe(20 + load);
    await expect.poll(() => shown(page), { message: `thanks load ${load}: the page is shown` }).toBe(true);
  }
  expect(new Set(statuses(forwarded, PREVIEW_PATH)), "preview read statuses").toEqual(new Set([200]));
  expect(statuses(forwarded, BATCH_PATH).length, "goal events sent from the preview tab").toBe(0);
  expect(statuses(forwarded, VISUAL_PATH), "page calls: only the ordinary page view").toEqual([200]);
  expect(seen.filter((url) => !answered.get(PREVIEW_SITE)!.includes(url)), "requests to the preview site that page.route did not answer").toEqual([]);

  arms.push({ arm: "preview goals", id: made.id, report: { assignments: 1, events: 1 } });
});

test("the API recorded what each page view sent", async () => {
  expect(
    arms.map(({ arm }) => arm),
    "the page views of this file",
  ).toEqual(["preview", "event on another page", "preview goals"]);
  const counts = await readExport(api, bearer, since, Object.fromEntries(arms.map(({ arm, id }) => [arm, id])));
  for (const { arm, id, report } of arms) {
    expect(counts[id], `${arm}: the experiment's report`).toEqual(report);
  }
});
