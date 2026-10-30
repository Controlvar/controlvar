import { test, expect, TEST_USERS, type UserRole } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";
import { ExperimentsPage } from "./pages/experiments.page";
import { FeatureFlagsPage } from "./pages/feature-flags.page";

/**
 * Journey 4 — role-based access control.
 *
 * The four seeded demo accounts (`seed_demo_data.py`) must see the navigation
 * their role allows and be refused what it does not. The client-side gates
 * (`NAV_ITEMS` in AppShell, `<RequireAuth>` / `withAdminGuard`) are
 * convenience only — the API is the enforcement, and they have to ask the same
 * question it does: the admin area is gated on `is_superuser`, because every
 * endpoint under /api/v1/admin is `Depends(deps.get_current_superuser)` (#84). The
 * create page asks the API's own create question too: an ANALYST or VIEWER
 * who opens it sees a notice instead of a form that could only earn a 403.
 */

/** Nav items the AppShell renders for a role (`NAV_ITEMS` in AppShell.tsx). */
const NAV_FOR_ROLE: Record<UserRole, { visible: string[]; hidden: string[] }> = {
  admin: {
    visible: ["nav-experiments", "nav-feature-flags", "nav-audit-log", "nav-admin", "nav-docs"],
    hidden: [],
  },
  developer: {
    // nav-admin is NOT here: the admin area is superusers only, and of the
    // four seeded accounts only the admin is one. The item used to be shown
    // to DEVELOPER, who then got a 403 from every /api/v1/admin request (#84).
    visible: ["nav-experiments", "nav-feature-flags", "nav-audit-log", "nav-docs"],
    hidden: ["nav-admin"],
  },
  analyst: {
    visible: ["nav-experiments", "nav-feature-flags", "nav-audit-log", "nav-docs"],
    hidden: ["nav-admin"],
  },
  viewer: {
    visible: ["nav-experiments", "nav-feature-flags", "nav-audit-log", "nav-docs"],
    hidden: ["nav-admin"],
  },
};

