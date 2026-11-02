/**
 * `onExposure` (#1499): the client calls it once for each `POST /api/v1/tracking/assign` the server
 * answered with `assigned: true` and the client stored, and never for a cached answer, the second
 * of concurrent first calls, a failed call, an `assigned: false` answer or a flag. What it throws
 * goes to `onError(err, 'exposure')` with `code: 'HOOK_ERROR'` and never reaches the caller.
 *
 * The server answers 'treatment' and the default variant is 'control', so a hook failure that
 * changed the answer could not pass by matching the fallback. Every "not called" assertion comes
 * after a macrotask flush, so a late call would be seen.
 */
import { ExperimentationClient } from '../src/client';
import { ExperimentationError } from '../src/errors';
import { callExposureHook } from '../src/exposure';
import { RULESET_PATH } from '../src/local';
import type { AssignResponse, ClientConfig, Exposure, SwallowedOperation } from '../src/types';

const ASSIGNED: AssignResponse = {
  experiment_key: 'exp-1',
  user_id: 'u-1',
  variant_id: 'v-2',
  variant_name: 'treatment',
  is_control: false,
  configuration: { color: 'green' },
  assigned: true,
  reason: 'assigned',
};
const EXPOSURE: Exposure = { experimentKey: 'exp-1', userId: 'u-1', variantId: 'v-2', variantName: 'treatment' };
const user = { userId: 'u-1', attributes: { plan: 'pro' } };

interface Step {
  status?: number;
  body?: unknown;
  /** The body is not JSON. */
  invalidJson?: boolean;
}

function response(step: Step) {
  const status = step.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: () => (step.invalidJson ? Promise.reject(new SyntaxError('Unexpected token <')) : Promise.resolve(step.body)),
  };
}

/**
 * A fetch that records each request and answers it with `answer(url, body)`; with `gate`, no
 * answer is given before the gate resolves.
 */
function fakeFetch(
  answer: (url: string, body: any) => Step | Error = () => ({ body: ASSIGNED }),
  gate: Promise<void> = Promise.resolve()
) {
  const requests: Array<{ method: string; path: string; body: any }> = [];
  const fetch = jest.fn(async (url: string, init: RequestInit = {}) => {
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    requests.push({ method: init.method ?? 'GET', path: new URL(url).pathname, body });
    await gate;
    const step = answer(url, body);
    if (step instanceof Error) throw step;
    return response(step);
  });
  return { fetch, requests };
}

function makeClient(fetch: jest.Mock, extra: Partial<ClientConfig> = {}) {
  const hook = jest.fn();
  const errors: Array<{ error: ExperimentationError; operation: SwallowedOperation }> = [];
  const client = new ExperimentationClient({
    apiUrl: 'https://api.example.com',
    apiKey: 'test-api-key',
    fetch: fetch as unknown as typeof globalThis.fetch,
    onExposure: hook,
    onError: (error, operation) => errors.push({ error, operation }),
    ...extra,
  });
  return { client, hook, errors };
}

/** Let every pending promise and timer callback queued so far run. */
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

const assignRequests = (requests: Array<{ path: string }>) =>
  requests.filter(request => request.path === '/api/v1/tracking/assign');

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── When it is called ───────────────────────────────────────────────────────

