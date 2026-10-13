/**
 * @jest-environment node
 */
import { ServerClient } from '../client/ServerClient';
import * as packageRoot from '../index';
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

const user: UserContext = { userId: 'user-123', attributes: { country: 'DE' } };

const flagOn: FeatureFlagEvaluateResponse = { key: 'my-flag', enabled: true, config: null };
const flagOff: FeatureFlagEvaluateResponse = { key: 'disabled-flag', enabled: false, config: null };
const flagWithVariant: FeatureFlagEvaluateResponse = {
  key: 'my-flag',
  enabled: true,
  config: { variant: 'treatment' },
};

const assignment: ExperimentAssignResponse = {
  experiment_key: 'hero',
  user_id: 'user-123',
  variant_id: 'var-9',
  variant_name: 'video_hero',
  is_control: false,
  configuration: { media: 'video' },
};

const controlDefaults = {
  variantKey: 'control',
  variantName: 'Control',
  variantId: null,
  isControl: true,
  configuration: null,
  loading: false,
};

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

function mockFetch(body: unknown, status = 200): jest.Mock {
  const mock = jest.fn().mockResolvedValue(jsonResponse(body, status));
  global.fetch = mock;
  return mock;
}

function mockFetchSequence(...steps: Array<{ body: unknown; status?: number } | Error>): jest.Mock {
  const mock = jest.fn();
  for (const step of steps) {
    if (step instanceof Error) mock.mockRejectedValueOnce(step);
    else mock.mockResolvedValueOnce(jsonResponse(step.body, step.status ?? 200));
  }
  global.fetch = mock;
  return mock;
}

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── Constructor validation ───────────────────────────────────────────────────

describe('ServerClient constructor', () => {
  it('throws when apiKey is missing', () => {
    expect(() => new ServerClient({ apiKey: '', baseUrl: 'https://api.example.com' })).toThrow(
      'apiKey is required'
    );
  });

  it('throws when baseUrl is missing', () => {
    expect(() => new ServerClient({ apiKey: 'key', baseUrl: '' })).toThrow('baseUrl is required');
  });

  it('creates client with valid config', () => {
    expect(() => new ServerClient(baseConfig)).not.toThrow();
  });

  it('strips trailing slash from baseUrl', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ServerClient({ ...baseConfig, baseUrl: 'https://api.example.com/' });
    await client.evaluateFeatureFlag('my-flag', user);
    expect(fetchMock.mock.calls[0][0]).toMatch(/^https:\/\/api\.example\.com\/api/);
    expect(fetchMock.mock.calls[0][0]).not.toMatch(/example\.com\/\/api/);
  });
});

// ─── evaluateFeatureFlag ──────────────────────────────────────────────────────

