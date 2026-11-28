/**
 * The experiment page's "Schedule resume" (#2283): a PAUSED experiment's
 * resume time, set in the browser's time zone and sent as `start_date` to
 * `PUT /experiments/{id}/schedule`, which stores it as `resume_at`.
 *
 * Every case renders the page module itself with `apiFetch` mocked and a
 * RESOLVED user (a null user is offered everything). The suite runs in
 * America/Los_Angeles with the clock faked at 2026-10-30T12:00Z.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { apiFetch } from '@/services/api';
import { Experiment } from '@/types/experiments';
import {
  RESUME_ERROR_COPY,
  RESUME_HELP,
  RESUME_IN_PAST,
  RESUME_SAVED,
  RESUME_TOO_LATE,
} from '@/components/experiments/ExperimentScheduleSection';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';
import { shapeErrors } from '../helpers/openapiShape';

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

const PAUSED: Experiment = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'paused',
  targeting_rules: null,
  tags: null,
  owner_id: 'someone-else',
  owner_name: 'Someone Else',
  start_date: '2026-10-20T10:00:00',
  end_date: null,
  resume_at: null,
  created_at: '2026-10-01T10:00:00',
  updated_at: '2026-10-01T10:00:00',
  variants: [
    { id: 'v-1', name: 'Control', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'Blue CTA', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [{ id: 'm-1', name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true }],
};

const paused = (overrides: Partial<Experiment> = {}): Experiment => ({ ...PAUSED, ...overrides });

const as = (role: string, extra: Record<string, unknown> = {}) => ({
  id: 'user-1',
  email: `${role.toLowerCase()}@demo.com`,
  username: role.toLowerCase(),
  role,
  is_superuser: false,
  ...extra,
});

const signIn = (user: Record<string, unknown>) => mockUseAuth.mockReturnValue({ user, status: 'authenticated' });

/** Text the server might send, which must never reach the page. */
const PLANTED = 'Traceback (most recent call last): PLANTED-resume-4d2';

/** The API stores an instant as UTC with no zone, and answers with it so. */
const stored = (instant: string | null | undefined) =>
  instant === null || instant === undefined ? instant : new Date(instant).toISOString().slice(0, 19);

type Handler = (body: Record<string, unknown>, current: Experiment) => unknown;

/** The server's answer: `start_date` is stored as `resume_at`; the start date never moves. */
const applyResume: Handler = (body, current) => ({
  ...current,
  owner_name: undefined,
  ...('start_date' in body ? { resume_at: stored(body.start_date as string | null) } : {}),
  ...('end_date' in body ? { end_date: stored(body.end_date as string | null) } : {}),
});

function install(current: Experiment, schedule: Handler = applyResume) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => current },
      { path: '/api/v1/holdout', handler: () => null },
      {
        method: 'PUT',
        path: SCHEDULE_PATH,
        handler: (_p, o) => schedule(o.json as Record<string, unknown>, current),
      },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderPage(current: Experiment = paused(), schedule?: Handler) {
  install(current, schedule);
  const view = render(<ExperimentDetailPage />);
  await screen.findByTestId('experiment-detail');
  return view;
}

async function openResume(current: Experiment = paused(), schedule?: Handler) {
  await renderPage(current, schedule);
  fireEvent.click(screen.getByTestId('experiment-schedule'));
  return screen.getByTestId('experiment-schedule-resume');
}

const pick = (value: string) => fireEvent.change(screen.getByTestId('schedule-start'), { target: { value } });
const save = () => fireEvent.click(screen.getByTestId('schedule-save'));
const puts = () => mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'PUT');
const toTheRoute = () => mockedApiFetch.mock.calls.filter(([url]) => String(url).endsWith('/schedule'));

const sentBody = () => {
  expect(puts()).toHaveLength(1);
  const [url, options] = puts()[0];
  expect(url).toBe(SCHEDULE_PATH);
  return options?.json as Record<string, unknown>;
};

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn(as('DEVELOPER'));
});

afterEach(() => {
  jest.useRealTimers();
});

describe('who is offered Schedule resume on a paused experiment', () => {
  const offered: [string, Record<string, unknown>][] = [
    ['superuser VIEWER', as('VIEWER', { is_superuser: true })],
    ['ADMIN', as('ADMIN')],
    ['DEVELOPER', as('DEVELOPER')],
  ];
  const refused: [string, Record<string, unknown>][] = [
    ['ANALYST', as('ANALYST')],
    ['VIEWER', as('VIEWER')],
  ];

  it.each(offered)('%s sees the button, and the form opens with its witness', async (_name, user) => {
    signIn(user);
    const form = await openResume();
    expect(screen.getByTestId('experiment-schedule')).toHaveTextContent(/^Schedule resume$/);
    expect(form).toBeInTheDocument();
  });

  it.each(refused)('%s loads the page, sees no control, and sends nothing to the route', async (_name, user) => {
    signIn(user);
    await renderPage();
    expect(screen.queryByTestId('experiment-schedule')).toBeNull();
    expect(screen.queryByTestId('experiment-schedule-resume')).toBeNull();
    expect(toTheRoute()).toEqual([]);
    expect(puts()).toEqual([]);
  });

  it('the control is offered on a paused experiment only (a presence check beside the absences)', async () => {
    await renderPage(paused({ status: 'active' }));
    expect(screen.queryByTestId('experiment-schedule')).toBeNull();
  });

  it('every role sees when it resumes, and the page offers no form to ANALYST', async () => {
    signIn(as('ANALYST'));
    await renderPage(paused({ resume_at: '2026-11-02T17:00:00' }));
    expect(screen.getByTestId('experiment-resume-at')).toHaveTextContent(/2026/);
    expect(screen.queryByTestId('experiment-schedule')).toBeNull();
  });
});

