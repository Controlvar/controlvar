import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import ExperimentDetailPage, { ACTIONS_BY_STATUS, keepOwnerName } from '@/pages/experiments/[id]';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { apiFetch } from '@/services/api';
import { docsUrl } from '@/services/docs';
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

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const BASE_EXPERIMENT: Experiment = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: 'Blue vs green CTA',
  hypothesis: 'Blue converts better',
  experiment_type: 'a_b',
  status: 'draft',
  targeting_rules: null,
  tags: null,
  owner_id: 'user-1',
  start_date: null,
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [
    { id: 'v-1', name: 'Control', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'Blue CTA', is_control: false, traffic_allocation: 50, description: 'Blue button' },
  ],
  metrics: [
    { id: 'm-1', name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true },
    { id: 'm-2', name: 'Revenue', event_name: 'purchase', metric_type: 'revenue', is_primary: false },
  ],
};

function experiment(overrides: Partial<Experiment> = {}): Experiment {
  return { ...BASE_EXPERIMENT, ...overrides };
}

function install(current: Experiment) {
  let state = current;
  const transition = (status: Experiment['status']) => () => {
    state = { ...state, status };
    return state;
  };
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => state },
      { method: 'POST', path: '/api/v1/experiments/exp-1/start', handler: transition('active') },
      { method: 'POST', path: '/api/v1/experiments/exp-1/pause', handler: transition('paused') },
      { method: 'POST', path: '/api/v1/experiments/exp-1/complete', handler: transition('completed') },
      { method: 'POST', path: '/api/v1/experiments/exp-1/archive', handler: transition('archived') },
    ]) as unknown as typeof apiFetch,
  );
}

const calledWith = (method: string, path: string) =>
  mockedApiFetch.mock.calls.some(
    ([p, o]) => p === path && ((o?.method ?? 'GET').toUpperCase() === method),
  );

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockUseAuth.mockReturnValue({
    user: { id: 'user-1', email: 'admin@demo.com', username: 'admin', role: 'ADMIN' },
    status: 'authenticated',
  });
});

describe('ACTIONS_BY_STATUS', () => {
  it('mirrors the backend lifecycle guards', () => {
    expect(ACTIONS_BY_STATUS.draft).toEqual(['start']);
    expect(ACTIONS_BY_STATUS.active).toEqual(['pause', 'complete']);
    expect(ACTIONS_BY_STATUS.paused).toEqual(['start', 'complete']);
    expect(ACTIONS_BY_STATUS.completed).toEqual(['archive']);
    expect(ACTIONS_BY_STATUS.archived).toEqual([]);
  });
});

