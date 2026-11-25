import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { FeatureFlagsPage } from "./pages/feature-flags.page";
import { API_URL, BASE_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: a flag in Production, Staging and Development (#2011).
 *
 * A DEVELOPER (not a superuser) opens a flag of their own, changes it in
 * Staging, in Production and in Development, from the flag's page and from the
 * list, and after each change reads all three environments back from the API:
 * the one on screen changed, and the other two are what they were, `updated_at`
 * included. The change requests the browser sent are read too, so a change that
 * lands in the right place by the wrong route fails. Two tests hold the answer
 * of a change until the environment has been switched: it may not move what is
 * on screen then.
 *
 * The flag is this journey's own, named with a timestamp, created and deleted
 * through the API; the seeded flags are never touched. The environment a user
 * chose is kept in the browser, and the signed-in contexts are shared by every
 * spec of the worker, so each test ends by forgetting the choice.
 *
 * axe scans the whole of each page, at desktop width and at 390 px.
 */

/** Source of truth: `FLAG_ENVIRONMENT_STORAGE_PREFIX` in src/contexts/FlagEnvironmentContext.tsx. */
const ENVIRONMENT_STORAGE_PREFIX = "controlvar.flag-environment.";

const PHONE = { width: 390, height: 844 };

type Environment = "production" | "staging" | "development";

/** A flag's settings in one environment, as the API answers them. */
interface Settings {
  environment: string;
  status: string;
  is_active: boolean;
  rollout_percentage: number;
  targeting_rules: unknown;
  configured: boolean;
  updated_at: string | null;
}

/** Everything the API holds: the flag itself and its settings in each environment. */
interface Stored {
  flag: Record<string, unknown>;
  production: Settings;
  staging: Settings;
  development: Settings;
}

const PRODUCTION_RULES = {
  logical_operator: "AND",
  groups: [{ logical_operator: "AND", conditions: [{ attribute: "country", operator: "equals", value: "US" }] }],
};

async function expectNoSeriousViolations(page: Page, where: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(serious, `axe on ${where}`).toEqual([]);
}

/** The page itself never scrolls sideways; what is too wide scrolls inside it. */
async function expectNoSidewaysScroll(page: Page, where: string): Promise<void> {
  const { scroll, client } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(scroll, `${where} does not scroll sideways`).toBeLessThanOrEqual(client);
}

/** The token of the session the page is signed in with. */
async function tokenIn(page: Page): Promise<string> {
  const token = await page.evaluate((storageKey) => window.localStorage.getItem(storageKey), TOKEN_STORAGE_KEY);
  expect(token, "the session has a token").toBeTruthy();
  return token as string;
}

/** Watch Next's route announcer from now on; the result answers what it has said, in order. */
async function routeAnnouncements(page: Page): Promise<() => Promise<string[]>> {
  await page.evaluate(() => {
    const said: string[] = [];
    (window as Window & { __announced?: string[] }).__announced = said;
    const announcer = document.querySelector("#__next-route-announcer__");
    if (!announcer) throw new Error("Next's route announcer is not on the page");
    new MutationObserver(() => {
      const text = announcer.textContent ?? "";
      if (text && text !== said[said.length - 1]) said.push(text);
    }).observe(announcer, { childList: true, characterData: true, subtree: true });
  });
  return () => page.evaluate(() => (window as Window & { __announced?: string[] }).__announced ?? []);
}

/** The flag changes the page sends from now on, as `METHOD path` with the flag's id left out. */
function flagChanges(page: Page, id: string): string[] {
  const sent: string[] = [];
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (request.method() === "GET" || request.method() === "OPTIONS") return;
    if (pathname.startsWith("/api/v1/feature-flags")) sent.push(`${request.method()} ${pathname.replace(id, "{id}")}`);
  });
  return sent;
}

/**
 * Let the change reach the API and hold its answer until the caller gives it.
 * `saved` resolves once the API has answered, so the change is stored by then.
 */
async function holdAnswer(page: Page, path: string) {
  let give!: () => void;
  const given = new Promise<void>((resolve) => (give = resolve));
  let store!: () => void;
  const saved = new Promise<void>((resolve) => (store = resolve));
  await page.route(`**${path}`, async (route) => {
    if (route.request().method() !== "PUT") return route.fallback();
    const response = await route.fetch();
    store();
    await given;
    await route.fulfill({ response });
  });
  return {
    saved,
    /**
     * Give the answer, and wait until the page has had time to read it and
     * paint. The wait can only make a page that shows it fail: one that does
     * not show it passes however long it is.
     */
    give: async () => {
      const answered = page.waitForResponse(
        (response) => response.request().method() === "PUT" && new URL(response.url()).pathname === path,
      );
      give();
      await (await answered).finished();
      await page.evaluate(
        () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 250)))),
      );
    },
  };
}

