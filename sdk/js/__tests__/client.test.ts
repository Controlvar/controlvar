import { ExperimentationClient } from '../src/client';
import { ExperimentationError } from '../src/errors';
import type { AssignResponse, ClientConfig, FlagEvaluateResponse, UserContext } from '../src/types';

const baseConfig: ClientConfig = { apiUrl: 'https://api.example.com', apiKey: 'test-api-key' };

const user: UserContext = { userId: 'user-123', attributes: { device: 'mobile', country: 'US' } };
const plainUser: UserContext = { userId: 'user-123' };
/** `encodeURIComponent(JSON.stringify(user.attributes))` */
const userContext = '%7B%22device%22%3A%22mobile%22%2C%22country%22%3A%22US%22%7D';

const expectedHeaders = {
  'X-API-Key': 'test-api-key',
  'Content-Type': 'application/json',
  Accept: 'application/json',
};

const flagOn: FlagEvaluateResponse = { key: 'my-flag', enabled: true, config: null };
const flagOff: FlagEvaluateResponse = { key: 'my-flag', enabled: false, config: null };
const flagWithConfig: FlagEvaluateResponse = {
  key: 'my-flag',
  enabled: true,
  config: { variant: 'treatment', color: 'green' },
};

const assignment: AssignResponse = {
  experiment_key: 'checkout',
  user_id: 'user-123',
  variant_id: 'var-2',
  variant_name: 'one_page',
  is_control: false,
  configuration: { steps: 1 },
};
const controlAssignment: AssignResponse = {
  experiment_key: 'checkout',
  user_id: 'user-123',
  variant_id: 'var-1',
  variant_name: 'control',
  is_control: true,
  configuration: null,
};

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

/** Mock fetch returning the same response for every call. */
function mockFetch(body: unknown = {}, status = 200): jest.Mock {
  const mock = jest.fn().mockResolvedValue(jsonResponse({ body, status }));
  global.fetch = mock;
  return mock;
}

/** Queue responses in order; an Error entry rejects (network failure). */
function mockFetchSequence(...steps: Array<Step | Error>): jest.Mock {
  const mock = jest.fn();
  for (const step of steps) {
    if (step instanceof Error) mock.mockRejectedValueOnce(step);
    else mock.mockResolvedValueOnce(jsonResponse(step));
  }
  global.fetch = mock;
  return mock;
}

function call(mock: jest.Mock, index = 0) {
  const [url, init] = mock.mock.calls[index];
  return { url: url as string, init, body: init?.body ? JSON.parse(init.body) : undefined };
}

async function captureError(promise: Promise<unknown>): Promise<ExperimentationError> {
  try {
    await promise;
  } catch (err) {
    return err as ExperimentationError;
  }
  throw new Error('expected the promise to reject');
}

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
});

// ─── Constructor ──────────────────────────────────────────────────────────────

describe('constructor', () => {
  it('requires apiKey', () => {
    expect(() => new ExperimentationClient({ apiUrl: 'https://x', apiKey: '' })).toThrow('apiKey is required');
  });

  it('requires apiUrl', () => {
    expect(() => new ExperimentationClient({ apiUrl: '', apiKey: 'k' })).toThrow('apiUrl is required');
  });

  it('strips trailing slashes from apiUrl', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient({ ...baseConfig, apiUrl: 'https://api.example.com//' });
    await client.evaluateFlag('my-flag', plainUser);
    expect(call(fetchMock).url).toBe('https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-123');
  });

  it('uses the global fetch when config.fetch is not given', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('defaults to a 5000 ms timeout', async () => {
    jest.useFakeTimers();
    global.fetch = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
      return new Promise((_resolve, reject) => {
        (init.signal as AbortSignal).addEventListener('abort', () => reject(new Error('The operation was aborted')));
      });
    });
    const client = new ExperimentationClient(baseConfig);
    const pending = client.evaluateFlag('my-flag', user);
    let settled = false;
    pending.catch(() => undefined).finally(() => { settled = true; });
    await jest.advanceTimersByTimeAsync(4_999);
    expect(settled).toBe(false);
    await jest.advanceTimersByTimeAsync(1);
    const error = await captureError(pending);
    expect(error.code).toBe('TIMEOUT');
    expect(error.message).toContain('5000 ms');
  });

  it('caches a success for 300000 ms by default', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = mockFetchSequence({ body: flagOn }, { body: flagOff });
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('my-flag', user);
    now.mockReturnValue(1_000_000 + 299_999);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    now.mockReturnValue(1_000_000 + 300_001);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('uses config.fetch when provided instead of the global', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('global fetch must not be used'));
    const custom = jest.fn().mockResolvedValue(jsonResponse({ body: flagOn }));
    const client = new ExperimentationClient({ ...baseConfig, fetch: custom as unknown as typeof fetch });
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: true });
    expect(custom).toHaveBeenCalledTimes(1);
  });
});

