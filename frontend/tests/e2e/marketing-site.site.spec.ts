import AxeBuilder from "@axe-core/playwright";
import { devices, expect, test, type Page } from "@playwright/test";
import { BASE_URL } from "./env";

/**
 * The public site in a real browser (#1459, PR G): controlvar.com's pages
 * (the homepage, /docs, /books, /walkthroughs, /power-calculator, /privacy)
 * and its not-found page at a phone's width and a desktop's, over the
 * MARKETING export.
 *
 * The `site` project (playwright.config.ts) runs this file and nothing else
 * does: it is the iPhone 13 descriptor on Chromium, the only browser CI
 * installs. The Frontend Tests job runs it right after "Marketing site export,
 * with the walkthrough media", when `frontend/out` is that export.
 *
 * For each page, at 390 px with the menu closed and then open, and at 1440 px:
 *   - phone width (390 only): `innerWidth` is 390 and so is the document's
 *     `scrollWidth` (`overflow-x: hidden` on html or body hides a wide element
 *     from both, so neither alone is enough), and no element's right edge is
 *     past 390 unless an ancestor below <body> clips it;
 *   - axe, every rule it runs by default, colour contrast included, and no
 *     violation of any impact;
 *   - Tab: every element Tab reaches shows a focus outline or ring that it
 *     does not have unfocused, and every visible tabbable element is reached.
 *
 * jsdom cannot measure any of this (no layout, no colour), so these are not
 * Jest tests. Defects are planted in the SOURCE and the export rebuilt: one
 * planted in `out/index.html` is erased by hydration.
 *
 * The marketing build has no API. Its shell asks for `/api/v1/modules`, which
 * the export server answers 502; that request is expected and nothing here
 * looks at it.
 */

// The last is a path that does not exist: the export server answers it with
// out/404.html, the not-found page, as nginx does (Amplify does what its
// console's 404 rule says). /books and /walkthroughs are the marketing build's
// own pages (site design 2026-11-11).
const PAGES = ["/", "/docs", "/books", "/walkthroughs", "/power-calculator", "/privacy", "/no-such-page"] as const;
const PHONE_WIDTH = 390;
const DESKTOP = { width: 1440, height: 900 };

/**
 * The marketing build's /walkthroughs page renders one <video> per walkthrough
 * (nine today); the platform build has no such page, and no homepage has a
 * player since the site design of 2026-11-11 (its teaser shows posters).
 */
const MIN_VIDEOS = 6;
const VIDEOS_PAGE = "/walkthroughs";

/** A focus walk longer than this is a focus trap, not a page. */
const MAX_TAB_STOPS = 400;

async function open(page: Page, path: string): Promise<void> {
  await page.goto(path);
  // Hydrated, and every poster loaded: the menu button only works once React has run.
  await page.waitForLoadState("networkidle");
}

/** innerWidth, scrollWidth, and every element past the right edge that nothing below <body> clips. */
async function checkPhoneWidth(page: Page, label: string): Promise<void> {
  const measured = await page.evaluate((width) => {
    // Measure from the left edge. Under `html,body{overflow-x:hidden}` the body
    // is itself a scroller: with a 600 px plant, by the time the menu was open
    // it had scrolled 149 px sideways and the plant's right edge read 475.
    window.scrollTo(0, window.scrollY);
    document.documentElement.scrollLeft = 0;
    document.body.scrollLeft = 0;
    const describe = (el: Element): string => {
      const cls = (el.getAttribute("class") ?? "").trim().split(/\s+/).filter(Boolean).slice(0, 4).join(".");
      return `<${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}${cls ? `.${cls}` : ""}>`;
    };
    const clipped = (el: Element): boolean => {
      for (let a = el.parentElement; a !== null && a !== document.body; a = a.parentElement) {
        if (getComputedStyle(a).overflowX !== "visible") return true;
      }
      return false;
    };
    const past = Array.from(document.body.querySelectorAll("*")).filter((el) => {
      const r = el.getBoundingClientRect();
      return (r.width > 0 || r.height > 0) && r.right > width && !clipped(el);
    });
    // The outermost offenders only: a wide element's children are past the edge because it is.
    const outermost = past.filter((el) => el.parentElement === null || !past.includes(el.parentElement));
    return {
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      past: outermost.map((el) => `${describe(el)} right ${Math.round(el.getBoundingClientRect().right)}`),
    };
  }, PHONE_WIDTH);

  expect.soft(measured.innerWidth, `${label}: innerWidth ${measured.innerWidth}, want ${PHONE_WIDTH}`).toBe(PHONE_WIDTH);
  expect
    .soft(measured.scrollWidth, `${label}: scrollWidth ${measured.scrollWidth}, want ${PHONE_WIDTH}`)
    .toBe(PHONE_WIDTH);
  expect.soft(measured.past, `${label}: past ${PHONE_WIDTH}`).toEqual([]);
}