test.describe("Journey: a flag in each environment", () => {
  test.describe.configure({ mode: "serial" });

  const stamp = Date.now();
  const key = `e2e_env_${stamp}`;
  const name = `E2E environments ${stamp}`;
  let flagId = "";
  let headers: Record<string, string> = {};

  async function stored(page: Page): Promise<Stored> {
    const flag = await page.request.get(`${API_URL}/api/v1/feature-flags/${flagId}`, { headers });
    expect(flag.status(), "GET /feature-flags/{id}").toBe(200);
    const all = await page.request.get(`${API_URL}/api/v1/feature-flags/${flagId}/environments`, { headers });
    expect(all.status(), "GET /feature-flags/{id}/environments").toBe(200);
    const { environments } = (await all.json()) as { environments: Settings[] };
    const named = (environment: Environment): Settings => {
      const found = environments.find((settings) => settings.environment === environment);
      expect(found, `the API answers ${environment}`).toBeTruthy();
      return found as Settings;
    };
    return {
      flag: (await flag.json()) as Record<string, unknown>,
      production: named("production"),
      staging: named("staging"),
      development: named("development"),
    };
  }

  /** The environments that must be exactly what they were, `updated_at` included. */
  function expectUnchanged(after: Stored, before: Stored, environments: Environment[]): void {
    for (const environment of environments) {
      expect(after[environment], `${environment} is what it was`).toEqual(before[environment]);
      // The flag's own read is Production's: nothing else about it moves either.
      if (environment === "production") expect(after.flag, "the flag is what it was").toEqual(before.flag);
    }
  }

  async function openFlag(page: Page, environment?: Environment): Promise<FeatureFlagsPage> {
    const flags = new FeatureFlagsPage(page);
    await page.goto(`/feature-flags/${flagId}${environment ? `?env=${environment}` : ""}`);
    await expect(flags.detailKey).toHaveText(key, { timeout: 15_000 });
    return flags;
  }

  async function openList(page: Page, environment: Environment): Promise<FeatureFlagsPage> {
    const flags = new FeatureFlagsPage(page);
    await page.goto(`/feature-flags?env=${environment}`);
    await expect(flags.flagList).toHaveAttribute("data-environment", environment, { timeout: 15_000 });
    await expect(flags.getFlagRow(key)).toBeVisible();
    return flags;
  }

  test.beforeAll(async ({ sessions }) => {
    const page = await (await sessions("developer")).newPage();
    try {
      await page.goto("/feature-flags?env=production");
      headers = { Authorization: `Bearer ${await tokenIn(page)}` };
      const created = await page.request.post(`${API_URL}/api/v1/feature-flags/`, {
        headers: { ...headers, "content-type": "application/json" },
        data: { key, name, is_active: true, rollout_percentage: 25, targeting_rules: PRODUCTION_RULES },
      });
      expect(created.status(), await created.text()).toBe(201);
      flagId = ((await created.json()) as { id: string }).id;
    } finally {
      await page.close();
    }
  });

  test.afterEach(async ({ developerPage: page }) => {
    // Other specs of this worker open the flag pages in the same browser context.
    await page.goto("/feature-flags?env=production");
    await page.evaluate((prefix) => {
      for (const stored of Object.keys(window.localStorage)) {
        if (stored.startsWith(prefix)) window.localStorage.removeItem(stored);
      }
    }, ENVIRONMENT_STORAGE_PREFIX);
  });

  test.afterAll(async ({ sessions }) => {
    if (!flagId) return;
    const page = await (await sessions("developer")).newPage();
    try {
      const removed = await page.request.delete(`${API_URL}/api/v1/feature-flags/${flagId}`, { headers });
      expect(removed.status(), `DELETE the journey's flag ${flagId}`).toBe(204);
    } finally {
      await page.close();
    }
  });

  test("a change in Staging stays out of Production and Development", async ({ developerPage: page }) => {
    const before = await stored(page);
    expect(before.production).toMatchObject({ is_active: true, rollout_percentage: 25, configured: true });
    expect(before.staging).toMatchObject({ is_active: false, configured: false, updated_at: null });
    expect(before.development).toMatchObject({ is_active: false, configured: false, updated_at: null });

    // Production first: marked in text, with its banner and its own controls.
    const flags = await openFlag(page);
    await expect(flags.environmentSelect).toHaveValue("production");
    await expect(flags.productionMarker).toHaveText("●Live");
    await expect(flags.productionBanner).toHaveText("You are editing Production. Changes affect live users.");
    await expect(flags.rolloutValue).toHaveText("25%");
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active");
    await expect(flags.rolloutScheduleSection).toBeVisible();
    await expect(flags.safetySection).toBeVisible();
    // The safety check has answered, so its values are scanned too.
    await expect(flags.safetyStatus).toBeVisible({ timeout: 20_000 });
    await expectNoSeriousViolations(page, "a flag in Production");
    await expect(flags.environmentAnnouncement, "nothing has changed yet").toHaveText("");

    // Staging: the URL names it, the page starts again, and the select keeps the focus.
    const changes = flagChanges(page, flagId);
    await flags.environmentSelect.focus();
    await flags.chooseEnvironment("staging");
    await expect(flags.notConfigured).toHaveText("Not configured in Staging");
    await expect(flags.environmentSelect).toBeFocused();
    await expect(flags.environmentAnnouncement).toHaveText("Showing Staging.");
    await expect(flags.statusBadge).toHaveText("Off");
    await expect(flags.rolloutValue).toHaveText("0%");
    await expect(flags.productionOnlyNote).toContainText(
      "Rollout schedules, safety monitoring and experiments run in Production only.",
    );
    for (const absent of [
      flags.productionMarker,
      flags.productionBanner,
      flags.rolloutScheduleSection,
      flags.safetySection,
      page.getByTestId("flag-archive"),
      page.getByTestId("flag-delete"),
    ]) {
      await expect(absent).toHaveCount(0);
    }
    await expect(page).toHaveTitle(`${name} in Staging · Controlvar`);
    await expectNoSeriousViolations(page, "a flag not configured in Staging");

    // On, and at 60%: two changes, both in Staging.
    await flags.detailToggle.click();
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active");
    await expect(flags.notConfigured).toHaveCount(0);
    await expect(flags.detailToggle).toBeEnabled();
    await expect(flags.saveButton).toHaveText("Save changes in Staging");
    await flags.setRolloutPercentage(60);

    const after = await stored(page);
    expectUnchanged(after, before, ["production", "development"]);
    expect(after.staging).toMatchObject({
      status: "active",
      is_active: true,
      rollout_percentage: 60,
      targeting_rules: null,
      configured: true,
    });
    expect(changes, "the browser changed the flag in Staging only").toEqual([
      "PUT /api/v1/feature-flags/{id}/environments/staging",
      "PUT /api/v1/feature-flags/{id}/environments/staging",
    ]);

    // It survives a reload, on the environment the URL names.
    await page.reload();
    await expect(flags.rolloutValue).toHaveText("60%", { timeout: 15_000 });
    await expect(flags.environmentSelect).toHaveValue("staging");
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active");

    // The comparison: the three side by side, as the API holds them.
    await flags.comparisonToggle.click();
    await expect(flags.comparisonTable.locator("thead th")).toHaveText([
      "Setting",
      "Development",
      "Staging",
      "Production",
    ]);
    const cells = (row: string) => flags.comparisonTable.getByTestId(row).locator("td");
    await expect(cells("compare-status")).toHaveText(["OffNot configured", "On", "On"]);
    await expect(cells("compare-rollout")).toHaveText(["0%", "60%", "25%"]);
    await expect(cells("compare-rules")).toHaveText(["None", "None", "1 condition in 1 group"]);
    await expectNoSeriousViolations(page, "the comparison");

    // And back: Production shows what it did before any of this.
    await flags.chooseEnvironment("production");
    await expect(flags.rolloutValue).toHaveText("25%");
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active");
    await expect(flags.productionMarker).toBeVisible();
    expectUnchanged(await stored(page), before, ["production", "development"]);
  });

  test("a change in Production stays out of Staging and Development", async ({ developerPage: page }) => {
    const before = await stored(page);
    expect(before.staging).toMatchObject({ is_active: true, rollout_percentage: 60 });

    const flags = await openFlag(page, "production");
    await expect(flags.rolloutValue).toHaveText("25%");
    const changes = flagChanges(page, flagId);
    await expect(flags.saveButton).toHaveText("Save Changes");
    await flags.setRolloutPercentage(30);

    const after = await stored(page);
    expectUnchanged(after, before, ["staging", "development"]);
    expect(after.production).toMatchObject({
      is_active: true,
      rollout_percentage: 30,
      targeting_rules: before.production.targeting_rules,
    });
    expect(changes, "the browser changed the flag by its own route only").toEqual([
      "PUT /api/v1/feature-flags/{id}",
    ]);

    await flags.chooseEnvironment("staging");
    await expect(flags.rolloutValue).toHaveText("60%");
  });

  test("the list shows and changes each environment's own setting", async ({ developerPage: page }) => {
    const before = await stored(page);
    const flags = await openList(page, "staging");
    const row = flags.getFlagRow(key);
    await expect(row.getByTestId("flag-status-pill")).toHaveText("On");
    await expect(row).toContainText("60%");
    await expect(flags.createButton).toHaveCount(0);
    await expect(page.getByTestId("flags-created-in-production")).toBeVisible();
    await expectNoSeriousViolations(page, "the list in Staging");

    // Development: not configured, until its switch is turned on from here.
    await flags.chooseEnvironment("development");
    await expect(flags.flagList).toHaveAttribute("data-environment", "development");
    await expect(row.getByTestId("flag-not-configured")).toHaveText("Not configured in Development");
    await expect(row.getByTestId("flag-status-pill")).toHaveText("Off");
    const changes = flagChanges(page, flagId);
    await flags.toggleFlagByKey(key);
    await expect(row.getByTestId("flag-status-pill")).toHaveText("On");
    await expect(row.getByTestId("flag-not-configured")).toHaveCount(0);

    const after = await stored(page);
    expectUnchanged(after, before, ["production", "staging"]);
    expect(after.development).toMatchObject({ is_active: true, rollout_percentage: 0, configured: true });
    expect(changes, "the browser changed the flag in Development only").toEqual([
      "PUT /api/v1/feature-flags/{id}/environments/development",
    ]);

    // Production's own row. Choosing it leaves Production as the stored choice.
    await flags.chooseEnvironment("production");
    await expect(flags.flagList).toHaveAttribute("data-environment", "production");
    await expect(row).toContainText("30%");
    await expect(flags.createButton).toBeVisible();
    await expect(flags.productionBanner).toBeVisible();

    // A row opens the flag's page in the environment the list shows: here one a
    // link named, not the stored one. The row's own link names none.
    await openList(page, "staging");
    await row.getByTestId("flag-link").click();
    await expect(flags.detailKey).toHaveText(key, { timeout: 15_000 });
    await expect(flags.environmentSelect).toHaveValue("staging");
    await expect(flags.rolloutValue).toHaveText("60%");
  });

  test("the choice is the user's own, and a link wins over it", async ({
    developerPage: page,
    adminPage,
    browser,
  }) => {
    const flags = await openFlag(page, "production");
    await flags.chooseEnvironment("staging");
    await expect(flags.rolloutValue).toHaveText("60%");

    // A new visit with no environment opens the choice; a link names its own.
    await openFlag(page);
    await expect(flags.environmentSelect).toHaveValue("staging");
    await expect(flags.rolloutValue).toHaveText("60%");
    await openFlag(page, "production");
    await expect(flags.environmentSelect).toHaveValue("production");
    await expect(flags.rolloutValue).toHaveText("30%");
    // A link does not change the choice, and a name that is no environment is ignored.
    await openFlag(page);
    await expect(flags.environmentSelect).toHaveValue("staging");
    await page.goto(`/feature-flags/${flagId}?env=prod`);
    await expect(flags.environmentSelect).toHaveValue("staging", { timeout: 15_000 });

    // Another user in the same browser: a copy of this one's session and stored
    // choice, then the admin's token in it. Their own choice is none, so they
    // open in Production; the developer's is still there when they come back.
    const copied = await browser.newContext({ baseURL: BASE_URL, storageState: await page.context().storageState() });
    try {
      await adminPage.goto("/experiments");
      const [developerToken, adminToken] = await Promise.all([tokenIn(page), tokenIn(adminPage)]);
      const shared = await copied.newPage();
      const signInAs = async (token: string, role: string) => {
        await shared.goto("/experiments");
        await shared.evaluate(([storageKey, value]) => window.localStorage.setItem(storageKey, value), [
          TOKEN_STORAGE_KEY,
          token,
        ]);
        const opened = await openFlag(shared);
        await expect(shared.getByTestId("user-menu-role")).toHaveText(role);
        return opened;
      };
      const admin = await signInAs(adminToken, "Admin");
      await expect(admin.environmentSelect).toHaveValue("production");
      await expect(admin.rolloutValue).toHaveText("30%");
      const developer = await signInAs(developerToken, "Developer");
      await expect(developer.environmentSelect).toHaveValue("staging");
      await expect(developer.rolloutValue).toHaveText("60%");
    } finally {
      await copied.close();
    }
  });

  test("the status says the environment on screen: after Back, and not after a load that failed", async ({
    developerPage: page,
  }) => {
    const flags = await openList(page, "production");
    await expect(flags.environmentAnnouncement).toHaveText("");
    await flags.getFlagRow(key).getByTestId("flag-link").click();
    await expect(flags.detailKey).toHaveText(key, { timeout: 15_000 });
    await flags.chooseEnvironment("staging");
    await expect(flags.environmentAnnouncement).toHaveText("Showing Staging.");

    // Back to the list, whose URL names Production: neither the status nor
    // Next's own announcement of the page's title names Staging.
    const said = await routeAnnouncements(page);
    await page.goBack();
    await expect(flags.flagList).toHaveAttribute("data-environment", "production", { timeout: 15_000 });
    await expect(flags.environmentSelect).toHaveValue("production");
    await expect(flags.environmentAnnouncement).toHaveText("Showing Production.");
    await expect.poll(said, { message: "what Next announced on Back" }).toEqual(["Feature Flags · Controlvar"]);

    // A load that fails is an alert; the status does not say the environment is shown.
    await openFlag(page, "production");
    await page.route(`**/api/v1/feature-flags/${flagId}/environments/development`, (route) =>
      route.fulfill({ status: 503, contentType: "application/json", body: '{"detail": "Service unavailable"}' }),
    );
    await flags.chooseEnvironment("development");
    await expect(page.getByTestId("flag-error")).toHaveAttribute("role", "alert");
    await expect(page.getByRole("alert").filter({ hasText: "Could not load this feature flag" })).toBeVisible();
    await expect(flags.environmentAnnouncement).toHaveText("");
    await flags.chooseEnvironment("production");
    await expect(flags.rolloutValue).toHaveText("30%");
    await expect(flags.environmentAnnouncement).toHaveText("Showing Production.");
  });

  test("a viewer reads each environment and is offered no change", async ({ viewerPage: page }) => {
    const changes = flagChanges(page, flagId);
    const flags = await openFlag(page, "staging");
    await expect(flags.rolloutValue).toHaveText("60%");
    await expect(flags.statusBadge).toHaveAttribute("data-status", "active");
    await expect(page.getByTestId("flag-role-note")).toBeVisible();
    await expect(flags.rolloutPercentageInput).toBeDisabled();
    await expect(flags.detailToggle).toHaveCount(0);
    await expect(flags.saveButton).toHaveCount(0);

    await flags.gotoFlag(`${flagId}?env=production`);
    await expect(flags.rolloutValue).toHaveText("30%", { timeout: 15_000 });
    await expect(flags.productionMarker).toBeVisible();
    await expect(flags.productionBanner).toHaveCount(0);

    const list = await openList(page, "staging");
    await expect(list.getFlagRow(key).getByTestId("flag-status-pill")).toHaveText("On");
    await expect(list.toggleSwitch).toHaveCount(0);
    expect(changes, "no change was sent").toEqual([]);
  });

  test("a Staging save that answers after a switch leaves Production's form alone", async ({
    developerPage: page,
  }) => {
    const before = await stored(page);
    const flags = await openFlag(page, "staging");
    await expect(flags.rolloutValue).toHaveText("60%");
    const put = await holdAnswer(page, `/api/v1/feature-flags/${flagId}/environments/staging`);
    await flags.rolloutPercentageInput.fill("75");
    await flags.saveButton.click();
    await put.saved;

    await flags.chooseEnvironment("production");
    await expect(flags.rolloutValue).toHaveText("30%");
    await put.give();
    await expect(flags.rolloutValue, "Production's slider does not move").toHaveText("30%", { timeout: 1_000 });
    await expect(flags.saveSuccess).toHaveCount(0);

    // Staging was saved, Production was not touched, and the next save from
    // this page sends Production's own percentage.
    const after = await stored(page);
    expectUnchanged(after, before, ["production", "development"]);
    expect(after.staging).toMatchObject({ is_active: true, rollout_percentage: 75 });
    const next = page.waitForRequest(
      (request) => request.method() === "PUT" && new URL(request.url()).pathname === `/api/v1/feature-flags/${flagId}`,
    );
    await flags.saveButton.click();
    expect((await next).postDataJSON(), "the save sends Production's own percentage").toEqual({
      rollout_percentage: 30,
    });
    await expect(flags.saveSuccess).toBeVisible();
    const last = await stored(page);
    expectUnchanged(last, after, ["staging", "development"]);
    expect(last.production.rollout_percentage).toBe(30);
  });

  test("a switch in Staging that answers after a move leaves Development's row alone", async ({
    developerPage: page,
  }) => {
    const before = await stored(page);
    expect(before.development).toMatchObject({ is_active: true, rollout_percentage: 0 });
    const flags = await openList(page, "staging");
    const row = flags.getFlagRow(key);
    await expect(row).toContainText("75%");
    const put = await holdAnswer(page, `/api/v1/feature-flags/${flagId}/environments/staging`);
    await flags.rowToggle(key).click();
    await put.saved;

    await flags.chooseEnvironment("development");
    await expect(flags.flagList).toHaveAttribute("data-environment", "development");
    await expect(row.getByTestId("flag-status-pill")).toHaveText("On");
    await put.give();
    await expect(row.getByTestId("flag-status-pill"), "Development's row does not move").toHaveText("On", {
      timeout: 1_000,
    });
    await expect(row, "Development's percentage does not move").not.toContainText("75%", { timeout: 1_000 });
    await expect(flags.rowToggle(key)).toBeEnabled();

    const after = await stored(page);
    expectUnchanged(after, before, ["production", "development"]);
    expect(after.staging).toMatchObject({ is_active: false, rollout_percentage: 75, configured: true });
  });

  test("a control that takes the focus is scrolled clear of the sticky header", async ({ developerPage: page }) => {
    // 1280 x 1024 at 400% zoom, where the header is tallest and takes half the screen.
    await page.setViewportSize({ width: 320, height: 256 });
    const flags = await openFlag(page, "production");
    await expect(flags.safetyStatus).toBeVisible({ timeout: 20_000 });
    const header = page.getByRole("banner");
    for (const [what, control] of [
      ["Save", flags.saveButton],
      ["the slider", flags.rolloutPercentageInput],
      ["the switch", flags.detailToggle],
    ] as const) {
      await page.evaluate(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" }));
      await control.focus();
      await expect
        .poll(
          async () => {
            const [focused, bar] = [await control.boundingBox(), await header.boundingBox()];
            return focused && bar ? Math.round(focused.y - (bar.y + bar.height)) : Number.NEGATIVE_INFINITY;
          },
          { message: `${what} is below the header` },
        )
        .toBeGreaterThanOrEqual(0);
    }

    // The select is in the header, so focusing it moves nothing, at any size.
    for (const size of [{ width: 320, height: 256 }, PHONE, { width: 1280, height: 720 }]) {
      await page.setViewportSize(size);
      const before = await page.evaluate(() => {
        (document.activeElement as HTMLElement | null)?.blur();
        window.scrollTo({ top: 300, behavior: "instant" });
        return window.scrollY;
      });
      expect(before, `the page scrolls at ${size.width} px`).toBe(300);
      await flags.environmentSelect.focus();
      // A smooth scroll would be under way: the wait can only make a page that moves fail.
      const after = await page.evaluate(
        () => new Promise<number>((done) => setTimeout(() => done(window.scrollY), 700)),
      );
      expect(after, `focusing the select does not scroll the page at ${size.width} px`).toBe(before);
    }
  });

  test("the flag pages fit a phone in every environment", async ({ developerPage: page }) => {
    await page.setViewportSize(PHONE);
    for (const environment of ["production", "staging", "development"] as const) {
      const flags = await openFlag(page, environment);
      await expect(flags.environmentSelect).toBeVisible();
      await expectNoSidewaysScroll(page, `a flag in ${environment} at 390 px`);
      await flags.comparisonToggle.click();
      await expect(flags.comparisonTable).toBeVisible();
      await expectNoSidewaysScroll(page, `the comparison in ${environment} at 390 px`);
      await expectNoSeriousViolations(page, `a flag in ${environment} at 390 px`);

      const list = await openList(page, environment);
      await expect(list.environmentSelect).toBeVisible();
      await expectNoSidewaysScroll(page, `the list in ${environment} at 390 px`);
      await expectNoSeriousViolations(page, `the list in ${environment} at 390 px`);
    }
  });
});