// ─── getAssignment ────────────────────────────────────────────────────────────

describe('getAssignment', () => {
  it('POSTs /api/v1/tracking/assign with the contract headers', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { url, init } = call(fetchMock);
    expect(url).toBe('https://api.example.com/api/v1/tracking/assign');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual(expectedHeaders);
  });

  it('sends {experiment_key, user_id, context: attributes} as the JSON body', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    expect(call(fetchMock).body).toEqual({
      experiment_key: 'checkout',
      user_id: 'user-123',
      context: { device: 'mobile', country: 'US' },
    });
  });

  it('omits context when the user has no attributes', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', { userId: 'user-123' });
    expect(call(fetchMock).body).toEqual({ experiment_key: 'checkout', user_id: 'user-123' });
  });

  it('maps the server response to an Assignment', async () => {
    mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.getAssignment('checkout', user)).resolves.toEqual({
      experimentKey: 'checkout',
      userId: 'user-123',
      variantId: 'var-2',
      variantName: 'one_page',
      isControl: false,
      configuration: { steps: 1 },
    });
  });

  it('carries assigned: true and reason from a server that enrolled the user', async () => {
    mockFetch({ ...assignment, assigned: true, reason: 'assigned' });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.getAssignment('checkout', user);
    expect(result.assigned).toBe(true);
    expect(result.reason).toBe('assigned');
  });

  it.each(['holdout', 'mutual_exclusion', 'targeting'])(
    'carries assigned: false and reason %s for a user the server did not enrol',
    async reason => {
      mockFetch({ ...controlAssignment, assigned: false, reason });
      const client = new ExperimentationClient(baseConfig);
      const result = await client.getAssignment('checkout', user);
      expect(result.assigned).toBe(false);
      expect(result.reason).toBe(reason);
      expect(result.isControl).toBe(true);
    }
  );

  it.each(['draft', 'paused', 'completed', 'archived'])(
    'carries assigned: false and reason %s for an experiment that is not ACTIVE',
    async reason => {
      mockFetch({ ...controlAssignment, assigned: false, reason });
      const client = new ExperimentationClient(baseConfig);
      const result = await client.getAssignment('checkout', user);
      expect(result.assigned).toBe(false);
      expect(result.reason).toBe(reason);
      expect(result.isControl).toBe(true);
    }
  );

  it('leaves assigned and reason undefined (not false) when an older server omits them', async () => {
    mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    const result = await client.getAssignment('checkout', user);
    expect(result.assigned).toBeUndefined();
    expect(result.reason).toBeUndefined();
    expect(result).not.toHaveProperty('assigned');
    expect(result).not.toHaveProperty('reason');
  });

  it('normalises missing variant_id / is_control / configuration', async () => {
    mockFetch({ experiment_key: 'checkout', user_id: 'user-123', variant_name: 'x' });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.getAssignment('checkout', user);
    expect(result).toMatchObject({ variantId: null, isControl: false, configuration: null });
  });

  it('is sticky: the second call is served from the cache without a request', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    const first = await client.getAssignment('checkout', user);
    const second = await client.getAssignment('checkout', user);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(second).toBe(first);
  });

  it('keeps separate cache entries per user and per experiment', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', { userId: 'a' });
    await client.getAssignment('checkout', { userId: 'b' });
    await client.getAssignment('hero', { userId: 'a' });
    await client.getAssignment('checkout', { userId: 'a' });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('shares one in-flight request between concurrent callers', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    const [a, b] = await Promise.all([client.getAssignment('checkout', user), client.getAssignment('checkout', user)]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(a).toBe(b);
  });

  it('throws ExperimentationError with status 404 when no experiment has the key', async () => {
    mockFetch({ detail: "No experiment has the key 'missing'." }, 404);
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.getAssignment('missing', user));
    expect(error).toBeInstanceOf(ExperimentationError);
    expect(error.code).toBe('HTTP_ERROR');
    expect(error.status).toBe(404);
    expect(error.message).toContain("No experiment has the key 'missing'.");
  });

  it('throws ExperimentationError with status 401 for a bad key', async () => {
    mockFetch({ detail: 'Invalid API key' }, 401);
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.getAssignment('checkout', user));
    expect(error.status).toBe(401);
  });

  it('never caches a failure — the next call re-fetches', async () => {
    const fetchMock = mockFetchSequence({ status: 404 }, { body: assignment });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.getAssignment('checkout', user)).rejects.toThrow('API error 404');
    await expect(client.getAssignment('checkout', user)).resolves.toMatchObject({ variantName: 'one_page' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('wraps network failures as NETWORK_ERROR without a status', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('ECONNREFUSED'));
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.getAssignment('checkout', user));
    expect(error.code).toBe('NETWORK_ERROR');
    expect(error.status).toBeUndefined();
    expect(error.message).toContain('ECONNREFUSED');
  });

  it('rejects an empty userId', async () => {
    mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.getAssignment('checkout', { userId: '' })).rejects.toThrow('userId is required');
  });
});

