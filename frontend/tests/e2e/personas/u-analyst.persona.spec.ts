import { test, expect } from "./persona.fixture";
import { apiState, countDelta, decide, uiMatchesApi } from "../oracles/oracle";
import {
  SUPERUSER,
  csvByVariant,
  digits,
  downloadResultsCsv,
  ledgerCounts,
  ledgerStatistics,
  scipy,
  shownCell,
  shownTable,
  stampDay,
  tryAs,
} from "./stamped";

/**
 * U-ANALYST's day (tests/personas/personas/u-analyst.yaml): analyst@demo.com,
 * an ANALYST in London, reads a running experiment's results, exports them,
 * sizes the next test and reads the audit log; each change the role may not
 * make is tried where the dashboard would offer it, then asked of the API
 * with a valid body and read back unchanged. One serial test; each
 * `test.step` title is its card line, word for word, and each step ends with
 * the oracles its card names, then the watcher.
 *
 * The superuser makes the day's experiment and flag first (stamped.ts), so
 * the numbers are decided against the ledger of what was sent.
 */

interface VariantResult {
  variant_id: string;
  variant_name: string;
  is_control: boolean;
  sample_size: number;
  conversions: number;
  mean: number;
  p_value: number | null;
}

interface Results {
  metrics: Array<{ metric_id: string; variants: VariantResult[] }>;
  srm: { p_value: number; warning: boolean } | null;
}

/** The page's own rule for the sample ratio check's p (SrmNotice.formatSrmP), written out. */
function srmText(p: number): string {
  return p < 0.001 ? "p < 0.001" : `p = ${Number(p.toPrecision(2))}`;
}

/** The power calculator's runtime rule (fmtDays), written out. */
function runtimeText(days: number | null): string {
  if (days === null) return "--";
  if (days < 1) return "< 1 day";
  if (days < 7) return `${days.toFixed(1)} days`;
  return `${(days / 7).toFixed(1)} weeks (${Math.round(days)} days)`;
}

/** What the next test is sized for: a 10% baseline, a 20% relative lift, 1,000 users a day. */
const NEXT_TEST = { baseline: 0.1, mde: 0.2, alpha: 0.05, power: 0.9, variants: 2, daily: 1000 };

/** docs/statistics/power-analysis.md's formula, in scipy, rounded up to a whole user. */
const POWER_FORMULA = [
  "import json, math, sys",
  "from scipy.stats import norm",
  "a = json.loads(sys.argv[1])",
  "p1 = a['baseline']; p2 = p1 + p1 * a['mde']; pbar = (p1 + p2) / 2",
  "za = norm.ppf(1 - a['alpha'] / 2); zb = norm.ppf(a['power'])",
  "n = (za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2 / (p2 - p1) ** 2",
  "per = math.ceil(n)",
  "print(json.dumps({'per_variant': per, 'total': per * a['variants'], 'runtime_days': per / (a['daily'] / a['variants'])}))",
].join("\n");

