import { test as base, expect, loginAs, TEST_USERS, type UserRole } from "../fixtures/auth.fixture";
import type { Page } from "@playwright/test";
import { BASE_URL } from "../env";
import { PersonaApi } from "../oracles/oracle";
import { Watcher } from "../oracles/watcher";

/**
 * A persona's session for one day: a browser context of its own (in the
 * persona's time zone), signed in through /login, its API as that session,
 * and the watcher on its page.
 *
 * The session is checked before the day starts: GET /api/v1/auth/me must
 * answer the persona's role, so a day can never run as someone else.
 */
export interface PersonaSession {
  page: Page;
  api: PersonaApi;
  watch: Watcher;
}

interface PersonaFixtures {
  /** dev@demo.com, a DEVELOPER (not a superuser), in Los Angeles. */
  developer: PersonaSession;
}

async function open(
  browser: import("@playwright/test").Browser,
  role: UserRole,
  timezoneId: string,
): Promise<{ session: PersonaSession; close: () => Promise<void> }> {
  const context = await browser.newContext({ baseURL: BASE_URL, timezoneId });
  const page = await context.newPage();
  const watch = new Watcher(page);
  await loginAs(page, role);
  const api = await PersonaApi.of(page);
  const me = await api.json<{ role: string; email: string }>("/api/v1/auth/me");
  const expected = TEST_USERS[role];
  if (me.role !== expected.role || me.email !== expected.username) {
    throw new Error(`session role ${me.role}, expected ${expected.role}`);
  }
  return { session: { page, api, watch }, close: () => context.close() };
}

export const test = base.extend<PersonaFixtures>({
  developer: async ({ browser }, use) => {
    const { session, close } = await open(browser, "developer", "America/Los_Angeles");
    await use(session);
    await close();
  },
});

export { expect };