// ─── getVariant ───────────────────────────────────────────────────────────────

describe('getVariant', () => {
  it('returns the assigned variant name', async () => {
    mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.getVariant('checkout', user)).resolves.toBe('one_page');
  });

  it("returns 'control' by default when assignment fails", async () => {
    mockFetch({}, 404);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.getVariant('checkout', user)).resolves.toBe('control');
  });

  it('returns the configured defaultVariant on failure', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('down'));
    const client = new ExperimentationClient({ ...baseConfig, defaultVariant: 'baseline' });
    await expect(client.getVariant('checkout', user)).resolves.toBe('baseline');
  });
});

// ─── evaluateFlag ─────────────────────────────────────────────────────────────

describe('evaluateFlag', () => {
  it('GETs /api/v1/feature-flags/evaluate/{key}?user_id=… with the contract headers and no body', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('my-flag', plainUser);

    const { url, init } = call(fetchMock);
    expect(url).toBe('https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-123');
    expect(init.method).toBe('GET');
    expect(init.headers).toEqual(expectedHeaders);
    expect(init.body).toBeUndefined();
  });

  it('appends context=<url-encoded JSON of user.attributes> when the user has attributes', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('my-flag', user);

    const { url, init } = call(fetchMock);
    expect(url).toBe(`https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-123&context=${userContext}`);
    expect(userContext).toBe('%7B%22device%22%3A%22mobile%22%2C%22country%22%3A%22US%22%7D');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
  });

  it('the context parameter round-trips to the original attributes', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    const attributes = { os: 'iOS', os_version: '17.4.0', tier: 'premium', employee: false, app: { version: '3.2.1' } };
    await client.evaluateFlag('my-flag', { userId: 'user-123', attributes });

    const query = new URL(call(fetchMock).url).searchParams;
    expect(query.get('user_id')).toBe('user-123');
    expect(JSON.parse(query.get('context')!)).toEqual(attributes);
  });

  it('omits context when attributes is an empty object', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('my-flag', { userId: 'user-123', attributes: {} });
    expect(call(fetchMock).url).toBe('https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-123');
  });

  it('URL-encodes flag key, user id and context together', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('my flag/key', { userId: 'user a/b@c', attributes: { q: 'a&b=c' } });
    expect(call(fetchMock).url).toBe(
      'https://api.example.com/api/v1/feature-flags/evaluate/my%20flag%2Fkey?user_id=user%20a%2Fb%40c&context=%7B%22q%22%3A%22a%26b%3Dc%22%7D'
    );
  });

  it('exposes the server reason when the response carries one', async () => {
    mockFetch({ ...flagOn, reason: 'targeting_rule' });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toEqual({
      key: 'my-flag',
      enabled: true,
      config: null,
      reason: 'targeting_rule',
    });
  });

  it('leaves reason undefined when the server omits it (older servers)', async () => {
    mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    const evaluation = await client.evaluateFlag('my-flag', user);
    expect(evaluation.reason).toBeUndefined();
    expect(evaluation).not.toHaveProperty('reason');
  });

  it('ignores a non-string reason', async () => {
    mockFetch({ ...flagOff, reason: 7 });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toEqual({ key: 'my-flag', enabled: false, config: null });
  });

  it('is cached by user + flag only: changed attributes hit the cache until clearCache', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('my-flag', { userId: 'user-123', attributes: { country: 'US' } });
    await client.evaluateFlag('my-flag', { userId: 'user-123', attributes: { country: 'DE' } });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    client.clearCache();
    await client.evaluateFlag('my-flag', { userId: 'user-123', attributes: { country: 'DE' } });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toContain('&context=%7B%22country%22%3A%22DE%22%7D');
  });

  it('URL-encodes the flag key and the user id', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('my flag/key', { userId: 'user a/b@c' });
    expect(call(fetchMock).url).toBe(
      'https://api.example.com/api/v1/feature-flags/evaluate/my%20flag%2Fkey?user_id=user%20a%2Fb%40c'
    );
  });

  it('maps {key, enabled, config}', async () => {
    mockFetch(flagWithConfig);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toEqual({
      key: 'my-flag',
      enabled: true,
      config: { variant: 'treatment', color: 'green' },
    });
  });

  it('normalises a missing config to null', async () => {
    mockFetch({ key: 'my-flag', enabled: false });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toEqual({ key: 'my-flag', enabled: false, config: null });
  });

  it('serves from cache until the TTL elapses, then re-fetches', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = mockFetchSequence({ body: flagOn }, { body: flagOff });
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 1_000 });

    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: true });
    now.mockReturnValue(1_000_999);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    now.mockReturnValue(1_001_001);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('caches disabled results too', async () => {
    const fetchMock = mockFetch(flagOff);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('my-flag', user);
    await client.evaluateFlag('my-flag', user);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('shares one in-flight request between concurrent callers', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await Promise.all([client.evaluateFlag('my-flag', user), client.evaluateFlag('my-flag', user)]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('throws ExperimentationError with status 404 when the flag is not ACTIVE', async () => {
    mockFetch({ detail: 'not found' }, 404);
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.evaluateFlag('my-flag', user));
    expect(error.status).toBe(404);
    expect(error.body).toEqual({ detail: 'not found' });
  });

  it('never caches a failure — the next call re-fetches', async () => {
    const fetchMock = mockFetchSequence(new Error('Network down'), { body: flagOn });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFlag('my-flag', user)).rejects.toThrow('Network down');
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('aborts after timeoutMs and throws a TIMEOUT error', async () => {
    jest.useFakeTimers();
    global.fetch = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
      return new Promise((_resolve, reject) => {
        (init.signal as AbortSignal).addEventListener('abort', () => reject(new Error('The operation was aborted')));
      });
    });
    const client = new ExperimentationClient({ ...baseConfig, timeoutMs: 50 });
    const pending = client.evaluateFlag('my-flag', user);
    jest.advanceTimersByTime(51);
    const error = await captureError(pending);
    expect(error.code).toBe('TIMEOUT');
    expect(error.message).toContain('50 ms');
  });

  it('throws INVALID_RESPONSE when the 2xx body is not JSON', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: () => Promise.reject(new SyntaxError('Unexpected token')),
    });
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.evaluateFlag('my-flag', user));
    expect(error.code).toBe('INVALID_RESPONSE');
    expect(error.status).toBe(200);
  });
});

