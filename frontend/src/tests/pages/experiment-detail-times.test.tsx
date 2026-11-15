import React from 'react';
import { render, screen } from '@testing-library/react';
import ExperimentDetailPage, { scheduleLabel } from '@/pages/experiments/[id]';
import { apiFetch } from '@/services/api';
import { Experiment } from '@/types/experiments';
import { makeRouter, routedApi } from './helpers/apiMock';

// #1134. The API sends `start_date`, `end_date` and `created_at` as stored,
// UTC with no zone (`2026-10-11T05:02:00`). A browser reads that as LOCAL, so
// the page showed the UTC clock as if it were the viewer's. The suite runs in
// America/Los_Angeles (jest.global-setup.js); in UTC this could not fail.
//
// #1443. The page called every start date "Started" and every end date
// "Ended", a scheduled draft's included. The terms now come from the status
// first and the clock second: `scheduleLabel`.

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

jest.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'user-1', email: 'admin@demo.com', username: 'admin', role: 'ADMIN' },
    status: 'authenticated',
  }),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const EXPERIMENT: Experiment = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'completed',
  targeting_rules: null,
  tags: null,
  owner_id: 'user-1',
  start_date: '2026-10-11T05:02:00',
  end_date: '2026-10-13T17:30:15.123456',
  created_at: '2026-10-10T03:04:05',
  updated_at: '2026-10-13T17:30:15',
  variants: [
    { id: 'v-1', name: 'Control', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'Blue CTA', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [
    { id: 'm-1', name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true },
  ],
};

async function shownFor(label: string): Promise<string> {
  const row = (await screen.findByText(label)).closest('div');
  return row?.querySelector('dd')?.textContent ?? '';
}

function serve(overrides: Partial<Experiment> = {}) {
  const current = { ...EXPERIMENT, ...overrides };
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => current },
    ]) as unknown as typeof apiFetch,
  );
}

/** The header's terms, in order, once the page has loaded. */
async function headerTerms(overrides: Partial<Experiment>): Promise<(string | null)[]> {
  serve(overrides);
  render(<ExperimentDetailPage />);
  await screen.findByTestId('experiment-detail');
  return Array.from(document.querySelectorAll('dl dt')).map((dt) => dt.textContent);
}

/** The dd beside the dt that reads exactly `term`, or null. */
function shownBeside(term: string): string | null {
  const dt = Array.from(document.querySelectorAll('dl dt')).find((t) => t.textContent === term);
  return dt?.nextElementSibling?.textContent ?? null;
}

/** How the page writes an API time: UTC, shown in the browser's zone. */
function local(utc: string): string {
  return new Date(`${utc}Z`).toLocaleString();
}

describe('ExperimentDetailPage — times the API sends without a zone', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    serve();
  });

  it('runs in a zone that is not UTC', () => {
    expect(new Date(0).getTimezoneOffset()).not.toBe(0);
    expect(new Date(Date.UTC(2026, 9, 11, 5, 2)).toLocaleString('en-US')).toBe(
      '10/10/2026, 10:02:00 PM',
    );
  });

  it('shows Started, Ended and Created as UTC converted to local time', async () => {
    render(<ExperimentDetailPage />);

    // 05:02 UTC is 22:02 the evening before in Los Angeles (UTC-7 in October):
    // "10/10/2026, 10:02:00 PM" in the en-US form, not "10/11/2026, 5:02:00 AM".
    const started = await shownFor('Started');
    expect(started).toBe(new Date('2026-10-11T05:02:00Z').toLocaleString());
    expect(started).not.toBe(new Date(2026, 9, 11, 5, 2).toLocaleString());

    expect(await shownFor('Ended')).toBe(new Date('2026-10-13T17:30:15.123Z').toLocaleString());
    expect(await shownFor('Created')).toBe(new Date('2026-10-10T03:04:05Z').toLocaleString());
  });
});

