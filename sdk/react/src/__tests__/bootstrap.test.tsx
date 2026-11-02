/**
 * The Provider's `bootstrap` in the browser (#1518): the hooks start from the
 * server's answers and commit them first, hydrate server markup made with them
 * without a mismatch, then ask as they always do. A changed answer is applied
 * like a fetched one; a failed request keeps the bootstrapped answer (with
 * `error`), and a hook without an entry reports off as before. A bootstrap for
 * another user, of the wrong shape, or with a malformed entry is ignored as
 * specified, with one warning per reason per Provider. Exposures are counted.
 *
 * Frames are what a `useLayoutEffect` sees after each commit, so a value that
 * was committed and replaced before the browser painted still shows.
 *
 * The server render itself (no request, no warning) is in
 * bootstrap.ssr.test.tsx, under `@jest-environment node`.
 */
import '@testing-library/jest-dom';
import React, { StrictMode, useEffect, useLayoutEffect } from 'react';
import { act, render, renderHook } from '@testing-library/react';
import { hydrateRoot } from 'react-dom/client';
import { ExperimentationProvider } from '../context/ExperimentationProvider';
import { ServerClient } from '../client/ServerClient';
import { useExperiment } from '../hooks/useExperiment';
import { useFeatureFlag } from '../hooks/useFeatureFlag';
import { useMultipleFlags } from '../hooks/useMultipleFlags';
import type {
  ExperimentAssignResponse,
  ExperimentationBootstrap,
  FeatureFlagEvaluateResponse,
  SdkConfig,
  UserContext,
} from '../client/types';

// The Node build of the server renderer: the browser build needs a TextEncoder jsdom lacks.
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { renderToString } = require('react-dom/server.node') as typeof import('react-dom/server');

const config: SdkConfig = { apiKey: 'test-key', baseUrl: 'https://api.example.com' };
const user: UserContext = { userId: 'u-1' };

const flagOn: FeatureFlagEvaluateResponse = { key: 'flag-1', enabled: true, config: null, reason: 'rollout' };
const flagOff: FeatureFlagEvaluateResponse = { key: 'flag-1', enabled: false, config: null, reason: 'inactive' };
const treatment: ExperimentAssignResponse = {
  experiment_key: 'exp-1',
  user_id: 'u-1',
  variant_id: 'v-2',
  variant_name: 'treatment',
  is_control: false,
  configuration: { color: 'green' },
  assigned: true,
  reason: 'assigned',
};
const control: ExperimentAssignResponse = {
  ...treatment,
  variant_id: 'v-1',
  variant_name: 'control',
  is_control: true,
  configuration: null,
};
const bootstrap: ExperimentationBootstrap = {
  userId: 'u-1',
  flags: { 'flag-1': flagOn },
  experiments: { 'exp-1': treatment },
};

const WARNING_USER =
  "@controlvar/react-sdk: bootstrap not used: its userId is not the provider's user.userId. Pass the same user to ServerClient.bootstrap() and to the provider.";
const WARNING_SHAPE =
  '@controlvar/react-sdk: bootstrap not used: it must be an object with a userId string and optional flags and experiments objects, as ServerClient.bootstrap() returns.';
const warningEntries = (n: number, m: number) =>
  `@controlvar/react-sdk: bootstrap entries not used for ${n} flag(s) and ${m} experiment(s): a flags entry must be a flag evaluation response (enabled true or false) and an experiments entry an assignment response (with variant_name), as ServerClient.bootstrap() returns.`;

/** An answer the mock API gives with this status and no usable body. */
const failure = (status: number) => ({ __status: status });

/**
 * The API: each evaluate request answers `flags[key]`, each assign request
 * `experiments[key]` (a fresh copy each time); a missing key or a `failure()` is
 * an error status.
 */
function mockApi(flags: Record<string, unknown>, experiments: Record<string, unknown>): jest.Mock {
  const mock = jest.fn().mockImplementation((url: string, init?: { body?: string }) => {
    let answer: unknown;
    if (url.includes('/api/v1/feature-flags/evaluate/')) {
      answer = flags[decodeURIComponent(url.split('/evaluate/')[1].split('?')[0])];
    } else if (url.endsWith('/api/v1/tracking/assign')) {
      answer = experiments[JSON.parse(String(init?.body)).experiment_key];
    }
    const status = answer === undefined ? 500 : (answer as { __status?: number }).__status ?? 200;
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(JSON.parse(JSON.stringify(answer ?? {}))),
    });
  });
  global.fetch = mock;
  return mock;
}

