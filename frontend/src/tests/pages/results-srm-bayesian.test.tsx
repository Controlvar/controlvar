/**
 * /results/[id]: the sample-ratio check and the Bayesian analysis (#442, PR A).
 *
 * The API is mocked at `apiFetch`, so the page, ResultsDashboard, both
 * services, SrmNotice and BayesianPanel run as they do in the browser. The
 * fixtures follow the shapes `GET /results/{id}` returns (SRMResult and
 * BayesianResultsResponse in docs/api/openapi-v1.stable.json); the Bayesian
 * numbers are the ones a seeded ShopLab experiment returned from a real API.
 *
 * What is pinned:
 *  - the notice follows the server's `warning`, never a threshold of its own;
 *    a null check renders nothing, and nothing says "passed" for it;
 *  - under a mismatch the "Leading" chip, the Bayesian panel and the
 *    Sequential tab's stopping banner ("Ready to stop", #1737) carry a
 *    qualifier, and without one they do not;
 *  - every Bayesian decision the API can send has fixed copy (the set is read
 *    from the API snapshot), and the panel's empty states are distinct;
 *  - a failed load shows fixed copy, never the server's text, and never an
 *    empty page;
 *  - the page asks for nothing new: the same five requests as before;
 *  - axe finds nothing in any of these states.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import ResultDetailPage from '@/pages/results/[id]';
import { ApiError, apiFetch } from '@/services/api';
import { BAYESIAN_DECISION_LABELS } from '@/components/results/Bayesian/BayesianPanel';
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

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const STABLE_SNAPSHOT = path.join(REPO_ROOT, 'docs', 'api', 'openapi-v1.stable.json');

function variant(id: string, name: string, isControl: boolean, n: number, conv: number) {
  return {
    variant_id: id,
    variant_name: name,
    is_control: isControl,
    sample_size: n,
    conversions: conv,
    mean: conv / n,
    std_dev: null,
    confidence_interval: null,
    p_value: isControl ? null : 0.001,
    adjusted_p_value: null,
    is_significant: !isControl,
    effect_size: null,
    effect_size_label: null,
    relative_improvement_pct: isControl ? null : 25,
    power: null,
    statistical_test_used: isControl ? null : 'z_test_proportions',
  };
}

/** 600/400 on a 50/50 split: chi-square 40, p about 2.5e-10. */
const SRM_MISMATCH = {
  chi2: 40.0,
  p_value: 2.5e-10,
  warning: true,
  expected: { 'v-ctrl': 500.0, 'v-treat': 500.0 },
  observed: { 'v-ctrl': 600, 'v-treat': 400 },
};

/** A p-value of 0.01 is far from 0.001: no notice, whatever a client might think. */
const SRM_FINE = {
  chi2: 6.6,
  p_value: 0.01,
  warning: false,
  expected: { 'v-ctrl': 500.0, 'v-treat': 500.0 },
  observed: { 'v-ctrl': 540, 'v-treat': 460 },
};

/** From a seeded ShopLab experiment (shoplab_hero_banner) on a real API. */
const BAYESIAN = {
  is_enabled: true,
  decision: 'STOP_WINNER',
  variant_results: [
    {
      variant_key: 'control',
      posterior: {
        alpha: 238.0,
        beta: 1791.0,
        mean: 0.1172991621488418,
        credible_interval_lower: 0.10366364534630707,
        credible_interval_upper: 0.1316491467037665,
      },
      probability_to_be_best: 0.00039,
      expected_loss: 0.03660356161655528,
      bayes_factor: null,
    },
    {
      variant_key: 'video_hero',
      posterior: {
        alpha: 304.0,
        beta: 1671.0,
        mean: 0.1539240506329114,
        credible_interval_lower: 0.1383486936044925,
        credible_interval_upper: 0.17016321024310654,
      },
      probability_to_be_best: 0.99961,
      expected_loss: 1.335028136287103e-6,
      bayes_factor: null,
    },
  ],
  // The API sent 6167842191318436840; JSON.parse gives this, which is why it is never shown.
  seed: 6167842191318437000,
  n_samples: 100000,
  engine_version: '1.3.0',
};

