import type { APIRequestContext, Page, Response } from "@playwright/test";
import { test, expect } from "./persona.fixture";
import { API_URL } from "../env";
import { apiState, decide, uiMatchesApi } from "../oracles/oracle";
import { signInByApi } from "../oracles/signin";
import { openAccount, seededAccount, type AccountSession } from "./accounts";

/**
 * U-OWNER's day (tests/personas/personas/u-owner.yaml): admin@demo.com, the
 * Admin role and a superuser, in Berlin, runs the install for the team. It
 * adds an account, changes its role, rotates an API key, changes the global
 * safety settings and its own notifications, reads the schedulers and the
 * audit log, gives the account a custom role and a workspace invitation (full
 * profile), and deactivates it. One serial test; each `test.step` title is
 * its card line, word for word, and each step ends with the oracles its card
 * names, then the watcher.
 *
 * Every change is made on its page, except the custom role's assignment (the
 * RBAC API, as the Roles page says), and read back through the API. The added
 * account signs in, in a browser of its own (in Kolkata), with the temporary
 * password the page showed, so its own session sees each change. The day
 * removes the account, keys, flag, role and workspace it made; the global
 * safety settings are put back as the day found them, and the owner's
 * notification preferences keep what the day saved.
 */

interface Flag {
  id: string;
  key: string;
  rollout_percentage: number;
}

interface AdminUser {
  id: string;
  email: string;
  role: string | null;
  is_superuser: boolean;
  is_active: boolean;
}

interface AuditEntry {
  id: string;
  action_type: string;
  user_email: string;
  user_id: string | null;
  entity_id: string | null;
  entity_name: string | null;
  reason: string | null;
  /** The list answers each as a JSON string (see `fields`). */
  old_value: unknown;
  new_value: unknown;
}

/** An audit entry's old or new value as an object; the list answers it as a JSON string. */
function fields(value: unknown): Record<string, unknown> {
  let parsed = value;
  if (typeof value === "string") {
    try {
      parsed = JSON.parse(value);
    } catch {
      parsed = null;
    }
  }
  return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {};
}

interface AuditPage {
  items: AuditEntry[];
  total: number;
}

interface Threshold {
  warning_threshold: number | null;
  critical_threshold: number | null;
  comparison_type: string;
}

interface SafetySettings {
  enable_automatic_rollbacks: boolean;
  default_metrics: Record<string, Threshold> | null;
}

interface Preferences {
  notify_experiment_started: boolean;
  notify_experiment_completed: boolean;
  notify_safety_rollback: boolean;
  notify_rollout_advanced: boolean;
  slack_channel: string | null;
  email_override: string | null;
}

interface SchedulerHealth {
  scheduler_name: string;
  is_running: boolean;
  last_run_at: string | null;
  last_run_status: string | null;
  next_run_at: string | null;
  consecutive_failures: number;
}

interface Member {
  user_id: string;
  role: string;
}

const OWNER_EMAIL = "admin@demo.com";

/** The thresholds the day types on the safety page: error rate in percent, latency in ms. */
const TYPED_THRESHOLDS = { errorWarning: "4", errorCritical: "9", latencyWarning: "750", latencyCritical: "1500" };

/** The notification choices the day saves, each the other way from the defaults where it can be. */
const TYPED_CHOICES = {
  notify_experiment_started: false,
  notify_experiment_completed: true,
  notify_safety_rollback: true,
  notify_rollout_advanced: true,
};

/** The flag's rollout when the day makes it, and the one the user sets with the custom role. */
const MADE_ROLLOUT = 20;
const GRANTED_ROLLOUT = 70;

/** The Edit dialog's warning (src/components/admin/users/EditUserModal.tsx, DEACTIVATE_WARNING). */
const DEACTIVATE_WARNING =
  "This user won't be able to sign in. Requests with a token they already hold are refused, " +
  "and API keys they created stop working — including keys your applications use.";

/** apiFetch's words for a 5xx whose body is not JSON (src/services/api.ts, serverErrorMessage). */
const SERVER_ERROR_503 = "Something went wrong on the server (HTTP 503). Your administrator can find the details in the API log.";

/** An API time with no zone is UTC (src/utils/apiTimestamp.ts); the whole second it names. */
function apiSecond(value: string | null): number | null {
  if (value === null) return null;
  const zoned = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)$/.test(value) ? `${value.replace(" ", "T")}Z` : value;
  return Math.floor(Date.parse(zoned) / 1000);
}

/** The instant a page shows (in its own time zone and locale), to the second; null for a dash. */
async function shownSecond(page: Page, text: string): Promise<number | null> {
  if (text.trim() === "—") return null;
  return page.evaluate((t) => Math.floor(new Date(t).getTime() / 1000), text.trim());
}

/** The answer to `method` on exactly `path`, once it arrives. */
function answerTo(page: Page, method: string, path: string): Promise<Response> {
  return page.waitForResponse((r) => r.request().method() === method && new URL(r.url()).pathname === path);
}

