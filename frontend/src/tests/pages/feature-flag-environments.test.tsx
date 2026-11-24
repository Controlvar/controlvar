/**
 * A flag's page in Production, Staging and Development.
 *
 * The routes the page calls are the contract under test:
 *   - Production reads and changes the flag through the stable routes, as
 *     before environments: `GET`/`PUT /api/v1/feature-flags/{id}` and
 *     `POST .../enable|disable`.
 *   - Staging and Development read and change the flag's settings there
 *     through the beta environment routes:
 *     `GET`/`PUT /api/v1/feature-flags/{id}/environments/{environment}`.
 * A save sent to the other family is the failure that edits Production while
 * the user is looking at Staging (or the reverse), so each direction has a
 * test that fails on it.
 *
 * The beta routes' answers are mocked in the shapes the plan documents; the
 * real routes land with the backend write path.
 */
import React from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import { EnvironmentSwitcher } from '@/components/feature-flags/EnvironmentSwitcher';
import { FlagEnvironmentProvider, flagEnvironmentStorageKey } from '@/contexts/FlagEnvironmentContext';
import { apiFetch, ApiFetchOptions } from '@/services/api';
import { FeatureFlag, FlagEnvironmentSettings } from '@/services/featureFlags';
import {
  PRODUCTION_BANNER,
  PRODUCTION_MARKER,
  PRODUCTION_ONLY_NOTE,
  SHARED_SETTINGS_NOTE,
} from '@/utils/flagEnvironments';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import { apiError, makeRouter, routedApi, Route } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

let mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

function signIn(role: string, id = 'u1') {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id, email: `${id}@example.com`, username: id, role, is_superuser: false },
  });
}

const US_RULES = {
  logical_operator: 'AND',
  groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'country', operator: 'equals', value: 'US' }] }],
};
const BETA_RULES = {
  logical_operator: 'AND',
  groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'plan', operator: 'equals', value: 'beta' }] }],
};

/** Production: the flag row, in the detail shape (`status` + `rules`). */
function flag(overrides: Partial<FeatureFlag> = {}): FeatureFlag {
  return {
    id: 'flag-1',
    key: 'checkout_v2',
    name: 'Checkout v2',
    description: 'New checkout flow',
    status: 'active',
    rollout_percentage: 25,
    rules: US_RULES,
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
    ...overrides,
  };
}

/** EnvSettings, as the plan documents it. */
function settings(environment: string, overrides: Partial<FlagEnvironmentSettings> = {}): FlagEnvironmentSettings {
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

const STAGING = () =>
  settings('staging', {
    status: 'active',
    is_active: true,
    rollout_percentage: 40,
    targeting_rules: BETA_RULES,
    configured: true,
    updated_at: '2026-11-20T09:00:00Z',
  });
const DEVELOPMENT = () => settings('development');

interface Overrides {
  flag?: FeatureFlag;
  staging?: FlagEnvironmentSettings;
  development?: FlagEnvironmentSettings;
}

function routes(o: Overrides = {}): Route[] {
  const production = () => o.flag ?? flag();
  const staging = () => o.staging ?? STAGING();
  const development = () => o.development ?? DEVELOPMENT();
  return [
    { path: '/api/v1/feature-flags/flag-1', handler: production },
    { method: 'PUT', path: '/api/v1/feature-flags/flag-1', handler: (_p, opts) => ({ ...production(), ...(opts.json as object) }) },
    {
      method: 'POST',
      path: /^\/api\/v1\/feature-flags\/flag-1\/(enable|disable)$/,
      handler: (p) => ({
        id: 'flag-1', name: 'Checkout v2', key: 'checkout_v2',
        status: p.endsWith('/enable') ? 'active' : 'inactive',
        updated_at: '2026-11-21T00:00:00Z', audit_log_id: null,
      }),
    },
    { path: '/api/v1/feature-flags/flag-1/environments/staging', handler: staging },
    { path: '/api/v1/feature-flags/flag-1/environments/development', handler: development },
    // Any name, production included: a save sent to the wrong route succeeds
    // here, so the test that catches it fails on the route, not on an error.
    { method: 'PUT', path: /^\/api\/v1\/feature-flags\/flag-1\/environments\/[a-z]+$/, handler: () => ({}) },
    {
      path: '/api/v1/feature-flags/flag-1/environments',
      handler: () => ({
        flag_id: 'flag-1',
        key: 'checkout_v2',
        environments: [
          settings('production', { status: 'active', is_active: true, rollout_percentage: 25, targeting_rules: US_RULES, configured: true, updated_at: '2026-09-05T10:00:00Z' }),
          staging(),
          development(),
        ],
      }),
    },
    { path: '/api/v1/rollout-schedules', handler: () => ({ items: [], total: 0, skip: 0, limit: 50 }) },
    {
      path: '/api/v1/safety/feature-flags/flag-1/check',
      handler: () => ({ feature_flag_id: 'flag-1', is_healthy: true, metrics: [], last_checked: '2026-11-21T00:00:00Z' }),
    },
  ];
}

function install(o: Overrides = {}) {
  mockedApiFetch.mockImplementation(routedApi(routes(o)) as unknown as typeof apiFetch);
}

/** Every call made so far, as `METHOD path` (query string dropped). */
function calls(): string[] {
  return mockedApiFetch.mock.calls.map(([p, opts]) => {
    const method = ((opts as ApiFetchOptions | undefined)?.method ?? 'GET').toUpperCase();
    return `${method} ${String(p).split('?')[0]}`;
  });
}
const callsTo = (signature: string) => calls().filter((c) => c === signature).length;
const anyEnvironmentRoute = () => calls().filter((c) => c.includes('/environments'));
const bodyOf = (signature: string) => {
  const found = mockedApiFetch.mock.calls.find(
    ([p, opts]) => `${((opts as ApiFetchOptions | undefined)?.method ?? 'GET').toUpperCase()} ${String(p)}` === signature,
  );
  return (found?.[1] as ApiFetchOptions | undefined)?.json;
};

function renderPage() {
  return renderInAppRoot(
    <FlagEnvironmentProvider>
      <EnvironmentSwitcher />
      <FeatureFlagDetailPage />
    </FlagEnvironmentProvider>,
  );
}

const select = () => screen.getByLabelText('Environment') as HTMLSelectElement;

async function choose(env: string) {
  fireEvent.change(select(), { target: { value: env } });
  await screen.findByTestId('flag-detail');
}

async function setSlider(value: number) {
  fireEvent.change(screen.getByTestId('rollout-percentage'), { target: { value: String(value) } });
  await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent(`${value}%`));
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
  localStorage.clear();
  mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1' } });
  signIn('ADMIN');
});

