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
 * test that fails on it. So has an answer that arrives after the environment
 * was switched: it may not change the page now on screen.
 *
 * The beta routes' answers are mocked in the API's shapes, which the first
 * test holds to the committed OpenAPI snapshot.
 */
import React from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import { EnvironmentSwitcher } from '@/components/feature-flags/EnvironmentSwitcher';
import { rulesSummary } from '@/components/feature-flags/FlagEnvironmentSections';
import { FlagEnvironmentProvider, flagEnvironmentStorageKey } from '@/contexts/FlagEnvironmentContext';
import { apiFetch, ApiFetchOptions } from '@/services/api';
import { FeatureFlag, FlagEnvironmentSettings, UpdateFlagEnvironmentRequest } from '@/services/featureFlags';
import {
  PRODUCTION_BANNER,
  PRODUCTION_MARKER,
  PRODUCTION_ONLY_NOTE,
} from '@/utils/flagEnvironments';
import { shapeErrors } from '@/tests/helpers/openapiShape';
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

/** The API's `FlagEnvironmentSettings`; by default a flag nobody configured there. */
function settings(environment: string, overrides: Partial<FlagEnvironmentSettings> = {}): FlagEnvironmentSettings {
  return {
    environment,
    status: 'inactive',
    is_active: false,
    rollout_percentage: 0,
    targeting_rules: null,
    split: null,
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
  // What the environment PUT answers: the setting there, with the fields sent changed.
  const changed = (environment: string, body: UpdateFlagEnvironmentRequest): FlagEnvironmentSettings => {
    const before =
      environment === 'staging' ? staging() : environment === 'development' ? development() : settings(environment);
    const on = body.is_active ?? before.is_active;
    return {
      ...before,
      ...body,
      is_active: on,
      status: on ? 'active' : 'inactive',
      configured: true,
      updated_at: '2026-11-22T00:00:00Z',
    };
  };
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
    {
      method: 'PUT',
      path: /^\/api\/v1\/feature-flags\/flag-1\/environments\/[a-z]+$/,
      handler: (p, opts) => changed(p.split('/')[6], opts.json as UpdateFlagEnvironmentRequest),
    },
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

/** The mocked API: `first` answers before the routes every test has. */
function install(o: Overrides = {}, first: Route[] = []) {
  mockedApiFetch.mockImplementation(routedApi([...first, ...routes(o)]) as unknown as typeof apiFetch);
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
/** Everything already answered, the URL change among it, has been read and rendered. */
const settle = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));
/** What the switcher's status says. */
const status = () => screen.getByTestId('environment-announcement').textContent;

/** Every time the document changes: what the status said that the select did not. */
function watchStatus(): { wrong: string[] } {
  const seen: { wrong: string[] } = { wrong: [] };
  new MutationObserver(() => {
    const said = document.querySelector('[data-testid="environment-announcement"]')?.textContent ?? '';
    const chosen = document.querySelector<HTMLSelectElement>('[data-testid="environment-select"]');
    const shown = chosen?.selectedOptions[0]?.textContent ?? '';
    if (said !== '' && said !== `Showing ${shown}.`) seen.wrong.push(`"${said}" while the select is ${shown}`);
    // Never said of a new environment that is loading, or of a load that failed.
    for (const testId of ['flag-loading', 'flag-error', 'flag-not-found']) {
      if (said !== '' && document.querySelector(`[data-testid="${testId}"]`)) seen.wrong.push(`"${said}" beside ${testId}`);
    }
  }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
  return seen;
}

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
// The mocks are what the API sends
// ---------------------------------------------------------------------------

it("mocks the environment routes in the API's own shapes", async () => {
  const api = routedApi(routes());
  const base = '/api/v1/feature-flags/flag-1/environments';
  expect(shapeErrors('FlagEnvironments', await api(base))).toEqual([]);
  for (const environment of ['staging', 'development']) {
    expect(shapeErrors('FlagEnvironmentSettings', await api(`${base}/${environment}`))).toEqual([]);
    const answer = await api(`${base}/${environment}`, { method: 'PUT', json: { rollout_percentage: 10 } });
    expect(shapeErrors('FlagEnvironmentSettings', answer)).toEqual([]);
  }
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
    await waitFor(() => expect(status()).toBe('Showing Staging.'));
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
// The status says the environment on screen, once it is on screen
// ---------------------------------------------------------------------------

describe('the status says the environment on screen', () => {
  // A new element each time, so a re-render reads the router again.
  const page = () => (
    <FlagEnvironmentProvider>
      <EnvironmentSwitcher />
      <FeatureFlagDetailPage />
    </FlagEnvironmentProvider>
  );
  /** The URL as the browser has it now (a choice, or Back): the router re-renders with it. */
  const urlIs = (env: string) => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env } });
  };

  it('says nothing for the first environment shown, and the next only after its data has loaded', async () => {
    const setting = later<FlagEnvironmentSettings>();
    install({}, [{ path: '/api/v1/feature-flags/flag-1/environments/staging', handler: () => setting.promise }]);
    renderPage();
    const seen = watchStatus();
    await screen.findByTestId('flag-detail');
    expect(status()).toBe('');
    fireEvent.change(select(), { target: { value: 'staging' } });
    await screen.findByTestId('flag-loading');
    // The URL change has landed; only the data is missing.
    await settle();
    expect(select().value).toBe('staging');
    expect(status()).toBe('');
    await act(async () => setting.give(STAGING()));
    await screen.findByTestId('flag-detail');
    await waitFor(() => expect(status()).toBe('Showing Staging.'));
    expect(seen.wrong).toEqual([]);
  });

  it('waits for the URL to name the environment, so the route announcer speaks first', async () => {
    const route = later<boolean>();
    install();
    renderPage();
    const seen = watchStatus();
    await screen.findByTestId('flag-detail');
    mockRouter.replace.mockReturnValueOnce(route.promise);
    fireEvent.change(select(), { target: { value: 'staging' } });
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('40%'));
    await settle();
    expect(status()).toBe('');
    await act(async () => route.give(true));
    await waitFor(() => expect(status()).toBe('Showing Staging.'));
    expect(seen.wrong).toEqual([]);
  });

  it('says the environment Back returns to', async () => {
    urlIs('production');
    install();
    const { rerender } = renderInAppRoot(page());
    const seen = watchStatus();
    await screen.findByTestId('flag-detail');
    await choose('staging');
    urlIs('staging');
    rerender(page());
    await waitFor(() => expect(status()).toBe('Showing Staging.'));

    urlIs('production');
    rerender(page());
    await waitFor(() => expect(select().value).toBe('production'));
    expect(status()).not.toBe('Showing Staging.');
    await waitFor(() => expect(status()).toBe('Showing Production.'));
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%');
    expect(seen.wrong).toEqual([]);
  });

  it('says nothing after a load that failed, which is an alert, and the next environment when it loads', async () => {
    install({}, [
      {
        path: '/api/v1/feature-flags/flag-1/environments/staging',
        handler: () => {
          throw apiError(503, 'Service unavailable');
        },
      },
    ]);
    renderPage();
    const seen = watchStatus();
    await screen.findByTestId('flag-detail');
    fireEvent.change(select(), { target: { value: 'staging' } });
    const error = await screen.findByTestId('flag-error');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error).toHaveTextContent('Could not load this feature flag');
    await settle();
    expect(status()).toBe('');
    await choose('production');
    await waitFor(() => expect(status()).toBe('Showing Production.'));
    expect(seen.wrong).toEqual([]);
  });

  it('says nothing after a switch to a flag that is not there, which is an alert', async () => {
    install({}, [
      {
        path: '/api/v1/feature-flags/flag-1/environments/staging',
        handler: () => {
          throw apiError(404, 'Feature flag not found');
        },
      },
    ]);
    renderPage();
    const seen = watchStatus();
    await screen.findByTestId('flag-detail');
    fireEvent.change(select(), { target: { value: 'staging' } });
    const missing = await screen.findByTestId('flag-not-found');
    expect(missing).toHaveAttribute('role', 'alert');
    expect(missing).toHaveTextContent('Feature flag not found');
    expect(within(missing).getByRole('link')).toHaveAttribute('href', '/feature-flags?env=staging');
    await settle();
    expect(status()).toBe('');
    expect(seen.wrong).toEqual([]);
  });

  it('keeps one status on the page for the loading state, from the first render', async () => {
    const setting = later<FlagEnvironmentSettings>();
    install({}, [{ path: '/api/v1/feature-flags/flag-1/environments/staging', handler: () => setting.promise }]);
    renderPage();
    const region = screen.getByTestId('flag-loading').closest('[role="status"]');
    expect(region).not.toBeNull();
    await screen.findByTestId('flag-detail');
    expect(region).toBeInTheDocument();
    expect(region).toBeEmptyDOMElement();
    fireEvent.change(select(), { target: { value: 'staging' } });
    expect(region).toContainElement(screen.getByTestId('flag-loading'));
    expect(screen.queryByTestId('flag-detail')).toBeNull();
    await act(async () => setting.give(STAGING()));
    expect(region).toBeEmptyDOMElement();
    expect(screen.getByTestId('flag-detail')).toBeInTheDocument();
  });

  it('names the environment in the page title, but not for Production', async () => {
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(document.title).toBe('Checkout v2 · Controlvar');
    await choose('staging');
    await waitFor(() => expect(document.title).toBe('Checkout v2 in Staging · Controlvar'));
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

  it.each(['staging', 'development'] as const)('a save in %s goes to that environment and to no other', async (env) => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    await setSlider(61);
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');
    expect(calls().filter((c) => !c.startsWith('GET '))).toEqual([`PUT /api/v1/feature-flags/flag-1/environments/${env}`]);
    expect(bodyOf(`PUT /api/v1/feature-flags/flag-1/environments/${env}`)).toEqual({ rollout_percentage: 61 });
  });

  it('a refused Staging switch says so, and the page shows the flag as it was', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install({}, [
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/flag-1/environments/staging',
        handler: () => {
          throw apiError(500, 'Could not update the feature flag (request ID: 8a5e6c1e).');
        },
      },
    ]);
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('switch', { name: 'Turn checkout_v2 off in Staging' }));
    expect(await screen.findByTestId('flag-toggle-error')).toHaveTextContent(
      'Could not update the feature flag (request ID: 8a5e6c1e).',
    );
    expect(screen.getByTestId('flag-status')).toHaveTextContent('On');
    expect(screen.getByRole('switch', { name: 'Turn checkout_v2 off in Staging' })).not.toHaveAttribute('aria-disabled');
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
    expect(shapeErrors('FlagEnvironmentUpdate', body)).toEqual([]);
    expect(callsTo('PUT /api/v1/feature-flags/flag-1')).toBe(0);
  });

  it('shows what the Staging PUT answered after a save, and does not read the setting again', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install({}, [
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/flag-1/environments/staging',
        // Another user's rules, saved since this page was opened.
        handler: () => ({ ...STAGING(), rollout_percentage: 61, targeting_rules: US_RULES }),
      },
    ]);
    renderPage();
    await screen.findByTestId('flag-detail');
    expect(screen.getByDisplayValue('beta')).toBeInTheDocument();
    await setSlider(60);
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('61%');
    expect(screen.getByDisplayValue('US')).toBeInTheDocument();
    expect(screen.queryByDisplayValue('beta')).toBeNull();
    expect(callsTo('GET /api/v1/feature-flags/flag-1/environments/staging')).toBe(1);
  });

  it("shows what the Staging PUT answered after the switch, and does not read the setting again", async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install({}, [
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/flag-1/environments/staging',
        // Another user has archived the flag since this page was opened.
        handler: () => ({ ...STAGING(), is_active: false, status: 'archived' }),
      },
    ]);
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('switch', { name: 'Turn checkout_v2 off in Staging' }));
    await waitFor(() => expect(screen.getByTestId('flag-status')).toHaveTextContent('Archived'));
    expect(callsTo('GET /api/v1/feature-flags/flag-1/environments/staging')).toBe(1);
  });

  it('a refused Staging save shows the error and changes nothing on screen', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install({}, [
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/flag-1/environments/staging',
        // What the API answers when the change or its audit entry cannot be saved.
        handler: () => {
          throw apiError(500, 'Could not update the feature flag (request ID: 8a5e6c1e).');
        },
      },
    ]);
    renderPage();
    await screen.findByTestId('flag-detail');
    await setSlider(60);
    fireEvent.click(screen.getByTestId('save-flag'));
    expect(await screen.findByTestId('save-error')).toHaveTextContent(
      'Could not update the feature flag (request ID: 8a5e6c1e).',
    );
    expect(screen.queryByTestId('save-success')).toBeNull();
    expect(callsTo('PUT /api/v1/feature-flags/flag-1')).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// A late answer belongs to the environment it was asked in
