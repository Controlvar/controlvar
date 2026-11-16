import type { Page } from "@playwright/test";
import { CLAIM_URL, claimSettled, html, newTicket, routeSite, SITE, snippet } from "./editor";
import { expect, test, type VisualPackage } from "./runtime.fixture";

/**
 * After the script's first run, scripts that run later on the page (an inline
 * script right after the snippet, one at the end of the body, a deferred
 * script and one after load) read the page's address in every place below.
 */

const PATH = "/a/b?x=1";

const READ = `function __read(stage) {
  var nav = performance.getEntriesByType("navigation")[0];
  var activation = navigation.activation && navigation.activation.entry;
  (window.__reads = window.__reads || {})[stage] = {
    now: performance.now(),
    entries: navigation.entries().length,
    values: {
      "location.href": location.href,
      "document.URL": document.URL,
      "document.baseURI": document.baseURI,
      "navigation.currentEntry.url": navigation.currentEntry.url,
      "navigation.entries()": navigation.entries().map(function (e) { return e.url; }).join(" "),
      "history.state": JSON.stringify(history.state),
      "navigation entry name": nav ? nav.name : "",
      "navigation.activation.entry.url": activation ? activation.url : ""
    }
  };
}`;

const KNOWN_PAIR = ["navigation entry name", "navigation.activation.entry.url"];
const STAGES = ["inline-head", "inline-body", "deferred", "after-load"];

const PAGE = html(
  `${snippet()}<script>${READ}; __read("inline-head");</script><script defer src="/deferred.js"></script>`,
  `<script>__read("inline-body");</script>
<script>addEventListener("load", function () { setTimeout(function () { __read("after-load"); }, 0); });</script>`,
);

interface Read {
  now: number;
  entries: number;
  values: Record<string, string>;
}

type Arm = "goto" | "link click";

/** Open the page in `arm`'s way, with `fragment` after the address, and return each later script's reads. */
async function load(page: Page, visualPackage: VisualPackage, arm: Arm, fragment: string) {
  const site = await routeSite(page, visualPackage, {
    pages: { "/a/b": PAGE, "/from": html("", '<a id="go" href="/">go</a>') },
    files: { "/deferred.js": { body: '__read("deferred");', contentType: "text/javascript" } },
  });
  if (arm === "goto") {
    await page.goto(`${SITE}${PATH}${fragment}`, { waitUntil: "load" });
  } else {
    await page.goto(`${SITE}/from`, { waitUntil: "load" });
    await page.evaluate((href) => document.getElementById("go")?.setAttribute("href", href), `${PATH}${fragment}`);
    await page.click("#go");
  }
  return { site, reads: await reads(page) };
}

async function reads(page: Page): Promise<Record<string, Read>> {
  await page.waitForFunction(() => {
    const all = (window as unknown as { __reads?: Record<string, unknown> }).__reads;
    return all !== undefined && "after-load" in all;
  });
  return page.evaluate(() => (window as unknown as { __reads: Record<string, Read> }).__reads);
}

/** Per stage, the names of the APIs whose value holds `ticket`. */
function holding(all: Record<string, Read>, ticket: string): Record<string, string[]> {
  return Object.fromEntries(
    Object.keys(all).map((stage) => [
      stage,
      Object.entries(all[stage].values)
        .filter(([, value]) => value.includes(ticket))
        .map(([name]) => name)
        .sort(),
    ]),
  );
}

for (const arm of ["goto", "link click"] as const) {
  test(`later scripts find no editor link in any location they read (${arm})`, async ({
    page,
    context,
    visualPackage,
  }) => {
    const control = await load(await context.newPage(), visualPackage, arm, "");
    const ticket = newTicket();
    const { site, reads: all } = await load(page, visualPackage, arm, `#controlvar-editor=${ticket}`);

    await claimSettled(page);
    const claimStart = await page.evaluate((url) => performance.getEntriesByName(url)[0].startTime, CLAIM_URL);
    expect(claimStart, "claim request sent before the first later inline script ran").toBeLessThan(
      all["inline-head"].now,
    );
    expect(site.api.length).toBe(1);
    expect(Object.keys(site.api[0].body as object).sort()).toEqual(["claim", "ticket"]);

    const found = holding(all, ticket);
    for (const stage of STAGES) {
      for (const name of found[stage].filter((api) => !KNOWN_PAIR.includes(api))) {
        expect.soft(name, `${stage}: ${name} holds the ticket`).toBe("");
      }
      const want = control.reads[stage].entries;
      expect.soft(all[stage].entries, `${stage}: entries ${all[stage].entries} != ${want}`).toBe(want);
    }
    test.info().annotations.push({ type: "known pair", description: JSON.stringify(found) });

    await page.reload({ waitUntil: "load" });
    expect(holding(await reads(page), ticket)["after-load"], "after a reload").toEqual([]);
  });
}