// ---------------------------------------------------------------------------
// scheduleLabel, the pure rule (plan-1449 #1). The boundary is tested here
// only: a page test's findBy* moves a fake clock on by about 50 ms.
// ---------------------------------------------------------------------------

describe('scheduleLabel (#1443)', () => {
  const NOW = Date.UTC(2026, 9, 11, 8, 0, 0); // 2026-10-11T08:00:00Z
  // Zone-less, as the API sends them. Read as local time in Los Angeles they
  // would all be seven hours later, and none of them due.
  const BEFORE = '2026-10-11T07:59:59.999'; // now − 1 ms
  const AT = '2026-10-11T08:00:00'; // now
  const AFTER = '2026-10-11T08:00:00.001'; // now + 1 ms

  it.each([
    // A draft's start, and an active experiment's end, at the boundary: the
    // scheduler's `<=`, so "at now" is due.
    ['start', BEFORE, 'draft', 'Starts', true],
    ['start', AT, 'draft', 'Starts', true],
    ['start', AFTER, 'draft', 'Starts', false],
    ['end', BEFORE, 'active', 'Ends', true],
    ['end', AT, 'active', 'Ends', true],
    ['end', AFTER, 'active', 'Ends', false],
    // The status says it happened, whatever the clock says.
    ['start', AFTER, 'active', 'Started', false],
    ['start', AFTER, 'paused', 'Started', false],
    ['start', AFTER, 'completed', 'Started', false],
    ['end', AFTER, 'completed', 'Ended', false],
    // Passed, but the status says it has not happened: never Started or Ended.
    ['start', BEFORE, 'scheduled', 'Starts', true],
    ['end', BEFORE, 'paused', 'Ends', true],
    ['end', BEFORE, 'draft', 'Ends', true],
    // Archived (C1): the API archives a draft too, so the term is neutral.
    ['start', AFTER, 'archived', 'Start date', false],
    ['end', AFTER, 'archived', 'End date', false],
    ['start', BEFORE, 'archived', 'Start date', false],
    ['end', BEFORE, 'archived', 'End date', false],
    // A value that is not a time is never due.
    ['start', 'not a date', 'draft', 'Starts', false],
    ['end', 'not a date', 'active', 'Ends', false],
  ] as const)('%s %s while %s reads %s, due %s', (which, value, status, term, due) => {
    expect(scheduleLabel(which, value, status, NOW)).toEqual({ term, due });
  });
});

// ---------------------------------------------------------------------------
// The page, under a fake clock (plan-1449 #2, #3). `jest.spyOn(Date, 'now')`
// would not move `new Date()`; useFakeTimers({ now }) moves both. Each case
// keeps a margin of a day or more from the boundary.
// ---------------------------------------------------------------------------

