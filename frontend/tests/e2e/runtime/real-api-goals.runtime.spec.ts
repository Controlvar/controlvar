import { randomBytes } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import type { APIRequestContext, Page } from "@playwright/test";
import { API_URL } from "../env";
import { TEST_USERS } from "../fixtures/auth.fixture";
import { html, snippet } from "./editor";
import { exportWindowStart, readExport, type Counts } from "./export-read";
import { expect, test } from "./runtime.fixture";
import { BATCH_PATH, VISUAL_PATH } from "./visual";

/**
 * Goals against the real API (#1545): the clicks and page views the packed
 * script records reach the database, in every arm, once each.
 *
 * A request a page makes as it goes away never reaches page.route, so the
 * site here is a real server on 127.0.0.1, at VISUAL_GOAL_SITE_PORT (4700 by
 * default; no other step or spec uses it): it serves the pages at
 * http://127.0.0.1:<port> and the packed script, and passes every request to
 * its /api/ on to the API under test (PLAYWRIGHT_API_URL), so the browser
 * never calls the API's own origin. The script's API is that server at
 * http://localhost:<port>, another origin, as an API on its own host is: each
 * call is preflighted, and the preflight is never cached. The server answers
 * a batch call's preflight after PREFLIGHT_DELAY_MS, as a slow network does,
 * so a call the browser drops with its page is never sent; it can answer a
 * batch call 503 after passing it on, as a lost answer looks to the script.
 * Its /away page, on that other origin, has no script.
 *
 * Each test makes its own running experiment through the experiment routes,
 * as the seeded developer, with a key with no scopes, and records what its
 * totals should be; the tests run in order, and the last one reads the
 * export once (export-read.ts): assignments, and events (exposures and goal
 * events; nothing else records one here).
 */

const RUN = randomBytes(4).toString("hex");
const PORT = Number(process.env.VISUAL_GOAL_SITE_PORT ?? 4700);
const ORIGIN = `http://127.0.0.1:${PORT}`;
/** The script's API: the same server, on another origin. */
const API_ORIGIN = `http://localhost:${PORT}`;
/** A page on that other origin, with no script. */
const AWAY = `${API_ORIGIN}/away`;
/** How long the server waits before it answers a batch call's preflight. */
const PREFLIGHT_DELAY_MS = 300;
const CORS = {
  "Access-Control-Allow-Origin": ORIGIN,
  "Access-Control-Allow-Methods": "POST",
  "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
  "Access-Control-Max-Age": "0",
};
const DEVELOPER = TEST_USERS.developer;
const BODY = `<h1 id="headline">Original</h1><button id="buy">Buy</button><a id="leave" href="${AWAY}">Leave</a>`;
const HEADLINE = { op: "set_text", selector: "#headline", text: "Changed" };
/** New visitors to make, at most, until both arms of a 50/50 experiment have one. */
const MAX_VISITORS = 24;

interface Forwarded {
  path: string;
  /** What the browser got. */
  status: number;
  /** What the API answered. */
  apiStatus: number;
  sent: { user_id?: string; events?: { event_type: string; user_id: string; experiment_key: string; event_id: string }[] };
  answer: unknown;
}

interface Made {
  id: string;
  key: string;
  path: string;
  click: string;
  view: string;
}

let api: APIRequestContext;
let bearer: Record<string, string>;
let pageKey = "";
let since: string;
let server: Server;
let script = "";
/** Every request to the site's /api/, in arrival order. */
const forwarded: Forwarded[] = [];
/** Batch calls the site answers 503 after passing them on. */
let refuseBatches = 0;
/** The site's pages, by path. */
const pages = new Map<string, string>();
/** Each test's arm, its experiment and the totals it expects. */
const arms: { arm: string; id: string; report: Counts }[] = [];

