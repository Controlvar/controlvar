/**
 * `evaluation: 'local'` through the public client: the vectors end to end, the refresh failure
 * table, the evaluation-count flush, the browser refusal and the API surface. `fetch` is mocked
 * by URL; nothing here needs a server.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import { ExperimentationClient, BROWSER_LOCAL_EVALUATION_MESSAGE } from '../src/client';
import type { ExperimentationError } from '../src/errors';
import {
  DEFAULT_REFRESH_INTERVAL_MS,
  EVALUATIONS_PATH,
  FLUSH_INTERVAL_MS,
  MAX_BACKOFF_MS,
  MIN_SERVER_RELEASE,
  REFUSED_RETRY_MS,
  RULESET_PATH,
} from '../src/local';
import type { ClientConfig, SwallowedOperation } from '../src/types';
import { isWellFormedString } from '../src/evaluator';
import * as evaluatorModule from '../src/evaluator';

const VECTORS_PATH = join(__dirname, '..', '..', '..', 'tests', 'sdk-contract', 'ruleset-vectors.json');
const vectors = JSON.parse(readFileSync(VECTORS_PATH, 'utf8'));

interface Step {
  status?: number;
  body?: unknown;
  /** A body that is not JSON. */
  text?: string;
  headers?: Record<string, string>;
}

function response(step: Step) {
  const status = step.status ?? 200;
  const headers = step.headers ?? {};
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => headers[name] ?? headers[name.toLowerCase()] ?? null },
    json: () =>
      step.text !== undefined ? Promise.reject(new SyntaxError('Unexpected token <')) : Promise.resolve(step.body ?? {}),
  };
}

const RULESET = {
  schema: 1,
  version: 'v1',
  bucketing: 'md5-mod100-v1',
  flags: [
    { key: 'off', active: false },
    { key: 'all-on', active: true, evaluation: 'local', rollout_percentage: 100, rules: [], default_rule: null },
    {
      key: 'us-only',
      active: true,
      evaluation: 'local',
      rollout_percentage: 0,
      rules: [
        {
          id: 'us',
          rollout_percentage: 100,
          match: { op: 'and', conditions: [{ attribute: 'user.country', operator: 'eq', value: 'US' }], groups: [] },
        },
      ],
      default_rule: null,
    },
  ],
};
const RULESET_WITH_REMOTE = {
  ...RULESET,
  version: 'v2',
  flags: [...RULESET.flags, { key: 'semver', active: true, evaluation: 'remote' }],
};

/**
 * A fake server. Ruleset responses are consumed in order (the last one repeats); every request is
 * recorded.
 */
class FakeServer {
  rulesetSteps: Array<Step | Error> = [];
  evaluate: (url: string) => Step = () => ({ body: { key: 'x', enabled: true, config: null, reason: 'rollout' } });
  userFlags: (url: string) => Step = () => ({ body: {} });
  evaluationsStatus = 201;
  calls: Array<{ url: string; method: string; headers: Record<string, string>; body: unknown }> = [];
  readonly fetch: jest.Mock;

  constructor(...rulesets: Array<Step | Error>) {
    this.rulesetSteps = rulesets;
    this.fetch = jest.fn(async (url: string, init: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
      this.calls.push({ url, method: init?.method ?? 'GET', headers, body });
      if (url.includes(RULESET_PATH)) {
        const step = this.rulesetSteps.length > 1 ? this.rulesetSteps.shift()! : this.rulesetSteps[0];
        if (step instanceof Error) throw step;
        return response(step);
      }
      if (url.includes(EVALUATIONS_PATH)) return response({ status: this.evaluationsStatus, body: { accepted: 0, errors: [] } });
      if (url.includes('/tracking/errors')) return response({ status: 201, body: { id: 'err-1' } });
      if (url.includes('/feature-flags/evaluate/')) return response(this.evaluate(url));
      if (url.includes('/feature-flags/user/')) return response(this.userFlags(url));
      return response({ body: { success_count: 1, failure_count: 0, errors: [] } });
    });
  }

  requests(fragment: string) {
    return this.calls.filter(call => call.url.includes(fragment));
  }
}

const ok = (body: unknown = RULESET, etag = '"v1"'): Step => ({ body, headers: { ETag: etag } });

function localClient(server: FakeServer, extra: Partial<ClientConfig> = {}) {
  const errors: Array<{ error: ExperimentationError; operation: SwallowedOperation }> = [];
  const client = new ExperimentationClient({
    apiUrl: 'https://api.example.com',
    apiKey: 'scoped-key',
    fetch: server.fetch as unknown as typeof fetch,
    evaluation: 'local',
    onError: (error, operation) => errors.push({ error, operation }),
    ...extra,
  });
  return { client, errors };
}

