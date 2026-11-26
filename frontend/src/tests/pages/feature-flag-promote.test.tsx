/**
 * Promoting a flag: "Promote to Staging" from Development, "Promote to
 * Production" from Staging.
 *
 * What these tests hold:
 *   - the dialog names every field that changes (status, rollout percentage,
 *     targeting rules), each from what the target has now to what it will have,
 *     and an unconfigured source shows the target turning off;
 *   - nothing is sent before the confirming button, and the confirming button
 *     sends exactly one request, `POST .../environments/{target}/promote` with
 *     `{from}`;
 *   - the target changed and the source and the third environment did not
 *     (the mocked API keeps the three in one `world`, and every test reads it
 *     back);
 *   - only a user who may change the flag is offered it, never for Production
 *     as the source, never for an archived flag;
 *   - a refusal shows as text in the dialog, which stays open, with nothing
 *     saved; an answer that arrives after the environment was switched, or
 *     after the dialog was closed, lands nowhere.
 */
import React from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import { EnvironmentSwitcher } from '@/components/feature-flags/EnvironmentSwitcher';
import { FlagEnvironmentProvider } from '@/contexts/FlagEnvironmentContext';
import { apiFetch, ApiError, ApiFetchOptions } from '@/services/api';
import { FeatureFlag, FlagEnvironmentSettings } from '@/services/featureFlags';
import { describeRules, promotionRows, promotionTarget, scheduleNote } from '@/utils/flagPromotion';
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

let mockDemo: { account: null } | null = null;
jest.mock('@/contexts/AuthConfigContext', () => ({
  ...jest.requireActual('@/contexts/AuthConfigContext'),
  useDemoMode: () => mockDemo,
}));

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

const rulesOn = (attribute: string, value: string) => ({
  logical_operator: 'AND',
  groups: [{ logical_operator: 'AND', conditions: [{ attribute, operator: 'equals', value }] }],
});
const US = rulesOn('country', 'US');
const BETA = rulesOn('plan', 'beta');
const ALPHA = rulesOn('plan', 'alpha');

type Name = 'production' | 'staging' | 'development';

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
const on = (environment: string, rollout: number, rules: unknown): FlagEnvironmentSettings =>
  settings(environment, {
    status: 'active',
    is_active: true,
    rollout_percentage: rollout,
    targeting_rules: rules as FlagEnvironmentSettings['targeting_rules'],
    configured: true,
    updated_at: '2026-11-20T09:00:00Z',
  });

/** The three environments in one place: what the mocked API holds. */
interface World {
  production: FlagEnvironmentSettings;
  staging: FlagEnvironmentSettings;
  development: FlagEnvironmentSettings;
  archived: boolean;
  schedules: Array<{ id: string; status: string; updated_at: string }>;
}

let world: World;
function freshWorld(): World {
  return {
    production: on('production', 25, US),
    staging: on('staging', 40, BETA),
    development: on('development', 10, ALPHA),
    archived: false,
    schedules: [],
  };
}

const productionFlag = (): FeatureFlag => ({
  id: 'flag-1',
  key: 'checkout_v2',
  name: 'Checkout v2',
  description: 'New checkout flow',
  status: world.archived ? 'archived' : world.production.status,
  rollout_percentage: world.production.rollout_percentage,
  rules: world.production.targeting_rules as FeatureFlag['rules'],
  owner_id: 'user-1',
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-05T10:00:00Z',
});

const view = (name: Name): FlagEnvironmentSettings =>
  world.archived ? { ...world[name], status: 'archived', is_active: false } : world[name];

const PROMOTE = /^\/api\/v1\/feature-flags\/flag-1\/environments\/(production|staging|development)\/promote$/;

function routes(): Route[] {
  return [
    { path: '/api/v1/feature-flags/flag-1', handler: productionFlag },
    { path: '/api/v1/feature-flags/flag-1/environments/staging', handler: () => view('staging') },
    { path: '/api/v1/feature-flags/flag-1/environments/development', handler: () => view('development') },
    {
      path: '/api/v1/feature-flags/flag-1/environments',
      handler: () => ({
        flag_id: 'flag-1',
        key: 'checkout_v2',
        environments: [view('production'), view('staging'), view('development')],
      }),
    },
    {
      method: 'POST',
      path: PROMOTE,
      handler: (p, opts) => {
        const target = p.split('/')[6] as Name;
        const from = world[(opts.json as { from: Name }).from];
        // What the API copies: the status, the rollout percentage and the rules. Nothing else.
        world[target] = {
          ...world[target],
          status: from.is_active ? 'active' : 'inactive',
          is_active: from.is_active,
          rollout_percentage: from.rollout_percentage,
          targeting_rules: from.targeting_rules,
          configured: true,
          environment: target,
          updated_at: '2026-11-24T10:00:00Z',
        };
        return world[target];
      },
    },
    {
      path: '/api/v1/rollout-schedules',
      handler: () => ({ items: world.schedules, total: world.schedules.length, skip: 0, limit: 50 }),
    },
    {
      path: '/api/v1/safety/feature-flags/flag-1/check',
      handler: () => ({ feature_flag_id: 'flag-1', is_healthy: true, metrics: [], last_checked: '2026-11-24T00:00:00Z' }),
    },
  ];
}

function install(first: Route[] = []) {
  mockedApiFetch.mockImplementation(routedApi([...first, ...routes()]) as unknown as typeof apiFetch);
}

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
    const method = ((opts as ApiFetchOptions | undefined)?.method ?? 'GET').toUpperCase();
    return `${method} ${String(p).split('?')[0]}`;
  });
}
const callsTo = (signature: string) => calls().filter((c) => c === signature).length;
const promoteCalls = () => calls().filter((c) => c.startsWith('POST') && c.endsWith('/promote'));
const bodyOf = (signature: string) => {
  const found = mockedApiFetch.mock.calls.find(
    ([p, opts]) => `${((opts as ApiFetchOptions | undefined)?.method ?? 'GET').toUpperCase()} ${String(p)}` === signature,
  );
  return (found?.[1] as ApiFetchOptions | undefined)?.json;
};
const STAGING_PROMOTE = 'POST /api/v1/feature-flags/flag-1/environments/staging/promote';
const PRODUCTION_PROMOTE = 'POST /api/v1/feature-flags/flag-1/environments/production/promote';
const ENVIRONMENTS_READ = 'GET /api/v1/feature-flags/flag-1/environments';

function renderPage() {
  return renderInAppRoot(
    <FlagEnvironmentProvider>
      <EnvironmentSwitcher />
      <FeatureFlagDetailPage />
    </FlagEnvironmentProvider>,
  );
}

async function openIn(env: Name) {
  mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env } });
  renderPage();
  await screen.findByTestId('flag-detail');
}

