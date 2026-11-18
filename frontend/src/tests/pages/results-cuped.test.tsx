/**
 * /results/[id] › CUPED (beta) (#1959): `GET /results/{id}/cuped` on the
 * results page of an experiment whose stored method is cuped or cuped_plus.
 *
 * Through `apiFetch`, so the page, ResultsDashboard, the results service and
 * the CUPED panel run as in the browser. Texts are written out here, not
 * imported, so a reworded sentence fails a test instead of following the code.
 *
 * What is pinned:
 *  - the tab exists only for a stored method of cuped or cuped_plus, and
 *    nothing is asked for until it is first opened (and then once);
 *  - the numbers are the API's: the adjusted effect and its bounds at the
 *    response's own confidence level, the corrected p-value, and the server's
 *    is_significant (never a comparison done here);
 *  - every comparison is listed, a reason row shows words and no numbers, and
 *    every reason the API documents has its fixed copy;
 *  - a failed load shows fixed copy and leaves the rest of the page alone.
 */
import React from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import ResultDetailPage from '@/pages/results/[id]';
import { CUPED_REASON_COPY } from '@/components/results/Cuped/CupedResultsPanel';
import { apiFetch } from '@/services/api';
import { documentedCupedReasonCodes } from '../helpers/cupedReasonCodes';
import { shapeErrors } from '../helpers/openapiShape';
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

const CUPED_ROUTE = '/api/v1/results/exp-1/cuped';
const PLANTED = 'Traceback (most recent call last): sqlalchemy.exc.PLANTED-1959';

// --- the answers ------------------------------------------------------------

const EXPERIMENT_VARIANTS = [
  { id: 'ctrl', name: 'Control' },
  { id: 't1', name: 'green_button' },
  { id: 't2', name: 'red_button' },
];

/** A computed comparison; the fields that differ from the unadjusted ones are made to differ. */
function computed(variantId: string, name: string, overrides: Record<string, unknown> = {}) {
  return {
    metric_id: 'm-purchase',
    metric_name: 'Purchase',
    variant_id: variantId,
    variant_name: name,
    control_variant_id: 'ctrl',
    control_sample_size: 15000,
    treatment_sample_size: 14000,
    adjusted_control_mean: 0.0799,
    adjusted_treatment_mean: 0.0885,
    adjusted_effect: 0.0086,
    adjusted_se: 0.003,
    adjusted_p_value: 0.0042,
    adjusted_ci_lower: 0.0037,
    adjusted_ci_upper: 0.0135,
    unadjusted_effect: 0.0091,
    unadjusted_se: 0.0032,
    corrected_p_value: 0.0084,
    is_significant: true,
    variance_reduction_pct: 12.69,
    theta: 0.331,
    covariate_event_name: 'purchase',
    covariate_coverage_pct: 9.89,
    unavailable_reason: null,
    method: 'cuped',
    ...overrides,
  };
}

/** A comparison that could not be computed: its reason, and no numbers. */
function unavailable(metricId: string, name: string, variantId: string, vName: string, reason: string) {
  return {
    metric_id: metricId,
    metric_name: name,
    variant_id: variantId,
    variant_name: vName,
    control_variant_id: 'ctrl',
    control_sample_size: 15000,
    treatment_sample_size: 14000,
    adjusted_control_mean: null,
    adjusted_treatment_mean: null,
    adjusted_effect: null,
    adjusted_se: null,
    adjusted_p_value: null,
    adjusted_ci_lower: null,
    adjusted_ci_upper: null,
    unadjusted_effect: null,
    unadjusted_se: null,
    corrected_p_value: null,
    is_significant: false,
    variance_reduction_pct: null,
    theta: null,
    covariate_event_name: null,
    covariate_coverage_pct: null,
    unavailable_reason: reason,
    method: 'cuped',
  };
}

