/**
 * The experiment page's "Schedule" (#1454): a DRAFT's start and end, set in
 * the browser's time zone and sent as instants.
 *
 * The suite runs in America/Los_Angeles (jest.global-setup.js), and the clock
 * is faked at 2026-10-30T12:00Z, two days before daylight saving ends there
 * (11-01). So a date on 10-31 is UTC-7 and one on 11-02 is UTC-8: an offset
 * taken from "now" instead of from the date itself sends 11-02 an hour off.
 *
 * Every case renders the page module itself with `apiFetch` mocked and a
 * RESOLVED user (a null user is offered everything).
 */
import React from 'react';
import fs from 'fs';
import path from 'path';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { ApiError, apiFetch } from '@/services/api';
import { Experiment, ExperimentStatus } from '@/types/experiments';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';
import {
  END_HELP,
  END_IN_PAST,
  END_TOO_SOON,
  SCHEDULE_ERROR_COPY,
  SCHEDULE_SAVED,
  START_HELP,
  START_IN_PAST,
  fromLocalInput,
  readBack,
  scheduleChanges,
  toLocalInput,
} from '@/components/experiments/ExperimentScheduleSection';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments/[id]', query: { id: 'exp-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockUseAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const NOW = new Date('2026-10-30T12:00:00Z');
const SCHEDULE_PATH = '/api/v1/experiments/exp-1/schedule';

const BASE: Experiment = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'draft',
  targeting_rules: null,
  tags: null,
  owner_id: 'someone-else',
  owner_name: 'Someone Else',
  start_date: null,
  end_date: null,
  created_at: '2026-10-01T10:00:00',
  updated_at: '2026-10-01T10:00:00',
  variants: [
    { id: 'v-1', name: 'Control', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'Blue CTA', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [{ id: 'm-1', name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true }],
};

const experiment = (overrides: Partial<Experiment> = {}): Experiment => ({ ...BASE, ...overrides });

const as = (role: string, extra: Record<string, unknown> = {}) => ({
  id: 'user-1',
  email: `${role.toLowerCase()}@demo.com`,
  username: role.toLowerCase(),
  role,
  is_superuser: false,
  ...extra,
});

function signIn(user: Record<string, unknown>) {
  mockUseAuth.mockReturnValue({ user, status: 'authenticated' });
}

/** Text the server might send, which must never reach the page. */
const PLANTED = 'Traceback (most recent call last): PLANTED-sched-9c1';

/** The API stores an instant as UTC with no zone, and answers with it so. */
function stored(instant: string | null | undefined): string | null | undefined {
  if (instant === null || instant === undefined) return instant;
  return new Date(instant).toISOString().slice(0, 19);
}

type ScheduleHandler = (body: Record<string, unknown>, current: Experiment) => unknown;

/** The server's answer to a schedule: the dates it was sent, the rest kept. */
const applySchedule: ScheduleHandler = (body, current) => ({
  ...current,
  owner_name: undefined, // the schedule route does not name the owner
  ...('start_date' in body ? { start_date: stored(body.start_date as string | null) } : {}),
  ...('end_date' in body ? { end_date: stored(body.end_date as string | null) } : {}),
});

function install(current: Experiment, schedule: ScheduleHandler = applySchedule) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => current },
      { path: '/api/v1/holdout', handler: () => null },
      {
        method: 'PUT',
        path: SCHEDULE_PATH,
        handler: (_p, o) => schedule(o.json as Record<string, unknown>, current),
      },
      {
        method: 'POST',
        path: '/api/v1/experiments/exp-1/start',
        handler: () => ({ ...current, status: 'active' }),
      },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderPage(current: Experiment = experiment(), schedule?: ScheduleHandler) {
  install(current, schedule);
  const view = render(<ExperimentDetailPage />);
  await screen.findByTestId('experiment-detail');
  return view;
}

