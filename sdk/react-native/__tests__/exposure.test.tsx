/**
 * `onExposure` (#1499): the client calls it once for each `POST /api/v1/tracking/assign` the server
 * answered with `assigned: true` and the client stored where no answer was cached, and never for a
 * cached answer, an answer read from AsyncStorage (unexpired, or the offline fallback), the second
 * of concurrent first calls, a failed call, an `assigned: false` answer or a flag. What it throws
 * goes to `onError(err, 'exposure')` and never reaches the caller.
 *
 * The server answers 'treatment' and AsyncStorage starts empty, so a hook failure that changed the
 * answer (to `null`, or to a stored value) could not pass by matching the fallback. Every "not
 * called" assertion comes after a macrotask flush, so a late call would be seen.
 */
import React from 'react';
import { Text } from 'react-native';
import { render, waitFor } from '@testing-library/react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { ApiError, ExperimentationClient } from '../src/client';
import { ExperimentationProvider } from '../src/context/ExperimentationProvider';
import { callExposureHook } from '../src/exposure';
import { useExperiment } from '../src/hooks/useExperiment';
import { OfflineStorage } from '../src/storage';
import type { Exposure as ExportedExposure } from '../src/index';
import type { Assignment, AssignResponse, Exposure, SdkConfig, SwallowedOperation } from '../src/types';

const BASE_URL = 'https://api.example.com';
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
const TREATMENT: Assignment = {
  experimentKey: 'exp-1',
  userId: 'u-1',
  variantId: 'v-2',
  variantName: 'treatment',
  isControl: false,
  configuration: { color: 'green' },
};

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
    json: () => (step.invalidJson ? Promise.reject(new SyntaxError('Unexpected token <')) : Promise.resolve(step.body)),
  };
}

/**
 * Install a global fetch that records each request and answers it with `answer(path, body)`;
 * with `gate`, no answer is given before the gate resolves.
 */
function fakeFetch(
  answer: (path: string, body: any) => Step | Error = () => ({ body: ASSIGNED }),
  gate: () => Promise<void> = () => Promise.resolve()
) {
  const requests: Array<{ method: string; path: string; body: any }> = [];
  const fetch = jest.fn(async (url: string, init: { method?: string; body?: string } = {}) => {
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    const path = url.slice(BASE_URL.length).split('?')[0];
    requests.push({ method: init.method ?? 'GET', path, body });
    await gate();
    const step = answer(path, body);
    if (step instanceof Error) throw step;
    return response(step);
  });
  global.fetch = fetch as unknown as typeof global.fetch;
  return { fetch, requests };
}

function makeClient(extra: Partial<SdkConfig> = {}) {
  const hook = jest.fn();
  const errors: Array<{ error: Error; operation: SwallowedOperation }> = [];
  const client = new ExperimentationClient({
    apiKey: 'test-api-key',
    baseUrl: BASE_URL,
    timeoutMs: 1000,
    onExposure: hook,
    onError: (error, operation) => errors.push({ error, operation }),
    ...extra,
  });
  return { client, hook, errors };
}

/** Let every pending promise and timer callback queued so far run. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** A gate the test opens by hand. */
function manualGate() {
  let open!: () => void;
  const opened = new Promise<void>((resolve) => (open = resolve));
  return { gate: () => opened, open };
}

beforeEach(async () => {
  jest.restoreAllMocks();
  await AsyncStorage.clear();
  // The AsyncStorage mock is module-level: keep its implementation, drop its call history.
  for (const fn of Object.values(AsyncStorage)) {
    if (jest.isMockFunction(fn)) fn.mockClear();
  }
});

// ─── When it is called ───────────────────────────────────────────────────────

