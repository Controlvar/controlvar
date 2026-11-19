/**
 * An ordinary page view in the built classic script, in jsdom: the call that
 * asks for the page's changes, the answer applied, the reports, the visitor
 * id, the page rules kept for 60 seconds, the silence after a refused key,
 * and track(). The browser tests (frontend/tests/e2e/runtime) cover the hide,
 * the timing and what the page shows.
 */
import { timeoutFrom } from '../src/config';
import { runClassic, type Answer, type FetchCall } from './classic';

const TAG = { 'data-api-url': 'https://cv.example', 'data-api-key': 'test-website-key' };
const PAGE = 'https://shop.example/a/b?email=x';
const BODY = '<h1 id="t">Original</h1><a id="l" href="/x">Link</a><p class="many">1</p><p class="many">2</p>';

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
const body = (call: FetchCall) => JSON.parse(String(call.init.body));
const paths = (calls: FetchCall[]) => calls.map((call) => new URL(call.url).pathname);

function experiment(key: string, changes: unknown, extra: Record<string, unknown> = {}) {
  return { experiment_key: key, variant_id: 'v1', variant_name: 'B', assigned: true, reason: 'assigned', changes, goals: [], ...extra };
}

function answering(experiments: unknown[], pages: unknown = { host: 'shop.example', rules: [{ match: 'prefix', path: '/' }], max_age: 60 }) {
  return (call: FetchCall): Answer | undefined => {
    const path = new URL(call.url).pathname;
    if (path === '/api/v1/tracking/visual') return { status: 200, body: { experiments, pages } };
    if (path === '/api/v1/tracking/errors') return { status: 201, body: {} };
    return { status: 200, body: {} };
  };
}

function page(respond: (call: FetchCall) => Answer | undefined, attributes: Record<string, string> = {}, setup = (_w: Window) => undefined as void, url = PAGE) {
  return runClassic(url, { ...TAG, ...attributes }, (window) => {
    window.document.body.innerHTML = BODY;
    setup(window);
  }, respond);
}

const reports = (calls: FetchCall[]) => calls.filter((call) => call.url.endsWith('/api/v1/tracking/errors')).map(body);

describe('the call for the page', () => {
  test('sends the visitor, the host and the path, never the query, with the key and no cookies', async () => {
    const p = page(answering([]), { 'data-attributes': '{"plan":"pro"}' });
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual']);
    const sent = body(p.fetches[0]);
    expect(Object.keys(sent).sort()).toEqual(['context', 'format_version', 'host', 'path', 'user_id']);
    expect(sent).toMatchObject({ host: 'shop.example', path: '/a/b', format_version: 1, context: { plan: 'pro' } });
    expect(JSON.stringify(sent)).not.toContain('email');
    expect(p.fetches[0].init).toMatchObject({ method: 'POST', credentials: 'omit', referrerPolicy: 'no-referrer' });
    expect((p.fetches[0].init.headers as Record<string, string>)['X-API-Key']).toBe('test-website-key');
    p.close();
  });

  test('applies the answer and records the assigned experiment for track()', async () => {
    const p = page(answering([experiment('headline', { version: 1, changes: [{ op: 'set_text', selector: '#t', text: 'Changed' }] })]));
    await settle();
    expect(p.window.document.getElementById('t')!.textContent).toBe('Changed');
    expect(Object.keys(JSON.parse(p.window.localStorage.getItem('controlvar_assigned')!))).toEqual(['headline']);
    expect(reports(p.fetches)).toEqual([]);
    p.close();
  });

  test('applies nothing for an experiment the visitor is not assigned to', async () => {
    const changes = { version: 1, changes: [{ op: 'set_text', selector: '#t', text: 'Changed' }] };
    const p = page(answering([experiment('headline', changes, { assigned: false, reason: 'targeting' })]));
    await settle();
    expect(p.window.document.getElementById('t')!.textContent).toBe('Original');
    expect(p.window.localStorage.getItem('controlvar_assigned')).toBeNull();
    p.close();
  });

  test('is not made in a tab whose editor link opened, and before consent', async () => {
    const editor = page(answering([]), { 'data-editor-origin': 'https://dash.example' }, undefined, `${PAGE}#controlvar-editor=${'A'.repeat(43)}`);
    const consent = page(answering([]), { 'data-require-consent': '' });
    await settle();
    expect(paths(editor.fetches)).toEqual(['/api/v1/visual-editor/tickets/claim']);
    expect(consent.fetches).toEqual([]);
    expect(consent.window.document.cookie).toBe('');
    editor.close();
    consent.close();
  });
});

