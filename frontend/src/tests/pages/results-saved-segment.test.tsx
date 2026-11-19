/**
 * The results page's "Show results for" picker (#1933): one experiment's
 * results for the members of one saved ID-list segment, from
 * GET /api/v1/results/{id}/segments/{segment_id} (beta).
 *
 * The page keeps the results for all users in their own state. A chosen
 * segment's answer replaces the summary card and the Overview only, labelled
 * exploratory, with no recommendation, winner or leading variant; every other
 * tab, the Bayesian panel and the CSV say they show all users. A failed or
 * missing answer (an older API answers 404) never shows the unfiltered
 * numbers under the segment's label.
 */
import React from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import ResultDetailPage from '@/pages/results/[id]';
import { apiFetch, ApiFetchOptions } from '@/services/api';
import {
  ALL_USERS,
  BAYESIAN_SEGMENT_NOTE,
  CREATE_SEGMENT_LINK,
  CSV_NOTE,
  NO_ID_LIST_SEGMENTS,
  PICKER_HINT,
  PICKER_LABEL,
  SEGMENTS_LOAD_FAILED,
  SEGMENTS_NOT_LOADED,
  segmentResultsUnavailable,
  tabNote,
} from '@/components/results/ResultsDashboard/SavedSegment';
import {
  RESULTS_NOT_FOUND,
} from '@/components/results/ResultsDashboard/ResultsDashboard';
import { SRM_QUALIFIER } from '@/components/results/ResultsDashboard/SrmNotice';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

