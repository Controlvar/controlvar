import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Page } from "@playwright/test";
import { expect, test } from "./runtime.fixture";
import { API_KEY, html, routeSite, SITE } from "./editor";
import { answering, calls, experiment, reports, shown, sitePage, VISUAL_PATH } from "./visual";

/**
 * A site with a strict Content-Security-Policy (#1545): every change and the
 * hide apply with no policy violation, also under Trusted Types, and the
 * documented snippet (sdk/visual/snippet.html) loads there as it is.
 */

const STRICT = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; object-src 'none'";
const TRUSTED_TYPES = `${STRICT}; require-trusted-types-for 'script'; trusted-types 'none'`;

const BODY = `<h1 id="t">Original</h1><a id="link" href="/old">Link</a><picture><source srcset="/a.webp"><img id="pic" src="/a.png" alt="A"></picture>
<p id="gone">Visible</p><p id="styled">Plain</p>`;

const EVERY_OP = [
  { op: "set_text", selector: "#t", text: "Changed" },
  { op: "set_attribute", selector: "#link", name: "href", value: "/new" },
  { op: "set_attribute", selector: "#link", name: "title", value: "New" },
  { op: "set_image", selector: "#pic", src: "/b.png", alt: "B" },
  { op: "hide", selector: "#gone" },
  { op: "set_style", selector: "#styled", styles: { color: "rgb(1, 2, 3)", "margin-top": "10px", "font-weight": "700" } },
];

async function recordViolations(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const violations: string[] = [];
    (window as unknown as { __violations: string[] }).__violations = violations;
    document.addEventListener("securitypolicyviolation", (event) => {
      violations.push(`${event.violatedDirective} ${event.blockedURI}`);
    });
  });
}

const violations = (page: Page) => page.evaluate(() => (window as unknown as { __violations: string[] }).__violations);

for (const [name, policy] of [
  ["a strict policy", STRICT],
  ["a strict policy with Trusted Types", TRUSTED_TYPES],
] as const) {
  test(`every change and the hide apply under ${name}, with no violation`, async ({ page, visualPackage }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await recordViolations(page);
    const site = await routeSite(page, visualPackage, {
      pages: { "/": sitePage(BODY) },
      pageHeaders: { "Content-Security-Policy": policy },
      files: {
        "/a.png": { body: Buffer.from(""), contentType: "image/png" },
        "/b.png": { body: Buffer.from(""), contentType: "image/png" },
      },
      answer: answering({ experiments: [experiment("every-op", EVERY_OP)] }),
    });
    await page.goto(`${SITE}/`);
    await expect.poll(() => calls(site, VISUAL_PATH).length).toBe(1);
    await expect.poll(() => shown(page)).toBe(true);
    await page.waitForLoadState("load");
    expect(await violations(page), "violations").toEqual([]);
    expect(errors, "page errors").toEqual([]);
    await expect(page.locator("#t")).toHaveText("Changed");
    expect(await page.locator("#link").getAttribute("href")).toBe("/new");
    expect(await page.locator("#pic").getAttribute("src")).toBe("/b.png");
    expect(await page.locator("picture source").count()).toBe(0);
    expect(await page.locator("#gone").evaluate((el) => getComputedStyle(el).display)).toBe("none");
    expect(await page.locator("#styled").evaluate((el) => getComputedStyle(el).color)).toBe("rgb(1, 2, 3)");
    expect(reports(site)).toEqual([]);
  });
}

test("the documented snippet loads under a strict policy and, waiting for consent, sends nothing", async ({ page, visualPackage }) => {
  const file = readFileSync(join(__dirname, "..", "..", "..", "..", "sdk", "visual", "snippet.html"), "utf8").trim();
  expect(file).toContain("data-require-consent");
  const tag = file
    .replace("https://controlvar.example.com/controlvar-visual.js", "/controlvar-visual.js")
    .replace("https://api.controlvar.example.com", SITE)
    .replace("YOUR_WEBSITE_KEY", API_KEY);
  expect(tag, "the snippet's placeholders").not.toContain("example.com");
  await recordViolations(page);
  const site = await routeSite(page, visualPackage, {
    pages: { "/": html(tag, BODY) },
    pageHeaders: { "Content-Security-Policy": STRICT },
    answer: answering({ experiments: [experiment("every-op", EVERY_OP)] }),
  });
  await page.goto(`${SITE}/`);
  await page.waitForLoadState("load");
  const version = await page.evaluate(() => (window as unknown as { controlvar: { visual: { version: string } } }).controlvar.visual.version);
  expect(version).toBe(visualPackage.version);
  expect(await violations(page), "violations").toEqual([]);
  expect(site.api, "requests before consent").toEqual([]);
  await expect(page.locator("#t")).toHaveText("Original");
});