/** Click the promote button and wait for the dialog to show the diff. */
async function openDialog() {
  fireEvent.click(screen.getByTestId('promote-button'));
  await screen.findByTestId('promote-diff');
}

const row = (field: string) => screen.getByTestId(`promote-row-${field}`);
const text = (testId: string) => screen.getByTestId(testId).textContent;

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
  localStorage.clear();
  world = freshWorld();
  mockDemo = null;
  signIn('ADMIN');
});

// ---------------------------------------------------------------------------
// The mocks are what the API sends and accepts
// ---------------------------------------------------------------------------

it("sends the API's own request body and is answered in the API's own shape", async () => {
  install();
  const api = routedApi(routes());
  const answer = await api('/api/v1/feature-flags/flag-1/environments/staging/promote', {
    method: 'POST',
    json: { from: 'development' },
  });
  expect(shapeErrors('FlagEnvironmentSettings', answer)).toEqual([]);
  world = freshWorld();

  await openIn('development');
  await openDialog();
  fireEvent.click(screen.getByTestId('promote-confirm'));
  await screen.findByText(/^Promoted Development to Staging/);
  expect(shapeErrors('FlagPromotion', bodyOf(STAGING_PROMOTE))).toEqual([]);
  // A body the API refuses is caught by the same check.
  expect(shapeErrors('FlagPromotion', { from: 'development', value_type: 'string' })).not.toEqual([]);
});

// ---------------------------------------------------------------------------
// Where the control is
// ---------------------------------------------------------------------------

