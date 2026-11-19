import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { LoginPage, RATE_LIMIT_RETRY_MS } from "./pages/login.page";
import { API_URL, BASE_URL, TOKEN_STORAGE_KEY } from "./env";
import { currentStep, nextCode, storedAndUrl, totp } from "./totp";

/**
 * Journey: two-step sign-in required for a role (#1812, U9 of PR 4), against
 * the real API.
 *
 * The `required-roles` project. The `browser-e2e` job runs it against a
 * second API that it starts with `LOCAL_AUTH_2FA_REQUIRED_ROLES=ANALYST`
 * (same database and SECRET_KEY as the journeys' API), through the same
 * export served on another port: the journeys sign in as the seeded analyst,
 * so the policy cannot be set on their API.
 *
 * A fresh ANALYST account signs in for the first time: the password step
 * answers `enrol: true` and no access token, and the dashboard asks it to set
 * two-step sign-in up before it has a session, with this file standing in for
 * the authenticator app. A wrong first code is refused with the clock hint;
 * the right one gives the recovery codes, and Done (once they are saved)
 * signs it in. Its account page then offers no Turn off, and the next sign-in
 * asks for a code. Then it changes its password: for a role that requires
 * two-step sign-in the change ends every session, this one too (#501), so the
 * page says so and its Sign in opens the sign-in form. The account is created
 * and deleted here.
 */

/** The password step: waits out the 10-a-minute limit. Returns its answer. */
async function passwordStep(
  page: Page,
  email: string,
  password: string,
  lands: "enrol-form" | "mfa-form",
): Promise<Record<string, unknown>> {
  const loginPage = new LoginPage(page);
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    const answer = page.waitForResponse(
      (r) => new URL(r.url()).pathname === "/api/v1/auth/login" && r.request().method() === "POST",
    );
    await loginPage.submit(email, password);
    const response = await answer;
    if (response.status() === 429 && attempt < 5) {
      await page.waitForTimeout(RATE_LIMIT_RETRY_MS);
      continue;
    }
    expect(response.status(), "the password step").toBe(200);
    await expect(page.getByTestId(lands)).toBeVisible({ timeout: 15_000 });
    return (await response.json()) as Record<string, unknown>;
  }
  throw new Error("the password step stayed rate-limited");
}

/** Send the first code of the new secret; returns the answer's status. */
async function firstCode(page: Page, code: string): Promise<number> {
  const answer = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/auth/mfa/enroll/confirm" && r.request().method() === "POST",
  );
  await page.getByTestId("enrol-code-input").fill(code);
  await page.getByTestId("enrol-submit").click();
  return (await answer).status();
}

