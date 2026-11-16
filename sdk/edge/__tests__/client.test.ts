/**
 * Tests for EdgeExperimentationClient.
 *
 * Runs in Node (Jest + ts-jest). `fetch` is mocked via jest.fn() — no network.
 * Covers the backend contract: URLs, methods, headers, JSON bodies, response
 * mapping, per user + key caching (TTL, never caching failures), the tracking
 * fan-out rule and the shared-store read-through.
 */

import { EdgeExperimentationClient, EdgeApiError } from '../src/client';
import type { EdgeSdkConfig, EdgeStore, FlagEvaluation, Assignment } from '../src/types';

// ---------------------------------------------------------------------------
// fetch mock helpers
// ---------------------------------------------------------------------------

const mockFetch = jest.fn();
global.fetch = mockFetch;

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: new Headers(),
    json: async () => body,
  } as unknown as Response;
}

function mockJson(body: unknown, status = 200): void {
  mockFetch.mockResolvedValueOnce(jsonResponse(body, status));
}

function assignBody(overrides: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    experiment_key: 'checkout_flow',
    user_id: 'user-1',
    variant_id: 'var-treatment',
    variant_name: 'treatment',
    is_control: false,
    configuration: { button: 'green' },
    ...overrides,
  };
}

function flagBody(overrides: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return { key: 'new-checkout', enabled: true, config: { variant: 'v2' }, ...overrides };
}

function lastCall(index = -1): { url: string; init: RequestInit } {
  const calls = mockFetch.mock.calls;
  const call = calls[index < 0 ? calls.length + index : index] as [string, RequestInit];
  return { url: call[0], init: call[1] };
}

function bodyOf(index = -1): Record<string, unknown> {
  return JSON.parse(lastCall(index).init.body as string) as Record<string, unknown>;
}

function makeClient(overrides: Partial<ConstructorParameters<typeof EdgeExperimentationClient>[0]> = {}) {
  return new EdgeExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test', ...overrides });
}

/** In-memory EdgeStore that records calls. */
function makeStore(initial: Record<string, string> = {}): EdgeStore & { data: Record<string, string>; ttls: number[] } {
  const data = { ...initial };
  const ttls: number[] = [];
  return {
    data,
    ttls,
    async get(key: string) {
      return data[key] ?? null;
    },
    async put(key: string, value: string, ttlMs: number) {
      data[key] = value;
      ttls.push(ttlMs);
    },
  };
}

beforeEach(() => {
  mockFetch.mockReset();
});

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------

