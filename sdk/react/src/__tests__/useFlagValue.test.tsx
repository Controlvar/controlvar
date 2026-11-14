/**
 * `useFlagValue` (#1810): the value a string, number or JSON flag serves the Provider's user.
 * Every answer in tests/sdk-contract/value-vectors.json is in valueVectors.test.ts; these tests
 * pin the hook's own behaviour: the default while loading, a bootstrapped value at the first
 * render, the one request it shares with `useFeatureFlag`, a copy of a JSON value per hook, the
 * default read at render (a new inline default asks nothing again), and a failed request answered
 * with the default and an `error`, never thrown.
 */
import '@testing-library/jest-dom';
import React, { useLayoutEffect } from 'react';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import { ExperimentationProvider } from '../context/ExperimentationProvider';
import { ExperimentationClient } from '../client/ExperimentationClient';
import { FlagValueError } from '../client/flagValue';
import { useFeatureFlag } from '../hooks/useFeatureFlag';
import { useFlagValue } from '../hooks/useFlagValue';
import * as root from '../index';
import type { ExperimentationBootstrap, FeatureFlagEvaluation, SdkConfig, UserContext } from '../client/types';

// The Node build of the server renderer: the browser build needs a TextEncoder jsdom lacks.
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { renderToString } = require('react-dom/server.node') as typeof import('react-dom/server');

const config: SdkConfig = { apiKey: 'test-key', baseUrl: 'https://api.example.com' };
const user: UserContext = { userId: 'u-1' };

// Bodies as the evaluate route sends them.
const copyOn = { key: 'copy', enabled: true, value: 'on-copy', value_type: 'string', config: null, reason: 'rollout' };
const copyOff = { ...copyOn, enabled: false, value: 'off-copy' };
const limitOn = { key: 'limit', enabled: true, value: 10, value_type: 'number', config: null, reason: 'rollout' };
const themeOn = {
  key: 'theme',
  enabled: true,
  value: { color: 'blue', sizes: [1, 2] },
  value_type: 'json',
  config: null,
  reason: 'rollout',
};
/** What a server from before flag values sends: neither `value` nor `value_type`. */
const beforeValues = { key: 'copy', enabled: true, config: null, reason: 'rollout' };

/** An answer the mock API gives with this status. */
const failure = (status: number) => ({ __status: status });

/**
 * The API: each evaluate request answers `flags[key]` as JSON bytes (parsed afresh each time),
 * a `failure()` with its status, and a key it does not have with the route's 404.
 */
function mockApi(flags: Record<string, unknown>): jest.Mock {
  const mock = jest.fn().mockImplementation((url: string) => {
    const key = decodeURIComponent(String(url).split('/evaluate/')[1].split('?')[0]);
    const answer = flags[key];
    const status = answer === undefined ? 404 : (answer as { __status?: number }).__status ?? 200;
    const body = status === 200 ? answer : { detail: `Feature flag with key '${key}' not found` };
    const text = JSON.stringify(body);
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(JSON.parse(text)),
    });
  });
  global.fetch = mock;
  return mock;
}

