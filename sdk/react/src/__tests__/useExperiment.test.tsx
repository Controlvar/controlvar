import React from 'react';
import { renderHook, act, waitFor } from '@testing-library/react';
import { ExperimentationProvider } from '../context/ExperimentationProvider';
import { useExperiment } from '../hooks/useExperiment';
import { SdkConfig, UserContext, ExperimentAssignResponse } from '../client/types';

const config: SdkConfig = {
  apiKey: 'test-key',
  baseUrl: 'https://api.example.com',
};

const user: UserContext = { userId: 'user-1', attributes: { device: 'mobile' } };

const treatment: ExperimentAssignResponse = {
  experiment_key: 'checkout-experiment',
  user_id: 'user-1',
  variant_id: 'var-2',
  variant_name: 'one_page',
  is_control: false,
  configuration: { steps: 1 },
};

const control: ExperimentAssignResponse = {
  experiment_key: 'checkout-experiment',
  user_id: 'user-1',
  variant_id: 'var-1',
  variant_name: 'standard',
  is_control: true,
  configuration: { steps: 3 },
};

const controlDefaults = {
  variantKey: 'control',
  variantName: 'Control',
  variantId: null,
  isControl: true,
  configuration: null,
};

function makeWrapper(u: UserContext = user) {
  return ({ children }: { children: React.ReactNode }) => (
    <ExperimentationProvider config={config} user={u}>
      {children}
    </ExperimentationProvider>
  );
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

function makeFetchMock(body: unknown, status = 200): jest.Mock {
  const mock = jest.fn().mockResolvedValue(jsonResponse(body, status));
  global.fetch = mock;
  return mock;
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe('useExperiment', () => {
  it('starts with loading: true', () => {
    global.fetch = jest.fn().mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    expect(result.current.loading).toBe(true);
  });

  it('starts with the control defaults and no error', () => {
    global.fetch = jest.fn().mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    expect(result.current).toEqual({
      experimentKey: 'checkout-experiment',
      ...controlDefaults,
      loading: true,
      error: null,
    });
  });

  it('exposes experimentKey in the returned state', async () => {
    makeFetchMock(treatment);
    const { result } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    expect(result.current.experimentKey).toBe('checkout-experiment');
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.experimentKey).toBe('checkout-experiment');
  });

  it('POSTs {experiment_key, user_id, context} to /api/v1/tracking/assign', async () => {
    const fetchMock = makeFetchMock(treatment);
    const { result } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://api.example.com/api/v1/tracking/assign');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ 'X-API-Key': 'test-key', 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body)).toEqual({
      experiment_key: 'checkout-experiment',
      user_id: 'user-1',
      context: { device: 'mobile' },
    });
  });

  it('resolves to the server assignment', async () => {
    makeFetchMock(treatment);
    const { result } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).toEqual({
      experimentKey: 'checkout-experiment',
      variantKey: 'one_page',
      variantName: 'one_page',
      variantId: 'var-2',
      isControl: false,
      configuration: { steps: 1 },
      loading: false,
      error: null,
      assigned: true,
    });
  });

  it('reports a control assignment with isControl: true', async () => {
    makeFetchMock(control);
    const { result } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.variantKey).toBe('standard');
    expect(result.current.isControl).toBe(true);
    expect(result.current.configuration).toEqual({ steps: 3 });
  });

  it('sets error and keeps the control defaults on a 404 (no experiment has the key)', async () => {
    makeFetchMock({ detail: "No experiment has the key 'checkout-experiment'." }, 404);
    const { result } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).toMatchObject({ experimentKey: 'checkout-experiment', ...controlDefaults });
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.message).toBe('API error: 404');
  });

  it('sets error and keeps the control defaults on network failure', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network down'));
    const { result } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).toMatchObject(controlDefaults);
    expect(result.current.error?.message).toBe('Network down');
  });

  it('sets loading: false after resolution', async () => {
    makeFetchMock(treatment);
    const { result } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.loading).toBe(false);
  });

  it('re-fetches when experimentKey changes', async () => {
    const fetchMock = makeFetchMock(treatment);

    let expKey = 'exp-a';
    const { result, rerender } = renderHook(() => useExperiment(expKey), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    expKey = 'exp-b';
    rerender();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).experiment_key).toBe('exp-b');
  });

  it('resets to loading with the control defaults while a new key is being assigned', async () => {
    let resolveSecond!: (val: unknown) => void;
    global.fetch = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(treatment))
      .mockReturnValueOnce(new Promise(resolve => { resolveSecond = resolve; }));

    let expKey = 'exp-a';
    const { result, rerender } = renderHook(() => useExperiment(expKey), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.variantKey).toBe('one_page');

    expKey = 'exp-b';
    rerender();
    expect(result.current).toEqual({
      experimentKey: 'exp-b',
      ...controlDefaults,
      loading: true,
      error: null,
    });

    await act(async () => {
      resolveSecond(jsonResponse(control));
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.variantKey).toBe('standard');
  });

  it('re-assigns when user.userId changes', async () => {
    const fetchMock = makeFetchMock(treatment);

    let currentUser = user;
    const DynamicWrapper = ({ children }: { children: React.ReactNode }) => (
      <ExperimentationProvider config={config} user={currentUser}>
        {children}
      </ExperimentationProvider>
    );

    const { result, rerender } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: DynamicWrapper,
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    currentUser = { userId: 'user-2' };
    rerender();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).user_id).toBe('user-2');
  });

  it('shares the cached assignment between two hooks for the same experiment', async () => {
    const fetchMock = makeFetchMock(treatment);
    const { result } = renderHook(
      () => [useExperiment('checkout-experiment'), useExperiment('checkout-experiment')] as const,
      { wrapper: makeWrapper() }
    );
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    await waitFor(() => expect(result.current[1].loading).toBe(false));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current[0].variantKey).toBe('one_page');
    expect(result.current[1].variantKey).toBe('one_page');
  });

  it('does not update state after unmount', async () => {
    let resolveFetch!: (val: unknown) => void;
    global.fetch = jest.fn().mockReturnValue(new Promise(resolve => { resolveFetch = resolve; }));

    const { result, unmount } = renderHook(() => useExperiment('checkout-experiment'), {
      wrapper: makeWrapper(),
    });
    expect(result.current.loading).toBe(true);
    unmount();

    await act(async () => {
      resolveFetch(jsonResponse(treatment));
    });

    expect(result.current.loading).toBe(true);
    expect(result.current.variantKey).toBe('control');
  });
});