/** axe with its default rules, colour contrast included; any violation fails. */
async function checkAxe(page: Page, label: string): Promise<void> {
  const results = await new AxeBuilder({ page }).analyze();
  const ran = [...results.passes, ...results.violations, ...results.incomplete].map((r) => r.id);
  expect.soft(ran, `${label}: axe ran color-contrast`).toContain("color-contrast");
  const violations = results.violations.map(
    (v) =>
      `${v.id} (${v.impact}) x${v.nodes.length}: ${v.nodes
        .slice(0, 3)
        .map((n) => n.target.join(" "))
        .join(" | ")}`,
  );
  expect.soft(violations, `${label}: axe violations`).toEqual([]);
}

/**
 * What is wrong with the code blocks in "How it works" at this width (site
 * polish 2026-11-10, #7):
 *   - a block whose content is wider than its box. Step 2's block scrolled
 *     sideways, showing 295 of its 823 px on a phone and 311 at 1440 px, with
 *     no scrollbar on macOS or iOS, so its lines read as cut off mid-word;
 *   - a line that wraps anywhere but at a space or right after a `(`. Wrapped
 *     with nothing more, the block broke 'user-123' after its `-` at 360 px,
 *     the package name at 375 px, and a string at 320 px. The one other
 *     place is right after a `/` of a URL past its `//`: step 1's clone URL
 *     (site design 2026-11-11) is 44 characters, wider than a phone's line,
 *     and may break after `github.com/` or `Controlvar/` (CodeLine.tsx).
 * Each character's line is read from its own rectangle (a Range over it).
 */
async function codeProblems(page: Page): Promise<string[]> {
  return page
    .locator('section[aria-labelledby="how-it-works-heading"] pre')
    .evaluateAll((pres) =>
      pres.flatMap((pre) => {
        const name = pre.getAttribute("aria-label");
        const problems: string[] = [];
        if (pre.scrollWidth > pre.clientWidth) {
          problems.push(`${name}: ${pre.scrollWidth} px of content in a ${pre.clientWidth} px box`);
        }
        const range = document.createRange();
        const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT);
        let line = "";
        // The token so far: the text since the last space or newline, across visual line breaks.
        let token = "";
        let top: number | null = null;
        let prev = "";
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          const text = node.textContent ?? "";
          for (let i = 0; i < text.length; i += 1) {
            const ch = text[i];
            if (ch === "\n") {
              line = "";
              token = "";
              top = null;
              prev = "";
              continue;
            }
            range.setStart(node, i);
            range.setEnd(node, i + 1);
            const rect = range.getClientRects()[0];
            if (rect && top !== null && rect.top > top + 2) {
              const afterUrlSlash = prev === "/" && token.startsWith("https://") && token.length > "https://".length;
              if (prev !== " " && prev !== "(" && ch !== " " && !afterUrlSlash) {
                problems.push(`${name}: a line breaks inside "${line.slice(-12)}${text.slice(i, i + 8)}"`);
              }
              line = "";
            }
            if (rect) top = rect.top;
            line += ch;
            token = ch === " " ? "" : token + ch;
            prev = ch;
          }
        }
        return problems;
      }),
    );
}

