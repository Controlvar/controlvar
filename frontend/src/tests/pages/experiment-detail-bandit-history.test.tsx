/**
 * "Weight history" on the experiment page (#1958): a bandit's past weight
 * updates as a table, behind a button. Nothing is requested on load; each
 * opening sends one `GET /api/v1/bandit/{id}/history` with no options; the same
 * section for all four roles; cells are the server's weights as one-decimal
 * shares, columns in the experiment's variant order; one fixed sentence for any
 * failure; and the witness `bandit-weight-history` marks the loaded state only.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { BANDIT_FOOTNOTE, formatWhen } from '@/components/experiments/BanditWeightsSection';
import {
  HISTORY_EMPTY,
  HISTORY_ERROR,
  HISTORY_NOT_RECORDED,
  historyCount,
} from '@/components/experiments/BanditWeightHistory';
import { apiFetch } from '@/services/api';
import { BanditHistoryTick, BanditStatus } from '@/types/bandit';
import { Experiment } from '@/types/experiments';
import { shapeErrors } from '../helpers/openapiShape';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments/[id]', query: { id: 'exp-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockUseAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

// Fixture order (zeta, alpha) differs from name order, so a sorted column fails.
const BANDIT: Experiment = {
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
  owner_id: 'someone-else',
  start_date: '2026-09-01T10:00:00Z',
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [
    { id: 'v-1', name: 'zeta', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'alpha', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [],
};

const STATUS: BanditStatus = {
  experiment_id: 'exp-1',
  algorithm: 'thompson_sampling',
  current_weights: [
    { variant_id: 'v-1', variant_name: 'zeta', current_weight: 0.2, successes: 10, pulls: 200, conversion_rate: 0.05 },
    { variant_id: 'v-2', variant_name: 'alpha', current_weight: 0.8, successes: 72, pulls: 800, conversion_rate: 0.09 },
  ],
  total_pulls: 1000,
  recommendation: 'DEPLOYING_alpha',
  last_updated: '2026-10-04T09:30:00+00:00',
};

const TICKS: BanditHistoryTick[] = [
  { tick_at: '2026-10-04T09:30:00Z', weights: { 'v-1': 0.2, 'v-2': 0.8 } },
  { tick_at: '2026-10-04T09:25:00Z', weights: { 'v-1': 0.3333, 'v-2': 0.6667 } },
  { tick_at: '2026-10-04T09:20:00Z', weights: { 'v-2': 1 } },
];

function install(experiment: Experiment, history: () => unknown = () => TICKS) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => experiment },
      { path: '/api/v1/bandit/exp-1', handler: () => STATUS },
      { path: '/api/v1/bandit/exp-1/history', handler: history },
    ]) as unknown as typeof apiFetch,
  );
}

/** A resolved, non-superuser user: never `null`, which would read as "may change". */
function signIn(role: string) {
  mockUseAuth.mockReturnValue({
    user: { id: 'user-1', email: `${role.toLowerCase()}@demo.com`, username: role.toLowerCase(), role, is_superuser: false },
    status: 'authenticated',
  });
}

const historyCalls = () =>
  mockedApiFetch.mock.calls.filter(([path]) => String(path).endsWith('/history'));

async function section() {
  return screen.findByTestId('bandit-weight-history-section');
}

async function openIt() {
  const box = await section();
  fireEvent.click(within(box).getByRole('button', { name: 'Show weight history' }));
  return box;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn('ADMIN');
});

describe('the footnote points at the history', () => {
  it('no longer says past weights are absent', () => {
    expect(BANDIT_FOOTNOTE).toBe(
      'Weights are recomputed on a schedule, and this panel does not refresh by itself. ' +
        'Past weights are under Weight history.',
    );
  });
});

describe('mounting and requests', () => {
  it('sends no history request on load; one per opening, with no options', async () => {
    install(BANDIT);
    render(<ExperimentDetailPage />);
    const box = await section();
    await screen.findByTestId('bandit-weights-table');
    expect(historyCalls()).toEqual([]);

    fireEvent.click(within(box).getByRole('button', { name: 'Show weight history' }));
    await within(box).findByTestId('bandit-weight-history-table');
    expect(historyCalls()).toHaveLength(1);
    expect(historyCalls()[0]).toEqual(['/api/v1/bandit/exp-1/history']);

    fireEvent.click(within(box).getByRole('button', { name: 'Hide weight history' }));
    expect(within(box).queryByTestId('bandit-weight-history-table')).toBeNull();
    expect(within(box).getByRole('button', { name: 'Show weight history' })).toHaveAttribute('aria-expanded', 'false');
    expect(historyCalls()).toHaveLength(1);

    fireEvent.click(within(box).getByRole('button', { name: 'Show weight history' }));
    await within(box).findByTestId('bandit-weight-history-table');
    expect(historyCalls()).toHaveLength(2);
  });

  it('sits below the weights panel, outside it, and only reads', async () => {
    install(BANDIT);
    render(<ExperimentDetailPage />);
    const box = await openIt();
    await within(box).findByTestId('bandit-weight-history-table');
    const panel = screen.getByTestId('bandit-weights');
    expect(panel.contains(box)).toBe(false);
    expect(within(panel).getAllByRole('button').map((b) => b.textContent)).toEqual(['Refresh']);
    expect(panel.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      mockedApiFetch.mock.calls.every(([, options]) => (options?.method ?? 'GET').toUpperCase() === 'GET'),
    ).toBe(true);
  });

  it('a fixed-allocation experiment has no section and never asks', async () => {
    install({ ...BANDIT, experiment_type: 'a_b', optimization_type: 'fixed' });
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
    expect(screen.queryByTestId('bandit-weight-history-section')).toBeNull();
    expect(screen.queryByText('Weight history')).toBeNull();
    expect(historyCalls()).toEqual([]);
  });
});

