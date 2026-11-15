import AxeBuilder from "@axe-core/playwright";
import { test, expect } from "./fixtures/auth.fixture";
import type { Page } from "@playwright/test";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey — the results page shows the API's sample-ratio check and Bayesian
 * analysis (#442).
 *
 * For every experiment the API lists, the page is compared with what
 * `GET /api/v1/results/{id}` returns for it, so each assertion is about a
 * value the server computed, not one the test typed:
 *
 *   - `srm` null: no notice and no "passed" line;
 *   - `srm.warning` false: the quiet line with the server's p-value;
 *   - `srm.warning` true: the notice, with the server's p-value;
 *   - `bayesian_results.is_enabled`: the panel, one row per variant, the
 *     decision's label;
 *   - otherwise, with the experiment's `bayesian_enabled` on: the muted line;
 *     with it off: nothing.
 *
 * `seed_demo_data.py` gives at least one fixed-allocation experiment with
 * assignments (`checkout_button_color`, 15,000 / 15,000), so the "passed" line
 * is exercised against real numbers; the journey fails if no experiment
 * reaches that branch.
 *
 * The metric comparison table (#1496): every treatment row's relative
 * interval, difference and impact are compared with the API's numbers,
 * formatted by the page's rules written out below, and the journey fails if
 * no row reaches an impact with an interval. The first results page that does
 * is scanned with axe (no critical or serious violation).
 */

interface SrmBlock {
  p_value: number;
  warning: boolean;
}
interface BayesianBlock {
  is_enabled: boolean;
  decision?: string | null;
  variant_results?: unknown[];
}
type Pair = [number, number] | null | undefined;
interface VariantBody {
  variant_id: string;
  is_control: boolean;
  sample_size: number;
  p_value: number | null;
  adjusted_p_value: number | null;
  absolute_difference?: number | null;
  absolute_difference_ci?: Pair;
  relative_improvement_pct_ci?: Pair;
  incremental_impact?: number | null;
  incremental_impact_ci?: Pair;
}
interface MetricBody {
  metric_id: string;
  metric_type: string;
  analysis?: "proportion" | "mean";
  variants: VariantBody[];
}
interface ResultsBody {
  srm?: SrmBlock | null;
  bayesian_results?: BayesianBlock | null;
  confidence_level: number;
  correction_method: string;
  metrics: MetricBody[];
}
interface ExperimentRow {
  id: string;
  key: string | null;
  bayesian_enabled?: boolean;
}

/** The page's own formatting rule (SrmNotice.formatSrmP), written out. */
function expectedP(p: number): string {
  return p < 0.001 ? "p < 0.001" : `p = ${Number(p.toPrecision(2))}`;
}

const DECISION_LABELS: Record<string, string> = {
  CONTINUE: "Continue",
  STOP_WINNER: "Stop: a winner is clear",
  STOP_EQUIVALENT: "Stop: the variants are equivalent",
  STOP_FUTILE: "Stop: a meaningful difference is unlikely",
};

// The table's formatting rules (resultFormat.ts), written out, so the page is
// compared with the API's numbers rather than with its own code.
const MINUS = "−";
const NOT_ENOUGH_DATA = "Not enough data to estimate";

function finite(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}
function finitePair(v: Pair): [number, number] | null {
  return Array.isArray(v) && v.length === 2 && finite(v[0]) && finite(v[1]) ? [v[0], v[1]] : null;
}
function signed(v: number, text: string, zero: string): string {
  return text === zero ? zero : `${v < 0 ? MINUS : "+"}${text}`;
}
function pctBound(v: number, side: "lower" | "upper"): string {
  if (side === "upper" && v > 1_000_000) return "above +1,000,000%";
  if (side === "lower" && v < -1_000_000) return `below ${MINUS}1,000,000%`;
  const text = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  return text === "0.0" ? "0.0%" : `${signed(v, text, "0.0")}%`;
}
function amount(v: number): string {
  return signed(v, Math.abs(v).toFixed(2), "0.00");
}
function wholeImpact(v: number): string {
  return signed(v, Math.round(Math.abs(v)).toLocaleString("en-US"), "0");
}
function valueImpact(v: number): string {
  const text = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return text === "0.00" ? "0" : signed(v, text, "0");
}
function ciLabel(body: ResultsBody, unadjusted: boolean): string {
  const level = `${Number((body.confidence_level * 100).toFixed(1))}% CI`;
  return unadjusted ? `${level}, unadjusted,` : level;
}
function impactNoun(metric: MetricBody): string {
  if (metric.analysis !== "mean") {
    return metric.metric_type === "conversion" ? "converting users" : "users who sent the event";
  }
  return { count: "events", revenue: "in revenue", duration: "in duration" }[metric.metric_type] ?? "in value";
}

interface ExpectedRow {
  interval: string | null;
  difference: string | null;
  impact: string;
  impactHasInterval: boolean;
}

function expectedRow(body: ResultsBody, metric: MetricBody, v: VariantBody): ExpectedRow {
  const control = metric.variants.find((x) => x.is_control);
  const armsHaveUsers = v.sample_size > 0 && (control === undefined || control.sample_size > 0);
  const comparisons = metric.variants.filter((x) => !x.is_control && finite(x.p_value)).length;
  const unadjusted = body.correction_method !== "none" && comparisons >= 2 && finite(v.adjusted_p_value);
  const label = ciLabel(body, unadjusted);
  const mean = metric.analysis === "mean";

  const rel = finitePair(v.relative_improvement_pct_ci);
  const interval =
    armsHaveUsers && rel ? `${label} ${pctBound(rel[0], "lower")} to ${pctBound(rel[1], "upper")}` : null;

  let difference: string | null = null;
  if (armsHaveUsers && finite(v.absolute_difference)) {
    const scale = mean ? 1 : 100;
    const lead = `${amount(v.absolute_difference * scale)} ${mean ? "per user" : "percentage points"}`;
    const ci = finitePair(v.absolute_difference_ci);
    if (ci) {
      difference = `${lead} (${label} ${amount(ci[0] * scale)} to ${amount(ci[1] * scale)})`;
    } else if (amount(v.absolute_difference * scale) !== "0.00") {
      difference = lead;
    }
  }

  let impact = NOT_ENOUGH_DATA;
  let impactHasInterval = false;
  if (armsHaveUsers && finite(v.incremental_impact)) {
    const format = !mean || metric.metric_type === "count" ? wholeImpact : valueImpact;
    const value = format(v.incremental_impact);
    const ci = finitePair(v.incremental_impact_ci);
    if (ci) {
      impact = `${value} ${impactNoun(metric)} (${label} ${format(ci[0])} to ${format(ci[1])})`;
      impactHasInterval = true;
    } else if (value !== "0") {
      impact = `${value} ${impactNoun(metric)}`;
    }
  }
  return { interval, difference, impact, impactHasInterval };
}

interface ShownRow {
  metric: string;
  variant: string;
  interval: string | null;
  difference: string | null;
  impact: string | null;
}

/** Each table row's lines as a sighted reader sees them (screen-reader twins left out). */
async function shownRows(page: Page): Promise<ShownRow[]> {
  return page.getByTestId("metric-comparison-table").evaluate((table) => {
    const seen = (el: Element | null): string | null => {
      if (!el) return null;
      const copy = el.cloneNode(true) as Element;
      copy.querySelectorAll(".sr-only").forEach((n) => n.remove());
      return (copy.textContent ?? "").replace(/\s+/g, " ").trim();
    };
    return Array.from(table.querySelectorAll("tbody tr[data-variant-id]")).map((tr) => ({
      metric: tr.getAttribute("data-metric-id") ?? "",
      variant: tr.getAttribute("data-variant-id") ?? "",
      interval: seen(tr.querySelector('[data-testid="change-interval"]')),
      difference: seen(tr.querySelector('[data-testid="change-difference"]')),
      impact: seen(tr.querySelector('[data-testid="impact-cell"]')),
    }));
  });
}

async function api<T>(page: Page, token: string, path: string): Promise<T> {
  const response = await page.request.get(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.status(), `GET ${path}`).toBe(200);
  return (await response.json()) as T;
}

test.describe("Journey: results checks", () => {
  test("every experiment's results page shows the API's sample-ratio check and Bayesian analysis", async ({
    adminPage,
  }) => {
    test.setTimeout(180_000);
    await adminPage.goto("/experiments");
    const token = await adminPage.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
    expect(token, "the admin session has a token").toBeTruthy();

    const list = await api<{ items: ExperimentRow[] }>(adminPage, token as string, "/api/v1/experiments/?limit=100");
    expect(list.items.length).toBeGreaterThan(0);

    const reached = {
      srmNull: 0,
      srmPassed: 0,
      srmWarning: 0,
      bayesian: 0,
      relativeInterval: 0,
      impactWithInterval: 0,
    };
    const passedKeys: (string | null)[] = [];
    let scanned = false;

    for (const experiment of list.items) {
      const body = await api<ResultsBody>(adminPage, token as string, `/api/v1/results/${experiment.id}`);

      await adminPage.goto(`/results/${experiment.id}`);
      await expect(adminPage.getByTestId("results-dashboard"), experiment.id).toBeVisible({ timeout: 20_000 });
      await expect(adminPage.getByTestId("error-state")).toHaveCount(0);

      const srm = body.srm ?? null;
      if (srm === null) {
        reached.srmNull += 1;
        await expect(adminPage.getByTestId("srm-notice")).toHaveCount(0);
        await expect(adminPage.getByTestId("srm-check-passed")).toHaveCount(0);
        await expect(adminPage.getByTestId("leading-srm-qualifier")).toHaveCount(0);
      } else if (!srm.warning) {
        reached.srmPassed += 1;
        passedKeys.push(experiment.key);
        await expect(adminPage.getByTestId("srm-notice")).toHaveCount(0);
        await expect(adminPage.getByTestId("srm-check-passed")).toContainText(`(${expectedP(srm.p_value)})`);
        await expect(adminPage.getByTestId("leading-srm-qualifier")).toHaveCount(0);
      } else {
        reached.srmWarning += 1;
        await expect(adminPage.getByTestId("srm-notice")).toBeVisible();
        await expect(adminPage.getByTestId("srm-notice").getByTestId("srm-p")).toHaveText(expectedP(srm.p_value));
        await expect(adminPage.getByTestId("srm-check-passed")).toHaveCount(0);
      }

      const bayesian = body.bayesian_results ?? null;
      if (bayesian?.is_enabled) {
        reached.bayesian += 1;
        const panel = adminPage.getByTestId("bayesian-panel");
        await expect(panel).toBeVisible();
        const rows = bayesian.variant_results ?? [];
        if (rows.length > 0) {
          await expect(panel.getByTestId("bayesian-row")).toHaveCount(rows.length);
        } else {
          await expect(panel.getByTestId("bayesian-no-data")).toBeVisible();
        }
        const label = bayesian.decision ? DECISION_LABELS[bayesian.decision] : "No decision";
        await expect(panel.getByTestId("bayesian-decision")).toHaveText(`Decision: ${label}`);
        await expect(adminPage.getByTestId("bayesian-unavailable")).toHaveCount(0);
      } else if (experiment.bayesian_enabled === true) {
        await expect(adminPage.getByTestId("bayesian-unavailable")).toBeVisible();
        await expect(adminPage.getByTestId("bayesian-panel")).toHaveCount(0);
      } else {
        await expect(adminPage.getByTestId("bayesian-panel")).toHaveCount(0);
        await expect(adminPage.getByTestId("bayesian-unavailable")).toHaveCount(0);
      }

      // The intervals and the impact (#1496), row by row, against the API.
      if (body.metrics.length === 0) continue;
      const rows = await shownRows(adminPage);
      let pageReachedImpact = false;
      for (const metric of body.metrics) {
        expect(metric.analysis, `${experiment.id} ${metric.metric_id}: analysis`).toBeDefined();
        for (const variant of metric.variants) {
          const shown = rows.find((r) => r.metric === metric.metric_id && r.variant === variant.variant_id);
          const where = `${experiment.id} ${metric.metric_id} ${variant.variant_id}`;
          expect(shown, `${where}: a row`).toBeDefined();
          if (!shown || variant.is_control) continue;
          const want = expectedRow(body, metric, variant);
          expect(shown.interval, `${where}: relative interval`).toBe(want.interval);
          expect(shown.difference, `${where}: difference`).toBe(want.difference);
          expect(shown.impact, `${where}: impact`).toBe(want.impact);
          if (want.interval !== null) reached.relativeInterval += 1;
          if (want.impactHasInterval) {
            reached.impactWithInterval += 1;
            pageReachedImpact = true;
          }
        }
      }

      if (pageReachedImpact && !scanned) {
        scanned = true;
        const scan = await new AxeBuilder({ page: adminPage }).withTags(["wcag2a", "wcag2aa"]).analyze();
        const severe = scan.violations.filter((v) => v.impact === "critical" || v.impact === "serious");
        expect(
          severe.map((v) => `[${v.impact}] ${v.id}: ${v.nodes.length} node(s)`),
          `axe on /results/${experiment.id}`,
        ).toEqual([]);
      }
    }

    // Not vacuous: the seed's balanced experiment reaches the "passed" branch.
    expect(reached.srmPassed, JSON.stringify(reached)).toBeGreaterThan(0);
    expect(passedKeys).toContain("checkout_button_color");
    // Not vacuous: some treatment's relative interval, and some impact with
    // its interval, were compared with the API's numbers.
    expect(reached.relativeInterval, JSON.stringify(reached)).toBeGreaterThan(0);
    expect(reached.impactWithInterval, JSON.stringify(reached)).toBeGreaterThan(0);
    expect(scanned, "a results page with an impact was scanned with axe").toBe(true);
  });
});
