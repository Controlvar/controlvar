import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import type { APIRequestContext, Download, Page } from "@playwright/test";
import { API_URL, TOKEN_STORAGE_KEY } from "../env";
import { signInByApi } from "../oracles/signin";

/**
 * What the read-only days (U-ANALYST, U-VIEWER) read, made for them first by
 * the superuser through the API: a running experiment with a conversion
 * metric and a revenue metric on one event, traffic sent with a key of the
 * superuser's, and a feature flag. The day keeps a ledger of every user it
 * assigned, who converted and with what value, so the numbers it reads are
 * decided against what was sent, not against seed data or the page itself.
 *
 * Everything carries the day's stamp: a second run on the same stack makes
 * new objects and never reads an old day's.
 */

export const SUPERUSER = { email: "admin@demo.com", password: "Demo1234!" };

/** The users the setup assigns, and how many of each variant convert. */
const USERS = 120;

export interface Variant {
  id: string;
  name: string;
  is_control: boolean;
}

export interface StampedDay {
  stamp: number;
  experiment: {
    id: string;
    key: string;
    name: string;
    variants: Variant[];
    metrics: Array<{ id: string; name: string; metric_type: string; is_primary: boolean }>;
  };
  flag: { id: string; key: string; name: string; rollout_percentage: number; status: string };
  /** user -> variant name, for every user the setup assigned. */
  assigned: Map<string, string>;
  /** user -> the value of the one purchase they sent. */
  purchases: Map<string, number>;
  /** The superuser's bearer header, for the day's own clean-up. */
  superuser: Record<string, string>;
}

/**
 * A change the persona's role may not make, sent as the persona: the
 * dashboard's stored token, straight to the API, with a valid body. Returns
 * the status; the caller reads the thing back to see that nothing changed.
 */
export async function tryAs(page: Page, method: "PUT" | "POST", path: string, data: unknown): Promise<number> {
  const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
  if (!token) throw new Error("the persona's page holds no session token");
  const answer = await page.request.fetch(`${API_URL}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  });
  return answer.status();
}

async function created<T>(what: string, answer: Promise<import("@playwright/test").APIResponse>, status = 201): Promise<T> {
  const response = await answer;
  if (response.status() !== status) {
    throw new Error(`setup: ${what} answered ${response.status()}`);
  }
  return (await response.json()) as T;
}

/**
 * The superuser's token, signed in for once per worker through
 * oracles/signin.ts: the days share the sign-in limit, so the setup spends one
 * sign-in, not one a day.
 */
let superuserToken: string | null = null;

async function superuserHeaders(request: APIRequestContext): Promise<Record<string, string>> {
  if (!superuserToken) {
    const { status, token } = await signInByApi(request, SUPERUSER.email, SUPERUSER.password, "superuser");
    if (status !== 200 || !token) throw new Error(`setup: the superuser's sign-in answered ${status}`);
    superuserToken = token;
  }
  return { Authorization: `Bearer ${superuserToken}` };
}

/**
 * The superuser makes the day's experiment, starts it, sends its traffic and
 * makes its flag. `who` names the day in every object ("analyst", "viewer").
 */