function parsed(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function handle(request: IncomingMessage, response: ServerResponse, text: string): Promise<void> {
  const url = new URL(request.url ?? "/", ORIGIN);
  const api = url.pathname.startsWith("/api/");
  const send = (status: number, type: string, content: string) => {
    response.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store", ...(api ? CORS : {}) });
    response.end(content);
  };
  if (api && request.method === "OPTIONS") {
    if (url.pathname === BATCH_PATH) await new Promise((resolve) => setTimeout(resolve, PREFLIGHT_DELAY_MS));
    return send(204, "text/plain", "");
  }
  if (url.pathname === "/controlvar-visual.js") return send(200, "text/javascript; charset=utf-8", script);
  if (url.pathname === "/away") return send(200, "text/html; charset=utf-8", "<!doctype html><title>away</title><p id=\"away\">Away</p>");
  if (api) {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const key = request.headers["x-api-key"];
    if (typeof key === "string") headers["X-API-Key"] = key;
    const answer = await fetch(`${API_URL}${url.pathname}${url.search}`, { method: request.method, headers, body: request.method === "POST" ? text : undefined });
    const answerText = await answer.text();
    const refused = url.pathname === BATCH_PATH && refuseBatches > 0;
    if (refused) refuseBatches -= 1;
    forwarded.push({
      path: url.pathname,
      status: refused ? 503 : answer.status,
      apiStatus: answer.status,
      sent: parsed(text) as Forwarded["sent"],
      answer: parsed(answerText),
    });
    if (refused) return send(503, "application/json", '{"detail":"unavailable"}');
    return send(answer.status, "application/json", answerText);
  }
  const body = pages.get(url.pathname);
  if (body !== undefined && url.host === new URL(ORIGIN).host) {
    const tag = snippet({ "data-api-url": API_ORIGIN, "data-api-key": pageKey, "data-editor-origin": null });
    return send(200, "text/html; charset=utf-8", html(tag, body));
  }
  send(404, "text/plain", "not found");
}

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ playwright, visualPackage }) => {
  since = exportWindowStart();
  arms.length = 0;
  script = visualPackage.classicScript;
  api = await playwright.request.newContext({ baseURL: API_URL });
  const signIn = await api.post("/api/v1/auth/token", { form: { username: DEVELOPER.username, password: DEVELOPER.password } });
  expect(signIn.status(), `sign in at ${API_URL}`).toBe(200);
  bearer = { Authorization: `Bearer ${(await signIn.json()).access_token}` };
  const key = await api.post("/api/v1/api-keys", { data: { name: `page script goals ${RUN}` }, headers: bearer });
  expect(key.status(), "the page's key").toBe(201);
  pageKey = (await key.json()).key;
  server = createServer((request, response) => {
    let text = "";
    request.on("data", (chunk: Buffer) => (text += chunk.toString("utf8")));
    request.on("end", () => {
      handle(request, response, text).catch((error: Error) => {
        response.writeHead(502, { "Content-Type": "text/plain" });
        response.end(String(error.message));
      });
    });
  });
  await new Promise<void>((resolve) => server.listen(PORT, "127.0.0.1", resolve));
  expect((server.address() as AddressInfo).port).toBe(PORT);
});

test.afterAll(async () => {
  await api?.dispose();
  if (server) await new Promise<void>((resolve) => server.close(() => resolve()));
});

/**
 * A running visual experiment on its own page of the site, with a click goal
 * on #buy there and a page_view goal on its /thanks page, each a metric:
 * Control (no changes) at *control* %, B (a changed headline) at the rest.
 */
