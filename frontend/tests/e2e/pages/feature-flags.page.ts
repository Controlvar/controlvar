import { type Page, type Locator, expect } from "@playwright/test";
import { CommonPage } from "./common.page";

/**
 * Page object for the feature flag list (`/feature-flags`), create form
 * (`/feature-flags/new`) and detail page (`/feature-flags/[id]`).
 *
 * Selectors are the `data-testid`s rendered by `src/pages/feature-flags/`.
 */
export class FeatureFlagsPage {
  readonly page: Page;
  readonly common: CommonPage;

  // List page
  readonly createButton: Locator;
  readonly flagList: Locator;
  readonly flagRows: Locator;
  readonly emptyState: Locator;
  readonly listError: Locator;
  readonly listToggleError: Locator;
  /** Every on/off switch on the current page (list rows or detail header). */
  readonly toggleSwitch: Locator;

  // Create form
  readonly nameInput: Locator;
  readonly keyInput: Locator;
  readonly descriptionInput: Locator;
  readonly submitButton: Locator;

  // Detail page
  readonly detail: Locator;
  readonly detailName: Locator;
  readonly detailKey: Locator;
  readonly statusBadge: Locator;
  readonly detailToggle: Locator;
  readonly detailToggleError: Locator;
  readonly rolloutPercentageInput: Locator;
  readonly rolloutValue: Locator;
  readonly saveButton: Locator;
  readonly saveSuccess: Locator;
  readonly rolloutScheduleSection: Locator;
  readonly rolloutScheduleEmpty: Locator;
  readonly rolloutStages: Locator;
  readonly safetySection: Locator;
  readonly safetyStatus: Locator;
  readonly safetyRecheck: Locator;
  readonly notFound: Locator;

  // Environments (the switcher is in the dashboard header, on these pages only)
  readonly environmentBar: Locator;
  readonly environmentSelect: Locator;
  readonly environmentAnnouncement: Locator;
  readonly productionMarker: Locator;
  readonly productionBanner: Locator;
  readonly productionOnlyNote: Locator;
  readonly notConfigured: Locator;
  readonly comparison: Locator;
  readonly comparisonToggle: Locator;
  readonly comparisonTable: Locator;

  constructor(page: Page) {
    this.page = page;
    this.common = new CommonPage(page);

    // List
    this.createButton = page.getByTestId("new-flag-btn");
    this.flagList = page.getByTestId("flags-table");
    this.flagRows = page.getByTestId("flag-row");
    this.emptyState = page.getByTestId("flags-empty");
    this.listError = page.getByTestId("flags-error");
    this.listToggleError = page.getByTestId("flag-toggle-error");
    this.toggleSwitch = page.getByRole("switch");

    // Create form (src/pages/feature-flags/new.tsx)
    this.nameInput = page.getByTestId("flag-name-input");
    this.keyInput = page.getByTestId("flag-key-input");
    this.descriptionInput = page.locator("textarea").first();
    this.submitButton = page.getByTestId("submit-flag");

    // Detail
    this.detail = page.getByTestId("flag-detail");
    this.detailName = page.getByTestId("flag-name");
    this.detailKey = page.getByTestId("flag-key");
    this.statusBadge = page.getByTestId("flag-status");
    this.detailToggle = page.getByTestId("flag-toggle");
    this.detailToggleError = page.getByTestId("flag-toggle-error");
    this.rolloutPercentageInput = page.getByTestId("rollout-percentage");
    this.rolloutValue = page.getByTestId("rollout-value");
    this.saveButton = page.getByTestId("save-flag");
    this.saveSuccess = page.getByTestId("save-success");
    this.rolloutScheduleSection = page.getByTestId("rollout-schedule-section");
    this.rolloutScheduleEmpty = page.getByTestId("rollout-schedule-empty");
    this.rolloutStages = page.getByTestId("rollout-stage");
    this.safetySection = page.getByTestId("safety-section");
    this.safetyStatus = page.getByTestId("safety-status");
    this.safetyRecheck = page.getByTestId("safety-recheck");
    this.notFound = page.getByTestId("flag-not-found");

    // Environments (src/components/feature-flags/, src/components/AppShell.tsx)
    this.environmentBar = page.getByTestId("environment-bar");
    this.environmentSelect = page.getByTestId("environment-select");
    this.environmentAnnouncement = page.getByTestId("environment-announcement");
    this.productionMarker = page.getByTestId("production-marker");
    this.productionBanner = page.getByTestId("production-banner");
    this.productionOnlyNote = page.getByTestId("production-only-note");
    this.notConfigured = page.getByTestId("flag-not-configured");
    this.comparison = page.getByTestId("environment-comparison");
    this.comparisonToggle = page.getByTestId("environment-comparison-toggle");
    this.comparisonTable = page.getByTestId("environment-comparison-table");
  }

