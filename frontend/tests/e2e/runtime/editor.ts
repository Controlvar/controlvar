import { randomBytes } from "node:crypto";
import type { Frame, Page } from "@playwright/test";
import { expect, type VisualPackage } from "./runtime.fixture";

/**
 * The page-runtime specs' sites (#1545): a customer site, the editor's panel
 * on the dashboard's origin, and a second origin. Every request to them is
 * answered by `page.route`, so nothing listens on these ports and they are
 * fixed (the App Router fixture is built with them).
 */
export const SITE = "http://localhost:4100";
export const EDITOR = "http://localhost:4300";
export const SECOND = "http://127.0.0.1:4200";
export const CLAIM_PATH = "/api/v1/visual-editor/tickets/claim";
export const CLAIM_URL = `${SITE}${CLAIM_PATH}`;
export const PANEL_URL = `${EDITOR}/visual-editor/panel`;
/** Another page on the editor's origin, served by the same stub. */
export const OTHER_EDITOR_PAGE = `${EDITOR}/other`;
export const FRAME_SELECTOR = 'iframe[title="Controlvar visual editor"]';
export const API_KEY = "test-website-key";
export const BASE64URL_43 = /^[A-Za-z0-9_-]{43}$/;

/** A fresh one-time link value, as the API mints one: 32 random bytes, base64url. */
export function newTicket(): string {
  return randomBytes(32).toString("base64url");
}

/** The snippet, with the API on the site's own origin unless told otherwise. */
export function snippet(attributes: Record<string, string | true | null> = {}): string {
  const all: Record<string, string | true | null> = {
    "data-api-url": SITE,
    "data-api-key": API_KEY,
    "data-editor-origin": EDITOR,
    ...attributes,
  };
  const rendered = Object.entries(all)
    .filter(([, value]) => value !== null)
    .map(([name, value]) => (value === true ? ` ${name}` : ` ${name}="${value}"`))
    .join("");
  return `<script src="/controlvar-visual.js"${rendered}></script>`;
}

export function html(head: string, body = ""): string {
  return `<!doctype html><html><head><meta charset="utf-8"><title>site</title>${head}</head><body>${body}</body></html>`;
}

/** The panel stub: records every message it gets; says hello when told. */
const PANEL = html(
  `<script>
window.__received = [];
addEventListener("message", function (e) { window.__received.push({ origin: e.origin, data: e.data }); });
window.__hello = function () { parent.postMessage({ cv: 1, type: "hello" }, "*"); };
</script>`,
  "<p>panel</p>",
);

export interface ApiCall {
  url: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

export interface Site {
  /** Every request to SITE/api/, in arrival order. */
  api: ApiCall[];
  /** Request headers of each request for the panel. */
  panelRequests: Record<string, string>[];
  /** Answer the held claim request (holdClaim). */
  releaseClaim(): void;
}

export interface SiteOptions {
  pages: Record<string, string>;
  files?: Record<string, { body: string | Buffer; contentType: string }>;
  /** Hold the claim request until releaseClaim(). */
  holdClaim?: boolean;
  /** Answers every API request except the claim; without it they get 404. */
  answer?: (call: ApiCall) => ApiAnswer | Promise<ApiAnswer>;
  /** Extra response headers for the pages (a Content-Security-Policy, say). */
  pageHeaders?: Record<string, string>;
}

export interface ApiAnswer {
  status: number;
  body?: unknown;
}

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".json": "application/json",
};

export function contentType(path: string): string {
  const dot = path.lastIndexOf(".");
  return CONTENT_TYPES[dot >= 0 ? path.slice(dot) : ""] ?? "application/octet-stream";
}

