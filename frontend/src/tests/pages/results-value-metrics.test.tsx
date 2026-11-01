/**
 * /results/[id] when the primary metric is a count, duration or custom metric
 * analysed as its mean per assigned user (#439, `analysis` "mean").
 *
 * The API is mocked at `apiFetch`, so the page, ResultsDashboard, the chart,
 * the table, BayesianPanel and SampleSizeMeter run as they do in the browser.
 *
 * What is pinned (UX C1, C2, C4, C5, C6):
 *  - the chart is headed "<metric> — events per user" (duration per user,
 *    value per user), never "— mean";
 *  - its tooltip reads "Events per user" (...), never "Mean" or "Rate";
 *  - the Bayesian panel says it compares the share of users who sent the
 *    metric's event, "not events per user." (...);
 *  - the Sample Size tab says its plan is for that share, not the mean, and
 *    never that the metric is "analysed as a conversion";
 *  - the table has the type's footnote;
 *  - a count primary a server analysed as a share (one older than #439) is
 *    headed and planned as that share: the page reads `analysis`.
 */
import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import ResultDetailPage from '@/pages/results/[id]';
import { ApiError, apiFetch } from '@/services/api';
import { makeRouter, routedApi } from './helpers/apiMock';

// The shared recharts mock draws no axis or tooltip, so this file's mock
// renders what the chart's formatters make of one value.
jest.mock('recharts', () => {
  const shared = jest.requireActual('recharts');
  return {
    ...shared,
    YAxis: ({ tickFormatter }: { tickFormatter?: (v: number) => string }) => (
      <div data-testid="y-axis">{tickFormatter ? tickFormatter(1.5) : ''}</div>
    ),
    Tooltip: ({ formatter }: { formatter?: (v: unknown) => [string, string] }) => {
      const [value, label] = formatter ? formatter(1.5) : ['', ''];
      return (
        <div data-testid="tooltip">
          <span data-testid="tooltip-value">{value}</span>
          <span data-testid="tooltip-label">{label}</span>
        </div>
      );
    },
  };
});

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

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

type MeanType = 'count' | 'duration' | 'custom';

const CASES: Array<[MeanType, string, string, string]> = [
  // [type, metric name, event name, label]
  ['count', 'Articles read', 'article_read', 'events per user'],
  ['duration', 'Watch time', 'watch_session', 'duration per user'],
  ['custom', 'Points', 'points_earned', 'value per user'],
];

function variant(overrides: Record<string, unknown>) {
  return {
    variant_id: 'v',
    variant_name: 'V',
    is_control: false,
    sample_size: 100,
    conversions: 75,
    mean: 0,
    std_dev: null,
    confidence_interval: null,
    p_value: null,
    adjusted_p_value: null,
    is_significant: false,
    effect_size: null,
    effect_size_label: null,
    relative_improvement_pct: null,
    power: null,
    statistical_test_used: null,
    ...overrides,
  };
}

function metricResult(type: MeanType, name: string, analysis: 'mean' | 'proportion' = 'mean') {
  const mean = analysis === 'mean';
  return {
    metric_id: `m-${type}`,
    metric_name: name,
    metric_type: type,
    is_primary: true,
    lower_is_better: false,
    analysis,
    variants: [
      variant({
        variant_id: 'ctrl',
        variant_name: 'control',
        is_control: true,
        mean: mean ? 1.5 : 0.75,
        std_dev: mean ? 1.12 : 0.43,
      }),
      variant({
        variant_id: 'b',
        variant_name: 'treatment',
        conversions: 80,
        mean: mean ? 2.0 : 0.8,
        std_dev: mean ? 1.42 : 0.4,
        p_value: mean ? 0.0064 : 0.4985,
        is_significant: mean,
        relative_improvement_pct: mean ? 33.33 : 6.67,
        statistical_test_used: mean ? 'welch_t_test' : 'fisher_exact',
        absolute_difference: mean ? 0.5 : null,
        absolute_difference_ci: mean ? [0.14, 0.86] : null,
      }),
    ],
  };
}

const BAYESIAN = {
  is_enabled: true,
  decision: 'CONTINUE',
  variant_results: [
    {
      variant_key: 'control',
      posterior: {
        alpha: 76.0,
        beta: 26.0,
        mean: 0.745,
        credible_interval_lower: 0.66,
        credible_interval_upper: 0.82,
      },
      probability_to_be_best: 0.2,
      expected_loss: 0.006,
      bayes_factor: null,
    },
    {
      variant_key: 'treatment',
      posterior: {
        alpha: 81.0,
        beta: 21.0,
        mean: 0.794,
        credible_interval_lower: 0.71,
        credible_interval_upper: 0.86,
      },
      probability_to_be_best: 0.8,
      expected_loss: 0.001,
      bayes_factor: null,
    },
  ],
  seed: 7,
  n_samples: 100000,
  engine_version: '1.5.0',
};

