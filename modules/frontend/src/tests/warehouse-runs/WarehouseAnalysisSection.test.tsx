/**
 * The experiment page's "Warehouse analysis (beta)" section, against a
 * routed stand-in for the W4 API: every state a run can be in, every refusal
 * of "Analyse now", the role gating, SRM, "Not computed", and View SQL.
 */
import React from 'react';
import axe from 'axe-core';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { ApiError, apiFetch } from '@/services/api';
import { routedApi, Route } from '@/tests/pages/helpers/apiMock';
import WarehouseAnalysisSection from '@modules/components/warehouse/runs/WarehouseAnalysisSection';
import RunResults from '@modules/components/warehouse/runs/RunResults';
import {
  isKnownFailure,
  RESULTS_DIFFERENCE,
  RUN_POLL_MS,
  failureCopy,
} from '@modules/services/warehouseRuns';
import {
  CONNECTIONS_PATH,
  EXPERIMENT,
  RUNS_PATH,
  SOURCES,
  SOURCES_PATH,
  SQL,
  computedMetric,
  connection,
  meanMetric,
  notComputedMetric,
  results,
  run,
  runPath,
} from './fixtures';
import { storableCodes } from './backendCodes';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockUseAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

function as(role: string, superuser = false) {
  mockUseAuth.mockReturnValue({
    user: { id: 'u-1', email: 'u@example.com', username: 'u', role, is_superuser: superuser },
    status: 'authenticated',
  });
}

function install(routes: Route[]) {
  mockedApiFetch.mockImplementation(routedApi(routes) as unknown as typeof apiFetch);
}

const calls = (method: string, path: string) =>
  mockedApiFetch.mock.calls.filter(
    ([p, o]) => p.split('?')[0] === path && (o?.method ?? 'GET').toUpperCase() === method,
  );

function renderSection(modules: string[] = ['warehouse']) {
  return render(
    <ModulesProvider initial={{ profile: 'full', modules, version: 'test' }}>
      <WarehouseAnalysisSection experiment={EXPERIMENT} />
    </ModulesProvider>,
  );
}

function refusal(status: number, detail: Record<string, unknown>) {
  return () => {
    throw new ApiError({ status, detail });
  };
}

const formRoutes = (extra: Route[] = []): Route[] => [
  { path: RUNS_PATH, handler: () => ({ runs: [] }) },
  { path: CONNECTIONS_PATH, handler: () => ({ connections: [connection()] }) },
  { path: SOURCES_PATH, handler: () => ({ sources: SOURCES }) },
  ...extra,
];

async function openForm() {
  fireEvent.click(await screen.findByTestId('warehouse-open-form'));
  await screen.findByTestId('warehouse-start-form');
}

function tick(name: RegExp) {
  fireEvent.click(screen.getByRole('checkbox', { name }));
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  as('DEVELOPER');
});

afterEach(() => {
  jest.useRealTimers();
});

