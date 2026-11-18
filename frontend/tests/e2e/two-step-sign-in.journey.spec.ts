import { createHmac } from "node:crypto";
import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { LoginPage, RATE_LIMIT_RETRY_MS } from "./pages/login.page";
import { API_URL, BASE_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: two-step sign-in (#1812, U9 of PR 3), against the real API.
 *
 * A fresh VIEWER account turns two-step sign-in on from its account page,
 * with this file standing in for the authenticator app (RFC 6238 over the key
 * the page shows as text). Then sign-in asks for the code, a wrong code is
 * refused, the lost-device path is on the page, the right code signs in, a
 * recovery code signs in, and a superuser resets it from Admin → Users, after
 * which the password alone signs in again. The account is created and deleted
 * here, so no seeded account ever has two-step sign-in on.
 *
 * Codes: the API accepts a code once, and a code of a step no later than the
 * last one it accepted never (the replay guard), so each code is computed for
 * the step after the last one used, which the one-step window accepts.
 */

const RATE_LIMITED = "Too many attempts. Please wait a moment and try again.";

function base32Decode(text: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const char of text.replace(/=+$/, "").toUpperCase()) {
    const index = alphabet.indexOf(char);
    if (index < 0) throw new Error(`not base32: ${char}`);
    value = (value << 5) | index;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

/** RFC 6238, HMAC-SHA1, 6 digits, the code of 30-second step `step`. */
function totp(secret: string, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeUInt32BE(Math.floor(step / 2 ** 32), 0);
  counter.writeUInt32BE(step >>> 0, 4);
  const digest = createHmac("sha1", base32Decode(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const number = digest.readUInt32BE(offset) & 0x7fffffff;
  return String(number % 1_000_000).padStart(6, "0");
}

function currentStep(): number {
  return Math.floor(Date.now() / 30_000);
}

/** The code for the first step after `lastStep` that the API still accepts. */
async function nextCode(page: Page, secret: string, lastStep: number): Promise<{ code: string; step: number }> {
  for (;;) {
    const now = currentStep();
    const step = Math.max(now, lastStep + 1);
    if (step <= now + 1) return { code: totp(secret, step), step };
    await page.waitForTimeout(1_000);
  }
}

/** Everything in both storages and the URL, for "the mfa_token is nowhere". */
async function storedAndUrl(page: Page): Promise<string> {
  const stored = await page.evaluate(() => {
    const values: string[] = [];
    for (const storage of [window.localStorage, window.sessionStorage]) {
      for (let i = 0; i < storage.length; i += 1) {
        const key = storage.key(i) as string;
        values.push(key, storage.getItem(key) ?? "");
      }
    }
    return values.join("\n");
  });
  return `${stored}\n${page.url()}`;
}

/**
 * The password step of an account with two-step sign-in on: it lands on the
 * code step, not on a page. Waits out the 10-a-minute limit like
 * LoginPage.submitResilient. Returns the step's answer.
 */
async function passwordStep(page: Page, email: string, password: string): Promise<Record<string, unknown>> {
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
    await expect(page.getByTestId("mfa-form")).toBeVisible({ timeout: 15_000 });
    return (await response.json()) as Record<string, unknown>;
  }
  throw new Error("the password step stayed rate-limited");
}

/** Send a code (or recovery code) at the code step, waiting out a 429. */
async function codeStep(page: Page, value: () => Promise<string>): Promise<number> {
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    const answer = page.waitForResponse(
      (r) => new URL(r.url()).pathname === "/api/v1/auth/mfa/verify" && r.request().method() === "POST",
    );
    await page.getByTestId("mfa-code-input").fill(await value());
    await page.getByTestId("mfa-submit").click();
    const status = (await answer).status();
    if (status === 429 && attempt < 5) {
      await expect(page.getByTestId("mfa-error")).toHaveText(RATE_LIMITED);
      await page.waitForTimeout(RATE_LIMIT_RETRY_MS);
      continue;
    }
    return status;
  }
  throw new Error("the code step stayed rate-limited");
}

test.describe("Journey: two-step sign-in", () => {
  test("turn it on, sign in with a code and a recovery code, and a superuser resets it", async ({
    adminPage,
    browser,
  }) => {
    const stamp = Date.now();
    const username = `e2e_2fa_${stamp}`;
    const email = `${username}@example.com`;
    const password = `Journey${stamp}x`;
    let headers: Record<string, string> = {};
    let userId: string | null = null;
    const context = await browser.newContext({ baseURL: BASE_URL });
    const page = await context.newPage();
    const loginPage = new LoginPage(page);
    let secret = "";
    let lastStep = 0;
    let recoveryCodes: string[] = [];

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

      await test.step("the account turns two-step sign-in on from its account page", async () => {
        await loginPage.goto();
        await loginPage.submitResilient(email, password);
        await page.waitForURL(/\/experiments$/, { timeout: 20_000 });
        // More holds the page; the panel is closed, the link is there.
        await expect(page.getByTestId("two-factor-link").first()).toHaveAttribute("href", "/account/two-factor");

        await page.goto("/account/two-factor");
        await expect(page.getByTestId("two-factor-off")).toBeVisible({ timeout: 15_000 });
        await page.getByTestId("two-factor-start").click();
        await expect(page.getByTestId("two-factor-enrol")).toBeVisible();
        await expect(page.getByTestId("two-factor-qr")).toHaveAttribute(
          "alt",
          "QR code for adding Controlvar to an authenticator app. The same key is written below.",
        );
        secret = (await page.getByTestId("two-factor-key").innerText()).replace(/\s+/g, "");
        expect(secret).toMatch(/^[A-Z2-7]{32}$/);
        expect(await page.getByTestId("two-factor-otpauth-link").getAttribute("href")).toContain(`secret=${secret}`);

        const first = await nextCode(page, secret, lastStep);
        await page.getByTestId("two-factor-code-input").fill(first.code);
        await page.getByTestId("two-factor-confirm").click();
        await expect(page.getByTestId("two-factor-codes")).toBeVisible({ timeout: 15_000 });
        lastStep = first.step;

        recoveryCodes = (await page.getByTestId("recovery-codes").locator("li").allInnerTexts()).map((code) =>
          code.replace(/\s+/g, ""),
        );
        expect(recoveryCodes).toHaveLength(10);
        await expect(page.getByTestId("recovery-codes-done")).toBeDisabled();
        await page.getByTestId("recovery-codes-saved").check();
        await page.getByTestId("recovery-codes-done").click();
        await expect(page.getByTestId("two-factor-on")).toBeVisible();
        await expect(page.getByTestId("recovery-codes-left")).toHaveText("10 recovery codes left.");
        await loginPage.logout();
      });

      await test.step("sign-in asks for the code, refuses a wrong one, shows the lost-device path, then signs in", async () => {
        await loginPage.goto();
        const answer = await passwordStep(page, email, password);
        expect(answer.mfa_required).toBe(true);
        expect(answer).not.toHaveProperty("access_token");
        const mfaToken = String(answer.mfa_token);
        expect(await loginPage.storedToken()).toBeNull();
        expect(await storedAndUrl(page)).not.toContain(mfaToken);
        await expect(page.getByTestId("mfa-heading")).toHaveText("Enter your code");

        await page.getByTestId("lost-device").locator("summary").click();
        await expect(page.getByTestId("lost-device-help")).toHaveText(
          "Ask an administrator to reset your two-step sign-in.",
        );

        // Five steps ahead: outside the one-step window, so refused.
        const wrong = await codeStep(page, async () => totp(secret, currentStep() + 5));
        expect(wrong).toBe(401);
        await expect(page.getByTestId("mfa-error")).toHaveText(
          "That code is not correct. Check your authenticator app and try again.",
        );
        expect(await loginPage.storedToken()).toBeNull();

        let used = lastStep;
        const right = await codeStep(page, async () => {
          const next = await nextCode(page, secret, lastStep);
          used = next.step;
          return next.code;
        });
        expect(right).toBe(200);
        lastStep = used;
        await page.waitForURL(/\/experiments$/, { timeout: 20_000 });
        expect(await loginPage.storedToken()).toBeTruthy();
        expect(await storedAndUrl(page)).not.toContain(mfaToken);
        await loginPage.logout();
      });

      await test.step("a recovery code signs in, and the account page counts one fewer", async () => {
        await loginPage.goto();
        await passwordStep(page, email, password);
        await page.getByTestId("mfa-switch").click();
        await expect(page.getByTestId("mfa-heading")).toHaveText("Enter a recovery code");
        await expect(page.getByLabel("Recovery code")).toBeVisible();
        const status = await codeStep(page, async () => recoveryCodes[0]);
        expect(status).toBe(200);
        await page.waitForURL(/\/experiments$/, { timeout: 20_000 });
        await page.goto("/account/two-factor");
        await expect(page.getByTestId("recovery-codes-left")).toHaveText("9 recovery codes left.", {
          timeout: 15_000,
        });
        await loginPage.logout();
      });

      await test.step("a superuser resets it from Admin → Users; the password alone signs in again", async () => {
        await adminPage.goto("/admin/users");
        await adminPage.getByTestId("user-search").fill(username);
        const edit = adminPage.getByTestId(`edit-user-${userId}`);
        await expect(edit).toBeVisible({ timeout: 15_000 });
        await edit.click();
        await adminPage.getByTestId("edit-two-factor-reset").click();
        await expect(adminPage.getByTestId("edit-two-factor-confirm")).toContainText(
          "Their current session stays valid until it expires.",
        );
        await adminPage.getByTestId("edit-two-factor-reset-confirm").click();
        await expect(adminPage.getByTestId("edit-two-factor-reset-done")).toHaveText(
          `Two-step sign-in is off for ${email}.`,
        );
        await adminPage.getByTestId("edit-cancel-button").click();

        await loginPage.goto();
        await loginPage.submitResilient(email, password);
        await page.waitForURL(/\/experiments$/, { timeout: 20_000 });
        expect(await loginPage.storedToken()).toBeTruthy();
      });
    } finally {
      await context.close();
      if (userId) {
        const removed = await adminPage.request.delete(`${API_URL}/api/v1/admin/users/${userId}`, { headers });
        // Soft, so a failed delete does not replace an error already propagating.
        expect.soft(removed.status(), "the journey's user is deleted").toBe(204);
      }
    }
  });
});
