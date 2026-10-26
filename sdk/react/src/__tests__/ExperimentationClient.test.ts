import { ExperimentationClient } from '../client/ExperimentationClient';
import {
  SdkConfig,
  UserContext,
  FeatureFlagEvaluateResponse,
  ExperimentAssignResponse,
} from '../client/types';

const baseConfig: SdkConfig = {
  apiKey: 'test-api-key',
  baseUrl: 'https://api.example.com',
};

const user: UserContext = {
  userId: 'user-123',
  attributes: { device: 'mobile', country: 'US' },
};
const plainUser: UserContext = { userId: 'user-123' };
/** `encodeURIComponent(JSON.stringify(user.attributes))` */
const userContext = '%7B%22device%22%3A%22mobile%22%2C%22country%22%3A%22US%22%7D';

const flagOn: FeatureFlagEvaluateResponse = { key: 'my-flag', enabled: true, config: null };
const flagOff: FeatureFlagEvaluateResponse = { key: 'my-flag', enabled: false, config: null };
const flagWithVariant: FeatureFlagEvaluateResponse = {
  key: 'my-flag',
  enabled: true,
  config: { variant: 'treatment', color: 'green' },
};
const flagWithConfig: FeatureFlagEvaluateResponse = {
  key: 'my-flag',
  enabled: true,
  config: { engine: 'v2' },
};

const assignment: ExperimentAssignResponse = {
  experiment_key: 'checkout',
  user_id: 'user-123',
  variant_id: 'var-2',
  variant_name: 'one_page',
  is_control: false,
  configuration: { steps: 1 },
};
const controlAssignment: ExperimentAssignResponse = {
  experiment_key: 'checkout',
  user_id: 'user-123',
  variant_id: 'var-1',
  variant_name: 'standard',
  is_control: true,
  configuration: { steps: 3 },
};

const expectedHeaders = { 'X-API-Key': 'test-api-key', 'Content-Type': 'application/json' };

/** A fixed clock start for the tests that move `Date.now`. */
const T0 = 1_700_000_000_000;
const SECOND = 1_000;
const HOUR = 3_600 * SECOND;
const BATCH_URL = 'https://api.example.com/api/v1/tracking/batch';
const TRACK_URL = 'https://api.example.com/api/v1/tracking/track';
type Body = { experiment_key?: string; feature_flag_key?: string };
/** The batch's entries as `experiment_key`, or `flag:<feature_flag_key>`. */
const batchKeys = (body: { events: Body[] }) => body.events.map(e => e.experiment_key ?? `flag:${e.feature_flag_key}`);

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

function mockFetch(body: unknown, status = 200): jest.Mock {
  const mock = jest.fn().mockResolvedValue(jsonResponse(body, status));
  global.fetch = mock;
  return mock;
}

/** Queue responses in order; an Error entry rejects (network failure). */
function mockFetchSequence(...steps: Array<{ body: unknown; status?: number } | Error>): jest.Mock {
  const mock = jest.fn();
  for (const step of steps) {
    if (step instanceof Error) mock.mockRejectedValueOnce(step);
    else mock.mockResolvedValueOnce(jsonResponse(step.body, step.status ?? 200));
  }
  global.fetch = mock;
  return mock;
}

function call(mock: jest.Mock, index = 0) {
  const [url, init] = mock.mock.calls[index];
  return { url: url as string, init, body: init?.body ? JSON.parse(init.body) : undefined };
}

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
});

// ─── Constructor validation ───────────────────────────────────────────────────

describe('ExperimentationClient constructor', () => {
  it('throws when apiKey is missing', () => {
    expect(
      () => new ExperimentationClient({ apiKey: '', baseUrl: 'https://api.example.com' })
    ).toThrow('apiKey is required');
  });

  it('throws when baseUrl is missing', () => {
    expect(() => new ExperimentationClient({ apiKey: 'key', baseUrl: '' })).toThrow(
      'baseUrl is required'
    );
  });

  it('creates client with valid config', () => {
    expect(() => new ExperimentationClient(baseConfig)).not.toThrow();
  });

  it('strips trailing slash from baseUrl', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient({ ...baseConfig, baseUrl: 'https://api.example.com/' });
    await client.evaluateFeatureFlag(user, 'my-flag');
    expect(call(fetchMock).url).toMatch(/^https:\/\/api\.example\.com\/api/);
    expect(call(fetchMock).url).not.toMatch(/example\.com\/\/api/);
  });
});

// ─── evaluateFeatureFlag — request ────────────────────────────────────────────

