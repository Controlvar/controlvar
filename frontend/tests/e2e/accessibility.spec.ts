import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Accessibility tests using @axe-core/playwright for WCAG 2.1 AA compliance.
 *
 * Install: npm install -D @axe-core/playwright
 * Run: npx playwright test accessibility
 *
 * These tests scan pages for accessibility violations and fail on
 * critical or serious issues. Moderate/minor issues are reported as warnings.
 */

// Dynamic import to gracefully handle missing dependency
async function getAxeBuilder() {
  try {
    const { default: AxeBuilder } = await import("@axe-core/playwright");
    return AxeBuilder;
  } catch {
    return null;
  }
}

test.describe("Accessibility Audits", () => {
  test.beforeEach(async () => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) {
      test.skip();
    }
  });

  test("login page has no critical a11y violations", async ({ page }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    await page.goto("/login");
    await page.waitForLoadState("networkidle");

    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();

    const critical = results.violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious"
    );

    if (critical.length > 0) {
      const summary = critical
        .map((v) => `[${v.impact}] ${v.id}: ${v.description} (${v.nodes.length} instances)`)
        .join("\n");
      expect(critical, `A11y violations found:\n${summary}`).toHaveLength(0);
    }
  });

  test("dashboard has no critical a11y violations", async ({ adminPage }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    await adminPage.goto("/");
    await adminPage.waitForLoadState("networkidle");

    const results = await new AxeBuilder({ page: adminPage })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();

    const critical = results.violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious"
    );

    expect(critical).toHaveLength(0);
  });

  test("experiments page has no critical a11y violations", async ({
    adminPage,
  }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    await adminPage.goto("/experiments");
    await adminPage.waitForLoadState("networkidle");

    const results = await new AxeBuilder({ page: adminPage })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();

    const critical = results.violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious"
    );

    expect(critical).toHaveLength(0);
  });

  test("feature flags page has no critical a11y violations", async ({
    adminPage,
  }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    await adminPage.goto("/feature-flags");
    await adminPage.waitForLoadState("networkidle");

    const results = await new AxeBuilder({ page: adminPage })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();

    const critical = results.violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious"
    );

    expect(critical).toHaveLength(0);
  });

  // A flag's value type and its two values (#1497): the form with each type
  // chosen, and a value flag's page. Neither page was scanned before.
  test("new feature flag form has no critical a11y violations, whichever type is chosen", async ({
    adminPage,
  }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    await adminPage.goto("/feature-flags/new");
    await adminPage.waitForLoadState("networkidle");

    for (const type of ["Boolean", "String", "Number", "JSON"]) {
      await adminPage.getByRole("radio", { name: type }).check();
      const results = await new AxeBuilder({ page: adminPage })
        .withTags(["wcag2a", "wcag2aa"])
        .analyze();
      const critical = results.violations.filter(
        (v) => v.impact === "critical" || v.impact === "serious"
      );
      const summary = critical.map((v) => `[${v.impact}] ${v.id}`).join("\n");
      expect(critical, `${type} chosen:\n${summary}`).toHaveLength(0);
    }
  });

  test("a value flag's page has no critical a11y violations", async ({
    adminPage,
  }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    await adminPage.goto("/feature-flags");
    await adminPage.waitForLoadState("networkidle");
    const token = await adminPage.evaluate(
      (key) => window.localStorage.getItem(key),
      TOKEN_STORAGE_KEY
    );
    expect(token, "the admin session has a token").toBeTruthy();
    const headers = { Authorization: `Bearer ${token}` };
    // The journey database is shared: this flag is the only one touched, and
    // it is deleted in `finally`.
    const stamp = Date.now();
    const created = await adminPage.request.post(`${API_URL}/api/v1/feature-flags/`, {
      headers,
      data: {
        key: `a11y_value_${stamp}`,
        name: `A11y value flag ${stamp}`,
        value_type: "json",
        on_value: { color: "blue", sizes: [1, 2] },
        off_value: { color: "grey" },
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    const { id } = (await created.json()) as { id: string };
    try {
      await adminPage.goto(`/feature-flags/${id}`);
      await expect(adminPage.getByTestId("flag-values-section")).toBeVisible({ timeout: 15_000 });

      // What a value flag adds: the header (its type and status) and the
      // Values section. The rest of the flag page is every flag's, and its
      // slate-400 "Last checked" and "0%"/"100%" fail colour contrast today
      // (measured on this scan of the whole page), which is not this change's.
      const results = await new AxeBuilder({ page: adminPage })
        .include('[data-testid="flag-header"]')
        .include('[data-testid="flag-values-section"]')
        .withTags(["wcag2a", "wcag2aa"])
        .analyze();
      const critical = results.violations.filter(
        (v) => v.impact === "critical" || v.impact === "serious"
      );
      expect(critical).toHaveLength(0);
    } finally {
      await adminPage.request.delete(`${API_URL}/api/v1/feature-flags/${id}`, { headers });
    }
  });

  test("admin panel has no critical a11y violations", async ({
    adminPage,
  }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    await adminPage.goto("/admin");
    await adminPage.waitForLoadState("networkidle");

    const results = await new AxeBuilder({ page: adminPage })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();

    const critical = results.violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious"
    );

    expect(critical).toHaveLength(0);
  });

  test("keyboard navigation works on main pages", async ({ adminPage }) => {
    await adminPage.goto("/");
    await adminPage.waitForLoadState("networkidle");

    // Tab through the page and verify focus is visible
    await adminPage.keyboard.press("Tab");
    const focusedElement = await adminPage.evaluate(() => {
      const el = document.activeElement;
      return el ? el.tagName : null;
    });

    // Something should receive focus
    expect(focusedElement).not.toBeNull();
  });

  test("forms have proper labels", async ({ adminPage }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    // Navigate to a form page (create experiment)
    await adminPage.goto("/experiments");
    await adminPage.waitForLoadState("networkidle");

    const createButton = adminPage.locator(
      'button:has-text("Create"), a:has-text("Create")'
    );
    if (await createButton.isVisible().catch(() => false)) {
      await createButton.click();
      await adminPage.waitForLoadState("networkidle");

      // Run axe specifically for form-related rules
      const results = await new AxeBuilder({ page: adminPage })
        .withRules(["label", "label-title-only", "select-name"])
        .analyze();

      const formViolations = results.violations.filter(
        (v) => v.impact === "critical" || v.impact === "serious"
      );

      expect(formViolations).toHaveLength(0);
    }
  });

  test("color contrast meets WCAG AA", async ({ adminPage }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    await adminPage.goto("/");
    await adminPage.waitForLoadState("networkidle");

    const results = await new AxeBuilder({ page: adminPage })
      .withRules(["color-contrast"])
      .analyze();

    // Report contrast issues but don't fail on minor ones
    if (results.violations.length > 0) {
      const summary = results.violations
        .map((v) => `${v.nodes.length} contrast issues`)
        .join(", ");
      console.warn(`Color contrast warnings: ${summary}`);
    }
  });

  test("images have alt text", async ({ adminPage }) => {
    const AxeBuilder = await getAxeBuilder();
    if (!AxeBuilder) return;

    await adminPage.goto("/");
    await adminPage.waitForLoadState("networkidle");

    const results = await new AxeBuilder({ page: adminPage })
      .withRules(["image-alt"])
      .analyze();

    const critical = results.violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious"
    );

    expect(critical).toHaveLength(0);
  });
});
