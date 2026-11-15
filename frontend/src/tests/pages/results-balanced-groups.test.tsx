/**
 * Results › Breakdowns for an experiment that balances its split within
 * groups (#448): the balanced groups come first and are chosen, read from
 * `GET /api/v1/results/{id}/balanced-groups`; each row is a stored group, and
 * per variant the rows add up to the headline; one row over all the groups,
 * with its interval and no winner; and the notes that say how users are
 * counted, when most were assigned without the attribute, and how many were
 * split without balancing.
 *
 * An experiment that does not balance keeps the tab exactly as it was.
 *
 * Through `apiFetch`, so the page, ResultsDashboard and the results service
 * run as in the browser. Texts are written out here, not imported, so a
 * reworded sentence fails a test instead of following the code.
 */
import React from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import ResultDetailPage from '@/pages/results/[id]';
import { apiFetch, ApiFetchOptions } from '@/services/api';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

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

const BALANCED_ROUTE = '/api/v1/results/exp-1/balanced-groups';
const RESULTS_ROUTE = '/api/v1/results/exp-1';

const COUNTING_PLATFORM =
  'Each user is counted in the group they were balanced in when they were assigned. ' +
  'Breakdowns › platform counts users by the first value they were seen with, so the two can differ ' +
  'for users assigned from a list or without platform.';
const UNKNOWN_LINK = 'How a user gets a group';
const OUTSIDE_LINK = 'Why users are split without balancing';
const OVERALL = 'All groups, weighted by size';
const DECISION = "The experiment's own results and their decision do not change.";
const FAILED =
  'The breakdown could not be computed. Try again; if it keeps failing, check the API logs.';

const SEG_A = '8d4c6f0e-3a1b-4c2d-9e8f-7a6b5c4d3e2f';
const SEG_B = '1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b';

// --- the answers ------------------------------------------------------------

type Counts = [number, number];

function arm(id: string, name: string, isControl: boolean, [n, conversions]: Counts) {
  return {
    variant_id: id,
    variant_name: name,
    is_control: isControl,
    sample_size: n,
    conversions,
    mean: n > 0 ? conversions / n : 0,
    confidence_interval: null,
    p_value: isControl ? null : 0.5,
    is_significant: false,
  };
}

/** One row: its value, then the control's and the treatment's users and converting users. */
function row(value: string, control: Counts, treatment: Counts) {
  return {
    segment_value: value,
    sample_size: control[0] + treatment[0],
    variants: [arm('ctrl', 'Control', true, control), arm('trt', 'Treatment', false, treatment)],
  };
}

type Row = ReturnType<typeof row>;

function rows(dimension: string, segments: Row[]) {
  return {
    dimension,
    is_exploratory: true,
    adjusted_alpha: 0.05 / Math.max(segments.length, 1),
    has_heterogeneous_effects: false,
    hte_warning: null,
    segments,
  };
}

// The stored groups, then other and unknown: 150 control users (15 converting)
// and 150 treatment users (22 converting), as in the headline below.
const PLATFORM_ROWS = [
  row('ios', [60, 6], [60, 9]),
  row('android', [50, 5], [50, 8]),
  row('other', [20, 2], [20, 3]),
  row('unknown', [20, 2], [20, 2]),
];

const ESTIMATE = {
  variant_id: 'trt',
  variant_name: 'Treatment',
  absolute_difference: 0.0467,
  absolute_difference_ci: [-0.02, 0.1134],
  standard_error: 0.034,
  relative_improvement_pct: 46.7,
  p_value: 0.0123,
  adjusted_p_value: 0.0123,
  // The API's own flag: the page must not turn it into a winner.
  is_significant: true,
};

function balanced(overrides: Record<string, unknown> = {}) {
  return {
    experiment_id: 'exp-1',
    confidence_level: 0.95,
    correction_method: 'benjamini_hochberg',
    groups: rows('platform', PLATFORM_ROWS),
    overall: [ESTIMATE],
    reason: null,
    merged_groups: [],
    users_outside_blocks: 0,
    unknown_share: 40 / 300,
    engine_version: '1.11.0',
    ...overrides,
  };
}

