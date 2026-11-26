/**
 * The flag list in Production, Staging and Development.
 *
 * Production lists the flags exactly as before environments. Staging and
 * Development list the same flags with their settings there, read from the
 * beta batch route (`GET /api/v1/environments/{environment}/feature-flag-settings`
 * with one `flag_id` per listed flag), and a row's switch changes the flag
 * there through `PUT /api/v1/feature-flags/{id}/environments/{environment}`,
 * never through enable/disable. The beta routes' answers are mocked in the
 * API's shapes, which the first test holds to the committed OpenAPI snapshot.
 */
import React from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import { EnvironmentSwitcher } from '@/components/feature-flags/EnvironmentSwitcher';
import { FlagEnvironmentProvider, useFlagEnvironment } from '@/contexts/FlagEnvironmentContext';
import { apiFetch, ApiFetchOptions, buildQuery } from '@/services/api';
import { FeatureFlagListItem, FlagEnvironmentSettings } from '@/services/featureFlags';
import { NEW_FLAGS_IN_PRODUCTION, PRODUCTION_BANNER } from '@/utils/flagEnvironments';
import { shapeErrors } from '@/tests/helpers/openapiShape';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import { apiError, makeRouter, routedApi, Route } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

let mockRouter = makeRouter({ pathname: '/feature-flags', asPath: '/feature-flags' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

function item(id: string, key: string, overrides: Partial<FeatureFlagListItem> = {}): FeatureFlagListItem {
  return {
    id,
    key,
    name: key,
    is_active: true,
    status: 'active',
    rollout_percentage: 100,
    targeting_rules: null,
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
    ...overrides,
  };
}

const FLAGS = [item('f1', 'checkout_v2'), item('f2', 'dark_mode', { stale: true, stale_reasons: ['on_for_everyone'] })];

function setting(environment: string, overrides: Partial<FlagEnvironmentSettings> = {}): FlagEnvironmentSettings {
  return {
    environment,
    status: 'inactive',
    is_active: false,
    rollout_percentage: 0,
    targeting_rules: null,
    configured: false,
    updated_at: null,
    ...overrides,
  };
}

/** The mocked API: `first` answers before the routes every test has. */
function install(batch: Record<string, FlagEnvironmentSettings> = {}, flags = FLAGS, first: Route[] = []) {
  const api = routedApi([...first, ...routes(batch, flags)]);
  mockedApiFetch.mockImplementation(api as unknown as typeof apiFetch);
  return api;
}

function routes(batch: Record<string, FlagEnvironmentSettings>, flags: FeatureFlagListItem[]): Route[] {
  const live: Record<string, FlagEnvironmentSettings> = { ...batch };
  return [
    { path: '/api/v1/feature-flags', handler: () => ({ items: flags, total: flags.length, skip: 0, limit: 100 }) },
    {
      path: /^\/api\/v1\/environments\/(staging|development)\/feature-flag-settings$/,
      handler: (p, opts) => {
        const env = p.split('/')[4];
        const ids = (opts.query?.flag_id as string[]) ?? [];
        return { environment: env, settings: Object.fromEntries(ids.map((id) => [id, live[id] ?? setting(env)])) };
      },
    },
    {
      method: 'PUT',
      path: /^\/api\/v1\/feature-flags\/[^/]+\/environments\/(staging|development)$/,
      handler: (p, opts) => {
        const [, , , , id, , env] = p.split('/');
        const body = opts.json as { is_active: boolean };
        live[id] = setting(env, { ...(live[id] ?? {}), is_active: body.is_active, status: body.is_active ? 'active' : 'inactive', configured: true, updated_at: '2026-11-22T00:00:00Z' });
        return live[id];
      },
    },
    {
      method: 'POST',
      path: /^\/api\/v1\/feature-flags\/[^/]+\/(enable|disable)$/,
      handler: (p) => ({ id: p.split('/')[4], name: 'x', key: 'x', status: p.endsWith('/enable') ? 'active' : 'inactive', updated_at: '2026-11-22T00:00:00Z', audit_log_id: null }),
    },
  ];
}

/** An answer the test gives when it chooses to. */
function later<T>() {
  let give!: (value: T) => void;
  let refuse!: (reason: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    give = resolve;
    refuse = reject;
  });
  return { promise, give, refuse };
}

function calls(): string[] {
  return mockedApiFetch.mock.calls.map(([p, opts]) => {
    const o = (opts as ApiFetchOptions | undefined) ?? {};
    return `${(o.method ?? 'GET').toUpperCase()} ${String(p)}${buildQuery(o.query)}`;
  });
}

function renderList(env?: string) {
  mockRouter = makeRouter({
    pathname: '/feature-flags',
    asPath: env ? `/feature-flags?env=${env}` : '/feature-flags',
    query: env ? { env } : {},
  });
  return renderInAppRoot(
    <FlagEnvironmentProvider>
      <EnvironmentSwitcher />
      <FeatureFlagsPage />
    </FlagEnvironmentProvider>,
  );
}

const row = (key: string) => screen.getAllByTestId('flag-row').find((r) => r.getAttribute('data-flag-key') === key)!;

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
  localStorage.clear();
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u1@example.com', username: 'u1', role: 'ADMIN', is_superuser: false },
  });
});