export async function stampDay(request: APIRequestContext, who: string): Promise<StampedDay> {
  const stamp = Date.now();
  const superuser = await superuserHeaders(request);

  const name = `Persona ${who} results ${stamp}`;
  const key = `persona_${who}_results_${stamp}`;
  const experiment = await created<StampedDay["experiment"]>(
    "the experiment",
    request.post(`${API_URL}/api/v1/experiments/`, {
      headers: superuser,
      data: {
        name,
        key,
        experiment_type: "a_b",
        variants: [
          { name: "Control", is_control: true, traffic_allocation: 50 },
          { name: "Treatment", is_control: false, traffic_allocation: 50 },
        ],
        metrics: [
          { name: "Purchase", event_name: "purchase", metric_type: "conversion", is_primary: true },
          { name: "Revenue", event_name: "purchase", metric_type: "revenue", is_primary: false },
        ],
      },
    }),
  );
  await created("the start", request.post(`${API_URL}/api/v1/experiments/${experiment.id}/start`, { headers: superuser }), 200);
  const apiKey = (
    await created<{ key: string }>(
      "the key",
      request.post(`${API_URL}/api/v1/api-keys`, { headers: superuser, data: { name: `persona-${who}-${stamp}` } }),
    )
  ).key;

  const assigned = new Map<string, string>();
  for (let i = 1; i <= USERS; i += 1) {
    const user = `u-${who}-${stamp}-${i}`;
    const body = await created<{ variant_name: string; assigned: boolean }>(
      `assign ${i}`,
      request.post(`${API_URL}/api/v1/tracking/assign`, {
        headers: { "X-API-Key": apiKey },
        data: { experiment_key: key, user_id: user },
      }),
      200,
    );
    if (!body.assigned) throw new Error(`setup: assign ${i} was not assigned`);
    assigned.set(user, body.variant_name);
  }

  // Half of the treatment's users buy, and a quarter of the control's, each
  // once, for 5.00, 7.50, 10.00, 12.50 or 15.00 (sums a float adds exactly).
  const purchases = new Map<string, number>();
  for (const [variant, share] of [
    ["Treatment", 2],
    ["Control", 4],
  ] as const) {
    const users = Array.from(assigned.keys()).filter((u) => assigned.get(u) === variant);
    const buyers = users.slice(0, Math.floor(users.length / share));
    for (let i = 0; i < buyers.length; i += 1) {
      const value = 5 + (i % 5) * 2.5;
      await created(
        `purchase ${buyers[i]}`,
        request.post(`${API_URL}/api/v1/tracking/track`, {
          headers: { "X-API-Key": apiKey },
          data: { event_type: "purchase", user_id: buyers[i], experiment_key: key, value },
        }),
        200,
      );
      purchases.set(buyers[i], value);
    }
  }

  const flag = await created<StampedDay["flag"]>(
    "the flag",
    request.post(`${API_URL}/api/v1/feature-flags/`, {
      headers: superuser,
      data: { name: `Persona ${who} flag ${stamp}`, key: `persona_${who}_flag_${stamp}`, rollout_percentage: 30 },
    }),
  );
  return {
    stamp,
    experiment: { ...experiment, name, key },
    flag,
    assigned,
    purchases,
    superuser,
  };
}

/** Per variant name: users, converting users and the revenue total, from the ledger. */
export function ledgerCounts(day: StampedDay): Record<string, { users: number; conversions: number; revenue: number }> {
  const counts: Record<string, { users: number; conversions: number; revenue: number }> = {};
  day.assigned.forEach((variant, user) => {
    const row = (counts[variant] ??= { users: 0, conversions: 0, revenue: 0 });
    row.users += 1;
    const value = day.purchases.get(user);
    if (value !== undefined) {
      row.conversions += 1;
      row.revenue += value;
    }
  });
  return counts;
}

/** A number as the oracles compare it: twelve significant digits. */
export function digits(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Number(value.toPrecision(12)) : null;
}

/**
 * Run a few lines of Python with scipy (PERSONAS_PYTHON, else python3 on
 * PATH: the nightly job's has backend/requirements.txt installed) on a JSON
 * argument, and return what it prints, parsed as JSON.
 */
export function scipy<T>(script: string, argument: unknown): T {
  const run = spawnSync(process.env.PERSONAS_PYTHON ?? "python3", ["-c", script, JSON.stringify(argument)], {
    encoding: "utf8",
  });
  if (run.status !== 0) throw new Error(`scipy did not run: ${run.stderr}`);
  return JSON.parse(run.stdout) as T;
}

