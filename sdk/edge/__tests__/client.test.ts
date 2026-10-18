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

  test('returns a disabled evaluation on 404 (flag not ACTIVE) and does not cache it', async () => {
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

  test('returns null on 404 (experiment not ACTIVE) and never caches the failure', async () => {
    mockJson({ detail: 'Experiment not found' }, 404);
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
