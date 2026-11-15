/**
 * `event_id` (#1543): the SDK sends one id per event, so the server stores an event sent again
 * with the same id once. `fetch` is mocked; the live case (one event sent twice with one id is
 * one stored row) is `examples/contract_smoke.test.ts` against a real server.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import { ExperimentationClient } from '../src/client';
import { eventIdFor, newEventId } from '../src/eventId';

const BASE_URL = 'https://api.example.com';
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function makeClient(): ExperimentationClient {
  return new ExperimentationClient({
    apiKey: 'test-api-key',
    baseUrl: BASE_URL,
    timeoutMs: 1000,
    cacheTtlMs: 60_000,
    offlineFallback: false,
  });
}

function fetchMock(): jest.Mock {
  return global.fetch as unknown as jest.Mock;
}

function respond(body: unknown, status = 200): void {
  fetchMock().mockResolvedValueOnce({ ok: status >= 200 && status < 300, status, json: async () => body });
}

type Entry = { event_id?: string; experiment_key?: string; feature_flag_key?: string };

/** The parsed body of the `index`th request. */
function bodyAt(index: number): Record<string, unknown> {
  return JSON.parse(fetchMock().mock.calls[index][1].body);
}

/** The entries of the `index`th request: a batch's events, or the single track body. */
function entriesAt(index: number): Entry[] {
  const body = bodyAt(index);
  return Array.isArray(body.events) ? (body.events as Entry[]) : [body as Entry];
}

/** A client that has been answered for one experiment and one flag, so a key-less event fans out to two entries. */
async function primedClient(): Promise<ExperimentationClient> {
  const client = makeClient();
  respond({ experiment_key: 'exp-a', user_id: 'u-1', variant_id: 'v1', variant_name: 'control', is_control: true, configuration: null });
  respond({ key: 'flag-a', enabled: true, config: null });
  await client.getAssignment('exp-a', 'u-1');
  await client.evaluateFlag('flag-a', 'u-1');
  fetchMock().mockClear();
  return client;
}

beforeEach(async () => {
  jest.restoreAllMocks();
  global.fetch = jest.fn();
  await AsyncStorage.clear();
});

describe('event ids (#1543)', () => {
  test('track() with a key sends a generated lowercase UUID v4 as event_id', async () => {
    respond({});
    await makeClient().track('purchase', 'u-1', undefined, { experimentKey: 'exp-a' });
    expect(fetchMock().mock.calls[0][0]).toBe(`${BASE_URL}/api/v1/tracking/track`);
    expect(bodyAt(0).event_id).toMatch(UUID_V4);
  });

  test('two events get two different ids', async () => {
    const client = makeClient();
    respond({});
    respond({});
    await client.track('purchase', 'u-1', undefined, { experimentKey: 'exp-a' });
    await client.track('purchase', 'u-1', undefined, { experimentKey: 'exp-a' });
    expect(bodyAt(0).event_id).toMatch(UUID_V4);
    expect(bodyAt(1).event_id).toMatch(UUID_V4);
    expect(bodyAt(0).event_id).not.toBe(bodyAt(1).event_id);
  });

  test('a key-less track() sends one id on every fan-out entry', async () => {
    const client = await primedClient();
    respond({ success_count: 2, failure_count: 0, errors: null });
    await client.track('page_view', 'u-1');
    const entries = entriesAt(0);
    expect(entries.map((e) => e.experiment_key ?? `flag:${e.feature_flag_key}`)).toEqual(['exp-a', 'flag:flag-a']);
    expect(entries[0].event_id).toMatch(UUID_V4);
    expect(entries[1].event_id).toBe(entries[0].event_id);
  });

  test("trackBatch: each event has its own id, shared by that event's entries", async () => {
    const client = await primedClient();
    respond({ success_count: 3, failure_count: 0, errors: null });
    await client.trackBatch([
      { eventName: 'a', userId: 'u-1' },
      { eventName: 'b', userId: 'u-1', experimentKey: 'exp-a' },
    ]);
    const entries = entriesAt(0);
    expect(entries).toHaveLength(3);
    const [a1, a2, b] = entries.map((e) => e.event_id);
    expect(a1).toMatch(UUID_V4);
    expect(a2).toBe(a1);
    expect(b).toMatch(UUID_V4);
    expect(b).not.toBe(a1);
  });

  test("a caller's eventId is sent as it is, on the track body and on every fan-out entry", async () => {
    const client = await primedClient();
    respond({});
    await client.track('purchase', 'u-1', undefined, { experimentKey: 'exp-a', eventId: 'order-1001' });
    expect(bodyAt(0).event_id).toBe('order-1001');

    respond({ success_count: 2, failure_count: 0, errors: null });
    await client.track('purchase', 'u-1', undefined, { eventId: 'order-1002' });
    expect(entriesAt(1).map((e) => e.event_id)).toEqual(['order-1002', 'order-1002']);

    respond({ success_count: 2, failure_count: 0, errors: null });
    await client.trackBatch([{ eventName: 'purchase', userId: 'u-1', eventId: 'order-1003' }]);
    expect(entriesAt(2).map((e) => e.event_id)).toEqual(['order-1003', 'order-1003']);
  });

  test('a call made again with the same eventId sends the same id both times', async () => {
    const client = makeClient();
    respond({});
    respond({});
    for (let i = 0; i < 2; i++) {
      await client.track('purchase', 'u-1', undefined, { experimentKey: 'exp-a', eventId: 'order-1001' });
    }
    expect(bodyAt(0).event_id).toBe('order-1001');
    expect(bodyAt(1).event_id).toBe('order-1001');
  });

  test('a request that fails is not sent again by the SDK', async () => {
    const errors: string[] = [];
    const client = new ExperimentationClient({
      apiKey: 'test-api-key',
      baseUrl: BASE_URL,
      timeoutMs: 1000,
      offlineFallback: false,
      onError: (error) => void errors.push(error.message),
    });
    fetchMock().mockRejectedValueOnce(new Error('network down'));
    await client.track('purchase', 'u-1', undefined, { experimentKey: 'exp-a' });
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(errors).toEqual(['network down']);
  });

  test('null and an empty string are no id of the caller: the SDK makes one', async () => {
    const client = makeClient();
    respond({});
    respond({});
    await client.track('purchase', 'u-1', undefined, { experimentKey: 'exp-a', eventId: null });
    await client.track('purchase', 'u-1', undefined, { experimentKey: 'exp-a', eventId: '' });
    expect(bodyAt(0).event_id).toMatch(UUID_V4);
    expect(bodyAt(1).event_id).toMatch(UUID_V4);
  });

  test('newEventId() falls back to getRandomValues, then to Math.random, and stays a v4 UUID', () => {
    const real = (globalThis as { crypto?: unknown }).crypto;
    try {
      (globalThis as { crypto?: unknown }).crypto = {
        getRandomValues: (bytes: Uint8Array) => bytes.fill(255),
      };
      expect(newEventId()).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff');
      (globalThis as { crypto?: unknown }).crypto = undefined;
      const ids = new Set(Array.from({ length: 50 }, () => newEventId()));
      expect(ids.size).toBe(50);
      for (const id of ids) expect(id).toMatch(UUID_V4);
    } finally {
      (globalThis as { crypto?: unknown }).crypto = real;
    }
  });

  test('eventIdFor() keeps the caller id and replaces undefined, null and an empty string', () => {
    expect(eventIdFor('mine')).toBe('mine');
    for (const none of [undefined, null, '']) expect(eventIdFor(none)).toMatch(UUID_V4);
  });
});