describe('ExperimentDetailPage — rendering', () => {
  it('shows the header, variants, metrics and owner', async () => {
    install(experiment());
    render(<ExperimentDetailPage />);

    expect(screen.getByTestId('experiment-loading')).toBeInTheDocument();
    expect(await screen.findByTestId('experiment-detail')).toBeInTheDocument();

    expect(screen.getByTestId('experiment-name')).toHaveTextContent('Checkout button colour');
    expect(screen.getByTestId('experiment-key')).toHaveTextContent('checkout-button-colour');
    expect(screen.getByTestId('experiment-status')).toHaveTextContent('Draft');
    expect(screen.getByTestId('experiment-type')).toHaveTextContent('A/B Test');
    expect(screen.getByTestId('experiment-owner')).toHaveTextContent('You (admin@demo.com)');
    expect(screen.getByTestId('experiment-description')).toHaveTextContent('Blue vs green CTA');
    expect(screen.getByTestId('experiment-hypothesis')).toHaveTextContent('Blue converts better');

    const rows = within(screen.getByTestId('variants-table')).getAllByTestId('variant-row');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Control');
    expect(rows[0]).toHaveTextContent('50%');
    expect(rows[1]).toHaveTextContent('Blue CTA');
    expect(rows[1]).toHaveTextContent('Treatment');

    const metrics = within(screen.getByTestId('metrics-list')).getAllByTestId('metric-row');
    expect(metrics).toHaveLength(2);
    expect(metrics[0]).toHaveTextContent('Purchase');
    expect(metrics[0]).toHaveTextContent('Primary');
    expect(metrics[0]).toHaveTextContent('purchase');
    expect(metrics[1]).not.toHaveTextContent('Primary');

    expect(screen.getByTestId('sdk-hint')).toHaveTextContent('"experiment_key": "checkout-button-colour"');
  });

  it('shows a truncated owner id for experiments owned by someone else', async () => {
    install(experiment({ owner_id: '0f9e8d7c-6b5a-4c3d-2e1f-0a9b8c7d6e5f' }));
    render(<ExperimentDetailPage />);
    expect(await screen.findByTestId('experiment-owner')).toHaveTextContent('0f9e8d7c…');
  });

  it('shows "No owner" when the creator\'s account was removed (owner_id null)', async () => {
    // The API answers `owner_id: null` for such an experiment. The page
    // prints a fixed text rather than a short id, and it is nobody's, not
    // even the signed-in user's.
    install(experiment({ owner_id: null }));
    render(<ExperimentDetailPage />);
    const owner = await screen.findByTestId('experiment-owner');
    expect(owner).toHaveTextContent('No owner');
    expect(owner).not.toHaveTextContent('You (');
    expect(owner).not.toHaveAttribute('title');
  });

  it('renders an empty metrics hint when there are none', async () => {
    install(experiment({ metrics: [] }));
    render(<ExperimentDetailPage />);
    expect(await screen.findByTestId('metrics-empty')).toBeInTheDocument();
  });

  it('disables "View results" for drafts and links it otherwise', async () => {
    install(experiment({ status: 'draft' }));
    const { unmount } = render(<ExperimentDetailPage />);
    expect(await screen.findByTestId('view-results-disabled')).toBeInTheDocument();
    expect(screen.queryByTestId('view-results')).not.toBeInTheDocument();
    unmount();

    install(experiment({ status: 'active' }));
    render(<ExperimentDetailPage />);
    const link = await screen.findByTestId('view-results');
    expect(link).toHaveAttribute('href', '/results/exp-1');
  });
});