/**
 * The ledger's statistics, from scipy: the two-sided Fisher exact p of the
 * treatment against the control (docs/architecture/technical-guide.md), and
 * the sample-ratio check's p, a chi-square test of the users per variant
 * against the 50/50 allocation (docs/guides/user-guide.md).
 */
export function ledgerStatistics(day: StampedDay): { fisherP: number; srmP: number } {
  const c = ledgerCounts(day);
  const script = [
    "import json, sys",
    "from scipy import stats",
    "t, c = json.loads(sys.argv[1])",
    "fisher = stats.fisher_exact([[t[1], t[0] - t[1]], [c[1], c[0] - c[1]]])[1]",
    "srm = stats.chisquare([c[0], t[0]]).pvalue",
    "print(json.dumps({'fisherP': float(fisher), 'srmP': float(srm)}))",
  ].join("\n");
  return scipy(script, [
    [c.Treatment.users, c.Treatment.conversions],
    [c.Control.users, c.Control.conversions],
  ]);
}

export interface ShownRow {
  metric: string;
  variant: string;
  /** Each cell's text as a sighted reader sees it (screen-reader text left out). */
  cells: string[];
  /** Each cell's first line: the value alone, without the words under it. */
  first: string[];
}

/** The results page's comparison table, by column header, row by metric and variant. */
export async function shownTable(page: Page): Promise<{ headers: string[]; rows: ShownRow[] }> {
  return page.getByTestId("metric-comparison-table").evaluate((table) => {
    const seen = (el: Element): string => {
      const copy = el.cloneNode(true) as Element;
      copy.querySelectorAll(".sr-only").forEach((n) => n.remove());
      return (copy.textContent ?? "").replace(/[↑↓↕]/g, "").replace(/\s+/g, " ").trim();
    };
    const first = (el: Element): string => {
      const inner = el.querySelector(":scope > span > span");
      return inner ? seen(inner) : seen(el);
    };
    return {
      headers: Array.from(table.querySelectorAll("thead th")).map(seen),
      rows: Array.from(table.querySelectorAll("tbody tr[data-variant-id]")).map((tr) => ({
        metric: tr.getAttribute("data-metric-id") ?? "",
        variant: tr.getAttribute("data-variant-id") ?? "",
        cells: Array.from(tr.querySelectorAll("th, td")).map(seen),
        first: Array.from(tr.querySelectorAll("th, td")).map(first),
      })),
    };
  });
}

/** One cell of the shown table: its first line, or "" when the row or column is absent. */
export function shownCell(
  table: { headers: string[]; rows: ShownRow[] },
  metric: string,
  variant: string,
  column: string,
): string {
  const row = table.rows.find((r) => r.metric === metric && r.variant === variant);
  return row ? row.first[table.headers.indexOf(column)] ?? "" : "";
}

export interface CsvRow {
  [column: string]: string;
}

/**
 * Press the results page's Download CSV and read the file the browser saved.
 * The rows are the API's (export.py's variant columns); the experiment's
 * names hold no comma or quote, so a line splits on commas.
 */
export async function downloadResultsCsv(page: Page): Promise<CsvRow[]> {
  const saved: Promise<Download> = page.waitForEvent("download");
  await page.getByTestId("results-export-csv").click();
  const path = await (await saved).path();
  const lines = readFileSync(path, "utf8").split(/\r?\n/).filter((l) => l.length > 0);
  if (lines.some((l) => l.includes('"'))) throw new Error("the CSV holds a quoted field");
  const [header, ...rows] = lines.map((l) => l.split(","));
  return rows.map((cells) => Object.fromEntries(header.map((h, i) => [h, cells[i] ?? ""])));
}

/** The CSV's rows as the oracle compares them with the ledger. */
export function csvByVariant(rows: CsvRow[]): Record<string, { assignments: number; conversions: number }> {
  return Object.fromEntries(
    rows.map((r) => [r.variant_name, { assignments: Number(r.assignments), conversions: Number(r.conversions) }]),
  );
}
