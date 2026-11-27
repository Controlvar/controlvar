import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: three pages reflow at 320 px (#2207).
 *
 * WCAG 2.1 1.4.10 (Reflow): at 320 CSS px wide a page must not scroll
 * sideways. `document.documentElement.scrollWidth` equals `window.innerWidth`
 * when it does not. Content that cannot wrap, such as a configuration block,
 * scrolls inside its own box. jsdom lays nothing out, so the unit tests read
 * the markup and this journey measures. The three checks are soft, so one run
 * names every page that is too wide.
 *
 * An ordinary DEVELOPER owns the draft it creates here and deletes it in
 * `finally`; the journey database is shared.
 */

const LONG_NAME = "Checkout button copy variant with a rather long name";
const WIDE_CONFIGURATION = {
  endpoint: "https://example.com/a/very/long/path/that/does/not/break/anywhere/at/all/0123456789",
  layout: { columns: 2, gutter: "16px" },
};

async function overflow(page: Page): Promise<{ scrollWidth: number; innerWidth: number; wide: string[] }> {
  return page.evaluate(() => {
    const width = window.innerWidth;
    const wide = Array.from(document.querySelectorAll<HTMLElement>("body *"))
      .filter((el) => el.getBoundingClientRect().right > width + 0.5)
      .slice(0, 8)
      .map((el) => `${el.tagName.toLowerCase()}${el.dataset.testid ? `[${el.dataset.testid}]` : ""}.${String(el.className).slice(0, 60)}`);
    return { scrollWidth: document.documentElement.scrollWidth, innerWidth: width, wide };
  });
}

async function expectReflow(page: Page, where: string): Promise<void> {
  const m = await overflow(page);
  expect.soft(m.scrollWidth, `${where} at 320 px: widest elements ${JSON.stringify(m.wide)}`).toBe(m.innerWidth);
}

test.describe("Journey: reflow at 320 px", () => {
  test("the advanced form, a draft's design edit and the experiment page fit in 320 px", async ({ sessions }) => {
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
          name: `E2E Reflow ${stamp}`,
          key: `e2e_reflow_${stamp}_${"a_long_key_".repeat(5)}`,
          experiment_type: "a_b",
          variants: [
            {
              name: "Control",
              description: "The page as it is",
              is_control: true,
              traffic_allocation: 34,
              configuration: WIDE_CONFIGURATION,
            },
            {
              name: LONG_NAME,
              description: "A change to the label",
              is_control: false,
              traffic_allocation: 33,
              configuration: WIDE_CONFIGURATION,
            },
            {
              name: "Green button",
              description: "A green button",
              is_control: false,
              traffic_allocation: 33,
              configuration: { color: "green" },
            },
          ],
          metrics: [
            {
              name: "Purchases",
              description: "Orders placed",
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

      await page.goto("/experiments/new?advanced");
      await expect(page.getByTestId("variant-row-0")).toBeVisible({ timeout: 15_000 });
      await expectReflow(page, "the advanced form");

      await page.goto(`/experiments/${draftId}`);
      await expect(page.getByTestId("experiment-detail")).toBeVisible({ timeout: 15_000 });
      await expect(page.getByTestId("variants-table")).toBeVisible();
      await expectReflow(page, "the experiment page");

      await page.getByTestId("experiment-edit-variants").click();
      await expect(page.getByTestId("design-form")).toBeVisible();
      await expectReflow(page, "the design edit");
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
});