const evaluations = (mock: jest.Mock, key = 'flag-1') =>
  mock.mock.calls.filter(([url]) => String(url).includes(`/feature-flags/evaluate/${key}?`)).length;
const assigns = (mock: jest.Mock, key?: string) =>
  mock.mock.calls.filter(
    ([url, init]) =>
      String(url).endsWith('/api/v1/tracking/assign') &&
      (key === undefined || JSON.parse(String(init?.body)).experiment_key === key)
  ).length;

const flush = () => new Promise(resolve => setTimeout(resolve, 0));
/** Consecutive equal frames as one: a re-commit of the same text is not a change on screen. */
const distinct = (list: string[]) => list.filter((frame, i) => i === 0 || frame !== list[i - 1]);

let frames: string[];
let requestsAtFirstCommit: number | null;
let api: jest.Mock;

function useFrame(text: string) {
  // useEffect when rendered as a server renders (no `window`): useLayoutEffect warns there.
  const useCommitEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect;
  useCommitEffect(() => {
    if (requestsAtFirstCommit === null) requestsAtFirstCommit = api.mock.calls.length;
    frames.push(text);
  });
}

function Banner() {
  const flag = useFeatureFlag('flag-1');
  const exp = useExperiment('exp-1');
  const text = `${flag.isEnabled ? 'FLAG-ON' : 'FLAG-OFF'}|${exp.variantName}`;
  useFrame(text);
  return <span>{text}</span>;
}

function FlagProbe({ flagKey = 'flag-1' }: { flagKey?: string }) {
  const flag = useFeatureFlag(flagKey);
  const text = `${flag.isEnabled ? 'FLAG-ON' : 'FLAG-OFF'}${flag.loading ? '|loading' : ''}${flag.error ? '|error' : ''}`;
  useFrame(text);
  return <span>{text}</span>;
}

function ExperimentProbe({ experimentKey = 'exp-1' }: { experimentKey?: string }) {
  const exp = useExperiment(experimentKey);
  const text = `${exp.variantName}${exp.loading ? '|loading' : ''}${exp.error ? '|error' : ''}`;
  useFrame(text);
  return <span>{text}</span>;
}

/**
 * The page: `children` under a Provider whose bootstrap is the shared one, or
 * the value given (an explicit `undefined` included), with `extra` config and
 * the user `who`.
 */
function tree(
  children: React.ReactNode,
  ...rest: [value?: unknown, extra?: Partial<SdkConfig>, who?: UserContext]
) {
  const given = rest.length >= 1 ? rest[0] : bootstrap;
  const extra = rest[1] ?? {};
  const who = rest[2] ?? user;
  return (
    <ExperimentationProvider config={{ ...config, ...extra }} user={who} bootstrap={given as ExperimentationBootstrap}>
      {children}
    </ExperimentationProvider>
  );
}

/** Server markup for `element`, rendered as a server renders it: with no `window`. */
function serverMarkup(element: React.ReactElement): string {
  const own = Object.getOwnPropertyDescriptor(globalThis, 'window')!;
  Object.defineProperty(globalThis, 'window', { value: undefined, configurable: true, writable: true });
  try {
    return renderToString(element);
  } finally {
    Object.defineProperty(globalThis, 'window', own);
  }
}

/** The console.error calls React makes for a hydration mismatch. */
function mismatches(spy: jest.SpyInstance): string[] {
  return spy.mock.calls
    .map(args => args.map(String).join(' '))
    .filter(line => /did not match|does not match|hydrat/i.test(line));
}

