import type { APIRequestContext, APIResponse, Page } from "@playwright/test";
import { test, expect } from "./persona.fixture";
import { API_URL } from "../env";
import { bucket, goldenRolloutCases } from "../oracles/bucketing";
import { apiState, countDelta, hashVector } from "../oracles/oracle";
import { goldenHashCases, variantFor } from "../oracles/variant-bucketing";
import { Watcher } from "../oracles/watcher";
import { serveSdkPage } from "./sdk-page";

/**
 * U-SDK's day (tests/personas/personas/u-sdk.yaml): a shop's page running this
 * checkout's JavaScript SDK in a browser of its own, with no dashboard
 * session. One serial test; each `test.step` title is its card line, word for
 * word, and each step ends with the oracles its card names, then the watcher
 * on the page.
 *
 * Before the day, dev@demo.com (the developer's session, signed in once per
 * run by the persona fixture) makes the page an API key, a running two-variant
 * experiment and a feature flag, all stamped. Everything the page shows is
 * then read back from the API with the page's key, or predicted by the
 * bucketing written from its documented rule.
 */

interface Variant {
  id: string;
  name: string;
  is_control: boolean;
  traffic_allocation: number;
}

interface Experiment {
  id: string;
  key: string;
  variants: Variant[];
}

/** What one visit showed: the page's data attributes, as text. */
interface Visit {
  user: string;
  country: string;
  variant: string;
  assigned: string;
  enabled: string;
  reason: string;
  exposures: number;
  errors: string[];
}

interface Results {
  metrics: Array<{ variants: Array<{ variant_name: string; sample_size: number; conversions: number }> }>;
}

const VISITORS = 20;
/** The first visitors are in the US, which the flag's one rule turns on; the rest are in Germany. */
const US_VISITORS = 5;
/** The flag's rollout for everyone its rule does not match. */
const ROLLOUT = 30;

test("U-SDK: the SDK app's day", async ({ developer, browser }) => {
  const { api } = developer;
  const stamp = Date.now();

  const apiKey = (await made<{ key: string }>("the key", api.post("/api/v1/api-keys", { name: `persona-sdk-${stamp}` }))).key;
  const created = await made<Experiment>(
    "the experiment",
    api.post("/api/v1/experiments/", {
      name: `Persona SDK checkout ${stamp}`,
      key: `persona_sdk_checkout_${stamp}`,
      experiment_type: "a_b",
      variants: [
        { name: "Control", is_control: true, traffic_allocation: 50 },
        { name: "Treatment", is_control: false, traffic_allocation: 50 },
      ],
      metrics: [{ name: "Purchase", event_name: "purchase", metric_type: "conversion", is_primary: true }],
    }),
  );
  await made("the start", api.post(`/api/v1/experiments/${created.id}/start`, {}), 200);
  const experiment = await api.json<Experiment>(`/api/v1/experiments/${created.id}`);
  const flag = await made<{ id: string; key: string }>(
    "the flag",
    api.post("/api/v1/feature-flags/", {
      name: `Persona SDK one-click pay ${stamp}`,
      key: `persona_sdk_one_click_${stamp}`,
      rollout_percentage: ROLLOUT,
      targeting_rules: {
        logical_operator: "AND",
        groups: [{ logical_operator: "AND", conditions: [{ attribute: "country", operator: "equals", value: "US" }] }],
      },
    }),
  );
  await made("the flag's activation", api.post(`/api/v1/feature-flags/${flag.id}/activate`, {}), 200);

  const names = experiment.variants.map((v) => v.name);
  const results = async () => perVariant(names, await api.json<Results>(`/api/v1/results/${experiment.id}?use_cache=false`));
  const before = await results();

  const server = await serveSdkPage({ apiKey, experimentKey: experiment.key, flagKey: flag.key });
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const watch = new Watcher(page);
    const request = context.request;
    const visit = async (user: string, country: string): Promise<Visit> => {
      await page.goto(`${server.origin}/?user=${encodeURIComponent(user)}&country=${country}`);
      await expect(page.locator("body")).toHaveAttribute("data-state", /^(ready|failed)$/, { timeout: 15_000 });
      return shown(page, user, country);
    };
    const visits: Visit[] = [];

    await test.step("U-SDK day 1: open the page as 20 visitors, each shown a variant and a flag answer by the JavaScript SDK", async () => {
      for (let i = 1; i <= VISITORS; i += 1) {
        visits.push(await visit(`u-sdk-${stamp}-${i}`, i <= US_VISITORS ? "US" : "DE"));
        await watch.step();
      }

      const stored = await Promise.all(visits.map((v) => storedVariant(request, apiKey, v.user, experiment.id)));
      apiState(
        "u-sdk.1.variant-stored",
        visits.map((v, i) => ({ user: v.user, variant: stored[i], assigned: "true", exposures: 1, errors: [] })),
        visits.map((v) => ({ user: v.user, variant: v.variant, assigned: v.assigned, exposures: v.exposures, errors: v.errors })),
      );

      // The experiment's bucketing, written from its documented rule, first
      // reproduces every golden hash vector, then predicts each visitor.
      const golden = goldenHashCases();
      expect(golden.cases, "golden hash vectors").toBeGreaterThan(5);
      expect(golden.disagree, "golden hash vectors the bucketing disagrees with").toBe(0);
      hashVector(
        "u-sdk.1.variant-hash",
        visits.map((v) => variantFor(v.user, experiment.key, experiment.variants)),
        visits.map((v) => v.variant),
      );

      const answers = await Promise.all(visits.map((v) => evaluate(request, apiKey, flag.key, v.user, v.country)));
      apiState(
        "u-sdk.1.flag-equals-evaluate",
        answers.map((a) => ({ enabled: String(a.enabled), reason: a.reason })),
        visits.map((v) => ({ enabled: v.enabled, reason: v.reason })),
      );

      // The flag's rollout bucketing first reproduces every golden case the
      // rollout decides, then predicts each visitor the US rule leaves out.
      const rollout = goldenRolloutCases();
      expect(rollout.cases, "golden rollout cases").toBeGreaterThan(100);
      expect(rollout.disagree, "golden rollout cases the bucketing disagrees with").toBe(0);
      const outside = visits.filter((v) => v.country !== "US");
      hashVector(
        "u-sdk.1.flag-hash",
        outside.map((v) => ({ enabled: String(bucket(v.user, flag.key) < ROLLOUT), reason: "rollout" })),
        outside.map((v) => ({ enabled: v.enabled, reason: v.reason })),
      );
      await watch.step();
    });

    await test.step("U-SDK day 2: read the experiment's results after the visits", async () => {
      const after = await results();
      countDelta(
        "u-sdk.2.visits-in-results",
        { users: tally(names, visits) },
        { users: delta(names, before.users, after.users) },
      );
      await watch.step();
    });

    await test.step("U-SDK day 3: buy from the page as 5 of those visitors", async () => {
      // Every fourth visitor, so the buyers come from both countries.
      const buyers = visits.filter((_, i) => i % 4 === 0);
      let sticky = 0;
      const errors: string[] = [];
      for (const first of buyers) {
        const again = await visit(first.user, first.country);
        if (again.variant === first.variant) sticky += 1;
        await page.getByTestId("sdk-buy").click();
        await expect(page.getByTestId("sdk-bought")).toHaveAttribute("data-sent", "true", { timeout: 15_000 });
        errors.push(...(await page.getByTestId("sdk-errors").locator("li").allInnerTexts()));
        await watch.step();
      }
      const after = await results();
      countDelta(
        "u-sdk.3.purchases-in-results",
        { buyers: 5, sticky: 5, errors: [], users: tally(names, visits), conversions: tally(names, buyers) },
        {
          buyers: buyers.length,
          sticky,
          errors,
          users: delta(names, before.users, after.users),
          conversions: delta(names, before.conversions, after.conversions),
        },
      );
      await watch.step();
    });
  } finally {
    await context.close();
    await server.stop();
  }
});

