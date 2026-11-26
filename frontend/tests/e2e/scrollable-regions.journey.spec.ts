import AxeBuilder from "@axe-core/playwright";
import type { Locator, Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: a box that scrolls takes Tab, has a name and shows a focus ring
 * (#2208).
 *
 * WCAG 2.1.1 (Keyboard): content that scrolls sideways has to be scrollable
 * without a mouse, so each such box is a tab stop with an accessible name
 * (axe `scrollable-region-focusable`). The boxes are the experiment page's
 * configuration blocks (a variant's, and the example request) and the
 * Bayesian results table. At 320 px each is bigger than its box, so each
 * really scrolls. The checks are soft, so one run names every box that fails.
 *
 * An ordinary DEVELOPER owns the draft it creates here and deletes it in
 * `finally`; the journey database is shared.
 */

// Long enough that the variant's block, which is capped in height, scrolls.
const WIDE_CONFIGURATION = {
  endpoint: "https://example.com/a/very/long/path/that/does/not/break/anywhere/at/all/0123456789",
  layout: { columns: 2, gutter: "16px", rows: 3, areas: ["header", "main", "footer"] },
  colors: { primary: "#0b5fff", secondary: "#222222", accent: "#ff6a00" },
  flags: { dense: true, sticky: false, animated: true },
};

/** Tab from the top of the page until `box` has focus: how a keyboard user reaches it. */
async function tabTo(page: Page, box: Locator): Promise<boolean> {
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  const handle = await box.elementHandle();
  if (!handle) return false;
  for (let i = 0; i < 120; i++) {
    await page.keyboard.press("Tab");
    if (await handle.evaluate((el) => el === document.activeElement)) return true;
  }
  return false;
}

async function expectKeyboardBox(page: Page, box: Locator, what: string): Promise<void> {
  await expect(box, `${what} is on the page`).toBeVisible();
  const scrolls = await box.evaluate((el) => el.scrollWidth > el.clientWidth || el.scrollHeight > el.clientHeight);
  expect.soft(scrolls, `${what} is bigger than its box at 320 px, so it scrolls`).toBe(true);
  expect.soft(await tabTo(page, box), `${what} takes focus from Tab`).toBe(true);
  const name = ((await box.getAttribute("aria-label")) ?? "").trim();
  expect.soft(name.length, `${what} has an accessible name`).toBeGreaterThan(0);
  const ring = await box.evaluate((el) => getComputedStyle(el).boxShadow);
  expect.soft(ring, `${what} shows a focus ring while it has focus`).not.toBe("none");
}

async function scrollableViolations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  return results.violations
    .filter((v) => v.id === "scrollable-region-focusable")
    .flatMap((v) => v.nodes.map((n) => n.target.join(" ")));
}

test.describe("Journey: scrolling boxes take the keyboard", () => {
  test("the experiment page's configuration blocks", async ({ sessions }) => {
    const page = await (await sessions("developer")).newPage();
    const stamp = Date.now();
    let headers: Record<string, string> = {};
    let draftId: string | null = null;
    try {
      await page.goto("/experiments");
      await expect(page.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
      const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
      expect(token, "the developer session has a token").toBeTruthy();
      headers = { Authorization: `Bearer ${token}` };

      const created = await page.request.post(`${API_URL}/api/v1/experiments/`, {
        headers,
        data: {
          name: `E2E Scroll ${stamp}`,
          key: `e2e_scroll_${stamp}`,
          experiment_type: "a_b",
          variants: [
            { name: "Control", is_control: true, traffic_allocation: 50, configuration: WIDE_CONFIGURATION },
            { name: "Treatment", is_control: false, traffic_allocation: 50, configuration: WIDE_CONFIGURATION },
          ],
          metrics: [
            {
              name: "Purchases",
              event_name: "purchase",
              metric_type: "conversion",
              is_primary: true,
              aggregation_method: "count",
              minimum_sample_size: 100,
              expected_effect: 0.1,
            },
          ],
        },
      });
      expect(created.status(), await created.text()).toBe(201);
      draftId = ((await created.json()) as { id: string }).id;

      await page.setViewportSize({ width: 320, height: 700 });
      await page.goto(`/experiments/${draftId}`);
      await expect(page.getByTestId("experiment-detail")).toBeVisible({ timeout: 15_000 });

      await expectKeyboardBox(page, page.getByTestId("variant-configuration-view-0"), "the variant's configuration");
      await expectKeyboardBox(page, page.locator("pre", { hasText: "/api/v1/tracking/assign" }), "the example request");
      expect.soft(await scrollableViolations(page), "axe scrollable-region-focusable on the experiment page").toEqual([]);
    } finally {
      if (draftId) {
        const removed = await page.request.delete(
          `${API_URL}/api/v1/experiments/${draftId}?experiment_key=${draftId}`,
          { headers },
        );
        expect.soft(removed.status(), `DELETE the journey's draft ${draftId}`).toBe(204);
      }
      await page.close();
    }
  });

  test("the Bayesian results table", async ({ sessions }) => {
    const page = await (await sessions("developer")).newPage();
    try {
      await page.goto("/experiments");
      await expect(page.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
      const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
      const headers = { Authorization: `Bearer ${token}` };
      const list = await page.request.get(`${API_URL}/api/v1/experiments/?limit=50`, { headers });
      const items = ((await list.json()) as { items?: { id: string }[] }).items ?? [];
      expect(items.length, "the journey database has an experiment to read results for").toBeGreaterThan(0);
      const id = items[0].id;

      // The results are the API's, with the Bayesian block added: the seeded
      // experiments do not have Bayesian analysis on.
      await page.route(new RegExp(`/api/v1/results/${id}(\\?|$)`), async (route) => {
        const response = await route.fetch();
        const body = (await response.json()) as Record<string, unknown>;
        body.bayesian_results = {
          is_enabled: true,
          decision: "CONTINUE",
          n_samples: 10000,
          variant_results: [
            {
              variant_key: "control",
              probability_to_be_best: 0.42,
              expected_loss: 0.0031,
              posterior: { mean: 0.121, credible_interval_lower: 0.101, credible_interval_upper: 0.143 },
            },
            {
              variant_key: "treatment",
              probability_to_be_best: 0.58,
              expected_loss: 0.0012,
              posterior: { mean: 0.134, credible_interval_lower: 0.112, credible_interval_upper: 0.157 },
            },
          ],
        };
        await route.fulfill({ response, json: body });
      });

      await page.setViewportSize({ width: 320, height: 700 });
      await page.goto(`/results/${id}`);
      await expect(page.getByTestId("bayesian-table")).toBeVisible({ timeout: 20_000 });

      await expectKeyboardBox(page, page.getByTestId("bayesian-table").locator(".."), "the Bayesian table");
      expect.soft(await scrollableViolations(page), "axe scrollable-region-focusable on the results page").toEqual([]);
    } finally {
      await page.close();
    }
  });
});