beforeEach(() => {
  frames = [];
  requestsAtFirstCommit = null;
  api = mockApi({ 'flag-1': flagOn }, { 'exp-1': treatment });
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('bootstrap: the first render', () => {
  it('commits the bootstrapped flag and variant first, with no request before the first commit', async () => {
    render(tree(<Banner />));
    expect(requestsAtFirstCommit).toBe(0);
    await act(flush);
    expect(distinct(frames)).toEqual(['FLAG-ON|treatment']);
    // ... and still asks, as without a bootstrap.
    expect(evaluations(api)).toBe(1);
    expect(assigns(api)).toBe(1);
  });

  it('starts each hook from its entry with loading: false, mapped as a fetched answer is', () => {
    global.fetch = jest.fn().mockReturnValue(new Promise(() => {})); // never answers
    const flag = renderHook(() => useFeatureFlag('flag-1'), { wrapper: ({ children }) => tree(children) });
    expect(flag.result.current).toEqual({
      flagKey: 'flag-1',
      variant: 'on',
      isEnabled: true,
      config: null,
      loading: false,
      error: null,
      reason: 'rollout',
    });
    const exp = renderHook(() => useExperiment('exp-1'), { wrapper: ({ children }) => tree(children) });
    expect(exp.result.current).toEqual({
      experimentKey: 'exp-1',
      variantKey: 'treatment',
      variantName: 'treatment',
      variantId: 'v-2',
      isControl: false,
      configuration: { color: 'green' },
      loading: false,
      error: null,
      assigned: true,
      reason: 'assigned',
    });
  });

  it('starts useMultipleFlags from the entries it has, and null for the others', () => {
    global.fetch = jest.fn().mockReturnValue(new Promise(() => {})); // never answers
    const { result } = renderHook(() => useMultipleFlags(['flag-1', 'flag-2']), {
      wrapper: ({ children }) => tree(children),
    });
    expect(result.current['flag-1']).toMatchObject({ isEnabled: true, loading: false, error: null });
    expect(result.current['flag-2']).toBeNull();
  });
});

describe('bootstrap: hydration', () => {
  it('hydrates server markup made with it with no mismatch, and commits the bootstrapped answers first', async () => {
    const html = serverMarkup(tree(<Banner />));
    expect(html).toBe('<span>FLAG-ON|treatment</span>');
    expect(api).toHaveBeenCalledTimes(0);
    frames = [];
    const error = jest.spyOn(console, 'error');
    const container = document.createElement('div');
    container.innerHTML = html;
    document.body.appendChild(container);
    await act(async () => {
      hydrateRoot(container, tree(<Banner />));
    });
    await act(flush);
    expect(mismatches(error)).toEqual([]);
    expect(requestsAtFirstCommit).toBe(0);
    expect(distinct(frames)).toEqual(['FLAG-ON|treatment']);
    expect(container.textContent).toBe('FLAG-ON|treatment');
    container.remove();
  });
});

describe('bootstrap: the refresh', () => {
  it('applies a refresh that answers differently as it applies a fetched answer', async () => {
    api = mockApi({ 'flag-1': flagOff }, {});
    render(tree(<FlagProbe />));
    await act(flush);
    expect(frames).toEqual(['FLAG-ON', 'FLAG-OFF']);
    expect(evaluations(api)).toBe(1);
  });

  it('applies an assignment refresh that answers differently as it applies a fetched answer', async () => {
    api = mockApi({}, { 'exp-1': control });
    render(tree(<ExperimentProbe />));
    await act(flush);
    expect(frames).toEqual(['treatment', 'control']);
    expect(assigns(api)).toBe(1);
  });

  it('keeps the bootstrapped answer, with error set, when its refresh fails', async () => {
    api = mockApi({ 'flag-1': failure(500) }, {});
    const { result } = renderHook(() => useFeatureFlag('flag-1'), { wrapper: ({ children }) => tree(children) });
    await act(flush);
    expect(result.current).toMatchObject({ isEnabled: true, variant: 'on', loading: false, reason: 'rollout' });
    expect(result.current.error).toEqual(new Error('API error: 500'));
    expect(evaluations(api)).toBe(1);
  });

  it('reports a flag without a bootstrap entry off, with error set, when its request fails', async () => {
    api = mockApi({ 'flag-2': failure(500) }, {});
    const { result } = renderHook(() => useFeatureFlag('flag-2'), { wrapper: ({ children }) => tree(children) });
    expect(result.current).toMatchObject({ isEnabled: false, loading: true });
    await act(flush);
    expect(result.current).toEqual({
      flagKey: 'flag-2',
      variant: null,
      isEnabled: false,
      config: null,
      loading: false,
      error: new Error('API error: 500'),
    });
  });

  it('keeps or turns off by whether the hook had an entry, in one tree whose requests all fail', async () => {
    api = mockApi({ 'flag-1': failure(503), 'flag-2': failure(503) }, { 'exp-1': failure(503), 'exp-2': failure(503) });
    const { container } = render(
      tree(
        <>
          <FlagProbe flagKey="flag-1" />|<FlagProbe flagKey="flag-2" />|<ExperimentProbe experimentKey="exp-1" />|
          <ExperimentProbe experimentKey="exp-2" />
        </>
      )
    );
    await act(flush);
    expect(container.textContent).toBe('FLAG-ON|error|FLAG-OFF|error|treatment|error|Control|error');
  });

  it('keeps the bootstrapped assignment, with error set, when its refresh fails', async () => {
    api = mockApi({}, { 'exp-1': failure(500) });
    const { result } = renderHook(() => useExperiment('exp-1'), { wrapper: ({ children }) => tree(children) });
    await act(flush);
    expect(result.current).toMatchObject({ variantKey: 'treatment', variantId: 'v-2', isControl: false, loading: false });
    expect(result.current.error).toEqual(new Error('API error: 500'));
  });

  it('returns the control defaults with error set for an experiment without an entry whose request fails', async () => {
    api = mockApi({}, { 'exp-2': failure(500) });
    const { result } = renderHook(() => useExperiment('exp-2'), { wrapper: ({ children }) => tree(children) });
    await act(flush);
    expect(result.current).toEqual({
      experimentKey: 'exp-2',
      variantKey: 'control',
      variantName: 'Control',
      variantId: null,
      isControl: true,
      configuration: null,
      loading: false,
      error: new Error('API error: 500'),
    });
  });

  it('useMultipleFlags keeps each bootstrapped answer with error set, and reports the others off', async () => {
    api = mockApi({ 'flag-1': failure(500), 'flag-2': failure(500) }, {});
    const { result } = renderHook(() => useMultipleFlags(['flag-1', 'flag-2']), {
      wrapper: ({ children }) => tree(children),
    });
    await act(flush);
    expect(result.current['flag-1']).toMatchObject({ isEnabled: true, error: new Error('API error: 500') });
    expect(result.current['flag-2']).toMatchObject({ isEnabled: false, error: new Error('API error: 500') });
  });

  it('keeps it under StrictMode, which runs the effect twice: one request, no loading frame', async () => {
    render(<StrictMode>{tree(<Banner />)}</StrictMode>);
    await act(flush);
    expect(distinct(frames)).toEqual(['FLAG-ON|treatment']);
    expect(evaluations(api)).toBe(1);
    expect(assigns(api)).toBe(1);
  });
});

describe('bootstrap: when it is not used', () => {
  // The server answers u-1 with the flag off and control, so a bootstrap used
  // when it should not be shows on screen.
  beforeEach(() => {
    api = mockApi({ 'flag-1': flagOff }, { 'exp-1': control });
  });

  it('ignores a bootstrap for another user: the hooks load and ask, as without one', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    render(tree(<Banner />, { ...bootstrap, userId: 'u-2' }));
    await act(flush);
    expect(distinct(frames)).toEqual(['FLAG-OFF|Control', 'FLAG-OFF|control']);
    expect(warn.mock.calls).toEqual([[WARNING_USER]]);
  });

  it.each([
    ['a string', 'u-1'],
    ['an array', [bootstrap]],
    ['a userId that is not a string', { ...bootstrap, userId: 1 }],
    ['no userId', { flags: bootstrap.flags }],
    ['flags that is an array', { ...bootstrap, flags: [flagOn] }],
    ['flags that is null', { ...bootstrap, flags: null }],
    ['experiments that is a string', { ...bootstrap, experiments: 'exp-1' }],
  ])('ignores a bootstrap of the wrong shape (%s), without throwing', async (_label, value) => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    render(tree(<Banner />, value));
    await act(flush);
    expect(distinct(frames)).toEqual(['FLAG-OFF|Control', 'FLAG-OFF|control']);
    expect(warn.mock.calls).toEqual([[WARNING_SHAPE]]);
  });

  it('uses the well-formed entries and skips a malformed one for its key alone', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    render(tree(<Banner />, { ...bootstrap, flags: { 'flag-1': { key: 'flag-1', isEnabled: true } } }));
    await act(flush);
    // The flag loads as without a bootstrap; the experiment starts from its entry.
    expect(distinct(frames)).toEqual(['FLAG-OFF|treatment', 'FLAG-OFF|control']);
    expect(warn.mock.calls).toEqual([[warningEntries(1, 0)]]);
  });

  it('loads a key the bootstrap does not have as without one, with no warning', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    render(tree(<Banner />, { userId: 'u-1', experiments: { 'exp-1': treatment } }));
    await act(flush);
    expect(distinct(frames)).toEqual(['FLAG-OFF|treatment', 'FLAG-OFF|control']);
    expect(warn).toHaveBeenCalledTimes(0);
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
  ])('takes %s as no bootstrap, with no warning', async (_label, value) => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    render(tree(<Banner />, value));
    await act(flush);
    expect(distinct(frames)).toEqual(['FLAG-OFF|Control', 'FLAG-OFF|control']);
    expect(warn).toHaveBeenCalledTimes(0);
  });
});

