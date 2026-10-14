/**
 * Tests for ExperimentationProvider (OpenFeature TypeScript provider).
 *
 * The provider delegates to @getexperimently/js-sdk; `fetch` is injected
 * so no network calls are made. Coverage:
 *   - construction / metadata / baseUrl handling / reusing a JS SDK client
 *   - request shape: GET /api/v1/feature-flags/evaluate/{key}?user_id=…, headers, no attributes
 *   - boolean / string / number / object resolution rules
 *   - error mapping: 404 → FLAG_NOT_FOUND, network → GENERAL, missing targetingKey
 *   - per user + flag caching (CACHED reason, TTL expiry, failures not cached)
 *   - onClose / refreshFlags clear the cache
 *   - hash utility vectors
 *   - OpenFeature.setProviderAndWait registration
 *   - provider.client tracking fan-out includes flags evaluated via OpenFeature
 */

import { ExperimentationProvider } from '../src/ExperimentationProvider';
import type { ExperimentationProviderOptions } from '../src/types';
import { ErrorCode, StandardResolutionReasons } from '@openfeature/server-sdk';
import { ExperimentationClient } from '@getexperimently/js-sdk';
import type { FlagEvaluateResponse } from '@getexperimently/js-sdk';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface Step {
  body?: unknown;
  status?: number;
}

function response(step: Step) {
  const status = step.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: { get: () => null },
    json: async () => step.body ?? {},
  } as unknown as Response;
}

/** Mock fetch that answers every call with the same flag evaluation. */
function flagFetch(body: Partial<FlagEvaluateResponse> & { enabled: boolean }, status = 200): jest.MockedFunction<typeof fetch> {
  return jest.fn().mockResolvedValue(response({ body: { key: 'flag', config: null, ...body }, status }));
}

/** Mock fetch that answers calls in order. */
function sequenceFetch(...steps: Array<Step | Error>): jest.MockedFunction<typeof fetch> {
  const mock = jest.fn();
  for (const step of steps) {
    if (step instanceof Error) mock.mockRejectedValueOnce(step);
    else mock.mockResolvedValueOnce(response(step));
  }
  return mock;
}

function errorFetch(message = 'Network error'): jest.MockedFunction<typeof fetch> {
  return jest.fn().mockRejectedValue(new Error(message));
}

function makeProvider(fetchImpl: typeof fetch, extra: Partial<ExperimentationProviderOptions> = {}): ExperimentationProvider {
  return new ExperimentationProvider({
    apiKey: 'test-api-key',
    baseUrl: 'http://localhost:8000',
    cacheTtlMs: 30_000,
    fetch: fetchImpl,
    ...extra,
  });
}

function requestOf(fetchMock: jest.MockedFunction<typeof fetch>, index = 0) {
  const [url, init] = fetchMock.mock.calls[index] as [string, RequestInit];
  return { url, init, body: init.body ? JSON.parse(init.body as string) : undefined };
}

const ctx = { targetingKey: 'user-1' };