// ---------------------------------------------------------------------------
// The switcher really switches
// ---------------------------------------------------------------------------

describe('the environment switcher on a flag page', () => {
  it('offers Development, Staging and Production, labelled "Environment", with Production marked in text', async () => {
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(Array.from(select().options).map((o) => o.textContent)).toEqual(['Development', 'Staging', 'Production']);
    expect(select().value).toBe('production');
    expect(screen.getByTestId('production-marker')).toHaveTextContent(PRODUCTION_MARKER);
    await choose('staging');
    expect(screen.queryByTestId('production-marker')).toBeNull();
    expect(screen.getByTestId('environment-announcement')).toHaveTextContent('Showing Staging.');
  });

  it("puts the environment in the page's fetch URL, and fetches again on every switch", async () => {
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    // Production: the flag's own route, and no environment route at all.
    expect(callsTo('GET /api/v1/feature-flags/flag-1')).toBe(1);
    expect(anyEnvironmentRoute()).toEqual([]);
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%');

    // The URL first: a switcher that changes only its label fails here.
    await choose('staging');
    await waitFor(() => expect(callsTo('GET /api/v1/feature-flags/flag-1/environments/staging')).toBe(1));
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('40%'));

    await choose('development');
    await waitFor(() => expect(callsTo('GET /api/v1/feature-flags/flag-1/environments/development')).toBe(1));
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('0%'));

    await choose('production');
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%'));
    expect(callsTo('GET /api/v1/feature-flags/flag-1')).toBe(4);
    expect(callsTo('GET /api/v1/feature-flags/flag-1/environments/staging')).toBe(1);
  });

  it('keeps the choice for the user and writes it into the URL', async () => {
    install();
    const first = renderPage();
    await screen.findByTestId('flag-detail');
    await choose('staging');
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('40%'));
    expect(localStorage.getItem(flagEnvironmentStorageKey('u1'))).toBe('staging');
    expect(mockRouter.replace).toHaveBeenLastCalledWith(
      { pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } },
      undefined,
      { shallow: true, scroll: false },
    );
    first.unmount();

    // A new visit with no ?env= opens the stored choice.
    mockedApiFetch.mockClear();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(select().value).toBe('staging');
    expect(callsTo('GET /api/v1/feature-flags/flag-1/environments/staging')).toBe(1);
  });

  it("keeps one user's choice from another user", async () => {
    localStorage.setItem(flagEnvironmentStorageKey('u1'), 'staging');
    signIn('ADMIN', 'u2');
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(select().value).toBe('production');
    expect(anyEnvironmentRoute()).toEqual([]);
  });

  it('lets ?env= win over the stored choice', async () => {
    localStorage.setItem(flagEnvironmentStorageKey('u1'), 'development');
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(select().value).toBe('staging');
    expect(callsTo('GET /api/v1/feature-flags/flag-1/environments/staging')).toBe(1);
    expect(callsTo('GET /api/v1/feature-flags/flag-1/environments/development')).toBe(0);
    // Following a link does not change what is stored.
    expect(localStorage.getItem(flagEnvironmentStorageKey('u1'))).toBe('development');
  });

  it('ignores an ?env= that is not an environment name', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'prod' } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(select().value).toBe('production');
    expect(anyEnvironmentRoute()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// A save goes to the route of the environment on screen
// ---------------------------------------------------------------------------

describe('a save goes to the route of the environment on screen', () => {
  it('a Staging save goes to the Staging route, and never to the stable PUT', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    await setSlider(60);
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');

    expect(callsTo('PUT /api/v1/feature-flags/flag-1/environments/staging')).toBe(1);
    expect(bodyOf('PUT /api/v1/feature-flags/flag-1/environments/staging')).toEqual({ rollout_percentage: 60 });
    expect(callsTo('PUT /api/v1/feature-flags/flag-1')).toBe(0);
    expect(calls().filter((c) => /\/(enable|disable)$/.test(c))).toEqual([]);
  });

  it('a Production save goes to the stable PUT, and never to an environment route', async () => {
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    await setSlider(60);
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');

    expect(callsTo('PUT /api/v1/feature-flags/flag-1')).toBe(1);
    expect(bodyOf('PUT /api/v1/feature-flags/flag-1')).toEqual({ rollout_percentage: 60 });
    expect(anyEnvironmentRoute()).toEqual([]);
  });

  it('turning a flag on in Development goes to the Development route, not to enable', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'development' } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('switch', { name: 'Turn checkout_v2 on in Development' }));
    await waitFor(() => expect(callsTo('PUT /api/v1/feature-flags/flag-1/environments/development')).toBe(1));
    expect(bodyOf('PUT /api/v1/feature-flags/flag-1/environments/development')).toEqual({ is_active: true });
    expect(calls().filter((c) => /\/(enable|disable)$/.test(c))).toEqual([]);
    expect(callsTo('PUT /api/v1/feature-flags/flag-1')).toBe(0);
  });

  it('turning a flag off in Production goes to disable, not to an environment route', async () => {
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('switch', { name: 'Turn checkout_v2 off' }));
    await waitFor(() => expect(callsTo('POST /api/v1/feature-flags/flag-1/disable')).toBe(1));
    expect(anyEnvironmentRoute()).toEqual([]);
  });

  it('a Staging save of changed rules sends them to the Staging route only', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    const value = screen.getByDisplayValue('beta');
    fireEvent.change(value, { target: { value: 'pro' } });
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');
    const body = bodyOf('PUT /api/v1/feature-flags/flag-1/environments/staging') as Record<string, unknown>;
    expect(body.rollout_percentage).toBe(40);
    expect(JSON.stringify(body.targeting_rules)).toContain('"pro"');
    expect(callsTo('PUT /api/v1/feature-flags/flag-1')).toBe(0);
  });

  it('a refused Staging save shows the error and changes nothing on screen', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    const base = routes();
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          method: 'PUT',
          path: '/api/v1/feature-flags/flag-1/environments/staging',
          handler: () => {
            throw apiError(500, 'The change was not saved: its audit entry could not be written.');
          },
        },
        ...base,
      ]) as unknown as typeof apiFetch,
    );
    renderPage();
    await screen.findByTestId('flag-detail');
    await setSlider(60);
    fireEvent.click(screen.getByTestId('save-flag'));
    expect(await screen.findByTestId('save-error')).toHaveTextContent('could not be written');
    expect(screen.queryByTestId('save-success')).toBeNull();
    expect(callsTo('PUT /api/v1/feature-flags/flag-1')).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// What each environment shows