// ─── isFeatureEnabled / getAllFlags ───────────────────────────────────────────

describe('isFeatureEnabled', () => {
  it('returns true for an enabled flag and false for a disabled one', async () => {
    mockFetchSequence({ body: flagOn }, { body: flagOff });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.isFeatureEnabled('a', user)).resolves.toBe(true);
    await expect(client.isFeatureEnabled('b', user)).resolves.toBe(false);
  });

  it('returns false on failure', async () => {
    mockFetch({}, 500);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.isFeatureEnabled('my-flag', user)).resolves.toBe(false);
  });
});

describe('getAllFlags', () => {
  it('GETs /api/v1/feature-flags/user/{user_id} and returns {key: boolean}', async () => {
    const fetchMock = mockFetch({ new_search: true, free_shipping: false });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.getAllFlags('user a')).resolves.toEqual({ new_search: true, free_shipping: false });
    const { url, init } = call(fetchMock);
    expect(url).toBe('https://api.example.com/api/v1/feature-flags/user/user%20a');
    expect(init.method).toBe('GET');
  });

  it('sends attributes as ?context=<url-encoded JSON> when given', async () => {
    const fetchMock = mockFetch({ new_search: true });
    const client = new ExperimentationClient(baseConfig);
    await client.getAllFlags('user a', { device: 'mobile', country: 'US' });
    expect(call(fetchMock).url).toBe(`https://api.example.com/api/v1/feature-flags/user/user%20a?context=${userContext}`);
  });

  it('omits context for an empty attributes object', async () => {
    const fetchMock = mockFetch({ new_search: true });
    const client = new ExperimentationClient(baseConfig);
    await client.getAllFlags('user-123', {});
    expect(call(fetchMock).url).toBe('https://api.example.com/api/v1/feature-flags/user/user-123');
  });

  it('throws on a non-2xx response and does not populate the evaluation cache', async () => {
    mockFetch({ detail: 'bad key' }, 401);
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.getAllFlags('user-123'));
    expect(error.status).toBe(401);
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
  });
});

