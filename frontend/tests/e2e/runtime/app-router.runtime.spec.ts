import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import type { Page } from "@playwright/test";
import { contentType, newTicket, routeSite, SITE } from "./editor";
import { expect, test, type VisualPackage } from "./runtime.fixture";

/**
 * A site built with Next's App Router (tests/e2e/runtime/fixtures/app-router,
 * the dashboard's own next and react, a static export): opened with an editor
 * link, the address stays free of it after React hydrates, and after a reload.
 */

const OUT = join(__dirname, "fixtures", "app-router", "out");

function exportedFiles(): Record<string, { body: Buffer; contentType: string }> {
  if (!existsSync(join(OUT, "index.html"))) {
    throw new Error(
      `${OUT}/index.html does not exist: build the fixtures first (from frontend/: node tests/e2e/runtime/fixtures/build.mjs)`,
    );
  }
  const files: Record<string, { body: Buffer; contentType: string }> = {};
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) {
        walk(path);
      } else {
        const served = `/${relative(OUT, path).split("\\").join("/")}`;
        files[served] = { body: readFileSync(path), contentType: contentType(served) };
      }
    }
  };
  walk(OUT);
  return files;
}

interface NavigationLike {
  currentEntry: { url: string } | null;
  entries(): { url: string }[];
}

async function addressReads(page: Page) {
  return page.evaluate(() => {
    const navigation = (window as unknown as { navigation: NavigationLike }).navigation;
    return {
      "location.href": location.href,
      "document.URL": document.URL,
      "document.baseURI": document.baseURI,
      "navigation.currentEntry.url": navigation.currentEntry?.url ?? "",
      "navigation.entries()": navigation
        .entries()
        .map((entry) => entry.url)
        .join(" "),
      "history.state": JSON.stringify(history.state),
    };
  });
}

function holding(values: Record<string, string>, ticket: string): string[] {
  return Object.entries(values)
    .filter(([, value]) => value.includes(ticket))
    .map(([name]) => name);
}

async function openApp(page: Page, visualPackage: VisualPackage, ticket: string) {
  const files = exportedFiles();
  const index = files["/index.html"].body.toString("utf8");
  const site = await routeSite(page, visualPackage, { pages: { "/": index }, files });
  await page.goto(`${SITE}/?x=1#controlvar-editor=${ticket}`, { waitUntil: "load" });
  return site;
}

test("an App Router page keeps the editor link out of its address after hydration and after a reload", async ({
  page,
  visualPackage,
}) => {
  const ticket = newTicket();
  const site = await openApp(page, visualPackage, ticket);
  await expect(page.locator("#hydrated")).toHaveText("hydrated");

  expect(site.api.map((call) => call.path)).toEqual(["/api/v1/visual-editor/tickets/claim"]);
  expect(Object.keys(site.api[0].body as object).sort()).toEqual(["claim", "ticket"]);

  const hydrated = await addressReads(page);
  expect(hydrated["location.href"]).toBe(`${SITE}/?x=1`);
  expect(holding(hydrated, ticket), "after hydration, these hold the ticket").toEqual([]);

  await page.reload({ waitUntil: "load" });
  await expect(page.locator("#hydrated")).toHaveText("hydrated");
  const reloaded = await addressReads(page);
  expect(reloaded["location.href"]).toBe(`${SITE}/?x=1`);
  expect(holding(reloaded, ticket), "after a reload, these hold the ticket").toEqual([]);
});
