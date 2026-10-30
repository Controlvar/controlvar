import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey — schedule a draft's start and end (#1454), in a browser that is
 * not in UTC.
 *
 * The runner is UTC, where a zone-less local time and a UTC instant read the
 * same, so a form that sent the picker's raw value would pass there. This
 * journey's browser is in America/Los_Angeles (`timezoneId` below; the
 * role fixtures' own contexts would not carry it, so it signs in on the
 * test's own page). An ordinary DEVELOPER (the seeded admin is a superuser)
 * picks a start in January (PST, UTC-8) and an end in July (PDT, UTC-7), and
 * the API's read-back must hold the UTC instants each date's own offset gives.
 *
 * The journey database is shared, so the only experiment this touches is the
 * draft it creates, deleted in `finally`.
 */

test.use({ timezoneId: "America/Los_Angeles" });

const START_LOCAL = "2099-01-05T09:00";
const START_UTC = "2099-01-05T17:00:00.000Z";
const END_LOCAL = "2099-07-05T09:00";
const END_UTC = "2099-07-05T16:00:00.000Z";

/** The API sends a stored time as UTC with no zone; read it as UTC. */
function asUtc(value: string): string {
  return new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`).toISOString();
}

async function expectNoSeriousViolations(page: Page, where: string): Promise<void> {
  const results = await new AxeBuilder({ page })
    .include('[data-testid="experiment-schedule-form"]')
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(serious, `axe on ${where}`).toEqual([]);
}

test.describe("Journey: schedule a draft's start and end", () => {
  test("a developer in Los Angeles schedules a draft, and the API stores the UTC instants", async ({
    page,
    loginAsRole,
  }) => {
    const stamp = Date.now();
    let headers: Record<string, string> = {};
    let draftId: string | null = null;
    const puts: Record<string, unknown>[] = [];
    page.on("request", (request) => {
      if (
        request.method() === "PUT" &&
        draftId !== null &&
        new URL(request.url()).pathname === `/api/v1/experiments/${draftId}/schedule`
      ) {
        puts.push(request.postDataJSON() as Record<string, unknown>);
      }
    });
    try {
      await loginAsRole("developer");
      // The option took: without it this journey passes on the defect it exists for.
      expect(await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone)).toBe(
        "America/Los_Angeles",
      );

      await page.goto("/experiments");
      await expect(page.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
      const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
      expect(token, "the developer session has a token").toBeTruthy();
      headers = { Authorization: `Bearer ${token}` };

      const created = await page.request.post(`${API_URL}/api/v1/experiments/`, {
        headers,
        data: {
          name: `E2E Schedule ${stamp}`,
          key: `e2e_schedule_${stamp}`,
          experiment_type: "a_b",
          variants: [
            { name: "Control", is_control: true, traffic_allocation: 50 },
            { name: "Treatment", is_control: false, traffic_allocation: 50 },
          ],
          metrics: [{ name: "Purchase", event_name: "purchase", metric_type: "conversion", is_primary: true }],
        },
      });
      expect(created.status(), await created.text()).toBe(201);
      draftId = ((await created.json()) as { id: string }).id;

      await page.goto(`/experiments/${draftId}`);
      await expect(page.getByTestId("experiment-detail")).toBeVisible({ timeout: 15_000 });
      await page.getByTestId("experiment-schedule").click();
      const form = page.getByTestId("experiment-schedule-form");
      await expect(form).toBeVisible();
      await expect(page.getByTestId("schedule-start")).toBeFocused();
      await expect(page.getByTestId("schedule-start-zone")).toHaveText(
        "In your time zone, America/Los_Angeles.",
      );

      await page.getByTestId("schedule-start").fill(START_LOCAL);
      await page.getByTestId("schedule-end").fill(END_LOCAL);
      await expect(page.getByTestId("schedule-start-readback")).toHaveText(
        "Saved as Mon, Jan 5, 2099, 9:00 AM PST (17:00 UTC).",
      );
      await expect(page.getByTestId("schedule-end-readback")).toHaveText(
        "Saved as Sun, Jul 5, 2099, 9:00 AM PDT (16:00 UTC).",
      );
      await expectNoSeriousViolations(page, "the form, filled");

      await page.getByTestId("schedule-save").click();
      await expect(page.getByTestId("schedule-saved")).toHaveText("Dates saved.");
      await expect(form).toHaveCount(0);
      await expect(page.getByTestId("experiment-schedule")).toBeFocused();

      // The one PUT: both dates, as instants, and nothing else.
      expect(puts).toEqual([{ start_date: START_UTC, end_date: END_UTC }]);

      // The header re-rendered from the answer: a draft's start is "Starts".
      await expect(page.getByTestId("experiment-start-date").locator("xpath=preceding-sibling::dt")).toHaveText(
        "Starts",
      );

      // Read back from the API.
      const read = await page.request.get(`${API_URL}/api/v1/experiments/${draftId}`, { headers });
      expect(read.status()).toBe(200);
      const stored = (await read.json()) as { status: string; start_date: string; end_date: string };
      expect(stored.status).toBe("draft");
      expect(asUtc(stored.start_date)).toBe(START_UTC);
      expect(asUtc(stored.end_date)).toBe(END_UTC);
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
    }
  });
});