// ---------------------------------------------------------------------------

describe('the page in each environment', () => {
  it('Production keeps its controls and says what a change reaches', async () => {
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(screen.getByTestId('production-banner')).toHaveTextContent(PRODUCTION_BANNER);
    expect(screen.getByTestId('rollout-schedule-section')).toBeInTheDocument();
    expect(screen.getByTestId('safety-section')).toBeInTheDocument();
    expect(screen.queryByTestId('production-only-note')).toBeNull();
    expect(screen.getByTestId('save-flag')).toHaveTextContent('Save Changes');
  });

  it('a user who cannot change flags gets no "editing" banner in Production', async () => {
    signIn('VIEWER');
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(screen.queryByTestId('production-banner')).toBeNull();
  });

  it("Staging shows Staging's settings, the production-only line, and no schedule, safety or manage controls", async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(screen.getByTestId('flag-status')).toHaveTextContent('On');
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('40%');
    expect(screen.getByDisplayValue('beta')).toBeInTheDocument();
    expect(screen.getByTestId('production-only-note')).toHaveTextContent(PRODUCTION_ONLY_NOTE);
    expect(screen.getByTestId('shared-settings-note')).toHaveTextContent(SHARED_SETTINGS_NOTE);
    expect(screen.queryByTestId('production-banner')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-section')).toBeNull();
    expect(screen.queryByTestId('safety-section')).toBeNull();
    expect(screen.queryByRole('button', { name: /archive/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Rollout in Staging' })).toBeInTheDocument();
    expect(screen.getByTestId('save-flag')).toHaveTextContent('Save changes in Staging');
    // Neither Production-only panel is even asked for.
    expect(calls().filter((c) => c.includes('/rollout-schedules') || c.includes('/safety/'))).toEqual([]);
  });

  it('a flag with no setting in Development is "Off", "Not configured in Development"', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'development' } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(screen.getByTestId('flag-status')).toHaveTextContent('Off');
    expect(screen.getByTestId('flag-not-configured')).toHaveTextContent('Not configured in Development');
    expect(screen.getByRole('switch', { name: 'Turn checkout_v2 on in Development' })).toBeInTheDocument();
  });

  it('an archived flag is archived in every environment, with no switch', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install({ flag: flag({ status: 'archived' }), staging: settings('staging', { status: 'archived', configured: true }) });
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(screen.getByTestId('flag-status')).toHaveTextContent('Archived');
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.getByTestId('flag-archived-note')).toBeInTheDocument();
  });

  it("a value flag's values are shown, not edited, outside Production", async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install({ flag: flag({ value_type: 'string', on_value: 'blue', off_value: 'grey' }) });
    renderPage();
    await screen.findByTestId('flag-detail');
    const section = screen.getByTestId('flag-values-section');
    expect(within(section).queryByRole('textbox')).toBeNull();
    expect(section).toHaveTextContent('blue');
    await setSlider(55);
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');
    expect(bodyOf('PUT /api/v1/feature-flags/flag-1/environments/staging')).toEqual({ rollout_percentage: 55 });
  });

  it('has no axe violations in Staging with the comparison open', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install();
    const { container } = renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('button', { name: /Compare environments/ }));
    await screen.findByTestId('environment-comparison-table');
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Compare environments
// ---------------------------------------------------------------------------