function wrapperFor(extra: Partial<SdkConfig> = {}, bootstrap?: ExperimentationBootstrap) {
  const merged = { ...config, ...extra };
  return ({ children }: { children: React.ReactNode }) => (
    <ExperimentationProvider config={merged} user={user} bootstrap={bootstrap}>
      {children}
    </ExperimentationProvider>
  );
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

let frames: string[];
let requestsAtFirstCommit: number | null;
let api: jest.Mock;

/** One string flag's value as text, recorded at each commit with the requests made by the first. */
function ValueProbe({ flagKey = 'copy' }: { flagKey?: string }) {
  const { value, loading, error } = useFlagValue(flagKey, 'string', 'fb');
  const text = `${value}${loading ? '|loading' : ''}${error ? `|${error.code}` : ''}`;
  useLayoutEffect(() => {
    if (requestsAtFirstCommit === null) requestsAtFirstCommit = api.mock.calls.length;
    frames.push(text);
  });
  return <span data-testid="value">{text}</span>;
}

beforeEach(() => {
  frames = [];
  requestsAtFirstCommit = null;
  api = mockApi({ copy: copyOn, limit: limitOn, theme: themeOn });
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('useFlagValue: before the answer', () => {
  it('answers the default with loading true and no error until the server answers', () => {
    global.fetch = jest.fn().mockReturnValue(new Promise(() => {})); // never answers
    const fallback = { fb: true };
    const { result } = renderHook(
      () => ({
        text: useFlagValue('copy', 'string', 'fb'),
        number: useFlagValue('limit', 'number', -1),
        json: useFlagValue('theme', 'json', fallback),
      }),
      { wrapper: wrapperFor() }
    );
    expect(result.current.text).toEqual({ value: 'fb', loading: true, error: null });
    expect(result.current.number).toEqual({ value: -1, loading: true, error: null });
    expect(result.current.json.value).toBe(fallback);
    expect(result.current.json.loading).toBe(true);
  });
});

describe('useFlagValue: reading a value', () => {
  it('reads the string, number and JSON value the server serves the user', async () => {
    const { result } = renderHook(
      () => ({
        text: useFlagValue('copy', 'string', 'fb'),
        number: useFlagValue('limit', 'number', -1),
        json: useFlagValue('theme', 'json', { fb: true }),
      }),
      { wrapper: wrapperFor() }
    );
    await waitFor(() => expect(Object.values(result.current).every(read => !read.loading)).toBe(true));
    expect(result.current.text).toEqual({ value: 'on-copy', loading: false, error: null });
    expect(result.current.number).toEqual({ value: 10, loading: false, error: null });
    expect(result.current.json).toEqual({ value: { color: 'blue', sizes: [1, 2] }, loading: false, error: null });
    expect(api).toHaveBeenCalledTimes(3);
    expect(api.mock.calls[0][0]).toBe('https://api.example.com/api/v1/feature-flags/evaluate/copy?user_id=u-1');
  });

  it("reads an off flag's value: the off value, not the default", async () => {
    api = mockApi({ copy: copyOff });
    const { result } = renderHook(() => useFlagValue('copy', 'string', 'fb'), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).toEqual({ value: 'off-copy', loading: false, error: null });
  });

  it('reads an empty string, 0, an empty object and an empty array as values, not as the default', async () => {
    api = mockApi({
      blank: { ...copyOn, key: 'blank', value: '' },
      zero: { ...limitOn, key: 'zero', value: 0 },
      none: { ...themeOn, key: 'none', value: {} },
      empty: { ...themeOn, key: 'empty', enabled: false, value: [] },
    });
    const objectFallback = { fb: true };
    const { result } = renderHook(
      () => ({
        blank: useFlagValue('blank', 'string', 'fb'),
        zero: useFlagValue('zero', 'number', -1),
        none: useFlagValue('none', 'json', objectFallback),
        empty: useFlagValue('empty', 'json', objectFallback),
      }),
      { wrapper: wrapperFor() }
    );
    await waitFor(() =>
      expect(Object.values(result.current).every(read => !read.loading)).toBe(true)
    );
    expect(result.current.blank).toEqual({ value: '', loading: false, error: null });
    expect(result.current.zero).toEqual({ value: 0, loading: false, error: null });
    expect(result.current.none).toEqual({ value: {}, loading: false, error: null });
    expect(result.current.empty).toEqual({ value: [], loading: false, error: null });
  });

  it('answers the default with a TYPE_MISMATCH error for a flag that serves another type', async () => {
    const { result } = renderHook(() => useFlagValue('copy', 'number', -1), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.value).toBe(-1);
    expect(result.current.error).toBeInstanceOf(FlagValueError);
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.code).toBe('TYPE_MISMATCH');
    expect(result.current.error?.flagKey).toBe('copy');
    expect(result.current.error?.message).toBe('Flag "copy" serves a string; useFlagValue returned your default');
  });

  it('answers the default with an INVALID_RESPONSE error when the server sends no value', async () => {
    api = mockApi({ copy: beforeValues });
    const { result } = renderHook(() => useFlagValue('copy', 'string', 'fb'), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.value).toBe('fb');
    expect(result.current.error?.code).toBe('INVALID_RESPONSE');
  });

  it('is exported from the package root, with FlagValueError', () => {
    expect(root.useFlagValue).toBe(useFlagValue);
    expect(root.FlagValueError).toBe(FlagValueError);
  });
});

describe('useFlagValue: a failed request', () => {
  it.each([
    ['a network failure', () => Promise.reject(new TypeError('Failed to fetch')), 'Failed to fetch'],
    ['a 404 (a key the server does not know)', null, 'API error: 404'],
    ['a 503', () => Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) }), 'API error: 503'],
  ])('answers the default with a REQUEST_FAILED error on %s, and never throws', async (_name, answer, cause) => {
    if (answer) global.fetch = jest.fn().mockImplementation(answer);
    else api = mockApi({});
    const { result } = renderHook(() => useFlagValue('copy', 'string', 'fb'), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.value).toBe('fb');
    expect(result.current.error).toBeInstanceOf(FlagValueError);
    expect(result.current.error?.code).toBe('REQUEST_FAILED');
    expect((result.current.error?.cause as Error).message).toBe(cause);
  });
});

