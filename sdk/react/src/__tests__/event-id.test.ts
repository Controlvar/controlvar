/**
 * `event_id` (#1543): the SDK sends one id per `trackEvent` call, so the server stores an event
 * sent again with the same id once. `fetch` is mocked; the live case (one event sent twice with
 * one id is one stored row) is `examples/contract_smoke.mjs` against a real server.
 */
import { ExperimentationClient } from '../client/ExperimentationClient';
import { eventIdFor, newEventId } from '../client/eventId';
import { UserContext } from '../client/types';

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const config = { apiKey: 'test-api-key', baseUrl: 'https://api.example.com' };
const user: UserContext = { userId: 'user-123' };

type Entry = { event_id?: string; experiment_key?: string; feature_flag_key?: string };

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

function mockFetch(): jest.Mock {
  const mock = jest.fn().mockResolvedValue(jsonResponse({}));
  global.fetch = mock;
  return mock;
}

/** The entries of the `index`th request: a batch's events, or the single track body. */
function entriesAt(mock: jest.Mock, index: number): Entry[] {
  const body = JSON.parse(mock.mock.calls[index][1].body);
  return Array.isArray(body.events) ? body.events : [body];
}

/** A client answered for one experiment and two flags, so a key-less event fans out to three entries. */
async function primedClient(mock: jest.Mock): Promise<ExperimentationClient> {
  const client = new ExperimentationClient(config);
  mock.mockResolvedValueOnce(
    jsonResponse({ experiment_key: 'exp-a', user_id: 'user-123', variant_id: 'v1', variant_name: 'control', is_control: true, configuration: null })
  );
  mock.mockResolvedValueOnce(jsonResponse({ key: 'flag-a', enabled: true, config: null }));
  mock.mockResolvedValueOnce(jsonResponse({ key: 'flag-b', enabled: false, config: null }));
  await client.assignExperiment(user, 'exp-a');
  await client.evaluateFeatureFlag(user, 'flag-a');
  await client.evaluateFeatureFlag(user, 'flag-b');
  mock.mockClear();
  return client;
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe('event ids (#1543)', () => {
  it('sends a generated lowercase UUID v4 as event_id on a keyed track', async () => {
    const fetchMock = mockFetch();
    await new ExperimentationClient(config).trackEvent('user-123', 'purchase', undefined, { experimentKey: 'exp-a' });
    expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/api/v1/tracking/track');
    expect(entriesAt(fetchMock, 0)[0].event_id).toMatch(UUID_V4);
  });

  it('sends a generated id when there is nothing to attribute the event to', async () => {
    const fetchMock = mockFetch();
    await new ExperimentationClient(config).trackEvent('user-123', 'page_view');
    expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/api/v1/tracking/track');
    expect(entriesAt(fetchMock, 0)[0].event_id).toMatch(UUID_V4);
  });

  it('two events get two different ids', async () => {
    const fetchMock = mockFetch();
    const client = new ExperimentationClient(config);
    await client.trackEvent('user-123', 'purchase', undefined, { experimentKey: 'exp-a' });
    await client.trackEvent('user-123', 'purchase', undefined, { experimentKey: 'exp-a' });
    const first = entriesAt(fetchMock, 0)[0].event_id;
    const second = entriesAt(fetchMock, 1)[0].event_id;
    expect(first).toMatch(UUID_V4);
    expect(second).toMatch(UUID_V4);
    expect(first).not.toBe(second);
  });

  it('a key-less trackEvent sends one id on every fan-out entry', async () => {
    const fetchMock = mockFetch();
    const client = await primedClient(fetchMock);
    await client.trackEvent('user-123', 'page_view');
    const entries = entriesAt(fetchMock, 0);
    expect(entries.map(e => e.experiment_key ?? `flag:${e.feature_flag_key}`)).toEqual(['exp-a', 'flag:flag-a', 'flag:flag-b']);
    expect(entries[0].event_id).toMatch(UUID_V4);
    expect(new Set(entries.map(e => e.event_id)).size).toBe(1);
  });

  it("sends the caller's eventId as it is, on a keyed track and on every fan-out entry", async () => {
    const fetchMock = mockFetch();
    const client = await primedClient(fetchMock);
    await client.trackEvent('user-123', 'purchase', undefined, { experimentKey: 'exp-a', eventId: 'order-1001' });
    expect(entriesAt(fetchMock, 0)[0].event_id).toBe('order-1001');
    await client.trackEvent('user-123', 'purchase', undefined, { eventId: 'order-1002' });
    expect(entriesAt(fetchMock, 1).map(e => e.event_id)).toEqual(['order-1002', 'order-1002', 'order-1002']);
  });

  it('a call made again with the same eventId sends the same id both times', async () => {
    const fetchMock = mockFetch();
    const client = new ExperimentationClient(config);
    for (let i = 0; i < 2; i++) {
      await client.trackEvent('user-123', 'purchase', undefined, { experimentKey: 'exp-a', eventId: 'order-1001' });
    }
    expect(entriesAt(fetchMock, 0)[0].event_id).toBe('order-1001');
    expect(entriesAt(fetchMock, 1)[0].event_id).toBe('order-1001');
  });

  it('a request that fails is not sent again by the SDK', async () => {
    const fetchMock = jest.fn().mockRejectedValue(new Error('network down'));
    global.fetch = fetchMock;
    await expect(
      new ExperimentationClient(config).trackEvent('user-123', 'purchase', undefined, { experimentKey: 'exp-a' })
    ).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('null and an empty string are no id of the caller: the SDK makes one', async () => {
    const fetchMock = mockFetch();
    const client = new ExperimentationClient(config);
    await client.trackEvent('user-123', 'purchase', undefined, { experimentKey: 'exp-a', eventId: null });
    await client.trackEvent('user-123', 'purchase', undefined, { experimentKey: 'exp-a', eventId: '' });
    expect(entriesAt(fetchMock, 0)[0].event_id).toMatch(UUID_V4);
    expect(entriesAt(fetchMock, 1)[0].event_id).toMatch(UUID_V4);
  });

  it('newEventId() falls back to getRandomValues, then to Math.random, and stays a v4 UUID', () => {
    const real = (globalThis as { crypto?: unknown }).crypto;
    try {
      Object.defineProperty(globalThis, 'crypto', {
        configurable: true,
        value: { getRandomValues: (bytes: Uint8Array) => bytes.fill(255) },
      });
      expect(newEventId()).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff');
      Object.defineProperty(globalThis, 'crypto', { configurable: true, value: undefined });
      const ids = new Set(Array.from({ length: 50 }, () => newEventId()));
      expect(ids.size).toBe(50);
      for (const id of ids) expect(id).toMatch(UUID_V4);
    } finally {
      Object.defineProperty(globalThis, 'crypto', { configurable: true, value: real });
    }
  });

  it('eventIdFor() keeps the caller id and replaces undefined, null and an empty string', () => {
    expect(eventIdFor('mine')).toBe('mine');
    for (const none of [undefined, null, '']) expect(eventIdFor(none)).toMatch(UUID_V4);
  });
});