export async function routeSite(page: Page, visualPackage: VisualPackage, options: SiteOptions): Promise<Site> {
  const api: ApiCall[] = [];
  const panelRequests: Record<string, string>[] = [];
  let release: () => void = () => undefined;
  const held = options.holdClaim ? new Promise<void>((resolve) => (release = resolve)) : Promise.resolve();

  await page.route(`${SITE}/**`, async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    if (pathname.startsWith("/api/")) {
      let body: unknown = request.postData();
      try {
        body = request.postDataJSON();
      } catch {
        // not JSON: kept as text
      }
      const call = { url: request.url(), path: pathname, headers: await request.allHeaders(), body };
      api.push(call);
      if (pathname === CLAIM_PATH) {
        await held;
        await route.fulfill({ status: 201, contentType: "application/json", body: "{}" });
      } else if (options.answer) {
        const answer = await options.answer(call);
        await route.fulfill({
          status: answer.status,
          contentType: "application/json",
          headers: { "Access-Control-Allow-Origin": "*" },
          body: JSON.stringify(answer.body ?? {}),
        });
      } else {
        await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
      }
      return;
    }
    if (pathname === "/controlvar-visual.js") {
      await route.fulfill({ contentType: contentType(pathname), body: visualPackage.classicScript });
      return;
    }
    const pageBody = options.pages[pathname];
    if (pageBody !== undefined) {
      await route.fulfill({ contentType: contentType(".html"), headers: options.pageHeaders, body: pageBody });
      return;
    }
    const file = options.files?.[pathname];
    if (file !== undefined) {
      await route.fulfill({ contentType: file.contentType, body: file.body });
      return;
    }
    await route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
  });
  await page.route(`${EDITOR}/**`, async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    if (pathname === "/visual-editor/panel") panelRequests.push(await request.allHeaders());
    if (pathname === "/visual-editor/panel" || pathname === "/other") {
      await route.fulfill({ contentType: contentType(".html"), body: PANEL });
      return;
    }
    await route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
  });
  await page.route(`${SECOND}/**`, async (route) => {
    await route.fulfill({ contentType: contentType(".html"), body: PANEL });
  });
  return { api, panelRequests, releaseClaim: () => release() };
}

/** The frame whose document is at `origin`, once it has loaded. */
export async function frameAt(page: Page, origin: string): Promise<Frame> {
  await expect
    .poll(() => page.frames().some((frame) => frame.url().startsWith(origin)), {
      message: `a frame at ${origin}`,
    })
    .toBe(true);
  const frame = page.frames().find((candidate) => candidate.url().startsWith(origin)) as Frame;
  await frame.waitForLoadState("load");
  return frame;
}

export async function sayHello(frame: Frame): Promise<void> {
  await frame.evaluate(() => (window as unknown as { __hello(): void }).__hello());
}

interface Received {
  origin: string;
  data: { type?: string; claim?: string; marker?: string } | null;
}

export async function received(frame: Frame): Promise<Received[]> {
  return frame.evaluate(() => (window as unknown as { __received: Received[] }).__received);
}

export async function claimsIn(frame: Frame): Promise<Received[]> {
  return (await received(frame)).filter((message) => message.data !== null && message.data.type === "claim");
}

/**
 * Post a marker from the page to the editor frame and wait until the frame
 * has it. Messages from one window to another arrive in the order they were
 * posted, so anything the script posted to the frame before this call has
 * arrived by the time this returns.
 */
export async function flushToFrame(page: Page, frame: Frame): Promise<void> {
  const marker = `marker-${randomBytes(4).toString("hex")}`;
  await page.evaluate(
    ([selector, value]) => {
      const element = document.querySelector(selector) as HTMLIFrameElement;
      const target = (element as unknown as { contentWindow: Window }).contentWindow;
      target.postMessage({ marker: value }, "*");
    },
    [FRAME_SELECTOR, marker] as const,
  );
  await expect
    .poll(async () => (await received(frame)).some((message) => message.data?.marker === marker), {
      message: `the frame got ${marker}`,
    })
    .toBe(true);
}

/** Wait until the page has the claim request's resource timing entry: the fetch has settled. */
export async function claimSettled(page: Page): Promise<void> {
  await page.waitForFunction((url) => performance.getEntriesByName(url).length > 0, CLAIM_URL);
}

/** Every controlvar_ key in the page's localStorage and sessionStorage, as "local:name" or "session:name". */
export async function storageKeys(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const keys: string[] = [];
    for (const [area, storage] of [
      ["local", localStorage],
      ["session", sessionStorage],
    ] as const) {
      for (let i = 0; i < storage.length; i += 1) {
        const key = storage.key(i);
        if (key !== null && key.startsWith("controlvar_")) keys.push(`${area}:${key}`);
      }
    }
    return keys.sort();
  });
}
