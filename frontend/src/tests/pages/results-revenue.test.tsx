/**
 * /results/[id] when the primary metric is a revenue metric analysed as
 * revenue per user (`analysis` "mean").
 *
 * The API is mocked at `apiFetch`, so the page, ResultsDashboard, the chart,
 * the table, BayesianPanel and SampleSizeMeter run as they do in the browser.
 *
 * What is pinned:
 *  - the chart is headed "<metric> — revenue per user";
 *  - the Bayesian panel says it compares the share of users who sent the
 *    metric's event (named from the experiment), not revenue per user;
 *  - the Sample Size tab says its plan is for that share, naming the event;
 *  - the table gives the difference per user and the revenue footnote;
 *  - a result cached before `analysis` existed is read from its test;
 *  - a conversion primary keeps its heading and gets none of these notes.
 */
import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
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

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

function variant(overrides: Record<string, unknown>) {
  return {
    variant_id: 'v',
    variant_name: 'V',
    is_control: false,
    sample_size: 2000,
    conversions: 120,
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

const REVENUE = {
  metric_id: 'm-rev',
  metric_name: 'Order value',
  metric_type: 'revenue',
  is_primary: true,
  lower_is_better: false,
  analysis: 'mean',
  variants: [
    variant({ variant_id: 'ctrl', variant_name: 'control', is_control: true, mean: 5.66, std_dev: 31.2 }),
    variant({
      variant_id: 'b',
      variant_name: 'one_page',
      mean: 6.85,
      std_dev: 34.9,
      p_value: 0.16,
      relative_improvement_pct: 21.02,
      statistical_test_used: 'welch_t_test',
      absolute_difference: 1.19,
      absolute_difference_ci: [-0.47, 2.84],
    }),
  ],
};

const CONVERSION = {
  metric_id: 'm-conv',
  metric_name: 'Checkout',
  metric_type: 'conversion',
  is_primary: true,
  lower_is_better: false,
  analysis: 'proportion',
  variants: [
    variant({ variant_id: 'ctrl', variant_name: 'control', is_control: true, mean: 0.1 }),
    variant({
      variant_id: 'b',
      variant_name: 'one_page',
      mean: 0.11,
      p_value: 0.4,
      relative_improvement_pct: 10,
      statistical_test_used: 'fisher_exact',
    }),
  ],
};

const BAYESIAN = {
  is_enabled: true,
  decision: 'CONTINUE',
  variant_results: [
    {
      variant_key: 'control',
      posterior: {
        alpha: 119.0,
        beta: 1883.0,
        mean: 0.0594,
        credible_interval_lower: 0.0494,
        credible_interval_upper: 0.0703,
      },
      probability_to_be_best: 0.2,
      expected_loss: 0.006,
      bayes_factor: null,
    },
    {
      variant_key: 'one_page',
      posterior: {
        alpha: 132.0,
        beta: 1870.0,
        mean: 0.0659,
        credible_interval_lower: 0.0554,
        credible_interval_upper: 0.0772,
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
    experiment_name: 'Checkout flow',
    status: 'ACTIVE',
    start_date: '2026-09-01T00:00:00Z',
    end_date: null,
    confidence_level: 0.95,
    correction_method: 'none',
    sample_size_adequate: true,
    computed_at: '2026-10-27T12:00:00Z',
    summary: {
      total_users: 4000,
      total_events: 249,
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
    required_sample_size_per_variant: 30000,
    current_sample_size_per_variant: 2000,
    is_adequate: false,
    achieved_power: 0.2,
    days_to_significance: null,
    projected_completion_date: null,
    baseline_rate: 0.059,
    mde: 0.05,
    mde_source: 'default',
    confidence_level: 0.95,
    power_target: 0.8,
    baseline_source: 'observed',
    baseline_users: 2000,
    metric_id: metric.metric_id,
    metric_name: metric.metric_name,
    metric_type: metric.metric_type,
    analysed_as: 'conversion',
    alpha: 0.05,
    comparisons: 1,
    correction_method: 'none',
    mde_absolute: 0.00295,
    unavailable_reason: null,
    guide_only_reasons: [],
  };
}

const EXPERIMENT_METRICS = [
  {
    id: 'm-rev',
    name: 'Order value',
    event_name: 'purchase',
    metric_type: 'revenue',
    is_primary: true,
    lower_is_better: false,
  },
  {
    id: 'm-conv',
    name: 'Checkout',
    event_name: 'checkout',
    metric_type: 'conversion',
    is_primary: false,
    lower_is_better: false,
  },
];

function install(metric: Record<string, unknown>, experimentReadable = true) {
  mockedApiFetch.mockImplementation(
    routedApi([
      {
        path: '/api/v1/experiments/exp-1',
        handler: () => {
          if (!experimentReadable) throw new ApiError({ status: 500, detail: 'down' });
          return {
            id: 'exp-1',
            correction_method: 'none',
            confidence_level: 0.95,
            bayesian_enabled: true,
            metrics: EXPERIMENT_METRICS,
          };
        },
      },
      { path: '/api/v1/results/exp-1', handler: () => results(metric) },
      {
        path: '/api/v1/results/exp-1/daily',
        handler: () => ({ experiment_id: 'exp-1', metric_id: null, series: [] }),
      },
      {
        path: '/api/v1/results/exp-1/sample-size',
        handler: () =>
          sampleSize(metric as { metric_id: string; metric_name: string; metric_type: string }),
      },
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

describe('a revenue primary on the results page', () => {
  it('heads the chart "Order value — revenue per user"', async () => {
    install(REVENUE);
    await renderLoaded();
    expect(
      screen.getByRole('heading', { name: 'Order value — revenue per user' }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Conversion Rates/)).not.toBeInTheDocument();
  });

  it('says the Bayesian analysis compares the share of users who sent purchase', async () => {
    install(REVENUE);
    await renderLoaded();
    const panel = screen.getByRole('region', { name: 'Bayesian analysis (primary metric)' });
    const note = within(panel).getByTestId('bayesian-share-note');
    expect(note).toHaveTextContent(
      /^The Bayesian analysis compares the share of users who sent purchase, not revenue per user\.$/,
    );
    expect(within(note).getByText('purchase').tagName).toBe('CODE');
  });

  it("names the metric's event in words when the experiment cannot be read", async () => {
    install(REVENUE, false);
    await renderLoaded();
    expect(screen.getByTestId('bayesian-share-note')).toHaveTextContent(
      "The Bayesian analysis compares the share of users who sent Order value's event, not revenue per user.",
    );
  });

  it('says on the Sample Size tab that the plan is for the share of users who send purchase', async () => {
    install(REVENUE);
    await renderLoaded();
    fireEvent.click(screen.getByRole('tab', { name: 'Sample Size' }));
    const note = await screen.findByTestId('sample-size-metric-type-note');
    expect(note).toHaveTextContent(
      'Order value is analysed as revenue per user, but this plan is for the share of users who send purchase (a two-proportion test).',
    );
  });

  it('gives the difference per user in the table, with the revenue footnote', async () => {
    install(REVENUE);
    await renderLoaded();
    const table = screen.getByTestId('metric-comparison-table');
    expect(table).toHaveTextContent('+21.0% +1.19 per user (95% CI −0.47 to +2.84)');
    expect(within(table).getByTestId('revenue-per-user-footnote')).toBeInTheDocument();
    expect(screen.getByTestId('results-dashboard').textContent).not.toContain('$');
  });

  it('reads a result cached before `analysis` from its test (welch_t_test is a mean)', async () => {
    const cached: Record<string, unknown> = { ...REVENUE };
    delete cached.analysis;
    install(cached);
    await renderLoaded();
    expect(
      screen.getByRole('heading', { name: 'Order value — revenue per user' }),
    ).toBeInTheDocument();
    expect(screen.getByTestId('bayesian-share-note')).toBeInTheDocument();
  });
});

describe('a conversion primary', () => {
  it('keeps its heading and has no revenue note anywhere', async () => {
    install(CONVERSION);
    await renderLoaded();
    expect(screen.getByRole('heading', { name: 'Conversion Rates — Checkout' })).toBeInTheDocument();
    expect(screen.queryByTestId('bayesian-share-note')).not.toBeInTheDocument();
    expect(screen.queryByTestId('revenue-per-user-footnote')).not.toBeInTheDocument();
    expect(screen.getByTestId('results-dashboard').textContent).not.toMatch(/per user/);
  });
});
