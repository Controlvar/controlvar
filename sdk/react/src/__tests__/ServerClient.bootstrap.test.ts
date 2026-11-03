/**
 * `ServerClient.bootstrap(keys, user)` (#1518): the existing evaluate and assign
 * routes, in parallel, each body as the API sent it keyed by the key asked for.
 * It never throws, leaves out a key whose request fails, and reads and fills no
 * cache.
 */
import { ServerClient } from '../client/ServerClient';
import type {
  ExperimentAssignResponse,
  ExperimentationBootstrap,
  FeatureFlagEvaluateResponse,
  SdkConfig,
  UserContext,
} from '../client/types';
import type { ExperimentationBootstrap as IndexBootstrap } from '../index';
import type { ExperimentationBootstrap as SsrBootstrap } from '../ssr';

const BASE = 'https://api.example.com';
const config: SdkConfig = { apiKey: 'test-key', baseUrl: BASE };
const user: UserContext = { userId: 'u 1', attributes: { plan: 'pro' } };

const flagOn: FeatureFlagEvaluateResponse = { key: 'flag-1', enabled: true, config: null, reason: 'rollout' };
const flagOff: FeatureFlagEvaluateResponse = { key: 'flag-2', enabled: false, config: null, reason: 'inactive' };
const enrolled: ExperimentAssignResponse = {
  experiment_key: 'exp-1',
  user_id: 'u 1',
  variant_id: 'v-2',
  variant_name: 'treatment',
  is_control: false,
  configuration: { color: 'green' },
  assigned: true,
  reason: 'assigned',
};

type Reply = { status?: number; body?: unknown; raw?: () => Promise<unknown>; throws?: Error; hang?: boolean };

/** Each request answers what `route(url, body)` gives; every request is recorded. */
function mockFetch(route: (url: string, body: Record<string, unknown> | null) => Reply): jest.Mock {
  const mock = jest.fn().mockImplementation((url: string, init: { body?: string; signal?: AbortSignal }) => {
    const reply = route(url, init?.body ? JSON.parse(init.body) : null);
    if (reply.throws) return Promise.reject(reply.throws);
    if (reply.hang) {
      return new Promise((_resolve, reject) =>
        init.signal?.addEventListener('abort', () => reject(new Error('The operation was aborted.')))
      );
    }
    const status = reply.status ?? 200;
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: reply.raw ?? (() => Promise.resolve(JSON.parse(JSON.stringify(reply.body ?? {})))),
    });
  });
  global.fetch = mock;
  return mock;
}

/** The API of these tests: flag-1 on, flag-2 off, exp-1 enrolled. */
function healthyApi(): jest.Mock {
  return mockFetch((url, body) => {
    if (url.includes('/evaluate/flag-1?')) return { body: flagOn };
    if (url.includes('/evaluate/flag-2?')) return { body: flagOff };
    if (url.endsWith('/tracking/assign') && body?.experiment_key === 'exp-1') return { body: enrolled };
    return { status: 404, body: { detail: 'Not Found' } };
  });
}

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
});