// ─── getAssignments / getEvaluatedFlags / fetchAssignments ────────────────────

describe('cache accessors', () => {
  it('getAssignments returns cached assignments for the user only, in order', async () => {
    mockFetchSequence(
      { body: { ...assignment, experiment_key: 'exp-a' } },
      { body: { ...controlAssignment, experiment_key: 'exp-b' } },
      { body: assignment }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('exp-a', user);
    await client.getAssignment('exp-b', user);
    await client.getAssignment('checkout', { userId: 'someone-else' });
    expect(client.getAssignments('user-123').map(a => a.experimentKey)).toEqual(['exp-a', 'exp-b']);
  });

  it('getEvaluatedFlags returns cached flag keys for the user only', async () => {
    mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('flag-a', user);
    await client.evaluateFlag('flag-b', user);
    await client.evaluateFlag('flag-c', { userId: 'someone-else' });
    expect(client.getEvaluatedFlags('user-123')).toEqual(['flag-a', 'flag-b']);
  });

  it('clearCache empties both caches', async () => {
    mockFetchSequence({ body: flagOn }, { body: assignment });
    const client = new ExperimentationClient(baseConfig);
    await client.evaluateFlag('flag-a', user);
    await client.getAssignment('checkout', user);
    client.clearCache();
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
    expect(client.getAssignments('user-123')).toEqual([]);
  });

  it('fetchAssignments GETs /api/v1/tracking/assignments/{user_id}?active_only=true', async () => {
    const rows = [{ experiment_key: 'checkout', variant_name: 'one_page' }];
    const fetchMock = mockFetch(rows);
    const client = new ExperimentationClient(baseConfig);
    await expect(client.fetchAssignments('user-123')).resolves.toEqual(rows);
    expect(call(fetchMock).url).toBe('https://api.example.com/api/v1/tracking/assignments/user-123?active_only=true');

    await client.fetchAssignments('user-123', { activeOnly: false });
    expect(call(fetchMock, 1).url).toBe('https://api.example.com/api/v1/tracking/assignments/user-123?active_only=false');
  });
});

// ─── track — explicit keys ────────────────────────────────────────────────────

describe('track with an explicit key', () => {
  it('POSTs one event to /api/v1/tracking/track with the exact body', async () => {
    const fetchMock = mockFetch({});
    const client = new ExperimentationClient(baseConfig);
    await client.track('user-123', 'purchase', {
      value: 85.5,
      properties: { order_id: 'o-1' },
      experimentKey: 'checkout',
      timestamp: new Date('2026-09-11T12:34:56.000Z'),
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { url, init, body } = call(fetchMock);
    expect(url).toBe('https://api.example.com/api/v1/tracking/track');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual(expectedHeaders);
    expect(body).toEqual({
      event_type: 'purchase',
      event_name: 'purchase',
      user_id: 'user-123',
      experiment_key: 'checkout',
      value: 85.5,
      metadata: { order_id: 'o-1' },
      timestamp: '2026-09-11T12:34:56.000Z',
    });
  });

  it('sends feature_flag_key (and no experiment_key) and accepts a string timestamp', async () => {
    const fetchMock = mockFetch({});
    const client = new ExperimentationClient(baseConfig);
    await client.track('user-123', 'search', { featureFlagKey: 'new-search', timestamp: '2026-01-02T03:04:05Z' });
    expect(call(fetchMock).body).toEqual({
      event_type: 'search',
      event_name: 'search',
      user_id: 'user-123',
      feature_flag_key: 'new-search',
      timestamp: '2026-01-02T03:04:05Z',
    });
  });

  it('uses eventType as event_type and keeps eventName as event_name; omits optional fields', async () => {
    const fetchMock = mockFetch({});
    const client = new ExperimentationClient(baseConfig);
    await client.track('user-123', 'hero_cta_click', { experimentKey: 'hero', eventType: 'conversion' });
    expect(call(fetchMock).body).toEqual({
      event_type: 'conversion',
      event_name: 'hero_cta_click',
      user_id: 'user-123',
      experiment_key: 'hero',
    });
  });

  it('does not fan out to cached assignments when a key is given', async () => {
    const fetchMock = mockFetchSequence({ body: assignment }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    await client.track('user-123', 'click', { featureFlagKey: 'new-search' });
    expect(call(fetchMock, 1).url).toBe('https://api.example.com/api/v1/tracking/track');
    expect(call(fetchMock, 1).body).not.toHaveProperty('experiment_key');
  });

  it.each([
    ['a network error', () => jest.fn().mockRejectedValue(new Error('Network failure'))],
    ['a 500', () => jest.fn().mockResolvedValue({ ok: false, status: 500, headers: { get: () => null } })],
    ['a 404', () => jest.fn().mockResolvedValue({ ok: false, status: 404, headers: { get: () => null } })],
    ['a 422', () => jest.fn().mockResolvedValue({ ok: false, status: 422, headers: { get: () => null } })],
  ])('never rejects on %s', async (_label, makeFetch) => {
    global.fetch = makeFetch();
    const client = new ExperimentationClient(baseConfig);
    await expect(client.track('user-123', 'click', { experimentKey: 'checkout' })).resolves.toBeUndefined();
  });

  it('reports swallowed failures through onError', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 404, headers: { get: () => null }, json: () => Promise.resolve({ detail: 'nope' }) });
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });
    await client.track('user-123', 'click', { experimentKey: 'missing' });
    expect(onError).toHaveBeenCalledTimes(1);
    const [error, operation] = onError.mock.calls[0];
    expect(error).toBeInstanceOf(ExperimentationError);
    expect(error.status).toBe(404);
    expect(operation).toBe('track');
  });

  it('survives a throwing onError handler', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('down'));
    const client = new ExperimentationClient({ ...baseConfig, onError: () => { throw new Error('handler bug'); } });
    await expect(client.track('user-123', 'click', { experimentKey: 'checkout' })).resolves.toBeUndefined();
  });
});

