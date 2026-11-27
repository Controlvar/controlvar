import type { Request } from "@playwright/test";
import { test, expect } from "./persona.fixture";
import { apiState, decide } from "../oracles/oracle";
import { makeAdminAccount, openAccount, seededAccount, type AccountSession } from "./accounts";

/**
 * U-ADMIN's day (tests/personas/personas/u-admin.yaml): an account with the
 * Admin role that is not a superuser, in New York, changes a feature flag and
 * pauses an experiment that dev@demo.com made, then looks for the admin area
 * and finds it closed to the role. One serial test; each `test.step` title is
 * its card line, word for word, and each step ends with the oracles its card
 * names, then the watcher.
 *
 * No seed makes such an account: the superuser (admin@demo.com) makes one for
 * the day through POST /api/v1/users/ (accounts.ts), and dev@demo.com makes the
 * day's flag and running experiment through the API first, so the admin acts
 * only on what the day made, and on what someone else owns. Every change is
 * read back as the superuser.
 */

interface Me {
  id: string;
  email: string;
}

interface Flag {
  id: string;
  key: string;
  rollout_percentage: number;
  owner_id: string | null;
}

interface Experiment {
  id: string;
  status: string;
  owner_id: string | null;
  owner_name: string | null;
}

interface AuditEntries {
  items: Array<{ action_type: string; user_email: string; user_id: string | null }>;
}

/** The flag's rollout when the developer makes it, and the one the admin sets. */
const MADE_ROLLOUT = 30;
const NEW_ROLLOUT = 60;

/** RequireAuth's 403 view for a page that needs a superuser (src/components/RequireAuth.tsx). */
const REFUSED = "You do not have permission to access this page.";
const needsSuperuser = (email: string) => `Signed in as ${email} (ADMIN). This page requires a superuser account.`;

/** Every session the day opened, closed after it whatever happened. */
const opened: AccountSession[] = [];
test.afterEach(async () => {
  for (const session of opened.splice(0)) await session.close();
});