test("U-ANALYST: the analyst's day", async ({ analyst, request }) => {
  const { page, api, watch } = analyst;
  const day = await stampDay(request, "analyst");
  const { experiment, flag } = day;
  const ledger = ledgerCounts(day);
  const primary = experiment.metrics.find((m) => m.is_primary) as { id: string };
  const revenue = experiment.metrics.find((m) => m.metric_type === "revenue") as { id: string };

  await test.step("U-ANALYST day 1: read a running experiment's results", async () => {
    await page.goto(`/results/${experiment.id}`);
    await expect(page.getByTestId("results-dashboard")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("metric-comparison-table")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("srm-p").first()).toBeVisible({ timeout: 20_000 });
    const results = await api.json<Results>(`/api/v1/results/${experiment.id}`);
    const variants = (results.metrics.find((m) => m.metric_id === primary.id) as Results["metrics"][0]).variants;

    // The page shows the API's numbers first, the weaker check: a page and
    // an API wrong in the same way pass it, and the ledger below catches them.
    const shown = await shownTable(page);
    uiMatchesApi(
      "u-analyst.1.page-equals-api",
      {
        variants: variants.map((v) => ({
          users: String(v.sample_size),
          rate: `${((100 * v.conversions) / v.sample_size).toFixed(2)}%`,
          p: v.is_control ? "—" : (v.p_value as number).toFixed(4),
        })),
        srm: results.srm ? srmText(results.srm.p_value) : null,
      },
      {
        variants: variants.map((v) => ({
          users: shownCell(shown, primary.id, v.variant_id, "Sample Size"),
          rate: shownCell(shown, primary.id, v.variant_id, "Value"),
          p: shownCell(shown, primary.id, v.variant_id, "p-value"),
        })),
        srm: (await page.getByTestId("srm-p").first().innerText()).trim(),
      },
    );

    const statistics = ledgerStatistics(day);
    const treatment = variants.find((v) => !v.is_control) as VariantResult;
    countDelta(
      "u-analyst.1.results-equal-ledger",
      {
        users: { Control: ledger.Control.users, Treatment: ledger.Treatment.users },
        conversions: { Control: ledger.Control.conversions, Treatment: ledger.Treatment.conversions },
        p: digits(statistics.fisherP),
        srmP: digits(statistics.srmP),
      },
      {
        users: Object.fromEntries(variants.map((v) => [v.variant_name, v.sample_size])),
        conversions: Object.fromEntries(variants.map((v) => [v.variant_name, v.conversions])),
        p: digits(treatment.p_value),
        srmP: digits(results.srm?.p_value),
      },
    );
    await watch.step();
  });

  await test.step("U-ANALYST day 2: download the results as CSV", async () => {
    const rows = await downloadResultsCsv(page);
    const results = await api.json<Results>(`/api/v1/results/${experiment.id}`);
    const variants = (results.metrics.find((m) => m.metric_id === primary.id) as Results["metrics"][0]).variants;
    const apiP = (variants.find((v) => !v.is_control) as VariantResult).p_value;
    countDelta(
      "u-analyst.2.csv-equals-ledger",
      {
        rows: 2,
        variants: {
          Control: { assignments: ledger.Control.users, conversions: ledger.Control.conversions },
          Treatment: { assignments: ledger.Treatment.users, conversions: ledger.Treatment.conversions },
        },
        p: digits(apiP),
      },
      {
        rows: rows.length,
        variants: csvByVariant(rows),
        p: digits(Number(rows.find((r) => r.variant_name === "Treatment")?.p_value)),
      },
    );
    await watch.step();
  });

  await test.step("U-ANALYST day 3: size the next test in the power calculator", async () => {
    await page.goto("/power-calculator");
    await page.locator("#power-baseline").fill(String(NEXT_TEST.baseline));
    await page.locator("#power-mde").fill(String(NEXT_TEST.mde));
    await page.locator("#power-alpha").selectOption(String(NEXT_TEST.alpha));
    await page.locator("#power-power").selectOption(String(NEXT_TEST.power));
    await page.locator("#power-variants").selectOption(String(NEXT_TEST.variants));
    await page.locator("#power-daily-users").fill(String(NEXT_TEST.daily));
    // The inputs took (the labels echo them) and the page has answered.
    await expect(page.locator('label[for="power-baseline"]')).toContainText("10.0%");
    await expect(page.locator('label[for="power-mde"]')).toContainText("20%");
    const perVariant = page.locator('output[aria-labelledby="power-per-variant-name"]');
    const runtime = page.locator('output[aria-labelledby="power-runtime-name"]');
    await expect(runtime).not.toHaveText(/^(--|\.\.\.)$/, { timeout: 10_000 });

    const answer = await api.post("/api/v1/power/sample-size", {
      baseline_rate: NEXT_TEST.baseline,
      minimum_detectable_effect: NEXT_TEST.mde,
      alpha: NEXT_TEST.alpha,
      power: NEXT_TEST.power,
      n_variants: NEXT_TEST.variants,
      daily_traffic: NEXT_TEST.daily,
    });
    expect(answer.status(), "POST /api/v1/power/sample-size").toBe(200);
    const sized = (await answer.json()) as { per_variant: number; total: number; runtime_days: number | null };
    uiMatchesApi(
      "u-analyst.3.page-equals-api",
      {
        perVariant: sized.per_variant.toLocaleString("en-US"),
        total: `Total: ${sized.total.toLocaleString("en-US")}`,
        runtime: runtimeText(sized.runtime_days),
      },
      {
        perVariant: (await perVariant.innerText()).trim(),
        total: (await page.locator('output[aria-labelledby="power-per-variant-name"] + p').innerText()).trim(),
        runtime: (await runtime.innerText()).trim(),
      },
    );
    const formula = scipy<{ per_variant: number; total: number; runtime_days: number }>(POWER_FORMULA, NEXT_TEST);
    apiState(
      "u-analyst.3.api-equals-formula",
      { perVariant: formula.per_variant, total: formula.total, runtimeDays: digits(formula.runtime_days) },
      { perVariant: sized.per_variant, total: sized.total, runtimeDays: digits(sized.runtime_days) },
    );
    await watch.step();
  });

  await test.step("U-ANALYST day 4: read the revenue metric's mean per user", async () => {
    await page.goto(`/results/${experiment.id}`);
    await expect(page.getByTestId("metric-comparison-table")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("revenue-per-user-footnote")).toBeVisible();
    const results = await api.json<Results>(`/api/v1/results/${experiment.id}`);
    const variants = (results.metrics.find((m) => m.metric_id === revenue.id) as Results["metrics"][0]).variants;
    const shown = await shownTable(page);
    uiMatchesApi(
      "u-analyst.4.page-equals-api",
      Object.fromEntries(variants.map((v) => [v.variant_name, v.mean.toFixed(2)])),
      Object.fromEntries(variants.map((v) => [v.variant_name, shownCell(shown, revenue.id, v.variant_id, "Value")])),
    );
    countDelta(
      "u-analyst.4.revenue-equals-ledger",
      {
        Control: digits(ledger.Control.revenue / ledger.Control.users),
        Treatment: digits(ledger.Treatment.revenue / ledger.Treatment.users),
      },
      Object.fromEntries(variants.map((v) => [v.variant_name, digits(v.mean)])),
    );
    await watch.step();
  });

  await test.step("U-ANALYST day 5: read the audit log", async () => {
    await page.getByTestId("nav-audit-log").click();
    await expect(page).toHaveURL(/\/admin\/audit\/?$/);
    await expect(page.getByTestId("audit-log-page")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("audit-log-table-loading")).toHaveCount(0, { timeout: 15_000 });
    // The page's own filtered request, answered, and the table drawn from it.
    const filtered = page.waitForResponse(
      (r) => new URL(r.url()).pathname === "/api/v1/audit-logs/" && new URL(r.url()).searchParams.get("entity_type") === "experiment",
    );
    await page.getByTestId("filter-entity-type").selectOption("experiment");
    await filtered;
    await expect(page.getByTestId("audit-log-table-loading")).toHaveCount(0, { timeout: 15_000 });
    const listed = await api.json<{
      items: Array<{ id: string; entity_id: string; action_type: string; user_email: string }>;
    }>("/api/v1/audit-logs/?entity_type=experiment&page=1&limit=50");
    const entries = listed.items.filter((e) => e.entity_id === experiment.id);
    const shown = await Promise.all(
      entries.map(async (e) => {
        const row = page.getByTestId(`audit-log-row-${e.id}`);
        if ((await row.count()) === 0) return null;
        return {
          actor: (await page.getByTestId(`actor-${e.id}`).innerText()).trim(),
          action: (await page.getByTestId(`action-${e.id}`).innerText()).trim(),
        };
      }),
    );
    decide(
      "u-analyst.5.other-users-entries",
      "audit_entry",
      {
        entries: [
          { action: "experiment_start", actor: SUPERUSER.email, shown: { actor: SUPERUSER.email, action: "Experiment started" } },
          { action: "experiment_create", actor: SUPERUSER.email, shown: { actor: SUPERUSER.email, action: "Experiment created" } },
        ],
        ownEntriesOnly: 0,
      },
      {
        entries: entries.map((e, i) => ({ action: e.action_type, actor: e.user_email, shown: shown[i] })),
        ownEntriesOnly: await page.getByTestId("audit-log-scope-own").count(),
      },
    );
    await watch.step();
  });

  await test.step("U-ANALYST day 6: open the new-experiment page by its address", async () => {
    const creates: string[] = [];
    const onRequest = (r: import("@playwright/test").Request) => {
      if (r.method() === "POST" && /\/api\/v1\/experiments\/?$/.test(new URL(r.url()).pathname)) creates.push(r.url());
    };
    page.on("request", onRequest);
    await page.goto("/experiments/new");
    const notice = page.getByTestId("create-not-allowed");
    await expect(notice).toBeVisible({ timeout: 15_000 });
    decide(
      "u-analyst.6.refusal-words",
      "text_exact",
      {
        says: "Your role can view experiments but not create them. Ask an admin to create it or to change your role.",
        wayOut: "/experiments",
        forms: 0,
      },
      {
        says: (await notice.locator("p").first().innerText()).trim(),
        wayOut: await notice.getByRole("link", { name: "Back to experiments" }).getAttribute("href"),
        forms: await page.locator('[data-testid="new-experiment-form"], [data-testid="wizard-step-heading"]').count(),
      },
    );

    const name = `Persona analyst refused ${day.stamp}`;
    const status = await tryAs(page, "POST", "/api/v1/experiments/", {
      name,
      key: `persona_analyst_refused_${day.stamp}`,
      experiment_type: "a_b",
      variants: [
        { name: "Control", is_control: true, traffic_allocation: 50 },
        { name: "Treatment", is_control: false, traffic_allocation: 50 },
      ],
      metrics: [{ name: "Purchase", event_name: "purchase", metric_type: "conversion", is_primary: true }],
    });
    const after = await api.json<{ items: Array<{ name: string }> }>(
      `/api/v1/experiments/?search=${encodeURIComponent(name)}`,
    );
    page.off("request", onRequest);
    decide(
      "u-analyst.6.create-refused",
      "api_refused",
      { pageCreates: 0, status: 403, stored: 0 },
      { pageCreates: creates.length, status, stored: after.items.filter((e) => e.name === name).length },
    );
    await watch.step();
  });

  await test.step("U-ANALYST day 7: try to change a feature flag", async () => {
    await page.goto(`/feature-flags/${flag.id}`);
    await expect(page.getByTestId("flag-detail")).toBeVisible({ timeout: 15_000 });
    // The flag's data rendered: an absent control is evidence only on a page that loaded.
    await expect(page.getByTestId("flag-key")).toHaveText(flag.key, { timeout: 15_000 });
    // The page has decided what to offer: the note that says why not, or the switch.
    await expect(page.getByTestId("flag-role-note").or(page.getByTestId("flag-toggle")).first()).toBeVisible();
    const offered = {
      toggle: await page.getByTestId("flag-toggle").count(),
      rolloutEditable: await page.getByTestId("rollout-percentage").isEnabled(),
      save: await page.getByTestId("save-flag").count(),
      reasonShown: await page.getByTestId("flag-role-note").count(),
    };
    const status = await tryAs(page, "PUT", `/api/v1/feature-flags/${flag.id}`, { rollout_percentage: 80 });
    const after = await api.json<{ rollout_percentage: number; status: string; name: string }>(
      `/api/v1/feature-flags/${flag.id}`,
    );
    decide(
      "u-analyst.7.flag-unchanged",
      "api_refused",
      {
        offered: { toggle: 0, rolloutEditable: false, save: 0, reasonShown: 1 },
        status: 403,
        after: { rollout: flag.rollout_percentage, status: flag.status, name: flag.name },
      },
      { offered, status, after: { rollout: after.rollout_percentage, status: after.status, name: after.name } },
    );
    await watch.step();
  });

  await test.step("U-ANALYST day 8: try to pause the running experiment", async () => {
    await page.goto(`/experiments/${experiment.id}`);
    await expect(page.getByTestId("experiment-detail")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("experiment-status")).toHaveAttribute("data-status", "active", { timeout: 15_000 });
    // The page has decided what to offer: the note that says why not, or an action.
    const note = page.getByTestId("experiment-role-note");
    await expect(note.or(page.getByTestId("experiment-actions").locator("button")).first()).toBeVisible();
    const offered = {
      actions: await page.getByTestId("experiment-actions").locator("button").count(),
      reason:
        (await note.count()) === 1 &&
        (await note.innerText()).includes("which the ADMIN and DEVELOPER roles have; you are ANALYST."),
    };
    const status = await tryAs(page, "POST", `/api/v1/experiments/${experiment.id}/pause`, {});
    const after = await api.json<{ status: string }>(`/api/v1/experiments/${experiment.id}`);
    decide(
      "u-analyst.8.experiment-still-active",
      "api_refused",
      { offered: { actions: 0, reason: true }, status: 403, after: "active" },
      { offered, status, after: after.status },
    );
    await watch.step();
  });
});
