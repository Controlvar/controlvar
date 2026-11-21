import { expect, test } from "./runtime.fixture";
import { API_KEY, routeSite, SITE } from "./editor";
import { answering, BATCH_PATH, calls, ERRORS_PATH, experiment, reports, shown, sitePage, VISUAL_PATH } from "./visual";

/**
 * An ordinary page view (#1545): the script asks for the page's visual
 * changes, checks them and applies them through text-only writes, refuses
 * the elements it never changes, and reports what it could not apply.
 * Every page is a fresh navigation.
 */

const BODY = `<h1 id="t">Original</h1><p class="many">1</p><p class="many">2</p><p class="many">3</p>
<a id="link" href="/old">Link</a><img id="pic" src="/a.png" alt="A">`;

test.describe("applying", () => {
  test("text is written as text: markup in a change stays text and runs nothing", async ({ page, visualPackage }) => {
    const imageRequests: string[] = [];
    await page.route(`${SITE}/pwned.png`, async (route) => {
      imageRequests.push(route.request().url());
      await route.fulfill({ status: 404, body: "" });
    });
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY) },
      answer: answering({
        experiments: [experiment("markup", [{ op: "set_text", selector: "#t", text: "<img src=/pwned.png onerror=window.__pwned=1>" }])],
      }),
    });
    await page.goto(`${SITE}/`);
    await expect.poll(() => calls(site, VISUAL_PATH).length).toBe(1);
    await expect.poll(() => shown(page)).toBe(true);
    await page.waitForLoadState("load");
    // Every image the page holds has loaded or failed, and its handlers have run.
    await page.waitForFunction(() => Array.from(document.images).every((image) => image.complete));
    await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned === 1), "canary").toBe(false);
    expect(imageRequests).toEqual([]);
    expect(await page.locator("#t > *").count(), "child elements of #t").toBe(0);
    await expect(page.locator("#t")).toHaveText("<img src=/pwned.png onerror=window.__pwned=1>");
    expect(reports(site)).toEqual([]);
  });

  test("every operation applies, with the key and nothing about the page but its host and path", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/a/b": sitePage(BODY) },
      answer: answering({
        experiments: [
          experiment("every-op", [
            { op: "set_text", selector: "#t", text: "Changed" },
            { op: "set_attribute", selector: "#link", name: "href", value: "/new" },
            { op: "set_image", selector: "#pic", src: "/b.png", alt: "B" },
            { op: "hide", selector: ".many:nth-of-type(1)" },
            { op: "set_style", selector: "#t", styles: { color: "rgb(1, 2, 3)" } },
          ]),
        ],
      }),
    });
    await page.goto(`${SITE}/a/b?email=jane%40shop.example#x`);
    await expect(page.locator("#t")).toHaveText("Changed");
    await expect.poll(() => shown(page)).toBe(true);
    expect(await page.locator("#link").getAttribute("href")).toBe("/new");
    expect(await page.locator("#pic").getAttribute("src")).toBe("/b.png");
    expect(await page.locator("#pic").getAttribute("alt")).toBe("B");
    expect(await page.locator(".many").first().evaluate((el) => getComputedStyle(el).display)).toBe("none");
    expect(await page.locator("#t").evaluate((el) => getComputedStyle(el).color)).toBe("rgb(1, 2, 3)");
    const [ask] = calls(site, VISUAL_PATH);
    expect(Object.keys(ask.body as object).sort()).toEqual(["format_version", "host", "path", "user_id"]);
    expect(ask.body).toMatchObject({ host: "localhost:4100", path: "/a/b", format_version: 1 });
    expect(JSON.stringify(ask.body)).not.toContain("jane");
    expect(ask.headers["x-api-key"]).toBe(API_KEY);
    expect(reports(site)).toEqual([]);
  });

  test("an element the script never changes is refused, and reported", async ({ page, visualPackage }) => {
    const evil: string[] = [];
    await page.route("https://evil.test/**", async (route) => {
      evil.push(route.request().url());
      await route.abort();
    });
    const body = `<div id="app"></div>
<script id="tpl" type="text/x-template"><b>template</b></script>
<iframe id="frame" src="about:blank"></iframe><object id="obj"></object><embed id="emb">
<svg><use id="use" href="#x"></use></svg><template id="tm"><p>t</p></template>
<p id="ok">Original</p>
<script>window.__render = function () { document.getElementById("app").innerHTML = document.getElementById("tpl").textContent; };</script>`;
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(body, {}, '<link id="css" rel="stylesheet" href="/a.css"><style id="st">p{}</style>') },
      answer: answering({
        experiments: [
          experiment("guard", [
            { op: "set_text", selector: "#tpl", text: '<img src="https://evil.test/tpl.png">' },
            { op: "set_image", selector: "#frame", src: "https://evil.test/frame.png" },
            { op: "set_image", selector: "#obj", src: "https://evil.test/obj.png" },
            { op: "set_image", selector: "#emb", src: "https://evil.test/emb.png" },
            { op: "set_attribute", selector: "#css", name: "href", value: "https://evil.test/x.css" },
            { op: "set_attribute", selector: "#use", name: "href", value: "https://evil.test/sprite.svg" },
            { op: "set_text", selector: "#st", text: "p{background:url(https://evil.test/s.png)}" },
            { op: "set_text", selector: "#tm", text: "x" },
            { op: "set_attribute", selector: "#ok", name: "href", value: "https://evil.test/p" },
            { op: "set_text", selector: "#ok", text: "Changed" },
          ]),
        ],
      }),
    });
    await page.goto(`${SITE}/`);
    await expect(page.locator("#ok")).toHaveText("Changed");
    await expect.poll(() => shown(page)).toBe(true);
    await page.evaluate(() => (window as unknown as { __render(): void }).__render());
    await page.waitForLoadState("load");
    await page.waitForFunction(() => Array.from(document.images).every((image) => image.complete));
    expect(evil, "requests to evil.test").toEqual([]);
    await expect.poll(() => reports(site).length).toBe(5);
    // Nine changes are refused; the first five refusals are reported (the cap),
    // in the order the page's elements arrived.
    const reported = reports(site).map((report) => report.metadata);
    expect(reported.every((metadata) => metadata.reason === "not_allowed")).toBe(true);
    expect(new Set(reported.map((metadata) => metadata.index)).size).toBe(5);
    expect(reported.every((metadata) => (metadata.index as number) <= 8)).toBe(true);
    const untouched = await page.evaluate(() => ({
      tpl: document.getElementById("tpl")!.textContent,
      css: document.getElementById("css")!.getAttribute("href"),
      use: document.getElementById("use")!.getAttribute("href"),
      style: document.getElementById("st")!.textContent,
      okHref: document.getElementById("ok")!.getAttribute("href"),
      frame: document.getElementById("frame")!.getAttribute("src"),
    }));
    expect(untouched).toEqual({ tpl: "<b>template</b>", css: "/a.css", use: "#x", style: "p{}", okHref: null, frame: "about:blank" });
  });

  test("an unknown operation skips the whole experiment and pollutes nothing", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY) },
      answer: answering({
        experiments: [
          experiment("unknown", [
            { op: "set_text", selector: "#t", text: "Changed" },
            { op: "set_html", selector: "#t", html: "<b>x</b>" },
          ]),
        ],
      }),
    });
    await page.goto(`${SITE}/`);
    await expect.poll(() => reports(site).length).toBe(1);
    await expect.poll(() => shown(page)).toBe(true);
    await expect(page.locator("#t")).toHaveText("Original");
    expect(reports(site)[0]).toEqual({
      error_type: "visual_change_not_applied",
      experiment_key: "unknown",
      message: "A visual change was not applied: this version of the script does not know its operation.",
      metadata: { index: null, reason: "unsupported_operation" },
    });
    expect(await page.evaluate(() => (({}) as Record<string, unknown>).polluted)).toBeUndefined();
  });
});

