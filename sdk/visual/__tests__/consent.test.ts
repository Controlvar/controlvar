/**
 * consent(), revokeConsent(), preview links and the script's own banner, in
 * the built classic script, in jsdom. The browser tests
 * (frontend/tests/e2e/runtime) cover what the page shows and the requests a
 * real page makes.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import { runClassic, type Answer, type FetchCall } from './classic';

const TAG = { 'data-api-url': 'https://cv.example', 'data-api-key': 'test-website-key' };
const PAGE = 'https://shop.example/a/b/page';
const BODY = '<h1 id="t">Original</h1><p id="other">Other</p>';
const TOKEN = 'p'.repeat(40) + 'abc';
const CHANGE = { version: 1, changes: [{ op: 'set_text', selector: '#t', text: 'Changed' }] };
const KEYS: { name: string; written: boolean }[] = JSON.parse(
  readFileSync(join(__dirname, '..', '..', '..', 'tests', 'sdk-contract', 'visual-storage-keys.json'), 'utf8'),
).keys;

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
const body = (call: FetchCall) => JSON.parse(String(call.init.body));
const paths = (calls: FetchCall[]) => calls.map((call) => new URL(call.url).pathname);

interface Visual {
  consent(): unknown;
  revokeConsent(): unknown;
  track(event: unknown, options?: unknown): unknown;
}
const visual = (window: Window) => (window as unknown as { controlvar: { visual: Visual } }).controlvar.visual;

/** A page_view goal on every page of the shop. */
const PAGE_GOAL = { event_name: 'page_shop', kind: 'page_view', page: { match: 'prefix', url: 'https://shop.example/' } };

/** The API's answers; with *goals*, the experiment has them, and the call that sends their events is left unanswered. */
function answering(changes: unknown = CHANGE, previewRules = [{ match: 'prefix', path: '/' }], host = 'shop.example', goals: unknown[] = []) {
  return (call: FetchCall): Answer | undefined => {
    const path = new URL(call.url).pathname;
    const pages = { host, rules: previewRules, max_age: 60 };
    if (path === '/api/v1/tracking/batch' && goals.length > 0) return undefined;
    if (path === '/api/v1/tracking/visual') {
      const experiments = [{ experiment_key: 'headline', variant_id: 'v1', variant_name: 'B', assigned: true, reason: 'assigned', changes, goals }];
      return { status: 200, body: { experiments, pages } };
    }
    if (path === '/api/v1/tracking/visual/preview') {
      return { status: 200, body: { experiment_key: 'headline', variant_name: 'B', changes, pages } };
    }
    if (path === '/api/v1/tracking/errors') return { status: 201, body: {} };
    return { status: 200, body: {} };
  };
}

/** Records every shadow root the page makes, closed ones included, as a test can see no other way. */
function recordShadows(window: Window & typeof globalThis): ShadowRoot[] {
  const roots: ShadowRoot[] = [];
  const native = window.Element.prototype.attachShadow;
  window.Element.prototype.attachShadow = function (this: Element, init: ShadowRootInit) {
    const root = native.call(this, init);
    roots.push(root);
    return root;
  };
  return roots;
}

function page(
  respond: (call: FetchCall) => Answer | undefined,
  attributes: Record<string, string> = {},
  setup = (_w: Window & typeof globalThis) => undefined as void,
  url = PAGE,
) {
  return runClassic(url, { ...TAG, ...attributes }, (window) => {
    window.document.body.innerHTML = BODY;
    setup(window);
  }, respond);
}

function storageKeys(window: Window): string[] {
  const keys: string[] = [];
  for (const [area, storage] of [['local', window.localStorage], ['session', window.sessionStorage]] as const) {
    for (let index = 0; index < storage.length; index += 1) keys.push(`${area}:${storage.key(index)}`);
  }
  return keys.sort();
}

function seedEveryKey(window: Window): void {
  for (const key of KEYS) {
    if (key.name === 'controlvar_uid') continue;
    window.localStorage.setItem(key.name, '1');
    window.sessionStorage.setItem(key.name, '1');
  }
  window.localStorage.setItem('controlvar_later_feature', '1');
  window.localStorage.setItem('site_cart', 'kept');
  window.sessionStorage.setItem('site_tab', 'kept');
}