function headlineVariant(id: string, name: string, isControl: boolean, n: number, conversions: number) {
  return {
    variant_id: id,
    variant_name: name,
    is_control: isControl,
    sample_size: n,
    conversions,
    mean: conversions / n,
    std_dev: null,
    confidence_interval: [0.05, 0.15],
    p_value: isControl ? null : 0.2,
    adjusted_p_value: null,
    is_significant: false,
    effect_size: null,
    effect_size_label: null,
    relative_improvement_pct: isControl ? null : 46.7,
    power: null,
    statistical_test_used: isControl ? null : 'fisher_exact',
  };
}

const HEADLINE = {
  experiment_id: 'exp-1',
  experiment_name: 'Checkout button colour',
  status: 'ACTIVE',
  start_date: '2026-09-01T00:00:00Z',
  end_date: null,
  confidence_level: 0.95,
  correction_method: 'benjamini_hochberg',
  sample_size_adequate: false,
  computed_at: '2026-09-20T12:00:00Z',
  summary: {
    total_users: 300,
    total_events: 40,
    duration_days: 21,
    has_winner: false,
    winning_variant_id: null,
    recommendation: 'CONTINUE_TESTING',
    recommendation_reason: 'No statistically significant difference yet.',
  },
  metrics: [
    {
      metric_id: 'm-1',
      metric_name: 'Purchase',
      metric_type: 'conversion',
      is_primary: true,
      variants: [
        headlineVariant('ctrl', 'Control', true, 150, 15),
        headlineVariant('trt', 'Treatment', false, 150, 22),
      ],
    },
  ],
};

const PLATFORM = { attribute: 'platform', values: ['ios', 'android', 'web'] };

interface Setup {
  balanceWithin?: unknown;
  /** Leave `balance_within` out of the experiment, as a server older than the field does. */
  omitBalance?: boolean;
  answer?: () => unknown;
  /** `/results?breakdown=<key>`'s breakdown: by default every user in unknown. */
  breakdown?: (key: string) => unknown;
}

function install({ balanceWithin = PLATFORM, omitBalance = false, answer, breakdown }: Setup = {}) {
  const experiment: Record<string, unknown> = {
    id: 'exp-1',
    correction_method: 'benjamini_hochberg',
    confidence_level: 0.95,
  };
  if (!omitBalance) experiment.balance_within = balanceWithin;
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => experiment },
      {
        path: RESULTS_ROUTE,
        handler: (_p: string, o: ApiFetchOptions) => {
          const key = (o.query as Record<string, unknown> | undefined)?.breakdown;
          if (typeof key !== 'string') return HEADLINE;
          return {
            ...HEADLINE,
            breakdown: breakdown
              ? breakdown(key)
              : rows(key, [row('unknown', [150, 15], [150, 22])]),
          };
        },
      },
      {
        path: '/api/v1/results/exp-1/daily',
        handler: () => ({ experiment_id: 'exp-1', metric_id: null, series: [] }),
      },
      {
        path: '/api/v1/results/exp-1/sample-size',
        handler: () => {
          throw apiError(500, 'not needed here');
        },
      },
      { path: BALANCED_ROUTE, handler: () => (answer ? answer() : balanced()) },
      {
        path: '/api/v1/segments',
        handler: () => [
          { id: SEG_A, name: 'High value', kind: 'id_list', rules: null, status: 'active', created_at: '', updated_at: '' },
          { id: SEG_B, name: 'Churn risk', kind: 'id_list', rules: null, status: 'active', created_at: '', updated_at: '' },
        ],
      },
    ]) as unknown as typeof apiFetch,
  );
}

// --- reading the page -------------------------------------------------------

const calls = (path: string) => mockedApiFetch.mock.calls.filter(([p]) => p === path);
const breakdownCalls = () =>
  calls(RESULTS_ROUTE).filter(([, o]) => typeof (o?.query as Record<string, unknown> | undefined)?.breakdown === 'string');

async function openBreakdowns() {
  const view = render(<ResultDetailPage />);
  const tab = await screen.findByRole('tab', { name: 'Breakdowns' });
  await userEvent.click(tab);
  return view;
}

async function openBalanced(caption = 'Results by balanced group: Platform') {
  const view = await openBreakdowns();
  const table = await screen.findByRole('table', { name: caption });
  return { view, table };
}