const T1 = computed('t1', 'green_button');
// Below 0.05 before correction, 0.03 after it, and the SERVER says not
// significant: the page must show the server's answer, not compare a p-value.
const T2 = computed('t2', 'red_button', {
  adjusted_effect: 0.0073,
  adjusted_p_value: 0.015,
  adjusted_ci_lower: 0.0024,
  adjusted_ci_upper: 0.0122,
  unadjusted_effect: 0.0078,
  corrected_p_value: 0.03,
  is_significant: false,
  variance_reduction_pct: -4.2,
  covariate_coverage_pct: 0,
  adjusted_treatment_mean: 0.0872,
});

function cuped(overrides: Record<string, unknown> = {}) {
  return {
    experiment_id: 'exp-1',
    method: 'cuped',
    confidence_level: 0.9,
    correction_method: 'benjamini_hochberg',
    covariate_lookback_days: 7,
    metrics: [
      T1,
      T2,
      unavailable('m-time', 'Time on page', 't1', 'green_button', 'not_a_proportion_metric'),
      unavailable('m-time', 'Time on page', 't2', 'red_button', 'not_a_proportion_metric'),
    ],
    computed_at: '2026-11-15T12:00:00+00:00',
    seed: null,
    n_samples: null,
    engine_version: '1.3.0',
    analysis_status: 'ga',
    analysis_notice: null,
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
    relative_improvement_pct: isControl ? null : 5,
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
  confidence_level: 0.9,
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
      metric_id: 'm-purchase',
      metric_name: 'Purchase',
      metric_type: 'conversion',
      is_primary: true,
      variants: [
        headlineVariant('ctrl', 'Control', true, 150, 15),
        headlineVariant('t1', 'green_button', false, 150, 22),
      ],
    },
  ],
};

interface Setup {
  /** The experiment's variance_reduction_config; `undefined` leaves the field out. */
  config?: unknown;
  /** The experiment cannot be read. */
  unreadable?: boolean;
  answer?: () => unknown;
}

function install(setup: Setup = {}) {
  // `config: undefined` leaves the field out; not naming it is a cuped experiment.
  const config = 'config' in setup ? setup.config : { method: 'cuped' };
  const { unreadable = false, answer } = setup;
  mockedApiFetch.mockImplementation(
    routedApi([
      {
        path: '/api/v1/experiments/exp-1',
        handler: () => {
          if (unreadable) throw apiError(500, 'no experiment');
          const experiment: Record<string, unknown> = {
            id: 'exp-1',
            correction_method: 'benjamini_hochberg',
            confidence_level: 0.9,
            variants: EXPERIMENT_VARIANTS,
          };
          if (config !== undefined) experiment.variance_reduction_config = config;
          return experiment;
        },
      },
      { path: '/api/v1/results/exp-1', handler: () => HEADLINE },
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
      { path: CUPED_ROUTE, handler: () => (answer ? answer() : cuped()) },
    ]) as unknown as typeof apiFetch,
  );
}

// --- reading the page -------------------------------------------------------

const cupedCalls = () => mockedApiFetch.mock.calls.filter(([p]) => p === CUPED_ROUTE);

async function openTab() {
  const view = render(<ResultDetailPage />);
  const tab = await screen.findByRole('tab', { name: 'CUPED (beta)' });
  await userEvent.click(tab);
  return view;
}

async function openLoaded() {
  const view = await openTab();
  const panel = await screen.findByTestId('cuped-results');
  return { view, panel };
}

const purchaseTable = () => screen.getByRole('table', { name: 'CUPED-adjusted results: Purchase' });

/** The row whose header names the variant. */
function rowOf(table: HTMLElement, variant: string): HTMLElement {
  const header = within(table)
    .getAllByRole('rowheader')
    .find((th) => (th.firstChild?.textContent ?? '').trim() === variant);
  if (!header) throw new Error(`no row for ${variant}`);
  return header.closest('tr') as HTMLElement;
}

/** The cells of a row after its header, by column. */
const cells = (row: HTMLElement) => Array.from(row.querySelectorAll('td')).map((td) => td.textContent ?? '');

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockRouter.query = { id: 'exp-1' };
});

