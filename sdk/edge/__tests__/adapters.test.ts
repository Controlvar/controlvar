/**
 * Adapter-specific tests for Cloudflare Workers, Vercel Edge, and Deno Deploy.
 *
 * Tests run in Node.js (Jest) by mocking the platform-specific APIs
 * (KVNamespace, ExecutionContext, Deno KV). `fetch` is mocked — no network.
 */

import { CloudflareExperimentationClient, CloudflareKvStore, withExperimentation } from '../src/adapters/cloudflare';
import type { KVNamespace, ExecutionContext } from '../src/adapters/cloudflare';
import { createEdgeMiddleware, extractUserId, evaluateFlagsForRequest } from '../src/adapters/vercel';
import { DenoExperimentationClient, DenoKvStore, createDenoHandler } from '../src/adapters/deno';
import type { DenoKv } from '../src/adapters/deno';
import { EdgeExperimentationClient } from '../src/client';
import type { Assignment, FlagEvaluation } from '../src/types';

// ---------------------------------------------------------------------------
// Global fetch mock
// ---------------------------------------------------------------------------

const mockFetch = jest.fn();
global.fetch = mockFetch;

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

function flagBody(key: string, enabled = true): Record<string, unknown> {
  return { key, enabled, config: null };
}

function assignBody(experimentKey: string, variant = 'treatment'): Record<string, unknown> {
  return { experiment_key: experimentKey, user_id: 'u', variant_id: 'v', variant_name: variant, is_control: variant === 'control', configuration: null };
}

beforeEach(() => {
  mockFetch.mockReset();
});

// ---------------------------------------------------------------------------
// Cloudflare adapter
// ---------------------------------------------------------------------------

describe('CloudflareKvStore', () => {
  function makeKv(stored: Record<string, string> = {}) {
    const store = { ...stored };
    const puts: Array<{ key: string; value: string; options?: { expirationTtl?: number } }> = [];
    const kv: KVNamespace = {
      async get(key: string) {
        return store[key] ?? null;
      },
      async put(key: string, value: string, options?: { expirationTtl?: number }) {
        store[key] = value;
        puts.push({ key, value, options });
      },
    };
    return { kv, store, puts };
  }

  test('namespaces keys with "ep:" and rounds the TTL up to the 60 s minimum', async () => {
    const { kv, puts } = makeKv({ 'ep:a': 'x' });
    const store = new CloudflareKvStore(kv);
    expect(await store.get('a')).toBe('x');
    expect(await store.get('missing')).toBeNull();
    await store.put('b', 'y', 15_000);
    await store.put('c', 'z', 120_000);
    expect(puts[0]).toEqual({ key: 'ep:b', value: 'y', options: { expirationTtl: 60 } });
    expect(puts[1].options).toEqual({ expirationTtl: 120 });
  });

  test('an explicit kvTtlSeconds wins over the cache TTL', async () => {
    const { kv, puts } = makeKv();
    await new CloudflareKvStore(kv, 300).put('k', 'v', 1_000);
    expect(puts[0].options).toEqual({ expirationTtl: 300 });
  });
});