describe('who is offered it, and where', () => {
  it('Development offers "Promote to Staging"', async () => {
    install();
    await openIn('development');
    expect(screen.getByTestId('promote-button')).toHaveTextContent('Promote to Staging');
    expect(promoteCalls()).toEqual([]);
  });

  it('Staging offers "Promote to Production"', async () => {
    install();
    await openIn('staging');
    expect(screen.getByTestId('promote-button')).toHaveTextContent('Promote to Production');
  });

  it('Production offers nothing', async () => {
    install();
    await openIn('production');
    expect(screen.queryByTestId('promote-button')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Promote/ })).toBeNull();
  });

  it.each(['VIEWER', 'ANALYST'])('a %s is offered no control, in either environment', async (role) => {
    signIn(role);
    install();
    await openIn('development');
    expect(screen.queryByTestId('promote-button')).toBeNull();
    expect(screen.getByTestId('flag-role-note')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'staging' } });
    await screen.findByTestId('flag-detail');
    expect(screen.queryByTestId('promote-button')).toBeNull();
  });

  it('a developer is offered it', async () => {
    signIn('DEVELOPER');
    install();
    await openIn('development');
    expect(screen.getByTestId('promote-button')).toBeInTheDocument();
  });

  it('an archived flag is offered none: it is archived in every environment', async () => {
    world.archived = true;
    install();
    await openIn('development');
    expect(screen.getByTestId('flag-status')).toHaveTextContent('Archived');
    expect(screen.queryByTestId('promote-button')).toBeNull();
  });

  it('the read-only demo is offered none, in either environment', async () => {
    mockDemo = { account: null };
    install();
    await openIn('development');
    expect(screen.queryByTestId('promote-button')).toBeNull();
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'staging' } });
    await screen.findByTestId('flag-detail');
    expect(screen.queryByTestId('promote-button')).toBeNull();
    // The same user outside the demo is offered it.
    mockDemo = null;
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'development' } });
    await screen.findByTestId('flag-detail');
    expect(screen.getByTestId('promote-button')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The diff names every field that changes
// ---------------------------------------------------------------------------

describe('the diff', () => {
  it('names the status, the rollout percentage and the rules, each from what Staging has to what it will have', async () => {
    world.development = on('development', 10, ALPHA);
    world.staging = settings('staging', {
      status: 'inactive',
      rollout_percentage: 40,
      targeting_rules: BETA,
      configured: true,
    });
    install();
    await openIn('development');
    await openDialog();
    expect(screen.getByRole('dialog', { name: 'Promote to Staging' })).toBeInTheDocument();
    expect(text('promote-now-status')).toBe('Off');
    expect(text('promote-after-status')).toBe('On');
    expect(text('promote-change-status')).toBe('Changes');
    expect(text('promote-now-rollout')).toBe('40%');
    expect(text('promote-after-rollout')).toBe('10%');
    expect(text('promote-change-rollout')).toBe('Changes');
    expect(text('promote-now-rules')).toContain('plan equals beta');
    expect(text('promote-after-rules')).toContain('plan equals alpha');
    expect(text('promote-change-rules')).toBe('Changes');
    // The three fields the API copies are all that is listed, in this order.
    expect(
      Array.from(screen.getByTestId('promote-diff').querySelectorAll('tbody th')).map((th) => th.textContent),
    ).toEqual(['Status', 'Rollout percentage', 'Targeting rules']);
  });

  // The field that is the only one to differ is the only one that changes, and
  // each unchanged one says so in words, never by being left out.
  it.each([
    ['status', { status: 'inactive', is_active: false }],
    ['rollout', { rollout_percentage: 77 }],
    ['rules', { targeting_rules: BETA }],
  ] as const)('names %s when it is the only field that differs', async (field, differs) => {
    world.development = on('development', 10, ALPHA);
    world.staging = { ...on('staging', 10, ALPHA), ...differs } as FlagEnvironmentSettings;
    install();
    await openIn('development');
    await openDialog();
    for (const name of ['status', 'rollout', 'rules']) {
      expect(text(`promote-change-${name}`)).toBe(name === field ? 'Changes' : 'No change');
      expect(row(name)).toHaveAttribute('data-changes', name === field ? 'yes' : 'no');
    }
  });

  it('names the rules by their conditions, so a changed value is a change', async () => {
    world.development = on('development', 10, rulesOn('country', 'CA'));
    world.staging = on('staging', 10, rulesOn('country', 'US'));
    install();
    await openIn('development');
    await openDialog();
    expect(text('promote-change-rules')).toBe('Changes');
    expect(text('promote-now-rules')).toContain('country equals US');
    expect(text('promote-after-rules')).toContain('country equals CA');
  });

  it('does not call rules with the same keys in another order a change', async () => {
    world.development = on('development', 10, { groups: US.groups, logical_operator: 'AND' });
    world.staging = on('staging', 10, US);
    install();
    await openIn('development');
    await openDialog();
    expect(text('promote-change-rules')).toBe('No change');
    expect(screen.getByTestId('promote-no-change')).toHaveTextContent('Staging already has these settings');
  });

  it.each([
    ['draft', 'a draft'],
    ['inactive', 'an inactive'],
  ])('counts %s as Off, as the API does: only an active flag is On', async (status) => {
    world.development = { ...on('development', 10, ALPHA), status, is_active: false };
    world.staging = { ...on('staging', 10, ALPHA), status, is_active: false };
    install();
    await openIn('development');
    await openDialog();
    expect(text('promote-now-status')).toBe('Off');
    expect(text('promote-after-status')).toBe('Off');
    expect(text('promote-change-status')).toBe('No change');
    // And a draft source turns an active target off.
    fireEvent.click(screen.getByTestId('promote-cancel'));
    world.staging = on('staging', 10, ALPHA);
    fireEvent.click(screen.getByTestId('promote-button'));
    await waitFor(() => expect(text('promote-now-status')).toBe('On'));
    expect(text('promote-after-status')).toBe('Off');
    expect(text('promote-change-status')).toBe('Changes');
  });

  it.each([
    ['no groups', { logical_operator: 'AND', groups: [] }],
    ['an empty object', {}],
    ['no groups and no operator', { groups: [] }],
  ])('rules stored as %s and no rules are the same "None": No change', async (_name, stored) => {
    world.development = on('development', 10, stored);
    world.staging = on('staging', 10, null);
    install();
    await openIn('development');
    await openDialog();
    expect(text('promote-after-rules')).toBe('None');
    expect(text('promote-now-rules')).toBe('None');
    expect(text('promote-change-rules')).toBe('No change');
    expect(screen.getByTestId('promote-no-change')).toBeInTheDocument();
  });

  it('says what is not copied', async () => {
    install();
    await openIn('development');
    await openDialog();
    expect(text('promote-not-copied')).toBe(
      "Not copied: the flag's rollout schedules and safety settings, and its type and values. They stay as they are.",
    );
    expect(text('promote-summary')).toBe(
      "Copies Development's status, rollout percentage and targeting rules into Staging.",
    );
  });

  describe('an unconfigured source', () => {
    it('shows Staging turning off, and the diff says Off, 0% and no rules', async () => {
      world.development = settings('development');
      world.staging = on('staging', 40, BETA);
      install();
      await openIn('development');
      await openDialog();
      expect(text('promote-source-empty')).toBe('Nothing is configured in Development, so Staging will be turned off.');
      expect(text('promote-now-status')).toBe('On');
      expect(text('promote-after-status')).toBe('Off');
      expect(text('promote-change-status')).toBe('Changes');
      expect(text('promote-after-rollout')).toBe('0%');
      expect(text('promote-after-rules')).toBe('None');
    });

    it('turns Staging off when confirmed, and leaves Development unconfigured', async () => {
      world.development = settings('development');
      world.staging = on('staging', 40, BETA);
      install();
      await openIn('development');
      await openDialog();
      fireEvent.click(screen.getByTestId('promote-confirm'));
      await screen.findByText(/^Promoted Development to Staging/);
      expect(world.staging.is_active).toBe(false);
      expect(world.staging.status).toBe('inactive');
      expect(world.staging.rollout_percentage).toBe(0);
      expect(world.development.configured).toBe(false);
      expect(screen.getByTestId('promote-result')).toHaveTextContent('Staging was set to Off at 0%.');
    });

    it('says "stays off" when Staging is off already, and does not claim it is turned off', async () => {
      world.development = settings('development');
      world.staging = settings('staging', { rollout_percentage: 30, configured: true });
      install();
      await openIn('development');
      await openDialog();
      expect(text('promote-source-empty')).toBe('Nothing is configured in Development, so Staging stays off.');
      expect(text('promote-change-status')).toBe('No change');
      expect(text('promote-change-rollout')).toBe('Changes');
    });

    it('says the same for Production as the target, from an unconfigured Staging', async () => {
      world.staging = settings('staging');
      install();
      await openIn('staging');
      await openDialog();
      expect(text('promote-source-empty')).toBe('Nothing is configured in Staging, so Production will be turned off.');
      expect(text('promote-after-status')).toBe('Off');
    });
  });

  describe("Production's rollout schedule", () => {
    it('is named when Production has one, as not copied and not changed', async () => {
      world.schedules = [{ id: 's1', status: 'active', updated_at: '2026-11-20T00:00:00Z' }];
      install();
      await openIn('staging');
      await openDialog();
      expect(text('promote-schedule-note')).toBe(
        'Production has a rollout schedule (Active). Promotion does not copy or change its progress: the next stage that starts sets the rollout percentage again.',
      );
      expect(callsTo('GET /api/v1/rollout-schedules')).toBe(1);
    });

    it.each([
      ['paused', 'Paused'],
      ['draft', 'Draft'],
    ])('names a %s schedule as it is, not as active', async (status, shown) => {
      world.schedules = [{ id: 's1', status, updated_at: '2026-11-20T00:00:00Z' }];
      install();
      await openIn('staging');
      await openDialog();
      expect(text('promote-schedule-note')).toContain(`Production has a rollout schedule (${shown}).`);
    });

    it('names the most open of several: active before paused before draft', async () => {
      world.schedules = [
        { id: 's1', status: 'draft', updated_at: '2026-11-20T00:00:00Z' },
        { id: 's2', status: 'paused', updated_at: '2026-11-20T00:00:00Z' },
      ];
      install();
      await openIn('staging');
      await openDialog();
      expect(text('promote-schedule-note')).toContain('(Paused)');
    });

    it.each(['completed', 'cancelled'])('says nothing of a %s schedule, which has no progress to move', async (status) => {
      world.schedules = [{ id: 's1', status, updated_at: '2026-11-20T00:00:00Z' }];
      install();
      await openIn('staging');
      await openDialog();
      expect(screen.queryByTestId('promote-schedule-note')).toBeNull();
    });

    it('is not mentioned when Production has none, and not read for Staging as the target', async () => {
      install();
      await openIn('staging');
      await openDialog();
      expect(screen.queryByTestId('promote-schedule-note')).toBeNull();
      mockedApiFetch.mockClear();
      fireEvent.click(screen.getByTestId('promote-cancel'));
      fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'development' } });
      await screen.findByTestId('flag-detail');
      await openDialog();
      expect(screen.queryByTestId('promote-schedule-note')).toBeNull();
      expect(callsTo('GET /api/v1/rollout-schedules')).toBe(0);
    });

    it('says so when it could not be read, and still allows the promotion', async () => {
      install([
        {
          path: '/api/v1/rollout-schedules',
          handler: () => {
            throw apiError(503, 'Service unavailable');
          },
        },
      ]);
      await openIn('staging');
      await openDialog();
      expect(text('promote-schedule-note')).toContain("Production's rollout schedule could not be read.");
      expect(screen.getByTestId('promote-continue')).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Confirming
// ---------------------------------------------------------------------------

describe('confirming', () => {
  it('sends nothing before the confirming button, and exactly one request after it', async () => {
    install();
    await openIn('development');
    await openDialog();
    expect(promoteCalls()).toEqual([]);
    // Reading both environments is the dialog's one read.
    expect(callsTo(ENVIRONMENTS_READ)).toBe(1);

    fireEvent.click(screen.getByTestId('promote-confirm'));
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await screen.findByText(/^Promoted Development to Staging/);
    expect(promoteCalls()).toEqual([STAGING_PROMOTE]);
    expect(bodyOf(STAGING_PROMOTE)).toEqual({ from: 'development' });
  });

  it('changes Staging and leaves Development and Production as they were', async () => {
    const before = freshWorld();
    install();
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await screen.findByText(/^Promoted Development to Staging/);
    expect(world.staging).toMatchObject({ is_active: true, rollout_percentage: 10, targeting_rules: ALPHA });
    expect(world.staging.rollout_percentage).not.toBe(before.staging.rollout_percentage);
    expect(world.development).toEqual(before.development);
    expect(world.production).toEqual(before.production);
    // The page on screen is Development's, and nothing but the promote route was written.
    expect(calls().filter((c) => c.startsWith('PUT') || /\/(enable|disable)$/.test(c))).toEqual([]);
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('10%');
  });

  it('closes, announces the result, and gives focus back to the button', async () => {
    install();
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await screen.findByText(/^Promoted Development to Staging/);
    expect(screen.queryByTestId('promote-dialog')).toBeNull();
    expect(screen.getByTestId('promote-result')).toHaveAttribute('role', 'status');
    expect(screen.getByTestId('promote-result')).toHaveTextContent('Staging was set to On at 10%.');
    await waitFor(() => expect(screen.getByTestId('promote-button')).toHaveFocus());
  });

  it('shows the new Staging through the comparison, which reloads, and "Show Staging" opens it', async () => {
    install();
    await openIn('development');
    fireEvent.click(screen.getByRole('button', { name: /Compare environments/ }));
    await screen.findByTestId('environment-comparison-table');
    expect(callsTo(ENVIRONMENTS_READ)).toBe(1);
    await openDialog();
    expect(callsTo(ENVIRONMENTS_READ)).toBe(2);
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await screen.findByText(/^Promoted Development to Staging/);
    await waitFor(() => expect(callsTo(ENVIRONMENTS_READ)).toBe(3));
    const rollout = within(screen.getByTestId('environment-comparison-table')).getByTestId('compare-rollout');
    expect(Array.from(rollout.querySelectorAll('td')).map((td) => td.textContent)).toEqual(['10%', '10%', '25%']);

    fireEvent.click(screen.getByTestId('promote-show'));
    await waitFor(() => expect(screen.getByLabelText('Environment')).toHaveValue('staging'));
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('10%'));
    expect(screen.getByTestId('promote-button')).toHaveTextContent('Promote to Production');
  });

  describe('into Production', () => {
    it('is a second step in the same dialog, named by the fields that change: Continue sends nothing', async () => {
      const confirm = jest.spyOn(window, 'confirm').mockReturnValue(true);
      install();
      await openIn('staging');
      await openDialog();
      expect(screen.queryByTestId('promote-confirm')).toBeNull();
      fireEvent.click(screen.getByTestId('promote-continue'));
      await screen.findByTestId('promote-confirm-step');
      expect(promoteCalls()).toEqual([]);
      expect(screen.getByTestId('promote-confirm-step')).toHaveTextContent(
        'Production serves live users. Promoting changes what they get now.',
      );
      // Production is on already: the status does not change, the other two do.
      expect(text('promote-confirm-fields')).toBe('Changes in Production: rollout percentage, targeting rules.');
      expect(screen.getByRole('dialog', { name: 'Promote to Production' })).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('promote-confirm'));
      await screen.findByText(/^Promoted Staging to Production/);
      expect(confirm).not.toHaveBeenCalled();
      confirm.mockRestore();
    });

    it('Back returns to the diff, and nothing is sent', async () => {
      install();
      await openIn('staging');
      await openDialog();
      fireEvent.click(screen.getByTestId('promote-continue'));
      await screen.findByTestId('promote-back');
      await waitFor(() => expect(screen.getByTestId('promote-back')).toHaveFocus());
      fireEvent.click(screen.getByTestId('promote-back'));
      await screen.findByTestId('promote-diff');
      await waitFor(() => expect(screen.getByTestId('promote-continue')).toHaveFocus());
      expect(promoteCalls()).toEqual([]);
    });

    it("confirming sends one promote to Production's route, and changes Production only", async () => {
      const before = freshWorld();
      install();
      await openIn('staging');
      await openDialog();
      fireEvent.click(screen.getByTestId('promote-continue'));
      fireEvent.click(await screen.findByTestId('promote-confirm'));
      await screen.findByText(/^Promoted Staging to Production/);
      expect(promoteCalls()).toEqual([PRODUCTION_PROMOTE]);
      expect(bodyOf(PRODUCTION_PROMOTE)).toEqual({ from: 'staging' });
      expect(world.production).toMatchObject({ is_active: true, rollout_percentage: 40, targeting_rules: BETA });
      expect(world.staging).toEqual(before.staging);
      expect(world.development).toEqual(before.development);
      expect(calls().filter((c) => c.startsWith('PUT'))).toEqual([]);
      expect(screen.getByTestId('promote-result')).toHaveTextContent('Production was set to On at 40%.');
    });

    it('Escape on the second step closes the dialog and sends nothing', async () => {
      install();
      await openIn('staging');
      await openDialog();
      fireEvent.click(screen.getByTestId('promote-continue'));
      const dialog = await screen.findByTestId('promote-dialog');
      fireEvent.keyDown(within(dialog).getByTestId('promote-back'), { key: 'Escape' });
      expect(screen.queryByTestId('promote-dialog')).toBeNull();
      expect(promoteCalls()).toEqual([]);
      await waitFor(() => expect(screen.getByTestId('promote-button')).toHaveFocus());
    });
  });
});

// ---------------------------------------------------------------------------
// The dialog as a dialog
// ---------------------------------------------------------------------------

describe('the dialog', () => {
  it('is named and described, focuses its safe answer, and Escape closes it with nothing sent', async () => {
    install();
    await openIn('development');
    fireEvent.click(screen.getByTestId('promote-button'));
    const dialog = await screen.findByRole('dialog', { name: 'Promote to Staging' });
    expect(dialog).toHaveAccessibleDescription(
      "Copies Development's status, rollout percentage and targeting rules into Staging.",
    );
    await screen.findByTestId('promote-diff');
    await waitFor(() => expect(screen.getByTestId('promote-cancel')).toHaveFocus());
    // A second press does nothing, so the opener does not say it is expanded.
    expect(screen.getByTestId('promote-button')).not.toHaveAttribute('aria-expanded');

    fireEvent.keyDown(screen.getByTestId('promote-cancel'), { key: 'Escape' });
    expect(screen.queryByTestId('promote-dialog')).toBeNull();
    await waitFor(() => expect(screen.getByTestId('promote-button')).toHaveFocus());
    expect(promoteCalls()).toEqual([]);
    expect(world).toEqual(freshWorld());
  });

  it('Cancel closes it and sends nothing', async () => {
    install();
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-cancel'));
    expect(screen.queryByTestId('promote-dialog')).toBeNull();
    expect(promoteCalls()).toEqual([]);
    await waitFor(() => expect(screen.getByTestId('promote-button')).toHaveFocus());
  });

  it('has no axe violations on the diff, the Production step, a refusal and the unconfigured source', async () => {
    install();
    await openIn('staging');
    const check = async () => {
      // The page's root, as the environments page test scans it: the dialog is inside it.
      const root = document.getElementById('__next') as HTMLElement;
      const result = await axe.run(root, { rules: { 'color-contrast': { enabled: false } } });
      return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
    };
    await openDialog();
    expect(await check()).toEqual([]);
    fireEvent.click(screen.getByTestId('promote-continue'));
    await screen.findByTestId('promote-confirm');
    expect(await check()).toEqual([]);
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          method: 'POST',
          path: PROMOTE,
          handler: () => {
            throw apiError(500, 'Could not promote the feature flag (request ID: 8a5e6c1e).');
          },
        },
        ...routes(),
      ]) as unknown as typeof apiFetch,
    );
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await screen.findByTestId('promote-error');
    expect(await check()).toEqual([]);

    fireEvent.click(screen.getByTestId('promote-cancel'));
    world.staging = settings('staging');
    fireEvent.click(screen.getByTestId('promote-button'));
    await screen.findByTestId('promote-source-empty');
    expect(await check()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Refusals
// ---------------------------------------------------------------------------

describe('a refusal', () => {
  it.each([
    [403, 'Not enough permissions to update this feature flag'],
    [404, 'Feature flag not found'],
    [500, 'Could not promote the feature flag (request ID: 8a5e6c1e).'],
  ])('%s shows as text in the dialog, which stays open, with nothing saved', async (status, detail) => {
    const before = freshWorld();
    install([
      {
        method: 'POST',
        path: PROMOTE,
        handler: () => {
          throw apiError(status, detail);
        },
      },
    ]);
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-confirm'));
    const error = await screen.findByTestId('promote-error');
    expect(error).toHaveTextContent(detail);
    expect(error).toHaveAttribute('role', 'alert');
    expect(screen.getByTestId('promote-dialog')).toBeInTheDocument();
    expect(screen.getByTestId('promote-diff')).toBeInTheDocument();
    expect(screen.queryByText(/^Promoted /)).toBeNull();
    expect(promoteCalls()).toEqual([STAGING_PROMOTE]);
    expect(world).toEqual(before);
    // Nothing on the page changed either, and it can be tried again.
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('10%');
    expect(screen.getByTestId('promote-confirm')).not.toHaveAttribute('aria-disabled', 'true');
  });

  it("a 422 in the API's own shape (a list of type, loc and msg) shows the sentence the client renders", async () => {
    const before = freshWorld();
    install([
      {
        method: 'POST',
        path: PROMOTE,
        handler: () => {
          throw new ApiError({
            status: 422,
            detail: [
              {
                type: 'same_environment',
                loc: ['body', 'from'],
                msg: 'from must name another environment than the one promoted into',
              },
            ],
          });
        },
      },
    ]);
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-confirm'));
    const error = await screen.findByTestId('promote-error');
    expect(error).toHaveTextContent('from: from must name another environment than the one promoted into');
    expect(screen.getByTestId('promote-dialog')).toBeInTheDocument();
    expect(promoteCalls()).toEqual([STAGING_PROMOTE]);
    expect(world).toEqual(before);
  });

  it('a refusal into Production stays on the confirming step', async () => {
    install([
      {
        method: 'POST',
        path: PROMOTE,
        handler: () => {
          throw apiError(500, 'Could not promote the feature flag (request ID: 8a5e6c1e).');
        },
      },
    ]);
    await openIn('staging');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-continue'));
    fireEvent.click(await screen.findByTestId('promote-confirm'));
    expect(await screen.findByTestId('promote-error')).toHaveTextContent('request ID: 8a5e6c1e');
    expect(screen.getByTestId('promote-confirm-step')).toBeInTheDocument();
    expect(world).toEqual(freshWorld());
  });

  it('a failed read of the two environments is shown, with nothing to confirm', async () => {
    install([
      {
        path: '/api/v1/feature-flags/flag-1/environments',
        handler: () => {
          throw apiError(500, 'Could not read the flag (request ID: 1f2e3d4c).');
        },
      },
    ]);
    await openIn('development');
    fireEvent.click(screen.getByTestId('promote-button'));
    expect(await screen.findByTestId('promote-load-error')).toHaveTextContent('request ID: 1f2e3d4c');
    expect(screen.queryByTestId('promote-confirm')).toBeNull();
    expect(promoteCalls()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Late answers land nowhere
// ---------------------------------------------------------------------------

describe('an answer that arrives late', () => {
  it('a promotion answered after the environment was switched changes nothing on the page now on screen', async () => {
    const answer = later<FlagEnvironmentSettings>();
    install([{ method: 'POST', path: PROMOTE, handler: () => answer.promise }]);
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await waitFor(() => expect(promoteCalls()).toEqual([STAGING_PROMOTE]));

    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'production' } });
    await screen.findByTestId('flag-detail');
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%'));

    await act(async () => {
      world.staging = on('staging', 10, ALPHA);
      answer.give(world.staging);
    });
    expect(screen.queryByTestId('promote-result')).toBeNull();
    expect(screen.queryByTestId('promote-show')).toBeNull();
    expect(screen.queryByTestId('promote-dialog')).toBeNull();
    expect(screen.getByLabelText('Environment')).toHaveValue('production');
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%');
    expect(screen.getByDisplayValue('US')).toBeInTheDocument();
    expect(screen.queryByTestId('save-success')).toBeNull();
    // Production's page asked nothing more of the API than its own load.
    expect(callsTo(ENVIRONMENTS_READ)).toBe(1);
  });

  it('a refused promotion answered after the switch shows no error on the new page', async () => {
    const answer = later<FlagEnvironmentSettings>();
    install([{ method: 'POST', path: PROMOTE, handler: () => answer.promise }]);
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await waitFor(() => expect(promoteCalls()).toEqual([STAGING_PROMOTE]));
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'staging' } });
    await screen.findByTestId('flag-detail');
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('40%'));
    await act(async () => {
      answer.refuse(apiError(500, 'Could not promote the feature flag (request ID: 8a5e6c1e).'));
    });
    expect(screen.queryByTestId('promote-error')).toBeNull();
    expect(screen.queryByTestId('promote-dialog')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    // Staging's own page offers its own promotion, untouched.
    expect(screen.getByTestId('promote-button')).toHaveTextContent('Promote to Production');
  });

  it('a read that finishes after Cancel does not replace the next opening, which reads afresh', async () => {
    const first = later<unknown>();
    let reads = 0;
    install([
      {
        path: '/api/v1/feature-flags/flag-1/environments',
        handler: () => {
          reads += 1;
          if (reads === 1) return first.promise;
          return {
            flag_id: 'flag-1',
            key: 'checkout_v2',
            environments: [view('production'), view('staging'), view('development')],
          };
        },
      },
    ]);
    await openIn('development');
    fireEvent.click(screen.getByTestId('promote-button'));
    await screen.findByTestId('promote-loading');
    fireEvent.click(screen.getByTestId('promote-cancel'));
    expect(screen.queryByTestId('promote-dialog')).toBeNull();

    fireEvent.click(screen.getByTestId('promote-button'));
    await screen.findByTestId('promote-diff');
    expect(text('promote-now-rollout')).toBe('40%');
    await act(async () => {
      first.give({
        flag_id: 'flag-1',
        key: 'checkout_v2',
        environments: [view('production'), on('staging', 99, null), view('development')],
      });
    });
    // The first opening's answer (Staging at 99%) is not the one on screen.
    expect(text('promote-now-rollout')).toBe('40%');
    expect(reads).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// What is heard, and where focus is (the accessibility run's findings)
// ---------------------------------------------------------------------------

describe('what a screen-reader or keyboard user gets', () => {
  const statusInDialog = () => within(screen.getByTestId('promote-dialog')).getByTestId('promote-status');

  it("Production's second step: Back and the confirming button are described by the warning and the changes", async () => {
    world.staging = settings('staging');
    install();
    await openIn('staging');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-continue'));
    await screen.findByTestId('promote-confirm-step');
    for (const id of ['promote-back', 'promote-confirm']) {
      const description = screen.getByTestId(id).getAttribute('aria-describedby');
      expect(description).toBe(screen.getByTestId('promote-confirm-text').id);
      expect(screen.getByTestId(id)).toHaveAccessibleDescription(
        /Production serves live users\. Promoting changes what they get now\..*Changes in Production: status, rollout percentage, targeting rules\..*Nothing is configured in Staging, so Production will be turned off\./,
      );
    }
    // What is not copied is on the step, readable, but not read again at each button.
    expect(screen.getByTestId('promote-confirm-step')).toContainElement(screen.getByTestId('promote-not-copied'));
    expect(screen.getByTestId('promote-confirm-text')).not.toContainElement(screen.getByTestId('promote-not-copied'));
    for (const id of ['promote-back', 'promote-confirm']) {
      expect(screen.getByTestId(id)).not.toHaveAccessibleDescription(/Not copied/);
    }
    // The first step has nothing to add to the dialog's own description.
    fireEvent.click(screen.getByTestId('promote-back'));
    await screen.findByTestId('promote-continue');
    expect(screen.getByTestId('promote-cancel')).not.toHaveAttribute('aria-describedby');
  });

  it("Production's second step says Production's rollout schedule is not moved, on the step but not in the buttons' description", async () => {
    world.schedules = [{ id: 's1', status: 'active', updated_at: '2026-11-20T00:00:00Z' }];
    install();
    await openIn('staging');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-continue'));
    await screen.findByTestId('promote-confirm-step');
    const step = screen.getByTestId('promote-confirm-step');
    expect(step).toContainElement(screen.getByTestId('promote-schedule-note'));
    expect(screen.getByTestId('promote-schedule-note')).toHaveTextContent(
      /Production has a rollout schedule \(Active\)\. Promotion does not copy or change its progress/,
    );
    for (const id of ['promote-back', 'promote-confirm']) {
      expect(screen.getByTestId(id)).not.toHaveAccessibleDescription(/rollout schedule/);
      expect(screen.getByTestId(id)).toHaveAccessibleDescription(/^Production serves live users\./);
    }
  });

  it('a draft schedule does not say the next stage sets the percentage "again"; the others do', async () => {
    world.schedules = [{ id: 's1', status: 'draft', updated_at: '2026-11-20T00:00:00Z' }];
    install();
    await openIn('staging');
    await openDialog();
    expect(text('promote-schedule-note')).toBe(
      'Production has a rollout schedule (Draft). Promotion does not copy or change its progress: the next stage that starts sets the rollout percentage.',
    );
    expect(scheduleNote('Active')).toMatch(/percentage again\.$/);
    expect(scheduleNote('Paused')).toMatch(/percentage again\.$/);
    expect(scheduleNote('Draft')).not.toMatch(/again/);
  });

  it("Production's second step does not invent a schedule", async () => {
    install();
    await openIn('staging');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-continue'));
    await screen.findByTestId('promote-confirm-step');
    expect(screen.queryByTestId('promote-schedule-note')).toBeNull();
  });

  describe('the dialog has one polite status from its first render', () => {
    it('says it is reading, then one sentence about the diff, and says it only once', async () => {
      const first = later<unknown>();
      install([{ path: '/api/v1/feature-flags/flag-1/environments', handler: () => first.promise }]);
      await openIn('development');
      fireEvent.click(screen.getByTestId('promote-button'));
      // In the first render of the dialog, before anything has been read.
      expect(statusInDialog()).toHaveAttribute('role', 'status');
      expect(statusInDialog()).toHaveTextContent('Reading checkout_v2 in Development and Staging…');
      await act(async () => {
        world.development = settings('development');
        first.give({
          flag_id: 'flag-1',
          key: 'checkout_v2',
          environments: [view('production'), view('staging'), view('development')],
        });
      });
      await screen.findByTestId('promote-diff');
      expect(statusInDialog()).toHaveTextContent(
        '3 of 3 settings change in Staging. Nothing is configured in Development, so Staging will be turned off.',
      );
      // The page's own regions do not say it too.
      const saying = screen.getAllByRole('status').filter((s) => /settings change/.test(s.textContent ?? ''));
      expect(saying).toEqual([statusInDialog()]);
    });

    it('says that nothing changes when nothing does', async () => {
      world.development = on('development', 40, BETA);
      install();
      await openIn('development');
      await openDialog();
      expect(statusInDialog()).toHaveTextContent('No setting changes in Staging.');
    });

    it('says "Promoting…" while the request is out, and nothing after a refusal (the alert speaks)', async () => {
      const answer = later<FlagEnvironmentSettings>();
      install([{ method: 'POST', path: PROMOTE, handler: () => answer.promise }]);
      await openIn('development');
      await openDialog();
      fireEvent.click(screen.getByTestId('promote-confirm'));
      await waitFor(() => expect(statusInDialog()).toHaveTextContent('Promoting…'));
      await act(async () => {
        answer.refuse(apiError(500, 'Could not promote the feature flag (request ID: 8a5e6c1e).'));
      });
      await screen.findByTestId('promote-error');
      expect(statusInDialog()).toBeEmptyDOMElement();
    });

    it('an answer that arrives after Cancel writes nothing into the next opening', async () => {
      const first = later<unknown>();
      let reads = 0;
      install([
        {
          path: '/api/v1/feature-flags/flag-1/environments',
          handler: () => {
            reads += 1;
            if (reads === 1) return first.promise;
            return {
              flag_id: 'flag-1',
              key: 'checkout_v2',
              environments: [view('production'), view('staging'), view('development')],
            };
          },
        },
      ]);
      await openIn('development');
      fireEvent.click(screen.getByTestId('promote-button'));
      fireEvent.click(screen.getByTestId('promote-cancel'));
      fireEvent.click(screen.getByTestId('promote-button'));
      await screen.findByTestId('promote-diff');
      const said = statusInDialog().textContent;
      expect(said).toMatch(/^\d of 3 settings change in Staging\./);
      await act(async () => {
        first.give({
          flag_id: 'flag-1',
          key: 'checkout_v2',
          environments: [view('production'), settings('staging'), settings('development')],
        });
      });
      expect(statusInDialog().textContent).toBe(said);
    });
  });

  it('"Show Staging" moves focus to the Environment select, which survives the page starting again', async () => {
    install();
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await screen.findByText(/^Promoted Development to Staging/);
    fireEvent.click(screen.getByTestId('promote-show'));
    expect(screen.getByLabelText('Environment')).toHaveFocus();
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('10%'));
    expect(screen.getByLabelText('Environment')).toHaveFocus();
    expect(screen.getByLabelText('Environment')).toHaveValue('staging');
    expect(screen.getByTestId('environment-announcement')).toHaveTextContent('Showing Staging.');
    expect(document.body).not.toHaveFocus();
  });

  describe('the diff arriving while Cancel has focus', () => {
    const original = Element.prototype.scrollIntoView;
    afterEach(() => {
      Element.prototype.scrollIntoView = original;
    });

    it('scrolls Cancel into view', async () => {
      const scroll = jest.fn();
      Element.prototype.scrollIntoView = scroll;
      install();
      await openIn('development');
      await openDialog();
      await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1));
      expect(scroll).toHaveBeenCalledWith({ block: 'nearest' });
      expect(scroll.mock.contexts[0]).toBe(screen.getByTestId('promote-cancel'));
      // Room for the focus ring's lowest edge at the bottom of the screen.
      expect(screen.getByTestId('promote-cancel')).toHaveClass('scroll-mb-2');
    });

    it('leaves the page where it is when focus is somewhere else', async () => {
      const scroll = jest.fn();
      Element.prototype.scrollIntoView = scroll;
      const first = later<unknown>();
      install([{ path: '/api/v1/feature-flags/flag-1/environments', handler: () => first.promise }]);
      await openIn('development');
      fireEvent.click(screen.getByTestId('promote-button'));
      screen.getByTestId('promote-button').focus();
      await act(async () => {
        first.give({
          flag_id: 'flag-1',
          key: 'checkout_v2',
          environments: [view('production'), view('staging'), view('development')],
        });
      });
      await screen.findByTestId('promote-diff');
      expect(scroll).not.toHaveBeenCalled();
    });

    it('is absent in jsdom and nothing breaks without it', async () => {
      Element.prototype.scrollIntoView = undefined as unknown as typeof original;
      install();
      await openIn('development');
      await openDialog();
      expect(screen.getByTestId('promote-diff')).toBeInTheDocument();
    });
  });

  it('the opener, the dialog and the confirming button are not all named alike, and the Change column comes second', async () => {
    install();
    await openIn('development');
    await openDialog();
    expect(screen.getByTestId('promote-button')).toHaveAccessibleName('Promote to Staging');
    expect(screen.getByTestId('promote-confirm')).toHaveAccessibleName('Promote Development to Staging');
    expect(
      Array.from(screen.getByTestId('promote-diff').querySelectorAll('thead th')).map((th) => th.textContent),
    ).toEqual(['Setting', 'Change', 'Staging now', 'Staging after']);
  });

  it('the result is worded in the past, so a later change on the page does not make it false', async () => {
    install();
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await screen.findByText(/^Promoted Development to Staging/);
    expect(screen.getByTestId('promote-result').textContent).toBe(
      'Promoted Development to Staging. Staging was set to On at 10%.',
    );
    expect(screen.getByTestId('promote-result').textContent).not.toMatch(/\bnow\b|\bis\b/);
  });

  describe('the dialog cannot send a second request', () => {
    it('from Enter after a click, from Cancel and Escape while it is out, or from the answer arriving', async () => {
      const user = userEvent.setup();
      const answer = later<FlagEnvironmentSettings>();
      install([{ method: 'POST', path: PROMOTE, handler: () => answer.promise }]);
      await openIn('development');
      await openDialog();
      const confirm = screen.getByTestId('promote-confirm');
      await user.click(confirm);
      await waitFor(() => expect(promoteCalls()).toEqual([STAGING_PROMOTE]));
      // The button keeps focus while busy: Enter and Space press it again.
      expect(confirm).toHaveFocus();
      await user.keyboard('{Enter}');
      await user.keyboard(' ');
      await user.dblClick(confirm);
      await user.click(screen.getByTestId('promote-cancel'));
      await user.keyboard('{Escape}');
      expect(screen.getByTestId('promote-dialog')).toBeInTheDocument();
      expect(promoteCalls()).toEqual([STAGING_PROMOTE]);
      await act(async () => {
        world.staging = on('staging', 10, ALPHA);
        answer.give(world.staging);
      });
      await screen.findByText(/^Promoted Development to Staging/);
      // The answer opens nothing and sends nothing; opening the dialog again only reads.
      expect(promoteCalls()).toEqual([STAGING_PROMOTE]);
      fireEvent.click(screen.getByTestId('promote-button'));
      await screen.findByTestId('promote-diff');
      expect(promoteCalls()).toEqual([STAGING_PROMOTE]);
    });

    it('Production: Continue, a held Enter on Continue and Back send nothing, and the confirming step sends one', async () => {
      const user = userEvent.setup();
      install();
      await openIn('staging');
      await openDialog();
      screen.getByTestId('promote-continue').focus();
      await user.keyboard('{Enter}');
      await screen.findByTestId('promote-confirm-step');
      expect(promoteCalls()).toEqual([]);
      // Focus is on Back after Continue: a repeated Enter only goes back.
      await user.keyboard('{Enter}');
      await screen.findByTestId('promote-continue');
      expect(promoteCalls()).toEqual([]);
      await user.click(screen.getByTestId('promote-continue'));
      await user.click(await screen.findByTestId('promote-confirm'));
      await screen.findByText(/^Promoted Staging to Production/);
      expect(promoteCalls()).toEqual([PRODUCTION_PROMOTE]);
    });
  });
});

// ---------------------------------------------------------------------------
// A string flag's split (#2012)
// ---------------------------------------------------------------------------

describe("a string flag's split in the diff", () => {
  const DEV_SPLIT = [
    { value: 'red', weight: 50 },
    { value: 'green', weight: 30 },
    { value: 'blue', weight: 20 },
  ];
  const STAGING_SPLIT = [
    { value: 'red', weight: 100 },
    { value: 'green', weight: 0 },
    { value: 'blue', weight: 0 },
  ];

  /** The flag is a string flag; the promotion copies the split too, as the API does. */
  function installString() {
    install([
      {
        path: '/api/v1/feature-flags/flag-1',
        handler: () => ({ ...productionFlag(), value_type: 'string', on_value: 'A', off_value: 'Z', split: world.production.split }),
      },
      {
        method: 'POST',
        path: PROMOTE,
        handler: (p, opts) => {
          const target = p.split('/')[6] as Name;
          const from = world[(opts.json as { from: Name }).from];
          world[target] = {
            ...world[target],
            status: from.is_active ? 'active' : 'inactive',
            is_active: from.is_active,
            rollout_percentage: from.rollout_percentage,
            targeting_rules: from.targeting_rules,
            split: from.split,
            configured: true,
            updated_at: '2026-11-24T10:00:00Z',
          };
          return world[target];
        },
      },
    ]);
  }

  beforeEach(() => {
    world.development = { ...on('development', 10, ALPHA), split: DEV_SPLIT };
    world.staging = { ...on('staging', 10, ALPHA), split: STAGING_SPLIT };
  });

  it('a string flag: the Split row names each value and weight, from what Staging has to what it will have', async () => {
    installString();
    await openIn('development');
    await openDialog();
    expect(within(row('split')).getByRole('rowheader')).toHaveTextContent('Split');
    expect(text('promote-change-split')).toBe('Changes');
    const lines = (testId: string) => Array.from(screen.getByTestId(testId).children).map((el) => el.textContent);
    expect(lines('promote-now-split')).toEqual(['"red": 100%', '"green": 0%', '"blue": 0%']);
    expect(lines('promote-after-split')).toEqual(['"red": 50%', '"green": 30%', '"blue": 20%']);
    expect(text('promote-change-rules')).toBe('No change');
    expect(text('promote-summary')).toBe(
      "Copies Development's status, rollout percentage, targeting rules and split into Staging.",
    );
    expect(text('promote-not-copied')).toBe(
      "Not copied: the flag's rollout schedules and safety settings, and its type, on value and off value. They stay as they are.",
    );
    expect(screen.getByTestId('promote-status')).toHaveTextContent('1 of 4 settings change in Staging.');
  });

  it('the same split in both is No change', async () => {
    world.staging = { ...on('staging', 10, ALPHA), split: DEV_SPLIT.map((entry) => ({ ...entry })) };
    installString();
    await openIn('development');
    await openDialog();
    expect(text('promote-change-split')).toBe('No change');
    expect(screen.getByTestId('promote-no-change')).toBeInTheDocument();
  });

  it('no split in the source shows the target losing its own', async () => {
    world.development = { ...on('development', 10, ALPHA), split: null };
    installString();
    await openIn('development');
    await openDialog();
    expect(text('promote-after-split')).toBe('None');
    expect(text('promote-change-split')).toBe('Changes');
  });

  it('a long value is a bounded, labelled preview in the diff', async () => {
    const long = 'x'.repeat(8190);
    world.development = { ...on('development', 10, ALPHA), split: [{ value: long, weight: 50 }, ...DEV_SPLIT.slice(1)] };
    installString();
    await openIn('development');
    await openDialog();
    const first = screen.getByTestId('promote-after-split').children[0];
    expect(first.textContent).toBe(`"${'x'.repeat(79)}… (first 80 of 8192 characters): 50%`);
  });

  it('confirming copies the split into Staging and leaves Development and Production as they were', async () => {
    world.production = { ...on('production', 25, US), split: null };
    installString();
    await openIn('development');
    await openDialog();
    fireEvent.click(screen.getByTestId('promote-confirm'));
    await waitFor(() => expect(promoteCalls()).toEqual([STAGING_PROMOTE]));
    await waitFor(() => expect(world.staging.split).toEqual(DEV_SPLIT));
    expect(world.development.split).toEqual(DEV_SPLIT);
    expect(world.production.split).toBeNull();
  });

  it('axe finds nothing on the diff with a split', async () => {
    installString();
    await openIn('development');
    await openDialog();
    const root = document.getElementById('__next') as HTMLElement;
    const result = await axe.run(root, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('a boolean flag lists no Split row', async () => {
    install();
    await openIn('development');
    await openDialog();
    expect(screen.queryByTestId('promote-row-split')).toBeNull();
    expect(screen.getByTestId('promote-diff').querySelectorAll('tbody tr')).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------
// The helpers
// ---------------------------------------------------------------------------

describe('the helpers', () => {
  it('promotes Development to Staging, Staging to Production, and Production nowhere', () => {
    expect(promotionTarget('development')).toBe('staging');
    expect(promotionTarget('staging')).toBe('production');
    expect(promotionTarget('production')).toBeNull();
    expect(promotionTarget(null)).toBeNull();
  });

  it('lists the three fields, always, whether or not they change', () => {
    const rows = promotionRows(on('development', 10, ALPHA), on('staging', 10, ALPHA));
    expect(rows.map((r) => [r.field, r.changes])).toEqual([
      ['status', false],
      ['rollout', false],
      ['rules', false],
    ]);
  });

  it('describes rules by their conditions and shows an unknown shape as stored', () => {
    expect(describeRules(null)).toEqual(['None']);
    expect(describeRules({ logical_operator: 'AND', groups: [] })).toEqual(['None']);
    expect(describeRules(ALPHA)).toEqual(['Group 1, match all: plan equals alpha']);
    expect(describeRules([{ type: 'context', key: 'a' }])).toEqual(['[{"key":"a","type":"context"}]']);
  });
});