// --- the fixture ------------------------------------------------------------

describe('the fixture can tell the cases apart', () => {
  it('is the shape the API documents', () => {
    expect(shapeErrors('CupedResultsResponse', cuped())).toEqual([]);
  });

  it('adjusted differs from unadjusted, corrected from adjusted, and the level is not 0.95', () => {
    for (const row of [T1, T2]) {
      expect(row.adjusted_effect).not.toBe(row.unadjusted_effect);
      expect(row.corrected_p_value).not.toBe(row.adjusted_p_value);
    }
    expect(cuped().confidence_level).toBe(0.9);
    expect(cuped().confidence_level).not.toBe(0.95);
  });
});

// --- the tab ----------------------------------------------------------------

describe('the CUPED tab', () => {
  it('sits after the Overview, Trends and Sample Size tabs and before Breakdowns', async () => {
    install();
    render(<ResultDetailPage />);
    await screen.findByRole('tab', { name: 'CUPED (beta)' });
    const names = screen.getAllByRole('tab').map((t) => t.textContent);
    expect(names.indexOf('CUPED (beta)')).toBe(names.indexOf('Breakdowns') - 1);
  });

  it('is there for cuped_plus too', async () => {
    install({ config: { method: 'cuped_plus' } });
    render(<ResultDetailPage />);
    expect(await screen.findByRole('tab', { name: 'CUPED (beta)' })).toBeInTheDocument();
  });

  it.each([
    ['none', { method: 'none' }],
    ['no config', null],
    ['no config field at all', undefined],
    ['winsorization', { method: 'winsorization' }],
    ['an unknown method', { method: 'cuped2' }],
  ])('is absent for %s, and nothing is asked', async (_name, config) => {
    install({ config });
    render(<ResultDetailPage />);
    await screen.findByRole('tab', { name: 'Breakdowns' });
    expect(screen.queryByRole('tab', { name: /cuped/i })).toBeNull();
    expect(cupedCalls()).toHaveLength(0);
  });

  it('is absent when the experiment could not be read', async () => {
    install({ unreadable: true });
    render(<ResultDetailPage />);
    await screen.findByRole('tab', { name: 'Breakdowns' });
    expect(screen.queryByRole('tab', { name: /cuped/i })).toBeNull();
    expect(cupedCalls()).toHaveLength(0);
  });

  it('asks for nothing until it is opened, then once, however often it is left and returned to', async () => {
    install();
    render(<ResultDetailPage />);
    await screen.findByRole('tab', { name: 'CUPED (beta)' });
    expect(cupedCalls()).toHaveLength(0);

    await userEvent.click(screen.getByRole('tab', { name: 'CUPED (beta)' }));
    await screen.findByTestId('cuped-results');
    expect(cupedCalls()).toHaveLength(1);
    // No query: the route reads nothing from one.
    expect(cupedCalls()[0][1]?.query).toBeUndefined();
    expect((cupedCalls()[0][1]?.method ?? 'GET').toUpperCase()).toBe('GET');

    await userEvent.click(screen.getByRole('tab', { name: 'Trends' }));
    await userEvent.click(screen.getByRole('tab', { name: 'CUPED (beta)' }));
    await screen.findByTestId('cuped-results');
    expect(cupedCalls()).toHaveLength(1);
  });

  it('says it is loading while it waits', async () => {
    let release: (value: unknown) => void = () => undefined;
    install({ answer: () => new Promise((resolve) => (release = resolve)) });
    await openTab();
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('Loading the CUPED results…');
    expect(screen.queryByTestId('cuped-results')).toBeNull();
    await act(async () => release(cuped()));
    await screen.findByTestId('cuped-results');
  });
});

// --- the numbers ------------------------------------------------------------

