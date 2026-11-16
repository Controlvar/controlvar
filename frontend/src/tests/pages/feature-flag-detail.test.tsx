import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import { RbacService } from '@modules/rbac';
import FeatureFlagDetailPage, { pickSchedule, stageSummary } from '@/pages/feature-flags/[id]';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import { takeDeletedFlagNotice } from '@/components/feature-flags/FlagManageSection';
import { EffectivePermissionsProvider } from '@/contexts/EffectivePermissionsContext';
import { ModulesProvider, __resetModulesCache } from '@/contexts/ModulesContext';
import { apiFetch } from '@/services/api';
import { FeatureFlag, RolloutSchedule, RolloutStageStatus } from '@/services/featureFlags';
import { ModulesInfo } from '@/services/modules';
import { SafetyCheckResponse } from '@/types/safety';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import { apiError, makeRouter, routedApi, Route } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1' } });
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

/** Detail shape from `FeatureFlagService._feature_flag_to_dict`: `status` + `rules`. */
function flag(overrides: Partial<FeatureFlag> = {}): FeatureFlag {
  return {
    id: 'flag-1',
    key: 'checkout_v2',
    name: 'Checkout v2',
    description: 'New checkout flow',
    status: 'inactive',
    rollout_percentage: 25,
    rules: {
      logical_operator: 'AND',
      groups: [
        {
          logical_operator: 'AND',
          conditions: [{ attribute: 'country', operator: 'equals', value: 'US' }],
        },
      ],
    },
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
    ...overrides,
  };
}

function schedule(overrides: Partial<RolloutSchedule> = {}): RolloutSchedule {
  return {
    id: 'sched-1',
    name: 'Gradual checkout rollout',
    feature_flag_id: 'flag-1',
    status: 'active',
    max_percentage: 100,
    stages: [
      {
        id: 's-1', rollout_schedule_id: 'sched-1', name: 'Canary', stage_order: 1, target_percentage: 10,
        trigger_type: 'time_based', status: 'completed', start_date: '2026-09-01T00:00:00Z',
        created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
      },
      {
        id: 's-2', rollout_schedule_id: 'sched-1', name: 'Half', stage_order: 2, target_percentage: 50,
        trigger_type: 'manual', status: 'in_progress',
        created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
      },
      {
        id: 's-3', rollout_schedule_id: 'sched-1', name: 'Everyone', stage_order: 3, target_percentage: 100,
        trigger_type: 'manual', status: 'pending',
        created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
      },
    ],
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-02T00:00:00Z',
    ...overrides,
  };
}

function safety(overrides: Partial<SafetyCheckResponse> = {}): SafetyCheckResponse {
  return {
    feature_flag_id: 'flag-1',
    is_healthy: true,
    metrics: [
      { name: 'error_rate', current_value: 0.4, threshold: 5, unit: '%', is_healthy: true },
      { name: 'latency_p95', current_value: 180, threshold: 500, unit: 'ms', is_healthy: true },
    ],
    last_checked: '2026-09-11T08:00:00Z',
    ...overrides,
  };
}

function install(routes: Route[]) {
  mockedApiFetch.mockImplementation(routedApi(routes) as unknown as typeof apiFetch);
}

const happyRoutes = (overrides: { flag?: FeatureFlag; schedules?: RolloutSchedule[]; safety?: SafetyCheckResponse } = {}): Route[] => [
  { path: '/api/v1/feature-flags/flag-1', handler: () => overrides.flag ?? flag() },
  {
    path: '/api/v1/rollout-schedules',
    handler: () => ({ items: overrides.schedules ?? [schedule()], total: 1, skip: 0, limit: 50 }),
  },
  { path: '/api/v1/safety/feature-flags/flag-1/check', handler: () => overrides.safety ?? safety() },
];

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
});

describe('pickSchedule', () => {
  it('prefers active, then paused, then draft, then most recent', () => {
    const items = [
      schedule({ id: 'done', status: 'completed', updated_at: '2026-09-10T00:00:00Z' }),
      schedule({ id: 'draft', status: 'draft' }),
      schedule({ id: 'live', status: 'active' }),
    ];
    expect(pickSchedule(items)?.id).toBe('live');
    expect(pickSchedule(items.filter((s) => s.id !== 'live'))?.id).toBe('draft');
    expect(pickSchedule([])).toBeNull();
  });
});