const us = { userId: 'user-1', attributes: { country: 'US' } };
const fr = { userId: 'user-1', attributes: { country: 'FR' } };

let clients: ExperimentationClient[] = [];
function track(client: ExperimentationClient) {
  clients.push(client);
  return client;
}

beforeEach(() => {
  jest.useFakeTimers();
  jest.spyOn(Math, 'random').mockReturnValue(0.5); // no jitter unless a test sets it
});

afterEach(async () => {
  for (const client of clients) await client.close();
  clients = [];
  jest.useRealTimers();
  jest.restoreAllMocks();
});

// ─── The vectors, end to end ─────────────────────────────────────────────────

/** A context holding a number JSON cannot carry back (it parsed to Infinity). */
function hasNonFinite(value: unknown): boolean {
  if (typeof value === 'number') return !Number.isFinite(value);
  if (Array.isArray(value)) return value.some(hasNonFinite);
  if (value && typeof value === 'object') return Object.values(value).some(hasNonFinite);
  return false;
}

describe('the vectors through evaluateFlag', () => {
  test('local answers equal the server, and only non-must_local cases reach it', async () => {
    const server = new FakeServer(ok(vectors.ruleset, `"${vectors.ruleset.version}"`));
    const { client } = localClient(server);
    track(client);
    await expect(client.ready()).resolves.toEqual({ ok: true, rulesetVersion: vectors.ruleset.version });

    // 10**400 parses to Infinity here; a JS caller cannot send it (JSON.stringify sends null),
    // so those cases are covered by the evaluator test instead. Pinned so none drop out silently.
    const cases = vectors.cases.filter((c: { context: unknown }) => !hasNonFinite(c.context));
    expect(vectors.cases.length - cases.length).toBe(42);

    let expectedRemote = 0;
    const wrong: string[] = [];
    const unsendable: string[] = [];
    let localAnswers = 0;
    for (const testCase of cases) {
      server.evaluate = () => ({ body: { key: testCase.flag, enabled: testCase.expected.enabled, config: null, reason: testCase.expected.reason } });
      if (!testCase.must_local) expectedRemote += 1;
      const before = server.requests('/feature-flags/evaluate/').length;
      let result;
      try {
        result = await client.evaluateFlag(testCase.flag, {
          userId: testCase.user_id,
          attributes: testCase.context ?? undefined,
        });
      } catch (err) {
        // A lone surrogate in the user id cannot be put in a URL: local mode deferred, and the
        // server path fails before sending, exactly as it does in server mode.
        unsendable.push(testCase.id);
        if (testCase.must_local || !(err instanceof URIError)) wrong.push(`${testCase.id}: threw ${String(err)}`);
        expectedRemote -= 1;
        continue;
      }
      const remote = server.requests('/feature-flags/evaluate/').length - before;
      if (result.source === 'local') localAnswers += 1;
      if (result.enabled !== testCase.expected.enabled || result.reason !== testCase.expected.reason) {
        wrong.push(`${testCase.id}: got ${result.enabled}/${result.reason}`);
      }
      if (result.source !== (remote ? 'server' : 'local') || remote !== (testCase.must_local ? 0 : 1)) {
        wrong.push(`${testCase.id}: must_local=${testCase.must_local} but ${remote} server calls, source ${result.source}`);
      }
      client.clearCache();
    }
    expect(wrong).toEqual([]);
    expect(unsendable.length).toBeGreaterThan(0);
    const userIdOf = (id: string): string => vectors.cases.find((c: { id: string }) => c.id === id).user_id;
    expect(unsendable.filter(id => isWellFormedString(userIdOf(id)))).toEqual([]);
    expect(server.requests('/feature-flags/evaluate/')).toHaveLength(expectedRemote);
    // Every must_local case except the three whose context holds 10**400 (see above).
    expect(localAnswers).toBe(1294 - 3);
  });
});

// ─── Answers ─────────────────────────────────────────────────────────────────

