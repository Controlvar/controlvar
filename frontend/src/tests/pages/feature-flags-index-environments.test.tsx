/**
 * The flag list in Production, Staging and Development.
 *
 * Production lists the flags exactly as before environments. Staging and
 * Development list the same flags with their settings there, read from the
 * beta batch route (`GET /api/v1/environments/{environment}/feature-flag-settings`
 * with one `flag_id` per listed flag), and a row's switch changes the flag
 * there through `PUT /api/v1/feature-flags/{id}/environments/{environment}`,
 * never through enable/disable. The batch route's answers are mocked in the
 * shape the plan documents.
 */
import React from 'react';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import { EnvironmentSwitcher } from '@/components/feature-flags/EnvironmentSwitcher';
import { FlagEnvironmentProvider } from '@/contexts/FlagEnvironmentContext';
import { apiFetch, ApiFetchOptions, buildQuery } from '@/services/api';
import { FeatureFlagListItem, FlagEnvironmentSettings } from '@/services/featureFlags';
import { NEW_FLAGS_IN_PRODUCTION, PRODUCTION_BANNER } from '@/utils/flagEnvironments';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import { makeRouter, routedApi, Route } from './helpers/apiMock';

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

function install(batch: Record<string, FlagEnvironmentSettings> = {}, flags = FLAGS) {
  const live: Record<string, FlagEnvironmentSettings> = { ...batch };
  const r: Route[] = [
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
        return {};
      },
    },
    {
      path: /^\/api\/v1\/feature-flags\/[^/]+\/environments\/(staging|development)$/,
      handler: (p) => {
        const [, , , , id, , env] = p.split('/');
        return live[id] ?? setting(env);
      },
    },
    {
      method: 'POST',
      path: /^\/api\/v1\/feature-flags\/[^/]+\/(enable|disable)$/,
      handler: (p) => ({ id: p.split('/')[4], name: 'x', key: 'x', status: p.endsWith('/enable') ? 'active' : 'inactive', updated_at: '2026-11-22T00:00:00Z', audit_log_id: null }),
    },
  ];
  mockedApiFetch.mockImplementation(routedApi(r) as unknown as typeof apiFetch);
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
    expect(calls().filter((c) => /\/(enable|disable)$/.test(c))).toEqual([]);
    expect(within(row('dark_mode')).queryByTestId('flag-not-configured')).toBeNull();
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
