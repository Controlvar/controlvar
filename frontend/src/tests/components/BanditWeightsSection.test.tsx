/**
 * "Current traffic weights" (#442 PR D): a bandit experiment's live split.
 * The exact request, the loaded, empty, loading and error states, fixed error
 * copy whatever the server says, nothing for a fixed split, no control but
 * Refresh for anyone but an ADMIN or a superuser, and axe in every state.
 * Setting the weights by hand (#1957) is BanditWeightsOverride.test.tsx.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import {
  BANDIT_EMPTY,
  BANDIT_ERROR,
  BANDIT_FOOTNOTE,
  BanditWeightsSection,
  formatShare,
  isBandit,
} from '@/components/experiments/BanditWeightsSection';
import { ApiError, apiFetch } from '@/services/api';
import { BanditStatus } from '@/types/bandit';
import { Experiment } from '@/types/experiments';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const EXPERIMENT: Experiment = {
  id: 'exp-1',
  name: 'Recommendation algorithm',
  key: 'recommendation-algorithm',
  description: null,
  hypothesis: null,
  experiment_type: 'bandit',
  optimization_type: 'thompson_sampling',
  status: 'active',
  targeting_rules: null,
  tags: null,
  owner_id: 'user-1',
  start_date: '2026-09-01T10:00:00Z',
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [
    { id: 'v-1', name: 'algo_v1', is_control: true, traffic_allocation: 33 },
    { id: 'v-2', name: 'algo_v2', is_control: false, traffic_allocation: 33 },
    { id: 'v-3', name: 'algo_v3', is_control: false, traffic_allocation: 34 },
  ],
  metrics: [],
};

const UPDATED: BanditStatus = {
  experiment_id: 'exp-1',
  algorithm: 'thompson_sampling',
  current_weights: [
    { variant_id: 'v-1', variant_name: 'algo_v1', current_weight: 0.1234, successes: 61, pulls: 1200, conversion_rate: 0.050833 },
    { variant_id: 'v-2', variant_name: 'algo_v2', current_weight: 0.7516, successes: 1890, pulls: 21000, conversion_rate: 0.09 },
    { variant_id: 'v-3', variant_name: 'algo_v3', current_weight: 0.125, successes: 66, pulls: 1100, conversion_rate: 0.06 },
  ],
  total_pulls: 23300,
  recommendation: 'CONVERGING',
  last_updated: '2026-10-04T09:30:00+00:00',
};

/** What the server answers before the first update: an even split it is not using. */
const NEVER_UPDATED: BanditStatus = {
  ...UPDATED,
  current_weights: UPDATED.current_weights.map((w) => ({
    ...w,
    current_weight: 1 / 3,
    successes: 0,
    pulls: 0,
    conversion_rate: 0,
  })),
  total_pulls: 0,
  recommendation: 'EXPLORING',
  last_updated: null,
};

const PLANTED = 'Traceback (most recent call last): sqlalchemy.exc.PLANTED-7f3';

function renderSection(overrides: Partial<Experiment> = {}) {
  return render(<BanditWeightsSection experiment={{ ...EXPERIMENT, ...overrides }} />);
}