describe('the numbers are the API\'s', () => {
  it('heads the panel, says it is beta and what history it needs, with a link to send it', async () => {
    install();
    const { panel } = await openLoaded();
    expect(screen.getByRole('heading', { name: 'CUPED-adjusted results Beta' })).toBeInTheDocument();
    expect(panel).toHaveTextContent(
      'Beta: only conversion metrics are adjusted for now, so this tab may change when averages are added. ' +
        'Its numbers are computed as the CUPED guide describes.',
    );
    expect(panel).toHaveTextContent(
      'CUPED narrows each interval using what each user did in the 7 days before they were assigned. ' +
        'It needs that history: an event the server received after a user was assigned does not count for that user.',
    );
    const link = screen.getByRole('link', { name: 'How to send pre-experiment history' });
    expect(link.getAttribute('href')).toMatch(/api\/cuped.*#sending-pre-experiment-history$/);
    expect(panel).toHaveTextContent('Method: CUPED.');
  });

  it('shows the ADJUSTED effect, with the API\'s bounds, at the response\'s own level', async () => {
    install();
    await openLoaded();
    const [users, rates, change, without] = cells(rowOf(purchaseTable(), 'green_button'));
    expect(users).toBe('14,000 / 15,000');
    expect(rates).toBe('8.85% / 7.99%');
    // +0.86 is adjusted_effect; the unadjusted +0.91 is its own column.
    expect(change).toMatch(/^\+0\.86 percentage points \(90% CI/);
    expect(change).toContain('+0.37');
    expect(change).toContain('+1.35');
    expect(change).not.toContain('95%');
    expect(change).not.toContain('+0.91');
    expect(without).toBe('+0.91 percentage points');
  });

  it('marks the interval unadjusted when a corrected p-value is shown on a metric with two comparisons', async () => {
    install();
    await openLoaded();
    // "90% CI" is shown; a screen reader hears "90% confidence interval" from a hidden twin.
    expect(cells(rowOf(purchaseTable(), 'green_button'))[2]).toMatch(
      /90% CI.*, unadjusted, \+0\.37 to \+1\.35\)$/,
    );
  });

  it('shows the CORRECTED p-value, named so, and the server\'s significance', async () => {
    install();
    await openLoaded();
    const table = purchaseTable();
    expect(within(rowOf(table, 'green_button')).getByRole('status')).toHaveTextContent(
      'Significant (corrected p=0.008)',
    );
    // 0.015 before and 0.03 after the correction, and the server says no.
    expect(within(rowOf(table, 'red_button')).getByRole('status')).toHaveTextContent(
      'Not Significant (corrected p=0.030)',
    );
  });

  it('shows the p-value before correction, named plainly, when there is no corrected one', async () => {
    install({
      answer: () =>
        cuped({
          correction_method: 'none',
          metrics: [{ ...T1, corrected_p_value: null }],
        }),
    });
    await openLoaded();
    const row = rowOf(purchaseTable(), 'green_button');
    expect(within(row).getByRole('status')).toHaveTextContent('Significant (p=0.004)');
    // One comparison and no correction: the interval is not marked.
    expect(cells(row)[2]).not.toContain('unadjusted');
    expect(screen.queryByText(/are corrected with/)).toBeNull();
  });

  it('says how the p-values were corrected, and what variance reduction means', async () => {
    install();
    const { panel } = await openLoaded();
    expect(panel).toHaveTextContent("p-values are corrected with Benjamini-Hochberg across each metric's variants.");
    expect(panel).toHaveTextContent(
      'Variance reduction is how much CUPED shrank the variance of the change: at 36% the interval is 80% as wide. ' +
        'It can be negative; then CUPED did not help that comparison, and its numbers are still valid.',
    );
  });

  it('shows variance reduction and users with history as the API sent them, a negative one with a real minus', async () => {
    install();
    await openLoaded();
    const table = purchaseTable();
    const first = cells(rowOf(table, 'green_button'));
    expect(first[4]).toBe('12.7%');
    expect(first[5]).toBe('9.9%');
    const second = cells(rowOf(table, 'red_button'));
    expect(second[4]).toBe('−4.2%');
    expect(second[5]).toBe('0% (no history: same as without CUPED)');
  });

  it('names the control each variant is compared with, from the experiment', async () => {
    install();
    await openLoaded();
    expect(rowOf(purchaseTable(), 'green_button').querySelector('th')).toHaveTextContent('vs Control');
  });

  it('says CUPED+ was computed as CUPED when the top says cuped_plus and the rows say cuped', async () => {
    install({ config: { method: 'cuped_plus' }, answer: () => cuped({ method: 'cuped_plus' }) });
    const { panel } = await openLoaded();
    expect(panel).toHaveTextContent('Method: CUPED+, computed as CUPED for now.');
    expect(panel).not.toHaveTextContent('Method: CUPED.');
  });

  it('never calls a number an "adjusted p-value": here that word means CUPED-adjusted, before correction', async () => {
    install();
    const { panel } = await openLoaded();
    // The panel, not the page: the CSV note beside it says "no adjusted p-value" of its own.
    expect(within(panel).queryByText(/adjusted p-value/i)).toBeNull();
    expect(panel.textContent ?? '').not.toMatch(/adjusted p-value/i);
  });

  it('keeps the Overview table as it was: unadjusted, on its own tab', async () => {
    install();
    await openLoaded();
    await userEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(await screen.findByText('All Metrics')).toBeInTheDocument();
    expect(screen.queryByTestId('cuped-results')).toBeNull();
  });

  it('is a real table: a caption, column headers, and a row header for each variant', async () => {
    install();
    const { panel } = await openLoaded();
    const table = purchaseTable();
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Variant',
      'Users (variant / control)',
      'Rate with CUPED (variant / control)',
      'Change with CUPED',
      'Change without CUPED',
      'Variance reduction',
      'Users with history',
      'Result',
    ]);
    const violations = (await axe.run(panel, axeOptions)).violations.map((v) => v.id);
    expect(violations).toEqual([]);
  });
});