/** The setup's `what`, answering `status` (201 unless given); anything else throws, naming it. */
async function made<T = unknown>(what: string, answer: Promise<APIResponse>, status = 201): Promise<T> {
  const response = await answer;
  if (response.status() !== status) throw new Error(`setup: ${what} answered ${response.status()}`);
  return (await response.json()) as T;
}

/** What the page shows once it is ready (or failed), from its data attributes. */
async function shown(page: Page, user: string, country: string): Promise<Visit> {
  const variant = page.getByTestId("sdk-variant");
  const flag = page.getByTestId("sdk-flag");
  return {
    user,
    country,
    variant: (await variant.getAttribute("data-variant")) ?? "",
    assigned: (await variant.getAttribute("data-assigned")) ?? "",
    enabled: (await flag.getAttribute("data-enabled")) ?? "",
    reason: (await flag.getAttribute("data-reason")) ?? "",
    exposures: Number(await page.getByTestId("sdk-exposures").getAttribute("data-count")),
    errors: await page.getByTestId("sdk-errors").locator("li").allInnerTexts(),
  };
}

/** The variant `GET /api/v1/tracking/assignments/{user_id}` holds for the experiment, or null. */
async function storedVariant(
  request: APIRequestContext,
  apiKey: string,
  user: string,
  experimentId: string,
): Promise<string | null> {
  const answer = await request.get(`${API_URL}/api/v1/tracking/assignments/${encodeURIComponent(user)}`, {
    headers: { "X-API-Key": apiKey },
  });
  if (answer.status() !== 200) return `answered ${answer.status()}`;
  const rows = (await answer.json()) as Array<{ experiment_id: string; variant_name: string }>;
  return rows.find((r) => r.experiment_id === experimentId)?.variant_name ?? null;
}

/** `GET /api/v1/feature-flags/evaluate/{key}` for one visitor, with the page's key. */
async function evaluate(
  request: APIRequestContext,
  apiKey: string,
  flagKey: string,
  user: string,
  country: string,
): Promise<{ enabled: boolean; reason: string }> {
  const context = encodeURIComponent(JSON.stringify({ country }));
  const answer = await request.get(
    `${API_URL}/api/v1/feature-flags/evaluate/${flagKey}?user_id=${encodeURIComponent(user)}&context=${context}`,
    { headers: { "X-API-Key": apiKey } },
  );
  expect(answer.status(), `evaluate ${user}`).toBe(200);
  return (await answer.json()) as { enabled: boolean; reason: string };
}

/** Users and conversions per variant name, in the experiment's variant order. */
function perVariant(names: string[], results: Results): { users: number[]; conversions: number[] } {
  const rows = results.metrics[0]?.variants ?? [];
  const row = (name: string) => rows.find((r) => r.variant_name === name);
  return {
    users: names.map((n) => row(n)?.sample_size ?? 0),
    conversions: names.map((n) => row(n)?.conversions ?? 0),
  };
}

/** How many of `visits` the page showed each variant. */
function tally(names: string[], visits: Visit[]): Record<string, number> {
  return Object.fromEntries(names.map((n) => [n, visits.filter((v) => v.variant === n).length]));
}

function delta(names: string[], before: number[], after: number[]): Record<string, number> {
  return Object.fromEntries(names.map((n, i) => [n, after[i] - before[i]]));
}