/** The default schedule with its three stages (Canary 10%, Half 50%, Everyone 100%) in these states. */
function scheduleWith(statuses: [RolloutStageStatus, RolloutStageStatus, RolloutStageStatus]): RolloutSchedule {
  const base = schedule();
  return { ...base, stages: base.stages.map((stage, i) => ({ ...stage, status: statuses[i] })) };
}

describe('the rollout summary (#1009)', () => {
  const cases: Array<[string, RolloutSchedule, string]> = [
    [
      'first stage in progress',
      scheduleWith(['in_progress', 'pending', 'pending']),
      'Up to 100% · current: Canary → 10% · next: Half → 50%',
    ],
    [
      'middle stage in progress',
      scheduleWith(['completed', 'in_progress', 'pending']),
      'Up to 100% · current: Half → 50% · next: Everyone → 100%',
    ],
    [
      'last stage in progress',
      scheduleWith(['completed', 'completed', 'in_progress']),
      'Up to 100% · current: Everyone → 100%',
    ],
    [
      'no stage started',
      { ...scheduleWith(['pending', 'pending', 'pending']), status: 'draft' },
      'Up to 100% · next: Canary → 10%',
    ],
    [
      'every stage completed',
      { ...scheduleWith(['completed', 'completed', 'completed']), status: 'completed' },
      'Up to 100%',
    ],
  ];

  it.each(cases)('%s', async (_name, sched, expected) => {
    install(happyRoutes({ schedules: [sched] }));
    render(<FeatureFlagDetailPage />);
    const summary = await screen.findByTestId('rollout-schedule-summary');
    expect(summary.textContent).toBe(expected);
  });

  it('orders the stages by stage_order, not by the order the API lists them in', () => {
    const sched = scheduleWith(['in_progress', 'pending', 'pending']);
    const { current, next } = stageSummary({ ...sched, stages: [...sched.stages].reverse() });
    expect([current?.name, next?.name]).toEqual(['Canary', 'Half']);
  });
});

