/**
 * The exposure hook, `onExposure` (#1499): called once for each answer the
 * client stores that the server sent with `assigned: true`, with a new object
 * holding four values; never for a cached answer, the second of concurrent
 * first calls, a failed call, an `assigned: false` answer or a flag; nothing
 * it does reaches the caller or changes the answer.
 *
 * The server render's side (no request, no call) is in exposure.ssr.test.tsx,
 * which runs under `@jest-environment node`.
 */
import '@testing-library/jest-dom';
import React, { StrictMode } from 'react';
import { act, render, screen } from '@testing-library/react';
import { hydrateRoot } from 'react-dom/client';
import { ExperimentationClient } from '../client/ExperimentationClient';
import { ServerClient } from '../client/ServerClient';
import {
  EXPOSURE_ERROR_PREFIX,
  ExposureHookError,
  callExposureHook,
  dropExposureError,
  exposureError,
} from '../client/exposure';
import { ExperimentationProvider } from '../context/ExperimentationProvider';
import { useExperiment } from '../hooks/useExperiment';
import type { ExperimentAssignResponse, Exposure, SdkConfig, UserContext } from '../client/types';

const ASSIGN_URL = 'https://api.example.com/api/v1/tracking/assign';
const BATCH_URL = 'https://api.example.com/api/v1/tracking/batch';
const config: SdkConfig = { apiKey: 'test-key', baseUrl: 'https://api.example.com' };
const user: UserContext = { userId: 'u-1', attributes: { plan: 'pro' } };

/** The shared answer: the server enrols u-1 in exp-1 with 'treatment' (the default is 'control'). */
const enrolled: ExperimentAssignResponse = {
  experiment_key: 'exp-1',
  user_id: 'u-1',
  variant_id: 'v-2',
  variant_name: 'treatment',
  is_control: false,
  configuration: { color: 'green' },
  assigned: true,
  reason: 'assigned',
};
const EXPOSURE: Exposure = {
  experimentKey: 'exp-1',
  userId: 'u-1',
  variantId: 'v-2',
  variantName: 'treatment',
};

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

/** Every request answers `body`; the answer is rebuilt per request so no test shares an object. */
function mockFetch(body: unknown = enrolled, status = 200): jest.Mock {
  const mock = jest.fn().mockImplementation(() =>
    Promise.resolve(jsonResponse(JSON.parse(JSON.stringify(body)), status))
  );
  global.fetch = mock;
  return mock;
}

/** A fetch that answers only when `release()` is called. */
function deferredFetch(body: unknown = enrolled) {
  const pending: Array<() => void> = [];
  const mock = jest.fn().mockImplementation(
    () => new Promise(resolve => pending.push(() => resolve(jsonResponse(body))))
  );
  global.fetch = mock;
  return {
    mock,
    release: () => {
      while (pending.length) pending.shift()!();
    },
  };
}

/** The assign requests the mock received. */
const assigns = (mock: jest.Mock) => mock.mock.calls.filter(([url]) => url === ASSIGN_URL).length;

/** Lets every pending promise, and a timer queued behind them, run. */
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── ExperimentationClient ───────────────────────────────────────────────────

