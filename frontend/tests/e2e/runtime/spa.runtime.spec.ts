import type { Page } from "@playwright/test";
import { routeSite, SITE, type ApiAnswer, type ApiCall } from "./editor";
import { fixtureFiles } from "./fixtures";
import { expect, test } from "./runtime.fixture";
import { answering, calls, experiment, reports, sitePage, VISUAL_PATH, type Experiment } from "./visual";

/**
 * Single-page apps (#1545). The script wraps history.pushState and
 * replaceState and listens for popstate; each of them is one re-evaluation:
 * one read of the kept page rules (localStorage controlvar_pages) against
 * the current path. The specs count those reads through a wrapper on
 * Storage.prototype.getItem installed before any script, and count page
 * views as calls to the page's API (each call records one). A new path
 * undoes the changes and asks for its own; a change after `#` alone asks
 * nothing.
 */

/** Answers each call for the page with the experiments of its path. */
function byPath(experiments: Record<string, Experiment[]>, rules: { match: "exact" | "prefix"; path: string }[]) {
  const others = answering({ experiments: [] });
  return async (call: ApiCall): Promise<ApiAnswer> => {
    if (call.path !== VISUAL_PATH) return others(call);
    const path = (call.body as { path: string }).path;
    return { status: 200, body: { experiments: experiments[path] ?? [], pages: { host: "localhost", rules, max_age: 60 } } };
  };
}

/** Records every change to #t, so a step can say how many writes it made. */
function watchHeadline() {
  (window as unknown as { __writes: number }).__writes = 0;
  document.addEventListener("DOMContentLoaded", () => {
    new MutationObserver((records) => {
      (window as unknown as { __writes: number }).__writes += records.length;
    }).observe(document.getElementById("t")!, { attributes: true, characterData: true, childList: true, subtree: true });
  });
}

function countRuleReads() {
  const native = Storage.prototype.getItem;
  (window as unknown as { __ruleReads: number }).__ruleReads = 0;
  Storage.prototype.getItem = function (key: string) {
    if (key === "controlvar_pages") (window as unknown as { __ruleReads: number }).__ruleReads += 1;
    return native.call(this, key);
  };
}

async function headline(page: Page) {
  return page.evaluate(() => {
    const t = document.getElementById("t")!;
    return { text: t.textContent, title: t.getAttribute("title"), color: t.style.color };
  });
}

const takeWrites = (page: Page) =>
  page.evaluate(() => {
    const w = window as unknown as { __writes: number };
    const writes = w.__writes;
    w.__writes = 0;
    return writes;
  });

const CHANGED = { text: "Changed", title: "B", color: "red" };
const ORIGINAL = { text: "Original", title: "server", color: "" };

test("a pushState app: the changes follow the path through links, back and forward, and each is written once per step", async ({
  page,
  visualPackage,
}) => {
  await page.addInitScript(watchHeadline);
  const body = `<h1 id="t" title="server">Original</h1>
<nav><a id="to-a" href="/a">A</a> <a id="to-b" href="/b">B</a> <a id="to-x" href="#x">x</a></nav>
<main id="app"></main>
<script>
function render() { document.getElementById("app").textContent = "page " + location.pathname; }
document.addEventListener("click", function (event) {
  var link = event.target.closest("a[href^='/']");
  if (!link) return;
  event.preventDefault();
  history.pushState({}, "", link.getAttribute("href"));
  render();
});
addEventListener("popstate", render);
render();
</script>`;
  const site = await routeSite(page, visualPackage, {
    pages: { "/a": sitePage(body), "/b": sitePage(body) },
    answer: byPath(
      {
        "/a": [
          experiment("on-a", [
            { op: "set_text", selector: "#t", text: "Changed" },
            { op: "set_attribute", selector: "#t", name: "title", value: "B" },
            { op: "set_style", selector: "#t", styles: { color: "red" } },
          ]),
        ],
      },
      [
        { match: "exact", path: "/a" },
        { match: "exact", path: "/b" },
      ],
    ),
  });
  const asked = () => calls(site, VISUAL_PATH).map((call) => (call.body as { path: string }).path);

  await page.goto(`${SITE}/a`, { waitUntil: "load" });
  await expect.poll(() => headline(page)).toEqual(CHANGED);
  expect(await takeWrites(page), "/a: writes").toBe(3);

  const steps: [string, () => Promise<unknown>, typeof CHANGED, string[]][] = [
    ["/b", () => page.click("#to-b"), ORIGINAL, ["/a", "/b"]],
    ["/a again", () => page.click("#to-a"), CHANGED, ["/a", "/b", "/a"]],
    ["back to /b", () => page.goBack(), ORIGINAL, ["/a", "/b", "/a", "/b"]],
    ["forward to /a", () => page.goForward(), CHANGED, ["/a", "/b", "/a", "/b", "/a"]],
  ];
  for (const [name, act, state, paths] of steps) {
    await act();
    await expect.poll(asked, { message: `${name}: calls` }).toEqual(paths);
    await expect.poll(() => headline(page), { message: name }).toEqual(state);
    expect(await takeWrites(page), `${name}: writes`).toBe(3);
  }

  await page.evaluate(
    () =>
      new Promise((resolve) => {
        addEventListener("popstate", () => setTimeout(resolve, 0), { once: true });
        document.getElementById("to-x")!.click();
      }),
  );
  expect(asked(), "#x: calls").toEqual(["/a", "/b", "/a", "/b", "/a"]);
  expect(await headline(page), "#x").toEqual(CHANGED);
  expect(await takeWrites(page), "#x: writes").toBe(0);
  expect(reports(site), "reports").toEqual([]);
});

