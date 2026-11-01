import { ExperimentationClient } from '../src/client';
import { ExperimentationError } from '../src/errors';
import type {
  AssignResponse,
  ClientConfig,
  ErrorReport,
  FlagEvaluateResponse,
  SwallowedOperation,
  UserContext,
} from '../src/types';

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

/** A fixed clock start for the tests that move `Date.now`. */
const T0 = 1_700_000_000_000;
const SECOND = 1_000;
const HOUR = 3_600 * SECOND;

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

  it('cacheTtlMs: 0 caches nothing, even within the same millisecond', async () => {
    jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = mockFetchSequence({ body: flagOn }, { body: flagOff }, { body: assignment }, { body: controlAssignment });
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 0 });
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: true });
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: false });
    await expect(client.getAssignment('checkout', user)).resolves.toMatchObject({ variantName: 'one_page' });
    await expect(client.getAssignment('checkout', user)).resolves.toMatchObject({ variantName: 'control' });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
    expect(client.getAssignments('user-123')).toEqual([]);
  });

  it('a cached answer expires at exactly cacheTtlMs', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = mockFetchSequence({ body: flagOn }, { body: flagOff });
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 1_000 });
    await client.evaluateFlag('my-flag', user);
    now.mockReturnValue(1_000_999);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    now.mockReturnValue(1_001_000);
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);
    await expect(client.evaluateFlag('my-flag', user)).resolves.toMatchObject({ enabled: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
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

// ─── Request error messages (#1551) ──────────────────────────────────────────

describe('request error messages name the path without its query string', () => {
  const evaluatePath = '/api/v1/feature-flags/evaluate/my-flag';
  /** What none of the messages may contain: the query and the attribute values in it. */
  function expectNoQuery(message: string) {
    expect(message).not.toContain('?');
    expect(message).not.toContain('user_id=');
    expect(message).not.toContain('context=');
    expect(message).not.toContain('mobile');
  }

  it('an HTTP error', async () => {
    mockFetch({ detail: 'bad context' }, 422);
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.evaluateFlag('my-flag', user));
    expect(error.code).toBe('HTTP_ERROR');
    expect(error.message).toBe(`API error 422: bad context (GET ${evaluatePath})`);
    expectNoQuery(error.message);
  });

  it('an HTTP error from getAllFlags', async () => {
    mockFetch({}, 500);
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.getAllFlags('user-123', user.attributes));
    expect(error.message).toBe('API error 500 (GET /api/v1/feature-flags/user/user-123)');
    expectNoQuery(error.message);
  });

  it('a network error', async () => {
    mockFetchSequence(new Error('Network down'));
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.evaluateFlag('my-flag', user));
    expect(error.code).toBe('NETWORK_ERROR');
    expect(error.message).toBe(`Network error (GET ${evaluatePath}): Network down`);
    expectNoQuery(error.message);
  });

  it('a network error whose own message names the full URL', async () => {
    const url = `https://api.example.com${evaluatePath}?user_id=user-123&context=${userContext}`;
    mockFetchSequence(new Error(`request to ${url} failed, reason: connect ECONNREFUSED`));
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.evaluateFlag('my-flag', user));
    expect(error.message).toBe(
      `Network error (GET ${evaluatePath}): request to https://api.example.com${evaluatePath} failed, reason: connect ECONNREFUSED`
    );
    expectNoQuery(error.message);
  });

  it('a timeout', async () => {
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
    expect(error.message).toBe(`Request timed out after 50 ms (GET ${evaluatePath})`);
    expectNoQuery(error.message);
  });

  it('a 2xx body that is not JSON', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: () => Promise.reject(new SyntaxError('Unexpected token')),
    });
    const client = new ExperimentationClient(baseConfig);
    const error = await captureError(client.evaluateFlag('my-flag', user));
    expect(error.code).toBe('INVALID_RESPONSE');
    expect(error.message).toBe(`Invalid JSON in response (GET ${evaluatePath})`);
    expectNoQuery(error.message);
  });

  it('what onError receives', async () => {
    mockFetch({ detail: 'bad context' }, 422);
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });
    await expect(client.getStringValue('my-flag', user, 'fallback')).resolves.toBe('fallback');
    expect(onError).toHaveBeenCalledTimes(1);
    const [error, operation] = onError.mock.calls[0] as [ExperimentationError, string];
    expect(operation).toBe('value');
    expect(error.message).toBe(`API error 422: bad context (GET ${evaluatePath})`);
    expectNoQuery(error.message);
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