describe('onExposure on the client', () => {
  it('is called once for an assigned: true answer, with exactly the four values', async () => {
    const fetchMock = mockFetch();
    const hook = jest.fn();
    const client = new ExperimentationClient({ ...config, onExposure: hook });

    const answer = await client.assignExperiment(user, 'exp-1');

    expect(answer.variantKey).toBe('treatment');
    expect(assigns(fetchMock)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook.mock.calls[0]).toHaveLength(1);
    expect(hook.mock.calls[0][0]).toStrictEqual(EXPOSURE);
  });

  it('passes a null variantId when the server sends none', async () => {
    mockFetch({ ...enrolled, variant_id: undefined });
    const hook = jest.fn();
    await new ExperimentationClient({ ...config, onExposure: hook }).assignExperiment(user, 'exp-1');
    expect(hook.mock.calls[0][0]).toStrictEqual({ ...EXPOSURE, variantId: null });
  });

  it('is called once per user and experiment', async () => {
    const fetchMock = jest.fn().mockImplementation((_url: string, init: { body: string }) => {
      const sent = JSON.parse(init.body);
      return Promise.resolve(
        jsonResponse({ ...enrolled, experiment_key: sent.experiment_key, user_id: sent.user_id })
      );
    });
    global.fetch = fetchMock;
    const hook = jest.fn();
    const client = new ExperimentationClient({ ...config, onExposure: hook });

    await client.assignExperiment({ userId: 'u-1' }, 'exp-1');
    await client.assignExperiment({ userId: 'u-2' }, 'exp-1');
    await client.assignExperiment({ userId: 'u-1' }, 'exp-2');
    await client.assignExperiment({ userId: 'u-1' }, 'exp-1');

    expect(assigns(fetchMock)).toBe(3);
    expect(hook.mock.calls.map(([e]) => [e.userId, e.experimentKey])).toEqual([
      ['u-1', 'exp-1'],
      ['u-2', 'exp-1'],
      ['u-1', 'exp-2'],
    ]);
  });

  it('is not called for a cached answer, however often it is asked for', async () => {
    const fetchMock = mockFetch();
    const hook = jest.fn();
    const client = new ExperimentationClient({ ...config, onExposure: hook });

    await client.assignExperiment(user, 'exp-1');
    await client.assignExperiment(user, 'exp-1');
    await client.assignExperiment({ userId: 'u-1' }, 'exp-1');
    await client.assignExperiment(user, 'exp-1');
    await flush();

    expect(assigns(fetchMock)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  it('is called once for concurrent first calls that share one request', async () => {
    const { mock, release } = deferredFetch();
    const hook = jest.fn();
    const client = new ExperimentationClient({ ...config, onExposure: hook });

    const calls = [
      client.assignExperiment(user, 'exp-1'),
      client.assignExperiment(user, 'exp-1'),
      client.assignExperiment(user, 'exp-1'),
    ];
    expect(assigns(mock)).toBe(1);
    release();
    const answers = await Promise.all(calls);
    await flush();

    expect(answers.map(a => a.variantKey)).toEqual(['treatment', 'treatment', 'treatment']);
    expect(assigns(mock)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  it.each(['holdout', 'mutual_exclusion', 'targeting', 'draft', 'paused', 'completed', 'archived'])(
    'is not called for an assigned: false answer (%s), and the answer is unchanged',
    async reason => {
      const notEnrolled = {
        ...enrolled,
        variant_id: 'v-1',
        variant_name: 'control',
        is_control: true,
        assigned: false,
        reason,
      };
      const fetchMock = mockFetch(notEnrolled);
      const hook = jest.fn();
      const answer = await new ExperimentationClient({ ...config, onExposure: hook }).assignExperiment(
        user,
        'exp-1'
      );
      await flush();

      expect(answer).toMatchObject({ variantKey: 'control', isControl: true, assigned: false, reason });
      expect(assigns(fetchMock)).toBe(1);
      expect(hook).toHaveBeenCalledTimes(0);
    }
  );

  it.each([
    ['missing', undefined],
    ['null', null],
    ['the string "true"', 'true'],
    ['the number 1', 1],
  ])('is not called when assigned is %s, and the answer is still returned', async (_name, value) => {
    const fetchMock = mockFetch({ ...enrolled, assigned: value });
    const hook = jest.fn();
    const answer = await new ExperimentationClient({ ...config, onExposure: hook }).assignExperiment(
      user,
      'exp-1'
    );
    await flush();

    expect(answer.variantKey).toBe('treatment');
    expect(assigns(fetchMock)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(0);
  });

  it('is not called for an answer without a variant name', async () => {
    mockFetch({ ...enrolled, variant_name: undefined });
    const hook = jest.fn();
    await new ExperimentationClient({ ...config, onExposure: hook }).assignExperiment(user, 'exp-1');
    await flush();
    expect(hook).toHaveBeenCalledTimes(0);
  });

  it.each([
    ['a 404', () => Promise.resolve(jsonResponse({ detail: 'Not Found' }, 404))],
    ['a 500', () => Promise.resolve(jsonResponse({ detail: 'boom' }, 500))],
    ['a network error', () => Promise.reject(new TypeError('Failed to fetch'))],
    ['invalid JSON', () => Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new SyntaxError('bad')) })],
  ])('is not called when the call fails (%s)', async (_name, answer) => {
    global.fetch = jest.fn().mockImplementation(answer);
    const hook = jest.fn();
    await expect(
      new ExperimentationClient({ ...config, onExposure: hook }).assignExperiment(user, 'exp-1')
    ).rejects.toThrow();
    await flush();
    expect(hook).toHaveBeenCalledTimes(0);
  });

  it('is not called when the call times out', async () => {
    global.fetch = jest.fn().mockImplementation(
      (_url: string, init: { signal: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener('abort', () => reject(new Error('aborted')));
        })
    );
    const hook = jest.fn();
    await expect(
      new ExperimentationClient({ ...config, timeoutMs: 5, onExposure: hook }).assignExperiment(user, 'exp-1')
    ).rejects.toThrow('aborted');
    await flush();
    expect(hook).toHaveBeenCalledTimes(0);
  });

  it('is never called for a flag', async () => {
    const fetchMock = mockFetch({ key: 'f', enabled: true, config: null, reason: 'rollout', assigned: true });
    const hook = jest.fn();
    const client = new ExperimentationClient({ ...config, onExposure: hook });
    await client.evaluateFeatureFlagDetailed(user, 'f');
    await client.evaluateFeatureFlag({ userId: 'u-2' }, 'f');
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(hook).toHaveBeenCalledTimes(0);
  });

  describe('is called again when the client asks the server again', () => {
    const T0 = 1_700_000_000_000;
    const TTL = 60_000;

    it('after the cache lifetime (one millisecond past it, never at the boundary)', async () => {
      const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
      const fetchMock = mockFetch();
      const hook = jest.fn();
      const client = new ExperimentationClient({ ...config, cacheTtlMs: TTL, onExposure: hook });

      await client.assignExperiment(user, 'exp-1');
      now.mockReturnValue(T0 + TTL);
      await client.assignExperiment(user, 'exp-1');
      expect([assigns(fetchMock), hook.mock.calls.length]).toEqual([1, 1]);

      now.mockReturnValue(T0 + TTL + 1);
      await client.assignExperiment(user, 'exp-1');
      expect([assigns(fetchMock), hook.mock.calls.length]).toEqual([2, 2]);
    });

    it('after clearCache()', async () => {
      const fetchMock = mockFetch();
      const hook = jest.fn();
      const client = new ExperimentationClient({ ...config, onExposure: hook });
      await client.assignExperiment(user, 'exp-1');
      client.clearCache();
      await client.assignExperiment(user, 'exp-1');
      expect([assigns(fetchMock), hook.mock.calls.length]).toEqual([2, 2]);
    });

    it('in a new client', async () => {
      const fetchMock = mockFetch();
      const hook = jest.fn();
      await new ExperimentationClient({ ...config, onExposure: hook }).assignExperiment(user, 'exp-1');
      await new ExperimentationClient({ ...config, onExposure: hook }).assignExperiment(user, 'exp-1');
      expect([assigns(fetchMock), hook.mock.calls.length]).toEqual([2, 2]);
    });
  });

  it('runs before assignExperiment resolves', async () => {
    mockFetch();
    const order: string[] = [];
    const client = new ExperimentationClient({ ...config, onExposure: () => order.push('hook') });
    await client.assignExperiment(user, 'exp-1').then(() => order.push('returned'));
    expect(order).toEqual(['hook', 'returned']);
  });

  it('runs last: after the answer is cached and recorded for a key-less trackEvent', async () => {
    const fetchMock = jest.fn().mockImplementation((url: string) =>
      Promise.resolve(jsonResponse(url === ASSIGN_URL ? enrolled : {}))
    );
    global.fetch = fetchMock;
    let again: Promise<unknown> | undefined;
    const client: ExperimentationClient = new ExperimentationClient({
      ...config,
      onExposure: () => {
        again = client.assignExperiment(user, 'exp-1');
        void client.trackEvent('u-1', 'page_view');
      },
    });

    await client.assignExperiment(user, 'exp-1');
    await expect(again).resolves.toMatchObject({ variantKey: 'treatment' });
    await flush();

    expect(assigns(fetchMock)).toBe(1);
    const batch = fetchMock.mock.calls.find(([url]) => url === BATCH_URL);
    expect(batch).toBeDefined();
    expect(JSON.parse(batch![1].body).events.map((e: { experiment_key?: string }) => e.experiment_key)).toEqual([
      'exp-1',
    ]);
  });

  it('sends nothing because of the hook', async () => {
    const fetchMock = mockFetch();
    await new ExperimentationClient({ ...config, onExposure: () => undefined }).assignExperiment(user, 'exp-1');
    await flush();
    expect(fetchMock.mock.calls.map(([url, init]) => `${init.method} ${url}`)).toEqual([`POST ${ASSIGN_URL}`]);
  });

  it('receives a new object that does not alias the cached answer', async () => {
    mockFetch();
    const seen: Exposure[] = [];
    const client = new ExperimentationClient({
      ...config,
      onExposure: e => {
        seen.push(e);
        (e as { variantName: string }).variantName = 'mutated';
      },
    });
    await client.assignExperiment(user, 'exp-1');
    const cached = await client.assignExperiment(user, 'exp-1');
    expect(cached.variantName).toBe('treatment');
    expect(cached.variantKey).toBe('treatment');
    client.clearCache();
    await client.assignExperiment(user, 'exp-1');
    expect(seen).toHaveLength(2);
    expect(seen[0]).not.toBe(seen[1]);
  });

  it('has four readonly fields', () => {
    const e: Exposure = { ...EXPOSURE };
    // @ts-expect-error experimentKey is readonly
    e.experimentKey = 'x';
    // @ts-expect-error userId is readonly
    e.userId = 'x';
    // @ts-expect-error variantId is readonly
    e.variantId = 'x';
    // @ts-expect-error variantName is readonly
    e.variantName = 'x';
    expect(Object.keys(EXPOSURE)).toEqual(['experimentKey', 'userId', 'variantId', 'variantName']);
  });
});

// ─── What the hook does never reaches the caller ─────────────────────────────

describe('a failing onExposure', () => {
  const boom = new Error('hook boom');

  it('that throws leaves the answer unchanged and cached', async () => {
    const fetchMock = mockFetch();
    const hook = jest.fn(() => {
      throw boom;
    });
    const client = new ExperimentationClient({ ...config, onExposure: hook });

    await expect(client.assignExperiment(user, 'exp-1')).resolves.toMatchObject({ variantKey: 'treatment' });
    await expect(client.assignExperiment(user, 'exp-1')).resolves.toMatchObject({ variantKey: 'treatment' });
    expect(assigns(fetchMock)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  it('that returns a rejected Promise leaves the answer unchanged', async () => {
    mockFetch();
    const client = new ExperimentationClient({ ...config, onExposure: () => Promise.reject(boom) });
    await expect(client.assignExperiment(user, 'exp-1')).resolves.toMatchObject({ variantKey: 'treatment' });
    await flush();
  });

  it('leaves ServerClient answering, with no error', async () => {
    mockFetch();
    const server = new ServerClient({
      ...config,
      onExposure: () => {
        throw boom;
      },
    });
    const answer = await server.assignExperiment('exp-1', user);
    expect(answer).toMatchObject({ variantKey: 'treatment', error: null });
  });

  it('leaves useExperiment with the answer and no error', async () => {
    mockFetch();
    function Variant() {
      const { variantKey, error, loading } = useExperiment('exp-1');
      return <span data-testid="v">{loading ? 'loading' : `${variantKey}:${error ? 'error' : 'ok'}`}</span>;
    }
    render(
      <ExperimentationProvider
        config={{
          ...config,
          onExposure: () => {
            throw boom;
          },
        }}
        user={user}
      >
        <Variant />
      </ExperimentationProvider>
    );
    expect(await screen.findByText('treatment:ok')).toBeInTheDocument();
  });
});

describe('the exposure guard', () => {
  it('builds the hook error: code HOOK_ERROR, the message prefix, and the cause', () => {
    const thrown = new Error('hook boom');
    const error = exposureError(thrown);
    expect(error).toBeInstanceOf(ExposureHookError);
    expect(error).toBeInstanceOf(Error);
    expect(error.code).toBe('HOOK_ERROR');
    expect(error.message).toBe('onExposure failed: hook boom');
    expect(error.message.startsWith(EXPOSURE_ERROR_PREFIX)).toBe(true);
    expect(EXPOSURE_ERROR_PREFIX).toBe('onExposure failed: ');
    expect(error.cause).toBe(thrown);
    expect(exposureError('a string').message).toBe('onExposure failed: a string');
    expect(exposureError(Object.create(null)).message).toBe(
      'onExposure failed: it threw a value that cannot be turned into a string'
    );
  });

  it('reports a throw, once, as the hook error', () => {
    const thrown = new Error('sync boom');
    const report = jest.fn();
    callExposureHook(
      () => {
        throw thrown;
      },
      EXPOSURE,
      report
    );
    expect(report).toHaveBeenCalledTimes(1);
    expect(report.mock.calls[0][0]).toMatchObject({ code: 'HOOK_ERROR', message: 'onExposure failed: sync boom' });
    expect(report.mock.calls[0][0].cause).toBe(thrown);
  });

  it('reports a rejection, once, as the hook error, without awaiting it', async () => {
    const thrown = new Error('async boom');
    const report = jest.fn();
    callExposureHook(() => Promise.reject(thrown), EXPOSURE, report);
    expect(report).toHaveBeenCalledTimes(0);
    await flush();
    expect(report).toHaveBeenCalledTimes(1);
    expect(report.mock.calls[0][0]).toBeInstanceOf(ExposureHookError);
    expect(report.mock.calls[0][0].message).toBe('onExposure failed: async boom');
    expect(report.mock.calls[0][0].cause).toBe(thrown);
  });

  it('reports a thenable whose then throws, or whose then getter throws', () => {
    const report = jest.fn();
    callExposureHook(
      () => ({
        then() {
          throw new Error('then boom');
        },
      }),
      EXPOSURE,
      report
    );
    callExposureHook(
      () =>
        Object.defineProperty({}, 'then', {
          get() {
            throw new Error('getter boom');
          },
        }),
      EXPOSURE,
      report
    );
    expect(report.mock.calls.map(([e]) => e.message)).toEqual([
      'onExposure failed: then boom',
      'onExposure failed: getter boom',
    ]);
  });

  it('reports nothing for a hook that returns, or returns a resolved Promise', async () => {
    const report = jest.fn();
    callExposureHook(() => undefined, EXPOSURE, report);
    callExposureHook(() => 'a value', EXPOSURE, report);
    callExposureHook(() => Promise.resolve(), EXPOSURE, report);
    await flush();
    expect(report).toHaveBeenCalledTimes(0);
  });

  it('drops what report throws', async () => {
    const report = jest.fn(() => {
      throw new Error('report boom');
    });
    expect(() =>
      callExposureHook(
        () => {
          throw new Error('x');
        },
        EXPOSURE,
        report
      )
    ).not.toThrow();
    callExposureHook(() => Promise.reject(new Error('y')), EXPOSURE, report);
    await flush();
    expect(report).toHaveBeenCalledTimes(2);
  });

  it('is given the dropping report by the clients: this SDK reports the failure nowhere', () => {
    expect(dropExposureError(exposureError(new Error('x')))).toBeUndefined();
  });
});

// ─── Refusals ────────────────────────────────────────────────────────────────

describe('a value that is not a function', () => {
  const bad: unknown[] = ['x', 1, true, {}, []];

  it.each(bad)('is refused by the client and ServerClient: %p', value => {
    const withHook = { ...config, onExposure: value } as unknown as SdkConfig;
    expect(() => new ExperimentationClient(withHook)).toThrow(new Error('onExposure must be a function'));
    expect(() => new ServerClient(withHook)).toThrow(new Error('onExposure must be a function'));
  });

  it.each(bad)('is refused by the Provider when it renders: %p', value => {
    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => undefined);
    const withHook = { ...config, onExposure: value } as unknown as SdkConfig;
    expect(() =>
      render(
        <ExperimentationProvider config={withHook} user={user}>
          <span>child</span>
        </ExperimentationProvider>
      )
    ).toThrow(new Error('onExposure must be a function'));
    consoleError.mockRestore();
  });

  it('null and undefined set none', async () => {
    mockFetch();
    for (const onExposure of [null, undefined]) {
      await expect(
        new ExperimentationClient({ ...config, onExposure }).assignExperiment(user, 'exp-1')
      ).resolves.toMatchObject({ variantKey: 'treatment' });
      expect(() => new ServerClient({ ...config, onExposure })).not.toThrow();
      expect(() =>
        render(
          <ExperimentationProvider config={{ ...config, onExposure }} user={user}>
            <span>child</span>
          </ExperimentationProvider>
        )
      ).not.toThrow();
    }
  });
});

// ─── ServerClient ────────────────────────────────────────────────────────────

describe('ServerClient', () => {
  it('calls its own onExposure once for two assignments of one user', async () => {
    const fetchMock = mockFetch();
    const hook = jest.fn();
    const server = new ServerClient({ ...config, onExposure: hook });
    await server.assignExperiment('exp-1', user);
    await server.assignExperiment('exp-1', user);
    expect(assigns(fetchMock)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook.mock.calls[0][0]).toStrictEqual(EXPOSURE);
  });

  it('never calls the onExposure of a Provider', async () => {
    mockFetch();
    const providerHook = jest.fn();
    render(
      <ExperimentationProvider config={{ ...config, onExposure: providerHook }} user={user}>
        <span>child</span>
      </ExperimentationProvider>
    );
    await new ServerClient(config).assignExperiment('exp-1', user);
    await flush();
    expect(providerHook).toHaveBeenCalledTimes(0);
  });
});

// ─── The Provider ────────────────────────────────────────────────────────────

function Variant({ experimentKey = 'exp-1' }: { experimentKey?: string }) {
  const { variantKey } = useExperiment(experimentKey);
  return <span>{variantKey}</span>;
}

describe('the Provider', () => {
  it('calls onExposure once when useExperiment mounts, with the four values', async () => {
    const fetchMock = mockFetch();
    const hook = jest.fn();
    render(
      <ExperimentationProvider config={{ ...config, onExposure: hook }} user={user}>
        <Variant />
        <Variant />
      </ExperimentationProvider>
    );
    expect(await screen.findAllByText('treatment')).toHaveLength(2);
    await flush();
    expect(assigns(fetchMock)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook.mock.calls[0][0]).toStrictEqual(EXPOSURE);
  });

  it('calls it once under StrictMode, which runs the effect twice', async () => {
    const fetchMock = mockFetch();
    const hook = jest.fn();
    const assign = jest.spyOn(ExperimentationClient.prototype, 'assignExperiment');
    render(
      <StrictMode>
        <ExperimentationProvider config={{ ...config, onExposure: hook }} user={user}>
          <Variant />
        </ExperimentationProvider>
      </StrictMode>
    );
    expect(await screen.findByText('treatment')).toBeInTheDocument();
    await flush();
    // The positive control: the effect really ran twice (jest runs React's development build).
    expect(assign).toHaveBeenCalledTimes(2);
    expect(assigns(fetchMock)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  it('calls it once after hydrating server markup', async () => {
    const fetchMock = mockFetch();
    const hook = jest.fn();
    const container = document.createElement('div');
    // What renderToString gives for this tree (exposure.ssr.test.tsx pins it).
    container.innerHTML = '<span>control</span>';
    document.body.appendChild(container);
    await act(async () => {
      hydrateRoot(
        container,
        <ExperimentationProvider config={{ ...config, onExposure: hook }} user={user}>
          <Variant />
        </ExperimentationProvider>
      );
    });
    await act(flush);
    expect(container.textContent).toBe('treatment');
    expect(assigns(fetchMock)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(1);
    container.remove();
  });

  it('keeps the client when onExposure changes, and calls the latest function', async () => {
    const { mock, release } = deferredFetch();
    const first = jest.fn();
    const second = jest.fn();
    const tree = (hook: (e: Exposure) => void) => (
      <ExperimentationProvider config={{ ...config, onExposure: hook }} user={user}>
        <Variant />
      </ExperimentationProvider>
    );
    const { rerender } = render(tree(e => first(e)));
    rerender(tree(e => second(e)));
    await act(async () => {
      release();
      await flush();
    });
    expect(screen.getByText('treatment')).toBeInTheDocument();
    expect(assigns(mock)).toBe(1);
    expect(first).toHaveBeenCalledTimes(0);
    expect(second).toHaveBeenCalledTimes(1);

    rerender(tree(e => first(e)));
    await act(flush);
    expect(assigns(mock)).toBe(1);
    expect(first).toHaveBeenCalledTimes(0);
  });

  it('passes on what onExposure returns, so a Promise it returns has its rejection handled', async () => {
    mockFetch();
    const then = jest.fn();
    const hook = jest.fn(() => ({ then }));
    render(
      <ExperimentationProvider config={{ ...config, onExposure: hook }} user={user}>
        <Variant />
      </ExperimentationProvider>
    );
    expect(await screen.findByText('treatment')).toBeInTheDocument();
    await flush();
    expect(hook).toHaveBeenCalledTimes(1);
    expect(then).toHaveBeenCalledTimes(1);
    expect(then).toHaveBeenCalledWith(undefined, expect.any(Function));
  });

  it('leaves the answer unchanged when onExposure returns a rejected Promise', async () => {
    mockFetch();
    const handled = jest.fn();
    const hook = () => {
      const rejected = Promise.reject(new Error('async boom'));
      const originalThen = rejected.then.bind(rejected);
      // Observe the handler the guard attaches, then let it handle the rejection.
      Object.defineProperty(rejected, 'then', {
        value: (ok: undefined, fail: (reason: unknown) => void) => {
          handled(fail);
          return originalThen(ok, fail);
        },
      });
      return rejected;
    };
    render(
      <ExperimentationProvider config={{ ...config, onExposure: hook }} user={user}>
        <Variant />
      </ExperimentationProvider>
    );
    expect(await screen.findByText('treatment')).toBeInTheDocument();
    await flush();
    expect(handled).toHaveBeenCalledTimes(1);
  });
});
