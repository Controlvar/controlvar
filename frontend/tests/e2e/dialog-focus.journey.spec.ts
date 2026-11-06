import AxeBuilder from "@axe-core/playwright";
import type { Locator, Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: a dialog keeps focus inside and gives it back (#369).
 *
 * The shared Dialog, in a real browser, on /admin/users (the edit-user
 * dialog): focus moves into it, Tab and Shift+Tab stay inside it, the page
 * behind it (`#__next`) is inert while Next's route announcer, a child of
 * body, is not, axe finds nothing serious in it, and on close focus is back
 * on the row's Edit button. jsdom implements no `inert` and moves no focus on
 * Tab, so this is the only place either is shown.
 *
 * "Saved changes to <name>." is written in the page's live region after the
 * dialog has taken `inert` off `#__next`: a MutationObserver installed in the
 * page records every live-region text change with whether an ancestor was
 * inert at that moment (the `inert` attribute changes are replayed in order
 * with the text changes), and none may be.
 *
 * The axe import is static, so a missing @axe-core/playwright fails this file
 * instead of skipping it.
 *
 * The journey creates its own VIEWER through the API, edits only that user,
 * and deletes it in a `finally`: the seeded accounts other journeys sign in
 * with are never changed.
 */

/** An explicit live region, or a role that is one (as in src/tests/helpers/watchLiveRegions.ts). */
const LIVE_REGION = '[aria-live], [role="status"], [role="alert"], [role="log"]';

interface LiveRegionChange {
  text: string;
  underInert: boolean;
}

type WatchedWindow = Window & { __liveRegionChanges?: LiveRegionChange[] };

async function watchLiveRegions(page: Page): Promise<void> {
  await page.evaluate((selector) => {
    const inert = new Set<Element>(Array.from(document.querySelectorAll("[inert]")));
    const changes: { text: string; underInert: boolean }[] = [];
    (window as Window & { __liveRegionChanges?: typeof changes }).__liveRegionChanges = changes;
    new MutationObserver((records) => {
      for (const record of records) {
        if (record.type === "attributes") {
          // The Dialog sets `inert` only where it is absent: no old value is
          // an add, an old value a remove.
          if (record.oldValue === null) inert.add(record.target as Element);
          else inert.delete(record.target as Element);
          continue;
        }
        const node =
          record.target.nodeType === Node.ELEMENT_NODE
            ? (record.target as Element)
            : record.target.parentElement;
        const region = node?.closest(selector);
        if (!region) continue;
        changes.push({
          text: region.textContent ?? "",
          underInert: Array.from(inert).some((el) => el.contains(region)),
        });
      }
    }).observe(document.body, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["inert"],
      attributeOldValue: true,
    });
  }, LIVE_REGION);
}

async function liveRegionChanges(page: Page): Promise<LiveRegionChange[]> {
  // A microtask turn, so the observer has been called with what is queued.
  return page.evaluate(
    () =>
      new Promise<LiveRegionChange[]>((resolve) =>
        setTimeout(() => resolve((window as WatchedWindow).__liveRegionChanges ?? []), 0),
      ),
  );
}

const focusIsInsideDialog = (page: Page) =>
  page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]')));

const appRootIsInert = (page: Page) =>
  page.evaluate(() => document.getElementById("__next")?.hasAttribute("inert") ?? null);

async function expectNoSeriousViolationsIn(page: Page, selector: string, where: string): Promise<void> {
  const results = await new AxeBuilder({ page }).include(selector).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(serious, `axe on ${where}`).toEqual([]);
  // The scan reached the dialog: the rule that names it ran on it and passed.
  const named = await new AxeBuilder({ page }).include(selector).withRules(["aria-dialog-name"]).analyze();
  expect(named.violations, `aria-dialog-name on ${where}`).toEqual([]);
  expect(named.passes.map((p) => p.id), `aria-dialog-name ran on ${where}`).toContain("aria-dialog-name");
}

/** Open the dialog from its row's Edit button, by keyboard, so the button is focused first. */
async function openFrom(page: Page, editButton: Locator): Promise<Locator> {
  await editButton.focus();
  await expect(editButton).toBeFocused();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Edit user" });
  await expect(dialog).toBeVisible();
  return dialog;
}

