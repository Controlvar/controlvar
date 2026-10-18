/**
 * /results/[id]: the Sequential tab shows the beta notice the API sends.
 *
 * The API is mocked at `apiFetch`, so the page, ResultsDashboard, the results
 * service and SequentialMonitor all run as they do in the browser. The
 * sequential fixture carries the notice text the backend's analysis-status
 * table produces (backend/app/core/analysis_status.py, "sequential").
 */
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import ResultDetailPage from '@/pages/results/[id]';
import { apiFetch } from '@/services/api';
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

const SEQUENTIAL_NOTICE =
  'Beta: the stop/continue decision is mSPRT alone, at the significance ' +
  'level shown by the boundary (1/alpha). alpha_spending is always empty: ' +
  'the planned-looks (alpha-spending) table is not computed yet. ' +
  'https://github.com/Controlvar/controlvar/issues/232';

const RESULTS = {
  experiment_id: 'exp-1',
  experiment_name: 'Checkout button colour',
  status: 'ACTIVE',
  start_date: '2026-09-01T00:00:00Z',
  end_date: null,
  confidence_level: 0.95,
  correction_method: 'bonferroni',
  sample_size_adequate: false,
  computed_at: '2026-09-20T12:00:00Z',
  summary: {
    total_users: 2000,
    total_events: 240,
    duration_days: 21,
    has_winner: false,
    winning_variant_id: null,
    recommendation: 'CONTINUE_TESTING',
    recommendation_reason: 'No statistically significant difference yet.',
  },
  metrics: [],
};

const SEQUENTIAL = {
  method: 'msprt',
  msprt_result: {
    lambda_ratio: 2.1,
    always_valid_p_value: 0.48,
    can_stop: false,
    evidence_strength: 'inconclusive',
    boundary: 20.0,
  },
  confidence_sequence: null,
  evidence_trajectory: [],
  alpha_spending: [],
  long_running_risk: {
    is_at_risk: true,
    expected_duration_days: 14,
    actual_duration_days: 21,
    risk_ratio: 1.5,
    recommendation:
      'Experiment has exceeded 1.5x its expected duration. Running long is not ' +
      'evidence of no effect; consider increasing traffic allocation or revisiting ' +
      'the expected duration.',
  },
  recommended_action: 'continue',
  at_risk: true,
  analysis_status: 'beta',
  analysis_notice: SEQUENTIAL_NOTICE,
};

function install() {
  mockedApiFetch.mockImplementation(
    routedApi([
      // The Sequential tab comes from the dedicated route below only because
      // the experiment says sequential testing is on and the results carry no
      // block; with it off, that route is never asked (#919).
      {
        path: '/api/v1/experiments/exp-1',
        handler: () => ({ id: 'exp-1', sequential_testing_enabled: true }),
      },
      { path: '/api/v1/results/exp-1', handler: () => RESULTS },
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
          baseline_rate: 0.12,
          mde: 0.02,
          confidence_level: 0.95,
          power_target: 0.8,
        }),
      },
      { path: '/api/v1/results/exp-1/sequential', handler: () => SEQUENTIAL },
    ]) as unknown as typeof apiFetch
  );
}

async function openSequentialTab() {
  render(<ResultDetailPage />);
  const tab = await screen.findByRole('tab', { name: /sequential/i });
  await userEvent.click(tab);
  await waitFor(() => expect(screen.getByTestId('sequential-monitor')).toBeInTheDocument());
}

describe('ResultDetailPage (/results/[id]) sequential tab', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    install();
  });

  it('shows the beta notice the API sends, with a Beta label and the issue link', async () => {
    await openSequentialTab();

    expect(
      screen.getByText(/the stop\/continue decision is mSPRT alone/i)
    ).toBeInTheDocument();
    const notice = screen.getByRole('region', { name: /beta/i });
    expect(within(notice).getByText('Beta')).toBeInTheDocument();
    expect(within(notice).getByRole('link', { name: /issue #232/i })).toHaveAttribute(
      'href',
      'https://github.com/Controlvar/controlvar/issues/232'
    );
    expect(notice).not.toHaveAttribute('role', 'alert');
  });

  it('shows at_risk as an advisory note, not an alert or a stop recommendation', async () => {
    await openSequentialTab();

    const note = screen.getByTestId('long-running-risk');
    expect(note).toHaveAttribute('role', 'note');
    expect(note).toHaveTextContent(/not a reason to stop/i);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('action-label')).toHaveTextContent(/continue testing/i);
  });
});