describe('onExposure: when it is called', () => {
  test('is called once, with exactly the experiment key, user id, variant id and variant name, for an assigned: true answer', async () => {
    const { fetch } = fakeFetch();
    const { client, hook } = makeClient(fetch);
    await client.getAssignment('exp-1', user);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook.mock.calls[0]).toHaveLength(1);
    expect(hook.mock.calls[0][0]).toStrictEqual(EXPOSURE);
  });

  test('passes variantId null when the server sends no variant_id', async () => {
    const { variant_id: _omitted, ...withoutId } = ASSIGNED;
    const { fetch } = fakeFetch(() => ({ body: withoutId }));
    const { client, hook } = makeClient(fetch);
    await client.getAssignment('exp-1', user);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook.mock.calls[0][0]).toStrictEqual({ ...EXPOSURE, variantId: null });
  });

  test('a cached answer calls nothing: however often the answer is asked for, one request and one call', async () => {
    const { fetch, requests } = fakeFetch();
    const { client, hook } = makeClient(fetch);
    expect(await client.getVariant('exp-1', user)).toBe('treatment');
    await client.getAssignment('exp-1', user);
    await client.getAssignment('exp-1', { userId: 'u-1' });
    expect(await client.getVariant('exp-1', user)).toBe('treatment');
    await flush();
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test('is called once per user and experiment: another user or another experiment is another request and another call', async () => {
    const { fetch, requests } = fakeFetch((_url, body) => ({
      body: { ...ASSIGNED, experiment_key: body.experiment_key, user_id: body.user_id },
    }));
    const { client, hook } = makeClient(fetch);
    await client.getAssignment('exp-1', { userId: 'u-1' });
    await client.getAssignment('exp-1', { userId: 'u-2' });
    await client.getAssignment('exp-2', { userId: 'u-1' });
    await client.getAssignment('exp-1', { userId: 'u-1' });
    await client.getAssignment('exp-2', { userId: 'u-1' });
    expect(requests).toHaveLength(3);
    expect(hook.mock.calls.map(([e]) => `${e.experimentKey}/${e.userId}`)).toEqual(['exp-1/u-1', 'exp-1/u-2', 'exp-2/u-1']);
  });

  test('concurrent first calls share one request and make one call', async () => {
    let release!: () => void;
    const gate = new Promise<void>(resolve => (release = resolve));
    const { fetch } = fakeFetch(undefined, gate);
    const { client, hook } = makeClient(fetch);
    const first = client.getAssignment('exp-1', user);
    const second = client.getVariant('exp-1', user);
    const third = client.getAssignment('exp-1', user);
    expect(fetch).toHaveBeenCalledTimes(1);
    release();
    await Promise.all([first, second, third]);
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test.each(['holdout', 'mutual_exclusion', 'targeting', 'draft', 'paused', 'completed', 'archived'])(
    'is not called for assigned: false (%s): the control answer comes back unchanged',
    async reason => {
      const control: AssignResponse = {
        ...ASSIGNED,
        variant_id: 'v-1',
        variant_name: 'control',
        is_control: true,
        configuration: null,
        assigned: false,
        reason,
      };
      const { fetch, requests } = fakeFetch(() => ({ body: control }));
      const { client, hook, errors } = makeClient(fetch);
      expect(await client.getAssignment('exp-1', user)).toEqual({
        experimentKey: 'exp-1',
        userId: 'u-1',
        variantId: 'v-1',
        variantName: 'control',
        isControl: true,
        configuration: null,
        assigned: false,
        reason,
      });
      await flush();
      expect(requests).toHaveLength(1);
      expect(hook).toHaveBeenCalledTimes(0);
      expect(errors).toEqual([]);
    }
  );

  test('is not called when the answer has no assigned field (a server that does not send it)', async () => {
    const { assigned: _a, reason: _r, ...older } = ASSIGNED;
    const { fetch } = fakeFetch(() => ({ body: older }));
    const { client, hook, errors } = makeClient(fetch);
    const assignment = await client.getAssignment('exp-1', user);
    expect(assignment.variantName).toBe('treatment');
    expect(assignment.assigned).toBeUndefined();
    await flush();
    expect(hook).toHaveBeenCalledTimes(0);
    expect(errors).toEqual([]);
  });

  test.each<[string, Step | Error]>([
    ['a 404', { status: 404, body: { detail: 'Experiment not found' } }],
    ['a 500', { status: 500, body: { detail: 'boom' } }],
    ['a network error', new TypeError('fetch failed')],
    ['an answer without variant_name', { body: { experiment_key: 'exp-1', user_id: 'u-1', assigned: true } }],
    ['a body that is not JSON', { invalidJson: true }],
  ])('a failed call calls nothing: %s', async (_name, step) => {
    const { fetch } = fakeFetch(() => step);
    const { client, hook } = makeClient(fetch);
    await expect(client.getAssignment('exp-1', user)).rejects.toBeInstanceOf(ExperimentationError);
    expect(await client.getVariant('exp-1', user)).toBe('control');
    await flush();
    expect(hook).toHaveBeenCalledTimes(0);
  });

  test('a call that times out calls nothing, even if the server recorded the exposure', async () => {
    const fetch = jest.fn(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          (init.signal as AbortSignal).addEventListener('abort', () => reject(new Error('The operation was aborted')));
        })
    );
    const { client, hook } = makeClient(fetch, { timeoutMs: 5 });
    await expect(client.getAssignment('exp-1', user)).rejects.toMatchObject({ code: 'TIMEOUT' });
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(hook).toHaveBeenCalledTimes(0);
  });

  test('flags never call it: evaluateFlag, isFeatureEnabled, getAllFlags and the typed getters, in server mode', async () => {
    const { fetch, requests } = fakeFetch(url =>
      url.includes('/feature-flags/user/')
        ? { body: { 'my-flag': true } }
        : { body: { key: 'my-flag', enabled: true, config: null, value: 'blue', value_type: 'string' } }
    );
    const { client, hook } = makeClient(fetch);
    await client.evaluateFlag('my-flag', user);
    await client.isFeatureEnabled('other-flag', user);
    await client.getStringValue('third-flag', user, 'red');
    await client.getAllFlags('u-1');
    await flush();
    expect(requests).toHaveLength(4);
    expect(hook).toHaveBeenCalledTimes(0);
  });

  test('flags never call it in local mode either, and experiments still do', async () => {
    const ruleset = {
      schema: 1,
      version: 'v1',
      bucketing: 'md5-mod100-v1',
      flags: [{ key: 'all-on', active: true, evaluation: 'local', rollout_percentage: 100, rules: [], default_rule: null }],
    };
    const { fetch } = fakeFetch(url => {
      if (url.includes(RULESET_PATH)) return { body: ruleset };
      if (url.includes('/tracking/assign')) return { body: ASSIGNED };
      return { status: 201, body: { accepted: 0, errors: [] } };
    });
    const { client, hook } = makeClient(fetch, { evaluation: 'local' });
    try {
      await expect(client.ready()).resolves.toEqual({ ok: true, rulesetVersion: 'v1' });
      expect(await client.evaluateFlag('all-on', user)).toMatchObject({ enabled: true, source: 'local' });
      expect(await client.isFeatureEnabled('all-on', { userId: 'u-2' })).toBe(true);
      await flush();
      expect(hook).toHaveBeenCalledTimes(0);
      await client.getAssignment('exp-1', user);
      expect(hook).toHaveBeenCalledTimes(1);
      expect(hook.mock.calls[0][0]).toStrictEqual(EXPOSURE);
    } finally {
      await client.close();
    }
  });
});

