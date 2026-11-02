import React from 'react';
import axe from 'axe-core';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import FeatureFlagDetailPage, { SCHEDULES_LOAD_ERROR, SLIDER_SCHEDULE_NOTE } from '@/pages/feature-flags/[id]';
import {
  CREATED,
  DISCARD_AND_CLOSE,
  ROLLOUT_ERROR_COPY,
  SCHEDULE_NAME_REQUIRED,
  TARGET_RANGE,
  localInputToNaiveUtc,
  pausedNote,
} from '@/components/feature-flags/RolloutScheduleManager';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { FeatureFlag, RolloutSchedule, RolloutStage } from '@/services/featureFlags';
import { apiError, makeRouter, routedApi, Route } from './helpers/apiMock';

// #1456: the flag page creates and runs a rollout schedule. The suite runs in
// America/Los_Angeles (jest.global-setup.js).

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

/** A resolved user: `canChangeFeatureFlags(null)` is true, so no test relies on an unset one. */
function signIn(role: string, is_superuser = false) {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u@example.com', username: 'u', role, is_superuser },
  });
}

const PLANTED = 'PLANTED-server-detail-7f3a';

function flag(overrides: Partial<FeatureFlag> = {}): FeatureFlag {
  return {
    id: 'flag-1',
    key: 'checkout_v2',
    name: 'Checkout v2',
    status: 'active',
    rollout_percentage: 10,
    rules: null,
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
    ...overrides,
  };
}

function stage(id: string, order: number, overrides: Partial<RolloutStage> = {}): RolloutStage {
  return {
    id,
    rollout_schedule_id: 'sched-1',
    name: `Stage ${id}`,
    stage_order: order,
    target_percentage: order * 10,
    trigger_type: 'manual',
    status: 'pending',
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    ...overrides,
  };
}

function schedule(overrides: Partial<RolloutSchedule> = {}): RolloutSchedule {
  return {
    id: 'sched-1',
    name: 'Gradual rollout',
    feature_flag_id: 'flag-1',
    status: 'draft',
    max_percentage: 100,
    stages: [
      stage('s-1', 1, { name: 'Canary', target_percentage: 10 }),
      stage('s-2', 2, { name: 'Expanded', target_percentage: 50 }),
      stage('s-3', 3, { name: 'Everyone', target_percentage: 100 }),
    ],
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-02T00:00:00',
    ...overrides,
  };
}

/** The page's three reads, with the schedule list (and the flag) mutable between calls. */
function install(state: { schedules: RolloutSchedule[]; flag?: FeatureFlag }, extra: Route[] = []) {
  const routes: Route[] = [
    ...extra,
    { path: '/api/v1/feature-flags/flag-1', handler: () => state.flag ?? flag() },
    {
      path: '/api/v1/rollout-schedules',
      handler: () => ({ items: state.schedules, total: state.schedules.length, skip: 0, limit: 50 }),
    },
    {
      path: '/api/v1/safety/feature-flags/flag-1/check',
      handler: () => ({ feature_flag_id: 'flag-1', is_healthy: true, metrics: [], last_checked: '2026-10-01T00:00:00' }),
    },
  ];
  mockedApiFetch.mockImplementation(routedApi(routes) as unknown as typeof apiFetch);
}

const writes = () =>
  mockedApiFetch.mock.calls
    .filter(([, o]) => o?.method && o.method !== 'GET')
    .map(([p, o]) => [String(o?.method), String(p), (o as ApiFetchOptions).json] as const);

const reads = () =>
  mockedApiFetch.mock.calls.filter(([, o]) => !o?.method || o.method === 'GET').map(([p]) => String(p));

/** One write sent, and the section done with it (no button still reads "Saving…" and the like). */
async function wroteOnce() {
  await waitFor(() => expect(writes()).toHaveLength(1));
  await waitFor(() => expect(within(section()).queryAllByRole('button', { name: /…$/ })).toEqual([]));
}

async function renderPage() {
  render(<FeatureFlagDetailPage />);
  await screen.findByTestId('flag-detail');
  await waitFor(() => expect(screen.queryByTestId('rollout-schedule-loading')).toBeNull());
}

function section() {
  return screen.getByTestId('rollout-schedule-section');
}

function change(element: HTMLElement, value: string) {
  fireEvent.change(element, { target: { value } });
}

/** Fill stage `i` (0-based) of the open form. */
function fillStage(i: number, values: { name?: string; target?: string; trigger?: string; start?: string }) {
  const box = within(screen.getByTestId('rollout-schedule-form')).getAllByTestId('rollout-stage-fields')[i];
  if (values.name !== undefined) change(within(box).getByTestId('rollout-stage-name-input'), values.name);
  if (values.target !== undefined) change(within(box).getByTestId('rollout-stage-target-input'), values.target);
  if (values.trigger !== undefined) change(within(box).getByTestId('rollout-stage-trigger-input'), values.trigger);
  if (values.start !== undefined) change(within(box).getByTestId('rollout-stage-start-input'), values.start);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
});

// ---------------------------------------------------------------------------
// Roles (QA #1, #2)
// ---------------------------------------------------------------------------
describe('who may create and run a schedule', () => {
  const CELLS: Array<[string, boolean, boolean]> = [
    ['ADMIN', false, true],
    ['DEVELOPER', false, true],
    ['ANALYST', false, false],
    ['VIEWER', false, false],
    ['VIEWER', true, true],
  ];

  it.each(CELLS)('%s (superuser: %s): the controls are offered only when it may change flags', async (role, su, may) => {
    signIn(role, su);
    const active = schedule({
      status: 'active',
      stages: [stage('s-1', 1, { name: 'Canary', status: 'in_progress' }), stage('s-2', 2)],
    });
    install({ schedules: [active] });
    await renderPage();
    expect(screen.getByTestId('rollout-schedule-name')).toHaveTextContent('Gradual rollout');
    expect(screen.queryByTestId('rollout-schedule-actions') !== null).toBe(may);
    expect(screen.queryByTestId('rollout-schedule-pause') !== null).toBe(may);
    expect(screen.queryByTestId('rollout-stage-complete') !== null).toBe(may);
  });

  it.each(CELLS)('%s (superuser: %s): "Create a rollout schedule" only when it may change flags', async (role, su, may) => {
    signIn(role, su);
    install({ schedules: [] });
    await renderPage();
    const empty = screen.getByTestId('rollout-schedule-empty');
    expect(screen.queryByTestId('rollout-schedule-create') !== null).toBe(may);
    if (may) {
      expect(empty).toHaveTextContent(
        'No rollout schedule. The rollout percentage below applies at once; a schedule raises it in stages. ' +
          'Create one here, or through /api/v1/rollout-schedules.',
      );
    } else {
      expect(empty.textContent).toBe('No rollout schedule.');
    }
  });

  it.each(['ANALYST', 'VIEWER'])('%s sends no write and only the three reads', async (role) => {
    signIn(role);
    install({ schedules: [schedule({ status: 'paused' })] });
    await renderPage();
    await screen.findByTestId('safety-section');
    expect(screen.queryByTestId('rollout-schedule-actions')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-edit')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-paused-note')).toBeNull();
    expect(writes()).toEqual([]);
    // Array.from, not a spread: the tests compile to ES5, where spreading a Set gives [].
    expect(Array.from(new Set(reads())).sort()).toEqual([
      '/api/v1/feature-flags/flag-1',
      '/api/v1/rollout-schedules',
      '/api/v1/safety/feature-flags/flag-1/check',
    ]);
  });
});