function results(overrides: Record<string, unknown> = {}) {
  return {
    experiment_id: 'exp-1',
    experiment_name: 'Hero banner',
    status: 'ACTIVE',
    start_date: '2026-09-01T00:00:00Z',
    end_date: null,
    confidence_level: 0.95,
    correction_method: 'none',
    sample_size_adequate: true,
    computed_at: '2026-09-20T12:00:00Z',
    summary: {
      total_users: 1000,
      total_events: 200,
      duration_days: 14,
      has_winner: true,
      winning_variant_id: 'v-treat',
      recommendation: 'INCONCLUSIVE',
      recommendation_reason: 'Planted reason.',
    },
    metrics: [
      {
        metric_id: 'm1',
        metric_name: 'Hero CTA click',
        metric_type: 'conversion',
        is_primary: true,
        variants: [variant('v-ctrl', 'control', true, 600, 60), variant('v-treat', 'video_hero', false, 400, 50)],
      },
    ],
    srm: null,
    bayesian_results: null,
    ...overrides,
  };
}

const SAMPLE_SIZE = {
  required_sample_size_per_variant: 3000,
  current_sample_size_per_variant: 400,
  is_adequate: false,
  achieved_power: 0.4,
  days_to_significance: null,
  projected_completion_date: null,
  baseline_rate: 0.1,
  mde: 0.05,
  confidence_level: 0.95,
  power_target: 0.8,
};

