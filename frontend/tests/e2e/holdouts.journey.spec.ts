import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: the global holdout (#1455).
 *
 * `/experiments/holdouts` is loaded straight from the static export, by its
 * URL: a static page beside the dynamic `/experiments/[id]`, and this is the
 * only run of that routing before production (PE 3).
 *
 * The journey database is shared, and activating a holdout would change every
 * later journey's assignments (PE 13), so NOTHING here activates one: the
 * activate and end paths are held by Jest. The ADMIN (the seeded admin is a
 * superuser) creates one holdout that is never activated, under a stamped
 * name; it holds no user out and no other journey reads it.
 */

async function expectNoSeriousViolations(page: Page, where: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(serious, `axe on ${where}`).toEqual([]);
}

function holdoutRequests(page: Page): string[] {
  const seen: string[] = [];
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (/^\/api\/v1\/holdout(\/|$)/.test(pathname)) seen.push(`${request.method()} ${pathname}`);
  });
  return seen;
}

test.describe("Journey: the global holdout", () => {
  test("a developer loads /experiments/holdouts by URL, reads the active holdout, and never lists them all", async ({
    developerPage: page,
  }) => {
    const seen = holdoutRequests(page);
    await page.goto("/experiments/holdouts");
    await expect(page.getByTestId("holdouts-page")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("holdout-scope-note")).toContainText(
      "A global holdout keeps a share of new users out of EVERY experiment, not one",
    );
    // The journey seed activates no holdout, and no journey makes one.
    await expect(page.getByTestId("holdout-none")).toHaveText("No global holdout is active.");
    await expect(page.getByTestId("holdout-admin-only")).toHaveText(
      "Creating, activating and ending holdouts requires the ADMIN role.",
    );
    await expect(page.getByTestId("new-holdout-btn")).toHaveCount(0);
    await expectNoSeriousViolations(page, "/experiments/holdouts as a developer");

    // And from the experiments list.
    await page.goto("/experiments");
    await page.getByTestId("holdouts-link").click();
    await expect(page).toHaveURL(/\/experiments\/holdouts$/);
    await expect(page.getByTestId("holdouts-page")).toBeVisible({ timeout: 15_000 });
    await page.waitForLoadState("networkidle");
    expect(seen.filter((r) => r.endsWith("/all")), "a developer never asks for /holdout/all").toEqual([]);
    expect(seen.length, "the active holdout was read").toBeGreaterThan(0);
  });

  test("an admin creates a holdout without activating it: 5% by default, sent as 5", async ({ adminPage: page }) => {
    const name = `E2E holdout ${Date.now()}`;
    await page.goto("/experiments/holdouts");
    await expect(page.getByTestId("holdouts-page")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("new-holdout-btn").click();
    await expect(page.getByTestId("holdout-size")).toHaveValue("5");
    await expect(page.getByTestId("holdout-activate-now")).not.toBeChecked();
    await page.getByLabel("Name", { exact: true }).fill(name);
    await expectNoSeriousViolations(page, "the new holdout form");
    await page.getByTestId("holdout-submit").click();
    const row = page.getByTestId("holdout-row").filter({ hasText: name });
    await expect(row).toHaveCount(1, { timeout: 15_000 });
    await expect(row.getByTestId("holdout-status")).toHaveText("Not activated");

    const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
    const headers = { Authorization: `Bearer ${token}` };
    const all = await page.request.get(`${API_URL}/api/v1/holdout/all`, { headers });
    expect(all.status()).toBe(200);
    const stored = ((await all.json()) as { items: { name: string; holdout_percentage: number; is_active: boolean }[] }).items.find(
      (h) => h.name === name,
    );
    expect(stored).toMatchObject({ holdout_percentage: 5, is_active: false });
    // Still none active: the holdout this journey made holds nobody out.
    const active = await page.request.get(`${API_URL}/api/v1/holdout`, { headers });
    expect(await active.json()).toBeNull();
  });

  for (const role of ["analyst", "viewer"] as const) {
    test(`${role} is told who can see it, and sends no holdout request`, async ({ sessions }) => {
      const page = await (await sessions(role)).newPage();
      const seen = holdoutRequests(page);
      try {
        await page.goto("/experiments/holdouts");
        await expect(page.getByTestId("holdouts-refused")).toHaveText(
          "Seeing the global holdout requires the ADMIN or DEVELOPER role.",
          { timeout: 15_000 },
        );
        await expect(page.getByTestId("holdout-scope-note")).toBeVisible();
        await expect(page.getByTestId("holdouts-page")).toHaveCount(0);
        await page.waitForLoadState("networkidle");
        expect(seen, "no holdout request is sent").toEqual([]);
      } finally {
        await page.close();
      }
    });
  }
});
