import type { BrowserContext, Request } from "@playwright/test";
import { test, expect } from "./persona.fixture";
import { API_URL, BASE_URL } from "../env";
import { apiState, countDelta, decide } from "../oracles/oracle";
import { SIGN_IN_REQUEST, signInByApi, signInByPage } from "../oracles/signin";
import { Watcher } from "../oracles/watcher";
import { csvByVariant, digits, downloadResultsCsv, ledgerCounts, stampDay, tryAs } from "./stamped";

/**
 * U-VIEWER's day (tests/personas/personas/u-viewer.yaml): viewer@demo.com, a
 * VIEWER in Tokyo, follows a running experiment and a feature flag, exports
 * the results and changes a password; each change the role may not make is
 * tried where the dashboard would offer it, then asked of the API with a
 * valid body and read back unchanged. One serial test; each `test.step`
 * title is its card line, word for word, and each step ends with the
 * oracles its card names, then the watcher.
 *
 * The superuser makes the day's experiment, its flag and the viewer account
 * whose password the day changes (stamped.ts), so no seeded account's
 * password ever changes.
 */

/** Changing one's own password, as a browser's request. */
const PASSWORD_CHANGE = "POST /api/v1/users/me/password";

/**
 * The read-only allow-list, per browser: the only requests other than GET
 * each of the day's browsers may send, each classified by hand. A new one
 * fails day 6 until it is classified here. The viewer's own browser only
 * signs in; the browser of the account made for the day also changes its
 * password, once.
 */
const READ_ONLY_ALLOWED: Readonly<Record<"viewer" | "account", ReadonlyArray<[string, string]>>> = {
  viewer: [[SIGN_IN_REQUEST, "signing in"]],
  account: [
    [SIGN_IN_REQUEST, "signing in"],
    [PASSWORD_CHANGE, "changing one's own password"],
  ],
};

const ROLE_NOTE_EXPERIMENT =
  "Starting, pausing, completing and archiving an experiment needs permission to change experiments, " +
  "which the ADMIN and DEVELOPER roles have; you are VIEWER. You can read its results.";
const ROLE_NOTE_FLAG =
  "Feature flags are created and changed with the feature flag permissions, which the ADMIN and DEVELOPER roles have.";

interface Results {
  metrics: Array<{
    is_primary: boolean;
    variants: Array<{ is_control: boolean; p_value: number | null }>;
  }>;
}

