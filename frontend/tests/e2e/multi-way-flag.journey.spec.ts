import AxeBuilder from "@axe-core/playwright";
import type { Locator, Page } from "@playwright/test";
import { test, expect } from "./fixtures/auth.fixture";
import { API_URL, TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: a flag that serves one of three values (#2012).
 *
 * A DEVELOPER (not a superuser) makes a string flag through the API, then
 * gives it a split of three values in the dashboard, by `data-testid`, and
 * saves. The API must hold the split exactly as typed (values, weights,
 * order), and `/evaluate` for user-0 .. user-99 must answer only the split's
 * values, all three of them, and the same value for each user on a second
 * call. Which user gets which value is the server's own tests' business
 * (backend/tests/unit/services/test_flag_split_serving.py); nothing here
 * works it out.
 *
 * The second test is the same edit by keyboard alone: add a split, type the
 * values, leave the weights adding up to 90, save and be refused, fix, save,
 * with where focus is asserted at each step.
 *
 * The flag and the API key are this journey's own, named with a timestamp,
 * and deleted at the end; the seeded flags are never touched.
 */

const VALUES = ["red", "green", "blue"];

async function expectNoSeriousViolations(page: Page, where: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(serious, `axe on ${where}`).toEqual([]);
}

async function tokenIn(page: Page): Promise<string> {
  const token = await page.evaluate((storageKey) => window.localStorage.getItem(storageKey), TOKEN_STORAGE_KEY);
  expect(token, "the session has a token").toBeTruthy();
  return token as string;
}

/** What the focused element is, by its test id, or its tag when it has none. */
async function focused(page: Page): Promise<string> {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    return el?.getAttribute("data-testid") ?? el?.tagName.toLowerCase() ?? "none";
  });
}

