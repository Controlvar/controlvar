/**
 * The outbox (src/outbox.ts), with a storage, a clock and a send function of
 * the test's own: how many events it keeps and for how long, what each
 * answer does to a call's events, how the calls are cut, and that an event
 * read back is sent only in its one form.
 */
import { DETAIL_KEYS, detailKept } from '../src/details';
import { KEYS } from '../src/keys';
import {
  chunks,
  createOutbox,
  eventFrom,
  MAX_CHUNK_BYTES,
  MAX_CHUNK_EVENTS,
  MAX_OUTBOX,
  OUTBOX_MS,
  readOutbox,
  type GoalEvent,
} from '../src/outbox';
import type { Send, SendOptions } from '../src/send';
import { SERVER, VOCABULARY } from './vectors';

const BATCH = '/api/v1/tracking/batch';

/** A Storage held in memory. */
function memoryStorage(): Storage {
  const items = new Map<string, string>();
  return {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: (key: string) => (items.has(key) ? items.get(key)! : null),
    key: (index: number) => Array.from(items.keys())[index] ?? null,
    removeItem: (key: string) => {
      items.delete(key);
    },
    setItem: (key: string, value: string) => {
      items.set(key, String(value));
    },
  } as Storage;
}

interface Call {
  path: string;
  events: GoalEvent[];
  options: SendOptions | undefined;
  answer(status: number): void;
  fail(): void;
}

