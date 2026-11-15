import { randomUUID } from "node:crypto";
import { createServer, type Server } from "node:http";
import type { Page } from "@playwright/test";
import { BASE_URL } from "./env";
import { expect, test } from "./fixtures/launch.fixture";

/**
 * V112 (#1545): the `launch` project runs the browser users run.
 *
 * The command-line half is fixtures/launch.fixture.ts, which runs before this
 * file's first test. This is the canary pair, which shows that third-party
 * storage partitioning is on:
 *
 *   - A, the base URL (http://localhost:3100 in CI), loaded at the top level,
 *     writes a localStorage canary.
 *   - The same page then frames A from a page on port F (V112_FRAME_PORT,
 *     4100 by default), served by a small server this file starts. Under
 *     http://localhost:F/ (the same site as A) the frame must read the
 *     canary; under http://127.0.0.1:F/ (another site) it must read null,
 *     because a frame under another site gets storage of its own.
 *
 * Playwright's default launch disables partitioning, and the cross-site frame
 * then reads the canary. All three steps share one page, so one browser
 * context: storage is per context, and a fresh context would read null under
 * any launch. The frame's origin is checked in both arms, so an error page
 * in the frame cannot pass the cross-site arm. The framed document is
 * whatever A serves at `/` (the marketing export in CI); its content is not
 * looked at.
 */

const FRAME_PORT = Number(process.env.V112_FRAME_PORT ?? 4100);
const SAME_SITE = `http://localhost:${FRAME_PORT}/`;
const CROSS_SITE = `http://127.0.0.1:${FRAME_PORT}/`;
const ORIGIN_A = new URL(BASE_URL).origin;
const KEY = "controlvar-v112-canary";

let server: Server | undefined;

test.beforeAll(async () => {
  const embedder = createServer((req, res) => {
    if (req.url !== "/") {
      res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
      res.end("not found");
      return;
    }
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    res.end(`<!doctype html><title>V112 embedder</title><iframe src="${BASE_URL}/"></iframe>`);
  });
  await new Promise<void>((resolve, reject) => {
    embedder.once("error", reject);
    embedder.listen(FRAME_PORT, "127.0.0.1", () => resolve());
  });
  server = embedder;
});

test.afterAll(async () => {
  // Undefined when the fixture failed before beforeAll ran, or listen failed.
  const embedder = server;
  if (embedder === undefined) return;
  server = undefined;
  await new Promise<void>((resolve) => embedder.close(() => resolve()));
});

/** Open the embedder page and read the canary in A's frame once that frame has loaded. */
async function canaryInFrame(page: Page, embedder: string): Promise<string | null> {
  await page.goto(embedder, { waitUntil: "load" });
  const frame = page.frames().find((candidate) => candidate !== page.mainFrame());
  if (frame === undefined) throw new Error(`${embedder} has no frame`);
  await frame.waitForLoadState("load");
  const origin = await frame.evaluate(() => location.origin);
  expect(origin, `the frame under ${embedder} is not A`).toBe(ORIGIN_A);
  return frame.evaluate((key) => localStorage.getItem(key), KEY);
}

test("a frame of A reads A's storage under the same site and not under another site", async ({
  page,
}) => {
  const canary = `canary-${randomUUID()}`;
  await page.goto(`${BASE_URL}/`, { waitUntil: "load" });
  await page.evaluate(([key, value]) => localStorage.setItem(key, value), [KEY, canary]);
  expect(await page.evaluate((key) => localStorage.getItem(key), KEY), "A did not keep the canary").toBe(
    canary,
  );

  const sameSite = await canaryInFrame(page, SAME_SITE);
  expect(sameSite, "same-site frame lost the canary").toBe(canary);

  const crossSite = await canaryInFrame(page, CROSS_SITE);
  expect(crossSite, `cross-site frame saw ${crossSite}`).toBeNull();
});
