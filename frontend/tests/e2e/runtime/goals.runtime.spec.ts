import type { Page } from "@playwright/test";
import { routeSite, SITE, type Site } from "./editor";
import { expect, test } from "./runtime.fixture";
import { answering, BATCH_PATH, calls, ERRORS_PATH, experiment, sitePage, VISUAL_PATH } from "./visual";

/**
 * Goals in a real browser (#1545): which page views and clicks the packed
 * script counts, from the goals the answer names. Every request is answered
 * by page.route; each goal event is one entry of a /api/v1/tracking/batch
 * call, and its event_id is "<page-view id>:<event name>".
 */

const BODY =
  '<button id="buy"><span id="label">Buy</span></button><p id="outside">Text</p><a class="twice">1</a><a class="twice">2</a>';

interface GoalEvent {
  event_type: string;
  user_id: string;
  experiment_key: string;
  event_id: string;
  metadata?: Record<string, string>;
}

const clickGoal = (name: string, selector: string, path = "/") => ({ event_name: name, kind: "click", selector, page: { match: "prefix", url: `${SITE}${path}` } });
const viewGoal = (name: string, path: string) => ({ event_name: name, kind: "page_view", page: { match: "prefix", url: `${SITE}${path}` } });

function events(site: Site): GoalEvent[] {
  return calls(site, BATCH_PATH).flatMap((call) => (call.body as { events: GoalEvent[] }).events);
}

const named = (site: Site, name: string) => events(site).filter((event) => event.event_type === name).length;

/** Settles the page: its timers and the answers to its requests. */
async function settle(page: Page): Promise<void> {
  await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 50)));
}

test.describe("page_view goals", () => {
  test("one per page load and per path change: a hash, a query and Next's pushState then replaceState add none", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/shop": sitePage(BODY), "/shop/b": sitePage(BODY) },
      answer: answering({ experiments: [experiment("shop", [], { goals: [viewGoal("page_shop", "/shop"), viewGoal("page_cart", "/cart")] })] }),
    });
    const counts: string[] = [];
    let before = 0;
    const step = async (name: string, act: () => Promise<unknown>) => {
      await act();
      await settle(page);
      const now = named(site, "page_shop");
      counts.push(`${name}: ${now - before}`);
      before = now;
    };
    await step("load", () => page.goto(`${SITE}/shop`, { waitUntil: "load" }));
    await step("pushState", () => page.evaluate(() => history.pushState(null, "", "/shop/a")));
    await step("next.js nav", () =>
      page.evaluate(() => {
        history.pushState(null, "", "/shop/b");
        history.replaceState(null, "", "/shop/b");
      }),
    );
    await step("hash", () => page.evaluate(() => (location.hash = "details")));
    await step("reload", () => page.reload({ waitUntil: "load" }));
    await step("query", () => page.evaluate(() => history.pushState(null, "", "/shop/b?sort=price")));
    counts.push(`a rule that does not match: ${named(site, "page_cart")}`);
    expect(counts).toEqual(["load: 1", "pushState: 1", "next.js nav: 1", "hash: 0", "reload: 1", "query: 0", "a rule that does not match: 0"]);
    const ids = events(site).map((event) => event.event_id.split(":")[0]);
    expect(new Set(ids).size, "one page-view id per page view").toBe(ids.length);
  });
});