test.describe("matching", () => {
  test("a selector that matches nothing changes nothing, shows the page and is reported", async ({ page, visualPackage }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const base = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY) },
      answer: answering({ experiments: [] }),
    });
    await page.goto(`${SITE}/`);
    await expect.poll(() => shown(page)).toBe(true);
    const before = await page.evaluate(() => document.body.innerHTML);
    expect(calls(base, VISUAL_PATH)).toHaveLength(1);

    const second = await page.context().newPage();
    second.on("pageerror", (error) => errors.push(error.message));
    const site = await routeSite(second, visualPackage, {
      pages: { "/": sitePage(BODY) },
      answer: answering({
        experiments: [
          experiment("miss", [
            { op: "set_text", selector: "#nothing-here", text: "Changed" },
            { op: "set_text", selector: "div[", text: "Changed" },
          ]),
        ],
      }),
    });
    // A selector that matches nothing is reported at the bound (load + 5 s), on the page clock.
    await second.clock.install({ time: new Date("2026-11-16T12:00:00Z") });
    await second.goto(`${SITE}/`, { waitUntil: "load" });
    await expect.poll(() => shown(second)).toBe(true);
    await expect.poll(() => reports(site).length).toBe(1);
    expect(reports(site)[0].metadata, "reported at once").toEqual({ index: 1, reason: "invalid" });
    await second.clock.runFor(6000);
    await expect.poll(() => reports(site).length).toBe(2);
    expect(await second.evaluate(() => document.body.innerHTML)).toBe(before);
    expect(reports(site)[1].metadata, "reported at the bound").toEqual({ index: 0, reason: "not_found" });
    expect(errors).toEqual([]);
  });

  test("a selector that matches three elements changes none of them", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY) },
      answer: answering({ experiments: [experiment("three", [{ op: "set_text", selector: ".many", text: "Changed" }])] }),
    });
    await page.goto(`${SITE}/`);
    await expect.poll(() => calls(site, VISUAL_PATH).length).toBe(1);
    await expect.poll(() => shown(page)).toBe(true);
    expect(await page.locator(".many").allTextContents(), "changed").toEqual(["1", "2", "3"]);
    await expect.poll(() => reports(site).length).toBe(1);
    expect(reports(site)[0].metadata).toEqual({ index: 0, reason: "ambiguous" });
  });
});