describe('answers', () => {
  test('after ready, evaluations make no request and follow the attributes passed', async () => {
    const server = new FakeServer(ok());
    const { client } = localClient(server);
    track(client);
    await client.ready();
    const before = server.calls.length;
    for (let i = 0; i < 1000; i++) await client.isFeatureEnabled('all-on', { userId: `u${i}` });
    expect(await client.evaluateFlag('us-only', us)).toEqual({
      key: 'us-only',
      enabled: true,
      config: null,
      reason: 'targeting_rule',
      source: 'local',
    });
    // Different attributes, same user, no clearCache(): the answer follows the attributes.
    expect(await client.evaluateFlag('us-only', fr)).toMatchObject({ enabled: false, reason: 'rollout', source: 'local' });
    expect(server.calls.length).toBe(before);
  });

  test('an inactive flag is enabled: false, reason inactive', async () => {
    const server = new FakeServer(ok());
    const { client } = localClient(server);
    track(client);
    await client.ready();
    await expect(client.evaluateFlag('off', us)).resolves.toMatchObject({ enabled: false, reason: 'inactive', source: 'local' });
  });

  test('an unknown key and a remote flag are evaluated by the server', async () => {
    const server = new FakeServer(ok(RULESET_WITH_REMOTE, '"v2"'));
    const { client } = localClient(server);
    track(client);
    await client.ready();
    await expect(client.evaluateFlag('semver', us)).resolves.toMatchObject({ source: 'server' });
    await expect(client.evaluateFlag('brand-new', us)).resolves.toMatchObject({ source: 'server' });
    const urls = server.requests('/feature-flags/evaluate/').map(c => c.url);
    expect(urls).toHaveLength(2);
    expect(urls[0]).toContain('/feature-flags/evaluate/semver?user_id=user-1&context=');
    expect(client.status().serverEvaluatedFlags).toEqual(['semver']);
  });

  test('a local answer is recorded for the key-less track() fan-out', async () => {
    const server = new FakeServer(ok());
    const { client } = localClient(server);
    track(client);
    await client.ready();
    await client.isFeatureEnabled('us-only', us);
    await client.track('user-1', 'page_view');
    const batch = server.requests('/tracking/batch');
    expect(batch).toHaveLength(1);
    expect(batch[0].body).toEqual({
      events: [{ event_type: 'page_view', event_name: 'page_view', user_id: 'user-1', feature_flag_key: 'us-only' }],
    });
  });

  test('reportError sends the report with the same key in local mode, before and after close()', async () => {
    const server = new FakeServer(ok());
    const { client, errors } = localClient(server);
    track(client);
    await client.ready();
    const report = { errorType: 'crash', message: 'boom', featureFlagKey: 'us-only', userId: 'user-1' };
    await expect(client.reportError(report)).resolves.toBe(true);
    await client.close();
    await expect(client.reportError(report)).resolves.toBe(true);

    const sent = server.requests('/api/v1/tracking/errors');
    expect(sent).toHaveLength(2);
    for (const request of sent) {
      expect(request.method).toBe('POST');
      expect(request.headers['X-API-Key']).toBe('scoped-key');
      expect(request.body).toEqual({ error_type: 'crash', message: 'boom', feature_flag_key: 'us-only', user_id: 'user-1' });
    }
    expect(errors).toEqual([]);
  });

  test('getAllFlags: every active flag locally, inactive omitted', async () => {
    const server = new FakeServer(ok());
    const { client } = localClient(server);
    track(client);
    await client.ready();
    await expect(client.getAllFlags('user-1', { country: 'US' })).resolves.toEqual({ 'all-on': true, 'us-only': true });
    expect(server.requests('/feature-flags/user/')).toHaveLength(0);
  });

  test('getAllFlags: one remote flag sends the whole call to the server', async () => {
    const server = new FakeServer(ok(RULESET_WITH_REMOTE, '"v2"'));
    server.userFlags = () => ({ body: { 'all-on': true, 'us-only': false, semver: true } });
    const { client } = localClient(server);
    track(client);
    await client.ready();
    await expect(client.getAllFlags('user-1', { country: 'US' })).resolves.toEqual({
      'all-on': true,
      'us-only': false,
      semver: true,
    });
    expect(server.requests('/feature-flags/user/')).toHaveLength(1);
  });

  test('before the first ruleset every evaluation is made on the server', async () => {
    let release: (value: unknown) => void = () => undefined;
    const pending = new Promise(resolve => (release = resolve));
    const server = new FakeServer(ok());
    const original = server.fetch.getMockImplementation()!;
    server.fetch.mockImplementation(async (url: string, init: RequestInit) => {
      if (url.includes(RULESET_PATH)) await pending;
      return original(url, init);
    });
    const { client } = localClient(server);
    track(client);
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'server' });
    expect(client.status().ready).toBe(false);
    release(undefined);
    await client.ready();
    client.clearCache();
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'local' });
  });

  test('experiments are assigned by the server in local mode', async () => {
    const server = new FakeServer(ok());
    const { client } = localClient(server);
    track(client);
    await client.ready();
    const answer = {
      experiment_key: 'checkout', user_id: 'user-1', variant_id: 'v-2',
      variant_name: 'treatment', is_control: false, configuration: null,
    };
    const answerRulesetAndFlags = server.fetch.getMockImplementation()!;
    server.fetch.mockImplementation(async (url: string, init: RequestInit) =>
      url.includes('/tracking/assign')
        ? (server.calls.push({ url, method: init.method ?? 'GET', headers: {}, body: JSON.parse(init.body as string) }),
          response({ body: answer }))
        : answerRulesetAndFlags(url, init)
    );
    await expect(client.getAssignment('checkout', us)).resolves.toMatchObject({ variantName: 'treatment' });
    expect(server.requests('/api/v1/tracking/assign').map(c => [c.method, (c.body as { experiment_key: string }).experiment_key])).toEqual([
      ['POST', 'checkout'],
    ]);
  });

  test('server mode is unchanged: no ruleset request, no source field', async () => {
    const server = new FakeServer(ok());
    const client = track(
      new ExperimentationClient({ apiUrl: 'https://api.example.com', apiKey: 'k', fetch: server.fetch as unknown as typeof fetch })
    );
    await expect(client.ready()).resolves.toEqual({ ok: true, rulesetVersion: null });
    await expect(client.evaluateFlag('x', us)).resolves.toEqual({ key: 'x', enabled: true, config: null, reason: 'rollout' });
    expect(server.requests(RULESET_PATH)).toHaveLength(0);
    expect(client.status()).toMatchObject({ evaluation: 'server', ready: true, rulesetVersion: null });
  });
});