describe('constructor', () => {
  test('throws when apiKey is empty', () => {
    expect(() => new EdgeExperimentationClient({ apiKey: '', baseUrl: 'http://api.test' })).toThrow('apiKey is required');
  });

  test('throws a clear error when baseUrl is missing: there is no default origin', () => {
    const fetchImpl = jest.fn();
    for (const config of [{ apiKey: 'k' }, { apiKey: 'k', baseUrl: '' }, { apiKey: 'k', baseUrl: '   ' }]) {
      expect(() => new EdgeExperimentationClient({ ...config, fetch: fetchImpl } as unknown as EdgeSdkConfig)).toThrow(
        "baseUrl is required: the origin of your API, such as 'https://api.example.com' (the SDK appends /api/v1/...)"
      );
    }
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  test('strips trailing slashes from baseUrl', async () => {
    mockJson(flagBody());
    const client = makeClient({ baseUrl: 'http://api.test//' });
    await client.evaluateFlag('new-checkout', 'user-1');
    expect(lastCall().url).toBe('http://api.test/api/v1/feature-flags/evaluate/new-checkout?user_id=user-1');
  });

  test('uses config.fetch when provided instead of the global', async () => {
    const custom = jest.fn().mockResolvedValue(jsonResponse(flagBody()));
    const client = makeClient({ fetch: custom as unknown as typeof fetch });
    await client.evaluateFlag('new-checkout', 'user-1');
    expect(custom).toHaveBeenCalledTimes(1);
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// evaluateFlag
// ---------------------------------------------------------------------------

describe('evaluateFlag', () => {
  test('GETs /api/v1/feature-flags/evaluate/{key}?user_id=… with the contract headers and no body', async () => {
    mockJson(flagBody());
    await makeClient().evaluateFlag('new-checkout', 'user-1');

    const { url, init } = lastCall();
    expect(url).toBe('http://api.test/api/v1/feature-flags/evaluate/new-checkout?user_id=user-1');
    expect(init.method).toBe('GET');
    expect(init.headers).toEqual({
      'X-API-Key': 'k',
      'Content-Type': 'application/json',
      Accept: 'application/json',
    });
    expect(init.body).toBeUndefined();
    expect(init.signal).toBeDefined();
  });

  test('URL-encodes the flag key and the user id', async () => {
    mockJson(flagBody({ key: 'a/b c' }));
    await makeClient().evaluateFlag('a/b c', 'user 1@x');
    expect(lastCall().url).toBe('http://api.test/api/v1/feature-flags/evaluate/a%2Fb%20c?user_id=user%201%40x');
  });

  test('maps {key, enabled, config}', async () => {
    mockJson(flagBody());
    const evaluation = await makeClient().evaluateFlag('new-checkout', 'user-1');
    expect(evaluation).toEqual<FlagEvaluation>({ key: 'new-checkout', enabled: true, config: { variant: 'v2' } });
  });

  test('normalises a missing config to null', async () => {
    mockJson({ key: 'f', enabled: false });
    expect(await makeClient().evaluateFlag('f', 'user-1')).toEqual({ key: 'f', enabled: false, config: null });
  });

  test('caches per user + key: the second call makes no request', async () => {
    mockJson(flagBody());
    const client = makeClient();
    await client.evaluateFlag('new-checkout', 'user-1');
    await client.evaluateFlag('new-checkout', 'user-1');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('keeps separate cache entries per user and per flag', async () => {
    mockFetch.mockImplementation(async () => jsonResponse(flagBody()));
    const client = makeClient();
    await client.evaluateFlag('f1', 'user-A');
    await client.evaluateFlag('f1', 'user-B');
    await client.evaluateFlag('f2', 'user-A');
    await client.evaluateFlag('f1', 'user-A');
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  test('caches disabled results too', async () => {
    mockJson(flagBody({ enabled: false }));
    const client = makeClient();
    expect((await client.evaluateFlag('new-checkout', 'user-1')).enabled).toBe(false);
    await client.evaluateFlag('new-checkout', 'user-1');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('shares one in-flight request between concurrent callers', async () => {
    let resolve!: (r: Response) => void;
    mockFetch.mockReturnValueOnce(new Promise<Response>((r) => (resolve = r)));
    const client = makeClient();
    const p1 = client.evaluateFlag('new-checkout', 'user-1');
    const p2 = client.evaluateFlag('new-checkout', 'user-1');
    resolve(jsonResponse(flagBody()));
    const [a, b] = await Promise.all([p1, p2]);
    expect(a).toBe(b);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('returns a disabled evaluation on 404 (unknown flag key) and does not cache it', async () => {
    mockJson({ detail: 'Feature flag not found' }, 404);
    mockJson(flagBody());
    const client = makeClient();
    expect(await client.evaluateFlag('new-checkout', 'user-1')).toEqual({ key: 'new-checkout', enabled: false, config: null });
    expect((await client.evaluateFlag('new-checkout', 'user-1')).enabled).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  test('returns a disabled evaluation on a network error', async () => {
    mockFetch.mockRejectedValueOnce(new Error('network error'));
    expect(await makeClient().evaluateFlag('f', 'user-1')).toEqual({ key: 'f', enabled: false, config: null });
  });

  test('re-fetches after cacheTtlMs elapses', async () => {
    jest.useFakeTimers();
    try {
      mockJson(flagBody());
      mockJson(flagBody({ enabled: false }));
      const client = makeClient({ cacheTtlMs: 1000 });
      expect((await client.evaluateFlag('new-checkout', 'user-1')).enabled).toBe(true);
      jest.advanceTimersByTime(999);
      expect((await client.evaluateFlag('new-checkout', 'user-1')).enabled).toBe(true);
      jest.advanceTimersByTime(2);
      expect((await client.evaluateFlag('new-checkout', 'user-1')).enabled).toBe(false);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    } finally {
      jest.useRealTimers();
    }
  });

  test('aborts after `timeout` ms and returns the disabled evaluation', async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockImplementationOnce(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () => reject(new Error('aborted')));
          }),
      );
      const client = makeClient({ timeout: 50 });
      const pending = client.evaluateFlag('f', 'user-1');
      await jest.advanceTimersByTimeAsync(51);
      expect(await pending).toEqual({ key: 'f', enabled: false, config: null });
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('isFeatureEnabled', () => {
  test('returns the server decision, false on failure', async () => {
    mockJson(flagBody({ enabled: true }));
    mockFetch.mockRejectedValueOnce(new Error('down'));
    const client = makeClient();
    expect(await client.isFeatureEnabled('new-checkout', 'user-1')).toBe(true);
    expect(await client.isFeatureEnabled('other', 'user-1')).toBe(false);
  });
});

describe('getAllFlags', () => {
  test('GETs /api/v1/feature-flags/user/{user_id} and returns {key: boolean}', async () => {
    mockJson({ a: true, b: false, c: 1 });
    const flags = await makeClient().getAllFlags('user 1');
    expect(lastCall().url).toBe('http://api.test/api/v1/feature-flags/user/user%201');
    expect(lastCall().init.method).toBe('GET');
    expect(flags).toEqual({ a: true, b: false, c: true });
  });

  test('returns {} on failure and does not populate the evaluation cache', async () => {
    mockJson({ detail: 'nope' }, 401);
    const client = makeClient();
    expect(await client.getAllFlags('user-1')).toEqual({});
    expect(client.getEvaluatedFlags('user-1')).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// getAssignment
// ---------------------------------------------------------------------------

describe('getAssignment', () => {
  test('POSTs /api/v1/tracking/assign with the contract headers', async () => {
    mockJson(assignBody());
    await makeClient().getAssignment('checkout_flow', 'user-1');

    const { url, init } = lastCall();
    expect(url).toBe('http://api.test/api/v1/tracking/assign');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({
      'X-API-Key': 'k',
      'Content-Type': 'application/json',
      Accept: 'application/json',
    });
  });

  test('sends {experiment_key, user_id, context: attributes} as the JSON body', async () => {
    mockJson(assignBody());
    await makeClient().getAssignment('checkout_flow', 'user-1', { country: 'US', plan: 'pro' });
    expect(bodyOf()).toEqual({
      experiment_key: 'checkout_flow',
      user_id: 'user-1',
      context: { country: 'US', plan: 'pro' },
    });
  });

  test('omits context when no attributes are given', async () => {
    mockJson(assignBody());
    await makeClient().getAssignment('checkout_flow', 'user-1');
    expect(bodyOf()).toEqual({ experiment_key: 'checkout_flow', user_id: 'user-1' });
  });

  test('maps the server response to an Assignment', async () => {
    mockJson(assignBody());
    const assignment = await makeClient().getAssignment('checkout_flow', 'user-1');
    expect(assignment).toEqual<Assignment>({
      experimentKey: 'checkout_flow',
      userId: 'user-1',
      variantId: 'var-treatment',
      variantName: 'treatment',
      isControl: false,
      configuration: { button: 'green' },
    });
  });

  test('normalises missing variant_id / is_control / configuration', async () => {
    mockJson({ experiment_key: 'checkout_flow', user_id: 'user-1', variant_name: 'control' });
    const assignment = await makeClient().getAssignment('checkout_flow', 'user-1');
    expect(assignment).toMatchObject({ variantId: null, isControl: false, configuration: null });
  });

  test('is sticky: the second call is served from the cache without a request', async () => {
    mockJson(assignBody());
    const client = makeClient();
    const first = await client.getAssignment('checkout_flow', 'user-1');
    const second = await client.getAssignment('checkout_flow', 'user-1');
    expect(second).toBe(first);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('shares one in-flight request between concurrent callers', async () => {
    let resolve!: (r: Response) => void;
    mockFetch.mockReturnValueOnce(new Promise<Response>((r) => (resolve = r)));
    const client = makeClient();
    const p1 = client.getAssignment('checkout_flow', 'user-1');
    const p2 = client.getAssignment('checkout_flow', 'user-1');
    resolve(jsonResponse(assignBody()));
    expect(await p1).toBe(await p2);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('returns null on 404 (no experiment has the key) and never caches the failure', async () => {
    mockJson({ detail: "No experiment has the key 'checkout_flow'." }, 404);
    mockJson(assignBody());
    const client = makeClient();
    expect(await client.getAssignment('checkout_flow', 'user-1')).toBeNull();
    expect((await client.getAssignment('checkout_flow', 'user-1'))?.variantName).toBe('treatment');
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  test('returns null on a network error and on a malformed body', async () => {
    mockFetch.mockRejectedValueOnce(new Error('refused'));
    mockJson({ unexpected: true });
    const client = makeClient();
    expect(await client.getAssignment('a', 'user-1')).toBeNull();
    expect(await client.getAssignment('b', 'user-1')).toBeNull();
  });
});

describe('getVariant', () => {
  test('returns the variant name, null on failure', async () => {
    mockJson(assignBody());
    mockJson({}, 404);
    const client = makeClient();
    expect(await client.getVariant('checkout_flow', 'user-1')).toBe('treatment');
    expect(await client.getVariant('other', 'user-1')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Sync accessors + cache helpers
// ---------------------------------------------------------------------------

describe('sync cache accessors', () => {
  test('evaluateFlagSync / getAssignmentSync read the in-memory cache only', async () => {
    const client = makeClient();
    expect(client.evaluateFlagSync('new-checkout', 'user-1')).toBe(false);
    expect(client.getAssignmentSync('checkout_flow', 'user-1')).toBeNull();
    expect(mockFetch).not.toHaveBeenCalled();

    mockJson(flagBody());
    mockJson(assignBody());
    await client.evaluateFlag('new-checkout', 'user-1');
    await client.getAssignment('checkout_flow', 'user-1');

    expect(client.evaluateFlagSync('new-checkout', 'user-1')).toBe(true);
    expect(client.getAssignmentSync('checkout_flow', 'user-1')).toBe('treatment');
    expect(client.getCachedFlag('new-checkout', 'user-1')).toEqual({ key: 'new-checkout', enabled: true, config: { variant: 'v2' } });
    expect(client.getCachedAssignment('checkout_flow', 'user-1')?.variantId).toBe('var-treatment');
    expect(client.getCachedFlag('new-checkout', 'other')).toBeNull();
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  test('getAssignments / getEvaluatedFlags are per user, in order; clearCache empties both', async () => {
    mockJson(assignBody({ experiment_key: 'e1' }));
    mockJson(assignBody({ experiment_key: 'e2' }));
    mockJson(assignBody({ experiment_key: 'e3', user_id: 'user-2' }));
    mockJson(flagBody({ key: 'f1' }));
    mockJson(flagBody({ key: 'f2' }));
    const client = makeClient();
    await client.getAssignment('e1', 'user-1');
    await client.getAssignment('e2', 'user-1');
    await client.getAssignment('e3', 'user-2');
    await client.evaluateFlag('f1', 'user-1');
    await client.evaluateFlag('f2', 'user-2');

    expect(client.getAssignments('user-1').map((a) => a.experimentKey)).toEqual(['e1', 'e2']);
    expect(client.getAssignments('user-2').map((a) => a.experimentKey)).toEqual(['e3']);
    expect(client.getEvaluatedFlags('user-1')).toEqual(['f1']);
    expect(client.flagCount).toBe(2);
    expect(client.experimentCount).toBe(3);

    client.clearCache();
    expect(client.getAssignments('user-1')).toEqual([]);
    expect(client.getEvaluatedFlags('user-1')).toEqual([]);
    expect(client.flagCount).toBe(0);
  });

  test('user ids containing ":" do not collide in the fan-out prefix', async () => {
    mockJson(assignBody({ experiment_key: 'e1' }));
    mockJson(assignBody({ experiment_key: 'e2' }));
    const client = makeClient();
    await client.getAssignment('e1', 'a');
    await client.getAssignment('e2', 'a:b');
    expect(client.getAssignments('a').map((a) => a.experimentKey)).toEqual(['e1']);
  });

  test('refreshFlags is a deprecated no-op that resolves without a request', async () => {
    await expect(makeClient().refreshFlags()).resolves.toBeUndefined();
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Shared store read-through
// ---------------------------------------------------------------------------

describe('shared store', () => {
  test('serves a flag from the store without a network call and populates memory', async () => {
    const stored: FlagEvaluation = { key: 'new-checkout', enabled: true, config: null };
    const store = makeStore({ 'flag:user-1:new-checkout': JSON.stringify(stored) });
    const client = makeClient({ store });
    expect(await client.evaluateFlag('new-checkout', 'user-1')).toEqual(stored);
    expect(mockFetch).not.toHaveBeenCalled();
    expect(client.evaluateFlagSync('new-checkout', 'user-1')).toBe(true);
  });

  test('serves an assignment from the store without a network call', async () => {
    const stored: Assignment = {
      experimentKey: 'checkout_flow',
      userId: 'user-1',
      variantId: 'v',
      variantName: 'control',
      isControl: true,
      configuration: null,
    };
    const store = makeStore({ 'assign:user-1:checkout_flow': JSON.stringify(stored) });
    expect(await makeClient({ store }).getAssignment('checkout_flow', 'user-1')).toEqual(stored);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  test('writes successful results to the store with the cache TTL, keyed per user + key', async () => {
    const store = makeStore();
    const client = makeClient({ store, cacheTtlMs: 45_000 });
    mockJson(flagBody());
    mockJson(assignBody());
    await client.evaluateFlag('new-checkout', 'user-1');
    await client.getAssignment('checkout_flow', 'user-1');
    expect(Object.keys(store.data).sort()).toEqual(['assign:user-1:checkout_flow', 'flag:user-1:new-checkout']);
    expect(JSON.parse(store.data['flag:user-1:new-checkout'])).toEqual({ key: 'new-checkout', enabled: true, config: { variant: 'v2' } });
    expect(store.ttls).toEqual([45_000, 45_000]);
  });

  test('ignores corrupt or mismatched store values and falls through to the network', async () => {
    const store = makeStore({ 'flag:user-1:f': 'not json', 'assign:user-1:e': JSON.stringify({ nope: 1 }) });
    mockJson(flagBody({ key: 'f' }));
    mockJson(assignBody({ experiment_key: 'e' }));
    const client = makeClient({ store });
    expect((await client.evaluateFlag('f', 'user-1')).enabled).toBe(true);
    expect((await client.getAssignment('e', 'user-1'))?.variantName).toBe('treatment');
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  test('a failing store never breaks evaluation', async () => {
    const store: EdgeStore = {
      get: async () => {
        throw new Error('kv down');
      },
      put: async () => {
        throw new Error('kv down');
      },
    };
    mockJson(flagBody());
    expect((await makeClient({ store }).evaluateFlag('new-checkout', 'user-1')).enabled).toBe(true);
  });

  test('failures are never written to the store', async () => {
    const store = makeStore();
    mockJson({}, 404);
    await makeClient({ store }).evaluateFlag('f', 'user-1');
    expect(store.data).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// track with an explicit key
// ---------------------------------------------------------------------------

describe('track with an explicit key', () => {
  test('POSTs one event to /api/v1/tracking/track with the exact body', async () => {
    mockJson({ id: 'evt' });
    const client = makeClient();
    await client.track(
      'purchase',
      'user-1',
      { sku: 'A1', qty: 2 },
      { value: 12.5, experimentKey: 'checkout_flow', timestamp: new Date('2026-01-02T03:04:05.000Z') },
    );

    const { url, init } = lastCall();
    expect(url).toBe('http://api.test/api/v1/tracking/track');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({
      'X-API-Key': 'k',
      'Content-Type': 'application/json',
      Accept: 'application/json',
    });
    expect(bodyOf()).toEqual({
      event_type: 'purchase',
      event_name: 'purchase',
      user_id: 'user-1',
      experiment_key: 'checkout_flow',
      value: 12.5,
      metadata: { sku: 'A1', qty: 2 },
      timestamp: '2026-01-02T03:04:05.000Z',
    });
  });

  test('sends feature_flag_key (and no experiment_key), uses eventType, accepts a string timestamp', async () => {
    mockJson({});
    await makeClient().track('flag_seen', 'user-1', undefined, {
      featureFlagKey: 'new-checkout',
      eventType: 'exposure',
      timestamp: '2026-01-01T00:00:00Z',
    });
    expect(bodyOf()).toEqual({
      event_type: 'exposure',
      event_name: 'flag_seen',
      user_id: 'user-1',
      feature_flag_key: 'new-checkout',
      metadata: {},
      timestamp: '2026-01-01T00:00:00Z',
    });
  });

  test('does not fan out to cached assignments when a key is given', async () => {
    mockJson(assignBody({ experiment_key: 'other' }));
    mockJson({});
    const client = makeClient();
    await client.getAssignment('other', 'user-1');
    await client.track('purchase', 'user-1', {}, { experimentKey: 'checkout_flow' });
    expect(lastCall().url).toBe('http://api.test/api/v1/tracking/track');
    expect(bodyOf().experiment_key).toBe('checkout_flow');
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  test('never throws: 500 responses and network errors are swallowed', async () => {
    mockJson({ detail: 'boom' }, 500);
    mockFetch.mockRejectedValueOnce(new Error('network'));
    const client = makeClient();
    await expect(client.track('e', 'user-1', {}, { experimentKey: 'x' })).resolves.toBeUndefined();
    await expect(client.track('e', 'user-1', {}, { experimentKey: 'x' })).resolves.toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// track fan-out (no key)
// ---------------------------------------------------------------------------

describe('track fan-out (no key)', () => {
  test('sends nothing when nothing is cached for the user', async () => {
    await makeClient().track('page_view', 'user-1', { page: '/' });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  test('sends nothing when only another user has cached data', async () => {
    mockJson(assignBody({ user_id: 'user-2' }));
    const client = makeClient();
    await client.getAssignment('checkout_flow', 'user-2');
    await client.track('page_view', 'user-1');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('POSTs /api/v1/tracking/batch with one entry per assignment plus one per evaluated flag', async () => {
    mockJson(assignBody({ experiment_key: 'exp_a' }));
    mockJson(assignBody({ experiment_key: 'exp_b' }));
    mockJson(flagBody({ key: 'flag_x' }));
    mockJson({ success_count: 3, failure_count: 0, errors: null });
    const client = makeClient();
    await client.getAssignment('exp_a', 'user-1');
    await client.getAssignment('exp_b', 'user-1');
    await client.evaluateFlag('flag_x', 'user-1');

    await client.track('page_view', 'user-1', { page: '/home' }, { value: 1 });

    const { url, init } = lastCall();
    expect(url).toBe('http://api.test/api/v1/tracking/batch');
    expect(init.method).toBe('POST');
    const base = { event_type: 'page_view', event_name: 'page_view', user_id: 'user-1', value: 1, metadata: { page: '/home' } };
    expect(bodyOf()).toEqual({
      events: [
        { ...base, experiment_key: 'exp_a' },
        { ...base, experiment_key: 'exp_b' },
        { ...base, feature_flag_key: 'flag_x' },
      ],
    });
  });

  test('does not fan out to a failed assignment or an expired one', async () => {
    jest.useFakeTimers();
    try {
      mockJson({}, 404); // exp_fail
      mockJson(assignBody({ experiment_key: 'exp_ok' }));
      const client = makeClient({ cacheTtlMs: 1000 });
      await client.getAssignment('exp_fail', 'user-1');
      await client.getAssignment('exp_ok', 'user-1');

      mockJson({ success_count: 1, failure_count: 0 });
      await client.track('page_view', 'user-1');
      expect((bodyOf() as { events: unknown[] }).events).toHaveLength(1);

      jest.advanceTimersByTime(1001);
      await client.track('page_view', 'user-1');
      expect(mockFetch).toHaveBeenCalledTimes(3); // no new batch request
    } finally {
      jest.useRealTimers();
    }
  });

  test('splits more than 100 entries into multiple batch requests', async () => {
    mockFetch.mockImplementation(async (url: string) =>
      url.endsWith('/assign')
        ? jsonResponse(assignBody({ experiment_key: `exp_${mockFetch.mock.calls.length}` }))
        : jsonResponse({ success_count: 0, failure_count: 0 }),
    );
    const client = makeClient();
    for (let i = 0; i < 150; i++) await client.getAssignment(`exp_${i}`, 'user-1');
    mockFetch.mockClear();

    await client.track('page_view', 'user-1');
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect((bodyOf(0) as { events: unknown[] }).events).toHaveLength(100);
    expect((bodyOf(1) as { events: unknown[] }).events).toHaveLength(50);
  });

  test('never rejects when the batch request fails', async () => {
    mockJson(assignBody());
    mockFetch.mockRejectedValueOnce(new Error('network'));
    const client = makeClient();
    await client.getAssignment('checkout_flow', 'user-1');
    await expect(client.track('page_view', 'user-1')).resolves.toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// trackBatch
// ---------------------------------------------------------------------------

describe('trackBatch', () => {
  test('POSTs keyed events to /api/v1/tracking/batch and aggregates the server counts', async () => {
    mockJson({ success_count: 2, failure_count: 0, errors: null });
    const result = await makeClient().trackBatch([
      { eventName: 'add_to_cart', userId: 'user-1', experimentKey: 'checkout_flow', value: 1 },
      { eventName: 'flag_seen', userId: 'user-1', featureFlagKey: 'new-checkout' },
    ]);
    expect(lastCall().url).toBe('http://api.test/api/v1/tracking/batch');
    expect(bodyOf()).toEqual({
      events: [
        { event_type: 'add_to_cart', event_name: 'add_to_cart', user_id: 'user-1', experiment_key: 'checkout_flow', value: 1 },
        { event_type: 'flag_seen', event_name: 'flag_seen', user_id: 'user-1', feature_flag_key: 'new-checkout' },
      ],
    });
    expect(result).toEqual({ successCount: 2, failureCount: 0, errors: [] });
  });

  test('chunks at 100 events per request', async () => {
    mockFetch.mockImplementation(async () => jsonResponse({ success_count: 0, failure_count: 0 }));
    const events = Array.from({ length: 250 }, (_, i) => ({ eventName: 'e', userId: 'u', experimentKey: `x${i}` }));
    await makeClient().trackBatch(events);
    expect(mockFetch).toHaveBeenCalledTimes(3);
    expect((bodyOf(2) as { events: unknown[] }).events).toHaveLength(50);
  });

  test('expands keyless entries with the fan-out rule and drops those with nothing cached', async () => {
    mockJson(assignBody({ experiment_key: 'exp_a' }));
    mockJson({ success_count: 2, failure_count: 0 });
    const client = makeClient();
    await client.getAssignment('exp_a', 'user-1');
    await client.trackBatch([
      { eventName: 'page_view', userId: 'user-1' },
      { eventName: 'page_view', userId: 'nobody' },
      { eventName: 'click', userId: 'user-1', featureFlagKey: 'f' },
    ]);
    const events = (bodyOf() as { events: Array<Record<string, unknown>> }).events;
    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({ event_name: 'page_view', experiment_key: 'exp_a' });
    expect(events[1]).toMatchObject({ event_name: 'click', feature_flag_key: 'f' });
  });

  test('sends nothing and returns zeros for an empty list', async () => {
    expect(await makeClient().trackBatch([])).toEqual({ successCount: 0, failureCount: 0, errors: [] });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  test('never rejects: a failed chunk counts its events as failures with the status', async () => {
    mockJson({ detail: 'rate limited' }, 429);
    const result = await makeClient().trackBatch([
      { eventName: 'a', userId: 'u', experimentKey: 'x' },
      { eventName: 'b', userId: 'u', experimentKey: 'x' },
    ]);
    expect(result.successCount).toBe(0);
    expect(result.failureCount).toBe(2);
    expect(result.errors).toEqual([{ message: expect.stringContaining('429'), status: 429 }]);
  });
});

describe('EdgeApiError', () => {
  test('carries the HTTP status', () => {
    const err = new EdgeApiError(404, 'nope');
    expect(err.status).toBe(404);
    expect(err.name).toBe('EdgeApiError');
    expect(err).toBeInstanceOf(Error);
  });
});

// ---------------------------------------------------------------------------
// Flag attributes: the user's attributes go out as `context` (#1720)
// ---------------------------------------------------------------------------

const EVAL = '/api/v1/feature-flags/evaluate/f?user_id=u1';
const USERS = '/api/v1/feature-flags/user/u1';
const BASE = 'http://api.test';

// The vectors of the plan, as the string the SDK writes (S). The JavaScript SDK writes the same
// bytes: `contextQuery` is a copy of its function.
const V2 = { plan: 'pro' };
const V2_S = `${EVAL}&context=%7B%22plan%22%3A%22pro%22%7D`;
const V3 = { q: "a+b&c=d#e f/é😀!'()*~" };
const V3_S = `${EVAL}&context=%7B%22q%22%3A%22a%2Bb%26c%3Dd%23e%20f%2F%C3%A9%F0%9F%98%80!'()*~%22%7D`;
const V3A = { q: 'a+b&c=d#e fé' };
const V3A_S = `${EVAL}&context=%7B%22q%22%3A%22a%2Bb%26c%3Dd%23e%20f%C3%A9%22%7D`;
const V4 = { age: 30, beta: true, device: { os: 'ios', version: '17.4.0' }, tags: ['a', 'b'] };
const V4_S =
  `${EVAL}&context=%7B%22age%22%3A30%2C%22beta%22%3Atrue%2C%22device%22%3A%7B%22os%22%3A%22ios%22%2C` +
  '%22version%22%3A%2217.4.0%22%7D%2C%22tags%22%3A%5B%22a%22%2C%22b%22%5D%7D';

/**
 * Reads a request's query the way the server does: split on `&`, then on the first `=`, `+` is a
 * space, then percent-decode as UTF-8. `decodeURIComponent` alone leaves `+` as `+`, which is
 * why the `+` is replaced first. `context` is parsed as JSON.
 */
function serverQuery(url: string): Array<[string, unknown]> {
  const query = url.includes('?') ? url.slice(url.indexOf('?') + 1) : '';
  if (query === '') return [];
  return query.split('&').map((pair): [string, unknown] => {
    const eq = pair.indexOf('=');
    const name = decodeURIComponent(pair.slice(0, eq).replace(/\+/g, ' '));
    const value = decodeURIComponent(pair.slice(eq + 1).replace(/\+/g, ' '));
    return [name, name === 'context' ? JSON.parse(value) : value];
  });
}

/** The path and query of a request URL: what the plan's expected strings start with. */
function pathOf(url: string): string {
  return url.slice(BASE.length);
}

/**
 * A fake server for the flag routes. It answers `enabled` for a request whose `context`, decoded
 * with the server's rules, has `plan == "pro"` (reason `targeting_rule`), and `rollout` otherwise.
 */
function fakeFlagServer(): void {
  mockFetch.mockImplementation(async (url: string) => {
    const context = serverQuery(url).find(([name]) => name === 'context')?.[1] as
      | Record<string, unknown>
      | undefined;
    const matched = context?.plan === 'pro';
    return jsonResponse({
      key: 'f',
      enabled: matched,
      config: null,
      reason: matched ? 'targeting_rule' : 'rollout',
    });
  });
}

/** A fake server answering `enabled` by which attribute map was sent: pro, or anything else. */
const A = { plan: 'pro' };
const B = { plan: 'free' };

describe('flag evaluation sends the attributes as context (#1720)', () => {
  test('regression #1720: sends the attributes as context=<compact JSON>, percent-encoded, after user_id', async () => {
    mockJson(flagBody());
    await makeClient().evaluateFlag('f', 'u1', V2);
    expect(pathOf(lastCall().url)).toBe(V2_S);
  });

  test('regression #1720: matches a targeting rule on an attribute: the server reads plan=pro from the context', async () => {
    fakeFlagServer();
    const client = makeClient();
    const evaluation = await client.evaluateFlag('f', 'u1', V2);
    expect(evaluation.enabled).toBe(true);
    expect(await makeClient().evaluateFlag('f', 'u1')).toMatchObject({ enabled: false });
  });

  test('isFeatureEnabled sends the attributes too', async () => {
    mockJson(flagBody());
    expect(await makeClient().isFeatureEnabled('f', 'u1', V2)).toBe(true);
    expect(pathOf(lastCall().url)).toBe(V2_S);
  });

  test.each([
    ['V3', V3, V3_S],
    ['V3a', V3A, V3A_S],
    ['V4', V4, V4_S],
  ])('every vector is byte-exact: %s', async (_name, attributes, expected) => {
    mockJson(flagBody());
    await makeClient().evaluateFlag('f', 'u1', attributes);
    expect(pathOf(lastCall().url)).toBe(expected);
    // The value is JS's percent-encoding of the SDK's own JSON text.
    expect(pathOf(lastCall().url)).toBe(`${EVAL}&context=${encodeURIComponent(JSON.stringify(attributes))}`);
  });

  test.each([
    ['V3', V3],
    ['V3a', V3A],
    ['V4', V4],
  ])('the server decodes %s to the same object, with user_id u1 and no other parameter', async (_name, attributes) => {
    mockJson(flagBody());
    await makeClient().evaluateFlag('f', 'u1', attributes);
    const pairs = serverQuery(lastCall().url);
    expect(pairs.map(([name]) => name)).toEqual(['user_id', 'context']);
    expect(pairs[0][1]).toBe('u1');
    expect(pairs[1][1]).toEqual(attributes);
  });

  test('the decode helper reads + as a space, and the context needs no other reading', () => {
    expect(serverQuery('http://x/p?user_id=a+b&context=%7B%22q%22%3A%22c+d%22%7D')).toEqual([
      ['user_id', 'a b'],
      ['context', { q: 'c d' }],
    ]);
  });

  test('sends no context for no attributes or an empty object: the request is the one sent before', async () => {
    fakeFlagServer();
    const client = makeClient();
    await client.evaluateFlag('f', 'u1');
    expect(pathOf(lastCall().url)).toBe(EVAL);
    await makeClient().evaluateFlag('f', 'u1', {});
    expect(pathOf(lastCall().url)).toBe(EVAL);
    await makeClient().evaluateFlag('f', 'u1', undefined);
    expect(pathOf(lastCall().url)).toBe(EVAL);
  });

  test('sends exactly what the JavaScript SDK sends for an array (the server answers it 422)', async () => {
    mockJson(flagBody());
    await makeClient().evaluateFlag('f', 'u1', [1] as unknown as Record<string, unknown>);
    expect(pathOf(lastCall().url)).toBe(`${EVAL}&context=%5B1%5D`);
  });

  test('contextQuery is a verbatim copy of the JavaScript SDK function, comment included', () => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const fs = require('fs') as typeof import('fs');
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const path = require('path') as typeof import('path');
    const take = (file: string): string => {
      const source = fs.readFileSync(path.join(__dirname, file), 'utf8');
      const start = source.indexOf('/**\n * `&context=<url-encoded JSON>`');
      const end = source.indexOf('\n}\n', source.indexOf('function contextQuery(')) + 3;
      expect(start).toBeGreaterThan(-1);
      return source.slice(start, end);
    };
    const edge = take('../src/client.ts');
    expect(edge).toContain('function contextQuery(');
    expect(edge).toBe(take('../../js/src/client.ts'));
  });

  test('is byte-identical to the JavaScript SDK function for every input it is given', async () => {
    const inputs: unknown[] = [undefined, null, {}, V2, V3, V3A, V4, [], [1], 'text', 5, { 'ключ': 'значение' }, { a: undefined }];
    for (const attributes of inputs) {
      mockFetch.mockReset();
      mockJson(flagBody());
      await makeClient().evaluateFlag('f', 'u1', attributes as Record<string, unknown>);
      // The JavaScript SDK's contextQuery, written out here from its source.
      const a = attributes as Record<string, unknown> | undefined;
      const expected =
        !a || typeof a !== 'object' || Object.keys(a).length === 0 ? '' : `&context=${encodeURIComponent(JSON.stringify(a))}`;
      expect(pathOf(lastCall().url)).toBe(`${EVAL}${expected}`);
    }
  });
});

describe('getAllFlags sends the attributes as context (#1720)', () => {
  test.each([
    ['U0: none', undefined, USERS],
    ['U1: {}', {}, USERS],
    ['U2: {plan: pro}', V2, `${USERS}?context=%7B%22plan%22%3A%22pro%22%7D`],
  ])('%s', async (_name, attributes, expected) => {
    mockJson({ a: true });
    expect(await makeClient().getAllFlags('u1', attributes)).toEqual({ a: true });
    expect(pathOf(lastCall().url)).toBe(expected);
  });

  test('getAllFlags sends the attributes as context after the path, and nothing else', async () => {
    mockJson({ a: true });
    await makeClient().getAllFlags('u1', V2);
    const pairs = serverQuery(lastCall().url);
    expect(pairs).toEqual([['context', V2]]);
  });

  test('does not read or write the flag cache or the store', async () => {
    const store = makeStore();
    mockJson({ a: true });
    const client = makeClient({ store });
    await client.getAllFlags('u1', V2);
    expect(client.getEvaluatedFlags('u1')).toEqual([]);
    expect(client.flagCount).toBe(0);
    expect(store.data).toEqual({});
  });
});

describe('the cache keeps one answer per user and flag, with the attributes it was asked with (#1720)', () => {
  /** A fake server that answers enabled for plan pro (A) and disabled otherwise. */
  beforeEach(() => fakeFlagServer());

  test('(a) A then B: two requests, two answers', async () => {
    const client = makeClient();
    const answers = [
      (await client.evaluateFlag('f', 'u1', A)).enabled,
      (await client.evaluateFlag('f', 'u1', B)).enabled,
    ];
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(answers).toEqual([true, false]);
  });

  test('(b) the minimal pairs that differ in one key or one type: two requests each', async () => {
    for (const [first, second] of [
      [{ plan: 'pro' }, { tier: 'pro' }],
      [{ n: 1 }, { n: '1' }],
    ]) {
      mockFetch.mockClear();
      const client = makeClient();
      await client.evaluateFlag('f', 'u1', first);
      await client.evaluateFlag('f', 'u1', second);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    }
  });

  test('(c) A, B, A: three requests, because the one slot holds B when A comes back', async () => {
    const client = makeClient();
    const answers = [
      (await client.evaluateFlag('f', 'u1', A)).enabled,
      (await client.evaluateFlag('f', 'u1', B)).enabled,
      (await client.evaluateFlag('f', 'u1', A)).enabled,
    ];
    expect(mockFetch).toHaveBeenCalledTimes(3);
    expect(answers).toEqual([true, false, true]);
  });

  test('(d) A then no attributes: two requests, and the second is the no-attribute answer', async () => {
    const client = makeClient();
    expect((await client.evaluateFlag('f', 'u1', A)).enabled).toBe(true);
    expect((await client.evaluateFlag('f', 'u1')).enabled).toBe(false);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  test('(e) {} and no attributes share an answer: one request', async () => {
    const client = makeClient();
    await client.evaluateFlag('f', 'u1', {});
    await client.evaluateFlag('f', 'u1', {});
    expect(mockFetch).toHaveBeenCalledTimes(1);
    await client.evaluateFlag('f', 'u1');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('(f) A then A: one request', async () => {
    const client = makeClient();
    await client.evaluateFlag('f', 'u1', A);
    await client.evaluateFlag('f', 'u1', { plan: 'pro' });
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('the answer cached for other attributes is not served after the slot is replaced', async () => {
    const client = makeClient();
    await client.evaluateFlag('f', 'u1', A);
    await client.evaluateFlag('f', 'u1', B);
    expect(client.getCachedFlag('f', 'u1')).toMatchObject({ enabled: false });
  });

  test('evaluateFlagSync and getCachedFlag return the last answer cached, whatever attributes it was asked with', async () => {
    const client = makeClient();
    await client.evaluateFlag('f', 'u1', A);
    expect(client.evaluateFlagSync('f', 'u1')).toBe(true);
    expect(client.getCachedFlag('f', 'u1')).toEqual({ key: 'f', enabled: true, config: null });
    await client.evaluateFlag('f', 'u1', B);
    expect(client.evaluateFlagSync('f', 'u1')).toBe(false);
  });

  test('the keyless track fan-out lists the flag once, with the latest answer, after A and then B', async () => {
    const client = makeClient();
    await client.evaluateFlag('f', 'u1', A);
    await client.evaluateFlag('f', 'u1', B);
    expect(client.getEvaluatedFlags('u1')).toEqual(['f']);
    expect(client.flagCount).toBe(1);

    mockFetch.mockClear();
    mockJson({ success_count: 1, failure_count: 0 });
    await client.track('page_view', 'u1');
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect((bodyOf() as { events: Array<Record<string, unknown>> }).events).toEqual([
      { event_type: 'page_view', event_name: 'page_view', user_id: 'u1', metadata: {}, feature_flag_key: 'f' },
    ]);
  });

  test('failures are not cached under attributes either', async () => {
    mockFetch.mockReset();
    mockJson({}, 500);
    mockJson(flagBody({ key: 'f' }));
    const client = makeClient();
    expect((await client.evaluateFlag('f', 'u1', A)).enabled).toBe(false);
    expect((await client.evaluateFlag('f', 'u1', A)).enabled).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });
});

describe('concurrent calls share a request only when their attributes are the same (#1720)', () => {
  /** A fake server that answers each request after the test releases it. */
  function delayedServer(): { release: () => Promise<void> } {
    const waiting: Array<() => void> = [];
    mockFetch.mockImplementation(
      (url: string) =>
        new Promise<Response>((resolve) => {
          const context = serverQuery(url).find(([name]) => name === 'context')?.[1] as Record<string, unknown> | undefined;
          waiting.push(() => resolve(jsonResponse({ key: 'f', enabled: context?.plan === 'pro', config: null })));
        }),
    );
    return {
      release: async () => {
        // Let every call reach the fake before it answers, so no timing decides the count.
        await new Promise((resolve) => setTimeout(resolve, 0));
        waiting.splice(0).forEach((fn) => fn());
      },
    };
  }

  test('A and B at once: two requests and two answers', async () => {
    const server = delayedServer();
    const client = makeClient();
    const pa = client.evaluateFlag('f', 'u1', A);
    const pb = client.evaluateFlag('f', 'u1', B);
    await server.release();
    expect([(await pa).enabled, (await pb).enabled]).toEqual([true, false]);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  test('A and A at once: one request', async () => {
    const server = delayedServer();
    const client = makeClient();
    const p1 = client.evaluateFlag('f', 'u1', A);
    const p2 = client.evaluateFlag('f', 'u1', { plan: 'pro' });
    await server.release();
    expect((await p1).enabled).toBe(true);
    expect(await p2).toBe(await p1);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});

describe('the shared store is used only by calls without attributes (#1720)', () => {
  beforeEach(() => fakeFlagServer());

  test('A on one client writes nothing, and B on another client does not get A answer', async () => {
    const store = makeStore();
    await makeClient({ store }).evaluateFlag('f', 'u1', A);
    expect(store.data).toEqual({});
    expect((await makeClient({ store }).evaluateFlag('f', 'u1', B)).enabled).toBe(false);
    expect(store.data).toEqual({});
  });

  test('a call with attributes does not read a stored answer', async () => {
    const store = makeStore({ 'flag:u1:f': JSON.stringify({ key: 'f', enabled: false, config: null }) });
    expect((await makeClient({ store }).evaluateFlag('f', 'u1', A)).enabled).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('a call without attributes reads and writes the store under the key it always used', async () => {
    const store = makeStore();
    await makeClient({ store }).evaluateFlag('f', 'u1');
    expect(Object.keys(store.data)).toEqual(['flag:u1:f']);
    expect(store.data['flag:u1:f']).toBe(JSON.stringify({ key: 'f', enabled: false, config: null }));
    mockFetch.mockClear();
    await makeClient({ store }).evaluateFlag('f', 'u1', {});
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe('attributes that cannot be written as JSON (#1720)', () => {
  const CANARY = 'attrcanary7f3';
  let spies: jest.SpyInstance[];
  beforeEach(() => {
    spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((m) =>
      jest.spyOn(console, m).mockImplementation(() => undefined),
    );
  });
  afterEach(() => spies.forEach((spy) => spy.mockRestore()));

  function cyclic(): Record<string, unknown> {
    const attributes: Record<string, unknown> = { note: CANARY };
    attributes.self = attributes;
    return attributes;
  }
  /** A BigInt and a cycle: the two values JSON.stringify refuses. Each carries the canary. */
  const unencodable: Array<() => Record<string, unknown>> = [() => ({ note: CANARY, n: BigInt(10) }), cyclic];

  test('attributes that cannot be written as JSON send nothing, cache nothing and report the flag as disabled', async () => {
    for (const make of unencodable) {
      const store = makeStore();
      const client = makeClient({ store });
      expect(await client.evaluateFlag('f', 'u1', make())).toEqual({ key: 'f', enabled: false, config: null });
      expect(await client.isFeatureEnabled('f', 'u1', make())).toBe(false);
      expect(await client.getAllFlags('u1', make())).toEqual({});
      expect(mockFetch).not.toHaveBeenCalled();
      expect(client.flagCount).toBe(0);
      expect(store.data).toEqual({});
    }
  });

  test('attributes that cannot be written as JSON print one fixed warning that names no attribute', async () => {
    for (const make of unencodable) {
      spies.forEach((spy) => spy.mockClear());
      await makeClient().evaluateFlag('f', 'u1', make());
      const warn = spies[2];
      expect(warn).toHaveBeenCalledTimes(1);
      expect(warn.mock.calls[0]).toEqual([
        'controlvar edge sdk: the attributes cannot be written as JSON, so nothing was sent and the flag is reported as disabled',
      ]);
      for (const spy of spies) expect(JSON.stringify(spy.mock.calls)).not.toContain(CANARY);
    }
  });
});

describe('request errors name the path without its query (#1720)', () => {
  const CANARY = 'attrcanary7f3';
  const ATTRIBUTES = { note: CANARY, quote: "it's" };
  let spies: jest.SpyInstance[];
  beforeEach(() => {
    spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((m) =>
      jest.spyOn(console, m).mockImplementation(() => undefined),
    );
  });
  afterEach(() => spies.forEach((spy) => spy.mockRestore()));

  /** The error a call to the HTTP layer rejects with (it is private: called as the client does). */
  async function failure(client: EdgeExperimentationClient, path: string): Promise<Error & { cause?: unknown }> {
    try {
      await (client as unknown as { requestJson(p: string): Promise<unknown> }).requestJson(path);
    } catch (err) {
      return err as Error;
    }
    throw new Error('expected the request to fail');
  }
  const PATH = `/api/v1/feature-flags/evaluate/f?user_id=u1&context=${encodeURIComponent(JSON.stringify(ATTRIBUTES))}`;

  test('an API error names the path alone, with the status', async () => {
    mockJson({ detail: 'nope' }, 500);
    const err = await failure(makeClient(), PATH);
    expect(err).toBeInstanceOf(EdgeApiError);
    expect(err.message).toBe('API error 500 (/api/v1/feature-flags/evaluate/f)');
  });

  test('an error from fetch that quotes the URL, as passed and as the URL parser writes it, is stripped and has no cause', async () => {
    const asParsed = PATH.replace(/'/g, '%27');
    for (const quoted of [PATH, asParsed]) {
      mockFetch.mockReset();
      mockFetch.mockRejectedValueOnce(
        Object.assign(new TypeError(`Failed to parse URL from ${BASE}${quoted}`), { cause: new Error(`inner ${quoted}`) }),
      );
      const err = await failure(makeClient(), PATH);
      expect(err.message).not.toContain(CANARY);
      expect(err.message).not.toContain('user_id');
      expect(err.message).not.toContain('context');
      expect(err.message).toBe(`Failed to parse URL from ${BASE}/api/v1/feature-flags/evaluate/f`);
      expect(err.cause).toBeUndefined();
      expect(Object.getOwnPropertyNames(err)).not.toContain('cause');
      expect(err.name).toBe('TypeError');
    }
  });

  test('a failing evaluateFlag and getAllFlags log nothing and return the failure values', async () => {
    mockFetch.mockRejectedValue(new TypeError(`Failed to parse URL from ${BASE}${PATH}`));
    const client = makeClient();
    expect(await client.evaluateFlag('f', 'u1', ATTRIBUTES)).toEqual({ key: 'f', enabled: false, config: null });
    expect(await client.getAllFlags('u1', ATTRIBUTES)).toEqual({});
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
  });

  test('a request without a query keeps the error fetch threw', async () => {
    const original = new Error('network down');
    mockFetch.mockRejectedValueOnce(original);
    expect(await failure(makeClient(), '/api/v1/tracking/batch')).toBe(original);
  });
});