test("U-VIEWER: the viewer's day", async ({ viewer, request, browser }) => {
  const { page, api, watch } = viewer;
  // Every request other than GET each of the day's browsers sends, all day.
  const sent: Record<"viewer" | "account", string[]> = { viewer: [], account: [] };
  const watchNetwork = (context: BrowserContext, browserOf: "viewer" | "account") =>
    context.on("request", (r: Request) => {
      if (r.method() !== "GET") sent[browserOf].push(`${r.method()} ${new URL(r.url()).pathname}`);
    });
  watchNetwork(page.context(), "viewer");

  const day = await stampDay(request, "viewer");
  const { experiment, flag } = day;
  const ledger = ledgerCounts(day);

  // The account whose password the day changes, made for the day.
  const account = {
    username: `persona_viewer_${day.stamp}`,
    email: `persona_viewer_${day.stamp}@example.com`,
    password: `Viewer${day.stamp}a`,
    changed: `Changed${day.stamp}b`,
  };
  const made = await request.post(`${API_URL}/api/v1/users/`, {
    headers: day.superuser,
    data: { username: account.username, email: account.email, password: account.password, role: "VIEWER" },
  });
  if (made.status() !== 201) throw new Error(`setup: the viewer account answered ${made.status()}`);
  const accountId = ((await made.json()) as { id: string }).id;

  try {
    await test.step("U-VIEWER day 1: browse a running experiment and find no change offered", async () => {
      await page.goto("/experiments");
      await expect(page.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
      const row = page.getByTestId("experiment-row").filter({ hasText: experiment.name }).first();
      await expect(row).toBeVisible({ timeout: 15_000 });
      const newOffered = await page.getByTestId("new-experiment-btn").count();
      await row.getByTestId("experiment-link").click();
      await expect(page.getByTestId("experiment-detail")).toBeVisible({ timeout: 15_000 });
      await expect(page.getByTestId("experiment-status")).toHaveAttribute("data-status", "active", { timeout: 15_000 });
      // The page has decided what to offer: the note that says why not, or an action.
      const note = page.getByTestId("experiment-role-note");
      const actionButtons = page.getByTestId("experiment-actions").locator("button");
      await expect(note.or(actionButtons).first()).toBeVisible();
      decide(
        "u-viewer.1.reason-stated",
        "text_exact",
        { note: ROLE_NOTE_EXPERIMENT },
        { note: (await note.count()) === 1 ? (await note.innerText()).trim() : null },
      );

      const actions = await actionButtons.count();
      const status = await tryAs(page, "POST", `/api/v1/experiments/${experiment.id}/pause`, {});
      const after = await api.json<{ status: string }>(`/api/v1/experiments/${experiment.id}`);
      decide(
        "u-viewer.1.experiment-still-active",
        "api_refused",
        { offered: { newExperiment: 0, actions: 0 }, status: 403, after: "active" },
        { offered: { newExperiment: newOffered, actions }, status, after: after.status },
      );
      await watch.step();
    });

    await test.step("U-VIEWER day 2: browse a feature flag and find no change offered", async () => {
      await page.goto("/feature-flags");
      await expect(page.getByTestId("flags-table")).toBeVisible({ timeout: 15_000 });
      const row = page.locator(`[data-testid="flag-row"][data-flag-key="${flag.key}"]`);
      await expect(row).toBeVisible({ timeout: 15_000 });
      const listOffered = {
        newFlag: await page.getByTestId("new-flag-btn").count(),
        rowSwitch: await page.getByTestId(`flag-toggle-${flag.key}`).count(),
      };
      await row.getByTestId("flag-link").click();
      await expect(page.getByTestId("flag-detail")).toBeVisible({ timeout: 15_000 });
      // The flag's data rendered: an absent control is evidence only on a page that loaded.
      await expect(page.getByTestId("flag-key")).toHaveText(flag.key, { timeout: 15_000 });
      // The page has decided what to offer: the note that says why not, or the switch.
      const note = page.getByTestId("flag-role-note");
      await expect(note.or(page.getByTestId("flag-toggle")).first()).toBeVisible();
      decide(
        "u-viewer.2.reason-stated",
        "text_exact",
        { note: ROLE_NOTE_FLAG },
        { note: (await note.count()) === 1 ? (await note.innerText()).trim() : null },
      );

      const offered = {
        ...listOffered,
        toggle: await page.getByTestId("flag-toggle").count(),
        rolloutEditable: await page.getByTestId("rollout-percentage").isEnabled(),
        save: await page.getByTestId("save-flag").count(),
      };
      const status = await tryAs(page, "PUT", `/api/v1/feature-flags/${flag.id}`, { rollout_percentage: 80 });
      const after = await api.json<{ rollout_percentage: number; status: string; name: string }>(
        `/api/v1/feature-flags/${flag.id}`,
      );
      decide(
        "u-viewer.2.flag-unchanged",
        "api_refused",
        {
          offered: { newFlag: 0, rowSwitch: 0, toggle: 0, rolloutEditable: false, save: 0 },
          status: 403,
          after: { rollout: flag.rollout_percentage, status: flag.status, name: flag.name },
        },
        { offered, status, after: { rollout: after.rollout_percentage, status: after.status, name: after.name } },
      );
      await watch.step();
    });

    await test.step("U-VIEWER day 3: download the results as CSV", async () => {
      await page.goto(`/results/${experiment.id}`);
      await expect(page.getByTestId("metric-comparison-table")).toBeVisible({ timeout: 20_000 });
      const rows = await downloadResultsCsv(page);
      const results = await api.json<Results>(`/api/v1/results/${experiment.id}`);
      const primary = results.metrics.find((m) => m.is_primary) as Results["metrics"][0];
      const apiP = primary.variants.find((v) => !v.is_control)?.p_value;
      countDelta(
        "u-viewer.3.csv-equals-ledger",
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

    await test.step("U-VIEWER day 4: change a viewer account's own password at /account/password", async () => {
      const context = await browser.newContext({ baseURL: BASE_URL, timezoneId: "Asia/Tokyo" });
      watchNetwork(context, "account");
      try {
        const own = await context.newPage();
        const ownWatch = new Watcher(own);
        await signInByPage(own, account.email, account.password, account.username);
        await own.goto("/account/password");
        await own.getByTestId("current-password-input").fill(account.password);
        await own.getByTestId("new-password-input").fill(account.changed);
        await own.getByTestId("confirm-password-input").fill(account.changed);
        await own.getByTestId("change-password-submit").click();
        await expect(own.getByTestId("change-password-outcome")).toHaveAttribute("data-outcome", "changed", {
          timeout: 15_000,
        });

        const replaced = await signInByApi(request, account.email, account.password, `${account.username} replaced`);
        const current = await signInByApi(request, account.email, account.changed, `${account.username} current`);
        apiState(
          "u-viewer.4.only-the-new-password-signs-in",
          { replaced: 401, current: 200 },
          { replaced: replaced.status, current: current.status },
        );
        await ownWatch.step();
      } finally {
        await context.close();
      }
      await watch.step();
    });

    await test.step("U-VIEWER day 6: spend the day without sending a change outside the read-only allow-list", async () => {
      const outside = (browserOf: "viewer" | "account") => {
        const allowed = new Set(READ_ONLY_ALLOWED[browserOf].map(([request]) => request));
        return sent[browserOf].filter((s) => !allowed.has(s));
      };
      decide(
        "u-viewer.6.read-only-network",
        "network_allowlist",
        // The made account's browser signed in and changed its password once:
        // the recorder was on that browser, and the change came from it.
        { viewer: { outside: [] }, account: { outside: [], signIns: 1, passwordChanges: 1 } },
        {
          viewer: { outside: outside("viewer") },
          account: {
            outside: outside("account"),
            signIns: sent.account.filter((s) => s === SIGN_IN_REQUEST).length,
            passwordChanges: sent.account.filter((s) => s === PASSWORD_CHANGE).length,
          },
        },
      );
      await watch.step();
    });
  } finally {
    // The day's account goes even when a step failed; a stamped leftover
    // changes no other day, so the answer is not asserted.
    await request.delete(`${API_URL}/api/v1/admin/users/${accountId}`, { headers: day.superuser });
  }
});
