import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { FeatureFlagsPage } from "./pages/feature-flags.page";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: the kill switch and the Safety page's rollback, read back the way
 * an SDK reads them (#1897).
 *
 * Every answer is checked twice: on screen, and through
 * `GET /api/v1/feature-flags/evaluate/{key}?user_id=` with an API key, the call
 * a client application makes. A switch that repaints but leaves the flag
 * serving is the failure this journey exists to catch.
 *
 * The flag is this journey's own: ON for everyone (100%), with one targeting
 * rule that serves users in the US at 100%. It has an ACTIVE rollout schedule
 * and a safety check that always fails (a floor of a million evaluations), so
 * the Safety page offers "Roll back" for it. The seeded flags are never
 * touched. The flag and the key are created and deleted through the API.
 *
 *   - a user turns the flag off in the dashboard: evaluate answers
 *     `enabled: false`, `reason: "inactive"` for a user the rule matches and
 *     for one it does not; turned on again, both are served as before;
 *   - the Safety page's Roll back turns it off the same way, and pauses the
 *     rollout schedule;
 *   - a rollback to 20% (the page always sends 0, so this one is the API's)
 *     lowers only the global percentage: the rule-matched user keeps being
 *     served by their rule.
 */

const stamp = Date.now();
const KEY = `e2e-kill-switch-${stamp}`;
const NAME = `E2E kill switch ${stamp}`;

/** Serves users in the US at 100%, whatever the global percentage is. */
const US_RULE = {
  logical_operator: "AND",
  groups: [
    {
      logical_operator: "AND",
      conditions: [{ attribute: "country", operator: "equals", value: "US" }],
    },
  ],
  rollout_percentage: 100,
};

/** Never met by a few evaluations: the check fails while the flag serves. */
const ALWAYS_FAILING_CHECK = {
  metrics: {
    total_evaluations: {
      warning_threshold: 1_000_000,
      critical_threshold: 1_000_000,
      comparison_type: "less_than",
    },
  },
};

/** Users with no country, to see the global percentage; enough that 20% serves some and not all. */
const GLOBAL_USERS = Array.from({ length: 40 }, (_, i) => `e2e-global-${stamp}-${i}`);
const US_USER = `e2e-us-${stamp}`;

interface Answer {
  key: string;
  enabled: boolean;
  reason: string;
}

test.describe("Journey: the kill switch and the Safety page's rollback", () => {
  test.describe.configure({ mode: "serial" });

  let flagId = "";
  let keyId = "";
  let sdkKey = "";
  let headers: Record<string, string> = {};

  /** What an SDK gets for a user: the evaluate route, an API key, the user's country as context. */
  async function evaluate(page: Page, userId: string, country?: string): Promise<Answer> {
    const query = new URLSearchParams({ user_id: userId });
    if (country) query.set("context", JSON.stringify({ country }));
    const response = await page.request.get(
      `${API_URL}/api/v1/feature-flags/evaluate/${KEY}?${query.toString()}`,
      { headers: { "X-API-Key": sdkKey } },
    );
    expect(response.status(), `evaluate ${KEY} for ${userId}`).toBe(200);
    return (await response.json()) as Answer;
  }

  /** The two users every step reads: the one the rule matches, and one it does not. */
  async function expectServedByRuleAndRollout(page: Page): Promise<void> {
    expect(await evaluate(page, US_USER, "US")).toMatchObject({ enabled: true, reason: "targeting_rule" });
    expect(await evaluate(page, GLOBAL_USERS[0])).toMatchObject({ enabled: true, reason: "rollout" });
  }

  async function expectKilled(page: Page): Promise<void> {
    expect(await evaluate(page, US_USER, "US")).toMatchObject({ enabled: false, reason: "inactive" });
    expect(await evaluate(page, GLOBAL_USERS[0])).toMatchObject({ enabled: false, reason: "inactive" });
  }

  async function storedFlag(page: Page): Promise<{ status: string; rollout_percentage: number }> {
    const response = await page.request.get(`${API_URL}/api/v1/feature-flags/${flagId}`, { headers });
    expect(response.status(), "GET the flag").toBe(200);
    return (await response.json()) as { status: string; rollout_percentage: number };
  }

  async function scheduleStatuses(page: Page): Promise<string[]> {
    const response = await page.request.get(`${API_URL}/api/v1/rollout-schedules/`, {
      headers,
      params: { feature_flag_id: flagId },
    });
    expect(response.status(), "GET the flag's rollout schedules").toBe(200);
    return ((await response.json()) as { items: { status: string }[] }).items.map((s) => s.status);
  }

  /** A schedule for the flag, activated. Its one stage is manual and never advances by itself. */
  async function activateSchedule(page: Page): Promise<void> {
    const created = await page.request.post(`${API_URL}/api/v1/rollout-schedules/`, {
      headers: { ...headers, "content-type": "application/json" },
      data: {
        name: `E2E schedule ${stamp}`,
        feature_flag_id: flagId,
        stages: [{ name: "Everyone", stage_order: 1, target_percentage: 100, trigger_type: "manual" }],
      },
    });
    expect(created.status(), `POST /rollout-schedules/: ${await created.text()}`).toBe(201);
    const id = ((await created.json()) as { id: string }).id;
    const activated = await page.request.post(`${API_URL}/api/v1/rollout-schedules/${id}/activate`, { headers });
    expect(activated.status(), `activate the schedule: ${await activated.text()}`).toBe(200);
    expect(await scheduleStatuses(page)).toContain("active");
  }

  test.beforeAll(async ({ sessions }) => {
    const page = await (await sessions("admin")).newPage();
    try {
      await page.goto("/feature-flags");
      const token = await page.evaluate((storageKey) => window.localStorage.getItem(storageKey), TOKEN_STORAGE_KEY);
      expect(token, "the admin's session token").toBeTruthy();
      headers = { Authorization: `Bearer ${token}` };
      const json = { ...headers, "content-type": "application/json" };

      const flag = await page.request.post(`${API_URL}/api/v1/feature-flags/`, {
        headers: json,
        data: { key: KEY, name: NAME, is_active: true, rollout_percentage: 100, targeting_rules: US_RULE },
      });
      expect(flag.status(), `POST /feature-flags/: ${await flag.text()}`).toBe(201);
      flagId = ((await flag.json()) as { id: string }).id;

      const config = await page.request.post(`${API_URL}/api/v1/safety/feature-flags/${flagId}/config`, {
        headers: json,
        data: ALWAYS_FAILING_CHECK,
      });
      expect(config.status(), `the flag's safety config: ${await config.text()}`).toBe(200);

      const apiKey = await page.request.post(`${API_URL}/api/v1/api-keys`, {
        headers: json,
        data: { name: `e2e-kill-switch-${stamp}` },
      });
      expect(apiKey.status(), `POST /api-keys: ${await apiKey.text()}`).toBe(201);
      const created = (await apiKey.json()) as { id: string; key: string };
      keyId = created.id;
      sdkKey = created.key;

      await activateSchedule(page);
    } finally {
      await page.close();
    }
  });

  test.afterAll(async ({ sessions }) => {
    const page = await (await sessions("admin")).newPage();
    try {
      if (flagId) {
        const items = await page.request.get(`${API_URL}/api/v1/rollout-schedules/`, {
          headers,
          params: { feature_flag_id: flagId },
          failOnStatusCode: false,
        });
        const schedules = items.ok() ? ((await items.json()) as { items: { id: string; status: string }[] }).items : [];
        for (const { id, status } of schedules) {
          if (status === "completed" || status === "cancelled") continue;
          await page.request.post(`${API_URL}/api/v1/rollout-schedules/${id}/cancel`, { headers, failOnStatusCode: false });
        }
        const removed = await page.request.delete(`${API_URL}/api/v1/feature-flags/${flagId}`, { headers });
        expect(removed.status(), `DELETE the journey's flag ${flagId}`).toBe(204);
      }
      if (keyId) {
        const removed = await page.request.delete(`${API_URL}/api/v1/api-keys/${keyId}`, { headers });
        expect(removed.status(), `DELETE the journey's API key ${keyId}`).toBe(204);
      }
    } finally {
      await page.close();
    }
  });

  test("a user turns the flag off in the dashboard and the SDK's answer follows, then back on", async ({ adminPage: page }) => {
    await expectServedByRuleAndRollout(page);

    const flags = new FeatureFlagsPage(page);
    await flags.gotoFlag(flagId);
    await expect(flags.detailKey).toHaveText(KEY, { timeout: 15_000 });
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active");

    await flags.detailToggle.click();
    await expect(flags.statusBadge).toHaveAttribute("data-status", "inactive", { timeout: 15_000 });
    await expect(flags.detailToggleError).toHaveCount(0);
    expect((await storedFlag(page)).status, "the stored status").toBe("inactive");
    await expectKilled(page);

    await flags.detailToggle.click();
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active", { timeout: 15_000 });
    await expect(flags.detailToggleError).toHaveCount(0);
    expect(await storedFlag(page)).toMatchObject({ status: "active", rollout_percentage: 100 });
    await expectServedByRuleAndRollout(page);
  });

  test("the Safety page's rollback turns the flag off for everyone and pauses its schedule", async ({ adminPage: page }) => {
    expect(await scheduleStatuses(page)).toEqual(["active"]);

    await page.goto("/admin/safety");
    await expect(page.getByTestId("safety-dashboard")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("flag-status-loading")).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByTestId("flag-status-error")).toHaveCount(0);
    const card = page.locator(`[data-testid="safety-status-card"][data-flag-id="${flagId}"]`);
    await expect(card, "the flag's card").toBeVisible({ timeout: 15_000 });
    await expect(card.getByTestId("status-badge")).toHaveText("critical");

    await card.getByTestId("rollback-button").click();
    await expect(page.getByTestId("rollback-modal-effect")).toContainText("including users matched by a targeting rule");
    await page.getByTestId("rollback-reason-input").fill("e2e: the check fails");
    await page.getByTestId("rollback-confirm-button").click();
    await expect(page.getByTestId("rollback-success-message")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("rollback-error")).toHaveCount(0);
    await page.getByTestId("rollback-cancel-button").click();
    await expect(card.getByTestId("flag-off-indicator")).toHaveText("Off", { timeout: 15_000 });
    await expect(card.getByTestId("rollback-button")).toHaveCount(0);

    expect(await storedFlag(page)).toMatchObject({ status: "inactive", rollout_percentage: 0 });
    await expectKilled(page);
    expect(await scheduleStatuses(page), "the rollout schedule is paused").toEqual(["paused"]);

    // The flag's own page agrees: Off, and its schedule paused.
    const flags = new FeatureFlagsPage(page);
    await flags.gotoFlag(flagId);
    await expect(flags.statusBadge).toHaveAttribute("data-status", "inactive", { timeout: 15_000 });
    await expect(page.getByTestId("rollout-schedule-status")).toHaveText("Paused", { timeout: 15_000 });
  });

  test("a user turns it back on from its page: everyone is served again", async ({ adminPage: page }) => {
    const flags = new FeatureFlagsPage(page);
    await flags.gotoFlag(flagId);
    await expect(flags.statusBadge).toHaveAttribute("data-status", "inactive", { timeout: 15_000 });
    await flags.detailToggle.click();
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active", { timeout: 15_000 });
    await flags.setRolloutPercentage(100);
    expect(await storedFlag(page)).toMatchObject({ status: "active", rollout_percentage: 100 });
    await expectServedByRuleAndRollout(page);
  });

  test("a rollback to 20% lowers only the global percentage and the rule's users keep theirs", async ({ adminPage: page }) => {
    await activateSchedule(page);
    expect(await scheduleStatuses(page)).toContain("active");

    const rolled = await page.request.post(`${API_URL}/api/v1/safety/feature-flags/${flagId}/rollback`, {
      headers,
      params: { percentage: 20, reason: "e2e: lower, not off" },
    });
    expect(rolled.status(), `the rollback: ${await rolled.text()}`).toBe(200);
    expect(await rolled.json()).toMatchObject({ success: true, previous_percentage: 100, new_percentage: 20 });

    // Still ON, at 20%.
    expect(await storedFlag(page)).toMatchObject({ status: "active", rollout_percentage: 20 });
    expect(await evaluate(page, US_USER, "US"), "the rule's user keeps the rule's 100%").toMatchObject({
      enabled: true,
      reason: "targeting_rule",
    });
    const answers = await Promise.all(GLOBAL_USERS.map((user) => evaluate(page, user)));
    for (const answer of answers) expect(answer.reason, "a user with no country is decided by the global percentage").toBe("rollout");
    const served = answers.filter((a) => a.enabled).length;
    expect(served, "some of 40 users are still served at 20%").toBeGreaterThan(0);
    expect(served, "and not all of them").toBeLessThan(GLOBAL_USERS.length);
    expect(await scheduleStatuses(page), "a lowered flag's schedule is paused too").not.toContain("active");

    // The dashboard shows what the SDK gets: On, at 20%.
    const flags = new FeatureFlagsPage(page);
    await flags.gotoFlag(flagId);
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active", { timeout: 15_000 });
    await expect(flags.rolloutValue).toHaveText("20%");
  });
});