// ---------------------------------------------------------------------------
// The witnesses, only in the loaded and admitted state (QA #27)
// ---------------------------------------------------------------------------
describe('the witnesses rollout-schedule-form and rollout-schedule-actions', () => {
  it('are present for a DEVELOPER: the actions on a draft, the form once opened', async () => {
    signIn('DEVELOPER');
    install({ schedules: [schedule()] });
    await renderPage();
    expect(screen.getByTestId('rollout-schedule-actions')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('rollout-schedule-edit'));
    expect(screen.getByTestId('rollout-schedule-form')).toBeInTheDocument();
  });

  it('are absent while the schedules load', async () => {
    signIn('ADMIN');
    let release: (value: unknown) => void = () => undefined;
    install({ schedules: [] }, [
      { path: '/api/v1/rollout-schedules', handler: () => new Promise((resolve) => (release = resolve)) },
    ]);
    render(<FeatureFlagDetailPage />);
    expect(await screen.findByTestId('rollout-schedule-loading')).toHaveTextContent('Loading…');
    expect(screen.queryByTestId('rollout-schedule-actions')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-form')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-create')).toBeNull();
    await act(async () => release({ items: [], total: 0, skip: 0, limit: 50 }));
  });

  it('are absent, with fixed copy, when the schedules fail to load', async () => {
    signIn('ADMIN');
    install({ schedules: [] }, [
      {
        path: '/api/v1/rollout-schedules',
        handler: () => {
          throw apiError(500, PLANTED);
        },
      },
    ]);
    render(<FeatureFlagDetailPage />);
    expect(await screen.findByTestId('rollout-schedule-error')).toHaveTextContent(SCHEDULES_LOAD_ERROR);
    expect(section()).not.toHaveTextContent(PLANTED);
    expect(screen.queryByTestId('rollout-schedule-actions')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-form')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-create')).toBeNull();
  });

  it('are absent for a VIEWER', async () => {
    signIn('VIEWER');
    install({ schedules: [schedule()] });
    await renderPage();
    expect(screen.queryByTestId('rollout-schedule-actions')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-form')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Which schedule, and which controls by status (C8)
// ---------------------------------------------------------------------------
describe('controls by status (C8)', () => {
  beforeEach(() => signIn('ADMIN'));

  it.each([
    ['draft', ['rollout-schedule-activate', 'rollout-schedule-edit', 'rollout-schedule-cancel'], false],
    ['active', ['rollout-schedule-pause', 'rollout-schedule-cancel'], false],
    ['paused', ['rollout-schedule-resume', 'rollout-schedule-edit', 'rollout-schedule-cancel'], false],
    ['completed', [], true],
    ['cancelled', [], true],
  ] as const)('%s offers %j, and Create a rollout schedule: %s', async (status, buttons, create) => {
    install({ schedules: [schedule({ status })] });
    await renderPage();
    const actions = screen.queryByTestId('rollout-schedule-actions');
    const offered = actions
      ? within(actions)
          .getAllByRole('button')
          .map((b) => b.getAttribute('data-testid'))
      : [];
    expect(offered).toEqual(buttons);
    expect(screen.queryByTestId('rollout-schedule-create') !== null).toBe(create);
  });

  it('offers no delete of a schedule, at any status', async () => {
    for (const status of ['draft', 'active', 'paused', 'completed', 'cancelled'] as const) {
      install({ schedules: [schedule({ status })] });
      const { unmount } = render(<FeatureFlagDetailPage />);
      await screen.findByTestId('rollout-schedule-name');
      expect(within(section()).queryByRole('button', { name: /delete/i })).toBeNull();
      unmount();
    }
  });

  it('never offers metric_based: the trigger select has exactly two options', async () => {
    install({ schedules: [] });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    const select = screen.getByTestId('rollout-stage-trigger-input');
    expect(within(select).getAllByRole('option').map((o) => (o as HTMLOptionElement).value)).toEqual([
      'manual',
      'time_based',
    ]);
  });

  it('an ACTIVE schedule offers no stage edit and no stage removal', async () => {
    install({ schedules: [schedule({ status: 'active' })] });
    await renderPage();
    expect(screen.queryByTestId('rollout-schedule-edit')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-form')).toBeNull();
    expect(screen.queryByTestId('rollout-stage-remove')).toBeNull();
  });

  it('the slider says a running schedule sets it again, only while one is active', async () => {
    install({ schedules: [schedule({ status: 'active' })] });
    const { unmount } = render(<FeatureFlagDetailPage />);
    expect(await screen.findByTestId('rollout-schedule-slider-note')).toHaveTextContent(SLIDER_SCHEDULE_NOTE);
    unmount();
    install({ schedules: [schedule({ status: 'paused' })] });
    await renderPage();
    expect(screen.queryByTestId('rollout-schedule-slider-note')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Create (QA #21)
// ---------------------------------------------------------------------------
describe('creating a schedule', () => {
  beforeEach(() => signIn('DEVELOPER'));

  function installCreate(state: { schedules: RolloutSchedule[] }, answer: () => unknown = () => schedule()) {
    install(state, [
      {
        method: 'POST',
        path: '/api/v1/rollout-schedules/',
        handler: () => {
          const made = answer() as RolloutSchedule;
          state.schedules = [made];
          return made;
        },
      },
    ]);
  }

  it('sends one POST with the stages numbered from 1 in list order, and shows the draft', async () => {
    const state = { schedules: [] as RolloutSchedule[] };
    installCreate(state);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    change(screen.getByTestId('rollout-schedule-name-input'), '  Gradual rollout ');
    fillStage(0, { name: 'Canary', target: '10' });
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    fillStage(1, { name: 'Expanded', target: '50', trigger: 'time_based' });
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    fillStage(2, { name: 'Everyone', target: '100' });
    fireEvent.click(screen.getByTestId('rollout-schedule-submit'));

    expect(await screen.findByTestId('rollout-schedule-notice')).toHaveTextContent(CREATED);
    expect(writes()).toEqual([
      [
        'POST',
        '/api/v1/rollout-schedules/',
        {
          name: 'Gradual rollout',
          feature_flag_id: 'flag-1',
          max_percentage: 100,
          stages: [
            { name: 'Canary', stage_order: 1, target_percentage: 10, trigger_type: 'manual' },
            { name: 'Expanded', stage_order: 2, target_percentage: 50, trigger_type: 'time_based' },
            { name: 'Everyone', stage_order: 3, target_percentage: 100, trigger_type: 'manual' },
          ],
        },
      ],
    ]);
    expect(screen.getByTestId('rollout-schedule-status')).toHaveTextContent('Draft');
    expect(screen.queryByTestId('rollout-schedule-form')).toBeNull();
    expect(screen.queryByTestId('rollout-schedule-create')).toBeNull();
  });

  it('says a time-based stage after a manual one waits for it, as a note', async () => {
    install({ schedules: [] });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    fillStage(1, { trigger: 'time_based' });
    const notes = screen.getByTestId('rollout-stage-notes');
    expect(notes.textContent).toBe(
      'Note: Stage 2 is time-based but follows a manual stage, so it starts only after you complete stage 1.',
    );
    expect(notes.querySelector('p')?.className).toContain('text-amber-800');
  });

  it('a stage switched from time-based back to manual after a time was picked is sent with no start_date', async () => {
    const state = { schedules: [] as RolloutSchedule[] };
    installCreate(state);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    change(screen.getByTestId('rollout-schedule-name-input'), 'Gradual rollout');
    fillStage(0, { name: 'Canary', target: '10', trigger: 'time_based', start: '2026-11-02T09:00' });
    expect(screen.getByTestId('rollout-stage-readback')).toBeInTheDocument();
    // Back to manual: the picked time stays in the form's state, hidden, and
    // must not be sent.
    fillStage(0, { trigger: 'manual' });
    expect(screen.queryByTestId('rollout-stage-start-input')).toBeNull();
    fireEvent.click(screen.getByTestId('rollout-schedule-submit'));

    expect(await screen.findByTestId('rollout-schedule-notice')).toHaveTextContent(CREATED);
    expect(writes()).toEqual([
      [
        'POST',
        '/api/v1/rollout-schedules/',
        {
          name: 'Gradual rollout',
          feature_flag_id: 'flag-1',
          max_percentage: 100,
          stages: [{ name: 'Canary', stage_order: 1, target_percentage: 10, trigger_type: 'manual' }],
        },
      ],
    ]);
  });

  it("each stage's fields carry the stage number in their names", async () => {
    install({ schedules: [] });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    fillStage(1, { trigger: 'time_based' });
    const [first, second] = screen.getAllByTestId('rollout-stage-fields');
    expect(screen.getByLabelText('Stage 1 name')).toBe(within(first).getByTestId('rollout-stage-name-input'));
    expect(screen.getByLabelText('Stage 1 target (% of users)')).toBe(
      within(first).getByTestId('rollout-stage-target-input'),
    );
    expect(screen.getByLabelText('Stage 1 starts')).toBe(within(first).getByTestId('rollout-stage-trigger-input'));
    expect(screen.getByLabelText('Stage 2 name')).toBe(within(second).getByTestId('rollout-stage-name-input'));
    expect(screen.getByLabelText('Stage 2 target (% of users)')).toBe(
      within(second).getByTestId('rollout-stage-target-input'),
    );
    expect(screen.getByLabelText('Stage 2 starts')).toBe(within(second).getByTestId('rollout-stage-trigger-input'));
    expect(screen.getByLabelText('Stage 2 start time (optional)')).toBe(
      within(second).getByTestId('rollout-stage-start-input'),
    );
  });

  const REFUSALS: Array<[string, Array<{ name?: string; target?: string }>, string, string]> = [
    ['a decrease', [{ name: 'A', target: '50' }, { name: 'B', target: '10' }], "Stage 2's target must be at least 50%, stage 1's target.", '1-target'],
    ['above 100', [{ name: 'A', target: '101' }], TARGET_RANGE, '0-target'],
    ['a fraction', [{ name: 'A', target: '1.5' }], TARGET_RANGE, '0-target'],
    ['no target', [{ name: 'A', target: '' }], TARGET_RANGE, '0-target'],
    ['a stage with no name', [{ name: 'A', target: '10' }, { name: '  ', target: '20' }], 'Give stage 2 a name.', '1-name'],
  ];

  it.each(REFUSALS)('refuses %s before any request, on the field', async (_what, stages, message, field) => {
    install({ schedules: [] });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    change(screen.getByTestId('rollout-schedule-name-input'), 'Gradual rollout');
    stages.forEach((values, i) => {
      if (i > 0) fireEvent.click(screen.getByTestId('rollout-stage-add'));
      fillStage(i, values);
    });
    fireEvent.click(screen.getByTestId('rollout-schedule-submit'));

    const alert = await screen.findByTestId('rollout-schedule-form-error');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert).toHaveTextContent(message);
    const [index, which] = field.split('-');
    const box = screen.getAllByTestId('rollout-stage-fields')[Number(index)];
    const input = within(box).getByTestId(which === 'name' ? 'rollout-stage-name-input' : 'rollout-stage-target-input');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    const describedBy = input.getAttribute('aria-describedby') ?? '';
    expect(document.getElementById(describedBy)).toHaveTextContent(message);
    expect(input).toHaveFocus();
    expect(writes()).toEqual([]);
  });

  it('refuses a schedule with no name before any request', async () => {
    install({ schedules: [] });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    fillStage(0, { name: 'A', target: '10' });
    fireEvent.click(screen.getByTestId('rollout-schedule-submit'));
    expect(await screen.findByTestId('rollout-schedule-form-error')).toHaveTextContent(SCHEDULE_NAME_REQUIRED);
    expect(screen.getByTestId('rollout-schedule-name-input')).toHaveFocus();
    expect(writes()).toEqual([]);
  });

  it('Cancel closes the form with no request and returns focus to "Create a rollout schedule"', async () => {
    install({ schedules: [] });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    fireEvent.click(screen.getByTestId('rollout-schedule-form-cancel'));
    expect(screen.queryByTestId('rollout-schedule-form')).toBeNull();
    await waitFor(() => expect(screen.getByTestId('rollout-schedule-create')).toHaveFocus());
    expect(writes()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Stage start dates: naive UTC, read in the browser's zone (PE 2)
// ---------------------------------------------------------------------------
describe('stage start dates are sent as naive UTC (PE 2)', () => {
  beforeEach(() => {
    signIn('ADMIN');
    // A fixed "now" before the 2026-11-01 change from PDT to PST, so an offset
    // taken from now would be wrong for the later date.
    jest.useFakeTimers({ now: new Date('2026-10-30T12:00:00Z') });
  });

  it('runs in Los Angeles, on both sides of the change', () => {
    expect(new Date(2026, 9, 31, 9, 0).getTimezoneOffset()).toBe(420);
    expect(new Date(2026, 10, 2, 9, 0).getTimezoneOffset()).toBe(480);
  });

  it('a local 09:00 on 10-31 is sent as 2026-10-31T16:00:00 and on 11-02 as 2026-11-02T17:00:00, with no Z', async () => {
    const state = { schedules: [] as RolloutSchedule[] };
    install(state, [
      {
        method: 'POST',
        path: '/api/v1/rollout-schedules/',
        handler: () => {
          state.schedules = [schedule()];
          return state.schedules[0];
        },
      },
    ]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    change(screen.getByTestId('rollout-schedule-name-input'), 'Timed');
    fillStage(0, { name: 'Before', target: '10', trigger: 'time_based', start: '2026-10-31T09:00' });
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    fillStage(1, { name: 'After', target: '50', trigger: 'time_based', start: '2026-11-02T09:00' });

    const readBacks = screen.getAllByTestId('rollout-stage-readback').map((p) => p.textContent);
    expect(readBacks).toEqual([
      'Saved as Sat, Oct 31, 2026, 9:00 AM PDT (16:00 UTC).',
      'Saved as Mon, Nov 2, 2026, 9:00 AM PST (17:00 UTC).',
    ]);
    expect(screen.getAllByText('In your time zone, America/Los_Angeles.')).toHaveLength(2);

    fireEvent.click(screen.getByTestId('rollout-schedule-submit'));
    await screen.findByTestId('rollout-schedule-notice');
    const body = writes()[0][2] as { stages: Array<{ start_date?: string }> };
    expect(body.stages.map((s) => s.start_date)).toEqual(['2026-10-31T16:00:00', '2026-11-02T17:00:00']);
  });

  it('a stored naive time is read as UTC, and an edit sends naive UTC too', async () => {
    const stored = schedule({
      status: 'paused',
      stages: [
        stage('s-1', 1, { name: 'Canary', status: 'completed' }),
        stage('s-2', 2, { name: 'Later', trigger_type: 'time_based', start_date: '2026-11-02T17:00:00' }),
      ],
    });
    install({ schedules: [stored] }, [
      { method: 'PUT', path: '/api/v1/rollout-schedules/stages/s-2', handler: () => stored.stages[1] },
    ]);
    await renderPage();
    expect(screen.getAllByTestId('rollout-stage')[1]).toHaveTextContent('11/2/2026, 9:00:00 AM');
    fireEvent.click(screen.getByTestId('rollout-schedule-edit'));
    const input = screen.getByTestId('rollout-stage-start-input') as HTMLInputElement;
    expect(input.value).toBe('2026-11-02T09:00');

    change(input, '2026-10-31T09:00');
    fireEvent.click(screen.getByTestId('rollout-stage-save'));
    await wroteOnce();
    expect(writes()).toEqual([
      ['PUT', '/api/v1/rollout-schedules/stages/s-2', { start_date: '2026-10-31T16:00:00' }],
    ]);
  });

  it('the conversion itself', () => {
    expect(localInputToNaiveUtc('2026-10-31T09:00')).toBe('2026-10-31T16:00:00');
    expect(localInputToNaiveUtc('2026-11-02T09:00')).toBe('2026-11-02T17:00:00');
    expect(localInputToNaiveUtc('not a time')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Editing a draft's or a paused schedule's stages (C8, QA #21)
// ---------------------------------------------------------------------------
describe('editing the stages of a draft or paused schedule', () => {
  beforeEach(() => signIn('DEVELOPER'));

  const paused = () =>
    schedule({
      status: 'paused',
      // Listed out of order: the form follows stage_order.
      stages: [
        stage('s-3', 3, { name: 'Everyone', target_percentage: 100 }),
        stage('s-1', 1, { name: 'Canary', target_percentage: 10, status: 'completed' }),
        stage('s-2', 2, { name: 'Expanded', target_percentage: 50, status: 'in_progress' }),
      ],
    });

  async function openEdit(routes: Route[] = []) {
    install({ schedules: [paused()] }, routes);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-edit'));
    return screen.getByTestId('rollout-schedule-form');
  }

  it('offers fields only for pending stages, in stage_order', async () => {
    const form = await openEdit();
    expect(within(form).getAllByTestId('rollout-stage-fixed').map((p) => p.textContent)).toEqual([
      'Stage 1: Canary, 10%, Completed',
      'Stage 2: Expanded, 50%, In progress',
    ]);
    const boxes = within(form).getAllByTestId('rollout-stage-fields');
    expect(boxes).toHaveLength(1);
    expect(within(boxes[0]).getByLabelText('Stage 3 name')).toHaveValue('Everyone');
    expect(within(boxes[0]).getByTestId('rollout-stage-remove')).toHaveTextContent('Remove stage 3 (Everyone)');
  });

  it('refuses an update that would make the whole list decrease, before any request', async () => {
    await openEdit();
    fillStage(0, { target: '40' });
    fireEvent.click(screen.getByTestId('rollout-stage-save'));
    expect(await screen.findByTestId('rollout-schedule-form-error')).toHaveTextContent(
      "Stage 3's target must be at least 50%, stage 2's target.",
    );
    expect(writes()).toEqual([]);
  });

  it('sends only the changed fields of a stage', async () => {
    await openEdit([
      { method: 'PUT', path: '/api/v1/rollout-schedules/stages/s-3', handler: () => paused().stages[0] },
    ]);
    fillStage(0, { name: 'All users ', target: '90', trigger: 'time_based' });
    fireEvent.click(screen.getByTestId('rollout-stage-save'));
    await wroteOnce();
    expect(writes()).toEqual([
      [
        'PUT',
        '/api/v1/rollout-schedules/stages/s-3',
        { name: 'All users', target_percentage: 90, trigger_type: 'time_based' },
      ],
    ]);
  });

  it('refuses an added stage below the last one, and adds one after the highest stage_order', async () => {
    await openEdit([
      {
        method: 'POST',
        path: '/api/v1/rollout-schedules/sched-1/stages',
        handler: () => stage('s-4', 4),
      },
    ]);
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    const fresh = screen.getByTestId('rollout-stage-new');
    change(within(fresh).getByTestId('rollout-stage-name-input'), 'Last');
    change(within(fresh).getByTestId('rollout-stage-target-input'), '20');
    fireEvent.click(screen.getByTestId('rollout-stage-add-save'));
    expect(await screen.findByTestId('rollout-schedule-form-error')).toHaveTextContent(
      "Stage 4's target must be at least 100%, stage 3's target.",
    );
    expect(writes()).toEqual([]);

    change(within(fresh).getByTestId('rollout-stage-target-input'), '100');
    fireEvent.click(screen.getByTestId('rollout-stage-add-save'));
    await wroteOnce();
    expect(writes()).toEqual([
      [
        'POST',
        '/api/v1/rollout-schedules/sched-1/stages',
        { name: 'Last', stage_order: 4, target_percentage: 100, trigger_type: 'manual' },
      ],
    ]);
  });

  it('removes a pending stage with DELETE on that stage', async () => {
    await openEdit([
      { method: 'DELETE', path: '/api/v1/rollout-schedules/stages/s-3', handler: () => undefined },
    ]);
    fireEvent.click(screen.getByTestId('rollout-stage-remove'));
    await wroteOnce();
    expect(writes()).toEqual([['DELETE', '/api/v1/rollout-schedules/stages/s-3', undefined]]);
  });

  it('adds a stage after the highest stage_order when the orders have a gap: 1, 2, 5 gives 6', async () => {
    // A gap is what a removed stage leaves behind: three stages, so the
    // count plus one would be 4, which is not after stage_order 5.
    const gapped = schedule({
      status: 'paused',
      stages: [
        stage('s-1', 1, { name: 'Canary', target_percentage: 10, status: 'completed' }),
        stage('s-2', 2, { name: 'Expanded', target_percentage: 50 }),
        stage('s-5', 5, { name: 'Most', target_percentage: 80 }),
      ],
    });
    install({ schedules: [gapped] }, [
      { method: 'POST', path: '/api/v1/rollout-schedules/sched-1/stages', handler: () => stage('s-6', 6) },
    ]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-edit'));
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    const fresh = screen.getByTestId('rollout-stage-new');
    change(within(fresh).getByLabelText('Stage 4 name'), 'Everyone');
    change(within(fresh).getByLabelText('Stage 4 target (% of users)'), '100');
    fireEvent.click(screen.getByTestId('rollout-stage-add-save'));
    await wroteOnce();
    expect(writes()).toEqual([
      [
        'POST',
        '/api/v1/rollout-schedules/sched-1/stages',
        { name: 'Everyone', stage_order: 6, target_percentage: 100, trigger_type: 'manual' },
      ],
    ]);
  });

  it('closing with a stage changed but not saved says it discards it, and sends nothing', async () => {
    await openEdit();
    const close = screen.getByTestId('rollout-schedule-form-done');
    expect(close).toHaveTextContent(/^Done$/);
    fillStage(0, { target: '90' });
    expect(close).toHaveTextContent(DISCARD_AND_CLOSE);
    // Put back as stored: nothing would be lost.
    fillStage(0, { target: '100' });
    expect(close).toHaveTextContent(/^Done$/);
    fillStage(0, { name: 'All users' });
    expect(close).toHaveTextContent(DISCARD_AND_CLOSE);
    fireEvent.click(close);
    expect(screen.queryByTestId('rollout-schedule-form')).toBeNull();
    await waitFor(() => expect(screen.getByTestId('rollout-schedule-edit')).toHaveFocus());
    expect(writes()).toEqual([]);
  });

  it('closing with a stage being added says it discards it', async () => {
    await openEdit();
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    expect(screen.getByTestId('rollout-schedule-form-done')).toHaveTextContent(DISCARD_AND_CLOSE);
    fireEvent.click(screen.getByTestId('rollout-stage-add-cancel'));
    expect(screen.getByTestId('rollout-schedule-form-done')).toHaveTextContent(/^Done$/);
  });

  it('after a save the close button reads Done again', async () => {
    await openEdit([
      { method: 'PUT', path: '/api/v1/rollout-schedules/stages/s-3', handler: () => paused().stages[0] },
    ]);
    fillStage(0, { target: '90' });
    expect(screen.getByTestId('rollout-schedule-form-done')).toHaveTextContent(DISCARD_AND_CLOSE);
    fireEvent.click(screen.getByTestId('rollout-stage-save'));
    await wroteOnce();
    expect(screen.getByTestId('rollout-schedule-form-done')).toHaveTextContent(/^Done$/);
  });
});

// ---------------------------------------------------------------------------
// Advancing (C8, QA #22)
// ---------------------------------------------------------------------------
describe('advancing a stage', () => {
  beforeEach(() => signIn('ADMIN'));

  it('offers exactly one control: Complete on the manual stage in progress, naming the next stage', async () => {
    const active = schedule({
      status: 'active',
      stages: [
        stage('s-1', 1, { name: 'Canary', target_percentage: 10, status: 'in_progress' }),
        stage('s-2', 2, {
          name: 'Expanded',
          target_percentage: 50,
          trigger_type: 'time_based',
          start_date: '2026-12-01T17:00:00',
        }),
        stage('s-3', 3, { name: 'Everyone', target_percentage: 100 }),
      ],
    });
    const state = { schedules: [active], flag: flag({ rollout_percentage: 10 }) };
    install(state, [
      {
        method: 'POST',
        path: '/api/v1/rollout-schedules/stages/s-1/advance',
        handler: () => {
          state.flag = flag({ rollout_percentage: 50 });
          return active.stages[0];
        },
      },
    ]);
    await renderPage();
    const rows = screen.getAllByTestId('rollout-stage');
    const controls = rows.map((row) => within(row).queryAllByRole('button').length);
    expect(controls).toEqual([1, 0, 0]);
    expect(screen.queryByTestId('rollout-stage-start')).toBeNull();
    const complete = within(rows[0]).getByTestId('rollout-stage-complete');
    expect(complete).toHaveAccessibleName('Complete stage 1 (Canary)');

    fireEvent.click(complete);
    expect(screen.getByTestId('rollout-schedule-confirm-text').textContent).toBe(
      'Complete “Canary” and start “Expanded” now? The flag goes from 10% to 50% at once, whatever Expanded\'s start time.',
    );
    expect(writes()).toEqual([]);
    fireEvent.click(screen.getByTestId('rollout-schedule-confirm-yes'));
    await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent('50%'));
    expect(writes()).toEqual([['POST', '/api/v1/rollout-schedules/stages/s-1/advance', undefined]]);
  });

  it('offers Start on the first pending stage only when it is manual and nothing is in progress', async () => {
    install({
      schedules: [
        schedule({
          status: 'active',
          stages: [stage('s-1', 1, { name: 'Canary', status: 'completed' }), stage('s-2', 2, { name: 'Half' }), stage('s-3', 3)],
        }),
      ],
    });
    await renderPage();
    const rows = screen.getAllByTestId('rollout-stage');
    expect(rows.map((row) => within(row).queryAllByRole('button').length)).toEqual([0, 1, 0]);
    fireEvent.click(within(rows[1]).getByTestId('rollout-stage-start'));
    expect(screen.getByTestId('rollout-schedule-confirm-text').textContent).toBe(
      'Start “Half” now? The flag goes to 20% of users.',
    );
  });

  it.each([
    ['a time-based stage in progress', 'active', [stage('s-1', 1, { trigger_type: 'time_based', status: 'in_progress' }), stage('s-2', 2)]],
    ['a time-based first pending stage', 'active', [stage('s-1', 1, { trigger_type: 'time_based' }), stage('s-2', 2)]],
    ['a paused schedule', 'paused', [stage('s-1', 1, { status: 'in_progress' }), stage('s-2', 2)]],
    ['a draft', 'draft', [stage('s-1', 1), stage('s-2', 2)]],
  ] as const)('offers none for %s', async (_what, status, stages) => {
    install({ schedules: [schedule({ status, stages: [...stages] })] });
    await renderPage();
    expect(screen.queryByTestId('rollout-stage-start')).toBeNull();
    expect(screen.queryByTestId('rollout-stage-complete')).toBeNull();
  });

  it('on the last stage, Complete says it completes the schedule and the flag stays', async () => {
    install({
      schedules: [
        schedule({
          status: 'active',
          stages: [stage('s-1', 1, { status: 'completed' }), stage('s-2', 2, { name: 'Everyone', status: 'in_progress' })],
        }),
      ],
      flag: flag({ rollout_percentage: 100 }),
    });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-stage-complete'));
    expect(screen.getByTestId('rollout-schedule-confirm-text').textContent).toBe(
      'Complete “Everyone” now? This completes the schedule; the flag stays at 100%.',
    );
  });
});

// ---------------------------------------------------------------------------
// Paused, resume, activate and cancel (QA #23, PE 5)
// ---------------------------------------------------------------------------
describe('a paused schedule and the other confirmations', () => {
  beforeEach(() => signIn('ADMIN'));

  it('paused: the badge, Resume, and a note naming both causes', async () => {
    install({ schedules: [schedule({ status: 'paused' })], flag: flag({ rollout_percentage: 5 }) });
    await renderPage();
    expect(screen.getByTestId('rollout-schedule-status')).toHaveTextContent('Paused');
    expect(screen.getByTestId('rollout-schedule-resume')).toHaveTextContent('Resume');
    const note = screen.getByTestId('rollout-schedule-paused-note');
    expect(note.textContent).toBe(pausedNote(5));
    expect(note).toHaveTextContent('until you resume it, and the flag stays at 5%');
    expect(note).toHaveTextContent('A safety rollback pauses a schedule this way too');
  });

  it('Resume states what happens at the next check, naming the next stage and its percentage', async () => {
    const state = {
      schedules: [
        schedule({
          status: 'paused',
          stages: [
            stage('s-1', 1, { name: 'Canary', target_percentage: 50, trigger_type: 'time_based', status: 'in_progress' }),
            stage('s-2', 2, { name: 'Everyone', target_percentage: 100, trigger_type: 'time_based' }),
          ],
        }),
      ],
      flag: flag({ rollout_percentage: 5 }),
    };
    install(state, [
      {
        method: 'POST',
        path: '/api/v1/rollout-schedules/sched-1/activate',
        handler: () => {
          state.schedules = [{ ...state.schedules[0], status: 'active' }];
          return state.schedules[0];
        },
      },
    ]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-resume'));
    expect(screen.getByTestId('rollout-schedule-confirm-text').textContent).toBe(
      'Resume “Gradual rollout”? Nothing changes at once. ' +
        "At the next check, if “Canary”'s time is up (time paused included), " +
        '“Everyone” starts and sets the flag to 100%, which can be above what a rollback set. ' +
        'After a rollback to 0% the flag stays off while its percentage rises.',
    );
    fireEvent.click(screen.getByTestId('rollout-schedule-confirm-yes'));
    await waitFor(() => expect(screen.getByTestId('rollout-schedule-status')).toHaveTextContent('Active'));
    expect(writes()).toEqual([['POST', '/api/v1/rollout-schedules/sched-1/activate', undefined]]);
  });

  it('Resume with a manual stage in progress says nothing starts by itself', async () => {
    install({
      schedules: [
        schedule({ status: 'paused', stages: [stage('s-1', 1, { name: 'Canary', status: 'in_progress' }), stage('s-2', 2)] }),
      ],
    });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-resume'));
    expect(screen.getByTestId('rollout-schedule-confirm-text').textContent).toBe(
      'Resume “Gradual rollout”? Nothing changes at once. No stage starts by itself: “Canary” waits for you to complete it.',
    );
  });

  it('Activate confirms, and Escape goes back with no request and focus on Activate', async () => {
    install({ schedules: [schedule()] });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-activate'));
    const dialog = screen.getByTestId('rollout-schedule-confirm');
    expect(dialog).toHaveAttribute('role', 'dialog');
    expect(screen.getByTestId('rollout-schedule-confirm-text').textContent).toBe(
      "Activate “Gradual rollout”? Its stages then set this flag's rollout percentage.",
    );
    await waitFor(() => expect(dialog).toHaveFocus());
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByTestId('rollout-schedule-confirm')).toBeNull();
    await waitFor(() => expect(screen.getByTestId('rollout-schedule-activate')).toHaveFocus());
    expect(writes()).toEqual([]);
  });

  it('Activate, confirmed, POSTs /activate and the schedule reads Active', async () => {
    const state = { schedules: [schedule()] };
    install(state, [
      {
        method: 'POST',
        path: '/api/v1/rollout-schedules/sched-1/activate',
        handler: () => {
          state.schedules = [schedule({ status: 'active' })];
          return state.schedules[0];
        },
      },
    ]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-activate'));
    fireEvent.click(screen.getByTestId('rollout-schedule-confirm-yes'));
    await waitFor(() => expect(screen.getByTestId('rollout-schedule-status')).toHaveTextContent('Active'));
    expect(writes()).toEqual([['POST', '/api/v1/rollout-schedules/sched-1/activate', undefined]]);
    expect(screen.getByTestId('rollout-schedule-pause')).toBeInTheDocument();
  });

  it('Cancel schedule confirms with the flag percentage, then POSTs /cancel', async () => {
    const state = { schedules: [schedule({ status: 'active' })] };
    install(state, [
      {
        method: 'POST',
        path: '/api/v1/rollout-schedules/sched-1/cancel',
        handler: () => {
          state.schedules = [schedule({ status: 'cancelled' })];
          return state.schedules[0];
        },
      },
    ]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-cancel'));
    expect(screen.getByTestId('rollout-schedule-confirm-text').textContent).toBe(
      'Cancel “Gradual rollout”? No more stages start, the flag stays at 10%, and it cannot be resumed.',
    );
    fireEvent.click(screen.getByTestId('rollout-schedule-confirm-yes'));
    expect(await screen.findByTestId('rollout-schedule-create')).toBeInTheDocument();
    expect(writes()).toEqual([['POST', '/api/v1/rollout-schedules/sched-1/cancel', undefined]]);
  });
});

// ---------------------------------------------------------------------------
// Focus once a change lands: the button that asked is gone or changed
// ---------------------------------------------------------------------------
describe('focus after a change', () => {
  beforeEach(() => signIn('ADMIN'));

  const heading = () => screen.getByRole('heading', { level: 2, name: 'Rollout schedule' });
  const API = '/api/v1/rollout-schedules';

  // [action, schedule before, the button, whether it asks first, the request's path, schedule after]
  const ACTIONS: Array<[string, RolloutSchedule, string, boolean, string, RolloutSchedule]> = [
    ['Activate', schedule(), 'rollout-schedule-activate', true, `${API}/sched-1/activate`, schedule({ status: 'active' })],
    [
      'Resume',
      schedule({ status: 'paused' }),
      'rollout-schedule-resume',
      true,
      `${API}/sched-1/activate`,
      schedule({ status: 'active' }),
    ],
    ['Pause', schedule({ status: 'active' }), 'rollout-schedule-pause', false, `${API}/sched-1/pause`, schedule({ status: 'paused' })],
    [
      'Cancel schedule',
      schedule({ status: 'active' }),
      'rollout-schedule-cancel',
      true,
      `${API}/sched-1/cancel`,
      schedule({ status: 'cancelled' }),
    ],
    [
      'Start stage',
      schedule({ status: 'active', stages: [stage('s-1', 1), stage('s-2', 2)] }),
      'rollout-stage-start',
      true,
      `${API}/stages/s-1/advance`,
      schedule({ status: 'active', stages: [stage('s-1', 1, { status: 'in_progress' }), stage('s-2', 2)] }),
    ],
    [
      'Complete stage',
      schedule({ status: 'active', stages: [stage('s-1', 1, { status: 'in_progress' }), stage('s-2', 2)] }),
      'rollout-stage-complete',
      true,
      `${API}/stages/s-1/advance`,
      schedule({
        status: 'active',
        stages: [stage('s-1', 1, { status: 'completed' }), stage('s-2', 2, { status: 'in_progress' })],
      }),
    ],
  ];

  it.each(ACTIONS)('%s: once it lands, focus is on the section heading', async (_action, before, button, asks, path, after) => {
    const state = { schedules: [before] };
    install(state, [
      {
        method: 'POST',
        path,
        handler: () => {
          state.schedules = [after];
          return after;
        },
      },
    ]);
    await renderPage();
    const opener = screen.getByTestId(button);
    opener.focus();
    fireEvent.click(opener);
    if (asks) fireEvent.click(screen.getByTestId('rollout-schedule-confirm-yes'));
    await wroteOnce();
    expect(writes()).toEqual([['POST', path, undefined]]);
    // The page shows the schedule as it is after the change.
    const shown: Record<string, string> = { active: 'Active', paused: 'Paused', cancelled: 'Cancelled' };
    await waitFor(() => expect(screen.getByTestId('rollout-schedule-status')).toHaveTextContent(shown[after.status]));
    await waitFor(() => expect(heading()).toHaveFocus());
  });

  it('a refused confirmation returns focus to the button that asked, beside the error', async () => {
    install({ schedules: [schedule()] }, [
      {
        method: 'POST',
        path: `${API}/sched-1/activate`,
        handler: () => {
          throw apiError(400, PLANTED);
        },
      },
    ]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-activate'));
    fireEvent.click(screen.getByTestId('rollout-schedule-confirm-yes'));
    expect(await screen.findByTestId('rollout-schedule-action-error')).toHaveTextContent(ROLLOUT_ERROR_COPY[400]);
    await waitFor(() => expect(screen.getByTestId('rollout-schedule-activate')).toHaveFocus());
    expect(heading()).not.toHaveFocus();
  });
});

// ---------------------------------------------------------------------------
// Refusals: fixed copy, never the server's detail
// ---------------------------------------------------------------------------
describe('a refused change shows fixed copy', () => {
  beforeEach(() => signIn('ADMIN'));

  const CASES: Array<[string, unknown, string]> = [
    ...[400, 403, 404, 422].map((s): [string, unknown, string] => [`${s}`, apiError(s, PLANTED), ROLLOUT_ERROR_COPY[s]]),
    ['500 with a JSON body', new ApiError({ status: 500, detail: PLANTED }), ROLLOUT_ERROR_COPY.fallback],
    ['status 0', new ApiError({ status: 0, detail: PLANTED, message: PLANTED }), UNREACHABLE],
    ['a browser exception', new TypeError(PLANTED), ROLLOUT_ERROR_COPY.fallback],
  ];

  it.each(CASES)('Pause sends at once, and a refusal (%s) shows fixed copy', async (_name, failure, copy) => {
    install({ schedules: [schedule({ status: 'active' })] }, [
      {
        method: 'POST',
        path: '/api/v1/rollout-schedules/sched-1/pause',
        handler: () => {
          throw failure;
        },
      },
    ]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-pause'));
    const alert = await screen.findByTestId('rollout-schedule-action-error');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert.textContent).toBe(copy);
    expect(document.body).not.toHaveTextContent(PLANTED);
  });
});

// ---------------------------------------------------------------------------
// D211-2c (#1460): a 409 is explained, and a target above the maximum is refused
// ---------------------------------------------------------------------------
describe('a schedule that changed (409) and the schedule maximum', () => {
  beforeEach(() => signIn('ADMIN'));

  const API = '/api/v1/rollout-schedules';
  const COPY_409 =
    "Something changed since the page loaded: a stage has started, or another schedule is now running this flag's rollout. Reload the page to see the rollout as it is now.";

  const refuse409 = (path: string): Route => ({
    method: path.includes('/stages/') && !path.endsWith('/advance') ? 'PUT' : 'POST',
    path,
    handler: () => {
      throw apiError(409, PLANTED);
    },
  });

  it('has the exact 409 copy', () => {
    expect(ROLLOUT_ERROR_COPY[409]).toBe(COPY_409);
  });

  it('Activate: a 409 shows the copy, in the alert, and focus returns to Activate', async () => {
    install({ schedules: [schedule()] }, [refuse409(`${API}/sched-1/activate`)]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-activate'));
    fireEvent.click(screen.getByTestId('rollout-schedule-confirm-yes'));
    const alert = await screen.findByTestId('rollout-schedule-action-error');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert.textContent).toBe(COPY_409);
    await waitFor(() => expect(screen.getByTestId('rollout-schedule-activate')).toHaveFocus());
    expect(document.body).not.toHaveTextContent(PLANTED);
  });

  it('Advance: a 409 shows the copy, and focus returns to the stage button', async () => {
    install({ schedules: [schedule({ status: 'active' })] }, [refuse409(`${API}/stages/s-1/advance`)]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-stage-start'));
    fireEvent.click(screen.getByTestId('rollout-schedule-confirm-yes'));
    const alert = await screen.findByTestId('rollout-schedule-action-error');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert.textContent).toBe(COPY_409);
    await waitFor(() => expect(screen.getByTestId('rollout-stage-start')).toHaveFocus());
  });

  it('Save: a 409 shows the copy, and focus returns to the Save button', async () => {
    install({ schedules: [schedule({ status: 'paused' })] }, [refuse409(`${API}/stages/s-1`)]);
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-edit'));
    fillStage(0, { target: '15' });
    const save = within(screen.getByTestId('rollout-schedule-form')).getAllByTestId('rollout-stage-save')[0];
    save.focus();
    fireEvent.click(save);
    // Focus leaves Save before the refusal resolves (as it does in a browser, where a
    // disabled button loses it), so only the return-focus step can put it back.
    screen.getByRole('heading', { level: 3, name: 'Edit stages' }).focus();
    expect(save).not.toHaveFocus();
    const alert = await screen.findByTestId('rollout-schedule-action-error');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert.textContent).toBe(COPY_409);
    await waitFor(() =>
      expect(within(screen.getByTestId('rollout-schedule-form')).getAllByTestId('rollout-stage-save')[0]).toHaveFocus(),
    );
  });

  describe('a stage target above the schedule maximum', () => {
    const capped = () =>
      schedule({
        status: 'paused',
        max_percentage: 60,
        stages: [
          stage('s-1', 1, { name: 'Canary', target_percentage: 10 }),
          stage('s-2', 2, { name: 'Expanded', target_percentage: 50 }),
        ],
      });

    it('is refused with the exact message, before any request', async () => {
      install({ schedules: [capped()] });
      await renderPage();
      fireEvent.click(screen.getByTestId('rollout-schedule-edit'));
      fillStage(1, { target: '70' });
      fireEvent.click(within(screen.getByTestId('rollout-schedule-form')).getAllByTestId('rollout-stage-save')[1]);
      expect(await screen.findByTestId('rollout-schedule-form-error')).toHaveTextContent(
        "Stage 2's target must be at most 60%, the schedule's maximum.",
      );
      expect(writes()).toEqual([]);
    });

    it('is accepted at the maximum (control)', async () => {
      install({ schedules: [capped()] }, [
        {
          method: 'PUT',
          path: `${API}/stages/s-2`,
          handler: () => stage('s-2', 2, { target_percentage: 60 }),
        },
      ]);
      await renderPage();
      fireEvent.click(screen.getByTestId('rollout-schedule-edit'));
      fillStage(1, { target: '60' });
      fireEvent.click(within(screen.getByTestId('rollout-schedule-form')).getAllByTestId('rollout-stage-save')[1]);
      await waitFor(() => expect(writes()).toHaveLength(1));
      expect(screen.queryByTestId('rollout-schedule-form-error')).toBeNull();
    });
  });
});

// ---------------------------------------------------------------------------
// Accessibility (QA #28): axe in jsdom, colour contrast off (not computable here)
// ---------------------------------------------------------------------------
describe('accessibility of the section (axe-core in jsdom)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };
  async function expectNoViolations() {
    const result = await axe.run(section(), axeOptions);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  }

  it('empty, with the create form open and a refusal shown', async () => {
    signIn('ADMIN');
    install({ schedules: [] });
    await renderPage();
    await expectNoViolations();
    fireEvent.click(screen.getByTestId('rollout-schedule-create'));
    fillStage(0, { trigger: 'time_based', start: '2026-11-02T09:00' });
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    fireEvent.click(screen.getByTestId('rollout-schedule-submit'));
    await screen.findByTestId('rollout-schedule-form-error');
    await expectNoViolations();
  });

  it('a draft with a confirmation open', async () => {
    signIn('DEVELOPER');
    install({ schedules: [schedule()] });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-activate'));
    await expectNoViolations();
  });

  it('a paused schedule with its stages open for editing', async () => {
    signIn('DEVELOPER');
    install({ schedules: [schedule({ status: 'paused' })] });
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-edit'));
    fireEvent.click(screen.getByTestId('rollout-stage-add'));
    await expectNoViolations();
  });

  it('a paused schedule with the edit form open, a target above the maximum and a 409 shown', async () => {
    signIn('DEVELOPER');
    install(
      {
        schedules: [
          schedule({
            status: 'paused',
            max_percentage: 60,
            stages: [
              stage('s-1', 1, { name: 'Canary', target_percentage: 10 }),
              stage('s-2', 2, { name: 'Expanded', target_percentage: 50 }),
              stage('s-3', 3, { name: 'Everyone', target_percentage: 60 }),
            ],
          }),
        ],
      },
      [
        {
          method: 'PUT',
          path: '/api/v1/rollout-schedules/stages/s-1',
          handler: () => {
            throw apiError(409, PLANTED);
          },
        },
      ],
    );
    await renderPage();
    fireEvent.click(screen.getByTestId('rollout-schedule-edit'));
    fillStage(2, { target: '70' });
    fireEvent.click(within(screen.getByTestId('rollout-schedule-form')).getAllByTestId('rollout-stage-save')[2]);
    await screen.findByTestId('rollout-schedule-form-error');
    await expectNoViolations();
    fillStage(2, { target: '60' });
    fillStage(0, { target: '15' });
    fireEvent.click(within(screen.getByTestId('rollout-schedule-form')).getAllByTestId('rollout-stage-save')[0]);
    await screen.findByTestId('rollout-schedule-action-error');
    await expectNoViolations();
  });

  it('an active schedule with a refusal shown, and read-only for a VIEWER', async () => {
    signIn('ADMIN');
    install({ schedules: [schedule({ status: 'active', stages: [stage('s-1', 1, { status: 'in_progress' })] })] }, [
      {
        method: 'POST',
        path: '/api/v1/rollout-schedules/sched-1/pause',
        handler: () => {
          throw apiError(400, PLANTED);
        },
      },
    ]);
    const { unmount } = render(<FeatureFlagDetailPage />);
    await screen.findByTestId('rollout-schedule-actions');
    fireEvent.click(screen.getByTestId('rollout-schedule-pause'));
    await screen.findByTestId('rollout-schedule-action-error');
    await expectNoViolations();
    unmount();

    signIn('VIEWER');
    install({ schedules: [schedule({ status: 'paused' })] });
    await renderPage();
    await expectNoViolations();
  });
});