describe('bootstrap: warnings', () => {
  it('warns with the fixed text for malformed entries, counting flags and experiments', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    render(
      tree(<Banner />, {
        userId: 'u-1',
        flags: { 'flag-1': flagOn, 'flag-2': { enabled: 'yes' } },
        experiments: { 'exp-1': { variant: 'treatment' }, 'exp-2': null },
      })
    );
    await act(flush);
    expect(warn.mock.calls).toEqual([[warningEntries(1, 2)]]);
  });

  it('warns once per reason per Provider, under StrictMode and across re-renders', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    const page = () => (
      <StrictMode>
        {tree(<Banner />, { ...bootstrap, userId: 'u-2' })}
        {tree(<Banner />, { ...bootstrap, userId: 'u-2' })}
      </StrictMode>
    );
    const { rerender } = render(page());
    rerender(page());
    rerender(page());
    await act(flush);
    // Two Providers, one warning each.
    expect(warn.mock.calls).toEqual([[WARNING_USER], [WARNING_USER]]);
  });

  it('warns about nothing for a bootstrap it uses whole', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    render(tree(<Banner />));
    await act(flush);
    expect(warn).toHaveBeenCalledTimes(0);
  });

  it('warns about the output of getAll() passed as bootstrap.flags (warning 3)', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    api = mockApi({ 'flag-1': flagOn, 'flag-2': flagOff }, {});
    const evaluated = await new ServerClient(config).getAll(['flag-1', 'flag-2'], user);
    render(tree(<FlagProbe />, { userId: 'u-1', flags: evaluated }));
    await act(flush);
    expect(warn.mock.calls).toEqual([[warningEntries(2, 0)]]);
    expect(frames[0]).toBe('FLAG-OFF|loading');
  });
});