describe('evaluateFeatureFlag request', () => {
  it('GETs /api/v1/feature-flags/evaluate/{key}?user_id=… when the user has no attributes', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(plainUser, 'my-flag');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { url, init } = call(fetchMock);
    expect(url).toBe('https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-123');
    expect(init.method).toBe('GET');
  });

  it('appends context=<url-encoded JSON of user.attributes> when the user has attributes', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(user, 'my-flag');

    const { url, init } = call(fetchMock);
    expect(url).toBe(
      `https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-123&context=${userContext}`
    );
    expect(userContext).toBe('%7B%22device%22%3A%22mobile%22%2C%22country%22%3A%22US%22%7D');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
  });

  it('the context parameter round-trips to the original attributes', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    const attributes = { os: 'iOS', os_version: '17.4.0', tier: 'premium', employee: false, app: { version: '3.2.1' } };
    await client.evaluateFeatureFlag({ userId: 'user-123', attributes }, 'my-flag');

    const query = new URL(call(fetchMock).url).searchParams;
    expect(query.get('user_id')).toBe('user-123');
    expect(JSON.parse(query.get('context')!)).toEqual(attributes);
  });

  it('omits context when attributes is an empty object', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag({ userId: 'user-123', attributes: {} }, 'my-flag');
    expect(call(fetchMock).url).toBe(
      'https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-123'
    );
  });

  it('URL-encodes flag keys that contain special characters', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(plainUser, 'my flag/key');
    expect(call(fetchMock).url).toBe(
      'https://api.example.com/api/v1/feature-flags/evaluate/my%20flag%2Fkey?user_id=user-123'
    );
  });

  it('URL-encodes flag key, user id and context together', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag({ userId: 'user a/b@c', attributes: { q: 'a&b=c' } }, 'my flag/key');
    expect(call(fetchMock).url).toBe(
      'https://api.example.com/api/v1/feature-flags/evaluate/my%20flag%2Fkey' +
        '?user_id=user%20a%2Fb%40c&context=%7B%22q%22%3A%22a%26b%3Dc%22%7D'
    );
  });

  it('URL-encodes the user id in the query string', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag({ userId: 'user a/b@c' }, 'my-flag');
    expect(call(fetchMock).url).toBe(
      'https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user%20a%2Fb%40c'
    );
  });

  it('sends exactly the X-API-Key and Content-Type headers', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(user, 'my-flag');
    expect(call(fetchMock).init.headers).toEqual(expectedHeaders);
  });

  it('does not send an X-User-ID header (user id travels in the query string)', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(user, 'my-flag');
    expect(call(fetchMock).init.headers).not.toHaveProperty('X-User-ID');
  });

  it('sends no request body', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(user, 'my-flag');
    expect(call(fetchMock).init.body).toBeUndefined();
  });

  it('passes an AbortSignal that aborts after timeoutMs', async () => {
    jest.useFakeTimers();
    let signal: AbortSignal | undefined;
    global.fetch = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
      signal = init.signal as AbortSignal;
      return new Promise((_resolve, reject) => {
        signal!.addEventListener('abort', () => reject(new Error('The operation was aborted')));
      });
    });

    const client = new ExperimentationClient({ ...baseConfig, timeoutMs: 50 });
    const pending = client.evaluateFeatureFlag(user, 'my-flag');
    expect(signal?.aborted).toBe(false);

    jest.advanceTimersByTime(51);
    expect(signal?.aborted).toBe(true);
    await expect(pending).rejects.toThrow('aborted');
  });
});

// ─── evaluateFeatureFlag — result ─────────────────────────────────────────────

describe('evaluateFeatureFlag result', () => {
  it('returns null for a disabled flag', async () => {
    mockFetch(flagOff);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBeNull();
  });

  it('returns "on" for an enabled flag with a null config', async () => {
    mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBe('on');
  });

  it('returns "on" when the config has no variant field', async () => {
    mockFetch(flagWithConfig);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBe('on');
  });

  it('returns "on" when config.variant is not a string', async () => {
    mockFetch({ key: 'my-flag', enabled: true, config: { variant: 3 } });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBe('on');
  });

  it('returns config.variant when it is a string', async () => {
    mockFetch(flagWithVariant);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBe('treatment');
  });

  it('returns null for a disabled flag even when config names a variant', async () => {
    mockFetch({ key: 'my-flag', enabled: false, config: { variant: 'treatment' } });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBeNull();
  });

  it('throws an Error on a non-2xx response', async () => {
    mockFetch({}, 500);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).rejects.toThrow('API error: 500');
  });

  it('throws an Error on a 404 response (flag not active)', async () => {
    mockFetch({ detail: 'not found' }, 404);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).rejects.toThrow('API error: 404');
  });

  it('propagates network failures', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network down'));
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).rejects.toThrow('Network down');
  });
});

// ─── evaluateFeatureFlagDetailed ──────────────────────────────────────────────

describe('evaluateFeatureFlagDetailed', () => {
  it('returns the full evaluation for an enabled flag', async () => {
    mockFetch(flagWithVariant);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlagDetailed(user, 'my-flag')).resolves.toEqual({
      flagKey: 'my-flag',
      variant: 'treatment',
      isEnabled: true,
      config: { variant: 'treatment', color: 'green' },
      loading: false,
      error: null,
    });
  });

  it('returns a disabled evaluation for a disabled flag', async () => {
    mockFetch(flagOff);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlagDetailed(user, 'my-flag')).resolves.toEqual({
      flagKey: 'my-flag',
      variant: null,
      isEnabled: false,
      config: null,
      loading: false,
      error: null,
    });
  });

  it('passes the server config through untouched', async () => {
    mockFetch({ key: 'my-flag', enabled: true, config: { engine: 'v2', limits: [1, 2] } });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.evaluateFeatureFlagDetailed(user, 'my-flag');
    expect(result.config).toEqual({ engine: 'v2', limits: [1, 2] });
  });

  it('normalises a missing config to null', async () => {
    mockFetch({ key: 'my-flag', enabled: true });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.evaluateFeatureFlagDetailed(user, 'my-flag');
    expect(result.config).toBeNull();
    expect(result.variant).toBe('on');
  });

  it('uses the requested key as flagKey', async () => {
    mockFetch({ key: 'server-key', enabled: true, config: null });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.evaluateFeatureFlagDetailed(user, 'requested-key');
    expect(result.flagKey).toBe('requested-key');
  });

  it('throws on failure (same contract as evaluateFeatureFlag)', async () => {
    mockFetch({}, 503);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlagDetailed(user, 'my-flag')).rejects.toThrow('API error: 503');
  });

  it('exposes the server reason when the response carries one', async () => {
    mockFetch({ key: 'my-flag', enabled: true, config: null, reason: 'targeting_rule' });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlagDetailed(user, 'my-flag')).resolves.toEqual({
      flagKey: 'my-flag',
      variant: 'on',
      isEnabled: true,
      config: null,
      loading: false,
      error: null,
      reason: 'targeting_rule',
    });
  });

  it('exposes reason for a disabled flag too', async () => {
    mockFetch({ key: 'my-flag', enabled: false, config: null, reason: 'rollout' });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.evaluateFeatureFlagDetailed(user, 'my-flag');
    expect(result.isEnabled).toBe(false);
    expect(result.reason).toBe('rollout');
  });

  it('leaves reason undefined when the server omits it (older servers)', async () => {
    mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    const result = await client.evaluateFeatureFlagDetailed(user, 'my-flag');
    expect(result.reason).toBeUndefined();
    expect(result).not.toHaveProperty('reason');
  });

  it('ignores a non-string reason', async () => {
    mockFetch({ key: 'my-flag', enabled: true, config: null, reason: 42 });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.evaluateFeatureFlagDetailed(user, 'my-flag');
    expect(result.reason).toBeUndefined();
  });
});