test.describe("Journey: two-step sign-in required for a role", () => {
  test("a required role sets it up at its first sign-in and cannot turn it off", async ({ adminPage, browser }) => {
    const stamp = Date.now();
    const username = `e2e_2fa_req_${stamp}`;
    const email = `${username}@example.com`;
    const password = `Required${stamp}x`;
    let headers: Record<string, string> = {};
    let userId: string | null = null;
    const context = await browser.newContext({ baseURL: BASE_URL });
    const page = await context.newPage();
    const loginPage = new LoginPage(page);
    let secret = "";
    let lastStep = 0;

    try {
      await test.step("a superuser creates an analyst", async () => {
        await adminPage.goto("/admin/users");
        await expect(adminPage.getByTestId("user-table")).toBeVisible({ timeout: 15_000 });
        const token = await adminPage.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
        expect(token, "the admin session has a token").toBeTruthy();
        headers = { Authorization: `Bearer ${token}` };
        const created = await adminPage.request.post(`${API_URL}/api/v1/users/`, {
          headers,
          data: { username, email, password, role: "ANALYST" },
        });
        expect(created.status(), await created.text()).toBe(201);
        userId = ((await created.json()) as { id: string }).id;
      });

      await test.step("its first sign-in asks it to set two-step sign-in up, with no session yet", async () => {
        await loginPage.goto();
        const answer = await passwordStep(page, email, password, "enrol-form");
        expect(answer.mfa_required).toBe(true);
        expect(answer.enrol).toBe(true);
        expect(answer.expires_in).toBe(600);
        expect(answer).not.toHaveProperty("access_token");
        const enrolToken = String(answer.mfa_token);
        await expect(page.getByTestId("enrol-heading")).toHaveText("Set up two-step sign-in");
        await expect(page.getByTestId("two-factor-qr")).toHaveAttribute(
          "alt",
          "QR code for adding Controlvar to an authenticator app. The same key is written below.",
        );
        secret = (await page.getByTestId("two-factor-key").innerText()).replace(/\s+/g, "");
        expect(secret).toMatch(/^[A-Z2-7]{32}$/);
        expect(await page.getByTestId("two-factor-otpauth-link").getAttribute("href")).toContain(`secret=${secret}`);
        expect(await loginPage.storedToken()).toBeNull();
        expect(await storedAndUrl(page)).not.toContain(enrolToken);

        // Five steps ahead: outside the one-step window, so refused.
        expect(await firstCode(page, totp(secret, currentStep() + 5))).toBe(401);
        await expect(page.getByTestId("enrol-error")).toHaveText(
          "That code did not match. Make sure your phone's clock is set automatically, then try again.",
        );
        expect(await loginPage.storedToken()).toBeNull();

        const first = await nextCode(page, secret, lastStep);
        expect(await firstCode(page, first.code)).toBe(200);
        lastStep = first.step;
        await expect(page.getByTestId("enrol-codes")).toBeVisible({ timeout: 15_000 });
        await expect(page.getByTestId("recovery-codes").locator("li")).toHaveCount(10);
        // Nothing stored until the codes are saved.
        expect(await loginPage.storedToken()).toBeNull();
        await expect(page.getByTestId("recovery-codes-done")).toBeDisabled();
        await page.getByTestId("recovery-codes-saved").check();
        await page.getByTestId("recovery-codes-done").click();
        await page.waitForURL(/\/experiments$/, { timeout: 20_000 });
        const stored = await loginPage.storedToken();
        expect(stored).toBeTruthy();
        expect(stored).not.toBe(enrolToken);
        expect(await storedAndUrl(page)).not.toContain(enrolToken);
      });

      await test.step("its account page says the role requires it and offers no Turn off", async () => {
        await page.goto("/account/two-factor");
        await expect(page.getByTestId("two-factor-on")).toBeVisible({ timeout: 15_000 });
        await expect(page.getByTestId("two-factor-required")).toHaveText(
          "Your role requires two-step sign-in, so it cannot be turned off. If you lose your authenticator " +
            "app and your recovery codes, ask an administrator to reset it.",
        );
        await expect(page.getByTestId("two-factor-open-disable")).toHaveCount(0);
        await expect(page.getByTestId("recovery-codes-left")).toHaveText("10 recovery codes left.");
        await loginPage.logout();
      });

      await test.step("the next sign-in asks for a code", async () => {
        await loginPage.goto();
        const answer = await passwordStep(page, email, password, "mfa-form");
        expect(answer.enrol).toBe(false);
        const next = await nextCode(page, secret, lastStep);
        const verified = page.waitForResponse(
          (r) => new URL(r.url()).pathname === "/api/v1/auth/mfa/verify" && r.request().method() === "POST",
        );
        await page.getByTestId("mfa-code-input").fill(next.code);
        await page.getByTestId("mfa-submit").click();
        expect((await verified).status()).toBe(200);
        await page.waitForURL(/\/experiments$/, { timeout: 20_000 });
        expect(await loginPage.storedToken()).toBeTruthy();
      });

      await test.step("changing its password signs every session out, this one too (#501)", async () => {
        const changed = `Changed${stamp}y`;
        await page.goto("/account/password");
        await page.getByTestId("current-password-input").fill(password);
        await page.getByTestId("new-password-input").fill(changed);
        await page.getByTestId("confirm-password-input").fill(changed);
        const answer = page.waitForResponse(
          (r) => new URL(r.url()).pathname === "/api/v1/users/me/password" && r.request().method() === "POST",
        );
        await page.getByTestId("change-password-submit").click();
        expect((await answer).status(), "the change, for a role that requires two-step sign-in").toBe(204);
        const outcome = page.getByTestId("change-password-outcome");
        await expect(outcome).toHaveAttribute("data-outcome", "signed-out", { timeout: 15_000 });
        await expect(outcome).toHaveAttribute("role", "status");
        await expect(outcome).toHaveText(
          "Your password has been changed, and every session has been signed out, this one too. " +
            "Sign in again with the new password.",
        );

        await page.getByTestId("change-password-sign-in").click();
        await page.waitForURL(/\/login$/, { timeout: 15_000 });
        await expect(loginPage.form).toBeVisible({ timeout: 15_000 });
        expect(await loginPage.storedToken()).toBeNull();

        // A protected page now lands on the sign-in page and stays there.
        await page.goto("/experiments");
        await page.waitForURL(/\/login(\?|$)/, { timeout: 20_000 });
        await expect(loginPage.form).toBeVisible({ timeout: 15_000 });
        await page.waitForTimeout(1_000);
        expect(new URL(page.url()).pathname).toBe("/login");
        expect(await loginPage.storedToken()).toBeNull();
      });
    } finally {
      await context.close();
      if (userId) {
        const removed = await adminPage.request.delete(`${API_URL}/api/v1/admin/users/${userId}`, { headers });
        expect.soft(removed.status(), "the journey's user is deleted").toBe(204);
      }
    }
  });
});