/** Narrow phones as well as the project's 390 px: a 320 px phone, a 360 px Android, a 375 px iPhone. */
const CODE_WIDTHS = [320, 360, 375, PHONE_WIDTH] as const;

type Stop = { end: true } | { end: false; repeat: boolean; what: string; visible: boolean; style: string };

/**
 * Tab through the whole page. Each element's first Tab stop must draw an
 * outline or a box-shadow ring it does not draw unfocused. A <video>'s later
 * stops are the browser's own player controls, inside the element: the same
 * element, so they are not checked again.
 */
async function checkFocus(page: Page, label: string): Promise<void> {
  // Each element's outline and box-shadow with nothing focused, to tell a focus style from a permanent one.
  await page.evaluate(() => {
    (document.activeElement as HTMLElement | null)?.blur?.();
    const before = new WeakMap<Element, { outline: string; shadow: string }>();
    Array.from(document.body.querySelectorAll("*")).forEach((el) => {
      const cs = getComputedStyle(el);
      before.set(el, {
        outline: `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor} ${cs.outlineOffset}`,
        shadow: cs.boxShadow,
      });
    });
    (window as unknown as { __siteFocus: unknown }).__siteFocus = { before, seen: new WeakSet<Element>() };
  });

  const step = async (): Promise<Stop> => {
    await page.keyboard.press("Tab");
    return page.evaluate((): Stop => {
      const el = document.activeElement;
      if (el === null || el === document.body || el === document.documentElement) return { end: true };
      const state = (window as unknown as {
        __siteFocus: { before: WeakMap<Element, { outline: string; shadow: string }>; seen: WeakSet<Element> };
      }).__siteFocus;
      const text = (el.getAttribute("aria-label") ?? el.textContent ?? "").trim().replace(/\s+/g, " ").slice(0, 40);
      const what = `<${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}> "${text}"`;
      if (state.seen.has(el)) return { end: false, repeat: true, what, visible: true, style: "" };
      state.seen.add(el);

      const alpha = (color: string): number => {
        if (color === "transparent") return 0;
        const m = /rgba?\(([^)]*)\)/.exec(color);
        if (m === null) return 1;
        const parts = m[1].split(/[\s,/]+/).filter(Boolean);
        return parts.length >= 4 ? parseFloat(parts[3]) : 1;
      };
      // "rgb(…) 0px 0px 0px 2px, rgba(…) …": split on the commas outside parentheses.
      const ringVisible = (shadow: string): boolean =>
        shadow !== "none" &&
        shadow.split(/,(?![^(]*\))/).some((one) => {
          const color = /rgba?\([^)]*\)/.exec(one);
          const lengths = one.replace(/rgba?\([^)]*\)/, "").match(/-?[\d.]+px/g) ?? [];
          return (color === null || alpha(color[0]) > 0) && lengths.some((l) => parseFloat(l) !== 0);
        });

      const cs = getComputedStyle(el);
      const outline = `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor} ${cs.outlineOffset}`;
      const was = state.before.get(el) ?? { outline: "none", shadow: "none" };
      const outlineShown =
        cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0 && alpha(cs.outlineColor) > 0 && outline !== was.outline;
      const ringShown = ringVisible(cs.boxShadow) && cs.boxShadow !== was.shadow;
      return {
        end: false,
        repeat: false,
        what,
        visible: outlineShown || ringShown,
        style: `outline ${outline}; box-shadow ${cs.boxShadow}`,
      };
    });
  };

  // Tab to the end of the page first, so the walk below starts at the top
  // whatever had focus (the menu button, after it opened the menu).
  let n = 0;
  while (!(await step()).end) {
    n += 1;
    if (n > MAX_TAB_STOPS) throw new Error(`${label}: Tab never reached the end of the page (${n} stops): a focus trap`);
  }
  await page.evaluate(() => {
    (window as unknown as { __siteFocus: { seen: WeakSet<Element> } }).__siteFocus.seen = new WeakSet<Element>();
  });

  const unfocused: string[] = [];
  let reached = 0;
  for (let stop = await step(); !stop.end; stop = await step()) {
    reached += 1;
    if (reached > MAX_TAB_STOPS) throw new Error(`${label}: more than ${MAX_TAB_STOPS} Tab stops: a focus trap`);
    if (!stop.repeat && !stop.visible) unfocused.push(`${stop.what} focused with no outline (${stop.style})`);
  }

  const unreached = await page.evaluate(() => {
    const seen = (window as unknown as { __siteFocus: { seen: WeakSet<Element> } }).__siteFocus.seen;
    const selector =
      "a[href], area[href], button, input, select, textarea, summary, iframe, [tabindex], [contenteditable], video[controls], audio[controls]";
    return Array.from(document.querySelectorAll<HTMLElement>(selector))
      .filter((el) => el.tabIndex >= 0 && !(el as HTMLButtonElement).disabled)
      .filter((el) => el.checkVisibility({ checkVisibilityCSS: true }))
      .filter((el) => !seen.has(el))
      .map((el) => `<${el.tagName.toLowerCase()}> "${(el.textContent ?? "").trim().slice(0, 40)}" not reached by Tab`);
  });

  expect.soft(reached, `${label}: Tab reached nothing`).toBeGreaterThan(0);
  expect.soft(unfocused, `${label}: focus visible`).toEqual([]);
  expect.soft(unreached, `${label}: reachable by Tab`).toEqual([]);
}