// ─── When it is called again ─────────────────────────────────────────────────

describe('onExposure: called again when the client asks the server again', () => {
  const T0 = 1_700_000_000_000;

  test('after the cache lifetime', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const { fetch, requests } = fakeFetch();
    const { client, hook } = makeClient(fetch, { cacheTtlMs: 60_000 });
    await client.getAssignment('exp-1', user);
    now.mockReturnValue(T0 + 59_999);
    await client.getAssignment('exp-1', user);
    expect(hook).toHaveBeenCalledTimes(1);
    // Past the lifetime, not on its boundary.
    now.mockReturnValue(T0 + 60_001);
    await client.getAssignment('exp-1', user);
    expect(requests).toHaveLength(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });

  test('after clearCache()', async () => {
    const { fetch, requests } = fakeFetch();
    const { client, hook } = makeClient(fetch);
    await client.getAssignment('exp-1', user);
    client.clearCache();
    await client.getAssignment('exp-1', user);
    expect(requests).toHaveLength(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });

  test('in a new client', async () => {
    const { fetch, requests } = fakeFetch();
    const hook = jest.fn();
    for (let i = 0; i < 2; i++) {
      const client = new ExperimentationClient({
        apiUrl: 'https://api.example.com',
        apiKey: 'k',
        fetch: fetch as unknown as typeof globalThis.fetch,
        onExposure: hook,
      });
      await client.getAssignment('exp-1', user);
      await client.getAssignment('exp-1', user);
    }
    expect(requests).toHaveLength(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });

  test('on every call with cacheTtlMs 0, which asks the server every time', async () => {
    const { fetch, requests } = fakeFetch();
    const { client, hook } = makeClient(fetch, { cacheTtlMs: 0 });
    await client.getAssignment('exp-1', user);
    await client.getAssignment('exp-1', user);
    await client.getAssignment('exp-1', user);
    expect(requests).toHaveLength(3);
    expect(hook).toHaveBeenCalledTimes(3);
  });
});

// ─── What it may do, and what the client does around it ──────────────────────

describe('onExposure: the client around it', () => {
  test('with a hook, an assignment sends exactly one request, POST /api/v1/tracking/assign', async () => {
    const { fetch, requests } = fakeFetch();
    const { client, hook } = makeClient(fetch);
    await client.getAssignment('exp-1', user);
    await flush();
    expect(hook).toHaveBeenCalledTimes(1);
    expect(requests.map(r => `${r.method} ${r.path}`)).toEqual(['POST /api/v1/tracking/assign']);
  });

  test('runs before getAssignment resolves', async () => {
    const order: string[] = [];
    const { fetch } = fakeFetch();
    const { client } = makeClient(fetch, { onExposure: () => order.push('hook') });
    await client.getAssignment('exp-1', user);
    order.push('returned');
    expect(order).toEqual(['hook', 'returned']);
  });

  test('runs after the answer is stored and recorded: a key-less track() in the hook reaches the experiment', async () => {
    const { fetch, requests } = fakeFetch(url =>
      url.includes('/tracking/assign') ? { body: ASSIGNED } : { body: { success_count: 1, failure_count: 0, errors: [] } }
    );
    let client!: ExperimentationClient;
    ({ client } = makeClient(fetch, { onExposure: e => void client.track(e.userId, 'experiment_viewed') }));
    await client.getAssignment('exp-1', user);
    await flush();
    expect(requests.map(r => `${r.method} ${r.path}`)).toEqual(['POST /api/v1/tracking/assign', 'POST /api/v1/tracking/batch']);
    expect(requests[1].body.events).toEqual([
      { event_type: 'experiment_viewed', event_name: 'experiment_viewed', user_id: 'u-1', experiment_key: 'exp-1' },
    ]);
  });

  test('a hook that asks for the same assignment gets the cached answer: one request, one call', async () => {
    const { fetch, requests } = fakeFetch();
    const inner: Array<Promise<string>> = [];
    let client!: ExperimentationClient;
    const hook = jest.fn((e: Exposure) => {
      inner.push(client.getVariant(e.experimentKey, { userId: e.userId }));
    });
    ({ client } = makeClient(fetch, { onExposure: hook }));
    await client.getAssignment('exp-1', user);
    expect(await Promise.all(inner)).toEqual(['treatment']);
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test("the hook's object is its own: changing it changes nothing the client keeps", async () => {
    const { fetch } = fakeFetch();
    const { client } = makeClient(fetch, {
      onExposure: e => {
        // @ts-expect-error Exposure's fields are readonly
        e.variantName = 'mutated';
      },
    });
    await client.getAssignment('exp-1', user);
    expect((await client.getAssignment('exp-1', user)).variantName).toBe('treatment');
    expect(client.getAssignments('u-1')[0].variantName).toBe('treatment');
  });

  test.each<[string, unknown]>([
    ['a string', 'onExposure'],
    ['a number', 42],
    ['an object', { onExposure: () => undefined }],
    ['true', true],
  ])('the constructor refuses %s as onExposure', (_name, value) => {
    expect(
      () =>
        new ExperimentationClient({
          apiUrl: 'https://api.example.com',
          apiKey: 'k',
          onExposure: value as ClientConfig['onExposure'],
        })
    ).toThrow(new Error('onExposure must be a function'));
  });

  test.each([undefined, null])('onExposure %s sets no hook, and the assignment works as before', async value => {
    const { fetch, requests } = fakeFetch();
    const errors: SwallowedOperation[] = [];
    const client = new ExperimentationClient({
      apiUrl: 'https://api.example.com',
      apiKey: 'k',
      fetch: fetch as unknown as typeof globalThis.fetch,
      onExposure: value,
      onError: (_error, operation) => errors.push(operation),
    });
    expect(await client.getVariant('exp-1', user)).toBe('treatment');
    await flush();
    expect(requests).toHaveLength(1);
    expect(errors).toEqual([]);
  });
});

// ─── When it fails ───────────────────────────────────────────────────────────

describe('onExposure: a hook that throws', () => {
  test('changes nothing: getVariant returns the variant and getAssignment resolves', async () => {
    const { fetch } = fakeFetch();
    const throwing = () => {
      throw new Error('hook boom');
    };
    const viaVariant = makeClient(fetch, { onExposure: throwing });
    expect(await viaVariant.client.getVariant('exp-1', user)).toBe('treatment');
    const viaAssignment = makeClient(fetch, { onExposure: throwing });
    await expect(viaAssignment.client.getAssignment('exp-1', user)).resolves.toMatchObject({
      variantName: 'treatment',
      assigned: true,
    });
  });

  test('leaves the answer cached: a second call makes no request and calls nothing', async () => {
    const { fetch, requests } = fakeFetch();
    const hook = jest.fn(() => {
      throw new Error('hook boom');
    });
    const { client } = makeClient(fetch, { onExposure: hook });
    await client.getAssignment('exp-1', user);
    expect((await client.getAssignment('exp-1', user)).variantName).toBe('treatment');
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test('is reported to onError once, with the operation exposure and an ExperimentationError whose code is HOOK_ERROR and whose cause is what it threw', async () => {
    const thrown = new Error('hook boom');
    const { fetch } = fakeFetch();
    const { client, errors } = makeClient(fetch, {
      onExposure: () => {
        throw thrown;
      },
    });
    await client.getAssignment('exp-1', user);
    await flush();
    expect(errors).toHaveLength(1);
    const [{ error, operation }] = errors;
    expect(operation).toBe('exposure');
    expect(error).toBeInstanceOf(ExperimentationError);
    expect(error.code).toBe('HOOK_ERROR');
    expect(error.message.startsWith('onExposure failed: ')).toBe(true);
    expect(error.message).toBe('onExposure failed: hook boom');
    expect(error.cause).toBe(thrown);
    expect(error.status).toBeUndefined();
  });

  test('a thrown value that is not an Error is reported the same way, as its string', async () => {
    const { fetch } = fakeFetch();
    const { client, errors } = makeClient(fetch, {
      onExposure: () => {
        throw 'plain string';
      },
    });
    await client.getAssignment('exp-1', user);
    expect(errors.map(({ error, operation }) => [operation, error.code, error.message, error.cause])).toEqual([
      ['exposure', 'HOOK_ERROR', 'onExposure failed: plain string', 'plain string'],
    ]);
  });

  test('an onError that throws too leaves the answer unchanged', async () => {
    const { fetch } = fakeFetch();
    const onError = jest.fn(() => {
      throw new Error('handler boom');
    });
    const { client } = makeClient(fetch, {
      onExposure: () => {
        throw new Error('hook boom');
      },
      onError,
    });
    expect(await client.getVariant('exp-1', user)).toBe('treatment');
    expect(onError).toHaveBeenCalledTimes(1);
  });

  test('an async hook that rejects is reported to onError with exposure, and the answer is unchanged', async () => {
    const rejection = new Error('async boom');
    const { fetch } = fakeFetch();
    const { client, errors } = makeClient(fetch, { onExposure: async () => Promise.reject(rejection) });
    expect(await client.getVariant('exp-1', user)).toBe('treatment');
    await flush();
    expect(errors.map(({ error, operation }) => [operation, error.code, error.message, error.cause])).toEqual([
      ['exposure', 'HOOK_ERROR', 'onExposure failed: async boom', rejection],
    ]);
  });

  test('the client does not wait for a Promise the hook returns', async () => {
    const { fetch } = fakeFetch();
    const { client, errors } = makeClient(fetch, { onExposure: () => new Promise<void>(() => undefined) });
    await expect(client.getAssignment('exp-1', user)).resolves.toMatchObject({ variantName: 'treatment' });
    await flush();
    expect(errors).toEqual([]);
  });
});

// ─── The guard ───────────────────────────────────────────────────────────────

describe('callExposureHook', () => {
  test('reports the rejection of the Promise an async hook returns', async () => {
    const rejection = new Error('async boom');
    const report = jest.fn();
    callExposureHook(() => Promise.reject(rejection), EXPOSURE, report);
    expect(report).toHaveBeenCalledTimes(0);
    await flush();
    expect(report).toHaveBeenCalledTimes(1);
    expect(report).toHaveBeenCalledWith(rejection);
  });

  test('reports what the hook throws, at once', () => {
    const report = jest.fn();
    callExposureHook(
      () => {
        throw 'sync boom';
      },
      EXPOSURE,
      report
    );
    expect(report).toHaveBeenCalledTimes(1);
    expect(report).toHaveBeenCalledWith('sync boom');
  });

  test('reports a then that throws and a then getter that throws', () => {
    const report = jest.fn();
    const thenThrows = {
      then() {
        throw new Error('then boom');
      },
    };
    const getterThrows = Object.defineProperty({}, 'then', {
      get() {
        throw new Error('getter boom');
      },
    });
    callExposureHook(() => thenThrows as unknown as void, EXPOSURE, report);
    callExposureHook(() => getterThrows as unknown as void, EXPOSURE, report);
    expect(report.mock.calls.map(([e]) => (e as Error).message)).toEqual(['then boom', 'getter boom']);
  });

  test('passes the hook the exposure, its own argument only, and reports nothing for a value or a resolved Promise', async () => {
    const report = jest.fn();
    const hook = jest.fn();
    callExposureHook(hook, EXPOSURE, report);
    callExposureHook(() => 'ignored' as unknown as void, EXPOSURE, report);
    callExposureHook(() => Promise.resolve(), EXPOSURE, report);
    await flush();
    expect(hook.mock.calls).toEqual([[EXPOSURE]]);
    expect(report).toHaveBeenCalledTimes(0);
  });

  test('drops what report itself throws', () => {
    const report = jest.fn(() => {
      throw new Error('report boom');
    });
    expect(() =>
      callExposureHook(
        () => {
          throw new Error('hook boom');
        },
        EXPOSURE,
        report
      )
    ).not.toThrow();
    expect(report).toHaveBeenCalledTimes(1);
  });
});