describe('ExperimentDetailPage — Starts or Started, Ends or Ended (#1443)', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('a draft scheduled ahead reads Starts and Ends, never Started or Ended (#1443)', async () => {
    jest.useFakeTimers({ now: new Date('2000-06-01T00:00:00Z') });
    const terms = await headerTerms({
      status: 'draft',
      start_date: '2001-01-01T00:00:00',
      end_date: '2001-02-01T00:00:00',
    });
    expect(terms).toEqual(expect.arrayContaining(['Starts', 'Ends']));
    expect(terms).not.toContain('Started');
    expect(terms).not.toContain('Ended');
    expect(shownBeside('Starts')).toBe(local('2001-01-01T00:00:00'));
    expect(shownBeside('Ends')).toBe(local('2001-02-01T00:00:00'));
  });

  it('a draft whose dates have passed still reads Starts and Ends, each marked due', async () => {
    jest.useFakeTimers({ now: new Date('2100-06-01T00:00:00Z') });
    const terms = await headerTerms({
      status: 'draft',
      start_date: '2099-01-01T00:00:00',
      end_date: '2099-02-01T00:00:00',
    });
    expect(terms).not.toContain('Started');
    expect(terms).not.toContain('Ended');
    expect(screen.getByTestId('experiment-start-date')).toHaveTextContent(
      `${local('2099-01-01T00:00:00')} (due, not started yet)`,
    );
    expect(screen.getByTestId('experiment-end-date')).toHaveTextContent(
      `${local('2099-02-01T00:00:00')} (due, not ended yet)`,
    );
    expect(shownBeside('Starts')).toBe(`${local('2099-01-01T00:00:00')} (due, not started yet)`);
    expect(shownBeside('Ends')).toBe(`${local('2099-02-01T00:00:00')} (due, not ended yet)`);
  });

  it('a completed experiment reads Started and Ended, with nothing due', async () => {
    jest.useFakeTimers({ now: new Date('2100-06-01T00:00:00Z') });
    await headerTerms({
      status: 'completed',
      start_date: '2099-01-01T00:00:00',
      end_date: '2099-02-01T00:00:00',
    });
    expect(shownBeside('Started')).toBe(local('2099-01-01T00:00:00'));
    expect(shownBeside('Ended')).toBe(local('2099-02-01T00:00:00'));
  });

  it('a paused experiment past its end reads Started, and Ends marked due', async () => {
    jest.useFakeTimers({ now: new Date('2100-06-01T00:00:00Z') });
    const terms = await headerTerms({
      status: 'paused',
      start_date: '2099-01-01T00:00:00',
      end_date: '2099-02-01T00:00:00',
    });
    expect(terms).not.toContain('Ended');
    expect(shownBeside('Started')).toBe(local('2099-01-01T00:00:00'));
    expect(shownBeside('Ends')).toBe(`${local('2099-02-01T00:00:00')} (due, not ended yet)`);
  });

  it.each([
    ['ahead', '2101-01-01T00:00:00', '2101-02-01T00:00:00'],
    ['passed', '2099-01-01T00:00:00', '2099-02-01T00:00:00'],
  ])('an archived experiment with its dates %s reads Start date and End date', async (_, start, end) => {
    jest.useFakeTimers({ now: new Date('2100-06-01T00:00:00Z') });
    const terms = await headerTerms({ status: 'archived', start_date: start, end_date: end });
    expect(terms).not.toContain('Started');
    expect(terms).not.toContain('Ended');
    expect(shownBeside('Start date')).toBe(local(start));
    expect(shownBeside('End date')).toBe(local(end));
  });

  it('a date the API did not send has no row', async () => {
    jest.useFakeTimers({ now: new Date('2100-06-01T00:00:00Z') });
    const terms = await headerTerms({ status: 'draft', start_date: null, end_date: null });
    expect(terms).not.toContain('Starts');
    expect(terms).not.toContain('Ends');
    expect(screen.queryByTestId('experiment-start-date')).not.toBeInTheDocument();
    expect(screen.queryByTestId('experiment-end-date')).not.toBeInTheDocument();
  });

  // #1134 at the boundary that matters: three hours before now in UTC, four
  // hours after it if read as Los Angeles time.
  it('reads a zone-less date as UTC when deciding whether it is due', async () => {
    jest.useFakeTimers({ now: new Date('2026-10-11T08:00:00Z') });
    await headerTerms({
      status: 'draft',
      start_date: '2026-10-11T05:02:00',
      end_date: '2026-10-20T00:00:00',
    });
    expect(shownBeside('Starts')).toBe(`${local('2026-10-11T05:02:00')} (due, not started yet)`);
  });

  it('reads the zone-less end of an active experiment as UTC when deciding whether it is due', async () => {
    jest.useFakeTimers({ now: new Date('2026-10-11T08:00:00Z') });
    await headerTerms({
      status: 'active',
      start_date: '2026-10-01T00:00:00',
      end_date: '2026-10-11T05:02:00',
    });
    expect(shownBeside('Started')).toBe(local('2026-10-01T00:00:00'));
    expect(shownBeside('Ends')).toBe(`${local('2026-10-11T05:02:00')} (due, not ended yet)`);
  });
});