// ─── The refresh failure table ───────────────────────────────────────────────

describe('refresh failures', () => {
  test('5xx after a load keeps serving the last ruleset and reports each failure', async () => {
    const server = new FakeServer(ok(), { status: 503 }, { status: 503 });
    const { client, errors } = localClient(server);
    track(client);
    await client.ready();
    const loadedAt = client.status().lastRefreshAt;
    await jest.advanceTimersByTimeAsync(DEFAULT_REFRESH_INTERVAL_MS);
    await jest.advanceTimersByTimeAsync(DEFAULT_REFRESH_INTERVAL_MS);
    expect(errors.map(e => [e.operation, e.error.status])).toEqual([
      ['refresh', 503],
      ['refresh', 503],
    ]);
    expect(client.status()).toMatchObject({ ready: true, rulesetVersion: 'v1', lastRefreshAt: loadedAt });
    expect(client.status().lastError?.status).toBe(503);
    await expect(client.evaluateFlag('us-only', us)).resolves.toMatchObject({ source: 'local', enabled: true });
  });

  test('a network failure before the first load: ready() resolves ok: false, evaluations go to the server', async () => {
    const server = new FakeServer(new TypeError('fetch failed'));
    const { client, errors } = localClient(server);
    track(client);
    const result = await client.ready();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe('NETWORK_ERROR');
    expect(errors).toHaveLength(1);
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'server' });
  });

  test('backs off exponentially to 5 minutes', async () => {
    const server = new FakeServer({ status: 500 });
    const { client } = localClient(server);
    track(client);
    await client.ready();
    const gaps: number[] = [];
    let last = Date.now();
    for (let i = 0; i < 6; i++) {
      const count = server.requests(RULESET_PATH).length;
      while (server.requests(RULESET_PATH).length === count) await jest.advanceTimersByTimeAsync(1_000);
      gaps.push(Date.now() - last);
      last = Date.now();
    }
    expect(gaps).toEqual([30_000, 60_000, 120_000, 240_000, MAX_BACKOFF_MS, MAX_BACKOFF_MS]);
  });

  test.each([401, 403])('%i after a load discards the ruleset: every evaluation goes to the server', async status => {
    const server = new FakeServer(ok(), { status, body: { detail: 'nope' } });
    const { client, errors } = localClient(server);
    track(client);
    await client.ready();
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'local' });

    await jest.advanceTimersByTimeAsync(DEFAULT_REFRESH_INTERVAL_MS); // the key is revoked
    client.clearCache();
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'server' });
    expect(client.status()).toMatchObject({ ready: false, rulesetVersion: null });
    expect(errors).toHaveLength(1);
    expect(errors[0].error.message).toContain(`refused (${status})`);
    expect(errors[0].error.message).toContain('nope');

    // Retried every 10 minutes, reported once, and the retry sends no stale ETag.
    const count = server.requests(RULESET_PATH).length;
    await jest.advanceTimersByTimeAsync(REFUSED_RETRY_MS - 1);
    expect(server.requests(RULESET_PATH)).toHaveLength(count);
    await jest.advanceTimersByTimeAsync(1);
    expect(server.requests(RULESET_PATH)).toHaveLength(count + 1);
    expect(server.requests(RULESET_PATH).pop()!.headers['If-None-Match']).toBeUndefined();
    expect(errors).toHaveLength(1);
  });

  test('a scope granted later is picked up by the 10-minute retry', async () => {
    const server = new FakeServer({ status: 403, body: { detail: 'no scope' } }, ok());
    const { client } = localClient(server);
    track(client);
    await expect(client.ready()).resolves.toMatchObject({ ok: false });
    await jest.advanceTimersByTimeAsync(REFUSED_RETRY_MS);
    await expect(client.ready()).resolves.toEqual({ ok: true, rulesetVersion: 'v1' });
  });

  test('404 names the minimum server release; after a load it discards', async () => {
    const server = new FakeServer({ status: 404 });
    const { client, errors } = localClient(server);
    track(client);
    const result = await client.ready();
    expect(result.ok).toBe(false);
    expect(errors[0].error.message).toContain(`Controlvar ${MIN_SERVER_RELEASE} or later`);

    const later = new FakeServer(ok(), { status: 404 });
    const second = localClient(later);
    track(second.client);
    await second.client.ready();
    await jest.advanceTimersByTimeAsync(DEFAULT_REFRESH_INTERVAL_MS);
    expect(second.client.status().ready).toBe(false);
  });

  test('429 keeps the ruleset and waits for Retry-After', async () => {
    const server = new FakeServer(ok(), { status: 429, headers: { 'Retry-After': '120' } }, { status: 304 });
    const { client } = localClient(server);
    track(client);
    await client.ready();
    await jest.advanceTimersByTimeAsync(DEFAULT_REFRESH_INTERVAL_MS);
    expect(server.requests(RULESET_PATH)).toHaveLength(2);
    expect(client.status().ready).toBe(true);
    await jest.advanceTimersByTimeAsync(119_000);
    expect(server.requests(RULESET_PATH)).toHaveLength(2);
    await jest.advanceTimersByTimeAsync(1_000);
    expect(server.requests(RULESET_PATH)).toHaveLength(3);
  });

  test.each([
    ['an HTML page', { text: '<html>' }],
    ['a document missing its flags', { body: { schema: 1, bucketing: 'md5-mod100-v1', version: 'v9' } }],
    ['a flag missing its rules', { body: { ...RULESET, flags: [{ key: 'x', active: true, evaluation: 'local' }] } }],
  ])('a malformed 200 (%s) keeps the last good ruleset, and before a load stays on the server', async (_label, step) => {
    const server = new FakeServer(ok(), step as Step);
    const { client, errors } = localClient(server);
    track(client);
    await client.ready();
    await jest.advanceTimersByTimeAsync(DEFAULT_REFRESH_INTERVAL_MS);
    expect(client.status()).toMatchObject({ ready: true, rulesetVersion: 'v1' });
    expect(errors.map(e => e.error.code)).toEqual(['INVALID_RESPONSE']);

    const fresh = new FakeServer(step as Step);
    const second = localClient(fresh);
    track(second.client);
    await expect(second.client.ready()).resolves.toMatchObject({ ok: false });
    await expect(second.client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'server' });
  });

  test.each([
    ['schema', { ...RULESET, schema: 2 }],
    ['bucketing', { ...RULESET, bucketing: 'md5-mod100-v2' }],
  ])('a 200 with an unknown %s discards and defers', async (_label, body) => {
    const server = new FakeServer(ok(), ok(body, '"v9"'));
    const { client, errors } = localClient(server);
    track(client);
    await client.ready();
    await jest.advanceTimersByTimeAsync(DEFAULT_REFRESH_INTERVAL_MS);
    expect(client.status().ready).toBe(false);
    expect(errors[0].error.message).toContain('does not understand');
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'server' });
  });

  test('polls send the ETag, and a 304 keeps the ruleset', async () => {
    const server = new FakeServer(ok(), { status: 304 });
    const { client } = localClient(server);
    track(client);
    await client.ready();
    const first = client.status().lastRefreshAt!.getTime();
    await jest.advanceTimersByTimeAsync(DEFAULT_REFRESH_INTERVAL_MS);
    const polls = server.requests(RULESET_PATH);
    expect(polls[0].headers['If-None-Match']).toBeUndefined();
    expect(polls[1].headers['If-None-Match']).toBe('"v1"');
    expect(polls[1].headers['X-API-Key']).toBe('scoped-key');
    expect(client.status()).toMatchObject({ ready: true, rulesetVersion: 'v1' });
    expect(client.status().lastRefreshAt!.getTime()).toBe(first + DEFAULT_REFRESH_INTERVAL_MS);
  });

  test('maxStaleMs: a ruleset older than that is not used', async () => {
    const server = new FakeServer(ok(), { status: 503 });
    const { client } = localClient(server, { maxStaleMs: 45_000 });
    track(client);
    await client.ready();
    await jest.advanceTimersByTimeAsync(40_000);
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'local' });
    await jest.advanceTimersByTimeAsync(10_000);
    client.clearCache();
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'server' });
    expect(client.status().ready).toBe(false);
  });
});

