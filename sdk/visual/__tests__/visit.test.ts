/**
 * The visit (localStorage controlvar_visit) and the first-seen time
 * (controlvar_seen), on a fake clock and a fake storage: when a visit starts
 * and ends, what it keeps, what is checked again when it is read back, and a
 * refused storage. Then, in the built classic script, that nothing about the
 * visitor is read before consent() or after revokeConsent().
 */
import { readConfig } from '../src/config';
import { detailsFor, type Collect } from '../src/details';
import { touchVisit, VISIT_MS, type VisitPage } from '../src/visit';
import { runClassic, type Answer, type FetchCall } from './classic';

const MINUTE = 60 * 1000;
const T0 = 1_800_000_000_000;

function collectOf(value: string | null): Collect {
  return readConfig({ getAttribute: (name: string) => (name === 'data-collect' ? value : null), hasAttribute: () => false } as unknown as Element).collect;
}
const DEFAULT = collectOf(null);
const WITH_LANDING = collectOf('visitor,utm,landing');

class MemoryStorage {
  items = new Map<string, string>();
  writes: string[] = [];
  getItem(key: string): string | null {
    return this.items.has(key) ? this.items.get(key)! : null;
  }
  setItem(key: string, value: string): void {
    this.writes.push(key);
    this.items.set(key, String(value));
  }
  removeItem(key: string): void {
    this.items.delete(key);
  }
}

const page = (url: string): VisitPage => {
  const parsed = new URL(url);
  return { search: parsed.search, pathname: parsed.pathname };
};

function view(storage: MemoryStorage | null, now: number, url: string, collect: Collect = WITH_LANDING) {
  const visit = touchVisit(storage as unknown as Storage, now, page(url), collect, () => true);
  return detailsFor({}, visit, collect);
}

const stored = (storage: MemoryStorage) => JSON.parse(storage.getItem('controlvar_visit')!);

describe('a visit', () => {
  test('lasts while pages are viewed less than 30 minutes apart, and keeps its first page campaign and landing page', () => {
    const storage = new MemoryStorage();
    expect(view(storage, T0, 'https://shop.example/sale?utm_source=news&utm_medium=email')).toEqual({
      web_visitor: 'new',
      web_utm_source: 'news',
      web_utm_medium: 'email',
      web_landing_path: '/sale',
    });
    const after29 = view(storage, T0 + 29 * MINUTE, 'https://shop.example/other?utm_source=ads');
    expect(`29 min: ${after29.web_utm_source === 'news' ? 'same visit' : 'new visit'}`).toBe('29 min: same visit');
    expect(after29).toEqual({ web_visitor: 'new', web_utm_source: 'news', web_utm_medium: 'email', web_landing_path: '/sale' });
    expect(stored(storage)).toEqual({ started: T0, last_seen: T0 + 29 * MINUTE, utm: { utm_source: 'news', utm_medium: 'email' }, landing: '/sale' });
    // 31 minutes after the last page view: a new visit, from this page.
    const after31 = view(storage, T0 + 60 * MINUTE, 'https://shop.example/ads?utm_source=ads');
    expect(`31 min: ${after31.web_utm_source === 'ads' ? 'new visit' : 'same visit'}`).toBe('31 min: new visit');
    expect(after31).toEqual({ web_visitor: 'returning', web_utm_source: 'ads', web_landing_path: '/ads' });
    expect(stored(storage)).toEqual({ started: T0 + 60 * MINUTE, last_seen: T0 + 60 * MINUTE, utm: { utm_source: 'ads' }, landing: '/ads' });
    expect(VISIT_MS).toBe(30 * MINUTE);
  });

  test('ends exactly after 30 minutes: 30 is the same visit, 30 and 1 ms is not', () => {
    const storage = new MemoryStorage();
    view(storage, T0, 'https://shop.example/a?utm_source=first');
    expect(view(storage, T0 + VISIT_MS, 'https://shop.example/b?utm_source=second').web_utm_source).toBe('first');
    expect(view(storage, T0 + 2 * VISIT_MS + 1, 'https://shop.example/c?utm_source=third').web_utm_source).toBe('third');
  });

  test('is returning when this browser was first seen before the visit started, and new on the first visit', () => {
    const storage = new MemoryStorage();
    storage.setItem('controlvar_seen', String(T0 - 5 * 24 * 60 * MINUTE));
    expect(view(storage, T0, 'https://shop.example/').web_visitor).toBe('returning');
    const fresh = new MemoryStorage();
    expect(view(fresh, T0, 'https://shop.example/').web_visitor).toBe('new');
    expect(fresh.getItem('controlvar_seen')).toBe(String(T0));
    expect(view(fresh, T0 + 10 * MINUTE, 'https://shop.example/x').web_visitor).toBe('new');
  });

  test('writes controlvar_seen once, when it is absent, as a visit starts', () => {
    const storage = new MemoryStorage();
    view(storage, T0, 'https://shop.example/');
    view(storage, T0 + 10 * MINUTE, 'https://shop.example/a');
    view(storage, T0 + 120 * MINUTE, 'https://shop.example/b');
    expect(storage.writes.filter((key) => key === 'controlvar_seen')).toHaveLength(1);
    expect(storage.getItem('controlvar_seen')).toBe(String(T0));
    // Absent in the middle of a visit: not written then, and the visitor is left out.
    storage.removeItem('controlvar_seen');
    expect(view(storage, T0 + 125 * MINUTE, 'https://shop.example/c').web_visitor).toBeUndefined();
    expect(storage.getItem('controlvar_seen')).toBeNull();
  });
});