describe('onExposure: when it is called', () => {
  test('is called once, with exactly the experiment key, user id, variant id and variant name, for an assigned: true answer', async () => {
    fakeFetch();
    const { client, hook } = makeClient();
    expect(await client.getAssignment('exp-1', 'u-1')).toEqual(TREATMENT);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook.mock.calls[0]).toHaveLength(1);
    expect(hook.mock.calls[0][0]).toStrictEqual(EXPOSURE);
  });

  test('passes variantId null when the server sends no variant_id', async () => {
    const { variant_id: _omitted, ...withoutId } = ASSIGNED;
    fakeFetch(() => ({ body: withoutId }));
    const { client, hook } = makeClient();
    await client.getAssignment('exp-1', 'u-1');
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook.mock.calls[0][0]).toStrictEqual({ ...EXPOSURE, variantId: null });
  });

  test('a cached answer calls nothing: however often the answer is asked for, one request and one call', async () => {
    const { requests } = fakeFetch();
    const { client, hook } = makeClient();
    expect(await client.getVariant('exp-1', 'u-1')).toBe('treatment');
    await client.getAssignment('exp-1', 'u-1');
    await client.getAssignment('exp-1', 'u-1', { plan: 'pro' });
    expect(await client.getVariant('exp-1', 'u-1')).toBe('treatment');
    await flush();
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test('is called once per user and experiment: another user or another experiment is another request and another call', async () => {
    const { requests } = fakeFetch((_path, body) => ({
      body: { ...ASSIGNED, experiment_key: body.experiment_key, user_id: body.user_id },
    }));
    const { client, hook } = makeClient();
    await client.getAssignment('exp-1', 'u-1');
    await client.getAssignment('exp-1', 'u-2');
    await client.getAssignment('exp-2', 'u-1');
    await client.getAssignment('exp-1', 'u-1');
    await client.getAssignment('exp-2', 'u-1');
    expect(requests).toHaveLength(3);
    expect(hook.mock.calls.map(([e]) => `${e.experimentKey}/${e.userId}`)).toEqual(['exp-1/u-1', 'exp-1/u-2', 'exp-2/u-1']);
  });

  test('concurrent first calls share one request and make one call', async () => {
    const { gate, open } = manualGate();
    const { fetch } = fakeFetch(undefined, gate);
    const { client, hook } = makeClient();
    const first = client.getAssignment('exp-1', 'u-1');
    const second = client.getVariant('exp-1', 'u-1');
    const third = client.getAssignment('exp-1', 'u-1');
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
    open();
    expect(await Promise.all([first, second, third])).toEqual([TREATMENT, 'treatment', TREATMENT]);
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test('clearCache() while a request is in flight: the next call makes a second request, and only the answer stored first calls it', async () => {
    const { gate, open } = manualGate();
    const { fetch } = fakeFetch(undefined, gate);
    const { client, hook } = makeClient();
    const first = client.getAssignment('exp-1', 'u-1');
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
    // clearCache() drops the in-flight request too, so this call cannot share it.
    client.clearCache();
    const second = client.getAssignment('exp-1', 'u-1');
    await flush();
    // The positive control: two requests really are in flight, so two answers are stored.
    expect(fetch).toHaveBeenCalledTimes(2);
    open();
    expect(await Promise.all([first, second])).toEqual([TREATMENT, TREATMENT]);
    await flush();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook.mock.calls[0][0]).toStrictEqual(EXPOSURE);
  });

  test.each(['holdout', 'mutual_exclusion', 'targeting', 'draft', 'paused', 'completed', 'archived'])(
    'is not called for assigned: false (%s): the control answer comes back unchanged',
    async (reason) => {
      const control: AssignResponse = {
        ...ASSIGNED,
        variant_id: 'v-1',
        variant_name: 'control',
        is_control: true,
        configuration: null,
        assigned: false,
        reason,
      };
      const { requests } = fakeFetch(() => ({ body: control }));
      const { client, hook, errors } = makeClient();
      expect(await client.getAssignment('exp-1', 'u-1')).toEqual({
        experimentKey: 'exp-1',
        userId: 'u-1',
        variantId: 'v-1',
        variantName: 'control',
        isControl: true,
        configuration: null,
      });
      await flush();
      expect(requests).toHaveLength(1);
      expect(hook).toHaveBeenCalledTimes(0);
      expect(errors).toEqual([]);
    }
  );

  test.each<[string, Record<string, unknown>]>([
    ['missing', {}],
    ['null', { assigned: null }],
    ['the string "true"', { assigned: 'true' }],
    ['1', { assigned: 1 }],
  ])('is not called when assigned is %s, and the answer is still returned', async (_name, assigned) => {
    const { assigned: _a, reason: _r, ...older } = ASSIGNED;
    fakeFetch(() => ({ body: { ...older, ...assigned } }));
    const { client, hook, errors } = makeClient();
    expect(await client.getAssignment('exp-1', 'u-1')).toEqual(TREATMENT);
    await flush();
    expect(hook).toHaveBeenCalledTimes(0);
    expect(errors).toEqual([]);
  });

  test.each<[string, Step | Error]>([
    ['a 404', { status: 404, body: { detail: 'Experiment not found' } }],
    ['a 500', { status: 500, body: { detail: 'boom' } }],
    ['a network error', new TypeError('Network request failed')],
    ['an answer without variant_name', { body: { experiment_key: 'exp-1', user_id: 'u-1', assigned: true } }],
    ['a body that is not JSON', { invalidJson: true }],
  ])('a failed call calls nothing: %s', async (_name, step) => {
    fakeFetch(() => step);
    const { client, hook, errors } = makeClient();
    expect(await client.getAssignment('exp-1', 'u-1')).toBeNull();
    await flush();
    expect(hook).toHaveBeenCalledTimes(0);
    expect(errors.map((e) => e.operation)).toEqual(['getAssignment']);
  });

  test('a call that times out calls nothing, even if the server recorded the exposure', async () => {
    const fetch = jest.fn(
      (_url: string, init: { signal: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener('abort', () => reject(new Error('The operation was aborted')));
        })
    );
    global.fetch = fetch as unknown as typeof global.fetch;
    const { client, hook, errors } = makeClient({ timeoutMs: 5 });
    expect(await client.getAssignment('exp-1', 'u-1')).toBeNull();
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(hook).toHaveBeenCalledTimes(0);
    expect(errors.map((e) => e.operation)).toEqual(['getAssignment']);
  });

  test('flags never call it: evaluateFlag, isFeatureEnabled and getAllFlags', async () => {
    const { requests } = fakeFetch((path) =>
      path.startsWith('/api/v1/feature-flags/user/')
        ? { body: { 'my-flag': true } }
        : { body: { key: 'my-flag', enabled: true, config: null } }
    );
    const { client, hook } = makeClient();
    await client.evaluateFlag('my-flag', 'u-1');
    await client.isFeatureEnabled('other-flag', 'u-1');
    await client.getAllFlags('u-1');
    await flush();
    expect(requests).toHaveLength(3);
    expect(hook).toHaveBeenCalledTimes(0);
  });
});

// ─── Answers from AsyncStorage ───────────────────────────────────────────────

describe('onExposure: an answer read from AsyncStorage calls nothing', () => {
  test('an unexpired stored answer, as after an app restart: no request and no call', async () => {
    await new OfflineStorage().setAssignment('u-1', 'exp-1', TREATMENT, 60_000);
    const { fetch } = fakeFetch();
    const { client, hook, errors } = makeClient();
    expect(await client.getAssignment('exp-1', 'u-1')).toEqual(TREATMENT);
    await flush();
    expect(fetch).toHaveBeenCalledTimes(0);
    expect(hook).toHaveBeenCalledTimes(0);
    expect(errors).toEqual([]);
  });

  test('the offline fallback: an expired stored answer is returned when the request fails, and onError gets getAssignment once', async () => {
    await new OfflineStorage().setAssignment('u-1', 'exp-1', TREATMENT, -1); // already expired
    const { fetch } = fakeFetch(() => new TypeError('Network request failed'));
    const { client, hook, errors } = makeClient();
    expect(await client.getAssignment('exp-1', 'u-1')).toEqual(TREATMENT);
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(hook).toHaveBeenCalledTimes(0);
    expect(errors.map((e) => e.operation)).toEqual(['getAssignment']);
  });
});

// ─── When it is called again ─────────────────────────────────────────────────

describe('onExposure: called again when the client asks the server again', () => {
  const T0 = 1_700_000_000_000;

  test('after the cache lifetime (one millisecond past it, never at the boundary), when the stored answer has expired too', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const { requests } = fakeFetch();
    const { client, hook } = makeClient({ cacheTtlMs: 60_000 });
    await client.getAssignment('exp-1', 'u-1');
    now.mockReturnValue(T0 + 59_999);
    await client.getAssignment('exp-1', 'u-1');
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
    now.mockReturnValue(T0 + 60_001);
    await client.getAssignment('exp-1', 'u-1');
    expect(requests).toHaveLength(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });

  test('after the default cache lifetime of 5 minutes, with the default offlineFallback', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const { requests } = fakeFetch();
    const hook = jest.fn();
    const { client } = makeClient({ onExposure: hook });
    await client.getAssignment('exp-1', 'u-1');
    now.mockReturnValue(T0 + 299_999);
    // Still in this client's memory, and still in AsyncStorage for a new client.
    await client.getAssignment('exp-1', 'u-1');
    await makeClient({ onExposure: hook }).client.getAssignment('exp-1', 'u-1');
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
    now.mockReturnValue(T0 + 300_001);
    await client.getAssignment('exp-1', 'u-1');
    expect(requests).toHaveLength(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });

  test('not after clearCache() with the default offlineFallback: the stored answer is served, with no request', async () => {
    const { requests } = fakeFetch();
    const { client, hook } = makeClient();
    await client.getAssignment('exp-1', 'u-1');
    client.clearCache();
    expect(await client.getAssignment('exp-1', 'u-1')).toEqual(TREATMENT);
    await flush();
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test('not in a new client with the default offlineFallback: the stored answer is served, with no request', async () => {
    const { requests } = fakeFetch();
    const hook = jest.fn();
    for (let i = 0; i < 2; i++) {
      const { client } = makeClient({ onExposure: hook });
      expect(await client.getAssignment('exp-1', 'u-1')).toEqual(TREATMENT);
      await client.getAssignment('exp-1', 'u-1');
    }
    await flush();
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test('after clearCache() with offlineFallback false', async () => {
    const { requests } = fakeFetch();
    const { client, hook } = makeClient({ offlineFallback: false });
    await client.getAssignment('exp-1', 'u-1');
    client.clearCache();
    await client.getAssignment('exp-1', 'u-1');
    expect(requests).toHaveLength(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });

  test('in a new client with offlineFallback false', async () => {
    const { requests } = fakeFetch();
    const hook = jest.fn();
    for (let i = 0; i < 2; i++) {
      const { client } = makeClient({ onExposure: hook, offlineFallback: false });
      await client.getAssignment('exp-1', 'u-1');
      await client.getAssignment('exp-1', 'u-1');
    }
    expect(requests).toHaveLength(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });

  test('after clearCache() and clearStorage()', async () => {
    const { requests } = fakeFetch();
    const { client, hook } = makeClient();
    await client.getAssignment('exp-1', 'u-1');
    client.clearCache();
    await client.clearStorage();
    await client.getAssignment('exp-1', 'u-1');
    expect(requests).toHaveLength(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });

  test('in a new client after clearStorage()', async () => {
    const { requests } = fakeFetch();
    const hook = jest.fn();
    const { client: first } = makeClient({ onExposure: hook });
    await first.getAssignment('exp-1', 'u-1');
    await first.clearStorage();
    const { client: second } = makeClient({ onExposure: hook });
    await second.getAssignment('exp-1', 'u-1');
    expect(requests).toHaveLength(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });
});

// ─── What it may do, and what the client does around it ──────────────────────

describe('onExposure: the client around it', () => {
  test('with a hook, an assignment sends exactly one request, POST /api/v1/tracking/assign', async () => {
    const { requests } = fakeFetch();
    const { client, hook } = makeClient();
    await client.getAssignment('exp-1', 'u-1');
    await flush();
    expect(hook).toHaveBeenCalledTimes(1);
    expect(requests.map((r) => `${r.method} ${r.path}`)).toEqual(['POST /api/v1/tracking/assign']);
  });

  test('runs before getAssignment resolves', async () => {
    const order: string[] = [];
    fakeFetch();
    const { client } = makeClient({ onExposure: () => void order.push('hook') });
    await client.getAssignment('exp-1', 'u-1');
    order.push('returned');
    expect(order).toEqual(['hook', 'returned']);
  });

  test('runs after the answer is stored: a key-less track() in the hook reaches the experiment', async () => {
    const { requests } = fakeFetch((path) =>
      path === '/api/v1/tracking/assign' ? { body: ASSIGNED } : { body: { success_count: 1, failure_count: 0, errors: [] } }
    );
    let client!: ExperimentationClient;
    ({ client } = makeClient({ onExposure: (e) => void client.track('experiment_viewed', e.userId) }));
    await client.getAssignment('exp-1', 'u-1');
    await flush();
    expect(requests.map((r) => `${r.method} ${r.path}`)).toEqual(['POST /api/v1/tracking/assign', 'POST /api/v1/tracking/batch']);
    expect(requests[1].body.events).toEqual([
      { event_type: 'experiment_viewed', event_name: 'experiment_viewed', user_id: 'u-1', experiment_key: 'exp-1' },
    ]);
  });

  test('runs after the answer is written to AsyncStorage', async () => {
    fakeFetch();
    const written: number[] = [];
    const { client } = makeClient({
      onExposure: () => void written.push((AsyncStorage.setItem as jest.Mock).mock.calls.length),
    });
    await client.getAssignment('exp-1', 'u-1');
    expect(written).toEqual([1]);
  });

  test('a hook that asks for the same assignment gets the cached answer: one request, one call', async () => {
    const { requests } = fakeFetch();
    const inner: Array<Promise<string | null>> = [];
    let client!: ExperimentationClient;
    const hook = jest.fn((e: Exposure) => {
      inner.push(client.getVariant(e.experimentKey, e.userId));
    });
    ({ client } = makeClient({ onExposure: hook }));
    await client.getAssignment('exp-1', 'u-1');
    expect(await Promise.all(inner)).toEqual(['treatment']);
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test("the hook's object is its own: changing it changes nothing the client keeps", async () => {
    fakeFetch();
    const { client } = makeClient({
      onExposure: (e) => {
        // @ts-expect-error Exposure's fields are readonly
        e.variantName = 'mutated';
      },
    });
    await client.getAssignment('exp-1', 'u-1');
    expect((await client.getAssignment('exp-1', 'u-1'))?.variantName).toBe('treatment');
    expect(client.getAssignments('u-1')[0].variantName).toBe('treatment');
    expect((await new OfflineStorage().getAssignment('u-1', 'exp-1'))?.value.variantName).toBe('treatment');
  });

  test('has four readonly fields, in the order experimentKey, userId, variantId, variantName', async () => {
    fakeFetch();
    const { client, hook } = makeClient();
    await client.getAssignment('exp-1', 'u-1');
    const received: Exposure = hook.mock.calls[0][0];
    expect(Object.keys(received)).toEqual(['experimentKey', 'userId', 'variantId', 'variantName']);
    // Each line fails to compile if its field loses `readonly`.
    // @ts-expect-error readonly
    received.experimentKey = 'x';
    // @ts-expect-error readonly
    received.userId = 'x';
    // @ts-expect-error readonly
    received.variantId = 'x';
    // @ts-expect-error readonly
    received.variantName = 'x';
  });

  test('is exported as a type from the package', () => {
    const exported: ExportedExposure = EXPOSURE;
    const local: Exposure = exported;
    expect(local).toBe(EXPOSURE);
  });

  test.each<[string, unknown]>([
    ['a string', 'onExposure'],
    ['a number', 42],
    ['an object', { onExposure: () => undefined }],
    ['an array', []],
    ['true', true],
  ])('the constructor refuses %s as onExposure', (_name, value) => {
    expect(
      () =>
        new ExperimentationClient({
          apiKey: 'k',
          baseUrl: BASE_URL,
          onExposure: value as SdkConfig['onExposure'],
        })
    ).toThrow(new Error('ExperimentationClient: onExposure must be a function'));
  });

  test.each([undefined, null])('onExposure %s sets no hook, and the assignment works as before', async (value) => {
    const { requests } = fakeFetch();
    const errors: SwallowedOperation[] = [];
    const client = new ExperimentationClient({
      apiKey: 'k',
      baseUrl: BASE_URL,
      onExposure: value,
      onError: (_error, operation) => errors.push(operation),
    });
    expect(await client.getVariant('exp-1', 'u-1')).toBe('treatment');
    await flush();
    expect(requests).toHaveLength(1);
    expect(errors).toEqual([]);
  });
});

// ─── When it fails ───────────────────────────────────────────────────────────

describe('onExposure: a hook that throws', () => {
  const throwing = () => {
    throw new Error('hook boom');
  };

  test('changes nothing: getVariant returns the variant and getAssignment resolves to it', async () => {
    fakeFetch();
    const viaVariant = makeClient({ onExposure: throwing, offlineFallback: false });
    expect(await viaVariant.client.getVariant('exp-1', 'u-1')).toBe('treatment');
    await AsyncStorage.clear();
    const viaAssignment = makeClient({ onExposure: throwing });
    await expect(viaAssignment.client.getAssignment('exp-1', 'u-1')).resolves.toEqual(TREATMENT);
  });

  test('leaves the answer cached: a second call makes no request and calls nothing', async () => {
    const { requests } = fakeFetch();
    const hook = jest.fn(throwing);
    const { client } = makeClient({ onExposure: hook });
    await client.getAssignment('exp-1', 'u-1');
    expect((await client.getAssignment('exp-1', 'u-1'))?.variantName).toBe('treatment');
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  test('is reported to onError once, with the operation exposure and the Error it threw, never as a request failure', async () => {
    const thrown = new Error('hook boom');
    fakeFetch();
    const { client, errors } = makeClient({
      onExposure: () => {
        throw thrown;
      },
    });
    await client.getAssignment('exp-1', 'u-1');
    await flush();
    expect(errors).toHaveLength(1);
    const [{ error, operation }] = errors;
    expect(operation).toBe('exposure');
    expect(error).toBe(thrown);
    expect(error).not.toBeInstanceOf(ApiError);
  });

  test('a thrown value that is not an Error is reported as an Error whose message is that value', async () => {
    fakeFetch();
    const { client, errors } = makeClient({
      onExposure: () => {
        throw 'plain string';
      },
    });
    await client.getAssignment('exp-1', 'u-1');
    expect(errors).toHaveLength(1);
    expect(errors[0].operation).toBe('exposure');
    expect(errors[0].error).toBeInstanceOf(Error);
    expect(errors[0].error.message).toBe('plain string');
  });

  test('a thrown value that cannot be turned into a string is still reported, as an Error that says so', async () => {
    fakeFetch();
    const { client, errors } = makeClient({
      onExposure: () => {
        throw Object.create(null);
      },
    });
    expect(await client.getVariant('exp-1', 'u-1')).toBe('treatment');
    expect(errors.map((e) => [e.operation, e.error.message])).toEqual([
      ['exposure', 'onExposure threw a value that cannot be turned into a string'],
    ]);
  });

  test('an onError that throws too leaves the answer unchanged', async () => {
    fakeFetch();
    const onError = jest.fn(() => {
      throw new Error('handler boom');
    });
    const { client } = makeClient({ onExposure: throwing, onError });
    expect(await client.getVariant('exp-1', 'u-1')).toBe('treatment');
    expect(onError).toHaveBeenCalledTimes(1);
  });

  test('an async hook that rejects is reported to onError with exposure, and the answer is unchanged', async () => {
    const rejection = new Error('async boom');
    fakeFetch();
    const { client, errors } = makeClient({ onExposure: async () => Promise.reject(rejection) });
    expect(await client.getVariant('exp-1', 'u-1')).toBe('treatment');
    await flush();
    expect(errors.map(({ error, operation }) => [operation, error])).toEqual([['exposure', rejection]]);
  });

  test('the client does not wait for a Promise the hook returns', async () => {
    fakeFetch();
    const { client, errors } = makeClient({ onExposure: () => new Promise<void>(() => undefined) });
    await expect(client.getAssignment('exp-1', 'u-1')).resolves.toEqual(TREATMENT);
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
    callExposureHook(() => thenThrows, EXPOSURE, report);
    callExposureHook(() => getterThrows, EXPOSURE, report);
    expect(report.mock.calls.map(([e]) => (e as Error).message)).toEqual(['then boom', 'getter boom']);
  });

  test('passes the hook the exposure, its own argument only, and reports nothing for a value or a resolved Promise', async () => {
    const report = jest.fn();
    const hook = jest.fn();
    callExposureHook(hook, EXPOSURE, report);
    callExposureHook(() => 'ignored', EXPOSURE, report);
    callExposureHook(() => Promise.resolve(), EXPOSURE, report);
    await flush();
    expect(hook.mock.calls).toEqual([[EXPOSURE]]);
    expect(report).toHaveBeenCalledTimes(0);
  });

  test('drops what report itself throws', () => {
    const report = jest.fn(() => {
      throw new Error('report boom');
    });
    expect(() => callExposureHook(throwingHook, EXPOSURE, report)).not.toThrow();
    expect(report).toHaveBeenCalledTimes(1);
  });
});

function throwingHook(): never {
  throw new Error('hook boom');
}

// ─── Through useExperiment ───────────────────────────────────────────────────

describe('onExposure through useExperiment', () => {
  function Probe({ experimentKey }: { experimentKey: string }) {
    const { variant } = useExperiment(experimentKey);
    return <Text testID={`variant-${experimentKey}`}>{variant ?? 'none'}</Text>;
  }

  test('calls onExposure once when useExperiment mounts, with the four values', async () => {
    const { requests } = fakeFetch();
    const { client, hook } = makeClient();
    const { getByTestId } = render(
      <ExperimentationProvider client={client} userId="u-1">
        <Probe experimentKey="exp-1" />
      </ExperimentationProvider>
    );
    await waitFor(() => expect(getByTestId('variant-exp-1').props.children).toBe('treatment'));
    await flush();
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook.mock.calls[0][0]).toStrictEqual(EXPOSURE);
  });

  test('two components using one experiment share one request and one call', async () => {
    const { requests } = fakeFetch();
    const { client, hook } = makeClient();
    const { getAllByTestId } = render(
      <ExperimentationProvider client={client} userId="u-1">
        <Probe experimentKey="exp-1" />
        <Probe experimentKey="exp-1" />
      </ExperimentationProvider>
    );
    await waitFor(() =>
      expect(getAllByTestId('variant-exp-1').map((node) => node.props.children)).toEqual(['treatment', 'treatment'])
    );
    await flush();
    expect(requests).toHaveLength(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });
});
