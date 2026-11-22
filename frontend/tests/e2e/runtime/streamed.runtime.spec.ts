import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { expect, test } from "./runtime.fixture";

/**
 * A text change on an element the browser receives in two pieces (#1545):
 * the script waits until the element is closed (a node follows it that it
 * does not contain, or DOMContentLoaded), so the text equals the change
 * exactly, and no frame after the page shows holds a partial value. These
 * pages are streamed by a real server, which `page.route` cannot do; it
 * listens on VISUAL_STREAM_PORT (4500 by default) on 127.0.0.1.
 */

const PORT = Number(process.env.VISUAL_STREAM_PORT ?? 4500);
const ORIGIN = `http://127.0.0.1:${PORT}`;
const CHANGED = "CHANGED2";

/** Each page: what is sent before the pause, and after it. */
const PAGES: Record<string, [string, string]> = {
  "/split-text": ['<p id="t">Orig', 'inal tail</p><p id="after">after</p>'],
  "/split-inside-child": ['<p id="t">Hel<b>lo', ' wor</b>ld</p><p id="after">after</p>'],
};

let server: Server;
let answered: () => void = () => undefined;
let answer: Promise<void> = Promise.resolve();
let script = "";

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
  const path = new URL(request.url ?? "/", ORIGIN).pathname;
  if (path === "/controlvar-visual.js") {
    response.writeHead(200, { "Content-Type": "text/javascript" });
    response.end(script);
    return;
  }
  if (path === "/api/v1/tracking/visual") {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(
      JSON.stringify({
        experiments: [
          {
            experiment_key: "split",
            variant_id: "v1",
            variant_name: "B",
            assigned: true,
            reason: "assigned",
            changes: { version: 1, changes: [{ op: "set_text", selector: "#t", text: CHANGED }] },
            goals: [],
          },
        ],
        pages: { host: "127.0.0.1", rules: [{ match: "prefix", path: "/" }], max_age: 60 },
      }),
    );
    answered();
    return;
  }
  const parts = PAGES[path];
  if (!parts || request.method !== "GET") {
    response.writeHead(404);
    response.end();
    return;
  }
  response.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
  response.write(
    `<!doctype html><html><head><meta charset="utf-8"><title>streamed</title>` +
      `<script src="/controlvar-visual.js" data-api-url="${ORIGIN}" data-api-key="test-website-key" data-timeout-ms="4000"></script>` +
      `</head><body><div>${"padding ".repeat(300)}</div>`,
  );
  // The answer is in the page before the element starts arriving.
  await answer;
  await wait(200);
  response.write(parts[0]);
  await wait(300);
  response.end(`${parts[1]}</body></html>`);
}

test.beforeAll(async ({ visualPackage }) => {
  script = visualPackage.classicScript;
  server = createServer((request, response) => {
    handle(request, response).catch(() => response.destroy());
  });
  await new Promise<void>((resolve) => server.listen(PORT, "127.0.0.1", resolve));
  expect((server.address() as AddressInfo).port).toBe(PORT);
});

test.afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

for (const [path, name] of [
  ["/split-text", "a text split across two pieces"],
  ["/split-inside-child", "a text split inside an inline child"],
] as const) {
  test(`${name} gets exactly the change, and no frame shows a part of it`, async ({ page }) => {
    answer = new Promise<void>((resolve) => (answered = resolve));
    await page.addInitScript(() => {
      const frames: (string | null)[] = [];
      (window as unknown as { __frames: typeof frames }).__frames = frames;
      const sample = () => {
        const element = document.getElementById("t");
        if (getComputedStyle(document.documentElement).visibility !== "hidden") frames.push(element ? element.textContent : null);
        if (frames.length < 500) requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
      // Every write of the change's text, through either way the script
      // writes a text: a text node's nodeValue, or a new text node. Written
      // once the element is closed, it is written once; written on first
      // sight, the rest of the element arrives after it and the script has
      // to write it again.
      const counter = window as unknown as { __writes: number };
      counter.__writes = 0;
      const nodeValue = Object.getOwnPropertyDescriptor(Node.prototype, "nodeValue")!;
      Object.defineProperty(Node.prototype, "nodeValue", {
        ...nodeValue,
        set(value: string | null) {
          if (value === "CHANGED2") counter.__writes += 1;
          nodeValue.set!.call(this, value);
        },
      });
      const createTextNode = Document.prototype.createTextNode;
      Document.prototype.createTextNode = function (data: string) {
        if (data === "CHANGED2") counter.__writes += 1;
        return createTextNode.call(this, data);
      };
    });
    await page.goto(`${ORIGIN}${path}`);
    await page.waitForLoadState("load");
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    expect(await page.locator("#t").textContent()).toBe(CHANGED);
    expect(await page.evaluate(() => (window as unknown as { __writes: number }).__writes), "writes of the change").toBe(1);
    const frames = await page.evaluate(() => (window as unknown as { __frames: (string | null)[] }).__frames);
    expect(frames.length, "frames sampled after the page showed").toBeGreaterThan(0);
    expect(frames.filter((text) => text !== CHANGED), "frames after the page showed with another value").toEqual([]);
  });
}