describe('the form', () => {
  beforeEach(() => {
    jest.useFakeTimers({ now: NOW });
  });

  it('has one picker, "Resume at", and no end picker', async () => {
    await openResume();
    expect(screen.getByLabelText('Resume at')).toBe(screen.getByTestId('schedule-start'));
    expect(screen.queryByTestId('schedule-end')).toBeNull();
    expect(screen.getByTestId('schedule-save')).toHaveTextContent('Save resume time');
  });

  it('opens on the stored resume_at, in the browser zone, not on the start date', async () => {
    await openResume(paused({ resume_at: '2026-11-02T17:00:00' }));
    expect(screen.getByTestId('schedule-start')).toHaveValue('2026-11-02T09:00');
  });

  it('the help names what happens and carries no caveat about safety checks', async () => {
    await openResume();
    const help = screen.getByTestId('schedule-start-help');
    expect((help.textContent ?? '').replace(/\s+/g, ' ')).toBe(RESUME_HELP);
    expect(help.textContent).not.toMatch(/safety|rollback/i);
  });

  it('a pick is sent as start_date alone, as an instant with Z, and the body fits the API', async () => {
    await openResume();
    pick('2026-11-02T09:00');
    save();
    await screen.findByTestId('schedule-saved');
    const body = sentBody();
    expect(body).toEqual({ start_date: '2026-11-02T17:00:00.000Z' });
    expect(shapeErrors('ScheduleConfig', body)).toEqual([]);
    expect(screen.getByTestId('schedule-saved')).toHaveTextContent(RESUME_SAVED);
    expect(screen.getByTestId('experiment-resume-at')).toHaveTextContent(/2026/);
    expect(screen.getByTestId('experiment-resume-at')).not.toHaveTextContent('Not scheduled');
  });

  it('a body with a number for the date does not fit the API (positive control for the shape check)', () => {
    expect(shapeErrors('ScheduleConfig', { start_date: 5 })).not.toEqual([]);
  });

  it('Remove sends null for the resume time alone, and the page then says none is scheduled', async () => {
    await openResume(paused({ resume_at: '2026-11-02T17:00:00' }));
    fireEvent.click(screen.getByTestId('schedule-start-remove'));
    expect(screen.getByTestId('schedule-start')).toHaveValue('');
    save();
    await screen.findByTestId('schedule-saved');
    expect(sentBody()).toEqual({ start_date: null });
    expect(screen.getByTestId('experiment-resume-at')).toHaveTextContent('Not scheduled');
  });

  it('nothing changed sends nothing', async () => {
    await openResume(paused({ resume_at: '2026-11-02T17:00:00' }));
    save();
    await waitFor(() => expect(screen.queryByTestId('experiment-schedule-resume')).toBeNull());
    expect(puts()).toEqual([]);
  });

  it('a resume time in the past is refused before anything is sent, at the field', async () => {
    await openResume();
    pick('2026-10-30T04:00'); // 11:00Z, an hour before now
    save();
    const alert = await screen.findByTestId('schedule-error');
    expect(alert).toHaveTextContent(RESUME_IN_PAST);
    expect(screen.getByTestId('schedule-start')).toHaveAttribute('aria-invalid', 'true');
    expect(puts()).toEqual([]);
  });

  it('a resume time less than an hour before the stored end is refused before sending', async () => {
    await openResume(paused({ end_date: '2026-11-05T17:00:00' }));
    pick('2026-11-05T08:30'); // 16:30Z, half an hour before the end
    save();
    expect(await screen.findByTestId('schedule-error')).toHaveTextContent(RESUME_TOO_LATE);
    expect(puts()).toEqual([]);
    pick('2026-11-05T07:00');
    save();
    await screen.findByTestId('schedule-saved');
    expect(sentBody()).toEqual({ start_date: '2026-11-05T15:00:00.000Z' });
  });

  it.each([
    [400, RESUME_ERROR_COPY[400]],
    [403, RESUME_ERROR_COPY[403]],
    [404, RESUME_ERROR_COPY[404]],
    [422, RESUME_ERROR_COPY[422]],
    [500, RESUME_ERROR_COPY.fallback],
  ])('a %s from the API shows fixed copy, never the server detail', async (status, copy) => {
    await openResume(paused(), () => {
      throw apiError(status, PLANTED);
    });
    pick('2026-11-02T09:00');
    save();
    const alert = await screen.findByTestId('schedule-error');
    expect(alert).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('the sentences name the resume time, not dates', () => {
    for (const status of [400, 422]) expect(RESUME_ERROR_COPY[status]).toMatch(/resume time/);
  });
});

describe('accessibility', () => {
  it('the picker has a visible label and is described by its zone line, help and any error', async () => {
    await openResume(paused({ resume_at: '2099-01-05T17:00:00' }));
    const input = screen.getByLabelText('Resume at');
    expect(input.getAttribute('aria-describedby')?.split(' ')).toEqual([
      'schedule-start-zone',
      'schedule-start-readback',
      'schedule-start-help',
    ]);
    const form = screen.getByTestId('experiment-schedule-resume');
    const result = await axe.run(form, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('a refusal is announced and the field is marked invalid', async () => {
    await openResume(paused(), () => {
      throw apiError(422, PLANTED);
    });
    pick('2099-01-05T09:00');
    save();
    expect(await screen.findByTestId('schedule-error')).toHaveAttribute('role', 'alert');
    await act(async () => {});
    await waitFor(() => expect(screen.getByTestId('schedule-save')).toHaveFocus());
  });
});
