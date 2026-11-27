import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";
import { ESTIMATE_STEP_HEADING, ExperimentsPage } from "./pages/experiments.page";

/**
 * Journey: a metric's conversion window (#1934, beta), from guided setup to
 * the results page.
 *
 * An ordinary DEVELOPER (the seeded admin is a superuser, so its run would
 * prove nothing about the role) gives the primary metric a 36-hour window and
 * a second metric 168 hours in guided setup's boxes, and creates the
 * experiment. The draft's page shows both. The design edit opens each box with
 * the stored hours; the primary's becomes 24 and the second's is cleared, and
 * the save sends 24 and null. Once started, the page offers no edit and the
 * API still holds 24 and none. Users assigned through the API are all inside
 * their 1-day window, and the results page says so with the API's numbers.
 *
 * Each step reads the body the page sent and what the API then holds, not the
 * page alone. The name and key carry the attempt's number and the time, so
 * Playwright's retry creates its own experiment. A started experiment cannot
 * be deleted, so the shared journey database keeps this one, as it keeps the
 * lifecycle journey's; a draft left by a failed step and the API key made for
 * the assignments are deleted in `finally`.
 */

type Item = Record<string, unknown>;

interface VariantBody {
  variant_name: string;
  sample_size: number;
  open_window_users?: number | null;
}
interface MetricBody {
  metric_id: string;
  metric_name: string;
  conversion_window_hours?: number | null;
  variants: VariantBody[];
}
interface ResultsBody {
  status: string;
  computed_at: string;
  metrics: MetricBody[];
}

const HELP_BLANK = "Blank: an event counts however long after the assignment it comes.";
const help = (span: string) =>
  `Counts events in the ${span} from a user's assignment. Fixed once the experiment leaves draft.`;

/** Each metric's `conversion_window_hours` by name; "absent" when the body has no such key. */
function windows(metrics: Item[]): Record<string, unknown> {
  return Object.fromEntries(
    metrics.map((m) => [m.name as string, "conversion_window_hours" in m ? m.conversion_window_hours : "absent"]),
  );
}

