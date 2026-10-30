import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey — edit a draft's variants and metrics (#1453).
 *
 * An ordinary DEVELOPER (the seeded admin is a superuser, so its run would
 * prove nothing about the role) opens a draft of its own, is refused one
 * variant before anything is sent, then renames a variant and saves. The PUT
 * carries every variant and every metric, and the API's read-back keeps every
 * field the form does not show. The journey database is shared, so the only
 * experiment this changes is the draft it creates, deleted in `finally`.
 */

const VARIANTS = [
  {
    name: "Control",
    description: "The page as it is",
    is_control: true,
    traffic_allocation: 34,
    configuration: { color: "grey", layout: { columns: 2 } },
  },
  {
    name: "Blue CTA",
    description: "A blue button",
    is_control: false,
    traffic_allocation: 33,
    configuration: { color: "blue" },
  },
  {
    name: "Green CTA",
    description: "A green button",
    is_control: false,
    traffic_allocation: 33,
    configuration: { color: "green", label: "Été ✓" },
  },
];

const METRICS = [
  {
    name: "Revenue",
    description: "Order value",
    event_name: "purchase",
    metric_type: "revenue",
    is_primary: true,
    aggregation_method: "sum",
    minimum_sample_size: 500,
    expected_effect: 0.1,
    event_value_path: "amount",
    lower_is_better: true,
  },
  {
    name: "Errors",
    description: "Client errors",
    event_name: "error",
    metric_type: "count",
    is_primary: false,
    aggregation_method: "median",
    minimum_sample_size: 250,
    expected_effect: 0.03,
    event_value_path: "count",
    lower_is_better: true,
  },
];

type Item = Record<string, unknown>;

/** Each item's fields as created, keyed by name: order is not part of the answer. */
function byName(items: Item[], fields: string[]): Record<string, Item> {
  return Object.fromEntries(
    items.map((item) => [item.name as string, Object.fromEntries(fields.map((f) => [f, item[f]]))]),
  );
}

async function expectNoSeriousViolations(page: Page, where: string): Promise<void> {
  const results = await new AxeBuilder({ page })
    .include('[data-testid="design-form"]')
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(serious, `axe on ${where}`).toEqual([]);
}

test.describe("Journey: edit a draft's variants and metrics", () => {
  test("developer refuses one variant, then saves a rename with every item and field kept", async ({
    sessions,
  }) => {
    const page = await (await sessions("developer")).newPage();
    const stamp = Date.now();
    let headers: Record<string, string> = {};
    let draftId: string | null = null;
    const puts: Item[] = [];
    page.on("request", (request) => {
      if (
        request.method() === "PUT" &&
        draftId !== null &&
        new URL(request.url()).pathname === `/api/v1/experiments/${draftId}`
      ) {
        puts.push(request.postDataJSON() as Item);
      }
    });
    try {
      await page.goto("/experiments");
      await expect(page.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
      const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
      expect(token, "the developer session has a token").toBeTruthy();
      headers = { Authorization: `Bearer ${token}` };

      const created = await page.request.post(`${API_URL}/api/v1/experiments/`, {
        headers,
        data: {
          name: `E2E Design ${stamp}`,
          key: `e2e_design_${stamp}`,
          experiment_type: "a_b",
          variants: VARIANTS,
          metrics: METRICS,
        },
      });
      expect(created.status(), await created.text()).toBe(201);
      draftId = ((await created.json()) as { id: string }).id;

      await page.goto(`/experiments/${draftId}`);
      await expect(page.getByTestId("experiment-detail")).toBeVisible({ timeout: 15_000 });
      await page.getByTestId("experiment-edit-variants").click();
      const form = page.getByTestId("design-form");
      await expect(form).toBeVisible();
      // The form stands in for the read-only cards.
      await expect(page.getByTestId("variants-table")).toHaveCount(0);
      await expectNoSeriousViolations(page, "the form as opened");

      // One variant is refused before anything is sent.
      await page.getByTestId("remove-variant-2").click();
      await page.getByTestId("remove-variant-1").click();
      await page.getByTestId("variant-allocation-0").fill("100");
      await page.getByTestId("design-save").click();
      await expect(page.getByTestId("design-error")).toHaveText(
        "An experiment needs at least two variants to start.",
      );
      await expect(page.getByTestId("add-variant")).toBeFocused();
      await expectNoSeriousViolations(page, "the form with a refusal");
      expect(puts).toEqual([]);

      // Cancel throws that away; the form opens again from what is stored.
      await page.getByTestId("design-cancel").click();
      await expect(page.getByTestId("experiment-edit-variants")).toBeFocused();
      await page.getByTestId("experiment-edit-variants").click();
      // All three are back; found by name, as the API's order is not fixed.
      const names = page.locator('input[data-testid^="variant-name-"]');
      await expect(names).toHaveCount(3);
      const values = await names.evaluateAll((inputs) => inputs.map((input) => (input as HTMLInputElement).value));
      expect([...values].sort()).toEqual(["Blue CTA", "Control", "Green CTA"]);

      await names.nth(values.indexOf("Blue CTA")).fill("Blue button");
      await page.getByTestId("design-save").click();
      await expect(page.getByTestId("design-saved")).toHaveText("Variants and metrics saved.");
      await expect(page.getByTestId("variants-table")).toContainText("Blue button");

      // The one PUT: exactly both lists, every item. The form keeps the order
      // the API listed them in, which is not creation order: metrics have no
      // ORDER BY, and variants created in one request can share a timestamp.
      // So the items are compared as sets of names.
      expect(puts).toHaveLength(1);
      const body = puts[0] as { variants: Item[]; metrics: Item[] };
      expect(Object.keys(body)).toEqual(["variants", "metrics"]);
      expect(body.variants.map((v) => v.name).sort()).toEqual(["Blue button", "Control", "Green CTA"]);
      expect(body.metrics.map((m) => m.name).sort()).toEqual(["Errors", "Revenue"]);

      // Read back from the API: nothing the form does not show has moved.
      const read = await page.request.get(`${API_URL}/api/v1/experiments/${draftId}`, { headers });
      expect(read.status()).toBe(200);
      const stored = (await read.json()) as { status: string; variants: Item[]; metrics: Item[] };
      expect(stored.status).toBe("draft");
      const variantFields = Object.keys(VARIANTS[0]);
      const metricFields = Object.keys(METRICS[0]);
      const renamed = VARIANTS.map((v) => (v.name === "Blue CTA" ? { ...v, name: "Blue button" } : v));
      expect(byName(stored.variants, variantFields)).toEqual(byName(renamed, variantFields));
      expect(byName(stored.metrics, metricFields)).toEqual(byName(METRICS, metricFields));
    } finally {
      // The journey database is shared, so the draft goes even when a step
      // above failed. Only a draft can be deleted, and the route takes its id
      // again as `experiment_key`.
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
