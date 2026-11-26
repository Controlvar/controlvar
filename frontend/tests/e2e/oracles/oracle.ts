import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import type { APIRequestContext, APIResponse, Page } from "@playwright/test";
import { API_URL, TOKEN_STORAGE_KEY } from "../env";

/**
 * The oracles of the persona days (tests/personas, #1950).
 *
 * A persona card names, for every step of a day, the oracle that decides it:
 * an id and one of eight kinds. A spec decides that step by calling the
 * helper of that kind with that id. Each call appends one line to the oracle
 * log, `{id, kind, pass, expected, actual}`, and throws `OracleError` when the
 * check fails. tests/personas/check_run.py reads the log after the run and
 * fails a day whose card names an oracle the log does not hold, so a step
 * that "passes" without running its oracle is red.
 *
 * The log is a file (PERSONAS_ORACLE_LOG), never stdout: what it holds stays
 * on the machine that ran the day.
 */

export type OracleKind =
  | "api_state"
  | "api_refused"
  | "ui_matches_api"
  | "count_delta"
  | "hash_vector"
  | "audit_entry"
  | "text_exact"
  | "network_allowlist";

export const ORACLE_LOG = process.env.PERSONAS_ORACLE_LOG ?? "persona-results/oracle-log.jsonl";

/** The longest expected or actual value a log line keeps, in characters. */
const KEEP = 2000;

export class OracleError extends Error {
  constructor(id: string, message: string) {
    super(`ORACLE[${id}] ${message}`);
    this.name = "OracleError";
  }
}

function brief(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text === undefined ? "undefined" : text.slice(0, KEEP);
}

/** Append one line to the oracle log (also the watcher's noise lines). */
export function writeLogLine(entry: Record<string, unknown>): void {
  mkdirSync(dirname(ORACLE_LOG), { recursive: true });
  appendFileSync(ORACLE_LOG, `${JSON.stringify(entry)}\n`);
}

/** JSON with object keys sorted, so two equal values have one spelling. */
export function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, v: unknown) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.keys(v as Record<string, unknown>)
          .sort()
          .reduce<Record<string, unknown>>((out, k) => {
            out[k] = (v as Record<string, unknown>)[k];
            return out;
          }, {})
      : v,
  );
}

/**
 * Decide one oracle: `actual` must equal `expected` (as canonical JSON).
 * Logs the result, then throws `OracleError` with `message` on a mismatch.
 */
export function decide(
  id: string,
  kind: OracleKind,
  expected: unknown,
  actual: unknown,
  message = "",
): void {
  const pass = canonical(actual) === canonical(expected);
  writeLogLine({ id, kind, pass, expected: brief(expected), actual: brief(actual) });
  if (!pass) {
    throw new OracleError(id, `${message ? `${message}: ` : ""}expected ${brief(expected)}, got ${brief(actual)}`);
  }
}

export const apiState = (id: string, expected: unknown, actual: unknown, message?: string) =>
  decide(id, "api_state", expected, actual, message);
export const countDelta = (id: string, expected: unknown, actual: unknown, message?: string) =>
  decide(id, "count_delta", expected, actual, message);
export const hashVector = (id: string, expected: unknown, actual: unknown, message?: string) =>
  decide(id, "hash_vector", expected, actual, message);
export const uiMatchesApi = (id: string, api: unknown, ui: unknown, message?: string) =>
  decide(id, "ui_matches_api", api, ui, message);

/**
 * The API as the signed-in persona: the bearer token the dashboard stored
 * (TOKEN_STORAGE_KEY), sent straight to the API origin, not through the page.
 */
export class PersonaApi {
  constructor(
    private readonly request: APIRequestContext,
    private readonly token: string,
  ) {}

  static async of(page: Page): Promise<PersonaApi> {
    const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
    if (!token) throw new Error("the persona's page holds no session token");
    return new PersonaApi(page.request, token);
  }

  private headers(): Record<string, string> {
    return { Authorization: `Bearer ${this.token}` };
  }

  get(path: string): Promise<APIResponse> {
    return this.request.get(`${API_URL}${path}`, { headers: this.headers() });
  }

  post(path: string, data: unknown): Promise<APIResponse> {
    return this.request.post(`${API_URL}${path}`, { headers: this.headers(), data });
  }

  /** GET `path`, requiring 200, and return the parsed body. */
  async json<T>(path: string): Promise<T> {
    const response = await this.get(path);
    if (response.status() !== 200) {
      throw new Error(`GET ${path}: ${response.status()}`);
    }
    return (await response.json()) as T;
  }
}
