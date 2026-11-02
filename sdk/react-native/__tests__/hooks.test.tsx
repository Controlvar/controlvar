/**
 * Tests for the React Native hooks and provider:
 * useFlag, useExperiment, useExperimentationClient, ExperimentationProvider.
 *
 * Most tests stub the client methods with the real return shapes
 * (`FlagEvaluation`, `Assignment | null`). The last block wires the hooks to a
 * real `ExperimentationClient` with a mocked `fetch` to verify the provider →
 * client → backend contract end to end.
 */

import React from 'react';
import { Text, View } from 'react-native';
import { render, waitFor, act } from '@testing-library/react-native';
import { ExperimentationProvider } from '../src/context/ExperimentationProvider';
import { useExperimentationContext } from '../src/context/ExperimentationContext';
import { ExperimentationClient } from '../src/client';
import { useFlag } from '../src/hooks/useFlag';
import { useExperiment } from '../src/hooks/useExperiment';
import { useExperimentationClient } from '../src/hooks/useExperimentationClient';
import type { Assignment, ExperimentState, FlagEvaluation, FlagState } from '../src/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const enabledFlag: FlagEvaluation = { key: 'dark-mode', enabled: true, config: { theme: 'dark' } };
const disabledFlag: FlagEvaluation = { key: 'dark-mode', enabled: false, config: null };
const treatment: Assignment = {
  experimentKey: 'checkout-experiment',
  userId: 'u1',
  variantId: 'var-t',
  variantName: 'treatment',
  isControl: false,
  configuration: { cta: 'Buy now' },
};

interface ClientStubs {
  evaluateFlag?: jest.Mock;
  getAssignment?: jest.Mock;
  track?: jest.Mock;
}

function makeClient(overrides: ClientStubs = {}): ExperimentationClient {
  return {
    evaluateFlag: overrides.evaluateFlag ?? jest.fn().mockResolvedValue(disabledFlag),
    getAssignment: overrides.getAssignment ?? jest.fn().mockResolvedValue(null),
    track: overrides.track ?? jest.fn().mockResolvedValue(undefined),
    clearCache: jest.fn(),
  } as unknown as ExperimentationClient;
}

/** Renders a hook inside the provider and records every state it returns. */
function renderHookInProvider<T>(
  useHook: () => T,
  props: { client: ExperimentationClient; userId: string; attributes?: Record<string, unknown> }
) {
  const states: T[] = [];
  function Probe() {
    const state = useHook();
    states.push(state);
    return <Text testID="probe">{JSON.stringify(state)}</Text>;
  }
  const ui = (p: typeof props) => (
    <ExperimentationProvider client={p.client} userId={p.userId} attributes={p.attributes}>
      <Probe />
    </ExperimentationProvider>
  );
  const utils = render(ui(props));
  return {
    ...utils,
    states,
    latest: () => states[states.length - 1],
    rerenderWith: (p: typeof props) => utils.rerender(ui(p)),
  };
}

const settled = (s: { loading: boolean }) => !s.loading;

// ---------------------------------------------------------------------------
// useFlag
// ---------------------------------------------------------------------------

