import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import type { Page } from "@playwright/test";
import { html, routeSite, SITE, snippet } from "./editor";
import { expect, test } from "./runtime.fixture";
import { answering, ERRORS_PATH, experiment, reports, sitePage, VISUAL_PATH, type Report } from "./visual";

/**
 * Keeping the changes (#1545): until the bound, min(load + 5 s, navigation
 * start + 10 s), the script writes a change again when the page replaces its
 * element or writes over it, at most 20 times in a second for each change;
 * after the bound no observer is left, and a change whose element the page
 * replaced since is reported once, as the page goes away. The page clock
 * stands in for the seconds; every page is a fresh navigation.
 */

const START = new Date("2026-11-16T12:00:00Z");

/** Counts the page's live MutationObservers (observing, not disconnected); installed before any script. */
function countObservers() {
  const Native = window.MutationObserver;
  const live = new Set<MutationObserver>();
  (window as unknown as { __liveObservers(): number }).__liveObservers = () => live.size;
  window.MutationObserver = class extends Native {
    observe(target: Node, options?: MutationObserverInit) {
      live.add(this);
      return super.observe(target, options);
    }
    disconnect() {
      live.delete(this);
      return super.disconnect();
    }
  };
}

const liveObservers = (page: Page) => page.evaluate(() => (window as unknown as { __liveObservers(): number }).__liveObservers());

function texts(page: Page, count: number) {
  return page.evaluate((n) => Array.from({ length: n }, (_, index) => document.getElementById(`t-${index + 1}`)!.textContent), count);
}

/** Replaces each element matching *selector* with a copy of its server version, as a framework re-render does. */
function replaceAll(page: Page, selector: string) {
  return page.evaluate((css) => {
    for (const element of Array.from(document.querySelectorAll(css))) {
      const copy = document.createElement(element.localName);
      copy.id = element.id;
      copy.textContent = "Original";
      element.replaceWith(copy);
    }
  }, selector);
}

const TARGETS = Array.from({ length: 25 }, (_, index) => `<p id="t-${index + 1}">Original</p>`).join("");
const ON_EACH = Array.from({ length: 25 }, (_, index) => ({ op: "set_text", selector: `#t-${index + 1}`, text: "Changed" }));