describe('every role reads it', () => {
  it.each(['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'])('%s sees the section and the table', async (role) => {
    signIn(role);
    install(BANDIT);
    render(<ExperimentDetailPage />);
    const box = await openIt();
    const table = await within(box).findByTestId('bandit-weight-history-table');
    expect(within(table).getAllByTestId('bandit-weight-history-row')).toHaveLength(3);
  });
});

describe('the table', () => {
  it('shows the server weights as shares, columns in the variant order', async () => {
    install(BANDIT);
    render(<ExperimentDetailPage />);
    const box = await openIt();
    const table = await within(box).findByTestId('bandit-weight-history-table');
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Updated', 'zeta', 'alpha']);
    const rows = within(table).getAllByTestId('bandit-weight-history-row');
    const cells = rows.map((row) => [
      within(row).getByRole('rowheader').textContent,
      ...within(row).getAllByRole('cell').map((c) => c.textContent),
    ]);
    expect(cells).toEqual([
      [formatWhen(TICKS[0].tick_at), '20.0%', '80.0%'],
      [formatWhen(TICKS[1].tick_at), '33.3%', '66.7%'],
      [formatWhen(TICKS[2].tick_at), HISTORY_NOT_RECORDED, '100.0%'],
    ]);
    expect(within(box).getByRole('table', { name: 'Weight updates, newest first' })).toBe(table);
    expect(within(box).getByTestId('bandit-weight-history-count')).toHaveTextContent(historyCount(3));
  });

  it('says how many it shows, and that older ones are not shown at 200', () => {
    expect(historyCount(1)).toBe('Showing the most recent update.');
    expect(historyCount(3)).toBe('Showing the 3 most recent updates.');
    expect(historyCount(200)).toBe('Showing the 200 most recent updates; older ones are not shown here.');
  });

  it('the fixture items are what the API sends', () => {
    for (const tick of TICKS) expect(shapeErrors('BanditHistoryTick', tick)).toEqual([]);
    expect(shapeErrors('BanditHistoryTick', { tick_at: 'x', weights: [0.5] })).not.toEqual([]);
    expect(shapeErrors('BanditHistoryTick', { weights: { a: 1 } })).not.toEqual([]);
  });
});

describe('states and the witness', () => {
  it('loading: a status line, and no witness', async () => {
    install(BANDIT, () => new Promise(() => undefined));
    render(<ExperimentDetailPage />);
    const box = await openIt();
    expect(await within(box).findByRole('status')).toHaveTextContent('Loading the weight history…');
    expect(screen.queryByTestId('bandit-weight-history')).toBeNull();
  });

  it('rows: the witness is present', async () => {
    install(BANDIT);
    render(<ExperimentDetailPage />);
    await openIt();
    expect(await screen.findByTestId('bandit-weight-history')).toBeInTheDocument();
  });

  it('no update yet: the empty line, with the witness', async () => {
    install(BANDIT, () => []);
    render(<ExperimentDetailPage />);
    await openIt();
    expect(await screen.findByTestId('bandit-weight-history')).toHaveTextContent(HISTORY_EMPTY);
    expect(screen.queryByTestId('bandit-weight-history-table')).toBeNull();
  });

  it('a failure shows one fixed sentence, never the server text, and no witness; Try again asks again', async () => {
    let fail = true;
    install(BANDIT, () => {
      if (fail) throw apiError(500, 'Traceback ... PLANTED-1958');
      return TICKS;
    });
    render(<ExperimentDetailPage />);
    const box = await openIt();
    expect(await within(box).findByRole('alert')).toHaveTextContent(HISTORY_ERROR);
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(screen.queryByTestId('bandit-weight-history')).toBeNull();
    // The rest of the page stays.
    expect(screen.getByTestId('bandit-weights-table')).toBeInTheDocument();
    expect(screen.queryByTestId('experiment-error')).toBeNull();

    fail = false;
    fireEvent.click(within(box).getByRole('button', { name: 'Try again' }));
    await within(box).findByTestId('bandit-weight-history-table');
    expect(historyCalls()).toHaveLength(2);
    expect(document.activeElement).toBe(within(box).getByRole('heading', { name: 'Weight history' }));
  });

  it.each([403, 404, 422])('a %s reads the same as any other failure', async (status) => {
    install(BANDIT, () => {
      throw apiError(status, 'PLANTED-detail');
    });
    render(<ExperimentDetailPage />);
    const box = await openIt();
    expect(await within(box).findByRole('alert')).toHaveTextContent(HISTORY_ERROR);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('closing while it loads drops the answer', async () => {
    let release: (v: unknown) => void = () => undefined;
    install(BANDIT, () => new Promise((resolve) => { release = resolve; }));
    render(<ExperimentDetailPage />);
    const box = await openIt();
    await within(box).findByRole('status');
    fireEvent.click(within(box).getByRole('button', { name: 'Hide weight history' }));
    release(TICKS);
    await waitFor(() => expect(historyCalls()).toHaveLength(1));
    expect(screen.queryByTestId('bandit-weight-history')).toBeNull();
  });
});