// ─── Refresh timing ──────────────────────────────────────────────────────────

describe('refresh interval', () => {
  async function gapAfterLoad(extra: Partial<ClientConfig>): Promise<number> {
    const server = new FakeServer(ok());
    const { client } = localClient(server, extra);
    track(client);
    await client.ready();
    const start = Date.now();
    while (server.requests(RULESET_PATH).length < 2) await jest.advanceTimersByTimeAsync(100);
    return Date.now() - start;
  }

  test('defaults to 30 seconds', async () => {
    expect(await gapAfterLoad({})).toBe(30_000);
  });

  test('is at least 5 seconds', async () => {
    expect(await gapAfterLoad({ refreshIntervalMs: 1_000 })).toBe(5_000);
  });

  test('is jittered by ±10%', async () => {
    (Math.random as jest.Mock).mockReturnValue(0);
    expect(await gapAfterLoad({ refreshIntervalMs: 10_000 })).toBe(9_000);
    (Math.random as jest.Mock).mockReturnValue(0.99999);
    expect(await gapAfterLoad({ refreshIntervalMs: 10_000 })).toBe(11_000);
  });

  test('the timers do not keep the process alive', async () => {
    const unref = jest.fn();
    const realSetTimeout = global.setTimeout;
    jest.spyOn(global, 'setTimeout').mockImplementation(((fn: () => void, ms?: number) => {
      const timer = realSetTimeout(fn, ms);
      return Object.assign(timer, { unref });
    }) as unknown as typeof setTimeout);
    const server = new FakeServer(ok());
    const { client } = localClient(server);
    track(client);
    await client.ready();
    expect(unref).toHaveBeenCalledTimes(2); // the poll and the flush
  });
});