async function running(arm: string, control: number, selector = "#buy"): Promise<Made> {
  const path = `/goals-${RUN}/${arm}`;
  const page = { match: "exact", url: `${ORIGIN}${path}` };
  const click = `click_${arm}_${RUN}`;
  const view = `page_${arm}_${RUN}`;
  const created = await api.post("/api/v1/experiments/", {
    headers: bearer,
    data: {
      name: `Page script goals ${arm} ${RUN}`,
      experiment_type: "a_b",
      visual_config: {
        version: 1,
        editor_url: page.url,
        pages: [page],
        goals: [
          { event_name: click, kind: "click", selector, page },
          { event_name: view, kind: "page_view", page: { match: "exact", url: `${ORIGIN}${path}/thanks` } },
        ],
      },
      variants: [
        { name: "Control", is_control: true, traffic_allocation: control },
        { name: "B", is_control: false, traffic_allocation: 100 - control, visual_changes: { version: 1, changes: [HEADLINE] } },
      ],
      metrics: [
        { name: `Clicked ${arm}`, event_name: click, metric_type: "conversion", is_primary: true },
        { name: `Thanked ${arm}`, event_name: view, metric_type: "conversion", is_primary: false },
      ],
    },
  });
  expect(created.status(), `create ${arm}: ${await created.text()}`).toBe(201);
  const { id, key } = await created.json();
  const read = await api.get(`/api/v1/experiments/${id}`, { headers: bearer });
  expect(read.status(), `read ${arm}`).toBe(200);
  const started = await api.post(`/api/v1/experiments/${id}/start`, { headers: bearer, data: { design_etag: (await read.json()).design_etag } });
  expect(started.status(), `start ${arm}: ${await started.text()}`).toBe(200);
  pages.set(path, BODY);
  pages.set(`${path}/thanks`, BODY);
  return { id, key, path, click, view };
}

/** The requests *page* sends to the API's own origin (none should be). */
function direct(page: Page): string[] {
  const seen: string[] = [];
  const apiOrigin = new URL(API_URL).origin;
  page.on("request", (request) => {
    if (new URL(request.url()).origin === apiOrigin) seen.push(request.url());
  });
  return seen;
}

/** The API's answer for *made* to the page calls from index *from* on. */
function entryFor(from: number, made: Made): { user: string; variant: string } | undefined {
  for (const call of forwarded.slice(from)) {
    if (call.path !== VISUAL_PATH || call.apiStatus !== 200) continue;
    const answer = call.answer as { experiments: { experiment_key: string; assigned: boolean; variant_name: string }[] };
    const entry = answer.experiments.find((item) => item.experiment_key === made.key && item.assigned);
    if (entry) return { user: String(call.sent.user_id), variant: entry.variant_name };
  }
  return undefined;
}

/** The goal events the API stored for *made*, from *user*, by event name. */
function stored(made: Made, user: string): string[] {
  return forwarded
    .filter((call) => call.path === BATCH_PATH && call.apiStatus === 200)
    .flatMap((call) => call.sent.events ?? [])
    .filter((event) => event.experiment_key === made.key && event.user_id === user)
    .map((event) => event.event_type);
}

async function conversions(made: Made): Promise<Record<string, Record<string, number>>> {
  const response = await api.get(`/api/v1/results/${made.id}`, { headers: bearer, params: { use_cache: "false" } });
  expect(response.status(), `results: ${await response.text()}`).toBe(200);
  const results = (await response.json()) as { metrics: { metric_name: string; variants: { variant_name: string; conversions: number }[] }[] };
  return Object.fromEntries(
    results.metrics.map((metric) => [metric.metric_name, Object.fromEntries(metric.variants.map((variant) => [variant.variant_name, variant.conversions]))]),
  );
}