describe("ExperimentDetailPage — the owner's name (#921)", () => {
  // Only GET /experiments/{id} names the owner. As on the server, the
  // lifecycle routes and the edit and targeting saves here answer with an
  // experiment that has no `owner_name` key, so the page has to keep the
  // name it read.
  const OWNER_ID = '0f9e8d7c-6b5a-4c3d-2e1f-0a9b8c7d6e5f';
  const BUILDER_RULES = {
    logical_operator: 'AND',
    groups: [
      {
        logical_operator: 'AND',
        conditions: [{ attribute: 'user.country', operator: 'in', value: ['US', 'CA'] }],
      },
    ],
  };

  function withoutName(current: Experiment): Experiment {
    const copy = { ...current };
    delete copy.owner_name;
    return copy;
  }

  /** `afterStart` is what else POST /start changes in the experiment. */
  function installAsServer(current: Experiment, afterStart: Partial<Experiment> = {}) {
    let state = current;
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/experiments/exp-1', handler: () => state },
        {
          method: 'POST',
          path: '/api/v1/experiments/exp-1/start',
          handler: () => {
            state = { ...state, status: 'active', ...afterStart };
            return withoutName(state);
          },
        },
        {
          method: 'PUT',
          path: '/api/v1/experiments/exp-1',
          handler: (_path, options) => {
            state = { ...state, ...(options.json as Partial<Experiment>) };
            return withoutName(state);
          },
        },
      ]) as unknown as typeof apiFetch,
    );
  }

  async function renderNamed(
    overrides: Partial<Experiment> = {},
    afterStart: Partial<Experiment> = {},
  ) {
    installAsServer(
      experiment({ owner_id: OWNER_ID, owner_name: 'Jane Doe', ...overrides }),
      afterStart,
    );
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
    return screen.getByTestId('experiment-owner');
  }

  it('shows the name to a reader who is not the owner, with the id on hover', async () => {
    const owner = await renderNamed();
    expect(owner).toHaveTextContent(/^Jane Doe$/);
    expect(owner).toHaveAttribute('title', OWNER_ID);
  });

  it.each([null, ''])('shows the short id when the name is %p', async (name) => {
    const owner = await renderNamed({ owner_name: name });
    expect(owner).toHaveTextContent(/^0f9e8d7c…$/);
    expect(owner).toHaveAttribute('title', OWNER_ID);
  });

  it('shows "No owner" when there is no owner', async () => {
    const owner = await renderNamed({ owner_id: null, owner_name: null });
    expect(owner).toHaveTextContent(/^No owner$/);
    expect(owner).not.toHaveAttribute('title');
  });

  it('still says "You (…)" to the owner', async () => {
    const owner = await renderNamed({ owner_id: 'user-1' });
    expect(owner).toHaveTextContent(/^You \(admin@demo\.com\)$/);
  });

  it('keeps the name after Start', async () => {
    await renderNamed();
    fireEvent.click(screen.getByTestId('action-start'));
    await waitFor(() => expect(screen.getByTestId('experiment-status')).toHaveTextContent('Active'));
    expect(calledWith('POST', '/api/v1/experiments/exp-1/start')).toBe(true);
    expect(screen.getByTestId('experiment-owner')).toHaveTextContent(/^Jane Doe$/);
  });

  it('shows "No owner" when a change answers that the owner\'s account was removed', async () => {
    // The answer has `owner_id: null` and, like every change's, no
    // `owner_name`: the name read earlier belongs to nobody now.
    await renderNamed({}, { owner_id: null });
    fireEvent.click(screen.getByTestId('action-start'));
    await waitFor(() => expect(screen.getByTestId('experiment-status')).toHaveTextContent('Active'));
    const owner = screen.getByTestId('experiment-owner');
    expect(owner).toHaveTextContent(/^No owner$/);
    expect(owner).not.toHaveAttribute('title');
  });

  it('keeps the name after Edit details is saved', async () => {
    await renderNamed();
    fireEvent.click(screen.getByTestId('experiment-edit-details'));
    fireEvent.change(screen.getByTestId('manage-edit-name'), { target: { value: 'Renamed' } });
    fireEvent.click(screen.getByTestId('manage-edit-save'));
    await screen.findByTestId('manage-saved');
    expect(screen.getByTestId('experiment-name')).toHaveTextContent('Renamed');
    expect(screen.getByTestId('experiment-owner')).toHaveTextContent(/^Jane Doe$/);
  });

  it('keeps the name after the targeting rules are saved', async () => {
    await renderNamed({ targeting_rules: BUILDER_RULES });
    const section = screen.getByTestId('targeting-section');
    fireEvent.click(within(section).getByRole('button', { name: 'Edit' }));
    fireEvent.click(within(section).getByRole('button', { name: 'Save rules' }));
    await within(section).findByText('Saved. The new rules apply when the experiment starts.');
    expect(calledWith('PUT', '/api/v1/experiments/exp-1')).toBe(true);
    expect(screen.getByTestId('experiment-owner')).toHaveTextContent(/^Jane Doe$/);
  });
});