async function openSchedule(current: Experiment = experiment(), schedule?: ScheduleHandler) {
  await renderPage(current, schedule);
  fireEvent.click(screen.getByTestId('experiment-schedule'));
  return screen.getByTestId('experiment-schedule-form');
}

const pick = (which: 'start' | 'end', value: string) =>
  fireEvent.change(screen.getByTestId(`schedule-${which}`), { target: { value } });

const save = () => fireEvent.click(screen.getByTestId('schedule-save'));

const callsTo = (method: string) =>
  mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === method);

const puts = () => callsTo('PUT');

/** The one PUT's body, exactly. */
const sentBody = () => {
  expect(puts()).toHaveLength(1);
  const [url, options] = puts()[0];
  expect(url).toBe(SCHEDULE_PATH);
  return options?.json as Record<string, unknown>;
};

const text = (el: HTMLElement) => (el.textContent ?? '').replace(/\s+/g, ' ').trim();

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockRouter.push.mockClear();
  signIn(as('DEVELOPER'));
});

afterEach(() => {
  jest.useRealTimers();
});

it('runs in America/Los_Angeles, on both sides of the change of 2026-11-01', () => {
  expect(Intl.DateTimeFormat().resolvedOptions().timeZone).toBe('America/Los_Angeles');
  expect(new Date('2026-10-31T09:00').getTimezoneOffset()).toBe(420);
  expect(new Date('2026-11-02T09:00').getTimezoneOffset()).toBe(480);
});

// ---------------------------------------------------------------------------
// Who is offered it, and on what (QA #1, #2, #27)
// ---------------------------------------------------------------------------