afterEach(() => {
  jest.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — construction', () => {
  test('requires apiKey', () => {
    expect(() => new ExperimentationProvider({ apiKey: '' })).toThrow('apiKey is required');
  });

  test('provider metadata name is unchanged', () => {
    const provider = makeProvider(flagFetch({ enabled: true }));
    expect(provider.metadata.name).toBe('experimently-provider');
  });

  test('hooks are undefined by default', () => {
    expect(makeProvider(flagFetch({ enabled: true })).hooks).toBeUndefined();
  });

  test('defaults baseUrl to http://localhost:8000', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = new ExperimentationProvider({ apiKey: 'key', fetch: fetchMock });
    await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(requestOf(fetchMock).url).toBe('http://localhost:8000/api/v1/feature-flags/evaluate/f?user_id=user-1');
  });

  test('strips trailing slash from baseUrl', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = new ExperimentationProvider({ apiKey: 'key', baseUrl: 'https://api.example.com/', fetch: fetchMock });
    await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(requestOf(fetchMock).url).toBe('https://api.example.com/api/v1/feature-flags/evaluate/f?user_id=user-1');
  });

  test('exposes the underlying JS SDK client', () => {
    const provider = makeProvider(flagFetch({ enabled: true }));
    expect(provider.client).toBeInstanceOf(ExperimentationClient);
  });

  test('reuses an injected JS SDK client (apiKey not required then)', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const client = new ExperimentationClient({ apiUrl: 'https://shared.example.com', apiKey: 'shared', fetch: fetchMock });
    const provider = new ExperimentationProvider({ apiKey: '', client });
    expect(provider.client).toBe(client);
    await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(requestOf(fetchMock).url).toContain('https://shared.example.com/');
  });

  test('initialize() makes no request (nothing to pre-fetch)', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.initialize();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Request shape
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — request', () => {
  test('GETs /api/v1/feature-flags/evaluate/{key}?user_id=targetingKey with the contract headers', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.resolveBooleanEvaluation('dark-mode', false, { targetingKey: 'user 1/a' });

    const { url, init } = requestOf(fetchMock);
    expect(url).toBe('http://localhost:8000/api/v1/feature-flags/evaluate/dark-mode?user_id=user%201%2Fa');
    expect(init.method).toBe('GET');
    expect(init.headers).toEqual({
      'X-API-Key': 'test-api-key',
      'Content-Type': 'application/json',
      Accept: 'application/json',
    });
    expect(init.body).toBeUndefined();
  });

  test('URL-encodes the flag key', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.resolveBooleanEvaluation('a b/c', false, ctx);
    expect(requestOf(fetchMock).url).toContain('/evaluate/a%20b%2Fc?user_id=user-1');
  });

  test('does not send context attributes (only targetingKey → user_id)', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.resolveBooleanEvaluation('f', false, { targetingKey: 'user-1', country: 'US', plan: 'pro' });
    const { url, init } = requestOf(fetchMock);
    expect(url).not.toContain('country');
    expect(init.body).toBeUndefined();
  });

  test('missing targetingKey → default with TARGETING_KEY_MISSING and no request', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    const result = await provider.resolveBooleanEvaluation('f', true);
    expect(result).toEqual({
      value: true,
      reason: StandardResolutionReasons.ERROR,
      errorCode: ErrorCode.TARGETING_KEY_MISSING,
      errorMessage: expect.stringContaining('targetingKey'),
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test('empty targetingKey is treated as missing', async () => {
    const provider = makeProvider(flagFetch({ enabled: true }));
    const result = await provider.resolveStringEvaluation('f', 'd', { targetingKey: '' });
    expect(result.errorCode).toBe(ErrorCode.TARGETING_KEY_MISSING);
  });
});

// ---------------------------------------------------------------------------
// Boolean resolution
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — resolveBooleanEvaluation()', () => {
  test('returns true with TARGETING_MATCH for an enabled flag', async () => {
    const provider = makeProvider(flagFetch({ key: 'my-feature', enabled: true }));
    const result = await provider.resolveBooleanEvaluation('my-feature', false, ctx);
    expect(result).toEqual({
      value: true,
      reason: StandardResolutionReasons.TARGETING_MATCH,
      variant: undefined,
      flagMetadata: { flagKey: 'my-feature', enabled: true },
    });
  });

  test('returns false with DISABLED for a disabled flag (ignores defaultValue)', async () => {
    const provider = makeProvider(flagFetch({ enabled: false }));
    const result = await provider.resolveBooleanEvaluation('off', true, ctx);
    expect(result.value).toBe(false);
    expect(result.reason).toBe(StandardResolutionReasons.DISABLED);
    expect(result.errorCode).toBeUndefined();
  });

  test('reports config.variant as the variant when it is a string', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: { variant: 'blue' } }));
    const result = await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(result.variant).toBe('blue');
  });

  test('404 → defaultValue with FLAG_NOT_FOUND and reason ERROR', async () => {
    const provider = makeProvider(flagFetch({ enabled: true }, 404));
    const result = await provider.resolveBooleanEvaluation('nonexistent', true, ctx);
    expect(result.value).toBe(true);
    expect(result.reason).toBe(StandardResolutionReasons.ERROR);
    expect(result.errorCode).toBe(ErrorCode.FLAG_NOT_FOUND);
    expect(result.errorMessage).toBe('Flag "nonexistent" not found');
  });

  test('network error → defaultValue with GENERAL and reason ERROR', async () => {
    const provider = makeProvider(errorFetch('ECONNREFUSED'));
    const result = await provider.resolveBooleanEvaluation('f', true, ctx);
    expect(result.value).toBe(true);
    expect(result.reason).toBe(StandardResolutionReasons.ERROR);
    expect(result.errorCode).toBe(ErrorCode.GENERAL);
    expect(result.errorMessage).toContain('ECONNREFUSED');
  });

  test('401 → defaultValue with GENERAL', async () => {
    const provider = makeProvider(flagFetch({ enabled: true }, 401));
    const result = await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(result.value).toBe(false);
    expect(result.errorCode).toBe(ErrorCode.GENERAL);
  });
});

