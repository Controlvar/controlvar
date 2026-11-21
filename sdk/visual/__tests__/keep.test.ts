/**
 * Keeping the changes, in the built classic script in jsdom: re-applied when
 * the page replaces or rewrites them until the bound, at most 20 times in a
 * second per change; reported once as replaced after the bound when the page
 * goes away; and a single-page app's path followed. The browser tests
 * (frontend/tests/e2e/runtime) cover the timing and the frameworks.
 */
import { runClassic, type Answer, type FetchCall } from './classic';

const TAG = { 'data-api-url': 'https://cv.example', 'data-api-key': 'test-website-key' };
const PAGE = 'https://shop.example/a';
const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
const body = (call: FetchCall) => JSON.parse(String(call.init.body));
const path = (call: FetchCall) => new URL(call.url).pathname;

function answering(changes: unknown[], rules = [{ match: 'prefix', path: '/' }]) {
  return (call: FetchCall): Answer | undefined => {
    if (path(call) === '/api/v1/tracking/visual') {
      const experiments = body(call).path === '/a'
        ? [{ experiment_key: 'exp', variant_id: 'v1', variant_name: 'B', assigned: true, reason: 'assigned', changes: { version: 1, changes }, goals: [] }]
        : [];
      return { status: 200, body: { experiments, pages: { host: 'shop.example', rules, max_age: 60 } } };
    }
    return { status: 201, body: {} };
  };
}

function page(changes: unknown[], setup: (window: Window & typeof globalThis) => void = () => undefined) {
  return runClassic(PAGE, TAG, (window) => {
    window.document.body.innerHTML = '<h1 id="t">Original</h1><p id="p">Text</p>';
    setup(window);
  }, answering(changes));
}

const reports = (calls: FetchCall[]) => calls.filter((call) => path(call) === '/api/v1/tracking/errors');
const visualCalls = (calls: FetchCall[]) => calls.filter((call) => path(call) === '/api/v1/tracking/visual');

describe('until the bound', () => {
  test('a replaced element gets the change again, and so does a change written over', async () => {
    const p = page([{ op: 'set_text', selector: '#t', text: 'Changed' }, { op: 'set_attribute', selector: '#p', name: 'title', value: 'B' }]);
    await settle();
    const doc = p.window.document;
    expect(doc.getElementById('t')!.textContent).toBe('Changed');
    const fresh = doc.createElement('h1');
    fresh.id = 't';
    fresh.textContent = 'Original';
    doc.getElementById('t')!.replaceWith(fresh);
    doc.getElementById('p')!.setAttribute('title', 'A');
    await settle();
    expect(doc.getElementById('t')!.textContent).toBe('Changed');
    expect(doc.getElementById('p')!.getAttribute('title')).toBe('B');
    expect(reports(p.fetches)).toEqual([]);
    p.close();
  });

  test('a page that keeps undoing a change stops it after 20 re-applies in a second, with one report', async () => {
    let resets = 0;
    const p = page([{ op: 'set_text', selector: '#t', text: 'Changed' }, { op: 'set_text', selector: '#p', text: 'Kept' }], (window) => {
      new window.MutationObserver(() => {
        const t = window.document.getElementById('t')!;
        if (t.textContent !== 'Original') {
          resets += 1;
          t.textContent = 'Original';
        }
      }).observe(window.document.body, { childList: true, subtree: true, characterData: true });
    });
    await settle();
    expect(resets).toBe(21);
    expect(reports(p.fetches).map((call) => body(call).metadata)).toEqual([{ index: 0, reason: 'reapply_limit' }]);
    expect(p.window.document.getElementById('p')!.textContent).toBe('Kept');
    p.close();
  });
});

describe('after the bound', () => {
  const late = (window: Window & typeof globalThis) => {
    window.performance.now = () => 20000;
  };

  test('a replaced element is reported once as the page goes away, with keepalive, and is not changed again', async () => {
    const p = page([{ op: 'set_text', selector: '#t', text: 'Changed' }], late);
    await settle();
    const doc = p.window.document;
    const fresh = doc.createElement('h1');
    fresh.id = 't';
    fresh.textContent = 'Original';
    doc.getElementById('t')!.replaceWith(fresh);
    await settle();
    expect(doc.getElementById('t')!.textContent).toBe('Original');
    expect(reports(p.fetches)).toEqual([]);
    p.window.dispatchEvent(new p.window.Event('pagehide'));
    p.window.dispatchEvent(new p.window.Event('pagehide'));
    const sent = reports(p.fetches);
    expect(sent.map((call) => body(call).metadata)).toEqual([{ index: 0, reason: 'replaced_after_bound' }]);
    expect(sent[0].init.keepalive).toBe(true);
    p.close();
  });

  test('a page that keeps its element sends nothing as it goes away', async () => {
    const p = page([{ op: 'set_text', selector: '#t', text: 'Changed' }], late);
    await settle();
    p.window.dispatchEvent(new p.window.Event('pagehide'));
    expect(reports(p.fetches)).toEqual([]);
    p.close();
  });
});

describe('a single-page app', () => {
  test('a new path undoes the changes and asks once; a change after # alone asks nothing', async () => {
    const p = page([{ op: 'set_text', selector: '#t', text: 'Changed' }]);
    await settle();
    const { document: doc, history } = p.window;
    expect(doc.getElementById('t')!.textContent).toBe('Changed');
    history.pushState(null, '', '/b');
    await settle();
    expect(doc.getElementById('t')!.textContent).toBe('Original');
    expect(visualCalls(p.fetches).map((call) => body(call).path)).toEqual(['/a', '/b']);
    history.pushState(null, '', '/b#x');
    history.replaceState(null, '', '/b?q=1');
    await settle();
    expect(visualCalls(p.fetches)).toHaveLength(2);
    history.pushState(null, '', '/a');
    await settle();
    expect(doc.getElementById('t')!.textContent).toBe('Changed');
    expect(visualCalls(p.fetches).map((call) => body(call).path)).toEqual(['/a', '/b', '/a']);
    p.close();
  });

  test("calls the page's own pushState first, and returns what it returns", async () => {
    const p = page([], (window) => {
      const own = window.history.pushState;
      window.history.pushState = function (...args: Parameters<History['pushState']>) {
        (window as unknown as { order: string[] }).order = ['own'];
        return own.apply(this, args);
      };
    });
    await settle();
    expect(p.window.history.pushState(null, '', '/c')).toBeUndefined();
    expect((p.window as unknown as { order: string[] }).order).toEqual(['own']);
    expect(p.window.location.pathname).toBe('/c');
    p.close();
  });
});
