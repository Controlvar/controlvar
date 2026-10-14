/**
 * /results/[id] as the user guide describes it: the Bayesian panel, the
 * colour of the improvement column and the Live tab.
 *
 * The API is mocked at `apiFetch`, so the page, ResultsDashboard, the results
 * services, BayesianPanel, MetricComparisonTable and LiveResultsPanel run as
 * they do in the browser. `WebSocket` is replaced by a recorder.
 *
 * What is pinned:
 *  - with Bayesian analysis on, the last section of the Overview tab is the
 *    Bayesian panel: the decision, with its fixed label for each value, and
 *    per variant the chance it is best, the expected loss, the posterior mean
 *    and the credible interval; with no block, one line says it could not be
 *    computed;
 *  - the improvement of a significant variant is green when it is better and
 *    red when it is worse, and gray when it is not significant;
 *  - the Live tab opens the results stream of this experiment.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ResultDetailPage from '@/pages/results/[id]';
import { ApiError, apiFetch } from '@/services/api';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/results/[id]', query: { id: 'exp-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

jest.mock('recharts', () => {
  const OriginalModule = jest.requireActual('recharts');
  return {
    ...OriginalModule,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container">{children}</div>
    ),
  };
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

function variant(
  id: string,
  name: string,
  isControl: boolean,
  improvement: number | null,
  significant: boolean
) {
  return {
    variant_id: id,
    variant_name: name,
    is_control: isControl,
    sample_size: 1000,
    conversions: 100,
    mean: 0.1,
    std_dev: null,
    confidence_interval: null,
    p_value: isControl ? null : significant ? 0.001 : 0.4,
    adjusted_p_value: null,
    is_significant: significant,
    effect_size: null,
    effect_size_label: null,
    relative_improvement_pct: improvement,
    power: null,
    statistical_test_used: isControl ? null : 'z_test_proportions',
  };
}

const BAYESIAN = {
  is_enabled: true,
  decision: 'CONTINUE',
  variant_results: [
    {
      variant_key: 'control',
      posterior: {
        alpha: 101.0,
        beta: 901.0,
        mean: 0.1008,
        credible_interval_lower: 0.0828,
        credible_interval_upper: 0.1204,
      },
      probability_to_be_best: 0.25,
      expected_loss: 0.012,
      bayes_factor: null,
    },
    {
      variant_key: 'better',
      posterior: {
        alpha: 121.0,
        beta: 881.0,
        mean: 0.1208,
        credible_interval_lower: 0.1012,
        credible_interval_upper: 0.1418,
      },
      probability_to_be_best: 0.75,
      expected_loss: 0.002,
      bayes_factor: null,
    },
  ],
  seed: 7,
  n_samples: 100000,
  engine_version: '1.3.0',
};

const DECISION_LABELS: Record<string, string> = {
  CONTINUE: 'Continue',
  STOP_WINNER: 'Stop: a winner is clear',
  STOP_EQUIVALENT: 'Stop: the variants are equivalent',
  STOP_FUTILE: 'Stop: a meaningful difference is unlikely',
};

function results(overrides: Record<string, unknown> = {}) {
  return {
    experiment_id: 'exp-1',
    experiment_name: 'Checkout',
    status: 'ACTIVE',
    start_date: '2026-09-01T00:00:00Z',
    end_date: null,
    confidence_level: 0.95,
    correction_method: 'none',
    sample_size_adequate: true,
    computed_at: '2026-09-20T12:00:00Z',
    summary: {
      total_users: 4000,
      total_events: 400,
      duration_days: 14,
      has_winner: false,
      winning_variant_id: null,
      recommendation: 'CONTINUE_TESTING',
      recommendation_reason: 'Keep going.',
    },
    metrics: [
      {
        metric_id: 'm1',
        metric_name: 'Checkout',
        metric_type: 'conversion',
        is_primary: true,
        variants: [
          variant('v-ctrl', 'control', true, null, false),
          variant('v-up', 'better', false, 12.5, true),
          variant('v-down', 'worse', false, -9.0, true),
          variant('v-flat', 'unclear', false, 4.0, false),
        ],
      },
    ],
    srm: null,
    bayesian_results: null,
    ...overrides,
  };
}

function install(body: Record<string, unknown>, experiment: Record<string, unknown>) {
  mockedApiFetch.mockImplementation(
    routedApi([
      {
        path: '/api/v1/experiments/exp-1',
        handler: () => ({
          id: 'exp-1',
          correction_method: 'none',
          confidence_level: 0.95,
          ...experiment,
        }),
      },
      { path: '/api/v1/results/exp-1', handler: () => body },
      {
        path: '/api/v1/results/exp-1/daily',
        handler: () => ({ experiment_id: 'exp-1', metric_id: null, series: [] }),
      },
      {
        path: '/api/v1/results/exp-1/sample-size',
        handler: () => ({
          required_sample_size_per_variant: 3000,
          current_sample_size_per_variant: 1000,
          is_adequate: false,
          achieved_power: 0.4,
          days_to_significance: null,
          projected_completion_date: null,
          baseline_rate: 0.1,
          mde: 0.05,
          confidence_level: 0.95,
          power_target: 0.8,
        }),
      },
      {
        path: '/api/v1/results/exp-1/sequential',
        handler: () => {
          throw new ApiError({ status: 404, detail: 'not sequential' });
        },
      },
    ]) as unknown as typeof apiFetch
  );
}

async function renderLoaded() {
  const view = render(<ResultDetailPage />);
  await screen.findByTestId('results-dashboard');
  return view;
}

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

beforeEach(() => {
  mockedApiFetch.mockReset();
  RecordingSocket.opened = [];
  (global as unknown as { WebSocket: unknown }).WebSocket = RecordingSocket;
});

afterAll(() => {
  (global as unknown as { WebSocket: unknown }).WebSocket = realWebSocket;
});

describe('the Bayesian analysis on the Overview tab', () => {
  it('is the last section, with the decision and a row per variant', async () => {
    install(results({ bayesian_results: BAYESIAN }), { bayesian_enabled: true });
    const { container } = await renderLoaded();
    const panel = screen.getByRole('region', { name: 'Bayesian analysis (primary metric)' });
    expect(within(panel).getByTestId('bayesian-decision')).toHaveTextContent('Decision: Continue');
    const rows = within(panel).getAllByTestId('bayesian-row');
    // Variant, chance it is best, expected loss, posterior mean, credible interval.
    expect(rows.map((r) => Array.from(r.children).map((c) => c.textContent))).toEqual([
      ['control', '25.0%', '0.012', '10.1%', '8.3% – 12.0%'],
      ['better', '75.0%', '0.002', '12.1%', '10.1% – 14.2%'],
    ]);
    const overview = container.querySelector('#panel-overview') as HTMLElement;
    const sections = Array.from(overview.querySelectorAll(':scope > div > section'));
    expect(sections[sections.length - 1]).toBe(panel);
  });

  it.each(Object.entries(DECISION_LABELS))('names the decision %s "%s"', async (decision, label) => {
    install(results({ bayesian_results: { ...BAYESIAN, decision } }), { bayesian_enabled: true });
    await renderLoaded();
    expect(screen.getByTestId('bayesian-decision')).toHaveTextContent(`Decision: ${label}`);
  });

  it('says it could not be computed when it is on and the results carry none', async () => {
    install(results({ bayesian_results: null }), { bayesian_enabled: true });
    await renderLoaded();
    expect(screen.getByTestId('bayesian-unavailable')).toHaveTextContent(
      'Bayesian analysis is on for this experiment, but it could not be computed for these results.'
    );
    expect(screen.queryByTestId('bayesian-panel')).not.toBeInTheDocument();
  });
});

describe('the improvement column', () => {
  it('is green for a significant improvement, red for a significant degradation, gray otherwise', async () => {
    install(results(), { bayesian_enabled: false });
    await renderLoaded();
    const cell = (name: RegExp) => {
      const row = screen.getByRole('row', { name });
      return within(row).getByText(/^[+\-\u2212]\d+(\.\d+)?%$/);
    };
    expect(cell(/better/).closest('td')).toHaveClass('text-green-700');
    expect(cell(/worse/).closest('td')).toHaveClass('text-red-700');
    const unclear = cell(/unclear/).closest('td');
    expect(unclear).toHaveClass('text-slate-500');
    expect(unclear).not.toHaveClass('text-green-700');
    expect(unclear).not.toHaveClass('text-red-700');
  });
});

describe('the Live tab', () => {
  it('opens the results stream of this experiment', async () => {
    install(results(), { bayesian_enabled: false });
    await renderLoaded();
    expect(RecordingSocket.opened).toEqual([]);
    fireEvent.click(screen.getByRole('tab', { name: '⚡ Live' }));
    expect(screen.getByTestId('live-tab-panel')).toBeInTheDocument();
    await waitFor(() => expect(RecordingSocket.opened).toHaveLength(1));
    expect(RecordingSocket.opened[0]).toMatch(/\/api\/v1\/ws\/experiments\/exp-1\/results$/);
  });
});
