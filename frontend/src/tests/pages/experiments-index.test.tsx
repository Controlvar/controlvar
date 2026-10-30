import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import ExperimentsPage from '@/pages/experiments/index';
import { apiFetch } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { Experiment } from '@/types/experiments';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments', asPath: '/experiments' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

// The page asks for the current role to decide whether to offer "+ New Experiment".
const mockAuth = jest.fn().mockReturnValue(null);
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

function exp(overrides: Partial<Experiment>): Experiment {
  return {
    id: 'exp-1',
    name: 'Checkout CTA',
    key: 'checkout-cta',
    experiment_type: 'a_b',
    status: 'active',
    targeting_rules: null,
    owner_id: 'a1b2c3d4-0000-0000-0000-000000000000',
    start_date: null,
    end_date: null,
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-01T10:00:00Z',
    variants: [
      { id: 'v1', name: 'Control', is_control: true, traffic_allocation: 50 },
      { id: 'v2', name: 'B', is_control: false, traffic_allocation: 50 },
    ],
    metrics: [],
    ...overrides,
  };
}

function listResponse(items: Experiment[]) {
  return { items, total: items.length, skip: 0, limit: 100 };
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  process.env.NEXT_PUBLIC_API_URL = 'http://api.test';
});

afterAll(() => {
  delete process.env.NEXT_PUBLIC_API_URL;
});

describe('ExperimentsPage', () => {
  it('renders the table with a Type column filled from experiment_type and no owner UUID', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments',
          handler: () =>
            listResponse([
              exp({ id: 'exp-1', name: 'Checkout CTA', experiment_type: 'a_b' }),
              exp({ id: 'exp-2', name: 'Pricing page', experiment_type: 'bandit', status: 'draft' }),
              exp({ id: 'exp-3', name: 'Unknown type', experiment_type: 'weird' }),
            ]),
        },
      ]) as unknown as typeof apiFetch,
    );

    render(<ExperimentsPage />);
    expect(screen.getByTestId('experiments-loading')).toBeInTheDocument();

    const table = await screen.findByTestId('experiments-table');
    const rows = within(table).getAllByTestId('experiment-row');
    expect(rows).toHaveLength(3);

    const types = within(table).getAllByTestId('experiment-type-cell').map((el) => el.textContent);
    expect(types).toEqual(['A/B Test', 'Bandit', 'weird']);

    expect(within(rows[0]).getByTestId('experiment-link')).toHaveAttribute('href', '/experiments/exp-1');
    expect(within(rows[0]).getByTestId('experiment-status-pill')).toHaveTextContent('Active');
    expect(within(rows[1]).getByTestId('experiment-status-pill')).toHaveTextContent('Draft');

    // The list response only carries owner_id; the raw UUID must not be rendered.
    expect(table).not.toHaveTextContent('a1b2c3d4-0000');
    expect(within(table).queryByText('Owner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('first-run-checklist')).not.toBeInTheDocument();
  });

  it('sends the status filter as status_filter when a pill is clicked', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([{ path: '/api/v1/experiments', handler: () => listResponse([exp({})]) }]) as unknown as typeof apiFetch,
    );
    render(<ExperimentsPage />);
    await screen.findByTestId('experiments-table');

    fireEvent.click(screen.getByTestId('filter-paused'));
    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledTimes(2));
    const [, options] = mockedApiFetch.mock.calls[1];
    expect(options?.query).toMatchObject({ status_filter: 'paused' });
    expect(screen.getByTestId('filter-paused')).toHaveAttribute('aria-pressed', 'true');
  });

  it('shows the first-run checklist with a copyable curl when there are no experiments', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([{ path: '/api/v1/experiments', handler: () => listResponse([]) }]) as unknown as typeof apiFetch,
    );
    render(<ExperimentsPage />);

    const card = await screen.findByTestId('first-run-checklist');
    expect(within(card).getByTestId('checklist-create-experiment')).toHaveAttribute('href', '/experiments/new');
    // No account is known here (useOptionalAuth gives null), so step 2 names
    // the route rather than the superuser-only Admin page (#920).
    expect(within(card).queryByTestId('checklist-api-keys')).toBeNull();
    expect(within(card).getByTestId('checklist-api-key-route')).toHaveTextContent('POST /api/v1/api-keys');

    const curl = within(card).getByTestId('checklist-curl').textContent ?? '';
    expect(curl).toContain('http://api.test/api/v1/tracking/assign');
    expect(curl).toContain('X-API-Key');
    expect(curl).toContain('"experiment_key"');
    expect(curl).toContain('"user_id"');

    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    fireEvent.click(within(card).getByTestId('checklist-copy-curl'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(curl));
    await waitFor(() => expect(within(card).getByTestId('checklist-copy-curl')).toHaveTextContent('Copied'));
    // Announced as well as shown: the status region says it too.
    expect(within(card).getByRole('status')).toHaveTextContent('Copied');
    expect(screen.queryByTestId('experiments-table')).not.toBeInTheDocument();
  });

  it('shows a plain empty state (not the checklist) when a filter yields nothing', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments',
          handler: (_path, options) =>
            options.query?.status_filter ? listResponse([]) : listResponse([exp({})]),
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<ExperimentsPage />);
    await screen.findByTestId('experiments-table');

    fireEvent.click(screen.getByTestId('filter-completed'));
    const empty = await screen.findByTestId('experiments-empty');
    expect(empty).toHaveTextContent('No completed experiments');
    expect(screen.queryByTestId('first-run-checklist')).not.toBeInTheDocument();

    fireEvent.click(within(empty).getByRole('button', { name: /show all/i }));
    expect(await screen.findByTestId('experiments-table')).toBeInTheDocument();
  });

  it('renders the error banner when the list request fails', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments',
          handler: () => {
            throw apiError(500, 'database is on fire');
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<ExperimentsPage />);
    expect(await screen.findByTestId('experiments-error')).toHaveTextContent('database is on fire');
    expect(screen.queryByTestId('first-run-checklist')).not.toBeInTheDocument();
  });
});

