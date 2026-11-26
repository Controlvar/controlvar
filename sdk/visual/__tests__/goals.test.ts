/**
 * Goals in the built classic script, in jsdom: which page views and clicks
 * count, what each event carries, the one click listener, the goals the
 * script keeps between pages, and the reports. jsdom cannot make a click a
 * person makes, so the test hands one to the script's own listener; the
 * browser tests (frontend/tests/e2e/runtime) make real ones.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import { runClassic, type Answer, type ClassicPage, type FetchCall } from './classic';
import { GOAL_MESSAGES } from '../src/reports';

const REPORTS = JSON.parse(readFileSync(join(__dirname, '..', '..', '..', 'tests', 'sdk-contract', 'visual-error-reports.json'), 'utf8'));

const TAG = { 'data-api-url': 'https://cv.example', 'data-api-key': 'test-website-key' };
const SHOP = 'https://shop.example/shop';
const BODY =
  '<button id="buy"><span id="label">Buy</span></button><p id="outside">Text</p><a class="twice">1</a><a class="twice">2</a>';
const PAGE_VIEW_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
const body = (call: FetchCall) => JSON.parse(String(call.init.body));
const pathOf = (call: FetchCall) => new URL(call.url).pathname;

interface Sent {
  event_type: string;
  user_id: string;
  experiment_key: string;
  event_id: string;
  metadata?: Record<string, string>;
}

const clickGoal = (name: string, selector: string, url = SHOP, match = 'prefix') => ({ event_name: name, kind: 'click', selector, page: { match, url } });
const viewGoal = (name: string, url = SHOP, match = 'prefix') => ({ event_name: name, kind: 'page_view', page: { match, url } });

function experiment(key: string, goals: unknown[] | null, extra: Record<string, unknown> = {}) {
  return { experiment_key: key, variant_id: 'v1', variant_name: 'Control', assigned: true, reason: 'assigned', changes: null, goals, ...extra };
}

interface Answers {
  /** The experiments the answer names, for the path asked about. */
  experiments(path: string): unknown[];
  /** The page rules on the host, and for how many seconds the script may keep them (0 by default). */
  rules?: unknown[];
  maxAge?: number;
  /** The batch call's status; null leaves it pending. */
  batch?: (call: FetchCall) => number | null;
}

function answering(answers: Answers) {
  return (call: FetchCall): Answer | undefined => {
    const path = pathOf(call);
    if (path === '/api/v1/tracking/visual') {
      const rules = answers.rules ?? [{ match: 'prefix', path: '/' }];
      return { status: 200, body: { experiments: answers.experiments(body(call).path), pages: { host: 'shop.example', rules, max_age: answers.maxAge ?? 0 } } };
    }
    if (path === '/api/v1/tracking/batch') {
      const status = answers.batch ? answers.batch(call) : 200;
      return status === null ? undefined : { status, body: {} };
    }
    return { status: 201, body: {} };
  };
}

/** The click listeners the script adds to the document, in the capture phase. */
interface Listeners {
  added: { listener: EventListener; capture: boolean }[];
}

function watchListeners(window: Window): Listeners {
  const watched: Listeners = { added: [] };
  const doc = window.document;
  const native = doc.addEventListener.bind(doc);
  doc.addEventListener = ((type: string, listener: EventListener, options?: boolean | AddEventListenerOptions) => {
    const capture = options === true || (typeof options === 'object' && options !== null && options.capture === true);
    if (type === 'click') watched.added.push({ listener, capture });
    native(type, listener, options);
  }) as typeof doc.addEventListener;
  return watched;
}

interface GoalPage extends ClassicPage {
  listeners: Listeners;
  /** A click a person makes on *target*, handed to the script's listener. */
  press(target: Element): void;
  events(): Sent[];
  reports(): Record<string, unknown>[];
}