// ─── Typed values ─────────────────────────────────────────────────────────────

type Getter = 'getStringValue' | 'getNumberValue' | 'getJsonValue';
const GETTERS: Getter[] = ['getStringValue', 'getNumberValue', 'getJsonValue'];
/** A default of each getter's type, all different from every value served below. */
const DEFAULTS: Record<Getter, unknown> = { getStringValue: 'the default', getNumberValue: -1, getJsonValue: { the: 'default' } };

function getValue(client: ExperimentationClient, method: Getter, flagKey: string, who: UserContext, fallback: unknown) {
  return (client[method] as (key: string, u: UserContext, d: unknown) => Promise<unknown>).call(client, flagKey, who, fallback);
}

/** An evaluate answer for flag `f`. */
const served = (fields: Record<string, unknown>) => ({ key: 'f', enabled: true, config: null, reason: 'rollout', ...fields });

function valueClient(extra: Partial<ClientConfig> = {}) {
  const errors: Array<[ExperimentationError, SwallowedOperation]> = [];
  const client = new ExperimentationClient({ ...baseConfig, onError: (error, operation) => errors.push([error, operation]), ...extra });
  return { client, errors };
}

describe('evaluateFlag: value and valueType', () => {
  it("copies the server's value and value_type", async () => {
    mockFetch(served({ value: 'Complete purchase', value_type: 'string' }));
    const client = new ExperimentationClient(baseConfig);
    await expect(client.evaluateFlag('f', user)).resolves.toEqual({
      key: 'f',
      enabled: true,
      config: null,
      reason: 'rollout',
      value: 'Complete purchase',
      valueType: 'string',
    });
  });

  it('leaves both undefined when an older server sends neither', async () => {
    mockFetch(flagOn);
    const client = new ExperimentationClient(baseConfig);
    const evaluation = await client.evaluateFlag('my-flag', user);
    expect(evaluation).not.toHaveProperty('value');
    expect(evaluation).not.toHaveProperty('valueType');
  });
});