describe('who sees Schedule', () => {
  const STATUSES: ExperimentStatus[] = ['draft', 'active', 'paused', 'completed', 'archived'];
  const table: [string, ExperimentStatus, boolean, Record<string, unknown>][] = [];
  for (const status of STATUSES) {
    const writer = status === 'draft';
    table.push(['ADMIN', status, writer, as('ADMIN')]);
    table.push(['DEVELOPER', status, writer, as('DEVELOPER')]);
    table.push(['ANALYST', status, false, as('ANALYST')]);
    table.push(['VIEWER', status, false, as('VIEWER')]);
    table.push(['superuser VIEWER', status, writer, as('VIEWER', { is_superuser: true })]);
  }

  it.each(table)('%s on a %s experiment: offered %s', async (_n, status, offered, user) => {
    signIn(user);
    await renderPage(experiment({ status, start_date: '2026-11-02T17:00:00' }));
    expect(screen.queryByTestId('experiment-schedule') !== null).toBe(offered);
    // The witness marks only the open form, never a page that cannot open it.
    expect(screen.queryByTestId('experiment-schedule-form')).toBeNull();
    // Nothing is ever sent to the schedule route without a save.
    expect(callsTo('PUT')).toEqual([]);
    expect(callsTo('POST')).toEqual([]);
  });

  it.each([['ANALYST'], ['VIEWER']])('%s reads the experiment and sends nothing else', async (role) => {
    signIn(as(role));
    await renderPage();
    expect(mockedApiFetch.mock.calls.map(([url, o]) => `${o?.method ?? 'GET'} ${url}`)).toEqual([
      'GET /api/v1/experiments/exp-1',
    ]);
  });

  it('a PAUSED experiment is never offered it: its resume time stays in the API', async () => {
    signIn(as('ADMIN', { is_superuser: true }));
    await renderPage(experiment({ status: 'paused', start_date: '2026-10-01T10:00:00' }));
    expect(screen.queryByTestId('experiment-schedule')).toBeNull();
  });

  it('the button sits with the manage controls, outside experiment-actions', async () => {
    await renderPage();
    const actions = screen.getByTestId('experiment-actions');
    expect(within(actions).queryByTestId('experiment-schedule')).toBeNull();
    expect(screen.getByTestId('experiment-manage')).toContainElement(screen.getByTestId('experiment-schedule'));
    expect(screen.getByTestId('experiment-schedule')).toHaveTextContent(/^Schedule$/);
  });

  it('the page is loading: no witness', () => {
    mockedApiFetch.mockImplementation(() => new Promise(() => {}));
    render(<ExperimentDetailPage />);
    expect(screen.getByTestId('experiment-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('experiment-schedule-form')).toBeNull();
    expect(screen.queryByTestId('experiment-schedule')).toBeNull();
  });

  it('the page failed to load: no witness', async () => {
    mockedApiFetch.mockImplementation(async () => {
      throw apiError(500, PLANTED);
    });
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-error');
    expect(screen.queryByTestId('experiment-schedule-form')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// The instant sent, and the instant shown (QA #11, PE 1)
// ---------------------------------------------------------------------------

describe('what is sent: only the changed dates, as instants with Z', () => {
  beforeEach(() => {
    jest.useFakeTimers({ now: NOW });
  });

  it.each([
    // Picker value (local, Los Angeles) → the exact string sent.
    ['2026-10-31T09:00', '2026-10-31T16:00:00.000Z'], // PDT, UTC-7
    ['2026-11-02T09:00', '2026-11-02T17:00:00.000Z'], // PST, UTC-8, after the change
  ])('a start picked at %s local is sent as %s, and nothing else', async (local, instant) => {
    await openSchedule();
    pick('start', local);
    save();
    await screen.findByTestId('schedule-saved');
    expect(sentBody()).toEqual({ start_date: instant });
  });

  it('both dates, either side of the change', async () => {
    await openSchedule();
    pick('start', '2026-10-31T09:00');
    pick('end', '2026-11-02T09:00');
    save();
    await screen.findByTestId('schedule-saved');
    expect(sentBody()).toEqual({
      start_date: '2026-10-31T16:00:00.000Z',
      end_date: '2026-11-02T17:00:00.000Z',
    });
  });

  it('only the end is sent when only the end changed; the stored start is not resent', async () => {
    await openSchedule(experiment({ start_date: '2026-11-02T17:00:00', end_date: '2026-11-09T17:00:00' }));
    pick('end', '2026-11-16T09:00');
    save();
    await screen.findByTestId('schedule-saved');
    expect(sentBody()).toEqual({ end_date: '2026-11-16T17:00:00.000Z' });
  });

  it('a draft that is due but not started: changing the end sends the end only, and is not refused', async () => {
    // Its start (two hours ago) is stored; resending it would be refused (422).
    await openSchedule(experiment({ start_date: '2026-10-30T10:00:00', end_date: '2026-11-05T10:00:00' }));
    pick('end', '2026-11-06T09:00');
    save();
    await screen.findByTestId('schedule-saved');
    expect(sentBody()).toEqual({ end_date: '2026-11-06T17:00:00.000Z' });
  });

  it.each([
    ['start', { start_date: null }],
    ['end', { end_date: null }],
  ] as const)('Remove on the %s sends null for it alone', async (which, body) => {
    await openSchedule(experiment({ start_date: '2026-11-02T17:00:00', end_date: '2026-11-09T17:00:00' }));
    fireEvent.click(screen.getByTestId(`schedule-${which}-remove`));
    expect(screen.getByTestId(`schedule-${which}`)).toHaveValue('');
    expect(screen.getByTestId(`schedule-${which}`)).toHaveFocus();
    save();
    await screen.findByTestId('schedule-saved');
    expect(sentBody()).toEqual(body);
  });

  it('never sends time_zone', async () => {
    await openSchedule();
    pick('start', '2026-10-31T09:00');
    pick('end', '2026-11-02T09:00');
    save();
    await screen.findByTestId('schedule-saved');
    expect(Object.keys(sentBody()).sort()).toEqual(['end_date', 'start_date']);
  });

  it('nothing changed sends nothing, closes the form and returns focus to Schedule', async () => {
    await openSchedule(experiment({ start_date: '2026-11-02T17:00:00' }));
    // Picked again, the same minute: unchanged.
    pick('start', '2026-11-02T09:00');
    save();
    await waitFor(() => expect(screen.queryByTestId('experiment-schedule-form')).toBeNull());
    expect(puts()).toEqual([]);
    expect(screen.getByTestId('experiment-schedule')).toHaveFocus();
    expect(screen.queryByTestId('schedule-saved')).toBeNull();
  });

  it('a stored value with seconds, left as it is, is not resent rounded to the minute', async () => {
    await openSchedule(experiment({ start_date: '2026-11-02T17:00:30', end_date: '2026-11-09T17:00:00' }));
    pick('end', '2026-11-10T09:00');
    save();
    await screen.findByTestId('schedule-saved');
    expect(sentBody()).toEqual({ end_date: '2026-11-10T17:00:00.000Z' });
  });
});

describe('what is shown: stored UTC read as UTC, in the browser zone', () => {
  beforeEach(() => {
    jest.useFakeTimers({ now: NOW });
  });

  it('the stored "2026-11-02T17:00:00" opens as 9:00 AM, with the zone and the read-back', async () => {
    await openSchedule(experiment({ start_date: '2026-11-02T17:00:00', end_date: '2026-10-31T23:30:00' }));
    expect(screen.getByTestId('schedule-start')).toHaveValue('2026-11-02T09:00');
    expect(text(screen.getByTestId('schedule-start-readback'))).toBe(
      'Saved as Mon, Nov 2, 2026, 9:00 AM PST (17:00 UTC).',
    );
    expect(text(screen.getByTestId('schedule-start-zone'))).toBe('In your time zone, America/Los_Angeles.');
    // 23:30 UTC on 10-31 is 4:30 PM on 10-31 in Los Angeles: same day.
    expect(screen.getByTestId('schedule-end')).toHaveValue('2026-10-31T16:30');
    expect(text(screen.getByTestId('schedule-end-readback'))).toBe(
      'Saved as Sat, Oct 31, 2026, 4:30 PM PDT (23:30 UTC).',
    );
    expect(text(screen.getByTestId('schedule-end-zone'))).toBe('In your time zone, America/Los_Angeles.');
  });

  it('the read-back names the UTC day when it is not the local one', async () => {
    await openSchedule();
    pick('end', '2026-11-02T20:00');
    expect(text(screen.getByTestId('schedule-end-readback'))).toBe(
      'Saved as Mon, Nov 2, 2026, 8:00 PM PST (Nov 3, 04:00 UTC).',
    );
  });

  it('an empty picker has no read-back; the zone line and help stay', async () => {
    await openSchedule();
    expect(screen.queryByTestId('schedule-start-readback')).toBeNull();
    expect(screen.queryByTestId('schedule-start-remove')).toBeNull();
    expect(screen.getByTestId('schedule-start-zone')).toBeInTheDocument();
    expect(screen.getByTestId('schedule-start')).toHaveValue('');
    expect(screen.getByTestId('schedule-end')).toHaveValue('');
  });

  it('the help says what the scheduler does, true to scheduler.py', async () => {
    await openSchedule();
    expect(text(screen.getByTestId('schedule-start-help'))).toBe(START_HELP);
    expect(text(screen.getByTestId('schedule-end-help'))).toBe(END_HELP);
    expect(START_HELP).toContain('A background check runs every 15 minutes by default');
    expect(START_HELP).toContain('Until then it stays a draft, and Start still starts it at once.');
    expect(START_HELP).toContain('Start before the scheduled start moves the start to now.');
    expect(START_HELP).toContain(
      'If it is not ready to start (two variants, a control and a metric), it stays a draft until it is.',
    );
    expect(END_HELP).toBe(
      'A running experiment completes by itself at this time. A paused one is not completed by its ' +
        'end date. A draft with only an end never starts on its own.',
    );
  });
});

// The conversions on their own, so each tamper names its rule.
describe('the conversions', () => {
  it('fromLocalInput reads each date with its own offset', () => {
    expect(fromLocalInput('2026-10-31T09:00')?.toISOString()).toBe('2026-10-31T16:00:00.000Z');
    expect(fromLocalInput('2026-11-02T09:00')?.toISOString()).toBe('2026-11-02T17:00:00.000Z');
    expect(fromLocalInput('')).toBeNull();
  });

  it('toLocalInput reads a stored value with no zone as UTC', () => {
    expect(toLocalInput('2026-11-02T17:00:00')).toBe('2026-11-02T09:00');
    expect(toLocalInput('2026-11-02T17:00:00.123456')).toBe('2026-11-02T09:00');
    expect(toLocalInput('2026-11-02T17:00:00+00:00')).toBe('2026-11-02T09:00');
    expect(toLocalInput(null)).toBe('');
    expect(toLocalInput('not a date')).toBe('');
  });

  it('a time the spring change skips reads back as the time it becomes', () => {
    // 2026-03-08 02:30 does not exist in Los Angeles; Date takes it as 3:30 AM PDT.
    const instant = fromLocalInput('2026-03-08T02:30') as Date;
    expect(readBack(instant)).toBe('Saved as Sun, Mar 8, 2026, 3:30 AM PDT (10:30 UTC).');
  });

  it('scheduleChanges sends what changed and nothing else', () => {
    const initial = { start: '2026-11-02T09:00', end: '' };
    expect(scheduleChanges(initial, initial)).toEqual({});
    expect(scheduleChanges(initial, { start: '', end: '' })).toEqual({ start_date: null });
    expect(scheduleChanges(initial, { start: '2026-11-02T09:00', end: '2026-11-09T09:00' })).toEqual({
      end_date: '2026-11-09T17:00:00.000Z',
    });
  });
});

// ---------------------------------------------------------------------------
// Refused before sending, against the merged values (QA #13)
// ---------------------------------------------------------------------------

describe('refused before anything is sent', () => {
  beforeEach(() => {
    jest.useFakeTimers({ now: NOW });
  });

  async function expectRefused(field: 'start' | 'end', copy: string) {
    const alert = await screen.findByTestId('schedule-error');
    expect(alert).toHaveTextContent(copy);
    expect(alert).toHaveAttribute('role', 'alert');
    const input = screen.getByTestId(`schedule-${field}`);
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input.getAttribute('aria-describedby')?.split(' ')).toContain('schedule-error');
    expect(input).toHaveFocus();
    expect(puts()).toEqual([]);
    expect(screen.getByTestId('experiment-schedule-form')).toBeInTheDocument();
  }

  it('a start one minute in the past', async () => {
    await openSchedule();
    pick('start', '2026-10-30T04:59'); // 11:59Z
    save();
    await expectRefused('start', START_IN_PAST);
  });

  it('an end in the past', async () => {
    await openSchedule();
    pick('end', '2026-10-30T04:00'); // 11:00Z
    save();
    await expectRefused('end', END_IN_PAST);
  });

  it('an end less than an hour after the start, both sent', async () => {
    await openSchedule();
    pick('start', '2026-10-31T09:00');
    pick('end', '2026-10-31T09:59');
    save();
    await expectRefused('end', END_TOO_SOON);
  });

  it('an end less than an hour after the STORED start, the end alone changed', async () => {
    await openSchedule(experiment({ start_date: '2026-10-31T16:00:00' })); // 09:00 local
    pick('end', '2026-10-31T09:30');
    save();
    await expectRefused('end', END_TOO_SOON);
  });

  it('a start less than an hour before the STORED end, the start alone changed', async () => {
    await openSchedule(experiment({ end_date: '2026-11-02T17:00:00' })); // 09:00 local
    pick('start', '2026-11-02T08:30');
    save();
    await expectRefused('start', END_TOO_SOON);
  });

  it('a start moved past the STORED end', async () => {
    await openSchedule(experiment({ start_date: '2026-11-01T17:00:00', end_date: '2026-11-02T17:00:00' }));
    pick('start', '2026-11-03T09:00');
    save();
    await expectRefused('start', END_TOO_SOON);
  });

  it('an end exactly an hour after the start is sent', async () => {
    await openSchedule();
    pick('start', '2026-10-31T09:00');
    pick('end', '2026-10-31T10:00');
    save();
    await screen.findByTestId('schedule-saved');
    expect(sentBody()).toEqual({ start_date: '2026-10-31T16:00:00.000Z', end_date: '2026-10-31T17:00:00.000Z' });
  });

  it('changing a picker clears the refusal', async () => {
    await openSchedule();
    pick('start', '2026-10-30T04:59');
    save();
    await screen.findByTestId('schedule-error');
    pick('start', '2026-10-31T09:00');
    expect(screen.queryByTestId('schedule-error')).toBeNull();
    expect(screen.getByTestId('schedule-start')).not.toHaveAttribute('aria-invalid');
  });
});

// ---------------------------------------------------------------------------
// The API's refusals: fixed copy, never the detail (QA #13, PE 1)
// ---------------------------------------------------------------------------

describe("the API's refusals are fixed copy", () => {
  beforeEach(() => {
    jest.useFakeTimers({ now: NOW });
  });

  const failing = (err: unknown): ScheduleHandler => () => {
    throw err;
  };

  const NEUTRAL_400 = 'The dates were not accepted, or this is no longer a draft. Reload the page.';

  it.each<[string, unknown, string]>([
    // The 400 the partial body can cause: the end against a start stored since the page loaded.
    ['400 "at least 1:00:00"', apiError(400, 'Experiment must run for at least 1:00:00'), NEUTRAL_400],
    ['400 not a draft', apiError(400, `Cannot schedule experiment with status active. ${PLANTED}`), NEUTRAL_400],
    ['403', apiError(403, PLANTED), 'Changing an experiment requires the ADMIN or DEVELOPER role.'],
    ['404', apiError(404, PLANTED), 'This experiment no longer exists.'],
    [
      '422',
      apiError(422, PLANTED),
      'The dates were not accepted. The start cannot be in the past, and the end must be at least an hour after the start.',
    ],
    ['500 with a JSON body', new ApiError({ status: 500, detail: PLANTED }), 'Could not save the dates. Try again.'],
    ['status 0', new ApiError({ status: 0, detail: PLANTED, message: PLANTED }), UNREACHABLE],
    ['a browser exception', new TypeError(`Cannot read properties of undefined ${PLANTED}`), 'Could not save the dates. Try again.'],
  ])('%s', async (_n, err, copy) => {
    await openSchedule(experiment(), failing(err));
    pick('start', '2026-10-31T09:00');
    save();
    const alert = await screen.findByTestId('schedule-error');
    expect(alert).toHaveTextContent(copy);
    expect(alert).toHaveAttribute('role', 'alert');
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(document.body.textContent).not.toContain('1:00:00');
    expect(document.body.textContent).not.toContain('Cannot read');
    // The answer is kept to try again, and focus is on Save.
    expect(screen.getByTestId('schedule-start')).toHaveValue('2026-10-31T09:00');
    await waitFor(() => expect(screen.getByTestId('schedule-save')).toHaveFocus());
    expect(screen.getByTestId('schedule-save')).not.toBeDisabled();
    expect(screen.queryByTestId('schedule-saved')).toBeNull();
  });

  it('the copy table is exactly the plan\'s, and status 0 is the page\'s UNREACHABLE', () => {
    expect(SCHEDULE_ERROR_COPY[400]).toBe(NEUTRAL_400);
    expect(SCHEDULE_ERROR_COPY[0]).toBe(UNREACHABLE);
  });
});

// ---------------------------------------------------------------------------
// After a save: the page re-renders from the answer (QA #14)
// ---------------------------------------------------------------------------

describe('after a save', () => {
  beforeEach(() => {
    jest.useFakeTimers({ now: NOW });
  });

  it('the header shows the answer\'s start as "Starts", never "Started", and the owner\'s name stays', async () => {
    await openSchedule();
    expect(screen.queryByTestId('experiment-start-date')).toBeNull();
    pick('start', '2026-11-02T09:00');
    save();
    expect(await screen.findByTestId('schedule-saved')).toHaveTextContent(SCHEDULE_SAVED);
    expect(screen.getByTestId('schedule-saved')).toHaveAttribute('role', 'status');
    expect(screen.queryByTestId('experiment-schedule-form')).toBeNull();

    const dd = screen.getByTestId('experiment-start-date');
    expect(dd.previousElementSibling?.textContent).toBe('Starts');
    expect(dd).toHaveTextContent(new Date('2026-11-02T17:00:00Z').toLocaleString());
    expect(Array.from(document.querySelectorAll('dl dt')).map((dt) => dt.textContent)).not.toContain('Started');
    // keepOwnerName: the schedule route answers without the owner's name.
    expect(screen.getByTestId('experiment-owner')).toHaveTextContent('Someone Else');
    // Only one GET of the experiment: the page took the PUT's answer, it did not reload.
    expect(callsTo('GET').filter(([url]) => url === '/api/v1/experiments/exp-1')).toHaveLength(1);
    await waitFor(() => expect(screen.getByTestId('experiment-schedule')).toHaveFocus());
  });

  it('the header shows what the server answered, not what was typed', async () => {
    await openSchedule(experiment(), (body, current) => ({
      ...(applySchedule(body, current) as Experiment),
      start_date: '2026-11-03T17:00:00',
    }));
    pick('start', '2026-11-02T09:00');
    save();
    await screen.findByTestId('schedule-saved');
    expect(screen.getByTestId('experiment-start-date')).toHaveTextContent(
      new Date('2026-11-03T17:00:00Z').toLocaleString(),
    );
  });

  it('reopened, the form is prefilled from the saved answer', async () => {
    await openSchedule();
    pick('start', '2026-11-02T09:00');
    save();
    await screen.findByTestId('schedule-saved');
    fireEvent.click(screen.getByTestId('experiment-schedule'));
    expect(screen.getByTestId('schedule-start')).toHaveValue('2026-11-02T09:00');
    expect(screen.queryByTestId('schedule-saved')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// The form among the page's other controls
// ---------------------------------------------------------------------------

describe('the form among the other controls', () => {
  it('opens on the start, and Cancel sends nothing and returns focus', async () => {
    await openSchedule();
    expect(screen.getByTestId('schedule-start')).toHaveFocus();
    expect(screen.getByTestId('experiment-schedule')).toBeDisabled();
    fireEvent.click(screen.getByTestId('schedule-cancel'));
    expect(screen.queryByTestId('experiment-schedule-form')).toBeNull();
    await waitFor(() => expect(screen.getByTestId('experiment-schedule')).toHaveFocus());
    expect(puts()).toEqual([]);
  });

  it('Edit details closes it, and Schedule closes Edit details', async () => {
    await openSchedule();
    fireEvent.click(screen.getByTestId('experiment-edit-details'));
    expect(screen.queryByTestId('experiment-schedule-form')).toBeNull();
    expect(screen.getByTestId('manage-edit-form')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('experiment-schedule'));
    expect(screen.queryByTestId('manage-edit-form')).toBeNull();
    expect(screen.getByTestId('experiment-schedule-form')).toBeInTheDocument();
  });

  it('Delete draft closes it', async () => {
    await openSchedule();
    fireEvent.click(screen.getByTestId('experiment-delete'));
    expect(screen.queryByTestId('experiment-schedule-form')).toBeNull();
    expect(screen.getByTestId('manage-delete-confirm')).toBeInTheDocument();
  });

  it('it goes once the draft is started, with what was typed unsent', async () => {
    await openSchedule();
    pick('start', '2026-11-02T09:00');
    await act(async () => {
      fireEvent.click(screen.getByTestId('action-start'));
    });
    await waitFor(() => expect(screen.getByTestId('experiment-status')).toHaveAttribute('data-status', 'active'));
    expect(screen.queryByTestId('experiment-schedule-form')).toBeNull();
    expect(screen.queryByTestId('experiment-schedule')).toBeNull();
    expect(puts()).toEqual([]);
  });

  it('the route is stable in the OpenAPI document, so the form shows no beta notice', async () => {
    const doc = JSON.parse(
      fs.readFileSync(path.join(__dirname, '../../../../docs/api/openapi-v1.full.json'), 'utf-8'),
    ) as { paths: Record<string, Record<string, { 'x-stability'?: string }>> };
    expect(doc.paths['/api/v1/experiments/{experiment_id}/schedule'].put['x-stability'] ?? 'stable').toBe('stable');
    await openSchedule();
    expect(screen.getByTestId('experiment-schedule-form').textContent).not.toMatch(/beta/i);
  });
});

// ---------------------------------------------------------------------------
// Accessibility (QA #28): axe per state, on the form itself. Real timers: axe
// schedules its own work. Dates are far ahead, so nothing is in the past.
// ---------------------------------------------------------------------------

describe('accessibility', () => {
  async function noViolations() {
    const form = screen.getByTestId('experiment-schedule-form');
    const result = await axe.run(form, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  }

  it('empty, filled, refused before sending, saving, and saved', async () => {
    let release: () => void = () => {};
    await openSchedule(experiment(), (body, current) => {
      const answer = applySchedule(body, current);
      return new Promise((resolve) => {
        release = () => resolve(answer);
      });
    });
    await noViolations(); // empty

    pick('start', '2099-01-05T09:00');
    pick('end', '2099-01-05T09:30');
    await noViolations(); // filled, with read-backs

    save();
    await screen.findByTestId('schedule-error');
    await noViolations(); // refused before sending

    pick('end', '2099-01-12T09:00');
    save();
    await waitFor(() => expect(screen.getByTestId('schedule-save')).toHaveTextContent('Saving…'));
    expect(screen.getByTestId('schedule-start')).toBeDisabled();
    await noViolations(); // saving

    await act(async () => {
      release();
    });
    expect(await screen.findByTestId('schedule-saved')).toHaveTextContent(SCHEDULE_SAVED);
    const region = screen.getByTestId('experiment-manage');
    const result = await axe.run(region, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]); // saved: the status, the buttons
  });

  it('a refusal from the API is announced and focus returns to Save', async () => {
    await openSchedule(experiment(), () => {
      throw apiError(422, PLANTED);
    });
    pick('start', '2099-01-05T09:00');
    save();
    const alert = await screen.findByTestId('schedule-error');
    expect(alert).toHaveAttribute('role', 'alert');
    await waitFor(() => expect(screen.getByTestId('schedule-save')).toHaveFocus());
    await noViolations();
  });

  it('each picker has a visible label, and its help, zone line and read-back describe it', async () => {
    await openSchedule(experiment({ start_date: '2099-01-05T17:00:00' }));
    const start = screen.getByLabelText('Start');
    expect(start).toBe(screen.getByTestId('schedule-start'));
    expect(start.getAttribute('aria-describedby')?.split(' ')).toEqual([
      'schedule-start-zone',
      'schedule-start-readback',
      'schedule-start-help',
    ]);
    const end = screen.getByLabelText('End (optional)');
    expect(end.getAttribute('aria-describedby')?.split(' ')).toEqual(['schedule-end-zone', 'schedule-end-help']);
    // Remove names its field.
    expect(screen.getByRole('button', { name: 'Remove the start' })).toBe(screen.getByTestId('schedule-start-remove'));
    expect(screen.getByRole('form', { name: 'Schedule' })).toBe(screen.getByTestId('experiment-schedule-form'));
  });
});
