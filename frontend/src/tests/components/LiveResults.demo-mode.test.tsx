/**
 * The live results stream with demo mode off and on. A read-only demo's API
 * refuses the results socket, so the dashboard opens none there: the results
 * page has no Live tab, and the live panel neither connects nor renders.
 * `WebSocket` is replaced by a recorder; with demo mode off the same steps
 * open one, which shows the recorder sees a socket when there is one.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ResultsDashboard } from '@/components/results/ResultsDashboard/ResultsDashboard';
import { LiveResultsPanel } from '@/components/experiments/LiveResultsPanel';
import { AuthConfigProvider, __resetAuthConfigCache } from '@/contexts/AuthConfigContext';
import { ResultsService } from '@/services/results';
import { ExperimentsService } from '@/services/experiments';
import { ExperimentResultsResponse } from '@/types/results';
import { BOTH_STATES, CONFIG_BODY, ConfigReady, DemoState, jsonResponse, routedFetch } from '../helpers/demoMode';

jest.mock('@/services/results');
jest.mock('@/services/experiments');

const mockFetch = jest.fn();
global.fetch = mockFetch;

class RecordingSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static opened: string[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(url: string) {
    RecordingSocket.opened.push(url);
  }
  send() {}
  close() {
    this.readyState = 3;
  }
}

const realWebSocket = global.WebSocket;

const results: ExperimentResultsResponse = {
  experiment_id: 'exp-1',
  experiment_name: 'Checkout button',
  status: 'ACTIVE',
  start_date: '2026-11-01T00:00:00Z',
  end_date: null,
  confidence_level: 0.95,
  correction_method: 'bonferroni',
  sample_size_adequate: true,
  computed_at: '2026-11-10T12:00:00Z',
  summary: {
    total_users: 2000,
    total_events: 240,
    duration_days: 9,
    has_winner: false,
    winning_variant_id: null,
    recommendation: 'CONTINUE_TESTING',
    recommendation_reason: 'No statistically significant difference yet.',
  },
  metrics: [
    {
      metric_id: 'm1',
      metric_name: 'Conversion Rate',
      metric_type: 'conversion',
      is_primary: true,
      variants: [
        {
          variant_id: 'ctrl',
          variant_name: 'Control',
          is_control: true,
          sample_size: 1000,
          conversions: 120,
          mean: 0.12,
          std_dev: 0.05,
          confidence_interval: [0.1, 0.14],
          p_value: null,
          adjusted_p_value: null,
          is_significant: false,
          effect_size: null,
          effect_size_label: null,
          relative_improvement_pct: null,
          power: null,
        },
      ],
    },
  ],
};

function withConfig(state: DemoState, ui: React.ReactElement) {
  mockFetch.mockImplementation(routedFetch(CONFIG_BODY[state]));
  return render(
    <AuthConfigProvider>
      <ConfigReady />
      {ui}
    </AuthConfigProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockFetch.mockReset();
  __resetAuthConfigCache();
  RecordingSocket.opened = [];
  (global as unknown as { WebSocket: unknown }).WebSocket = RecordingSocket;
  (ResultsService.getResults as jest.Mock).mockResolvedValue(results);
  (ResultsService.getDailyResults as jest.Mock).mockResolvedValue({ experiment_id: 'exp-1', metric_id: null, series: [] });
  // The sample-size card stays loading; it has no part in this.
  (ResultsService.getSampleSize as jest.Mock).mockImplementation(() => new Promise(() => {}));
  // The experiment cannot be read: the results are then asked for without settings.
  (ExperimentsService.get as jest.Mock).mockRejectedValue(new Error('not needed here'));
});

afterAll(() => {
  (global as unknown as { WebSocket: unknown }).WebSocket = realWebSocket;
});

describe.each(BOTH_STATES)('the results page with demo mode %s', (state: DemoState) => {
  const on = state === 'on';

  it(on ? 'has no Live tab and opens no results stream' : 'opens the results stream from its Live tab', async () => {
    withConfig(state, <ResultsDashboard experimentId="exp-1" />);
    await screen.findByTestId('config-ready');
    await screen.findByRole('tablist');

    if (on) {
      expect(screen.queryByRole('tab', { name: /live/i })).not.toBeInTheDocument();
      // Every tab there is, opened in turn: none of them connects.
      for (const tab of screen.getAllByRole('tab')) fireEvent.click(tab);
      expect(screen.queryByTestId('live-tab-panel')).not.toBeInTheDocument();
      expect(screen.queryByTestId('live-results-panel')).not.toBeInTheDocument();
      expect(screen.queryByText(/lost connection|disconnected|reconnect/i)).not.toBeInTheDocument();
      expect(RecordingSocket.opened).toEqual([]);
    } else {
      expect(RecordingSocket.opened).toEqual([]);
      fireEvent.click(screen.getByRole('tab', { name: '⚡ Live' }));
      expect(screen.getByTestId('live-results-panel')).toBeInTheDocument();
      await waitFor(() => expect(RecordingSocket.opened).toHaveLength(1));
      expect(RecordingSocket.opened[0]).toMatch(/\/ws\/experiments\/exp-1\/results$/);
    }
  });
});

describe('before /auth/config has answered', () => {
  it('the live panel does not connect, and does not once a demo answers', async () => {
    let answer: (r: Response) => void = () => undefined;
    const config = new Promise<Response>((resolve) => {
      answer = resolve;
    });
    mockFetch.mockImplementation(() => config);
    render(
      <AuthConfigProvider>
        <ConfigReady />
        <LiveResultsPanel experimentId="exp-1" />
      </AuthConfigProvider>,
    );
    expect(RecordingSocket.opened).toEqual([]);
    expect(screen.queryByTestId('live-results-panel')).not.toBeInTheDocument();
    await act(async () => {
      answer(jsonResponse(200, CONFIG_BODY.on));
      await config;
    });
    await screen.findByTestId('config-ready');
    expect(RecordingSocket.opened).toEqual([]);
  });
});

describe.each(BOTH_STATES)('the live panel itself with demo mode %s', (state: DemoState) => {
  const on = state === 'on';

  it(on ? 'neither renders nor connects' : 'renders and connects', async () => {
    withConfig(state, <LiveResultsPanel experimentId="exp-1" />);
    await screen.findByTestId('config-ready');
    if (on) {
      expect(screen.queryByTestId('live-results-panel')).not.toBeInTheDocument();
      expect(RecordingSocket.opened).toEqual([]);
    } else {
      expect(screen.getByTestId('live-results-panel')).toBeInTheDocument();
      await waitFor(() => expect(RecordingSocket.opened).toHaveLength(1));
    }
  });
});