// --- every comparison, reasons included -------------------------------------

describe('every comparison is listed', () => {
  it('lists one row for every metric and treatment: 2 metrics x 2 treatments = 4', async () => {
    install();
    const { panel } = await openLoaded();
    expect(within(panel).getAllByRole('table')).toHaveLength(2);
    expect(panel.querySelectorAll('tbody tr')).toHaveLength(4);
  });

  it('a comparison that was not adjusted shows its variant and the reason, and no number', async () => {
    install();
    await openLoaded();
    const table = screen.getByRole('table', { name: 'CUPED-adjusted results: Time on page' });
    const row = rowOf(table, 'green_button');
    expect(cells(row)).toEqual([
      'Not adjusted: only conversion metrics are adjusted for now. Its numbers without CUPED are on the Overview tab.',
    ]);
    expect(within(row).queryByRole('status')).toBeNull();
  });

  it('a metric with no variant to compare says why in a sentence, not a table', async () => {
    install({
      answer: () =>
        cuped({
          metrics: [
            {
              ...unavailable('m-purchase', 'Purchase', 't1', 'x', 'no_control_variant'),
              variant_id: null,
              variant_name: null,
              control_variant_id: null,
            },
          ],
        }),
    });
    const { panel } = await openLoaded();
    expect(within(panel).queryByRole('table')).toBeNull();
    expect(panel).toHaveTextContent('Not adjusted: the experiment has no control variant.');
  });

  it('says so when the response is not a CUPED one, or has no metrics', async () => {
    install({ answer: () => cuped({ method: 'none' }) });
    const first = await openLoaded();
    expect(first.panel).toHaveTextContent('This experiment does not use CUPED.');
    first.view.unmount();

    mockedApiFetch.mockReset();
    install({ answer: () => cuped({ metrics: [] }) });
    const second = await openLoaded();
    expect(second.panel).toHaveTextContent('This experiment has no metrics to adjust.');
  });
});