test.describe("until the bound", () => {
  test("an element that appears inside the bound is changed; after it, no observer is left and nothing changes", async ({
    page,
    visualPackage,
  }) => {
    await page.addInitScript(countObservers);
    await page.clock.install({ time: START });
    const body = `<h1 id="t">Original</h1>
<script>
function add(id, at) { setTimeout(function () { var p = document.createElement("p"); p.id = id; p.textContent = "Original"; document.body.appendChild(p); }, at); }
add("late", 500); add("later", 7000);
</script>`;
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(body) },
      answer: answering({
        experiments: [
          experiment("bound", [
            { op: "set_text", selector: "#t", text: "Changed" },
            { op: "set_text", selector: "#late", text: "Changed" },
            { op: "set_text", selector: "#later", text: "Changed" },
          ]),
        ],
      }),
    });
    await page.goto(`${SITE}/`, { waitUntil: "load" });
    await expect(page.locator("#t")).toHaveText("Changed");
    await page.clock.runFor(600);
    await expect(page.locator("#late")).toHaveText("Changed");
    expect(await liveObservers(page), "live observers inside the bound").toBe(1);
    expect(reports(site), "reports inside the bound").toEqual([]);

    await page.clock.runFor(7000);
    await expect(page.locator("#later")).toHaveText("Original");
    expect(await liveObservers(page), "live observers").toBe(0);
    await replaceAll(page, "#t");
    expect(await page.locator("#t").textContent(), "replaced after the bound").toBe("Original");
    await expect.poll(() => reports(site).length).toBe(1);
    expect(reports(site)[0].metadata).toEqual({ index: 2, reason: "not_found" });
  });

  test("the bound is never later than 10 s after the navigation started, however late the load", async ({ page, visualPackage }) => {
    await page.addInitScript(countObservers);
    await page.clock.install({ time: START });
    const body = `<h1 id="t">Original</h1><img src="/slow.png" alt="">
<script>
function add(id, at) { setTimeout(function () { var p = document.createElement("p"); p.id = id; p.textContent = "Original"; document.body.appendChild(p); }, at); }
add("late", 9500); add("after", 10500);
</script>`;
    await routeSite(page, visualPackage, {
      pages: { "/": sitePage(body) },
      answer: answering({
        experiments: [
          experiment("cap", [
            { op: "set_text", selector: "#late", text: "Changed" },
            { op: "set_text", selector: "#after", text: "Changed" },
          ]),
        ],
      }),
    });
    // Registered after routeSite's, so it answers first: the image holds the load event.
    let release: () => void = () => undefined;
    const slow = new Promise<void>((resolve) => (release = resolve));
    await page.route(`${SITE}/slow.png`, async (route) => {
      await slow;
      await route.fulfill({ status: 404, body: "" });
    });
    await page.goto(`${SITE}/`, { waitUntil: "domcontentloaded" });
    expect(await page.evaluate(() => document.readyState), "the load is held").toBe("interactive");
    await page.clock.runFor(9700);
    await expect(page.locator("#late")).toHaveText("Changed");
    expect(await liveObservers(page), "live observers at 9.7 s, before the load").toBe(1);
    await page.clock.runFor(1000);
    await expect(page.locator("#after")).toHaveText("Original");
    expect(await liveObservers(page), "live observers at 10.7 s").toBe(0);
    release();
  });

  test("a page that undoes a change at once is stopped after 20 re-applies, reported once, and stays responsive", async ({
    page,
    visualPackage,
  }) => {
    const body = `<h1 id="t">Original</h1><p id="p">Original</p>
<script>
window.__resets = 0;
new MutationObserver(function () {
  var t = document.getElementById("t");
  if (t.textContent !== "Original") { window.__resets += 1; t.textContent = "Original"; }
}).observe(document.body, { childList: true, subtree: true, characterData: true });
</script>`;
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(body) },
      answer: answering({
        experiments: [
          experiment("fight", [
            { op: "set_text", selector: "#t", text: "Changed" },
            { op: "set_text", selector: "#p", text: "Changed" },
          ]),
        ],
      }),
    });
    await page.goto(`${SITE}/`);
    await expect(page.locator("#p")).toHaveText("Changed");
    const started = Date.now();
    const resets = await page.evaluate(() => (window as unknown as { __resets: number }).__resets);
    expect(Date.now() - started, "page.evaluate took (ms)").toBeLessThan(1000);
    expect(resets, "the page's resets").toBe(21);
    await expect(page.locator("#t")).toHaveText("Original");
    await expect.poll(() => reports(site).length).toBe(1);
    expect(reports(site)[0].metadata).toEqual({ index: 0, reason: "reapply_limit" });
  });

  test("a page that resets a text every 100 ms gets the change back each time until the bound, then keeps its own", async ({
    page,
    visualPackage,
  }) => {
    await page.clock.install({ time: START });
    const body = `<h1 id="t">Original</h1>
<script>
window.__seen = [];
setInterval(function () {
  var t = document.getElementById("t");
  window.__seen.push(t.textContent);
  t.textContent = "Original";
}, 100);
</script>`;
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(body) },
      answer: answering({ experiments: [experiment("reset", [{ op: "set_text", selector: "#t", text: "Changed" }])] }),
    });
    await page.goto(`${SITE}/`, { waitUntil: "load" });
    await expect.poll(() => page.evaluate(() => (window as unknown as { __seen: string[] }).__seen.length)).toBeGreaterThanOrEqual(12);
    const seen = await page.evaluate(() => (window as unknown as { __seen: string[] }).__seen.slice(2, 12));
    expect(seen, "what each reset found, inside the bound").toEqual(Array(10).fill("Changed"));
    await page.clock.runFor(6000);
    await expect(page.locator("#t")).toHaveText("Original");
    expect(reports(site), "reports").toEqual([]);
  });

  for (const [every, perSecond] of [
    [40, 25],
    [60, 16],
  ] as const) {
    const capped = perSecond > 20;
    test(`a page that resets a text every ${every} ms (${perSecond} a second) ${capped ? "is stopped by the cap of 20 re-applies in a second" : "gets the change back each time until the bound, with no report"}`, async ({
      page,
      visualPackage,
    }) => {
      // The clock moves only through runFor, which runs the page's timers one by one.
      await page.clock.install({ time: START });
      await page.clock.pauseAt(new Date(START.getTime() + 1000));
      const body = `<h1 id="t">Original</h1>
<script>
window.__seen = [];
setInterval(function () {
  var t = document.getElementById("t");
  window.__seen.push(t.textContent);
  t.textContent = "Original";
}, ${every});
</script>`;
      const site = await routeSite(page, visualPackage, {
        pages: { "/": sitePage(body) },
        answer: answering({ experiments: [experiment("reset", [{ op: "set_text", selector: "#t", text: "Changed" }])] }),
      });
      await page.goto(`${SITE}/`, { waitUntil: "load" });
      await expect(page.locator("#t")).toHaveText("Changed");
      const seen = () => page.evaluate(() => (window as unknown as { __seen: string[] }).__seen.slice());
      if (capped) {
        await page.clock.runFor(2000);
        await expect.poll(() => reports(site).length, { message: "reports" }).toBe(1);
        expect(reports(site)[0].metadata).toEqual({ index: 0, reason: "reapply_limit" });
        expect((await seen()).slice(0, 21), "what the first 21 resets found").toEqual(Array(21).fill("Changed"));
        await expect(page.locator("#t")).toHaveText("Original");
      } else {
        // The bound is 5 s after the load; stop just before it.
        await page.clock.runFor(4900);
        const found = await seen();
        expect(found.length, "resets inside the bound").toBe(Math.floor(4900 / every));
        expect(found.filter((text) => text !== "Changed"), "resets that found the change gone").toEqual([]);
        await expect(page.locator("#t")).toHaveText("Changed");
        expect(reports(site), "reports").toEqual([]);
      }
    });
  }

  test("25 elements a re-render replaces at once all get their change back, none reported", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(TARGETS) },
      answer: answering({ experiments: [experiment("many", ON_EACH)] }),
    });
    await page.goto(`${SITE}/`, { waitUntil: "load" });
    await expect(page.locator("#t-25")).toHaveText("Changed");
    await replaceAll(page, "p");
    expect(await texts(page, 25), "after one re-render").toEqual(Array(25).fill("Changed"));
    await replaceAll(page, "p");
    expect(await texts(page, 25), "after a second re-render").toEqual(Array(25).fill("Changed"));
    expect(reports(site), "reports").toEqual([]);
  });
});