// ─── Evaluation counts ───────────────────────────────────────────────────────

describe('evaluation counts', () => {
  test('close() sends one entry per flag; deferred evaluations are not counted', async () => {
    const server = new FakeServer(ok(RULESET_WITH_REMOTE, '"v2"'));
    const { client } = localClient(server);
    await client.ready();
    for (let i = 0; i < 7; i++) await client.isFeatureEnabled('us-only', i < 3 ? us : fr);
    await client.isFeatureEnabled('off', us);
    await client.isFeatureEnabled('semver', us); // remote: the server records it itself
    await client.getAllFlags('user-2', { country: 'US' }); // defers (semver is remote): not counted
    await client.close();

    const posts = server.requests(EVALUATIONS_PATH);
    expect(posts).toHaveLength(1);
    expect(posts[0].method).toBe('POST');
    const entries = (posts[0].body as { evaluations: Array<Record<string, unknown>> }).evaluations;
    expect(entries.map(({ flag_key, count, enabled_count }) => ({ flag_key, count, enabled_count }))).toEqual([
      { flag_key: 'us-only', count: 7, enabled_count: 3 },
      { flag_key: 'off', count: 1, enabled_count: 0 },
    ]);
    const { window_start, window_end } = entries[0] as { window_start: string; window_end: string };
    expect(Date.parse(window_end) - Date.parse(window_start)).toBeLessThanOrEqual(10 * 60_000);

    // After close() nothing is answered locally, so nothing is left uncounted.
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'server' });
  });

  test('getAllFlags counts every flag it answered', async () => {
    const server = new FakeServer(ok());
    const { client } = localClient(server);
    await client.ready();
    await client.getAllFlags('user-1', { country: 'US' });
    await client.close();
    const entries = (server.requests(EVALUATIONS_PATH)[0].body as { evaluations: Array<{ flag_key: string; count: number }> }).evaluations;
    expect(entries.map(e => [e.flag_key, e.count])).toEqual([
      ['all-on', 1],
      ['us-only', 1],
    ]);
  });

  test('counts are sent every 60 seconds, and only when there are some', async () => {
    const server = new FakeServer(ok(), { status: 304 });
    const { client } = localClient(server);
    track(client);
    await client.ready();
    await client.isFeatureEnabled('all-on', us);
    await jest.advanceTimersByTimeAsync(FLUSH_INTERVAL_MS);
    expect(server.requests(EVALUATIONS_PATH)).toHaveLength(1);
    await jest.advanceTimersByTimeAsync(FLUSH_INTERVAL_MS);
    expect(server.requests(EVALUATIONS_PATH)).toHaveLength(1);
    await client.isFeatureEnabled('all-on', us);
    await jest.advanceTimersByTimeAsync(FLUSH_INTERVAL_MS);
    expect(server.requests(EVALUATIONS_PATH)).toHaveLength(2);
  });

  test('a failed report goes to onError as flush', async () => {
    const server = new FakeServer(ok());
    server.evaluationsStatus = 403;
    const { client, errors } = localClient(server);
    await client.ready();
    await client.isFeatureEnabled('all-on', us);
    await client.close();
    expect(errors.map(e => [e.operation, e.error.status])).toEqual([['flush', 403]]);
  });
});