describe('CloudflareExperimentationClient', () => {
  function makeKv(stored: Record<string, string> = {}) {
    const store = { ...stored };
    const kv: KVNamespace = {
      async get(key: string) {
        return store[key] ?? null;
      },
      async put(key: string, value: string) {
        store[key] = value;
      },
    };
    return { kv, store };
  }

  test('serves a flag cached in KV per user + key without calling the API', async () => {
    const cached: FlagEvaluation = { key: 'kv-flag', enabled: true, config: { variant: 'b' } };
    const { kv } = makeKv({ 'ep:flag:user-1:kv-flag': JSON.stringify(cached) });
    const client = new CloudflareExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test', kvNamespace: kv });

    expect(await client.evaluateFlag('kv-flag', 'user-1')).toEqual(cached);
    expect(mockFetch).not.toHaveBeenCalled();
    expect(client.evaluateFlagSync('kv-flag', 'user-1')).toBe(true);
  });

  test('another user is not served from a different user\'s KV entry', async () => {
    const { kv } = makeKv({ 'ep:flag:user-1:f': JSON.stringify({ key: 'f', enabled: true, config: null }) });
    mockFetch.mockResolvedValueOnce(jsonResponse(flagBody('f', false)));
    const client = new CloudflareExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test', kvNamespace: kv });
    expect((await client.evaluateFlag('f', 'user-2')).enabled).toBe(false);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('falls back to the API on a KV miss and writes the result to KV', async () => {
    const { kv, store } = makeKv();
    mockFetch.mockResolvedValueOnce(jsonResponse(flagBody('api-flag')));
    mockFetch.mockResolvedValueOnce(jsonResponse(assignBody('exp')));
    const client = new CloudflareExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test', kvNamespace: kv });

    await client.evaluateFlag('api-flag', 'user-1');
    await client.getAssignment('exp', 'user-1');

    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(mockFetch.mock.calls[0][0]).toBe('http://api.test/api/v1/feature-flags/evaluate/api-flag?user_id=user-1');
    expect(mockFetch.mock.calls[1][0]).toBe('http://api.test/api/v1/tracking/assign');
    expect(JSON.parse(store['ep:flag:user-1:api-flag'])).toEqual({ key: 'api-flag', enabled: true, config: null });
    expect((JSON.parse(store['ep:assign:user-1:exp']) as Assignment).variantName).toBe('treatment');
  });

  test('falls back to the API when KV has corrupt data', async () => {
    const { kv } = makeKv({ 'ep:flag:user-1:f': 'not-valid-json' });
    mockFetch.mockResolvedValueOnce(jsonResponse(flagBody('f')));
    const client = new CloudflareExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test', kvNamespace: kv });
    expect((await client.evaluateFlag('f', 'user-1')).enabled).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  test('works without a KV namespace (API only)', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(flagBody('no-kv-flag')));
    const client = new CloudflareExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test' });
    expect((await client.evaluateFlag('no-kv-flag', 'user-1')).enabled).toBe(true);
  });

  test('loadFromKvOrApi / refreshAndStore are deprecated no-ops', async () => {
    const client = new CloudflareExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test' });
    await expect(client.loadFromKvOrApi()).resolves.toBeUndefined();
    await expect(client.refreshAndStore()).resolves.toBeUndefined();
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe('withExperimentation', () => {
  function makeCtx(): ExecutionContext {
    return { waitUntil: jest.fn(), passThroughOnException: jest.fn() };
  }

  test('injects EP_CLIENT into env without any startup request', async () => {
    let capturedClient: unknown;
    const handler = jest.fn(async (_req: Request, env: Record<string, unknown>) => {
      capturedClient = env['EP_CLIENT'];
      return new Response('ok');
    });

    const wrapped = withExperimentation(handler, { apiKey: 'k', baseUrl: 'http://api.test' });
    await wrapped(new Request('http://localhost/'), {}, makeCtx());

    expect(capturedClient).toBeInstanceOf(CloudflareExperimentationClient);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  test('the injected client talks to the contract endpoints', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(flagBody('f')));
    const wrapped = withExperimentation(
      async (_req, env) => {
        const client = env['EP_CLIENT'] as CloudflareExperimentationClient;
        const { enabled } = await client.evaluateFlag('f', 'user-1');
        return new Response(String(enabled));
      },
      { apiKey: 'k', baseUrl: 'http://api.test' },
    );
    const response = await wrapped(new Request('http://localhost/'), {}, makeCtx());
    expect(await response.text()).toBe('true');
    expect(mockFetch.mock.calls[0][0]).toBe('http://api.test/api/v1/feature-flags/evaluate/f?user_id=user-1');
  });

  test('uses env.KV as the store when no kvNamespace is configured', async () => {
    const stored: Record<string, string> = {
      'ep:flag:user-1:f': JSON.stringify({ key: 'f', enabled: true, config: null }),
    };
    const kv: KVNamespace = {
      async get(key: string) {
        return stored[key] ?? null;
      },
      async put() {},
    };
    const wrapped = withExperimentation(
      async (_req, env) => {
        const client = env['EP_CLIENT'] as CloudflareExperimentationClient;
        return new Response(String((await client.evaluateFlag('f', 'user-1')).enabled));
      },
      { apiKey: 'k', baseUrl: 'http://api.test' },
    );
    const response = await wrapped(new Request('http://localhost/'), { KV: kv }, makeCtx());
    expect(await response.text()).toBe('true');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  test('custom clientEnvKey is used and original env properties pass through', async () => {
    let capturedEnv: Record<string, unknown> = {};
    const handler = jest.fn(async (_req: Request, env: Record<string, unknown>) => {
      capturedEnv = env;
      return new Response('ok');
    });
    const wrapped = withExperimentation(handler, { apiKey: 'k', baseUrl: 'http://api.test' }, 'MY_CLIENT');
    await wrapped(new Request('http://localhost/'), { DB: 'my-db-binding', SECRET: 'shhh' }, makeCtx());

    expect(capturedEnv['MY_CLIENT']).toBeInstanceOf(CloudflareExperimentationClient);
    expect(capturedEnv['DB']).toBe('my-db-binding');
    expect(capturedEnv['SECRET']).toBe('shhh');
  });
});

// ---------------------------------------------------------------------------
// Vercel Edge adapter
// ---------------------------------------------------------------------------

describe('createEdgeMiddleware', () => {
  test('evaluates each flagKeys entry on the server and injects X-EP-Flag-* headers', async () => {
    mockFetch.mockImplementation(async (url: unknown) => {
      if (typeof url !== 'string') return new Response('upstream ok'); // pass-through
      if (url.includes('/evaluate/dark-mode')) return jsonResponse(flagBody('dark-mode', true));
      if (url.includes('/evaluate/checkout-v2')) return jsonResponse(flagBody('checkout-v2', false));
      return jsonResponse({}, 404);
    });

    const middleware = createEdgeMiddleware({
      apiKey: 'k',
      baseUrl: 'http://api.test',
      flagKeys: ['dark-mode', 'checkout-v2'],
    });
    const response = await middleware(new Request('http://localhost/page', { headers: { 'X-User-Id': 'user-9' } }));
    expect(response).toBeDefined();

    const evaluateUrls = mockFetch.mock.calls.map((c) => c[0]).filter((u) => typeof u === 'string');
    expect(evaluateUrls).toEqual(
      expect.arrayContaining([
        'http://api.test/api/v1/feature-flags/evaluate/dark-mode?user_id=user-9',
        'http://api.test/api/v1/feature-flags/evaluate/checkout-v2?user_id=user-9',
      ]),
    );
    const passedReq = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as Request;
    expect(passedReq.headers.get('X-EP-Flag-dark-mode')).toBe('true');
    expect(passedReq.headers.get('X-EP-Flag-checkout-v2')).toBe('false');
    expect(passedReq.headers.get('X-EP-User-Id')).toBe('user-9');
  });

  test('without flagKeys injects every flag from GET /api/v1/feature-flags/user/{user_id}', async () => {
    mockFetch.mockImplementation(async (url: unknown) => {
      if (typeof url === 'string' && url.endsWith('/api/v1/feature-flags/user/user-9')) return jsonResponse({ a: true, b: false });
      return new Response('ok');
    });
    const middleware = createEdgeMiddleware({ apiKey: 'k', baseUrl: 'http://api.test' });
    await middleware(new Request('http://localhost/', { headers: { 'X-User-Id': 'user-9' } }));
    const passedReq = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as Request;
    expect(passedReq.headers.get('X-EP-Flag-a')).toBe('true');
    expect(passedReq.headers.get('X-EP-Flag-b')).toBe('false');
  });

  test('extracts userId from the cookie when the header is absent', async () => {
    mockFetch.mockImplementation(async (url: unknown) =>
      typeof url === 'string' ? jsonResponse(flagBody('cookie-flag')) : new Response('ok'),
    );
    const middleware = createEdgeMiddleware({ apiKey: 'k', baseUrl: 'http://api.test', flagKeys: ['cookie-flag'] });
    await middleware(new Request('http://localhost/', { headers: { Cookie: 'ep_user_id=user-from-cookie; other=val' } }));
    const passedReq = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as Request;
    expect(passedReq.headers.get('X-EP-User-Id')).toBe('user-from-cookie');
    expect(mockFetch.mock.calls[0][0]).toContain('user_id=user-from-cookie');
  });

  test('anonymous requests (no user id) evaluate nothing and report flags as false', async () => {
    mockFetch.mockResolvedValueOnce(new Response('ok'));
    const middleware = createEdgeMiddleware({ apiKey: 'k', baseUrl: 'http://api.test', flagKeys: ['some-flag'] });
    await middleware(new Request('http://localhost/'));
    expect(mockFetch).toHaveBeenCalledTimes(1); // only the pass-through
    const passedReq = mockFetch.mock.calls[0][0] as Request;
    expect(passedReq.headers.get('X-EP-Flag-some-flag')).toBe('false');
    expect(passedReq.headers.get('X-EP-User-Id')).toBeNull();
  });

  test('continues without throwing when evaluation fails', async () => {
    mockFetch.mockRejectedValueOnce(new Error('api down'));
    mockFetch.mockResolvedValueOnce(new Response('ok'));
    const middleware = createEdgeMiddleware({ apiKey: 'k', baseUrl: 'http://api.test', flagKeys: ['some-flag'] });
    const request = new Request('http://localhost/', { headers: { 'X-User-Id': 'u' } });
    await expect(middleware(request)).resolves.toBeDefined();
    const passedReq = mockFetch.mock.calls[1][0] as Request;
    expect(passedReq.headers.get('X-EP-Flag-some-flag')).toBe('false');
  });

  test('uses custom userIdHeaderName', () => {
    const request = new Request('http://localhost/', { headers: { 'X-Custom-User': 'custom-user-id' } });
    expect(extractUserId(request, { apiKey: 'k', baseUrl: 'http://api.test', userIdHeaderName: 'X-Custom-User' })).toBe('custom-user-id');
  });

  test('evaluateFlagsForRequest with an explicitly empty list evaluates nothing', async () => {
    const client = new EdgeExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test' });
    expect(await evaluateFlagsForRequest(client, 'u', [])).toEqual({});
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Deno Deploy adapter
// ---------------------------------------------------------------------------

describe('DenoKvStore', () => {
  function makeDenoKv() {
    const store: Record<string, unknown> = {};
    const sets: Array<{ key: string[]; options?: { expireIn?: number } }> = [];
    const kv: DenoKv = {
      async get<T>(key: string[]) {
        return { value: (store[JSON.stringify(key)] ?? null) as T | null };
      },
      async set(key: string[], value: unknown, options?: { expireIn?: number }) {
        store[JSON.stringify(key)] = value;
        sets.push({ key, options });
      },
    };
    return { kv, store, sets };
  }

  test('namespaces keys under ["ep", key] and passes expireIn in ms', async () => {
    const { kv, sets } = makeDenoKv();
    const store = new DenoKvStore(kv);
    await store.put('flag:u:f', '{"x":1}', 30_000);
    expect(sets[0]).toEqual({ key: ['ep', 'flag:u:f'], options: { expireIn: 30_000 } });
    expect(await store.get('flag:u:f')).toBe('{"x":1}');
    expect(await store.get('missing')).toBeNull();
  });

  test('an explicit kvTtlMs wins over the cache TTL', async () => {
    const { kv, sets } = makeDenoKv();
    await new DenoKvStore(kv, 300_000).put('k', 'v', 1_000);
    expect(sets[0].options).toEqual({ expireIn: 300_000 });
  });
});

describe('DenoExperimentationClient', () => {
  function makeDenoKv(stored: Record<string, unknown> = {}) {
    const store = { ...stored };
    const kv: DenoKv = {
      async get<T>(key: string[]) {
        return { value: (store[JSON.stringify(key)] ?? null) as T | null };
      },
      async set(key: string[], value: unknown) {
        store[JSON.stringify(key)] = value;
      },
    };
    return { kv, store };
  }

  test('serves an assignment cached in Deno KV per user + key without calling the API', async () => {
    const cached: Assignment = { experimentKey: 'exp', userId: 'user-1', variantId: 'v', variantName: 'control', isControl: true, configuration: null };
    const { kv } = makeDenoKv({ '["ep","assign:user-1:exp"]': JSON.stringify(cached) });
    const client = new DenoExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test', kv });
    expect(await client.getAssignment('exp', 'user-1')).toEqual(cached);
    expect(mockFetch).not.toHaveBeenCalled();
    expect(client.getAssignmentSync('exp', 'user-1')).toBe('control');
  });

  test('falls back to the API on a KV miss and writes the result to Deno KV', async () => {
    const { kv, store } = makeDenoKv();
    mockFetch.mockResolvedValueOnce(jsonResponse(flagBody('deno-api-flag')));
    const client = new DenoExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test', kv });
    expect((await client.evaluateFlag('deno-api-flag', 'user-1')).enabled).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(JSON.parse(store['["ep","flag:user-1:deno-api-flag"]'] as string)).toEqual({ key: 'deno-api-flag', enabled: true, config: null });
  });

  test('works without KV (API only) and the deprecated methods are no-ops', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(flagBody('deno-no-kv')));
    const client = new DenoExperimentationClient({ apiKey: 'k', baseUrl: 'http://api.test' });
    await expect(client.loadFromKvOrApi()).resolves.toBeUndefined();
    await expect(client.refreshAndStore()).resolves.toBeUndefined();
    expect((await client.evaluateFlag('deno-no-kv', 'user-1')).enabled).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});

describe('createDenoHandler', () => {
  test('creates a handler that passes the client to the inner function', async () => {
    let receivedClient: unknown;
    const handler = createDenoHandler(
      async (_req, client) => {
        receivedClient = client;
        return new Response('handled');
      },
      { apiKey: 'k', baseUrl: 'http://api.test' },
    );
    const response = await handler(new Request('http://localhost/'));
    expect(response.status).toBe(200);
    expect(receivedClient).toBeInstanceOf(DenoExperimentationClient);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  test('reuses one client (and its cache) across calls in the same isolate', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(flagBody('singleton-flag')));
    const handler = createDenoHandler(
      async (_req, client) => new Response(String((await client.evaluateFlag('singleton-flag', 'user-1')).enabled)),
      { apiKey: 'k', baseUrl: 'http://api.test' },
    );
    expect(await (await handler(new Request('http://localhost/'))).text()).toBe('true');
    expect(await (await handler(new Request('http://localhost/'))).text()).toBe('true');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});
