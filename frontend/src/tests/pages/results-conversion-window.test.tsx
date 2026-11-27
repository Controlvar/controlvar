/**
 * The results page with a conversion window (beta, #1934).
 *
 * Counting stops with the experiment, so users assigned near its end never
 * get their full window. The API counts them per variant
 * (`metrics[].variants[].open_window_users`) at the time that applies, and
 * the page says so below the sample ratio check, one line per metric with a
 * window. What is pinned, word for word:
 *  - active (and draft): how many were still inside their window when the
 *    results were computed, at the time `computed_at` gives (never the
 *    browser's clock), with each variant's share;
 *  - paused, or archived with no end: no count, and no "can change";
 *  - completed, or archived with an end: how many had less than the window
 *    before it ended, and that a pause, which is not stored, may have taken
 *    more; shown at 0 too, which a pause longer than the window gives;
 *  - nothing for a metric with no window, or from an answer without the keys;
 *  - the summary card's line for the primary metric, and the table's tag.
 */
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ResultDetailPage from '@/pages/results/[id]';
import { PICKER_LABEL } from '@/components/results/ResultsDashboard/SavedSegment';
import { ApiError, apiFetch } from '@/services/api';
import { ExperimentResultsResponse, MetricResult } from '@/types/results';
import {
  computedAtText,
  WINDOW_PAUSE_SENTENCE,
  WINDOW_STOPPED_SENTENCE,
  windowNotice,
  windowNotices,
} from '@/components/results/ResultsDashboard/ConversionWindowNotice';
import { windowSummaryLine, windowTag } from '@/utils/conversionWindow';
import ANSWER_ACTIVE from '@/tests/fixtures/conversion-window/results-active.json';
import ANSWER_PAUSED from '@/tests/fixtures/conversion-window/results-paused.json';
import ANSWER_ARCHIVED from '@/tests/fixtures/conversion-window/results-archived-no-end.json';
import ANSWER_COMPLETED from '@/tests/fixtures/conversion-window/results-completed.json';
import ANSWER_PAUSED_THEN_COMPLETED from '@/tests/fixtures/conversion-window/results-paused-then-completed.json';
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

function variant(id: string, name: string, isControl: boolean, n: number, conv: number, open: number | null) {
  return {
    variant_id: id,
    variant_name: name,
    is_control: isControl,
    sample_size: n,
    conversions: conv,
    mean: n > 0 ? conv / n : 0,
    std_dev: null,
    confidence_interval: null,
    p_value: isControl ? null : 0.2,
    adjusted_p_value: null,
    is_significant: false,
    effect_size: null,
    effect_size_label: null,
    relative_improvement_pct: isControl ? null : 3,
    power: null,
    statistical_test_used: isControl ? null : 'z_test_proportions',
    open_window_users: open,
  };
}

/** A 7-day window: 1,204 of 9,830 users still inside it (590 of 5,000 and 614 of 4,830). */
function purchase(open: [number | null, number | null] = [590, 614], hours: number | null = 168): MetricResult {
  return {
    metric_id: 'm-1',
    metric_name: 'Purchase',
    metric_type: 'conversion',
    is_primary: true,
    lower_is_better: false,
    analysis: 'proportion',
    conversion_window_hours: hours,
    variants: [
      variant('v-ctrl', 'Control', true, 5000, 500, open[0]),
      variant('v-treat', 'Treatment', false, 4830, 500, open[1]),
    ],
  } as MetricResult;
}

/** A metric with no window: both keys null, as the API sends them. */
function visits(): MetricResult {
  return {
    metric_id: 'm-2',
    metric_name: 'Visit',
    metric_type: 'conversion',
    is_primary: false,
    lower_is_better: false,
    analysis: 'proportion',
    conversion_window_hours: null,
    variants: [
      variant('v-ctrl', 'Control', true, 5000, 900, null),
      variant('v-treat', 'Treatment', false, 4830, 880, null),
    ],
  } as MetricResult;
}

const COMPUTED_AT = '2026-11-24T23:40:12.345678Z';