// ─── Construction ────────────────────────────────────────────────────────────

describe('construction', () => {
  afterEach(() => {
    delete (globalThis as { window?: unknown }).window;
  });

  test('local mode refuses to start in a browser', () => {
    (globalThis as { window?: unknown }).window = { document: {} };
    const server = new FakeServer(ok());
    expect(() => localClient(server)).toThrow(BROWSER_LOCAL_EVALUATION_MESSAGE);
    expect(BROWSER_LOCAL_EVALUATION_MESSAGE).toBe(
      "evaluation: 'local' downloads every flag's targeting rules, including the values in them, and is " +
        "for server-side code. In a browser, leave evaluation at its default ('server')."
    );
    // The default mode still works in a browser.
    expect(
      () => new ExperimentationClient({ apiUrl: 'https://api.example.com', apiKey: 'k', fetch: server.fetch as unknown as typeof fetch })
    ).not.toThrow();
    expect(server.requests(RULESET_PATH)).toHaveLength(0);
  });

  test('a window without a document (a worker, Deno) is not a browser', () => {
    (globalThis as { window?: unknown }).window = {};
    const server = new FakeServer(ok());
    expect(() => track(localClient(server).client)).not.toThrow();
  });

  test('rejects a bad evaluation mode or refresh interval', () => {
    const server = new FakeServer(ok());
    expect(() => localClient(server, { evaluation: 'edge' as never })).toThrow("evaluation must be 'server' or 'local'");
    expect(() => localClient(server, { refreshIntervalMs: Number.NaN })).toThrow('refreshIntervalMs');
    expect(() => localClient(server, { maxStaleMs: 0 })).toThrow('maxStaleMs');
  });

  test('status() reports the ruleset', async () => {
    const server = new FakeServer(ok(RULESET_WITH_REMOTE, '"v2"'));
    const { client } = localClient(server);
    track(client);
    expect(client.status()).toMatchObject({ evaluation: 'local', ready: false, rulesetVersion: null, lastRefreshAt: null });
    await client.ready();
    const status = client.status();
    expect(status).toMatchObject({ evaluation: 'local', ready: true, rulesetVersion: 'v2', lastError: null, serverEvaluatedFlags: ['semver'] });
    expect(status.lastRefreshAt).toBeInstanceOf(Date);
  });

  test('ready({ timeoutMs }) resolves ok: false when the first load takes longer', async () => {
    const server = new FakeServer(ok());
    server.fetch.mockImplementation(() => new Promise(() => undefined));
    const { client } = localClient(server, { timeoutMs: 60_000 });
    track(client);
    const ready = client.ready({ timeoutMs: 1_000 });
    await jest.advanceTimersByTimeAsync(1_000);
    await expect(ready).resolves.toMatchObject({ ok: false });
  });
});

// ─── Review fixes: exact waits, evaluate errors, report limits ───────────────

