import React from 'react';
import { renderHook, act, waitFor } from '@testing-library/react';
import { ExperimentationProvider } from '../context/ExperimentationProvider';
import { useFeatureFlag } from '../hooks/useFeatureFlag';
import { SdkConfig, UserContext, FeatureFlagEvaluateResponse } from '../client/types';

const config: SdkConfig = {
  apiKey: 'test-key',
  baseUrl: 'https://api.example.com',
};

const defaultUser: UserContext = { userId: 'user-1' };

const enabledFlag: FeatureFlagEvaluateResponse = { key: 'my-flag', enabled: true, config: null };
const disabledFlag: FeatureFlagEvaluateResponse = { key: 'my-flag', enabled: false, config: null };
const variantFlag: FeatureFlagEvaluateResponse = {
  key: 'my-flag',
  enabled: true,
  config: { variant: 'treatment', engine: 'v2' },
};

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

function makeFetchMock(body: unknown, status = 200): jest.Mock {
  const mock = jest.fn().mockResolvedValue(jsonResponse(body, status));
  global.fetch = mock;
  return mock;
}

function makeWrapper(user: UserContext = defaultUser) {
  return ({ children }: { children: React.ReactNode }) => (
    <ExperimentationProvider config={config} user={user}>
      {children}
    </ExperimentationProvider>
  );
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe('useFeatureFlag', () => {
  it('starts with loading: true and isEnabled: false', () => {
    global.fetch = jest.fn().mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    expect(result.current.loading).toBe(true);
    expect(result.current.isEnabled).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('starts with variant null, config null and no error', () => {
    global.fetch = jest.fn().mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    expect(result.current).toEqual({
      flagKey: 'my-flag',
      variant: null,
      isEnabled: false,
      config: null,
      loading: true,
      error: null,
    });
  });

  it('GETs /api/v1/feature-flags/evaluate/{key}?user_id=… with the API headers', async () => {
    const fetchMock = makeFetchMock(enabledFlag);
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://api.example.com/api/v1/feature-flags/evaluate/my-flag?user_id=user-1');
    expect(init.method).toBe('GET');
    expect(init.headers).toEqual({ 'X-API-Key': 'test-key', 'Content-Type': 'application/json' });
  });

  it('sends the provider user attributes as context=<url-encoded JSON>', async () => {
    const fetchMock = makeFetchMock(enabledFlag);
    const wrapper = makeWrapper({ userId: 'user-1', attributes: { os: 'iOS', tier: 'premium' } });
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(fetchMock.mock.calls[0][0]).toBe(
      'https://api.example.com/api/v1/feature-flags/evaluate/my-flag' +
        '?user_id=user-1&context=%7B%22os%22%3A%22iOS%22%2C%22tier%22%3A%22premium%22%7D'
    );
  });

  it('exposes the server reason on the evaluation', async () => {
    makeFetchMock({ ...enabledFlag, reason: 'targeting_rule' });
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.reason).toBe('targeting_rule');
    expect(result.current.isEnabled).toBe(true);
  });

  it('sets isEnabled: true and variant "on" when the server enables the flag', async () => {
    makeFetchMock(enabledFlag);
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.isEnabled).toBe(true);
    expect(result.current.variant).toBe('on');
  });

  it('uses config.variant as the variant and exposes config', async () => {
    makeFetchMock(variantFlag);
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).toEqual({
      flagKey: 'my-flag',
      variant: 'treatment',
      isEnabled: true,
      config: { variant: 'treatment', engine: 'v2' },
      loading: false,
      error: null,
    });
  });

  it('sets isEnabled: false and variant null when the server disables the flag', async () => {
    makeFetchMock(disabledFlag);
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.isEnabled).toBe(false);
    expect(result.current.variant).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it('exposes flagKey in the returned state', async () => {
    makeFetchMock(enabledFlag);
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    expect(result.current.flagKey).toBe('my-flag');
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.flagKey).toBe('my-flag');
  });

  it('sets error and keeps the flag off when the API returns a non-2xx response', async () => {
    makeFetchMock({}, 500);
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.message).toBe('API error: 500');
    expect(result.current.isEnabled).toBe(false);
    expect(result.current.variant).toBeNull();
    expect(result.current.config).toBeNull();
  });

  it('sets error on network failure', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network down'));
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.message).toBe('Network down');
  });

  it('clears error on successful re-fetch after flagKey change', async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValueOnce(new Error('Network down'))
      .mockResolvedValueOnce(jsonResponse(enabledFlag));

    let flagKey = 'flag-a';
    const { result, rerender } = renderHook(() => useFeatureFlag(flagKey), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).not.toBeNull();

    flagKey = 'flag-b';
    rerender();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
    expect(result.current.isEnabled).toBe(true);
  });

  it('re-fetches when flagKey changes', async () => {
    const fetchMock = makeFetchMock(enabledFlag);

    let flagKey = 'flag-a';
    const { result, rerender } = renderHook(() => useFeatureFlag(flagKey), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    flagKey = 'flag-b';
    rerender();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain('/evaluate/flag-b?user_id=user-1');
  });

  it('resets to loading (flag off) while a new key is being evaluated', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(enabledFlag))
      .mockReturnValueOnce(new Promise(() => {})); // second call never resolves

    let flagKey = 'flag-a';
    const { result, rerender } = renderHook(() => useFeatureFlag(flagKey), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isEnabled).toBe(true));

    flagKey = 'flag-b';
    rerender();
    expect(result.current).toEqual({
      flagKey: 'flag-b',
      variant: null,
      isEnabled: false,
      config: null,
      loading: true,
      error: null,
    });
  });

  it('re-fetches when user.userId changes', async () => {
    const fetchMock = makeFetchMock(enabledFlag);

    let currentUser = defaultUser;
    const DynamicWrapper = ({ children }: { children: React.ReactNode }) => (
      <ExperimentationProvider config={config} user={currentUser}>
        {children}
      </ExperimentationProvider>
    );

    const { result, rerender } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: DynamicWrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    currentUser = { userId: 'user-2' };
    rerender();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain('user_id=user-2');
  });

  it('does not re-fetch when the provider re-renders with the same user', async () => {
    const fetchMock = makeFetchMock(enabledFlag);
    const { result, rerender } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));

    rerender();
    rerender();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  // #1519: the kill-switch page says a component already on screen keeps its answer until it
  // mounts again; the SDK neither polls nor streams.
  it('a mounted hook does not ask again: past the cache lifetime it keeps its answer, one request', async () => {
    jest.useFakeTimers();
    try {
      const fetchMock = makeFetchMock(enabledFlag);
      const { result, rerender } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
      await waitFor(() => expect(result.current.loading).toBe(false));
      fetchMock.mockResolvedValue(jsonResponse(disabledFlag));

      await act(async () => {
        await jest.advanceTimersByTimeAsync(10 * 60 * 1000);
      });
      rerender();
      await act(async () => {
        await jest.advanceTimersByTimeAsync(10 * 60 * 1000);
      });

      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(result.current.isEnabled).toBe(true);
    } finally {
      jest.useRealTimers();
    }
  });

  it('shares the cached evaluation between two hooks for the same flag', async () => {
    const fetchMock = makeFetchMock(variantFlag);
    const { result } = renderHook(
      () => [useFeatureFlag('my-flag'), useFeatureFlag('my-flag')] as const,
      { wrapper: makeWrapper() }
    );
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    await waitFor(() => expect(result.current[1].loading).toBe(false));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current[0].variant).toBe('treatment');
    expect(result.current[1].variant).toBe('treatment');
  });

  it('does not update state after unmount (no setState on cancelled effect)', async () => {
    let resolveFetch!: (val: unknown) => void;
    global.fetch = jest.fn().mockReturnValue(new Promise(resolve => { resolveFetch = resolve; }));

    const { result, unmount } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });

    expect(result.current.loading).toBe(true);
    unmount();

    await act(async () => {
      resolveFetch(jsonResponse(enabledFlag));
    });

    expect(result.current.loading).toBe(true);
  });

  it('returns loading: false and no error after a successful fetch', async () => {
    makeFetchMock(enabledFlag);
    const { result } = renderHook(() => useFeatureFlag('my-flag'), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
  });
});
