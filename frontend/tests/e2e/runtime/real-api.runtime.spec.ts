import { randomBytes } from "node:crypto";
import type { APIRequestContext, Page } from "@playwright/test";
import { API_URL } from "../env";
import { TEST_USERS } from "../fixtures/auth.fixture";
import { SITE } from "./editor";
import { expect, test, type VisualPackage } from "./runtime.fixture";
import { ERRORS_PATH, shown, sitePage, VISUAL_PATH, type Report } from "./visual";

/**
 * What a page view records in the real API (#1545). The pages are on the
 * routed site, and every request the script sends to SITE/api/ is passed on
 * to the API under test (PLAYWRIGHT_API_URL, the Browser E2E job's API) with
 * route.fetch: the browser never calls the API's own origin, so the API needs
 * no cross-origin setting for these pages. Each test has its own experiment,
 * made and started through the experiment routes by the seeded developer,
 * on its own page of the site, and the page uses a key with no scopes made
 * through the API key route.
 *
 * The exposures are read back from the experiment's report
 * (GET /api/v1/export/reports/experiments/{id}): its total of event rows.
 * Nothing else records an event for these experiments (no test here calls
 * track()), so that total is the exposures.
 *
 * Every page is a fresh navigation, on the page clock, which stands in for
 * the seconds to the bound.
 */

const START = new Date("2026-11-16T12:00:00Z");
const RUN = randomBytes(4).toString("hex");
const ORIGINAL = "<h1 id=\"headline\">Original</h1><p id=\"other\">Other</p>";
const DEVELOPER = TEST_USERS.developer;

interface Forwarded {
  path: string;
  body: unknown;
  /** The API's status. */
  status: number;
}

interface Made {
  id: string;
  key: string;
  path: string;
}

let api: APIRequestContext;
let bearer: Record<string, string>;
let pageKey: string;

test.beforeAll(async ({ playwright }) => {
  api = await playwright.request.newContext({ baseURL: API_URL });
  // The token route, not /login: the browser journeys that run before this
  // spec sign in through /login, and each route has its own limit.
  const signIn = await api.post("/api/v1/auth/token", {
    form: { username: DEVELOPER.username, password: DEVELOPER.password },
  });
  expect(signIn.status(), `sign in at ${API_URL}`).toBe(200);
  bearer = { Authorization: `Bearer ${(await signIn.json()).access_token}` };
  const key = await api.post("/api/v1/api-keys", { data: { name: `page script ${RUN}` }, headers: bearer });
  expect(key.status(), "the page's key").toBe(201);
  pageKey = (await key.json()).key;
});

test.afterAll(async () => {
  await api?.dispose();
});