describe('keepOwnerName (#921)', () => {
  const OWNER_ID = '0f9e8d7c-6b5a-4c3d-2e1f-0a9b8c7d6e5f';
  const read = experiment({ owner_id: OWNER_ID, owner_name: 'Jane Doe' });
  const answer = (overrides: Partial<Experiment> = {}): Experiment => {
    const copy: Experiment = { ...read, status: 'active', ...overrides };
    if (!('owner_name' in overrides)) delete copy.owner_name;
    return copy;
  };

  it.each([
    ['a name', 'Janet Doe'],
    ['null', null],
  ])('an answer that carries owner_name (%s) wins', (_label, name) => {
    expect(keepOwnerName(read, answer({ owner_name: name })).owner_name).toBe(name);
  });

  it('an answer without owner_name keeps the name when the owner is the same', () => {
    const merged = keepOwnerName(read, answer());
    expect(merged.owner_name).toBe('Jane Doe');
    expect(merged.status).toBe('active');
  });

  it.each([
    ['removed (null)', null],
    ['someone else', '11111111-2222-4333-8444-555555555555'],
  ])('an answer whose owner changed to %s does not keep the name', (_label, ownerId) => {
    const merged = keepOwnerName(read, answer({ owner_id: ownerId }));
    expect(merged.owner_id).toBe(ownerId);
    expect(merged.owner_name).toBeNull();
  });

  it('with nothing read before, an answer without owner_name has none', () => {
    expect(keepOwnerName(null, answer()).owner_name).toBeNull();
  });
});

describe('ExperimentDetailPage — lifecycle actions', () => {
  it('draft → Start calls POST /start and refreshes the status pill', async () => {
    install(experiment({ status: 'draft' }));
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');

    const actions = screen.getByTestId('experiment-actions');
    expect(within(actions).getByTestId('action-start')).toBeInTheDocument();
    expect(within(actions).queryByTestId('action-pause')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('action-start'));
    await waitFor(() => expect(screen.getByTestId('experiment-status')).toHaveTextContent('Active'));
    expect(calledWith('POST', '/api/v1/experiments/exp-1/start')).toBe(true);

    // Now active: Pause and Complete are offered, Start is gone.
    expect(screen.getByTestId('action-pause')).toBeInTheDocument();
    expect(screen.getByTestId('action-complete')).toBeInTheDocument();
    expect(screen.queryByTestId('action-start')).not.toBeInTheDocument();
    expect(screen.getByTestId('view-results')).toHaveAttribute('href', '/results/exp-1');
  });

  it('active → Pause calls POST /pause; paused offers Start + Complete', async () => {
    install(experiment({ status: 'active' }));
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');

    fireEvent.click(screen.getByTestId('action-pause'));
    await waitFor(() => expect(screen.getByTestId('experiment-status')).toHaveTextContent('Paused'));
    expect(calledWith('POST', '/api/v1/experiments/exp-1/pause')).toBe(true);
    expect(screen.getByTestId('action-start')).toBeInTheDocument();
    expect(screen.getByTestId('action-complete')).toBeInTheDocument();
  });

  it('Complete asks for confirmation, then calls POST /complete', async () => {
    install(experiment({ status: 'active' }));
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');

    fireEvent.click(screen.getByTestId('action-complete'));
    expect(screen.getByTestId('confirm-action')).toBeInTheDocument();
    expect(calledWith('POST', '/api/v1/experiments/exp-1/complete')).toBe(false);

    // Cancel keeps the status.
    fireEvent.click(screen.getByTestId('confirm-cancel'));
    expect(screen.queryByTestId('confirm-action')).not.toBeInTheDocument();
    expect(screen.getByTestId('experiment-status')).toHaveTextContent('Active');

    fireEvent.click(screen.getByTestId('action-complete'));
    fireEvent.click(screen.getByTestId('confirm-yes'));
    await waitFor(() => expect(screen.getByTestId('experiment-status')).toHaveTextContent('Completed'));
    expect(calledWith('POST', '/api/v1/experiments/exp-1/complete')).toBe(true);
    expect(screen.getByTestId('action-archive')).toBeInTheDocument();
  });

  it('completed → Archive (confirmed) calls POST /archive and leaves no actions', async () => {
    install(experiment({ status: 'completed' }));
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');

    fireEvent.click(screen.getByTestId('action-archive'));
    fireEvent.click(screen.getByTestId('confirm-yes'));
    await waitFor(() => expect(screen.getByTestId('experiment-status')).toHaveTextContent('Archived'));
    expect(calledWith('POST', '/api/v1/experiments/exp-1/archive')).toBe(true);
    expect(screen.getByTestId('experiment-actions').querySelectorAll('button')).toHaveLength(0);
  });

  it('surfaces the backend error and keeps the old status when a transition fails', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/experiments/exp-1', handler: () => experiment({ status: 'draft' }) },
        {
          method: 'POST',
          path: '/api/v1/experiments/exp-1/start',
          handler: () => {
            throw apiError(400, 'Cannot start experiment with status: draft (no metrics)');
          },
        },
      ]) as unknown as typeof apiFetch,
    );

    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
    fireEvent.click(screen.getByTestId('action-start'));

    expect(await screen.findByTestId('action-error')).toHaveTextContent(
      'Cannot start experiment with status: draft (no metrics)',
    );
    expect(screen.getByTestId('experiment-status')).toHaveTextContent('Draft');
    expect(screen.getByTestId('action-start')).not.toBeDisabled();
  });
});