// ─── track — fan-out ──────────────────────────────────────────────────────────

describe('track fan-out (no key)', () => {
  it('sends nothing when nothing is cached for the user', async () => {
    const fetchMock = mockFetch({});
    const client = new ExperimentationClient(baseConfig);
    await expect(client.track('user-123', 'page_view', { properties: { page: '/' } })).resolves.toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('sends nothing when only another user has cached data', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', { userId: 'someone-else' });
    fetchMock.mockClear();
    await client.track('user-123', 'page_view');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('POSTs /api/v1/tracking/batch with one entry per assignment plus one per evaluated flag', async () => {
    const fetchMock = mockFetchSequence(
      { body: { ...assignment, experiment_key: 'hero' } },
      { body: { ...controlAssignment, experiment_key: 'checkout' } },
      { body: flagOn },
      { body: flagOff },
      { body: { success_count: 4, failure_count: 0 } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('hero', user);
    await client.getAssignment('checkout', user);
    await client.evaluateFlag('new-search', user);
    await client.evaluateFlag('free-shipping', user);

    await client.track('user-123', 'page_view', { properties: { page: '/products' }, value: 3 });

    expect(fetchMock).toHaveBeenCalledTimes(5);
    const { url, init, body } = call(fetchMock, 4);
    expect(url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual(expectedHeaders);
    const base = {
      event_type: 'page_view',
      event_name: 'page_view',
      user_id: 'user-123',
      value: 3,
      metadata: { page: '/products' },
    };
    expect(body).toEqual({
      events: [
        { ...base, experiment_key: 'hero' },
        { ...base, experiment_key: 'checkout' },
        { ...base, feature_flag_key: 'new-search' },
        { ...base, feature_flag_key: 'free-shipping' },
      ],
    });
  });

  it('fans out to an assignment the global holdout answered (assigned: false)', async () => {
    // Holdout results (#445) count a held-out user's events exactly as everyone
    // else's: a key-less track() must reach the held-out experiment too.
    const fetchMock = mockFetchSequence(
      { body: { ...controlAssignment, experiment_key: 'hero', assigned: false, reason: 'holdout' } },
      { body: { success_count: 1, failure_count: 0 } }
    );
    const client = new ExperimentationClient(baseConfig);
    const answer = await client.getAssignment('hero', user);
    expect(answer.assigned).toBe(false);

    await client.track('user-123', 'purchase');

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const { url, body } = call(fetchMock, 1);
    expect(url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(body.events.map((e: { experiment_key: string }) => e.experiment_key)).toEqual(['hero']);
  });

  it('does not fan out to a failed assignment or an expired one', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = mockFetchSequence(
      { status: 404 },
      { body: { ...assignment, experiment_key: 'old' } },
      { body: { ...assignment, experiment_key: 'fresh' } },
      { body: {} }
    );
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 1_000 });
    await expect(client.getAssignment('missing', user)).rejects.toThrow();
    await client.getAssignment('old', user);
    now.mockReturnValue(1_000_800);
    await client.getAssignment('fresh', user);
    now.mockReturnValue(1_001_100); // 'old' expired, 'fresh' still live

    await client.track('user-123', 'click');
    expect(call(fetchMock, 3).body.events.map((e: { experiment_key: string }) => e.experiment_key)).toEqual(['fresh']);
  });

  it('splits more than 100 entries into multiple batch requests', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    for (let i = 0; i < 101; i++) await client.evaluateFlag(`flag-${i}`, user);
    fetchMock.mockClear();

    await client.track('user-123', 'page_view');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 0).body.events).toHaveLength(100);
    expect(call(fetchMock, 1).body.events).toHaveLength(1);
  });

  it('never rejects when the batch request fails', async () => {
    mockFetchSequence({ body: assignment }, new Error('Network failure'));
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    await expect(client.track('user-123', 'click')).resolves.toBeUndefined();
  });
});

// ─── trackBatch ───────────────────────────────────────────────────────────────

describe('trackBatch', () => {
  it('POSTs keyed events to /api/v1/tracking/batch and aggregates the server counts', async () => {
    const fetchMock = mockFetch({ success_count: 2, failure_count: 0, errors: null });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.trackBatch([
      { userId: 'user-123', eventName: 'add_to_cart', experimentKey: 'checkout', value: 1 },
      { userId: 'user-123', eventName: 'flag_seen', featureFlagKey: 'new-search' },
    ]);
    expect(result).toEqual({ successCount: 2, failureCount: 0, errors: [] });
    const { url, body } = call(fetchMock);
    expect(url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(body).toEqual({
      events: [
        { event_type: 'add_to_cart', event_name: 'add_to_cart', user_id: 'user-123', experiment_key: 'checkout', value: 1 },
        { event_type: 'flag_seen', event_name: 'flag_seen', user_id: 'user-123', feature_flag_key: 'new-search' },
      ],
    });
  });

  it('chunks at 100 events per request', async () => {
    const fetchMock = mockFetch({ success_count: 100, failure_count: 0 });
    const client = new ExperimentationClient(baseConfig);
    const events = Array.from({ length: 250 }, (_, i) => ({ userId: `u-${i}`, eventName: 'e', experimentKey: 'x' }));
    const result = await client.trackBatch(events);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(call(fetchMock, 0).body.events).toHaveLength(100);
    expect(call(fetchMock, 1).body.events).toHaveLength(100);
    expect(call(fetchMock, 2).body.events).toHaveLength(50);
    expect(result.successCount).toBe(300); // the mock answers 100 for every chunk
  });

  it('expands keyless entries with the fan-out rule and drops those with nothing cached', async () => {
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 2, failure_count: 0 } });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    await client.trackBatch([
      { userId: 'user-123', eventName: 'page_view' },
      { userId: 'nobody', eventName: 'page_view' },
      { userId: 'user-123', eventName: 'click', featureFlagKey: 'f' },
    ]);
    expect(call(fetchMock, 1).body.events).toEqual([
      { event_type: 'page_view', event_name: 'page_view', user_id: 'user-123', experiment_key: 'checkout' },
      { event_type: 'click', event_name: 'click', user_id: 'user-123', feature_flag_key: 'f' },
    ]);
  });

  it('sends nothing and returns zeros for an empty list', async () => {
    const fetchMock = mockFetch({});
    const client = new ExperimentationClient(baseConfig);
    await expect(client.trackBatch([])).resolves.toEqual({ successCount: 0, failureCount: 0, errors: [] });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('never rejects: a failed chunk counts its events as failures and reports through onError', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 422, headers: { get: () => null }, json: () => Promise.resolve({ detail: 'bad' }) });
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });
    const result = await client.trackBatch([
      { userId: 'u', eventName: 'a', experimentKey: 'x' },
      { userId: 'u', eventName: 'b', experimentKey: 'x' },
    ]);
    expect(result.successCount).toBe(0);
    expect(result.failureCount).toBe(2);
    expect(result.errors).toEqual([{ message: expect.stringContaining('422'), status: 422 }]);
    expect(onError).toHaveBeenCalledWith(expect.any(ExperimentationError), 'trackBatch');
  });

  it('collects server-reported per-event errors', async () => {
    mockFetch({ success_count: 1, failure_count: 1, errors: [{ index: 1, error: 'unknown key' }] });
    const client = new ExperimentationClient(baseConfig);
    const result = await client.trackBatch([
      { userId: 'u', eventName: 'a', experimentKey: 'x' },
      { userId: 'u', eventName: 'b', experimentKey: 'nope' },
    ]);
    expect(result).toEqual({ successCount: 1, failureCount: 1, errors: [{ index: 1, error: 'unknown key' }] });
  });
});

