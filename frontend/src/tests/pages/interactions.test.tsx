/**
 * The interactions page (#1960): the scan (stable) and the pair test (beta).
 *
 * The API admits a superuser or the ANALYST, DEVELOPER or ADMIN role
 * (`interactions._require_developer`, twinned in
 * test_dashboard_experiment_roles.py). Anyone else, and a user not yet known,
 * gets no request at all. Users here are NOT superusers unless the cell says
 * so. Every fixture is checked against the committed OpenAPI snapshot, and
 * the server's words (`detail`, `recommendations`) are planted so that a page
 * which showed them would fail.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import InteractionsPage from '@/pages/experiments/interactions';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { INTERACTIONS_COPY as COPY } from '@/utils/interactions';
import { shapeErrors } from '../helpers/openapiShape';
import { Route, apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments/interactions', asPath: '/experiments/interactions' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => mockAuth(),
  useAuth: () => mockAuth(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const C = '33333333-3333-4333-8333-333333333333';
const D = '44444444-4444-4444-8444-444444444444';
const UNLISTED = '99999999-9999-4999-8999-999999999999';

const LIST_PATH = '/api/v1/experiments';
const SCAN_PATH = '/api/v1/interactions/scan';
const PAIR_PATH = /^\/api\/v1\/interactions\/[^/]+\/[^/]+$/;

function experiment(id: string, name: string, status: string) {
  return {
    id,
    name,
    experiment_type: 'a_b',
    status,
    created_at: '2026-10-01T10:00:00Z',
    updated_at: '2026-10-01T10:00:00Z',
    variants: [],
    metrics: [],
  };
}

const ITEMS = [
  experiment(A, 'Checkout CTA', 'active'),
  experiment(B, 'Pricing page', 'active'),
  experiment(C, 'Search ranking', 'draft'),
  experiment(D, 'Onboarding tour', 'paused'),
];
const LIST = { items: ITEMS, total: ITEMS.length, skip: 0, limit: 500 };

/**
 * Two rows, but the server's totals say 5 active experiments, 3 pairs and 2 of
 * them above 0.6: a page that counted rows would print 2 pairs and 1.
 */
const SCAN = {
  total_active_experiments: 5,
  pairs_analyzed: 3,
  high_risk_pairs: 2,
  analyses: [
    {
      experiment_a_id: A,
      experiment_b_id: B,
      overlap_coefficient: 0.6512,
      has_significant_overlap: true,
      interaction_result: null,
      novelty_result: null,
      sutva_result: null,
      overall_risk: 'high',
      recommendations: ['PLANTED-SCAN-RECOMMENDATION'],
    },
    {
      experiment_a_id: A,
      experiment_b_id: D,
      overlap_coefficient: 0.3333,
      has_significant_overlap: true,
      interaction_result: null,
      novelty_result: null,
      sutva_result: null,
      overall_risk: 'medium',
      recommendations: ['PLANTED-SCAN-RECOMMENDATION'],
    },
  ],
};

const NOTICE =
  "Beta: tests whether each treatment's lift differs across the other experiment's variants. " +
  'https://github.com/Controlvar/controlvar/issues/219';

function arm(name: string, effect: number | null) {
  return {
    other_variant_id: `arm-${name}`,
    other_variant_name: name,
    n_control: 300,
    converted_control: 30,
    n_treatment: 310,
    converted_treatment: 40,
    control_rate: effect === null ? null : 0.1,
    treatment_rate: effect === null ? null : 0.1 + effect,
    effect,
    relative_lift: effect === null ? null : effect / 0.1,
  };
}

function tested(
  experimentId: string,
  otherId: string,
  variant: string,
  metric: string,
  arms: ReturnType<typeof arm>[],
  p: number,
  corrected: number | null,
  significant: boolean,
) {
  return {
    experiment_id: experimentId,
    other_experiment_id: otherId,
    metric_id: `metric-${metric}`,
    metric_name: metric,
    variant_id: `variant-${variant}`,
    variant_name: variant,
    control_variant_id: `control-${experimentId}`,
    arms,
    statistic: 6.1,
    degrees_of_freedom: arms.length - 1,
    p_value: p,
    corrected_p_value: corrected,
    correction_method: corrected === null ? 'none' : 'bonferroni',
    confidence_level: 0.95,
    is_significant: significant,
    unavailable_reason: null,
  };
}