const selector = () => screen.getByTestId('breakdown-selector') as HTMLSelectElement;
const optionTexts = () => Array.from(selector().options).map((o) => o.textContent);

/** Each row's group, as the row header names it (without its "(n=…)"). */
function groupNames(table: HTMLElement): string[] {
  return within(table)
    .getAllByRole('rowheader')
    .map((cell) => (cell.firstChild?.textContent ?? '').trim());
}

/** Per variant name, the users of every group row added up. */
function usersByVariant(table: HTMLElement): Record<string, number> {
  const totals: Record<string, number> = {};
  for (const tr of Array.from(table.querySelectorAll('tbody tr'))) {
    const cells = tr.querySelectorAll('td');
    const users = Number((cells[0].textContent ?? '').replace(/,/g, ''));
    const name = (cells[1].firstChild?.textContent ?? '').trim();
    totals[name] = (totals[name] ?? 0) + users;
  }
  return totals;
}

const headlineUsers = () =>
  Object.fromEntries(HEADLINE.metrics[0].variants.map((v) => [v.variant_name, v.sample_size]));

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };
async function violations(node: Element) {
  const result = await axe.run(node, axeOptions);
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join('; ')}`);
}

/** Rows that add up to `users`: an ios group and the rest in unknown. */
function rowsOf(users: number) {
  const quarter = Math.floor(users / 4);
  const rest = users - 2 * quarter;
  return [
    row('ios', [quarter, 1], [quarter, 1]),
    row('unknown', [Math.ceil(rest / 2), 1], [Math.floor(rest / 2), 1]),
  ];
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockRouter.query = { id: 'exp-1' };
});

// --- UC7 ----------------------------------------------------------------------

describe('a balanced experiment opens Breakdowns on its balanced groups', () => {
  it('lists Platform (balanced) first and chooses it, from the balanced-groups route', async () => {
    install();
    await openBalanced();
    expect(optionTexts()).toEqual(['Platform (balanced)', '— None —', 'Platform', 'Country', 'User Tier']);
    expect(selector().selectedOptions[0].textContent).toBe('Platform (balanced)');
    expect(calls(BALANCED_ROUTE)).toHaveLength(1);
    expect(breakdownCalls()).toHaveLength(0);
  });

  it('asks for the balanced groups only once Breakdowns is opened', async () => {
    install();
    render(<ResultDetailPage />);
    await screen.findByRole('tab', { name: 'Breakdowns' });
    expect(calls(BALANCED_ROUTE)).toHaveLength(0);
    await userEvent.click(screen.getByRole('tab', { name: 'Breakdowns' }));
    await screen.findByRole('table', { name: 'Results by balanced group: Platform' });
    expect(calls(BALANCED_ROUTE)).toHaveLength(1);
  });

  it('the rows are the stored groups, then other and unknown, and per variant they add up to the headline', async () => {
    install();
    const { table } = await openBalanced();
    expect(groupNames(table)).toEqual(['ios', 'android', 'other', 'unknown']);
    expect(usersByVariant(table)).toEqual(headlineUsers());
    const overall = screen.getByRole('table', { name: OVERALL });
    const users = within(overall)
      .getAllByRole('row')
      .slice(1)
      .map((tr) => [(tr.querySelector('th')?.firstChild?.textContent ?? '').trim(), tr.querySelectorAll('td')[0].textContent]);
    expect(Object.fromEntries(users)).toEqual({ Control: '150', Treatment: '150' });
  });

  it('a list assigned in one batch shows its tiers, not unknown', async () => {
    // A batch writes no exposure, so a breakdown by tier puts the whole list in
    // unknown; the balanced groups put each user in the tier stored for them.
    install({
      balanceWithin: { attribute: 'tier', values: ['high_value', 'churn_risk', 'new'] },
      answer: () =>
        balanced({
          groups: rows('tier', [
            row('high_value', [70, 7], [70, 10]),
            row('churn_risk', [50, 5], [50, 8]),
            row('new', [30, 3], [30, 4]),
          ]),
          unknown_share: 0,
        }),
    });
    const { table } = await openBalanced('Results by balanced group: tier');
    expect(selector().selectedOptions[0].textContent).toBe('tier (balanced)');
    expect(groupNames(table)).toEqual(['high_value', 'churn_risk', 'new']);
    expect(usersByVariant(table)).toEqual(headlineUsers());
    expect(breakdownCalls()).toHaveLength(0);
    expect(screen.getByTestId('balanced-counting-note').textContent).toBe(
      'Each user is counted in the group they were balanced in when they were assigned. ' +
        'A breakdown by tier counts users by the first value they were seen with, so the two can differ ' +
        'for users assigned from a list or without tier.',
    );
  });

  it('saved segments are named, never shown by their ids', async () => {
    install({
      balanceWithin: { segments: [SEG_A, SEG_B] },
      answer: () =>
        balanced({
          groups: rows('group', [
            row(SEG_A, [70, 7], [70, 10]),
            row(SEG_B, [50, 5], [50, 8]),
            row('other', [30, 3], [30, 4]),
          ]),
          unknown_share: 0,
        }),
    });
    const { table } = await openBalanced('Results by balanced group: saved segments');
    expect(selector().selectedOptions[0].textContent).toBe('Saved segments (balanced)');
    await waitFor(() => expect(groupNames(table)).toEqual(['High value', 'Churn risk', 'other']));
    expect(table.textContent).not.toContain(SEG_A);
    expect(screen.getByTestId('balanced-counting-note').textContent).toBe(
      'Each user is counted in the group they were balanced in when they were assigned.',
    );
  });

  it('another dimension reads /results?breakdown= as before, and the balanced groups can be chosen again', async () => {
    install({ breakdown: (key) => rows(key, PLATFORM_ROWS) });
    await openBalanced();
    await userEvent.selectOptions(selector(), 'Platform');
    await screen.findByTestId('segment-comparison-table');
    expect(breakdownCalls()).toHaveLength(1);
    expect(breakdownCalls()[0][1]?.query).toMatchObject({ breakdown: 'platform' });
    expect(screen.queryByTestId('balanced-groups')).toBeNull();
    await userEvent.selectOptions(selector(), 'Platform (balanced)');
    await screen.findByRole('table', { name: 'Results by balanced group: Platform' });
    expect(screen.queryByTestId('segment-comparison-table')).toBeNull();
    expect(calls(BALANCED_ROUTE)).toHaveLength(2);
  });

  it('a slow answer for an earlier choice never replaces the later one', async () => {
    let release: (value: unknown) => void = () => undefined;
    install({
      answer: () =>
        new Promise((resolve) => {
          release = resolve;
        }),
      breakdown: (key) => rows(key, PLATFORM_ROWS),
    });
    await openBreakdowns();
    await waitFor(() => expect(calls(BALANCED_ROUTE)).toHaveLength(1));
    // The balanced groups are still on their way when Country is chosen.
    await userEvent.selectOptions(selector(), 'Country');
    await screen.findByTestId('segment-comparison-table');
    await act(async () => {
      release(balanced());
    });
    expect(screen.getByTestId('segment-comparison-table')).toBeInTheDocument();
    expect(screen.queryByText('No breakdown data available for the selected dimension.')).toBeNull();
    expect(screen.queryByTestId('balanced-groups')).toBeNull();
    expect(selector().selectedOptions[0].textContent).toBe('Country');
  });

  it('a link to ?tab=breakdowns opens the tab on the balanced groups', async () => {
    install();
    mockRouter.query = { id: 'exp-1', tab: 'breakdowns' };
    render(<ResultDetailPage />);
    await screen.findByRole('table', { name: 'Results by balanced group: Platform' });
    expect(screen.getByRole('tab', { name: 'Breakdowns' })).toHaveAttribute('aria-selected', 'true');
  });

  it('a failed request says so in fixed words, and Try again asks again', async () => {
    let fail = true;
    install({
      answer: () => {
        if (fail) throw apiError(500, 'Traceback: secret');
        return balanced();
      },
    });
    await openBreakdowns();
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe(FAILED);
    expect(document.body.textContent).not.toContain('secret');
    fail = false;
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await screen.findByRole('table', { name: 'Results by balanced group: Platform' });
    expect(calls(BALANCED_ROUTE)).toHaveLength(2);
  });
});

// --- UC8 ----------------------------------------------------------------------

describe('the row over all the groups', () => {
  it('has its interval and no winner or verdict badge', async () => {
    install();
    await openBalanced();
    const overall = screen.getByRole('table', { name: OVERALL });
    const treatment = within(overall).getByRole('row', { name: /^Treatment/ });
    expect(treatment).toHaveTextContent('+4.67 percentage points');
    expect(treatment).toHaveTextContent(/95% confidence interval −2\.00 to \+11\.34\)/);
    expect(treatment).toHaveTextContent('0.0123');
    expect(overall.textContent).not.toMatch(/significant|winner|ship|verdict|better|worse/i);
    expect(screen.getByText(DECISION)).toBeInTheDocument();
  });

  it.each([
    ['mean_metric', `${OVERALL}: no estimate, because the primary metric is not a conversion.`],
    ['sequential_testing', `${OVERALL}: no estimate, because the experiment uses sequential testing.`],
    ['too_few_users', `${OVERALL}: no estimate yet, because too few users have been assigned.`],
  ])('without an estimate (%s) it says why', async (reason, sentence) => {
    install({ answer: () => balanced({ overall: null, reason }) });
    await openBalanced();
    expect(screen.queryByRole('table', { name: OVERALL })).toBeNull();
    expect(screen.getByTestId('balanced-overall-none').textContent).toBe(sentence);
  });

  it('with 2 treatments under a correction shows the adjusted p-value and marks the interval unadjusted; with a standard error of 0, no interval and no p-value', async () => {
    const arms = (users: number) => [
      arm('ctrl', 'Control', true, [users, 1]),
      arm('blue', 'Blue', false, [users, 1]),
      arm('green', 'Green', false, [users, 1]),
      arm('red', 'Red', false, [users, 0]),
    ];
    const groupRows = [
      { segment_value: 'ios', sample_size: 200, variants: arms(50) },
      { segment_value: 'android', sample_size: 120, variants: arms(30) },
    ];
    const estimate = (id: string, name: string, extra: Record<string, unknown>) => ({
      ...ESTIMATE,
      variant_id: id,
      variant_name: name,
      ...extra,
    });
    install({
      answer: () =>
        balanced({
          groups: rows('platform', groupRows),
          overall: [
            estimate('blue', 'Blue', {
              absolute_difference: 0.01,
              absolute_difference_ci: [-0.005, 0.025],
              p_value: 0.01,
              adjusted_p_value: 0.02,
            }),
            estimate('green', 'Green', {
              absolute_difference: 0.02,
              absolute_difference_ci: [0.001, 0.039],
              p_value: 0.04,
              adjusted_p_value: 0.04,
            }),
            // Every user's outcome is the same within each arm of each group.
            estimate('red', 'Red', {
              absolute_difference: 1,
              absolute_difference_ci: null,
              standard_error: 0,
              relative_improvement_pct: null,
              p_value: null,
              adjusted_p_value: null,
              is_significant: false,
            }),
          ],
        }),
    });
    await openBalanced();
    const overall = screen.getByRole('table', { name: OVERALL });
    const blue = within(overall).getByRole('row', { name: /^Blue/ });
    expect(blue).toHaveTextContent('+1.00 percentage points');
    expect(blue).toHaveTextContent(/95% confidence interval, unadjusted, −0\.50 to \+2\.50\)/);
    expect(blue).toHaveTextContent('0.0200, Benjamini-Hochberg adjusted');
    expect(blue).toHaveTextContent('unadjusted 0.0100');
    const red = within(overall).getByRole('row', { name: /^Red/ });
    expect(red).toHaveTextContent('+100.00 percentage points');
    expect(red.textContent).not.toMatch(/CI|confidence interval|0\.00 to/);
    expect(red).toHaveTextContent('Not enough data to estimate');
    expect(within(overall).getByRole('row', { name: /^Control/ })).toHaveTextContent('80');
    expect(overall.textContent).not.toMatch(/significant|winner|ship|verdict|better|worse/i);
  });

  it('names a group counted with a larger one', async () => {
    install({ answer: () => balanced({ merged_groups: [{ group: 'other', into: 'ios' }] }) });
    await openBalanced();
    expect(screen.getByTestId('balanced-merged').textContent).toBe(
      'other has too few users for an estimate of its own, so this estimate counts it with ios.',
    );
  });
});

// --- UC9 ----------------------------------------------------------------------

describe('the notes', () => {
  it('always says how a user is counted', async () => {
    install();
    await openBalanced();
    const note = screen.getByTestId('balanced-counting-note');
    expect(note).toHaveAttribute('role', 'note');
    expect(note.textContent).toBe(COUNTING_PLATFORM);
  });

  it.each([
    [100, 0.51, 'Most users (51% of 100)'],
    [1000, 0.504, 'Most users (over 50% of 1,000)'],
    [100, 0.5, null],
    [99, 0.9, null],
    [100, null, null],
  ])('with %d users and %s of them unknown, the majority note is %s', async (users, share, start) => {
    install({ answer: () => balanced({ groups: rows('platform', rowsOf(users)), unknown_share: share }) });
    await openBalanced();
    const note = screen.queryByTestId('balanced-unknown-note');
    if (start === null) {
      expect(note).toBeNull();
      return;
    }
    expect(note).toHaveAttribute('role', 'note');
    expect(note?.textContent).toBe(
      `${start} were assigned without platform, so they are balanced together as unknown. ` +
        `Send platform in the user's attributes when you assign them. ${UNKNOWN_LINK}`,
    );
    expect(within(note as HTMLElement).getByRole('link', { name: UNKNOWN_LINK }).getAttribute('href')).toMatch(
      /guides\/balance-within-groups.*#assign-users$/,
    );
  });

  it.each([
    [1000, 0.995, 'Most users (over 99% of 1,000)'],
    [1000, 1, 'Most users (100% of 1,000)'],
  ])('a share that rounds to 100%% says so only when it is all: %d users, %s unknown -> %s', async (users, share, start) => {
    install({ answer: () => balanced({ groups: rows('platform', rowsOf(users)), unknown_share: share }) });
    await openBalanced();
    expect(screen.getByTestId('balanced-unknown-note').textContent).toBe(
      `${start} were assigned without platform, so they are balanced together as unknown. ` +
        `Send platform in the user's attributes when you assign them. ${UNKNOWN_LINK}`,
    );
  });

  // Users with no stored group (an older server version's) sit in the unknown
  // row and outside the blocks, and not in unknown_share's base: the note is
  // about the users whose group was stored, S = (N - U) / (1 - share).
  it.each([
    [
      '200 users, 40 with no stored group, 60% of the other 160 in unknown: most of the 160',
      [row('ios', [32, 1], [32, 1]), row('unknown', [68, 1], [68, 1])],
      40,
      0.6,
      'Most users (60% of 160)',
    ],
    [
      'the same 200 users, 10 more kept their group but got no place: still most of the 160',
      [row('ios', [32, 1], [32, 1]), row('unknown', [68, 1], [68, 1])],
      50,
      0.6,
      'Most users (60% of 160)',
    ],
    [
      '1,000 users, 900 with no stored group, 60 of the other 100 in unknown: most of the 100 stored, not of the 1,000',
      [row('ios', [20, 1], [20, 1]), row('unknown', [480, 1], [480, 1])],
      900,
      0.6,
      'Most users (60% of 100)',
    ],
    [
      '1,000 users, 950 with no stored group, 40 of the other 50 in unknown: too few stored users, no note',
      [row('ios', [5, 1], [5, 1]), row('unknown', [495, 1], [495, 1])],
      950,
      0.8,
      null,
    ],
    [
      '300 users, 50 outside the blocks, every stored user in unknown: at least 250 stored, the number left out',
      [row('unknown', [150, 1], [150, 1])],
      50,
      1,
      'Most users (100%)',
    ],
    [
      '300 users, 250 outside the blocks, every stored user in unknown: perhaps only 50 stored, no note',
      [row('unknown', [150, 1], [150, 1])],
      250,
      1,
      null,
    ],
  ])('with users outside the blocks the majority note is about the stored users: %s', async (_name, segments, outside, share, start) => {
    install({
      answer: () =>
        balanced({ groups: rows('platform', segments), users_outside_blocks: outside, unknown_share: share }),
    });
    await openBalanced();
    const note = screen.queryByTestId('balanced-unknown-note');
    if (start === null) {
      expect(note).toBeNull();
      return;
    }
    expect(note?.textContent).toBe(
      `${start} were assigned without platform, so they are balanced together as unknown. ` +
        `Send platform in the user's attributes when you assign them. ${UNKNOWN_LINK}`,
    );
  });

  it.each([
    [0, null],
    [1, '1 of 100 users was split without balancing.'],
    [20, '20 of 100 users were split without balancing.'],
  ])('users outside the blocks: %d, so the count note is %s', async (outside, sentence) => {
    install({
      answer: () => balanced({ groups: rows('platform', rowsOf(100)), users_outside_blocks: outside, unknown_share: 0.2 }),
    });
    await openBalanced();
    const note = screen.queryByTestId('balanced-outside-note');
    if (sentence === null) {
      expect(note).toBeNull();
      return;
    }
    expect(note).toHaveAttribute('role', 'note');
    expect(note?.textContent).toBe(`${sentence} ${OUTSIDE_LINK}`);
    expect(within(note as HTMLElement).getByRole('link', { name: OUTSIDE_LINK }).getAttribute('href')).toMatch(
      /guides\/balance-within-groups.*#users-assigned-without-balancing$/,
    );
  });

  it('users with no stored group count in the split-without-balancing number, not in the unknown share', async () => {
    // 100 users: 20 of them assigned by an older server version (no stored
    // group), so the unknown row has 60 users but the stored share is 40 of 80.
    install({
      answer: () =>
        balanced({
          groups: rows('platform', [row('ios', [20, 1], [20, 1]), row('unknown', [30, 1], [30, 1])]),
          users_outside_blocks: 20,
          unknown_share: 0.5,
        }),
    });
    await openBalanced();
    expect(screen.queryByTestId('balanced-unknown-note')).toBeNull();
    expect(screen.getByTestId('balanced-outside-note').textContent).toBe(
      `20 of 100 users were split without balancing. ${OUTSIDE_LINK}`,
    );
  });
});

