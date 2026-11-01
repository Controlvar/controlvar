/**
 * Tests for ExperimentationProvider (OpenFeature TypeScript provider).
 *
 * The provider delegates to @controlvar/js-sdk; `fetch` is injected
 * so no network calls are made. Coverage:
 *   - construction / metadata / baseUrl handling / reusing a JS SDK client
 *   - request shape: GET /api/v1/feature-flags/evaluate/{key}?user_id=…&context=…, headers;
 *     the context minus targetingKey is the `context`, a Date as ISO 8601
 *   - boolean resolution (unchanged), and string / number / object resolution: one test per
 *     row of the resolution table in the README
 *   - error mapping: 404 → FLAG_NOT_FOUND, network → GENERAL, missing targetingKey
 *   - per user + flag caching (CACHED reason, TTL expiry, failures not cached, the attributes
 *     not part of the key, cacheTtlMs 0)
 *   - onClose / refreshFlags clear the cache
 *   - hash utility vectors
 *   - OpenFeature.setProviderAndWait registration, and what an OpenFeature client returns
 *   - provider.client tracking fan-out includes flags evaluated via OpenFeature
 */

import { ExperimentationProvider } from '../src/ExperimentationProvider';
import type { ExperimentationProviderOptions } from '../src/types';
import { ErrorCode, StandardResolutionReasons } from '@openfeature/server-sdk';
import { ExperimentationClient } from '@controlvar/js-sdk';
import type { FlagEvaluateResponse } from '@controlvar/js-sdk';

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

/**
 * Mock fetch that answers every call with the same flag evaluation. The reason defaults to
 * `targeting_rule` (every server answer carries one); a test of a server without flag values
 * leaves out `value` and `value_type`, as such a server does.
 */
function flagFetch(body: Partial<FlagEvaluateResponse> & { enabled: boolean }, status = 200): jest.MockedFunction<typeof fetch> {
  return jest.fn().mockResolvedValue(response({ body: { key: 'flag', config: null, reason: 'targeting_rule', ...body }, status }));
}

/** Mock fetch whose 200 answer is not JSON. */
function invalidJsonFetch(): jest.MockedFunction<typeof fetch> {
  return jest.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: { get: () => null },
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON at position 0');
    },
  } as unknown as Response);
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

/** The `context` query parameter of a request, parsed; `undefined` when none was sent. */
function contextOf(fetchMock: jest.MockedFunction<typeof fetch>, index = 0): unknown {
  const raw = new URL(requestOf(fetchMock, index).url).searchParams.get('context');
  return raw === null ? undefined : JSON.parse(raw);
}