describe('what is read back', () => {
  const visitWith = (value: unknown) => {
    const storage = new MemoryStorage();
    storage.setItem('controlvar_visit', typeof value === 'string' ? value : JSON.stringify(value));
    storage.setItem('controlvar_seen', String(T0 - 2 * MINUTE));
    return storage;
  };

  test('a value a page wrote into the visit is checked again, and dropped when it fails', () => {
    const handWritten = visitWith({
      started: T0 - MINUTE,
      last_seen: T0 - MINUTE,
      utm: { utm_source: 'a@b.c', utm_medium: 'x'.repeat(101), utm_campaign: 'ok-value', utm_term: 'never' },
      landing: '/u/a@b.c',
      referrer: 'https://www.google.com/search?q=cv-secret-7731',
    });
    const details = view(handWritten, T0, 'https://shop.example/now?utm_source=now');
    expect(details).toEqual({ web_visitor: 'returning', web_utm_campaign: 'ok-value' });
    expect(Object.keys(stored(handWritten)).sort()).toEqual(['last_seen', 'started', 'utm']);
    expect(stored(handWritten).utm).toEqual({ utm_campaign: 'ok-value' });
    expect(JSON.stringify([...handWritten.items.values()])).not.toMatch(/a@b\.c|cv-secret|never/);
  });

  test.each([
    ['not JSON', '{started'],
    ['an array', [1, 2]],
    ['a time as text', { started: String(T0 - MINUTE), last_seen: T0 - MINUTE }],
    ['a start in the future', { started: T0 + MINUTE, last_seen: T0 + MINUTE }],
    ['a start after the last page view', { started: T0 - MINUTE, last_seen: T0 - 2 * MINUTE }],
    ['a fraction', { started: T0 - 0.5, last_seen: T0 - 0.5 }],
  ])('a visit that is %s starts a new one, from this page', (_name, value) => {
    const storage = visitWith(value);
    expect(view(storage, T0, 'https://shop.example/now?utm_source=now')).toEqual({
      web_visitor: 'returning',
      web_utm_source: 'now',
      web_landing_path: '/now',
    });
    expect(stored(storage)).toEqual({ started: T0, last_seen: T0, utm: { utm_source: 'now' }, landing: '/now' });
  });

  test.each([['text', 'yesterday'], ['in the future', String(T0 + MINUTE)], ['zero', '0'], ['a fraction', '1.5']])(
    'a first-seen time that is %s is replaced as a visit starts',
    (_name, value) => {
      const storage = new MemoryStorage();
      storage.setItem('controlvar_seen', value);
      expect(view(storage, T0, 'https://shop.example/').web_visitor).toBe('new');
      expect(storage.getItem('controlvar_seen')).toBe(String(T0));
    },
  );
});

describe('a refused storage', () => {
  const refusing = (method: 'getItem' | 'setItem') => {
    const storage = new MemoryStorage();
    storage[method] = () => {
      throw new Error('refused');
    };
    return storage;
  };

  test.each([
    ['no storage at all', null],
    ['a read that throws', refusing('getItem')],
    ['a write that throws', refusing('setItem')],
  ])('%s: the visit details are left out, never filled in from this page', (_name, storage) => {
    expect(view(storage as MemoryStorage | null, T0, 'https://shop.example/landing?utm_source=news')).toEqual({});
  });

  test('a tag that collects none of them reads and writes nothing', () => {
    const storage = new MemoryStorage();
    expect(view(storage, T0, 'https://shop.example/?utm_source=news', collectOf('device,browser,language,referrer'))).toEqual({});
    expect(storage.writes).toEqual([]);
  });

  test("utm alone keeps the visit's campaign without a first-seen time; landing is never kept without landing", () => {
    const storage = new MemoryStorage();
    expect(view(storage, T0, 'https://shop.example/sale?utm_source=news', collectOf('utm'))).toEqual({ web_utm_source: 'news' });
    expect(storage.writes).toEqual(['controlvar_visit']);
    expect(stored(storage)).toEqual({ started: T0, last_seen: T0, utm: { utm_source: 'news' } });
    expect(view(new MemoryStorage(), T0, 'https://shop.example/sale', DEFAULT)).toEqual({ web_visitor: 'new' });
  });

  test('nothing is written once the script may no longer store', () => {
    const storage = new MemoryStorage();
    expect(touchVisit(storage as unknown as Storage, T0, page('https://shop.example/'), DEFAULT, () => false)).toBeNull();
    expect(storage.writes).toEqual([]);
  });
});