function untested(experimentId: string, otherId: string, reason: string, variant: string | null, metric: string | null) {
  return {
    experiment_id: experimentId,
    other_experiment_id: otherId,
    metric_id: metric === null ? null : `metric-${metric}`,
    metric_name: metric,
    variant_id: variant === null ? null : `variant-${variant}`,
    variant_name: variant,
    control_variant_id: null,
    arms: [],
    statistic: null,
    degrees_of_freedom: null,
    p_value: null,
    corrected_p_value: null,
    correction_method: 'none',
    confidence_level: 0.95,
    is_significant: null,
    unavailable_reason: reason,
  };
}

function pairAnswer(overrides: Record<string, unknown> = {}) {
  return {
    experiment_a_id: A,
    experiment_b_id: B,
    shared_users: 1240,
    share_of_a: 0.4132,
    share_of_b: 0.6201,
    overlap_coefficient: 0.3389,
    has_significant_overlap: true,
    mutual_exclusion_group_id: null,
    min_users_per_cell: 100,
    min_expected_per_cell: 5,
    interaction_results: [
      tested(A, B, 'Green button', 'purchase', [arm('Monthly first', 0.0516), arm('Annual first', -0.0213)], 0.0135, 0.027, true),
      tested(B, A, 'Annual first', 'signup', [arm('Control', 0.012), arm('Green button', null)], 0.41, null, false),
      untested(B, A, 'too_few_conversions', 'Quarterly first', 'signup'),
    ],
    has_interaction: true,
    recommendations: ['PLANTED-PAIR-RECOMMENDATION'],
    analysis_status: 'beta',
    analysis_notice: NOTICE,
    ...overrides,
  };
}

const PAIR_NULL = pairAnswer({
  interaction_results: [
    untested(A, B, 'too_few_shared_users', 'Green button', 'purchase'),
    untested(B, A, 'too_few_shared_users', 'Annual first', 'signup'),
  ],
  has_interaction: null,
});

const PAIR_FALSE = pairAnswer({
  interaction_results: [
    tested(A, B, 'Green button', 'purchase', [arm('Monthly first', 0.01), arm('Annual first', 0.012)], 0.62, 0.9, false),
    tested(B, A, 'Annual first', 'signup', [arm('Control', 0.002), arm('Green button', 0.004)], 0.71, null, false),
  ],
  has_interaction: false,
});

/** One row per reason the API gives, plus a code this page does not know. */
const REASONS: [string, string | null, string][] = [
  ['mutual_exclusion_group', null, 'Not tested: the two experiments are in the same mutual exclusion group.'],
  ['no_shared_users', null, 'Not tested: the two experiments share no users.'],
  ['no_metric', null, 'Not tested: this experiment has no metric.'],
  ['not_a_proportion_metric', null, 'Not tested: its primary metric is not a conversion metric.'],
  ['no_control_variant', null, 'Not tested: this experiment has no control variant.'],
  [
    'too_few_shared_users',
    'Green button',
    "Not tested yet: too few shared users; each combination of the two experiments' variants needs at least 100.",
  ],
  [
    'too_few_conversions',
    'Green button',
    'Not tested yet: too few conversions, or too few users without one, in some combination of variants; each needs about 5.',
  ],
  ['a_reason_added_later', 'Green button', 'Not tested.'],
];

const PAIR_REASONS = pairAnswer({
  interaction_results: REASONS.map(([reason, variant]) => untested(A, B, reason, variant, variant ? 'purchase' : null)),
  has_interaction: null,
});

type Reply = (path: string, options: ApiFetchOptions) => unknown;

function serve(handlers: { list?: Reply; scan?: Reply; pair?: Reply } = {}) {
  const routes: Route[] = [
    { path: LIST_PATH, handler: handlers.list ?? (() => LIST) },
    { path: SCAN_PATH, handler: handlers.scan ?? (() => SCAN) },
    { path: PAIR_PATH, handler: handlers.pair ?? (() => pairAnswer()) },
  ];
  mockedApiFetch.mockImplementation(routedApi(routes) as unknown as typeof apiFetch);
}