describe('ExperimentDetailPage — lifecycle actions by role', () => {
  // The API's change routes (start, pause, complete, archive) require a
  // superuser or EXPERIMENT UPDATE -- the ADMIN and DEVELOPER roles, or a
  // custom role or direct grant -- whoever owns the experiment. The page
  // offers exactly those buttons.
  const OTHER_OWNER = 'someone-else';
  const NOTE =
    'Starting, pausing, completing and archiving an experiment needs permission to change experiments, ' +
    'which the ADMIN and DEVELOPER roles have; ';

  function signIn(user: Record<string, unknown> | null) {
    mockUseAuth.mockReturnValue({
      user,
      status: user ? 'authenticated' : 'loading',
    });
  }

  const as = (role: string, extra: Record<string, unknown> = {}) => ({
    id: 'user-1',
    email: `${role.toLowerCase()}@demo.com`,
    username: role.toLowerCase(),
    role,
    is_superuser: false,
    ...extra,
  });

  async function renderActive(owner: string | null = OTHER_OWNER) {
    install(experiment({ status: 'active', owner_id: owner }));
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
  }

  const lifecycleButtons = () =>
    ['start', 'pause', 'complete', 'archive'].filter(
      (a) => screen.queryByTestId(`action-${a}`) !== null,
    );

  it.each(['ADMIN', 'DEVELOPER'])('%s sees Pause and Complete and no note', async (role) => {
    signIn(as(role));
    await renderActive();
    expect(lifecycleButtons()).toEqual(['pause', 'complete']);
    expect(screen.queryByTestId('experiment-role-note')).toBeNull();
    expect(screen.getByTestId('view-results')).toHaveAttribute('href', '/results/exp-1');
  });

  it.each(['ANALYST', 'VIEWER'])(
    '%s sees no lifecycle button, the role note, and View results',
    async (role) => {
      signIn(as(role));
      await renderActive();
      expect(lifecycleButtons()).toEqual([]);
      expect(screen.getByTestId('experiment-actions').querySelectorAll('button')).toHaveLength(0);
      const note = screen.getByTestId('experiment-role-note');
      expect(note.tagName).toBe('P');
      expect(note).toHaveClass('text-slate-700');
      expect(note).toHaveTextContent(
        `${NOTE}you are ${role}. You can read its results.`,
      );
      expect(screen.getByTestId('view-results')).toHaveAttribute('href', '/results/exp-1');
    },
  );

  it('a superuser whose role is VIEWER sees the buttons and no note', async () => {
    signIn(as('VIEWER', { is_superuser: true }));
    await renderActive();
    expect(lifecycleButtons()).toEqual(['pause', 'complete']);
    expect(screen.queryByTestId('experiment-role-note')).toBeNull();
  });

  it('with no user yet, the buttons stay and no note is shown', async () => {
    signIn(null);
    await renderActive();
    expect(lifecycleButtons()).toEqual(['pause', 'complete']);
    expect(screen.queryByTestId('experiment-role-note')).toBeNull();
  });

  it('an ANALYST who owns the experiment gets the owner sentence and still no button', async () => {
    signIn(as('ANALYST'));
    await renderActive('user-1');
    expect(screen.getByTestId('experiment-owner')).toHaveTextContent('You (analyst@demo.com)');
    expect(lifecycleButtons()).toEqual([]);
    expect(screen.getByTestId('experiment-role-note')).toHaveTextContent(
      'You own this experiment, but starting, pausing, completing and archiving it needs permission ' +
        'to change experiments, which the ADMIN and DEVELOPER roles have; you are ANALYST. You can ' +
        'read its results.',
    );
  });

  it('an ANALYST sees an experiment with no owner as not theirs', async () => {
    signIn(as('ANALYST'));
    await renderActive(null);
    expect(screen.getByTestId('experiment-owner')).toHaveTextContent('No owner');
    expect(lifecycleButtons()).toEqual([]);
    expect(screen.getByTestId('experiment-role-note')).toHaveTextContent(
      `${NOTE}you are ANALYST. You can read its results.`,
    );
  });

  it('a draft says results come once it has started', async () => {
    signIn(as('VIEWER'));
    install(experiment({ status: 'draft', owner_id: OTHER_OWNER }));
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
    expect(lifecycleButtons()).toEqual([]);
    expect(screen.getByTestId('experiment-role-note')).toHaveTextContent(
      `${NOTE}you are VIEWER. You can read its results once it has started.`,
    );
    expect(screen.getByTestId('view-results-disabled')).toBeInTheDocument();
  });

  it('an archived experiment has no actions, so no note, for any role', async () => {
    for (const role of ['ANALYST', 'VIEWER', 'ADMIN']) {
      signIn(as(role));
      install(experiment({ status: 'archived', owner_id: OTHER_OWNER }));
      const { unmount } = render(<ExperimentDetailPage />);
      await screen.findByTestId('experiment-detail');
      expect(lifecycleButtons()).toEqual([]);
      expect(screen.queryByTestId('experiment-role-note')).toBeNull();
      expect(screen.getByTestId('view-results')).toBeInTheDocument();
      unmount();
    }
  });

  it.each([
    [403, "You don't have permission to update experiments"],
    [400, 'Cannot pause experiment with status: paused'],
  ])('a %s from the API still renders in action-error', async (status, detail) => {
    signIn(as('DEVELOPER'));
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments/exp-1',
          handler: () => experiment({ status: 'active', owner_id: OTHER_OWNER }),
        },
        {
          method: 'POST',
          path: '/api/v1/experiments/exp-1/pause',
          handler: () => {
            throw apiError(status, detail);
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
    fireEvent.click(screen.getByTestId('action-pause'));
    expect(await screen.findByTestId('action-error')).toHaveTextContent(detail);
    expect(screen.getByTestId('experiment-status')).toHaveTextContent('Active');
    expect(screen.queryByTestId('experiment-role-note')).toBeNull();
  });
});

describe('ExperimentDetailPage — error states', () => {
  it('renders a 404 view when the experiment does not exist', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments/exp-1',
          handler: () => {
            throw apiError(404, 'Experiment not found');
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<ExperimentDetailPage />);
    expect(await screen.findByTestId('experiment-not-found')).toBeInTheDocument();
    expect(screen.getByText('Experiment not found')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /back to experiments/i })).toHaveAttribute('href', '/experiments');
    expect(screen.queryByTestId('experiment-retry')).not.toBeInTheDocument();
  });

  it('renders a generic error with retry for other failures', async () => {
    let calls = 0;
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments/exp-1',
          handler: () => {
            calls += 1;
            if (calls === 1) throw apiError(0, "Can't reach the API");
            return experiment();
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<ExperimentDetailPage />);
    const errorView = await screen.findByTestId('experiment-error');
    expect(errorView).toHaveTextContent("Can't reach the API");

    fireEvent.click(screen.getByTestId('experiment-retry'));
    expect(await screen.findByTestId('experiment-detail')).toBeInTheDocument();
  });
});