test.describe("Journey: a flag with a split of three values", () => {
  test.describe.configure({ mode: "serial" });

  const stamp = Date.now();
  let headers: Record<string, string> = {};
  let apiKey = "";
  let keyId = "";
  const flags: string[] = [];

  async function makeFlag(page: Page, key: string): Promise<string> {
    const created = await page.request.post(`${API_URL}/api/v1/feature-flags/`, {
      headers: { ...headers, "content-type": "application/json" },
      data: {
        key,
        name: `E2E split ${key}`,
        is_active: true,
        rollout_percentage: 100,
        value_type: "string",
        on_value: "plain",
        off_value: "none",
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    const id = ((await created.json()) as { id: string }).id;
    flags.push(id);
    return id;
  }

  async function storedSplit(page: Page, id: string): Promise<unknown> {
    const flag = await page.request.get(`${API_URL}/api/v1/feature-flags/${id}`, { headers });
    expect(flag.status(), "GET /feature-flags/{id}").toBe(200);
    return ((await flag.json()) as { split: unknown }).split;
  }

  /** The value `/evaluate` answers for each of user-0 .. user-99. */
  async function evaluateAll(page: Page, key: string): Promise<Record<string, unknown>> {
    const answers: Record<string, unknown> = {};
    for (let n = 0; n < 100; n += 1) {
      const user = `user-${n}`;
      const answer = await page.request.get(
        `${API_URL}/api/v1/feature-flags/evaluate/${key}?user_id=${encodeURIComponent(user)}`,
        { headers: { "X-API-Key": apiKey } },
      );
      expect(answer.status(), `evaluate ${key} for ${user}`).toBe(200);
      const body = (await answer.json()) as { enabled: boolean; value: unknown; value_type: string };
      expect(body.enabled, `${user} is enabled at 100%`).toBe(true);
      expect(body.value_type).toBe("string");
      answers[user] = body.value;
    }
    return answers;
  }

  test.beforeAll(async ({ sessions }) => {
    const page = await (await sessions("developer")).newPage();
    try {
      await page.goto("/feature-flags");
      headers = { Authorization: `Bearer ${await tokenIn(page)}` };
      const made = await page.request.post(`${API_URL}/api/v1/api-keys`, {
        headers,
        data: { name: `e2e_split_${stamp}` },
      });
      expect(made.status(), await made.text()).toBe(201);
      const key = (await made.json()) as { id: string; key: string };
      apiKey = key.key;
      keyId = key.id;
    } finally {
      await page.close();
    }
  });

  test.afterAll(async ({ sessions }) => {
    const page = await (await sessions("developer")).newPage();
    try {
      for (const id of flags) {
        const removed = await page.request.delete(`${API_URL}/api/v1/feature-flags/${id}`, { headers });
        expect(removed.status(), `DELETE the journey's flag ${id}`).toBe(204);
      }
      if (keyId) {
        const removed = await page.request.delete(`${API_URL}/api/v1/api-keys/${keyId}`, { headers });
        expect([200, 204], `DELETE the journey's key ${keyId}`).toContain(removed.status());
      }
    } finally {
      await page.close();
    }
  });

  test("the split saved in the dashboard is the one the API holds and serves", async ({ developerPage: page }) => {
    const key = `e2e_split_${stamp}`;
    const id = await makeFlag(page, key);
    expect(await storedSplit(page, id)).toBeNull();

    await page.goto(`/feature-flags/${id}?env=production`);
    await expect(page.getByTestId("flag-key")).toHaveText(key, { timeout: 15_000 });
    await page.getByTestId("split-add").click();
    for (let i = 0; i < VALUES.length; i += 1) {
      await page.getByTestId(`split-value-${i}`).fill(VALUES[i]);
    }
    await expect(page.getByTestId("split-total")).toHaveText("Total 100%");
    await expectNoSeriousViolations(page, "the split editor, valid");

    const sent = page.waitForRequest(
      (request) => request.method() === "PUT" && new URL(request.url()).pathname === `/api/v1/feature-flags/${id}`,
    );
    await page.getByTestId("save-flag").click();
    await expect(page.getByTestId("save-success")).toBeVisible();
    const body = (await sent).postDataJSON() as Record<string, unknown>;
    expect(body.split, "the save sent the split").toEqual([
      { value: "red", weight: 34 },
      { value: "green", weight: 33 },
      { value: "blue", weight: 33 },
    ]);

    // The API holds it exactly: the values, the weights, the order.
    expect(await storedSplit(page, id)).toEqual([
      { value: "red", weight: 34 },
      { value: "green", weight: 33 },
      { value: "blue", weight: 33 },
    ]);

    // And serves only its values, all three, and the same one again to each user.
    const first = await evaluateAll(page, key);
    expect(new Set(Object.values(first))).toEqual(new Set(VALUES));
    const second = await evaluateAll(page, key);
    expect(second).toEqual(first);

    // The list shows the badge, in text.
    await page.goto("/feature-flags?env=production");
    const row = page.locator(`[data-testid="flag-row"][data-flag-key="${key}"]`);
    await expect(row.getByTestId("flag-split-badge")).toHaveText("Split", { timeout: 15_000 });

    // A save of the rollout alone sends no split, and leaves it as it was.
    await page.goto(`/feature-flags/${id}?env=production`);
    await expect(page.getByTestId("split-value-0")).toHaveValue("red", { timeout: 15_000 });
    const rolloutOnly = page.waitForRequest(
      (request) => request.method() === "PUT" && new URL(request.url()).pathname === `/api/v1/feature-flags/${id}`,
    );
    const slider = page.getByTestId("rollout-percentage");
    await slider.focus();
    await page.keyboard.press("ArrowLeft");
    await page.getByTestId("save-flag").click();
    await expect(page.getByTestId("save-success")).toBeVisible();
    expect(Object.keys((await rolloutOnly).postDataJSON() as object)).toEqual(["rollout_percentage"]);
    expect(await storedSplit(page, id)).toEqual([
      { value: "red", weight: 34 },
      { value: "green", weight: 33 },
      { value: "blue", weight: 33 },
    ]);
  });

  test("by keyboard: add a split, be refused at 90%, fix it and save, focus where it should be", async ({
    developerPage: page,
  }) => {
    const key = `e2e_split_keys_${stamp}`;
    const id = await makeFlag(page, key);
    await page.goto(`/feature-flags/${id}?env=production`);
    await expect(page.getByTestId("flag-key")).toHaveText(key, { timeout: 15_000 });

    const add: Locator = page.getByTestId("split-add");
    await add.focus();
    await expectNoSeriousViolations(page, "the split editor, empty");
    await page.keyboard.press("Enter");
    expect(await focused(page), "Add a split moves focus to Value 1").toBe("split-value-0");

    // Value, then Weight, then Remove, per row; a text area does not keep Tab.
    for (let i = 0; i < VALUES.length; i += 1) {
      expect(await focused(page)).toBe(`split-value-${i}`);
      await page.keyboard.type(VALUES[i]);
      await page.keyboard.press("Tab");
      expect(await focused(page)).toBe(`split-weight-${i}`);
      if (i === 2) {
        await page.keyboard.press("ControlOrMeta+a");
        await page.keyboard.type("23");
      }
      await page.keyboard.press("Tab");
      expect(await focused(page)).toBe(`split-remove-${i}`);
      await page.keyboard.press("Tab");
    }
    await expect(page.getByTestId("split-total-status")).toHaveText("Total 90%: 10% more needed");

    await page.getByTestId("save-flag").focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("split-total-error")).toHaveText("Weights must add up to 100; they add up to 90.");
    expect(await focused(page), "the refusal moves focus to the first weight").toBe("split-weight-0");
    await expectNoSeriousViolations(page, "the split editor, refused");
    expect(await storedSplit(page, id), "nothing was saved").toBeNull();

    // Fix the third weight: Tab from the first weight through the rows.
    for (const stop of ["split-remove-0", "split-value-1", "split-weight-1", "split-remove-1", "split-value-2", "split-weight-2"]) {
      await page.keyboard.press("Tab");
      expect(await focused(page)).toBe(stop);
    }
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type("33");
    await page.keyboard.press("Tab");
    await expect(page.getByTestId("split-total-status")).toHaveText("Total 100%");

    await page.getByTestId("save-flag").focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("save-success")).toBeVisible();
    expect(await storedSplit(page, id)).toEqual([
      { value: "red", weight: 34 },
      { value: "green", weight: 33 },
      { value: "blue", weight: 33 },
    ]);
  });
});
