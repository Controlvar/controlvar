import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { test as base, expect, type Browser, type Page } from "@playwright/test";
import { API_URL, BASE_URL, TOKEN_STORAGE_KEY } from "../env";
import { ORACLE_LOG, PersonaApi } from "../oracles/oracle";
import { signInByPage } from "../oracles/signin";
import { Watcher } from "../oracles/watcher";

/**
 * A persona's session for one day: a browser context of its own (in the
 * persona's time zone), its API as that session, and the watcher on its page.
 *
 * Each persona signs in through /login once per run (oracles/signin.ts, which
 * keeps the run under the sign-in limit). The session is saved to a state
 * file beside the oracle log (PERSONAS_STATE_DIR to choose another), and a
 * later test or a retry of the run reuses it while the API still accepts its
 * token.
 *
 * The session is checked before the day starts: GET /api/v1/auth/me must
 * answer the persona's role, so a day can never run as someone else.
 *
 * Built on @playwright/test's own `test`, not on the role fixtures in
 * tests/e2e/fixtures: their role pages sign in on their own, unpaced, so a
 * persona spec has no way to sign in but this fixture and oracles/signin.ts.
 */
export interface PersonaSession {
  page: Page;
  api: PersonaApi;
  watch: Watcher;
}

interface PersonaFixtures {
  /** dev@demo.com, a DEVELOPER (not a superuser), in Los Angeles. */
  developer: PersonaSession;
  /** analyst@demo.com, an ANALYST, in London. */
  analyst: PersonaSession;
  /** viewer@demo.com, a VIEWER, in Tokyo. */
  viewer: PersonaSession;
}

/** The seeded accounts (backend/scripts/seed_demo_data.py) the days sign in as. */
export const ACCOUNTS = {
  developer: { email: "dev@demo.com", password: "Demo1234!", role: "DEVELOPER" },
  analyst: { email: "analyst@demo.com", password: "Demo1234!", role: "ANALYST" },
  viewer: { email: "viewer@demo.com", password: "Demo1234!", role: "VIEWER" },
} as const;

export type Persona = keyof typeof ACCOUNTS;

const STATE_DIR = process.env.PERSONAS_STATE_DIR ?? join(dirname(ORACLE_LOG), "state");

interface StorageState {
  origins?: Array<{ origin: string; localStorage: Array<{ name: string; value: string }> }>;
}

/** The token a saved session holds, when the API still answers it with this account. */
async function savedToken(page: Page, file: string, email: string): Promise<string | null> {
  if (!existsSync(file)) return null;
  const state = JSON.parse(readFileSync(file, "utf8")) as StorageState;
  const origin = new URL(BASE_URL).origin;
  const token = state.origins
    ?.find((o) => o.origin === origin)
    ?.localStorage.find((item) => item.name === TOKEN_STORAGE_KEY)?.value;
  if (!token) return null;
  const me = await page.request.get(`${API_URL}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (me.status() === 200 && ((await me.json()) as { email: string }).email === email) return token;
  rmSync(file);
  return null;
}

async function open(
  browser: Browser,
  role: Persona,
  timezoneId: string,
): Promise<{ session: PersonaSession; close: () => Promise<void> }> {
  const user = ACCOUNTS[role];
  const file = join(STATE_DIR, `${role}.json`);
  mkdirSync(STATE_DIR, { recursive: true });

  const probe = await browser.newContext({ baseURL: BASE_URL });
  const token = await savedToken(await probe.newPage(), file, user.email);
  await probe.close();

  const context = await browser.newContext(
    token ? { baseURL: BASE_URL, timezoneId, storageState: file } : { baseURL: BASE_URL, timezoneId },
  );
  const page = await context.newPage();
  const watch = new Watcher(page);
  let session = token;
  if (!session) {
    await signInByPage(page, user.email, user.password, role);
    await context.storageState({ path: file });
    session = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
    if (!session) throw new Error(`sign-in ${role}: no session token stored`);
  }
  const api = new PersonaApi(page.request, session);
  const me = await api.json<{ role: string; email: string }>("/api/v1/auth/me");
  if (me.role !== user.role || me.email !== user.email) {
    throw new Error(`session role ${me.role}, expected ${user.role}`);
  }
  return { session: { page, api, watch }, close: () => context.close() };
}

export const test = base.extend<PersonaFixtures>({
  developer: async ({ browser }, use) => {
    const { session, close } = await open(browser, "developer", "America/Los_Angeles");
    await use(session);
    await close();
  },
  analyst: async ({ browser }, use) => {
    const { session, close } = await open(browser, "analyst", "Europe/London");
    await use(session);
    await close();
  },
  viewer: async ({ browser }, use) => {
    const { session, close } = await open(browser, "viewer", "Asia/Tokyo");
    await use(session);
    await close();
  },
});

export { expect };