test.describe("Journey: RBAC", () => {
  for (const role of ["admin", "developer", "analyst", "viewer"] as const) {
    test(`${role} sees the navigation their role allows`, async ({ sessions }) => {
      const page = await (await sessions(role)).newPage();
      try {
        await page.goto("/experiments");
        await expect(page.getByTestId("app-shell")).toBeVisible();
        await expect(page.getByTestId("user-menu-role")).toHaveText(TEST_USERS[role].roleLabel);

        for (const testId of NAV_FOR_ROLE[role].visible) {
          await expect(page.getByTestId(testId), `${role} should see ${testId}`).toBeVisible();
        }
        for (const testId of NAV_FOR_ROLE[role].hidden) {
          await expect(page.getByTestId(testId), `${role} must not see ${testId}`).toHaveCount(0);
        }

        // The experiments page renders for every role, without an error banner.
        await expect(page.getByTestId("status-filter")).toBeVisible();
        // "+ New Experiment" is offered only to the roles the API lets create:
        // an analyst or viewer who clicked it would get a 403 from POST
        // /api/v1/experiments/.
        if (role === "admin" || role === "developer") {
          await expect(page.getByTestId("new-experiment-btn")).toBeVisible();
        } else {
          await expect(page.getByTestId("new-experiment-btn")).toHaveCount(0);
        }
        await expect(page.getByTestId("experiments-error")).toHaveCount(0);
        await expect(page.getByTestId("experiments-loading")).toHaveCount(0, { timeout: 20_000 });

        // Every role sees the platform, not an empty page.
        //
        // This used to assert the populated table for the admin alone, with a
        // comment explaining that `GET /experiments` gated its "see
        // everything" branch on `is_superuser` rather than on the role, so an
        // ANALYST or VIEWER got an empty list "for a product reason this
        // journey cannot fix". #83 fixed it: the list honours the role table,
        // and all four seeded roles carry LIST. So the assertion that was
        // documenting the gap now proves it closed, for every role.
        await expect(page.getByTestId("experiments-table")).toBeVisible({ timeout: 15_000 });
        expect(await page.getByTestId("experiment-row").count()).toBeGreaterThan(0);
      } finally {
        await page.close();
      }
    });
  }

  test("the admin reaches the admin area", async ({ adminPage }) => {
    await adminPage.goto("/admin");
    await expect(adminPage.getByTestId("admin-layout")).toBeVisible({ timeout: 15_000 });
    await expect(adminPage.getByTestId("require-auth-forbidden")).toHaveCount(0);
  });

  // developer joins analyst and viewer: every endpoint under /api/v1/admin is
  // `Depends(deps.get_current_superuser)`, and the seeded developer is not one.
  // Letting them in was #84 -- the page rendered and then every request on it
  // returned 403.
  // developer joins analyst and viewer for the ADMIN AREA only: every endpoint
  // under /api/v1/admin is `Depends(deps.get_current_superuser)`, and the
  // seeded developer is not one. Letting them in was #84.
  //
  // Two loops, not one. These tests had shared a loop, and widening it to
  // include developer widened BOTH -- which made "developer cannot create an
  // experiment" run for a role that certainly can, and fail. The audiences
  // differ, so the loops do.
  for (const role of ["developer", "analyst", "viewer"] as const) {
    test(`${role} is refused the admin area`, async ({ sessions }) => {
      const page = await (await sessions(role)).newPage();
      try {
        await page.goto("/admin");
        const forbidden = page.getByTestId("require-auth-forbidden");
        await expect(forbidden).toBeVisible({ timeout: 15_000 });
        await expect(forbidden).toContainText(TEST_USERS[role].role);
        // Nothing of the admin surface leaks through the 403 view.
        await expect(page.getByTestId("admin-dashboard")).toHaveCount(0);
        await expect(page.getByTestId("admin-sidebar")).toHaveCount(0);
      } finally {
        await page.close();
      }
    });

  }

  // Create is a different question: a DEVELOPER may create experiments -- the
  // API allows it and the nav test above asserts they get the button. Only
  // ANALYST and VIEWER are refused.
  // /experiments/new asks the same question as the API (superuser, ADMIN or
  // DEVELOPER): anyone else gets a notice in place of either view, and no
  // create request can be sent from it.
  for (const role of ["analyst", "viewer"] as const) {
    test(`${role} cannot create an experiment and the UI says so`, async ({ sessions }) => {
      const page = await (await sessions(role)).newPage();
      const experiments = new ExperimentsPage(page);
      let posts = 0;
      page.on("request", (request) => {
        const { pathname } = new URL(request.url());
        if (request.method() === "POST" && /\/api\/v1\/experiments\/?$/.test(pathname)) posts += 1;
      });
      try {
        for (const open of [() => experiments.gotoGuided(), () => experiments.gotoNew()]) {
          await open();
          await expect(experiments.notAllowed).toBeVisible({ timeout: 15_000 });
          await expect(experiments.notAllowed).toContainText(
            "Your role can view experiments but not create them.",
          );
          await expect(experiments.guided).toHaveCount(0);
          await expect(experiments.form).toHaveCount(0);
          await expect(experiments.nameInput).toHaveCount(0);
        }
        expect(posts).toBe(0);
      } finally {
        await page.close();
      }
    });
  }

  // Opening an experiment is READ, which the role table grants all four roles.
  // The detail route used to answer 403 to every non-superuser, the creator
  // included, and no journey noticed: the lifecycle journey opens detail pages
  // only as the seeded admin, who is a superuser and bypasses every check, and
  // this file stopped at the list. The seeded experiments belong to the admin,
  // so each of these opens one it does not own.
  for (const role of ["developer", "analyst", "viewer"] as const) {
    test(`${role} opens an experiment from the list`, async ({ sessions }) => {
      const page = await (await sessions(role)).newPage();
      const experiments = new ExperimentsPage(page);
      try {
        await experiments.goto();
        await expect(experiments.experimentList).toBeVisible({ timeout: 15_000 });
        await experiments.experimentRows.first().getByTestId("experiment-link").click();
        await expect(experiments.detail).toBeVisible({ timeout: 15_000 });
        await expect(page.getByTestId("experiment-error")).toHaveCount(0);
      } finally {
        await page.close();
      }
    });
  }

  // Starting, pausing, completing and archiving need a role with EXPERIMENT
  // UPDATE (ADMIN or DEVELOPER) or a superuser, whoever owns the experiment.
  // An analyst or viewer opening a running experiment is offered none of
  // them, is told why, and reads its results -- and no request they make on
  // the way is refused by the experiments or results API.
  //
  // The page's group and holdout rows (#1449) read routes these two roles are
  // refused (EXPERIMENT CREATE), so the page does not ask: it says who can see
  // them, and sends neither request.
  for (const role of ["analyst", "viewer"] as const) {
    test(`${role} sees no lifecycle action on a running experiment and reads its results`, async ({
      sessions,
    }) => {
      const page = await (await sessions(role)).newPage();
      const experiments = new ExperimentsPage(page);
      const refused: string[] = [];
      const eligibilityRequests: string[] = [];
      page.on("request", (request) => {
        const { pathname } = new URL(request.url());
        if (/^\/api\/v1\/(holdout|mutual-exclusion-groups)(\/|$)/.test(pathname)) {
          eligibilityRequests.push(`${request.method()} ${pathname}`);
        }
      });
      page.on("response", (response) => {
        const { pathname } = new URL(response.url());
        if (
          /^\/api\/v1\/(experiments|results|holdout|mutual-exclusion-groups)(\/|$)/.test(pathname) &&
          (response.status() === 401 || response.status() === 403)
        ) {
          refused.push(`${response.status()} ${response.request().method()} ${pathname}`);
        }
      });
      try {
        await experiments.goto();
        await expect(experiments.experimentList).toBeVisible({ timeout: 15_000 });
        // Seeded ACTIVE by seed_demo_data.py and owned by the admin; no
        // journey changes its status.
        await experiments.clickExperiment("Checkout Button Color");
        await experiments.expectStatus("active");

        for (const button of [
          experiments.startButton,
          experiments.pauseButton,
          experiments.completeButton,
          experiments.archiveButton,
        ]) {
          await expect(button).toHaveCount(0);
        }
        await expect(experiments.actions.locator("button")).toHaveCount(0);
        // No clone, edit or delete either (#442).
        await expect(page.getByTestId("experiment-manage")).toHaveCount(0);
        const note = page.getByTestId("experiment-role-note");
        await expect(note).toBeVisible();
        await expect(note).toContainText("requires the ADMIN or DEVELOPER role");
        await expect(note).toContainText(`you are ${TEST_USERS[role].role}.`);

        // The seeded experiment is in no group, which the experiment itself
        // says; the holdout needs the role.
        await expect(page.getByTestId("experiment-exclusion-group")).toHaveText("None");
        await expect(page.getByTestId("experiment-holdout-refused")).toHaveText(
          "Seeing this requires the ADMIN or DEVELOPER role.",
        );
        await expect(page.getByTestId("experiment-holdout")).toHaveCount(0);

        await experiments.viewResults();
        await expect(page.getByTestId("results-dashboard")).toBeVisible({ timeout: 20_000 });
        await page.waitForLoadState("networkidle");
        expect(refused, "no experiments/results/holdout/group request is refused").toEqual([]);
        expect(eligibilityRequests, "no holdout or group request is sent").toEqual([]);
      } finally {
        await page.close();
      }
    });
  }

  // Feature flags follow the role the same way (#917). ANALYST and VIEWER hold
  // FEATURE_FLAG READ and LIST only, so the list offers them no row switch and
  // no "+ New Flag", /feature-flags/new shows a notice instead of a form that
  // could only earn a 403, and the flag page offers no switch, no slider and
  // no Save. Each "absent" check sits beside a "present" one on the same page,
  // so a page that failed to render cannot pass it.
  for (const role of ["analyst", "viewer"] as const) {
    test(`${role} reads feature flags and is offered no change`, async ({ sessions }) => {
      const page = await (await sessions(role)).newPage();
      const flags = new FeatureFlagsPage(page);
      // Seeded by seed_demo_data.py; the flag journey toggles beta_features,
      // never this one, and no journey deletes a seeded flag.
      const seededKey = "new_dashboard_ui";
      let posts = 0;
      page.on("request", (request) => {
        const { pathname } = new URL(request.url());
        if (request.method() === "POST" && /\/api\/v1\/feature-flags\/?$/.test(pathname)) posts += 1;
      });
      try {
        await flags.goto();
        const listNote = page.getByTestId("flags-role-note");
        await expect(listNote).toBeVisible({ timeout: 15_000 });
        await expect(listNote).toHaveText(
          "Feature flags are created and changed by the ADMIN and DEVELOPER roles.",
        );
        await expect(flags.listError).toHaveCount(0);
        await expect(flags.flagList).toBeVisible({ timeout: 15_000 });
        expect(await flags.flagRows.count()).toBeGreaterThan(0);
        const seededRow = flags.getFlagRow(seededKey);
        await expect(seededRow).toBeVisible();
        await expect(seededRow.getByTestId("flag-status-pill")).toHaveText(/^(On|Off)$/);
        await expect(flags.rowToggle(seededKey)).toHaveCount(0);
        await expect(flags.toggleSwitch).toHaveCount(0);
        await expect(flags.createButton).toHaveCount(0);

        await flags.gotoNew();
        const newNote = page.getByTestId("flag-new-role-note");
        await expect(newNote).toBeVisible({ timeout: 15_000 });
        await expect(newNote).toHaveText(
          "Your role can view feature flags but not create them. Feature flags are created and changed by the ADMIN and DEVELOPER roles.",
        );
        await expect(page.getByRole("link", { name: "Back to feature flags" })).toBeVisible();
        await expect(flags.submitButton).toHaveCount(0);
        await expect(flags.nameInput).toHaveCount(0);
        expect(posts, "no create request was sent").toBe(0);

        await flags.goto();
        await flags.getFlagRow(seededKey).getByTestId("flag-link").click();
        await expect(flags.detail).toBeVisible({ timeout: 15_000 });
        await expect(flags.detailKey).toHaveText(seededKey);
        await expect(page.getByTestId("flag-role-note")).toHaveText(
          "Feature flags are created and changed by the ADMIN and DEVELOPER roles.",
        );
        await expect(flags.detailToggle).toHaveCount(0);
        await expect(flags.rolloutPercentageInput).toBeDisabled();
        await expect(flags.saveButton).toHaveCount(0);
        await expect(page.getByTestId("targeting-replace")).toHaveCount(0);
        // Reading stays: the safety check still resolves for this role.
        await expect(flags.safetyStatus).toBeVisible({ timeout: 20_000 });
      } finally {
        await page.close();
      }
    });
  }

  // Reading a bandit's current weights is allowed to every signed-in role
  // (`GET /api/v1/bandit/{id}`), and the panel changes nothing, so all four
  // roles get the same read-only panel. What it shows is compared with the
  // server's own answer to the page's request, not with a value typed here:
  // before the first update (`last_updated` null) the panel says so instead
  // of showing the even split the server fills in.
  for (const role of ["admin", "developer", "analyst", "viewer"] as const) {
    test(`${role} reads a bandit's current traffic weights and is offered no change`, async ({ sessions }) => {
      const page = await (await sessions(role)).newPage();
      const experiments = new ExperimentsPage(page);
      const writes: string[] = [];
      page.on("request", (request) => {
        const { pathname } = new URL(request.url());
        if (pathname.startsWith("/api/v1/bandit/") && request.method() !== "GET") {
          writes.push(`${request.method()} ${pathname}`);
        }
      });
      try {
        await experiments.goto();
        await expect(experiments.experimentList).toBeVisible({ timeout: 15_000 });
        const answer = page.waitForResponse(
          (response) =>
            new URL(response.url()).pathname.startsWith("/api/v1/bandit/") &&
            response.request().method() === "GET",
          { timeout: 20_000 },
        );
        // Seeded ACTIVE with Thompson sampling by seed_demo_data.py.
        await experiments.clickExperiment("Recommendation Algorithm MAB");
        const response = await answer;
        expect(response.status(), `${role} may read the weights`).toBe(200);
        const body = (await response.json()) as {
          last_updated: string | null;
          current_weights: { variant_id: string; current_weight: number }[];
        };

        const panel = page.getByTestId("bandit-weights");
        await expect(panel).toBeVisible();
        await expect(panel.getByTestId("bandit-weights-loading")).toHaveCount(0, { timeout: 15_000 });
        await expect(panel.getByTestId("bandit-weights-error")).toHaveCount(0);
        await expect(panel.getByRole("button")).toHaveText(["Refresh"]);

        if (body.last_updated === null) {
          await expect(panel.getByTestId("bandit-weights-empty")).toBeVisible();
          await expect(panel.getByTestId("bandit-weights-table")).toHaveCount(0);
        } else {
          expect(body.current_weights.length).toBeGreaterThan(0);
          await expect(panel.getByTestId("bandit-weight-row")).toHaveCount(body.current_weights.length);
          for (const weight of body.current_weights) {
            const row = panel.locator(`[data-testid="bandit-weight-row"][data-variant-id="${weight.variant_id}"]`);
            await expect(row.getByTestId("bandit-weight-share")).toHaveText(
              `${(weight.current_weight * 100).toFixed(1)}%`,
            );
          }
        }
        expect(writes, "the panel only reads").toEqual([]);
      } finally {
        await page.close();
      }
    });
  }

  // The reported path end to end: guided setup redirects to the new
  // experiment's page, and its creator -- an ordinary DEVELOPER -- must be able
  // to open it. `createExperiment` resolves only once the detail has rendered.
  test("developer creates an experiment and lands on its page", async ({ sessions }) => {
    const page = await (await sessions("developer")).newPage();
    const experiments = new ExperimentsPage(page);
    const stamp = Date.now();
    try {
      const id = await experiments.createExperiment(`E2E Developer owns ${stamp}`, `e2e_dev_owns_${stamp}`);
      expect(id).toMatch(/^[0-9a-f-]{36}$/);
      await expect(experiments.detailName).toContainText(`E2E Developer owns ${stamp}`);
      await expect(page.getByTestId("experiment-error")).toHaveCount(0);

      // And again from a cold load, not only straight after the redirect.
      await page.reload();
      await expect(experiments.detail).toBeVisible({ timeout: 15_000 });
    } finally {
      await page.close();
    }
  });

  // #1449 (plan-1449 #14): the experiment page's read-only settings against
  // the real API, as an ordinary DEVELOPER (the seeded admin is a superuser,
  // so its cell would prove nothing about the role). The journey database is
  // shared, so the only experiment it schedules is a draft it creates, and the
  // seeded one it opens is only read.
  test("developer reads an experiment's sequential testing, holdout, group and scheduled start", async ({
    sessions,
  }) => {
    const page = await (await sessions("developer")).newPage();
    const experiments = new ExperimentsPage(page);
    const stamp = Date.now();
    const holdoutAnswer = () =>
      page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/api/v1/holdout" && response.request().method() === "GET",
        { timeout: 20_000 },
      );
    try {
      await experiments.goto();
      await expect(experiments.experimentList).toBeVisible({ timeout: 15_000 });
      const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
      expect(token, "the developer session has a token").toBeTruthy();
      const headers = { Authorization: `Bearer ${token}` };

      // The seeded msprt experiment (seed_demo_data.py: sequential testing on),
      // opened by id: a clone of it would also match its name in the list.
      const list = await page.request.get(`${API_URL}/api/v1/experiments/?limit=100`, { headers });
      expect(list.status()).toBe(200);
      const seeded = ((await list.json()) as { items: { id: string; name: string }[] }).items.find(
        (item) => item.name === "Homepage Hero Copy Test",
      );
      expect(seeded, "the seeded msprt experiment").toBeTruthy();
      const first = holdoutAnswer();
      await page.goto(`/experiments/${seeded!.id}`);
      await expect(experiments.detail).toBeVisible({ timeout: 15_000 });
      const answer = await first;
      expect(answer.status(), "a DEVELOPER may read the holdout").toBe(200);
      // The journey seed activates no holdout, and no journey makes one.
      expect(await answer.json()).toBeNull();
      await expect(page.getByTestId("experiment-sequential")).toHaveText("On");
      await expect(page.getByTestId("experiment-holdout")).toHaveText("None active");
      await expect(page.getByTestId("experiment-exclusion-group")).toHaveText("None");

      // A draft of its own, scheduled two days ahead: "Starts", nothing due.
      const id = await experiments.createExperiment(`E2E Scheduled ${stamp}`, `e2e_scheduled_${stamp}`);
      const day = 24 * 60 * 60 * 1000;
      const schedule = await page.request.put(`${API_URL}/api/v1/experiments/${id}/schedule`, {
        headers,
        data: {
          start_date: new Date(Date.now() + 2 * day).toISOString(),
          end_date: new Date(Date.now() + 3 * day).toISOString(),
        },
      });
      expect(schedule.status(), await schedule.text()).toBe(200);
      const second = holdoutAnswer();
      await page.reload();
      await expect(experiments.detail).toBeVisible({ timeout: 15_000 });
      expect((await second).status()).toBe(200);
      await experiments.expectStatus("draft");
      const start = page.getByTestId("experiment-start-date");
      await expect(start).toBeVisible();
      await expect(start.locator("xpath=preceding-sibling::dt")).toHaveText("Starts");
      await expect(start).not.toContainText("(due");
      await expect(page.getByTestId("experiment-end-date").locator("xpath=preceding-sibling::dt")).toHaveText("Ends");
      await expect(page.getByTestId("experiment-exclusion-group")).toHaveText("None");
      await expect(page.getByTestId("experiment-holdout")).toHaveText("None active");

      await page.getByTestId("experiment-delete").click();
      await page.getByTestId("manage-delete-yes").click();
      await page.waitForURL(/\/experiments$/, { timeout: 15_000 });
    } finally {
      await page.close();
    }
  });

  test("developer gets guided setup", async ({ sessions }) => {
    const page = await (await sessions("developer")).newPage();
    const experiments = new ExperimentsPage(page);
    try {
      await experiments.gotoGuided();
      await expect(experiments.guided).toBeVisible({ timeout: 15_000 });
      await expect(experiments.notAllowed).toHaveCount(0);
    } finally {
      await page.close();
    }
  });

  // #442, D33: a DEVELOPER may clone an experiment someone else owns (the
  // seeded ones belong to the admin), and delete the draft that makes.
  test("developer clones an experiment it does not own, then deletes the clone", async ({ sessions }) => {
    const page = await (await sessions("developer")).newPage();
    const experiments = new ExperimentsPage(page);
    try {
      await experiments.goto();
      await expect(experiments.experimentList).toBeVisible({ timeout: 15_000 });
      // Seeded COMPLETED and owned by the admin; no other journey opens it, so
      // a clone left behind by a failed attempt cannot be picked up by name.
      await experiments.clickExperiment("Homepage Hero Copy Test");
      await experiments.expectStatus("completed");
      const sourceUrl = new URL(page.url()).pathname;

      const manage = page.getByTestId("experiment-manage");
      await expect(manage.getByTestId("experiment-clone")).toBeVisible();
      await expect(manage.getByTestId("experiment-edit-details")).toHaveCount(0);
      await expect(manage.getByTestId("experiment-delete")).toHaveCount(0);

      await manage.getByTestId("experiment-clone").click();
      await page.waitForURL((url) => url.pathname !== sourceUrl && /\/experiments\/[0-9a-f-]{36}$/.test(url.pathname), {
        timeout: 15_000,
      });
      await expect(experiments.detailName).toHaveText("Copy of Homepage Hero Copy Test", { timeout: 15_000 });
      await experiments.expectStatus("draft");

      await page.getByTestId("experiment-delete").click();
      await page.getByTestId("manage-delete-yes").click();
      await page.waitForURL(/\/experiments$/, { timeout: 15_000 });
      await expect(page.getByTestId("manage-error")).toHaveCount(0);
    } finally {
      await page.close();
    }
  });
});
