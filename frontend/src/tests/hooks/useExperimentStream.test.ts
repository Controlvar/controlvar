import { renderHook, act } from '@testing-library/react';
import {
  buildStreamProtocols,
  buildStreamUrl,
  useExperimentStream,
  WS_AUTH_SUBPROTOCOL,
  WS_CLOSE_UNAUTHORIZED,
} from '@/hooks/useExperimentStream';
import { TOKEN_STORAGE_KEY } from '@/services/api';

// Mock WebSocket
class MockWebSocket {
  static OPEN = 1;
  static CLOSED = 3;

  url: string;
  protocols: string[] | undefined;
  readyState = MockWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((e?: { code: number }) => void) | null = null;
  sentMessages: string[] = [];
  closeCalled = false;

  constructor(url: string, protocols?: string[]) {
    this.url = url;
    this.protocols = protocols;
    // Simulate async open
    setTimeout(() => this.onopen?.(), 0);
  }

  send(data: string) {
    this.sentMessages.push(data);
  }

  close() {
    // Prevent delayed constructor-triggered open from racing after close.
    this.onopen = null;
    this.closeCalled = true;
    this.readyState = MockWebSocket.CLOSED;
    setTimeout(() => this.onclose?.(), 0);
  }
}

let wsInstances: MockWebSocket[] = [];

beforeEach(() => {
  wsInstances = [];
  (global as unknown as Record<string, unknown>).WebSocket = class extends MockWebSocket {
    constructor(url: string, protocols?: string[]) {
      super(url, protocols);
      wsInstances.push(this);
    }
  };
  // Set static properties on the mock constructor
  (global as unknown as Record<string, unknown>).WebSocket = Object.assign(
    (global as unknown as Record<string, typeof MockWebSocket>).WebSocket,
    { OPEN: 1, CLOSED: 3 },
  );
});

afterEach(() => {
  jest.restoreAllMocks();
  localStorage.clear();
  delete process.env.NEXT_PUBLIC_API_URL;
  delete process.env.NEXT_PUBLIC_WS_URL;
});

describe('buildStreamUrl', () => {
  it('derives ws origin from the page when the API is same-origin', () => {
    expect(buildStreamUrl('exp-1')).toBe('ws://localhost/api/v1/ws/experiments/exp-1/results');
  });

  it('derives wss origin from NEXT_PUBLIC_API_URL', () => {
    process.env.NEXT_PUBLIC_API_URL = 'https://api.example.com';
    expect(buildStreamUrl('exp-1')).toBe('wss://api.example.com/api/v1/ws/experiments/exp-1/results');
  });

  it('prefers NEXT_PUBLIC_WS_URL when set', () => {
    process.env.NEXT_PUBLIC_WS_URL = 'ws://stream.local:9000';
    expect(buildStreamUrl('exp-1')).toBe('ws://stream.local:9000/api/v1/ws/experiments/exp-1/results');
  });

  it('never puts the token in the URL (URLs end up in access logs)', () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok/123');
    expect(buildStreamUrl('exp-1')).toBe('ws://localhost/api/v1/ws/experiments/exp-1/results');
  });
});

describe('buildStreamProtocols', () => {
  it('is undefined without a token', () => {
    expect(buildStreamProtocols()).toBeUndefined();
  });

  it('offers the marker subprotocol followed by the token', () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'abc.def-ghi_jkl');
    expect(buildStreamProtocols()).toEqual([WS_AUTH_SUBPROTOCOL, 'abc.def-ghi_jkl']);
  });
});