function page(
  answers: Answers,
  options: { url?: string; attributes?: Record<string, string>; storage?: Record<string, string>; setup?: (window: Window & typeof globalThis) => void } = {},
): GoalPage {
  let listeners: Listeners = { added: [] };
  const opened = runClassic(
    options.url ?? SHOP,
    { ...TAG, ...(options.attributes ?? {}) },
    (window) => {
      window.document.body.innerHTML = BODY;
      for (const [key, value] of Object.entries(options.storage ?? {})) window.localStorage.setItem(key, value);
      listeners = watchListeners(window);
      if (options.setup) options.setup(window);
    },
    answering(answers),
  );
  return {
    ...opened,
    listeners,
    press(target) {
      for (const { listener } of listeners.added) listener({ isTrusted: true, target, type: 'click' } as unknown as Event);
    },
    events: () => opened.fetches.filter((call) => pathOf(call) === '/api/v1/tracking/batch').flatMap((call) => body(call).events),
    reports: () => opened.fetches.filter((call) => pathOf(call) === '/api/v1/tracking/errors').map(body),
  };
}

const named = (events: Sent[], name: string) => events.filter((event) => event.event_type === name).length;

function localKeys(window: Window): Record<string, string> {
  const kept: Record<string, string> = {};
  for (let index = 0; index < window.localStorage.length; index += 1) {
    const key = window.localStorage.key(index)!;
    kept[key] = window.localStorage.getItem(key)!;
  }
  return kept;
}