describe('bootstrap: exposures', () => {
  it('records one exposure on the server and one after hydration, and no more', async () => {
    const serverHook = jest.fn();
    const providerHook = jest.fn();
    const built = await new ServerClient({ ...config, onExposure: serverHook }).bootstrap(
      { flags: ['flag-1'], experiments: ['exp-1'] },
      user
    );
    expect([assigns(api), serverHook.mock.calls.length]).toEqual([1, 1]);
    const page = tree(<Banner />, JSON.parse(JSON.stringify(built)), { onExposure: providerHook });
    const container = document.createElement('div');
    container.innerHTML = serverMarkup(page);
    document.body.appendChild(container);
    expect([assigns(api), providerHook.mock.calls.length]).toEqual([1, 0]);
    await act(async () => {
      hydrateRoot(container, page);
    });
    await act(flush);
    expect(assigns(api)).toBe(2);
    expect(serverHook).toHaveBeenCalledTimes(1);
    expect(providerHook).toHaveBeenCalledTimes(1);
    expect(distinct(frames)).toEqual(['FLAG-ON|treatment']);
    container.remove();
  });

  it('calls the Provider onExposure once after hydration under StrictMode too', async () => {
    const hook = jest.fn();
    render(<StrictMode>{tree(<Banner />, bootstrap, { onExposure: hook })}</StrictMode>);
    await act(flush);
    expect(assigns(api)).toBe(1);
    expect(hook).toHaveBeenCalledTimes(1);
  });

  it('sends no assign request for a bootstrapped experiment no hook renders', async () => {
    const hook = jest.fn();
    render(
      tree(<FlagProbe />, { ...bootstrap, experiments: { 'exp-1': treatment, 'exp-2': treatment } }, { onExposure: hook })
    );
    await act(flush);
    expect(assigns(api)).toBe(0);
    expect(hook).toHaveBeenCalledTimes(0);
  });

  it('ServerClient.bootstrap() assigns each experiment once and calls its onExposure once per assigned: true answer', async () => {
    api = mockApi({}, { 'exp-1': treatment, 'exp-2': { ...control, assigned: false, reason: 'holdout' } });
    const hook = jest.fn();
    const built = await new ServerClient({ ...config, onExposure: hook }).bootstrap(
      { experiments: ['exp-1', 'exp-2'] },
      user
    );
    expect(Object.keys(built.experiments ?? {})).toEqual(['exp-1', 'exp-2']);
    expect(assigns(api, 'exp-1')).toBe(1);
    expect(assigns(api, 'exp-2')).toBe(1);
    expect(hook.mock.calls).toEqual([
      [{ experimentKey: 'exp-1', userId: 'u-1', variantId: 'v-2', variantName: 'treatment' }],
    ]);
  });

  it('ServerClient.bootstrap() assigns again on each call: it reads no cache', async () => {
    const hook = jest.fn();
    const server = new ServerClient({ ...config, onExposure: hook });
    await server.bootstrap({ experiments: ['exp-1'] }, user);
    await server.bootstrap({ experiments: ['exp-1'] }, user);
    expect(assigns(api)).toBe(2);
    expect(hook).toHaveBeenCalledTimes(2);
  });
});