describe('useFlag', () => {
  test('starts in the loading state with a disabled default', () => {
    const client = makeClient({ evaluateFlag: jest.fn(() => new Promise(() => {})) });
    const { latest } = renderHookInProvider(() => useFlag('dark-mode'), { client, userId: 'u1' });

    expect(latest()).toEqual<FlagState>({
      key: 'dark-mode',
      enabled: false,
      value: false,
      config: null,
      loading: true,
      error: null,
    });
  });

  test('exposes key, enabled, config (and the `value` alias) when the server enables the flag', async () => {
    const client = makeClient({ evaluateFlag: jest.fn().mockResolvedValue(enabledFlag) });
    const { latest } = renderHookInProvider(() => useFlag('dark-mode'), { client, userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toEqual<FlagState>({
      key: 'dark-mode',
      enabled: true,
      value: true,
      config: { theme: 'dark' },
      loading: false,
      error: null,
    });
  });

  test('exposes enabled=false when the server disables the flag', async () => {
    const client = makeClient({ evaluateFlag: jest.fn().mockResolvedValue(disabledFlag) });
    const { latest } = renderHookInProvider(() => useFlag('dark-mode'), { client, userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toMatchObject({ enabled: false, value: false, config: null, error: null });
  });

  test('surfaces a rejection as `error` with the disabled default', async () => {
    const client = makeClient({ evaluateFlag: jest.fn().mockRejectedValue(new Error('API error')) });
    const { latest } = renderHookInProvider(() => useFlag('dark-mode'), { client, userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toMatchObject({ key: 'dark-mode', enabled: false, value: false, config: null, loading: false });
    expect(latest().error?.message).toBe('API error');
  });

  test('calls client.evaluateFlag(flagKey, userId) — attributes are not sent', async () => {
    const evaluateFlag = jest.fn().mockResolvedValue(enabledFlag);
    const client = makeClient({ evaluateFlag });
    renderHookInProvider(() => useFlag('specific-flag', { plan: 'pro' }), {
      client,
      userId: 'specific-user',
      attributes: { plan: 'pro' },
    });

    await waitFor(() => expect(evaluateFlag).toHaveBeenCalledTimes(1));
    expect(evaluateFlag.mock.calls[0]).toEqual(['specific-flag', 'specific-user']);
  });

  test('re-evaluates (and goes back to loading) when the flag key changes', async () => {
    const evaluateFlag = jest
      .fn()
      .mockImplementation((key: string) => Promise.resolve({ key, enabled: key === 'b', config: null }));
    const client = makeClient({ evaluateFlag });
    let flagKey = 'a';
    const { latest, rerenderWith, states } = renderHookInProvider(() => useFlag(flagKey), { client, userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toMatchObject({ key: 'a', enabled: false });

    const before = states.length;
    flagKey = 'b';
    rerenderWith({ client, userId: 'u1' });

    await waitFor(() => expect(latest()).toMatchObject({ key: 'b', enabled: true, loading: false }));
    expect(states.slice(before).some((s) => s.loading && s.key === 'b')).toBe(true);
    expect(evaluateFlag.mock.calls.map((c) => c[0])).toEqual(['a', 'b']);
  });

  test('re-evaluates when the userId changes', async () => {
    const evaluateFlag = jest.fn().mockResolvedValue(enabledFlag);
    const client = makeClient({ evaluateFlag });
    const { rerenderWith } = renderHookInProvider(() => useFlag('dark-mode'), { client, userId: 'user-A' });

    await waitFor(() => expect(evaluateFlag).toHaveBeenCalledWith('dark-mode', 'user-A'));
    rerenderWith({ client, userId: 'user-B' });
    await waitFor(() => expect(evaluateFlag).toHaveBeenCalledWith('dark-mode', 'user-B'));
    expect(evaluateFlag).toHaveBeenCalledTimes(2);
  });

  test('does not re-evaluate on a re-render with the same client, user and key', async () => {
    const evaluateFlag = jest.fn().mockResolvedValue(enabledFlag);
    const client = makeClient({ evaluateFlag });
    const attributes = { plan: 'pro' };
    const { latest, rerenderWith } = renderHookInProvider(() => useFlag('dark-mode'), { client, userId: 'u1', attributes });

    await waitFor(() => expect(settled(latest())).toBe(true));
    rerenderWith({ client, userId: 'u1', attributes });
    rerenderWith({ client, userId: 'u1', attributes: { plan: 'enterprise' } });
    await act(async () => {});

    expect(evaluateFlag).toHaveBeenCalledTimes(1);
  });

  test('ignores a result that arrives after unmount', async () => {
    let resolve!: (v: FlagEvaluation) => void;
    const client = makeClient({ evaluateFlag: jest.fn(() => new Promise<FlagEvaluation>((r) => (resolve = r))) });
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    const { unmount, states } = renderHookInProvider(() => useFlag('dark-mode'), { client, userId: 'u1' });

    unmount();
    await act(async () => {
      resolve(enabledFlag);
    });

    expect(states.every((s) => s.loading)).toBe(true);
    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });

  test('throws when used outside an ExperimentationProvider', () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    function Bare() {
      useFlag('dark-mode');
      return null;
    }
    expect(() => render(<Bare />)).toThrow('useExperimentationContext must be used inside an <ExperimentationProvider>');
    errorSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// useExperiment
// ---------------------------------------------------------------------------

describe('useExperiment', () => {
  test('starts in the loading state with null variant fields', () => {
    const client = makeClient({ getAssignment: jest.fn(() => new Promise(() => {})) });
    const { latest } = renderHookInProvider(() => useExperiment('checkout-experiment'), { client, userId: 'u1' });

    expect(latest()).toEqual<ExperimentState>({
      experimentKey: 'checkout-experiment',
      variant: null,
      variantId: null,
      variantName: null,
      isControl: false,
      configuration: null,
      loading: true,
      error: null,
    });
  });

  test('exposes the full assignment after a successful call', async () => {
    const client = makeClient({ getAssignment: jest.fn().mockResolvedValue(treatment) });
    const { latest } = renderHookInProvider(() => useExperiment('checkout-experiment'), { client, userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toEqual<ExperimentState>({
      experimentKey: 'checkout-experiment',
      variant: 'treatment',
      variantId: 'var-t',
      variantName: 'treatment',
      isControl: false,
      configuration: { cta: 'Buy now' },
      loading: false,
      error: null,
    });
  });

  test('exposes isControl=true for a control assignment', async () => {
    const control: Assignment = { ...treatment, variantId: 'var-c', variantName: 'control', isControl: true, configuration: null };
    const client = makeClient({ getAssignment: jest.fn().mockResolvedValue(control) });
    const { latest } = renderHookInProvider(() => useExperiment('checkout-experiment'), { client, userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toMatchObject({ variant: 'control', variantName: 'control', variantId: 'var-c', isControl: true, configuration: null });
  });

  test('null assignment (unknown experiment key / offline) → variant null, no error', async () => {
    const client = makeClient({ getAssignment: jest.fn().mockResolvedValue(null) });
    const { latest } = renderHookInProvider(() => useExperiment('checkout-experiment'), { client, userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toEqual<ExperimentState>({
      experimentKey: 'checkout-experiment',
      variant: null,
      variantId: null,
      variantName: null,
      isControl: false,
      configuration: null,
      loading: false,
      error: null,
    });
  });

  test('surfaces a rejection as `error`', async () => {
    const client = makeClient({ getAssignment: jest.fn().mockRejectedValue(new Error('Assignment failed')) });
    const { latest } = renderHookInProvider(() => useExperiment('checkout-experiment'), { client, userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toMatchObject({ variant: null, variantName: null, loading: false });
    expect(latest().error?.message).toBe('Assignment failed');
  });

  test("calls client.getAssignment(experimentKey, userId, attributes) with the provider's attributes", async () => {
    const getAssignment = jest.fn().mockResolvedValue(treatment);
    const client = makeClient({ getAssignment });
    renderHookInProvider(() => useExperiment('checkout-experiment'), {
      client,
      userId: 'user-xyz',
      attributes: { plan: 'pro', country: 'DE' },
    });

    await waitFor(() => expect(getAssignment).toHaveBeenCalledTimes(1));
    expect(getAssignment).toHaveBeenCalledWith('checkout-experiment', 'user-xyz', { plan: 'pro', country: 'DE' });
  });

  test('passes undefined attributes when the provider has none', async () => {
    const getAssignment = jest.fn().mockResolvedValue(treatment);
    renderHookInProvider(() => useExperiment('checkout-experiment'), { client: makeClient({ getAssignment }), userId: 'u1' });

    await waitFor(() => expect(getAssignment).toHaveBeenCalledWith('checkout-experiment', 'u1', undefined));
  });

  test('re-assigns (and goes back to loading) when the experiment key changes', async () => {
    const getAssignment = jest
      .fn()
      .mockImplementation((key: string) => Promise.resolve({ ...treatment, experimentKey: key, variantName: key === 'exp-b' ? 'control' : 'treatment' }));
    const client = makeClient({ getAssignment });
    let experimentKey = 'exp-a';
    const { latest, rerenderWith, states } = renderHookInProvider(() => useExperiment(experimentKey), { client, userId: 'u1' });

    await waitFor(() => expect(latest()).toMatchObject({ experimentKey: 'exp-a', variant: 'treatment', loading: false }));

    const before = states.length;
    experimentKey = 'exp-b';
    rerenderWith({ client, userId: 'u1' });

    await waitFor(() => expect(latest()).toMatchObject({ experimentKey: 'exp-b', variant: 'control', loading: false }));
    expect(states.slice(before).some((s) => s.loading && s.experimentKey === 'exp-b')).toBe(true);
    expect(getAssignment.mock.calls.map((c) => c[0])).toEqual(['exp-a', 'exp-b']);
  });

  test('re-assigns when the userId changes but not when only attributes change (sticky per user)', async () => {
    const getAssignment = jest.fn().mockResolvedValue(treatment);
    const client = makeClient({ getAssignment });
    const { latest, rerenderWith } = renderHookInProvider(() => useExperiment('checkout-experiment'), {
      client,
      userId: 'user-A',
      attributes: { plan: 'free' },
    });

    await waitFor(() => expect(settled(latest())).toBe(true));
    rerenderWith({ client, userId: 'user-A', attributes: { plan: 'pro' } });
    await act(async () => {});
    expect(getAssignment).toHaveBeenCalledTimes(1);

    rerenderWith({ client, userId: 'user-B', attributes: { plan: 'pro' } });
    await waitFor(() => expect(getAssignment).toHaveBeenCalledTimes(2));
    expect(getAssignment).toHaveBeenLastCalledWith('checkout-experiment', 'user-B', { plan: 'pro' });
  });

  test('throws when used outside an ExperimentationProvider', () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    function Bare() {
      useExperiment('checkout-experiment');
      return null;
    }
    expect(() => render(<Bare />)).toThrow('useExperimentationContext must be used inside an <ExperimentationProvider>');
    errorSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// useExperimentationClient / useExperimentationContext
// ---------------------------------------------------------------------------

describe('useExperimentationClient', () => {
  function ClientConsumer() {
    const { client, userId, attributes } = useExperimentationClient();
    return (
      <View>
        <Text testID="userId">{userId}</Text>
        <Text testID="hasClient">{client ? 'yes' : 'no'}</Text>
        <Text testID="attrs">{attributes ? JSON.stringify(attributes) : 'none'}</Text>
      </View>
    );
  }

  test('exposes client, userId, and attributes from the provider', () => {
    const client = makeClient();
    const { getByTestId } = render(
      <ExperimentationProvider client={client} userId="ctx-user" attributes={{ plan: 'pro' }}>
        <ClientConsumer />
      </ExperimentationProvider>
    );

    expect(getByTestId('userId').props.children).toBe('ctx-user');
    expect(getByTestId('hasClient').props.children).toBe('yes');
    expect(getByTestId('attrs').props.children).toBe('{"plan":"pro"}');
  });

  test('returns the very same client instance that was passed to the provider', () => {
    const client = makeClient();
    let seen: ExperimentationClient | undefined;
    function Capture() {
      seen = useExperimentationClient().client;
      return null;
    }
    render(
      <ExperimentationProvider client={client} userId="u1">
        <Capture />
      </ExperimentationProvider>
    );
    expect(seen).toBe(client);
  });

  test('lets components call client.track imperatively', async () => {
    const track = jest.fn().mockResolvedValue(undefined);
    const client = makeClient({ track });
    function TrackOnMount() {
      const { client: c, userId } = useExperimentationClient();
      React.useEffect(() => {
        void c.track('cta_clicked', userId, { screen: 'home' }, { featureFlagKey: 'dark-mode' });
      }, [c, userId]);
      return null;
    }
    render(
      <ExperimentationProvider client={client} userId="u1">
        <TrackOnMount />
      </ExperimentationProvider>
    );
    await waitFor(() => expect(track).toHaveBeenCalledWith('cta_clicked', 'u1', { screen: 'home' }, { featureFlagKey: 'dark-mode' }));
  });

  test('throws when used outside an ExperimentationProvider', () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<ClientConsumer />)).toThrow(
      'useExperimentationContext must be used inside an <ExperimentationProvider>'
    );
    errorSpy.mockRestore();
  });

  test('useExperimentationContext throws the same error outside a provider', () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    function Bare() {
      useExperimentationContext();
      return null;
    }
    expect(() => render(<Bare />)).toThrow('useExperimentationContext must be used inside an <ExperimentationProvider>');
    errorSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// ExperimentationProvider
// ---------------------------------------------------------------------------

describe('ExperimentationProvider', () => {
  test('renders children', () => {
    const { getByText } = render(
      <ExperimentationProvider client={makeClient()} userId="u1">
        <Text>Hello from child</Text>
      </ExperimentationProvider>
    );
    expect(getByText('Hello from child')).toBeTruthy();
  });

  test('memoises the context value while client/userId/attributes are unchanged', () => {
    const client = makeClient();
    const attributes = { plan: 'pro' };
    const seen: unknown[] = [];
    function Capture() {
      seen.push(useExperimentationContext());
      return null;
    }
    function Parent({ tick }: { tick: number }) {
      return (
        <ExperimentationProvider client={client} userId="u1" attributes={attributes}>
          <Capture />
          <Text>{tick}</Text>
        </ExperimentationProvider>
      );
    }

    const { rerender } = render(<Parent tick={0} />);
    rerender(<Parent tick={1} />);
    rerender(<Parent tick={2} />);

    expect(seen.length).toBeGreaterThanOrEqual(2);
    expect(new Set(seen).size).toBe(1);
  });

  test('produces a new context value when userId changes', () => {
    const client = makeClient();
    const seen: unknown[] = [];
    function Capture() {
      seen.push(useExperimentationContext());
      return null;
    }
    const { rerender } = render(
      <ExperimentationProvider client={client} userId="u1">
        <Capture />
      </ExperimentationProvider>
    );
    rerender(
      <ExperimentationProvider client={client} userId="u2">
        <Capture />
      </ExperimentationProvider>
    );
    expect(new Set(seen).size).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// End-to-end: hooks → real client → mocked backend
// ---------------------------------------------------------------------------

describe('hooks with a real ExperimentationClient', () => {
  const BASE_URL = 'https://api.example.com';

  function jsonResponse(body: unknown, status = 200) {
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  }

  function realClient() {
    return new ExperimentationClient({ apiKey: 'k', baseUrl: BASE_URL, offlineFallback: false });
  }

  beforeEach(() => {
    global.fetch = jest.fn();
  });

  test('useFlag issues GET /api/v1/feature-flags/evaluate/{key}?user_id= and reflects the response', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({ key: 'dark-mode', enabled: true, config: { theme: 'dark' } }));
    const { latest } = renderHookInProvider(() => useFlag('dark-mode'), { client: realClient(), userId: 'user 1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toMatchObject({ key: 'dark-mode', enabled: true, value: true, config: { theme: 'dark' }, error: null });

    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${BASE_URL}/api/v1/feature-flags/evaluate/dark-mode?user_id=user%201`);
    expect(init.method).toBe('GET');
    expect(init.headers['X-API-Key']).toBe('k');
  });

  // #1519: the kill-switch page says a component already on screen keeps its answer until it
  // mounts again; the SDK neither polls nor streams.
  test('a mounted useFlag does not ask again: past the cache lifetime it keeps its answer, one request', async () => {
    jest.useFakeTimers();
    try {
      (global.fetch as jest.Mock).mockResolvedValue(jsonResponse({ key: 'dark-mode', enabled: true, config: null }));
      const client = realClient();
      const { latest, rerenderWith } = renderHookInProvider(() => useFlag('dark-mode'), { client, userId: 'u1' });
      await waitFor(() => expect(settled(latest())).toBe(true));
      (global.fetch as jest.Mock).mockResolvedValue(jsonResponse({ key: 'dark-mode', enabled: false, config: null }));

      await act(async () => {
        await jest.advanceTimersByTimeAsync(10 * 60 * 1000);
      });
      rerenderWith({ client, userId: 'u1' });
      await act(async () => {
        await jest.advanceTimersByTimeAsync(10 * 60 * 1000);
      });

      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(latest()).toMatchObject({ enabled: true, loading: false });
    } finally {
      jest.useRealTimers();
    }
  });

  test('useExperiment POSTs /api/v1/tracking/assign with the provider attributes as context', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      jsonResponse({
        experiment_key: 'checkout-experiment',
        user_id: 'u1',
        variant_id: 'v2',
        variant_name: 'treatment',
        is_control: false,
        configuration: { cta: 'Go' },
      })
    );
    const { latest } = renderHookInProvider(() => useExperiment('checkout-experiment'), {
      client: realClient(),
      userId: 'u1',
      attributes: { plan: 'pro' },
    });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toMatchObject({ variant: 'treatment', variantId: 'v2', isControl: false, configuration: { cta: 'Go' }, error: null });

    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${BASE_URL}/api/v1/tracking/assign`);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ experiment_key: 'checkout-experiment', user_id: 'u1', context: { plan: 'pro' } });
  });

  test('a 404 from the server leaves useExperiment with variant null and no thrown error', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({ detail: 'not found' }, 404));
    const { latest } = renderHookInProvider(() => useExperiment('paused-experiment'), { client: realClient(), userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toMatchObject({ variant: null, variantName: null, isControl: false, configuration: null, error: null });
  });

  test('a network failure leaves useFlag disabled with no thrown error', async () => {
    (global.fetch as jest.Mock).mockRejectedValueOnce(new Error('offline'));
    const { latest } = renderHookInProvider(() => useFlag('dark-mode'), { client: realClient(), userId: 'u1' });

    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toMatchObject({ enabled: false, value: false, config: null, error: null });
  });
});