/** A send function that records each call and leaves it pending until the test answers it. */
function recorder(): { send: Send; calls: Call[] } {
  const calls: Call[] = [];
  const send: Send = (path, body, options) => {
    let answer: (status: number) => void = () => undefined;
    let fail: () => void = () => undefined;
    const request = new Promise<Response>((resolve, reject) => {
      answer = (status) => resolve({ ok: status >= 200 && status < 300, status } as Response);
      fail = () => reject(new TypeError('Failed to fetch'));
    });
    calls.push({ path, events: (body as { events: GoalEvent[] }).events, options, answer, fail });
    return request;
  };
  return { send, calls };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/** A page-view id for *n*: a version 4 UUID. */
const pageView = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`;

function goalEvent(n: number, extra: Partial<GoalEvent> = {}): GoalEvent {
  return { event_type: 'click_buy', user_id: 'visitor-1', experiment_key: 'headline', event_id: `${pageView(n)}:click_buy`, ...extra };
}

/** Website details at their longest. */
const LONGEST = {
  web_device: 'desktop',
  web_utm_source: 's'.repeat(100),
  web_utm_medium: 'm'.repeat(100),
  web_utm_campaign: 'c'.repeat(100),
  web_referrer: `${'r'.repeat(245)}.example`,
  web_landing_path: `/${'p'.repeat(254)}`,
};

/** A goal event as long as the script makes one. */
const longest = (n: number) => goalEvent(n, { user_id: 'u'.repeat(255), experiment_key: 'k'.repeat(100), metadata: LONGEST });

function setup(now = 1_800_000_000_000) {
  const storage = memoryStorage();
  const clock = { now };
  const { send, calls } = recorder();
  const outbox = createOutbox({ local: () => storage, now: () => clock.now, send, path: BATCH, allowed: () => true });
  const kept = () => readOutbox(storage, clock.now).map((entry) => entry.event.event_id);
  return { storage, clock, calls, outbox, kept };
}

describe('the outbox', () => {
  test('keeps at most 50 events, the newest, when nothing gets through', async () => {
    const { calls, outbox, kept } = setup();
    for (let n = 0; n < 1000; n += 1) outbox.add([goalEvent(n)]);
    for (const call of calls) call.fail();
    await settle();
    expect(kept().length).toBe(MAX_OUTBOX);
    expect(kept()).toEqual(Array.from({ length: 50 }, (_, i) => `${pageView(950 + i)}:click_buy`));
  });

  test('drops an event 24 hours after it was made', async () => {
    const { calls, clock, outbox, kept } = setup();
    outbox.add([goalEvent(1)]);
    calls[0].fail();
    await settle();
    clock.now += OUTBOX_MS - 1;
    expect(kept()).toEqual([`${pageView(1)}:click_buy`]);
    clock.now += 2;
    expect(kept()).toEqual([]);
    outbox.flush();
    expect(calls).toHaveLength(1);
  });

  test('sends each new event at once through the one send function, with the key and keepalive', () => {
    const { calls, outbox } = setup();
    outbox.add([goalEvent(1), goalEvent(2)]);
    expect(calls.map((call) => [call.path, call.events.length, call.options])).toEqual([[BATCH, 2, { apiKey: true, keepalive: true }]]);
    expect(calls[0].events).toEqual([goalEvent(1), goalEvent(2)]);
  });

  test('a 2xx removes the call\'s events', async () => {
    const { calls, outbox, kept, storage } = setup();
    outbox.add([goalEvent(1)]);
    calls[0].answer(200);
    await settle();
    expect(kept()).toEqual([]);
    expect(storage.getItem(KEYS.outbox)).toBeNull();
    outbox.flush();
    expect(calls).toHaveLength(1);
  });

  test.each([400, 401, 403, 404, 413, 422])('a %i drops the call\'s events, and they are never sent again', async (status) => {
    const { calls, outbox, kept } = setup();
    outbox.add([goalEvent(1)]);
    calls[0].answer(status);
    await settle();
    outbox.flush();
    outbox.add([goalEvent(2)]);
    const resent = calls.slice(1).filter((call) => call.events.some((event) => event.event_id === goalEvent(1).event_id)).length;
    expect(`re-sent ${resent}`).toBe('re-sent 0');
    expect(kept()).toEqual([`${pageView(2)}:click_buy`]);
  });

  test.each([
    ['a 429', (call: Call) => call.answer(429)],
    ['a 503', (call: Call) => call.answer(503)],
    ['a 500', (call: Call) => call.answer(500)],
    ['a network failure', (call: Call) => call.fail()],
  ])('%s keeps the call\'s events, and the next flush sends them again exactly once', async (_name, answer) => {
    const { calls, outbox, kept } = setup();
    outbox.add([goalEvent(1)]);
    answer(calls[0]);
    await settle();
    expect(kept()).toEqual([`${pageView(1)}:click_buy`]);
    expect(calls).toHaveLength(1);
    outbox.flush();
    // Flushed again while that call is on its way: nothing more is sent.
    outbox.flush();
    expect(calls.map((call) => call.events.map((event) => event.event_id))).toEqual([
      [goalEvent(1).event_id],
      [goalEvent(1).event_id],
    ]);
    calls[1].answer(202);
    await settle();
    expect(kept()).toEqual([]);
    outbox.flush();
    expect(calls).toHaveLength(2);
  });

  test('a new event while a call is on its way is sent in a call of its own', async () => {
    const { calls, outbox } = setup();
    outbox.add([goalEvent(1)]);
    outbox.add([goalEvent(2)]);
    expect(calls.map((call) => call.events.map((event) => event.event_id))).toEqual([[goalEvent(1).event_id], [goalEvent(2).event_id]]);
  });

  test('cuts calls at 100 events or 60 KiB of JSON, whichever comes first', () => {
    const small = Array.from({ length: 150 }, (_, n) => goalEvent(n));
    expect(chunks(small).map((call) => call.length)).toEqual([MAX_CHUNK_EVENTS, 50]);
    const large = Array.from({ length: 150 }, (_, n) => longest(n));
    const calls = chunks(large);
    expect(calls.reduce((total, call) => total + call.length, 0)).toBe(150);
    for (const call of calls) {
      expect(call.length).toBeLessThanOrEqual(MAX_CHUNK_EVENTS);
      expect(Buffer.byteLength(JSON.stringify({ events: call }), 'utf8')).toBeLessThanOrEqual(MAX_CHUNK_BYTES);
    }
    // Each call is as full as the limit lets it be.
    const size = Buffer.byteLength(JSON.stringify(large[0]), 'utf8');
    expect(calls[0].length).toBe(Math.floor((MAX_CHUNK_BYTES - '{"events":[]}'.length + 1) / (size + 1)));
  });

  test('sends 50 kept events over 60 KiB as two calls, the second once the first is answered', async () => {
    const { calls, outbox, kept } = setup();
    const big = Array.from({ length: 50 }, (_, n) => longest(n));
    outbox.add(big);
    expect(calls).toHaveLength(1);
    const first = calls[0].events.length;
    expect(first).toBeLessThan(50);
    expect(Buffer.byteLength(JSON.stringify({ events: calls[0].events }), 'utf8')).toBeLessThanOrEqual(MAX_CHUNK_BYTES);
    calls[0].answer(200);
    await settle();
    expect(calls).toHaveLength(2);
    expect(calls[1].events.length).toBe(50 - first);
    calls[1].answer(200);
    await settle();
    expect(kept()).toEqual([]);
  });

  test('a 2xx that counts some events as failed or duplicate still removes them all', async () => {
    const { calls, outbox, kept } = setup();
    outbox.add([goalEvent(1), goalEvent(2)]);
    calls[0].answer(200);
    await settle();
    expect(kept()).toEqual([]);
  });

  test('reads, stores and sends nothing while the script may not', () => {
    const storage = memoryStorage();
    storage.setItem(KEYS.outbox, JSON.stringify([{ at: 1, event: goalEvent(1) }]));
    const reads: string[] = [];
    const watched = { ...storage, getItem: (key: string) => (reads.push(key), storage.getItem(key)), setItem: storage.setItem, removeItem: storage.removeItem } as Storage;
    const { send, calls } = recorder();
    const outbox = createOutbox({ local: () => watched, now: () => 2, send, path: BATCH, allowed: () => false });
    outbox.flush();
    outbox.add([goalEvent(2)]);
    expect([reads, calls.length]).toEqual([[], 0]);
  });
});

describe('an event read back', () => {
  const good = goalEvent(7, { metadata: { web_device: 'phone', web_utm_source: 'news' } });

  test('is sent exactly as it was kept when it is in its form', () => {
    expect(eventFrom(JSON.parse(JSON.stringify(good)))).toEqual(good);
    expect(eventFrom(goalEvent(7))).toEqual(goalEvent(7));
  });

  test.each([
    ['an extra field', { ...good, value: 3 }],
    ['an event_id for another name', { ...good, event_id: `${pageView(7)}:click_other` }],
    ['an event_id that is not a page-view id', { ...good, event_id: 'abc:click_buy' }],
    ['an event name out of form', { ...good, event_type: 'Click Buy', event_id: `${pageView(7)}:Click Buy` }],
    ['a user id of 256 characters', { ...good, user_id: 'u'.repeat(256) }],
    ['an experiment key of 101 characters', { ...good, experiment_key: 'k'.repeat(101) }],
    ['a detail the script never sends', { ...good, metadata: { plan: 'pro' } }],
    ['a detail out of its form', { ...good, metadata: { web_utm_source: 'a@b.c' } }],
    ['the address in a detail', { ...good, metadata: { web_referrer: 'https://www.google.com/search?q=1' } }],
    ['empty details', { ...good, metadata: {} }],
  ])('is dropped with %s', (_name, value) => {
    expect(eventFrom(value)).toBeNull();
  });

  test('of a kept list that fails is dropped alone, and a list in another shape is no list', () => {
    const storage = memoryStorage();
    storage.setItem(KEYS.outbox, JSON.stringify([{ at: 5, event: goalEvent(1) }, { at: 5, event: { ...goalEvent(2), extra: 1 } }, { at: 'x', event: goalEvent(3) }, { at: 6, event: goalEvent(4) }]));
    expect(readOutbox(storage, 10).map((entry) => entry.event.event_id)).toEqual([goalEvent(1).event_id, goalEvent(4).event_id]);
    storage.setItem(KEYS.outbox, JSON.stringify({ at: 5, event: goalEvent(1) }));
    expect(readOutbox(storage, 10)).toEqual([]);
    storage.setItem(KEYS.outbox, '{not json');
    expect(readOutbox(storage, 10)).toEqual([]);
  });

  test("its details are checked with the forms the server keeps: every detail vector agrees", () => {
    expect([...DETAIL_KEYS]).toEqual(VOCABULARY.details.map((detail: { key: string }) => detail.key));
    const disagree = SERVER.details
      .filter((vector: { key: string; value: unknown; kept: boolean }) => detailKept(vector.key, vector.value) !== vector.kept)
      .map((vector: { id: string }) => vector.id);
    expect(disagree).toEqual([]);
    expect(SERVER.details.length).toBeGreaterThan(50);
  });
});