// --- In the built classic script: reads before consent and after revokeConsent().

const TAG = { 'data-api-url': 'https://cv.example', 'data-api-key': 'test-website-key' };
const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
const DETAIL_KEYS = ['controlvar_visit', 'controlvar_seen'];

function answering(call: FetchCall): Answer | undefined {
  if (new URL(call.url).pathname === '/api/v1/tracking/visual') {
    return { status: 200, body: { experiments: [], pages: { host: 'shop.example', rules: [{ match: 'prefix', path: '/' }], max_age: 60 } } };
  }
  return { status: 200, body: {} };
}

/** Counts every read of what the details come from, and every access to the two visit keys. */
function counting(window: Window & typeof globalThis, reads: string[], writes: string[]) {
  const nav = window.navigator;
  const counted = (target: object, name: string, value: unknown) =>
    Object.defineProperty(target, name, {
      configurable: true,
      get: () => {
        reads.push(name);
        return value;
      },
    });
  counted(nav, 'userAgent', 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) Mobile/15E148 Safari/604.1');
  counted(nav, 'userAgentData', { mobile: true });
  counted(nav, 'language', 'en-GB');
  counted(nav, 'maxTouchPoints', 5);
  counted(window.document, 'referrer', 'https://www.google.com/');
  const proto = Object.getPrototypeOf(window.localStorage);
  const getItem = proto.getItem;
  const setItem = proto.setItem;
  proto.getItem = function (this: Storage, key: string) {
    if (DETAIL_KEYS.includes(key)) reads.push(key);
    return getItem.call(this, key);
  };
  proto.setItem = function (this: Storage, key: string, value: string) {
    if (DETAIL_KEYS.includes(key)) writes.push(key);
    return setItem.call(this, key, value);
  };
}

describe('consent and the details', () => {
  test('before consent() nothing they come from is read; after it they are; after revokeConsent() a new path reads and writes nothing', async () => {
    const reads: string[] = [];
    const writes: string[] = [];
    const p = runClassic('https://shop.example/a?utm_source=news', { ...TAG, 'data-require-consent': '' }, (window) => counting(window, reads, writes), answering);
    await settle();
    expect(`reads before consent ${reads.length} != 0`).toBe('reads before consent 0 != 0');
    expect(writes).toEqual([]);
    const visual = (p.window as unknown as { controlvar: { visual: { consent(): void; revokeConsent(): void } } }).controlvar.visual;
    visual.consent();
    await settle();
    // The positive control: consent() reads them, and writes the two keys.
    expect(new Set(reads)).toEqual(new Set(['userAgent', 'userAgentData', 'language', 'maxTouchPoints', 'referrer', 'controlvar_visit', 'controlvar_seen']));
    expect(writes.sort()).toEqual(['controlvar_seen', 'controlvar_visit']);
    visual.revokeConsent();
    reads.length = 0;
    writes.length = 0;
    p.window.history.pushState(null, '', '/b?utm_source=later');
    await settle();
    expect(reads).toEqual([]);
    expect(writes).toEqual([]);
    expect(DETAIL_KEYS.map((key) => p.window.localStorage.getItem(key))).toEqual([null, null]);
    p.close();
  });

  test('a path change is a page view of the visit, and the call for it carries the first page campaign', async () => {
    const p = runClassic('https://shop.example/a?utm_source=news', TAG, (window) => {
      Object.defineProperty(window.document, 'referrer', { configurable: true, get: () => 'https://www.google.com/' });
    }, answering);
    await settle();
    const before = JSON.parse(p.window.localStorage.getItem('controlvar_visit')!);
    p.window.history.pushState(null, '', '/b?utm_source=later');
    await settle();
    const asks = p.fetches.filter((call) => new URL(call.url).pathname === '/api/v1/tracking/visual').map((call) => JSON.parse(String(call.init.body)));
    expect(asks.map((ask) => [ask.path, ask.context.web_utm_source, ask.context.web_referrer])).toEqual([
      ['/a', 'news', 'www.google.com'],
      ['/b', 'news', 'www.google.com'],
    ]);
    const after = JSON.parse(p.window.localStorage.getItem('controlvar_visit')!);
    expect(after.started).toBe(before.started);
    expect(after.last_seen).toBeGreaterThanOrEqual(before.last_seen);
    p.close();
  });
});
