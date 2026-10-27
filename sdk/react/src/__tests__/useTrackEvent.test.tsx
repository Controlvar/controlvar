import React from 'react';
import { renderHook, act, waitFor } from '@testing-library/react';
import { ExperimentationProvider } from '../context/ExperimentationProvider';
import { useTrackEvent } from '../hooks/useTrackEvent';
import { useExperiment } from '../hooks/useExperiment';
import { useFeatureFlag } from '../hooks/useFeatureFlag';
import { SdkConfig, UserContext } from '../client/types';

const config: SdkConfig = {
  apiKey: 'test-key',
  baseUrl: 'https://api.example.com',
};

const user: UserContext = { userId: 'user-42' };

const assignment = {
  experiment_key: 'checkout',
  user_id: 'user-42',
  variant_id: 'var-2',
  variant_name: 'one_page',
  is_control: false,
  configuration: { steps: 1 },
};

const flagOn = { key: 'new-search', enabled: true, config: null };

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

function setupFetchMock(): jest.Mock {
  const mock = jest.fn().mockResolvedValue(jsonResponse({}));
  global.fetch = mock;
  return mock;
}

function lastBody(mock: jest.Mock) {
  const [, init] = mock.mock.calls[mock.mock.calls.length - 1];
  return JSON.parse(init.body);
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe('useTrackEvent', () => {
  it('returns a callable function', () => {
    setupFetchMock();
    const { result } = renderHook(() => useTrackEvent(), { wrapper: makeWrapper() });
    expect(typeof result.current).toBe('function');
  });

  it('returns void (fire-and-forget, not a promise)', () => {
    setupFetchMock();
    const { result } = renderHook(() => useTrackEvent(), { wrapper: makeWrapper() });
    expect(result.current('click', undefined, { experimentKey: 'checkout' })).toBeUndefined();
  });

  it('sends the event once to /api/v1/tracking/track with no key when no key is given and nothing is attributed to the user', async () => {
    const fetchMock = setupFetchMock();
    const { result } = renderHook(() => useTrackEvent(), { wrapper: makeWrapper() });

    await act(async () => {
      result.current('button_clicked');
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://api.example.com/api/v1/tracking/track');
    const body = JSON.parse(init.body);
    expect(body.user_id).toBe('user-42');
    expect(body.event_name).toBe('button_clicked');
    expect(body).not.toHaveProperty('experiment_key');
    expect(body).not.toHaveProperty('feature_flag_key');
  });

  it('POSTs to /api/v1/tracking/track with the provider user id when an experimentKey is given', async () => {
    const fetchMock = setupFetchMock();
    const { result } = renderHook(() => useTrackEvent(), { wrapper: makeWrapper() });

    await act(async () => {
      result.current('button_clicked', undefined, { experimentKey: 'checkout' });
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://api.example.com/api/v1/tracking/track');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ 'X-API-Key': 'test-key', 'Content-Type': 'application/json' });
    expect(lastBody(fetchMock)).toEqual({
      event_type: 'button_clicked',
      event_name: 'button_clicked',
      user_id: 'user-42',
      experiment_key: 'checkout',
    });
  });

  it('passes the event name and properties as event_name / metadata', async () => {
    const fetchMock = setupFetchMock();
    const { result } = renderHook(() => useTrackEvent(), { wrapper: makeWrapper() });

    await act(async () => {
      result.current('add_to_cart', { product_id: 'sku-123', quantity: 2 }, { experimentKey: 'pdp' });
    });

    expect(lastBody(fetchMock)).toMatchObject({
      event_name: 'add_to_cart',
      metadata: { product_id: 'sku-123', quantity: 2 },
    });
  });

  it('sends feature_flag_key when a featureFlagKey is given', async () => {
    const fetchMock = setupFetchMock();
    const { result } = renderHook(() => useTrackEvent(), { wrapper: makeWrapper() });

    await act(async () => {
      result.current('search', { query: 'boots' }, { featureFlagKey: 'new-search' });
    });

    expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/api/v1/tracking/track');
    expect(lastBody(fetchMock)).toEqual({
      event_type: 'search',
      event_name: 'search',
      user_id: 'user-42',
      feature_flag_key: 'new-search',
      metadata: { query: 'boots' },
    });
  });

  it('forwards eventType, value and timestamp options', async () => {
    const fetchMock = setupFetchMock();
    const { result } = renderHook(() => useTrackEvent(), { wrapper: makeWrapper() });

    await act(async () => {
      result.current('purchase', { order_id: 'o-1' }, {
        experimentKey: 'checkout',
        eventType: 'conversion',
        value: 99.5,
        timestamp: new Date('2026-09-11T10:00:00.000Z'),
      });
    });

    expect(lastBody(fetchMock)).toEqual({
      event_type: 'conversion',
      event_name: 'purchase',
      user_id: 'user-42',
      experiment_key: 'checkout',
      value: 99.5,
      metadata: { order_id: 'o-1' },
      timestamp: '2026-09-11T10:00:00.000Z',
    });
  });

  it('fans out to /api/v1/tracking/batch for an experiment assigned via useExperiment', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(assignment))
      .mockResolvedValue(jsonResponse({ success_count: 1 }));
    global.fetch = fetchMock;

    const { result } = renderHook(
      () => ({ track: useTrackEvent(), experiment: useExperiment('checkout') }),
      { wrapper: makeWrapper() }
    );
    await waitFor(() => expect(result.current.experiment.loading).toBe(false));

    await act(async () => {
      result.current.track('purchase', { order_id: 'o-1' }, { value: 120 });
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe('https://api.example.com/api/v1/tracking/batch');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({
      events: [
        {
          event_type: 'purchase',
          event_name: 'purchase',
          user_id: 'user-42',
          experiment_key: 'checkout',
          value: 120,
          metadata: { order_id: 'o-1' },
        },
      ],
    });
  });

  it('fans out to both the assigned experiment and the evaluated flag', async () => {
    const fetchMock = jest
      .fn()
      .mockImplementation((url: string) =>
        Promise.resolve(jsonResponse(url.includes('/tracking/assign') ? assignment : flagOn))
      );
    global.fetch = fetchMock;

    const { result } = renderHook(
      () => ({
        track: useTrackEvent(),
        experiment: useExperiment('checkout'),
        flag: useFeatureFlag('new-search'),
      }),
      { wrapper: makeWrapper() }
    );
    await waitFor(() => expect(result.current.experiment.loading).toBe(false));
    await waitFor(() => expect(result.current.flag.loading).toBe(false));
    fetchMock.mockClear();

    await act(async () => {
      result.current.track('page_view', { page: '/search' });
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/api/v1/tracking/batch');
    const base = {
      event_type: 'page_view',
      event_name: 'page_view',
      user_id: 'user-42',
      metadata: { page: '/search' },
    };
    expect(lastBody(fetchMock)).toEqual({
      events: [
        { ...base, experiment_key: 'checkout' },
        { ...base, feature_flag_key: 'new-search' },
      ],
    });
  });

  it('returns a stable callback reference across re-renders (same user)', () => {
    setupFetchMock();
    const { result, rerender } = renderHook(() => useTrackEvent(), { wrapper: makeWrapper() });

    const first = result.current;
    rerender();
    const second = result.current;

    expect(first).toBe(second);
  });

  it('returns a new callback when userId changes', () => {
    setupFetchMock();

    let currentUser = user;
    const DynamicWrapper = ({ children }: { children: React.ReactNode }) => (
      <ExperimentationProvider config={config} user={currentUser}>
        {children}
      </ExperimentationProvider>
    );

    const { result, rerender } = renderHook(() => useTrackEvent(), { wrapper: DynamicWrapper });
    const firstCallback = result.current;

    currentUser = { userId: 'user-99' };
    rerender();
    const secondCallback = result.current;

    expect(secondCallback).not.toBe(firstCallback);
  });

  it('uses the new user id after the user changes', async () => {
    const fetchMock = setupFetchMock();

    let currentUser = user;
    const DynamicWrapper = ({ children }: { children: React.ReactNode }) => (
      <ExperimentationProvider config={config} user={currentUser}>
        {children}
      </ExperimentationProvider>
    );

    const { result, rerender } = renderHook(() => useTrackEvent(), { wrapper: DynamicWrapper });
    currentUser = { userId: 'user-99' };
    rerender();

    await act(async () => {
      result.current('click', undefined, { experimentKey: 'checkout' });
    });

    expect(lastBody(fetchMock).user_id).toBe('user-99');
  });

  it('does not throw when network fails (fire-and-forget)', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network error'));
    const { result } = renderHook(() => useTrackEvent(), { wrapper: makeWrapper() });

    await act(async () => {
      expect(() => result.current('click', undefined, { experimentKey: 'checkout' })).not.toThrow();
    });
  });
});