describe('an experiment the script cannot check', () => {
  test('an unknown operation skips the whole experiment, with one report', async () => {
    const changes = { version: 1, changes: [{ op: 'set_text', selector: '#t', text: 'Changed' }, { op: 'set_html', selector: '#l', html: '<b>' }] };
    const p = page(answering([experiment('headline', changes)]));
    await settle();
    expect(p.window.document.getElementById('t')!.textContent).toBe('Original');
    expect(reports(p.fetches)).toEqual([
      {
        error_type: 'visual_change_not_applied',
        experiment_key: 'headline',
        message: 'A visual change was not applied: this version of the script does not know its operation.',
        metadata: { index: null, reason: 'unsupported_operation' },
      },
    ]);
    p.close();
  });

  test('a __proto__ key is refused, and pollutes nothing', async () => {
    const answer = JSON.parse(
      '{"version":1,"changes":[{"op":"hide","selector":"#t","__proto__":{"polluted":true}},{"op":"set_style","selector":"#t","styles":{"__proto__":"x"}}]}',
    );
    const p = page(answering([experiment('headline', answer)]));
    await settle();
    expect(reports(p.fetches).map((report) => report.metadata)).toEqual([{ index: null, reason: 'invalid' }]);
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
    expect((p.window as unknown as { eval(code: string): unknown }).eval('({}).polluted')).toBeUndefined();
    expect(p.window.document.getElementById('t')!.style.display).toBe('');
    p.close();
  });
});

describe('the reports', () => {
  test('carry exactly the four keys, never a feature flag key, and stop at five per page view', async () => {
    const bad = (index: number) => experiment(`exp-${index}`, { version: 2, changes: [] });
    const p = page(answering([1, 2, 3, 4, 5, 6, 7].map(bad)));
    await settle();
    const sent = reports(p.fetches);
    expect(sent).toHaveLength(5);
    for (const report of sent) expect(Object.keys(report).sort()).toEqual(['error_type', 'experiment_key', 'message', 'metadata']);
    expect(JSON.stringify(sent)).not.toContain('feature_flag');
    expect(JSON.stringify(sent)).not.toContain('user_id');
    p.close();
  });

  test('name a selector that matched several at once, and one that matched nothing only at the bound', async () => {
    const changes = { version: 1, changes: [{ op: 'hide', selector: '#missing' }, { op: 'hide', selector: '.many' }, { op: 'hide', selector: 'div[' }] };
    const early = page(answering([experiment('headline', changes)]));
    await settle();
    expect(reports(early.fetches).map((report) => report.metadata)).toEqual([
      { index: 1, reason: 'ambiguous' },
      { index: 2, reason: 'invalid' },
    ]);
    early.close();
    // A page 20 s after its navigation started is past every bound.
    const p = page(answering([experiment('headline', changes)]), {}, (window) => {
      window.performance.now = () => 20000;
    });
    await settle();
    expect(reports(p.fetches).map((report) => report.metadata)).toEqual([
      { index: 1, reason: 'ambiguous' },
      { index: 2, reason: 'invalid' },
      { index: 0, reason: 'not_found' },
    ]);
    expect([...p.window.document.querySelectorAll<HTMLElement>('.many')].map((el) => el.style.display)).toEqual(['', '']);
    p.close();
  });
});

describe('a refused key', () => {
  test.each([401, 403])('after %s nothing more is sent for 5 minutes, and one warning says why', async (status) => {
    const respond = (call: FetchCall): Answer => (call.url.endsWith('/visual') ? { status, body: { detail: 'no' } } : { status: 201 });
    const p = page(respond);
    await settle();
    (p.window as unknown as { controlvar: { visual: { track(e: string): void } } }).controlvar.visual.track('purchase');
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual']);
    expect(p.console.filter((line) => line.startsWith('warn:'))).toHaveLength(1);
    const until = Number(p.window.sessionStorage.getItem('controlvar_backoff'));
    expect(until - Date.now()).toBeGreaterThan(299000);
    expect(until - Date.now()).toBeLessThanOrEqual(300000);
    p.close();
  });

  test('a page whose tab is silent makes no call and is never hidden', async () => {
    const p = page(answering([]), {}, (window) => window.sessionStorage.setItem('controlvar_backoff', String(Date.now() + 60000)));
    await settle();
    expect(p.fetches).toEqual([]);
    expect(p.window.document.documentElement.style.visibility).toBe('');
    p.close();
  });
});

describe('the page rules', () => {
  const kept = (rules: unknown[], until: number) => (window: Window) =>
    window.localStorage.setItem('controlvar_pages', JSON.stringify({ host: 'shop.example', rules, until }));

  test('are kept for at most 60 seconds from the answer', async () => {
    const p = page(answering([], { host: 'Shop.Example', rules: [{ match: 'exact', path: '/c' }], max_age: 600 }));
    await settle();
    const saved = JSON.parse(p.window.localStorage.getItem('controlvar_pages')!);
    expect(saved.host).toBe('shop.example');
    expect(saved.rules).toEqual([{ match: 'exact', path: '/c' }]);
    expect(saved.until - Date.now()).toBeLessThanOrEqual(60000);
    expect(saved.until - Date.now()).toBeGreaterThan(55000);
    p.close();
  });

  test('when fresh and none covers the page: no call, no cookie, no hide', async () => {
    const p = page(answering([]), {}, kept([{ match: 'exact', path: '/c' }], Date.now() + 30000));
    await settle();
    expect(p.fetches).toEqual([]);
    expect(p.window.document.cookie).toBe('');
    expect(p.window.document.documentElement.style.visibility).toBe('');
    p.close();
  });

  test.each([
    ['one covers the page', [{ match: 'prefix', path: '/a' }], 30000],
    ['they are stale', [{ match: 'exact', path: '/c' }], -1],
  ])('when %s: the call is made', async (_name, rules, offset) => {
    const p = page(answering([]), {}, kept(rules, Date.now() + offset));
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual']);
    p.close();
  });
});