async function bearer(page: Page): Promise<Record<string, string>> {
  await page.goto("/experiments");
  await expect(page.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
  const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
  expect(token, "the session has a token").toBeTruthy();
  return { Authorization: `Bearer ${token}` };
}

async function stored(page: Page, id: string, headers: Record<string, string>): Promise<{ status: string; metrics: Item[] }> {
  const read = await page.request.get(`${API_URL}/api/v1/experiments/${id}`, { headers });
  expect(read.status(), await read.text()).toBe(200);
  return (await read.json()) as { status: string; metrics: Item[] };
}

// The results line's rules (ConversionWindowNotice.tsx), written out, so the
// page is compared with the API's numbers rather than with its own code.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad = (n: number) => String(n).padStart(2, "0");
const share = (part: number, whole: number) => {
  const percent = Math.round((part / whole) * 100);
  if (percent === 0 && part > 0) return "<1%";
  if (percent === 100 && part < whole) return ">99%";
  return `${percent}%`;
};

/** "27 Nov 2026, 01:40 UTC"; a time the API sends without a zone is UTC. */
function computedAt(value: string): string {
  const at = new Date(/(Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`);
  return `${at.getUTCDate()} ${MONTHS[at.getUTCMonth()]} ${at.getUTCFullYear()}, ${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())} UTC`;
}

/** An active experiment's line for a conversion metric with a 1-day window. */
function oneDayNotice(body: ResultsBody, metric: MetricBody): string {
  const open = metric.variants.reduce((sum, v) => sum + (v.open_window_users ?? 0), 0);
  const total = metric.variants.reduce((sum, v) => sum + v.sample_size, 0);
  const inside = metric.variants
    .filter((v) => v.sample_size > 0)
    .map((v) => `${v.variant_name} ${share(v.open_window_users ?? 0, v.sample_size)}`)
    .join(", ");
  return (
    `${metric.metric_name}: ${open} of ${total} ${total === 1 ? "user" : "users"} (${share(open, total)}) ` +
    `${open === 1 ? "was" : "were"} still inside their 1 day ` +
    `when these results were computed (${computedAt(body.computed_at)}). ` +
    "They are in the sample size, and count as not converted until they convert. " +
    "The numbers and the comparison can change, in either direction. " +
    `Still inside: ${inside}.`
  );
}

test.describe("Journey: a metric's conversion window", () => {
  test("developer sets each metric's window, edits it on the draft, and reads who is inside it on the results", async ({
    sessions,
  }, testInfo) => {
    const page = await (await sessions("developer")).newPage();
    const experiments = new ExperimentsPage(page);
    const stamp = `${testInfo.retry}_${Date.now()}`;
    const key = `e2e_window_${stamp}`;
    let headers: Record<string, string> = {};
    let madeId: string | null = null;
    let keyId: string | null = null;
    try {
      headers = await bearer(page);

      // Guided setup: 36 hours on the primary metric, 168 on a second one.
      await experiments.gotoGuided();
      await expect(experiments.stepHeading).toHaveText("What kind of experiment?", { timeout: 15_000 });
      await experiments.nextStep("Name it and choose what to measure");
      await experiments.nameInput.fill(`E2E Window ${stamp}`);
      await experiments.keyInput.fill(key);
      await page.getByTestId("metric-name-0").fill("Purchase");
      await page.getByTestId("metric-event-0").fill("purchase");
      await expect(page.getByTestId("metric-window-0")).toHaveValue("");
      await expect(page.getByTestId("metric-window-help-0")).toHaveText(HELP_BLANK);
      await page.getByTestId("metric-window-0").fill("36");
      await expect(page.getByTestId("metric-window-help-0")).toHaveText(help("36 hours"));
      await experiments.addMetricButton.click();
      await page.getByTestId("metric-name-1").fill("Signup");
      await page.getByTestId("metric-event-1").fill("signup");
      await page.getByTestId("metric-window-1").fill("168");
      await expect(page.getByTestId("metric-window-help-1")).toHaveText(help("7 days (168 hours)"));

      await experiments.nextStep("Set up the versions users will see");
      await experiments.nextStep(ESTIMATE_STEP_HEADING);
      await expect(page.getByTestId("estimate-window-line")).toHaveText(
        "The estimate counts users, not their windows: users who join in the last 36 hours have less than 36 hours when the experiment ends.",
      );
      await experiments.nextStep("Check and create");
      await expect(page.getByTestId("review-metrics").locator("li")).toHaveText([
        "Purchase (purchase, Conversion) — primary metric, conversion window 36 hours",
        "Signup (signup, Conversion), conversion window 7 days",
      ]);

      // The create itself, not the redirect the API may answer first for the
      // path without its trailing slash.
      const answer = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          /^\/api\/v1\/experiments\/?$/.test(new URL(response.url()).pathname) &&
          (response.status() < 300 || response.status() >= 400),
        { timeout: 20_000 },
      );
      await experiments.createStepButton.click();
      const created = await answer;
      expect(created.status(), `create answered ${created.status()}: ${await created.text()}`).toBe(201);
      const sent = created.request().postDataJSON() as { metrics: Item[] };
      expect(windows(sent.metrics), "the create's body").toEqual({ Purchase: 36, Signup: 168 });
      const id = ((await created.json()) as { id: string }).id;
      madeId = id;
      await page.waitForURL((url) => url.pathname === `/experiments/${id}`, { timeout: 15_000 });
      await expect(experiments.detail).toBeVisible({ timeout: 15_000 });
      expect(windows((await stored(page, id, headers)).metrics), "stored after the create").toEqual({
        Purchase: 36,
        Signup: 168,
      });

      // The draft's page shows each window.
      const row = (metric: string) => page.getByTestId("metric-row").filter({ hasText: metric });
      await expect(row("Purchase").getByTestId("metric-window-line")).toHaveText("Conversion window: 36 hours");
      await expect(row("Signup").getByTestId("metric-window-line")).toHaveText("Conversion window: 7 days");

      // The design edit: each box opens with the stored hours; 24 on the
      // primary, the second cleared.
      const puts: Item[] = [];
      page.on("request", (request) => {
        if (request.method() === "PUT" && new URL(request.url()).pathname === `/api/v1/experiments/${id}`) {
          puts.push(request.postDataJSON() as Item);
        }
      });
      await page.getByTestId("experiment-edit-variants").click();
      const form = page.getByTestId("design-form");
      await expect(form).toBeVisible();
      // Found by name, as the API's order of metrics is not fixed.
      const names = form.locator('input[data-testid^="metric-name-"]');
      await expect(names).toHaveCount(2);
      const order = await names.evaluateAll((inputs) => inputs.map((input) => (input as HTMLInputElement).value));
      const purchase = order.indexOf("Purchase");
      const signup = order.indexOf("Signup");
      expect([purchase, signup].sort(), `the form's metrics: ${order.join(", ")}`).toEqual([0, 1]);
      await expect(form.getByTestId(`metric-window-${purchase}`)).toHaveValue("36");
      await expect(form.getByTestId(`metric-window-${signup}`)).toHaveValue("168");
      await form.getByTestId(`metric-window-${purchase}`).fill("24");
      await expect(form.getByTestId(`metric-window-help-${purchase}`)).toHaveText(help("1 day (24 hours)"));
      await form.getByTestId(`metric-window-${signup}`).fill("");
      await expect(form.getByTestId(`metric-window-help-${signup}`)).toHaveText(HELP_BLANK);

      const saved = page.waitForResponse(
        (response) =>
          response.request().method() === "PUT" && new URL(response.url()).pathname === `/api/v1/experiments/${id}`,
        { timeout: 20_000 },
      );
      await page.getByTestId("design-save").click();
      const put = await saved;
      expect(put.status(), await put.text()).toBe(200);
      await expect(page.getByTestId("design-saved")).toHaveText("Variants and metrics saved.");
      // The cleared box is sent as null, not left out.
      expect(puts).toHaveLength(1);
      expect(windows((puts[0] as { metrics: Item[] }).metrics), "the save's body").toEqual({
        Purchase: 24,
        Signup: null,
      });
      expect(windows((await stored(page, id, headers)).metrics), "stored after the save").toEqual({
        Purchase: 24,
        Signup: null,
      });
      await expect(row("Purchase").getByTestId("metric-window-line")).toHaveText("Conversion window: 1 day");
      await expect(row("Signup").getByTestId("metric-window-line")).toHaveCount(0);

      // Started: the windows are fixed. The control the started page lacks
      // was there on the draft (above).
      await experiments.startExperiment();
      await expect(page.getByTestId("experiment-edit-variants")).toHaveCount(0);
      await page.reload();
      await expect(experiments.detail).toBeVisible({ timeout: 15_000 });
      await expect(row("Purchase").getByTestId("metric-window-line")).toHaveText("Conversion window: 1 day");
      await expect(row("Signup").getByTestId("metric-window-line")).toHaveCount(0);
      await expect(page.getByTestId("experiment-edit-variants")).toHaveCount(0);
      const active = await stored(page, id, headers);
      expect(active.status).toBe("active");
      expect(windows(active.metrics), "stored after the start").toEqual({ Purchase: 24, Signup: null });

      // Eight users assigned through the API, every one inside their 1 day.
      const madeKey = await page.request.post(`${API_URL}/api/v1/api-keys`, {
        headers,
        data: { name: `e2e-window-${stamp}` },
      });
      expect(madeKey.status(), await madeKey.text()).toBe(201);
      const apiKey = (await madeKey.json()) as { id: string; key: string };
      keyId = apiKey.id;
      const assigned = new Map<string, number>();
      for (let i = 0; i < 8; i += 1) {
        const user = `e2e-window-${stamp}-${i}`;
        const given = await page.request.post(`${API_URL}/api/v1/tracking/assign`, {
          headers: { "X-API-Key": apiKey.key },
          data: { experiment_key: key, user_id: user },
        });
        expect(given.status(), `assign ${user}: ${await given.text()}`).toBe(200);
        const variant = (await given.json()) as { variant_name: string; assigned: boolean; reason: string };
        expect(variant.assigned, `${user}: ${variant.reason}`).toBe(true);
        assigned.set(variant.variant_name, (assigned.get(variant.variant_name) ?? 0) + 1);
      }

      // The results page, against the answer the API gave it.
      const results = page.waitForResponse(
        (response) =>
          response.request().method() === "GET" && new URL(response.url()).pathname === `/api/v1/results/${id}`,
        { timeout: 30_000 },
      );
      await page.goto(`/results/${id}`);
      const reply = await results;
      expect(reply.status(), await reply.text()).toBe(200);
      const body = (await reply.json()) as ResultsBody;
      await expect(page.getByTestId("results-dashboard")).toBeVisible({ timeout: 20_000 });
      await expect(page.getByTestId("error-state")).toHaveCount(0);

      const byName = Object.fromEntries(body.metrics.map((m) => [m.metric_name, m]));
      expect(Object.keys(byName).sort()).toEqual(["Purchase", "Signup"]);
      expect(byName.Purchase.conversion_window_hours).toBe(24);
      expect(byName.Signup.conversion_window_hours ?? null).toBeNull();
      expect(body.status).toBe("active");
      for (const metric of body.metrics) {
        const counted = Object.fromEntries(metric.variants.map((v) => [v.variant_name, v.sample_size]));
        const want = Object.fromEntries(metric.variants.map((v) => [v.variant_name, assigned.get(v.variant_name) ?? 0]));
        expect(counted, `${metric.metric_name}: each variant's users`).toEqual(want);
      }
      // Assigned just now, so every user is still inside the 1-day window.
      for (const v of byName.Purchase.variants) {
        expect(v.open_window_users, `Purchase, ${v.variant_name}: still inside`).toBe(v.sample_size);
      }

      await expect(page.getByTestId("window-notice")).toHaveText([oneDayNotice(body, byName.Purchase)]);
      await expect(page.getByTestId("summary-window")).toHaveText(
        "Conversion window: events in the 1 day (24 hours) from each user's assignment count.",
      );
      const table = page.getByTestId("metric-comparison-table");
      const tags = (metric: MetricBody) =>
        table.locator(`tr[data-metric-id="${metric.metric_id}"] [data-testid="metric-window-tag"]`);
      await expect(table.locator(`tr[data-metric-id="${byName.Purchase.metric_id}"]`)).toHaveCount(
        byName.Purchase.variants.length,
      );
      await expect(tags(byName.Purchase)).toHaveText(byName.Purchase.variants.map(() => "within 1 day"));
      await expect(table.locator(`tr[data-metric-id="${byName.Signup.metric_id}"]`)).toHaveCount(
        byName.Signup.variants.length,
      );
      await expect(tags(byName.Signup)).toHaveCount(0);
    } finally {
      // A step that failed before the start leaves a draft, which can go;
      // the route takes its id again as `experiment_key`.
      if (madeId) {
        const left = await page.request.get(`${API_URL}/api/v1/experiments/${madeId}`, { headers });
        if (left.ok() && ((await left.json()) as { status: string }).status === "draft") {
          const removed = await page.request.delete(
            `${API_URL}/api/v1/experiments/${madeId}?experiment_key=${madeId}`,
            { headers },
          );
          expect.soft(removed.status(), `DELETE the journey's draft ${madeId}`).toBe(204);
        }
      }
      if (keyId) {
        const removed = await page.request.delete(`${API_URL}/api/v1/api-keys/${keyId}`, { headers });
        expect.soft(removed.status(), `DELETE the journey's API key ${keyId}`).toBe(204);
      }
      await page.close();
    }
  });
});