test.describe("requests", () => {
  test("carry no Referer and no cookie, even from a page that asks for unsafe-url", async ({ page, visualPackage, context }) => {
    await context.addCookies([{ name: "site_session", value: "secret-value", url: SITE }]);
    const site = await routeSite(page, visualPackage, {
      pages: { "/a/b/page": sitePage(BODY, {}, '<meta name="referrer" content="unsafe-url">') },
      answer: answering({ experiments: [experiment("bad", [{ op: "set_text", selector: "#nothing", text: "x" }])] }),
    });
    await page.goto(`${SITE}/a/b/page?email=jane%40shop.example`);
    await expect.poll(() => reports(site).length).toBe(1);
    await page.evaluate(() => (window as unknown as { controlvar: { visual: { track(e: string): void } } }).controlvar.visual.track("purchase"));
    await expect.poll(() => calls(site, BATCH_PATH).length).toBe(1);
    const sent = [VISUAL_PATH, ERRORS_PATH, BATCH_PATH].map((path) => calls(site, path)[0]);
    for (const call of sent) {
      expect(call.headers.referer, `${call.path}: referer`).toBeUndefined();
      expect(call.headers.cookie, `${call.path}: cookie`).toBeUndefined();
    }
    expect(await page.evaluate(() => document.cookie)).toContain("site_session=secret-value");
  });

  test("after a 403 the script sends nothing more in the tab, says why once, and stops hiding", async ({ page, visualPackage }) => {
    const warnings: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "warning") warnings.push(message.text());
    });
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY) },
      answer: answering({ experiments: [], status: 403 }),
    });
    await page.goto(`${SITE}/`);
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem("controlvar_backoff"))).not.toBeNull();
    await page.evaluate(() => (window as unknown as { controlvar: { visual: { track(e: string): void } } }).controlvar.visual.track("purchase"));
    await page.reload();
    expect(await shown(page)).toBe(true);
    expect(calls(site, ERRORS_PATH), "errors calls").toHaveLength(0);
    expect(calls(site, BATCH_PATH), "batch calls").toHaveLength(0);
    expect(calls(site, VISUAL_PATH)).toHaveLength(1);
    expect(warnings.filter((text) => text.includes("401 or 403"))).toHaveLength(1);
  });

  test("fresh page rules that cover nothing: the next page makes no call", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/a": sitePage(BODY), "/b": sitePage(BODY) },
      answer: answering({ experiments: [], rules: [{ match: "exact", path: "/campaign" }] }),
    });
    await page.goto(`${SITE}/a`);
    await expect.poll(() => page.evaluate(() => localStorage.getItem("controlvar_pages"))).not.toBeNull();
    await page.goto(`${SITE}/b`);
    expect(await shown(page)).toBe(true);
    expect(calls(site, VISUAL_PATH).map((call) => (call.body as { path: string }).path)).toEqual(["/a"]);
  });

  test("at most five reports per page view", async ({ page, visualPackage }) => {
    const misses = Array.from({ length: 8 }, (_, index) => ({ op: "hide", selector: `#missing-${index}` }));
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY) },
      answer: answering({ experiments: [experiment("misses", misses)] }),
    });
    await page.clock.install({ time: new Date("2026-11-16T12:00:00Z") });
    await page.goto(`${SITE}/`, { waitUntil: "load" });
    await page.clock.runFor(6000);
    await expect.poll(() => reports(site).length).toBe(5);
    expect(reports(site).map((report) => report.metadata.index)).toEqual([0, 1, 2, 3, 4]);
  });

  test("track() sends one batch call naming the experiment this visitor was assigned to", async ({ page, visualPackage }) => {
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY) },
      answer: answering({ experiments: [experiment("headline", [{ op: "set_text", selector: "#t", text: "Changed" }])] }),
    });
    await page.goto(`${SITE}/`);
    await expect(page.locator("#t")).toHaveText("Changed");
    await page.evaluate(() =>
      (window as unknown as { controlvar: { visual: { track(e: string, o: object): void } } }).controlvar.visual.track("purchase", { value: 9.5 }),
    );
    await expect.poll(() => calls(site, BATCH_PATH).length).toBe(1);
    const userId = (calls(site, VISUAL_PATH)[0].body as { user_id: string }).user_id;
    expect(calls(site, BATCH_PATH)[0].body).toEqual({
      events: [{ event_type: "purchase", user_id: userId, experiment_key: "headline", value: 9.5 }],
    });
  });
});