describe('page views', () => {
  test('a page_view goal counts once per page load and per path change; never for a hash, a query or a replaceState of the same path', async () => {
    const answers: Answers = { experiments: () => [experiment('shop', [viewGoal('page_shop'), viewGoal('page_cart', 'https://shop.example/cart', 'exact')])] };
    const p = page(answers);
    const counts: string[] = [];
    let before = 0;
    const step = async (name: string, act: () => void) => {
      act();
      await settle();
      const now = named(p.events(), 'page_shop');
      counts.push(`${name}: ${now - before}`);
      before = now;
    };
    await step('load', () => undefined);
    await step('pushState', () => p.window.history.pushState(null, '', '/shop/a'));
    await step('next.js nav', () => {
      p.window.history.pushState(null, '', '/shop/b');
      p.window.history.replaceState(null, '', '/shop/b');
    });
    await step('hash', () => {
      p.window.location.hash = 'details';
    });
    await step('query', () => p.window.history.pushState(null, '', '/shop/b?sort=price'));
    await step('another page', () => p.window.history.pushState(null, '', '/elsewhere'));
    const storage = localKeys(p.window);
    p.close();

    const reloaded = page(answers, { url: 'https://shop.example/shop/b', storage });
    await settle();
    counts.push(`reload: ${named(reloaded.events(), 'page_shop')}`);
    counts.push(`a rule that does not match: ${named([...p.events(), ...reloaded.events()], 'page_cart')}`);
    reloaded.close();
    expect(counts).toEqual(['load: 1', 'pushState: 1', 'next.js nav: 1', 'hash: 0', 'query: 0', 'another page: 0', 'reload: 1', 'a rule that does not match: 0']);
  });

  test("an event carries the goal's name, the visitor, the experiment, the page view's id and its website details, and nothing else", async () => {
    const answers: Answers = { experiments: () => [experiment('shop', [viewGoal('page_shop'), viewGoal('page_any', 'https://shop.example/', 'prefix')])] };
    const p = page(answers, { url: `${SHOP}?utm_source=news&email=a%40b.c#top`, attributes: { 'data-attributes': '{"plan":"pro"}', 'data-user-id': 'account-77' } });
    await settle();
    const [ask] = p.fetches.filter((call) => pathOf(call) === '/api/v1/tracking/visual').map(body);
    const batch = p.fetches.filter((call) => pathOf(call) === '/api/v1/tracking/batch');
    expect(batch).toHaveLength(1);
    expect(batch[0].init).toMatchObject({ method: 'POST', credentials: 'omit', referrerPolicy: 'no-referrer', keepalive: true });
    expect((batch[0].init.headers as Record<string, string>)['X-API-Key']).toBe('test-website-key');
    const events = p.events();
    expect(events.map((event) => event.event_type)).toEqual(['page_shop', 'page_any']);
    const details = Object.fromEntries(Object.entries(ask.context).filter(([key]) => key.startsWith('web_')));
    expect(Object.keys(details).length).toBeGreaterThan(3);
    for (const event of events) {
      expect(Object.keys(event)).toEqual(['event_type', 'user_id', 'experiment_key', 'event_id', 'metadata']);
      expect(event).toMatchObject({ user_id: 'account-77', experiment_key: 'shop', metadata: details });
      const [id, name] = event.event_id.split(':');
      expect([PAGE_VIEW_ID.test(id), name]).toEqual([true, event.event_type]);
    }
    // One page view, one id; the next page view, another.
    expect(events[0].event_id.split(':')[0]).toBe(events[1].event_id.split(':')[0]);
    p.window.history.pushState(null, '', '/shop/next');
    await settle();
    const next = p.events().slice(2);
    expect(next.map((event) => event.event_type)).toEqual(['page_shop', 'page_any']);
    expect(next[0].event_id.split(':')[0]).not.toBe(events[0].event_id.split(':')[0]);
    expect(JSON.stringify(p.events())).not.toMatch(/plan|pro"|email|a%40b|#top|https?:/);
    p.close();
  });

  test('with data-collect none an event carries no details', async () => {
    const p = page({ experiments: () => [experiment('shop', [viewGoal('page_shop')])] }, { attributes: { 'data-collect': 'none' } });
    await settle();
    expect(p.events().map((event) => Object.keys(event))).toEqual([['event_type', 'user_id', 'experiment_key', 'event_id']]);
    p.close();
  });
});

describe('click goals', () => {
  test('a click a person makes on the element or inside it counts, once per page view; a click elsewhere or one a script makes does not', async () => {
    const p = page({ experiments: () => [experiment('shop', [clickGoal('click_buy', '#buy')])] });
    await settle();
    const doc = p.window.document;
    const counts: string[] = [];
    const step = async (name: string, act: () => void) => {
      act();
      await settle();
      counts.push(`${name}: ${named(p.events(), 'click_buy')}`);
    };
    await step('outside', () => p.press(doc.getElementById('outside')!));
    await step('el.click()', () => doc.getElementById('buy')!.click());
    await step('dispatchEvent', () => doc.getElementById('label')!.dispatchEvent(new p.window.MouseEvent('click', { bubbles: true })));
    await step('child span', () => p.press(doc.getElementById('label')!));
    await step('again on the button', () => p.press(doc.getElementById('buy')!));
    await step('the next page view', () => {
      p.window.history.pushState(null, '', '/shop/2');
    });
    await step('a click there', () => p.press(doc.getElementById('buy')!));
    await step('a page the goal is not on', () => {
      p.window.history.pushState(null, '', '/elsewhere');
    });
    await step('a click there', () => p.press(doc.getElementById('buy')!));
    expect(counts).toEqual([
      'outside: 0',
      'el.click(): 0',
      'dispatchEvent: 0',
      'child span: 1',
      'again on the button: 1',
      'the next page view: 1',
      'a click there: 2',
      'a page the goal is not on: 2',
      'a click there: 2',
    ]);
    p.close();
  });

  test("a selector that matches no element or several counts nothing and is reported once per page view, with the goal's position", async () => {
    const goals = [clickGoal('click_none', '#missing'), clickGoal('click_many', '.twice'), clickGoal('click_buy', '#buy')];
    const p = page({ experiments: () => [experiment('shop', goals)] });
    await settle();
    const doc = p.window.document;
    p.press(doc.getElementById('outside')!);
    p.press(doc.querySelector('.twice')!);
    p.press(doc.getElementById('label')!);
    await settle();
    expect(p.events().map((event) => event.event_type)).toEqual(['click_buy']);
    expect(p.reports()).toEqual([
      { error_type: 'goal_not_found', experiment_key: 'shop', message: GOAL_MESSAGES.goal_not_found, metadata: { index: 0 } },
      { error_type: 'goal_ambiguous', experiment_key: 'shop', message: GOAL_MESSAGES.goal_ambiguous, metadata: { index: 1 } },
    ]);
    for (const report of p.reports()) {
      const type = REPORTS.types.find((item: { error_type: string }) => item.error_type === report.error_type);
      expect([Object.keys(report), Object.keys(report.metadata as object)]).toEqual([REPORTS.request_keys, type.metadata_keys]);
    }
    expect(JSON.stringify(p.reports())).not.toContain('feature_flag');
    // A new page view reports again.
    p.window.history.pushState(null, '', '/shop/2');
    await settle();
    p.press(doc.getElementById('outside')!);
    await settle();
    expect(p.reports().map((report) => report.error_type)).toEqual(['goal_not_found', 'goal_ambiguous', 'goal_not_found', 'goal_ambiguous']);
    p.close();
  });

  test('a selector the browser cannot read counts nothing and is reported as matching no element', async () => {
    const p = page({ experiments: () => [experiment('shop', [clickGoal('click_odd', 'button:unknown-pseudo')])] });
    await settle();
    p.press(p.window.document.getElementById('buy')!);
    await settle();
    expect([p.events().length, p.reports().map((report) => report.error_type)]).toEqual([0, ['goal_not_found']]);
    p.close();
  });
});

describe('the click listener', () => {
  test.each([
    ['a tag that does not wait for consent', {}],
    ['a tag that waits for consent', { 'data-require-consent': '' }],
  ])('on %s is added once: path changes, a popstate and consent() add none, and a click counts once for each assigned experiment', async (_name, attributes) => {
    const p = page(
      { experiments: () => [experiment('first', [clickGoal('click_buy', '#buy')]), experiment('second', [clickGoal('click_buy_too', '#buy')]), experiment('third', null, { assigned: false, reason: 'targeting' })] },
      { attributes },
    );
    const visual = (p.window as unknown as { controlvar: { visual: { consent(): void } } }).controlvar.visual;
    visual.consent();
    await settle();
    for (const path of ['/shop/1', '/shop/2', '/shop/3']) {
      p.window.history.pushState(null, '', path);
      await settle();
    }
    p.window.dispatchEvent(new p.window.PopStateEvent('popstate'));
    visual.consent();
    await settle();
    const captured = p.listeners.added.filter((added) => added.capture);
    expect(`listeners ${captured.length}`).toBe('listeners 1');
    expect(p.listeners.added).toHaveLength(1);
    p.press(p.window.document.getElementById('label')!);
    await settle();
    expect(p.events().map((event) => [event.experiment_key, event.event_type])).toEqual([
      ['first', 'click_buy'],
      ['second', 'click_buy_too'],
    ]);
    p.close();
  });

  test('an event the API did not accept is sent again, with the same event_id, by the next page', async () => {
    let status = 503;
    const answers: Answers = { experiments: () => [experiment('shop', [clickGoal('click_buy', '#buy')])], batch: () => status };
    const p = page(answers);
    await settle();
    p.press(p.window.document.getElementById('buy')!);
    await settle();
    const [first] = p.events();
    expect(JSON.parse(p.window.localStorage.getItem('controlvar_outbox')!).map((entry: { event: Sent }) => entry.event)).toEqual([first]);
    const storage = localKeys(p.window);
    p.close();

    status = 200;
    const next = page(answers, { url: 'https://shop.example/shop/next', storage });
    await settle();
    expect(next.events()).toEqual([first]);
    expect(next.window.localStorage.getItem('controlvar_outbox')).toBeNull();
    next.close();
  });
});

describe('the goals kept between pages', () => {
  test('are kept per experiment, replaced when an answer names it, and removed when an answer for a page it covers no longer does', async () => {
    let stopped = false;
    const answers: Answers = {
      rules: [
        { match: 'exact', path: '/shop' },
        { match: 'exact', path: '/other' },
      ],
      experiments(path) {
        if (path === '/shop') return [experiment('a', [clickGoal(stopped ? 'click_a_new' : 'click_a', '#buy')])];
        if (path === '/other') return stopped ? [] : [experiment('b', [viewGoal('page_b', 'https://shop.example/other', 'exact')])];
        return [];
      },
    };
    const p = page(answers);
    const kept = () => {
      const goals = JSON.parse(p.window.localStorage.getItem('controlvar_goals') || '{}');
      return Object.keys(goals)
        .sort()
        .map((key) => `${key}: ${goals[key].goals.map((goal: { event_name: string }) => goal.event_name).join(',')}`);
    };
    const seen: string[][] = [];
    await settle();
    seen.push(kept());
    p.window.history.pushState(null, '', '/other');
    await settle();
    seen.push(kept());
    stopped = true;
    p.window.history.pushState(null, '', '/shop');
    await settle();
    seen.push(kept());
    p.window.history.pushState(null, '', '/other');
    await settle();
    seen.push(kept());
    expect(seen).toEqual([['a: click_a'], ['a: click_a', 'b: page_b'], ['a: click_a_new', 'b: page_b'], ['a: click_a_new']]);
    const stored = JSON.parse(p.window.localStorage.getItem('controlvar_goals')!);
    expect(stored.a).toEqual({
      at: expect.any(Number),
      pages: [{ match: 'exact', path: '/shop' }],
      goals: [{ kind: 'click', event_name: 'click_a_new', selector: '#buy', page: { match: 'prefix', host: 'shop.example', path: '/shop' } }],
    });
    p.close();
  });

  test('an experiment the visitor is no longer assigned to, or that has no goals, keeps none', async () => {
    let round = 0;
    const p = page({
      experiments: () =>
        round === 0
          ? [experiment('a', [viewGoal('page_a')]), experiment('b', [viewGoal('page_b')])]
          : [experiment('a', null, { assigned: false, reason: 'targeting' }), experiment('b', [])],
    });
    await settle();
    expect(Object.keys(JSON.parse(p.window.localStorage.getItem('controlvar_goals')!)).sort()).toEqual(['a', 'b']);
    round = 1;
    p.window.history.pushState(null, '', '/shop/2');
    await settle();
    expect(p.window.localStorage.getItem('controlvar_goals')).toBeNull();
    p.close();
  });

  test('a goal on a page no experiment covers counts there, from the goals kept on an earlier page', async () => {
    const answers: Answers = {
      rules: [{ match: 'exact', path: '/shop' }],
      maxAge: 60,
      experiments: (path) => (path === '/shop' ? [experiment('shop', [viewGoal('page_thanks', 'https://shop.example/thanks', 'exact')])] : []),
    };
    const p = page(answers);
    await settle();
    const storage = localKeys(p.window);
    p.close();
    const thanks = page(answers, { url: 'https://shop.example/thanks', storage });
    await settle();
    expect(thanks.fetches.map(pathOf)).toEqual(['/api/v1/tracking/batch']);
    expect(thanks.events().map((event) => event.event_type)).toEqual(['page_thanks']);
    thanks.close();
  });

  test('uses only what it can check: an experiment older than 30 days, or with one goal out of its form, counts nothing', async () => {
    const now = Date.now();
    const day = 24 * 60 * 60 * 1000;
    const page_ = (path = '/shop') => ({ match: 'prefix', host: 'shop.example', path });
    const entry = (at: number, goals: unknown[]) => ({ at, pages: [], goals });
    const kept = {
      fresh: entry(now - 1000, [{ kind: 'page_view', event_name: 'page_fresh', page: page_() }]),
      old: entry(now - 30 * day - 1, [{ kind: 'page_view', event_name: 'page_old', page: page_() }]),
      later: entry(now + 60 * 1000, [{ kind: 'page_view', event_name: 'page_later', page: page_() }]),
      hand_written: entry(now - 1000, [{ kind: 'page_view', event_name: 'hand_written_goal', page: page_() }, { kind: 'click', event_name: 'click_bad', selector: 'a{color:red}', page: page_() }]),
      bad_name: entry(now - 1000, [{ kind: 'page_view', event_name: 'Page-Bad', page: page_() }]),
      bad_mark: entry(now - 1000, [{ kind: 'page_view', event_name: 'page_bad_mark!', page: page_() }]),
      query: entry(now - 1000, [{ kind: 'page_view', event_name: 'page_with_query', page: { match: 'prefix', host: 'shop.example', path: '/shop?ref=1' } }]),
      other_host: entry(now - 1000, [{ kind: 'page_view', event_name: 'page_other_host', page: { match: 'prefix', host: 'other.example', path: '/shop' } }]),
      extra_kind: entry(now - 1000, [{ kind: 'scroll', event_name: 'scroll_shop', page: page_() }]),
    };
    const p = page({ experiments: () => [] }, { storage: { controlvar_goals: JSON.stringify(kept) } });
    await settle();
    expect(p.events().map((event) => event.event_type)).toEqual(['page_fresh']);
    p.close();
  });

  test('keeps at most 10 experiments, the ones named most recently', async () => {
    const now = Date.now();
    const kept: Record<string, unknown> = {};
    for (let n = 0; n < 12; n += 1) {
      kept[`exp-${n}`] = { at: now - 1000 * (n + 1), pages: [], goals: [{ kind: 'page_view', event_name: `page_${n}`, page: { match: 'prefix', host: 'shop.example', path: '/' } }] };
    }
    const p = page({ experiments: () => [] }, { storage: { controlvar_goals: JSON.stringify(kept) } });
    await settle();
    expect(p.events().map((event) => event.event_type).sort()).toEqual(Array.from({ length: 10 }, (_, n) => `page_${n}`).sort());
    p.close();
  });

  test('nothing about goals is read, kept or sent before consent(), nor after revokeConsent()', async () => {
    const reads: string[] = [];
    const now = Date.now();
    const kept = { shop: { at: now - 1000, pages: [], goals: [{ kind: 'page_view', event_name: 'page_shop', page: { match: 'prefix', host: 'shop.example', path: '/' } }] } };
    const outbox = [{ at: now - 1000, event: { event_type: 'page_shop', user_id: 'v', experiment_key: 'shop', event_id: '00000000-0000-4000-8000-000000000001:page_shop' } }];
    const p = page(
      { experiments: () => [experiment('shop', [viewGoal('page_shop'), clickGoal('click_buy', '#buy')])] },
      {
        attributes: { 'data-require-consent': '' },
        storage: { controlvar_goals: JSON.stringify(kept), controlvar_outbox: JSON.stringify(outbox) },
        setup: (window) => {
          const proto = Object.getPrototypeOf(window.localStorage);
          const getItem = proto.getItem;
          proto.getItem = function (this: Storage, key: string) {
            if (key === 'controlvar_goals' || key === 'controlvar_outbox') reads.push(key);
            return getItem.call(this, key);
          };
        },
      },
    );
    await settle();
    p.press(p.window.document.getElementById('buy')!);
    await settle();
    expect([reads, p.fetches.length]).toEqual([[], 0]);
    const visual = (p.window as unknown as { controlvar: { visual: { consent(): void; revokeConsent(): void } } }).controlvar.visual;
    visual.consent();
    await settle();
    expect(p.events().length).toBeGreaterThan(0);
    visual.revokeConsent();
    const sent = p.fetches.length;
    p.press(p.window.document.getElementById('buy')!);
    p.window.history.pushState(null, '', '/shop/after');
    p.window.dispatchEvent(new p.window.Event('pagehide'));
    await settle();
    expect(p.fetches.length - sent).toBe(0);
    expect(p.window.localStorage.getItem('controlvar_goals')).toBeNull();
    expect(p.window.localStorage.getItem('controlvar_outbox')).toBeNull();
    p.close();
  });

  test('a preview tab counts no goal and sends no kept event', async () => {
    const now = Date.now();
    const kept = { shop: { at: now - 1000, pages: [], goals: [{ kind: 'page_view', event_name: 'page_shop', page: { match: 'prefix', host: 'shop.example', path: '/' } }, { kind: 'click', event_name: 'click_buy', selector: '#buy', page: { match: 'prefix', host: 'shop.example', path: '/' } }] } };
    const outbox = [{ at: now - 1000, event: { event_type: 'page_shop', user_id: 'v', experiment_key: 'shop', event_id: '00000000-0000-4000-8000-000000000001:page_shop' } }];
    const p = page(
      { experiments: () => [] },
      { url: `${SHOP}#controlvar-preview=${'p'.repeat(43)}`, storage: { controlvar_goals: JSON.stringify(kept), controlvar_outbox: JSON.stringify(outbox) } },
    );
    await settle();
    p.press(p.window.document.getElementById('buy')!);
    p.window.history.pushState(null, '', '/shop/2');
    await settle();
    p.press(p.window.document.getElementById('buy')!);
    await settle();
    expect(p.fetches.map(pathOf).filter((path) => path !== '/api/v1/tracking/visual/preview')).toEqual([]);
    p.close();
  });
});
