import { test, expect, loginAs } from "./fixtures/auth.fixture";
import { FeatureFlagsPage } from "./pages/feature-flags.page";
import { API_URL, BASE_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey 3 — feature flags.
 *
 * Walks the flag surface an operator uses under pressure: the list, the on/off
 * switch (which must really persist, not just repaint optimistically), and the
 * detail page's rollout schedule and safety check.
 *
 * Uses the two flags from `seed_demo_data.py`:
 *   - `new_dashboard_ui` — 50% rollout, an ACTIVE rollout schedule with three
 *     stages, and a safety config (error_rate > 5% → rollback).
 *   - `beta_features`    — 100% rollout, targeting rules, no schedule. Toggled
 *     here and restored before the journey ends.
 */
const SCHEDULED_FLAG = "new_dashboard_ui";
/** How `seed_demo_data.py` leaves these flags; the teardown restores it. */
const SEEDED_FLAG_STATE: Record<string, boolean> = {
  beta_features: true,
  new_dashboard_ui: true,
};
const TOGGLE_FLAG = "beta_features";

test.describe("Journey: feature flags", () => {
  test.describe.configure({ mode: "serial" });

  // The journey toggles seeded flags. Restore them here rather than relying on
  // a later test to flip them back: with `retries: 1` a failure part-way
  // through would otherwise leave the next attempt starting from the mutated
  // state, where the first assertion can never hold.
  test.afterAll(async ({ browser }) => {
    const context = await browser.newContext({ baseURL: BASE_URL });
    try {
      const page = await context.newPage();
      await loginAs(page, "admin");
      const token = await page.evaluate((key) => localStorage.getItem(key), TOKEN_STORAGE_KEY);
      if (!token) return;
      for (const [key, shouldBeOn] of Object.entries(SEEDED_FLAG_STATE)) {
        await page.request.post(
          `${API_URL}/api/v1/feature-flags/${key}/${shouldBeOn ? "enable" : "disable"}`,
          {
            headers: { Authorization: `Bearer ${token}`, "content-type": "application/json" },
            data: { reason: "e2e teardown: restore the seeded state" },
            failOnStatusCode: false,
          },
        );
      }
    } finally {
      await context.close();
    }
  });

  test("the list renders the seeded flags", async ({ adminPage }) => {
    const flags = new FeatureFlagsPage(adminPage);
    await flags.goto();

    await expect(flags.createButton).toBeVisible();
    await expect(flags.listError).toHaveCount(0);
    await expect(flags.flagList).toBeVisible({ timeout: 15_000 });
    expect(await flags.flagRows.count()).toBeGreaterThan(1);

    const scheduled = flags.getFlagRow(SCHEDULED_FLAG);
    await expect(scheduled).toBeVisible();
    await expect(scheduled.getByTestId("flag-link")).toHaveText("New Dashboard UI");
    await expect(scheduled.getByTestId("flag-status-pill")).toHaveText("On");
    await expect(scheduled).toContainText("50%");
    await expect(flags.rowToggle(SCHEDULED_FLAG)).toHaveAttribute("aria-checked", "true");

    // The status filters are wired to the API, not to a client-side slice.
    await flags.filterByStatus("inactive");
    await expect(flags.listError).toHaveCount(0);
    await expect(flags.getFlagRow(SCHEDULED_FLAG)).toHaveCount(0);
    await flags.filterByStatus("all");
    await expect(flags.getFlagRow(SCHEDULED_FLAG)).toBeVisible({ timeout: 15_000 });
  });

  test("the list switch turns a flag off and the change survives a reload", async ({
    adminPage,
  }) => {
    const flags = new FeatureFlagsPage(adminPage);
    await flags.goto();

    const toggle = flags.rowToggle(TOGGLE_FLAG);
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    await expect(toggle).toHaveAttribute("aria-checked", "true");

    await flags.toggleFlagByKey(TOGGLE_FLAG);
    await expect(flags.listToggleError).toHaveCount(0);
    await expect(flags.getFlagRow(TOGGLE_FLAG).getByTestId("flag-status-pill")).toHaveText("Off");

    // The optimistic repaint must be backed by a persisted write.
    await adminPage.reload();
    await expect(flags.rowToggle(TOGGLE_FLAG)).toHaveAttribute("aria-checked", "false", {
      timeout: 15_000,
    });
    await expect(flags.getFlagRow(TOGGLE_FLAG).getByTestId("flag-status-pill")).toHaveText("Off");
  });

  test("the detail switch turns it back on and that also survives a reload", async ({
    adminPage,
  }) => {
    const flags = new FeatureFlagsPage(adminPage);
    await flags.goto();
    await flags.clickFlag(TOGGLE_FLAG);

    await expect(flags.detailKey).toHaveText(TOGGLE_FLAG);
    await expect(flags.statusBadge).toHaveAttribute("data-status", "inactive");

    await flags.detailToggle.click();
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active", { timeout: 15_000 });
    await expect(flags.detailToggleError).toHaveCount(0);

    await adminPage.reload();
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active", { timeout: 15_000 });
    await expect(flags.detailToggle).toHaveAttribute("aria-checked", "true");
  });

  test("the detail page shows the rollout schedule and the safety check", async ({ adminPage }) => {
    const flags = new FeatureFlagsPage(adminPage);
    await flags.goto();
    await flags.clickFlag(SCHEDULED_FLAG);

    await expect(flags.detailName).toHaveText("New Dashboard UI");
    await expect(flags.detailKey).toHaveText(SCHEDULED_FLAG);
    await expect(flags.rolloutValue).toHaveText("50%");

    // --- rollout schedule --------------------------------------------------
    await expect(flags.rolloutScheduleSection).toBeVisible();
    await expect(adminPage.getByTestId("rollout-schedule-error")).toHaveCount(0);
    await expect(adminPage.getByTestId("rollout-schedule-status")).toBeVisible({ timeout: 15_000 });
    await expect(adminPage.getByTestId("rollout-schedule-status")).toHaveText("Active");
    await expect(adminPage.getByTestId("rollout-schedule-name")).toHaveText(
      "Dashboard UI Gradual Rollout",
    );
    await expect(flags.rolloutStages).toHaveCount(3);
    await expect(flags.rolloutStages.first()).toContainText("Initial 10%");
    await expect(flags.rolloutStages.last()).toContainText("Expand to 50%");

    // --- safety check ------------------------------------------------------
    await expect(flags.safetySection).toBeVisible();
    await expect(adminPage.getByTestId("safety-error")).toHaveCount(0);
    await expect(flags.safetyStatus).toBeVisible({ timeout: 20_000 });
    await expect(flags.safetyStatus).toHaveAttribute("data-healthy", /true|false/);
    await expect(adminPage.getByTestId("safety-last-checked")).toContainText("Last checked");

    // Re-checking hits the API again and resolves to a status, not an error.
    await flags.safetyRecheck.click();
    await expect(flags.safetyStatus).toBeVisible({ timeout: 20_000 });
    await expect(adminPage.getByTestId("safety-error")).toHaveCount(0);
  });

  test("a flag whose name, key and description are one long word fits a 320 px screen", async ({
    adminPage,
  }) => {
    const flags = new FeatureFlagsPage(adminPage);
    await flags.goto();
    await flags.clickFlag(SCHEDULED_FLAG);

    // The API answers with one long word for each text field; nothing is stored.
    const word = "Unbroken".repeat(12);
    await adminPage.route("**/api/v1/feature-flags/**", async (route) => {
      const response = await route.fetch();
      const body = await response.json().catch(() => null);
      if (body && !Array.isArray(body) && body.key === SCHEDULED_FLAG) {
        await route.fulfill({
          response,
          json: { ...body, name: word, key: `${word}_key`, description: word.repeat(2) },
        });
      } else {
        await route.fulfill({ response });
      }
    });
    await adminPage.setViewportSize({ width: 320, height: 800 });
    await adminPage.reload();

    await expect(flags.detailName).toHaveText(word, { timeout: 15_000 });
    await expect(adminPage.getByTestId("flag-description")).toHaveText(word.repeat(2));
    // WCAG 1.4.10: nothing makes the page itself scroll sideways.
    const scrollWidth = await adminPage.evaluate(() => document.documentElement.scrollWidth);
    expect(scrollWidth).toBeLessThanOrEqual(320);
  });

  test("an unknown flag id shows the 404 view", async ({ adminPage }) => {
    const flags = new FeatureFlagsPage(adminPage);
    await flags.gotoFlag("00000000-0000-0000-0000-000000000000");
    await expect(flags.notFound).toBeVisible({ timeout: 15_000 });
  });
});