const SITE_KEYS = ['local:site_cart', 'session:site_tab'];

describe('consent()', () => {
  test('before it, a tag that waits reads, stores and sends nothing; after it, the page is asked for', async () => {
    const p = page(answering(CHANGE, undefined, undefined, [PAGE_GOAL]), { 'data-require-consent': '' });
    await settle();
    expect(p.fetches).toEqual([]);
    expect(p.window.document.cookie).toBe('');
    expect(storageKeys(p.window)).toEqual([]);
    visual(p.window).track('buy');
    await settle();
    // track() before consent
    expect(p.fetches).toEqual([]);
    expect(visual(p.window).consent()).toBeUndefined();
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual', '/api/v1/tracking/batch']);
    expect(p.window.document.getElementById('t')!.textContent).toBe('Changed');
    expect(p.window.document.cookie).toMatch(/^controlvar_uid=/);
    expect(storageKeys(p.window)).toEqual([
      'local:controlvar_assigned',
      'local:controlvar_goals',
      'local:controlvar_outbox',
      'local:controlvar_pages',
      'local:controlvar_seen',
      'local:controlvar_visit',
    ]);
    visual(p.window).consent();
    await settle();
    // A second consent() asks nothing more.
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual', '/api/v1/tracking/batch']);
    p.close();
  });
});

describe('revokeConsent()', () => {
  test("deletes the cookie, the default-named one too, and every controlvar_ key, and keeps the site's own", async () => {
    const p = page(answering(), { 'data-cookie-name': 'shop_visitor' }, (window) => {
      seedEveryKey(window);
      window.document.cookie = 'controlvar_uid=old; Path=/';
      window.document.cookie = 'site_session=kept; Path=/';
    });
    await settle();
    expect(p.window.document.cookie).toContain('shop_visitor=');
    expect(visual(p.window).revokeConsent()).toBeUndefined();
    expect(p.window.document.cookie).toBe('site_session=kept');
    expect(storageKeys(p.window)).toEqual(SITE_KEYS);
    p.close();
  });

  test('stops every request from this page: track(), a new path and the page going away', async () => {
    const p = page(answering());
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual']);
    visual(p.window).revokeConsent();
    visual(p.window).track('buy', { value: 3 });
    p.window.history.pushState(null, '', '/elsewhere');
    p.window.dispatchEvent(new p.window.Event('pagehide'));
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual']);
    expect(p.window.document.cookie).toBe('');
    expect(storageKeys(p.window)).toEqual([]);
    // A change already shown stays.
    expect(p.window.document.getElementById('t')!.textContent).toBe('Changed');
    p.close();
  });

  test('drops an answer that arrives after it: nothing is applied or stored', async () => {
    let answer: (value: unknown) => void = () => undefined;
    const p = page(() => undefined, {}, (window) => {
      (window as unknown as { fetch: unknown }).fetch = () =>
        new window.Promise((resolve) => {
          answer = resolve;
        });
    });
    await settle();
    visual(p.window).revokeConsent();
    const experiments = [{ experiment_key: 'headline', variant_id: 'v1', variant_name: 'B', assigned: true, reason: 'assigned', changes: CHANGE, goals: [] }];
    const text = JSON.stringify({ experiments, pages: { host: 'shop.example', rules: [{ match: 'prefix', path: '/' }], max_age: 60 } });
    answer({ ok: true, status: 200, json: () => p.window.Promise.resolve(p.window.JSON.parse(text)) });
    await settle();
    expect(p.window.document.getElementById('t')!.textContent).toBe('Original');
    expect(storageKeys(p.window)).toEqual([]);
    expect(p.window.document.cookie).toBe('');
    p.close();
  });

  test('with data-require-consent the script waits again, and a later consent() starts with a new id', async () => {
    const p = page(answering(), { 'data-require-consent': '' });
    visual(p.window).consent();
    await settle();
    const first = body(p.fetches[0]).user_id;
    visual(p.window).revokeConsent();
    p.window.history.pushState(null, '', '/waiting');
    await settle();
    expect(p.fetches).toHaveLength(1);
    visual(p.window).consent();
    p.window.history.pushState(null, '', '/again');
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual', '/api/v1/tracking/visual']);
    expect(body(p.fetches[1]).user_id).not.toBe(first);
    p.close();
  });

  test('is idempotent and never throws, even when the browser refuses storage and cookies', async () => {
    const p = page(answering(), {}, (window) => {
      Object.defineProperty(window, 'localStorage', { get: () => { throw new Error('refused'); } });
      Object.defineProperty(window, 'sessionStorage', { get: () => { throw new Error('refused'); } });
      Object.defineProperty(window.document, 'cookie', { get: () => { throw new Error('refused'); }, set: () => { throw new Error('refused'); } });
    });
    await settle();
    expect(() => visual(p.window).revokeConsent()).not.toThrow();
    expect(() => visual(p.window).revokeConsent()).not.toThrow();
    expect(visual(p.window).revokeConsent()).toBeUndefined();
    p.close();
  });
});