  async goto() {
    await this.common.navigateTo("/feature-flags");
  }

  /** Choose an environment in the header, and wait for `?env=` to name it. */
  async chooseEnvironment(environment: "development" | "staging" | "production") {
    await this.environmentSelect.selectOption(environment);
    await expect(this.page).toHaveURL(new RegExp(`[?&]env=${environment}(&|$)`));
    await expect(this.environmentSelect).toHaveValue(environment);
  }

  async gotoNew() {
    await this.common.navigateTo("/feature-flags/new");
  }

  async gotoFlag(id: string) {
    await this.common.navigateTo(`/feature-flags/${id}`);
  }

  /** Filter pill on the list page: `all | active | inactive`. */
  async filterByStatus(status: "all" | "active" | "inactive") {
    await this.page.getByTestId(`flag-filter-${status}`).click();
    await this.common.waitForPageReady();
  }

  /** Create a flag and resolve with its id once the detail page is up. */
  async createFlag(name: string, key: string, description?: string): Promise<string> {
    await this.gotoNew();
    await this.nameInput.fill(name);
    await this.keyInput.fill(key);
    if (description) {
      await this.descriptionInput.fill(description);
    }
    await this.submitButton.click();
    await this.page.waitForURL(/\/feature-flags\/[^/]+$/, { timeout: 15_000 });
    await expect(this.detail).toBeVisible({ timeout: 15_000 });
    const match = this.page.url().match(/\/feature-flags\/([^/?#]+)/);
    return match ? match[1] : "";
  }

  getFlagRow(key: string): Locator {
    return this.page.locator(`[data-testid="flag-row"][data-flag-key="${key}"]`);
  }

  /** The row switch for a flag key on the list page. */
  rowToggle(key: string): Locator {
    return this.page.getByTestId(`flag-toggle-${key}`);
  }

  async clickFlag(name: string) {
    await this.flagRows.filter({ hasText: name }).first().getByTestId("flag-link").click();
    await expect(this.detail).toBeVisible({ timeout: 15_000 });
  }

  /** Flip the first switch on the page and wait for the request to settle. */
  async toggleFlag() {
    const toggle = this.toggleSwitch.first();
    const before = await toggle.getAttribute("aria-checked");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", before === "true" ? "false" : "true");
    await expect(toggle).toBeEnabled({ timeout: 15_000 });
  }

  /** Flip a specific flag's row switch on the list page. */
  async toggleFlagByKey(key: string) {
    const toggle = this.rowToggle(key);
    const before = await toggle.getAttribute("aria-checked");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", before === "true" ? "false" : "true");
    await expect(toggle).toBeEnabled({ timeout: 15_000 });
  }

  /** Detail-page switch state: `"active"` or `"inactive"`. */
  async getStatus(): Promise<string> {
    return (await this.statusBadge.getAttribute("data-status")) ?? "";
  }

  async setRolloutPercentage(percentage: number) {
    await this.rolloutPercentageInput.fill(String(percentage));
    await this.saveButton.click();
    await expect(this.saveSuccess).toBeVisible({ timeout: 15_000 });
  }

  async getFlagCount(): Promise<number> {
    return this.flagRows.count();
  }
}
