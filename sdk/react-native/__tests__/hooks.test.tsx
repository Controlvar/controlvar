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
import AsyncStorage from '@react-native-async-storage/async-storage';
import { ExperimentationProvider } from '../src/context/ExperimentationProvider';
import { useExperimentationContext } from '../src/context/ExperimentationContext';
import { ApiError, ExperimentationClient, FlagValueError, readFlagValue } from '../src/client';
import { OfflineStorage } from '../src/storage';
import { useFlag } from '../src/hooks/useFlag';
import { useFlagValue } from '../src/hooks/useFlagValue';
import { useExperiment } from '../src/hooks/useExperiment';
import { useExperimentationClient } from '../src/hooks/useExperimentationClient';
import type { Assignment, ExperimentState, FlagEvaluation, FlagState, SdkConfig } from '../src/types';

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

  test('regression #1520: calls client.evaluateFlag(flagKey, userId, attributes) with its own argument', async () => {
    const evaluateFlag = jest.fn().mockResolvedValue(enabledFlag);
    const client = makeClient({ evaluateFlag });
    renderHookInProvider(() => useFlag('specific-flag', { plan: 'pro' }), {
      client,
      userId: 'specific-user',
      attributes: { plan: 'free' },
    });

    await waitFor(() => expect(evaluateFlag).toHaveBeenCalledTimes(1));
    expect(evaluateFlag.mock.calls[0]).toEqual(['specific-flag', 'specific-user', { plan: 'pro' }]);
  });

  test('regression #1520: without its own argument it sends the provider attributes (no merge)', async () => {
    const evaluateFlag = jest.fn().mockResolvedValue(enabledFlag);
    renderHookInProvider(() => useFlag('f'), {
      client: makeClient({ evaluateFlag }),
      userId: 'u1',
      attributes: { plan: 'pro' },
    });
    await waitFor(() => expect(evaluateFlag).toHaveBeenCalledTimes(1));
    expect(evaluateFlag.mock.calls[0]).toEqual(['f', 'u1', { plan: 'pro' }]);
  });

  test('regression #1520: its own argument replaces the provider attributes; keys of the provider are not merged in', async () => {
    const evaluateFlag = jest.fn().mockResolvedValue(enabledFlag);
    renderHookInProvider(() => useFlag('f', { country: 'US' }), {
      client: makeClient({ evaluateFlag }),
      userId: 'u1',
      attributes: { plan: 'pro' },
    });
    await waitFor(() => expect(evaluateFlag).toHaveBeenCalledTimes(1));
    expect(evaluateFlag.mock.calls[0]).toEqual(['f', 'u1', { country: 'US' }]);
  });

  test('regression #1520: an equal inline object re-rendered 3 times asks once', async () => {
    const evaluateFlag = jest.fn().mockResolvedValue(enabledFlag);
    const client = makeClient({ evaluateFlag });
    const { latest, rerenderWith } = renderHookInProvider(() => useFlag('f', { plan: 'pro' }), {
      client,
      userId: 'u1',
      attributes: { plan: 'pro' },
    });
    await waitFor(() => expect(settled(latest())).toBe(true));
    for (let i = 0; i < 3; i++) rerenderWith({ client, userId: 'u1', attributes: { plan: 'pro' } });
    await act(async () => {});
    expect(evaluateFlag).toHaveBeenCalledTimes(1);
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

    await waitFor(() => expect(evaluateFlag).toHaveBeenCalledWith('dark-mode', 'user-A', undefined));
    rerenderWith({ client, userId: 'user-B' });
    await waitFor(() => expect(evaluateFlag).toHaveBeenCalledWith('dark-mode', 'user-B', undefined));
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

  test('regression #1520: useFlag puts the attributes on the wire', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({ key: 'f', enabled: true, config: null }));
    const { latest } = renderHookInProvider(() => useFlag('f'), {
      client: realClient(),
      userId: 'u1',
      attributes: { plan: 'pro' },
    });
    await waitFor(() => expect(settled(latest())).toBe(true));
    expect((global.fetch as jest.Mock).mock.calls[0][0]).toBe(
      `${BASE_URL}/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D`
    );
  });

  // docs/sdk/react-native.md: with offlineFallback: false, clearCache() plus a remount asks again
  // with the new attributes; either one alone does not.
  test('regression #1520: offlineFallback false, new attributes are asked after clearCache() plus a remount only', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(jsonResponse({ key: 'f', enabled: false, config: null }));
    const client = realClient();
    const states: FlagState[] = [];
    function Probe() {
      const state = useFlag('f');
      states.push(state);
      return <Text>{String(state.enabled)}</Text>;
    }
    // A new `key` on the provider remounts everything under it.
    const ui = (attributes: Record<string, unknown>, key: string) => (
      <ExperimentationProvider key={key} client={client} userId="u1" attributes={attributes}>
        <Probe />
      </ExperimentationProvider>
    );
    const urls = () => (global.fetch as jest.Mock).mock.calls.map(([url]) => url as string);

    const { rerender } = render(ui({ plan: 'free' }, 'first'));
    await waitFor(() => expect(settled(states[states.length - 1])).toBe(true));
    expect(urls()).toEqual([`${BASE_URL}/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22plan%22%3A%22free%22%7D`]);

    // New attributes and a remount, no clearCache(): the memory cache answers.
    rerender(ui({ plan: 'pro' }, 'second'));
    await waitFor(() => expect(settled(states[states.length - 1])).toBe(true));
    expect(urls()).toHaveLength(1);

    // clearCache(), no remount: the mounted hook does not ask again.
    client.clearCache();
    rerender(ui({ plan: 'pro' }, 'second'));
    await act(async () => {});
    expect(urls()).toHaveLength(1);

    // clearCache() and a remount: one request, with the new attributes.
    rerender(ui({ plan: 'pro' }, 'third'));
    await waitFor(() => expect(urls()).toHaveLength(2));
    expect(urls()[1]).toBe(`${BASE_URL}/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D`);
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

// ---------------------------------------------------------------------------
// useFlagValue (#1810), with a real ExperimentationClient and a mocked fetch
// ---------------------------------------------------------------------------

describe('useFlagValue', () => {
  const BASE_URL = 'https://api.example.com';

  function jsonResponse(body: unknown, status = 200) {
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  }

  function valueBody(key: string, value: unknown, valueType: string, enabled = true) {
    return { key, enabled, config: null, value, value_type: valueType };
  }

  function realClient(overrides: Partial<SdkConfig> = {}) {
    return new ExperimentationClient({ apiKey: 'k', baseUrl: BASE_URL, offlineFallback: false, ...overrides });
  }

  beforeEach(async () => {
    global.fetch = jest.fn();
    await AsyncStorage.clear();
  });

  afterAll(async () => {
    await AsyncStorage.clear();
  });

  test('starts with the default and loading true, then answers the value the server serves', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(jsonResponse(valueBody('headline', 'on-copy', 'string')));
    const { states, latest } = renderHookInProvider(() => useFlagValue('headline', 'string', 'Welcome'), {
      client: realClient(),
      userId: 'u1',
    });

    expect(states[0]).toEqual({ value: 'Welcome', loading: true, error: null });
    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest()).toEqual({ value: 'on-copy', loading: false, error: null });
    expect((global.fetch as jest.Mock).mock.calls.map(([url]) => url)).toEqual([
      `${BASE_URL}/api/v1/feature-flags/evaluate/headline?user_id=u1`,
    ]);
  });

  test('sends its own attributes as context, else the provider attributes', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(jsonResponse(valueBody('f', 10, 'number')));
    const { latest } = renderHookInProvider(() => useFlagValue('f', 'number', -1, { plan: 'pro' }), {
      client: realClient(),
      userId: 'u1',
      attributes: { plan: 'free' },
    });
    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest().value).toBe(10);
    expect((global.fetch as jest.Mock).mock.calls[0][0]).toBe(
      `${BASE_URL}/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D`
    );
  });

  test('another type: the default, a FlagValueError TYPE_MISMATCH, and one onError with "value"', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(jsonResponse(valueBody('f', 10, 'number')));
    const onError = jest.fn();
    const { latest } = renderHookInProvider(() => useFlagValue('f', 'string', 'fb'), {
      client: realClient({ onError }),
      userId: 'u1',
    });
    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest().value).toBe('fb');
    expect(latest().error).toBeInstanceOf(FlagValueError);
    expect(latest().error?.code).toBe('TYPE_MISMATCH');
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][1]).toBe('value');
  });

  test('a failed request: the default, REQUEST_FAILED with the request error as cause, never a throw', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(jsonResponse({ detail: 'down' }, 503));
    const onError = jest.fn();
    const { latest } = renderHookInProvider(() => useFlagValue('f', 'json', { fallback: true }), {
      client: realClient({ onError }),
      userId: 'u1',
    });
    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest().value).toEqual({ fallback: true });
    expect(latest().error?.code).toBe('REQUEST_FAILED');
    expect(latest().error?.cause).toBeInstanceOf(ApiError);
    expect((latest().error?.cause as ApiError).status).toBe(503);
    // The client's onError gets the request's own error, once.
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toBe(latest().error?.cause);
  });

  test('a new inline default on every render asks once (the default is not a dependency)', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(jsonResponse(valueBody('f', { color: 'blue' }, 'json')));
    // cacheTtlMs 0: a second read would reach the client and the server again, not a cache.
    const client = realClient({ cacheTtlMs: 0 });
    const read = jest.spyOn(client, readFlagValue);
    const { latest, rerenderWith } = renderHookInProvider(
      () => useFlagValue('f', 'json', { color: 'none' }),
      { client, userId: 'u1' }
    );
    await waitFor(() => expect(settled(latest())).toBe(true));
    for (let i = 0; i < 3; i++) rerenderWith({ client, userId: 'u1' });
    await act(async () => {});

    expect(latest().value).toEqual({ color: 'blue' });
    expect(read).toHaveBeenCalledTimes(1);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  test('a JSON value keeps its identity across re-renders and is the hook\'s own copy', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(jsonResponse(valueBody('f', { color: 'blue' }, 'json')));
    const client = realClient();
    const { latest, rerenderWith } = renderHookInProvider(
      () => useFlagValue<{ color: string } | null>('f', 'json', null),
      { client, userId: 'u1' }
    );
    await waitFor(() => expect(settled(latest())).toBe(true));
    const first = latest().value as { color: string };
    rerenderWith({ client, userId: 'u1' });
    expect(latest().value).toBe(first);
    first.color = 'red';
    expect(await client.getJsonValue('f', 'u1', null)).toEqual({ color: 'blue' });
  });

  test('a changed flag key or user is never answered with the previous one\'s value', async () => {
    (global.fetch as jest.Mock).mockImplementation(async (url: string) => {
      const key = url.includes('/evaluate/name?') ? 'name' : 'title';
      const user = new URL(url).searchParams.get('user_id');
      return jsonResponse(valueBody(key, `${key}-${user}`, 'string'));
    });
    const client = realClient();
    let flagKey = 'title';
    const { states, latest, rerenderWith } = renderHookInProvider(() => useFlagValue(flagKey, 'string', 'fb'), {
      client,
      userId: 'alice',
    });
    await waitFor(() => expect(latest().value).toBe('title-alice'));

    let before = states.length;
    flagKey = 'name';
    rerenderWith({ client, userId: 'alice' });
    await waitFor(() => expect(latest().value).toBe('name-alice'));
    expect(states.slice(before).map((s) => s.value)).not.toContain('title-alice');
    expect(states[before]).toEqual({ value: 'fb', loading: true, error: null });

    before = states.length;
    rerenderWith({ client, userId: 'bob' });
    await waitFor(() => expect(latest().value).toBe('name-bob'));
    expect(states.slice(before).map((s) => s.value)).not.toContain('name-alice');
    expect(states[before]).toEqual({ value: 'fb', loading: true, error: null });
  });

  test('useFlag().value stays the boolean alias of enabled for a string flag', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(jsonResponse(valueBody('f', 'on-copy', 'string')));
    const { latest } = renderHookInProvider(() => useFlag('f'), { client: realClient(), userId: 'u1' });
    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest().value).toBe(true);
    expect(latest().enabled).toBe(true);
  });

  test('an answer stored by 0.1 (no value) gives the default and INVALID_RESPONSE', async () => {
    const storage = new OfflineStorage();
    await storage.setFlag('u1', 'f', { key: 'f', enabled: true, config: null }, 60_000);
    const client = new ExperimentationClient({ apiKey: 'k', baseUrl: BASE_URL }, storage);
    const { latest } = renderHookInProvider(() => useFlagValue('f', 'string', 'fb'), { client, userId: 'u1' });
    await waitFor(() => expect(settled(latest())).toBe(true));
    expect(latest().value).toBe('fb');
    expect(latest().error?.code).toBe('INVALID_RESPONSE');
    expect(global.fetch).not.toHaveBeenCalled();
  });

  // docs/sdk/react-native.md "Mocking the client": a plain object, not a client this SDK built.
  test('under the documented mock client it reads the mocked evaluateFlag, and never throws', async () => {
    const mockClientWith = (evaluateFlag: jest.Mock) =>
      ({
        evaluateFlag,
        getAssignment: jest.fn().mockResolvedValue({
          experimentKey: 'exp', userId: 'u1', variantId: 'v2', variantName: 'treatment', isControl: false, configuration: null,
        }),
        track: jest.fn().mockResolvedValue(undefined),
        clearCache: jest.fn(),
      }) as unknown as ExperimentationClient;

    const valued = jest
      .fn()
      .mockResolvedValue({ key: 'my-flag', enabled: true, config: null, value: 'on-copy', valueType: 'string' });
    const first = renderHookInProvider(() => useFlagValue('my-flag', 'string', 'fb'), {
      client: mockClientWith(valued),
      userId: 'u1',
    });
    await waitFor(() => expect(settled(first.latest())).toBe(true));
    expect(first.latest()).toEqual({ value: 'on-copy', loading: false, error: null });
    expect(valued.mock.calls[0]).toEqual(['my-flag', 'u1', undefined]);

    const plain = jest.fn().mockResolvedValue({ key: 'my-flag', enabled: true, config: null });
    const second = renderHookInProvider(() => useFlagValue('my-flag', 'string', 'fb'), {
      client: mockClientWith(plain),
      userId: 'u1',
    });
    await waitFor(() => expect(settled(second.latest())).toBe(true));
    expect(second.latest().value).toBe('fb');
    expect(second.latest().error?.code).toBe('INVALID_RESPONSE');

    const failing = jest.fn().mockRejectedValue(new Error('API error'));
    const third = renderHookInProvider(() => useFlagValue('my-flag', 'string', 'fb'), {
      client: mockClientWith(failing),
      userId: 'u1',
    });
    await waitFor(() => expect(settled(third.latest())).toBe(true));
    expect(third.latest().value).toBe('fb');
    expect(third.latest().error).toBeInstanceOf(FlagValueError);
    expect(third.latest().error?.code).toBe('REQUEST_FAILED');
    expect((third.latest().error?.cause as Error).message).toBe('API error');
  });

  test('ignores an answer that arrives after unmount', async () => {
    let resolve!: (r: unknown) => void;
    (global.fetch as jest.Mock).mockImplementation(() => new Promise((r) => (resolve = r)));
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    const { unmount, states } = renderHookInProvider(() => useFlagValue('f', 'string', 'fb'), {
      client: realClient(),
      userId: 'u1',
    });
    unmount();
    await act(async () => {
      resolve(jsonResponse(valueBody('f', 'on-copy', 'string')));
    });
    expect(states.every((s) => s.loading)).toBe(true);
    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});