describe('only the regular interval is jittered', () => {
  /** Times (fake clock) at which the ruleset was requested. */
  function pollTimes(server: FakeServer): number[] {
    const times: number[] = [];
    const original = server.fetch.getMockImplementation()!;
    server.fetch.mockImplementation(async (url: string, init: RequestInit) => {
      if (url.includes(RULESET_PATH)) times.push(Date.now());
      return original(url, init);
    });
    return times;
  }

  test.each([0, 0.99999])('Retry-After is a floor (Math.random %p)', async random => {
    (Math.random as jest.Mock).mockReturnValue(random);
    const server = new FakeServer(ok(), { status: 429, headers: { 'Retry-After': '120' } }, { status: 304 });
    const times = pollTimes(server);
    const { client } = localClient(server);
    track(client);
    await client.ready();
    while (times.length < 3) await jest.advanceTimersByTimeAsync(1_000);
    expect(times[2] - times[1]).toBe(120_000);
  });

  test.each([0, 0.99999])('the backoff cap is a ceiling (Math.random %p)', async random => {
    (Math.random as jest.Mock).mockReturnValue(random);
    const server = new FakeServer({ status: 500 });
    const times = pollTimes(server);
    const { client } = localClient(server);
    track(client);
    await client.ready();
    while (times.length < 8) await jest.advanceTimersByTimeAsync(10_000);
    const gaps = times.slice(1).map((t, i) => t - times[i]);
    expect(gaps).toEqual([30_000, 60_000, 120_000, 240_000, MAX_BACKOFF_MS, MAX_BACKOFF_MS, MAX_BACKOFF_MS]);
  });

  test.each([0, 0.99999])('the refused retry is exactly 10 minutes (Math.random %p)', async random => {
    (Math.random as jest.Mock).mockReturnValue(random);
    const server = new FakeServer({ status: 403, body: { detail: 'no scope' } });
    const times = pollTimes(server);
    const { client } = localClient(server);
    track(client);
    await client.ready();
    while (times.length < 3) await jest.advanceTimersByTimeAsync(60_000);
    expect(times[1] - times[0]).toBe(REFUSED_RETRY_MS);
    expect(times[2] - times[1]).toBe(REFUSED_RETRY_MS);
  });
});

describe('an unexpected failure in local evaluation', () => {
  test('is reported as evaluate, and the call goes to the server', async () => {
    const server = new FakeServer(ok());
    server.userFlags = () => ({ body: { 'all-on': true } });
    const { client, errors } = localClient(server);
    track(client);
    await client.ready();
    const spy = jest.spyOn(evaluatorModule, 'evaluateLocally').mockImplementation(() => {
      throw new Error('boom');
    });
    await expect(client.evaluateFlag('all-on', us)).resolves.toMatchObject({ source: 'server' });
    await expect(client.getAllFlags('user-1', { country: 'US' })).resolves.toEqual({ 'all-on': true });
    spy.mockRestore();
    expect(errors.map(e => e.operation)).toEqual(['evaluate', 'evaluate']);
    expect(errors[0].error.message).toContain('boom');
    await client.close();
    expect(server.requests(EVALUATIONS_PATH)).toHaveLength(0); // nothing counted locally
  });
});

describe('evaluation report limits', () => {
  function manyFlags(n: number) {
    const flags = Array.from({ length: n }, (_, i) => ({
      key: `f${String(i).padStart(5, '0')}`,
      active: true,
      evaluation: 'local',
      rollout_percentage: 100,
      rules: [],
      default_rule: null,
    }));
    return { ...RULESET, version: `n${n}`, flags };
  }

  test.each([
    [1000, [1000]],
    [1001, [1000, 1]],
  ])('%i flags are reported in requests of %j entries', async (n, sizes) => {
    const server = new FakeServer(ok(manyFlags(n as number), `"n${n}"`));
    const { client } = localClient(server);
    await client.ready();
    await client.getAllFlags('user-1');
    await client.close();
    const posts = server.requests(EVALUATIONS_PATH);
    expect(posts.map(p => (p.body as { evaluations: unknown[] }).evaluations.length)).toEqual(sizes);
    const total = posts
      .flatMap(p => (p.body as { evaluations: Array<{ count: number }> }).evaluations)
      .reduce((sum, e) => sum + e.count, 0);
    expect(total).toBe(n);
  });

  test.each([
    [1_000_000, 1_000_000, [[1_000_000, 1_000_000]]],
    [1_000_001, 1_000_001, [[1_000_000, 1_000_000], [1, 1]]],
    [1_000_001, 5, [[1_000_000, 5], [1, 0]]],
    [2_000_000, 0, [[1_000_000, 0], [1_000_000, 0]]],
  ])('a count of %i (%i enabled) is split at 1,000,000', async (count, enabled, expected) => {
    const server = new FakeServer(ok());
    const { client } = localClient(server);
    await client.ready();
    await client.isFeatureEnabled('all-on', us);
    const runtime = (client as unknown as { local: { tallies: Map<string, { count: number; enabled: number }> } }).local;
    runtime.tallies.set('all-on', { count: count as number, enabled: enabled as number });
    await client.close();
    const body = server.requests(EVALUATIONS_PATH)[0].body as { evaluations: Array<{ count: number; enabled_count: number }> };
    expect(body.evaluations.map(e => [e.count, e.enabled_count])).toEqual(expected);
  });
});