// ─── Flag cache ───────────────────────────────────────────────────────────────

describe('flag cache', () => {
  it('uses the cached result on the second call — fetch is only called once', async () => {
    const fetchMock = mockFetch(flagWithVariant);
    const client = new ExperimentationClient(baseConfig);

    const first = await client.evaluateFeatureFlag(user, 'my-flag');
    const second = await client.evaluateFeatureFlag(user, 'my-flag');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(first).toBe('treatment');
    expect(second).toBe('treatment');
  });

  it('caches disabled results too', async () => {
    const fetchMock = mockFetch(flagOff);
    const client = new ExperimentationClient(baseConfig);

    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBeNull();
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('shares one in-flight request between concurrent callers', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);

    const [a, b] = await Promise.all([
      client.evaluateFeatureFlag(user, 'my-flag'),
      client.evaluateFeatureFlag(user, 'my-flag'),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(a).toBe('on');
    expect(b).toBe('on');
  });

  it('keeps separate cache entries per user', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);

    await client.evaluateFeatureFlag({ userId: 'user-A' }, 'my-flag');
    await client.evaluateFeatureFlag({ userId: 'user-B' }, 'my-flag');

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('keeps separate cache entries per flag key', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);

    await client.evaluateFeatureFlag(user, 'flag-a');
    await client.evaluateFeatureFlag(user, 'flag-b');
    await client.evaluateFeatureFlag(user, 'flag-a');

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('makes a fresh fetch after clearCache', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);

    await client.evaluateFeatureFlag(user, 'my-flag');
    client.clearCache();
    await client.evaluateFeatureFlag(user, 'my-flag');

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('is keyed by user + flag only: changed attributes hit the cache until clearCache', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);

    await client.evaluateFeatureFlag({ userId: 'user-123', attributes: { country: 'US' } }, 'my-flag');
    await client.evaluateFeatureFlag({ userId: 'user-123', attributes: { country: 'DE' } }, 'my-flag');
    expect(fetchMock).toHaveBeenCalledTimes(1);

    client.clearCache();
    await client.evaluateFeatureFlag({ userId: 'user-123', attributes: { country: 'DE' } }, 'my-flag');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toContain('&context=%7B%22country%22%3A%22DE%22%7D');
  });

  it('does not cache a non-2xx response — the next call re-fetches', async () => {
    const fetchMock = mockFetchSequence({ body: {}, status: 500 }, { body: flagOn });
    const client = new ExperimentationClient(baseConfig);

    await expect(client.evaluateFeatureFlag(user, 'my-flag')).rejects.toThrow('API error: 500');
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBe('on');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not cache a network failure — the next call re-fetches', async () => {
    const fetchMock = mockFetchSequence(new Error('Network down'), { body: flagOn });
    const client = new ExperimentationClient(baseConfig);

    await expect(client.evaluateFeatureFlag(user, 'my-flag')).rejects.toThrow('Network down');
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBe('on');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('serves from cache until the TTL elapses', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 1_000 });

    await client.evaluateFeatureFlag(user, 'my-flag');
    now.mockReturnValue(1_000_999);
    await client.evaluateFeatureFlag(user, 'my-flag');

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('re-fetches after the TTL has expired', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = mockFetchSequence({ body: flagOn }, { body: flagOff });
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 1_000 });

    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBe('on');
    now.mockReturnValue(1_001_001);
    await expect(client.evaluateFeatureFlag(user, 'my-flag')).resolves.toBeNull();

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ─── assignExperiment — request ───────────────────────────────────────────────

describe('assignExperiment request', () => {
  it('POSTs to /api/v1/tracking/assign', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { url, init } = call(fetchMock);
    expect(url).toBe('https://api.example.com/api/v1/tracking/assign');
    expect(init.method).toBe('POST');
  });

  it('sends {experiment_key, user_id, context: user.attributes} as the JSON body', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');

    expect(call(fetchMock).body).toEqual({
      experiment_key: 'checkout',
      user_id: 'user-123',
      context: { device: 'mobile', country: 'US' },
    });
  });

  it('omits context when the user has no attributes', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment({ userId: 'user-123' }, 'checkout');

    expect(call(fetchMock).body).toEqual({ experiment_key: 'checkout', user_id: 'user-123' });
  });

  it('sends exactly the X-API-Key and Content-Type headers', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    expect(call(fetchMock).init.headers).toEqual(expectedHeaders);
  });
});