// ---------------------------------------------------------------------------
// String resolution
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — resolveStringEvaluation()', () => {
  test('returns config.variant when it is a string', async () => {
    const provider = makeProvider(flagFetch({ key: 'ab', enabled: true, config: { variant: 'new-design', color: 'green' } }));
    const result = await provider.resolveStringEvaluation('ab', 'original', ctx);
    expect(result).toEqual({
      value: 'new-design',
      reason: StandardResolutionReasons.TARGETING_MATCH,
      variant: 'new-design',
      flagMetadata: { flagKey: 'ab', enabled: true },
    });
  });

  test('returns config itself when config is a string', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: 'plain' }));
    const result = await provider.resolveStringEvaluation('f', 'd', ctx);
    expect(result.value).toBe('plain');
    expect(result.variant).toBeUndefined();
  });

  test('enabled flag without a string variant → defaultValue with reason DEFAULT', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: { variant: 42 } }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result.value).toBe('fallback');
    expect(result.reason).toBe(StandardResolutionReasons.DEFAULT);
    expect(result.errorCode).toBeUndefined();
  });

  test('enabled flag with null config → defaultValue with reason DEFAULT', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: null }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result.value).toBe('fallback');
    expect(result.reason).toBe(StandardResolutionReasons.DEFAULT);
  });

  test('disabled flag → defaultValue with reason DISABLED even when config names a variant', async () => {
    const provider = makeProvider(flagFetch({ enabled: false, config: { variant: 'treatment' } }));
    const result = await provider.resolveStringEvaluation('f', 'control', ctx);
    expect(result.value).toBe('control');
    expect(result.reason).toBe(StandardResolutionReasons.DISABLED);
  });

  test('404 → defaultValue with FLAG_NOT_FOUND', async () => {
    const provider = makeProvider(flagFetch({ enabled: true }, 404));
    const result = await provider.resolveStringEvaluation('missing', 'default-str', ctx);
    expect(result.value).toBe('default-str');
    expect(result.errorCode).toBe(ErrorCode.FLAG_NOT_FOUND);
  });
});

// ---------------------------------------------------------------------------
// Number resolution
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — resolveNumberEvaluation()', () => {
  test('returns config.value when it is a number', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: { value: 9.99, variant: 'v1' } }));
    const result = await provider.resolveNumberEvaluation('price', 0, ctx);
    expect(result.value).toBe(9.99);
    expect(result.variant).toBe('v1');
    expect(result.reason).toBe(StandardResolutionReasons.TARGETING_MATCH);
  });

  test('returns config itself when config is a number', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: 100 }));
    const result = await provider.resolveNumberEvaluation('count', 0, ctx);
    expect(result.value).toBe(100);
  });

  test('non-numeric config → defaultValue with reason DEFAULT', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: { value: 'not-a-number' } }));
    const result = await provider.resolveNumberEvaluation('f', 7, ctx);
    expect(result.value).toBe(7);
    expect(result.reason).toBe(StandardResolutionReasons.DEFAULT);
  });

  test('disabled flag → defaultValue with reason DISABLED', async () => {
    const provider = makeProvider(flagFetch({ enabled: false, config: { value: 3 } }));
    const result = await provider.resolveNumberEvaluation('f', 42, ctx);
    expect(result.value).toBe(42);
    expect(result.reason).toBe(StandardResolutionReasons.DISABLED);
  });

  test('404 → defaultValue with FLAG_NOT_FOUND', async () => {
    const provider = makeProvider(flagFetch({ enabled: true }, 404));
    const result = await provider.resolveNumberEvaluation('missing', 42, ctx);
    expect(result.value).toBe(42);
    expect(result.errorCode).toBe(ErrorCode.FLAG_NOT_FOUND);
  });
});