describe('useExperimentStream', () => {
  it('returns disconnected status initially with no experimentId', () => {
    const { result } = renderHook(() => useExperimentStream(null));
    expect(result.current.status).toBe('disconnected');
    expect(result.current.snapshot).toBeNull();
  });

  it('auto-connects when experimentId is provided', async () => {
    const { result } = renderHook(() => useExperimentStream('exp-1'));
    expect(result.current.status).toBe('connecting');
    expect(wsInstances.length).toBe(1);
    expect(wsInstances[0].url).toContain('/api/v1/ws/experiments/exp-1/results');
    expect(wsInstances[0].url).not.toContain('token=');

    // Simulate open
    await act(async () => {
      wsInstances[0].onopen?.();
    });
    expect(result.current.status).toBe('connected');
  });

  it('sends the token as a subprotocol, not in the URL, when one is stored', () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'abc');
    renderHook(() => useExperimentStream('exp-2'));
    expect(wsInstances[0].url).toBe('ws://localhost/api/v1/ws/experiments/exp-2/results');
    expect(wsInstances[0].protocols).toEqual([WS_AUTH_SUBPROTOCOL, 'abc']);
  });

  it('opens the socket without a protocol list when there is no token', () => {
    renderHook(() => useExperimentStream('exp-2'));
    expect(wsInstances[0].protocols).toBeUndefined();
  });

  it('stops reconnecting and reports unauthorized on close code 4401', async () => {
    jest.useFakeTimers();
    try {
      const { result } = renderHook(() =>
        useExperimentStream('exp-1', { reconnectDelayMs: 10, maxReconnectAttempts: 5 }),
      );
      await act(async () => {
        jest.advanceTimersByTime(0);
      });
      expect(result.current.status).toBe('connected');

      // The backend accepts, then closes 4401: onopen already fired (count reset).
      await act(async () => {
        wsInstances[0].onclose?.({ code: WS_CLOSE_UNAUTHORIZED });
      });
      expect(result.current.status).toBe('unauthorized');
      expect(result.current.error).toMatch(/not authorized/i);

      await act(async () => {
        jest.advanceTimersByTime(1000);
      });
      expect(wsInstances.length).toBe(1); // no reconnect attempt
    } finally {
      jest.useRealTimers();
    }
  });

  it('stops retrying and reports the refusal on close code 4403', async () => {
    jest.useFakeTimers();
    try {
      const { result } = renderHook(() =>
        useExperimentStream('exp-1', { reconnectDelayMs: 10, maxReconnectAttempts: 5 }),
      );
      await act(async () => {
        jest.advanceTimersByTime(0);
      });
      expect(result.current.status).toBe('connected');

      // The backend accepts, then closes 4403: the caller may not view the
      // experiment, and a new socket with the same token would get the same.
      await act(async () => {
        wsInstances[0].onclose?.({ code: 4403 });
      });
      await act(async () => {
        jest.advanceTimersByTime(1000);
      });
      expect(wsInstances).toHaveLength(1); // no retry
      expect(result.current.status).toBe('unauthorized');
      expect(result.current.error).toMatch(/code 4403/);
      expect(result.current.error).not.toMatch(/sign in again/i);
    } finally {
      jest.useRealTimers();
    }
  });

  it('still reconnects after an ordinary close', async () => {
    jest.useFakeTimers();
    try {
      const { result } = renderHook(() =>
        useExperimentStream('exp-1', { reconnectDelayMs: 10, maxReconnectAttempts: 5 }),
      );
      await act(async () => {
        jest.advanceTimersByTime(0);
      });
      await act(async () => {
        wsInstances[0].onclose?.({ code: 1006 });
      });
      expect(result.current.status).toBe('connecting');
      await act(async () => {
        jest.advanceTimersByTime(20);
      });
      expect(wsInstances.length).toBe(2);
    } finally {
      jest.useRealTimers();
    }
  });

  it('does not auto-connect when autoConnect is false', () => {
    renderHook(() => useExperimentStream('exp-1', { autoConnect: false }));
    expect(wsInstances.length).toBe(0);
  });

  it('parses snapshot from message', async () => {
    const { result } = renderHook(() => useExperimentStream('exp-1'));

    await act(async () => {
      wsInstances[0].onopen?.();
    });

    const raw = {
      event: 'results_update',
      experiment_id: 'exp-1',
      timestamp: '2024-06-01T00:00:00Z',
      status: 'active',
      variants: [
        {
          key: 'control',
          name: 'Control',
          participant_count: 500,
          conversion_count: 50,
          conversion_rate: 0.1,
          relative_lift: 0,
          p_value: null,
          is_control: true,
        },
      ],
      total_participants: 500,
      days_running: 3,
      is_significant: false,
    };

    await act(async () => {
      wsInstances[0].onmessage?.({ data: JSON.stringify(raw) });
    });

    expect(result.current.snapshot).not.toBeNull();
    expect(result.current.snapshot!.experimentId).toBe('exp-1');
    expect(result.current.snapshot!.totalParticipants).toBe(500);
    expect(result.current.snapshot!.variants[0].participantCount).toBe(500);
    expect(result.current.snapshot!.variants[0].isControl).toBe(true);
  });

  it('sends refresh action', async () => {
    const { result } = renderHook(() => useExperimentStream('exp-1'));

    await act(async () => { wsInstances[0].onopen?.(); });
    act(() => { result.current.refresh(); });

    expect(wsInstances[0].sentMessages).toContain(JSON.stringify({ action: 'refresh' }));
  });

  it('sends ping action', async () => {
    const { result } = renderHook(() => useExperimentStream('exp-1'));

    await act(async () => { wsInstances[0].onopen?.(); });
    act(() => { result.current.ping(); });

    expect(wsInstances[0].sentMessages).toContain(JSON.stringify({ action: 'ping' }));
  });

  it('disconnect sets status to disconnected', async () => {
    const { result } = renderHook(() => useExperimentStream('exp-1'));

    await act(async () => { wsInstances[0].onopen?.(); });
    expect(result.current.status).toBe('connected');

    await act(async () => { result.current.disconnect(); });
    expect(result.current.status).toBe('disconnected');
  });

  it('sets error status on WebSocket error', async () => {
    const { result } = renderHook(() => useExperimentStream('exp-1'));

    // The mock schedules its simulated `onopen` with setTimeout(0). Let that
    // fire first, otherwise it can land after the error below and flip the
    // status back to "connected" (this made the test flaky in CI).
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(result.current.status).toBe('connected');

    await act(async () => {
      wsInstances[0].onerror?.();
    });

    expect(result.current.status).toBe('error');
    expect(result.current.error).toBe('WebSocket connection error');
  });

  it('manual connect works after autoConnect false', async () => {
    const { result } = renderHook(() =>
      useExperimentStream('exp-1', { autoConnect: false }),
    );

    expect(wsInstances.length).toBe(0);
    act(() => { result.current.connect(); });
    expect(wsInstances.length).toBe(1);
  });

  it('handles malformed message gracefully', async () => {
    const spy = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const { result } = renderHook(() => useExperimentStream('exp-1'));

    await act(async () => { wsInstances[0].onopen?.(); });
    await act(async () => {
      wsInstances[0].onmessage?.({ data: 'not-json' });
    });

    expect(result.current.snapshot).toBeNull();
    spy.mockRestore();
  });

  it('cleans up on unmount', async () => {
    const { unmount } = renderHook(() => useExperimentStream('exp-1'));

    await act(async () => { wsInstances[0].onopen?.(); });
    unmount();

    expect(wsInstances[0].closeCalled).toBe(true);
  });

  it('does not connect with null experimentId via manual connect', () => {
    const { result } = renderHook(() => useExperimentStream(null));
    act(() => { result.current.connect(); });
    expect(wsInstances.length).toBe(0);
  });
});