describe('every reason the API documents has its fixed copy', () => {
  // The codes the API names for `unavailable_reason`, read from the stable
  // snapshot: a code added there with no copy here fails this file.
  const documented = documentedCupedReasonCodes();

  it('reads the codes out of the snapshot', () => {
    expect(documented).toHaveLength(10);
    expect(documented).toContain('no_residual_variation');
    expect(documented).toContain('result_invalid');
  });

  it('has copy for exactly the documented codes, each beginning "Not adjusted"', () => {
    expect(Object.keys(CUPED_REASON_COPY).sort()).toEqual([...documented].sort());
    for (const copy of Object.values(CUPED_REASON_COPY)) expect(copy).toMatch(/^Not adjusted/);
  });

  it.each(documented)('%s: its sentence, and no number', async (code) => {
    install({
      answer: () =>
        cuped({
          metrics: [unavailable('m-purchase', 'Purchase', 't1', 'green_button', code)],
        }),
    });
    await openLoaded();
    const row = rowOf(purchaseTable(), 'green_button');
    expect(cells(row)).toEqual([CUPED_REASON_COPY[code]]);
    expect(row.textContent ?? '').not.toMatch(/\d+\.\d/);
  });

  it('a code it does not know reads "Not adjusted for this comparison."', async () => {
    install({
      answer: () =>
        cuped({
          metrics: [unavailable('m-purchase', 'Purchase', 't1', 'green_button', 'a_code_from_a_newer_server')],
        }),
    });
    await openLoaded();
    expect(cells(rowOf(purchaseTable(), 'green_button'))).toEqual(['Not adjusted for this comparison.']);
  });
});

// --- failures ---------------------------------------------------------------

describe('a failed load shows fixed copy, never the server text, and leaves the page alone', () => {
  it.each([
    ['a 500', () => apiError(500, PLANTED), 'The server could not compute the CUPED results. Try again; if it keeps failing, check the API logs.'],
    ['a 403', () => apiError(403, PLANTED), 'You do not have access to these results.'],
    ['a 404', () => apiError(404, PLANTED), 'No results were found for this experiment.'],
    ['a 422', () => apiError(422, PLANTED), 'The CUPED results could not be loaded.'],
    ['a TypeError', () => new TypeError(`Cannot read ${PLANTED}`), 'The CUPED results could not be loaded.'],
  ])('%s', async (_name, makeError, copy) => {
    install({
      answer: () => {
        throw makeError();
      },
    });
    await openTab();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(copy);
    expect(document.body.textContent ?? '').not.toContain('PLANTED');
    // The witness marks the answered state only.
    expect(screen.queryByTestId('cuped-results')).toBeNull();
    // The rest of the page still works.
    await userEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(await screen.findByText('All Metrics')).toBeInTheDocument();
  });

  it('an unreachable API keeps its own message', async () => {
    install({
      answer: () => {
        throw apiError(0, 'The API could not be reached.');
      },
    });
    await openTab();
    expect(await screen.findByRole('alert')).toHaveTextContent('The API could not be reached.');
  });

  it('Try again asks once more, and moves focus to the heading', async () => {
    let fail = true;
    install({
      answer: () => {
        if (fail) throw apiError(500, PLANTED);
        return cuped();
      },
    });
    await openTab();
    await screen.findByRole('alert');
    expect(cupedCalls()).toHaveLength(1);
    fail = false;
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await screen.findByTestId('cuped-results');
    expect(cupedCalls()).toHaveLength(2);
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'CUPED-adjusted results Beta' })),
    );
  });

  it('offers no control but Try again: the tab only reads', async () => {
    install();
    const { panel } = await openLoaded();
    expect(within(panel).queryAllByRole('button')).toEqual([]);
    expect(within(panel).queryAllByRole('textbox')).toEqual([]);
    const requests = mockedApiFetch.mock.calls.map(([, o]) => (o?.method ?? 'GET').toUpperCase());
    expect(requests.every((m) => m === 'GET')).toBe(true);
  });
});