describe('Compare environments', () => {
  it('shows the flag in all three environments side by side, from the environments route', async () => {
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    const toggle = screen.getByRole('button', { name: /Compare environments/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(callsTo('GET /api/v1/feature-flags/flag-1/environments')).toBe(0);

    fireEvent.click(toggle);
    const table = await screen.findByTestId('environment-comparison-table');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(callsTo('GET /api/v1/feature-flags/flag-1/environments')).toBe(1);
    const headers = within(table).getAllByRole('columnheader').map((h) => h.textContent);
    expect(headers).toEqual(['Setting', 'Development', 'Staging', 'Production']);
    const row = (testId: string) =>
      Array.from(within(table).getByTestId(testId).querySelectorAll('td')).map((td) => td.textContent);
    expect(row('compare-status')).toEqual(['OffNot configured', 'On', 'On']);
    expect(row('compare-rollout')).toEqual(['0%', '40%', '25%']);
    expect(row('compare-rules')).toEqual(['None', '1 condition in 1 group', '1 condition in 1 group']);
    // Scrolled sideways on a phone, so the keyboard can reach it.
    const region = screen.getByRole('region', { name: 'checkout_v2 in each environment' });
    expect(region).toHaveAttribute('tabindex', '0');
    expect(region).toContainElement(table);
  });

  it('loads again after a change made on the page', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('button', { name: /Compare environments/ }));
    await screen.findByTestId('environment-comparison-table');
    expect(callsTo('GET /api/v1/feature-flags/flag-1/environments')).toBe(1);
    await setSlider(70);
    await act(async () => {
      fireEvent.click(screen.getByTestId('save-flag'));
    });
    await screen.findByTestId('save-success');
    await waitFor(() => expect(callsTo('GET /api/v1/feature-flags/flag-1/environments')).toBe(2));
  });

  it('keeps a failure inside the section', async () => {
    const base = routes();
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/feature-flags/flag-1/environments',
          handler: () => {
            throw apiError(503, 'Service unavailable');
          },
        },
        ...base,
      ]) as unknown as typeof apiFetch,
    );
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('button', { name: /Compare environments/ }));
    expect(await screen.findByTestId('environment-comparison-error')).toBeInTheDocument();
    expect(screen.getByTestId('flag-detail')).toBeInTheDocument();
  });
});