// ─── 429 handling ─────────────────────────────────────────────────────────────

describe('429 rate limiting', () => {
  it('retries once after Retry-After and returns the second response', async () => {
    const fetchMock = mockFetchSequence(
      { status: 429, headers: { 'Retry-After': '0' } },
      { body: flagOn }
    );
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toBe(call(fetchMock, 0).url);
  });

  it('waits Retry-After seconds (capped at timeoutMs) before retrying', async () => {
    jest.useFakeTimers();
    const fetchMock = mockFetchSequence(
      { status: 429, headers: { 'Retry-After': '2' } },
      { body: flagOn }
    );
    const client = new ExperimentationClient({ ...baseConfig, timeoutMs: 5_000 });
    const pending = client.evaluateFlag('my-flag', user);

    await jest.advanceTimersByTimeAsync(1_999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await jest.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await expect(pending).resolves.toMatchObject({ enabled: true });
  });

  it('waits 1 s when the 429 carries no Retry-After', async () => {
    jest.useFakeTimers();
    const fetchMock = mockFetchSequence({ status: 429 }, { body: flagOn });
    const client = new ExperimentationClient(baseConfig);
    const pending = client.evaluateFlag('my-flag', user);
    await jest.advanceTimersByTimeAsync(999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await jest.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await expect(pending).resolves.toMatchObject({ enabled: true });
  });

  it('reads a Retry-After given as an HTTP date', async () => {
    jest.useFakeTimers({ now: Date.parse('2026-10-12T10:00:00Z') });
    const fetchMock = mockFetchSequence(
      { status: 429, headers: { 'Retry-After': 'Mon, 12 Oct 2026 10:00:03 GMT' } },
      { body: flagOn }
    );
    const client = new ExperimentationClient(baseConfig);
    const pending = client.evaluateFlag('my-flag', user);
    await jest.advanceTimersByTimeAsync(2_999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await jest.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await expect(pending).resolves.toMatchObject({ enabled: true });
  });

  it('caps a long Retry-After at timeoutMs', async () => {
    jest.useFakeTimers();
    const fetchMock = mockFetchSequence(
      { status: 429, headers: { 'Retry-After': '120' } },
      { body: flagOn }
    );
    const client = new ExperimentationClient({ ...baseConfig, timeoutMs: 1_000 });
    const pending = client.evaluateFlag('my-flag', user);
    await jest.advanceTimersByTimeAsync(1_000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await expect(pending).resolves.toMatchObject({ enabled: true });
  });

  it('gives up after the single retry and surfaces status 429', async () => {
    const fetchMock = mockFetchSequence(
      { status: 429, headers: { 'Retry-After': '0' } },
      { status: 429, headers: { 'Retry-After': '0' } }
    );
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.getAssignment('checkout', user));
    expect(error.status).toBe(429);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('also retries tracking requests', async () => {
    const fetchMock = mockFetchSequence({ status: 429, headers: { 'Retry-After': '0' } }, { body: {} });
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });
    await client.track('user-123', 'click', { experimentKey: 'checkout' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onError).not.toHaveBeenCalled();
  });
});
