import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import type { APIRequestContext, APIResponse, Browser, Page } from "@playwright/test";
import { API_URL, BASE_URL, TOKEN_STORAGE_KEY } from "../env";
import { ORACLE_LOG } from "../oracles/oracle";
import { signInByPage } from "../oracles/signin";
import { Watcher } from "../oracles/watcher";

/**
 * The accounts of the persona days beyond the fixture's: any account in a
 * browser context of its own, and the Admin-role account that is not a
 * superuser, which no seed makes (docs/rbac/README.md: "The Admin role is
 * not a superuser."). A day makes its own, stamped, through
 * `POST /api/v1/users/` as the superuser, so it acts only on what it made.
 *
 * Every sign-in goes through oracles/signin.ts, which keeps the run under the
 * sign-in limit. An account signs in once per run: its session is saved in
 * the fixture's state directory under the account's label, and a later test
 * or a retry reuses it while the API still accepts its token. The developer's
 * label is the fixture's own, so the developer's day and this share a session.
 */

/** The five columns of tests/personas/role-matrix.yaml. */
export type MatrixRole = "OWNER" | "ADMIN" | "DEVELOPER" | "ANALYST" | "VIEWER";
export const MATRIX_ROLES: readonly MatrixRole[] = ["OWNER", "ADMIN", "DEVELOPER", "ANALYST", "VIEWER"];

export interface Account {
  /** The name its sign-in and its saved session go under. */
  label: string;
  email: string;
  password: string;
  /** The role GET /api/v1/auth/me must answer. */
  role: "ADMIN" | "DEVELOPER" | "ANALYST" | "VIEWER";
  /** The superuser flag GET /api/v1/auth/me must answer. */
  superuser: boolean;
}

/** The seeded accounts (backend/scripts/seed_demo_data.py) by matrix column. */
const SEEDED: Record<Exclude<MatrixRole, "ADMIN">, Omit<Account, "password">> = {
  OWNER: { label: "owner", email: "admin@demo.com", role: "ADMIN", superuser: true },
  DEVELOPER: { label: "developer", email: "dev@demo.com", role: "DEVELOPER", superuser: false },
  ANALYST: { label: "analyst", email: "analyst@demo.com", role: "ANALYST", superuser: false },
  VIEWER: { label: "viewer", email: "viewer@demo.com", role: "VIEWER", superuser: false },
};

/** The seeded account of a matrix column. OWNER is admin@demo.com: role ADMIN and a superuser. */
export function seededAccount(column: Exclude<MatrixRole, "ADMIN">): Account {
  return { ...SEEDED[column], password: "Demo1234!" };
}

/** A session's API: its bearer token, sent straight to the API origin, not through the page. */
export class SessionApi {
  constructor(
    private readonly request: APIRequestContext,
    private readonly token: string,
  ) {}

  send(method: "GET" | "POST" | "PUT" | "DELETE", path: string, data?: unknown): Promise<APIResponse> {
    return this.request.fetch(`${API_URL}${path}`, {
      method,
      headers: { Authorization: `Bearer ${this.token}` },
      data,
    });
  }

  /** GET `path`, requiring 200, and return the parsed body. */
  async json<T>(path: string): Promise<T> {
    const response = await this.send("GET", path);
    if (response.status() !== 200) throw new Error(`GET ${path}: ${response.status()}`);
    return (await response.json()) as T;
  }
}

export interface AccountSession {
  account: Account;
  page: Page;
  api: SessionApi;
  watch: Watcher;
  close: () => Promise<void>;
}

/** The fixture's state directory (persona.fixture.ts): one saved session per label. */
const STATE_DIR = process.env.PERSONAS_STATE_DIR ?? join(dirname(ORACLE_LOG), "state");

interface StorageState {
  origins?: Array<{ origin: string; localStorage: Array<{ name: string; value: string }> }>;
}

/** The token a saved session holds, when the API still answers it with this account. */
async function savedToken(browser: Browser, file: string, email: string): Promise<string | null> {
  if (!existsSync(file)) return null;
  const state = JSON.parse(readFileSync(file, "utf8")) as StorageState;
  const token = state.origins
    ?.find((o) => o.origin === new URL(BASE_URL).origin)
    ?.localStorage.find((item) => item.name === TOKEN_STORAGE_KEY)?.value;
  if (!token) return null;
  const probe = await browser.newContext({ baseURL: BASE_URL });
  try {
    const me = await probe.request.get(`${API_URL}/api/v1/auth/me`, { headers: { Authorization: `Bearer ${token}` } });
    if (me.status() === 200 && ((await me.json()) as { email: string }).email === email) return token;
  } finally {
    await probe.close();
  }
  rmSync(file);
  return null;
}

/**
 * `account` in a browser context of its own, with the watcher on its page,
 * signed in through /login (oracles/signin.ts) unless its saved session still
 * holds. GET /api/v1/auth/me must answer the account's email, role and
 * superuser flag before the session is used, so a day never runs as someone
 * else.
 */
export async function openAccount(browser: Browser, account: Account, timezoneId = "UTC"): Promise<AccountSession> {
  mkdirSync(STATE_DIR, { recursive: true });
  const file = join(STATE_DIR, `${account.label}.json`);
  const saved = await savedToken(browser, file, account.email);
  const context = await browser.newContext(
    saved ? { baseURL: BASE_URL, timezoneId, storageState: file } : { baseURL: BASE_URL, timezoneId },
  );
  const page = await context.newPage();
  const watch = new Watcher(page);
  let token = saved;
  if (!token) {
    await signInByPage(page, account.email, account.password, account.label);
    await context.storageState({ path: file });
    token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
    if (!token) throw new Error(`sign-in ${account.label}: no session token stored`);
  }
  const api = new SessionApi(page.request, token);
  const me = await api.json<{ email: string; role: string; is_superuser: boolean }>("/api/v1/auth/me");
  if (me.email !== account.email || me.role !== account.role || me.is_superuser !== account.superuser) {
    throw new Error(
      `session role ${me.role} (superuser ${me.is_superuser}), expected ${account.role} (superuser ${account.superuser})`,
    );
  }
  return { account, page, api, watch, close: () => context.close() };
}

export interface MadeAccount {
  account: Account;
  id: string;
  /** The status POST /api/v1/users/ answered. */
  status: number;
}

/**
 * Make an account with the Admin role that is not a superuser, as a superuser
 * session. Its email and label carry `stamp`; its password is random and
 * lives only in this run.
 */
export async function makeAdminAccount(superuser: SessionApi, stamp: string | number): Promise<MadeAccount> {
  const name = `persona-admin-${stamp}`;
  // Upper case, lower case and a digit: the password rule (schemas/user.py).
  const password = `Pa${randomBytes(12).toString("hex")}7`;
  const account: Account = { label: name, email: `${name}@demo.com`, password, role: "ADMIN", superuser: false };
  const created = await superuser.send("POST", "/api/v1/users/", {
    username: name,
    email: account.email,
    password,
    full_name: "Persona Admin",
    role: "ADMIN",
    is_superuser: false,
  });
  const body = created.ok() ? ((await created.json()) as { id: string }) : { id: "" };
  return { account, id: body.id, status: created.status() };
}
