import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";
import { ESTIMATE_STEP_HEADING, ExperimentsPage } from "./pages/experiments.page";

/**
 * Journey: balance the split within groups (#448), from guided setup to a
 * started experiment.
 *
 * An ordinary DEVELOPER (the seeded admin is a superuser, so its run would
 * prove nothing about the role) turns on "Balance the split within groups",
 * keeps US and DE apart by country, and creates the experiment. The API's
 * read-back holds exactly that setting. Once it has started, the page shows the
 * setting and offers no control that edits it, and the API refuses a change to
 * it even from a superuser.
 *
 * The create's own response is awaited before anything is read. The name and
 * key carry the attempt's number and the time, so Playwright's retry creates
 * its own experiment. A started experiment cannot be deleted, so the shared
 * journey database keeps this one, as it keeps the lifecycle journey's.
 */

const BALANCE = { attribute: "country", values: ["US", "DE"] };
const SUMMARY = "within Country (US, DE)";
const REFUSAL = "Cannot update balance_within for experiments in active status";

async function bearer(page: Page): Promise<Record<string, string>> {
  await page.goto("/experiments");
  await expect(page.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
  const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
  expect(token, "the session has a token").toBeTruthy();
  return { Authorization: `Bearer ${token}` };
}

test.describe("Journey: balance the split within groups", () => {
  test("developer balances by country, the API holds it, and it is fixed once started", async ({
    sessions,
  }, testInfo) => {
    const developer = await (await sessions("developer")).newPage();
    const admin = await (await sessions("admin")).newPage();
    const experiments = new ExperimentsPage(developer);
    const stamp = `${testInfo.retry}_${Date.now()}`;
    const name = `E2E Balance ${stamp}`;
    try {
      await experiments.gotoGuided();
      await expect(experiments.stepHeading).toHaveText("What kind of experiment?", { timeout: 15_000 });
      await experiments.nextStep("Name it and choose what to measure");
      await experiments.nameInput.fill(name);
      await experiments.keyInput.fill(`e2e_balance_${stamp}`);
      await experiments.nextStep("Set up the versions users will see");

      await developer.getByTestId("balance-switch").check();
      await developer.getByTestId("balance-group-by").selectOption("country");
      for (const value of BALANCE.values) {
        await developer.getByTestId("balance-value-input").fill(value);
        await developer.getByTestId("balance-value-add").click();
      }
      await expect(developer.getByTestId("balance-values")).toHaveText(/US.*DE/);

      await experiments.nextStep(ESTIMATE_STEP_HEADING);
      await experiments.nextStep("Check and create");
      await expect(developer.getByTestId("review-balance")).toHaveText(`Balance: ${SUMMARY}`);

      // The create itself, not the redirect the API may answer first for the
      // path without its trailing slash.
      const answer = developer.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          /^\/api\/v1\/experiments\/?$/.test(new URL(response.url()).pathname) &&
          (response.status() < 300 || response.status() >= 400),
        { timeout: 20_000 },
      );
      await experiments.createStepButton.click();
      const response = await answer;
      expect(response.status(), `create answered ${response.status()}: ${await response.text()}`).toBe(201);
      const id = ((await response.json()) as { id: string }).id;
      await developer.waitForURL((url) => url.pathname === `/experiments/${id}`, { timeout: 15_000 });
      await expect(experiments.detail).toBeVisible({ timeout: 15_000 });

      // What the API holds, exactly.
      const developerHeaders = await bearer(developer);
      const read = await developer.request.get(`${API_URL}/api/v1/experiments/${id}`, {
        headers: developerHeaders,
      });
      expect(read.status(), await read.text()).toBe(200);
      expect(((await read.json()) as { balance_within: unknown }).balance_within).toEqual(BALANCE);

      // The page shows the setting on the draft and, unchanged, after the start.
      await experiments.gotoExperiment(id);
      await expect(developer.getByTestId("experiment-balance")).toHaveText(SUMMARY);
      // Not vacuous: the control the started page lacks is there on the draft.
      await expect(developer.getByTestId("experiment-edit-variants")).toBeVisible();
      await experiments.startExperiment();
      await expect(developer.getByTestId("experiment-balance")).toHaveText(SUMMARY);
      await expect(developer.getByTestId("experiment-edit-variants")).toHaveCount(0);
      await expect(developer.getByTestId("balance-switch")).toHaveCount(0);
      await expect(developer.getByTestId("balance-within")).toHaveCount(0);
      await developer.reload();
      await expect(developer.getByTestId("experiment-balance")).toHaveText(SUMMARY);
      await expect(developer.getByTestId("balance-switch")).toHaveCount(0);

      // The API refuses a change to it, a superuser's included, and still holds the setting.
      const adminHeaders = await bearer(admin);
      const refused = await admin.request.put(`${API_URL}/api/v1/experiments/${id}`, {
        headers: adminHeaders,
        data: { balance_within: { attribute: "platform", values: ["ios"] } },
      });
      expect(refused.status(), await refused.text()).toBe(400);
      expect(((await refused.json()) as { detail: string }).detail).toBe(REFUSAL);
      const after = await developer.request.get(`${API_URL}/api/v1/experiments/${id}`, {
        headers: developerHeaders,
      });
      expect(((await after.json()) as { balance_within: unknown }).balance_within).toEqual(BALANCE);
    } finally {
      await developer.close();
      await admin.close();
    }
  });
});