test.describe("the public site", () => {
  // The export under test must be the marketing build: the platform export
  // has the same homepage and hub, so every check below could pass on the
  // wrong one. Only the marketing build has the walkthroughs page.
  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    try {
      await page.goto(`${BASE_URL}${VIDEOS_PAGE}`);
      const videos = await page.locator("video").count();
      expect(
        videos,
        `not the marketing build: ${videos} <video> on ${VIDEOS_PAGE}, want at least ${MIN_VIDEOS}`,
      ).toBeGreaterThanOrEqual(MIN_VIDEOS);
    } finally {
      await page.close();
    }
  });

  test("the homepage plays nothing, and each teaser card opens its row of /walkthroughs", async ({ page }) => {
    await open(page, "/");
    expect(await page.locator("video").count(), "<video> on /").toBe(0);
    const cards = await page.locator('a[href^="/walkthroughs#"]').evaluateAll((as) => as.map((a) => a.getAttribute("href") ?? ""));
    expect(cards.length, "teaser cards").toBeGreaterThan(0);
    for (const href of cards) {
      await page.goto(href);
      const id = href.slice("/walkthroughs#".length);
      await expect(page.locator(`li#${id} video`), href).toHaveCount(1);
    }
  });

  for (const path of PAGES) {
    test(`${path} at ${PHONE_WIDTH} px, menu closed and open`, async ({ page }) => {
      await open(page, path);

      await checkPhoneWidth(page, `${path} 390 closed`);
      await checkAxe(page, `${path} 390 closed`);
      await checkFocus(page, `${path} 390 closed`);

      await page.getByTestId("site-menu-button").click();
      await expect(page.getByTestId("site-menu")).toBeVisible();

      await checkPhoneWidth(page, `${path} 390 open`);
      await checkAxe(page, `${path} 390 open`);
      await checkFocus(page, `${path} 390 open`);
    });
  }

  test(`How it works: every code block fits its box, and breaks only at a space, after a "(" or after a URL's "/", at ${CODE_WIDTHS.join(", ")} px`, async ({
    page,
  }) => {
    await open(page, "/");
    const problems: string[] = [];
    for (const width of CODE_WIDTHS) {
      await page.setViewportSize({ width, height: 844 });
      problems.push(...(await codeProblems(page)).map((p) => `${width} px: ${p}`));
    }
    expect(problems).toEqual([]);
  });

  test("/docs/ is the docs hub, and a missing path is the not-found page", async ({ page }) => {
    const hub = await page.goto("/docs/");
    expect(hub?.status(), "/docs/").toBe(200);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Controlvar Documentation");

    const missing = await page.goto("/no-such-page");
    expect(missing?.status(), "/no-such-page").toBe(404);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Page not found");
  });

  test("/books/ and /walkthroughs/ are their pages", async ({ page }) => {
    for (const [path, h1] of [
      ["/books/", "The Controlvar books"],
      ["/walkthroughs/", "Walkthroughs"],
    ] as const) {
      const answer = await page.goto(path);
      expect(answer?.status(), path).toBe(200);
      await expect(page.getByRole("heading", { level: 1 }), path).toHaveText(h1);
    }
  });

  test.describe(`at ${DESKTOP.width} px`, () => {
    test.use({
      viewport: DESKTOP,
      isMobile: false,
      hasTouch: false,
      deviceScaleFactor: 1,
      userAgent: devices["Desktop Chrome"].userAgent,
    });

    for (const path of PAGES) {
      test(`${path} at ${DESKTOP.width} px`, async ({ page }) => {
        await open(page, path);
        const width = await page.evaluate(() => window.innerWidth);
        expect(width, `${path}: innerWidth ${width}, want ${DESKTOP.width}`).toBe(DESKTOP.width);

        await checkAxe(page, `${path} ${DESKTOP.width}`);
        await checkFocus(page, `${path} ${DESKTOP.width}`);
      });
    }

    test(`How it works: every code block fits its box, and breaks only at a space, after a "(" or after a URL's "/", at ${DESKTOP.width} px`, async ({
      page,
    }) => {
      await open(page, "/");
      expect(await codeProblems(page), `${DESKTOP.width} px`).toEqual([]);
    });

    // Hero B (site design 2026-11-11): the text on the left, the screenshot
    // on the right, its top above the headline's bottom, and no code box.
    test(`the hero's screenshot sits beside the headline, with no code in the hero, at ${DESKTOP.width} px`, async ({ page }) => {
      await open(page, "/");
      const hero = page.locator("h1").locator("xpath=ancestor::section[1]");
      expect(await hero.locator("pre, code").count(), "code in the hero").toBe(0);
      const h1 = await page.locator("h1").boundingBox();
      const img = await hero.locator("img").boundingBox();
      expect(h1 && img, "the h1 and the screenshot have boxes").toBeTruthy();
      if (!h1 || !img) return;
      expect(img.x, `screenshot left ${img.x}, headline right ${h1.x + h1.width}`).toBeGreaterThanOrEqual(h1.x + h1.width);
      expect(img.y, `screenshot top ${img.y}, headline bottom ${h1.y + h1.height}`).toBeLessThan(h1.y + h1.height);
    });

    // The hero is the power calculator's screenshot since 2026-11-10, 1300 px
    // wide, taken at a device scale of 2 (Screenshot.tsx CAPTURE_SCALE): wider
    // than 650 CSS px it would be enlarged on a 2x display.
    test(`the hero's screenshot is not enlarged at ${DESKTOP.width} px`, async ({ page }) => {
      await open(page, "/");
      const hero = await page.locator("h1").locator("xpath=ancestor::section[1]").locator("img").evaluate((img: HTMLImageElement) => ({
        src: img.getAttribute("src"),
        natural: img.naturalWidth,
        shown: img.getBoundingClientRect().width,
      }));
      expect(hero.src).toMatch(/^\/screens\/power-calculator-[0-9a-f]{12}\.webp$/);
      expect(hero.natural).toBeGreaterThan(0);
      expect(hero.shown, `shown ${hero.shown} CSS px of a ${hero.natural} px image`).toBeLessThanOrEqual(hero.natural / 2);
    });
  });
});