/** A flag evaluation with an API key: only the status matters here. */
async function evaluateWith(request: APIRequestContext, key: string, flagKey: string, user: string): Promise<number> {
  const answer = await request.get(`${API_URL}/api/v1/feature-flags/evaluate/${flagKey}?user_id=${user}`, {
    headers: { "X-API-Key": key },
  });
  return answer.status();
}

/** Every session the day opened, closed after it whatever happened. */
const opened: AccountSession[] = [];
test.afterEach(async () => {
  for (const session of opened.splice(0)) await session.close();
});

test("U-OWNER: the owner's day", async ({ browser, request }) => {
  // Ten steps in two browsers: about 7 seconds on a local stack, but the
  // sign-in pacer may hold the added account's sign-ins for up to a minute.
  test.setTimeout(4 * 60_000);
  const stamp = Date.now();
  const owner = await openAccount(browser, seededAccount("OWNER"), "Europe/Berlin");
  opened.push(owner);
  const { page, api, watch } = owner;

  // What the day makes before it starts: a flag the keys evaluate and the custom role changes.
  const flagMade = await api.send("POST", "/api/v1/feature-flags/", {
    key: `persona-owner-${stamp}-flag`,
    name: `Persona owner flag ${stamp}`,
    rollout_percentage: MADE_ROLLOUT,
  });
  if (flagMade.status() !== 201) throw new Error(`setup: the day's flag answered ${flagMade.status()}`);
  const flag = (await flagMade.json()) as Flag;

  // The account the day adds, and what it leaves behind to remove at the end.
  const username = `persona-owner-${stamp}`;
  const email = `${username}@demo.com`;
  let userId = "";
  let password = "";
  let user: AccountSession | null = null;
  const keysToRevoke: string[] = [];
  let roleName = "";
  let workspaceId = "";
  let settingsFound: SafetySettings | null = null;
  let settingsChanged = false;

  /** The audit log's entries of one action on one thing, newest first, as the owner reads them. */
  const audited = async (entityId: string, action: string): Promise<AuditEntry[]> =>
    (await api.json<AuditPage>(`/api/v1/audit-logs/?entity_id=${entityId}&action_type=${action}&page=1&limit=50`))
      .items;
  const byOwner = (entries: AuditEntry[]) => entries.map((e) => ({ action: e.action_type, actor: e.user_email }));
  const stored = () => api.json<AdminUser>(`/api/v1/admin/users/${userId}`);
  const theUser = (): AccountSession => {
    if (!user) throw new Error("the added account has no session: day 1 or 2 did not finish");
    return user;
  };

  /** The Edit dialog has taken Save's answer: closed on a 200, else showing its error. */
  const savedOrRefused = async (status: number) => {
    if (status === 200) await expect(page.getByTestId("edit-user-modal")).toHaveCount(0, { timeout: 15_000 });
    else await expect(page.getByTestId("edit-error-message")).toBeVisible({ timeout: 15_000 });
  };

  /** The Users page, searched down to the added account, with its row loaded. */
  const openUserRow = async () => {
    await page.goto("/admin/users");
    await expect(page.getByTestId("user-table")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("user-search").fill(email);
    const edit = page.getByTestId(`edit-user-${userId}`);
    await expect(edit).toBeVisible({ timeout: 15_000 });
    return { row: page.locator("tr", { has: edit }), edit };
  };

  try {
    await test.step("U-OWNER day 1: add a user with the Developer role", async () => {
      await page.goto("/admin/users");
      await expect(page.getByTestId("user-table")).toBeVisible({ timeout: 15_000 });
      await page.getByTestId("invite-user-button").click();
      await expect(page.getByTestId("invite-user-modal")).toBeVisible();
      await page.getByTestId("invite-email-input").fill(email);
      await page.getByTestId("invite-full-name-input").fill("Persona Owner Hire");
      await page.getByTestId("invite-role-select").selectOption("DEVELOPER");
      const posted = answerTo(page, "POST", "/api/v1/users/");
      await page.getByTestId("invite-submit-button").click();
      const answer = await posted;
      // The dialog has taken the answer: the password, or the error it shows.
      const created = page.getByTestId("invite-created");
      await expect(created.or(page.getByTestId("invite-error-message")).first()).toBeVisible({ timeout: 15_000 });
      const shownOnce = (await created.count()) === 1;
      if (answer.status() === 201) userId = ((await answer.json()) as { id: string }).id;
      if (shownOnce) {
        password = (await page.getByTestId("invite-temp-password").innerText()).trim();
        await page.getByTestId("invite-done-button").click();
        await expect(page.getByTestId("invite-user-modal")).toHaveCount(0);
      }
      if (!userId) throw new Error(`POST /api/v1/users/ answered ${answer.status()}`);

      const { row } = await openUserRow();
      const cells = (await row.locator("td").allInnerTexts()).map((t) => t.trim());
      const account = await stored();
      apiState(
        "u-owner.1.user-added",
        {
          status: 201,
          passwordShown: true,
          stored: { email, role: "DEVELOPER", superuser: false, active: true },
          listed: { email, role: "Developer", superuser: "No", status: "Active" },
        },
        {
          status: answer.status(),
          passwordShown: shownOnce && password.length >= 8,
          stored: { email: account.email, role: account.role, superuser: account.is_superuser, active: account.is_active },
          listed: { email: cells[1], role: cells[2], superuser: cells[3], status: cells[4] },
        },
      );
      decide(
        "u-owner.1.creation-audited",
        "audit_entry",
        [{ action: "user_create", actor: OWNER_EMAIL }],
        byOwner(await audited(userId, "user_create")),
      );
      await watch.step();
    });

    await test.step("U-OWNER day 2: change the user's role to Analyst", async () => {
      user = await openAccount(
        browser,
        { label: username, email, password, role: "DEVELOPER", superuser: false },
        "Asia/Kolkata",
      );
      opened.push(user);
      const own = user.page;
      await own.goto("/experiments");
      await expect(own.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
      await own.waitForLoadState("networkidle");
      const offeredBefore = await own.getByTestId("new-experiment-btn").count();

      const { edit } = await openUserRow();
      await edit.click();
      await expect(page.getByTestId("edit-user-modal")).toBeVisible();
      await page.getByTestId("edit-role-select").selectOption("ANALYST");
      const patched = answerTo(page, "PATCH", `/api/v1/admin/users/${userId}`);
      await page.getByTestId("edit-save-button").click();
      const status = (await patched).status();
      // The page has taken the answer: the dialog closed, or the error it shows.
      await savedOrRefused(status);

      // The user's next page, then what their own session says they are.
      await own.goto("/experiments");
      await expect(own.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
      await own.waitForLoadState("networkidle");
      const offeredAfter = await own.getByTestId("new-experiment-btn").count();
      const me = await user.api.json<{ role: string }>("/api/v1/auth/me");
      const account = await stored();
      apiState(
        "u-owner.2.role-changed",
        { offeredBefore: 1, status: 200, stored: "ANALYST", me: "ANALYST", offeredAfter: 0 },
        { offeredBefore, status, stored: account.role, me: me.role, offeredAfter },
      );
      decide(
        "u-owner.2.change-audited",
        "audit_entry",
        [{ action: "role_assign", actor: OWNER_EMAIL, from: "DEVELOPER", to: "ANALYST" }],
        (await audited(userId, "role_assign")).map((e) => ({
          action: e.action_type,
          actor: e.user_email,
          from: fields(e.old_value).role ?? null,
          to: fields(e.new_value).role ?? null,
        })),
      );
      await user.watch.step();
      await watch.step();
    });

    await test.step("U-OWNER day 3: create an API key, rotate it and revoke the old one", async () => {
      const keyName = `persona-owner-${stamp}-service`;
      const visitor = `persona-owner-${stamp}-visitor`;
      await page.goto("/admin/api-keys");
      await expect(page.getByTestId("api-key-table")).toBeVisible({ timeout: 15_000 });
      await expect(page.getByTestId("api-key-table-loading")).toHaveCount(0, { timeout: 15_000 });

      /** Create Key on the page: the id from the API's answer, the key from the banner. */
      const createKey = async () => {
        await page.getByTestId("create-api-key-button").click();
        await expect(page.getByTestId("create-api-key-modal")).toBeVisible();
        await page.getByTestId("api-key-name-input").fill(keyName);
        const posted = answerTo(page, "POST", "/api/v1/api-keys");
        await page.getByTestId("create-api-key-submit").click();
        const answer = await posted;
        await expect(page.getByTestId("new-key-banner").or(page.getByTestId("modal-error")).first()).toBeVisible({
          timeout: 15_000,
        });
        const id = answer.status() === 201 ? ((await answer.json()) as { id: string }).id : "";
        if (id) keysToRevoke.push(id);
        const key = (await page.getByTestId("new-key-banner").count()) === 1 ? (await page.getByTestId("new-key-value").innerText()).trim() : "";
        if (key) await page.getByTestId("dismiss-key-banner").click();
        return { id, key, status: answer.status() };
      };

      const old = await createKey();
      const beforeOld = await evaluateWith(request, old.key, flag.key, visitor);
      const rotated = await createKey();
      const beforeNew = await evaluateWith(request, rotated.key, flag.key, visitor);

      // Revoke the old key on its row, once the new one works.
      let revoked: number | null = null;
      const revoke = page.getByTestId(`delete-button-${old.id}`);
      if ((await revoke.count()) === 1) {
        await revoke.click();
        await expect(page.getByTestId("delete-confirm")).toBeVisible();
        const deleted = answerTo(page, "DELETE", `/api/v1/api-keys/${old.id}`);
        await page.getByTestId("confirm-delete").click();
        revoked = (await deleted).status();
        await expect(page.getByTestId("api-key-revoked").or(page.getByTestId("delete-error")).first()).toBeVisible({
          timeout: 15_000,
        });
      }
      const afterOld = await evaluateWith(request, old.key, flag.key, visitor);
      const afterNew = await evaluateWith(request, rotated.key, flag.key, visitor);
      await page.reload();
      await expect(page.getByTestId("api-key-table-loading")).toHaveCount(0, { timeout: 15_000 });
      await expect(page.getByTestId(`api-key-row-${rotated.id}`)).toBeVisible({ timeout: 15_000 });
      const newBadge = page.getByTestId(`status-badge-${rotated.id}`);
      apiState(
        "u-owner.3.old-key-refused",
        {
          created: [201, 201],
          shown: [true, true],
          before: { old: 200, new: 200 },
          revoked: 204,
          after: { old: 401, new: 200 },
          listed: { old: 0, new: "active" },
        },
        {
          created: [old.status, rotated.status],
          shown: [old.key.length > 0, rotated.key.length > 0],
          before: { old: beforeOld, new: beforeNew },
          revoked,
          after: { old: afterOld, new: afterNew },
          listed: {
            old: await page.getByTestId(`api-key-row-${old.id}`).count(),
            new: (await newBadge.count()) === 1 ? (await newBadge.innerText()).trim() : null,
          },
        },
      );
      decide(
        "u-owner.3.keys-audited",
        "audit_entry",
        {
          old: [
            { action: "api_key_create", actor: OWNER_EMAIL },
            { action: "api_key_revoke", actor: OWNER_EMAIL },
          ],
          new: [{ action: "api_key_create", actor: OWNER_EMAIL }],
        },
        {
          old: [...byOwner(await audited(old.id, "api_key_create")), ...byOwner(await audited(old.id, "api_key_revoke"))],
          new: [...byOwner(await audited(rotated.id, "api_key_create")), ...byOwner(await audited(rotated.id, "api_key_revoke"))],
        },
      );
      await watch.step();
    });

    await test.step("U-OWNER day 4: change the global safety settings", async () => {
      const found = await api.json<SafetySettings>("/api/v1/safety/settings");
      settingsFound = { enable_automatic_rollbacks: found.enable_automatic_rollbacks, default_metrics: found.default_metrics };
      const saves = async () =>
        api.json<AuditPage>("/api/v1/audit-logs/?action_type=safety_config_update&page=1&limit=1");
      const savesBefore = (await saves()).total;

      await page.goto("/admin/safety");
      await expect(page.getByTestId("safety-settings-form")).toBeVisible({ timeout: 20_000 });
      await page.getByTestId("error-rate-warning-input").fill(TYPED_THRESHOLDS.errorWarning);
      await page.getByTestId("error-rate-critical-input").fill(TYPED_THRESHOLDS.errorCritical);
      await page.getByTestId("latency-warning-input").fill(TYPED_THRESHOLDS.latencyWarning);
      await page.getByTestId("latency-critical-input").fill(TYPED_THRESHOLDS.latencyCritical);
      const posted = answerTo(page, "POST", "/api/v1/safety/settings");
      await page.getByTestId("save-settings-button").click();
      const status = (await posted).status();
      settingsChanged = true;
      await expect(page.getByTestId("settings-success").or(page.getByTestId("settings-error")).first()).toBeVisible({
        timeout: 15_000,
      });

      await page.reload();
      await expect(page.getByTestId("safety-settings-form")).toBeVisible({ timeout: 20_000 });
      const shown = {
        automatic: await page.getByTestId("auto-rollback-checkbox").isChecked(),
        errorWarning: await page.getByTestId("error-rate-warning-input").inputValue(),
        errorCritical: await page.getByTestId("error-rate-critical-input").inputValue(),
        latencyWarning: await page.getByTestId("latency-warning-input").inputValue(),
        latencyCritical: await page.getByTestId("latency-critical-input").inputValue(),
      };
      const now = await api.json<SafetySettings>("/api/v1/safety/settings");
      const metrics = now.default_metrics ?? {};
      apiState(
        "u-owner.4.settings-saved",
        {
          status: 200,
          stored: {
            automatic: found.enable_automatic_rollbacks,
            errorRate: [0.04, 0.09],
            latency: [750, 1500],
          },
          shown: { automatic: found.enable_automatic_rollbacks, ...TYPED_THRESHOLDS },
        },
        {
          status,
          stored: {
            automatic: now.enable_automatic_rollbacks,
            errorRate: [metrics.error_rate?.warning_threshold ?? null, metrics.error_rate?.critical_threshold ?? null],
            latency: [metrics.latency?.warning_threshold ?? null, metrics.latency?.critical_threshold ?? null],
          },
          shown,
        },
      );
      const latest = await saves();
      const entry = latest.items[0];
      const savedMetrics = (fields(entry?.new_value).default_metrics ?? {}) as Record<string, Threshold>;
      decide(
        "u-owner.4.save-audited",
        "audit_entry",
        { more: 1, actor: OWNER_EMAIL, errorRate: [0.04, 0.09], latency: [750, 1500] },
        {
          more: latest.total - savesBefore,
          actor: entry?.user_email ?? null,
          errorRate: [savedMetrics.error_rate?.warning_threshold ?? null, savedMetrics.error_rate?.critical_threshold ?? null],
          latency: [savedMetrics.latency?.warning_threshold ?? null, savedMetrics.latency?.critical_threshold ?? null],
        },
      );
      // As the day found them, before anything reads the typed thresholds.
      const restored = await api.send("POST", "/api/v1/safety/settings", settingsFound);
      if (restored.status() !== 200) throw new Error(`the safety settings put back answered ${restored.status()}`);
      settingsChanged = false;
      await watch.step();
    });

    await test.step("U-OWNER day 5: change one's own notification preferences", async () => {
      const channel = `#persona-owner-${stamp}`;
      const address = `persona-owner-${stamp}-alerts@example.com`;
      await page.goto("/admin/notifications");
      await expect(page.getByTestId("notification-prefs-form")).toBeVisible({ timeout: 15_000 });
      for (const [name, checked] of Object.entries(TYPED_CHOICES)) {
        await page.getByTestId(`pref-${name}`).setChecked(checked);
      }
      await page.getByTestId("slack-channel-input").fill(channel);
      await page.getByTestId("email-override-input").fill(address);
      const put = answerTo(page, "PUT", "/api/v1/notifications/preferences");
      await page.getByTestId("save-prefs-btn").click();
      const status = (await put).status();

      await page.reload();
      await expect(page.getByTestId("notification-prefs-form")).toBeVisible({ timeout: 15_000 });
      const shown: Record<string, unknown> = {};
      for (const name of Object.keys(TYPED_CHOICES)) shown[name] = await page.getByTestId(`pref-${name}`).isChecked();
      shown.slack_channel = await page.getByTestId("slack-channel-input").inputValue();
      shown.email_override = await page.getByTestId("email-override-input").inputValue();
      const saved = await api.json<Preferences>("/api/v1/notifications/preferences");
      const typed = { ...TYPED_CHOICES, slack_channel: channel, email_override: address };
      apiState(
        "u-owner.5.preferences-saved",
        { status: 200, stored: typed, shown: typed },
        {
          status,
          stored: {
            notify_experiment_started: saved.notify_experiment_started,
            notify_experiment_completed: saved.notify_experiment_completed,
            notify_safety_rollback: saved.notify_safety_rollback,
            notify_rollout_advanced: saved.notify_rollout_advanced,
            slack_channel: saved.slack_channel,
            email_override: saved.email_override,
          },
          shown,
        },
      );
      // The same page when both reads fail, in a second tab of the owner's own
      // browser with no watcher on it: the failure there is the point.
      const failing = await page.context().newPage();
      try {
        for (const path of ["**/api/v1/notifications/preferences", "**/api/v1/notifications/delivery-log**"]) {
          await failing.route(path, (route) =>
            route.fulfill({ status: 503, contentType: "text/plain", body: "Service Unavailable" }),
          );
        }
        await failing.goto("/admin/notifications");
        await expect(failing.getByTestId("notifications-admin-page")).toBeVisible({ timeout: 15_000 });
        await expect(failing.getByTestId("loading-indicator")).toHaveCount(0, { timeout: 15_000 });
        const shownText = async (testId: string) => {
          const found = failing.getByTestId(testId);
          return (await found.count()) === 1 ? (await found.innerText()).trim() : null;
        };
        decide(
          "u-owner.5.error-shown",
          "text_exact",
          { prefs: SERVER_ERROR_503, log: SERVER_ERROR_503, loading: 0, empty: 0 },
          {
            prefs: await shownText("prefs-error"),
            log: await shownText("log-error"),
            loading: await failing.getByTestId("prefs-loading").count(),
            empty: await failing.getByTestId("empty-state").count(),
          },
        );
      } finally {
        await failing.close();
      }
      await watch.step();
    });

    await test.step("U-OWNER day 6: read the scheduler health", async () => {
      const answered = answerTo(page, "GET", "/api/v1/scheduler/health");
      await page.goto("/admin/scheduler");
      const answer = await answered;
      const health = answer.status() === 200 ? ((await answer.json()) as SchedulerHealth[]) : [];
      await expect(page.getByTestId("scheduler-loading")).toHaveCount(0, { timeout: 15_000 });
      const cards = page.getByTestId("scheduler-status-card");
      const shown: Array<Record<string, string | number | null>> = [];
      for (let i = 0; i < (await cards.count()); i += 1) {
        const card = cards.nth(i);
        shown.push({
          name: (await card.locator("h3").innerText()).trim(),
          state: (await card.getByTestId("status-badge").innerText()).trim(),
          lastRun: await shownSecond(page, await card.getByTestId("last-run").innerText()),
          lastStatus: (await card.getByTestId("last-run-status").innerText()).trim(),
          nextRun: await shownSecond(page, await card.getByTestId("next-run").innerText()),
          failures: Number((await card.getByTestId("consecutive-failures").innerText()).trim()),
        });
      }
      uiMatchesApi(
        "u-owner.6.page-equals-api",
        {
          status: 200,
          empty: health.length === 0 ? 1 : 0,
          cards: health.map((s) => ({
            name: s.scheduler_name,
            state: s.is_running ? "running" : "idle",
            lastRun: apiSecond(s.last_run_at),
            lastStatus: s.last_run_status ?? "—",
            nextRun: apiSecond(s.next_run_at),
            failures: s.consecutive_failures,
          })),
        },
        { status: answer.status(), empty: await page.getByTestId("scheduler-empty").count(), cards: shown },
      );

      // The same page when the request fails, in a second tab of the owner's
      // own browser with no watcher on it: the failure there is the point.
      const failing = await page.context().newPage();
      try {
        await failing.route("**/api/v1/scheduler/health", (route) =>
          route.fulfill({ status: 503, contentType: "text/plain", body: "Service Unavailable" }),
        );
        await failing.goto("/admin/scheduler");
        await expect(failing.getByTestId("scheduler-health-page")).toBeVisible({ timeout: 15_000 });
        await expect(failing.getByTestId("scheduler-loading")).toHaveCount(0, { timeout: 15_000 });
        const error = failing.getByTestId("scheduler-error");
        decide(
          "u-owner.6.error-shown",
          "text_exact",
          { error: SERVER_ERROR_503, empty: 0, cards: 0 },
          {
            error: (await error.count()) === 1 ? (await error.innerText()).trim() : null,
            empty: await failing.getByTestId("scheduler-empty").count(),
            cards: await failing.getByTestId("scheduler-status-card").count(),
          },
        );
      } finally {
        await failing.close();
      }
      await watch.step();
    });

    await test.step("U-OWNER day 7: find the role change in the audit log, filtered to role changes", async () => {
      await page.goto("/admin/audit");
      await expect(page.getByTestId("audit-log-filter")).toBeVisible({ timeout: 15_000 });
      await expect(page.getByTestId("audit-log-table-loading")).toHaveCount(0, { timeout: 20_000 });
      // Each choice reloads the list: wait for that answer before the next, so
      // the rows read are the last choice's, whatever the page asked for.
      for (const [filter, value] of [
        ["filter-action-type", "role_assign"],
        ["filter-entity-type", "user"],
      ]) {
        const reloaded = answerTo(page, "GET", "/api/v1/audit-logs/");
        await page.getByTestId(filter).selectOption(value);
        await reloaded;
        await expect(page.getByTestId("audit-log-table-loading")).toHaveCount(0, { timeout: 20_000 });
      }
      const rows = page.locator('[data-testid^="audit-log-row-"]');
      const listed: Array<{ id: string; actor: string; action: string }> = [];
      for (let i = 0; i < (await rows.count()); i += 1) {
        const id = ((await rows.nth(i).getAttribute("data-testid")) ?? "").replace("audit-log-row-", "");
        listed.push({
          id,
          actor: (await page.getByTestId(`actor-${id}`).innerText()).trim(),
          action: (await page.getByTestId(`action-${id}`).innerText()).trim(),
        });
      }
      const answer = await api.json<AuditPage>("/api/v1/audit-logs/?action_type=role_assign&entity_type=user&page=1&limit=50");
      uiMatchesApi(
        "u-owner.7.rows-exact",
        answer.items.map((e) => ({ id: e.id, actor: e.user_email, action: "Role changed" })),
        listed,
      );
      const ours = answer.items.filter((e) => e.entity_id === userId);
      decide(
        "u-owner.7.change-listed",
        "audit_entry",
        [{ listed: true, actor: OWNER_EMAIL, from: "DEVELOPER", to: "ANALYST" }],
        ours.map((e) => ({
          listed: listed.some((row) => row.id === e.id),
          actor: e.user_email,
          from: fields(e.old_value).role ?? null,
          to: fields(e.new_value).role ?? null,
        })),
      );
      await watch.step();
    });

    await test.step("U-OWNER day 8: give the user a custom role that lets them change feature flags", async () => {
      const own = theUser();
      /** What the user's flag page offers them, once it has decided. */
      const offeredOnFlag = async () => {
        await own.page.goto(`/feature-flags/${flag.id}`);
        await expect(own.page.getByTestId("flag-key")).toHaveText(flag.key, { timeout: 15_000 });
        await expect(own.page.getByTestId("save-flag").or(own.page.getByTestId("flag-role-note")).first()).toBeVisible();
        return {
          toggle: await own.page.getByTestId("flag-toggle").count(),
          rolloutEditable: await own.page.getByTestId("rollout-percentage").isEnabled(),
          save: await own.page.getByTestId("save-flag").count(),
        };
      };
      const before = await offeredOnFlag();

      roleName = `persona-owner-${stamp}`;
      await page.goto("/admin/roles");
      await expect(page.getByTestId("role-table")).toBeVisible({ timeout: 20_000 });
      await expect(page.getByTestId("role-table-loading")).toHaveCount(0, { timeout: 15_000 });
      await page.getByTestId("create-role-button").click();
      await page.getByTestId("role-name-input").fill(roleName);
      await page.getByTestId("permission-checkbox-feature_flag:update").check();
      const posted = answerTo(page, "POST", "/api/v1/rbac/roles");
      await page.getByTestId("modal-submit-button").click();
      const created = (await posted).status();
      await expect(page.getByTestId("role-table")).toContainText(roleName, { timeout: 15_000 });
      // Roles are given to a user through the RBAC API, as the page says.
      const assigned = (await api.send("POST", "/api/v1/rbac/roles/assign", { user_id: userId, role_name: roleName })).status();

      const after = await offeredOnFlag();
      let status: number | null = null;
      if (after.rolloutEditable && after.save === 1) {
        await own.page.getByTestId("rollout-percentage").fill(String(GRANTED_ROLLOUT));
        await expect(own.page.getByTestId("rollout-value")).toHaveText(`${GRANTED_ROLLOUT}%`);
        const put = answerTo(own.page, "PUT", `/api/v1/feature-flags/${flag.id}`);
        await own.page.getByTestId("save-flag").click();
        status = (await put).status();
        await expect(own.page.getByTestId("save-success").or(own.page.getByTestId("save-error")).first()).toBeVisible({
          timeout: 15_000,
        });
      }
      const now = await api.json<Flag>(`/api/v1/feature-flags/${flag.id}?skip_cache=true`);
      apiState(
        "u-owner.8.grant-enforced",
        {
          before: { toggle: 0, rolloutEditable: false, save: 0 },
          created: 201,
          assigned: 200,
          after: { toggle: 1, rolloutEditable: true, save: 1 },
          status: 200,
          rollout: GRANTED_ROLLOUT,
        },
        { before, created, assigned, after, status, rollout: now.rollout_percentage },
      );
      const roleCreated = (await api.json<AuditPage>("/api/v1/audit-logs/?action_type=role_create&page=1&limit=50")).items
        .filter((e) => e.entity_name === roleName)
        .map((e) => ({ action: e.action_type, actor: e.user_email }));
      const roleGiven = (await audited(userId, "role_assign"))
        .filter((e) => fields(e.new_value).custom_role !== undefined)
        .map((e) => ({ action: e.action_type, actor: e.user_email, role: fields(e.new_value).custom_role }));
      decide(
        "u-owner.8.grant-audited",
        "audit_entry",
        {
          created: [{ action: "role_create", actor: OWNER_EMAIL }],
          given: [{ action: "role_assign", actor: OWNER_EMAIL, role: roleName }],
        },
        { created: roleCreated, given: roleGiven },
      );
      await own.watch.step();
      await watch.step();
    });

    await test.step("U-OWNER day 9: invite the user to a workspace as a viewer", async () => {
      const own = theUser();
      const made = await api.send("POST", "/api/v1/workspaces/", {
        name: `Persona owner ${stamp}`,
        slug: `persona-owner-${stamp}`,
      });
      if (made.status() !== 201) throw new Error(`setup: the day's workspace answered ${made.status()}`);
      workspaceId = ((await made.json()) as { id: string }).id;

      await page.goto(`/workspaces/${workspaceId}/members`);
      await expect(page.getByRole("heading", { name: "Members", level: 1 })).toBeVisible({ timeout: 15_000 });
      await page.getByRole("button", { name: "+ Invite Member" }).click();
      const dialog = page.getByRole("dialog", { name: "Invite Member" });
      await expect(dialog).toBeVisible();
      await dialog.locator("#invite-email").fill(email);
      await dialog.locator("#invite-role").selectOption("VIEWER");
      const posted = answerTo(page, "POST", `/api/v1/workspaces/${workspaceId}/invites`);
      await dialog.getByRole("button", { name: "Create invitation" }).click();
      const invite = await posted;
      const invited = invite.status() === 201 ? ((await invite.json()) as { role: string }) : { role: null };
      const link = dialog.locator("#invite-link");
      await expect(link.or(dialog.getByRole("alert")).first()).toBeVisible({ timeout: 15_000 });
      const href = (await link.count()) === 1 ? await link.inputValue() : "";
      await dialog.getByRole("button", { name: "Close", exact: true }).first().click();

      let accepted: number | null = null;
      if (href) {
        await own.page.goto(href);
        const accept = own.page.getByRole("button", { name: "Accept Invitation" });
        await expect(accept).toBeVisible({ timeout: 15_000 });
        const answer = answerTo(own.page, "POST", `/api/v1${new URL(href).pathname}/accept`);
        await accept.click();
        accepted = (await answer).status();
        await expect(own.page.getByText("Invitation Accepted!").or(own.page.getByRole("alert")).first()).toBeVisible({
          timeout: 15_000,
        });
        // The page then opens the workspace: let it, so the watcher reads the page the user ends on.
        if (accepted === 201) await own.page.waitForURL(`**/workspaces/${workspaceId}`, { timeout: 15_000 });
      }
      const members = await api.json<Member[]>(`/api/v1/workspaces/${workspaceId}/members`);
      apiState(
        "u-owner.9.member-role",
        { invited: 201, inviteRole: "VIEWER", linkShown: true, accepted: 201, member: "VIEWER" },
        {
          invited: invite.status(),
          inviteRole: invited.role,
          linkShown: href.includes("/workspaces/invites/"),
          accepted,
          member: members.find((m) => m.user_id === userId)?.role ?? null,
        },
      );
      await own.watch.step();
      await watch.step();
    });

    await test.step("U-OWNER day 10: deactivate the user", async () => {
      const own = theUser();
      // A key the user made for themselves, and their session: both work before.
      const ownKey = await own.api.send("POST", "/api/v1/api-keys", { name: `persona-owner-${stamp}-own` });
      if (ownKey.status() !== 201) throw new Error(`setup: the user's own key answered ${ownKey.status()}`);
      const { key, name } = (await ownKey.json()) as { key: string; name: string };
      const visitor = `persona-owner-${stamp}-visitor`;
      const before = {
        token: (await own.api.send("GET", "/api/v1/auth/me")).status(),
        key: await evaluateWith(request, key, flag.key, visitor),
      };

      const { edit } = await openUserRow();
      await edit.click();
      await expect(page.getByTestId("edit-user-modal")).toBeVisible();
      await page.getByTestId("edit-active-toggle").uncheck();
      const warning = page.getByTestId("deactivate-warning");
      await expect(warning).toBeVisible();
      // The dialog has asked which keys stop: what it found, or why it could not.
      await expect(page.getByTestId("deactivate-keys-summary")).not.toHaveText(/Checking/, { timeout: 15_000 });
      const listedKeys = await page.getByTestId("deactivate-key").locator("span.font-medium").allInnerTexts();
      decide(
        "u-owner.10.dialog-promise",
        "text_exact",
        { warning: DEACTIVATE_WARNING, keys: [name] },
        {
          warning: (await warning.locator("#edit-deactivate-warning").innerText()).trim(),
          keys: listedKeys.map((t) => t.trim()),
        },
      );
      const patched = answerTo(page, "PATCH", `/api/v1/admin/users/${userId}`);
      await page.getByTestId("edit-save-button").click();
      const status = (await patched).status();
      await savedOrRefused(status);

      const tokenAfter = await own.api.send("GET", "/api/v1/auth/me");
      const signIn = await signInByApi(request, email, password, `${username} deactivated`);
      decide(
        "u-owner.10.access-ended",
        "api_refused",
        {
          status: 200,
          active: false,
          before: { token: 200, key: 200 },
          after: { token: 400, detail: "Inactive user", key: 401, signIn: 401 },
        },
        {
          status,
          active: (await stored()).is_active,
          before,
          after: {
            token: tokenAfter.status(),
            detail: ((await tokenAfter.json().catch(() => ({}))) as { detail?: string }).detail ?? null,
            key: await evaluateWith(request, key, flag.key, visitor),
            signIn: signIn.status,
          },
        },
      );
      decide(
        "u-owner.10.deactivation-audited",
        "audit_entry",
        [{ action: "user_deactivate", actor: OWNER_EMAIL }],
        byOwner(await audited(userId, "user_deactivate")),
      );
      await watch.step();
    });
  } finally {
    // What the day made goes even when a step failed; a stamped leftover
    // changes no other day, so these answers are not asserted. The global
    // safety settings are the exception: they are put back as found.
    if (settingsChanged && settingsFound) await api.send("POST", "/api/v1/safety/settings", settingsFound);
    for (const id of keysToRevoke) await api.send("DELETE", `/api/v1/api-keys/${id}`);
    if (workspaceId) await api.send("DELETE", `/api/v1/workspaces/${workspaceId}`);
    if (roleName) await api.send("DELETE", `/api/v1/rbac/roles/${roleName}`);
    if (userId) await api.send("DELETE", `/api/v1/admin/users/${userId}`);
    await api.send("DELETE", `/api/v1/feature-flags/${flag.id}`);
  }
});