describe('module gate', () => {
  it('renders nothing and calls no warehouse route when the module is not installed', () => {
    install([]);
    const { container } = renderSection([]);
    expect(container).toBeEmptyDOMElement();
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it('renders the section with a Beta label explained on keyboard focus', async () => {
    install([{ path: RUNS_PATH, handler: () => ({ runs: [] }) }]);
    renderSection();
    expect(await screen.findByTestId('warehouse-no-runs')).toHaveTextContent(
      'No warehouse analysis of this experiment yet.',
    );
    const beta = screen.getByTestId('warehouse-beta');
    expect(beta).toHaveTextContent('Beta');
    const note = document.getElementById('warehouse-beta-explanation')!;
    expect(note).not.toBeVisible();
    act(() => beta.focus());
    expect(note).toBeVisible();
    expect(note).toHaveTextContent('its settings and API may change');
  });
});

describe('roles', () => {
  it('ANALYST reads runs, sees why it cannot start one, and never lists connections', async () => {
    as('ANALYST');
    install([{ path: RUNS_PATH, handler: () => ({ runs: [run()] }) }]);
    renderSection();
    expect(await screen.findByTestId('warehouse-results')).toBeInTheDocument();
    expect(screen.getByTestId('warehouse-role-note')).toHaveTextContent(
      'Starting a warehouse analysis requires the ADMIN or DEVELOPER role; you are ANALYST.',
    );
    expect(screen.queryByTestId('warehouse-open-form')).toBeNull();
    expect(calls('GET', CONNECTIONS_PATH)).toHaveLength(0);
    expect(calls('GET', SOURCES_PATH)).toHaveLength(0);
  });

  // D50: VIEWER does not read warehouse analyses or previews. The section asks
  // the API for nothing, so a VIEWER's experiment page causes no refused
  // request, and says why in one line.
  const READ_NOTE =
    'Viewing warehouse analyses requires the ADMIN, DEVELOPER or ANALYST role; you are VIEWER.';

  it('VIEWER gets one line saying why, and the section makes no request at all', async () => {
    as('VIEWER');
    install([{ path: RUNS_PATH, handler: () => ({ runs: [run()] }) }]);
    renderSection();
    expect(await screen.findByTestId('warehouse-read-note')).toHaveTextContent(READ_NOTE);
    // Give a load that should not happen the chance to happen.
    await act(async () => {
      await Promise.resolve();
    });
    expect(calls('GET', RUNS_PATH)).toHaveLength(0);
    expect(mockedApiFetch).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'Warehouse analysis' })).toBeInTheDocument();
    expect(screen.queryByTestId('warehouse-results')).toBeNull();
    expect(screen.queryByTestId('warehouse-no-runs')).toBeNull();
    expect(screen.queryByTestId('warehouse-run-history')).toBeNull();
    expect(screen.queryByTestId('warehouse-open-form')).toBeNull();
    expect(screen.queryByText('View SQL')).toBeNull();
    expect(screen.queryByText(/You can read every analysis below/)).toBeNull();
  });

  it('the VIEWER render has no axe violations', async () => {
    as('VIEWER');
    install([]);
    const { container } = renderSection();
    await screen.findByTestId('warehouse-read-note');
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('renders nothing and asks for no runs before the session has loaded', async () => {
    mockUseAuth.mockReturnValue({ user: null, status: 'loading' });
    install([{ path: RUNS_PATH, handler: () => ({ runs: [run()] }) }]);
    const { container } = renderSection();
    await act(async () => {
      await Promise.resolve();
    });
    expect(container).toBeEmptyDOMElement();
    expect(calls('GET', RUNS_PATH)).toHaveLength(0);
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it('a superuser whose role is VIEWER reads runs', async () => {
    as('VIEWER', true);
    install([{ path: RUNS_PATH, handler: () => ({ runs: [run()] }) }]);
    renderSection();
    expect(await screen.findByTestId('warehouse-results')).toBeInTheDocument();
    expect(calls('GET', RUNS_PATH)).toHaveLength(1);
    expect(screen.queryByTestId('warehouse-read-note')).toBeNull();
  });

  it.each([
    ['ADMIN', false],
    ['DEVELOPER', false],
    ['VIEWER', true],
  ])('%s (superuser %s) may start an analysis', async (role, superuser) => {
    as(role, superuser);
    install([{ path: RUNS_PATH, handler: () => ({ runs: [] }) }]);
    renderSection();
    expect(await screen.findByTestId('warehouse-open-form')).toHaveTextContent('Analyse in warehouse');
    expect(screen.queryByTestId('warehouse-role-note')).toBeNull();
  });
});

describe('starting an analysis', () => {
  it('says so when there is no warehouse connection', async () => {
    install([
      { path: RUNS_PATH, handler: () => ({ runs: [] }) },
      { path: CONNECTIONS_PATH, handler: () => ({ connections: [] }) },
      { path: SOURCES_PATH, handler: () => ({ sources: [] }) },
    ]);
    renderSection();
    fireEvent.click(await screen.findByTestId('warehouse-open-form'));
    expect(await screen.findByTestId('warehouse-no-connection')).toHaveTextContent(
      'No warehouse connection yet.',
    );
  });

  it('lets a mean metric be ticked, and says it is a mean', async () => {
    install(formRoutes());
    renderSection();
    await openForm();
    const mean = screen.getByRole('checkbox', { name: /Revenue per user/ });
    expect(mean).toBeEnabled();
    tick(/Revenue per user/);
    expect(mean).toBeChecked();
    expect(mean).toHaveAccessibleName('Revenue per user (mean, primary)');
  });

  it('sends the ticked metrics in tick order (first is primary) and shows the queued run', async () => {
    let posted: unknown = null;
    install(
      formRoutes([
        {
          method: 'POST',
          path: RUNS_PATH,
          handler: (_p, o) => {
            posted = o.json;
            return { run_id: 'run-9', status: 'queued' };
          },
        },
        { path: runPath('run-9'), handler: () => run({ id: 'run-9', status: 'queued' }) },
      ]),
    );
    renderSection();
    await openForm();
    const start = screen.getByTestId('warehouse-start');
    expect(start).toBeDisabled();
    tick(/Signed up/);
    tick(/Purchased/);
    expect(screen.getByRole('checkbox', { name: /Signed up/ })).toHaveAccessibleName(
      'Signed up (proportion, primary)',
    );
    fireEvent.click(start);
    await waitFor(() =>
      expect(screen.getByTestId('warehouse-run-status')).toHaveTextContent(
        'Queued: Analysis queued on BigQuery · Prod analytics.',
      ),
    );
    expect(posted).toEqual({
      connection_id: 'conn-1',
      assignment_source_id: 'src-a',
      metric_source_ids: ['src-s', 'src-p'],
    });
  });

  it('requires a window when the experiment has no start date, and sends it as UTC', async () => {
    let posted: Record<string, unknown> = {};
    install(
      formRoutes([
        {
          method: 'POST',
          path: RUNS_PATH,
          handler: (_p, o) => {
            posted = o.json as Record<string, unknown>;
            return { run_id: 'run-9', status: 'queued' };
          },
        },
        { path: runPath('run-9'), handler: () => run({ id: 'run-9', status: 'queued' }) },
      ]),
    );
    render(
      <ModulesProvider initial={{ profile: 'full', modules: ['warehouse'], version: 'test' }}>
        <WarehouseAnalysisSection experiment={{ ...EXPERIMENT, start_date: null }} />
      </ModulesProvider>,
    );
    await openForm();
    tick(/Purchased/);
    expect(screen.getByTestId('warehouse-start')).toBeDisabled();
    fireEvent.change(screen.getByTestId('warehouse-window-start'), { target: { value: '2026-09-01T00:00' } });
    fireEvent.change(screen.getByTestId('warehouse-window-end'), { target: { value: '2026-09-15T12:30' } });
    fireEvent.click(screen.getByTestId('warehouse-start'));
    await waitFor(() => expect(posted.window_start).toBe('2026-09-01T00:00:00Z'));
    expect(posted.window_end).toBe('2026-09-15T12:30:00Z');
  });

  it('shows the daily limit with the limit, the reset time and the wait', async () => {
    jest.useFakeTimers({ now: new Date('2026-09-29T18:48:00Z'), doNotFake: ['setTimeout', 'clearTimeout'] });
    install(
      formRoutes([
        {
          method: 'POST',
          path: RUNS_PATH,
          handler: refusal(429, {
            code: 'daily_run_limit_reached',
            message: 'API words',
            limit: 20,
            resets_at: '2026-09-30T00:00:00Z',
          }),
        },
      ]),
    );
    renderSection();
    await openForm();
    tick(/Purchased/);
    fireEvent.click(screen.getByTestId('warehouse-start'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveAttribute('data-code', 'daily_run_limit_reached');
    expect(alert).toHaveTextContent('reached its limit of 20 analyses per day (UTC, previews included)');
    expect(alert).toHaveTextContent('The count resets at 2026-09-30 00:00 UTC, in 5 h 12 min.');
  });

  it.each([
    [
      409,
      'run_in_progress',
      'Not started: an analysis or preview on this connection is already queued or running.',
    ],
    [429, 'warehouse_busy', 'Try again in about 30 seconds.'],
    [
      422,
      'source_not_validated',
      'Not started: Validate the source “Exposures” before using it. Validate it in Warehouse › Sources.',
    ],
    [
      403,
      'role_required',
      'Starting a warehouse analysis requires the ADMIN or DEVELOPER role; you are VIEWER.',
    ],
    [
      503,
      'credentials_unavailable',
      'WAREHOUSE_CREDENTIALS_KEYS is not set on this deployment.',
    ],
  ])('shows a %s %s refusal as an alert', async (status, code, words) => {
    const message =
      code === 'source_not_validated'
        ? 'Validate the source “Exposures” before using it.'
        : code === 'role_required'
          ? 'Starting a warehouse analysis requires the ADMIN or DEVELOPER role; you are VIEWER.'
          : code === 'credentials_unavailable'
            ? "Warehouse credentials can't be stored or used: WAREHOUSE_CREDENTIALS_KEYS is not set on this deployment."
            : 'API words';
    install(formRoutes([{ method: 'POST', path: RUNS_PATH, handler: refusal(status, { code, message }) }]));
    renderSection();
    await openForm();
    tick(/Purchased/);
    fireEvent.click(screen.getByTestId('warehouse-start'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveAttribute('data-code', code);
    expect(alert).toHaveTextContent(words);
  });
});

describe('a run in progress', () => {
  it('announces queued, then running, then the results, from a polite status region', async () => {
    jest.useFakeTimers();
    const sequence = [run({ status: 'running' }), run({ status: 'succeeded' })];
    install([
      { path: RUNS_PATH, handler: () => ({ runs: [run({ status: 'queued' })] }) },
      { path: runPath('run-1'), handler: () => sequence.shift() ?? run() },
    ]);
    renderSection();
    const status = await screen.findByTestId('warehouse-run-status');
    expect(status).toHaveAttribute('role', 'status');
    expect(status).toHaveAttribute('aria-live', 'polite');
    await waitFor(() => expect(status).toHaveTextContent('Queued: Analysis queued on BigQuery · Prod analytics'));
    expect(screen.queryByTestId('warehouse-results')).toBeNull();
    // The start button waits for the run to finish, and says so.
    expect(screen.getByTestId('warehouse-open-form')).toBeDisabled();
    expect(screen.getByTestId('warehouse-open-form')).toHaveAccessibleDescription(
      'An analysis is in progress; you can start another when it finishes.',
    );

    await act(async () => {
      jest.advanceTimersByTime(RUN_POLL_MS);
    });
    await waitFor(() => expect(status).toHaveTextContent('Running: Analysis running on BigQuery'));
    await act(async () => {
      jest.advanceTimersByTime(RUN_POLL_MS);
    });
    await waitFor(() => expect(status).toHaveTextContent('Succeeded: Analysis succeeded at 2026-09-28 09:00 UTC.'));
    expect(screen.getByTestId('warehouse-results')).toBeInTheDocument();
    expect(calls('GET', runPath('run-1'))).toHaveLength(2);
    // Finished: no more polling.
    await act(async () => {
      jest.advanceTimersByTime(RUN_POLL_MS * 3);
    });
    expect(calls('GET', runPath('run-1'))).toHaveLength(2);
  });
});

describe('results', () => {
  it('shows the source strip, the metric table and the difference from /results', async () => {
    install([{ path: RUNS_PATH, handler: () => ({ runs: [run()] }) }]);
    renderSection();
    const strip = await screen.findByTestId('warehouse-source-strip');
    expect(strip).toHaveTextContent('BigQuery · Prod analytics');
    expect(strip).toHaveTextContent('took 48 s');
    const times = strip.querySelectorAll('time');
    expect(Array.from(times).map((t) => t.getAttribute('datetime'))).toEqual([
      '2026-09-01T00:00:00Z',
      '2026-09-28T00:00:00Z',
      '2026-09-28T09:00:49Z',
    ]);
    expect(screen.getByTestId('warehouse-cost')).toHaveTextContent('Billed 21.0 MB');
    const rows = screen.getAllByTestId('warehouse-variant-row');
    expect(rows[0]).toHaveTextContent('control (control)10,0001,00010.00%——Baseline');
    expect(rows[1]).toHaveTextContent('blue10,0501,20612.00%+20.00% · better< 0.0001Significant');
    expect(screen.getByTestId('warehouse-results-difference')).toHaveTextContent(RESULTS_DIFFERENCE);
    expect(screen.getByTestId('warehouse-srm-ok')).toHaveTextContent('Sample ratio check: passed (p = 0.7200).');
  });

  it('warns about a sample ratio mismatch in words, with the expected and observed split', async () => {
    install([
      {
        path: RUNS_PATH,
        handler: () => ({
          runs: [
            run({
              results: results({
                srm: {
                  chi2: 40,
                  p_value: 0.00002,
                  warning: true,
                  expected: { 'v-control': 10000, 'v-blue': 10000 },
                  observed: { 'v-control': 9000, 'v-blue': 11000 },
                },
              }),
            }),
          ],
        }),
      },
    ]);
    renderSection();
    const srm = await screen.findByTestId('warehouse-srm-warning');
    expect(srm).toHaveTextContent('Warning: sample ratio mismatch (p = < 0.0001).');
    expect(srm).toHaveTextContent('may be biased');
    const table = within(srm).getByRole('table');
    expect(table).toHaveTextContent('control50.0%45.0%9,000');
    expect(table).toHaveTextContent('blue50.0%55.0%11,000');
  });

  it('says the SRM check was skipped for adaptive allocation', async () => {
    install([
      { path: RUNS_PATH, handler: () => ({ runs: [run({ results: results({ srm: null, srm_skipped: 'adaptive_allocation' }) })] }) },
    ]);
    renderSection();
    expect(await screen.findByTestId('warehouse-srm-skipped')).toHaveTextContent(
      'Sample ratio check: skipped',
    );
    expect(screen.queryByTestId('warehouse-srm-warning')).toBeNull();
  });

  it('shows a mean metric as its mean and interval, not as a rate', async () => {
    install([
      {
        path: RUNS_PATH,
        handler: () => ({ runs: [run({ results: results({ metrics: [computedMetric(), meanMetric()] }) })] }),
      },
    ]);
    renderSection();
    const tables = await screen.findAllByTestId('warehouse-metric');
    const mean = tables[1];
    expect(within(mean).getByRole('table')).toHaveAccessibleName('Revenue per user: mean by variant');
    const headers = within(mean).getAllByRole('columnheader').map((h) => h.textContent);
    expect(headers).toEqual(['Variant', 'Units', 'Mean', '95% interval', 'Change', 'p-value', 'Result']);
    const rows = within(mean).getAllByTestId('warehouse-variant-row');
    expect(rows[0]).toHaveTextContent('control (control)10,00012.3512.26 to 12.43——Baseline');
    expect(rows[1]).toHaveTextContent('blue10,05012.512.41 to 12.59+1.25% · better0.0120Significant');
    expect(mean).not.toHaveTextContent('%—');
  });

  it('says why a mean comparison was not computed, in the Result cell', async () => {
    const metric = meanMetric();
    const [control, treatment] = metric.result!.variants;
    metric.result!.variants = [
      control,
      { ...treatment, p_value: null, is_significant: false, statistical_test_used: null, note: 'Not computed: no variation' },
    ];
    install([
      { path: RUNS_PATH, handler: () => ({ runs: [run({ results: results({ metrics: [metric] }) })] }) },
    ]);
    renderSection();
    const rows = await screen.findAllByTestId('warehouse-variant-row');
    expect(rows[1]).toHaveTextContent('Not computed: no variation');
    expect(rows[1]).not.toHaveTextContent('Not significant');
  });

  it.each(['no_units', 'join_key_mismatch', 'result_invalid', 'too_many_variant_values', 'fewer_than_2_units'])(
    'shows a metric not computed for %s as "Not computed", never as 0',
    async (code) => {
      install([
        {
          path: RUNS_PATH,
          handler: () => ({
            runs: [run({ results: results({ metrics: [computedMetric(), notComputedMetric(code)] }) })],
          }),
        },
      ]);
      renderSection();
      const box = await screen.findByTestId('warehouse-metric-not-computed');
      const copy = failureCopy(code, null, {
        warehouse: 'BigQuery',
        connection: 'Prod analytics',
        experimentKey: 'checkout-v2',
      });
      expect(box).toHaveTextContent(`Signed up`);
      expect(box).toHaveTextContent(`Not computed: ${copy.title}`);
      expect(box).not.toHaveTextContent(/\b0(\.0+)?%/);
      expect(within(box).queryByRole('table')).toBeNull();
    },
  );

  it('falls back to the API’s words for a not-computed reason it has none for', async () => {
    install([
      {
        path: RUNS_PATH,
        handler: () => ({ runs: [run({ results: results({ metrics: [notComputedMetric('brand_new_code')] }) })] }),
      },
    ]);
    renderSection();
    expect(await screen.findByTestId('warehouse-metric-not-computed')).toHaveTextContent(
      'Not computed: API words for brand_new_code.',
    );
  });

  it('lists multi-variant units and unmapped labels as left out', async () => {
    const base = results();
    install([
      {
        path: RUNS_PATH,
        handler: () => ({
          runs: [
            run({
              results: results({
                diagnostics: { ...base.diagnostics!, multi_variant_units: 12 },
                unmapped_labels: [{ label: 'Treatment', units: 40 }],
              }),
            }),
          ],
        }),
      },
    ]);
    renderSection();
    const notes = await screen.findByTestId('warehouse-diagnostics');
    expect(notes).toHaveTextContent('12 units were exposed to more than one variant and are left out.');
    expect(notes).toHaveTextContent('The variant value “Treatment” (40 units) matches no variant');
  });
});

describe('a failed run', () => {
  it.each(storableCodes())('shows %s in words, with no numbers', async (code) => {
    install([
      {
        path: RUNS_PATH,
        handler: () => ({
          runs: [run({ status: 'failed', error_code: code, error_message: 'API words', results: null })],
        }),
      },
    ]);
    renderSection();
    const box = await screen.findByTestId('warehouse-run-failed');
    const copy = failureCopy(code, 'API words', {
      warehouse: 'BigQuery',
      connection: 'Prod analytics',
      experimentKey: 'checkout-v2',
    });
    // The dashboard's own words, not the API's fallback message.
    expect(isKnownFailure(code)).toBe(true);
    expect(copy.title).not.toBe('API words');
    expect(copy.fix).toBeTruthy();
    expect(box).toHaveAttribute('data-code', code);
    expect(box).toHaveTextContent(`Failed: ${copy.title}`);
    expect(box).toHaveTextContent(copy.fix as string);
    expect(box).not.toHaveAttribute('role', 'alert');
    expect(screen.queryByTestId('warehouse-results')).toBeNull();
    expect(screen.getByTestId('warehouse-run-status')).toHaveTextContent('Failed: Analysis failed at');
  });

  it('shows the API’s words for a code it has none for', async () => {
    install([
      {
        path: RUNS_PATH,
        handler: () => ({
          runs: [run({ status: 'failed', error_code: 'a_code_added_later', error_message: 'Words the API has for a newer code.' })],
        }),
      },
    ]);
    renderSection();
    expect(await screen.findByTestId('warehouse-run-failed')).toHaveTextContent(
      'Failed: Words the API has for a newer code.',
    );
  });

  it('keeps the last good results, dated, under a failed run', async () => {
    install([
      {
        path: RUNS_PATH,
        handler: () => ({
          runs: [
            run({ id: 'run-2', status: 'failed', error_code: 'auth_failed', created_at: '2026-09-29T09:00:00Z', finished_at: '2026-09-29T09:00:03Z' }),
            run(),
          ],
        }),
      },
    ]);
    renderSection();
    expect(await screen.findByTestId('warehouse-run-failed')).toHaveTextContent(
      'Failed: BigQuery did not accept the credentials of Prod analytics.',
    );
    expect(screen.getByTestId('warehouse-results-dated')).toHaveTextContent(
      'These results are from the analysis that finished 2026-09-28 09:00 UTC.',
    );
    expect(screen.getByTestId('warehouse-run-history')).toHaveTextContent('auth_failed');
  });
});

describe('View SQL', () => {
  it('opens a labelled dialog, keeps focus inside, closes on Escape and returns focus', async () => {
    install([{ path: RUNS_PATH, handler: () => ({ runs: [run()] }) }]);
    renderSection();
    const trigger = await screen.findByTestId('warehouse-view-sql');
    act(() => trigger.focus());
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'SQL this analysis ran' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const close = screen.getByTestId('warehouse-sql-close');
    expect(close).toHaveFocus();
    const regions = within(dialog).getAllByRole('region', { name: /SQL$/ });
    expect(regions[0]).toHaveAttribute('tabindex', '0');
    expect(regions[0]).toHaveAccessibleName('Diagnostics statement, SQL');
    expect(regions[0]).toHaveTextContent(SQL);
    expect(regions[1]).toHaveAccessibleName('Metric statement 1, SQL');

    // Shift+Tab from the first focusable wraps to the last; Tab from the last wraps to the first.
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
    const focusables = dialog.querySelectorAll<HTMLElement>('button, [tabindex="0"]');
    const last = focusables[focusables.length - 1];
    expect(last).toHaveFocus();
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(close).toHaveFocus();

    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it('is open to an ANALYST, and a copy is announced politely', async () => {
    as('ANALYST');
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    install([{ path: RUNS_PATH, handler: () => ({ runs: [run()] }) }]);
    renderSection();
    fireEvent.click(await screen.findByTestId('warehouse-view-sql'));
    fireEvent.click(screen.getByRole('button', { name: 'Copy diagnostics statement' }));
    await waitFor(() =>
      expect(screen.getByTestId('warehouse-sql-copied')).toHaveTextContent('Copied the diagnostics statement.'),
    );
    expect(writeText).toHaveBeenCalledWith(SQL);
    expect(screen.getByTestId('warehouse-sql-copied')).toHaveAttribute('aria-live', 'polite');
  });
  // Both runs carry statements, so what hides the buttons here is the role,
  // not a null from the API.
  const withStatements = () => [
    run({ id: 'run-2', status: 'failed', error_code: 'auth_failed', created_at: '2026-09-29T09:00:00Z', finished_at: '2026-09-29T09:00:03Z' }),
    run(),
  ];

  it('is not offered to a VIEWER, who is not given the runs at all (D50)', async () => {
    as('VIEWER');
    const runs = withStatements();
    expect(runs.every((r) => r.statements && r.statements.length > 0)).toBe(true);
    install([{ path: RUNS_PATH, handler: () => ({ runs }) }]);
    renderSection();
    await screen.findByTestId('warehouse-read-note');
    expect(calls('GET', RUNS_PATH)).toHaveLength(0);
    expect(screen.queryByTestId('warehouse-run-failed')).toBeNull();
    expect(screen.queryByTestId('warehouse-view-sql')).toBeNull();
    expect(screen.queryByTestId('warehouse-failed-view-sql')).toBeNull();
    expect(screen.queryByText('View SQL')).toBeNull();
  });

  it.each([['ADMIN'], ['DEVELOPER'], ['ANALYST']])(
    'is offered to %s on both the results and a failed run, with no note',
    async (role) => {
      as(role);
      install([{ path: RUNS_PATH, handler: () => ({ runs: withStatements() }) }]);
      renderSection();
      expect(await screen.findByTestId('warehouse-view-sql')).toHaveTextContent('View SQL');
      expect(screen.getByTestId('warehouse-failed-view-sql')).toHaveTextContent('View SQL');
      expect(screen.queryByTestId('warehouse-sql-note')).toBeNull();
    },
  );

  it('is not offered when the API sends no statements', async () => {
    as('ANALYST');
    install([
      {
        path: RUNS_PATH,
        handler: () => ({
          runs: withStatements().map((r) => ({ ...r, statements: null })),
        }),
      },
    ]);
    renderSection();
    expect(await screen.findByTestId('warehouse-results')).toBeInTheDocument();
    expect(screen.getByTestId('warehouse-run-failed')).toBeInTheDocument();
    expect(screen.queryByTestId('warehouse-view-sql')).toBeNull();
    expect(screen.queryByTestId('warehouse-failed-view-sql')).toBeNull();
  });
});

describe('the direction each metric was analysed in (#1386)', () => {
  /** A computed proportion metric with the run's stored direction and one treatment change. */
  function directed(lower: boolean | undefined, pct: number, significant = true) {
    const metric = computedMetric();
    const result = metric.result!;
    if (lower === undefined) delete result.lower_is_better;
    else result.lower_is_better = lower;
    result.variants = [
      result.variants[0],
      { ...result.variants[1], relative_improvement_pct: pct, is_significant: significant },
    ];
    return metric;
  }

  async function shown(metric: ReturnType<typeof computedMetric>) {
    install([{ path: RUNS_PATH, handler: () => ({ runs: [run({ results: results({ metrics: [metric] }) })] }) }]);
    const view = renderSection();
    const table = await screen.findByTestId('warehouse-metric');
    const treatment = within(table).getAllByTestId('warehouse-variant-row')[1];
    // Variant, then Units, Converted, Rate, Change: the fourth data cell.
    const change = treatment.querySelectorAll('td')[3];
    return { view, heading: within(table).getByRole('heading'), change };
  }

  it.each([
    [true, -12.4, true, 'Purchased (primary) · lower is better', '-12.40% · better', 'text-green-700'],
    [true, 20, true, 'Purchased (primary) · lower is better', '+20.00% · worse', 'text-red-700'],
    [true, -12.4, false, 'Purchased (primary) · lower is better', '-12.40%', null],
    [false, 20, true, 'Purchased (primary) · higher is better', '+20.00% · better', 'text-green-700'],
    [false, -12.4, true, 'Purchased (primary) · higher is better', '-12.40% · worse', 'text-red-700'],
  ] as const)(
    'lower is better: %s, change %s%%, significant: %s -> "%s", "%s"',
    async (lower, pct, significant, heading, change, colour) => {
      const shownMetric = await shown(directed(lower, pct, significant));
      expect(shownMetric.heading).toHaveTextContent(heading);
      expect(shownMetric.change.textContent).toBe(change);
      if (colour) expect(shownMetric.change).toHaveClass(colour);
      else expect(shownMetric.change.className).not.toMatch(/text-(green|red)-700/);
    },
  );

  it('says nothing about direction for a result stored before the field', async () => {
    const { heading, change } = await shown(directed(undefined, 20));
    expect(heading).toHaveTextContent('Purchased (primary)');
    expect(heading).not.toHaveTextContent('is better');
    expect(change.textContent).toBe('+20.00%');
  });

  it('gives a metric that was not computed no direction', async () => {
    install([
      {
        path: RUNS_PATH,
        handler: () => ({ runs: [run({ results: results({ metrics: [notComputedMetric('no_units')] }) })] }),
      },
    ]);
    renderSection();
    const box = await screen.findByTestId('warehouse-metric-not-computed');
    expect(box).not.toHaveTextContent('is better');
  });

  it('a lower-is-better result has no axe violations', async () => {
    // The component on its own: inside the section its h4 follows the
    // section's h2, which axe's heading-order reports with or without a
    // direction (the levels predate #1386).
    const { container } = render(
      <RunResults
        run={run()}
        results={results({
          metrics: [
            directed(true, -12.4),
            { ...directed(true, 20), metric_source_id: 'src-e', name: 'Errors', is_primary: false },
          ],
        })}
        experimentKey={EXPERIMENT.key}
      />,
    );
    expect(screen.getAllByText(/lower is better/)).toHaveLength(2);
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('names a metric source marked lower is better as such when choosing metrics', async () => {
    install([
      { path: RUNS_PATH, handler: () => ({ runs: [] }) },
      { path: CONNECTIONS_PATH, handler: () => ({ connections: [connection()] }) },
      {
        path: SOURCES_PATH,
        handler: () => ({
          sources: [
            SOURCES[0],
            { ...SOURCES[1], name: 'Checkout errors', lower_is_better: true },
            SOURCES[2],
          ],
        }),
      },
    ]);
    renderSection();
    await openForm();
    tick(/Checkout errors/);
    expect(screen.getByRole('checkbox', { name: /Checkout errors/ })).toHaveAccessibleName(
      'Checkout errors (proportion, lower is better, primary)',
    );
    expect(screen.getByRole('checkbox', { name: /Signed up/ })).toHaveAccessibleName('Signed up (proportion)');
  });
});
