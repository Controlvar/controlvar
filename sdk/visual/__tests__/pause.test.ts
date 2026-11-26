/**
 * The 5-minute pause (controlvar_backoff) in the built classic script, in
 * jsdom: a call the API refuses, or that fails while the page is open,
 * silences the tab; a call the page's own navigation cuts off (it fails after
 * pagehide, as Chromium rejects a call cut off by the page going away) does
 * not, and the next page asks as usual. The browser test is
 * frontend/tests/e2e/runtime/pause.runtime.spec.ts.
 */
import { runClassic, type Answer, type ClassicPage, type FetchCall } from './classic';

const TAG = { 'data-api-url': 'https://cv.example', 'data-api-key': 'test-website-key' };
const PAGE = 'https://shop.example/a/b';
const TOKEN = 'p'.repeat(43);
const VISUAL = '/api/v1/tracking/visual';
const PREVIEW = '/api/v1/tracking/visual/preview';
const PAGES = { host: 'shop.example', rules: [{ match: 'prefix', path: '/' }], max_age: 60 };
const FIVE_MINUTES = 5 * 60 * 1000;

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
const paths = (calls: FetchCall[]) => calls.map((call) => new URL(call.url).pathname);

interface Held {
  answer(given: Answer): void;
  fail(): void;
}

/** Holds every call to `path` until the test answers it or makes it fail; any other call gets `others`. */
function holding(path: string, others: (call: FetchCall) => Answer = () => ({ status: 201, body: {} })) {
  const held: Held[] = [];
  const respond = (call: FetchCall): Answer | Promise<Answer> => {
    if (new URL(call.url).pathname !== path) return others(call);
    return new Promise<Answer>((resolve, reject) => held.push({ answer: resolve, fail: () => reject(new Error('cut off')) }));
  };
  return { held, respond };
}

function answering(call: FetchCall): Answer {
  const path = new URL(call.url).pathname;
  if (path === VISUAL) return { status: 200, body: { experiments: [], pages: PAGES } };
  if (path === PREVIEW) {
    return { status: 200, body: { experiment_key: 'headline', variant_name: 'B', changes: { version: 1, changes: [] }, pages: PAGES } };
  }
  return { status: 201, body: {} };
}

/** The next page of the same tab: a new page that starts with this one's sessionStorage. */
function nextPage(previous: ClassicPage, url = PAGE): ClassicPage {
  const kept: [string, string][] = [];
  for (let i = 0; i < previous.window.sessionStorage.length; i += 1) {
    const key = previous.window.sessionStorage.key(i)!;
    kept.push([key, previous.window.sessionStorage.getItem(key)!]);
  }
  return runClassic(url, TAG, (window) => {
    for (const [key, value] of kept) window.sessionStorage.setItem(key, value);
  }, answering);
}

const leave = (p: ClassicPage) => p.window.dispatchEvent(new p.window.Event('pagehide'));
/** How long the tab's pause has left, in ms; null when there is none. */
function pausedFor(p: ClassicPage): number | null {
  const until = p.window.sessionStorage.getItem('controlvar_backoff');
  return until === null ? null : Number(until) - Date.now();
}
const warnings = (p: ClassicPage) => p.console.filter((line) => line.startsWith('warn:'));

describe('regression #2173: a call the page cuts off by going away starts no pause', () => {
  test('the call for the page, cut off after pagehide: no controlvar_backoff, and the next page asks', async () => {
    const { held, respond } = holding(VISUAL);
    const p = runClassic(PAGE, TAG, () => undefined, respond);
    await settle();
    expect(paths(p.fetches)).toEqual([VISUAL]);
    leave(p);
    held[0].fail();
    await settle();
    expect(p.window.sessionStorage.getItem('controlvar_backoff')).toBeNull();
    expect(warnings(p)).toEqual([]);
    const next = nextPage(p);
    await settle();
    expect(paths(next.fetches)).toEqual([VISUAL]);
    p.close();
    next.close();
  });

  test("a preview link's call, cut off after pagehide: no controlvar_backoff, and the next page asks for the preview again", async () => {
    const { held, respond } = holding(PREVIEW);
    const p = runClassic(`${PAGE}#controlvar-preview=${TOKEN}`, TAG, () => undefined, respond);
    await settle();
    expect(paths(p.fetches)).toEqual([PREVIEW]);
    leave(p);
    held[0].fail();
    await settle();
    expect(p.window.sessionStorage.getItem('controlvar_backoff')).toBeNull();
    expect(p.window.sessionStorage.getItem('controlvar_preview')).toBe(TOKEN);
    const next = nextPage(p, 'https://shop.example/a/c');
    await settle();
    expect(paths(next.fetches)).toEqual([PREVIEW]);
    p.close();
    next.close();
  });

  test('a call that fails while the page is open still pauses the tab for 5 minutes, and the next page makes no call', async () => {
    const { held, respond } = holding(VISUAL);
    const p = runClassic(PAGE, TAG, () => undefined, respond);
    await settle();
    held[0].fail();
    await settle();
    expect(pausedFor(p)).toBeGreaterThan(FIVE_MINUTES - 1000);
    expect(pausedFor(p)).toBeLessThanOrEqual(FIVE_MINUTES);
    const next = nextPage(p);
    await settle();
    expect(next.fetches).toEqual([]);
    p.close();
    next.close();
  });

  test.each([401, 403, 503])('a %s answer that arrives after pagehide still pauses the tab for 5 minutes', async (status) => {
    const { held, respond } = holding(VISUAL);
    const p = runClassic(PAGE, TAG, () => undefined, respond);
    await settle();
    leave(p);
    held[0].answer({ status, body: { detail: 'no' } });
    await settle();
    expect(pausedFor(p)).toBeGreaterThan(FIVE_MINUTES - 1000);
    expect(pausedFor(p)).toBeLessThanOrEqual(FIVE_MINUTES);
    expect(warnings(p)).toHaveLength(status === 503 ? 0 : 1);
    p.close();
  });

  test('a page shown again after pagehide pauses the tab when a later call fails', async () => {
    const { held, respond } = holding(VISUAL);
    const p = runClassic(PAGE, TAG, () => undefined, respond);
    await settle();
    held[0].answer({ status: 200, body: { experiments: [], pages: PAGES } });
    await settle();
    leave(p);
    p.window.dispatchEvent(new p.window.Event('pageshow'));
    p.window.history.pushState(null, '', '/a/c');
    await settle();
    expect(paths(p.fetches)).toEqual([VISUAL, VISUAL]);
    held[1].fail();
    await settle();
    expect(pausedFor(p)).toBeGreaterThan(FIVE_MINUTES - 1000);
    p.close();
  });
});
