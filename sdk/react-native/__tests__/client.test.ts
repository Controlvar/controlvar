/**
 * Unit tests for ExperimentationClient against the real backend contract.
 *
 * `fetch` is mocked; AsyncStorage is the in-memory `__mocks__` implementation.
 * Endpoints under test:
 *   POST /api/v1/tracking/assign
 *   GET  /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=…
 *   GET  /api/v1/feature-flags/user/{user_id}
 *   POST /api/v1/tracking/track
 *   POST /api/v1/tracking/batch
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import { ApiError, ExperimentationClient, FlagValueError } from '../src/client';
import { OfflineStorage } from '../src/storage';
import type { Assignment, FlagEvaluation, SdkConfig, TrackEvent } from '../src/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const BASE_URL = 'https://api.example.com';
const API_KEY = 'test-api-key';
const HEADERS = {
  'X-API-Key': API_KEY,
  'Content-Type': 'application/json',
  Accept: 'application/json',
};

function makeConfig(overrides: Partial<SdkConfig> = {}): SdkConfig {
  return {
    apiKey: API_KEY,
    baseUrl: BASE_URL,
    timeoutMs: 1000,
    cacheTtlMs: 60_000,
    offlineFallback: false,
    ...overrides,
  };
}

function makeClient(overrides: Partial<SdkConfig> = {}, storage?: OfflineStorage): ExperimentationClient {
  return new ExperimentationClient(makeConfig(overrides), storage);
}

interface MockResponse {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}

function jsonResponse(body: unknown, status = 200): MockResponse {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function assignBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    experiment_key: 'checkout-experiment',
    user_id: 'user-1',
    variant_id: 'var-treatment',
    variant_name: 'treatment',
    is_control: false,
    configuration: { color: 'blue' },
    ...overrides,
  };
}

function flagBody(key = 'dark-mode', enabled = true, config: unknown = { theme: 'dark' }): Record<string, unknown> {
  return { key, enabled, config };
}

function fetchMock(): jest.Mock {
  return global.fetch as unknown as jest.Mock;
}

/** Queue one response for the next fetch call. */
function mockFetchOnce(body: unknown, status = 200): void {
  fetchMock().mockResolvedValueOnce(jsonResponse(body, status));
}

/** Make every fetch call resolve with the same response. */
function mockFetchAlways(body: unknown, status = 200): void {
  fetchMock().mockResolvedValue(jsonResponse(body, status));
}

function mockFetchError(message = 'Network error'): void {
  fetchMock().mockRejectedValueOnce(new Error(message));
}

interface CapturedCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
  rawBody: string | undefined;
}

function callAt(index: number): CapturedCall {
  const [url, init] = fetchMock().mock.calls[index] as [string, { method: string; headers: Record<string, string>; body?: string }];
  return {
    url,
    method: init.method,
    headers: init.headers,
    body: init.body === undefined ? undefined : JSON.parse(init.body),
    rawBody: init.body,
  };
}

function calls(): CapturedCall[] {
  return fetchMock().mock.calls.map((_, i) => callAt(i));
}

beforeEach(async () => {
  jest.restoreAllMocks();
  global.fetch = jest.fn();
  await AsyncStorage.clear();
  // The AsyncStorage mock is module-level: keep its implementation, drop its call history.
  for (const fn of Object.values(AsyncStorage)) {
    if (jest.isMockFunction(fn)) fn.mockClear();
  }
});

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------

describe('ExperimentationClient — constructor', () => {
  test('constructs without error with valid config', () => {
    expect(() => makeClient()).not.toThrow();
  });

  test('throws if apiKey is missing', () => {
    expect(() => new ExperimentationClient({ ...makeConfig(), apiKey: '' })).toThrow('apiKey is required');
  });

  test('throws if baseUrl is missing', () => {
    expect(() => new ExperimentationClient({ ...makeConfig(), baseUrl: '' })).toThrow('baseUrl is required');
  });

  test('trailing slashes on baseUrl are stripped before appending /api/v1', async () => {
    mockFetchOnce(flagBody());
    const client = makeClient({ baseUrl: 'https://api.example.com//' });
    await client.evaluateFlag('dark-mode', 'user-1');
    expect(callAt(0).url).toBe('https://api.example.com/api/v1/feature-flags/evaluate/dark-mode?user_id=user-1');
  });
});

// ---------------------------------------------------------------------------
// getAssignment — POST /api/v1/tracking/assign
// ---------------------------------------------------------------------------

describe('getAssignment', () => {
  test('POSTs to /api/v1/tracking/assign with the contract headers and body', async () => {
    mockFetchOnce(assignBody());
    const client = makeClient();

    await client.getAssignment('checkout-experiment', 'user-1');

    const call = callAt(0);
    expect(call.url).toBe(`${BASE_URL}/api/v1/tracking/assign`);
    expect(call.method).toBe('POST');
    expect(call.headers).toEqual(HEADERS);
    expect(call.body).toEqual({ experiment_key: 'checkout-experiment', user_id: 'user-1' });
  });

  test('sends the user attributes as `context`', async () => {
    mockFetchOnce(assignBody());
    const client = makeClient();

    await client.getAssignment('checkout-experiment', 'user-1', { plan: 'pro', country: 'DE' });

    expect(callAt(0).body).toEqual({
      experiment_key: 'checkout-experiment',
      user_id: 'user-1',
      context: { plan: 'pro', country: 'DE' },
    });
  });

  test('omits `context` entirely when no attributes are given', async () => {
    mockFetchOnce(assignBody());
    await makeClient().getAssignment('checkout-experiment', 'user-1');
    expect(callAt(0).body).not.toHaveProperty('context');
  });

  test('maps the snake_case response to a camelCase Assignment', async () => {
    mockFetchOnce(assignBody());
    const result = await makeClient().getAssignment('checkout-experiment', 'user-1');

    expect(result).toEqual<Assignment>({
      experimentKey: 'checkout-experiment',
      userId: 'user-1',
      variantId: 'var-treatment',
      variantName: 'treatment',
      isControl: false,
      configuration: { color: 'blue' },
    });
  });

  test('maps a control assignment with null configuration and missing optional fields', async () => {
    mockFetchOnce({ experiment_key: 'checkout-experiment', user_id: 'user-1', variant_name: 'control', is_control: true });
    const result = await makeClient().getAssignment('checkout-experiment', 'user-1');

    expect(result).toEqual<Assignment>({
      experimentKey: 'checkout-experiment',
      userId: 'user-1',
      variantId: null,
      variantName: 'control',
      isControl: true,
      configuration: null,
    });
  });

  test('sticky: a second call for the same user + experiment is served from cache (one fetch)', async () => {
    mockFetchAlways(assignBody());
    const client = makeClient();

    const first = await client.getAssignment('checkout-experiment', 'user-1');
    const second = await client.getAssignment('checkout-experiment', 'user-1');

    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(second).toEqual(first);
  });

  test('cache is keyed on user + experiment', async () => {
    mockFetchAlways(assignBody());
    const client = makeClient();

    await client.getAssignment('checkout-experiment', 'user-1');
    await client.getAssignment('checkout-experiment', 'user-2');
    await client.getAssignment('other-experiment', 'user-1');

    expect(fetchMock()).toHaveBeenCalledTimes(3);
  });

  test('concurrent calls for the same key share one request', async () => {
    mockFetchAlways(assignBody());
    const client = makeClient();

    const results = await Promise.all([
      client.getAssignment('checkout-experiment', 'user-1'),
      client.getAssignment('checkout-experiment', 'user-1'),
      client.getAssignment('checkout-experiment', 'user-1'),
    ]);

    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(results.map((r) => r?.variantName)).toEqual(['treatment', 'treatment', 'treatment']);
  });

  test('404 (no experiment has the key) → null, reported to onError, and not cached', async () => {
    const onError = jest.fn();
    mockFetchAlways({ detail: "No experiment has the key 'missing'." }, 404);
    const client = makeClient({ onError });

    expect(await client.getAssignment('missing', 'user-1')).toBeNull();
    expect(await client.getAssignment('missing', 'user-1')).toBeNull();

    expect(fetchMock()).toHaveBeenCalledTimes(2); // failure was not cached
    expect(onError).toHaveBeenCalledTimes(2);
    const [err, op] = onError.mock.calls[0];
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
    expect(op).toBe('getAssignment');
  });

  test('network error → null (no offline fallback)', async () => {
    mockFetchError('Network error');
    expect(await makeClient().getAssignment('checkout-experiment', 'user-1')).toBeNull();
  });

  test('response without variant_name → null and reported', async () => {
    const onError = jest.fn();
    mockFetchOnce({ experiment_key: 'checkout-experiment', user_id: 'user-1' });
    expect(await makeClient({ onError }).getAssignment('checkout-experiment', 'user-1')).toBeNull();
    expect(onError).toHaveBeenCalledWith(expect.any(Error), 'getAssignment');
    expect(onError.mock.calls[0][0].message).toMatch(/variant_name/);
  });

  test('cache entry expires after cacheTtlMs and the server is asked again', async () => {
    mockFetchAlways(assignBody());
    const client = makeClient({ cacheTtlMs: 1000 });
    const now = Date.now();
    const nowSpy = jest.spyOn(Date, 'now').mockReturnValue(now);

    await client.getAssignment('checkout-experiment', 'user-1');
    nowSpy.mockReturnValue(now + 999);
    await client.getAssignment('checkout-experiment', 'user-1');
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    nowSpy.mockReturnValue(now + 1001);
    await client.getAssignment('checkout-experiment', 'user-1');
    expect(fetchMock()).toHaveBeenCalledTimes(2);
  });

  test('getVariant returns the variant name, or null on failure', async () => {
    mockFetchOnce(assignBody({ variant_name: 'control', is_control: true }));
    const client = makeClient();
    expect(await client.getVariant('checkout-experiment', 'user-1')).toBe('control');

    mockFetchOnce({}, 404);
    expect(await client.getVariant('missing', 'user-1')).toBeNull();
  });
});

