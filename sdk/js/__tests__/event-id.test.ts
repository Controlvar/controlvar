/**
 * `eventId` (#1543): the caller's id goes out as `event_id` on `/track` and on every fan-out
 * entry, is left out when not given, and is the same on a 429 retry. The SDK makes no id.
 */
import { ExperimentationClient } from '../src/client';
import type { AssignResponse, ClientConfig } from '../src/types';

const baseConfig: ClientConfig = { apiUrl: 'https://api.example.com', apiKey: 'test-api-key' };

const assignment: AssignResponse = {
  experiment_key: 'checkout',
  user_id: 'user-123',
  variant_id: 'var-2',
  variant_name: 'one_page',
  is_control: false,
  configuration: null,
};
const flagOn = { key: 'my-flag', enabled: true, config: null };

interface Step {
  body?: unknown;
  status?: number;
  headers?: Record<string, string>;
}

function jsonResponse(step: Step) {
  const status = step.status ?? 200;
  const headers = step.headers ?? {};
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => headers[name] ?? headers[name.toLowerCase()] ?? null },
    json: () => Promise.resolve(step.body ?? {}),
  };
}

function mockFetchSequence(...steps: Step[]): jest.Mock {
  const mock = jest.fn();
  for (const step of steps) mock.mockResolvedValueOnce(jsonResponse(step));
  global.fetch = mock;
  return mock;
}

function bodyOf(mock: jest.Mock, index: number) {
  const init = mock.mock.calls[index][1];
  return JSON.parse(init.body);
}

/** A client that has answered for user-123 in one experiment and one flag, so a key-less event fans out to both. */
async function fannedOutClient(): Promise<{ client: ExperimentationClient; fetchMock: jest.Mock }> {
  const fetchMock = mockFetchSequence({ body: assignment }, { body: flagOn });
  const client = new ExperimentationClient(baseConfig);
  await client.getAssignment('checkout', { userId: 'user-123' });
  await client.evaluateFlag('my-flag', { userId: 'user-123' });
  fetchMock.mockReset();
  return { client, fetchMock };
}

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
});