function signIn(role: string | null, superuser = false) {
  mockAuth.mockReturnValue({
    user: { id: 'u-1', email: 'u@demo.com', username: 'u', role, is_superuser: superuser, is_active: true },
    status: 'authenticated',
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const tick = () => act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
const paths = () => mockedApiFetch.mock.calls.map(([path]) => path);
const scanCalls = () => mockedApiFetch.mock.calls.filter(([path]) => path === SCAN_PATH);
const pairCalls = () => mockedApiFetch.mock.calls.filter(([path]) => PAIR_PATH.test(path));
const listCalls = () => mockedApiFetch.mock.calls.filter(([path]) => path === LIST_PATH);
const cells = (tr: HTMLElement) => Array.from(tr.querySelectorAll('td')).map((td) => td.textContent);

async function openPage() {
  render(<InteractionsPage />);
  return screen.findByTestId('interactions-page');
}

function choose(first: string, second: string) {
  fireEvent.change(screen.getByLabelText(COPY.firstLabel), { target: { value: first } });
  fireEvent.change(screen.getByLabelText(COPY.secondLabel), { target: { value: second } });
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn('ANALYST');
});

describe('the fixtures are what the API sends', () => {
  it.each([
    ['ExperimentListResponse', LIST],
    ['ActiveInteractionScanResponse', SCAN],
    ['InteractionPairResponse', pairAnswer()],
    ['InteractionPairResponse', PAIR_NULL],
    ['InteractionPairResponse', PAIR_FALSE],
    ['InteractionPairResponse', PAIR_REASONS],
  ])('%s', (schema, value) => {
    expect(shapeErrors(schema, value)).toEqual([]);
  });

  it('the scan fixture discriminates: its totals are not its row counts', () => {
    expect(SCAN.pairs_analyzed).not.toBe(SCAN.analyses.length);
    expect(SCAN.high_risk_pairs).not.toBe(SCAN.analyses.filter((a) => a.overall_risk === 'high').length);
  });
});

describe('who may use it, and exactly the requests each sends', () => {
  it.each(['ANALYST', 'DEVELOPER', 'ADMIN'])('%s (not a superuser) reads the list once, and nothing else until asked', async (role) => {
    signIn(role);
    serve();
    // The capability table's witness, written out: test_dashboard_and_api.py reads it here.
    const page = await openPage();
    expect(page).toBeInTheDocument();
    expect(listCalls()).toHaveLength(1);
    const [, options] = listCalls()[0];
    expect(options?.query?.limit).toBe(500);
    expect(Object.entries(options?.query ?? {}).filter(([, value]) => value !== undefined)).toEqual([['limit', 500]]);
    expect(paths()).toEqual([LIST_PATH]);
    expect(screen.queryByTestId('interactions-refused')).toBeNull();
    expect(screen.getByTestId('interactions-scan-button')).toHaveTextContent(/^Scan active experiments$/);
  });

  it('a superuser whose role is VIEWER uses it: the API admits a superuser before it reads the role', async () => {
    signIn('VIEWER', true);
    serve();
    await openPage();
    expect(paths()).toEqual([LIST_PATH]);
  });

  it.each([
    ['VIEWER', 'VIEWER', 'Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role; you are VIEWER.'],
    [
      'an account with no role',
      null,
      'Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role; you are signed in without a role.',
    ],
  ])('%s is told who can, and no request is sent', async (_, role, note) => {
    signIn(role);
    serve();
    render(<InteractionsPage />);
    expect(screen.getByTestId('interactions-refused')).toHaveTextContent(new RegExp(`^${note.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`));
    await tick();
    expect(mockedApiFetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId('interactions-page')).toBeNull();
    expect(screen.queryByTestId('interactions-scan-button')).toBeNull();
    expect(screen.queryByTestId('interactions-loading')).toBeNull();
  });

  it('a user not yet known: "Loading…", no role note, and nothing is sent', async () => {
    mockAuth.mockReturnValue({ user: null, status: 'loading' });
    serve();
    render(<InteractionsPage />);
    expect(screen.getByTestId('interactions-loading')).toHaveTextContent(/^Loading…$/);
    await tick();
    expect(mockedApiFetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId('interactions-refused')).toBeNull();
    expect(screen.queryByTestId('interactions-page')).toBeNull();
  });

  it('heads the page with its title, the explanation and the guide', async () => {
    serve();
    await openPage();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/^Experiment interactions$/);
    const intro = screen.getByTestId('interactions-intro');
    expect(intro).toHaveTextContent(`${COPY.intro} How interactions are tested`);
    expect(within(intro).getByRole('link', { name: 'How interactions are tested' })).toHaveAttribute(
      'href',
      docsUrl('api/interaction-detection'),
    );
  });
});

describe('the states the witness never marks', () => {
  it('while the list loads', async () => {
    serve({ list: () => new Promise<never>(() => undefined) });
    render(<InteractionsPage />);
    expect(await screen.findByTestId('interactions-loading')).toHaveTextContent(/^Loading…$/);
    expect(screen.queryByTestId('interactions-page')).toBeNull();
  });

  it('a list that fails reads as fixed copy, and Try again reads it again', async () => {
    let fail = true;
    serve({
      list: () => {
        if (fail) throw apiError(500, 'Traceback (most recent call last): PLANTED-list');
        return LIST;
      },
    });
    render(<InteractionsPage />);
    const error = await screen.findByTestId('interactions-list-error');
    expect(error).toHaveTextContent(/^The experiments could not be loaded\. Try again\.Try again$/);
    expect(screen.queryByTestId('interactions-page')).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
    fail = false;
    fireEvent.click(within(error).getByRole('button', { name: 'Try again' }));
    await screen.findByTestId('interactions-page');
    expect(listCalls()).toHaveLength(2);
  });
});

describe('the scan (stable): overlap only, on request', () => {
  it('sends GET /interactions/scan with no query and shows the server’s totals and pairs', async () => {
    serve();
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    const summary = await screen.findByTestId('interactions-scan-summary');
    expect(scanCalls()).toEqual([[SCAN_PATH]]);
    expect(summary).toHaveTextContent(
      /^5 active experiments\. 3 pairs share many users \(an overlap above 0\.3\); 2 of them above 0\.6\.$/,
    );
    const table = screen.getByTestId('interactions-scan-table');
    expect(table.querySelector('caption')).toHaveTextContent(/^Pairs of active experiments that share many users$/);
    const headers = Array.from(table.querySelectorAll('thead th')).map((th) => th.textContent);
    expect(headers).toEqual(['First experiment', 'Second experiment', 'Overlap', 'Overlap level', 'Pair test']);
    // Exactly these cells: no verdict, no "Yes" or "No", for a pair the scan did not test.
    expect(screen.getAllByTestId('interactions-scan-row').map(cells)).toEqual([
      ['Checkout CTA', 'Pricing page', '0.65', 'High', 'Test this pair'],
      ['Checkout CTA', 'Onboarding tour', '0.33', 'Medium', 'Test this pair'],
    ]);
    expect(screen.getByTestId('interactions-scan-under')).toHaveTextContent(COPY.scanUnder);
    expect(screen.getByTestId('interactions-scan-button')).toHaveTextContent(/^Scan again$/);
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(pairCalls()).toEqual([]);
  });

  it('says "Scanning…" while it runs, and the button waits', async () => {
    const answer = deferred<typeof SCAN>();
    serve({ scan: () => answer.promise });
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    expect(await screen.findByTestId('interactions-scan-running')).toHaveTextContent(/^Scanning…$/);
    expect(screen.getByTestId('interactions-scan-button')).toBeDisabled();
    await act(async () => answer.resolve(SCAN));
    expect(screen.queryByTestId('interactions-scan-running')).toBeNull();
  });

  it('one pair: the singular, and whether it is above 0.6', async () => {
    serve({ scan: () => ({ ...SCAN, total_active_experiments: 2, pairs_analyzed: 1, high_risk_pairs: 1, analyses: [SCAN.analyses[0]] }) });
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    expect(await screen.findByTestId('interactions-scan-summary')).toHaveTextContent(
      /^2 active experiments\. 1 pair shares many users \(an overlap above 0\.3\); it is above 0\.6\.$/,
    );
  });

  it('no pair over the threshold: the empty line, and no table', async () => {
    serve({ scan: () => ({ total_active_experiments: 3, pairs_analyzed: 0, high_risk_pairs: 0, analyses: [] }) });
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    expect(await screen.findByTestId('interactions-scan-empty')).toHaveTextContent(COPY.scanEmpty);
    expect(screen.queryByTestId('interactions-scan-table')).toBeNull();
    expect(screen.queryByTestId('interactions-scan-summary')).toBeNull();
  });

  it.each([0, 1])('%i active experiments: there is no pair to scan', async (active) => {
    serve({ scan: () => ({ total_active_experiments: active, pairs_analyzed: 0, high_risk_pairs: 0, analyses: [] }) });
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    expect(await screen.findByTestId('interactions-scan-too-few')).toHaveTextContent(
      /^Fewer than two experiments are active, so there is no pair to scan\.$/,
    );
    expect(screen.queryByTestId('interactions-scan-empty')).toBeNull();
  });

  it.each([
    [500, COPY.scanFailedServer],
    [503, COPY.scanFailedServer],
    [418, COPY.scanFailed],
  ])('a scan answered %i reads as fixed copy', async (status, copy) => {
    serve({
      scan: () => {
        throw apiError(status, 'Traceback (most recent call last): PLANTED-scan');
      },
    });
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    expect(await screen.findByTestId('interactions-scan-error')).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(screen.getByTestId('interactions-scan-button')).toHaveTextContent(/^Scan again$/);
  });

  it('a scan that cannot reach the API says so in the dashboard’s own sentence', async () => {
    const unreachable = 'Could not reach the server. Check your connection and try again.';
    serve({
      scan: () => {
        throw new ApiError({ status: 0, detail: unreachable, message: unreachable });
      },
    });
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    expect(await screen.findByTestId('interactions-scan-error')).toHaveTextContent(unreachable);
  });

  it('a 403 that arrives anyway reads as the role note, never the server’s text', async () => {
    serve({
      scan: () => {
        throw apiError(403, 'PLANTED-403');
      },
    });
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    expect(await screen.findByTestId('interactions-scan-refused')).toHaveTextContent(
      /^Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role; you are ANALYST\.$/,
    );
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('a pair past the list’s 500 shows its id in monospace, never a blank cell', async () => {
    serve({
      scan: () => ({
        ...SCAN,
        analyses: [{ ...SCAN.analyses[0], experiment_b_id: UNLISTED }],
      }),
    });
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    const [row] = await screen.findAllByTestId('interactions-scan-row');
    const id = within(row).getByText(UNLISTED);
    expect(id.tagName).toBe('CODE');
    expect(Array.from(id.classList)).toContain('font-mono');
    expect(within(row).getByRole('button')).toHaveAccessibleName(`Test Checkout CTA and ${UNLISTED}`);
  });
});

describe('the list is one request of 500', () => {
  const MANY = Array.from({ length: 500 }, (_, i) =>
    experiment(`aaaaaaaa-0000-4000-8000-${String(i).padStart(12, '0')}`, `Experiment ${i}`, 'completed'),
  );
  MANY[0] = experiment(A, 'Checkout CTA', 'active');

  it('620 experiments: it says the lists show 500, and a pair past them still has a name or its id', async () => {
    serve({
      list: () => ({ items: MANY, total: 620, skip: 0, limit: 500 }),
      scan: () => ({ ...SCAN, analyses: [{ ...SCAN.analyses[0], experiment_b_id: UNLISTED }] }),
      pair: (path) => pairAnswer({ experiment_b_id: path.split('/').pop() }),
    });
    await openPage();
    expect(shapeErrors('ExperimentListResponse', { items: MANY, total: 620, skip: 0, limit: 500 })).toEqual([]);
    expect(listCalls()).toHaveLength(1);
    expect(screen.getByTestId('interactions-partial-list')).toHaveTextContent(
      /^The lists show 500 of the 620 experiments\. To test a pair that is not listed, use the API\.$/,
    );
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    const [row] = await screen.findAllByTestId('interactions-scan-row');
    fireEvent.click(within(row).getByRole('button', { name: `Test Checkout CTA and ${UNLISTED}` }));
    const heading = await screen.findByTestId('interaction-pair-result-heading');
    expect(heading).toHaveTextContent(`Checkout CTA and ${UNLISTED}`);
    expect(within(heading).getByText(UNLISTED).tagName).toBe('CODE');
    // The second choice holds the unlisted id, shown by its id.
    const second = screen.getByLabelText(COPY.secondLabel) as HTMLSelectElement;
    expect(second.value).toBe(UNLISTED);
    expect(second.selectedOptions[0].textContent).toBe(UNLISTED);
    expect(listCalls()).toHaveLength(1);
  });

  it('every experiment fits: no such line', async () => {
    serve();
    await openPage();
    expect(screen.queryByTestId('interactions-partial-list')).toBeNull();
  });
});

describe('the pair test (beta)', () => {
  it('is headed "Pair test" with a Beta chip; the scan is not marked beta', async () => {
    serve();
    await openPage();
    expect(screen.getByTestId('interactions-pair-heading')).toHaveTextContent(/^Pair testBeta$/);
    expect(screen.getByRole('heading', { name: COPY.scanHeading })).toHaveTextContent(
      /^Active experiments that share users$/,
    );
  });

  it('offers every listed experiment as "{name} ({status})", after the placeholder', async () => {
    serve();
    await openPage();
    for (const label of [COPY.firstLabel, COPY.secondLabel]) {
      const select = screen.getByLabelText(label) as HTMLSelectElement;
      expect(Array.from(select.options).map((o) => o.textContent)).toEqual([
        'Choose an experiment',
        'Checkout CTA (Active)',
        'Pricing page (Active)',
        'Search ranking (Draft)',
        'Onboarding tour (Paused)',
      ]);
    }
    expect(screen.getByTestId('interaction-pair-button')).toBeDisabled();
  });

  it('sends one GET for the two chosen, with no query, and shows the answer the server gave', async () => {
    serve();
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    const heading = await screen.findByTestId('interaction-pair-result-heading');
    expect(pairCalls()).toEqual([[`/api/v1/interactions/${A}/${B}`]]);
    expect(heading).toHaveTextContent(/^Checkout CTA and Pricing page$/);
    expect(heading.tagName).toBe('H3');
    expect(heading).toHaveFocus();
    expect(screen.getByTestId('analysis-notice-text')).toHaveTextContent(
      "tests whether each treatment's lift differs across the other experiment's variants. issue #219",
    );
    expect(screen.getByTestId('interaction-pair-overlap')).toHaveTextContent(
      /^They share 1240 users: 41% of Checkout CTA and 62% of Pricing page \(overlap 0\.34\)\.$/,
    );
    expect(screen.getByTestId('interaction-pair-verdict')).toHaveTextContent(COPY.verdictFound);
    const table = screen.getByTestId('interaction-pair-table');
    expect(table.querySelector('caption')).toHaveTextContent(/^Each treatment's lift across the other experiment's variants$/);
    expect(Array.from(table.querySelectorAll('thead th')).map((th) => th.textContent)).toEqual([
      'Experiment',
      'Treatment',
      'Metric',
      'Lift in each variant of the other experiment',
      'Result',
    ]);
    expect(screen.getAllByTestId('interaction-pair-row').map(cells)).toEqual([
      [
        'Checkout CTA',
        'Green button',
        'purchase',
        'Monthly first: +5.16 percentage points; Annual first: −2.13 percentage points',
        'Significant (corrected p=0.027)',
      ],
      ['Pricing page', 'Annual first', 'signup', 'Control: +1.20 percentage points; Green button: not measured', 'Not Significant (p=0.410)'],
      [
        'Pricing page',
        'Quarterly first',
        'signup',
        '—',
        'Not tested yet: too few conversions, or too few users without one, in some combination of variants; each needs about 5.',
      ],
    ]);
    expect(screen.getByTestId('interaction-pair-significant-note')).toHaveTextContent(COPY.significantNote);
    expect(screen.getByTestId('interaction-pair-footnote')).toHaveTextContent(COPY.footnote);
    // The server's recommendations are not shown; the page's own copy says what to do.
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('no beta notice when the answer is not beta', async () => {
    serve({ pair: () => pairAnswer({ analysis_status: 'ga', analysis_notice: null }) });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    await screen.findByTestId('interaction-pair-result-heading');
    expect(screen.queryByTestId('analysis-notice')).toBeNull();
  });

  it('a null verdict is "No answer", never "no interaction"', async () => {
    serve({ pair: () => PAIR_NULL });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    expect(await screen.findByTestId('interaction-pair-verdict')).toHaveTextContent(COPY.verdictUnknown);
    expect(document.body.textContent).not.toContain('No interaction was found');
    expect(screen.queryByTestId('interaction-pair-significant-note')).toBeNull();
  });

  it('a false verdict says so, and that it is weak evidence with few users', async () => {
    serve({ pair: () => PAIR_FALSE });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    expect(await screen.findByTestId('interaction-pair-verdict')).toHaveTextContent(COPY.verdictNone);
    expect(screen.queryByTestId('interaction-pair-significant-note')).toBeNull();
  });

  it('no shared users: the overlap says so', async () => {
    serve({ pair: () => pairAnswer({ shared_users: 0, share_of_a: 0, share_of_b: 0, overlap_coefficient: 0, has_significant_overlap: false, interaction_results: [untested(A, B, 'no_shared_users', null, null)], has_interaction: null }) });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    expect(await screen.findByTestId('interaction-pair-overlap')).toHaveTextContent(/^They share no users\.$/);
  });

  it('lists every row, each untested one with its reason', async () => {
    serve({ pair: () => PAIR_REASONS });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    await screen.findByTestId('interaction-pair-table');
    const rows = screen.getAllByTestId('interaction-pair-row');
    expect(rows).toHaveLength(PAIR_REASONS.interaction_results.length);
    expect(rows.map((tr) => within(tr).getByTestId('interaction-pair-outcome').textContent)).toEqual(
      REASONS.map(([, , copy]) => copy),
    );
    // A reason for the whole experiment has no treatment: "All treatments".
    expect(rows.map((tr) => within(tr).getByTestId('interaction-pair-treatment').textContent)).toEqual(
      REASONS.map(([, variant]) => variant ?? 'All treatments'),
    );
  });

  it('the same experiment twice: told, and nothing is sent', async () => {
    serve();
    await openPage();
    choose(A, A);
    const button = screen.getByTestId('interaction-pair-button');
    expect(button).toBeDisabled();
    expect(screen.getByTestId('interaction-same-twice')).toHaveTextContent(/^Choose two different experiments\.$/);
    const second = screen.getByLabelText(COPY.secondLabel);
    expect(second).toHaveAttribute('aria-invalid', 'true');
    expect(second).toHaveAttribute('aria-describedby', 'interaction-same-twice');
    fireEvent.click(button);
    await tick();
    expect(pairCalls()).toEqual([]);
  });

  it('says "Testing the pair…" while it runs', async () => {
    const answer = deferred<ReturnType<typeof pairAnswer>>();
    serve({ pair: () => answer.promise });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    const running = await screen.findByTestId('interaction-pair-running');
    expect(running).toHaveTextContent(/^Testing the pair…$/);
    expect(running).toHaveAttribute('role', 'status');
    await act(async () => answer.resolve(pairAnswer()));
    expect(screen.queryByTestId('interaction-pair-running')).toBeNull();
  });

  it('changing a choice clears the answer', async () => {
    serve();
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    await screen.findByTestId('interaction-pair-result');
    fireEvent.change(screen.getByLabelText(COPY.secondLabel), { target: { value: D } });
    expect(screen.queryByTestId('interaction-pair-result')).toBeNull();
  });

  it('an answer for an earlier choice is never shown', async () => {
    const answers: Array<ReturnType<typeof deferred<ReturnType<typeof pairAnswer>>>> = [];
    serve({
      pair: () => {
        const answer = deferred<ReturnType<typeof pairAnswer>>();
        answers.push(answer);
        return answer.promise;
      },
    });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    await waitFor(() => expect(answers).toHaveLength(1));
    // Changed while the first is on its way; then the first answers.
    fireEvent.change(screen.getByLabelText(COPY.secondLabel), { target: { value: D } });
    await act(async () => answers[0].resolve(pairAnswer()));
    expect(screen.queryByTestId('interaction-pair-result')).toBeNull();
    expect(screen.queryByTestId('interaction-pair-running')).toBeNull();
    // The new pair's own answer is shown.
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    await waitFor(() => expect(answers).toHaveLength(2));
    await act(async () => answers[1].resolve(pairAnswer({ experiment_b_id: D })));
    expect(await screen.findByTestId('interaction-pair-result-heading')).toHaveTextContent(
      /^Checkout CTA and Onboarding tour$/,
    );
    expect(pairCalls().map(([path]) => path)).toEqual([`/api/v1/interactions/${A}/${B}`, `/api/v1/interactions/${A}/${D}`]);
  });

  it("a scan row's Test this pair fills both choices, runs, and moves focus to the answer", async () => {
    serve();
    await openPage();
    fireEvent.click(screen.getByTestId('interactions-scan-button'));
    await screen.findByTestId('interactions-scan-table');
    fireEvent.click(screen.getByRole('button', { name: 'Test Checkout CTA and Onboarding tour' }));
    const heading = await screen.findByTestId('interaction-pair-result-heading');
    expect((screen.getByLabelText(COPY.firstLabel) as HTMLSelectElement).value).toBe(A);
    expect((screen.getByLabelText(COPY.secondLabel) as HTMLSelectElement).value).toBe(D);
    expect(pairCalls()).toEqual([[`/api/v1/interactions/${A}/${D}`]]);
    expect(heading).toHaveFocus();
  });

  it.each([
    [403, 'Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role; you are ANALYST.', 'interaction-pair-refused'],
    [404, COPY.pairGone, 'interaction-pair-error'],
    [422, COPY.sameTwice, 'interaction-pair-error'],
    [500, COPY.pairFailedServer, 'interaction-pair-error'],
    [502, COPY.pairFailedServer, 'interaction-pair-error'],
    [409, COPY.pairFailed, 'interaction-pair-error'],
  ])('a pair test answered %i reads as fixed copy', async (status, copy, testId) => {
    serve({
      pair: () => {
        throw apiError(status, 'Traceback (most recent call last): PLANTED-pair');
      },
    });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    expect(await screen.findByTestId(testId)).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(screen.queryByTestId('interaction-pair-result')).toBeNull();
  });

  it('a pair test that cannot reach the API says so in the dashboard’s own sentence', async () => {
    const unreachable = 'Could not reach the server. Check your connection and try again.';
    serve({
      pair: () => {
        throw new ApiError({ status: 0, detail: unreachable, message: unreachable });
      },
    });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    expect(await screen.findByTestId('interaction-pair-error')).toHaveTextContent(unreachable);
  });

  it('a 404 reads the list again, keeping the page and the message', async () => {
    let gone = false;
    serve({
      list: () => (gone ? { ...LIST, items: ITEMS.filter((item) => item.id !== B), total: 3 } : LIST),
      pair: () => {
        gone = true;
        throw apiError(404, 'PLANTED-404');
      },
    });
    await openPage();
    choose(A, B);
    fireEvent.click(screen.getByTestId('interaction-pair-button'));
    await screen.findByTestId('interaction-pair-error');
    await waitFor(() => expect(listCalls()).toHaveLength(2));
    expect(screen.getByTestId('interactions-page')).toBeInTheDocument();
    expect(screen.getByTestId('interaction-pair-error')).toHaveTextContent(COPY.pairGone);
    // The experiment that went is no longer offered by name.
    const second = screen.getByLabelText(COPY.secondLabel) as HTMLSelectElement;
    expect(Array.from(second.options).map((o) => o.textContent)).not.toContain('Pricing page (Active)');
  });
});