function results(overrides: Partial<ExperimentResultsResponse> = {}): ExperimentResultsResponse {
  return {
    experiment_id: 'exp-1',
    experiment_name: 'Checkout',
    status: 'active',
    start_date: '2026-11-01T00:00:00',
    end_date: null,
    confidence_level: 0.95,
    correction_method: 'none',
    sample_size_adequate: true,
    computed_at: COMPUTED_AT,
    summary: {
      total_users: 9830,
      total_events: 2780,
      duration_days: 24,
      has_winner: false,
      winning_variant_id: null,
      recommendation: 'CONTINUE_TESTING',
      recommendation_reason: 'Planted reason.',
    },
    metrics: [purchase(), visits()],
    srm: { chi2: 0.3, p_value: 0.58, warning: false, expected: {}, observed: {} },
    bayesian_results: null,
    ...overrides,
  };
}

const ACTIVE_LINE =
  'Purchase: 1,204 of 9,830 users (12%) were still inside their 7 days when these results were computed ' +
  '(24 Nov 2026, 23:40 UTC). They are in the sample size, and count as not converted until they convert. ' +
  'The numbers and the comparison can change, in either direction. Still inside: Control 12%, Treatment 13%.';

const STOPPED_LINE =
  'Purchase: Events sent while the experiment is paused or archived are not counted, so users assigned ' +
  'shortly before it stopped have less than the full window.';

const ENDED_LINE =
  'Purchase: 1,204 of 9,830 users (12%) had less than 7 days before the experiment ended. Events received ' +
  'after it ended are not counted, so this does not change. If it was paused, events sent during the pause ' +
  'were not counted either, so more users may have had less than the full window.';

const SAMPLE_SIZE = {
  required_sample_size_per_variant: 3000,
  current_sample_size_per_variant: 4830,
  is_adequate: true,
  achieved_power: 0.9,
  days_to_significance: null,
  projected_completion_date: null,
  baseline_rate: 0.1,
  mde: 0.05,
  confidence_level: 0.95,
  power_target: 0.8,
};

const SEGMENT_ID = '3f1e2d4c-5b6a-4978-8a9b-0c1d2e3f4a5b';
const SEGMENT_NAME = 'Pilot customers';