describe('ExperimentDetailPage — warehouse analysis section (the `warehouse` module)', () => {
  // A core build (`modules/` deleted, or EXPERIMENTLY_PROFILE=core) resolves
  // `@modules/*` to the stub, which renders nothing. Asked of the resolver
  // itself, so the test follows whichever tree actually answered.
  const full = !require
    .resolve('@modules/components/warehouse/runs/WarehouseAnalysisSection')
    .includes('modules-stub');
  const RUNS = '/api/v1/warehouse/analysis/experiments/exp-1/runs';
  const warehouseCalls = () =>
    mockedApiFetch.mock.calls.filter(([p]) => String(p).startsWith('/api/v1/warehouse/'));

  function renderWith(modules: string[]) {
    return render(
      <ModulesProvider initial={{ profile: 'full', modules, version: 'test' }}>
        <ExperimentDetailPage />
      </ModulesProvider>,
    );
  }

  it('shows the section and asks for the runs only in a full build with the module installed', async () => {
    install(experiment({ status: 'active' }));
    renderWith(['warehouse']);
    expect(await screen.findByTestId('experiment-detail')).toBeInTheDocument();
    if (full) {
      expect(await screen.findByTestId('warehouse-analysis')).toBeInTheDocument();
      await waitFor(() => expect(calledWith('GET', RUNS)).toBe(true));
    } else {
      expect(screen.queryByTestId('warehouse-analysis')).toBeNull();
      expect(warehouseCalls()).toEqual([]);
    }
  });

  it('shows no section and calls no warehouse route when the module is not installed', async () => {
    install(experiment({ status: 'active' }));
    renderWith([]);
    expect(await screen.findByTestId('experiment-detail')).toBeInTheDocument();
    expect(screen.queryByTestId('warehouse-analysis')).toBeNull();
    expect(warehouseCalls()).toEqual([]);
  });
});