describe('FeatureFlagDetailPage', () => {
  it('renders header, on/off state, targeting rules, rollout schedule and safety check', async () => {
    install(happyRoutes());
    render(<FeatureFlagDetailPage />);
    expect(screen.getByTestId('flag-loading')).toBeInTheDocument();

    expect(await screen.findByTestId('flag-detail')).toBeInTheDocument();
    expect(screen.getByTestId('flag-name')).toHaveTextContent('Checkout v2');
    expect(screen.getByTestId('flag-key')).toHaveTextContent('checkout_v2');
    expect(screen.getByTestId('flag-status')).toHaveTextContent('Off');
    expect(screen.getByTestId('flag-toggle')).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%');

    // Rules come back under the legacy `rules` key and must still load into the builder.
    expect(screen.getByDisplayValue('US')).toBeInTheDocument();

    // Rollout schedule
    const sched = await screen.findByTestId('rollout-schedule-name');
    expect(sched).toHaveTextContent('Gradual checkout rollout');
    expect(screen.getByTestId('rollout-schedule-status')).toHaveTextContent('Active');
    const stages = within(screen.getByTestId('rollout-stages')).getAllByTestId('rollout-stage');
    expect(stages).toHaveLength(3);
    expect(stages[0]).toHaveTextContent('Canary');
    expect(stages[0]).toHaveTextContent('10%');
    expect(stages[1]).toHaveTextContent('In progress');
    expect(screen.getByTestId('rollout-schedule-section')).toHaveTextContent('current: Half → 50%');
    expect(screen.getByTestId('rollout-schedule-summary')).toHaveTextContent(
      /^Up to 100% · current: Half → 50% · next: Everyone → 100%$/,
    );

    // Safety
    expect(await screen.findByTestId('safety-status')).toHaveTextContent('Healthy');
    const metrics = within(screen.getByTestId('safety-metrics')).getAllByTestId('safety-metric');
    expect(metrics).toHaveLength(2);
    expect(metrics[0]).toHaveTextContent('Error rate');
    expect(metrics[0]).toHaveTextContent('0.4 %');
    expect(metrics[1]).toHaveTextContent('180 ms');
    expect(screen.getByTestId('safety-last-checked')).toBeInTheDocument();

    const paths = mockedApiFetch.mock.calls.map(([p]) => String(p).split('?')[0]);
    expect(paths).toEqual(
      expect.arrayContaining([
        '/api/v1/feature-flags/flag-1',
        '/api/v1/rollout-schedules',
        '/api/v1/safety/feature-flags/flag-1/check',
      ]),
    );
    expect(mockedApiFetch.mock.calls.find(([p]) => p === '/api/v1/rollout-schedules')?.[1]?.query).toMatchObject({
      feature_flag_id: 'flag-1',
    });
  });

  it('turns the flag on through POST /enable and updates the pill', async () => {
    install([
      ...happyRoutes(),
      {
        method: 'POST',
        path: '/api/v1/feature-flags/flag-1/enable',
        handler: () => ({ id: 'flag-1', key: 'checkout_v2', name: 'Checkout v2', status: 'active', updated_at: '2026-09-11T00:00:00Z', audit_log_id: 'audit-1' }),
      },
    ]);
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-detail');

    fireEvent.click(screen.getByTestId('flag-toggle'));
    expect(screen.getByTestId('flag-toggle')).toHaveAttribute('aria-checked', 'true');
    await waitFor(() => expect(screen.getByTestId('flag-toggle')).not.toBeDisabled());
    expect(screen.getByTestId('flag-status')).toHaveTextContent('On');
    expect(screen.getByTestId('flag-status')).toHaveAttribute('data-status', 'active');

    const call = mockedApiFetch.mock.calls.find(([p]) => p === '/api/v1/feature-flags/flag-1/enable');
    expect(call?.[1]?.method).toBe('POST');
  });

  it('reverts the switch and shows the error when enabling fails', async () => {
    install([
      ...happyRoutes(),
      {
        method: 'POST',
        path: '/api/v1/feature-flags/flag-1/enable',
        handler: () => {
          throw apiError(500, 'Failed to enable feature flag');
        },
      },
    ]);
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-detail');

    fireEvent.click(screen.getByTestId('flag-toggle'));
    expect(await screen.findByTestId('flag-toggle-error')).toHaveTextContent('Failed to enable feature flag');
    expect(screen.getByTestId('flag-toggle')).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByTestId('flag-status')).toHaveTextContent('Off');
  });

  it('shows the empty schedule hint and a non-fatal safety error', async () => {
    install([
      { path: '/api/v1/feature-flags/flag-1', handler: () => flag({ rules: null }) },
      { path: '/api/v1/rollout-schedules', handler: () => ({ items: [], total: 0, skip: 0, limit: 50 }) },
      {
        path: '/api/v1/safety/feature-flags/flag-1/check',
        handler: () => {
          throw apiError(500, 'safety service down');
        },
      },
    ]);
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-detail');
    expect(await screen.findByTestId('rollout-schedule-empty')).toBeInTheDocument();
    expect(await screen.findByTestId('safety-error')).toHaveTextContent('safety service down');
    expect(screen.queryByTestId('safety-status')).not.toBeInTheDocument();
    // The page itself is still usable.
    expect(screen.getByTestId('save-flag')).toBeInTheDocument();
  });

  it('shows Unhealthy when the safety check fails a threshold and re-checks on demand', async () => {
    let checks = 0;
    install([
      ...happyRoutes().filter((r) => !String(r.path).includes('/safety/')),
      {
        path: '/api/v1/safety/feature-flags/flag-1/check',
        handler: () => {
          checks += 1;
          return safety({
            is_healthy: checks === 1 ? false : true,
            metrics: [{ name: 'error_rate', current_value: 9, threshold: 5, unit: '%', is_healthy: checks !== 1 }],
          });
        },
      },
    ]);
    render(<FeatureFlagDetailPage />);
    expect(await screen.findByTestId('safety-status')).toHaveTextContent('Unhealthy');
    expect(screen.getByTestId('safety-status')).toHaveAttribute('data-healthy', 'false');

    fireEvent.click(screen.getByTestId('safety-recheck'));
    await waitFor(() => expect(screen.getByTestId('safety-status')).toHaveTextContent('Healthy'));
    expect(checks).toBe(2);
  });

  it('says the error rate cannot be computed, in text, when errors arrive with no evaluations', async () => {
    install([
      ...happyRoutes().filter((r) => !String(r.path).includes('/safety/')),
      {
        path: '/api/v1/safety/feature-flags/flag-1/check',
        // What the API returns for a flag evaluated only locally before its SDK
        // reported any counts: 0 / 0 reads as a 0% error rate and "healthy".
        handler: () =>
          safety({
            is_healthy: true,
            metrics: [{ name: 'error_rate', current_value: 0, threshold: 0.05, is_healthy: true }],
            details: { feature_flag_key: 'checkout', error_count: 7, total_evaluations: 0, timeframe_minutes: 15 },
          }),
      },
    ]);
    render(<FeatureFlagDetailPage />);

    const status = await screen.findByTestId('safety-status');
    expect(status).toHaveTextContent('Error rate unknown');
    expect(status).not.toHaveTextContent('Healthy');
    expect(status).toHaveAttribute('data-healthy', 'unknown');
    expect(status.className).not.toMatch(/green|red/);
    expect(screen.getByTestId('safety-no-evaluations')).toHaveTextContent(
      '7 errors reported, but no evaluations reported in the last 15 minutes: the error rate cannot be computed.',
    );
    // The error_rate row says so in words; it does not show a green "healthy" dot.
    const metric = screen.getByTestId('safety-metric');
    expect(within(metric).getByTestId('safety-metric-not-computed')).toHaveTextContent('not computed');
    expect(within(metric).queryByLabelText('healthy')).not.toBeInTheDocument();
  });

  it('keeps Healthy when evaluations were recorded, and when there were no errors at all', async () => {
    for (const details of [
      { error_count: 7, total_evaluations: 1000, timeframe_minutes: 15 },
      { error_count: 0, total_evaluations: 0, timeframe_minutes: 15 },
    ]) {
      install([
        ...happyRoutes().filter((r) => !String(r.path).includes('/safety/')),
        { path: '/api/v1/safety/feature-flags/flag-1/check', handler: () => safety({ details }) },
      ]);
      const { unmount } = render(<FeatureFlagDetailPage />);
      expect(await screen.findByTestId('safety-status')).toHaveTextContent('Healthy');
      expect(screen.queryByTestId('safety-no-evaluations')).not.toBeInTheDocument();
      unmount();
    }
  });

  it('says how many error reports were stored but not counted, only when there are some', async () => {
    const cases: Array<[Record<string, unknown>, string | null]> = [
      [
        { error_count: 0, uncounted_error_count: 12, total_evaluations: 300, timeframe_minutes: 15 },
        "12 error reports in the last 15 minutes were sent with keys that do not count toward automatic rollback (no sdk:ruleset scope, or the key's owner can no longer change flags). They are stored, but do not count toward the error rate.",
      ],
      [
        { error_count: 2, uncounted_error_count: 1, total_evaluations: 300, timeframe_minutes: 15 },
        "1 error report in the last 15 minutes was sent with a key that does not count toward automatic rollback (no sdk:ruleset scope, or the key's owner can no longer change flags). It is stored, but does not count toward the error rate.",
      ],
      [{ error_count: 2, uncounted_error_count: 0, total_evaluations: 300, timeframe_minutes: 15 }, null],
      [{ error_count: 2, total_evaluations: 300, timeframe_minutes: 15 }, null],
    ];
    for (const [details, text] of cases) {
      install([
        ...happyRoutes().filter((r) => !String(r.path).includes('/safety/')),
        { path: '/api/v1/safety/feature-flags/flag-1/check', handler: () => safety({ details }) },
      ]);
      const { unmount } = render(<FeatureFlagDetailPage />);
      expect(await screen.findByTestId('safety-status')).toHaveTextContent('Healthy');
      if (text === null) {
        expect(screen.queryByTestId('safety-uncounted-reports')).not.toBeInTheDocument();
      } else {
        const line = screen.getByTestId('safety-uncounted-reports');
        expect(line).toHaveTextContent(text);
        expect(line).toHaveAttribute('role', 'status');
        expect(line.className).toContain('text-slate-700');
      }
      unmount();
    }
  });

  it('saves the rollout percentage through PUT /feature-flags/{id}, leaving untouched rules out', async () => {
    install([
      ...happyRoutes(),
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/flag-1',
        handler: (_p, options) => ({ ...flag(), ...(options.json as object) }),
      },
    ]);
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-detail');

    fireEvent.change(screen.getByTestId('rollout-percentage'), { target: { value: '60' } });
    expect(screen.getByTestId('rollout-value')).toHaveTextContent('60%');
    fireEvent.click(screen.getByTestId('save-flag'));

    expect(await screen.findByTestId('save-success')).toBeInTheDocument();
    const call = mockedApiFetch.mock.calls.find(([p, o]) => p === '/api/v1/feature-flags/flag-1' && o?.method === 'PUT');
    // The rules were not touched, so they are not sent: the stored ones stay as they are.
    expect(call?.[1]?.json).toEqual({ rollout_percentage: 60 });
  });

  it('sends the targeting rules with the rollout when they were changed', async () => {
    install([
      ...happyRoutes(),
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/flag-1',
        handler: (_p, options) => ({ ...flag(), ...(options.json as object) }),
      },
    ]);
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-detail');

    fireEvent.change(screen.getByDisplayValue('US'), { target: { value: 'CA' } });
    fireEvent.click(screen.getByTestId('save-flag'));

    expect(await screen.findByTestId('save-success')).toBeInTheDocument();
    const call = mockedApiFetch.mock.calls.find(([p, o]) => p === '/api/v1/feature-flags/flag-1' && o?.method === 'PUT');
    expect(call?.[1]?.json).toEqual({
      rollout_percentage: 25,
      targeting_rules: {
        logical_operator: 'AND',
        groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'country', operator: 'equals', value: 'CA' }] }],
      },
    });
  });

  it('renders a 404 view for a missing flag', async () => {
    install([
      {
        path: '/api/v1/feature-flags/flag-1',
        handler: () => {
          throw apiError(404, 'Feature flag not found');
        },
      },
    ]);
    render(<FeatureFlagDetailPage />);
    expect(await screen.findByTestId('flag-not-found')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /back to feature flags/i })).toHaveAttribute('href', '/feature-flags');
  });

  it('renders a generic error view for other failures', async () => {
    install([
      {
        path: '/api/v1/feature-flags/flag-1',
        handler: () => {
          throw apiError(0, "Can't reach the API");
        },
      },
    ]);
    render(<FeatureFlagDetailPage />);
    expect(await screen.findByTestId('flag-error')).toHaveTextContent("Can't reach the API");
  });
});