function install(
  body: Record<string, unknown> | (() => never),
  experiment: Record<string, unknown> | null = { bayesian_enabled: false }
) {
  mockedApiFetch.mockImplementation(
    routedApi([
      {
        path: '/api/v1/experiments/exp-1',
        handler: () => {
          if (experiment === null) throw new ApiError({ status: 500, detail: 'no experiment' });
          return { id: 'exp-1', correction_method: 'none', confidence_level: 0.95, ...experiment };
        },
      },
      {
        path: '/api/v1/results/exp-1',
        handler: () => (typeof body === 'function' ? body() : body),
      },
      {
        path: '/api/v1/results/exp-1/daily',
        handler: () => ({ experiment_id: 'exp-1', metric_id: null, series: [] }),
      },
      { path: '/api/v1/results/exp-1/sample-size', handler: () => SAMPLE_SIZE },
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

async function expectNoAxeViolations(container: HTMLElement) {
  const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
  expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual(
    []
  );
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe('the sample-ratio check follows the server, not a threshold of its own', () => {
  it('a null check renders nothing, and nothing claims the split passed', async () => {
    install(results({ srm: null }));
    await renderLoaded();
    expect(screen.queryByTestId('srm-notice')).not.toBeInTheDocument();
    expect(screen.queryByTestId('srm-check-passed')).not.toBeInTheDocument();
    expect(screen.queryByTestId('leading-srm-qualifier')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/sample ratio|traffic split/i);
    // The chip itself is still there: it is the qualifier that depends on the check.
    expect(screen.getByTestId('leading-variant')).toHaveTextContent('Leading: video_hero');
  });

  it('warning false at p = 0.01 shows one quiet line and no notice', async () => {
    install(results({ srm: SRM_FINE }));
    await renderLoaded();
    expect(screen.queryByTestId('srm-notice')).not.toBeInTheDocument();
    expect(screen.getByTestId('srm-check-passed')).toHaveTextContent(
      'Sample ratio check passed: the traffic split matches the allocation (p = 0.01).'
    );
    expect(screen.queryByTestId('leading-srm-qualifier')).not.toBeInTheDocument();
  });

  it('warning true shows the notice with names, counts and p, above the tabs', async () => {
    install(results({ srm: SRM_MISMATCH }));
    await renderLoaded();
    const notice = screen.getByRole('region', {
      name: /the traffic split does not match the allocation/i,
    });
    expect(notice).toHaveAttribute('data-testid', 'srm-notice');
    expect(notice).not.toHaveAttribute('role', 'alert');
    expect(within(notice).getAllByTestId('srm-count').map((li) => li.textContent)).toEqual([
      'control: 600 users (expected 500)',
      'video_hero: 400 users (expected 500)',
    ]);
    expect(within(notice).getByTestId('srm-p')).toHaveTextContent('p < 0.001');
    expect(notice).toHaveTextContent('Chi-square 40.0');
    expect(notice).toHaveTextContent(/including the recommendation/);
    expect(notice).not.toHaveTextContent(/allocation while the experiment was running/i);
    expect(within(notice).getByRole('link', { name: 'What a sample ratio mismatch is' })).toHaveAttribute(
      'href',
      expect.stringMatching(/guides\/user-guide(\.md|\/)#sample-ratio-check$/)
    );
    expect(screen.queryByTestId('srm-check-passed')).not.toBeInTheDocument();

    // Above the tabs: it qualifies every tab, not only the Overview.
    const tabs = screen.getByRole('tablist', { name: 'Results sections' });
    expect(notice.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('a variant id the results do not name is shown as the id', async () => {
    install(
      results({
        srm: {
          ...SRM_MISMATCH,
          expected: { ...SRM_MISMATCH.expected, 'v-gone': 0 },
          observed: { ...SRM_MISMATCH.observed, 'v-gone': 3 },
        },
      })
    );
    await renderLoaded();
    expect(screen.getAllByTestId('srm-count').map((li) => li.textContent)).toEqual([
      'control: 600 users (expected 500)',
      'video_hero: 400 users (expected 500)',
      'v-gone: 3 users (expected 0)',
    ]);
  });
});

describe('the "Leading" chip and the Bayesian panel are qualified under a mismatch', () => {
  it('both carry the qualifier when srm.warning is true', async () => {
    install(results({ srm: SRM_MISMATCH, bayesian_results: BAYESIAN }), { bayesian_enabled: true });
    await renderLoaded();
    expect(screen.getByTestId('leading-variant')).toHaveTextContent('Leading: video_hero');
    expect(screen.getByTestId('leading-srm-qualifier')).toHaveTextContent(
      'The traffic split does not match the allocation, so treat this with caution.'
    );
    expect(screen.getByTestId('bayesian-srm-qualifier')).toHaveTextContent(
      'The traffic split does not match the allocation, so treat this with caution.'
    );
  });

  it('neither carries it when the check passed', async () => {
    install(results({ srm: SRM_FINE, bayesian_results: BAYESIAN }), { bayesian_enabled: true });
    await renderLoaded();
    expect(screen.getByTestId('bayesian-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('leading-srm-qualifier')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bayesian-srm-qualifier')).not.toBeInTheDocument();
  });

  it('neither carries it when the check is null', async () => {
    install(results({ srm: null, bayesian_results: BAYESIAN }), { bayesian_enabled: true });
    await renderLoaded();
    expect(screen.getByTestId('bayesian-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('leading-srm-qualifier')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bayesian-srm-qualifier')).not.toBeInTheDocument();
  });

  it('adds no qualifier to the Ship pill or the crown (the server answers INCONCLUSIVE under a mismatch)', async () => {
    install(
      results({
        srm: SRM_MISMATCH,
        summary: { ...results().summary, recommendation: 'SHIP_VARIANT' },
      })
    );
    await renderLoaded();
    expect(screen.getByTestId('recommendation-pill')).toHaveTextContent(/^Ship Variant ✓$/);
    expect(screen.queryByTestId('leading-variant')).not.toBeInTheDocument();
    expect(screen.queryByTestId('leading-srm-qualifier')).not.toBeInTheDocument();
  });
});

// #1737: the Sequential tab's banner can say "Ready to stop" on the same split
// the notice above the tabs says not to trust. It follows the same field,
// `srm.warning` from GET /results/{id}, and no threshold of its own.
describe('the Sequential tab\'s stopping banner is qualified under a mismatch (#1737)', () => {
  const QUALIFIER = 'The traffic split does not match the allocation, so treat this with caution.';

  /** A sequential block; the interval's sign says which way the change went. */
  function sequential(action: string, lower = 0.012, upper = 0.034) {
    return {
      method: 'msprt',
      msprt_result: {
        lambda_ratio: 40,
        always_valid_p_value: 0.025,
        can_stop: action !== 'continue',
        evidence_strength: action === 'continue' ? 'inconclusive' : 'strong_for_effect',
        boundary: 20,
      },
      confidence_sequence: { lower, upper, width: upper - lower, sample_size: 20000 },
      evidence_trajectory: [],
      alpha_spending: [],
      long_running_risk: null,
      recommended_action: action,
      at_risk: false,
      analysis_status: 'beta',
      analysis_notice: 'Sequential testing is in beta.',
    };
  }

  /** The primary metric is a conversion where higher is better, so a rise reads "better". */
  const EXPERIMENT = {
    bayesian_enabled: false,
    sequential_testing_enabled: true,
    metrics: [
      {
        id: 'm1',
        name: 'Hero CTA click',
        event_name: 'hero_click',
        metric_type: 'conversion',
        is_primary: true,
        lower_is_better: false,
      },
    ],
  };

  async function openSequential(srm: unknown, block = sequential('stop_for_effect')) {
    install(results({ srm, sequential_testing: block }), EXPERIMENT);
    const { container } = await renderLoaded();
    fireEvent.click(await screen.findByRole('tab', { name: /sequential/i }));
    return { banner: await screen.findByTestId('early-stopping-banner'), container };
  }

  it('"Ready to stop" carries the qualifier when srm.warning is true', async () => {
    const { banner, container } = await openSequential(SRM_MISMATCH);
    expect(within(banner).getByTestId('action-label')).toHaveTextContent(
      'Ready to stop — the variant is significantly better'
    );
    expect(within(banner).getByTestId('sequential-srm-qualifier')).toHaveTextContent(QUALIFIER);
    await expectNoAxeViolations(container);
  });

  it.each([
    ['a worse variant', sequential('stop_for_effect', -0.034, -0.012), /significantly worse/],
    ['futility', sequential('stop_for_futility'), /unlikely to reach significance/],
    ['continue', sequential('continue', -0.01, 0.03), /more data needed/],
  ])('%s carries it too: the split qualifies every answer', async (_name, block, label) => {
    const { banner } = await openSequential(SRM_MISMATCH, block);
    expect(within(banner).getByTestId('action-label')).toHaveTextContent(label);
    expect(within(banner).getByTestId('sequential-srm-qualifier')).toHaveTextContent(QUALIFIER);
  });

  it('"Ready to stop" carries none when the check passed (warning false at p = 0.01)', async () => {
    const { banner } = await openSequential(SRM_FINE);
    expect(within(banner).getByTestId('action-label')).toHaveTextContent(/^Ready to stop/);
    expect(screen.queryByTestId('sequential-srm-qualifier')).not.toBeInTheDocument();
    expect(banner).not.toHaveTextContent(/traffic split/i);
  });

  it('"Ready to stop" carries none when the check is null', async () => {
    const { banner } = await openSequential(null);
    expect(within(banner).getByTestId('action-label')).toHaveTextContent(/^Ready to stop/);
    expect(screen.queryByTestId('sequential-srm-qualifier')).not.toBeInTheDocument();
  });
});

describe('the Bayesian panel', () => {
  it('shows the decision and a row per variant at the end of the Overview', async () => {
    install(results({ bayesian_results: BAYESIAN }), { bayesian_enabled: true });
    const { container } = await renderLoaded();
    const panel = screen.getByRole('region', { name: 'Bayesian analysis (primary metric)' });
    expect(within(panel).getByTestId('bayesian-decision')).toHaveTextContent(
      'Decision: Stop: a winner is clear'
    );
    const rows = within(panel).getAllByTestId('bayesian-row');
    expect(rows.map((r) => Array.from(r.children).map((c) => c.textContent))).toEqual([
      ['control', '< 0.1%', '0.037', '11.7%', '10.4% – 13.2%'],
      ['video_hero', '> 99.9%', '< 0.0001', '15.4%', '13.8% – 17.0%'],
    ]);
    expect(within(panel).getByTestId('bayesian-provenance')).toHaveTextContent(
      'Estimated from 100,000 simulated draws per variant, statistics engine 1.3.0.'
    );
    // The seed is larger than a JavaScript number holds exactly; it is not shown.
    expect(panel).not.toHaveTextContent('61678421913184');

    // Last on the Overview tab: after the metric comparison.
    const overview = container.querySelector('#panel-overview') as HTMLElement;
    const sections = Array.from(overview.querySelectorAll(':scope > div > section'));
    expect(sections[sections.length - 1]).toBe(panel);
  });

  it.each(Object.entries(BAYESIAN_DECISION_LABELS))(
    'decision %s has its own fixed label',
    async (decision, label) => {
      install(results({ bayesian_results: { ...BAYESIAN, decision } }), { bayesian_enabled: true });
      await renderLoaded();
      expect(screen.getByTestId('bayesian-decision')).toHaveTextContent(`Decision: ${label}`);
    }
  );

  it('labels exactly the decisions the API snapshot lists', () => {
    const spec = JSON.parse(fs.readFileSync(STABLE_SNAPSHOT, 'utf8'));
    const apiDecisions: string[] = spec.components.schemas.BayesianDecision.enum;
    expect(Object.keys(BAYESIAN_DECISION_LABELS).sort()).toEqual([...apiDecisions].sort());
    expect(new Set(Object.values(BAYESIAN_DECISION_LABELS)).size).toBe(apiDecisions.length);
  });

  it('enabled with no variant rows says there is no data yet', async () => {
    install(results({ bayesian_results: { ...BAYESIAN, decision: null, variant_results: [] } }), {
      bayesian_enabled: true,
    });
    await renderLoaded();
    expect(screen.getByTestId('bayesian-no-data')).toHaveTextContent(
      'The Bayesian analysis has no data yet.'
    );
    expect(screen.getByTestId('bayesian-decision')).toHaveTextContent('Decision: No decision');
    expect(screen.queryByTestId('bayesian-table')).not.toBeInTheDocument();
  });

  it('a non-finite number reads "Not enough data to estimate", never NaN', async () => {
    const broken = {
      ...BAYESIAN,
      variant_results: [
        {
          ...BAYESIAN.variant_results[0],
          probability_to_be_best: Number.NaN,
          expected_loss: null,
          posterior: { ...BAYESIAN.variant_results[0].posterior, mean: null },
        },
      ],
    };
    install(results({ bayesian_results: broken }), { bayesian_enabled: true });
    await renderLoaded();
    const row = screen.getByTestId('bayesian-row');
    expect(row).not.toHaveTextContent('NaN');
    expect(within(row).getByTestId('bayesian-prob-best')).toHaveTextContent('Not enough data to estimate');
    expect(within(row).getByTestId('bayesian-expected-loss')).toHaveTextContent('Not enough data to estimate');
    expect(within(row).getByTestId('bayesian-mean')).toHaveTextContent('Not enough data to estimate');
  });

  it('a null block with bayesian_enabled on gives the muted line', async () => {
    install(results({ bayesian_results: null }), { bayesian_enabled: true });
    await renderLoaded();
    expect(screen.getByTestId('bayesian-unavailable')).toHaveTextContent(
      'Bayesian analysis is on for this experiment, but it could not be computed for these results.'
    );
    expect(screen.queryByTestId('bayesian-panel')).not.toBeInTheDocument();
  });

  it('a null block with bayesian_enabled off gives nothing', async () => {
    install(results({ bayesian_results: null }), { bayesian_enabled: false });
    await renderLoaded();
    expect(screen.queryByTestId('bayesian-unavailable')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bayesian-panel')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/bayesian/i);
  });

  it('a null block when the experiment cannot be read gives nothing', async () => {
    install(results({ bayesian_results: null }), null);
    await renderLoaded();
    expect(screen.queryByTestId('bayesian-unavailable')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bayesian-panel')).not.toBeInTheDocument();
  });

  it('carries no Beta label: both blocks are stable and send no analysis notice', async () => {
    install(results({ srm: SRM_MISMATCH, bayesian_results: BAYESIAN }), { bayesian_enabled: true });
    await renderLoaded();
    expect(screen.queryByRole('region', { name: /beta/i })).not.toBeInTheDocument();
    expect(screen.getByTestId('bayesian-panel')).not.toHaveTextContent(/\bbeta\b/i);
    expect(screen.getByTestId('srm-notice')).not.toHaveTextContent(/\bbeta\b/i);
  });

  it('offers no control in either block: the page only reads', async () => {
    install(results({ srm: SRM_MISMATCH, bayesian_results: BAYESIAN }), { bayesian_enabled: true });
    await renderLoaded();
    for (const id of ['srm-notice', 'bayesian-panel']) {
      const block = screen.getByTestId(id);
      expect(within(block).queryAllByRole('button')).toEqual([]);
      expect(within(block).queryAllByRole('textbox')).toEqual([]);
      expect(within(block).queryAllByRole('checkbox')).toEqual([]);
    }
  });
});

describe('the page asks for nothing new', () => {
  it('makes exactly these requests: both blocks come in GET /results/{id}, and nothing asks /sequential', async () => {
    install(results({ srm: SRM_MISMATCH, bayesian_results: BAYESIAN }), { bayesian_enabled: true });
    await renderLoaded();
    await waitFor(() => expect(screen.getByTestId('bayesian-panel')).toBeInTheDocument());
    const calls = mockedApiFetch.mock.calls.map(([p, o]) => `${(o?.method ?? 'GET').toUpperCase()} ${p}`);
    // No GET /results/{id}/sequential: the experiment does not say sequential
    // testing is on, so that route (which would answer 404) is not asked (#919).
    expect(Array.from(new Set(calls)).sort()).toEqual(
      [
        'GET /api/v1/experiments/exp-1',
        'GET /api/v1/results/exp-1',
        'GET /api/v1/results/exp-1/daily',
        'GET /api/v1/results/exp-1/sample-size',
      ].sort()
    );
  });
});

describe('a failed load shows fixed copy, never the server text', () => {
  const PLANTED = 'Traceback (most recent call last): sqlalchemy.exc.PLANTED-7f3';

  it.each([
    ['a JSON 500', () => new ApiError({ status: 500, detail: PLANTED }), 'The server could not compute the results.'],
    ['a 404', () => new ApiError({ status: 404, detail: PLANTED }), 'No results were found for this experiment.'],
    ['a 403', () => new ApiError({ status: 403, detail: PLANTED }), 'You do not have access to these results.'],
    ['a 422', () => new ApiError({ status: 422, detail: PLANTED }), 'The results could not be loaded.'],
    ['a TypeError', () => new TypeError(`Cannot read properties of undefined ${PLANTED}`), 'The results could not be loaded.'],
  ])('%s', async (_name, makeError, copy) => {
    install(() => {
      throw makeError();
    });
    const { container } = render(<ResultDetailPage />);
    const error = await screen.findByTestId('error-state');
    expect(error).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(document.body.textContent).not.toContain('Traceback');
    expect(document.body.textContent).not.toContain('Cannot read');
    // An error is never an empty page or an empty panel.
    expect(screen.queryByTestId('results-dashboard')).not.toBeInTheDocument();
    expect(screen.queryByTestId('srm-check-passed')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bayesian-unavailable')).not.toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it('an unreachable API keeps the message the dashboard writes for it', async () => {
    install(() => {
      throw new ApiError({ status: 0, detail: "Can't reach the API at http://localhost. Check it." });
    });
    render(<ResultDetailPage />);
    expect(await screen.findByTestId('error-state')).toHaveTextContent("Can't reach the API");
  });
});

describe('accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  it.each([
    ['a mismatch with the Bayesian panel', results({ srm: SRM_MISMATCH, bayesian_results: BAYESIAN }), true],
    ['a passed check', results({ srm: SRM_FINE }), false],
    ['Bayesian with no data', results({ bayesian_results: { ...BAYESIAN, variant_results: [] } }), true],
    ['Bayesian on but not computed', results({ bayesian_results: null }), true],
  ])('%s has no violations', async (_name, body, bayesianEnabled) => {
    install(body, { bayesian_enabled: bayesianEnabled });
    const { container } = await renderLoaded();
    await expectNoAxeViolations(container);
  });
});

describe('these tests run: none is skipped', () => {
  it('this file and the journey that drives the page in a browser contain no skip or todo', () => {
    const files = [
      __filename,
      path.join(__dirname, '..', 'services', 'results-srm-bayesian-types.test.ts'),
      path.join(REPO_ROOT, 'frontend', 'tests', 'e2e', 'results-checks.journey.spec.ts'),
    ];
    const skip = /\b(?:it|test|describe)\s*\.\s*(?:skip|todo|fixme)\s*\(|\bx(?:it|describe|test)\s*\(/;
    for (const file of files) {
      const text = fs.readFileSync(file, 'utf8').replace(/const skip = .*$/m, '');
      expect({ file: path.basename(file), skips: skip.test(text) }).toEqual({
        file: path.basename(file),
        skips: false,
      });
    }
  });
});