// ─── assignExperiment — result ────────────────────────────────────────────────

describe('assignExperiment result', () => {
  it('maps the server response to an ExperimentAssignment', async () => {
    mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.assignExperiment(user, 'checkout')).resolves.toEqual({
      experimentKey: 'checkout',
      variantKey: 'one_page',
      variantName: 'one_page',
      variantId: 'var-2',
      isControl: false,
      configuration: { steps: 1 },
      loading: false,
      error: null,
      assigned: true,
    });
  });

  it('defaults assigned to true and leaves reason undefined when the server omits both (older servers)', async () => {
    mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    const result = await client.assignExperiment(user, 'checkout');
    expect(result.assigned).toBe(true);
    expect(result.reason).toBeUndefined();
    expect(result).not.toHaveProperty('reason');
  });

  it('maps assigned: true + reason: "assigned" from the server', async () => {
    mockFetch({ ...assignment, assigned: true, reason: 'assigned' });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.assignExperiment(user, 'checkout');
    expect(result.assigned).toBe(true);
    expect(result.reason).toBe('assigned');
    expect(result.isControl).toBe(false);
  });

  it('maps an ineligible user (assigned: false + reason) onto the control variant the server returned', async () => {
    mockFetch({ ...controlAssignment, assigned: false, reason: 'mutual_exclusion' });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.assignExperiment(user, 'checkout');
    expect(result).toEqual({
      experimentKey: 'checkout',
      variantKey: 'standard',
      variantName: 'standard',
      variantId: 'var-1',
      isControl: true,
      configuration: { steps: 3 },
      loading: false,
      error: null,
      assigned: false,
      reason: 'mutual_exclusion',
    });
    // An ineligible result is still cached: the server would answer the same way again.
    expect((await client.assignExperiment(user, 'checkout')).reason).toBe('mutual_exclusion');
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it.each(['holdout', 'mutual_exclusion', 'targeting'])(
    'carries assigned: false and reason %s for a user the server did not enrol',
    async reason => {
      mockFetch({ ...controlAssignment, assigned: false, reason });
      const client = new ExperimentationClient(baseConfig);
      const result = await client.assignExperiment(user, 'checkout');
      expect(result.assigned).toBe(false);
      expect(result.reason).toBe(reason);
      expect(result.isControl).toBe(true);
    }
  );

  it('marks control assignments with isControl: true', async () => {
    mockFetch(controlAssignment);
    const client = new ExperimentationClient(baseConfig);
    const result = await client.assignExperiment(user, 'checkout');
    expect(result.isControl).toBe(true);
    expect(result.variantKey).toBe('standard');
    expect(result.configuration).toEqual({ steps: 3 });
  });

  it('normalises a missing configuration to null and missing is_control to false', async () => {
    mockFetch({ experiment_key: 'checkout', user_id: 'user-123', variant_id: 'v', variant_name: 'x' });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.assignExperiment(user, 'checkout');
    expect(result.configuration).toBeNull();
    expect(result.isControl).toBe(false);
  });

  it('normalises a missing variant_id to null', async () => {
    mockFetch({ experiment_key: 'checkout', user_id: 'user-123', variant_name: 'x' });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.assignExperiment(user, 'checkout');
    expect(result.variantId).toBeNull();
  });

  it('throws an Error on a 404 response (no active experiment)', async () => {
    mockFetch({ detail: 'not found' }, 404);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.assignExperiment(user, 'missing')).rejects.toThrow('API error: 404');
  });

  it('propagates network failures', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network down'));
    const client = new ExperimentationClient(baseConfig);
    await expect(client.assignExperiment(user, 'checkout')).rejects.toThrow('Network down');
  });
});

// ─── Assignment cache ─────────────────────────────────────────────────────────

describe('assignment cache', () => {
  it('returns the cached assignment on the second call — fetch is only called once', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);

    const first = await client.assignExperiment(user, 'checkout');
    const second = await client.assignExperiment(user, 'checkout');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(second).toBe(first);
  });

  it('shares one in-flight request between concurrent callers', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);

    const [a, b] = await Promise.all([
      client.assignExperiment(user, 'checkout'),
      client.assignExperiment(user, 'checkout'),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(a).toBe(b);
  });

  it('keeps separate cache entries per user', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);

    await client.assignExperiment({ userId: 'user-A' }, 'checkout');
    await client.assignExperiment({ userId: 'user-B' }, 'checkout');

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('keeps separate cache entries per experiment key', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);

    await client.assignExperiment(user, 'exp-a');
    await client.assignExperiment(user, 'exp-b');
    await client.assignExperiment(user, 'exp-a');

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not cache a failed assignment — the next call re-fetches', async () => {
    const fetchMock = mockFetchSequence({ body: {}, status: 404 }, { body: assignment });
    const client = new ExperimentationClient(baseConfig);

    await expect(client.assignExperiment(user, 'checkout')).rejects.toThrow('API error: 404');
    const result = await client.assignExperiment(user, 'checkout');

    expect(result.variantKey).toBe('one_page');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('re-fetches after the TTL has expired', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 1_000 });

    await client.assignExperiment(user, 'checkout');
    now.mockReturnValue(1_001_001);
    await client.assignExperiment(user, 'checkout');

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('makes a fresh fetch after clearCache', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);

    await client.assignExperiment(user, 'checkout');
    client.clearCache();
    await client.assignExperiment(user, 'checkout');

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ─── getAssignments ───────────────────────────────────────────────────────────