describe('ExperimentDetailPage — where the SDK hint sends the reader for an API key (#920)', () => {
  // /admin/api-keys opens only for a superuser (withAdminGuard), so the hint
  // names that page to a superuser alone. Everyone else is given the route
  // any signed-in user may call, with the docs page beside it.
  const signInAs = (role: string, is_superuser: boolean) =>
    mockUseAuth.mockReturnValue({
      user: {
        id: 'user-1',
        email: `${role.toLowerCase()}@demo.com`,
        username: role.toLowerCase(),
        role,
        is_superuser,
      },
      status: 'authenticated',
    });

  async function sdkHint() {
    install(experiment());
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
    return screen.getByTestId('sdk-hint');
  }

  it('sends a superuser to Admin → API Keys', async () => {
    signInAs('VIEWER', true);
    const box = await sdkHint();
    expect(within(box).getByRole('link', { name: 'Admin → API Keys' })).toHaveAttribute(
      'href',
      '/admin/api-keys',
    );
    expect(within(box).queryByTestId('sdk-hint-api-key-route')).toBeNull();
  });

  it.each(['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'])(
    'gives %s, who cannot open that page, the route and the docs page instead',
    async (role) => {
      signInAs(role, false);
      const box = await sdkHint();
      expect(within(box).queryByRole('link', { name: 'Admin → API Keys' })).toBeNull();
      expect(box.querySelector('a[href="/admin/api-keys"]')).toBeNull();
      const route = within(box).getByTestId('sdk-hint-api-key-route');
      expect(route).toHaveTextContent(
        'Create one for yourself with POST /api/v1/api-keys (see API Key Management) or ask an administrator.',
      );
      expect(within(route).getByRole('link', { name: 'API Key Management' })).toHaveAttribute(
        'href',
        docsUrl('security/api-keys'),
      );
      // The curl sample is still there for everyone.
      expect(box).toHaveTextContent('"experiment_key": "checkout-button-colour"');
    },
  );
});