/** A running visual experiment on its own page of the site: Control 0 %, B 100 % with *changes*. */
async function running(arm: string, changes: unknown[]): Promise<Made> {
  const path = `/exposure-${RUN}/${arm}`;
  const page = { match: "exact", url: `${SITE}${path}` };
  const created = await api.post("/api/v1/experiments/", {
    headers: bearer,
    data: {
      name: `Page script ${arm} ${RUN}`,
      experiment_type: "a_b",
      visual_config: { version: 1, editor_url: page.url, pages: [page], goals: [] },
      variants: [
        { name: "Control", is_control: true, traffic_allocation: 0 },
        { name: "B", is_control: false, traffic_allocation: 100, visual_changes: { version: 1, changes } },
      ],
      metrics: [{ name: "Buy", event_name: `buy_${RUN}`, metric_type: "conversion", is_primary: true }],
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

/** The experiment's exposures: the event total of its report. */
async function exposures(made: Made): Promise<number> {
  const report = await api.get(`/api/v1/export/reports/experiments/${made.id}?format=json`, { headers: bearer });
  expect(report.status(), "the experiment's report").toBe(200);
  const rows = (await report.json()).experiments as { experiment_id: string; total_events: number }[];
  expect(rows.map((row) => row.experiment_id)).toEqual([made.id]);
  return rows[0].total_events;
}

/** Records the path of every fetch the page makes, the script's included; installed before any script. */
function countFetches() {
  const native = window.fetch;
  const sent: string[] = [];
  (window as unknown as { __sent: string[] }).__sent = sent;
  window.fetch = function (input: RequestInfo | URL, init?: RequestInit) {
    sent.push(new URL(input instanceof Request ? input.url : String(input), location.href).pathname);
    return native(input, init);
  };
}

const sent = (page: Page) => page.evaluate(() => (window as unknown as { __sent: string[] }).__sent.slice());

interface Site {
  forwarded: Forwarded[];
  /** Requests the browser sent to the API's own origin. */
  direct: string[];
}

interface SiteOptions {
  /** Hold every S3 request until this settles. */
  holdVisual?: Promise<void>;
  /** The script's file never loads, as when a blocker refuses it. */
  blockScript?: boolean;
}

/** The site: *made*'s page with the snippet first in <head>, the packed script, and SITE/api/ passed on to the API. */
async function site(page: Page, visualPackage: VisualPackage, made: Made, options: SiteOptions = {}): Promise<Site> {
  const forwarded: Forwarded[] = [];
  const direct: string[] = [];
  const apiOrigin = new URL(API_URL).origin;
  page.on("request", (request) => {
    if (new URL(request.url()).origin === apiOrigin) direct.push(request.url());
  });
  await page.addInitScript(countFetches);
  await page.route(`${SITE}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) {
      if (url.pathname === VISUAL_PATH && options.holdVisual) await options.holdVisual;
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
      if (options.blockScript) await route.abort("blockedbyclient");
      else await route.fulfill({ contentType: "text/javascript; charset=utf-8", body: visualPackage.classicScript });
      return;
    }
    if (url.pathname === made.path) {
      await route.fulfill({
        contentType: "text/html; charset=utf-8",
        body: sitePage(ORIGINAL, { "data-api-key": pageKey }),
      });
      return;
    }
    await route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
  });
  return { forwarded, direct };
}

/** The tab reports hidden (a headless tab never is): the script compares the targets it changed. */
function hide(page: Page) {
  return page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
  });
}

/**
 * Past the bound, with the tab reported hidden: every report the script would
 * send has been sent. Then every request the page sent to the API has its
 * answer, so the reads below see all that the page view recorded.
 */
async function settle(page: Page, made: Made, s: Site) {
  await page.clock.runFor(6000);
  await hide(page);
  const paths = (await sent(page)).filter((path) => path.startsWith("/api/"));
  await expect.poll(() => s.forwarded.length, { message: "every API request answered" }).toBe(paths.length);
  return {
    visual: s.forwarded.filter((call) => call.path === VISUAL_PATH),
    reports: s.forwarded.filter((call) => call.path === ERRORS_PATH),
    exposures: await exposures(made),
  };
}

const HEADLINE = { op: "set_text", selector: "#headline", text: "Changed" };

test.describe("a page view against the real API", () => {
  test("a change that applies: one exposure, no report", async ({ page, visualPackage }) => {
    const made = await running("applied", [HEADLINE]);
    const s = await site(page, visualPackage, made);
    await page.clock.install({ time: START });
    await page.goto(`${SITE}${made.path}`, { waitUntil: "load" });
    await expect(page.locator("#headline")).toHaveText("Changed");
    const seen = await settle(page, made, s);
    expect(seen.exposures, "applied: exposures").toBe(1);
    expect(seen.reports, "applied: reports").toHaveLength(0);
    expect(seen.visual.map((call) => call.status), "applied: S3 answers").toEqual([200]);
    expect(s.direct, "requests from the browser to the API's origin").toEqual([]);
  });

  test("a selector that matches nothing: one exposure, and one not_found report the API accepts", async ({
    page,
    visualPackage,
  }) => {
    const made = await running("miss", [{ op: "set_text", selector: "#missing", text: "Changed" }]);
    const s = await site(page, visualPackage, made);
    await page.clock.install({ time: START });
    await page.goto(`${SITE}${made.path}`, { waitUntil: "load" });
    await expect
      .poll(() => s.forwarded.some((call) => call.path === VISUAL_PATH), { message: "the S3 answer, before the bound" })
      .toBe(true);
    const seen = await settle(page, made, s);
    expect(seen.exposures, "selector miss: exposures").toBe(1);
    expect(seen.reports, "selector miss: reports").toHaveLength(1);
    expect(seen.reports[0].status, "the API's answer to the report").toBe(201);
    expect(seen.reports[0].body as Report).toEqual({
      error_type: "visual_change_not_applied",
      experiment_key: made.key,
      message: "A visual change was not applied: its selector matched no element.",
      metadata: { index: 0, reason: "not_found" },
    });
    expect(await page.locator("#headline").textContent()).toBe("Original");
    expect(s.direct, "requests from the browser to the API's origin").toEqual([]);
  });

  test("an answer slower than the hide: the page shows first, the change applies late, one exposure, no report", async ({
    page,
    visualPackage,
  }) => {
    const made = await running("slow", [HEADLINE]);
    let release: () => void = () => undefined;
    const holdVisual = new Promise<void>((resolve) => (release = resolve));
    const s = await site(page, visualPackage, made, { holdVisual });
    await page.clock.install({ time: START });
    await page.goto(`${SITE}${made.path}`, { waitUntil: "load" });
    await expect.poll(() => shown(page), { message: "shown at the hide's timeout, before the answer" }).toBe(true);
    expect(await page.locator("#headline").textContent(), "shown before the answer").toBe("Original");
    expect(s.forwarded, "nothing answered yet").toEqual([]);
    release();
    await expect(page.locator("#headline")).toHaveText("Changed");
    const seen = await settle(page, made, s);
    expect(seen.exposures, "slow: exposures").toBe(1);
    expect(seen.reports, "slow: reports").toHaveLength(0);
    expect(seen.visual.map((call) => call.status), "slow: S3 answers").toEqual([200]);
    expect(s.direct, "requests from the browser to the API's origin").toEqual([]);
  });

  test("a blocked script: no request, no exposure, the page as it is", async ({ page, visualPackage }) => {
    const made = await running("blocked", [HEADLINE]);
    const s = await site(page, visualPackage, made, { blockScript: true });
    await page.clock.install({ time: START });
    await page.goto(`${SITE}${made.path}`, { waitUntil: "load" });
    const seen = await settle(page, made, s);
    expect(seen.exposures, "blocked: exposures").toBe(0);
    expect(seen.visual, "blocked: S3 calls").toHaveLength(0);
    expect(seen.reports, "blocked: reports").toHaveLength(0);
    expect(await shown(page), "shown").toBe(true);
    expect(await page.locator("#headline").textContent()).toBe("Original");
    expect(s.direct, "requests from the browser to the API's origin").toEqual([]);
  });

  test("an element replaced after the bound: one exposure, and one replaced_after_bound report the API accepts", async ({
    page,
    visualPackage,
  }) => {
    const made = await running("replaced", [HEADLINE, { op: "set_text", selector: "#other", text: "Changed" }]);
    const s = await site(page, visualPackage, made);
    await page.clock.install({ time: START });
    await page.goto(`${SITE}${made.path}`, { waitUntil: "load" });
    await expect(page.locator("#other")).toHaveText("Changed");
    await page.clock.runFor(6000);
    await page.evaluate(() => {
      const copy = document.createElement("h1");
      copy.id = "headline";
      copy.textContent = "Original";
      document.getElementById("headline")!.replaceWith(copy);
    });
    const seen = await settle(page, made, s);
    expect(seen.exposures, "replaced: exposures").toBe(1);
    expect(seen.reports, "replaced: reports").toHaveLength(1);
    expect(seen.reports[0].status, "the API's answer to the report").toBe(201);
    expect(seen.reports[0].body as Report).toEqual({
      error_type: "visual_change_not_applied",
      experiment_key: made.key,
      message: "A visual change was lost: the page replaced the element after the script stopped watching.",
      metadata: { index: 0, reason: "replaced_after_bound" },
    });
    expect(s.direct, "requests from the browser to the API's origin").toEqual([]);
  });
});
