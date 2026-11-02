import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import { apiFetch } from '@/services/api';
import { FeatureFlag } from '@/services/featureFlags';
import { FLAG_ROLE_NOTE } from '@/utils/experimentPermissions';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/feature-flags', asPath: '/feature-flags' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

// Unset by default (no session, as before #917): the page keeps every control.
const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

function signIn(role: string, is_superuser = false) {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u@example.com', username: 'u', role, is_superuser },
  });
}

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

/** List items follow `FeatureFlagReadExtended`: `is_active`, no `status`. */
function flag(overrides: Partial<FeatureFlag>): FeatureFlag {
  return {
    id: 'flag-1',
    key: 'checkout_v2',
    name: 'Checkout v2',
    description: 'New checkout flow',
    is_active: false,
    rollout_percentage: 25,
    targeting_rules: null,
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
    ...overrides,
  };
}

const list = (items: FeatureFlag[]) => ({ items, total: items.length, skip: 0, limit: 100 });

const toggleCalls = () =>
  mockedApiFetch.mock.calls.filter(([p]) => /\/(enable|disable)$/.test(String(p)));

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
});

describe('FeatureFlagsPage', () => {
  it('renders key, name, status, rollout and updated columns with a switch per row', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/feature-flags',
          handler: () =>
            list([
              flag({ id: 'flag-1', key: 'checkout_v2', name: 'Checkout v2', is_active: false, rollout_percentage: 25 }),
              flag({ id: 'flag-2', key: 'dark_mode', name: 'Dark mode', is_active: true, rollout_percentage: 100 }),
              // Detail-shaped item (status string) must also work.
              flag({ id: 'flag-3', key: 'legacy', name: 'Legacy', status: 'active', is_active: undefined }),
            ]),
        },
      ]) as unknown as typeof apiFetch,
    );

    render(<FeatureFlagsPage />);
    expect(screen.getByTestId('flags-loading')).toBeInTheDocument();
    const table = await screen.findByTestId('flags-table');
    const rows = within(table).getAllByTestId('flag-row');
    expect(rows).toHaveLength(3);

    expect(rows[0]).toHaveTextContent('checkout_v2');
    expect(within(rows[0]).getByTestId('flag-link')).toHaveAttribute('href', '/feature-flags/flag-1');
    expect(within(rows[0]).getByTestId('flag-status-pill')).toHaveTextContent('Off');
    expect(rows[0]).toHaveTextContent('25%');
    expect(within(rows[0]).getByRole('switch')).toHaveAttribute('aria-checked', 'false');

    expect(within(rows[1]).getByTestId('flag-status-pill')).toHaveTextContent('On');
    expect(within(rows[1]).getByRole('switch')).toHaveAttribute('aria-checked', 'true');
    expect(within(rows[2]).getByRole('switch')).toHaveAttribute('aria-checked', 'true');

    expect(screen.getByTestId('new-flag-btn')).toHaveAttribute('href', '/feature-flags/new');
  });

  it('turns a flag on via POST /enable with an optimistic update', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/feature-flags', handler: () => list([flag({ is_active: false })]) },
        {
          method: 'POST',
          path: '/api/v1/feature-flags/flag-1/enable',
          handler: () => ({ id: 'flag-1', key: 'checkout_v2', name: 'Checkout v2', status: 'active', updated_at: '2026-09-11T00:00:00Z', audit_log_id: null }),
        },
      ]) as unknown as typeof apiFetch,
    );

    render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-table');

    const toggle = screen.getByTestId('flag-toggle-checkout_v2');
    fireEvent.click(toggle);

    // Optimistic: flips immediately, disabled while in flight.
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(toggle).toBeDisabled();

    await waitFor(() => expect(toggle).not.toBeDisabled());
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('flag-status-pill')).toHaveTextContent('On');

    const [[path, options]] = toggleCalls();
    expect(path).toBe('/api/v1/feature-flags/flag-1/enable');
    expect(options?.method).toBe('POST');
    expect(options?.json).toEqual({ reason: null });
  });

  it('turns a flag off via POST /disable', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/feature-flags', handler: () => list([flag({ is_active: true })]) },
        {
          method: 'POST',
          path: '/api/v1/feature-flags/flag-1/disable',
          handler: () => ({ id: 'flag-1', key: 'checkout_v2', name: 'Checkout v2', status: 'inactive', updated_at: '2026-09-11T00:00:00Z', audit_log_id: null }),
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-table');
    fireEvent.click(screen.getByTestId('flag-toggle-checkout_v2'));
    await waitFor(() => expect(screen.getByTestId('flag-status-pill')).toHaveTextContent('Off'));
    expect(toggleCalls()[0][0]).toBe('/api/v1/feature-flags/flag-1/disable');
  });

  it('rolls the switch back and shows the error when the toggle fails', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/feature-flags', handler: () => list([flag({ is_active: false })]) },
        {
          method: 'POST',
          path: '/api/v1/feature-flags/flag-1/enable',
          handler: () => {
            throw apiError(403, 'Not enough permissions to enable this feature flag');
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-table');

    const toggle = screen.getByTestId('flag-toggle-checkout_v2');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-checked', 'true');

    expect(await screen.findByTestId('flag-toggle-error')).toHaveTextContent(
      'Not enough permissions to enable this feature flag',
    );
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(toggle).not.toBeDisabled();
    expect(screen.getByTestId('flag-status-pill')).toHaveTextContent('Off');
  });

  it('rolls back only the row that failed, leaving a concurrent toggle alone', async () => {
    let rejectA: (reason: unknown) => void = () => {};
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/feature-flags',
          handler: () =>
            list([
              flag({ id: 'flag-a', key: 'flag_a', name: 'Flag A', is_active: false }),
              flag({ id: 'flag-b', key: 'flag_b', name: 'Flag B', is_active: false }),
            ]),
        },
        {
          method: 'POST',
          path: '/api/v1/feature-flags/flag-a/enable',
          handler: () =>
            new Promise((_resolve, reject) => {
              rejectA = reject;
            }),
        },
        {
          method: 'POST',
          path: '/api/v1/feature-flags/flag-b/enable',
          handler: () => ({
            id: 'flag-b',
            key: 'flag_b',
            name: 'Flag B',
            status: 'active',
            updated_at: '2026-09-11T00:00:00Z',
            audit_log_id: null,
          }),
        },
      ]) as unknown as typeof apiFetch,
    );

    render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-table');

    const toggleA = screen.getByTestId('flag-toggle-flag_a');
    const toggleB = screen.getByTestId('flag-toggle-flag_b');

    // A is flipped first and stays in flight; B is flipped next and succeeds.
    fireEvent.click(toggleA);
    fireEvent.click(toggleB);
    await waitFor(() => expect(toggleB).not.toBeDisabled());
    expect(toggleB).toHaveAttribute('aria-checked', 'true');

    // Now A fails: only A may revert — B is already on server-side.
    await act(async () => {
      rejectA(apiError(403, 'Not enough permissions to enable flag_a'));
    });

    expect(await screen.findByTestId('flag-toggle-error')).toHaveTextContent('flag_a');
    expect(toggleA).toHaveAttribute('aria-checked', 'false');
    expect(toggleB).toHaveAttribute('aria-checked', 'true');

    const rows = within(screen.getByTestId('flags-table')).getAllByTestId('flag-row');
    expect(within(rows[0]).getByTestId('flag-status-pill')).toHaveTextContent('Off');
    expect(within(rows[1]).getByTestId('flag-status-pill')).toHaveTextContent('On');
  });

  it('shows a message per failing row when two toggles fail', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/feature-flags',
          handler: () =>
            list([
              flag({ id: 'flag-a', key: 'flag_a', name: 'Flag A', is_active: false }),
              flag({ id: 'flag-b', key: 'flag_b', name: 'Flag B', is_active: false }),
            ]),
        },
        {
          method: 'POST',
          path: '/api/v1/feature-flags/flag-a/enable',
          handler: () => Promise.reject(new Error('flag_a is locked by a rollout')),
        },
        {
          method: 'POST',
          path: '/api/v1/feature-flags/flag-b/enable',
          handler: () => Promise.reject(new Error('flag_b is locked by a rollout')),
        },
      ]) as unknown as typeof apiFetch,
    );

    render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-table');

    fireEvent.click(screen.getByTestId('flag-toggle-flag_a'));
    fireEvent.click(screen.getByTestId('flag-toggle-flag_b'));

    const banner = await screen.findByTestId('flag-toggle-error');
    await waitFor(() => {
      expect(banner).toHaveTextContent('flag_a is locked by a rollout');
      expect(banner).toHaveTextContent('flag_b is locked by a rollout');
    });
  });

  it('ignores a stale list response that lands after a newer one', async () => {
    let resolveActive: (value: unknown) => void = () => {};
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/feature-flags',
          handler: (_p, options) => {
            const status = options.query?.status;
            if (status === 'ACTIVE') {
              return new Promise((resolve) => {
                resolveActive = resolve;
              });
            }
            if (status === 'INACTIVE') {
              return list([flag({ id: 'flag-off', key: 'off_flag', name: 'Off flag', is_active: false })]);
            }
            return list([flag({})]);
          },
        },
      ]) as unknown as typeof apiFetch,
    );

    render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-table');

    fireEvent.click(screen.getByTestId('flag-filter-active')); // slow
    fireEvent.click(screen.getByTestId('flag-filter-inactive')); // fast

    await waitFor(() => expect(screen.getByTestId('flags-table')).toHaveTextContent('off_flag'));

    // The superseded "On" request now answers — it must not repaint the list.
    await act(async () => {
      resolveActive(list([flag({ id: 'flag-on', key: 'on_flag', name: 'On flag', is_active: true })]));
    });

    expect(screen.getByTestId('flags-table')).toHaveTextContent('off_flag');
    expect(screen.getByTestId('flags-table')).not.toHaveTextContent('on_flag');
    expect(within(screen.getByTestId('flags-table')).getAllByTestId('flag-row')).toHaveLength(1);
  });

  it('sends the upper-cased status filter and shows the filtered empty state', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/feature-flags',
          handler: (_p, options) => (options.query?.status ? list([]) : list([flag({})])),
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-table');

    fireEvent.click(screen.getByTestId('flag-filter-active'));
    const empty = await screen.findByTestId('flags-empty');
    expect(empty).toHaveTextContent('No flags match this filter');
    expect(mockedApiFetch.mock.calls[1][1]?.query).toMatchObject({ status: 'ACTIVE' });
  });

  it('shows the create-first-flag empty state when there are no flags', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([{ path: '/api/v1/feature-flags', handler: () => list([]) }]) as unknown as typeof apiFetch,
    );
    render(<FeatureFlagsPage />);
    const empty = await screen.findByTestId('flags-empty');
    expect(empty).toHaveTextContent('No feature flags yet');
    expect(within(empty).getByRole('link', { name: /create your first flag/i })).toHaveAttribute(
      'href',
      '/feature-flags/new',
    );
  });

  it('renders the error state with a working retry', async () => {
    let calls = 0;
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/feature-flags',
          handler: () => {
            calls += 1;
            if (calls === 1) throw apiError(500, 'boom');
            return list([flag({})]);
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<FeatureFlagsPage />);
    expect(await screen.findByTestId('flags-error')).toHaveTextContent('boom');
    fireEvent.click(screen.getByTestId('flags-retry'));
    expect(await screen.findByTestId('flags-table')).toBeInTheDocument();
  });

  it("shows each flag's type in a Type column after Name (#1497)", async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/feature-flags',
          handler: () =>
            list([
              flag({ id: 'f-1', key: 'dark_mode', value_type: 'boolean', on_value: true, off_value: false }),
              flag({ id: 'f-2', key: 'label', value_type: 'string', on_value: 'Complete purchase', off_value: 'Buy now' }),
              flag({ id: 'f-3', key: 'retries', value_type: 'number', on_value: 5, off_value: 3 }),
              flag({ id: 'f-4', key: 'theme', value_type: 'json', on_value: { color: 'blue' }, off_value: { color: 'grey' } }),
              // A server without values sends no type: the flag is boolean.
              flag({ id: 'f-5', key: 'legacy' }),
            ]),
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<FeatureFlagsPage />);
    const table = await screen.findByTestId('flags-table');
    const headers = within(table).getAllByRole('columnheader').map((th) => th.textContent);
    expect(headers.slice(0, 3)).toEqual(['Key', 'Name', 'Type']);
    expect(within(table).getAllByTestId('flag-value-type').map((td) => td.textContent)).toEqual([
      'Boolean',
      'String',
      'Number',
      'JSON',
      'Boolean',
    ]);
    // No value is shown in the list.
    expect(table).not.toHaveTextContent('Complete purchase');
  });
});