describe('getAssignments', () => {
  it('returns an empty array before any assignment', () => {
    const client = new ExperimentationClient(baseConfig);
    expect(client.getAssignments('user-123')).toEqual([]);
  });

  it('returns the cached assignments for the user', async () => {
    mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    const result = await client.assignExperiment(user, 'checkout');
    expect(client.getAssignments('user-123')).toEqual([result]);
  });

  it('returns one entry per experiment, in assignment order', async () => {
    mockFetchSequence(
      { body: { ...assignment, experiment_key: 'exp-a' } },
      { body: { ...controlAssignment, experiment_key: 'exp-b' } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'exp-a');
    await client.assignExperiment(user, 'exp-b');

    expect(client.getAssignments('user-123').map(a => a.experimentKey)).toEqual(['exp-a', 'exp-b']);
  });

  it('does not include other users’ assignments', async () => {
    mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment({ userId: 'someone-else' }, 'checkout');
    expect(client.getAssignments('user-123')).toEqual([]);
  });

  it('does not include failed assignments', async () => {
    mockFetch({}, 404);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.assignExperiment(user, 'checkout')).rejects.toThrow();
    expect(client.getAssignments('user-123')).toEqual([]);
  });

  it('does not include expired assignments', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    mockFetch(assignment);
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 1_000 });
    await client.assignExperiment(user, 'checkout');

    expect(client.getAssignments('user-123')).toHaveLength(1);
    now.mockReturnValue(1_001_001);
    expect(client.getAssignments('user-123')).toEqual([]);
  });

  it('is emptied by clearCache', async () => {
    mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    client.clearCache();
    expect(client.getAssignments('user-123')).toEqual([]);
  });
});

// ─── getEvaluatedFlags ────────────────────────────────────────────────────────

describe('getEvaluatedFlags', () => {
  it('returns an empty array before any evaluation', () => {
    const client = new ExperimentationClient(baseConfig);
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
  });

  it('returns the keys of flags evaluated for the user, in evaluation order', async () => {
    mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(user, 'flag-a');
    await client.evaluateFeatureFlag(user, 'flag-b');
    expect(client.getEvaluatedFlags('user-123')).toEqual(['flag-a', 'flag-b']);
  });

  it('includes flags that evaluated as disabled', async () => {
    mockFetch(flagOff);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(user, 'flag-off');
    expect(client.getEvaluatedFlags('user-123')).toEqual(['flag-off']);
  });

  it('does not include other users’ flags', async () => {
    mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag({ userId: 'someone-else' }, 'flag-a');
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
  });

  it('does not include failed evaluations', async () => {
    mockFetch({}, 500);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFeatureFlag(user, 'flag-a')).rejects.toThrow();
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
  });

  it('does not include expired evaluations', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    mockFetch(flagOn);
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 1_000 });
    await client.evaluateFeatureFlag(user, 'flag-a');

    expect(client.getEvaluatedFlags('user-123')).toEqual(['flag-a']);
    now.mockReturnValue(1_001_001);
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
  });

  it('is emptied by clearCache', async () => {
    mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(user, 'flag-a');
    client.clearCache();
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
  });
});

// ─── trackEvent — explicit keys ───────────────────────────────────────────────