test("U-ADMIN: the admin's day", async ({ browser }) => {
  const stamp = Date.now();
  const owner = await openAccount(browser, seededAccount("OWNER"));
  opened.push(owner);
  const developer = await openAccount(browser, seededAccount("DEVELOPER"), "America/Los_Angeles");
  opened.push(developer);

  // What the developer makes before the day: a flag, and an experiment it starts.
  const developerId = (await developer.api.json<Me>("/api/v1/auth/me")).id;
  const flagMade = await developer.api.send("POST", "/api/v1/feature-flags/", {
    key: `persona-admin-${stamp}-flag`,
    name: `Persona admin flag ${stamp}`,
    rollout_percentage: MADE_ROLLOUT,
  });
  if (flagMade.status() !== 201) throw new Error(`setup: the developer's flag answered ${flagMade.status()}`);
  const flag = (await flagMade.json()) as Flag;
  const experimentMade = await developer.api.send("POST", "/api/v1/experiments/", {
    name: `Persona admin checkout ${stamp}`,
    key: `persona_admin_checkout_${stamp}`,
    experiment_type: "a_b",
    variants: [
      { name: "Control", is_control: true, traffic_allocation: 50 },
      { name: "Treatment", is_control: false, traffic_allocation: 50 },
    ],
    metrics: [{ name: "Purchase", event_name: "purchase", metric_type: "conversion", is_primary: true }],
  });
  if (experimentMade.status() !== 201) {
    throw new Error(`setup: the developer's experiment answered ${experimentMade.status()}`);
  }
  const experimentId = ((await experimentMade.json()) as Experiment).id;
  const started = await developer.api.send("POST", `/api/v1/experiments/${experimentId}/start`);
  if (started.status() !== 200) throw new Error(`setup: the experiment's start answered ${started.status()}`);
  // The day changes what someone else owns: the developer, never the admin.
  if (flag.owner_id !== developerId) throw new Error("setup: the developer does not own the day's flag");

  const made = await makeAdminAccount(owner.api, stamp);
  if (made.status !== 201) throw new Error(`setup: the Admin-role account answered ${made.status}`);
  const admin = await openAccount(browser, made.account, "America/New_York");
  opened.push(admin);
  const { page, api, watch } = admin;
  const adminEmail = made.account.email;

  /** The audit log's entries of one action on one thing, as the superuser reads them. */
  const audited = async (entityId: string, action: string) =>
    (
      await owner.api.json<AuditEntries>(
        `/api/v1/audit-logs/?entity_id=${entityId}&action_type=${action}&page=1&limit=50`,
      )
    ).items.map((e) => ({ action: e.action_type, actor: e.user_email, actorId: e.user_id }));

  await test.step("U-ADMIN day 1: change the rollout of a feature flag the developer owns", async () => {
    await page.goto(`/feature-flags/${flag.id}`);
    await expect(page.getByTestId("flag-detail")).toBeVisible({ timeout: 15_000 });
    // The flag's data rendered: what is offered is evidence only on a page that loaded.
    await expect(page.getByTestId("flag-key")).toHaveText(flag.key, { timeout: 15_000 });
    // The page has decided what to offer: Save, or the note that says why not.
    await expect(page.getByTestId("save-flag").or(page.getByTestId("flag-role-note")).first()).toBeVisible();
    const offered = {
      toggle: await page.getByTestId("flag-toggle").count(),
      rolloutEditable: await page.getByTestId("rollout-percentage").isEnabled(),
      save: await page.getByTestId("save-flag").count(),
      roleNote: await page.getByTestId("flag-role-note").count(),
    };
    // A page that offers no change sends none: the oracle below then names what was missing.
    let status: number | null = null;
    if (offered.rolloutEditable && offered.save === 1) {
      await page.getByTestId("rollout-percentage").fill(String(NEW_ROLLOUT));
      await expect(page.getByTestId("rollout-value")).toHaveText(`${NEW_ROLLOUT}%`);
      const saved = page.waitForResponse(
        (r) => r.request().method() === "PUT" && new URL(r.url()).pathname === `/api/v1/feature-flags/${flag.id}`,
      );
      await page.getByTestId("save-flag").click();
      status = (await saved).status();
      // The page has taken the answer: its success, or the error it shows.
      await expect(page.getByTestId("save-success").or(page.getByTestId("save-error")).first()).toBeVisible({
        timeout: 15_000,
      });
    }

    const stored = await owner.api.json<Flag>(`/api/v1/feature-flags/${flag.id}?skip_cache=true`);
    apiState(
      "u-admin.1.flag-changed",
      {
        offered: { toggle: 1, rolloutEditable: true, save: 1, roleNote: 0 },
        status: 200,
        rollout: NEW_ROLLOUT,
        owner: developerId,
      },
      { offered, status, rollout: stored.rollout_percentage, owner: stored.owner_id },
    );
    decide(
      "u-admin.1.change-audited",
      "audit_entry",
      [{ action: "feature_flag_update", actor: adminEmail, actorId: made.id }],
      await audited(flag.id, "feature_flag_update"),
    );
    await watch.step();
  });

  await test.step("U-ADMIN day 2: pause the developer's running experiment", async () => {
    await page.goto(`/experiments/${experimentId}`);
    await expect(page.getByTestId("experiment-detail")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("experiment-status")).toHaveAttribute("data-status", "active", { timeout: 15_000 });
    const before = await api.json<Experiment>(`/api/v1/experiments/${experimentId}`);
    const shownOwner = (await page.getByTestId("experiment-owner").innerText()).trim();
    // The page has decided what to offer: an action, or the note that says why not.
    const pause = page.getByTestId("action-pause");
    await expect(pause.or(page.getByTestId("experiment-role-note")).first()).toBeVisible();
    const pauseOffered = await pause.count();
    let status: number | null = null;
    if (pauseOffered === 1) {
      const paused = page.waitForResponse(
        (r) =>
          r.request().method() === "POST" && new URL(r.url()).pathname === `/api/v1/experiments/${experimentId}/pause`,
      );
      await pause.click();
      status = (await paused).status();
      // The page has taken the answer: the new status, or the error it shows.
      const shownPaused = page.locator('[data-testid="experiment-status"][data-status="paused"]');
      await expect(shownPaused.or(page.getByTestId("action-error")).first()).toBeVisible({ timeout: 15_000 });
    }
    const shownStatus = await page.getByTestId("experiment-status").getAttribute("data-status");

    const stored = await owner.api.json<Experiment>(`/api/v1/experiments/${experimentId}`);
    apiState(
      "u-admin.2.experiment-paused",
      {
        shownOwner: before.owner_name,
        pauseOffered: 1,
        status: 200,
        shown: "paused",
        after: "paused",
        owner: developerId,
      },
      { shownOwner, pauseOffered, status, shown: shownStatus, after: stored.status, owner: stored.owner_id },
    );
    decide(
      "u-admin.2.pause-audited",
      "audit_entry",
      [{ action: "experiment_pause", actor: adminEmail, actorId: made.id }],
      await audited(experimentId, "experiment_pause"),
    );
    await watch.step();
  });

  await test.step("U-ADMIN day 3: look for the admin area", async () => {
    await page.goto("/experiments");
    await expect(page.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
    // The header has rendered the role's items: an absent Admin link is evidence only then.
    await expect(page.getByTestId("nav-audit-log")).toBeVisible({ timeout: 15_000 });
    const adminLink = await page.getByTestId("nav-admin").count();

    const adminRequests: string[] = [];
    const onRequest = (r: Request) => {
      if (new URL(r.url()).pathname.startsWith("/api/v1/admin/")) adminRequests.push(`${r.method()} ${r.url()}`);
    };
    page.on("request", onRequest);
    await page.goto("/admin");
    // The page has decided: the 403 view, or the admin dashboard itself.
    const refusal = page.getByTestId("require-auth-forbidden");
    await expect(refusal.or(page.getByTestId("admin-dashboard")).first()).toBeVisible({ timeout: 15_000 });
    await page.waitForLoadState("networkidle");
    page.off("request", onRequest);

    const stats = await api.send("GET", "/api/v1/admin/stats");
    decide(
      "u-admin.3.admin-area-refused",
      "api_refused",
      { adminLink: 0, pageAdminRequests: 0, status: 403 },
      { adminLink, pageAdminRequests: adminRequests.length, status: stats.status() },
    );
    const words = refusal.locator("p");
    const shown =
      (await refusal.count()) === 1
        ? {
            refused: (await words.nth(1).innerText()).trim(),
            says: (await words.nth(2).innerText()).replace(/\s+/g, " ").trim(),
            wayOut: await refusal.getByRole("link", { name: "Go to Home" }).getAttribute("href"),
          }
        : { refused: null, says: null, wayOut: null };
    decide(
      "u-admin.3.refusal-words",
      "text_exact",
      { refused: REFUSED, says: needsSuperuser(adminEmail), wayOut: "/experiments" },
      shown,
    );
    await watch.step();
  });
});