describe('ExperimentsPage — create button by role', () => {
  const renderAs = (role: string | null) => {
    mockAuth.mockReturnValue(
      role === null
        ? null
        : {
            user: {
              id: 'u1',
              email: 'u@example.com',
              username: 'u',
              role,
              is_superuser: false,
              is_active: true,
            },
            status: 'authenticated' as const,
            login: jest.fn(),
            logout: jest.fn(),
            hasRole: (...roles: string[]) => roles.includes(role),
          },
    );
    return render(<ExperimentsPage />);
  };

  it.each(['ADMIN', 'DEVELOPER'])('offers "+ New Experiment" to %s', async (role) => {
    renderAs(role);
    expect(await screen.findByTestId('new-experiment-btn')).toBeInTheDocument();
  });

  it.each(['ANALYST', 'VIEWER'])('hides it from %s, whom the API refuses', async (role) => {
    renderAs(role);
    await screen.findByTestId('status-filter');
    expect(screen.queryByTestId('new-experiment-btn')).not.toBeInTheDocument();
  });

  // #1455: a secondary link beside it, to the roles the group routes admit.
  it.each(['ADMIN', 'DEVELOPER'])('offers %s the "Exclusion groups" link, underlined', async (role) => {
    renderAs(role);
    const link = await screen.findByTestId('exclusion-groups-link');
    expect(link).toHaveTextContent(/^Exclusion groups$/);
    expect(link).toHaveAttribute('href', '/experiments/exclusion-groups');
    expect(Array.from(link.classList)).toContain('underline');
  });

  it.each(['ANALYST', 'VIEWER'])('shows %s no "Exclusion groups" link', async (role) => {
    renderAs(role);
    await screen.findByTestId('status-filter');
    expect(screen.queryByTestId('exclusion-groups-link')).not.toBeInTheDocument();
  });

  it.each(['ADMIN', 'DEVELOPER'])('offers %s the "Global holdouts" link, underlined, after "Exclusion groups"', async (role) => {
    renderAs(role);
    const link = await screen.findByTestId('holdouts-link');
    expect(link).toHaveTextContent(/^Global holdouts$/);
    expect(link).toHaveAttribute('href', '/experiments/holdouts');
    expect(Array.from(link.classList)).toContain('underline');
    const order = Array.from(link.parentElement!.children).map((el) => el.getAttribute('data-testid'));
    expect(order).toEqual(['exclusion-groups-link', 'holdouts-link', 'new-experiment-btn']);
  });

  it.each(['ANALYST', 'VIEWER'])('shows %s no "Global holdouts" link', async (role) => {
    renderAs(role);
    await screen.findByTestId('status-filter');
    expect(screen.queryByTestId('holdouts-link')).not.toBeInTheDocument();
  });

  it('offers it to a superuser whatever their role says', async () => {
    // check_permission lets a superuser through before it reads the role
    // table, so hiding the button from them would be stricter than the API.
    mockAuth.mockReturnValue({
      user: {
        id: 'u1',
        email: 'root@example.com',
        username: 'root',
        role: 'VIEWER',
        is_superuser: true,
        is_active: true,
      },
      status: 'authenticated' as const,
      login: jest.fn(),
      logout: jest.fn(),
      hasRole: () => false,
    });
    render(<ExperimentsPage />);
    expect(await screen.findByTestId('new-experiment-btn')).toBeInTheDocument();
    expect(screen.getByTestId('exclusion-groups-link')).toBeInTheDocument();
  });
});

describe("ExperimentsPage — the checklist's API-key step by account (#920)", () => {
  // /admin/api-keys opens only for a superuser (withAdminGuard). The step
  // links there for a superuser alone; everyone else gets the route any
  // signed-in user may call, with the docs page beside it.
  const signIn = (role: string, is_superuser: boolean) =>
    mockAuth.mockReturnValue({
      user: { id: 'u1', email: 'u@example.com', username: 'u', role, is_superuser, is_active: true },
      status: 'authenticated' as const,
      login: jest.fn(),
      logout: jest.fn(),
      hasRole: (...roles: string[]) => roles.includes(role),
    });

  afterEach(() => mockAuth.mockReturnValue(null));

  async function checklist() {
    mockedApiFetch.mockImplementation(
      routedApi([{ path: '/api/v1/experiments', handler: () => listResponse([]) }]) as unknown as typeof apiFetch,
    );
    render(<ExperimentsPage />);
    return screen.findByTestId('first-run-checklist');
  }

  it('opens Admin → API Keys for a superuser', async () => {
    signIn('VIEWER', true);
    const card = await checklist();
    expect(within(card).getByTestId('checklist-api-keys')).toHaveAttribute('href', '/admin/api-keys');
    expect(within(card).queryByTestId('checklist-api-key-route')).toBeNull();
  });

  it.each(['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'])(
    'gives %s, who cannot open that page, the route and the docs page instead',
    async (role) => {
      signIn(role, false);
      const card = await checklist();
      expect(within(card).queryByTestId('checklist-api-keys')).toBeNull();
      expect(card.querySelector('a[href="/admin/api-keys"]')).toBeNull();
      const route = within(card).getByTestId('checklist-api-key-route');
      expect(route).toHaveTextContent(
        'Create one for yourself with POST /api/v1/api-keys (see API Key Management) or ask an administrator.',
      );
      expect(within(route).getByRole('link', { name: 'API Key Management' })).toHaveAttribute(
        'href',
        docsUrl('security/api-keys'),
      );
    },
  );
});