describe('trackEvent with explicit keys', () => {
  function okFetch(): jest.Mock {
    const mock = jest.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) });
    global.fetch = mock;
    return mock;
  }

  it('POSTs a single event to /api/v1/tracking/track when experimentKey is given', async () => {
    const fetchMock = okFetch();
    const client = new ExperimentationClient(baseConfig);
    await client.trackEvent('user-123', 'purchase', { amount: 99 }, { experimentKey: 'checkout' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { url, init } = call(fetchMock);
    expect(url).toBe('https://api.example.com/api/v1/tracking/track');
    expect(init.method).toBe('POST');
  });

  it('sends the exact JSON body for an experiment event', async () => {
    const fetchMock = okFetch();
    const client = new ExperimentationClient(baseConfig);
    await client.trackEvent('user-123', 'purchase', { amount: 99 }, { experimentKey: 'checkout' });

    expect(call(fetchMock).body).toEqual({
      event_type: 'purchase',
      event_name: 'purchase',
      user_id: 'user-123',
      experiment_key: 'checkout',
      metadata: { amount: 99 },
    });
  });

  it('sends feature_flag_key (and no experiment_key) when featureFlagKey is given', async () => {
    const fetchMock = okFetch();
    const client = new ExperimentationClient(baseConfig);
    await client.trackEvent('user-123', 'search', { query: 'shoes' }, { featureFlagKey: 'new-search' });

    expect(call(fetchMock).url).toBe('https://api.example.com/api/v1/tracking/track');
    expect(call(fetchMock).body).toEqual({
      event_type: 'search',
      event_name: 'search',
      user_id: 'user-123',
      feature_flag_key: 'new-search',
      metadata: { query: 'shoes' },
    });
  });

  it('sends both keys when both are given', async () => {
    const fetchMock = okFetch();
    const client = new ExperimentationClient(baseConfig);
    await client.trackEvent('user-123', 'click', undefined, {
      experimentKey: 'checkout',
      featureFlagKey: 'new-search',
    });

    expect(call(fetchMock).body).toMatchObject({
      experiment_key: 'checkout',
      feature_flag_key: 'new-search',
    });
  });

  it('uses options.eventType as event_type and keeps eventName as event_name', async () => {
    const fetchMock = okFetch();
    const client = new ExperimentationClient(baseConfig);
    await client.trackEvent('user-123', 'hero_cta_click', undefined, {
      experimentKey: 'hero',
      eventType: 'conversion',
    });

    expect(call(fetchMock).body).toMatchObject({ event_type: 'conversion', event_name: 'hero_cta_click' });
  });

  it('sends value and an ISO-8601 timestamp when given', async () => {
    const fetchMock = okFetch();
    const client = new ExperimentationClient(baseConfig);
    const timestamp = new Date('2026-09-11T12:34:56.000Z');
    await client.trackEvent('user-123', 'purchase', { order_id: 'o-1' }, {
      experimentKey: 'checkout',
      value: 85.5,
      timestamp,
    });

    expect(call(fetchMock).body).toEqual({
      event_type: 'purchase',
      event_name: 'purchase',
      user_id: 'user-123',
      experiment_key: 'checkout',
      value: 85.5,
      metadata: { order_id: 'o-1' },
      timestamp: '2026-09-11T12:34:56.000Z',
    });
  });

  it('omits metadata, value and timestamp when not provided', async () => {
    const fetchMock = okFetch();
    const client = new ExperimentationClient(baseConfig);
    await client.trackEvent('user-123', 'click', undefined, { experimentKey: 'checkout' });

    expect(call(fetchMock).body).toEqual({
      event_type: 'click',
      event_name: 'click',
      user_id: 'user-123',
      experiment_key: 'checkout',
    });
  });

  it('sends exactly the X-API-Key and Content-Type headers', async () => {
    const fetchMock = okFetch();
    const client = new ExperimentationClient(baseConfig);
    await client.trackEvent('user-123', 'click', undefined, { experimentKey: 'checkout' });
    expect(call(fetchMock).init.headers).toEqual(expectedHeaders);
  });

  it('does not fan out to cached assignments when an explicit key is given', async () => {
    const fetchMock = mockFetchSequence({ body: assignment }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    await client.trackEvent('user-123', 'click', undefined, { featureFlagKey: 'new-search' });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toBe('https://api.example.com/api/v1/tracking/track');
  });

  it('does NOT throw on a network error (fire-and-forget)', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network failure'));
    const client = new ExperimentationClient(baseConfig);
    await expect(
      client.trackEvent('user-123', 'click', undefined, { experimentKey: 'checkout' })
    ).resolves.toBeUndefined();
  });

  it('does NOT throw on a 500 server error', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 500 });
    const client = new ExperimentationClient(baseConfig);
    await expect(
      client.trackEvent('user-123', 'click', undefined, { experimentKey: 'checkout' })
    ).resolves.toBeUndefined();
  });

  it('does NOT throw on a 404 (unknown key)', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 404 });
    const client = new ExperimentationClient(baseConfig);
    await expect(
      client.trackEvent('user-123', 'click', undefined, { experimentKey: 'nope' })
    ).resolves.toBeUndefined();
  });
});

// ─── trackEvent — fan-out ─────────────────────────────────────────────────────