function install(body: ExperimentResultsResponse, segmentBody?: Record<string, unknown>) {
  mockedApiFetch.mockImplementation(
    routedApi([
      {
        path: '/api/v1/experiments/exp-1',
        handler: () => ({ id: 'exp-1', correction_method: 'none', confidence_level: 0.95, bayesian_enabled: false }),
      },
      { path: '/api/v1/results/exp-1', handler: () => body },
      {
        path: '/api/v1/segments',
        handler: () => [
          {
            id: SEGMENT_ID,
            name: SEGMENT_NAME,
            kind: 'id_list',
            rules: null,
            status: 'active',
            created_at: '2026-11-01T00:00:00Z',
            updated_at: '2026-11-01T00:00:00Z',
          },
        ],
      },
      { path: `/api/v1/results/exp-1/segments/${SEGMENT_ID}`, handler: () => segmentBody },
      { path: '/api/v1/results/exp-1/daily', handler: () => ({ experiment_id: 'exp-1', metric_id: null, series: [] }) },
      { path: '/api/v1/results/exp-1/sample-size', handler: () => SAMPLE_SIZE },
      {
        path: '/api/v1/results/exp-1/sequential',
        handler: () => {
          throw new ApiError({ status: 404, detail: 'not sequential' });
        },
      },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderLoaded(body: ExperimentResultsResponse) {
  install(body);
  render(<ResultDetailPage />);
  await screen.findByTestId('results-dashboard');
}

const noticeTexts = () => screen.queryAllByTestId('window-notice').map((p) => p.textContent);

beforeEach(() => {
  mockedApiFetch.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
});

describe('the notice, on the page', () => {
  it('active: the count, the time the results were computed, and each variant’s share, below the sample ratio line', async () => {
    await renderLoaded(results());
    expect(noticeTexts()).toEqual([ACTIVE_LINE]);
    const notices = screen.getByTestId('window-notices');
    const srm = screen.getByTestId('srm-check-passed');
    expect(srm.compareDocumentPosition(notices) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Plain text: it qualifies the numbers, as the sample ratio line does.
    for (const p of screen.getAllByTestId('window-notice')) {
      expect(p).not.toHaveAttribute('role');
      expect(p).not.toHaveAttribute('aria-live');
    }
    expect(notices.closest('[role="alert"], [aria-live]')).toBeNull();
  });

  it('paused: no count, and nothing says the numbers can change', async () => {
    const paused = results({ status: 'paused', metrics: [purchase([null, null]), visits()] });
    await renderLoaded(paused);
    expect(noticeTexts()).toEqual([STOPPED_LINE]);
    expect(screen.getByTestId('window-notices').textContent).not.toMatch(/can change/);
  });

  it('completed: who had less than the window before it ended, and the pause sentence', async () => {
    await renderLoaded(results({ status: 'completed', end_date: '2026-11-20T18:00:00' }));
    expect(noticeTexts()).toEqual([ENDED_LINE]);
  });

  it('the summary card names the primary metric’s window, and its table rows carry the tag', async () => {
    await renderLoaded(results());
    expect(screen.getByTestId('summary-window')).toHaveTextContent(windowSummaryLine(168));
    expect(windowSummaryLine(168)).toBe(
      "Conversion window: events in the 7 days (168 hours) from each user's assignment count.",
    );
    const rows = screen
      .getAllByRole('row')
      .filter((r) => r.getAttribute('data-metric-id') !== null);
    const tagged = rows.filter((r) => within(r).queryByTestId('metric-window-tag') !== null);
    expect(tagged.map((r) => r.getAttribute('data-metric-id'))).toEqual(['m-1', 'm-1']);
    for (const r of tagged) expect(within(r).getByTestId('metric-window-tag')).toHaveTextContent(windowTag(168));
    expect(windowTag(168)).toBe('within 7 days');
  });

  it('no window anywhere: no notice, no summary line, no tag', async () => {
    const plain = results({ metrics: [purchase([null, null], null), visits()] });
    await renderLoaded(plain);
    expect(screen.queryByTestId('window-notices')).toBeNull();
    expect(screen.queryByTestId('summary-window')).toBeNull();
    expect(screen.queryByTestId('metric-window-tag')).toBeNull();
    expect(screen.getByTestId('experiment-summary').textContent).not.toMatch(/window/i);
  });

  it('an answer without the keys (an older or cached one): nothing about a window', async () => {
    const strip = (m: MetricResult): MetricResult => {
      const { conversion_window_hours, ...rest } = m;
      return {
        ...rest,
        variants: m.variants.map(({ open_window_users, ...v }) => v),
      };
    };
    await renderLoaded(results({ metrics: [strip(purchase()), strip(visits())] }));
    expect(screen.queryByTestId('window-notices')).toBeNull();
    expect(screen.queryByTestId('summary-window')).toBeNull();
    expect(screen.queryByTestId('metric-window-tag')).toBeNull();
  });
});

// Answers the API gave (`GET /api/v1/results/{id}`), saved as returned: a 7-day
// window on Purchase, 8 users of whom 5 were assigned 10 days before, so 3 were
// still inside it; then the same experiment paused, then completed; another
// paused and archived (no end); and one paused for longer than its 1-day
// window and then completed, which leaves 0 with less than the full window.
describe('a saved segment’s results', () => {
  it('show the members’ own line, not the line for all users', async () => {
    // The segment's members: 4 of the 9,830, 1 of them still inside the window.
    const members = {
      ...results(),
      summary: { ...results().summary, total_users: 4 },
      metrics: [
        {
          ...purchase(),
          variants: [
            variant('v-ctrl', 'Control', true, 2, 1, 0),
            variant('v-treat', 'Treatment', false, 2, 1, 1),
          ],
        },
        visits(),
      ],
      srm: { chi2: 0, p_value: 1, warning: false, expected: {}, observed: {} },
      segment: {
        id: SEGMENT_ID,
        name: SEGMENT_NAME,
        kind: 'id_list',
        status: 'active',
        membership: 'current',
        member_count: 4,
        experiment_users: 9830,
        experiment_srm_warning: false,
      },
      is_exploratory: true,
    };
    install(results(), members);
    render(<ResultDetailPage />);
    await screen.findByTestId('results-dashboard');
    expect(noticeTexts()).toEqual([ACTIVE_LINE]);

    const picker = screen.getByRole('combobox', { name: PICKER_LABEL });
    await userEvent.hover(picker);
    await waitFor(() => expect(within(picker).getByRole('option', { name: SEGMENT_NAME })).toBeInTheDocument());
    await userEvent.selectOptions(picker, SEGMENT_ID);
    await screen.findByTestId('summary-segment-line');

    expect(noticeTexts()).toEqual([
      'Purchase: 1 of 4 users (25%) was still inside their 7 days when these results were computed ' +
        '(24 Nov 2026, 23:40 UTC). They are in the sample size, and count as not converted until they convert. ' +
        'The numbers and the comparison can change, in either direction. Still inside: Control 0%, Treatment 50%.',
    ]);
  });
});

describe('the notice, from answers the API gave', () => {
  const ANSWERS: Record<string, unknown> = {
    'results-active': ANSWER_ACTIVE,
    'results-paused': ANSWER_PAUSED,
    'results-archived-no-end': ANSWER_ARCHIVED,
    'results-completed': ANSWER_COMPLETED,
    'results-paused-then-completed': ANSWER_PAUSED_THEN_COMPLETED,
  };
  const captured = (name: string) => () => ANSWERS[name] as ExperimentResultsResponse;

  const ENDED_SUFFIX =
    'Events received after it ended are not counted, so this does not change. If it was paused, events sent ' +
    'during the pause were not counted either, so more users may have had less than the full window.';

  it.each<[string, string[]]>([
    [
      'results-active',
      [
        'Purchase: 3 of 8 users (38%) were still inside their 7 days when these results were computed ' +
          '(26 Nov 2026, 16:42 UTC). They are in the sample size, and count as not converted until they convert. ' +
          'The numbers and the comparison can change, in either direction. Still inside: Control 25%, Treatment 50%.',
      ],
    ],
    ['results-paused', [STOPPED_LINE]],
    ['results-archived-no-end', [STOPPED_LINE]],
    ['results-completed', [`Purchase: 3 of 8 users (38%) had less than 7 days before the experiment ended. ${ENDED_SUFFIX}`]],
    [
      'results-paused-then-completed',
      [`Purchase in a day: 0 of 6 users (0%) had less than 1 day before the experiment ended. ${ENDED_SUFFIX}`],
    ],
  ])('%s: exactly these lines, and none for the metric with no window', async (name, lines) => {
    const body = captured(name)();
    await renderLoaded(body);
    expect(noticeTexts()).toEqual(lines);
  });

  it('the metric cell’s name reads the window tag as its own words', async () => {
    const refund = {
      ...purchase([10, 20], 36),
      metric_id: 'm-3',
      metric_name: 'Refund',
      is_primary: false,
      lower_is_better: true,
    } as MetricResult;
    await renderLoaded(results({ metrics: [purchase(), visits(), refund] }));
    // jsdom's name computation puts a space between inline elements, which a
    // browser does not: the tags are set apart by margin only, and Chromium's
    // accessibility tree read "Purchaseprimarywithin 7 days". So the text the
    // browser concatenates is pinned too: each tag carries an sr-only space.
    const cells = (name: string) => screen.getAllByRole('cell', { name });
    for (const [name, text] of [
      ['Purchase primary within 7 days', 'Purchase primary within 7 days'],
      ['Refund lower is better within 36 hours', 'Refund lower is better within 36 hours'],
      ['Visit', 'Visit'],
    ] as const) {
      expect(cells(name)).toHaveLength(2);
      for (const cell of cells(name)) expect(cell.textContent).toBe(text);
    }
  });

  it('the active answer: the summary card names the window, and only that metric’s rows are tagged', async () => {
    await renderLoaded(captured('results-active')());
    expect(screen.getByTestId('summary-window')).toHaveTextContent(windowSummaryLine(168));
    const tagged = screen
      .getAllByRole('row')
      .filter((r) => within(r).queryByTestId('metric-window-tag') !== null)
      .map((r) => r.getAttribute('data-metric-id'));
    const purchase = captured('results-active')().metrics.find((m) => m.metric_name === 'Purchase')?.metric_id;
    expect(tagged).toEqual([purchase, purchase]);
  });
});

describe('windowNotice, by status', () => {
  it('reads the time from computed_at, never the browser’s clock', () => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2027-03-01T08:15:00Z'));
    expect(windowNotice(results(), purchase())).toBe(ACTIVE_LINE);
    expect(computedAtText(COMPUTED_AT)).toBe('24 Nov 2026, 23:40 UTC');
    // A time with no zone is UTC, as the API means it.
    expect(computedAtText('2026-11-05T09:05:00')).toBe('5 Nov 2026, 09:05 UTC');
  });

  it('a draft reads as an active experiment', () => {
    expect(windowNotice(results({ status: 'draft' }), purchase())).toBe(ACTIVE_LINE);
  });

  it('a mean metric counts its open users as 0 until they send the event', () => {
    const revenue = { ...purchase(), metric_name: 'Revenue', metric_type: 'revenue', analysis: 'mean' } as MetricResult;
    expect(windowNotice(results(), revenue)).toBe(
      ACTIVE_LINE.replace('Purchase:', 'Revenue:').replace(
        'count as not converted until they convert',
        'count as 0 until they send the event',
      ),
    );
  });

  it('archived with no end, or with an end after the results were computed: the paused sentence', () => {
    expect(windowNotice(results({ status: 'archived', end_date: null }), purchase([null, null]))).toBe(STOPPED_LINE);
    expect(
      windowNotice(results({ status: 'archived', end_date: '2026-11-25T00:00:00' }), purchase([null, null])),
    ).toBe(STOPPED_LINE);
    expect(WINDOW_STOPPED_SENTENCE).not.toMatch(/can change/);
  });

  it('archived with an end at or before the results were computed: the completed line', () => {
    expect(windowNotice(results({ status: 'archived', end_date: '2026-11-24T23:40:12' }), purchase())).toBe(ENDED_LINE);
  });

  it('completed with 0: the line is still shown, with the pause sentence', () => {
    const line = windowNotice(results({ status: 'completed', end_date: '2026-11-20T18:00:00' }), purchase([0, 0], 1));
    expect(line).toBe(
      'Purchase: 0 of 9,830 users (0%) had less than 1 hour before the experiment ended. Events received after ' +
        `it ended are not counted, so this does not change. ${WINDOW_PAUSE_SENTENCE}`,
    );
  });

  it('a metric with no window: nothing, whatever the counts say', () => {
    const counted = { ...purchase([590, 614]), conversion_window_hours: null } as MetricResult;
    for (const status of ['active', 'paused', 'completed', 'archived']) {
      expect(windowNotice(results({ status, end_date: '2026-11-20T18:00:00' }), counted)).toBeNull();
    }
  });

  it('active with 0 inside a window: nothing', () => {
    expect(windowNotice(results(), purchase([0, 0]))).toBeNull();
  });

  it('one user is "1 user", and one inside "was"', () => {
    const one = {
      ...purchase(),
      variants: [variant('v-ctrl', 'Control', true, 1, 0, 1), variant('v-treat', 'Treatment', false, 0, 0, 0)],
    } as MetricResult;
    expect(windowNotice(results(), one)).toBe(
      'Purchase: 1 of 1 user (100%) was still inside their 7 days when these results were computed ' +
        '(24 Nov 2026, 23:40 UTC). They are in the sample size, and count as not converted until they convert. ' +
        'The numbers and the comparison can change, in either direction. Still inside: Control 100%.',
    );
  });

  it('one line per metric with a window, in the results’ order', () => {
    const refund = { ...purchase([10, 20], 36), metric_id: 'm-3', metric_name: 'Refund', is_primary: false } as MetricResult;
    const lines = windowNotices(results({ metrics: [purchase(), visits(), refund] }));
    expect(lines).toHaveLength(2);
    expect(lines[0]).toBe(ACTIVE_LINE);
    expect(lines[1]).toMatch(/^Refund: 30 of 9,830 users \(<1%\) were still inside their 36 hours when/);
  });
  it('a share above 0 that rounds to 0% reads "<1%", one below whole that rounds to 100% reads ">99%"', () => {
    const small = {
      ...purchase(),
      variants: [variant('v-ctrl', 'Control', true, 1000, 0, 1), variant('v-treat', 'Treatment', false, 1000, 0, 0)],
    } as MetricResult;
    const line = windowNotice(results(), small) as string;
    expect(line).toContain('1 of 2,000 users (<1%)');
    expect(line).toContain('Still inside: Control <1%, Treatment 0%.');
    const near = {
      ...purchase(),
      variants: [variant('v-ctrl', 'Control', true, 1000, 0, 999), variant('v-treat', 'Treatment', false, 1000, 0, 1000)],
    } as MetricResult;
    const nearLine = windowNotice(results(), near) as string;
    expect(nearLine).toContain('1,999 of 2,000 users (>99%)');
    expect(nearLine).toContain('Still inside: Control >99%, Treatment 100%.');
  });
});