describe('a preview link', () => {
  test('leaves the address, is kept for the tab, and asks with the token, the host and the path only', async () => {
    const p = page(answering(), {}, () => undefined, `${PAGE}?q=1#controlvar-preview=${TOKEN}`);
    await settle();
    expect(p.window.location.href).toBe(`${PAGE}?q=1`);
    expect(p.window.sessionStorage.getItem('controlvar_preview')).toBe(TOKEN);
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual/preview']);
    expect(body(p.fetches[0])).toEqual({ token: TOKEN, host: 'shop.example', path: '/a/b/page' });
    expect((p.fetches[0].init.headers as Record<string, string>)['X-API-Key']).toBe('test-website-key');
    expect(p.window.document.getElementById('t')!.textContent).toBe('Changed');
    // A preview sets no cookie and keeps no rules or assignments, and track() sends nothing.
    expect(p.window.document.cookie).toBe('');
    expect(storageKeys(p.window)).toEqual(['session:controlvar_preview']);
    visual(p.window).track('buy');
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual/preview']);
    p.close();
  });

  test('a later page in the tab is a preview too, and asks for nothing else', async () => {
    const p = page(answering(), {}, (window) => window.sessionStorage.setItem('controlvar_preview', TOKEN));
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual/preview']);
    p.close();
  });

  test("changes nothing on a page that is not one of the experiment's pages", async () => {
    const p = page(answering(CHANGE, [{ match: 'exact', path: '/checkout' }]), {}, () => undefined, `${PAGE}#controlvar-preview=${TOKEN}`);
    await settle();
    expect(p.window.document.getElementById('t')!.textContent).toBe('Original');
    p.close();
  });

  test('changes nothing when its answer names another host, and the banner says so', async () => {
    let roots: ShadowRoot[] = [];
    const p = page(answering(CHANGE, [{ match: 'prefix', path: '/' }], 'other.example'), {}, (window) => {
      roots = recordShadows(window);
    }, `${PAGE}#controlvar-preview=${TOKEN}`);
    await settle();
    expect(p.window.document.getElementById('t')!.textContent).toBe('Original');
    expect(roots).toHaveLength(1);
    expect(roots[0].textContent).toContain('Variant B changes none of this page');
    p.close();
  });

  test('waits for consent() on a tag that waits, and is kept only then', async () => {
    const p = page(answering(), { 'data-require-consent': '' }, () => undefined, `${PAGE}#controlvar-preview=${TOKEN}`);
    await settle();
    expect(p.window.location.hash).toBe('');
    expect(p.fetches).toEqual([]);
    expect(storageKeys(p.window)).toEqual([]);
    visual(p.window).consent();
    await settle();
    expect(paths(p.fetches)).toEqual(['/api/v1/tracking/visual/preview']);
    expect(storageKeys(p.window)).toEqual(['session:controlvar_preview']);
    p.close();
  });

  test('that no longer works is forgotten, and the banner says so', async () => {
    let roots: ShadowRoot[] = [];
    const p = page(() => ({ status: 404, body: { detail: 'gone' } }), {}, (window) => {
      roots = recordShadows(window);
    }, `${PAGE}#controlvar-preview=${TOKEN}`);
    await settle();
    expect(p.window.sessionStorage.getItem('controlvar_preview')).toBeNull();
    expect(roots).toHaveLength(1);
    expect(roots[0].textContent).toContain('This preview link has expired, was revoked, or does not exist.');
    expect(p.window.document.getElementById('t')!.textContent).toBe('Original');
    p.close();
  });
});