describe('who may change a flag (#917)', () => {
  /** A shape the builder cannot show, so the page offers "Replace rules" to roles that may change it. */
  const NATIVE_RULES = {
    rules: [{ id: 'r-1', conditions: [{ attribute: 'country', operator: 'equals', value: 'DE' }] }],
  } as unknown as FeatureFlag['rules'];

  const writes = () => mockedApiFetch.mock.calls.filter(([, o]) => o?.method && o.method !== 'GET');

  it.each(['ANALYST', 'VIEWER'])(
    '%s reads the flag with no switch, a read-only builder, a disabled slider and no Save',
    async (role) => {
      signIn(role);
      install(happyRoutes());
      render(<FeatureFlagDetailPage />);
      await screen.findByTestId('flag-detail');

      // The state is still shown, in the pill and in words; only the switch is gone.
      expect(screen.getByTestId('flag-status')).toHaveTextContent('Off');
      expect(screen.getByText('Not serving')).toBeInTheDocument();
      expect(screen.queryByTestId('flag-toggle')).toBeNull();
      expect(screen.queryByRole('switch')).toBeNull();
      expect(screen.getByTestId('flag-role-note')).toHaveTextContent(
        'Feature flags are created and changed with the feature flag permissions, which the ADMIN and DEVELOPER roles have.',
      );

      // The builder shows the stored rules and takes no change.
      expect(screen.getByDisplayValue('US')).toBeDisabled();
      expect(screen.queryByRole('button', { name: '+ Add Group' })).toBeNull();
      expect(screen.queryByLabelText('Remove condition')).toBeNull();

      expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%');
      expect(screen.getByTestId('rollout-percentage')).toBeDisabled();
      expect(screen.queryByTestId('save-flag')).toBeNull();

      // #442: no "Manage this flag" group, so no Archive and no Delete.
      expect(screen.queryByTestId('flag-archive')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Archive' })).toBeNull();
      expect(screen.queryByTestId('flag-delete')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();

      // Reading stays: the schedule and the safety check load, and Re-check is offered.
      expect(await screen.findByTestId('rollout-schedule-name')).toHaveTextContent('Gradual checkout rollout');
      expect(await screen.findByTestId('safety-status')).toHaveTextContent('Healthy');
      expect(screen.getByTestId('safety-recheck')).toBeEnabled();
      expect(writes()).toEqual([]);
    },
  );

  it.each(['ANALYST', 'VIEWER'])('%s sees stored rules the builder cannot show, without "Replace rules"', async (role) => {
    signIn(role);
    install(happyRoutes({ flag: flag({ rules: NATIVE_RULES }) }));
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-detail');
    const section = screen.getByTestId('flag-targeting-section');
    expect(within(section).getByTestId('targeting-raw')).toBeInTheDocument();
    expect(within(section).getByTestId('targeting-raw-json')).toHaveTextContent('"DE"');
    expect(within(section).queryByTestId('targeting-replace')).toBeNull();
    expect(screen.queryByTestId('save-flag')).toBeNull();
  });

  it.each([
    ['ADMIN', false],
    ['DEVELOPER', false],
    ['VIEWER', true],
  ])('%s (superuser: %s) keeps every control', async (role, is_superuser) => {
    signIn(role, is_superuser);
    install(happyRoutes());
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-detail');
    expect(screen.getByTestId('flag-toggle')).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByDisplayValue('US')).toBeEnabled();
    expect(screen.getByRole('button', { name: '+ Add Group' })).toBeInTheDocument();
    expect(screen.getByTestId('rollout-percentage')).toBeEnabled();
    expect(screen.getByTestId('save-flag')).toBeInTheDocument();
    expect(screen.queryByTestId('flag-role-note')).toBeNull();
    // #442: the "Manage this flag" group, with Archive for an inactive flag, and Delete.
    expect(within(screen.getByTestId('flag-archive')).getByRole('button', { name: 'Archive' })).toBeEnabled();
    expect(within(screen.getByTestId('flag-delete')).getByRole('button', { name: 'Delete' })).toBeEnabled();
  });

  it('an ADMIN still gets "Replace rules" over rules the builder cannot show', async () => {
    signIn('ADMIN');
    install(happyRoutes({ flag: flag({ rules: NATIVE_RULES }) }));
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-detail');
    expect(screen.getByTestId('targeting-replace')).toBeInTheDocument();
  });

  it('has no axe violations across the whole feature flag detail page (#1466)', async () => {
    install(happyRoutes());
    const { container } = render(<FeatureFlagDetailPage />);
    await screen.findByTestId('safety-status');
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// #442: deleting a flag. Delete cannot be undone, so the Dialog asks for the
// flag's key: no DELETE until the typed text equals it exactly and Delete is
// pressed; Escape and Cancel send nothing. It is offered by FEATURE_FLAG
// DELETE, which a custom role or grant can give apart from UPDATE. Every role
// here signs in as itself, never as a superuser (which passes every check).
// ---------------------------------------------------------------------------

describe('deleting a flag (#442)', () => {
  /** The delete Dialog's sentences (PLAN-v1.1 DESIGN E2) for checkout_v2, word for word. */
  const DESCRIPTION =
    "This cannot be undone. Code that still asks for checkout_v2 gets a 404 and its SDK's fallback, " +
    'so archive it instead while any code may still ask for it. ' +
    'Its rollout schedules and safety settings are deleted with it; its events are kept.';
  const FIELD = 'Type checkout_v2 to delete it';
  const ROLE_NOTE =
    'Feature flags are created and changed with the feature flag permissions, which the ADMIN and DEVELOPER roles have.';
  const FULL: ModulesInfo = { profile: 'full', modules: ['rbac'], version: 'test' };

  /** The element's whole text, which must be the sentence and nothing else. */
  const textOf = (el: HTMLElement) => (el.textContent ?? '').trim();

  /** Every DELETE the pages sent. */
  const deletes = () => mockedApiFetch.mock.calls.filter(([, options]) => options?.method === 'DELETE');

  /** The flag page's reads, the list's, and the delete, answered by `answer` (a 204 by default). */
  function installDelete(answer: () => unknown = () => undefined) {
    install([
      ...happyRoutes(),
      { path: '/api/v1/feature-flags', handler: () => ({ items: [], total: 0, skip: 0, limit: 100 }) },
      { method: 'DELETE', path: '/api/v1/feature-flags/flag-1', handler: answer },
    ]);
  }

  async function openDeleteDialog() {
    fireEvent.click(within(await screen.findByTestId('flag-delete')).getByRole('button', { name: 'Delete' }));
    return screen.getByRole('dialog', { name: 'Delete Checkout v2?' });
  }

  let getUserPermissions: jest.SpyInstance;

  beforeEach(() => {
    __resetModulesCache();
    mockRouter.push.mockClear();
    getUserPermissions = jest.spyOn(RbacService, 'getUserPermissions');
    // A notice an earlier test left for the list.
    takeDeletedFlagNotice();
  });

  afterEach(() => {
    getUserPermissions.mockRestore();
  });

  /** A VIEWER (who changes no flag by role) whose custom role holds `actions` on feature_flag. */
  async function renderWithGrant(actions: string[]) {
    signIn('VIEWER');
    getUserPermissions.mockResolvedValue({
      user_id: 'u1',
      username: 'u',
      base_role: 'viewer',
      custom_roles: ['flag-role'],
      permissions: { feature_flag: ['read', 'list', ...actions] },
      is_superuser: false,
    });
    installDelete();
    const view = render(
      <ModulesProvider initial={FULL}>
        <EffectivePermissionsProvider>
          <FeatureFlagDetailPage />
        </EffectivePermissionsProvider>
      </ModulesProvider>,
    );
    // The group appears once the map has answered.
    await screen.findByTestId('flag-manage');
    expect(getUserPermissions).toHaveBeenCalledWith('u1');
    return view;
  }

  it('ADMIN and DEVELOPER see Delete; ANALYST and VIEWER see none and the role note', async () => {
    for (const role of ['ADMIN', 'DEVELOPER']) {
      signIn(role);
      installDelete();
      const { unmount } = render(<FeatureFlagDetailPage />);
      const control = await screen.findByTestId('flag-delete');
      expect(screen.getByRole('group', { name: 'Manage this flag' })).toContainElement(control);
      expect(within(control).getByRole('button', { name: 'Delete' })).toBeEnabled();
      unmount();
    }
    for (const role of ['ANALYST', 'VIEWER']) {
      signIn(role);
      installDelete();
      const { unmount } = render(<FeatureFlagDetailPage />);
      expect(textOf(await screen.findByTestId('flag-role-note'))).toBe(ROLE_NOTE);
      expect(screen.queryByTestId('flag-delete')).toBeNull();
      expect(screen.queryAllByRole('button', { name: /delete/i })).toEqual([]);
      unmount();
    }
    // Offering is not doing: no page load sent a DELETE.
    expect(deletes()).toEqual([]);
  });

  it('a grant with feature_flag update but not delete sees no Delete; one with delete but not update sees only Delete', async () => {
    // Update alone: Archive, and no Delete.
    const updateOnly = await renderWithGrant(['update']);
    expect(within(screen.getByTestId('flag-archive')).getByRole('button', { name: 'Archive' })).toBeEnabled();
    expect(screen.queryByTestId('flag-delete')).toBeNull();
    expect(screen.queryAllByRole('button', { name: /delete/i })).toEqual([]);
    updateOnly.unmount();

    // Delete alone: Delete, and nothing that needs update.
    await renderWithGrant(['delete']);
    expect(within(screen.getByTestId('flag-delete')).getByRole('button', { name: 'Delete' })).toBeEnabled();
    expect(screen.queryByTestId('flag-archive')).toBeNull();
    expect(screen.queryByRole('switch')).toBeNull();
    expect(deletes()).toEqual([]);
  });

  it('no DELETE before the typed key equals flag.key and Delete is pressed; Escape and Cancel send nothing', async () => {
    signIn('DEVELOPER');
    installDelete();
    renderInAppRoot(<FeatureFlagDetailPage />);
    const opener = within(await screen.findByTestId('flag-delete')).getByRole('button', { name: 'Delete' });

    // Opening the dialog sends nothing; Cancel has focus, and Escape is Cancel.
    let dialog = await openDeleteDialog();
    await act(async () => undefined);
    expect(deletes()).toEqual([]);
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(opener).toHaveFocus();
    expect(deletes()).toEqual([]);

    // Cancel sends nothing either, even with the key typed.
    dialog = await openDeleteDialog();
    fireEvent.change(within(dialog).getByLabelText(FIELD), { target: { value: 'checkout_v2' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(opener).toHaveFocus();
    expect(deletes()).toEqual([]);

    // Opened again, the field is empty, and Delete stays disabled for
    // anything but the key exactly: no trimming, no change of case, not the name.
    dialog = await openDeleteDialog();
    const field = within(dialog).getByLabelText(FIELD);
    expect(field).toHaveValue('');
    const confirm = within(dialog).getByRole('button', { name: 'Delete' });
    expect(confirm).toBeDisabled();
    for (const typed of ['checkout', 'checkout_v2 ', ' checkout_v2', 'Checkout_v2', 'CHECKOUT_V2', 'Checkout v2', 'checkout_v23']) {
      fireEvent.change(field, { target: { value: typed } });
      expect(confirm).toBeDisabled();
      fireEvent.click(confirm);
    }
    await act(async () => undefined);
    expect(deletes()).toEqual([]);

    // The key itself enables Delete, and still nothing is sent until it is pressed.
    fireEvent.change(field, { target: { value: 'checkout_v2' } });
    expect(confirm).toBeEnabled();
    await act(async () => undefined);
    expect(deletes()).toEqual([]);

    fireEvent.click(confirm);
    await waitFor(() => expect(deletes()).toHaveLength(1));
    const [[path, options]] = deletes();
    expect(path).toBe('/api/v1/feature-flags/flag-1');
    expect(options?.json).toBeUndefined();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/feature-flags'));
    expect(deletes()).toHaveLength(1);
  });

  it('while the DELETE is in flight, Delete again, Enter, Escape and Cancel send nothing more', async () => {
    signIn('ADMIN');
    let finish!: () => void;
    const held = new Promise<undefined>((resolve) => {
      finish = () => resolve(undefined);
    });
    installDelete(() => held);
    renderInAppRoot(<FeatureFlagDetailPage />);
    const dialog = await openDeleteDialog();
    const field = within(dialog).getByLabelText(FIELD);
    fireEvent.change(field, { target: { value: 'checkout_v2' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(deletes()).toHaveLength(1));

    // The answer has not come: every way of asking again is tried.
    const confirm = within(dialog).getByTestId('flag-delete-confirm');
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    fireEvent.keyDown(field, { key: 'Enter' });
    fireEvent.keyDown(confirm, { key: 'Enter' });
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(within(dialog).getByTestId('flag-delete-cancel'));
    await act(async () => undefined);
    expect(deletes()).toHaveLength(1);
    expect(screen.getByRole('dialog', { name: 'Delete Checkout v2?' })).toBe(dialog);

    await act(async () => {
      finish();
      await held;
    });
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/feature-flags'));
    expect(mockRouter.push).toHaveBeenCalledTimes(1);
    expect(deletes()).toHaveLength(1);
  });

  it('the dialog asks "Delete {name}?" in these words, Cancel first, and axe finds nothing in it', async () => {
    signIn('ADMIN');
    installDelete();
    renderInAppRoot(<FeatureFlagDetailPage />);
    const dialog = await openDeleteDialog();
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Delete Checkout v2?');
    expect(dialog).toHaveAccessibleDescription(DESCRIPTION);
    expect(within(dialog).getByRole('textbox', { name: FIELD })).toHaveValue('');
    expect(within(dialog).getAllByRole('button').map(textOf)).toEqual(['Cancel', 'Delete']);
    const result = await axe.run(dialog, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  });

  it('after the 204 the list shows "Deleted {name}." in a status region, once', async () => {
    signIn('ADMIN');
    installDelete();
    const detail = renderInAppRoot(<FeatureFlagDetailPage />);
    const dialog = await openDeleteDialog();
    fireEvent.change(within(dialog).getByLabelText(FIELD), { target: { value: 'checkout_v2' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/feature-flags'));
    expect(mockRouter.push).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).toBeNull();
    detail.unmount();

    // The router has moved to the list.
    const list = render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-empty');
    const status = screen.getByTestId('flags-deleted-status');
    expect(status).toHaveAttribute('role', 'status');
    expect(textOf(status)).toBe('Deleted Checkout v2.');
    list.unmount();

    // Said once: the list opened again says nothing.
    render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-empty');
    expect(screen.queryByTestId('flags-deleted-status')).toBeNull();
    expect(screen.queryByText('Deleted Checkout v2.')).toBeNull();
  });

  it.each([
    [403, 'Deleting a flag needs permission to delete feature flags, which the ADMIN and DEVELOPER roles have.'],
    [404, 'This flag no longer exists.'],
    [500, 'Could not delete this flag.'],
  ])("a %s from the delete reads fixed copy, never the server's words, and the page stays", async (status, copy) => {
    signIn('ADMIN');
    installDelete(() => {
      throw apiError(status, 'server words that must not be shown');
    });
    renderInAppRoot(<FeatureFlagDetailPage />);
    const dialog = await openDeleteDialog();
    fireEvent.change(within(dialog).getByLabelText(FIELD), { target: { value: 'checkout_v2' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    expect(textOf(await within(dialog).findByRole('alert'))).toBe(copy);
    expect(screen.queryByText(/server words/)).toBeNull();
    expect(deletes()).toHaveLength(1);
    expect(mockRouter.push).not.toHaveBeenCalled();
    expect(takeDeletedFlagNotice()).toBeNull();
    // The way to try again stays.
    expect(within(dialog).getByRole('button', { name: 'Delete' })).toBeEnabled();
  });
});