describe('bootstrap: after the first render', () => {
  it('starts loading and asks again when the user changes, as without one', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    const { rerender } = render(tree(<FlagProbe />));
    await act(flush);
    api = mockApi({ 'flag-1': flagOff }, {});
    rerender(tree(<FlagProbe />, bootstrap, {}, { userId: 'u-2' }));
    await act(flush);
    expect(distinct(frames)).toEqual(['FLAG-ON', 'FLAG-OFF|loading', 'FLAG-OFF']);
    expect(evaluations(api)).toBe(1);
    // The bootstrap still passed is now another user's: not used, and said once.
    expect(warn.mock.calls).toEqual([[WARNING_USER]]);
  });

  it('starts loading and asks again when the key changes, as without one', async () => {
    const { rerender } = render(tree(<FlagProbe flagKey="flag-1" />));
    await act(flush);
    api = mockApi({ 'flag-2': flagOn }, {});
    rerender(tree(<FlagProbe flagKey="flag-2" />));
    await act(flush);
    expect(distinct(frames)).toEqual(['FLAG-ON', 'FLAG-OFF|loading', 'FLAG-ON']);
    expect(evaluations(api, 'flag-2')).toBe(1);
  });

  it('a hook already mounted ignores a new bootstrap prop, and a hook that mounts later reads it', async () => {
    global.fetch = jest.fn().mockReturnValue(new Promise(() => {})); // never answers
    const later = { userId: 'u-1', flags: { 'flag-1': flagOff } };
    const page = (value: unknown, second: boolean) =>
      tree(
        <>
          <FlagProbe key="first" />
          {second && <FlagProbe key="second" />}
        </>,
        value
      );
    const { rerender, container } = render(page(bootstrap, false));
    rerender(page(later, false));
    expect(container.textContent).toBe('FLAG-ON');
    rerender(page(later, true));
    expect(container.textContent).toBe('FLAG-ONFLAG-OFF');
  });

  it('asks nothing again when the parent re-renders with an equal bootstrap object', async () => {
    const { rerender } = render(tree(<Banner />, { ...bootstrap }));
    await act(flush);
    rerender(tree(<Banner />, { ...bootstrap }));
    rerender(tree(<Banner />, JSON.parse(JSON.stringify(bootstrap))));
    await act(flush);
    expect(evaluations(api)).toBe(1);
    expect(assigns(api)).toBe(1);
    expect(distinct(frames)).toEqual(['FLAG-ON|treatment']);
  });
});
