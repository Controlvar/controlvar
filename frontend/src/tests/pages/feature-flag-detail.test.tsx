import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import FeatureFlagDetailPage, { pickSchedule, stageSummary } from '@/pages/feature-flags/[id]';
import { apiFetch } from '@/services/api';
import { FeatureFlag, RolloutSchedule, RolloutStageStatus } from '@/services/featureFlags';
import { SafetyCheckResponse } from '@/types/safety';
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
        'Feature flags are created and changed by the ADMIN and DEVELOPER roles.',
      );

      // The builder shows the stored rules and takes no change.
      expect(screen.getByDisplayValue('US')).toBeDisabled();
      expect(screen.queryByRole('button', { name: '+ Add Group' })).toBeNull();
      expect(screen.queryByLabelText('Remove condition')).toBeNull();

      expect(screen.getByTestId('rollout-value')).toHaveTextContent('25%');
      expect(screen.getByTestId('rollout-percentage')).toBeDisabled();
      expect(screen.queryByTestId('save-flag')).toBeNull();

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
  });

  it('an ADMIN still gets "Replace rules" over rules the builder cannot show', async () => {
    signIn('ADMIN');
    install(happyRoutes({ flag: flag({ rules: NATIVE_RULES }) }));
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-detail');
    expect(screen.getByTestId('targeting-replace')).toBeInTheDocument();
  });
});