describe('useFlagValue: one request, one cache', () => {
  it('shares its request and cached answer with useFeatureFlag for the same flag and user', async () => {
    function Flag() {
      const flag = useFeatureFlag('copy');
      return <span data-testid="flag">{flag.loading ? 'loading' : String(flag.isEnabled)}</span>;
    }
    const page = (withValue: boolean) => (
      <ExperimentationProvider config={config} user={user}>
        <Flag />
        {withValue ? <ValueProbe /> : null}
      </ExperimentationProvider>
    );
    const { rerender } = render(page(false));
    await waitFor(() => expect(screen.getByTestId('flag')).toHaveTextContent('true'));
    rerender(page(true));
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('on-copy'));
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("gives each hook its own copy of a JSON value: changing one changes nothing another reader gets", async () => {
    const seen: { flag?: FeatureFlagEvaluation; [name: string]: unknown } = {};
    function Reader({ name }: { name: string }) {
      seen[name] = useFlagValue('theme', 'json', null);
      return null;
    }
    function FlagReader() {
      seen.flag = useFeatureFlag('theme');
      return null;
    }
    const page = (names: string[]) => (
      <ExperimentationProvider config={config} user={user}>
        <FlagReader />
        {names.map(name => (
          <Reader key={name} name={name} />
        ))}
      </ExperimentationProvider>
    );
    const read = (name: string) => seen[name] as { value: { color: string; sizes: number[] }; loading: boolean };

    const { rerender } = render(page(['first']));
    await waitFor(() => expect(read('first').loading).toBe(false));
    read('first').value.color = 'red';
    read('first').value.sizes.push(3);

    rerender(page(['first', 'second']));
    await waitFor(() => expect(read('second').loading).toBe(false));
    expect(read('second').value).toEqual({ color: 'blue', sizes: [1, 2] });
    // The client's cached evaluation, which useFeatureFlag returns, is untouched too.
    expect(seen.flag?.value).toEqual({ color: 'blue', sizes: [1, 2] });
    expect(api).toHaveBeenCalledTimes(1);
  });
});

describe('useFlagValue: the default is read at render', () => {
  it('asks once however often it re-renders with a new inline default, with no cache to hide a request', async () => {
    const asked = jest.spyOn(ExperimentationClient.prototype, 'evaluateFeatureFlagDetailed');
    const { result, rerender } = renderHook(
      ({ fallback }: { fallback: { fb: number } }) => useFlagValue('theme', 'json', fallback),
      { wrapper: wrapperFor({ cacheTtlMs: 0 }), initialProps: { fallback: { fb: 1 } } }
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    rerender({ fallback: { fb: 1 } });
    rerender({ fallback: { fb: 1 } });
    await act(flush);
    expect(result.current.value).toEqual({ color: 'blue', sizes: [1, 2] });
    expect(asked).toHaveBeenCalledTimes(1);
    expect(api).toHaveBeenCalledTimes(1);
  });

  it('returns the default given at this render when it falls back, asking nothing again', async () => {
    api = mockApi({});
    const { result, rerender } = renderHook(
      ({ fallback }: { fallback: string }) => useFlagValue('gone', 'string', fallback),
      { wrapper: wrapperFor(), initialProps: { fallback: 'first' } }
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.value).toBe('first');
    const error = result.current.error;
    rerender({ fallback: 'second' });
    expect(result.current.value).toBe('second');
    // The same error object: a re-render does not make a new one.
    expect(result.current.error).toBe(error);
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("never answers with another flag's value while the new key loads", async () => {
    api = mockApi({ copy: copyOn, name: { ...copyOn, key: 'name', value: 'Ada' } });
    const renders: Array<[string, string]> = [];
    const { result, rerender } = renderHook(
      ({ flagKey }: { flagKey: string }) => {
        const read = useFlagValue(flagKey, 'string', 'fb');
        renders.push([flagKey, read.value]);
        return read;
      },
      { wrapper: wrapperFor(), initialProps: { flagKey: 'copy' } }
    );
    await waitFor(() => expect(result.current.value).toBe('on-copy'));
    rerender({ flagKey: 'name' });
    await waitFor(() => expect(result.current.value).toBe('Ada'));
    expect(renders.filter(([flagKey, value]) => flagKey === 'name' && value === 'on-copy')).toEqual([]);
  });
});

describe('useFlagValue: with a bootstrap', () => {
  const bootstrap: ExperimentationBootstrap = { userId: 'u-1', flags: { copy: copyOn } };

  it('answers the bootstrapped value at the first render, before any request, then asks as always', async () => {
    render(
      <ExperimentationProvider config={config} user={user} bootstrap={bootstrap}>
        <ValueProbe />
      </ExperimentationProvider>
    );
    expect(requestsAtFirstCommit).toBe(0);
    expect(frames[0]).toBe('on-copy');
    await act(flush);
    expect(frames.every(frame => frame === 'on-copy')).toBe(true);
    expect(api).toHaveBeenCalledTimes(1);
  });

  it('renders the bootstrapped value on the server, with no request', async () => {
    function Copy() {
      const { value, loading } = useFlagValue('copy', 'string', 'fb');
      return <span>{`${value}${loading ? '|loading' : ''}`}</span>;
    }
    const html = renderToString(
      <ExperimentationProvider config={config} user={user} bootstrap={bootstrap}>
        <Copy />
      </ExperimentationProvider>
    );
    // A request started during the render would come later: count after two macrotasks.
    await flush();
    await flush();
    expect(html).toBe('<span>on-copy</span>');
    expect(api).toHaveBeenCalledTimes(0);
  });

  it('keeps the bootstrapped value, with a REQUEST_FAILED error, when its refresh fails', async () => {
    api = mockApi({ copy: failure(503) });
    render(
      <ExperimentationProvider config={config} user={user} bootstrap={bootstrap}>
        <ValueProbe />
      </ExperimentationProvider>
    );
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('on-copy|REQUEST_FAILED'));
    expect(frames[0]).toBe('on-copy');
  });
});