describe('ServerClient.evaluateFeatureFlag', () => {
  it('GETs /api/v1/feature-flags/evaluate/{key}?user_id=…&context=… with the API headers', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ServerClient(baseConfig);
    await client.evaluateFeatureFlag('my-flag', user);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      'https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-123&context=%7B%22country%22%3A%22DE%22%7D'
    );
    expect(init.method).toBe('GET');
    expect(init.headers).toEqual({ 'X-API-Key': 'test-api-key', 'Content-Type': 'application/json' });
  });

  it('sends no context when the user has no attributes', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ServerClient(baseConfig);
    await client.evaluateFeatureFlag('my-flag', { userId: 'user-123' });
    expect(fetchMock.mock.calls[0][0]).toBe(
      'https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-123'
    );
  });

  it('exposes the server reason on the evaluation', async () => {
    mockFetch({ key: 'my-flag', enabled: true, config: null, reason: 'targeting_rule' });
    const client = new ServerClient(baseConfig);
    const result = await client.evaluateFeatureFlag('my-flag', user);
    expect(result.reason).toBe('targeting_rule');
    expect(result.isEnabled).toBe(true);
  });

  it('returns an enabled evaluation with the full structure', async () => {
    mockFetch(flagOn);
    const client = new ServerClient(baseConfig);
    await expect(client.evaluateFeatureFlag('my-flag', user)).resolves.toEqual({
      flagKey: 'my-flag',
      variant: 'on',
      isEnabled: true,
      config: null,
      loading: false,
      error: null,
    });
  });

  it('uses config.variant as the variant when present', async () => {
    mockFetch(flagWithVariant);
    const client = new ServerClient(baseConfig);
    const result = await client.evaluateFeatureFlag('my-flag', user);
    expect(result.variant).toBe('treatment');
    expect(result.config).toEqual({ variant: 'treatment' });
  });

  it('returns a disabled evaluation when the flag is disabled', async () => {
    mockFetch(flagOff);
    const client = new ServerClient(baseConfig);
    const result = await client.evaluateFeatureFlag('disabled-flag', user);
    expect(result.flagKey).toBe('disabled-flag');
    expect(result.isEnabled).toBe(false);
    expect(result.variant).toBeNull();
    expect(result.error).toBeNull();
  });

  it('returns an error evaluation (does not throw) on a non-2xx response', async () => {
    mockFetch({}, 500);
    const client = new ServerClient(baseConfig);
    const result = await client.evaluateFeatureFlag('my-flag', user);
    expect(result).toMatchObject({
      flagKey: 'my-flag',
      variant: null,
      isEnabled: false,
      config: null,
      loading: false,
    });
    expect(result.error).toBeInstanceOf(Error);
    expect(result.error?.message).toBe('API error: 500');
  });

  it('returns an error evaluation (does not throw) on network failure', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network error'));
    const client = new ServerClient(baseConfig);
    const result = await client.evaluateFeatureFlag('my-flag', user);
    expect(result.isEnabled).toBe(false);
    expect(result.variant).toBeNull();
    expect(result.error?.message).toBe('Network error');
  });

  it('wraps non-Error rejections in an Error', async () => {
    global.fetch = jest.fn().mockRejectedValue('boom');
    const client = new ServerClient(baseConfig);
    const result = await client.evaluateFeatureFlag('my-flag', user);
    expect(result.error).toBeInstanceOf(Error);
    expect(result.error?.message).toBe('boom');
  });

  it('caches successful evaluations (fetch once for repeated calls)', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ServerClient(baseConfig);
    await client.evaluateFeatureFlag('my-flag', user);
    await client.evaluateFeatureFlag('my-flag', user);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not cache failures — a later call re-fetches and succeeds', async () => {
    const fetchMock = mockFetchSequence({ body: {}, status: 500 }, { body: flagOn });
    const client = new ServerClient(baseConfig);

    const first = await client.evaluateFeatureFlag('my-flag', user);
    const second = await client.evaluateFeatureFlag('my-flag', user);

    expect(first.error).toBeInstanceOf(Error);
    expect(second.error).toBeNull();
    expect(second.isEnabled).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('re-fetches after the cache TTL has expired', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const fetchMock = mockFetch(flagOn);
    const client = new ServerClient({ ...baseConfig, cacheTtlMs: 1_000 });

    await client.evaluateFeatureFlag('my-flag', user);
    now.mockReturnValue(1_001_001);
    await client.evaluateFeatureFlag('my-flag', user);

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ─── assignExperiment ─────────────────────────────────────────────────────────

describe('ServerClient.assignExperiment', () => {
  it('POSTs {experiment_key, user_id, context} to /api/v1/tracking/assign', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ServerClient(baseConfig);
    await client.assignExperiment('hero', user);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://api.example.com/api/v1/tracking/assign');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ 'X-API-Key': 'test-api-key', 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body)).toEqual({
      experiment_key: 'hero',
      user_id: 'user-123',
      context: { country: 'DE' },
    });
  });

  it('returns the mapped assignment on success', async () => {
    mockFetch(assignment);
    const client = new ServerClient(baseConfig);
    await expect(client.assignExperiment('hero', user)).resolves.toEqual({
      experimentKey: 'hero',
      variantKey: 'video_hero',
      variantName: 'video_hero',
      variantId: 'var-9',
      isControl: false,
      configuration: { media: 'video' },
      loading: false,
      error: null,
      assigned: true,
    });
  });

  it('returns the control defaults with an error (does not throw) on 404', async () => {
    mockFetch({ detail: 'not found' }, 404);
    const client = new ServerClient(baseConfig);
    const result = await client.assignExperiment('missing', user);
    expect(result).toMatchObject({ experimentKey: 'missing', ...controlDefaults });
    expect(result.error?.message).toBe('API error: 404');
  });

  it('returns the control defaults with an error (does not throw) on network failure', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network error'));
    const client = new ServerClient(baseConfig);
    const result = await client.assignExperiment('hero', user);
    expect(result).toMatchObject({ experimentKey: 'hero', ...controlDefaults });
    expect(result.error?.message).toBe('Network error');
  });

  it('caches successful assignments', async () => {
    const fetchMock = mockFetch(assignment);
    const client = new ServerClient(baseConfig);
    await client.assignExperiment('hero', user);
    await client.assignExperiment('hero', user);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not cache failed assignments', async () => {
    const fetchMock = mockFetchSequence({ body: {}, status: 404 }, { body: assignment });
    const client = new ServerClient(baseConfig);
    await client.assignExperiment('hero', user);
    const second = await client.assignExperiment('hero', user);
    expect(second.variantKey).toBe('video_hero');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ─── getAll ───────────────────────────────────────────────────────────────────

describe('ServerClient.getAll', () => {
  it('returns empty object for empty flagKeys array', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ServerClient(baseConfig);
    await expect(client.getAll([], user)).resolves.toEqual({});
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('evaluates multiple flags and returns map keyed by flagKey', async () => {
    mockFetchSequence({ body: flagOn }, { body: flagOff });
    const client = new ServerClient(baseConfig);
    const result = await client.getAll(['my-flag', 'disabled-flag'], user);

    expect(Object.keys(result)).toEqual(['my-flag', 'disabled-flag']);
    expect(result['my-flag'].isEnabled).toBe(true);
    expect(result['disabled-flag'].isEnabled).toBe(false);
  });

  it('evaluates flags in parallel (calls fetch for each key)', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ServerClient(baseConfig);
    await client.getAll(['flag-a', 'flag-b', 'flag-c'], user);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const context = '&context=%7B%22country%22%3A%22DE%22%7D';
    expect(fetchMock.mock.calls.map(c => c[0])).toEqual([
      `https://api.example.com/api/v1/feature-flags/evaluate/flag-a?user_id=user-123${context}`,
      `https://api.example.com/api/v1/feature-flags/evaluate/flag-b?user_id=user-123${context}`,
      `https://api.example.com/api/v1/feature-flags/evaluate/flag-c?user_id=user-123${context}`,
    ]);
  });

  it('returns all keys even when some fail', async () => {
    mockFetchSequence({ body: flagOn }, new Error('Network error'));
    const client = new ServerClient(baseConfig);
    const result = await client.getAll(['my-flag', 'failing-flag'], user);

    expect(result['my-flag'].isEnabled).toBe(true);
    expect(result['failing-flag'].isEnabled).toBe(false);
    expect(result['failing-flag'].error).toBeInstanceOf(Error);
  });
});

// ─── No browser APIs / cache isolation ───────────────────────────────────────

describe('ServerClient — no browser-specific API usage', () => {
  it('does not reference document', async () => {
    const originalDocument = global.document;
    // @ts-expect-error intentionally removing document
    delete global.document;

    mockFetch(flagOn);
    const client = new ServerClient(baseConfig);
    const result = await client.evaluateFeatureFlag('my-flag', user);
    expect(result.flagKey).toBe('my-flag');

    global.document = originalDocument;
  });

  it('does not reference window.localStorage', async () => {
    mockFetch(flagOn);
    const client = new ServerClient(baseConfig);
    const result = await client.evaluateFeatureFlag('my-flag', user);
    expect(result).toBeDefined();
  });

  it('has separate per-instance cache (not shared between instances)', async () => {
    const fetchMock = mockFetch(flagOn);
    const client1 = new ServerClient(baseConfig);
    const client2 = new ServerClient(baseConfig);

    await client1.evaluateFeatureFlag('my-flag', user);
    await client2.evaluateFeatureFlag('my-flag', user);

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('clearCache forces a fresh fetch', async () => {
    const fetchMock = mockFetch(flagOn);
    const client = new ServerClient(baseConfig);
    await client.evaluateFeatureFlag('my-flag', user);
    client.clearCache();
    await client.evaluateFeatureFlag('my-flag', user);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('handles timeout configuration', () => {
    expect(() => new ServerClient({ ...baseConfig, timeoutMs: 1000 })).not.toThrow();
  });
});

describe('the package root', () => {
  it('exports ServerClient, so Node and SSR code import it from @getexperimently/react-sdk', () => {
    expect(packageRoot.ServerClient).toBe(ServerClient);
  });
});
