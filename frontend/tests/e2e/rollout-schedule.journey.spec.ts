import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey — a flag's rollout schedule, made and run on the flag's page (#1456).
 *
 * A DEVELOPER (not a superuser) creates a flag of their own through the API.
 * On its page they create a two-stage manual schedule through the form,
 * activate it and start its first stage: the flag's rollout goes to that
 * stage's target. The seeded ADMIN (a superuser) then rolls the flag back
 * through the safety API, which pauses the schedule; the DEVELOPER's page,
 * reloaded, reads Paused, offers Resume and says a safety rollback can pause
 * it. An ANALYST opening the same page is offered nothing.
 *
 * The flag and its schedule are this journey's own, named with a timestamp; it
 * never touches the seeded `new_dashboard_ui` schedule (PE 13). The schedule is
 * cancelled and the flag switched off at the end.
 *
 * axe runs on the rollout schedule section in each state, in this fail-hard
 * project: any serious or critical WCAG 2.0 A/AA violation there fails the PR.
 * It is scoped to the section because a whole-page scan fails on things this
 * change does not touch (measured on #1465's first run): color-contrast on the
 * safety check's thresholds, its "Last checked" and the slider's 0%/100%
 * (slate-400, 2.56:1 on white), and aria-prohibited-attr on the safety
 * metric's dot (an aria-label on a span with no role).
 */

async function expectNoSeriousViolations(page: Page, where: string): Promise<void> {
  const results = await new AxeBuilder({ page })
    .include('[data-testid="rollout-schedule-section"]')
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(serious, `axe on ${where}`).toEqual([]);
}

async function tokenOf(page: Page): Promise<string> {
  await page.goto("/feature-flags");
  const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
  expect(token, "the session has a token").toBeTruthy();
  return token as string;
}

test.describe("Journey: a rollout schedule on the flag's page", () => {
  test.describe.configure({ mode: "serial" });

  const stamp = Date.now();
  const key = `e2e_rollout_${stamp}`;
  let flagId = "";

  test.afterAll(async ({ sessions }) => {
    if (!flagId) return;
    const page = await (await sessions("developer")).newPage();
    try {
      const token = await tokenOf(page);
      const headers = { Authorization: `Bearer ${token}` };
      const list = await page.request.get(`${API_URL}/api/v1/rollout-schedules/`, {
        headers,
        params: { feature_flag_id: flagId },
        failOnStatusCode: false,
      });
      const items = list.ok() ? ((await list.json()) as { items: Array<{ id: string; status: string }> }).items : [];
      for (const item of items) {
        if (item.status === "completed" || item.status === "cancelled") continue;
        await page.request.post(`${API_URL}/api/v1/rollout-schedules/${item.id}/cancel`, {
          headers,
          failOnStatusCode: false,
        });
      }
      await page.request.post(`${API_URL}/api/v1/feature-flags/${flagId}/disable`, {
        headers: { ...headers, "content-type": "application/json" },
        data: { reason: "e2e teardown: the rollout journey's own flag" },
        failOnStatusCode: false,
      });
    } finally {
      await page.close();
    }
  });

  test("a developer creates a schedule, activates it and starts its first stage", async ({ developerPage: page }) => {
    const token = await tokenOf(page);
    const created = await page.request.post(`${API_URL}/api/v1/feature-flags/`, {
      headers: { Authorization: `Bearer ${token}`, "content-type": "application/json" },
      data: { key, name: `E2E rollout ${stamp}`, rollout_percentage: 0, is_active: true },
    });
    expect(created.status(), "POST /feature-flags/").toBe(201);
    flagId = ((await created.json()) as { id: string }).id;

    await page.goto(`/feature-flags/${flagId}`);
    await expect(page.getByTestId("flag-key")).toHaveText(key, { timeout: 15_000 });
    await expect(page.getByTestId("rollout-schedule-empty")).toContainText("/api/v1/rollout-schedules");
    await expectNoSeriousViolations(page, "a flag with no schedule");

    await page.getByTestId("rollout-schedule-create").click();
    const form = page.getByTestId("rollout-schedule-form");
    await expect(form).toBeVisible();
    await page.getByTestId("rollout-schedule-name-input").fill("E2E gradual rollout");
    const stages = form.getByTestId("rollout-stage-fields");
    await stages.nth(0).getByLabel("Stage 1 name", { exact: true }).fill("Canary");
    await stages.nth(0).getByLabel("Stage 1 target (% of users)", { exact: true }).fill("10");
    await page.getByTestId("rollout-stage-add").click();
    await stages.nth(1).getByLabel("Stage 2 name", { exact: true }).fill("Everyone");
    await stages.nth(1).getByLabel("Stage 2 target (% of users)", { exact: true }).fill("100");
    await expectNoSeriousViolations(page, "the create form");
    await page.getByTestId("rollout-schedule-submit").click();

    await expect(page.getByTestId("rollout-schedule-notice")).toHaveText(
      "Schedule created as a draft. Nothing changes for users until you activate it.",
      { timeout: 15_000 },
    );
    await expect(page.getByTestId("rollout-schedule-status")).toHaveText("Draft");
    await expect(page.getByTestId("rollout-stage")).toHaveCount(2);

    await page.getByTestId("rollout-schedule-activate").click();
    await expect(page.getByTestId("rollout-schedule-confirm")).toBeVisible();
    await expectNoSeriousViolations(page, "the activate confirmation");
    await page.getByTestId("rollout-schedule-confirm-yes").click();
    await expect(page.getByTestId("rollout-schedule-status")).toHaveText("Active", { timeout: 15_000 });
    await expect(page.getByTestId("rollout-value")).toHaveText("0%");
    // Activate is gone once the schedule is active: focus is on the section's heading.
    await expect(page.getByRole("heading", { name: "Rollout schedule", exact: true })).toBeFocused();

    await page.getByTestId("rollout-stage-start").click();
    await expect(page.getByTestId("rollout-schedule-confirm-text")).toHaveText(
      "Start “Canary” now? The flag goes to 10% of users.",
    );
    await page.getByTestId("rollout-schedule-confirm-yes").click();
    await expect(page.getByTestId("rollout-value")).toHaveText("10%", { timeout: 15_000 });
    await expect(page.getByTestId("rollout-stage").first()).toContainText("In progress");
    await expect(page.getByTestId("rollout-stage-complete")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Rollout schedule", exact: true })).toBeFocused();

    // What the server holds, read back.
    const flag = await page.request.get(`${API_URL}/api/v1/feature-flags/${flagId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(((await flag.json()) as { rollout_percentage: number }).rollout_percentage).toBe(10);
  });

  test("a safety rollback pauses it, and the page says so and offers Resume", async ({
    adminPage,
    developerPage: page,
  }) => {
    expect(flagId, "the first test made the flag").toBeTruthy();
    const adminToken = await tokenOf(adminPage);
    const rollback = await adminPage.request.post(
      `${API_URL}/api/v1/safety/feature-flags/${flagId}/rollback`,
      {
        headers: { Authorization: `Bearer ${adminToken}` },
        params: { percentage: 5, reason: "e2e: the rollout journey" },
      },
    );
    expect(rollback.ok(), "POST /safety/feature-flags/{id}/rollback").toBe(true);

    await page.goto(`/feature-flags/${flagId}`);
    await expect(page.getByTestId("rollout-schedule-status")).toHaveText("Paused", { timeout: 15_000 });
    await expect(page.getByTestId("rollout-value")).toHaveText("5%");
    await expect(page.getByTestId("rollout-schedule-resume")).toBeVisible();
    await expect(page.getByTestId("rollout-schedule-paused-note")).toContainText(
      "A safety rollback pauses a schedule this way too",
    );
    await expect(page.getByTestId("rollout-stage-complete")).toHaveCount(0);
    await expectNoSeriousViolations(page, "a paused schedule");

    await page.getByTestId("rollout-schedule-resume").click();
    await expect(page.getByTestId("rollout-schedule-confirm-text")).toContainText(
      "Resume “E2E gradual rollout”? Nothing changes at once.",
    );
    await page.getByTestId("rollout-schedule-confirm-no").click();
    await expect(page.getByTestId("rollout-schedule-confirm")).toHaveCount(0);
    await expect(page.getByTestId("rollout-schedule-status")).toHaveText("Paused");
  });

  test("an analyst sees the schedule and is offered nothing", async ({ sessions }) => {
    expect(flagId, "the first test made the flag").toBeTruthy();
    const page = await (await sessions("analyst")).newPage();
    try {
      await page.goto(`/feature-flags/${flagId}`);
      await expect(page.getByTestId("rollout-schedule-name")).toHaveText("E2E gradual rollout", {
        timeout: 15_000,
      });
      for (const id of [
        "rollout-schedule-actions",
        "rollout-schedule-create",
        "rollout-schedule-form",
        "rollout-stage-start",
        "rollout-stage-complete",
      ]) {
        await expect(page.getByTestId(id)).toHaveCount(0);
      }
      await expectNoSeriousViolations(page, "a paused schedule as an analyst");
    } finally {
      await page.close();
    }
  });
});