function results(metric: Record<string, unknown>) {
  return {
    experiment_id: 'exp-1',
    experiment_name: 'Reading',
    status: 'ACTIVE',
    start_date: '2026-09-01T00:00:00Z',
    end_date: null,
    confidence_level: 0.95,
    correction_method: 'none',
    sample_size_adequate: true,
    computed_at: '2026-10-31T12:00:00Z',
    summary: {
      total_users: 200,
      total_events: 350,
      duration_days: 14,
      has_winner: false,
      winning_variant_id: null,
      recommendation: 'CONTINUE_TESTING',
      recommendation_reason: 'Keep going.',
    },
    metrics: [metric],
    srm: null,
    bayesian_results: BAYESIAN,
  };
}

function sampleSize(metric: { metric_id: string; metric_name: string; metric_type: string }) {
  return {
    required_sample_size_per_variant: 3000,
    current_sample_size_per_variant: 100,
    is_adequate: false,
    achieved_power: 0.2,
    days_to_significance: null,
    projected_completion_date: null,
    baseline_rate: 0.75,
    mde: 0.05,
    mde_source: 'default',
    confidence_level: 0.95,
    power_target: 0.8,
    baseline_source: 'observed',
    baseline_users: 100,
    metric_id: metric.metric_id,
    metric_name: metric.metric_name,
    metric_type: metric.metric_type,
    analysed_as: 'conversion',
    alpha: 0.05,
    comparisons: 1,
    correction_method: 'none',
    mde_absolute: 0.0375,
    unavailable_reason: null,
    guide_only_reasons: [],
  };
}

function install(metric: { metric_id: string; metric_name: string; metric_type: string }, event: string) {
  mockedApiFetch.mockImplementation(
    routedApi([
      {
        path: '/api/v1/experiments/exp-1',
        handler: () => ({
          id: 'exp-1',
          correction_method: 'none',
          confidence_level: 0.95,
          bayesian_enabled: true,
          metrics: [
            {
              id: metric.metric_id,
              name: metric.metric_name,
              event_name: event,
              metric_type: metric.metric_type,
              is_primary: true,
              lower_is_better: false,
            },
          ],
        }),
      },
      { path: '/api/v1/results/exp-1', handler: () => results(metric) },
      {
        path: '/api/v1/results/exp-1/daily',
        handler: () => ({ experiment_id: 'exp-1', metric_id: null, series: [] }),
      },
      { path: '/api/v1/results/exp-1/sample-size', handler: () => sampleSize(metric) },
      {
        path: '/api/v1/results/exp-1/sequential',
        handler: () => {
          throw new ApiError({ status: 404, detail: 'not sequential' });
        },
      },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderLoaded() {
  const view = render(<ResultDetailPage />);
  await screen.findByTestId('results-dashboard');
  return view;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe.each(CASES)('a %s primary on the results page', (type, name, event, label) => {
  const capital = label.charAt(0).toUpperCase() + label.slice(1);

  it(`heads the chart "${name} — ${label}" and plots ${label}`, async () => {
    install(metricResult(type, name), event);
    await renderLoaded();
    expect(screen.getByRole('heading', { name: `${name} — ${label}` })).toBeInTheDocument();
    const chart = within(screen.getByTestId('conversion-chart'));
    expect(chart.getByTestId('tooltip-label').textContent).toBe(capital);
    expect(chart.getByTestId('tooltip-value').textContent).toBe('1.50');
    expect(screen.getByTestId('conversion-chart').textContent).not.toMatch(/%|\$|Rate|Mean/);
  });

  it(`says the Bayesian analysis compares the share of users who sent ${event}, not ${label}`, async () => {
    install(metricResult(type, name), event);
    await renderLoaded();
    const note = screen.getByTestId('bayesian-share-note');
    expect(note).toHaveTextContent(
      new RegExp(`^The Bayesian analysis compares the share of users who sent ${event}, not ${label}\\.$`),
    );
  });

  it('says on the Sample Size tab that the plan is for the share, not the mean', async () => {
    install(metricResult(type, name), event);
    await renderLoaded();
    fireEvent.click(screen.getByRole('tab', { name: 'Sample Size' }));
    const note = await screen.findByTestId('sample-size-metric-type-note');
    expect(note).toHaveTextContent(
      `${name} is analysed as ${label}, but this plan is for the share of users who send ${event} (a two-proportion test). ` +
        `A sample-size plan for ${label} is not available, and the Power Calculator plans conversion rates only.`,
    );
    expect(note).not.toHaveTextContent(/analysed as a conversion/);
  });

  it(`has the ${type} footnote under the table`, async () => {
    install(metricResult(type, name), event);
    await renderLoaded();
    const table = screen.getByTestId('metric-comparison-table');
    expect(within(table).getByTestId(`${type}-per-user-footnote`)).toBeInTheDocument();
    expect(table).toHaveTextContent('+0.50 per user (95% CI +0.14 to +0.86)');
  });
});

describe('a count primary a server analysed as a share (before #439)', () => {
  it('is headed and planned as the share of users, with no per-user words', async () => {
    install(metricResult('count', 'Articles read', 'proportion'), 'article_read');
    await renderLoaded();
    expect(
      screen.getByRole('heading', { name: 'Articles read — share of users with at least one event' }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId('bayesian-share-note')).not.toBeInTheDocument();
    expect(screen.queryByTestId('count-per-user-footnote')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Sample Size' }));
    const note = await screen.findByTestId('sample-size-metric-type-note');
    expect(note).toHaveTextContent('Articles read is a count metric, analysed as a conversion');
  });
});
