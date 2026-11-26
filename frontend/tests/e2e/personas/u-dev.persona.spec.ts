import { spawnSync } from "node:child_process";
import type { Page } from "@playwright/test";
import { test, expect } from "./persona.fixture";
import { API_URL } from "../env";
import { bucket, goldenRolloutCases } from "../oracles/bucketing";
import { apiState, countDelta, hashVector, uiMatchesApi } from "../oracles/oracle";

/**
 * U-DEV's day (tests/personas/personas/u-dev.yaml): dev@demo.com, a DEVELOPER
 * in Los Angeles, in the dashboard from a new API key to a staged flag
 * rollout. One serial test; each `test.step` title is its card line, word for
 * word, and each step ends with the oracle its card names, then the watcher.
 * Everything the day makes carries the day's stamp, so it acts only on its
 * own objects and a second run on the same stack makes new ones.
 *
 * Day 5's p-value is scipy's, run by the Python on PATH (PERSONAS_PYTHON to
 * name another): the nightly job's has backend/requirements.txt installed.
 */

interface Experiment {
  id: string;
  key: string;
  status: string;
  end_date: string | null;
  variants: Array<{ name: string; is_control: boolean; traffic_allocation: number }>;
  metrics: Array<{ event_name: string }>;
}

test("U-DEV: the developer's day", async ({ developer }) => {
  const { page, api, watch } = developer;
  const stamp = Date.now();
  let apiKey = "";
  let experiment: Experiment | null = null;

  await test.step("U-DEV day 1: create an API key at /account/api-keys", async () => {
    const keyName = `persona-dev-${stamp}`;
    await page.goto("/account/api-keys");
    await expect(page.getByTestId("own-api-keys-page")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("own-key-name").fill(keyName);
    await page.getByTestId("own-key-submit").click();
    apiKey = (await page.getByTestId("own-key-value").innerText()).trim();
    expect(apiKey).toMatch(/^eptk_[0-9a-f]{32}$/);

    await page.reload();
    await expect(page.getByTestId("own-key-table").locator("tr", { hasText: keyName })).toHaveCount(1, {
      timeout: 15_000,
    });
    const shownAfterReload = await page.getByText(apiKey).count();
    const listed = await api.get("/api/v1/api-keys");
    const body = await listed.text();
    const named = (JSON.parse(body) as Array<{ name: string; is_active: boolean }>).filter((k) => k.name === keyName);
    apiState(
      "u-dev.1.key-listed-once",
      { status: 200, listed: 1, active: true, secretInList: false, shownAfterReload: 0 },
      {
        status: listed.status(),
        listed: named.length,
        active: named[0]?.is_active,
        secretInList: body.includes(apiKey),
        shownAfterReload,
      },
    );
    await watch.step();
  });

  await test.step("U-DEV day 2: build an experiment in the dashboard's guided setup with two variants and one metric", async () => {
    const name = `Persona dev checkout ${stamp}`;
    await page.goto("/experiments/new");
    await expect(page.getByTestId("wizard-step-heading")).toHaveText("What kind of experiment?", { timeout: 15_000 });
    await page.getByTestId("wizard-next").click();
    await expect(page.getByTestId("wizard-step-heading")).toHaveText("Name it and choose what to measure");
    await page.getByTestId("experiment-name").fill(name);
    await page.locator('[data-testid^="metric-event-"]').first().fill("purchase");
    await page.getByTestId("wizard-next").click();
    await expect(page.getByTestId("wizard-step-heading")).toHaveText("Set up the versions users will see");
    await page.getByTestId("wizard-next").click();
    await page.getByTestId("wizard-next").click();
    await expect(page.getByTestId("wizard-step-heading")).toHaveText("Check and create");
    await page.getByTestId("wizard-create").click();
    await page.waitForURL(/\/experiments\/(?!new)[^/?]+$/, { timeout: 15_000 });
    await expect(page.getByTestId("experiment-detail")).toBeVisible({ timeout: 15_000 });
    const id = new URL(page.url()).pathname.split("/").pop() as string;

    experiment = await api.json<Experiment>(`/api/v1/experiments/${id}`);
    apiState(
      "u-dev.2.draft-with-key",
      { status: "draft", key: `persona_dev_checkout_${stamp}`, allocation: 100, variants: 2, metrics: 1 },
      {
        status: experiment.status,
        key: experiment.key,
        allocation: experiment.variants.reduce((sum, v) => sum + v.traffic_allocation, 0),
        variants: experiment.variants.length,
        metrics: experiment.metrics.length,
      },
    );
    await watch.step();
  });

  await test.step("U-DEV day 3: start the experiment", async () => {
    const id = (experiment as Experiment).id;
    await page.getByTestId("action-start").click();
    await expect(page.getByTestId("experiment-status")).toHaveAttribute("data-status", "active", { timeout: 15_000 });
    const read = await api.json<Experiment>(`/api/v1/experiments/${id}`);
    apiState("u-dev.3.active", { status: "active" }, { status: read.status });
    await watch.step();
  });

  // The day's own ledger of what it sent: user -> variant, and who converted.
  const ledger = new Map<string, string>();
  const converted = new Set<string>();

  await test.step("U-DEV day 4: send 200 users through assign and 40 conversions with the new key", async () => {
    const { id, key } = experiment as Experiment;
    const before = (await api.json<Results>(`/api/v1/results/${id}`)).summary.total_users;
    const headers = { "X-API-Key": apiKey };
    for (let i = 1; i <= 200; i += 1) {
      const user = `u-dev-${stamp}-${i}`;
      const answer = await page.request.post(`${API_URL}/api/v1/tracking/assign`, {
        headers,
        data: { experiment_key: key, user_id: user },
      });
      expect(answer.status(), `assign ${i}`).toBe(200);
      const body = (await answer.json()) as { variant_name: string; assigned: boolean };
      expect(body.assigned, `assign ${i}`).toBe(true);
      ledger.set(user, body.variant_name);
    }
    // A repeat assign returns the variant the user already has.
    let sticky = 0;
    const users = Array.from(ledger.keys());
    for (const user of users.slice(0, 10)) {
      const again = await page.request.post(`${API_URL}/api/v1/tracking/assign`, {
        headers,
        data: { experiment_key: key, user_id: user },
      });
      const body = (await again.json()) as { variant_name: string };
      if (again.status() === 200 && body.variant_name === ledger.get(user)) sticky += 1;
    }
    // 40 users convert once each: 25 from the treatment, the rest from the control.
    const control = (experiment as Experiment).variants.find((v) => v.is_control)?.name;
    const treated = users.filter((u) => ledger.get(u) !== control).slice(0, 25);
    const rest = users.filter((u) => ledger.get(u) === control).slice(0, 40 - treated.length);
    for (const user of treated.concat(rest)) {
      const sent = await page.request.post(`${API_URL}/api/v1/tracking/track`, {
        headers,
        data: { event_type: "purchase", user_id: user, experiment_key: key },
      });
      expect(sent.ok(), `track ${user}`).toBe(true);
      converted.add(user);
    }
    const after = (await api.json<Results>(`/api/v1/results/${id}`)).summary.total_users;
    countDelta(
      "u-dev.4.assignments-equal-ledger",
      { assigned: ledger.size, sticky: 10, conversionsSent: 40 },
      { assigned: after - before, sticky, conversionsSent: converted.size },
    );
    await watch.step();
  });

  await test.step("U-DEV day 5: read the results page", async () => {
    const { id } = experiment as Experiment;
    await page.goto(`/results/${id}`);
    await expect(page.getByTestId("results-dashboard")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("metric-comparison-table")).toBeVisible({ timeout: 20_000 });
    const results = await api.json<Results>(`/api/v1/results/${id}`);
    const variants = results.metrics[0].variants;

    // The page shows the API's numbers, row by row. First, because it is the
    // weaker check: a page and an API wrong in the same way pass it, and the
    // ledger below is what catches them.
    const shown = await shownTable(page);
    const cell = (variantId: string, column: string): string => {
      const row = shown.rows.find((r) => r.variant === variantId);
      return row ? row.cells[shown.headers.indexOf(column)] ?? "" : "";
    };
    uiMatchesApi(
      "u-dev.5.page-equals-api",
      variants.map((v) => ({
        users: String(v.sample_size),
        rate: `${((100 * v.conversions) / v.sample_size).toFixed(2)}%`,
        p: v.is_control ? "—" : (v.p_value as number).toFixed(4),
      })),
      variants.map((v) => ({
        users: cell(v.variant_id, "Sample Size"),
        rate: cell(v.variant_id, "Value"),
        p: cell(v.variant_id, "p-value"),
      })),
    );

    // The ledger's counts, and the p-value scipy gives for them (Fisher's
    // exact test, two-sided, as docs/architecture/technical-guide.md states).
    const users: Record<string, number> = {};
    const conversions: Record<string, number> = {};
    ledger.forEach((variant, user) => {
      users[variant] = (users[variant] ?? 0) + 1;
      conversions[variant] = (conversions[variant] ?? 0) + (converted.has(user) ? 1 : 0);
    });
    const control = variants.find((v) => v.is_control) as Results["metrics"][0]["variants"][0];
    const treatment = variants.find((v) => !v.is_control) as Results["metrics"][0]["variants"][0];
    const p = scipyFisherP(
      [conversions[treatment.variant_name], users[treatment.variant_name] - conversions[treatment.variant_name]],
      [conversions[control.variant_name], users[control.variant_name] - conversions[control.variant_name]],
    );
    countDelta(
      "u-dev.5.results-equal-ledger",
      { users, conversions, p: Number(p.toPrecision(12)) },
      {
        users: Object.fromEntries(variants.map((v) => [v.variant_name, v.sample_size])),
        conversions: Object.fromEntries(variants.map((v) => [v.variant_name, v.conversions])),
        p: treatment.p_value === null ? null : Number(treatment.p_value.toPrecision(12)),
      },
    );
    await watch.step();
  });

  let flag: { id: string; key: string } | null = null;

  await test.step("U-DEV day 6: target a feature flag at country US with a 20% rollout", async () => {
    await page.goto("/feature-flags/new");
    await page.getByTestId("flag-name-input").fill(`Persona dev flag ${stamp}`);
    const section = page.getByTestId("flag-targeting-section");
    await section.getByTestId("add-group").click();
    const condition = section.getByTestId("condition-row").first();
    await expect(condition).toBeVisible();
    await condition.getByTestId("condition-attribute").fill("country");
    await condition.getByTestId("condition-operator").selectOption("equals");
    await condition.getByTestId("condition-value").fill("US");
    await page.getByTestId("rollout-percentage").fill("20");
    await page.getByTestId("submit-flag").click();
    await page.waitForURL(/\/feature-flags\/(?!new)[^/?]+$/, { timeout: 15_000 });
    await expect(page.getByTestId("flag-detail")).toBeVisible({ timeout: 15_000 });
    // A new flag is off; the developer switches it on.
    await page.getByTestId("flag-toggle").click();
    await expect(page.getByTestId("flag-status")).toHaveAttribute("data-status", "active", { timeout: 15_000 });
    const id = new URL(page.url()).pathname.split("/").pop() as string;
    flag = { id, key: await api.json<{ key: string }>(`/api/v1/feature-flags/${id}`).then((f) => f.key) };

    const evaluate = async (user: string, country: string) => {
      const context = encodeURIComponent(JSON.stringify({ country }));
      const answer = await page.request.get(
        `${API_URL}/api/v1/feature-flags/evaluate/${(flag as { key: string }).key}?user_id=${user}&context=${context}`,
        { headers: { "X-API-Key": apiKey } },
      );
      expect(answer.status(), `evaluate ${user}`).toBe(200);
      return (await answer.json()) as { enabled: boolean; reason: string };
    };
    const us = Array.from({ length: 5 }, (_, i) => `u-dev-us-${stamp}-${i}`);
    const de = Array.from({ length: 40 }, (_, i) => `u-dev-de-${stamp}-${i}`);
    const usAnswers = await Promise.all(us.map((u) => evaluate(u, "US")));
    const deAnswers = await Promise.all(de.map((u) => evaluate(u, "DE")));
    apiState(
      "u-dev.6.evaluate-by-rule",
      { us: us.map(() => ({ enabled: true, reason: "targeting_rule" })), de: de.map(() => "rollout") },
      { us: usAnswers.map((a) => ({ enabled: a.enabled, reason: a.reason })), de: deAnswers.map((a) => a.reason) },
    );

    // The bucketing written from its documented rule first reproduces every
    // golden case the rollout decides, then predicts each DE user.
    const golden = goldenRolloutCases();
    expect(golden.cases, "golden rollout cases").toBeGreaterThan(100);
    expect(golden.disagree, "golden rollout cases the bucketing disagrees with").toBe(0);
    hashVector(
      "u-dev.6.hash-vector",
      de.map((u) => bucket(u, (flag as { key: string }).key) < 20),
      deAnswers.map((a) => a.enabled),
    );
    await watch.step();
  });

  await test.step("U-DEV day 7: add a rollout schedule of 10, 50 and 100 percent", async () => {
    const { id } = flag as { id: string };
    await page.getByTestId("rollout-schedule-create").click();
    const form = page.getByTestId("rollout-schedule-form");
    await expect(form).toBeVisible();
    await page.getByTestId("rollout-schedule-name-input").fill(`Persona dev rollout ${stamp}`);
    const stages = form.getByTestId("rollout-stage-fields");
    const targets = ["10", "50", "100"];
    for (let i = 0; i < targets.length; i += 1) {
      if (i > 0) await page.getByTestId("rollout-stage-add").click();
      await stages.nth(i).getByLabel(`Stage ${i + 1} name`, { exact: true }).fill(`Stage ${i + 1}`);
      await stages.nth(i).getByLabel(`Stage ${i + 1} target (% of users)`, { exact: true }).fill(targets[i]);
    }
    await page.getByTestId("rollout-schedule-submit").click();
    await expect(page.getByTestId("rollout-schedule-status")).toHaveText("Draft", { timeout: 15_000 });
    await page.getByTestId("rollout-schedule-activate").click();
    await page.getByTestId("rollout-schedule-confirm-yes").click();
    await expect(page.getByTestId("rollout-schedule-status")).toHaveText("Active", { timeout: 15_000 });
    await page.getByTestId("rollout-stage-start").click();
    await page.getByTestId("rollout-schedule-confirm-yes").click();
    await expect(page.getByTestId("rollout-stage").first()).toContainText("In progress", { timeout: 15_000 });

    const list = await api.json<{ items: Array<{ id: string; stages: Array<{ target_percentage: number }> }> }>(
      `/api/v1/rollout-schedules/?feature_flag_id=${id}`,
    );
    const stored = await api.json<{ rollout_percentage: number }>(`/api/v1/feature-flags/${id}`);
    apiState(
      "u-dev.7.stages-stored",
      { schedules: 1, stages: [10, 50, 100], flagRollout: 10 },
      {
        schedules: list.items.length,
        stages: (list.items[0]?.stages ?? []).map((s) => s.target_percentage),
        flagRollout: stored.rollout_percentage,
      },
    );
    await watch.step();
  });

  await test.step("U-DEV day 8: set an experiment end date in Los Angeles time", async () => {
    // A draft is what takes dates: the developer clones the day's experiment.
    await page.goto(`/experiments/${(experiment as Experiment).id}`);
    await expect(page.getByTestId("experiment-detail")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("experiment-clone").click();
    await page.waitForURL((url) => !url.pathname.endsWith((experiment as Experiment).id), { timeout: 15_000 });
    await expect(page.getByTestId("experiment-status")).toHaveAttribute("data-status", "draft", { timeout: 15_000 });
    const cloneId = new URL(page.url()).pathname.split("/").pop() as string;
    await page.getByTestId("experiment-schedule").click();
    await expect(page.getByTestId("schedule-end")).toBeVisible();
    await page.getByTestId("schedule-end").fill("2099-07-05T09:00");
    await expect(page.getByTestId("schedule-end-readback")).toHaveText(
      "Saved as Sun, Jul 5, 2099, 9:00 AM PDT (16:00 UTC).",
    );
    await page.getByTestId("schedule-save").click();
    await expect(page.getByTestId("schedule-saved")).toHaveText("Dates saved.");

    const stored = await api.json<Experiment>(`/api/v1/experiments/${cloneId}`);
    apiState(
      "u-dev.8.end-date-utc",
      { status: "draft", end: "2099-07-05T16:00:00.000Z" },
      { status: stored.status, end: stored.end_date ? asUtc(stored.end_date) : null },
    );
    await watch.step();
  });
});

/** The API sends a stored time as UTC with no zone; read it as UTC. */
function asUtc(value: string): string {
  return new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`).toISOString();
}

/** scipy.stats.fisher_exact's two-sided p for [[a, b], [c, d]], run by Python. */
function scipyFisherP(treatment: [number, number], control: [number, number]): number {
  const script = "import json,sys\nfrom scipy import stats\nprint(repr(float(stats.fisher_exact(json.loads(sys.argv[1]))[1])))";
  const run = spawnSync(process.env.PERSONAS_PYTHON ?? "python3", ["-c", script, JSON.stringify([treatment, control])], {
    encoding: "utf8",
  });
  if (run.status !== 0) throw new Error(`scipy did not run: ${run.stderr}`);
  return Number(run.stdout.trim());
}

/** The comparison table as a sighted reader sees it (screen-reader text left out). */
async function shownTable(page: Page): Promise<{ headers: string[]; rows: Array<{ variant: string; cells: string[] }> }> {
  return page.getByTestId("metric-comparison-table").evaluate((table) => {
    const seen = (el: Element): string => {
      const copy = el.cloneNode(true) as Element;
      copy.querySelectorAll(".sr-only").forEach((n) => n.remove());
      return (copy.textContent ?? "").replace(/[↑↓↕]/g, "").replace(/\s+/g, " ").trim();
    };
    return {
      headers: Array.from(table.querySelectorAll("thead th")).map(seen),
      rows: Array.from(table.querySelectorAll("tbody tr[data-variant-id]")).map((tr) => ({
        variant: tr.getAttribute("data-variant-id") ?? "",
        cells: Array.from(tr.querySelectorAll("th, td")).map(seen),
      })),
    };
  });
}

interface Results {
  summary: { total_users: number };
  metrics: Array<{
    variants: Array<{
      variant_id: string;
      variant_name: string;
      is_control: boolean;
      sample_size: number;
      conversions: number;
      p_value: number | null;
    }>;
  }>;
}
