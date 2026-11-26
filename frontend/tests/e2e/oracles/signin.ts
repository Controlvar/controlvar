import { readFileSync } from "node:fs";
import type { APIRequestContext, Page } from "@playwright/test";
import { API_URL } from "../env";
import { LoginPage } from "../pages/login.page";
import { ORACLE_LOG, writeLogLine } from "./oracle";

/**
 * Every sign-in the persona days make goes through here.
 *
 * The API allows `POST /api/v1/auth/login` 10 times a minute per address
 * (`RATE_LIMIT_CONFIG` in backend/app/middleware/rate_limiter.py), and the
 * days all sign in from one address. Rather than wait out a 429 after the
 * fact, each sign-in first waits, if it must, until the last 60 seconds hold
 * fewer than SIGN_IN_BUDGET (one under the limit), then writes a `signin`
 * line with its time to the oracle log. The log is shared by every test and
 * every retry of the run, so the budget holds across them, and
 * tests/personas/check_run.py fails a run whose log shows more. It also
 * counts the sign-in requests the API itself logged during the job and fails
 * the run when they are not the ones logged here, however a sign-in was sent.
 *
 * A 429 is never retried: it throws, naming it.
 */

export const LOGIN_PATH = "/api/v1/auth/login";
/** The sign-in request as a day's list of expected requests names it. */
export const SIGN_IN_REQUEST = `POST ${LOGIN_PATH}`;
export const SIGN_IN_LIMIT = 10;
export const SIGN_IN_WINDOW_MS = 60_000;
export const SIGN_IN_BUDGET = SIGN_IN_LIMIT - 1;

function signInTimes(): number[] {
  let text = "";
  try {
    text = readFileSync(ORACLE_LOG, "utf8");
  } catch {
    return [];
  }
  return text
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as { signin?: string; at?: number })
    .filter((entry) => entry.signin !== undefined)
    .map((entry) => Number(entry.at));
}

/** Wait until one more sign-in keeps the last 60 seconds within the budget; record it. */
export async function paceSignIn(label: string): Promise<void> {
  for (;;) {
    const now = Date.now();
    const recent = signInTimes()
      .filter((at) => at > now - SIGN_IN_WINDOW_MS)
      .sort((a, b) => a - b);
    if (recent.length < SIGN_IN_BUDGET) break;
    const wait = recent[recent.length - SIGN_IN_BUDGET] + SIGN_IN_WINDOW_MS - now + 250;
    await new Promise((resolve) => setTimeout(resolve, wait));
  }
  writeLogLine({ signin: label, at: Date.now() });
}

function refused(label: string, status: number): Error {
  return new Error(`sign-in ${label}: ${status} from POST ${LOGIN_PATH}`);
}

/** Sign in through the API. Returns the status (a refusal is an answer) and the token on a 200. */
export async function signInByApi(
  request: APIRequestContext,
  email: string,
  password: string,
  label: string,
): Promise<{ status: number; token: string | null }> {
  await paceSignIn(label);
  const answer = await request.post(`${API_URL}${LOGIN_PATH}`, { data: { email, password } });
  if (answer.status() === 429) throw refused(label, 429);
  const token = answer.status() === 200 ? ((await answer.json()) as { access_token: string }).access_token : null;
  return { status: answer.status(), token };
}

/** Sign in through /login on `page`, once; anything but a 200 throws, naming the status. */
export async function signInByPage(page: Page, email: string, password: string, label: string): Promise<void> {
  const login = new LoginPage(page);
  await login.goto();
  await paceSignIn(label);
  const answer = page.waitForResponse(
    (r) => new URL(r.url()).pathname === LOGIN_PATH && r.request().method() === "POST",
  );
  await login.submit(email, password);
  const status = (await answer).status();
  if (status !== 200) throw refused(label, status);
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 15_000 });
  await login.logoutButton.waitFor({ state: "visible", timeout: 15_000 });
}