// ---------------------------------------------------------------------------
// Object resolution
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — resolveObjectEvaluation()', () => {
  test('returns the config object', async () => {
    const config = { theme: 'dark', maxRetries: 3, enabled: true };
    const provider = makeProvider(flagFetch({ enabled: true, config }));
    const result = await provider.resolveObjectEvaluation('config-flag', {}, ctx);
    expect(result.value).toEqual(config);
    expect(result.reason).toBe(StandardResolutionReasons.TARGETING_MATCH);
  });

  test('returns a JSON array config', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: ['a', 'b'] }));
    const result = await provider.resolveObjectEvaluation('arr', [], ctx);
    expect(result.value).toEqual(['a', 'b']);
  });

  test('string config → defaultValue with reason DEFAULT', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: 'not-an-object' }));
    const result = await provider.resolveObjectEvaluation('f', { d: 1 }, ctx);
    expect(result.value).toEqual({ d: 1 });
    expect(result.reason).toBe(StandardResolutionReasons.DEFAULT);
  });

  test('null config → defaultValue with reason DEFAULT', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: null }));
    const result = await provider.resolveObjectEvaluation('f', { d: 1 }, ctx);
    expect(result.value).toEqual({ d: 1 });
    expect(result.reason).toBe(StandardResolutionReasons.DEFAULT);
  });

  test('disabled flag → defaultValue with reason DISABLED', async () => {
    const provider = makeProvider(flagFetch({ enabled: false, config: { x: 1 } }));
    const result = await provider.resolveObjectEvaluation('f', { default: true }, ctx);
    expect(result.value).toEqual({ default: true });
    expect(result.reason).toBe(StandardResolutionReasons.DISABLED);
  });

  test('404 → defaultValue with FLAG_NOT_FOUND', async () => {
    const provider = makeProvider(flagFetch({ enabled: true }, 404));
    const result = await provider.resolveObjectEvaluation('missing', { default: true }, ctx);
    expect(result.value).toEqual({ default: true });
    expect(result.errorCode).toBe(ErrorCode.FLAG_NOT_FOUND);
  });
});