test.describe("after the bound", () => {
  // A request a page makes as it goes away never reaches page.route, so these
  // pages, the script and the API come from a real server on 127.0.0.1, at
  // VISUAL_KEEP_PORT (4600 by default). The next page has no script.
  const PORT = Number(process.env.VISUAL_KEEP_PORT ?? 4600);
  const ORIGIN = `http://127.0.0.1:${PORT}`;
  let server: Server;
  let script = "";
  let body = "";
  let changes: unknown[] = [];
  let received: Report[] = [];

  function handle(request: IncomingMessage, response: ServerResponse, text: string): void {
    const path = new URL(request.url ?? "/", ORIGIN).pathname;
    const send = (status: number, type: string, content: string) => {
      response.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
      response.end(content);
    };
    if (path === "/") return send(200, "text/html; charset=utf-8", html(snippet({ "data-api-url": ORIGIN, "data-editor-origin": null }), body));
    if (path === "/next") return send(200, "text/html; charset=utf-8", "<!doctype html><title>next</title><p>next</p>");
    if (path === "/controlvar-visual.js") return send(200, "text/javascript", script);
    if (path === VISUAL_PATH) {
      const pages = { host: "127.0.0.1", rules: [{ match: "prefix", path: "/" }], max_age: 60 };
      return send(200, "application/json", JSON.stringify({ experiments: [experiment("lost", changes)], pages }));
    }
    if (path === ERRORS_PATH) {
      received.push(JSON.parse(text) as Report);
      return send(201, "application/json", "{}");
    }
    send(404, "text/plain", "not found");
  }

  test.beforeAll(async ({ visualPackage }) => {
    script = visualPackage.classicScript;
    server = createServer((request, response) => {
      let text = "";
      request.on("data", (chunk: Buffer) => (text += chunk.toString("utf8")));
      request.on("end", () => handle(request, response, text));
    });
    await new Promise<void>((resolve) => server.listen(PORT, "127.0.0.1", resolve));
    expect((server.address() as AddressInfo).port).toBe(PORT);
  });

  test.afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  async function pastTheBound(page: Page, pageBody: string, pageChanges: unknown[]) {
    body = pageBody;
    changes = pageChanges;
    received = [];
    await page.clock.install({ time: START });
    await page.goto(`${ORIGIN}/`, { waitUntil: "load" });
    await expect(page.locator(pageBody.includes('id="t-1"') ? "#t-1" : "#t")).toHaveText("Changed");
    await page.clock.runFor(6000);
  }

  test("an element replaced after the bound is reported once when the page goes away", async ({ page }) => {
    await pastTheBound(page, '<h1 id="t">Original</h1><p id="p">Original</p>', [
      { op: "set_text", selector: "#t", text: "Changed" },
      { op: "set_text", selector: "#p", text: "Changed" },
    ]);
    await replaceAll(page, "#t");
    expect(received, "reports before the page goes away").toEqual([]);
    await page.goto(`${ORIGIN}/next`, { waitUntil: "load" });
    await expect.poll(() => received.length).toBe(1);
    expect(received[0]).toEqual({
      error_type: "visual_change_not_applied",
      experiment_key: "lost",
      message: "A visual change was lost: the page replaced the element after the script stopped watching.",
      metadata: { index: 0, reason: "replaced_after_bound" },
    });
  });

  test("a tab going hidden reports it too, once, and 25 replaced elements give five reports", async ({ page }) => {
    await pastTheBound(page, TARGETS, ON_EACH);
    const hide = () =>
      page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
        document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
      });
    await hide();
    expect(received, "nothing replaced yet").toEqual([]);
    await replaceAll(page, "#t-3");
    await hide();
    await hide();
    await expect.poll(() => received.length).toBe(1);
    expect(received[0].metadata).toEqual({ index: 2, reason: "replaced_after_bound" });
    await replaceAll(page, "p");
    await page.goto(`${ORIGIN}/next`, { waitUntil: "load" });
    await expect.poll(() => received.length).toBe(5);
    expect(received.map((report) => report.metadata.index)).toEqual([2, 0, 1, 3, 4]);
  });
});