describe('ServerClient.bootstrap()', () => {
  it('returns each body as the API sent it, keyed by the key asked for, in the order asked for', async () => {
    healthyApi();
    const built = await new ServerClient(config).bootstrap(
      { flags: ['flag-2', 'flag-1'], experiments: ['exp-1'] },
      user
    );
    expect(built).toEqual({
      userId: 'u 1',
      flags: { 'flag-2': flagOff, 'flag-1': flagOn },
      experiments: { 'exp-1': enrolled },
    });
    expect(Object.keys(built.flags ?? {})).toEqual(['flag-2', 'flag-1']);
  });

  it('calls the existing evaluate and assign routes, with the user attributes as context', async () => {
    const mock = healthyApi();
    await new ServerClient(config).bootstrap({ flags: ['flag-1'], experiments: ['exp-1'] }, user);
    const context = encodeURIComponent(JSON.stringify({ plan: 'pro' }));
    expect(mock.mock.calls.map(([url, init]) => [url, init.method, init.body ?? null])).toEqual([
      [`${BASE}/api/v1/feature-flags/evaluate/flag-1?user_id=u%201&context=${context}`, 'GET', null],
      [
        `${BASE}/api/v1/tracking/assign`,
        'POST',
        JSON.stringify({ experiment_key: 'exp-1', user_id: 'u 1', context: { plan: 'pro' } }),
      ],
    ]);
    expect(mock.mock.calls[0][1].headers).toEqual({ 'X-API-Key': 'test-key', 'Content-Type': 'application/json' });
  });

  it('starts every request before any answers: in parallel', async () => {
    const pending: Array<() => void> = [];
    const mock = jest.fn().mockImplementation(
      (url: string) =>
        new Promise(resolve =>
          pending.push(() =>
            resolve({
              ok: true,
              status: 200,
              json: () => Promise.resolve(url.endsWith('/assign') ? enrolled : flagOn),
            })
          )
        )
    );
    global.fetch = mock;
    const done = new ServerClient(config).bootstrap({ flags: ['flag-1', 'flag-2'], experiments: ['exp-1'] }, user);
    await Promise.resolve();
    expect(mock).toHaveBeenCalledTimes(3);
    pending.forEach(release => release());
    expect(Object.keys((await done).flags ?? {})).toEqual(['flag-1', 'flag-2']);
  });

  it.each([
    ['a 404', { status: 404, body: { detail: 'Not Found' } }],
    ['a 429', { status: 429, body: { detail: 'Too Many Requests' } }],
    ['a 500', { status: 500, body: {} }],
    ['a network error', { throws: new TypeError('fetch failed') }],
    ['a body that is not JSON', { raw: () => Promise.reject(new SyntaxError('Unexpected token <')) }],
    ['a body that is not a response of its route', { body: { detail: 'something else' } }],
  ] as Array<[string, Reply]>)('leaves out a key whose request fails (%s), and keeps the others', async (_label, reply) => {
    mockFetch((url, body) => {
      if (url.includes('/evaluate/flag-1?') || body?.experiment_key === 'exp-1') return reply;
      if (url.includes('/evaluate/flag-2?')) return { body: flagOff };
      return { body: { ...enrolled, experiment_key: 'exp-2' } };
    });
    const built = await new ServerClient(config).bootstrap(
      { flags: ['flag-1', 'flag-2'], experiments: ['exp-1', 'exp-2'] },
      user
    );
    expect(Object.keys(built.flags ?? {})).toEqual(['flag-2']);
    expect(Object.keys(built.experiments ?? {})).toEqual(['exp-2']);
  });

  it('leaves out a key whose request times out', async () => {
    mockFetch(url => (url.includes('flag-1') ? { hang: true } : { body: flagOff }));
    const built = await new ServerClient({ ...config, timeoutMs: 20 }).bootstrap(
      { flags: ['flag-1', 'flag-2'] },
      user
    );
    expect(Object.keys(built.flags ?? {})).toEqual(['flag-2']);
  });

  it('never throws, even when fetch itself throws', async () => {
    global.fetch = jest.fn().mockImplementation(() => {
      throw new Error('fetch is broken');
    });
    await expect(
      new ServerClient(config).bootstrap({ flags: ['flag-1'], experiments: ['exp-1'] }, user)
    ).resolves.toEqual({ userId: 'u 1', flags: {}, experiments: {} });
  });

  it('never throws for keys it cannot use: asks once per distinct string key', async () => {
    const mock = healthyApi();
    const server = new ServerClient(config);
    const odd = { flags: ['flag-1', 'flag-1', 7, null], experiments: 'exp-1' } as unknown as { flags: string[] };
    await expect(server.bootstrap(odd, user)).resolves.toEqual({
      userId: 'u 1',
      flags: { 'flag-1': flagOn },
      experiments: {},
    });
    expect(mock).toHaveBeenCalledTimes(1);
    await expect(server.bootstrap(undefined as unknown as { flags: string[] }, user)).resolves.toEqual({
      userId: 'u 1',
      flags: {},
      experiments: {},
    });
    await expect(
      server.bootstrap({ flags: ['flag-1'] }, undefined as unknown as UserContext)
    ).resolves.toEqual({ userId: '', flags: {}, experiments: {} });
  });

  it('reads no cache: a key the client answered before is asked again', async () => {
    const mock = healthyApi();
    const server = new ServerClient(config);
    await server.evaluateFeatureFlag('flag-1', user);
    await server.assignExperiment('exp-1', user);
    expect(mock).toHaveBeenCalledTimes(2);
    await server.bootstrap({ flags: ['flag-1'], experiments: ['exp-1'] }, user);
    expect(mock).toHaveBeenCalledTimes(4);
  });

  it('fills no cache: the client asks again for a key bootstrap() answered', async () => {
    const mock = healthyApi();
    const server = new ServerClient(config);
    await server.bootstrap({ flags: ['flag-1'], experiments: ['exp-1'] }, user);
    expect(mock).toHaveBeenCalledTimes(2);
    await server.evaluateFeatureFlag('flag-1', user);
    await server.assignExperiment('exp-1', user);
    expect(mock).toHaveBeenCalledTimes(4);
  });

  it('returns plain JSON, safe to pass as a prop', async () => {
    healthyApi();
    const built = await new ServerClient(config).bootstrap({ flags: ['flag-1'], experiments: ['exp-1'] }, user);
    expect(JSON.parse(JSON.stringify(built))).toEqual(built);
    expect(Object.getPrototypeOf(built.flags)).toBe(Object.prototype);
  });

  it('keeps a key named __proto__ as an entry, never as a prototype', async () => {
    mockFetch(() => ({ body: { key: '__proto__', enabled: true, config: null } }));
    const built = await new ServerClient(config).bootstrap({ flags: ['__proto__'] }, user);
    expect(Object.keys(built.flags ?? {})).toEqual(['__proto__']);
    expect(Object.getPrototypeOf(built.flags)).toBe(Object.prototype);
  });

  it('takes the keys first and the user second, as the other ServerClient methods do', () => {
    type Params = Parameters<ServerClient['bootstrap']>;
    const keys = { flags: ['flag-1'], experiments: ['exp-1'] };
    const ok: Params = [keys, user];
    // @ts-expect-error the user does not come first
    const swapped: Params = [user, keys];
    expect([ok, swapped]).toHaveLength(2);
  });

  it('is exported as a type from the package and from ssr', () => {
    const fromIndex: IndexBootstrap = { userId: 'u 1' };
    const fromSsr: SsrBootstrap = fromIndex;
    const own: ExperimentationBootstrap = fromSsr;
    expect(own).toEqual({ userId: 'u 1' });
  });
});