test.describe("goals against the real API", () => {
  test("a control visitor and a B visitor each click once, and each arm counts one conversion", async ({ browser }) => {
    test.setTimeout(120_000);
    const made = await running("arms", 50);
    const clicked = new Set<string>();
    const calls: string[] = [];
    let visitors = 0;
    while (clicked.size < 2 && visitors < MAX_VISITORS) {
      visitors += 1;
      const context = await browser.newContext();
      try {
        const visitor = await context.newPage();
        const seen = direct(visitor);
        const from = forwarded.length;
        await visitor.goto(`${ORIGIN}${made.path}`, { waitUntil: "load" });
        await expect.poll(() => entryFor(from, made), { message: `visitor ${visitors}: the API's answer` }).toBeDefined();
        const { user, variant } = entryFor(from, made)!;
        if (!clicked.has(variant)) {
          clicked.add(variant);
          await visitor.click("#buy");
          await expect.poll(() => stored(made, user).length, { message: `${variant.toLowerCase()} rows` }).toBe(1);
        }
        calls.push(...seen);
      } finally {
        await context.close();
      }
    }
    expect(Array.from(clicked).sort(), `the arms of ${visitors} visitors`).toEqual(["B", "Control"]);
    expect(await conversions(made), "the experiment's results").toEqual({
      "Clicked arms": { Control: 1, B: 1 },
      "Thanked arms": { Control: 0, B: 0 },
    });
    expect(calls, "requests from the browser to the API's origin").toEqual([]);
    arms.push({ arm: "arms", id: made.id, report: { assignments: visitors, events: visitors + 2 } });
  });

  test("an event whose answer was lost is sent again from the next page, and stored once", async ({ page }) => {
    const made = await running("resend", 0);
    const seen = direct(page);
    const from = forwarded.length;
    await page.goto(`${ORIGIN}${made.path}`, { waitUntil: "load" });
    await expect(page.locator("#headline")).toHaveText("Changed");
    await expect.poll(() => entryFor(from, made)).toBeDefined();
    const { user } = entryFor(from, made)!;
    refuseBatches = 1;
    await page.click("#buy");
    await expect.poll(() => forwarded.filter((call) => call.path === BATCH_PATH && call.status === 503).length, { message: "the lost answer" }).toBe(1);
    const kept = await page.evaluate(() => JSON.parse(localStorage.getItem("controlvar_outbox") ?? "[]").length);
    expect(kept, "events kept in the outbox").toBe(1);

    await page.goto(`${ORIGIN}${made.path}/thanks`, { waitUntil: "load" });
    await expect.poll(() => stored(made, user).sort(), { message: "the events stored" }).toEqual([made.click, made.click, made.view].sort());
    const [lost, again] = forwarded.filter((call) => call.path === BATCH_PATH && call.sent.events?.some((event) => event.event_type === made.click));
    expect(lost.answer, "the first call").toMatchObject({ success_count: 1, duplicate_count: 0 });
    expect(again.answer, "the call sent again").toMatchObject({ success_count: 2, duplicate_count: 1 });
    const ids = [lost, again].map((call) => call.sent.events!.find((event) => event.event_type === made.click)!.event_id);
    expect(ids[1], "the event_id sent again").toBe(ids[0]);
    await expect.poll(() => page.evaluate(() => localStorage.getItem("controlvar_outbox")), { message: "the outbox" }).toBeNull();
    expect(forwarded.slice(from).filter((call) => call.path === VISUAL_PATH).length, "page calls: the thanks page is not one of the experiment's").toBe(1);
    expect(seen, "requests from the browser to the API's origin").toEqual([]);
    arms.push({ arm: "resend", id: made.id, report: { assignments: 1, events: 3 } });
  });

  test("a click on a link to another site is recorded as the page goes away", async ({ page }) => {
    const made = await running("leave", 0, "#leave");
    const seen = direct(page);
    const from = forwarded.length;
    await page.goto(`${ORIGIN}${made.path}`, { waitUntil: "load" });
    await expect(page.locator("#headline")).toHaveText("Changed");
    await expect.poll(() => entryFor(from, made)).toBeDefined();
    const { user } = entryFor(from, made)!;
    const clickedAt = Date.now();
    await page.click("#leave");
    await page.waitForURL(AWAY);
    await expect(page.locator("#away")).toHaveText("Away");
    await expect.poll(() => stored(made, user), { message: "the event sent as the page went away" }).toEqual([made.click]);
    console.log(`page-runtime goals: the event sent as the page went away reached the site after the click (${Date.now() - clickedAt} ms to here)`);
    expect(seen, "requests from the browser to the API's origin").toEqual([]);
    arms.push({ arm: "leave", id: made.id, report: { assignments: 1, events: 2 } });
  });

  test("the API recorded what each page view sent", async () => {
    expect(
      arms.map(({ arm }) => arm),
      "the page views of this file",
    ).toEqual(["arms", "resend", "leave"]);
    const counts = await readExport(api, bearer, since, Object.fromEntries(arms.map(({ arm, id }) => [arm, id])));
    for (const { arm, id, report } of arms) {
      expect(counts[id], `${arm}: the experiment's totals`).toEqual(report);
    }
    const resend = arms.find(({ arm }) => arm === "resend")!;
    // One exposure and one page_view event; the rest are the click's rows.
    expect(`resend: click rows ${counts[resend.id].events - 2}`).toBe("resend: click rows 1");
  });
});