test.describe("Journey: a dialog keeps focus inside and gives it back", () => {
  test("the edit-user dialog: focus in, Tab trapped, page inert, focus back, announced after close", async ({
    adminPage: page,
  }) => {
    const stamp = Date.now();
    const username = `e2e_dialog_${stamp}`;
    let headers: Record<string, string> = {};
    let userId: string | null = null;
    try {
      await page.goto("/admin/users");
      await expect(page.getByTestId("user-table")).toBeVisible({ timeout: 15_000 });
      const token = await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY);
      expect(token, "the admin session has a token").toBeTruthy();
      headers = { Authorization: `Bearer ${token}` };

      const created = await page.request.post(`${API_URL}/api/v1/users/`, {
        headers,
        data: {
          username,
          email: `${username}@example.com`,
          password: `Journey${stamp}x`,
          role: "VIEWER",
        },
      });
      expect(created.status(), await created.text()).toBe(201);
      userId = ((await created.json()) as { id: string }).id;

      await page.getByTestId("user-search").fill(username);
      const editButton = page.getByTestId(`edit-user-${userId}`);
      await expect(editButton).toBeVisible({ timeout: 15_000 });
      await watchLiveRegions(page);

      // --- Open: focus moves in, the page behind is inert, the announcer is not.
      const dialog = await openFrom(page, editButton);
      await expect(page.getByTestId("edit-role-select")).toBeFocused();
      expect(await appRootIsInert(page), "#__next is inert while the dialog is open").toBe(true);
      expect(
        await page.evaluate(() => document.getElementById("__next")?.contains(document.querySelector('[role="dialog"]'))),
        "the dialog is not inside #__next",
      ).toBe(false);
      expect(
        await page.evaluate(() => {
          const announcer = document.querySelector("next-route-announcer");
          return announcer
            ? { childOfBody: announcer.parentElement === document.body, inert: announcer.closest("[inert]") !== null }
            : null;
        }),
        "Next's route announcer is a child of body and not inert",
      ).toEqual({ childOfBody: true, inert: false });
      expect(
        await page.evaluate(() => {
          const behind = document.querySelector<HTMLElement>('[data-testid="invite-user-button"]');
          behind?.focus();
          return behind !== null && document.activeElement === behind;
        }),
        "a control behind the dialog cannot take focus",
      ).toBe(false);
      expect(await page.evaluate(() => getComputedStyle(document.body).overflow), "the page does not scroll").toBe(
        "hidden",
      );
      await expectNoSeriousViolationsIn(page, '[role="dialog"]', "the edit-user dialog");

      // --- Tab and Shift+Tab: never out of the dialog.
      for (let i = 1; i <= 8; i += 1) {
        await page.keyboard.press("Tab");
        expect(await focusIsInsideDialog(page), `Tab ${i} stays in the dialog`).toBe(true);
      }
      for (let i = 1; i <= 8; i += 1) {
        await page.keyboard.press("Shift+Tab");
        expect(await focusIsInsideDialog(page), `Shift+Tab ${i} stays in the dialog`).toBe(true);
      }

      // --- Save: the dialog closes, focus is back on the row, the save is
      // announced, and never while the page was inert.
      await dialog.getByTestId("edit-role-select").selectOption("DEVELOPER");
      await dialog.getByTestId("edit-save-button").click();
      await expect(dialog).toHaveCount(0, { timeout: 15_000 });
      await expect(page.getByTestId("user-save-status")).toHaveText(`Saved changes to ${username}.`, {
        timeout: 15_000,
      });
      await expect(editButton).toBeFocused({ timeout: 15_000 });
      expect(await appRootIsInert(page), "#__next is not inert after close").toBe(false);
      const changes = await liveRegionChanges(page);
      expect(
        changes.some((c) => c.text.includes(`Saved changes to ${username}.`)),
        `the observer saw the announcement: ${JSON.stringify(changes)}`,
      ).toBe(true);
      expect(changes.filter((c) => c.underInert), "no live region changed under an inert ancestor").toEqual([]);

      // --- Escape: closes it, and focus is back on the row.
      await openFrom(page, editButton);
      await page.keyboard.press("Escape");
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await expect(editButton).toBeFocused();
      expect(await appRootIsInert(page)).toBe(false);
      expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).not.toBe("hidden");

      // --- At 320 px: the dialog fits the width and scrolls inside itself.
      await page.setViewportSize({ width: 320, height: 480 });
      const small = await openFrom(page, editButton);
      const box = await small.boundingBox();
      expect(box, "the dialog has a box").not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(320);
      expect(box!.height).toBeLessThanOrEqual(480);
      const cancel = small.getByTestId("edit-cancel-button");
      await cancel.scrollIntoViewIfNeeded();
      await expect(cancel).toBeInViewport();
      await cancel.click();
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await expect(editButton).toBeFocused();

      expect((await liveRegionChanges(page)).filter((c) => c.underInert)).toEqual([]);
    } finally {
      if (userId) {
        const removed = await page.request.delete(`${API_URL}/api/v1/admin/users/${userId}`, { headers });
        expect(removed.status(), "the journey's user is deleted").toBe(204);
      }
    }
  });
});