test.describe("click goals", () => {
  test("a click a person makes on the element or inside it counts, and Enter and Space on it; a click from a script does not", async ({
    page,
    visualPackage,
  }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/shop": sitePage(BODY) },
      answer: answering({ experiments: [experiment("shop", [], { goals: [clickGoal("click_buy", "#buy")] })] }),
    });
    await page.goto(`${SITE}/shop`, { waitUntil: "load" });
    await settle(page);
    const counts: string[] = [];
    let before = 0;
    let view = 0;
    const step = async (name: string, act: () => Promise<unknown>) => {
      // Each in a page view of its own: a goal counts at most once per page view.
      view += 1;
      await page.evaluate((path) => history.pushState(null, "", path), `/shop/${view}`);
      await settle(page);
      await act();
      await settle(page);
      const now = named(site, "click_buy");
      counts.push(`${name}: ${now - before}`);
      before = now;
    };
    await step("outside", () => page.click("#outside"));
    await step("child span", () => page.click("#label"));
    await step("twice in one page view", async () => {
      await page.click("#buy");
      await page.click("#label");
    });
    await step("Enter", async () => {
      await page.focus("#buy");
      await page.keyboard.press("Enter");
    });
    await step("Space", async () => {
      await page.focus("#buy");
      await page.keyboard.press("Space");
    });
    await step("el.click()", () => page.evaluate(() => (document.getElementById("buy") as HTMLButtonElement).click()));
    await step("dispatchEvent", () =>
      page.evaluate(() => document.getElementById("label")!.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }))),
    );
    expect(counts).toEqual(["outside: 0", "child span: 1", "twice in one page view: 1", "Enter: 1", "Space: 1", "el.click(): 0", "dispatchEvent: 0"]);
    for (const event of events(site)) {
      expect(Object.keys(event), "the event's fields").toEqual(["event_type", "user_id", "experiment_key", "event_id", "metadata"]);
    }
    expect(calls(site, ERRORS_PATH), "reports").toEqual([]);
  });

  test("a selector that matches no element or two counts nothing, and is reported once each", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/shop": sitePage(BODY) },
      answer: answering({ experiments: [experiment("shop", [], { goals: [clickGoal("click_none", "#missing"), clickGoal("click_many", ".twice")] })] }),
    });
    await page.goto(`${SITE}/shop`, { waitUntil: "load" });
    await settle(page);
    await page.click(".twice >> nth=0");
    await page.click("#buy");
    await settle(page);
    expect(events(site), "goal events").toEqual([]);
    await expect.poll(() => calls(site, ERRORS_PATH).length).toBe(2);
    const sent = calls(site, ERRORS_PATH).map((call) => call.body as { error_type: string; metadata: unknown });
    expect(sent.map((report) => [report.error_type, report.metadata]).sort()).toEqual([
      ["goal_ambiguous", { index: 1 }],
      ["goal_not_found", { index: 0 }],
    ]);
  });
});

test.describe("frames and shadow roots", () => {
  test("a goal's element inside a frame or a shadow root is not found: its clicks count nothing, and each goal is reported", async ({
    page,
    visualPackage,
  }) => {
    const body = `${BODY}<iframe id="frame" srcdoc="<button id='framed'>In a frame</button>"></iframe><div id="host"></div>
<script>document.getElementById("host").attachShadow({ mode: "open" }).innerHTML = "<button id='shadowed'>In a shadow root</button>";</script>`;
    const site = await routeSite(page, visualPackage, {
      pages: { "/shop": sitePage(body) },
      answer: answering({ experiments: [experiment("shop", [], { goals: [clickGoal("click_framed", "#framed"), clickGoal("click_shadowed", "#shadowed")] })] }),
    });
    await page.goto(`${SITE}/shop`, { waitUntil: "load" });
    await settle(page);
    await page.frameLocator("#frame").locator("#framed").click();
    await page.locator("#shadowed").click();
    await settle(page);
    expect(events(site), "goal events").toEqual([]);
    await expect.poll(() => calls(site, ERRORS_PATH).length).toBe(2);
    const sent = calls(site, ERRORS_PATH).map((call) => call.body as { error_type: string; metadata: unknown });
    expect(sent.map((report) => [report.error_type, report.metadata]).sort()).toEqual([
      ["goal_not_found", { index: 0 }],
      ["goal_not_found", { index: 1 }],
    ]);
  });
});

/** Counts the click listeners added to the document in the capture phase; installed before any script. */
function countClickListeners() {
  const native = EventTarget.prototype.addEventListener;
  (window as unknown as { __clickListeners: number }).__clickListeners = 0;
  EventTarget.prototype.addEventListener = function (this: EventTarget, type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | AddEventListenerOptions) {
    const capture = options === true || (typeof options === "object" && options !== null && options.capture === true);
    if (this === document && type === "click" && capture) (window as unknown as { __clickListeners: number }).__clickListeners += 1;
    return native.call(this, type, listener, options);
  };
}