test("a Next Pages Router site: its client-side navigation is followed, back included", async ({ page, visualPackage }) => {
  const files = fixtureFiles("pages-router");
  const site = await routeSite(page, visualPackage, {
    pages: { "/": files["/index.html"].body.toString("utf8"), "/other": files["/other.html"].body.toString("utf8") },
    files,
    answer: byPath({ "/": [experiment("home", [{ op: "set_text", selector: "#t", text: "Changed" }])] }, [
      { match: "exact", path: "/" },
      { match: "exact", path: "/other" },
    ]),
  });
  const asked = () => calls(site, VISUAL_PATH).map((call) => (call.body as { path: string }).path);
  await page.goto(`${SITE}/`, { waitUntil: "load" });
  await expect(page.locator("#hydrated")).toHaveText("hydrated");
  await expect(page.locator("#t")).toHaveText("Changed");

  await page.click("#to-other");
  await expect(page.locator("#other")).toBeVisible();
  await expect.poll(asked, { message: "/other: calls" }).toEqual(["/", "/other"]);
  await expect(page.locator("#t"), "/other").toHaveText("Original");

  await page.goBack();
  await expect(page.locator("#hydrated")).toBeVisible();
  await expect.poll(asked, { message: "back to /: calls" }).toEqual(["/", "/other", "/"]);
  await expect(page.locator("#t"), "back to /").toHaveText("Changed");
  expect(reports(site), "reports").toEqual([]);
});

test.describe("a hash-routed app", () => {
  const body = `<h1 id="t">Original</h1><a id="link" href="#/y">y</a><div id="view"></div>
<script>
function show() { document.getElementById("view").textContent = location.hash; }
addEventListener("hashchange", show);
show();
</script>`;

  /** Runs *action* in the page and waits until every listener of *events* has run, and any work it deferred. */
  async function step(page: Page, action: string, events: string[]) {
    await page.evaluate(
      ([code, names]) =>
        Promise.all([
          ...names.map(
            (name) => new Promise((resolve) => addEventListener(name, () => setTimeout(resolve, 0), { once: true })),
          ),
          new Promise((resolve) => {
            new Function(code)();
            setTimeout(resolve, 0);
          }),
        ]),
      [action, events] as const,
    );
  }

  test("each kind of change after # re-evaluates exactly once, with no call and no page view", async ({ page, visualPackage }) => {
    await page.addInitScript(countRuleReads);
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(body) },
      answer: byPath({ "/": [experiment("home", [{ op: "set_text", selector: "#t", text: "Changed" }])] }, [
        { match: "exact", path: "/" },
      ]),
    });
    await page.goto(`${SITE}/`, { waitUntil: "load" });
    await expect(page.locator("#t")).toHaveText("Changed");
    const reads = () => page.evaluate(() => (window as unknown as { __ruleReads: number }).__ruleReads);
    expect(await reads(), "the page view: re-evaluations").toBe(1);
    expect(calls(site, VISUAL_PATH), "the page view: page views").toHaveLength(1);

    const kinds: [string, string, string[]][] = [
      ["location.hash", 'location.hash = "#/x"', ["popstate", "hashchange"]],
      ["a #/y link", 'document.getElementById("link").click()', ["popstate", "hashchange"]],
      ["back()", "history.back()", ["popstate", "hashchange"]],
      ["pushState(#/z)", 'history.pushState(null, "", "#/z")', []],
    ];
    for (const [name, action, events] of kinds) {
      const before = await reads();
      await step(page, action, events);
      expect((await reads()) - before, `${name}: re-evaluations`).toBe(1);
      expect(calls(site, VISUAL_PATH).length - 1, `${name}: page views`).toBe(0);
      await expect(page.locator("#t"), name).toHaveText("Changed");
    }
  });
});