describe('trackEvent fan-out (no explicit keys)', () => {
  it('sends the event once to /api/v1/tracking/track with no key and resolves when nothing is attributed to the user', async () => {
    const fetchMock = mockFetch({});
    const client = new ExperimentationClient(baseConfig);
    await expect(
      client.trackEvent('user-123', 'page_view', { page: '/' }, { value: 2 })
    ).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { url, init, body } = call(fetchMock);
    expect(url).toBe('https://api.example.com/api/v1/tracking/track');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual(expectedHeaders);
    expect(body).toEqual({
      event_type: 'page_view',
      event_name: 'page_view',
      user_id: 'user-123',
      value: 2,
      metadata: { page: '/' },
    });
  });

  it("never sends another user's keys: the event goes once to /api/v1/tracking/track with no key", async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment({ userId: 'someone-else' }, 'checkout');
    fetchMock.mockClear();

    await client.trackEvent('user-123', 'page_view');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(call(fetchMock).url).toBe('https://api.example.com/api/v1/tracking/track');
    expect(call(fetchMock).body).toEqual({ event_type: 'page_view', event_name: 'page_view', user_id: 'user-123' });
  });

  it('does NOT throw when the untagged send fails', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network failure'));
    const client = new ExperimentationClient(baseConfig);
    await expect(client.trackEvent('user-123', 'page_view')).resolves.toBeUndefined();
  });

  it('POSTs one entry per cached assignment to /api/v1/tracking/batch', async () => {
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1 } });
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    await client.trackEvent('user-123', 'purchase', { order_id: 'o-1' }, { value: 120 });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const { url, init, body } = call(fetchMock, 1);
    expect(url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual(expectedHeaders);
    expect(body).toEqual({
      events: [
        {
          event_type: 'purchase',
          event_name: 'purchase',
          user_id: 'user-123',
          experiment_key: 'checkout',
          value: 120,
          metadata: { order_id: 'o-1' },
        },
      ],
    });
  });

  it('includes one entry per assignment plus one per evaluated flag', async () => {
    const fetchMock = mockFetchSequence(
      { body: { ...assignment, experiment_key: 'hero' } },
      { body: { ...controlAssignment, experiment_key: 'checkout' } },
      { body: flagOn },
      { body: flagOff },
      { body: { success_count: 4 } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'hero');
    await client.assignExperiment(user, 'checkout');
    await client.evaluateFeatureFlag(user, 'new-search');
    await client.evaluateFeatureFlag(user, 'free-shipping');

    await client.trackEvent('user-123', 'page_view', { page: '/products' });

    expect(fetchMock).toHaveBeenCalledTimes(5);
    const base = {
      event_type: 'page_view',
      event_name: 'page_view',
      user_id: 'user-123',
      metadata: { page: '/products' },
    };
    expect(call(fetchMock, 4).url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(call(fetchMock, 4).body).toEqual({
      events: [
        { ...base, experiment_key: 'hero' },
        { ...base, experiment_key: 'checkout' },
        { ...base, feature_flag_key: 'new-search' },
        { ...base, feature_flag_key: 'free-shipping' },
      ],
    });
  });

  it('fans out to evaluated flags alone when no assignments are cached', async () => {
    const fetchMock = mockFetchSequence({ body: flagOn }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(user, 'new-search');
    await client.trackEvent('user-123', 'search', { query: 'boots' });

    expect(call(fetchMock, 1).url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(call(fetchMock, 1).body).toEqual({
      events: [
        {
          event_type: 'search',
          event_name: 'search',
          user_id: 'user-123',
          feature_flag_key: 'new-search',
          metadata: { query: 'boots' },
        },
      ],
    });
  });

  it('applies eventType, value and timestamp to every fanned-out entry', async () => {
    const fetchMock = mockFetchSequence({ body: assignment }, { body: flagOn }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    await client.evaluateFeatureFlag(user, 'new-search');
    await client.trackEvent('user-123', 'purchase', undefined, {
      eventType: 'conversion',
      value: 42,
      timestamp: new Date('2026-01-02T03:04:05.000Z'),
    });

    const { events } = call(fetchMock, 2).body;
    expect(events).toHaveLength(2);
    for (const event of events) {
      expect(event).toMatchObject({
        event_type: 'conversion',
        event_name: 'purchase',
        value: 42,
        timestamp: '2026-01-02T03:04:05.000Z',
      });
      expect(event).not.toHaveProperty('metadata');
    }
  });

  it('does not fan out to a failed assignment', async () => {
    const fetchMock = mockFetchSequence({ body: {}, status: 404 }, { body: assignment }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.assignExperiment(user, 'missing')).rejects.toThrow();
    await client.assignExperiment(user, 'checkout');
    await client.trackEvent('user-123', 'click');

    const { events } = call(fetchMock, 2).body;
    expect(events.map((e: { experiment_key: string }) => e.experiment_key)).toEqual(['checkout']);
  });

  it('splits more than 100 entries into multiple batch requests', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    for (let i = 0; i < 101; i++) {
      await client.evaluateFeatureFlag(user, `flag-${i}`);
    }
    fetchMock.mockClear();

    await client.trackEvent('user-123', 'page_view');

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 0).url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(call(fetchMock, 0).body.events).toHaveLength(100);
    expect(call(fetchMock, 1).body.events).toHaveLength(1);
  });

  it('still fans out to the attributed keys after clearCache, which empties only the cache', async () => {
    const fetchMock = mockFetchSequence({ body: assignment }, { body: flagOn }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    await client.evaluateFeatureFlag(user, 'new-search');
    client.clearCache();
    expect(client.getAssignments('user-123')).toEqual([]);
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);

    await client.trackEvent('user-123', 'click');
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(call(fetchMock, 2).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 2).body)).toEqual(['checkout', 'flag:new-search']);
  });

  it('does NOT throw when the batch request fails', async () => {
    mockFetchSequence({ body: assignment }, new Error('Network failure'));
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    await expect(client.trackEvent('user-123', 'click')).resolves.toBeUndefined();
  });
});

// ─── trackEvent — late conversions (the attributed keys outlive the cache) ────