// The signed-in user (none by default); a case sets one to see a creator's link.
const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({
  ...jest.requireActual('@/contexts/AuthContext'),
  useOptionalAuth: () => mockAuth(),
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

const SEG = '3f1e2d4c-5b6a-4978-8a9b-0c1d2e3f4a5b';
const SEG_B = '9a8b7c6d-5e4f-4321-8abc-def012345678';
const SEGMENT_ROUTE = `/api/v1/results/exp-1/segments/${SEG}`;
const NAME = 'Desktop testers';

// --- the answers ------------------------------------------------------------

function variant(
  id: string,
  name: string,
  isControl: boolean,
  n: number,
  conversions: number,
  extra: Record<string, unknown> = {},
) {
  return {
    variant_id: id,
    variant_name: name,
    is_control: isControl,
    sample_size: n,
    conversions,
    mean: n > 0 ? conversions / n : 0,
    std_dev: null,
    confidence_interval: [0, 1],
    p_value: isControl ? null : 0.2,
    adjusted_p_value: isControl ? null : 0.2,
    is_significant: false,
    effect_size: null,
    effect_size_label: null,
    relative_improvement_pct: isControl ? null : 12.5,
    power: null,
    statistical_test_used: isControl ? null : 'fisher_exact',
    absolute_difference: isControl ? null : 0.1,
    absolute_difference_ci: isControl ? null : [-0.2, 0.4],
    relative_improvement_pct_ci: null,
    incremental_impact: isControl ? null : 0.4,
    incremental_impact_ci: isControl ? null : [-0.8, 1.6],
    ...extra,
  };
}

function metric(variants: ReturnType<typeof variant>[]) {
  return {
    metric_id: 'm1',
    metric_name: 'Purchase',
    metric_type: 'conversion',
    is_primary: true,
    lower_is_better: false,
    analysis: 'proportion',
    variants,
    has_significant_result: variants.some((v) => v.is_significant),
    winning_variant_id: null,
  };
}

const SRM_PASS = {
  chi2: 0.1,
  p_value: 0.75,
  warning: false,
  expected: { ctrl: 15000, trt: 15000 },
  observed: { ctrl: 15010, trt: 14990 },
};

const UNFILTERED = {
  experiment_id: 'exp-1',
  experiment_name: 'Checkout copy',
  status: 'active',
  start_date: '2026-11-01T00:00:00Z',
  end_date: null,
  confidence_level: 0.95,
  correction_method: 'benjamini_hochberg',
  sample_size_adequate: true,
  computed_at: '2026-11-16T12:00:00Z',
  summary: {
    total_users: 30000,
    total_events: 50000,
    total_conversions: 3000,
    duration_days: 15,
    has_winner: true,
    winning_variant_id: 'trt',
    recommendation: 'SHIP_VARIANT',
    recommendation_reason: 'Treatment shows 14.3% improvement on Purchase (p=0.0100).',
  },
  metrics: [
    metric([
      variant('ctrl', 'Control', true, 15000, 1400),
      variant('trt', 'Treatment', false, 15000, 1600, {
        is_significant: true,
        p_value: 0.01,
        adjusted_p_value: 0.01,
        relative_improvement_pct: 14.3,
      }),
    ]),
  ],
  sequential_testing: null,
  breakdown: null,
  bayesian_results: null,
  srm: SRM_PASS,
};

function segmentAnswer(overrides: Record<string, unknown> = {}, segment: Record<string, unknown> = {}) {
  return {
    experiment_id: 'exp-1',
    experiment_name: 'Checkout copy',
    status: 'active',
    start_date: '2026-11-01T00:00:00Z',
    end_date: null,
    confidence_level: 0.95,
    correction_method: 'benjamini_hochberg',
    sample_size_adequate: false,
    computed_at: '2026-11-16T12:00:00Z',
    // The API keeps the engine's words for a segment; the page never shows them.
    summary: {
      total_users: 4,
      total_events: 9,
      total_conversions: 3,
      duration_days: 15,
      has_winner: true,
      winning_variant_id: 'trt',
      recommendation: 'SHIP_VARIANT',
      recommendation_reason: 'Treatment shows 50.0% improvement on Purchase (p=0.0400).',
    },
    metrics: [
      metric([
        variant('ctrl', 'Control', true, 2, 1),
        variant('trt', 'Treatment', false, 2, 2, { is_significant: true }),
      ]),
    ],
    srm: { chi2: 0, p_value: 1, warning: false, expected: { ctrl: 2, trt: 2 }, observed: { ctrl: 2, trt: 2 } },
    segment: {
      id: SEG,
      name: NAME,
      kind: 'id_list',
      status: 'active',
      membership: 'current',
      // Two listed IDs were never assigned: the page's N is summary.total_users
      // (4), never this count, and a test can tell the two apart.
      member_count: 6,
      experiment_users: 30000,
      experiment_srm_warning: false,
      ...segment,
    },
    is_exploratory: true,
    ...overrides,
  };
}

function listed(id: string, name: string, kind: 'id_list' | 'rules', status = 'active') {
  return {
    id,
    name,
    kind,
    rules: kind === 'rules' ? { logical_operator: 'AND', groups: [] } : null,
    status,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  };
}

const LIST = [
  listed('rules-1', 'Desktop by rule', 'rules'),
  listed('arch-1', 'Alpha archived', 'id_list', 'archived'),
  listed(SEG_B, 'Beta pilot', 'id_list'),
  listed('inact-1', 'Zeta inactive', 'id_list', 'inactive'),
  listed(SEG, NAME, 'id_list'),
];

const EXPERIMENT = {
  id: 'exp-1',
  name: 'Checkout copy',
  correction_method: 'benjamini_hochberg',
  confidence_level: 0.95,
  bayesian_enabled: true,
  sequential_testing_enabled: false,
  metrics: [{ id: 'm1', event_name: 'purchase', is_primary: true, metric_type: 'conversion' }],
  balance_within: null,
};

// --- the harness ------------------------------------------------------------

interface Setup {
  list?: () => Promise<unknown>;
  segment?: (path: string, options: ApiFetchOptions) => Promise<unknown>;
  results?: Record<string, unknown>;
  /** The experiment's stored method is CUPED: its results have a CUPED (beta) tab. */
  cuped?: boolean;
}

function setup({ list, segment, results, cuped }: Setup = {}) {
  const api = routedApi([
    {
      path: '/api/v1/experiments/exp-1',
      handler: async () => (cuped ? { ...EXPERIMENT, variance_reduction_config: { method: 'cuped' } } : EXPERIMENT),
    },
    {
      path: '/api/v1/results/exp-1/cuped',
      handler: async () => ({
        experiment_id: 'exp-1',
        method: 'cuped',
        confidence_level: 0.95,
        correction_method: 'benjamini_hochberg',
        covariate_lookback_days: 7,
        metrics: [],
        computed_at: '2026-11-15T12:00:00+00:00',
        seed: null,
        n_samples: null,
        engine_version: '1.3.0',
        analysis_status: 'ga',
        analysis_notice: null,
      }),
    },
    { path: '/api/v1/results/exp-1', handler: async () => results ?? UNFILTERED },
    { path: '/api/v1/results/exp-1/daily', handler: async () => ({ experiment_id: 'exp-1', metric_id: null, series: [] }) },
    { path: '/api/v1/results/exp-1/sample-size', handler: async () => Promise.reject(apiError(500, 'no plan')) },
    { path: '/api/v1/segments', handler: list ?? (async () => LIST) },
    {
      path: /^\/api\/v1\/results\/exp-1\/segments\/[^/]+$/,
      handler: segment ?? (async () => segmentAnswer()),
    },
  ]);
  mockedApiFetch.mockImplementation(api);
  return api;
}

function calls(path: string | RegExp) {
  return mockedApiFetch.mock.calls.filter(([p]) =>
    typeof path === 'string' ? p.split('?')[0] === path : path.test(p.split('?')[0]),
  );
}

async function openPage() {
  render(<ResultDetailPage />);
  await waitFor(() => screen.getByTestId('experiment-summary'));
}

const picker = () => screen.getByRole('combobox', { name: PICKER_LABEL });

/** Hover the picker (which loads the list), then wait for the list. */
async function engage() {
  await userEvent.hover(picker());
  await waitFor(() => expect(within(picker()).getByRole('option', { name: NAME })).toBeInTheDocument());
}

async function choose(id: string = SEG) {
  await engage();
  await userEvent.selectOptions(picker(), id);
}

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };
async function violations(node: Element) {
  const result = await axe.run(node, axeOptions);
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join('; ')}`);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
});

// --- F2, F8: one request, and only it -------------------------------------------

it('F2, F8: choosing a segment makes one call, with the stored settings, and nothing else is fetched', async () => {
  setup();
  await openPage();
  expect(calls('/api/v1/segments')).toHaveLength(0);
  const before = mockedApiFetch.mock.calls.length;
  await engage();
  const listCalls = mockedApiFetch.mock.calls.length - before;
  await userEvent.selectOptions(picker(), SEG);
  await waitFor(() => screen.getByTestId('segment-banner'));

  const segmentCalls = calls(SEGMENT_ROUTE);
  expect(segmentCalls).toHaveLength(1);
  const [, options] = segmentCalls[0];
  expect(options?.query).toEqual({ correction_method: 'benjamini_hochberg', confidence_level: 0.95 });
  expect(Object.keys(options?.query ?? {})).not.toContain('breakdown');
  expect(Object.keys(options?.query ?? {})).not.toContain('use_cache');
  // Nothing else: the list, then the one segment call.
  expect(mockedApiFetch.mock.calls.length - before).toBe(listCalls + 1);
  expect(calls('/api/v1/results/exp-1')).toHaveLength(1);
  expect(calls('/api/v1/results/exp-1/daily')).toHaveLength(1);
  expect(calls('/api/v1/results/exp-1/sample-size')).toHaveLength(1);
  expect(calls(/\/sequential$/)).toHaveLength(0);
  // No other call carries a segment.
  for (const [p, o] of mockedApiFetch.mock.calls) {
    if (p.split('?')[0] === SEGMENT_ROUTE) continue;
    expect(JSON.stringify(o?.query ?? {})).not.toContain(SEG);
    expect(p).not.toContain(SEG);
  }
});

// --- F3: All users -------------------------------------------------------------

it('F3: All users sends no request and shows the results for all users again', async () => {
  setup();
  await openPage();
  const unfilteredCard = screen.getByTestId('experiment-summary').textContent;
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  const count = mockedApiFetch.mock.calls.length;
  await userEvent.selectOptions(picker(), '');
  expect(mockedApiFetch.mock.calls.length).toBe(count);
  expect(screen.queryByTestId('segment-banner')).not.toBeInTheDocument();
  expect(screen.getByTestId('experiment-summary').textContent).toBe(unfilteredCard);
  expect(screen.getByTestId('segment-status')).toHaveTextContent('Showing results for all users.');
});

it('the choice is not saved: the page opened again shows all users', async () => {
  setup();
  const { unmount } = render(<ResultDetailPage />);
  await waitFor(() => screen.getByTestId('experiment-summary'));
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  unmount();
  await openPage();
  expect(picker()).toHaveValue('');
  expect(screen.queryByTestId('segment-banner')).not.toBeInTheDocument();
  expect(screen.getByTestId('recommendation-pill')).toHaveTextContent('Ship Variant');
});

// --- F4: latest wins -------------------------------------------------------------

it('F4: the latest choice wins over a slower earlier answer', async () => {
  let releaseFirst: (v: unknown) => void = () => undefined;
  setup({
    segment: (path) =>
      path.includes(SEG)
        ? new Promise((resolve) => {
            releaseFirst = resolve;
          })
        : Promise.resolve(segmentAnswer({}, { id: SEG_B, name: 'Beta pilot', member_count: 2 })),
  });
  await openPage();
  await choose(SEG);
  await userEvent.selectOptions(picker(), SEG_B);
  await waitFor(() => screen.getByTestId('segment-banner'));
  expect(screen.getByTestId('segment-banner')).toHaveTextContent('Exploratory: Beta pilot only');
  await act(async () => releaseFirst(segmentAnswer()));
  expect(screen.getByTestId('segment-banner')).toHaveTextContent('Exploratory: Beta pilot only');
  expect(screen.queryByText(/Exploratory: Desktop testers only/)).not.toBeInTheDocument();
});

// --- F5: exploratory, no recommendation --------------------------------------------

it('F5: the card is exploratory, with no recommendation, winner or leading variant', async () => {
  setup();
  await openPage();
  expect(screen.getByTestId('recommendation-pill')).toHaveTextContent('Ship Variant');
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  const card = screen.getByTestId('experiment-summary');
  expect(within(card).getByTestId('recommendation-pill')).toHaveTextContent(/^Exploratory$/);
  expect(within(card).getByTestId('summary-segment-line')).toHaveTextContent(
    `Saved segment: ${NAME} · exploratory`,
  );
  expect(within(card).getByTestId('recommendation-reason')).toHaveTextContent(
    'No recommendation for a segment. Decide on the results for all users.',
  );
  expect(card).not.toHaveTextContent(/Ship Variant|Winner|Leading/);
  expect(card).not.toHaveTextContent('Treatment shows 50.0% improvement');
  expect(within(card).getByText('Users in segment')).toBeInTheDocument();
  expect(within(card).getByText('Events in segment')).toBeInTheDocument();
  expect(within(card).getByText('Planned sample (all users)')).toBeInTheDocument();
  expect(within(card).getByTestId('summary-impact')).toHaveTextContent(`Impact on Purchase in ${NAME}:`);
  expect(within(card).getByTestId('analysis-summary-segment')).toHaveTextContent(
    `The correction covers the comparisons inside ${NAME}, not the choice of segment.`,
  );
  const banner = screen.getByTestId('segment-banner');
  expect(within(banner).getByRole('heading', { level: 2 })).toHaveTextContent(`Exploratory: ${NAME} only`);
  expect(banner).toHaveTextContent(
    `These results count only the 4 users in this experiment whose user ID is in the saved segment ${NAME}.`,
  );
  expect(banner).toHaveTextContent('Membership is the list as it is now, not when each user was assigned');
  expect(screen.getByTestId('segment-status')).toHaveTextContent(`Showing results for ${NAME}: 4 users.`);
});

// --- F6: two checks, two sources ------------------------------------------------------

it('F6: the inside check comes from the segment answer, the all-users line from the page', async () => {
  setup({
    segment: async () =>
      segmentAnswer({
        srm: {
          chi2: 16,
          p_value: 0.00006,
          warning: true,
          expected: { ctrl: 2, trt: 2 },
          observed: { ctrl: 3, trt: 1 },
        },
      }),
  });
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  expect(screen.getByTestId('srm-check-passed')).toHaveTextContent(
    'Sample ratio check for all users passed: the traffic split matches the allocation (p = 0.75).',
  );
  const inside = screen.getByTestId('segment-srm-notice');
  expect(inside).toHaveTextContent(`Inside ${NAME}, the traffic split does not match the allocation.`);
  expect(inside).toHaveTextContent('Control: 3 users (expected 2)');
  expect(inside).toHaveTextContent("Do not use this segment's results until you know which.");
  expect(screen.getByTestId('summary-impact-srm')).toHaveTextContent(SRM_QUALIFIER);
});

it("F6: the whole experiment's warning alone qualifies the impact lines", async () => {
  setup({ segment: async () => segmentAnswer({}, { experiment_srm_warning: true }) });
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  expect(screen.getByTestId('segment-srm-check-passed')).toHaveTextContent(
    `Sample ratio check inside ${NAME} passed for its 4 users: their split matches the allocation (p = 1).`,
  );
  expect(screen.getByTestId('summary-impact-srm')).toHaveTextContent(SRM_QUALIFIER);
});

it('F6: no inside check says so', async () => {
  setup({ segment: async () => segmentAnswer({ srm: null }) });
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  expect(screen.getByTestId('segment-srm-check-missing')).toHaveTextContent(
    `No sample ratio check could be run inside ${NAME}, so nothing here says its users split as allocated.`,
  );
});

// --- F7: the tab notes: five, and a sixth on the CUPED tab ----------------------------------

it('F7: each tab but the Overview says it shows all users, only under a segment', async () => {
  setup({ results: { ...UNFILTERED, sequential_testing: SEQUENTIAL } });
  await openPage();
  const tabs = ['Trends', 'Sample Size', 'Sequential', 'Breakdowns', '⚡ Live'];
  for (const tab of tabs) {
    await userEvent.click(screen.getByRole('tab', { name: tab }));
    expect(screen.queryByTestId('segment-tab-note')).not.toBeInTheDocument();
  }
  await userEvent.click(screen.getByRole('tab', { name: 'Overview' }));
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  let notes = 0;
  for (const tab of tabs) {
    await userEvent.click(screen.getByRole('tab', { name: tab }));
    expect(screen.getByTestId('segment-tab-note')).toHaveTextContent(tabNote(NAME));
    notes += screen.getAllByTestId('segment-tab-note').length;
  }
  expect(notes).toBe(5);
  await userEvent.click(screen.getByRole('tab', { name: 'Overview' }));
  expect(screen.queryByTestId('segment-tab-note')).not.toBeInTheDocument();
  // The Sequential tab keeps the experiment's own block.
  await userEvent.click(screen.getByRole('tab', { name: 'Sequential' }));
  expect(calls(/\/sequential$/)).toHaveLength(0);
});

const SEQUENTIAL = {
  method: 'msprt',
  msprt_result: {
    lambda_ratio: 1.2,
    always_valid_p_value: 0.4,
    can_stop: false,
    evidence_strength: 'inconclusive',
    boundary: 20,
  },
  confidence_sequence: { lower: -0.01, upper: 0.03, width: 0.04, sample_size: 2000 },
  evidence_trajectory: [],
  alpha_spending: [],
  long_running_risk: null,
  recommended_action: 'continue',
  at_risk: false,
  analysis_status: 'beta',
  analysis_notice: 'Beta',
};

// --- F9: Bayesian -------------------------------------------------------------------------

it('F9: the Overview under a segment says the Bayesian analysis is not computed for it', async () => {
  setup();
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-overview'));
  expect(screen.getByTestId('segment-bayesian-note')).toHaveTextContent(BAYESIAN_SEGMENT_NOTE);
  expect(screen.queryByText(/Bayesian analysis is not enabled|could not be computed/)).not.toBeInTheDocument();
});

// --- F10, F11: the picker's list ------------------------------------------------------------

it('F10: ID-list segments of every status, active by name first, then the others labelled', async () => {
  setup();
  await openPage();
  await engage();
  const names = within(picker())
    .getAllByRole('option')
    .map((o) => o.textContent);
  expect(names).toEqual([
    ALL_USERS,
    'Beta pilot',
    NAME,
    'Archived segment: Alpha archived',
    'Inactive segment: Zeta inactive',
  ]);
  expect(screen.getByText(PICKER_HINT)).toBeInTheDocument();
  expect(picker()).toHaveAttribute('aria-describedby', 'results-segment-picker-hint');
});

it('F10: a failed list shows its error with Retry, and Retry loads it', async () => {
  let fail = true;
  setup({
    list: async () => {
      if (fail) throw apiError(500, 'down');
      return LIST;
    },
  });
  await openPage();
  await userEvent.hover(picker());
  await waitFor(() => screen.getByText(SEGMENTS_LOAD_FAILED));
  expect(screen.getByRole('alert')).toHaveTextContent(SEGMENTS_LOAD_FAILED);
  expect(within(picker()).getByRole('option', { name: SEGMENTS_NOT_LOADED })).toBeDisabled();
  fail = false;
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
  await waitFor(() => within(picker()).getByRole('option', { name: NAME }));
});

it('F10: no ID-list segment: the empty line, with the create link only for a creator', async () => {
  setup({ list: async () => [listed('rules-1', 'Desktop by rule', 'rules')] });
  await openPage();
  await userEvent.hover(picker());
  await waitFor(() => screen.getByTestId('segment-list-empty'));
  expect(screen.getByTestId('segment-list-empty')).toHaveTextContent(NO_ID_LIST_SEGMENTS);
  // No signed-in user here, so no role that may create a segment.
  expect(screen.queryByText(CREATE_SEGMENT_LINK)).not.toBeInTheDocument();
});

it('F10: no ID-list segment: a creator sees the link to the Segments page, and a viewer does not', async () => {
  mockAuth.mockReturnValue({ user: { role: 'DEVELOPER', is_superuser: false } });
  setup({ list: async () => [listed('rules-1', 'Desktop by rule', 'rules')] });
  const { unmount } = render(<ResultDetailPage />);
  await waitFor(() => screen.getByTestId('experiment-summary'));
  await userEvent.hover(picker());
  await waitFor(() => screen.getByTestId('segment-list-empty'));
  const link = screen.getByRole('link', { name: CREATE_SEGMENT_LINK });
  expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  expect(link).toHaveAttribute('target', '_blank');
  unmount();

  mockedApiFetch.mockReset();
  mockAuth.mockReturnValue({ user: { role: 'VIEWER', is_superuser: false } });
  setup({ list: async () => [listed('rules-1', 'Desktop by rule', 'rules')] });
  render(<ResultDetailPage />);
  await waitFor(() => screen.getByTestId('experiment-summary'));
  await userEvent.hover(picker());
  await waitFor(() => screen.getByTestId('segment-list-empty'));
  expect(screen.queryByText(CREATE_SEGMENT_LINK)).not.toBeInTheDocument();
});

it('F11: the page renders, and is not delayed, when the list fails', async () => {
  setup({ list: async () => Promise.reject(apiError(500, 'down')) });
  await openPage();
  expect(screen.getByTestId('experiment-summary')).toHaveTextContent('Checkout copy');
  await userEvent.hover(picker());
  await waitFor(() => screen.getByText(SEGMENTS_LOAD_FAILED));
  expect(screen.getByTestId('experiment-summary')).toBeInTheDocument();
});

// --- F12, F16, F19: request states ---------------------------------------------------------

const STATES: [string, () => Promise<unknown>, string][] = [
  ['F16 an older API (404 Not Found)', async () => Promise.reject(apiError(404, 'Not Found')), segmentResultsUnavailable(NAME)],
  ['an unknown segment (404)', async () => Promise.reject(apiError(404, 'Segment not found')), segmentResultsUnavailable(NAME)],
  ['a rules segment (422)', async () => Promise.reject(apiError(422, 'This is a rules segment.')), `${NAME} is not an ID-list segment, so it cannot filter these results. Choose another segment, or All users.`],
  ['a refusal (403)', async () => Promise.reject(apiError(403, "You don't have permission")), 'You do not have access to these results.'],
  ['F19 a busy API (503)', async () => Promise.reject(apiError(503, "Another segment's results are being computed.")), `The results for ${NAME} could not be computed. Try again; if it keeps failing, check the API logs.`],
  ['a failure (500)', async () => Promise.reject(apiError(500, 'Traceback (most recent call last)')), `The results for ${NAME} could not be computed. Try again; if it keeps failing, check the API logs.`],
  ['anything else (409)', async () => Promise.reject(apiError(409, 'odd')), `The results for ${NAME} could not be loaded.`],
];

it.each(STATES)('F12: %s shows its fixed words, never the server text or the unfiltered numbers', async (_name, answer, words) => {
  setup({ segment: answer });
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-error'));
  expect(screen.getByRole('alert')).toHaveTextContent(words);
  expect(screen.queryByText(RESULTS_NOT_FOUND)).not.toBeInTheDocument();
  expect(document.body).not.toHaveTextContent(/Traceback|Not Found|being computed|odd/);
  // No numbers under the segment's label: no card, no chart, no table.
  expect(screen.queryByTestId('experiment-summary')).not.toBeInTheDocument();
  expect(screen.queryByText('30,000')).not.toBeInTheDocument();
  expect(screen.queryByText('All Metrics')).not.toBeInTheDocument();
});

it('F12: Try again asks again and Show all users goes back; both move focus to the picker', async () => {
  let answer: () => Promise<unknown> = async () => Promise.reject(apiError(500, 'x'));
  setup({ segment: () => answer() });
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-error'));
  answer = async () => segmentAnswer();
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
  expect(picker()).toHaveFocus();
  await waitFor(() => screen.getByTestId('segment-banner'));
  expect(calls(SEGMENT_ROUTE)).toHaveLength(2);

  answer = async () => Promise.reject(apiError(500, 'x'));
  await userEvent.selectOptions(picker(), SEG_B);
  await waitFor(() => screen.getByTestId('segment-error'));
  await userEvent.click(screen.getByRole('button', { name: 'Show all users' }));
  expect(picker()).toHaveFocus();
  expect(picker()).toHaveValue('');
  expect(screen.getByTestId('recommendation-pill')).toHaveTextContent('Ship Variant');
});

// --- F13: zero states ------------------------------------------------------------------------

it('F13: no member assigned: the zero state from member_count, never a table of zeros', async () => {
  setup({
    segment: async () =>
      segmentAnswer({ summary: { ...segmentAnswer().summary, total_users: 0 }, srm: null }, { member_count: 7 }),
  });
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-zero'));
  expect(screen.getByTestId('segment-zero')).toHaveTextContent(
    `None of the 7 IDs in ${NAME} has been assigned to this experiment, so there is nothing to compare.`,
  );
  expect(screen.queryByText('All Metrics')).not.toBeInTheDocument();
  expect(screen.queryByTestId('experiment-summary')).not.toBeInTheDocument();
});

it('F13: an empty list', async () => {
  setup({
    segment: async () =>
      segmentAnswer({ summary: { ...segmentAnswer().summary, total_users: 0 }, srm: null }, { member_count: 0 }),
  });
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-zero'));
  expect(screen.getByTestId('segment-zero')).toHaveTextContent(
    `${NAME} has no IDs yet, so there is nothing to compare.`,
  );
});

it('F7: the CUPED (beta) tab of a CUPED experiment is the sixth, and says it shows all users', async () => {
  setup({ cuped: true });
  await openPage();
  await userEvent.click(screen.getByRole('tab', { name: 'CUPED (beta)' }));
  await screen.findByTestId('cuped-results');
  expect(screen.queryByTestId('segment-tab-note')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('tab', { name: 'Overview' }));
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  await userEvent.click(screen.getByRole('tab', { name: 'CUPED (beta)' }));
  expect(screen.getByTestId('segment-tab-note')).toHaveTextContent(tabNote(NAME));
  expect(screen.getAllByTestId('segment-tab-note')).toHaveLength(1);
  await screen.findByTestId('cuped-results');
  // The CUPED numbers stay the whole experiment's: the segment route is not asked for them.
  expect(calls('/api/v1/results/exp-1/cuped')).toHaveLength(1);
});

// --- F14: accessibility ------------------------------------------------------------------------

it('F14: the picker is a labelled native select, and axe finds nothing in each state', async () => {
  setup();
  await openPage();
  expect(picker().tagName).toBe('SELECT');
  expect(await violations(screen.getByTestId('results-dashboard'))).toEqual([]);
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  expect(await violations(screen.getByTestId('results-dashboard'))).toEqual([]);
});

const ZERO = segmentAnswer({ summary: { ...segmentAnswer().summary, total_users: 0 }, srm: null });

// One render per state, so each is scanned on its own page.
const AXE_STATES: [string, Setup, string, boolean][] = [
  ['loading', { segment: () => new Promise(() => undefined) }, 'segment-loading', true],
  ['error', { segment: async () => Promise.reject(apiError(500, 'x')) }, 'segment-error', true],
  ['zero', { segment: async () => ZERO }, 'segment-zero', true],
  ['archived', { segment: async () => segmentAnswer({}, { status: 'archived' }) }, 'segment-banner-archived', true],
  ['empty list', { list: async () => [listed('rules-1', 'Desktop by rule', 'rules')] }, 'segment-list-empty', false],
  ['list failure', { list: async () => Promise.reject(apiError(500, 'down')) }, 'segment-list-error', false],
];

it.each(AXE_STATES)('F14: axe finds nothing in the %s state', async (_name, config, testId, chooses) => {
  setup(config);
  await openPage();
  if (chooses) await choose();
  else await userEvent.hover(picker());
  await waitFor(() => screen.getByTestId(testId));
  expect(await violations(screen.getByTestId('results-dashboard'))).toEqual([]);
});

// --- F15: the CSV ------------------------------------------------------------------------------

it('F15: the CSV note appears under a segment and the download is the one for all users', async () => {
  setup();
  await openPage();
  expect(screen.queryByText(CSV_NOTE)).not.toBeInTheDocument();
  const download = screen.getByRole('button', { name: /csv/i });
  const before = download.outerHTML;
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  expect(screen.getByTestId('segment-csv-note')).toHaveTextContent(CSV_NOTE);
  expect(screen.getByRole('button', { name: /csv/i }).outerHTML).toBe(before);
});

// --- F17: an archived list -------------------------------------------------------------------

it('F17: an archived list says so in the banner', async () => {
  setup({ segment: async () => segmentAnswer({}, { status: 'archived' }) });
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  expect(screen.getByTestId('segment-banner-archived')).toHaveTextContent(
    `${NAME} is archived, so its IDs cannot change unless it is made active again.`,
  );
});

it('F17: an active list has no archived line', async () => {
  setup();
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-banner'));
  expect(screen.queryByTestId('segment-banner-archived')).not.toBeInTheDocument();
});

// --- F18: an empty arm -------------------------------------------------------------------------

it('F18: a variant with no member gets no impact line and never shows -100%', async () => {
  setup({
    segment: async () =>
      segmentAnswer({
        metrics: [
          metric([
            variant('ctrl', 'Control', true, 3, 1),
            variant('trt', 'Treatment', false, 0, 0, {
              p_value: 1,
              adjusted_p_value: 1,
              relative_improvement_pct: -100,
              absolute_difference: null,
              absolute_difference_ci: null,
              incremental_impact: 0,
              incremental_impact_ci: null,
            }),
          ]),
        ],
      }),
  });
  await openPage();
  await choose();
  await waitFor(() => screen.getByTestId('segment-overview'));
  expect(screen.queryAllByTestId('summary-impact-line')).toHaveLength(0);
  expect(document.body).not.toHaveTextContent(/-100(\.0)?%|−100(\.0)?%/);
});