test.describe("the click listener", () => {
  test("is added once: three path changes, a popstate and consent() add none, and one click counts once for each assigned experiment", async ({
    page,
    visualPackage,
  }) => {
    await page.addInitScript(countClickListeners);
    const site = await routeSite(page, visualPackage, {
      pages: { "/shop": sitePage(BODY) },
      answer: answering({
        experiments: [
          experiment("first", [], { goals: [clickGoal("click_buy", "#buy")] }),
          experiment("second", [], { goals: [clickGoal("click_buy_too", "#buy")] }),
          experiment("third", [], { assigned: false, reason: "targeting", changes: null, goals: [] }),
        ],
      }),
    });
    await page.goto(`${SITE}/shop`, { waitUntil: "load" });
    for (const path of ["/shop/1", "/shop/2", "/shop/3"]) {
      await page.evaluate((next) => history.pushState(null, "", next), path);
      await settle(page);
    }
    await page.evaluate(() => {
      history.back();
    });
    await settle(page);
    await page.evaluate(() => (window as unknown as { controlvar: { visual: { consent(): void } } }).controlvar.visual.consent());
    await settle(page);
    const listeners = await page.evaluate(() => (window as unknown as { __clickListeners: number }).__clickListeners);
    expect(`listeners ${listeners}`).toBe("listeners 1");
    await page.click("#label");
    await settle(page);
    expect(events(site).map((event) => [event.experiment_key, event.event_type])).toEqual([
      ["first", "click_buy"],
      ["second", "click_buy_too"],
    ]);
  });
});

test.describe("the goals kept between pages", () => {
  test("count on a later page of the same origin only: a page on another port, subdomain or scheme records none", async ({
    page,
    visualPackage,
  }) => {
    const others: [string, string][] = [
      ["another port", "http://localhost:4101"],
      ["another subdomain", "http://www.localhost:4100"],
      ["another scheme", "https://localhost:4100"],
    ];
    const site = await routeSite(page, visualPackage, {
      pages: { "/shop": sitePage(BODY), "/thanks": sitePage("<p>Thanks</p>") },
      answer: answering({
        rules: [{ match: "exact", path: "/shop" }],
        experiments: [
          experiment("shop", [], {
            goals: [
              { event_name: "page_thanks", kind: "page_view", page: { match: "exact", url: `${SITE}/thanks` } },
              { event_name: "page_thanks_www", kind: "page_view", page: { match: "exact", url: "http://www.localhost:4100/thanks" } },
            ],
          }),
        ],
      }),
    });
    // Each other origin serves the same page and script, with the API on its own origin; no experiment covers its page.
    const elsewhere: string[] = [];
    for (const [, origin] of others) {
      await page.route(`${origin}/**`, async (route) => {
        const { pathname } = new URL(route.request().url());
        if (pathname === "/controlvar-visual.js") {
          await route.fulfill({ contentType: "text/javascript", body: visualPackage.classicScript });
        } else if (!pathname.startsWith("/api/")) {
          await route.fulfill({ contentType: "text/html", body: sitePage("<p>Thanks</p>", { "data-api-url": origin }) });
        } else {
          elsewhere.push(`${origin}${pathname}`);
          const pages = { host: new URL(origin).hostname, rules: [{ match: "exact", path: "/shop" }], max_age: 60 };
          await route.fulfill({ contentType: "application/json", body: JSON.stringify(pathname === VISUAL_PATH ? { experiments: [], pages } : {}) });
        }
      });
    }
    await page.goto(`${SITE}/shop`, { waitUntil: "load" });
    await expect
      .poll(() => page.evaluate(() => Object.keys(JSON.parse(localStorage.getItem("controlvar_goals") ?? "{}"))), { message: "the goals kept" })
      .toEqual(["shop"]);
    const counts: string[] = [];
    for (const [name, origin] of others) {
      await page.goto(`${origin}/thanks`, { waitUntil: "load" });
      await expect.poll(() => elsewhere.includes(`${origin}${VISUAL_PATH}`), { message: `the page call on ${name}` }).toBe(true);
      await settle(page);
      counts.push(`${name}: ${elsewhere.filter((call) => call === `${origin}${BATCH_PATH}`).length}`);
    }
    await page.goto(`${SITE}/thanks`, { waitUntil: "load" });
    await expect.poll(() => named(site, "page_thanks"), { message: "the goal on the same origin" }).toBeGreaterThan(0);
    await settle(page);
    counts.push(`the same origin: ${named(site, "page_thanks")}`);
    expect(counts).toEqual(["another port: 0", "another subdomain: 0", "another scheme: 0", "the same origin: 1"]);
    expect(elsewhere, "each other origin made its page call and nothing else").toEqual(others.map(([, origin]) => `${origin}${VISUAL_PATH}`));
  });
});