async function axeViolations(container: HTMLElement) {
  const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
  return result.violations.map((v) => v.id);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe('the request', () => {
  it('reads GET /api/v1/bandit/{id} once on load, with no options, and again on Refresh', async () => {
    mockedApiFetch.mockResolvedValue(UPDATED);
    renderSection();
    await screen.findByTestId('bandit-weights-table');
    expect(mockedApiFetch.mock.calls).toEqual([['/api/v1/bandit/exp-1']]);

    fireEvent.click(screen.getByTestId('bandit-weights-refresh'));
    await screen.findByTestId('bandit-weights-table');
    expect(mockedApiFetch.mock.calls).toEqual([['/api/v1/bandit/exp-1'], ['/api/v1/bandit/exp-1']]);
  });

  it('never polls: no further request without a click', async () => {
    jest.useFakeTimers();
    try {
      mockedApiFetch.mockResolvedValue(UPDATED);
      renderSection();
      await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledTimes(1));
      jest.advanceTimersByTime(30 * 60 * 1000);
      expect(mockedApiFetch).toHaveBeenCalledTimes(1);
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('a fixed-allocation experiment', () => {
  it.each([
    ['fixed', { optimization_type: 'fixed' }],
    ['not set', { optimization_type: undefined }],
    ['a bandit type with a fixed split', { experiment_type: 'bandit', optimization_type: 'fixed' }],
  ] as const)('renders nothing and sends nothing (%s)', (_label, overrides) => {
    const { container } = renderSection(overrides as Partial<Experiment>);
    expect(container).toBeEmptyDOMElement();
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it('isBandit is true for every algorithm but fixed', () => {
    expect(isBandit({ optimization_type: 'thompson_sampling' })).toBe(true);
    expect(isBandit({ optimization_type: 'ucb1' })).toBe(true);
    expect(isBandit({ optimization_type: 'epsilon_greedy' })).toBe(true);
    expect(isBandit({ optimization_type: 'fixed' })).toBe(false);
    expect(isBandit({})).toBe(false);
  });
});

describe('loaded', () => {
  it('shows each variant with the server values, in its order, and when they were computed', async () => {
    mockedApiFetch.mockResolvedValue(UPDATED);
    renderSection();
    const table = await screen.findByTestId('bandit-weights-table');
    const rows = within(table).getAllByTestId('bandit-weight-row');
    const cells = rows.map((row) =>
      ['name', 'share', 'pulls', 'successes', 'rate'].map(
        (k) => within(row).getByTestId(`bandit-weight-${k}`).textContent,
      ),
    );
    expect(cells).toEqual([
      ['algo_v1', '12.3%', '1,200', '61', '5.1%'],
      ['algo_v2', '75.2%', '21,000', '1,890', '9.0%'],
      ['algo_v3', '12.5%', '1,100', '66', '6.0%'],
    ]);
    const updated = screen.getByTestId('bandit-weights-updated').textContent ?? '';
    expect(updated).toContain(new Date(UPDATED.last_updated as string).toLocaleString());
    expect(updated).toContain(BANDIT_FOOTNOTE);
    expect(screen.queryByTestId('bandit-weights-empty')).toBeNull();
  });

  it('formats a share with one decimal', () => {
    expect(formatShare(0)).toBe('0.0%');
    expect(formatShare(1)).toBe('100.0%');
    expect(formatShare(0.4567)).toBe('45.7%');
  });

  // Only a superuser or an ADMIN may set the weights by hand (#1957, its own
  // tests in BanditWeightsOverride.test.tsx); for anyone else the panel only
  // reads. No user at all (no session yet) is refused too, never offered.
  it('offers no control but Refresh to anyone but an ADMIN or a superuser', async () => {
    const users = [
      undefined,
      null,
      { role: 'DEVELOPER', is_superuser: false },
      { role: 'ANALYST', is_superuser: false },
      { role: 'VIEWER', is_superuser: false },
      { role: null, is_superuser: false },
    ];
    for (const user of users) {
      mockedApiFetch.mockResolvedValue(UPDATED);
      const view = render(<BanditWeightsSection experiment={EXPERIMENT} user={user} />);
      const section = await screen.findByTestId('bandit-weights');
      await screen.findByTestId('bandit-weights-table');
      expect(within(section).getAllByRole('button').map((b) => b.textContent)).toEqual(['Refresh']);
      expect(within(section).queryAllByRole('textbox')).toEqual([]);
      expect(within(section).queryAllByRole('spinbutton')).toEqual([]);
      expect(screen.queryByTestId('bandit-weights-override')).toBeNull();
      view.unmount();
    }
  });
});

describe('empty: before the first update', () => {
  it('shows the fixed copy, not the even split the server fills in', async () => {
    mockedApiFetch.mockResolvedValue(NEVER_UPDATED);
    renderSection();
    expect(await screen.findByTestId('bandit-weights-empty')).toHaveTextContent(BANDIT_EMPTY);
    expect(screen.queryByTestId('bandit-weights-table')).toBeNull();
    expect(screen.getByTestId('bandit-weights').textContent).not.toContain('33.3%');
  });

  it('no sentence claims new users follow current weights before there are any', async () => {
    mockedApiFetch.mockResolvedValue(NEVER_UPDATED);
    renderSection();
    await screen.findByTestId('bandit-weights-empty');
    const text = screen.getByTestId('bandit-weights').textContent ?? '';
    // BANDIT_EMPTY says new users follow the starting allocation; nothing else
    // in the panel may say they follow the (not yet computed) weights.
    expect(text).not.toMatch(/follows? (these|the|current)[^.]{0,20}weights/i);
    expect(text).not.toMatch(/new users follow/i);
  });

  it('once weights exist, the panel says new users follow them', async () => {
    mockedApiFetch.mockResolvedValue(UPDATED);
    renderSection();
    await screen.findByTestId('bandit-weights-table');
    expect(screen.getByTestId('bandit-weights').textContent).toMatch(/new users follow the weights below/i);
  });
});

describe('errors show fixed copy, never the server text', () => {
  const cases: [string, () => unknown][] = [
    ['a 500 with a traceback', () => new ApiError({ status: 500, detail: PLANTED })],
    ['a 403 with a server rule', () => new ApiError({ status: 403, detail: `PLANTED-7f3 you must be the owner` })],
    ['a 404', () => new ApiError({ status: 404, detail: 'Experiment PLANTED-7f3 not found' })],
    ['the API unreachable', () => new ApiError({ status: 0, detail: 'PLANTED-7f3 unreachable' })],
    ['a browser exception', () => new TypeError("Cannot read properties of undefined (reading 'PLANTED-7f3')")],
  ];

  it.each(cases)('%s', async (_label, make) => {
    mockedApiFetch.mockRejectedValue(make());
    renderSection();
    expect(await screen.findByTestId('bandit-weights-error')).toHaveTextContent(BANDIT_ERROR);
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('PLANTED');
    expect(body).not.toContain('Cannot read');
    expect(body).not.toContain('Traceback');
  });

  it('an error is never shown as the empty state or an empty table', async () => {
    mockedApiFetch.mockRejectedValue(new ApiError({ status: 500, detail: PLANTED }));
    renderSection();
    await screen.findByTestId('bandit-weights-error');
    expect(screen.queryByTestId('bandit-weights-empty')).toBeNull();
    expect(screen.queryByTestId('bandit-weights-table')).toBeNull();
  });

  it('Refresh after an error loads the weights', async () => {
    mockedApiFetch.mockRejectedValueOnce(new ApiError({ status: 500, detail: PLANTED }));
    mockedApiFetch.mockResolvedValueOnce(UPDATED);
    renderSection();
    await screen.findByTestId('bandit-weights-error');
    fireEvent.click(screen.getByTestId('bandit-weights-refresh'));
    await screen.findByTestId('bandit-weights-table');
    expect(screen.queryByTestId('bandit-weights-error')).toBeNull();
  });
});

describe('a11y (axe-core in jsdom; colour contrast is not computable here)', () => {
  it('loading', async () => {
    mockedApiFetch.mockReturnValue(new Promise(() => undefined));
    const { container } = renderSection();
    expect(screen.getByTestId('bandit-weights-loading')).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it.each([
    ['loaded', () => mockedApiFetch.mockResolvedValue(UPDATED), 'bandit-weights-table'],
    ['empty', () => mockedApiFetch.mockResolvedValue(NEVER_UPDATED), 'bandit-weights-empty'],
    [
      'error',
      () => mockedApiFetch.mockRejectedValue(new ApiError({ status: 500, detail: PLANTED })),
      'bandit-weights-error',
    ],
  ] as const)('%s', async (_label, arrange, testId) => {
    arrange();
    const { container } = renderSection();
    await screen.findByTestId(testId);
    expect(await axeViolations(container)).toEqual([]);
  });
});