describe('the visitor id', () => {
  test('is a new random id in the cookie, for 30 days', async () => {
    const p = page(answering([]));
    await settle();
    const id = body(p.fetches[0]).user_id;
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(p.window.document.cookie).toBe(`controlvar_uid=${id}`);
    p.close();
  });

  test("is the cookie's when it has one, under data-cookie-name", async () => {
    const p = page(answering([]), { 'data-cookie-name': 'site_uid' }, (window) => {
      window.document.cookie = 'site_uid=known-visitor-1; Path=/';
    });
    await settle();
    expect(body(p.fetches[0]).user_id).toBe('known-visitor-1');
    p.close();
  });

  test('is data-user-id when set, with no cookie, and one warning when it holds an @', async () => {
    const p = page(answering([]), { 'data-user-id': 'jane@shop.example' });
    await settle();
    expect(body(p.fetches[0]).user_id).toBe('jane@shop.example');
    expect(p.window.document.cookie).toBe('');
    expect(p.console.filter((line) => line.startsWith('warn:') && line.includes('data-user-id'))).toHaveLength(1);
    p.close();
  });
});

describe('the hide', () => {
  /** The tag first in <head>: the script runs before the page's <body> exists. */
  const inHead = (window: Window) => {
    window.document.documentElement.removeChild(window.document.body);
  };

  test('holds <html> hidden while the answer is awaited, and data-timeout-ms=0 never hides', () => {
    const waiting = page(() => undefined, {}, inHead);
    const never = page(() => undefined, { 'data-timeout-ms': '0' }, inHead);
    expect(waiting.window.document.documentElement.style.getPropertyValue('visibility')).toBe('hidden');
    expect(waiting.window.document.documentElement.style.getPropertyPriority('visibility')).toBe('important');
    expect(never.window.document.documentElement.style.visibility).toBe('');
    waiting.close();
    never.close();
  });

  test('a script that starts once the page has a <body> never hides it, and still applies the changes', async () => {
    const changes = { version: 1, changes: [{ op: 'set_text', selector: '#t', text: 'Changed' }] };
    const waiting = page(() => undefined);
    expect(waiting.window.document.documentElement.style.visibility).toBe('');
    const answered = page(answering([experiment('headline', changes)]));
    expect(answered.window.document.documentElement.style.visibility).toBe('');
    await settle();
    expect(answered.window.document.getElementById('t')!.textContent).toBe('Changed');
    expect(answered.window.document.documentElement.style.visibility).toBe('');
    waiting.close();
    answered.close();
  });

  test.each([
    [null, 1000],
    ['', 1000],
    ['abc', 1000],
    ['250', 250],
    ['-5', 0],
    ['10000', 4000],
    ['4000', 4000],
  ])('data-timeout-ms %p is %p ms', (value, expected) => {
    expect(timeoutFrom(value)).toBe(expected);
  });
});

describe('track()', () => {
  const DAY = 24 * 60 * 60 * 1000;
  const assigned = (entries: Record<string, number>) => (window: Window) =>
    window.localStorage.setItem('controlvar_assigned', JSON.stringify(entries));
  const call = (p: { window: Window }, ...args: unknown[]) =>
    (p.window as unknown as { controlvar: { visual: { track(...a: unknown[]): void } } }).controlvar.visual.track(...args);

  test('sends one batch call for the experiments assigned in the last 30 days', async () => {
    const p = page(answering([]), {}, assigned({ recent: Date.now() - DAY, old: Date.now() - 31 * DAY }));
    await settle();
    call(p, 'purchase', { value: 42.5 });
    const batch = p.fetches.filter((f) => f.url.endsWith('/api/v1/tracking/batch'));
    expect(batch).toHaveLength(1);
    expect(batch[0].init.keepalive).toBe(true);
    const id = body(p.fetches[0]).user_id;
    expect(body(batch[0])).toEqual({ events: [{ event_type: 'purchase', user_id: id, experiment_key: 'recent', value: 42.5 }] });
    p.close();
  });

  test('sends nothing with no assigned experiment, and warns on a bad event name', async () => {
    const none = page(answering([]));
    await settle();
    call(none, 'purchase');
    call(none, '');
    expect(paths(none.fetches)).toEqual(['/api/v1/tracking/visual']);
    expect(none.console.filter((line) => line.includes('track()'))).toHaveLength(1);
    none.close();
  });
});