describe('getAssignment — offline fallback (AsyncStorage)', () => {
  test('persists the assignment as a CacheEntry that OfflineStorage can read back', async () => {
    mockFetchOnce(assignBody());
    const storage = new OfflineStorage();
    const client = makeClient({ offlineFallback: true, cacheTtlMs: 60_000 }, storage);
    const before = Date.now();

    const assignment = await client.getAssignment('checkout-experiment', 'user-1');
    const entry = await storage.getAssignment('user-1', 'checkout-experiment');

    expect(entry).not.toBeNull();
    expect(entry!.value).toEqual(assignment);
    expect(entry!.expiresAt).toBeGreaterThanOrEqual(before + 60_000);
    expect(entry!.expiresAt).toBeLessThanOrEqual(Date.now() + 60_000);
  });

  test('serves an unexpired AsyncStorage entry without calling the server (e.g. after app restart)', async () => {
    const storage = new OfflineStorage();
    const persisted: Assignment = {
      experimentKey: 'checkout-experiment',
      userId: 'user-1',
      variantId: 'v1',
      variantName: 'control',
      isControl: true,
      configuration: null,
    };
    await storage.setAssignment('user-1', 'checkout-experiment', persisted, 60_000);

    const client = makeClient({ offlineFallback: true }, storage);
    expect(await client.getAssignment('checkout-experiment', 'user-1')).toEqual(persisted);
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  test('on API failure returns the last-known assignment even if it has expired', async () => {
    const storage = new OfflineStorage();
    const persisted: Assignment = {
      experimentKey: 'checkout-experiment',
      userId: 'user-1',
      variantId: 'v1',
      variantName: 'treatment',
      isControl: false,
      configuration: { color: 'red' },
    };
    await storage.setAssignment('user-1', 'checkout-experiment', persisted, -1); // already expired
    mockFetchError('Connection refused');

    const client = makeClient({ offlineFallback: true }, storage);
    expect(await client.getAssignment('checkout-experiment', 'user-1')).toEqual(persisted);
    expect(fetchMock()).toHaveBeenCalledTimes(1); // expired entry → server was tried first
  });

  test('on API failure with nothing persisted returns null', async () => {
    mockFetchError('Connection refused');
    const client = makeClient({ offlineFallback: true }, new OfflineStorage());
    expect(await client.getAssignment('checkout-experiment', 'user-1')).toBeNull();
  });

  test('offlineFallback=false never touches AsyncStorage', async () => {
    mockFetchOnce(assignBody());
    await makeClient({ offlineFallback: false }).getAssignment('checkout-experiment', 'user-1');
    expect(AsyncStorage.getItem).not.toHaveBeenCalled();
    expect(AsyncStorage.setItem).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// evaluateFlag — GET /api/v1/feature-flags/evaluate/{key}?user_id=…
// ---------------------------------------------------------------------------

describe('evaluateFlag', () => {
  test('GETs /api/v1/feature-flags/evaluate/{key}?user_id= with the contract headers and no body', async () => {
    mockFetchOnce(flagBody());
    await makeClient().evaluateFlag('dark-mode', 'user-1');

    const call = callAt(0);
    expect(call.url).toBe(`${BASE_URL}/api/v1/feature-flags/evaluate/dark-mode?user_id=user-1`);
    expect(call.method).toBe('GET');
    expect(call.headers).toEqual(HEADERS);
    expect(call.rawBody).toBeUndefined();
  });

  test('URL-encodes the flag key and the user id', async () => {
    mockFetchOnce(flagBody('flag key/ü'));
    await makeClient().evaluateFlag('flag key/ü', 'user@example.com/1');
    expect(callAt(0).url).toBe(
      `${BASE_URL}/api/v1/feature-flags/evaluate/flag%20key%2F%C3%BC?user_id=user%40example.com%2F1`
    );
  });

  test('maps {key, enabled, config}', async () => {
    mockFetchOnce(flagBody('dark-mode', true, { theme: 'dark', contrast: 2 }));
    const result = await makeClient().evaluateFlag('dark-mode', 'user-1');
    expect(result).toEqual<FlagEvaluation>({ key: 'dark-mode', enabled: true, config: { theme: 'dark', contrast: 2 } });
  });

  test('missing config → null; disabled flag → enabled false', async () => {
    mockFetchOnce({ key: 'off-flag', enabled: false });
    expect(await makeClient().evaluateFlag('off-flag', 'user-1')).toEqual({ key: 'off-flag', enabled: false, config: null });
  });

  test('a successful "disabled" evaluation is still cached (one fetch)', async () => {
    mockFetchAlways(flagBody('off-flag', false, null));
    const client = makeClient();
    await client.evaluateFlag('off-flag', 'user-1');
    await client.evaluateFlag('off-flag', 'user-1');
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  test('uses the in-memory cache on the second call', async () => {
    mockFetchAlways(flagBody());
    const client = makeClient();
    await client.evaluateFlag('dark-mode', 'user-1');
    await client.evaluateFlag('dark-mode', 'user-1');
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  test('cache is keyed on (userId, flagKey)', async () => {
    mockFetchAlways(flagBody());
    const client = makeClient();
    await client.evaluateFlag('dark-mode', 'user-A');
    await client.evaluateFlag('dark-mode', 'user-B');
    await client.evaluateFlag('other', 'user-A');
    expect(fetchMock()).toHaveBeenCalledTimes(3);
  });

  test('re-fetches after the TTL expires', async () => {
    mockFetchAlways(flagBody());
    const client = makeClient({ cacheTtlMs: 5000 });
    const now = Date.now();
    const nowSpy = jest.spyOn(Date, 'now').mockReturnValue(now);

    await client.evaluateFlag('dark-mode', 'user-1');
    nowSpy.mockReturnValue(now + 4999);
    await client.evaluateFlag('dark-mode', 'user-1');
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    nowSpy.mockReturnValue(now + 5001);
    await client.evaluateFlag('dark-mode', 'user-1');
    expect(fetchMock()).toHaveBeenCalledTimes(2);
  });

  test('clearCache causes a re-fetch', async () => {
    mockFetchAlways(flagBody());
    const client = makeClient();
    await client.evaluateFlag('dark-mode', 'user-1');
    client.clearCache();
    await client.evaluateFlag('dark-mode', 'user-1');
    expect(fetchMock()).toHaveBeenCalledTimes(2);
  });

  test('404 (unknown flag key) → disabled, reported, and NOT cached', async () => {
    const onError = jest.fn();
    mockFetchAlways({ detail: 'Not found' }, 404);
    const client = makeClient({ onError });

    expect(await client.evaluateFlag('missing', 'user-1')).toEqual({ key: 'missing', enabled: false, config: null });
    expect(await client.evaluateFlag('missing', 'user-1')).toEqual({ key: 'missing', enabled: false, config: null });

    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(client.getEvaluatedFlags('user-1')).toEqual([]);
    expect(onError).toHaveBeenCalledWith(expect.any(ApiError), 'evaluateFlag');
    expect((onError.mock.calls[0][0] as ApiError).status).toBe(404);
  });

  test('500 → disabled (no offline fallback)', async () => {
    mockFetchOnce({}, 500);
    expect((await makeClient().evaluateFlag('dark-mode', 'user-1')).enabled).toBe(false);
  });

  test('401 → disabled', async () => {
    mockFetchOnce({ detail: 'Unauthorized' }, 401);
    expect((await makeClient().evaluateFlag('dark-mode', 'user-1')).enabled).toBe(false);
  });

  test('network error → disabled', async () => {
    mockFetchError('Network error');
    expect(await makeClient().evaluateFlag('dark-mode', 'user-1')).toEqual({ key: 'dark-mode', enabled: false, config: null });
  });

  test('isFeatureEnabled returns the enabled boolean (false on failure)', async () => {
    const client = makeClient();
    mockFetchOnce(flagBody('on', true));
    expect(await client.isFeatureEnabled('on', 'user-1')).toBe(true);
    mockFetchOnce(flagBody('off', false));
    expect(await client.isFeatureEnabled('off', 'user-1')).toBe(false);
    mockFetchError('boom');
    expect(await client.isFeatureEnabled('broken', 'user-1')).toBe(false);
  });

  test('concurrent evaluations for the same key make exactly one request', async () => {
    mockFetchAlways(flagBody());
    const client = makeClient();
    const results = await Promise.all([
      client.evaluateFlag('dark-mode', 'user-1'),
      client.evaluateFlag('dark-mode', 'user-1'),
      client.evaluateFlag('dark-mode', 'user-1'),
    ]);
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(results.every((r) => r.enabled)).toBe(true);
  });

  test('concurrent evaluations for different users each make their own request', async () => {
    mockFetchAlways(flagBody());
    const client = makeClient();
    await Promise.all([
      client.evaluateFlag('dark-mode', 'user-A'),
      client.evaluateFlag('dark-mode', 'user-B'),
      client.evaluateFlag('dark-mode', 'user-C'),
    ]);
    expect(fetchMock()).toHaveBeenCalledTimes(3);
  });

  test('times out after timeoutMs via AbortSignal and returns the disabled default', async () => {
    jest.useFakeTimers();
    try {
      const onError = jest.fn();
      fetchMock().mockImplementation(
        (_url: string, init: { signal: AbortSignal }) =>
          new Promise((_resolve, reject) => {
            init.signal.addEventListener('abort', () => reject(new Error('The operation was aborted')));
          })
      );
      const client = makeClient({ timeoutMs: 250, onError });

      const pending = client.evaluateFlag('dark-mode', 'user-1');
      await jest.advanceTimersByTimeAsync(251);
      const result = await pending;

      expect(result).toEqual({ key: 'dark-mode', enabled: false, config: null });
      expect(onError).toHaveBeenCalledWith(expect.any(Error), 'evaluateFlag');
      expect(onError.mock.calls[0][0].message).toMatch(/aborted/);
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('evaluateFlag — offline fallback (AsyncStorage)', () => {
  test('persists the evaluation as a CacheEntry that OfflineStorage can read back', async () => {
    mockFetchOnce(flagBody('dark-mode', true, { theme: 'dark' }));
    const storage = new OfflineStorage();
    const client = makeClient({ offlineFallback: true, cacheTtlMs: 30_000 }, storage);
    const before = Date.now();

    await client.evaluateFlag('dark-mode', 'user-1');
    const entry = await storage.getFlag('user-1', 'dark-mode');

    expect(entry).toEqual({
      value: { key: 'dark-mode', enabled: true, config: { theme: 'dark' } },
      expiresAt: expect.any(Number),
    });
    expect(entry!.expiresAt).toBeGreaterThanOrEqual(before + 30_000);
    expect(entry!.expiresAt).toBeLessThanOrEqual(Date.now() + 30_000);
  });

  test('serves an unexpired AsyncStorage entry without calling the server', async () => {
    const storage = new OfflineStorage();
    await storage.setFlag('user-1', 'dark-mode', { key: 'dark-mode', enabled: true, config: { a: 1 } }, 60_000);

    const client = makeClient({ offlineFallback: true }, storage);
    expect(await client.evaluateFlag('dark-mode', 'user-1')).toEqual({ key: 'dark-mode', enabled: true, config: { a: 1 } });
    expect(fetchMock()).not.toHaveBeenCalled();
    // ...and it is now hot in memory too
    expect(client.getEvaluatedFlags('user-1')).toEqual(['dark-mode']);
  });

  test('on API failure returns the last-known evaluation even if it has expired', async () => {
    const storage = new OfflineStorage();
    await storage.setFlag('user-1', 'dark-mode', { key: 'dark-mode', enabled: true, config: null }, -1);
    mockFetchError('Connection refused');

    const client = makeClient({ offlineFallback: true }, storage);
    expect(await client.evaluateFlag('dark-mode', 'user-1')).toEqual({ key: 'dark-mode', enabled: true, config: null });
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  // #1519: the kill-switch page says a device that cannot reach the API serves the last answer it
  // stored with no time limit, also after a restart (a new client over the same storage).
  test('on API failure returns the last-known evaluation however long ago it expired', async () => {
    const storage = new OfflineStorage();
    const thirtyDaysMs = 30 * 24 * 60 * 60 * 1000;
    await storage.setFlag('user-1', 'dark-mode', { key: 'dark-mode', enabled: true, config: null }, -thirtyDaysMs);
    mockFetchError('Connection refused');

    const client = makeClient({ offlineFallback: true }, storage);
    expect(await client.evaluateFlag('dark-mode', 'user-1')).toEqual({ key: 'dark-mode', enabled: true, config: null });
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  test('a 404 (deleted flag) answers disabled and removes the stored answer', async () => {
    const storage = new OfflineStorage();
    await storage.setFlag('user-1', 'dark-mode', { key: 'dark-mode', enabled: true, config: { a: 1 } }, -1);
    mockFetchOnce({ detail: 'not found' }, 404);

    const client = makeClient({ offlineFallback: true }, storage);
    expect(await client.evaluateFlag('dark-mode', 'user-1')).toEqual({ key: 'dark-mode', enabled: false, config: null });
    expect(await storage.getFlag('user-1', 'dark-mode')).toBeNull();

    // a later outage has nothing left to serve
    mockFetchError('Connection refused');
    expect((await makeClient({ offlineFallback: true }, storage).evaluateFlag('dark-mode', 'user-1')).enabled).toBe(false);
  });

  test('a 500 still serves the stored answer and keeps it', async () => {
    const storage = new OfflineStorage();
    await storage.setFlag('user-1', 'dark-mode', { key: 'dark-mode', enabled: true, config: null }, -1);
    mockFetchOnce({ detail: 'boom' }, 500);

    const client = makeClient({ offlineFallback: true }, storage);
    expect((await client.evaluateFlag('dark-mode', 'user-1')).enabled).toBe(true);
    expect(await storage.getFlag('user-1', 'dark-mode')).not.toBeNull();
  });

  test('on API failure with nothing persisted returns disabled (and persists nothing)', async () => {
    mockFetchError('Connection refused');
    const storage = new OfflineStorage();
    const client = makeClient({ offlineFallback: true }, storage);

    expect((await client.evaluateFlag('dark-mode', 'user-1')).enabled).toBe(false);
    expect(await storage.getFlag('user-1', 'dark-mode')).toBeNull();
  });

  test('a stale fallback value is not written back to memory (next call retries the server)', async () => {
    const storage = new OfflineStorage();
    await storage.setFlag('user-1', 'dark-mode', { key: 'dark-mode', enabled: true, config: null }, -1);
    mockFetchError('down');
    mockFetchOnce(flagBody('dark-mode', false, null));

    const client = makeClient({ offlineFallback: true }, storage);
    expect((await client.evaluateFlag('dark-mode', 'user-1')).enabled).toBe(true); // stale fallback
    expect((await client.evaluateFlag('dark-mode', 'user-1')).enabled).toBe(false); // server back
    expect(fetchMock()).toHaveBeenCalledTimes(2);
  });

  test('offlineFallback=false never touches AsyncStorage', async () => {
    mockFetchOnce(flagBody());
    await makeClient({ offlineFallback: false }).evaluateFlag('dark-mode', 'user-1');
    expect(AsyncStorage.getItem).not.toHaveBeenCalled();
    expect(AsyncStorage.setItem).not.toHaveBeenCalled();
  });

  test('offlineFallback defaults to true', async () => {
    mockFetchOnce(flagBody());
    const client = new ExperimentationClient({ apiKey: API_KEY, baseUrl: BASE_URL });
    await client.evaluateFlag('dark-mode', 'user-1');
    expect(AsyncStorage.setItem).toHaveBeenCalledTimes(1);
    expect((AsyncStorage.setItem as jest.Mock).mock.calls[0][0]).toBe('ep_sdk_flag:user-1:dark-mode');
  });
});

// ---------------------------------------------------------------------------
// Typed getters — the value a string, number or JSON flag serves (#1810)
// ---------------------------------------------------------------------------

function valueBody(value: unknown, valueType: string, enabled = true): Record<string, unknown> {
  return { key: 'f', enabled, config: null, value, value_type: valueType };
}

describe('typed getters', () => {
  test('each getter reads its type from GET /api/v1/feature-flags/evaluate/{key}, attributes as context', async () => {
    const client = makeClient();
    mockFetchOnce(valueBody('on-copy', 'string'));
    expect(await client.getStringValue('s', 'user-1', 'fb', { plan: 'pro' })).toBe('on-copy');
    mockFetchOnce(valueBody(2.5, 'number'));
    expect(await client.getNumberValue('n', 'user-1', -1)).toBe(2.5);
    mockFetchOnce(valueBody({ color: 'blue' }, 'json'));
    expect(await client.getJsonValue('j', 'user-1', null)).toEqual({ color: 'blue' });
    expect(calls().map((c) => c.url)).toEqual([
      `${BASE_URL}/api/v1/feature-flags/evaluate/s?user_id=user-1&context=%7B%22plan%22%3A%22pro%22%7D`,
      `${BASE_URL}/api/v1/feature-flags/evaluate/n?user_id=user-1`,
      `${BASE_URL}/api/v1/feature-flags/evaluate/j?user_id=user-1`,
    ]);
  });

  test('a flag that is off for the user answers its off value, not the default', async () => {
    mockFetchOnce(valueBody('off-copy', 'string', false));
    expect(await makeClient().getStringValue('f', 'user-1', 'fb')).toBe('off-copy');
  });

  test('an empty string, 0, {} and [] are values, not missing ones', async () => {
    const onError = jest.fn();
    const client = makeClient({ onError });
    mockFetchOnce(valueBody('', 'string'));
    expect(await client.getStringValue('a', 'user-1', 'fb')).toBe('');
    mockFetchOnce(valueBody(0, 'number'));
    expect(await client.getNumberValue('b', 'user-1', -1)).toBe(0);
    mockFetchOnce(valueBody({}, 'json'));
    expect(await client.getJsonValue('c', 'user-1', null)).toEqual({});
    mockFetchOnce(valueBody([], 'json'));
    expect(await client.getJsonValue('d', 'user-1', null)).toEqual([]);
    expect(onError).not.toHaveBeenCalled();
  });

  test('another type answers the default and reports one TYPE_MISMATCH with "value"', async () => {
    const onError = jest.fn();
    mockFetchOnce(valueBody(10, 'number'));
    expect(await makeClient({ onError }).getStringValue('f', 'user-1', 'fb')).toBe('fb');
    expect(onError).toHaveBeenCalledTimes(1);
    const [err, operation] = onError.mock.calls[0];
    expect(operation).toBe('value');
    expect(err).toBeInstanceOf(FlagValueError);
    expect((err as FlagValueError).code).toBe('TYPE_MISMATCH');
    expect((err as FlagValueError).flagKey).toBe('f');
    expect(err.message).toBe('Flag "f" serves a number; getStringValue returned your default');
  });

  test('an answer without a value (a server without flag values) answers the default, INVALID_RESPONSE', async () => {
    const onError = jest.fn();
    mockFetchOnce(flagBody('f', true, null));
    expect(await makeClient({ onError }).getNumberValue('f', 'user-1', -1)).toBe(-1);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][1]).toBe('value');
    expect((onError.mock.calls[0][0] as FlagValueError).code).toBe('INVALID_RESPONSE');
  });

  test.each([
    ['a 503', () => mockFetchOnce({ detail: 'down' }, 503), 503],
    ['a 404 (unknown flag)', () => mockFetchOnce({ detail: 'Not found' }, 404), 404],
  ])('%s answers the default and passes the ApiError itself to onError once, with "value"', async (_label, serve, status) => {
    const onError = jest.fn();
    serve();
    await expect(makeClient({ onError }).getStringValue('f', 'user-1', 'fb')).resolves.toBe('fb');
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toBeInstanceOf(ApiError);
    expect((onError.mock.calls[0][0] as ApiError).status).toBe(status);
    expect(onError.mock.calls[0][1]).toBe('value');
  });

  test('never rejects: a network error answers the default and is reported once', async () => {
    const onError = jest.fn();
    mockFetchError('offline');
    await expect(makeClient({ onError }).getJsonValue('f', 'user-1', ['fb'])).resolves.toEqual(['fb']);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0]).toEqual([expect.objectContaining({ message: 'offline' }), 'value']);
  });

  test('never rejects when onError itself throws', async () => {
    mockFetchOnce(valueBody(10, 'number'));
    const client = makeClient({
      onError: () => {
        throw new Error('handler bug');
      },
    });
    await expect(client.getStringValue('f', 'user-1', 'fb')).resolves.toBe('fb');
  });

  test('a JSON value is a new copy on every read: changing it changes nothing the client keeps', async () => {
    mockFetchOnce(valueBody({ color: 'blue', sizes: [1, 2] }, 'json'));
    const client = makeClient();
    const first = await client.getJsonValue<{ color: string; sizes: number[] }>('f', 'user-1', { color: '', sizes: [] });
    first.color = 'red';
    first.sizes.push(3);
    expect(await client.getJsonValue('f', 'user-1', null)).toEqual({ color: 'blue', sizes: [1, 2] });
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  test('one request answers isFeatureEnabled and then a getter (the same cache)', async () => {
    mockFetchAlways(valueBody('on-copy', 'string'));
    const client = makeClient();
    expect(await client.isFeatureEnabled('f', 'user-1')).toBe(true);
    expect(await client.getStringValue('f', 'user-1', 'fb')).toBe('on-copy');
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  test('evaluateFlag carries value and valueType only when the server sent them', async () => {
    mockFetchOnce(valueBody('on-copy', 'string'));
    mockFetchOnce(flagBody('g', true, null));
    const client = makeClient();
    expect(await client.evaluateFlag('f', 'user-1')).toStrictEqual({
      key: 'f',
      enabled: true,
      config: null,
      value: 'on-copy',
      valueType: 'string',
    });
    expect(await client.evaluateFlag('g', 'user-1')).toStrictEqual({ key: 'g', enabled: true, config: null });
  });

  test('a failed request shared by evaluateFlag and a getter: one report each, under its own name', async () => {
    const onError = jest.fn();
    mockFetchOnce({ detail: 'down' }, 503);
    const client = makeClient({ onError });
    const [evaluation, value] = await Promise.all([
      client.evaluateFlag('f', 'user-1'),
      client.getStringValue('f', 'user-1', 'fb'),
      client.evaluateFlag('f', 'user-1'),
    ]);
    expect(evaluation.enabled).toBe(false);
    expect(value).toBe('fb');
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    // evaluateFlag reports a failed request once however many calls share it, as before;
    // each getter call that falls back reports once.
    expect(onError.mock.calls.map(([, operation]) => operation).sort()).toEqual(['evaluateFlag', 'value']);
  });

  test('offline fallback: a failed request answers the stored value of that type, and still reports', async () => {
    const onError = jest.fn();
    const storage = new OfflineStorage();
    const stored: FlagEvaluation = { key: 'f', enabled: true, config: null, value: 'stored-copy', valueType: 'string' };
    await storage.setFlag('user-1', 'f', stored, -1);
    mockFetchError('Connection refused');
    const client = makeClient({ offlineFallback: true, onError }, storage);
    expect(await client.getStringValue('f', 'user-1', 'fb')).toBe('stored-copy');
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0]).toEqual([expect.objectContaining({ message: 'Connection refused' }), 'value']);
  });

  test('an answer persisted with its value is read back from AsyncStorage by a new client', async () => {
    const storage = new OfflineStorage();
    mockFetchOnce(valueBody(10, 'number'));
    await makeClient({ offlineFallback: true }, storage).getNumberValue('f', 'user-1', -1);
    expect(await makeClient({ offlineFallback: true }, storage).getNumberValue('f', 'user-1', -1)).toBe(10);
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  test('an answer stored by 0.1 (no value) answers the default and reports INVALID_RESPONSE, no request', async () => {
    const onError = jest.fn();
    const storage = new OfflineStorage();
    await storage.setFlag('user-1', 'f', { key: 'f', enabled: true, config: null }, 60_000);
    const client = makeClient({ offlineFallback: true, onError }, storage);
    expect(await client.getStringValue('f', 'user-1', 'fb')).toBe('fb');
    expect(fetchMock()).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][1]).toBe('value');
    expect((onError.mock.calls[0][0] as FlagValueError).code).toBe('INVALID_RESPONSE');
    // ...and the boolean answer it holds is unchanged.
    expect(await client.isFeatureEnabled('f', 'user-1')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// getAllFlags — GET /api/v1/feature-flags/user/{user_id}
// ---------------------------------------------------------------------------

describe('getAllFlags', () => {
  test('GETs /api/v1/feature-flags/user/{user_id} and coerces values to booleans', async () => {
    mockFetchOnce({ 'dark-mode': true, 'new-checkout': false, weird: 1 });
    const flags = await makeClient().getAllFlags('user@1');

    expect(callAt(0).url).toBe(`${BASE_URL}/api/v1/feature-flags/user/user%401`);
    expect(callAt(0).method).toBe('GET');
    expect(flags).toEqual({ 'dark-mode': true, 'new-checkout': false, weird: true });
  });

  test('is not cached — every call hits the server', async () => {
    mockFetchAlways({ a: true });
    const client = makeClient();
    await client.getAllFlags('user-1');
    await client.getAllFlags('user-1');
    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(client.getEvaluatedFlags('user-1')).toEqual([]);
  });

  test('returns {} and reports on failure', async () => {
    const onError = jest.fn();
    mockFetchOnce({}, 500);
    expect(await makeClient({ onError }).getAllFlags('user-1')).toEqual({});
    expect(onError).toHaveBeenCalledWith(expect.any(ApiError), 'getAllFlags');
  });
});

// ---------------------------------------------------------------------------
// track — POST /api/v1/tracking/track (with a key)
// ---------------------------------------------------------------------------

describe('track — with experimentKey / featureFlagKey', () => {
  test('sends exactly one POST /api/v1/tracking/track with the contract body', async () => {
    mockFetchOnce({});
    await makeClient().track('purchase', 'user-1', { sku: 'A1' }, { value: 12.5, experimentKey: 'checkout-experiment' });

    expect(fetchMock()).toHaveBeenCalledTimes(1);
    const call = callAt(0);
    expect(call.url).toBe(`${BASE_URL}/api/v1/tracking/track`);
    expect(call.method).toBe('POST');
    expect(call.headers).toEqual(HEADERS);
    expect(call.body).toEqual({
      event_type: 'purchase',
      event_name: 'purchase',
      user_id: 'user-1',
      value: 12.5,
      metadata: { sku: 'A1' },
      experiment_key: 'checkout-experiment',
    });
  });

  test('featureFlagKey is sent as feature_flag_key', async () => {
    mockFetchOnce({});
    await makeClient().track('cta_clicked', 'user-1', undefined, { featureFlagKey: 'dark-mode' });
    expect(callAt(0).body).toEqual({
      event_type: 'cta_clicked',
      event_name: 'cta_clicked',
      user_id: 'user-1',
      feature_flag_key: 'dark-mode',
    });
  });

  test('both keys can be sent together', async () => {
    mockFetchOnce({});
    await makeClient().track('signup', 'user-1', undefined, { experimentKey: 'exp', featureFlagKey: 'flag' });
    expect(callAt(0).body).toMatchObject({ experiment_key: 'exp', feature_flag_key: 'flag' });
  });

  test('custom eventType and a Date timestamp (sent as ISO-8601)', async () => {
    mockFetchOnce({});
    const ts = new Date('2026-09-11T10:20:30.000Z');
    await makeClient().track('purchase', 'user-1', undefined, { experimentKey: 'exp', eventType: 'conversion', timestamp: ts });
    expect(callAt(0).body).toEqual({
      event_type: 'conversion',
      event_name: 'purchase',
      user_id: 'user-1',
      experiment_key: 'exp',
      timestamp: '2026-09-11T10:20:30.000Z',
    });
  });

  test('string timestamps pass through unchanged', async () => {
    mockFetchOnce({});
    await makeClient().track('purchase', 'user-1', undefined, { experimentKey: 'exp', timestamp: '2026-01-01T00:00:00Z' });
    expect(callAt(0).body).toMatchObject({ timestamp: '2026-01-01T00:00:00Z' });
  });

  test('omits metadata / value / timestamp when not provided', async () => {
    mockFetchOnce({});
    await makeClient().track('page_view', 'user-1', undefined, { experimentKey: 'exp' });
    const body = callAt(0).body as Record<string, unknown>;
    expect(body).not.toHaveProperty('metadata');
    expect(body).not.toHaveProperty('value');
    expect(body).not.toHaveProperty('timestamp');
    expect(body).not.toHaveProperty('feature_flag_key');
  });

  test('never rejects on a network error (reported via onError)', async () => {
    const onError = jest.fn();
    mockFetchError('Timeout');
    await expect(makeClient({ onError }).track('e', 'user-1', undefined, { experimentKey: 'exp' })).resolves.toBeUndefined();
    expect(onError).toHaveBeenCalledWith(expect.any(Error), 'track');
  });

  test('never rejects on a 422 / 500 response', async () => {
    const onError = jest.fn();
    const client = makeClient({ onError });
    mockFetchOnce({ detail: 'validation' }, 422);
    await expect(client.track('e', 'user-1', undefined, { experimentKey: 'exp' })).resolves.toBeUndefined();
    mockFetchOnce({}, 500);
    await expect(client.track('e', 'user-1', undefined, { experimentKey: 'exp' })).resolves.toBeUndefined();
    expect((onError.mock.calls[0][0] as ApiError).status).toBe(422);
    expect((onError.mock.calls[1][0] as ApiError).status).toBe(500);
  });

  test('a throwing onError handler does not break fire-and-forget semantics', async () => {
    const onError = jest.fn(() => {
      throw new Error('handler exploded');
    });
    mockFetchError('down');
    await expect(makeClient({ onError }).track('e', 'user-1', undefined, { experimentKey: 'exp' })).resolves.toBeUndefined();
    expect(onError).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// track — fan-out via POST /api/v1/tracking/batch (no key)
// ---------------------------------------------------------------------------

describe('track — without a key (fan-out)', () => {
  test('sends nothing when the user has no cached assignments or flags', async () => {
    await makeClient().track('page_view', 'user-1', { page: 'home' });
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  test('sends one /tracking/batch with one entry per cached assignment and per evaluated flag', async () => {
    const client = makeClient();
    mockFetchOnce(assignBody({ experiment_key: 'checkout-experiment' }));
    mockFetchOnce(assignBody({ experiment_key: 'pricing-experiment', variant_name: 'control', is_control: true }));
    mockFetchOnce(flagBody('dark-mode', true));
    mockFetchOnce(flagBody('new-nav', false, null)); // disabled but successfully evaluated → still fanned out
    await client.getAssignment('checkout-experiment', 'user-1');
    await client.getAssignment('pricing-experiment', 'user-1');
    await client.evaluateFlag('dark-mode', 'user-1');
    await client.evaluateFlag('new-nav', 'user-1');
    fetchMock().mockClear();

    mockFetchOnce({ success_count: 4, failure_count: 0, errors: null });
    await client.track('page_view', 'user-1', { page: 'home' }, { value: 1 });

    expect(fetchMock()).toHaveBeenCalledTimes(1);
    const call = callAt(0);
    expect(call.url).toBe(`${BASE_URL}/api/v1/tracking/batch`);
    expect(call.method).toBe('POST');
    expect(call.headers).toEqual(HEADERS);
    const base = { event_type: 'page_view', event_name: 'page_view', user_id: 'user-1', value: 1, metadata: { page: 'home' } };
    expect(call.body).toEqual({
      events: [
        { ...base, experiment_key: 'checkout-experiment' },
        { ...base, experiment_key: 'pricing-experiment' },
        { ...base, feature_flag_key: 'dark-mode' },
        { ...base, feature_flag_key: 'new-nav' },
      ],
    });
  });

  test('only fans out to the same user\'s cached entries', async () => {
    const client = makeClient();
    mockFetchOnce(assignBody({ user_id: 'user-2' }));
    mockFetchOnce(flagBody('dark-mode'));
    await client.getAssignment('checkout-experiment', 'user-2');
    await client.evaluateFlag('dark-mode', 'user-1');
    fetchMock().mockClear();

    mockFetchOnce({ success_count: 1, failure_count: 0 });
    await client.track('page_view', 'user-1');

    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(callAt(0).body).toEqual({
      events: [{ event_type: 'page_view', event_name: 'page_view', user_id: 'user-1', feature_flag_key: 'dark-mode' }],
    });
  });

  test('failed evaluations / assignments are not part of the fan-out', async () => {
    const client = makeClient();
    mockFetchOnce({}, 404); // assignment fails
    mockFetchOnce({}, 500); // flag fails
    await client.getAssignment('missing-exp', 'user-1');
    await client.evaluateFlag('missing-flag', 'user-1');
    fetchMock().mockClear();

    await client.track('page_view', 'user-1');
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  test('expired cache entries are not fanned out', async () => {
    const client = makeClient({ cacheTtlMs: 1000 });
    const now = Date.now();
    const nowSpy = jest.spyOn(Date, 'now').mockReturnValue(now);
    mockFetchOnce(flagBody('dark-mode'));
    await client.evaluateFlag('dark-mode', 'user-1');
    fetchMock().mockClear();

    nowSpy.mockReturnValue(now + 2000);
    await client.track('page_view', 'user-1');
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  test('fan-out is chunked at 100 events per batch request', async () => {
    const client = makeClient();
    mockFetchAlways(flagBody());
    for (let i = 0; i < 150; i++) await client.evaluateFlag(`flag-${i}`, 'user-1');
    fetchMock().mockClear();
    mockFetchAlways({ success_count: 0, failure_count: 0 });

    await client.track('page_view', 'user-1');

    const batches = calls();
    expect(batches).toHaveLength(2);
    expect(batches.every((c) => c.url === `${BASE_URL}/api/v1/tracking/batch`)).toBe(true);
    expect((batches[0].body as { events: unknown[] }).events).toHaveLength(100);
    expect((batches[1].body as { events: unknown[] }).events).toHaveLength(50);
  });

  test('never rejects when the batch request fails', async () => {
    const onError = jest.fn();
    const client = makeClient({ onError });
    mockFetchOnce(flagBody('dark-mode'));
    await client.evaluateFlag('dark-mode', 'user-1');
    mockFetchError('down');

    await expect(client.track('page_view', 'user-1')).resolves.toBeUndefined();
    expect(onError).toHaveBeenCalledWith(expect.any(Error), 'track');
  });
});

// ---------------------------------------------------------------------------
// trackBatch — POST /api/v1/tracking/batch
// ---------------------------------------------------------------------------

describe('trackBatch', () => {
  const keyed = (i: number): TrackEvent => ({ eventName: `evt-${i}`, userId: 'user-1', experimentKey: 'exp' });

  test('sends the events in one batch and aggregates the server counts', async () => {
    mockFetchOnce({ success_count: 2, failure_count: 0, errors: null });
    const result = await makeClient().trackBatch([
      { eventName: 'purchase', userId: 'user-1', properties: { sku: 'A1' }, value: 9.99, experimentKey: 'exp' },
      { eventName: 'view', userId: 'user-1', featureFlagKey: 'flag', eventType: 'impression' },
    ]);

    expect(result).toEqual({ successCount: 2, failureCount: 0, errors: [] });
    expect(callAt(0).url).toBe(`${BASE_URL}/api/v1/tracking/batch`);
    expect(callAt(0).method).toBe('POST');
    expect(callAt(0).body).toEqual({
      events: [
        { event_type: 'purchase', event_name: 'purchase', user_id: 'user-1', value: 9.99, metadata: { sku: 'A1' }, experiment_key: 'exp' },
        { event_type: 'impression', event_name: 'view', user_id: 'user-1', feature_flag_key: 'flag' },
      ],
    });
  });

  test('chunks at 100 events per request and sums the counts', async () => {
    mockFetchAlways({ success_count: 100, failure_count: 0 });
    fetchMock().mockResolvedValueOnce(jsonResponse({ success_count: 100, failure_count: 0 }));
    fetchMock().mockResolvedValueOnce(jsonResponse({ success_count: 98, failure_count: 2, errors: ['bad-1', 'bad-2'] }));
    fetchMock().mockResolvedValueOnce(jsonResponse({ success_count: 50, failure_count: 0 }));

    const events = Array.from({ length: 250 }, (_, i) => keyed(i));
    const result = await makeClient().trackBatch(events);

    expect(fetchMock()).toHaveBeenCalledTimes(3);
    expect(calls().map((c) => (c.body as { events: unknown[] }).events.length)).toEqual([100, 100, 50]);
    expect(result).toEqual({ successCount: 248, failureCount: 2, errors: ['bad-1', 'bad-2'] });
  });

  test('keyless entries are expanded with the same fan-out rule', async () => {
    const client = makeClient();
    mockFetchOnce(assignBody({ experiment_key: 'exp-a' }));
    mockFetchOnce(flagBody('flag-b'));
    await client.getAssignment('exp-a', 'user-1');
    await client.evaluateFlag('flag-b', 'user-1');
    fetchMock().mockClear();
    mockFetchOnce({ success_count: 3, failure_count: 0 });

    await client.trackBatch([
      { eventName: 'page_view', userId: 'user-1' },
      { eventName: 'click', userId: 'user-1', featureFlagKey: 'flag-b' },
    ]);

    expect(callAt(0).body).toEqual({
      events: [
        { event_type: 'page_view', event_name: 'page_view', user_id: 'user-1', experiment_key: 'exp-a' },
        { event_type: 'page_view', event_name: 'page_view', user_id: 'user-1', feature_flag_key: 'flag-b' },
        { event_type: 'click', event_name: 'click', user_id: 'user-1', feature_flag_key: 'flag-b' },
      ],
    });
  });

  test('keyless entries with nothing cached produce no request and zero counts', async () => {
    const result = await makeClient().trackBatch([{ eventName: 'page_view', userId: 'nobody' }]);
    expect(fetchMock()).not.toHaveBeenCalled();
    expect(result).toEqual({ successCount: 0, failureCount: 0, errors: [] });
  });

  test('an empty list produces no request', async () => {
    expect(await makeClient().trackBatch([])).toEqual({ successCount: 0, failureCount: 0, errors: [] });
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  test('a failed chunk counts all its events as failures and never rejects', async () => {
    const onError = jest.fn();
    fetchMock().mockResolvedValueOnce(jsonResponse({ success_count: 100, failure_count: 0 }));
    fetchMock().mockResolvedValueOnce(jsonResponse({ detail: 'rate limited' }, 429));

    const events = Array.from({ length: 130 }, (_, i) => keyed(i));
    const result = await makeClient({ onError }).trackBatch(events);

    expect(result.successCount).toBe(100);
    expect(result.failureCount).toBe(30);
    expect(result.errors).toEqual([{ message: expect.stringContaining('429'), status: 429 }]);
    expect(onError).toHaveBeenCalledWith(expect.any(ApiError), 'trackBatch');
  });

  test('a network error on a chunk is recorded without a status', async () => {
    mockFetchError('socket hang up');
    const result = await makeClient().trackBatch([keyed(0), keyed(1)]);
    expect(result).toEqual({ successCount: 0, failureCount: 2, errors: [{ message: 'socket hang up', status: undefined }] });
  });
});

// ---------------------------------------------------------------------------
// Cache access
// ---------------------------------------------------------------------------

describe('cache access', () => {
  test('getAssignments returns cached assignments in assignment order', async () => {
    const client = makeClient();
    mockFetchOnce(assignBody({ experiment_key: 'exp-b', variant_name: 'control', is_control: true }));
    mockFetchOnce(assignBody({ experiment_key: 'exp-a' }));
    await client.getAssignment('exp-b', 'user-1');
    await client.getAssignment('exp-a', 'user-1');

    expect(client.getAssignments('user-1').map((a) => [a.experimentKey, a.variantName])).toEqual([
      ['exp-b', 'control'],
      ['exp-a', 'treatment'],
    ]);
    expect(client.getAssignments('someone-else')).toEqual([]);
  });

  test('getEvaluatedFlags lists successfully evaluated flag keys', async () => {
    const client = makeClient();
    mockFetchOnce(flagBody('a'));
    mockFetchOnce(flagBody('b', false, null));
    mockFetchOnce({}, 404);
    await client.evaluateFlag('a', 'user-1');
    await client.evaluateFlag('b', 'user-1');
    await client.evaluateFlag('c', 'user-1');
    expect(client.getEvaluatedFlags('user-1')).toEqual(['a', 'b']);
  });

  test('clearCache empties both caches but leaves AsyncStorage intact', async () => {
    const storage = new OfflineStorage();
    const client = makeClient({ offlineFallback: true }, storage);
    mockFetchOnce(assignBody());
    mockFetchOnce(flagBody('dark-mode'));
    await client.getAssignment('checkout-experiment', 'user-1');
    await client.evaluateFlag('dark-mode', 'user-1');

    client.clearCache();

    expect(client.getAssignments('user-1')).toEqual([]);
    expect(client.getEvaluatedFlags('user-1')).toEqual([]);
    expect(await storage.getFlag('user-1', 'dark-mode')).not.toBeNull();
    expect(await storage.getAssignment('user-1', 'checkout-experiment')).not.toBeNull();
  });

  test('clearStorage removes only SDK-namespaced AsyncStorage keys', async () => {
    await AsyncStorage.setItem('host-app-key', 'keep me');
    const storage = new OfflineStorage();
    const client = makeClient({ offlineFallback: true }, storage);
    mockFetchOnce(assignBody());
    mockFetchOnce(flagBody('dark-mode'));
    await client.getAssignment('checkout-experiment', 'user-1');
    await client.evaluateFlag('dark-mode', 'user-1');

    await client.clearStorage();

    expect(await AsyncStorage.getAllKeys()).toEqual(['host-app-key']);
    expect(await storage.getFlag('user-1', 'dark-mode')).toBeNull();
    expect(await storage.getAssignment('user-1', 'checkout-experiment')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// OfflineStorage
// ---------------------------------------------------------------------------

describe('OfflineStorage', () => {
  test('namespaces keys and URL-encodes user id and key', async () => {
    const storage = new OfflineStorage();
    await storage.setFlag('user 1', 'a/b', { key: 'a/b', enabled: true, config: null }, 1000);
    await storage.setAssignment('user 1', 'exp:1', {
      experimentKey: 'exp:1', userId: 'user 1', variantId: null, variantName: 'control', isControl: true, configuration: null,
    }, 1000);

    expect([...(await AsyncStorage.getAllKeys())].sort()).toEqual(['ep_sdk_asgn:user%201:exp%3A1', 'ep_sdk_flag:user%201:a%2Fb']);
  });

  test('returns null for missing, malformed, or wrong-shaped entries', async () => {
    const storage = new OfflineStorage();
    expect(await storage.getFlag('u', 'missing')).toBeNull();

    await AsyncStorage.setItem('ep_sdk_flag:u:broken', 'not json');
    expect(await storage.getFlag('u', 'broken')).toBeNull();

    await AsyncStorage.setItem('ep_sdk_flag:u:noexpiry', JSON.stringify({ value: { key: 'x', enabled: true, config: null } }));
    expect(await storage.getFlag('u', 'noexpiry')).toBeNull();

    await AsyncStorage.setItem('ep_sdk_flag:u:legacy', JSON.stringify({ value: true, expiresAt: Date.now() + 1000 }));
    expect(await storage.getFlag('u', 'legacy')).toBeNull(); // 0.1 scalar layout is rejected

    await AsyncStorage.setItem('ep_sdk_asgn:u:legacy', JSON.stringify({ value: 'control', expiresAt: Date.now() + 1000 }));
    expect(await storage.getAssignment('u', 'legacy')).toBeNull();
  });

  test('never throws when AsyncStorage fails', async () => {
    const storage = new OfflineStorage();
    (AsyncStorage.setItem as jest.Mock).mockRejectedValueOnce(new Error('disk full'));
    (AsyncStorage.getItem as jest.Mock).mockRejectedValueOnce(new Error('io'));
    (AsyncStorage.getAllKeys as jest.Mock).mockRejectedValueOnce(new Error('io'));

    await expect(storage.setFlag('u', 'f', { key: 'f', enabled: true, config: null }, 1)).resolves.toBeUndefined();
    await expect(storage.getFlag('u', 'f')).resolves.toBeNull();
    await expect(storage.clear()).resolves.toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// #1520 — flag evaluation sends the user's attributes, as the JavaScript SDK does
// ---------------------------------------------------------------------------

describe('regression #1520 — flag evaluation sends the user attributes', () => {
  // The same shape as sdk/js contextQuery: `&context=` + encodeURIComponent(JSON.stringify(attributes)).
  // sdk/js/__tests__/client.test.ts (describe 'regression #1520') pins the same seven literal rows.
  const FIXTURE: Array<[string, Record<string, unknown>, string]> = [
    ['a space', { plan: 'a b' }, '%7B%22plan%22%3A%22a%20b%22%7D'],
    ['non-ASCII', { city: 'été' }, '%7B%22city%22%3A%22%C3%A9t%C3%A9%22%7D'],
    ['nested', { user: { tier: 'gold' } }, '%7B%22user%22%3A%7B%22tier%22%3A%22gold%22%7D%7D'],
    ['a number', { age: 42 }, '%7B%22age%22%3A42%7D'],
    ['a boolean', { beta: true }, '%7B%22beta%22%3Atrue%7D'],
    ['null', { x: null }, '%7B%22x%22%3Anull%7D'],
    ['a non-empty array', ['a'] as unknown as Record<string, unknown>, '%5B%22a%22%5D'],
  ];

  test.each(FIXTURE)('evaluateFlag sends %s as context=<encodeURIComponent(JSON)>', async (_name, attributes, encoded) => {
    mockFetchOnce(flagBody());
    await makeClient().evaluateFlag('dark-mode', 'user-1', attributes);
    expect(callAt(0).url).toBe(
      `${BASE_URL}/api/v1/feature-flags/evaluate/dark-mode?user_id=user-1&context=${encoded}`
    );
  });

  test.each([
    ['undefined', undefined],
    ['{}', {}],
  ])('sends no context for %s', async (_name, attributes) => {
    mockFetchOnce(flagBody());
    await makeClient().evaluateFlag('dark-mode', 'user-1', attributes);
    expect(callAt(0).url).toBe(`${BASE_URL}/api/v1/feature-flags/evaluate/dark-mode?user_id=user-1`);
    expect(callAt(0).url).not.toContain('context');
  });

  test('isFeatureEnabled passes the attributes on', async () => {
    mockFetchOnce(flagBody('dark-mode', true));
    expect(await makeClient().isFeatureEnabled('dark-mode', 'user-1', { plan: 'pro' })).toBe(true);
    expect(callAt(0).url).toContain('&context=%7B%22plan%22%3A%22pro%22%7D');
  });

  test('getAllFlags sends ?context= and nothing for {} or undefined', async () => {
    mockFetchAlways({ a: true });
    const client = makeClient();
    await client.getAllFlags('user@1', { plan: 'a b' });
    await client.getAllFlags('user@1', {});
    await client.getAllFlags('user@1');
    expect(callAt(0).url).toBe(`${BASE_URL}/api/v1/feature-flags/user/user%401?context=%7B%22plan%22%3A%22a%20b%22%7D`);
    expect(callAt(1).url).toBe(`${BASE_URL}/api/v1/feature-flags/user/user%401`);
    expect(callAt(2).url).toBe(`${BASE_URL}/api/v1/feature-flags/user/user%401`);
  });

  test('the cache, storage and dedupe keys stay user + flag: other attributes are served the stored answer', async () => {
    mockFetchAlways(flagBody());
    const storage = new OfflineStorage();
    const first = makeClient({ offlineFallback: true }, storage);
    await first.evaluateFlag('dark-mode', 'user-1', { plan: 'pro' });
    await first.evaluateFlag('dark-mode', 'user-1', { plan: 'free' });
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    // A new client sharing the storage is served the stored answer too.
    const second = makeClient({ offlineFallback: true }, storage);
    await second.evaluateFlag('dark-mode', 'user-1', { plan: 'free' });
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    // The remedies: clearCache() + clearStorage(), after which the new attributes are sent.
    first.clearCache();
    await first.clearStorage();
    await first.evaluateFlag('dark-mode', 'user-1', { plan: 'free' });
    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(callAt(1).url).toContain('&context=%7B%22plan%22%3A%22free%22%7D');

    // Concurrent calls share one fetch.
    fetchMock().mockClear();
    const third = makeClient();
    await Promise.all([
      third.evaluateFlag('other', 'user-1', { plan: 'a' }),
      third.evaluateFlag('other', 'user-1', { plan: 'b' }),
    ]);
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  test('without offlineFallback the memory key is user + flag too: other attributes, no clearCache(), one fetch', async () => {
    // AsyncStorage is off, so nothing but the in-memory cache can answer the second call.
    mockFetchAlways(flagBody());
    const client = makeClient({ offlineFallback: false });
    await client.evaluateFlag('dark-mode', 'user-1', { plan: 'pro' });
    expect(await client.evaluateFlag('dark-mode', 'user-1', { plan: 'free' })).toEqual({
      key: 'dark-mode',
      enabled: true,
      config: { theme: 'dark' },
    });
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(callAt(0).url).toContain('%22plan%22%3A%22pro%22');
  });

  test('without offlineFallback, a changed attribute set is asked again after clearCache()', async () => {
    mockFetchAlways(flagBody());
    const client = makeClient({ offlineFallback: false });
    await client.evaluateFlag('dark-mode', 'user-1', { plan: 'pro' });
    client.clearCache();
    await client.evaluateFlag('dark-mode', 'user-1', { plan: 'free' });
    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(callAt(1).url).toContain('%22plan%22%3A%22free%22');
  });

  test('a 422 goes to onError with the path but not the query, and nothing is cached', async () => {
    const onError = jest.fn();
    mockFetchAlways({ detail: 'context too large' }, 422);
    const client = makeClient({ onError });
    expect(await client.evaluateFlag('dark-mode', 'user-1', { plan: 'pro' })).toEqual({
      key: 'dark-mode',
      enabled: false,
      config: null,
    });
    expect(client.getEvaluatedFlags('user-1')).toEqual([]);
    const err = onError.mock.calls[0][0] as ApiError;
    expect(onError.mock.calls[0][1]).toBe('evaluateFlag');
    expect(err.status).toBe(422);
    expect(err.message).toBe('API error 422 (/api/v1/feature-flags/evaluate/dark-mode)');
    expect(err.message).not.toContain('context=');
    expect(err.message).not.toContain('pro');
    expect(err.message).not.toContain('user-1');
  });

  test('attributes that cannot be serialised go to onError, nothing is sent or cached', async () => {
    const onError = jest.fn();
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    const client = makeClient({ onError });
    expect((await client.evaluateFlag('dark-mode', 'user-1', circular)).enabled).toBe(false);
    expect(fetchMock()).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(expect.any(Error), 'evaluateFlag');
    expect(client.getEvaluatedFlags('user-1')).toEqual([]);
  });

  test('a getAllFlags failure message gives the path without the query', async () => {
    const onError = jest.fn();
    mockFetchOnce({}, 500);
    await makeClient({ onError }).getAllFlags('user-1', { plan: 'pro' });
    expect(onError.mock.calls[0][0].message).toBe('API error 500 (/api/v1/feature-flags/user/user-1)');
  });
});
