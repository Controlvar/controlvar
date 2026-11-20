/**
 * The bandit weights panel on the experiment page (#442 PR D): mounted for a
 * bandit, absent for a fixed split, the same weights for all four roles
 * (reading them is allowed to every signed-in user), and a failure to read the
 * weights leaves the rest of the page intact. "Set weights by hand" (#1957)
 * is offered to an ADMIN or a superuser only, never through a custom-role
 * grant, and every other signed-in role gets the one-sentence role note.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { BANDIT_ERROR } from '@/components/experiments/BanditWeightsSection';
import { apiFetch } from '@/services/api';
import { BanditStatus } from '@/types/bandit';
import { Experiment } from '@/types/experiments';
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

// The page's effective map (#891): role lists only unless a test grants more.
const mockEffective = jest.fn();
jest.mock('@/contexts/EffectivePermissionsContext', () => ({
  ...jest.requireActual('@/contexts/EffectivePermissionsContext'),
  useEffectivePermissions: () => mockEffective(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

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
    { id: 'v-1', name: 'algo_v1', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'algo_v2', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [],
};

const STATUS: BanditStatus = {
  experiment_id: 'exp-1',
  algorithm: 'thompson_sampling',
  current_weights: [
    { variant_id: 'v-1', variant_name: 'algo_v1', current_weight: 0.2, successes: 10, pulls: 200, conversion_rate: 0.05 },
    { variant_id: 'v-2', variant_name: 'algo_v2', current_weight: 0.8, successes: 72, pulls: 800, conversion_rate: 0.09 },
  ],
  total_pulls: 1000,
  recommendation: 'DEPLOYING_algo_v2',
  last_updated: '2026-10-04T09:30:00+00:00',
};

function install(experiment: Experiment, bandit: () => unknown = () => STATUS) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => experiment },
      { path: '/api/v1/bandit/exp-1', handler: bandit },
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

const banditCalls = () =>
  mockedApiFetch.mock.calls.filter(([path]) => String(path).startsWith('/api/v1/bandit/'));

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockEffective.mockReturnValue({ effective: undefined, loading: false });
  signIn('ADMIN');
});

describe('mounting', () => {
  it('a bandit experiment shows the panel below the page', async () => {
    install(BANDIT);
    render(<ExperimentDetailPage />);
    const panel = await screen.findByTestId('bandit-weights');
    await within(panel).findByTestId('bandit-weights-table');
    expect(within(panel).getByRole('heading', { name: 'Current traffic weights' })).toBeInTheDocument();
  });

  it('a fixed-allocation experiment has no panel and never asks for weights', async () => {
    install({ ...BANDIT, experiment_type: 'a_b', optimization_type: 'fixed' });
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
    expect(screen.queryByTestId('bandit-weights')).toBeNull();
    expect(banditCalls()).toEqual([]);
  });

  it('a failure to read the weights leaves the rest of the page in place', async () => {
    install(BANDIT, () => {
      throw apiError(500, 'PLANTED-7f3 server text');
    });
    render(<ExperimentDetailPage />);
    expect(await screen.findByTestId('bandit-weights-error')).toHaveTextContent(BANDIT_ERROR);
    expect(screen.getByTestId('experiment-name')).toHaveTextContent('Recommendation algorithm');
    expect(screen.getByTestId('variants-table')).toBeInTheDocument();
    expect(screen.getByTestId('targeting-section')).toBeInTheDocument();
    expect(screen.queryByTestId('experiment-error')).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

// #1957: every role reads the same weights, and only an ADMIN or a superuser
// is offered "Set weights by hand" (PUT /api/v1/bandit/{id}/weights admits
// exactly those two). Each role is a NON-superuser here: a superuser fixture
// would pass every row whatever the role rule said.
describe('every role reads the same weights; only an ADMIN or a superuser may set them by hand', () => {
  const roles = ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'] as const;

  async function panelFor(role: string) {
    signIn(role);
    install(BANDIT);
    const view = render(<ExperimentDetailPage />);
    const panel = await screen.findByTestId('bandit-weights');
    await within(panel).findByTestId('bandit-weights-table');
    const snapshot = {
      weights: within(panel).getByTestId('bandit-weights-table').textContent,
      buttons: within(panel).getAllByRole('button').map((b) => b.textContent),
      note: within(panel).queryByTestId('bandit-weights-role-note')?.textContent ?? null,
    };
    view.unmount();
    return snapshot;
  }

  const onlyReads = () =>
    banditCalls().every(([, options]) => (options?.method ?? 'GET').toUpperCase() === 'GET');

  it.each(roles)('%s, not a superuser', async (role) => {
    const { buttons, note, weights } = await panelFor(role);
    expect(weights).toContain('80.0%');
    if (role === 'ADMIN') {
      expect(buttons).toEqual(['Refresh', 'Set weights by hand']);
      expect(note).toBeNull();
    } else {
      expect(buttons).toEqual(['Refresh']);
      expect(note).toBe("Setting a bandit's weights by hand needs the ADMIN role.");
    }
    // Offering is not doing: loading the page sends no update or override.
    expect(onlyReads()).toBe(true);
  });

  it('a superuser is offered it whatever the role', async () => {
    for (const role of ['VIEWER', 'ANALYST', 'DEVELOPER']) {
      mockUseAuth.mockReturnValue({
        user: { id: 'user-1', email: 'root@demo.com', username: 'root', role, is_superuser: true },
        status: 'authenticated',
      });
      install(BANDIT);
      const view = render(<ExperimentDetailPage />);
      const panel = await screen.findByTestId('bandit-weights');
      await within(panel).findByTestId('bandit-weights-table');
      expect(within(panel).getAllByRole('button').map((b) => b.textContent)).toEqual(['Refresh', 'Set weights by hand']);
      expect(within(panel).queryByTestId('bandit-weights-role-note')).toBeNull();
      view.unmount();
    }
  });

  it('no user yet (no session): neither the control nor the note', async () => {
    mockUseAuth.mockReturnValue({ user: null, status: 'loading' });
    install(BANDIT);
    render(<ExperimentDetailPage />);
    const panel = await screen.findByTestId('bandit-weights');
    await within(panel).findByTestId('bandit-weights-table');
    expect(within(panel).getAllByRole('button').map((b) => b.textContent)).toEqual(['Refresh']);
    expect(within(panel).queryByTestId('bandit-weights-role-note')).toBeNull();
  });

  // The route checks the role itself, not a permission, so no custom role or
  // direct grant can admit anyone: a full experiment grant changes nothing.
  it.each(['DEVELOPER', 'ANALYST', 'VIEWER'])('a custom-role grant does not unlock it (%s)', async (role) => {
    mockEffective.mockReturnValue({
      effective: { experiment: ['create', 'read', 'update', 'delete', 'list'], bandit: ['update'] },
      loading: false,
    });
    const { buttons, note } = await panelFor(role);
    expect(buttons).toEqual(['Refresh']);
    expect(note).toBe("Setting a bandit's weights by hand needs the ADMIN role.");
  });

  it('every role reads the same weights', async () => {
    const seen = [];
    for (const role of roles) seen.push(await panelFor(role));
    expect(seen.map((s) => s.weights)).toEqual(Array(roles.length).fill(seen[0].weights));
  });

  it.each(['completed', 'archived'] as const)('on a %s experiment nobody is offered it, and no note is shown', async (status) => {
    for (const role of roles) {
      signIn(role);
      install({ ...BANDIT, status });
      const view = render(<ExperimentDetailPage />);
      const panel = await screen.findByTestId('bandit-weights');
      await within(panel).findByTestId('bandit-weights-table');
      expect(within(panel).getAllByRole('button').map((b) => b.textContent)).toEqual(['Refresh']);
      expect(within(panel).queryByTestId('bandit-weights-role-note')).toBeNull();
      view.unmount();
    }
  });
});

describe('Set weights by hand on the page', () => {
  it('an ADMIN opens the form and saves; the panel shows the answer', async () => {
    const saved: BanditStatus = {
      ...STATUS,
      current_weights: STATUS.current_weights.map((w, i) => ({
        ...w,
        current_weight: i === 0 ? 0.35 : 0.65,
        pulls: 0,
        successes: 0,
        conversion_rate: 0,
      })),
      total_pulls: 0,
      last_updated: '2026-10-05T12:00:00+00:00',
    };
    const puts: unknown[] = [];
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/experiments/exp-1', handler: () => BANDIT },
        { path: '/api/v1/bandit/exp-1', handler: () => STATUS },
        {
          method: 'PUT',
          path: '/api/v1/bandit/exp-1/weights',
          handler: (_path, options) => {
            puts.push(options.json);
            return saved;
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<ExperimentDetailPage />);
    const panel = await screen.findByTestId('bandit-weights');
    await within(panel).findByTestId('bandit-weights-table');
    fireEvent.click(within(panel).getByRole('button', { name: 'Set weights by hand' }));
    const form = screen.getByTestId('bandit-weights-override');
    expect(within(form).getByTestId('bandit-override-status-copy')).toHaveTextContent(
      'The bandit replaces these weights at its next update, within minutes (every 5 minutes by default).',
    );
    const inputs = within(form).getAllByRole('spinbutton') as HTMLInputElement[];
    // After an update the form opens with the current weights: 20% and 80%.
    expect(inputs.map((i) => i.value)).toEqual(['20', '80']);
    fireEvent.change(inputs[0], { target: { value: '35' } });
    fireEvent.change(inputs[1], { target: { value: '65' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Save weights' }));
    await waitFor(() => expect(screen.queryByTestId('bandit-weights-override')).toBeNull());
    expect(puts).toEqual([{ weights: { 'v-1': 0.35, 'v-2': 0.65 } }]);
    expect(within(panel).getAllByTestId('bandit-weight-share').map((c) => c.textContent)).toEqual(['35.0%', '65.0%']);
    expect(within(panel).getByTestId('bandit-weights-saved')).toHaveTextContent('Weights saved.');
    // The rest of the page is untouched by the save.
    expect(screen.getByTestId('experiment-name')).toHaveTextContent('Recommendation algorithm');
  });
});