// ---------------------------------------------------------------------------

describe('a change that answers after the environment was switched', () => {
  const stagingPut = '/api/v1/feature-flags/flag-1/environments/staging';

  it('a Staging save leaves the Production page, and the next Production save, as they were', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    const answer = later<FlagEnvironmentSettings>();
    const o: Overrides = {};
    install(o, [{ method: 'PUT', path: stagingPut, handler: () => answer.promise }]);
    renderPage();
    await screen.findByTestId('flag-detail');
    await setSlider(60);
    fireEvent.click(screen.getByTestId('save-flag'));
    await waitFor(() => expect(callsTo(`PUT ${stagingPut}`)).toBe(1));
    await choose('production');
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%'));

    await act(async () => {
      o.staging = { ...STAGING(), is_active: false, status: 'inactive', rollout_percentage: 60 };
      answer.give(o.staging);
    });
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%');
    expect(screen.getByTestId('flag-status')).toHaveTextContent('On');
    expect(screen.getByDisplayValue('US')).toBeInTheDocument();
    expect(screen.queryByTestId('save-success')).toBeNull();

    fireEvent.click(screen.getByTestId('save-flag'));
    await waitFor(() => expect(callsTo('PUT /api/v1/feature-flags/flag-1')).toBe(1));
    expect(bodyOf('PUT /api/v1/feature-flags/flag-1')).toEqual({ rollout_percentage: 25 });
  });

  it('a refused Staging save says nothing on the Production page', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    const answer = later<FlagEnvironmentSettings>();
    install({}, [{ method: 'PUT', path: stagingPut, handler: () => answer.promise }]);
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByTestId('save-flag'));
    await waitFor(() => expect(callsTo(`PUT ${stagingPut}`)).toBe(1));
    await choose('production');
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%'));
    await act(async () => answer.refuse(apiError(500, 'Could not update the feature flag.')));
    expect(screen.queryByTestId('save-error')).toBeNull();
    expect(screen.getByTestId('save-flag')).not.toHaveAttribute('aria-disabled');
  });

  it('a Staging switch leaves the Production page as it was', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    const answer = later<FlagEnvironmentSettings>();
    const o: Overrides = {};
    install(o, [{ method: 'PUT', path: stagingPut, handler: () => answer.promise }]);
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('switch', { name: 'Turn checkout_v2 off in Staging' }));
    await waitFor(() => expect(callsTo(`PUT ${stagingPut}`)).toBe(1));
    await choose('production');
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%'));
    await act(async () => {
      o.staging = { ...STAGING(), is_active: false, status: 'inactive' };
      answer.give(o.staging);
    });
    expect(screen.getByTestId('flag-status')).toHaveTextContent('On');
    expect(screen.getByRole('switch', { name: 'Turn checkout_v2 off' })).not.toHaveAttribute('aria-disabled');
  });

  it('a Production save leaves the Staging page, and the next Staging save, as they were', async () => {
    const answer = later<FeatureFlag>();
    install({}, [{ method: 'PUT', path: '/api/v1/feature-flags/flag-1', handler: () => answer.promise }]);
    renderPage();
    await screen.findByTestId('flag-detail');
    await setSlider(70);
    fireEvent.click(screen.getByTestId('save-flag'));
    await waitFor(() => expect(callsTo('PUT /api/v1/feature-flags/flag-1')).toBe(1));
    await choose('staging');
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('40%'));

    await act(async () => answer.give(flag({ rollout_percentage: 70 })));
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('40%');
    expect(screen.getByDisplayValue('beta')).toBeInTheDocument();
    expect(screen.queryByTestId('save-success')).toBeNull();

    fireEvent.click(screen.getByTestId('save-flag'));
    await waitFor(() => expect(callsTo(`PUT ${stagingPut}`)).toBe(1));
    expect(bodyOf(`PUT ${stagingPut}`)).toEqual({ rollout_percentage: 40 });
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

  it.each(['VIEWER', 'ANALYST'])('%s reads the flag in Staging and Development and is offered no change', async (role) => {
    signIn(role);
    for (const env of ['staging', 'development']) {
      mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env } });
      install();
      const { unmount } = renderPage();
      await screen.findByTestId('flag-detail');
      expect(select().value).toBe(env);
      expect(screen.getByTestId('flag-role-note')).toBeInTheDocument();
      expect(screen.queryByRole('switch')).toBeNull();
      expect(screen.queryByTestId('save-flag')).toBeNull();
      expect(screen.queryByTestId('add-group')).toBeNull();
      expect(screen.getByTestId('rollout-percentage')).toBeDisabled();
      unmount();
    }
  });

  it('the links back to the list keep the environment on screen', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    install();
    renderPage();
    await screen.findByTestId('flag-detail');
    const back = () => screen.getByRole('link', { name: /Feature Flags/ });
    expect(back()).toHaveAttribute('href', '/feature-flags?env=staging');
    await choose('production');
    expect(back()).toHaveAttribute('href', '/feature-flags?env=production');
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
    expect(screen.getByTestId('shared-settings-note').textContent).toBe(
      "A flag's values, and archiving or deleting it, apply in every environment, so they are done in Production only.",
    );
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

  it('says None of no groups, whatever the API stored beside them', () => {
    expect(rulesSummary({ logical_operator: 'and', groups: [] })).toBe('None');
    expect(rulesSummary({ groups: [], rollout_percentage: 50 })).toBe('None');
  });

  it('says None of rules that are none, in each form the API answers', async () => {
    install({ development: settings('development', { configured: true, targeting_rules: {} }) }, [
      {
        path: '/api/v1/feature-flags/flag-1/environments',
        handler: () => ({
          flag_id: 'flag-1',
          key: 'checkout_v2',
          environments: [
            // What the builder saves when its last rule is removed.
            settings('production', { configured: true, targeting_rules: {} }),
            settings('staging', { configured: true, targeting_rules: { logical_operator: 'AND', groups: [] } }),
            settings('development', { configured: true, targeting_rules: {} }),
          ],
        }),
      },
    ]);
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('button', { name: /Compare environments/ }));
    const table = await screen.findByTestId('environment-comparison-table');
    const rules = Array.from(within(table).getByTestId('compare-rules').querySelectorAll('td')).map((td) => td.textContent);
    expect(rules).toEqual(['None', 'None', 'None']);
  });

  it('keeps the newest answer when an older one arrives after it', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    const [older, newer] = [later<unknown>(), later<unknown>()];
    const answers = [older, newer];
    install({}, [{ path: '/api/v1/feature-flags/flag-1/environments', handler: () => answers.shift()!.promise }]);
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('button', { name: /Compare environments/ }));
    await setSlider(70);
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');
    await waitFor(() => expect(callsTo('GET /api/v1/feature-flags/flag-1/environments')).toBe(2));
    const at = (percentage: number) => ({
      flag_id: 'flag-1',
      key: 'checkout_v2',
      environments: [settings('production'), settings('staging', { rollout_percentage: percentage }), settings('development')],
    });
    await act(async () => newer.give(at(70)));
    await act(async () => older.give(at(40)));
    const rollout = Array.from(screen.getByTestId('compare-rollout').querySelectorAll('td')).map((td) => td.textContent);
    expect(rollout).toEqual(['0%', '70%', '0%']);
  });

  it('does not show an older failure beside the newest answer', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env: 'staging' } });
    const older = later<unknown>();
    let asked = 0;
    const newest = { flag_id: 'flag-1', key: 'checkout_v2', environments: [settings('production'), STAGING(), settings('development')] };
    install({}, [
      { path: '/api/v1/feature-flags/flag-1/environments', handler: () => (asked++ === 0 ? older.promise : newest) },
    ]);
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('button', { name: /Compare environments/ }));
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('environment-comparison-table');
    await act(async () => older.refuse(apiError(503, 'Service unavailable')));
    expect(screen.queryByTestId('environment-comparison-error')).toBeNull();
    expect(screen.getByTestId('environment-comparison-table')).toBeInTheDocument();
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
    install({}, [
      {
        path: '/api/v1/feature-flags/flag-1/environments',
        handler: () => {
          throw apiError(503, 'Service unavailable');
        },
      },
    ]);
    renderPage();
    await screen.findByTestId('flag-detail');
    fireEvent.click(screen.getByRole('button', { name: /Compare environments/ }));
    expect(await screen.findByTestId('environment-comparison-error')).toBeInTheDocument();
    expect(screen.getByTestId('flag-detail')).toBeInTheDocument();
  });
});