/** A server answer for a flag that serves a value (`value_type` other than boolean). */
function valueFetch(
  value_type: string,
  value: unknown,
  extra: Partial<FlagEvaluateResponse> = {},
  status = 200,
): jest.MockedFunction<typeof fetch> {
  return flagFetch({ enabled: true, reason: 'targeting_rule', value, value_type, ...extra }, status);
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
    expect(provider.metadata.name).toBe('controlvar-provider');
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

  test('sends every context key but targetingKey as the context query parameter', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.resolveBooleanEvaluation('f', false, { targetingKey: 'user-1', country: 'US', plan: 'pro', seats: 12, beta: true });
    const { url, init } = requestOf(fetchMock);
    expect(new URL(url).searchParams.get('user_id')).toBe('user-1');
    expect(contextOf(fetchMock)).toEqual({ country: 'US', plan: 'pro', seats: 12, beta: true });
    expect(init.body).toBeUndefined();
  });

  test('targetingKey is the user_id and never part of the context', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.resolveStringEvaluation('f', 'd', { targetingKey: 'user-1', plan: 'pro' });
    const context = contextOf(fetchMock) as Record<string, unknown>;
    expect(context).toEqual({ plan: 'pro' });
    expect(Object.keys(context)).not.toContain('targetingKey');
  });

  test('a Date, at any depth, is sent as its ISO 8601 string', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    const signedUp = new Date(Date.UTC(2026, 0, 2, 3, 4, 5, 6));
    await provider.resolveNumberEvaluation('f', 0, {
      targetingKey: 'user-1',
      signedUp,
      account: { renewed: signedUp, history: [signedUp, 'x'] },
      broken: new Date(Number.NaN),
    });
    expect(contextOf(fetchMock)).toEqual({
      signedUp: '2026-01-02T03:04:05.006Z',
      account: { renewed: '2026-01-02T03:04:05.006Z', history: ['2026-01-02T03:04:05.006Z', 'x'] },
      broken: null,
    });
  });

  test('a context with only targetingKey sends no context parameter', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    await provider.resolveObjectEvaluation('f', {}, { targetingKey: 'user-1' });
    expect(requestOf(fetchMock).url).toBe('http://localhost:8000/api/v1/feature-flags/evaluate/f?user_id=user-1');
  });

  test('a __proto__ key in the context stays an ordinary attribute', async () => {
    const fetchMock = flagFetch({ enabled: true });
    const provider = makeProvider(fetchMock);
    const context = JSON.parse('{"targetingKey":"user-1","__proto__":{"plan":"pro"},"nested":{"__proto__":{"a":1}}}');
    await provider.resolveBooleanEvaluation('f', false, context);
    expect(contextOf(fetchMock)).toEqual(JSON.parse('{"__proto__":{"plan":"pro"},"nested":{"__proto__":{"a":1}}}'));
    expect(({} as Record<string, unknown>).plan).toBeUndefined();
  });

  test('the SDK client is given the attributes, a Date already a string (what a local evaluation reads)', async () => {
    const client = new ExperimentationClient({ apiUrl: 'http://localhost:8000', apiKey: 'k', fetch: flagFetch({ enabled: true }) });
    const spy = jest.spyOn(client, 'evaluateFlag');
    const provider = new ExperimentationProvider({ apiKey: '', client });
    const at = new Date(Date.UTC(2026, 5, 1));
    await provider.resolveStringEvaluation('f', 'd', { targetingKey: 'user-1', plan: 'pro', at });
    expect(spy).toHaveBeenCalledWith('f', { userId: 'user-1', attributes: { plan: 'pro', at: '2026-06-01T00:00:00.000Z' } });
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
  test('targeting_rule, on → true, TARGETING_MATCH, variant "on"', async () => {
    const provider = makeProvider(valueFetch('boolean', true, { key: 'my-feature' }));
    const result = await provider.resolveBooleanEvaluation('my-feature', false, ctx);
    expect(result).toEqual({
      value: true,
      reason: StandardResolutionReasons.TARGETING_MATCH,
      variant: 'on',
      flagMetadata: { flagKey: 'my-feature', enabled: true },
    });
  });

  test('targeting_rule, off → false, TARGETING_MATCH, variant "off"', async () => {
    const provider = makeProvider(valueFetch('boolean', false, { enabled: false }));
    const result = await provider.resolveBooleanEvaluation('f', true, ctx);
    expect(result).toEqual({
      value: false,
      reason: StandardResolutionReasons.TARGETING_MATCH,
      variant: 'off',
      flagMetadata: { flagKey: 'f', enabled: false },
    });
  });

  test('rollout, on → true, SPLIT, variant "on"', async () => {
    const provider = makeProvider(valueFetch('boolean', true, { reason: 'rollout' }));
    const result = await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(result).toMatchObject({ value: true, reason: StandardResolutionReasons.SPLIT, variant: 'on' });
    expect(result.errorCode).toBeUndefined();
  });

  test('rollout, off → false, SPLIT, variant "off"', async () => {
    const provider = makeProvider(valueFetch('boolean', false, { reason: 'rollout', enabled: false }));
    const result = await provider.resolveBooleanEvaluation('f', true, ctx);
    expect(result).toMatchObject({ value: false, reason: StandardResolutionReasons.SPLIT, variant: 'off' });
  });

  test('inactive → false (the off value, not the caller default), DISABLED, variant "off"', async () => {
    const provider = makeProvider(valueFetch('boolean', false, { reason: 'inactive', enabled: false }));
    const result = await provider.resolveBooleanEvaluation('off', true, ctx);
    expect(result).toEqual({
      value: false,
      reason: StandardResolutionReasons.DISABLED,
      variant: 'off',
      flagMetadata: { flagKey: 'off', enabled: false },
    });
  });

  test('served from the SDK cache → the cached value and variant, CACHED', async () => {
    const fetchMock = valueFetch('boolean', false, { reason: 'rollout', enabled: false });
    const provider = makeProvider(fetchMock);
    await provider.resolveBooleanEvaluation('f', true, ctx);
    const second = await provider.resolveBooleanEvaluation('f', true, ctx);
    expect(second).toMatchObject({ value: false, reason: StandardResolutionReasons.CACHED, variant: 'off' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test('reason "error" → the caller default, ERROR, GENERAL (not false)', async () => {
    const provider = makeProvider(valueFetch('boolean', false, { reason: 'error', enabled: false }));
    const result = await provider.resolveBooleanEvaluation('f', true, ctx);
    expect(result).toEqual({
      value: true,
      reason: StandardResolutionReasons.ERROR,
      errorCode: ErrorCode.GENERAL,
      errorMessage: 'The server could not evaluate flag "f" for this user',
    });
  });

  test.each([
    ['string', 'Complete purchase', 'a string'],
    ['number', 10, 'a number'],
    ['json', { steps: 2 }, 'a JSON value'],
  ])('a %s flag → the caller default, ERROR, TYPE_MISMATCH', async (type, value, described) => {
    const provider = makeProvider(valueFetch(type, value));
    const result = await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(result).toEqual({
      value: false,
      reason: StandardResolutionReasons.ERROR,
      errorCode: ErrorCode.TYPE_MISMATCH,
      errorMessage: `Flag "f" serves ${described}; it was resolved as a boolean`,
    });
  });

  test('a server without flag values (no value_type) → enabled, with the server reason', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, reason: 'rollout' }));
    const result = await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(result).toMatchObject({ value: true, reason: StandardResolutionReasons.SPLIT, variant: 'on' });
    expect(result.errorCode).toBeUndefined();
  });

  test('a server reason this provider does not know, or none → UNKNOWN', async () => {
    const unknown = await makeProvider(valueFetch('boolean', true, { reason: 'prerequisite' })).resolveBooleanEvaluation('f', false, ctx);
    expect(unknown).toMatchObject({ value: true, reason: StandardResolutionReasons.UNKNOWN, variant: 'on' });
    const none = await makeProvider(flagFetch({ enabled: false, reason: undefined })).resolveBooleanEvaluation('f', true, ctx);
    expect(none).toMatchObject({ value: false, reason: StandardResolutionReasons.UNKNOWN, variant: 'off' });
  });

  test('the config is not read: the variant is "on" or "off"', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: { variant: 'blue' } }));
    const result = await provider.resolveBooleanEvaluation('f', false, ctx);
    expect(result.variant).toBe('on');
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

  test('a 2xx whose body is not JSON → the caller default, PARSE_ERROR', async () => {
    const result = await makeProvider(invalidJsonFetch()).resolveBooleanEvaluation('f', true, ctx);
    expect(result).toMatchObject({ value: true, reason: StandardResolutionReasons.ERROR, errorCode: ErrorCode.PARSE_ERROR });
    expect(result.errorMessage).toContain('Invalid JSON in response');
  });

  test('a 2xx whose JSON is not an evaluation → false, with no server reason UNKNOWN', async () => {
    const fetchMock = jest.fn().mockResolvedValue(response({ body: [] }));
    const result = await makeProvider(fetchMock).resolveBooleanEvaluation('f', true, ctx);
    expect(result).toMatchObject({ value: false, reason: StandardResolutionReasons.UNKNOWN, variant: 'off' });
    expect(result.errorCode).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// String resolution
// ---------------------------------------------------------------------------

describe('ExperimentationProvider — resolveStringEvaluation()', () => {
  test('targeting_rule, on → the on value, TARGETING_MATCH, variant "on"', async () => {
    const provider = makeProvider(valueFetch('string', 'Complete purchase'));
    const result = await provider.resolveStringEvaluation('checkout-button-label', 'fallback', ctx);
    expect(result).toEqual({
      value: 'Complete purchase',
      reason: StandardResolutionReasons.TARGETING_MATCH,
      variant: 'on',
      flagMetadata: { flagKey: 'checkout-button-label', enabled: true },
    });
  });

  test('targeting_rule, off → the off value, TARGETING_MATCH, variant "off"', async () => {
    const provider = makeProvider(valueFetch('string', 'Buy now', { enabled: false }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result).toEqual({
      value: 'Buy now',
      reason: StandardResolutionReasons.TARGETING_MATCH,
      variant: 'off',
      flagMetadata: { flagKey: 'f', enabled: false },
    });
  });

  test('rollout, on → the on value, SPLIT, variant "on"', async () => {
    const provider = makeProvider(valueFetch('string', 'Complete purchase', { reason: 'rollout' }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result).toMatchObject({ value: 'Complete purchase', reason: StandardResolutionReasons.SPLIT, variant: 'on' });
    expect(result.errorCode).toBeUndefined();
  });

  test('rollout, off → the off value, SPLIT, variant "off"', async () => {
    const provider = makeProvider(valueFetch('string', 'Buy now', { reason: 'rollout', enabled: false }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result).toMatchObject({ value: 'Buy now', reason: StandardResolutionReasons.SPLIT, variant: 'off' });
  });

  test('inactive → the off value, not the caller default, DISABLED, variant "off"', async () => {
    const provider = makeProvider(valueFetch('string', 'Buy now', { reason: 'inactive', enabled: false }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result).toEqual({
      value: 'Buy now',
      reason: StandardResolutionReasons.DISABLED,
      variant: 'off',
      flagMetadata: { flagKey: 'f', enabled: false },
    });
  });

  test('served from the SDK cache → the cached value and variant, CACHED', async () => {
    const fetchMock = valueFetch('string', 'Buy now', { reason: 'rollout', enabled: false });
    const provider = makeProvider(fetchMock);
    await provider.resolveStringEvaluation('f', 'fallback', ctx);
    const second = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(second).toMatchObject({ value: 'Buy now', reason: StandardResolutionReasons.CACHED, variant: 'off' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test('reason "error" → the caller default, ERROR, GENERAL (not the off value the server sent)', async () => {
    const provider = makeProvider(valueFetch('string', 'Buy now', { reason: 'error', enabled: false }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result).toEqual({
      value: 'fallback',
      reason: StandardResolutionReasons.ERROR,
      errorCode: ErrorCode.GENERAL,
      errorMessage: 'The server could not evaluate flag "f" for this user',
    });
  });

  test('a number flag → the caller default, ERROR, TYPE_MISMATCH', async () => {
    const provider = makeProvider(valueFetch('number', 10));
    const result = await provider.resolveStringEvaluation('price', 'fallback', ctx);
    expect(result).toEqual({
      value: 'fallback',
      reason: StandardResolutionReasons.ERROR,
      errorCode: ErrorCode.TYPE_MISMATCH,
      errorMessage: 'Flag "price" serves a number; it was resolved as a string',
    });
  });

  test('a boolean flag → the caller default, TYPE_MISMATCH (no coercion of true to "true")', async () => {
    const provider = makeProvider(valueFetch('boolean', true));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result.value).toBe('fallback');
    expect(result.errorCode).toBe(ErrorCode.TYPE_MISMATCH);
    expect(result.errorMessage).toBe('Flag "f" serves a boolean; it was resolved as a string');
  });

  test('a server without flag values (no value, no value_type) → the caller default, GENERAL', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, reason: 'rollout' }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result.value).toBe('fallback');
    expect(result.reason).toBe(StandardResolutionReasons.ERROR);
    expect(result.errorCode).toBe(ErrorCode.GENERAL);
    expect(result.errorMessage).toContain('came back without a value');
  });

  test('a value_type with no value (a stored value the server cannot serve) → the caller default, GENERAL', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, reason: 'rollout', value_type: 'string' }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result.value).toBe('fallback');
    expect(result.errorCode).toBe(ErrorCode.GENERAL);
  });

  test('a value that is not of the flag type → the caller default, PARSE_ERROR', async () => {
    const provider = makeProvider(valueFetch('string', 42));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result.value).toBe('fallback');
    expect(result.errorCode).toBe(ErrorCode.PARSE_ERROR);
    expect(result.errorMessage).toBe('Flag "f" serves a string, but the server\'s value is not one');
  });

  test('a 2xx whose body is not JSON → the caller default, PARSE_ERROR', async () => {
    const result = await makeProvider(invalidJsonFetch()).resolveStringEvaluation('f', 'fallback', ctx);
    expect(result).toMatchObject({ value: 'fallback', reason: StandardResolutionReasons.ERROR, errorCode: ErrorCode.PARSE_ERROR });
    expect(result.errorMessage).toContain('Invalid JSON in response');
  });

  test('a 2xx whose JSON is not an evaluation → the caller default, GENERAL (no value)', async () => {
    const fetchMock = jest.fn().mockResolvedValue(response({ body: [] }));
    const result = await makeProvider(fetchMock).resolveStringEvaluation('f', 'fallback', ctx);
    expect(result).toMatchObject({ value: 'fallback', reason: StandardResolutionReasons.ERROR, errorCode: ErrorCode.GENERAL });
    expect(result.errorMessage).toContain('came back without a value');
  });

  test('an empty string is a value', async () => {
    const provider = makeProvider(valueFetch('string', '', { enabled: false }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result).toMatchObject({ value: '', variant: 'off' });
    expect(result.errorCode).toBeUndefined();
  });

  test('a server reason this provider does not know → UNKNOWN', async () => {
    const provider = makeProvider(valueFetch('string', 'x', { reason: 'prerequisite' }));
    const result = await provider.resolveStringEvaluation('f', 'fallback', ctx);
    expect(result).toMatchObject({ value: 'x', reason: StandardResolutionReasons.UNKNOWN, variant: 'on' });
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
  test('targeting_rule, on → the on value, TARGETING_MATCH, variant "on"', async () => {
    const provider = makeProvider(valueFetch('number', 9.99));
    const result = await provider.resolveNumberEvaluation('price', 0, ctx);
    expect(result).toEqual({
      value: 9.99,
      reason: StandardResolutionReasons.TARGETING_MATCH,
      variant: 'on',
      flagMetadata: { flagKey: 'price', enabled: true },
    });
  });

  test('rollout, off → the off value (0 is a value), SPLIT, variant "off"', async () => {
    const provider = makeProvider(valueFetch('number', 0, { reason: 'rollout', enabled: false }));
    const result = await provider.resolveNumberEvaluation('f', 42, ctx);
    expect(result).toMatchObject({ value: 0, reason: StandardResolutionReasons.SPLIT, variant: 'off' });
    expect(result.errorCode).toBeUndefined();
  });

  test('inactive → the off value, not the caller default, DISABLED', async () => {
    const provider = makeProvider(valueFetch('number', 3, { reason: 'inactive', enabled: false }));
    const result = await provider.resolveNumberEvaluation('f', 42, ctx);
    expect(result).toMatchObject({ value: 3, reason: StandardResolutionReasons.DISABLED, variant: 'off' });
  });

  test('a string flag → the caller default, TYPE_MISMATCH', async () => {
    const provider = makeProvider(valueFetch('string', '10'));
    const result = await provider.resolveNumberEvaluation('f', 7, ctx);
    expect(result).toEqual({
      value: 7,
      reason: StandardResolutionReasons.ERROR,
      errorCode: ErrorCode.TYPE_MISMATCH,
      errorMessage: 'Flag "f" serves a string; it was resolved as a number',
    });
  });

  test('a json flag → the caller default, TYPE_MISMATCH', async () => {
    const provider = makeProvider(valueFetch('json', { n: 1 }));
    const result = await provider.resolveNumberEvaluation('f', 7, ctx);
    expect(result.value).toBe(7);
    expect(result.errorMessage).toBe('Flag "f" serves a JSON value; it was resolved as a number');
  });

  test('reason "error" → the caller default, GENERAL', async () => {
    const provider = makeProvider(valueFetch('number', 3, { reason: 'error', enabled: false }));
    const result = await provider.resolveNumberEvaluation('f', 42, ctx);
    expect(result).toMatchObject({ value: 42, reason: StandardResolutionReasons.ERROR, errorCode: ErrorCode.GENERAL });
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
  test('a json flag, targeting_rule, on → the on value, TARGETING_MATCH, variant "on"', async () => {
    const onValue = { theme: 'dark', maxRetries: 3, enabled: true };
    const provider = makeProvider(valueFetch('json', onValue));
    const result = await provider.resolveObjectEvaluation('config-flag', {}, ctx);
    expect(result).toEqual({
      value: onValue,
      reason: StandardResolutionReasons.TARGETING_MATCH,
      variant: 'on',
      flagMetadata: { flagKey: 'config-flag', enabled: true },
    });
  });

  test('a JSON array is a value', async () => {
    const provider = makeProvider(valueFetch('json', ['a', 'b'], { reason: 'rollout' }));
    const result = await provider.resolveObjectEvaluation('arr', [], ctx);
    expect(result).toMatchObject({ value: ['a', 'b'], reason: StandardResolutionReasons.SPLIT });
  });

  test('inactive → the off value, not the caller default, DISABLED, variant "off"', async () => {
    const provider = makeProvider(valueFetch('json', { x: 0 }, { reason: 'inactive', enabled: false }));
    const result = await provider.resolveObjectEvaluation('f', { default: true }, ctx);
    expect(result).toMatchObject({ value: { x: 0 }, reason: StandardResolutionReasons.DISABLED, variant: 'off' });
  });

  test('each call returns a new copy: changing one changes nothing the SDK keeps', async () => {
    const fetchMock = valueFetch('json', { limits: { seats: 5 } });
    const provider = makeProvider(fetchMock);
    const first = await provider.resolveObjectEvaluation('f', {}, ctx);
    (first.value as { limits: { seats: number } }).limits.seats = 999;
    const second = await provider.resolveObjectEvaluation('f', {}, ctx);
    expect(second).toMatchObject({ value: { limits: { seats: 5 } }, reason: StandardResolutionReasons.CACHED });
    const evaluation = await provider.client.evaluateFlag('f', { userId: 'user-1' });
    expect(evaluation.value).toEqual({ limits: { seats: 5 } });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test('a string flag → the caller default, TYPE_MISMATCH', async () => {
    const provider = makeProvider(valueFetch('string', 'not-an-object'));
    const result = await provider.resolveObjectEvaluation('f', { d: 1 }, ctx);
    expect(result).toEqual({
      value: { d: 1 },
      reason: StandardResolutionReasons.ERROR,
      errorCode: ErrorCode.TYPE_MISMATCH,
      errorMessage: 'Flag "f" serves a string; it was resolved as an object',
    });
  });

  test('reason "error" → the caller default, GENERAL', async () => {
    const provider = makeProvider(valueFetch('json', { x: 0 }, { reason: 'error', enabled: false }));
    const result = await provider.resolveObjectEvaluation('f', { d: 1 }, ctx);
    expect(result).toMatchObject({ value: { d: 1 }, reason: StandardResolutionReasons.ERROR, errorCode: ErrorCode.GENERAL });
  });

  test('a server without flag values → the caller default, GENERAL', async () => {
    const provider = makeProvider(flagFetch({ enabled: true, config: null }));
    const result = await provider.resolveObjectEvaluation('f', { d: 1 }, ctx);
    expect(result).toMatchObject({ value: { d: 1 }, reason: StandardResolutionReasons.ERROR, errorCode: ErrorCode.GENERAL });
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

  test('the attributes are not part of the cache key: a changed one is seen after cacheTtlMs', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = sequenceFetch(
      { body: { key: 'f', enabled: false, reason: 'targeting_rule', value: 'Buy now', value_type: 'string', config: null } },
      { body: { key: 'f', enabled: true, reason: 'targeting_rule', value: 'Complete purchase', value_type: 'string', config: null } },
    );
    const provider = makeProvider(fetchMock, { cacheTtlMs: 1_000 });

    await expect(provider.resolveStringEvaluation('f', 'd', { targetingKey: 'u1', plan: 'free' })).resolves.toMatchObject({ value: 'Buy now' });
    now.mockReturnValue(1_000_999);
    await expect(provider.resolveStringEvaluation('f', 'd', { targetingKey: 'u1', plan: 'enterprise' })).resolves.toMatchObject({
      value: 'Buy now',
      reason: StandardResolutionReasons.CACHED,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    now.mockReturnValue(1_001_000);
    await expect(provider.resolveStringEvaluation('f', 'd', { targetingKey: 'u1', plan: 'enterprise' })).resolves.toMatchObject({
      value: 'Complete purchase',
      reason: StandardResolutionReasons.TARGETING_MATCH,
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(contextOf(fetchMock, 1)).toEqual({ plan: 'enterprise' });
  });

  test('cacheTtlMs: 0 caches nothing: every call is sent with its own attributes', async () => {
    const fetchMock = valueFetch('string', 'Buy now', { enabled: false });
    const provider = makeProvider(fetchMock, { cacheTtlMs: 0 });
    await provider.resolveStringEvaluation('f', 'd', { targetingKey: 'u1', plan: 'free' });
    const second = await provider.resolveStringEvaluation('f', 'd', { targetingKey: 'u1', plan: 'enterprise' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(contextOf(fetchMock, 1)).toEqual({ plan: 'enterprise' });
    expect(second.reason).toBe(StandardResolutionReasons.TARGETING_MATCH);
  });

  test('a client with local evaluation answers each call from its ruleset with that call\'s attributes, never CACHED', async () => {
    const ruleset = {
      schema: 1,
      version: 'v1',
      bucketing: 'md5-mod100-v1',
      flags: [
        {
          key: 'label',
          active: true,
          evaluation: 'local',
          rollout_percentage: 0,
          rules: [
            {
              id: 'enterprise',
              rollout_percentage: 100,
              match: { op: 'and', conditions: [{ attribute: 'user.plan', operator: 'eq', value: 'enterprise' }], groups: [] },
            },
          ],
          default_rule: null,
          value_type: 'string',
          on_value: 'Complete purchase',
          off_value: 'Buy now',
        },
      ],
    };
    const fetchMock = jest.fn(async (url: string) =>
      url.includes('/api/v1/sdk/ruleset')
        ? response({ body: ruleset })
        : url.includes('/feature-flags/evaluate/')
          ? response({ status: 500 })
          : response({ status: 201, body: { accepted: 0, errors: [] } }),
    );
    const client = new ExperimentationClient({
      apiUrl: 'http://localhost:8000',
      apiKey: 'k',
      fetch: fetchMock as unknown as typeof fetch,
      evaluation: 'local',
    });
    try {
      await expect(client.ready()).resolves.toMatchObject({ ok: true });
      const provider = new ExperimentationProvider({ apiKey: '', client });
      const free = await provider.resolveStringEvaluation('label', 'd', { targetingKey: 'u1', plan: 'free' });
      const enterprise = await provider.resolveStringEvaluation('label', 'd', { targetingKey: 'u1', plan: 'enterprise' });
      expect(free).toMatchObject({ value: 'Buy now', reason: StandardResolutionReasons.SPLIT, variant: 'off' });
      expect(enterprise).toMatchObject({ value: 'Complete purchase', reason: StandardResolutionReasons.TARGETING_MATCH, variant: 'on' });
      const again = await provider.resolveBooleanEvaluation('label', false, { targetingKey: 'u1', plan: 'enterprise' });
      expect(again.errorCode).toBe(ErrorCode.TYPE_MISMATCH);
      expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/feature-flags/evaluate/'))).toEqual([]);
    } finally {
      await client.close();
    }
  });

  test('failures are not cached — the next evaluation retries', async () => {
    const fetchMock = sequenceFetch(new Error('down'), { body: { key: 'f', enabled: true, config: null, reason: 'targeting_rule' } });
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

  test('an OpenFeature client gets the off value of an inactive flag, with reason DISABLED', async () => {
    const { OpenFeature } = await import('@openfeature/server-sdk');
    const fetchMock = valueFetch('string', 'Buy now', { reason: 'inactive', enabled: false });
    await OpenFeature.setProviderAndWait(new ExperimentationProvider({ apiKey: 'k', fetch: fetchMock }));
    const client = OpenFeature.getClient('test-values');
    const details = await client.getStringDetails('checkout-button-label', 'fallback', { targetingKey: 'u1', plan: 'free' });
    expect(details).toMatchObject({ value: 'Buy now', reason: StandardResolutionReasons.DISABLED, variant: 'off' });
    expect(details.errorCode).toBeUndefined();
    expect(contextOf(fetchMock)).toEqual({ plan: 'free' });
    await OpenFeature.clearProviders();
  });

  test('an OpenFeature client gets its default with TYPE_MISMATCH for a flag of another type', async () => {
    const { OpenFeature } = await import('@openfeature/server-sdk');
    await OpenFeature.setProviderAndWait(new ExperimentationProvider({ apiKey: 'k', fetch: valueFetch('string', 'Buy now') }));
    const client = OpenFeature.getClient('test-values');
    const details = await client.getNumberDetails('checkout-button-label', 7, { targetingKey: 'u1' });
    expect(details).toMatchObject({ value: 7, reason: StandardResolutionReasons.ERROR, errorCode: ErrorCode.TYPE_MISMATCH });
    await OpenFeature.clearProviders();
  });

  test('an OpenFeature client reads a json flag with getObjectValue', async () => {
    const { OpenFeature } = await import('@openfeature/server-sdk');
    await OpenFeature.setProviderAndWait(
      new ExperimentationProvider({ apiKey: 'k', fetch: valueFetch('json', { steps: 2 }, { reason: 'rollout' }) }),
    );
    const client = OpenFeature.getClient('test-values');
    await expect(client.getObjectValue('checkout-config', { steps: 1 }, { targetingKey: 'u1' })).resolves.toEqual({ steps: 2 });
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