describe('trackEvent after the cache TTL', () => {
  it('sends an untagged purchase made 301 s after the assignment to the experiment', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1 } });
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    now.mockReturnValue(T0 + 301 * SECOND); // past the 300 s cache TTL
    expect(client.getAssignments('user-123')).toEqual([]);

    await client.trackEvent('user-123', 'purchase', undefined, { value: 120 });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toBe(BATCH_URL);
    expect(call(fetchMock, 1).body).toEqual({
      events: [
        { event_type: 'purchase', event_name: 'purchase', user_id: 'user-123', experiment_key: 'checkout', value: 120 },
      ],
    });
  });

  it('control: sends the same purchase at +299 s, while the assignment is still cached', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1 } });
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    now.mockReturnValue(T0 + 299 * SECOND);
    expect(client.getAssignments('user-123').map(a => a.experimentKey)).toEqual(['checkout']);

    await client.trackEvent('user-123', 'purchase', undefined, { value: 120 });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 1).body)).toEqual(['checkout']);
  });

  it('sends to exactly n experiments plus m flags past the TTL, each once, experiments first', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence(
      { body: { ...assignment, experiment_key: 'hero' } },
      { body: { ...assignment, experiment_key: 'checkout' } },
      { body: flagOn },
      { body: { ...assignment, experiment_key: 'sort' } },
      { body: flagOff },
      { body: { ...assignment, experiment_key: 'checkout' } },
      { body: { success_count: 5 } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'hero');
    await client.assignExperiment(user, 'checkout');
    await client.evaluateFeatureFlag(user, 'new-search');
    await client.assignExperiment(user, 'sort');
    await client.evaluateFeatureFlag(user, 'free-shipping');
    now.mockReturnValue(T0 + 400 * SECOND);
    await client.assignExperiment(user, 'checkout'); // answered again: still one entry, in its first place
    now.mockReturnValue(T0 + 800 * SECOND);

    await client.trackEvent('user-123', 'purchase');

    expect(fetchMock).toHaveBeenCalledTimes(7);
    const { events } = call(fetchMock, 6).body;
    expect(events).toHaveLength(5);
    expect(batchKeys({ events })).toEqual(['hero', 'checkout', 'sort', 'flag:new-search', 'flag:free-shipping']);
  });

  it.each(['holdout', 'mutual_exclusion', 'targeting'])(
    'still sends to an experiment answered assigned: false with reason %s, past the TTL',
    async reason => {
      const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
      const fetchMock = mockFetchSequence(
        { body: { ...controlAssignment, experiment_key: 'hero', assigned: false, reason } },
        { body: { success_count: 1 } }
      );
      const client = new ExperimentationClient(baseConfig);
      await client.assignExperiment(user, 'hero');
      now.mockReturnValue(T0 + 301 * SECOND);

      await client.trackEvent('user-123', 'purchase');

      expect(call(fetchMock, 1).url).toBe(BATCH_URL);
      expect(batchKeys(call(fetchMock, 1).body)).toEqual(['hero']);
    }
  );

  it.each(['draft', 'paused', 'completed', 'archived'])(
    'does not send to an experiment whose latest answer has reason %s',
    async reason => {
      const fetchMock = mockFetchSequence(
        { body: { ...controlAssignment, experiment_key: 'checkout', assigned: false, reason } },
        { body: { ...assignment, experiment_key: 'hero' } },
        { body: { success_count: 1 } }
      );
      const client = new ExperimentationClient(baseConfig);
      await client.assignExperiment(user, 'checkout');
      await client.assignExperiment(user, 'hero');

      await client.trackEvent('user-123', 'purchase');

      expect(call(fetchMock, 2).url).toBe(BATCH_URL);
      expect(batchKeys(call(fetchMock, 2).body)).toEqual(['hero']);
    }
  );

  it('follows the latest answer: a paused answer stops the fan-out, a later running answer restores it', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence(
      { body: { ...assignment, assigned: true, reason: 'assigned' } },
      { body: { ...controlAssignment, assigned: false, reason: 'paused' } },
      { body: {} },
      { body: { ...assignment, assigned: true, reason: 'assigned' } },
      { body: { success_count: 1 } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    now.mockReturnValue(T0 + 301 * SECOND);
    await client.assignExperiment(user, 'checkout'); // the experiment was paused meanwhile

    await client.trackEvent('user-123', 'purchase');
    expect(call(fetchMock, 2).url).toBe(TRACK_URL);
    expect(call(fetchMock, 2).body).not.toHaveProperty('experiment_key');

    now.mockReturnValue(T0 + 602 * SECOND);
    await client.assignExperiment(user, 'checkout'); // resumed
    await client.trackEvent('user-123', 'purchase');
    expect(call(fetchMock, 4).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 4).body)).toEqual(['checkout']);
  });

  it('sends to a key answered 24 hours less 1 s ago', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1 } });
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    now.mockReturnValue(T0 + 24 * HOUR - SECOND);

    await client.trackEvent('user-123', 'purchase');

    expect(call(fetchMock, 1).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 1).body)).toEqual(['checkout']);
  });

  it('does not send to a key answered more than 24 hours ago: the event goes once with no key', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.assignExperiment(user, 'checkout');
    now.mockReturnValue(T0 + 24 * HOUR + SECOND);

    await client.trackEvent('user-123', 'purchase');

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toBe(TRACK_URL);
    expect(call(fetchMock, 1).body).toEqual({ event_type: 'purchase', event_name: 'purchase', user_id: 'user-123' });
  });

  it('another client instance has none of them: its event goes once with no key', async () => {
    const fetchMock = mockFetchSequence({ body: assignment }, { body: {} });
    const assigning = new ExperimentationClient(baseConfig);
    const tracking = new ExperimentationClient(baseConfig); // say, after a page reload
    await assigning.assignExperiment(user, 'checkout');

    await tracking.trackEvent('user-123', 'purchase');

    expect(call(fetchMock, 1).url).toBe(TRACK_URL);
    expect(call(fetchMock, 1).body).toEqual({ event_type: 'purchase', event_name: 'purchase', user_id: 'user-123' });
  });
});

// ─── clearCache ───────────────────────────────────────────────────────────────

describe('clearCache', () => {
  it('clears flag entries for every user so subsequent calls fetch fresh data', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);

    await client.evaluateFeatureFlag(user, 'my-flag');
    await client.evaluateFeatureFlag({ userId: 'user-B' }, 'my-flag');
    client.clearCache();
    await client.evaluateFeatureFlag(user, 'my-flag');

    // Two calls before clear + one after = 3 total
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('clears both the flag and assignment caches', async () => {
    mockFetchSequence({ body: flagOn }, { body: assignment });
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFeatureFlag(user, 'my-flag');
    await client.assignExperiment(user, 'checkout');

    client.clearCache();

    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
    expect(client.getAssignments('user-123')).toEqual([]);
  });
});
