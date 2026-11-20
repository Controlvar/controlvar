import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { LoginPage } from "./pages/login.page";
import { API_URL, BASE_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: changing a password signs out the account's other sessions (#501),
 * against the real API.
 *
 * A fresh VIEWER account (created and deleted here, so no seeded account's
 * password ever changes) signs in in two browsers. The first changes the
 * password on its account page: it stays signed in, with a new token, and
 * says the other sessions have ended. The second browser, still holding the
 * token from before, is sent to the sign-in page on its next page.
 */

const CHANGED =
  "Your password has been changed. Other browsers and devices signed in to your account have been " +
  "signed out. Use the new password the next time you sign in.";

async function signIn(page: Page, email: string, password: string): Promise<string> {
  const loginPage = new LoginPage(page);
  await loginPage.goto();
  await loginPage.submitResilient(email, password);
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20_000 });
  const token = await loginPage.storedToken();
  expect(token, "the sign-in stored a token").toBeTruthy();
  return String(token);
}

test.describe("Journey: a password change signs out the other sessions", () => {
  test("the changing browser stays signed in and another browser is signed out", async ({
    adminPage,
    browser,
  }) => {
    const stamp = Date.now();
    const username = `e2e_pw_${stamp}`;
    const email = `${username}@example.com`;
    const password = `Sessions${stamp}x`;
    const changed = `Changed${stamp}y`;
    let headers: Record<string, string> = {};
    let userId: string | null = null;
    const first = await browser.newContext({ baseURL: BASE_URL });
    const second = await browser.newContext({ baseURL: BASE_URL });
    const pageA = await first.newPage();
    const pageB = await second.newPage();

    try {
      await test.step("a superuser creates the account", async () => {
        await adminPage.goto("/admin/users");
        await expect(adminPage.getByTestId("user-table")).toBeVisible({ timeout: 15_000 });
        const token = await adminPage.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
        expect(token, "the admin session has a token").toBeTruthy();
        headers = { Authorization: `Bearer ${token}` };
        const created = await adminPage.request.post(`${API_URL}/api/v1/users/`, {
          headers,
          data: { username, email, password, role: "VIEWER" },
        });
        expect(created.status(), await created.text()).toBe(201);
        userId = ((await created.json()) as { id: string }).id;
      });

      let before = "";
      await test.step("it signs in in two browsers", async () => {
        before = await signIn(pageA, email, password);
        await signIn(pageB, email, password);
      });

      await test.step("the first browser changes the password and stays signed in", async () => {
        await pageA.goto("/account/password");
        await pageA.getByTestId("current-password-input").fill(password);
        await pageA.getByTestId("new-password-input").fill(changed);
        await pageA.getByTestId("confirm-password-input").fill(changed);
        const answer = pageA.waitForResponse(
          (r) => new URL(r.url()).pathname === "/api/v1/users/me/password" && r.request().method() === "POST",
        );
        await pageA.getByTestId("change-password-submit").click();
        expect((await answer).status(), "the change").toBe(200);
        const outcome = pageA.getByTestId("change-password-outcome");
        await expect(outcome).toHaveAttribute("data-outcome", "changed", { timeout: 15_000 });
        await expect(outcome).toHaveText(CHANGED);
        const after = await new LoginPage(pageA).storedToken();
        expect(after, "a new token is stored").toBeTruthy();
        expect(after).not.toBe(before);

        await pageA.goto("/experiments");
        await expect(new LoginPage(pageA).logoutButton).toBeVisible({ timeout: 15_000 });
        expect(new URL(pageA.url()).pathname).toBe("/experiments");
      });

      await test.step("the second browser's next page sends it to sign in", async () => {
        await pageB.goto("/experiments");
        await pageB.waitForURL(/\/login(\?|$)/, { timeout: 20_000 });
        await expect(pageB.getByTestId("login-form")).toBeVisible({ timeout: 15_000 });
        expect(await new LoginPage(pageB).storedToken()).toBeNull();
      });
    } finally {
      await first.close();
      await second.close();
      if (userId) {
        const removed = await adminPage.request.delete(`${API_URL}/api/v1/admin/users/${userId}`, { headers });
        expect.soft(removed.status(), "the journey's user is deleted").toBe(204);
      }
    }
  });
});