describe('eventId', () => {
  it('sends event_id on a track with an explicit key', async () => {
    const fetchMock = mockFetchSequence({ body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.track('user-123', 'purchase', { experimentKey: 'checkout', eventId: 'order-42' });
    expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/api/v1/tracking/track');
    expect(bodyOf(fetchMock, 0)).toEqual({
      event_type: 'purchase',
      event_name: 'purchase',
      user_id: 'user-123',
      experiment_key: 'checkout',
      event_id: 'order-42',
    });
  });

  it('sends event_id on a track with nothing to attribute it to', async () => {
    const fetchMock = mockFetchSequence({ body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.track('nobody', 'page_view', { eventId: 'view-1' });
    expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/api/v1/tracking/track');
    expect(bodyOf(fetchMock, 0).event_id).toBe('view-1');
  });

  it('sends the same event_id on every fan-out entry', async () => {
    const { client, fetchMock } = await fannedOutClient();
    fetchMock.mockResolvedValueOnce(jsonResponse({ body: {} }));
    await client.track('user-123', 'purchase', { value: 5, eventId: 'order-42' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/api/v1/tracking/batch');
    const { events } = bodyOf(fetchMock, 0);
    expect(events).toHaveLength(2);
    expect(events.map((e: Record<string, unknown>) => e.experiment_key ?? e.feature_flag_key)).toEqual([
      'checkout',
      'my-flag',
    ]);
    for (const entry of events) expect(entry.event_id).toBe('order-42');
  });

  it('sends no event_id on any entry when none is given', async () => {
    const { client, fetchMock } = await fannedOutClient();
    fetchMock.mockResolvedValueOnce(jsonResponse({ body: {} }));
    await client.track('user-123', 'purchase');
    const { events } = bodyOf(fetchMock, 0);
    expect(events).toHaveLength(2);
    for (const entry of events) expect(entry).not.toHaveProperty('event_id');
  });

  it('sends no event_id for undefined, null or an empty string', async () => {
    for (const given of [undefined, null, '']) {
      const fetchMock = mockFetchSequence({ body: {} });
      const client = new ExperimentationClient(baseConfig);
      await client.track('user-123', 'purchase', {
        experimentKey: 'checkout',
        eventId: given as unknown as string | undefined,
      });
      expect(bodyOf(fetchMock, 0)).not.toHaveProperty('event_id');
    }
  });

  it('makes no id of its own: two tracks without an eventId send none', async () => {
    const fetchMock = mockFetchSequence({ body: {} }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.track('user-123', 'purchase', { experimentKey: 'checkout' });
    await client.track('user-123', 'purchase', { experimentKey: 'checkout' });
    expect(bodyOf(fetchMock, 0)).not.toHaveProperty('event_id');
    expect(bodyOf(fetchMock, 1)).not.toHaveProperty('event_id');
  });

  it('trackBatch: each event keeps its own event_id on every entry, and one without has none', async () => {
    const { client, fetchMock } = await fannedOutClient();
    fetchMock.mockResolvedValueOnce(jsonResponse({ body: { success_count: 4, failure_count: 0 } }));
    await client.trackBatch([
      { userId: 'user-123', eventName: 'purchase', eventId: 'order-1' },
      { userId: 'user-123', eventName: 'refund', eventId: 'refund-1' },
      { userId: 'user-123', eventName: 'click', experimentKey: 'checkout' },
    ]);
    const { events } = bodyOf(fetchMock, 0);
    expect(events).toHaveLength(5);
    expect(events.filter((e: Record<string, unknown>) => e.event_name === 'purchase').map((e: Record<string, unknown>) => e.event_id)).toEqual(['order-1', 'order-1']);
    expect(events.filter((e: Record<string, unknown>) => e.event_name === 'refund').map((e: Record<string, unknown>) => e.event_id)).toEqual(['refund-1', 'refund-1']);
    expect(events.find((e: Record<string, unknown>) => e.event_name === 'click')).not.toHaveProperty('event_id');
  });

  it('sends the same event_id when a 429 is retried', async () => {
    const fetchMock = mockFetchSequence({ status: 429, headers: { 'Retry-After': '0' } }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.track('user-123', 'purchase', { experimentKey: 'checkout', eventId: 'order-42' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(bodyOf(fetchMock, 0).event_id).toBe('order-42');
    expect(bodyOf(fetchMock, 1).event_id).toBe('order-42');
  });

  it('does not send a request that timed out again, and reports it through onError', async () => {
    jest.useFakeTimers();
    const fetchMock = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
      return new Promise((_resolve, reject) => {
        (init.signal as AbortSignal).addEventListener('abort', () => reject(new Error('The operation was aborted')));
      });
    });
    global.fetch = fetchMock;
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, timeoutMs: 50, onError });
    const pending = client.track('user-123', 'purchase', { experimentKey: 'checkout', eventId: 'order-42' });
    await jest.advanceTimersByTimeAsync(51);
    await expect(pending).resolves.toBeUndefined();
    await jest.advanceTimersByTimeAsync(10_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: 'TIMEOUT' }), 'track');
    jest.useRealTimers();
  });

  it('sends the same event_id on every entry when a fanned-out 429 is retried', async () => {
    const { client, fetchMock } = await fannedOutClient();
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ status: 429, headers: { 'Retry-After': '0' } }))
      .mockResolvedValueOnce(jsonResponse({ body: {} }));
    await client.track('user-123', 'purchase', { eventId: 'order-42' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const index of [0, 1]) {
      const { events } = bodyOf(fetchMock, index);
      expect(events.map((e: Record<string, unknown>) => e.event_id)).toEqual(['order-42', 'order-42']);
    }
  });
});