describe('getStringValue, getNumberValue and getJsonValue', () => {
  it.each([
    ['getStringValue', { value: 'Complete purchase', value_type: 'string' }, 'Complete purchase'],
    ['getStringValue', { value: '', value_type: 'string' }, ''],
    ['getNumberValue', { value: 10, value_type: 'number' }, 10],
    ['getNumberValue', { value: 2.5, value_type: 'number' }, 2.5],
    ['getNumberValue', { value: 0, value_type: 'number' }, 0],
    ['getJsonValue', { value: { color: 'blue' }, value_type: 'json' }, { color: 'blue' }],
    ['getJsonValue', { value: ['a', 1], value_type: 'json' }, ['a', 1]],
    ['getJsonValue', { value: {}, value_type: 'json' }, {}],
  ] as Array<[Getter, Record<string, unknown>, unknown]>)('%s returns the value the flag serves: %j', async (method, fields, expected) => {
    const fetchMock = mockFetch(served(fields));
    const { client, errors } = valueClient();
    await expect(getValue(client, method, 'f', user, DEFAULTS[method])).resolves.toEqual(expected);
    expect(call(fetchMock).url).toBe(`https://api.example.com/api/v1/feature-flags/evaluate/f?user_id=user-123&context=${userContext}`);
    expect(errors).toEqual([]);
  });

  it.each(['rollout', 'targeting_rule', 'inactive', 'error'])(
    'returns the off value the server sends when the flag is off for the user (reason %s)',
    async reason => {
      mockFetch(served({ enabled: false, reason, value: 'Buy now', value_type: 'string' }));
      const { client, errors } = valueClient();
      await expect(client.getStringValue('f', user, 'the default')).resolves.toBe('Buy now');
      expect(errors).toEqual([]);
    }
  );

  it('is evaluated once per user and flag for cacheTtlMs, like evaluateFlag', async () => {
    const fetchMock = mockFetch(served({ value: 10, value_type: 'number' }));
    const client = new ExperimentationClient(baseConfig);
    await client.getNumberValue('f', user, 0);
    await client.getNumberValue('f', user, 0);
    await client.evaluateFlag('f', user);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['a network failure', () => jest.fn().mockRejectedValue(new Error('Network down')), 'NETWORK_ERROR'],
    ['an unknown flag (404)', () => jest.fn().mockResolvedValue(jsonResponse({ status: 404, body: { detail: 'not found' } })), 'HTTP_ERROR'],
    ['a 500', () => jest.fn().mockResolvedValue(jsonResponse({ status: 500 })), 'HTTP_ERROR'],
    [
      'a body that is not JSON',
      () => jest.fn().mockResolvedValue({ ok: true, status: 200, headers: { get: () => null }, json: () => Promise.reject(new SyntaxError('x')) }),
      'INVALID_RESPONSE',
    ],
    ['an older server, which sends no value', () => jest.fn().mockResolvedValue(jsonResponse({ body: flagOn })), 'INVALID_RESPONSE'],
    [
      'a stored value the server cannot serve (value_type and no value)',
      () => jest.fn().mockResolvedValue(jsonResponse({ body: served({ value_type: 'string' }) })),
      'INVALID_RESPONSE',
    ],
  ])('each returns the default and reports %s to onError(err, "value")', async (_label, makeFetch, code) => {
    for (const method of GETTERS) {
      global.fetch = makeFetch() as unknown as typeof fetch;
      const { client, errors } = valueClient();
      await expect(getValue(client, method, 'f', user, DEFAULTS[method])).resolves.toBe(DEFAULTS[method]);
      expect(errors.map(([error, operation]) => [error.code, operation])).toEqual([[code, 'value']]);
      expect(errors[0][0]).toBeInstanceOf(ExperimentationError);
    }
  });

  it('names what an older server lacks', async () => {
    mockFetch(flagOn);
    const { client, errors } = valueClient();
    await client.getStringValue('my-flag', user, 'x');
    expect(errors[0][0].message).toBe(
      'Flag "my-flag" came back without a value (from a server without flag values, or one that cannot serve ' +
        "this flag's stored value); getStringValue returned your default"
    );
  });

  const STRING = { value: 'Buy now', value_type: 'string' };
  const NUMBER = { value: 10, value_type: 'number' };
  const JSON_VALUE = { value: { color: 'blue' }, value_type: 'json' };
  const BOOLEAN = { value: true, value_type: 'boolean' };
  /** Every getter against every type it does not read. */
  const MISMATCHES: Array<[Getter, string, Record<string, unknown>]> = [
    ['getNumberValue', 'a string', STRING],
    ['getJsonValue', 'a string', STRING],
    ['getStringValue', 'a number', NUMBER],
    ['getJsonValue', 'a number', NUMBER],
    ['getStringValue', 'a JSON value', JSON_VALUE],
    ['getNumberValue', 'a JSON value', JSON_VALUE],
    ['getStringValue', 'a boolean', BOOLEAN],
    ['getNumberValue', 'a boolean', BOOLEAN],
    ['getJsonValue', 'a boolean', BOOLEAN],
  ];

  it.each(MISMATCHES)('%s on a flag that serves %s: the default, and TYPE_MISMATCH', async (method, name, fields) => {
    mockFetch(served({ key: 'checkout-button-label', ...fields }));
    const { client, errors } = valueClient();
    await expect(getValue(client, method, 'checkout-button-label', user, DEFAULTS[method])).resolves.toBe(DEFAULTS[method]);
    expect(errors).toHaveLength(1);
    const [error, operation] = errors[0];
    expect(operation).toBe('value');
    expect(error.code).toBe('TYPE_MISMATCH');
    expect(error.message).toBe(`Flag "checkout-button-label" serves ${name}; ${method} returned your default`);
  });

  it('never coerces: "10" is not a number and 10 is not a string', async () => {
    mockFetchSequence({ body: served({ key: 'a', value: '10', value_type: 'string' }) }, { body: served({ key: 'b', value: 10, value_type: 'number' }) });
    const { client, errors } = valueClient();
    await expect(client.getNumberValue('a', user, -1)).resolves.toBe(-1);
    await expect(client.getStringValue('b', user, 'x')).resolves.toBe('x');
    expect(errors.map(([error]) => error.code)).toEqual(['TYPE_MISMATCH', 'TYPE_MISMATCH']);
  });

  it("reads the value's own type when the server sends no value_type", async () => {
    mockFetch(served({ value: 'Buy now' }));
    const { client, errors } = valueClient();
    await expect(client.getStringValue('f', user, 'x')).resolves.toBe('Buy now');
    await expect(client.getNumberValue('f', user, -1)).resolves.toBe(-1);
    expect(errors.map(([error]) => error.message)).toEqual(['Flag "f" serves a string; getNumberValue returned your default']);
  });

  it('a value that is not of the type the server names is the default too, never a wrong value', async () => {
    mockFetchSequence(
      { body: served({ key: 'a', value: '10', value_type: 'number' }) },
      { body: served({ key: 'b', value: 'x', value_type: 'json' }) }
    );
    const { client, errors } = valueClient();
    await expect(client.getNumberValue('a', user, -1)).resolves.toBe(-1);
    await expect(client.getJsonValue('b', user, null)).resolves.toBeNull();
    expect(errors.map(([error]) => error.code)).toEqual(['INVALID_RESPONSE', 'INVALID_RESPONSE']);
  });

  it('reports every call that returns the default, not only the first', async () => {
    mockFetch(served({ value: 'Buy now', value_type: 'string' }));
    const { client, errors } = valueClient();
    await client.getNumberValue('f', user, 0);
    await client.getNumberValue('f', user, 0);
    expect(errors).toHaveLength(2);
  });

  it('never rejects: no userId, no onError, or a throwing onError handler', async () => {
    mockFetch({}, 500);
    const { client, errors } = valueClient();
    await expect(client.getStringValue('f', { userId: '' }, 'x')).resolves.toBe('x');
    expect(errors.map(([error, operation]) => [error.message, operation])).toEqual([['user.userId is required', 'value']]);
    const quiet = new ExperimentationClient(baseConfig);
    await expect(quiet.getNumberValue('f', user, 3)).resolves.toBe(3);
    const throwing = new ExperimentationClient({ ...baseConfig, onError: () => { throw new Error('handler bug'); } });
    await expect(throwing.getJsonValue('f', user, [])).resolves.toEqual([]);
  });

  it('getJsonValue returns a new copy on every call: changing one changes nothing the SDK keeps', async () => {
    const fetchMock = mockFetch(served({ key: 'theme', value: { color: 'blue', sizes: [1, 2] }, value_type: 'json' }));
    const client = new ExperimentationClient(baseConfig);
    const first = await client.getJsonValue<{ color: string; sizes: number[] }>('theme', user, { color: '', sizes: [] });
    first.color = 'red';
    first.sizes.push(3);
    const second = await client.getJsonValue('theme', user, {});
    expect(second).toEqual({ color: 'blue', sizes: [1, 2] });
    expect(second).not.toBe(first);
    expect((await client.evaluateFlag('theme', user)).value).toEqual({ color: 'blue', sizes: [1, 2] });
    expect(fetchMock).toHaveBeenCalledTimes(1); // both later answers came from the cache
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
  it('sends the event once to /api/v1/tracking/track with no key when nothing is attributed to the user', async () => {
    const fetchMock = mockFetch({});
    const client = new ExperimentationClient(baseConfig);
    await expect(
      client.track('user-123', 'page_view', { properties: { page: '/' }, value: 2 })
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

  it("never sends another user's keys: that user's event goes once to /api/v1/tracking/track with no key", async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', { userId: 'someone-else' });
    fetchMock.mockClear();
    await client.track('user-123', 'page_view');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(call(fetchMock).url).toBe('https://api.example.com/api/v1/tracking/track');
    expect(call(fetchMock).body).toEqual({ event_type: 'page_view', event_name: 'page_view', user_id: 'user-123' });
  });

  it('reports a failed untagged send through onError as track', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('down'));
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });
    await expect(client.track('user-123', 'page_view')).resolves.toBeUndefined();
    expect(onError).toHaveBeenCalledWith(expect.any(ExperimentationError), 'track');
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

  it('does not fan out to a failed assignment, and still fans out to one past the cache TTL', async () => {
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
    now.mockReturnValue(1_001_100); // 'old' has left the cache, 'fresh' is still in it
    expect(client.getAssignments('user-123').map(a => a.experimentKey)).toEqual(['fresh']);

    await client.track('user-123', 'click');
    expect(call(fetchMock, 3).url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(call(fetchMock, 3).body.events.map((e: { experiment_key: string }) => e.experiment_key)).toEqual([
      'old',
      'fresh',
    ]);
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

// ─── track — late conversions (the attributed keys outlive the cache) ────────

type Body = { experiment_key?: string; feature_flag_key?: string };
const batchKeys = (body: { events: Body[] }) => body.events.map(e => e.experiment_key ?? `flag:${e.feature_flag_key}`);
const BATCH_URL = 'https://api.example.com/api/v1/tracking/batch';
const TRACK_URL = 'https://api.example.com/api/v1/tracking/track';

describe('track after the cache TTL', () => {
  it('#1423: a cached answer older than 24 hours still tags a keyless track() (cacheTtlMs above 24 h)', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1, failure_count: 0 } });
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 48 * HOUR });
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 25 * HOUR); // past 24 hours, inside the 48 hour cache
    expect(client.getAssignments('user-123')).toHaveLength(1);

    await client.track('user-123', 'purchase', { value: 120 });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 1).body)).toEqual(['checkout']);
    now.mockRestore();
  });

  it('cacheTtlMs: 0 caches nothing, and a key-less track() still reaches the experiment and flag just answered', async () => {
    jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: flagOn }, { body: { success_count: 2, failure_count: 0 } });
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 0 });
    await client.getAssignment('checkout', user);
    await client.evaluateFlag('new-search', user);
    expect(client.getAssignments('user-123')).toEqual([]);
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);

    await client.track('user-123', 'purchase');

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(call(fetchMock, 2).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 2).body)).toEqual(['checkout', 'flag:new-search']);
  });

  it('the record outlives the cache entry: at exactly a cacheTtlMs above 24 h the entry has expired and a key-less track() is still tagged', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1, failure_count: 0 } });
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 48 * HOUR });
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 48 * HOUR - 1);
    expect(client.getAssignments('user-123')).toHaveLength(1);
    now.mockReturnValue(T0 + 48 * HOUR);
    expect(client.getAssignments('user-123')).toEqual([]);

    await client.track('user-123', 'purchase');

    expect(call(fetchMock, 1).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 1).body)).toEqual(['checkout']);
  });

  it('sends an untagged purchase made 301 s after the assignment to the experiment', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1, failure_count: 0 } });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 301 * SECOND); // past the 300 s cache TTL
    expect(client.getAssignments('user-123')).toEqual([]);

    await client.track('user-123', 'purchase', { value: 120 });

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
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1, failure_count: 0 } });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 299 * SECOND);
    expect(client.getAssignments('user-123').map(a => a.experimentKey)).toEqual(['checkout']);

    await client.track('user-123', 'purchase', { value: 120 });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 1).body)).toEqual(['checkout']);
  });

  it('sends to exactly n experiments plus m flags past the TTL, each once, experiments first', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetch({ success_count: 5, failure_count: 0 });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ body: { ...assignment, experiment_key: 'hero' } }))
      .mockResolvedValueOnce(jsonResponse({ body: { ...assignment, experiment_key: 'checkout' } }))
      .mockResolvedValueOnce(jsonResponse({ body: flagOn }))
      .mockResolvedValueOnce(jsonResponse({ body: { ...assignment, experiment_key: 'sort' } }))
      .mockResolvedValueOnce(jsonResponse({ body: flagOff }))
      .mockResolvedValueOnce(jsonResponse({ body: { ...assignment, experiment_key: 'checkout' } }));
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('hero', user);
    await client.getAssignment('checkout', user);
    await client.evaluateFlag('new-search', user);
    await client.getAssignment('sort', user);
    await client.evaluateFlag('free-shipping', user);
    now.mockReturnValue(T0 + 400 * SECOND);
    await client.getAssignment('checkout', user); // answered again: still one entry, in its first place
    now.mockReturnValue(T0 + 800 * SECOND);
    fetchMock.mockClear();

    await client.track('user-123', 'purchase');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { events } = call(fetchMock).body;
    expect(events).toHaveLength(5);
    expect(batchKeys({ events })).toEqual(['hero', 'checkout', 'sort', 'flag:new-search', 'flag:free-shipping']);
  });

  it.each(['holdout', 'mutual_exclusion', 'targeting'])(
    'still sends to an experiment answered assigned: false with reason %s, past the TTL',
    async reason => {
      const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
      const fetchMock = mockFetchSequence(
        { body: { ...controlAssignment, experiment_key: 'hero', assigned: false, reason } },
        { body: { success_count: 1, failure_count: 0 } }
      );
      const client = new ExperimentationClient(baseConfig);
      await client.getAssignment('hero', user);
      now.mockReturnValue(T0 + 301 * SECOND);

      await client.track('user-123', 'purchase');

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
        { body: { success_count: 1, failure_count: 0 } }
      );
      const client = new ExperimentationClient(baseConfig);
      await client.getAssignment('checkout', user);
      await client.getAssignment('hero', user);

      await client.track('user-123', 'purchase');

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
      { body: { success_count: 1, failure_count: 0 } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 301 * SECOND);
    await client.getAssignment('checkout', user); // the experiment was paused meanwhile

    await client.track('user-123', 'purchase');
    expect(call(fetchMock, 2).url).toBe(TRACK_URL);
    expect(call(fetchMock, 2).body).not.toHaveProperty('experiment_key');

    now.mockReturnValue(T0 + 602 * SECOND);
    await client.getAssignment('checkout', user); // resumed
    await client.track('user-123', 'purchase');
    expect(call(fetchMock, 4).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 4).body)).toEqual(['checkout']);
  });

  it('drops an experiment whose assignment is later answered 404: the event goes once with no key', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence(
      { body: assignment },
      { status: 404, body: { detail: "Active experiment with key 'checkout' not found" } },
      { body: {} }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 301 * SECOND); // the cache has expired; the server now answers 404
    await expect(client.getVariant('checkout', user)).resolves.toBe('control');
    now.mockReturnValue(T0 + 3600 * SECOND);

    await client.track('user-123', 'purchase');

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(call(fetchMock, 2).url).toBe(TRACK_URL);
    expect(call(fetchMock, 2).body).toEqual({ event_type: 'purchase', event_name: 'purchase', user_id: 'user-123' });
  });

  it.each([
    ['a 500', { status: 500, body: {} }],
    ['a network failure', new Error('ECONNRESET')],
  ])('keeps the key when the later assignment fails with %s', async (_label, failure) => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, failure as Step | Error, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 301 * SECOND);
    await expect(client.getVariant('checkout', user)).resolves.toBe('control');

    await client.track('user-123', 'purchase');

    expect(call(fetchMock, 2).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 2).body)).toEqual(['checkout']);
  });

  it('sends to a key answered 24 hours less 1 s ago', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1, failure_count: 0 } });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 24 * HOUR - SECOND);

    await client.track('user-123', 'purchase');

    expect(call(fetchMock, 1).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 1).body)).toEqual(['checkout']);
  });

  it('does not send to a key answered more than 24 hours ago: the event goes once with no key', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: {} });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 24 * HOUR + SECOND);

    await client.track('user-123', 'purchase');

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toBe(TRACK_URL);
    expect(call(fetchMock, 1).body).toEqual({ event_type: 'purchase', event_name: 'purchase', user_id: 'user-123' });
  });

  it('still sends to the attributed keys after clearCache, which empties only the cache', async () => {
    const fetchMock = mockFetchSequence(
      { body: assignment },
      { body: flagOn },
      { body: { success_count: 2, failure_count: 0 } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    await client.evaluateFlag('new-search', user);
    client.clearCache();
    expect(client.getAssignments('user-123')).toEqual([]);
    expect(client.getEvaluatedFlags('user-123')).toEqual([]);

    await client.track('user-123', 'purchase');

    expect(call(fetchMock, 2).url).toBe(BATCH_URL);
    expect(batchKeys(call(fetchMock, 2).body)).toEqual(['checkout', 'flag:new-search']);
  });

  it('another client instance has none of them: its event goes once with no key', async () => {
    const fetchMock = mockFetchSequence({ body: assignment }, { body: {} });
    const assigning = new ExperimentationClient(baseConfig);
    const tracking = new ExperimentationClient(baseConfig); // say, an order service
    await assigning.getAssignment('checkout', user);

    await tracking.track('user-123', 'purchase');

    expect(call(fetchMock, 1).url).toBe(TRACK_URL);
    expect(call(fetchMock, 1).body).toEqual({ event_type: 'purchase', event_name: 'purchase', user_id: 'user-123' });
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

  it('expands keyless entries with the fan-out rule and sends those with nothing attributed once with no key', async () => {
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 3, failure_count: 0 } });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    await client.trackBatch([
      { userId: 'user-123', eventName: 'page_view' },
      { userId: 'nobody', eventName: 'page_view' },
      { userId: 'user-123', eventName: 'click', featureFlagKey: 'f' },
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(call(fetchMock, 1).body.events).toEqual([
      { event_type: 'page_view', event_name: 'page_view', user_id: 'user-123', experiment_key: 'checkout' },
      { event_type: 'page_view', event_name: 'page_view', user_id: 'nobody' },
      { event_type: 'click', event_name: 'click', user_id: 'user-123', feature_flag_key: 'f' },
    ]);
  });

  it('expands keyless entries to the keys attributed past the cache TTL', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence({ body: assignment }, { body: { success_count: 1, failure_count: 0 } });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('checkout', user);
    now.mockReturnValue(T0 + 301 * SECOND);
    await client.trackBatch([{ userId: 'user-123', eventName: 'purchase', value: 9 }]);
    expect(call(fetchMock, 1).body.events).toEqual([
      { event_type: 'purchase', event_name: 'purchase', user_id: 'user-123', experiment_key: 'checkout', value: 9 },
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

// ─── reportError ──────────────────────────────────────────────────────────────

const ERRORS_URL = 'https://api.example.com/api/v1/tracking/errors';
const STORED = { id: 'err-1', error_type: 'crash', timestamp: '2026-10-26T10:00:00Z' };

describe('reportError', () => {
  it('POSTs one report to /api/v1/tracking/errors with the server field names and resolves true on 201', async () => {
    const fetchMock = mockFetchSequence({ status: 201, body: STORED });
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });

    const stored = await client.reportError({
      errorType: 'crash',
      message: 'TypeError in checkout',
      featureFlagKey: 'new-checkout',
      experimentKey: 'checkout',
      userId: 'user-123',
      stackTrace: 'at pay (checkout.ts:12)',
      metadata: { os: 'linux', release: '3.2.1' },
      timestamp: new Date('2026-10-26T09:59:00.000Z'),
    });

    expect(stored).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { url, init, body } = call(fetchMock);
    expect(url).toBe(ERRORS_URL);
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual(expectedHeaders);
    expect(body).toEqual({
      error_type: 'crash',
      message: 'TypeError in checkout',
      feature_flag_key: 'new-checkout',
      experiment_key: 'checkout',
      user_id: 'user-123',
      stack_trace: 'at pay (checkout.ts:12)',
      metadata: { os: 'linux', release: '3.2.1' },
      timestamp: '2026-10-26T09:59:00.000Z',
    });
    expect(onError).not.toHaveBeenCalled();
  });

  it('sends only the fields given, and a string timestamp as it is', async () => {
    const fetchMock = mockFetchSequence({ status: 201, body: STORED });
    const client = new ExperimentationClient(baseConfig);
    await client.reportError({ errorType: 'crash', message: 'boom', featureFlagKey: 'f', timestamp: '2026-01-02T03:04:05Z' });
    expect(call(fetchMock).body).toEqual({
      error_type: 'crash',
      message: 'boom',
      feature_flag_key: 'f',
      timestamp: '2026-01-02T03:04:05Z',
    });
  });

  it('does not check the report itself: one with no key is sent, and the 422 resolves false', async () => {
    const fetchMock = mockFetchSequence({ status: 422, body: { detail: 'At least one of feature_flag_key or experiment_key is required' } });
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });
    await expect(client.reportError({ errorType: 'crash', message: 'boom' })).resolves.toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(call(fetchMock).body).toEqual({ error_type: 'crash', message: 'boom' });
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ status: 422 }), 'reportError');
  });

  it.each([401, 404, 422, 500, 503])(
    'resolves false on %s and passes the failure to onError as reportError',
    async status => {
      mockFetchSequence({ status, body: { detail: 'no' } });
      const onError = jest.fn();
      const client = new ExperimentationClient({ ...baseConfig, onError });
      await expect(client.reportError({ errorType: 'crash', message: 'boom', featureFlagKey: 'f' })).resolves.toBe(false);
      expect(onError).toHaveBeenCalledTimes(1);
      const [error, operation] = onError.mock.calls[0];
      expect(error).toBeInstanceOf(ExperimentationError);
      expect(error.status).toBe(status);
      expect(operation).toBe('reportError');
    }
  );

  it('resolves false on a 2xx that is not 201: true means stored, and only 201 says so', async () => {
    mockFetchSequence({ status: 200, body: STORED });
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });
    await expect(client.reportError({ errorType: 'crash', message: 'boom', featureFlagKey: 'f' })).resolves.toBe(false);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ status: 200 }), 'reportError');
  });

  it('resolves false on a 429 that is still a 429 after its one retry', async () => {
    const fetchMock = mockFetchSequence(
      { status: 429, headers: { 'Retry-After': '0' } },
      { status: 429, headers: { 'Retry-After': '0' } }
    );
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });
    await expect(client.reportError({ errorType: 'crash', message: 'boom', featureFlagKey: 'f' })).resolves.toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ status: 429 }), 'reportError');
  });

  it('resolves true when the retry after a 429 is answered 201', async () => {
    const fetchMock = mockFetchSequence({ status: 429, headers: { 'Retry-After': '0' } }, { status: 201, body: STORED });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.reportError({ errorType: 'crash', message: 'boom', featureFlagKey: 'f' })).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('resolves false on a network failure and passes it to onError', async () => {
    mockFetchSequence(new Error('ECONNREFUSED'));
    const onError = jest.fn();
    const client = new ExperimentationClient({ ...baseConfig, onError });
    await expect(client.reportError({ errorType: 'crash', message: 'boom', featureFlagKey: 'f' })).resolves.toBe(false);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: 'NETWORK_ERROR' }), 'reportError');
  });

  it('never rejects, even with a throwing onError handler or no report at all', async () => {
    mockFetchSequence({ status: 500 });
    const client = new ExperimentationClient({ ...baseConfig, onError: () => { throw new Error('handler bug'); } });
    await expect(client.reportError({ errorType: 'crash', message: 'boom', featureFlagKey: 'f' })).resolves.toBe(false);
    await expect(client.reportError(undefined as unknown as ErrorReport)).resolves.toBe(false);
  });
});

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