// ---------------------------------------------------------------------------
// Cache behaviour
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — cache', () => {
  test('second evaluation for the same user + flag is served from cache with reason CACHED', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    const first = await provider.resolveBooleanEvaluation('f1', false, ctx);
    const second = await provider.resolveBooleanEvaluation('f1', false, ctx);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(first.reason).toBe(StandardResolutionReasons.TARGETING_MATCH);
    expect(second.reason).toBe(StandardResolutionReasons.CACHED);
    expect(second.value).toBe(true);
  });

  test('cache is per user and per flag', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.resolveBooleanEvaluation('f1', false, { targetingKey: 'u1' });
    await provider.resolveBooleanEvaluation('f1', false, { targetingKey: 'u2' });
    await provider.resolveBooleanEvaluation('f2', false, { targetingKey: 'u1' });
    await provider.resolveBooleanEvaluation('f1', false, { targetingKey: 'u1' });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  test('re-fetches after cacheTtlMs elapses', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = sequenceFetch({ body: { key: 'f', enabled: true, config: null } }, { body: { key: 'f', enabled: false, config: null } });
    const provider = makeProvider(fetchMock, { cacheTtlMs: 1_000 });

    await expect(provider.resolveBooleanEvaluation('f', false, ctx)).resolves.toMatchObject({ value: true });
    now.mockReturnValue(1_000_999);
    await expect(provider.resolveBooleanEvaluation('f', false, ctx)).resolves.toMatchObject({ value: true, reason: 'CACHED' });
    now.mockReturnValue(1_001_001);
    await expect(provider.resolveBooleanEvaluation('f', false, ctx)).resolves.toMatchObject({ value: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  test('failures are not cached — the next evaluation retries', async () => {
    const fetchMock = sequenceFetch(new Error('down'), { body: { key: 'f', enabled: true, config: null } });
    const provider = makeProvider(fetchMock);
    const first = await provider.resolveBooleanEvaluation('f', false, ctx);
    const second = await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(first.errorCode).toBe(ErrorCode.GENERAL);
    expect(second.value).toBe(true);
    expect(second.reason).toBe(StandardResolutionReasons.TARGETING_MATCH);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  test('onClose() clears the cache', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.resolveBooleanEvaluation('f', false, ctx);
    await provider.onClose();
    await provider.onClose(); // idempotent
    await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  test('refreshFlags() (deprecated) clears the cache and resolves', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.resolveBooleanEvaluation('f', false, ctx);
    await expect(provider.refreshFlags()).resolves.toBeUndefined();
    await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// Hash utility (cross-SDK test vectors)
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — hashUser utility', () => {
  const provider = new ExperimentationProvider({ apiKey: 'key', fetch: flagFetch({ enabled: true }) });

  test('matches the primary cross-SDK vector', () => {
    expect(provider.hashUser('user-123', 'my-flag')).toBeCloseTo(0.6927449859213084, 10);
  });

  test.each([
    ['alice', 'dark-mode', 0.0353864398784935],
    ['bob', 'new-checkout', 0.1463384565431625],
    ['', 'empty-user', 0.3582690393086523],
    ['a', 'b', 0.6056532170623541],
  ])('hashUser(%j, %j)', (userId, flagKey, expected) => {
    expect(provider.hashUser(userId, flagKey)).toBeCloseTo(expected, 10);
  });

  test('is deterministic and in [0, 1)', () => {
    const h = provider.hashUser('alice', 'flag-x');
    expect(h).toBe(provider.hashUser('alice', 'flag-x'));
    expect(h).toBeGreaterThanOrEqual(0);
    expect(h).toBeLessThan(1);
    expect(provider.hashUser('bob', 'flag-x')).not.toBe(h);
  });
});

// ---------------------------------------------------------------------------
// OpenFeature SDK integration (registration)
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — OpenFeature registration', () => {
  test('provider can be registered with OpenFeature.setProviderAndWait()', async () => {
    const { OpenFeature } = await import('@openfeature/server-sdk');
    const fetchMock = flagFetch({ key: 'registered-flag', enabled: true });
    const provider = new ExperimentationProvider({ apiKey: 'reg-key', fetch: fetchMock });

    await OpenFeature.setProviderAndWait(provider);
    const client = OpenFeature.getClient('test-registration');
    const details = await client.getBooleanDetails('registered-flag', false, { targetingKey: 'test-user' });
    expect(details.value).toBe(true);
    expect(details.reason).toBe(StandardResolutionReasons.TARGETING_MATCH);
    expect(requestOf(fetchMock).url).toContain('/api/v1/feature-flags/evaluate/registered-flag?user_id=test-user');

    await OpenFeature.clearProviders();
  });
});

// ---------------------------------------------------------------------------
// provider.client — experiments and tracking share the cache
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — provider.client', () => {
  test('a keyless track() fans out to flags evaluated through OpenFeature', async () => {
    const fetchMock = sequenceFetch(
      { body: { key: 'new-search', enabled: true, config: null } },
      { body: { success_count: 1, failure_count: 0 } },
    );
    const provider = makeProvider(fetchMock);
    await provider.resolveBooleanEvaluation('new-search', false, ctx);
    await provider.client.track('user-1', 'search', { properties: { q: 'boots' } });

    const { url, body } = requestOf(fetchMock, 1);
    expect(url).toBe('http://localhost:8000/api/v1/tracking/batch');
    expect(body).toEqual({
      events: [
        { event_type: 'search', event_name: 'search', user_id: 'user-1', feature_flag_key: 'new-search', metadata: { q: 'boots' } },
      ],
    });
  });

  test('getAssignment goes through POST /api/v1/tracking/assign', async () => {
    const fetchMock = sequenceFetch({
      body: { experiment_key: 'checkout', user_id: 'user-1', variant_id: 'v2', variant_name: 'treatment', is_control: false, configuration: { steps: 1 } },
    });
    const provider = makeProvider(fetchMock);
    const assignment = await provider.client.getAssignment('checkout', { userId: 'user-1', attributes: { plan: 'pro' } });
    expect(assignment).toMatchObject({ variantName: 'treatment', isControl: false, configuration: { steps: 1 } });
    const { url, body } = requestOf(fetchMock);
    expect(url).toBe('http://localhost:8000/api/v1/tracking/assign');
    expect(body).toEqual({ experiment_key: 'checkout', user_id: 'user-1', context: { plan: 'pro' } });
  });
});
