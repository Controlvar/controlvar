import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";
import { ExperimentsPage } from "./pages/experiments.page";

/**
 * Journey: exclusion groups (#1455).
 *
 * A DEVELOPER (not a superuser) opens Exclusion groups from the experiments
 * list, creates a group of its own at a 50% share, and opens the group's page
 * by its URL: `/experiments/exclusion-groups/<id>` is a nested dynamic route
 * of the static export, served in production by the nginx rule the build
 * generates, and this is the only run of it before production (PE 3, EM-9).
 * It then adds a draft it created itself, reads the group on that draft's
 * page, and removes it again. ANALYST and VIEWER are refused with no request.
 *
 * The journey database is shared (PE 13): the journey never adds a seeded
 * experiment to a group (the rbac journey asserts the seeded ones read
 * "None"), and its draft is deleted in a `finally`. Groups cannot be deleted,
 * so the group it creates stays, with no experiment in it and a name no other
 * journey reads.
 */

async function expectNoSeriousViolations(page: Page, where: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(serious, `axe on ${where}`).toEqual([]);
}

test.describe("Journey: exclusion groups", () => {
  test.describe.configure({ mode: "serial" });

  const stamp = Date.now();
  const groupName = `E2E group ${stamp}`;
  let groupPath = "";

  test("a developer creates a group, opens it by URL, adds its own draft and removes it", async ({
    developerPage: page,
  }) => {
    const experiments = new ExperimentsPage(page);
    let headers: Record<string, string> = {};
    let draftId: string | null = null;
    try {
      await experiments.goto();
      await expect(experiments.experimentList).toBeVisible({ timeout: 15_000 });
      const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
      expect(token, "the developer session has a token").toBeTruthy();
      headers = { Authorization: `Bearer ${token}` };

      await page.getByTestId("exclusion-groups-link").click();
      await expect(page).toHaveURL(/\/experiments\/exclusion-groups$/);
      await expect(page.getByTestId("exclusion-groups-page")).toBeVisible({ timeout: 15_000 });
      await expectNoSeriousViolations(page, "/experiments/exclusion-groups");

      await page.getByTestId("new-exclusion-group-btn").click();
      await page.getByLabel("Name", { exact: true }).fill(groupName);
      await page.getByLabel("Share of traffic (%)").fill("50");
      await expectNoSeriousViolations(page, "the new group form");
      await page.getByTestId("exclusion-group-submit").click();
      await expect(page).toHaveURL(/\/experiments\/exclusion-groups\/[0-9a-f-]{36}$/, { timeout: 15_000 });
      groupPath = new URL(page.url()).pathname;
      const groupId = groupPath.split("/").pop()!;

      // The share went as a fraction.
      const stored = await page.request.get(`${API_URL}/api/v1/mutual-exclusion-groups/${groupId}`, { headers });
      expect(stored.status()).toBe(200);
      expect(await stored.json()).toMatchObject({ name: groupName, traffic_allocation: 0.5, status: "active" });

      // A draft of its own, then the group's page opened by its URL.
      draftId = await experiments.createExperiment(`E2E grouped ${stamp}`, `e2e_grouped_${stamp}`);
      await page.goto(groupPath);
      await expect(page.getByTestId("exclusion-group-name-heading")).toHaveText(groupName, { timeout: 15_000 });
      await expect(page.getByTestId("exclusion-group-share-value")).toHaveText("50%");
      await expect(page.getByTestId("exclusion-group-no-members")).toHaveText("None");
      const picker = page.getByTestId("exclusion-group-add-select");
      await expect(picker).toBeVisible({ timeout: 15_000 });
      await expectNoSeriousViolations(page, "the group page");
      await picker.selectOption(draftId);
      await page.getByTestId("exclusion-group-add").click();
      await expect(page.getByTestId("exclusion-group-member")).toHaveCount(1, { timeout: 15_000 });
      await expect(page.getByTestId("exclusion-group-member")).toContainText(`E2E grouped ${stamp} (Draft)`);

      // The draft's own page names the group, and links back to it.
      await page.goto(`/experiments/${draftId}`);
      const row = page.getByTestId("experiment-exclusion-group");
      await expect(row).toHaveText(groupName, { timeout: 15_000 });
      await expect(row.getByRole("link")).toHaveAttribute("href", groupPath);

      await page.goto(groupPath);
      await page.getByRole("button", { name: `Remove E2E grouped ${stamp}` }).click();
      await expect(page.getByTestId("exclusion-group-remove-confirm")).toContainText(
        `Remove “E2E grouped ${stamp}” from this group?`,
      );
      await page.getByTestId("exclusion-group-remove-yes").click();
      await expect(page.getByTestId("exclusion-group-no-members")).toHaveText("None", { timeout: 15_000 });
      const after = await page.request.get(`${API_URL}/api/v1/experiments/${draftId}`, { headers });
      expect(after.status()).toBe(200);
      expect((await after.json()).mutual_exclusion_group_id ?? null).toBeNull();
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

  for (const role of ["analyst", "viewer"] as const) {
    test(`${role} is told who manages groups, and sends no group request`, async ({ sessions }) => {
      const page = await (await sessions(role)).newPage();
      const groupRequests: string[] = [];
      page.on("request", (request) => {
        const { pathname } = new URL(request.url());
        if (/^\/api\/v1\/mutual-exclusion-groups(\/|$)/.test(pathname)) {
          groupRequests.push(`${request.method()} ${pathname}`);
        }
      });
      try {
        await page.goto("/experiments/exclusion-groups");
        await expect(page.getByTestId("exclusion-groups-refused")).toHaveText(
          "Exclusion groups are managed by the ADMIN and DEVELOPER roles.",
          { timeout: 15_000 },
        );
        await expect(page.getByTestId("exclusion-groups-page")).toHaveCount(0);
        if (groupPath) {
          await page.goto(groupPath);
          await expect(page.getByTestId("exclusion-group-refused")).toBeVisible({ timeout: 15_000 });
          await expect(page.getByTestId("exclusion-group-detail")).toHaveCount(0);
        }
        await page.waitForLoadState("networkidle");
        expect(groupRequests, "no group request is sent").toEqual([]);
      } finally {
        await page.close();
      }
    });
  }
});