it("mocks the environment routes in the API's own shapes", async () => {
  const api = install();
  const batch = await api('/api/v1/environments/staging/feature-flag-settings', { query: { flag_id: ['f1', 'f2'] } });
  expect(shapeErrors('EnvironmentFlagSettings', batch)).toEqual([]);
  const answer = await api('/api/v1/feature-flags/f1/environments/staging', { method: 'PUT', json: { is_active: true } });
  expect(shapeErrors('FlagEnvironmentSettings', answer)).toEqual([]);
});

describe('the flag list in Production', () => {
  it('lists the flags as before, with no environment route, and says what a change reaches', async () => {
    install();
    renderList();
    await screen.findByTestId('flags-table');
    expect(calls()).toEqual(['GET /api/v1/feature-flags']);
    expect(screen.getByTestId('production-banner')).toHaveTextContent(PRODUCTION_BANNER);
    expect(screen.getByTestId('new-flag-btn')).toBeInTheDocument();
    expect(screen.getByTestId('flag-status-filter')).toBeInTheDocument();
    expect(within(row('dark_mode')).getByTestId('flag-stale-pill')).toBeInTheDocument();
  });
});

describe('the flag list in Staging and Development', () => {
  it("reads every listed flag's Staging setting from the batch route, one flag_id each", async () => {
    install({ f1: setting('staging', { status: 'active', is_active: true, rollout_percentage: 30, configured: true, updated_at: '2026-11-20T09:00:00Z' }) });
    renderList('staging');
    await screen.findByTestId('flags-table');
    expect(calls()).toEqual([
      'GET /api/v1/feature-flags',
      'GET /api/v1/environments/staging/feature-flag-settings?flag_id=f1&flag_id=f2',
    ]);
    expect(within(row('checkout_v2')).getByTestId('flag-status-pill')).toHaveTextContent('On');
    expect(row('checkout_v2')).toHaveTextContent('30%');
    // No setting in Staging: off, and the row says so.
    expect(within(row('dark_mode')).getByTestId('flag-status-pill')).toHaveTextContent('Off');
    expect(within(row('dark_mode')).getByTestId('flag-not-configured')).toHaveTextContent('Not configured in Staging');
    expect(row('dark_mode')).toHaveTextContent('0%');
    // Production's filters, stale marks and banner are not shown.
    expect(screen.queryByTestId('flag-status-filter')).toBeNull();
    expect(screen.queryByTestId('flag-stale-filter')).toBeNull();
    expect(screen.queryByTestId('flag-stale-pill')).toBeNull();
    expect(screen.queryByTestId('production-banner')).toBeNull();
    // A new flag starts in Production, so it is created from there.
    expect(screen.queryByTestId('new-flag-btn')).toBeNull();
    expect(screen.getByTestId('flags-created-in-production')).toHaveTextContent(NEW_FLAGS_IN_PRODUCTION);
    expect(screen.queryByTestId('flags-environment-empty')).toBeNull();
  });

  it('says so when no listed flag has a setting in Staging', async () => {
    install();
    renderList('staging');
    await screen.findByTestId('flags-table');
    expect(screen.getByTestId('flags-environment-empty')).toHaveTextContent(
      'No flags configured in Staging yet. Promote a flag from Development, or configure one here.',
    );
  });

  it('announces that text, in a status that is on the page before it', async () => {
    const batch = later<unknown>();
    install({}, FLAGS, [{ path: '/api/v1/environments/staging/feature-flag-settings', handler: () => batch.promise }]);
    renderList('staging');
    await screen.findByTestId('flags-loading');
    const empty = screen.getAllByRole('status').filter((s) => s.getAttribute('data-testid') !== 'environment-announcement');
    expect(empty).toHaveLength(1);
    expect(empty[0]).toBeEmptyDOMElement();
    await act(async () => batch.give({ environment: 'staging', settings: {} }));
    expect(empty[0]).toContainElement(await screen.findByTestId('flags-environment-empty'));
  });

  it('says the environment only once its rows have loaded, and not after a load that failed', async () => {
    const batch = later<unknown>();
    install({}, FLAGS, [{ path: '/api/v1/environments/staging/feature-flag-settings', handler: () => batch.promise }]);
    renderList();
    await screen.findByTestId('flags-table');
    const said = () => screen.getByTestId('environment-announcement').textContent;
    expect(said()).toBe('');
    const select = screen.getByLabelText('Environment') as HTMLSelectElement;
    fireEvent.change(select, { target: { value: 'staging' } });
    await screen.findByTestId('flags-loading');
    // Everything already answered, the URL change among it, has been read and rendered.
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));
    expect(said()).toBe('');
    await act(async () => batch.refuse(apiError(503, 'Service unavailable')));
    await screen.findByTestId('flags-error');
    expect(said()).toBe('');
    fireEvent.change(select, { target: { value: 'development' } });
    await screen.findByTestId('flags-table');
    await waitFor(() => expect(said()).toBe('Showing Development.'));
    expect(select.value).toBe('development');
  });

  it('names the environment in the page title, but not for Production', async () => {
    install();
    renderList('staging');
    await screen.findByTestId('flags-table');
    expect(document.title).toBe('Feature Flags in Staging · Controlvar');
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'production' } });
    await waitFor(() => expect(document.title).toBe('Feature Flags · Controlvar'));
  });

  it('Back to the list in another environment never titles the page with the one just left', async () => {
    install();
    const titles: string[] = [];
    /** The page's title at every commit, as a screen reader's route announcement would read it. */
    function Titles() {
      useFlagEnvironment();
      React.useLayoutEffect(() => {
        titles.push(document.title);
      });
      return null;
    }
    // The provider outlives a page, as in _app: first another page, in Staging.
    const at = (env: string, page: React.ReactNode) => {
      mockRouter = makeRouter({ pathname: '/feature-flags', asPath: `/feature-flags?env=${env}`, query: { env } });
      return (
        <FlagEnvironmentProvider>
          <EnvironmentSwitcher />
          {page}
          <Titles />
        </FlagEnvironmentProvider>
      );
    };
    const { rerender } = renderInAppRoot(at('staging', <p>another page</p>));
    await waitFor(() => expect(screen.getByLabelText('Environment')).toHaveValue('staging'));
    titles.length = 0;

    // Back: the URL names Production, and the list mounts.
    rerender(at('production', <FeatureFlagsPage />));
    await screen.findByTestId('flags-table');
    expect(screen.getByLabelText('Environment')).toHaveValue('production');
    expect(titles.length).toBeGreaterThan(1);
    expect(titles.filter((title) => title.includes('Staging'))).toEqual([]);
    expect(document.title).toBe('Feature Flags · Controlvar');
  });

  it('says so when no listed flag has a setting in Development', async () => {
    install();
    renderList('development');
    await screen.findByTestId('flags-table');
    expect(screen.getByTestId('flags-environment-empty')).toHaveTextContent(
      'No flags configured in Development yet. Configure one here.',
    );
  });

  it("a row's switch changes the flag in Staging through the environment route, never enable", async () => {
    install();
    renderList('staging');
    await screen.findByTestId('flags-table');
    fireEvent.click(within(row('dark_mode')).getByRole('switch', { name: 'Turn dark_mode on in Staging' }));
    await waitFor(() => expect(within(row('dark_mode')).getByTestId('flag-status-pill')).toHaveTextContent('On'));
    const put = mockedApiFetch.mock.calls.find(([, o]) => (o as ApiFetchOptions | undefined)?.method === 'PUT');
    expect(put?.[0]).toBe('/api/v1/feature-flags/f2/environments/staging');
    expect((put?.[1] as ApiFetchOptions).json).toEqual({ is_active: true });
    expect(shapeErrors('FlagEnvironmentUpdate', (put?.[1] as ApiFetchOptions).json)).toEqual([]);
    expect(calls().filter((c) => /\/(enable|disable)$/.test(c))).toEqual([]);
    expect(within(row('dark_mode')).queryByTestId('flag-not-configured')).toBeNull();
  });

  it("a row's switch in Development changes Development, and no other environment", async () => {
    install();
    renderList('development');
    await screen.findByTestId('flags-table');
    fireEvent.click(within(row('dark_mode')).getByRole('switch', { name: 'Turn dark_mode on in Development' }));
    await waitFor(() => expect(within(row('dark_mode')).getByTestId('flag-status-pill')).toHaveTextContent('On'));
    expect(calls().filter((c) => !c.startsWith('GET '))).toEqual(['PUT /api/v1/feature-flags/f2/environments/development']);
  });

  it('a refused Staging switch says so, and the row shows the flag as it was', async () => {
    install({}, FLAGS, [
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/f2/environments/staging',
        handler: () => {
          throw apiError(500, 'Could not update the feature flag.');
        },
      },
    ]);
    renderList('staging');
    await screen.findByTestId('flags-table');
    fireEvent.click(within(row('dark_mode')).getByRole('switch', { name: 'Turn dark_mode on in Staging' }));
    expect(await screen.findByTestId('flag-toggle-error-f2')).toHaveTextContent('Could not update the feature flag.');
    expect(within(row('dark_mode')).getByTestId('flag-status-pill')).toHaveTextContent('Off');
    expect(within(row('dark_mode')).getByTestId('flag-not-configured')).toHaveTextContent('Not configured in Staging');
    expect(within(row('dark_mode')).getByRole('switch', { name: 'Turn dark_mode on in Staging' })).not.toHaveAttribute('aria-disabled');
  });

  it.each(['VIEWER', 'ANALYST'])('%s reads the list in Staging and Development and is offered no change', async (role) => {
    mockAuth.mockReturnValue({
      status: 'authenticated',
      user: { id: 'u1', email: 'u1@example.com', username: 'u1', role, is_superuser: false },
    });
    for (const env of ['staging', 'development']) {
      install();
      const { unmount } = renderList(env);
      await screen.findByTestId('flags-table');
      expect(screen.queryByRole('switch')).toBeNull();
      expect(screen.queryByTestId('new-flag-btn')).toBeNull();
      expect(screen.queryByTestId('flags-created-in-production')).toBeNull();
      unmount();
    }
  });

  it("a row opens the flag in the environment the list shows, from its own link", async () => {
    install();
    renderList('staging');
    await screen.findByTestId('flags-table');
    const link = () => within(row('dark_mode')).getByTestId('flag-link');
    expect(link()).toHaveAttribute('href', '/feature-flags/f2?env=staging');
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'production' } });
    await waitFor(() => expect(link()).toHaveAttribute('href', '/feature-flags/f2?env=production'));
  });

  it('does not say that no flag is configured when the list holds only some of them', async () => {
    install({}, FLAGS, [
      { path: '/api/v1/feature-flags', handler: () => ({ items: FLAGS, total: 120, skip: 0, limit: 100 }) },
    ]);
    renderList('staging');
    await screen.findByTestId('flags-table');
    expect(screen.getByTestId('flag-limit-notice')).toHaveTextContent('Showing 2 of 120 flags.');
    expect(screen.queryByTestId('flags-environment-empty')).toBeNull();
  });

  it("shows what the PUT answered in the row, and does not read the setting again", async () => {
    install({}, FLAGS, [
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/f2/environments/staging',
        // Another user has set 35% there since the list was loaded.
        handler: () =>
          setting('staging', { status: 'active', is_active: true, rollout_percentage: 35, configured: true, updated_at: '2026-11-22T00:00:00Z' }),
      },
    ]);
    renderList('staging');
    await screen.findByTestId('flags-table');
    fireEvent.click(within(row('dark_mode')).getByRole('switch', { name: 'Turn dark_mode on in Staging' }));
    await waitFor(() => expect(row('dark_mode')).toHaveTextContent('35%'));
    expect(within(row('dark_mode')).getByTestId('flag-status-pill')).toHaveTextContent('On');
    expect(calls()).toEqual([
      'GET /api/v1/feature-flags',
      'GET /api/v1/environments/staging/feature-flag-settings?flag_id=f1&flag_id=f2',
      'PUT /api/v1/feature-flags/f2/environments/staging',
    ]);
  });

  it('lists a flag the batch answer leaves out (deleted meanwhile) as off, and not configured', async () => {
    install({}, FLAGS, [
      {
        path: '/api/v1/environments/staging/feature-flag-settings',
        handler: () => ({
          environment: 'staging',
          settings: { f1: setting('staging', { status: 'active', is_active: true, rollout_percentage: 30, configured: true }) },
        }),
      },
    ]);
    renderList('staging');
    await screen.findByTestId('flags-table');
    expect(within(row('checkout_v2')).getByTestId('flag-status-pill')).toHaveTextContent('On');
    expect(within(row('dark_mode')).getByTestId('flag-status-pill')).toHaveTextContent('Off');
    expect(within(row('dark_mode')).getByTestId('flag-not-configured')).toHaveTextContent('Not configured in Staging');
  });

  it("never shows one environment's rows under another's name, at any change of the document", async () => {
    install();
    renderList('staging');
    const table = await screen.findByTestId('flags-table');
    const renamed: string[] = [];
    const observer = new MutationObserver((records) => {
      for (const record of records) renamed.push(`${record.oldValue} rows became ${table.getAttribute('data-environment')}`);
    });
    observer.observe(table, { attributes: true, attributeOldValue: true, attributeFilter: ['data-environment'] });
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'development' } });
    await waitFor(() => expect(screen.getByTestId('flags-table')).toHaveAttribute('data-environment', 'development'));
    expect(renamed).toEqual([]);
    observer.disconnect();
  });

  it('a Staging switch that answers after a move to Development leaves the Development row as it was', async () => {
    const answer = later<FlagEnvironmentSettings>();
    install({}, FLAGS, [
      { method: 'PUT', path: '/api/v1/feature-flags/f2/environments/staging', handler: () => answer.promise },
    ]);
    renderList('staging');
    await screen.findByTestId('flags-table');
    fireEvent.click(within(row('dark_mode')).getByRole('switch', { name: 'Turn dark_mode on in Staging' }));
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'development' } });
    await waitFor(() => expect(screen.getByTestId('flags-table')).toHaveAttribute('data-environment', 'development'));
    await act(async () => {
      answer.give(setting('staging', { status: 'active', is_active: true, rollout_percentage: 35, configured: true }));
    });
    expect(within(row('dark_mode')).getByTestId('flag-status-pill')).toHaveTextContent('Off');
    expect(within(row('dark_mode')).getByTestId('flag-not-configured')).toHaveTextContent('Not configured in Development');
    expect(row('dark_mode')).toHaveTextContent('0%');
    expect(within(row('dark_mode')).getByRole('switch', { name: 'Turn dark_mode on in Development' })).not.toHaveAttribute('aria-disabled');
  });

  it('a refused Staging switch says nothing in Development', async () => {
    const answer = later<FlagEnvironmentSettings>();
    install({}, FLAGS, [
      { method: 'PUT', path: '/api/v1/feature-flags/f2/environments/staging', handler: () => answer.promise },
    ]);
    renderList('staging');
    await screen.findByTestId('flags-table');
    fireEvent.click(within(row('dark_mode')).getByRole('switch', { name: 'Turn dark_mode on in Staging' }));
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'development' } });
    await waitFor(() => expect(screen.getByTestId('flags-table')).toHaveAttribute('data-environment', 'development'));
    await act(async () => answer.refuse(apiError(500, 'Could not update the feature flag.')));
    expect(screen.queryByText('Could not update the feature flag.')).toBeNull();
    expect(within(row('dark_mode')).getByTestId('flag-not-configured')).toHaveTextContent('Not configured in Development');
  });

  it('a refused Production switch says nothing in Staging', async () => {
    const answer = later<unknown>();
    install({}, FLAGS, [
      { method: 'POST', path: '/api/v1/feature-flags/f1/disable', handler: () => answer.promise },
    ]);
    renderList();
    await screen.findByTestId('flags-table');
    fireEvent.click(within(row('checkout_v2')).getByRole('switch', { name: 'Turn checkout_v2 off' }));
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'staging' } });
    await waitFor(() => expect(screen.getByTestId('flags-table')).toHaveAttribute('data-environment', 'staging'));
    await act(async () => answer.refuse(apiError(500, 'Could not toggle the feature flag.')));
    expect(screen.queryByText('Could not toggle the feature flag.')).toBeNull();
    expect(within(row('checkout_v2')).getByTestId('flag-not-configured')).toHaveTextContent('Not configured in Staging');
  });

  it('switching back to Production lists Production again, and its switch uses disable', async () => {
    install();
    renderList('staging');
    await screen.findByTestId('flags-table');
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'production' } });
    await waitFor(() => expect(screen.getByTestId('flags-table')).toHaveAttribute('data-environment', 'production'));
    expect(calls().filter((c) => c.includes('feature-flag-settings'))).toHaveLength(1);
    fireEvent.click(within(row('checkout_v2')).getByRole('switch', { name: 'Turn checkout_v2 off' }));
    await waitFor(() => expect(calls()).toContain('POST /api/v1/feature-flags/f1/disable'));
    expect(calls().filter((c) => c.startsWith('PUT '))).toEqual([]);
  });

  it('lists an archived flag as archived in Staging, with no switch', async () => {
    install({}, [item('f3', 'old_banner', { status: 'archived', is_active: false })]);
    renderList('staging');
    await screen.findByTestId('flags-table');
    expect(within(row('old_banner')).getByTestId('flag-status-pill')).toHaveTextContent('Archived');
    expect(within(row('old_banner')).queryByRole('switch')).toBeNull();
  });
});