// --- UC12 ---------------------------------------------------------------------

describe('axe', () => {
  it('finds no violation in the balanced groups with every note (colour contrast is not computable in jsdom)', async () => {
    install({
      answer: () =>
        balanced({
          groups: rows('platform', rowsOf(200)),
          users_outside_blocks: 3,
          unknown_share: 0.6,
          merged_groups: [{ group: 'ios', into: 'unknown' }],
        }),
    });
    await openBalanced();
    expect(screen.getByTestId('balanced-unknown-note')).toBeInTheDocument();
    expect(screen.getByTestId('balanced-outside-note')).toBeInTheDocument();
    expect(await violations(screen.getByTestId('balanced-groups'))).toEqual([]);
  });

  it('finds no violation without an estimate', async () => {
    install({ answer: () => balanced({ overall: null, reason: 'too_few_users' }) });
    await openBalanced();
    expect(await violations(screen.getByTestId('balanced-groups'))).toEqual([]);
  });
});

// --- unchanged ----------------------------------------------------------------

describe('an experiment that does not balance keeps Breakdowns as it was', () => {
  it.each([
    ['balance_within null', { balanceWithin: null }],
    ['no balance_within (an older server)', { omitBalance: true }],
  ])('%s: the same choices, none chosen, and no balanced-groups request', async (_name, setup) => {
    install(setup as Setup);
    await openBreakdowns();
    expect(optionTexts()).toEqual(['— None —', 'Platform', 'Country', 'User Tier']);
    expect(selector().value).toBe('');
    expect(screen.getByText('Select a dimension above to view segment-level results.')).toBeInTheDocument();
    await userEvent.selectOptions(selector(), 'Platform');
    const table = await screen.findByTestId('segment-comparison-table');
    expect(breakdownCalls()[0][1]?.query).toEqual({
      breakdown: 'platform',
      correction_method: 'benjamini_hochberg',
      confidence_level: 0.95,
    });
    expect(within(table).getByTestId('segment-attribution-caption')).toBeInTheDocument();
    expect(screen.queryByTestId('balanced-groups')).toBeNull();
    expect(screen.queryByTestId('balanced-counting-note')).toBeNull();
    expect(document.body.textContent).not.toContain(OVERALL);
    expect(calls(BALANCED_ROUTE)).toHaveLength(0);
  });
});