describe('who may change flags (#917)', () => {
  const serve = (items: FeatureFlag[]) =>
    mockedApiFetch.mockImplementation(
      routedApi([{ path: '/api/v1/feature-flags', handler: () => list(items) }]) as unknown as typeof apiFetch,
    );

  it.each(['ANALYST', 'VIEWER'])(
    '%s reads the list with no switch, no "+ New Flag" and a note saying why',
    async (role) => {
      signIn(role);
      serve([flag({ is_active: true }), flag({ id: 'flag-2', key: 'dark_mode', name: 'Dark mode', is_active: false })]);
      render(<FeatureFlagsPage />);

      const table = await screen.findByTestId('flags-table');
      const rows = within(table).getAllByTestId('flag-row');
      expect(rows).toHaveLength(2);
      // The rows still say whether each flag is on; the pill is the only state shown.
      expect(within(rows[0]).getByTestId('flag-status-pill')).toHaveTextContent('On');
      expect(within(rows[1]).getByTestId('flag-status-pill')).toHaveTextContent('Off');
      expect(within(rows[0]).getByTestId('flag-link')).toHaveAttribute('href', '/feature-flags/flag-1');

      expect(screen.queryByTestId('flag-toggle-checkout_v2')).toBeNull();
      expect(screen.queryByTestId('flag-toggle-dark_mode')).toBeNull();
      expect(screen.queryByRole('switch')).toBeNull();
      expect(within(table).queryByText('On / Off')).toBeNull();
      // Key, Name, Type (#1497), Status, Rollout, Updated: no On / Off.
      expect(within(table).getAllByRole('columnheader').map((th) => th.textContent)).toEqual([
        'Key',
        'Name',
        'Type',
        'Status',
        'Rollout',
        'Updated',
      ]);

      expect(screen.queryByTestId('new-flag-btn')).toBeNull();
      const note = screen.getByTestId('flags-role-note');
      expect(note).toHaveTextContent(FLAG_ROLE_NOTE);
      expect(note).toHaveTextContent('Feature flags are created and changed by the ADMIN and DEVELOPER roles.');
    },
  );

  it.each(['ANALYST', 'VIEWER'])('%s gets the empty state with no create link', async (role) => {
    signIn(role);
    serve([]);
    render(<FeatureFlagsPage />);
    const empty = await screen.findByTestId('flags-empty');
    expect(empty).toHaveTextContent('No feature flags yet');
    expect(within(empty).queryByRole('link')).toBeNull();
    expect(screen.queryByTestId('new-flag-btn')).toBeNull();
    expect(screen.getByTestId('flags-role-note')).toBeInTheDocument();
  });

  it.each([
    ['ADMIN', false],
    ['DEVELOPER', false],
    ['VIEWER', true],
  ])('%s (superuser: %s) keeps "+ New Flag" and the switches', async (role, is_superuser) => {
    signIn(role, is_superuser);
    serve([flag({ is_active: true })]);
    render(<FeatureFlagsPage />);
    const table = await screen.findByTestId('flags-table');
    expect(screen.getByTestId('new-flag-btn')).toHaveAttribute('href', '/feature-flags/new');
    expect(screen.getByTestId('flag-toggle-checkout_v2')).toHaveAttribute('aria-checked', 'true');
    expect(within(table).getByText('On / Off')).toBeInTheDocument();
    expect(screen.queryByTestId('flags-role-note')).toBeNull();
  });

  it('an ADMIN still gets the create link in the empty state', async () => {
    signIn('ADMIN');
    serve([]);
    render(<FeatureFlagsPage />);
    const empty = await screen.findByTestId('flags-empty');
    expect(within(empty).getByRole('link', { name: /create your first flag/i })).toHaveAttribute('href', '/feature-flags/new');
  });
});