describe('the banner', () => {
  test('is one host beside <body>, closed, visible while the page is hidden, and [Exit preview] ends the preview', async () => {
    let roots: ShadowRoot[] = [];
    const p = page(answering(), {}, (window) => {
      roots = recordShadows(window);
    }, `${PAGE}#controlvar-preview=${TOKEN}`);
    await settle();
    const doc = p.window.document;
    const hosts = doc.getElementsByTagName('controlvar-ui');
    expect(hosts).toHaveLength(1);
    expect(hosts[0].parentNode).toBe(doc.documentElement);
    // A closed root: the page cannot reach it.
    expect(hosts[0].shadowRoot).toBeNull();
    expect((hosts[0] as HTMLElement).style.getPropertyValue('visibility')).toBe('visible');
    expect((hosts[0] as HTMLElement).style.getPropertyPriority('visibility')).toBe('important');
    expect(roots[0].textContent).toContain('Variant B, as visitors in it see this page. Nothing is recorded.');
    const exit = roots[0].querySelector('button')!;
    expect(exit.textContent).toBe('Exit preview');
    exit.click();
    expect(doc.getElementById('t')!.textContent).toBe('Original');
    expect(doc.getElementsByTagName('controlvar-ui')).toHaveLength(0);
    expect(p.window.sessionStorage.getItem('controlvar_preview')).toBeNull();
    p.close();
  });

  test('is put back when the page removes it', async () => {
    const p = page(answering(), {}, () => undefined, `${PAGE}#controlvar-preview=${TOKEN}`);
    await settle();
    const doc = p.window.document;
    doc.documentElement.removeChild(doc.getElementsByTagName('controlvar-ui')[0]);
    await settle();
    expect(doc.getElementsByTagName('controlvar-ui')).toHaveLength(1);
    expect(doc.getElementsByTagName('controlvar-ui')[0].parentNode).toBe(doc.documentElement);
    p.close();
  });

  test('a change on <html> or on the banner is never applied, and is reported as own_ui', async () => {
    const changes = {
      version: 1,
      changes: [
        { op: 'set_style', selector: 'html', styles: { opacity: '0' } },
        { op: 'hide', selector: 'controlvar-ui' },
        { op: 'set_style', selector: ':root > :not(head):not(body)', styles: { visibility: 'hidden' } },
        { op: 'set_text', selector: '#t', text: 'Changed' },
      ],
    };
    const p = page(answering(changes), {}, () => undefined, `${PAGE}#controlvar-preview=${TOKEN}`);
    await settle();
    const doc = p.window.document;
    const host = doc.getElementsByTagName('controlvar-ui')[0] as HTMLElement;
    expect(doc.documentElement.style.getPropertyValue('opacity')).toBe('');
    expect(host.style.getPropertyValue('display')).toBe('block');
    expect(host.style.getPropertyValue('visibility')).toBe('visible');
    expect(doc.getElementById('t')!.textContent).toBe('Changed');
    const sent = p.fetches.filter((call) => call.url.endsWith('/api/v1/tracking/errors')).map(body);
    expect(sent).toEqual([0, 1, 2].map((index) => ({
      error_type: 'own_ui',
      experiment_key: 'headline',
      message: "A visual change was not applied: it would have changed the page's root element or the script's own banner.",
      metadata: { index },
    })));
    p.close();
  });

  test('on an ordinary page view, a change on <html> is reported as own_ui too', async () => {
    const changes = { version: 1, changes: [{ op: 'set_style', selector: 'html', styles: { opacity: '0' } }] };
    const p = page(answering(changes));
    await settle();
    expect(p.window.document.documentElement.style.getPropertyValue('opacity')).toBe('');
    const sent = p.fetches.filter((call) => call.url.endsWith('/api/v1/tracking/errors')).map(body);
    expect(sent.map((report) => [report.error_type, report.metadata])).toEqual([['own_ui', { index: 0 }]]);
    p.close();
  });
});