// #580: the page reads the experiment and sends its stored settings; the
// results it then shows are judged with them. Through apiFetch, so the page,
// the dashboard and both services run as in the browser.
describe('ResultDetailPage (/results/[id]) with stored analysis settings', () => {
  function treatment(id: string, name: string, p: number, adjusted: number, significant: boolean) {
    return {
      variant_id: id,
      variant_name: name,
      is_control: false,
      sample_size: 1000,
      conversions: 140,
      mean: 0.14,
      std_dev: null,
      confidence_interval: [0.12, 0.16],
      p_value: p,
      adjusted_p_value: adjusted,
      is_significant: significant,
      effect_size: null,
      effect_size_label: null,
      relative_improvement_pct: 16.7,
      power: null,
      statistical_test_used: 'fisher_exact',
    };
  }

  const THREE_VARIANTS = {
    ...RESULTS,
    correction_method: 'benjamini_hochberg',
    confidence_level: 0.9,
    metrics: [
      {
        metric_id: 'm-1',
        metric_name: 'Purchase',
        metric_type: 'conversion',
        is_primary: true,
        variants: [
          {
            ...treatment('ctrl', 'Control', 0, 0, false),
            is_control: true,
            p_value: null,
            adjusted_p_value: null,
            relative_improvement_pct: null,
            statistical_test_used: null,
          },
          treatment('b', 'Blue', 0.035, 0.071, true),
          treatment('c', 'Green', 0.2, 0.2, false),
        ],
      },
    ],
  };

  function installStored() {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments/exp-1',
          handler: () => ({ id: 'exp-1', correction_method: 'benjamini_hochberg', confidence_level: 0.9 }),
        },
        { path: '/api/v1/results/exp-1', handler: () => THREE_VARIANTS },
        {
          path: '/api/v1/results/exp-1/daily',
          handler: () => ({ experiment_id: 'exp-1', metric_id: null, series: [] }),
        },
        {
          path: '/api/v1/results/exp-1/sample-size',
          handler: () => {
            throw new Error('not needed here');
          },
        },
        {
          path: '/api/v1/results/exp-1/sequential',
          handler: () => {
            throw new Error('not sequential');
          },
        },
      ]) as unknown as typeof apiFetch
    );
  }

  beforeEach(() => {
    mockedApiFetch.mockReset();
    installStored();
  });

  it('sends the stored settings and shows the adjusted p-values with the footnote, and no one-release notice (#821)', async () => {
    render(<ResultDetailPage />);
    await screen.findByTestId('experiment-summary');
    const resultsCall = mockedApiFetch.mock.calls.find(([p]) => p === '/api/v1/results/exp-1');
    expect(resultsCall?.[1]?.query).toMatchObject({
      correction_method: 'benjamini_hochberg',
      confidence_level: 0.9,
    });
    expect(screen.getByTestId('analysis-summary-text')).toHaveTextContent(
      '90% confidence · Benjamini-Hochberg correction for the 2 comparisons with the control on each metric'
    );
    expect(screen.getByRole('columnheader', { name: /^adjusted p-value/i })).toBeInTheDocument();
    expect(screen.getByText('unadjusted 0.0350')).toBeInTheDocument();
    expect(screen.getByTestId('adjusted-p-footnote')).toHaveTextContent('below 0.1.');
    expect(screen.queryByTestId('corrected-results-notice')).not.toBeInTheDocument();
  });

  it('has no axe violations on the Overview (axe-core in jsdom; colour contrast is not computable here)', async () => {
    const { container } = render(<ResultDetailPage />);
    await screen.findByTestId('adjusted-p-footnote');
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual(
      []
    );
  });
});
