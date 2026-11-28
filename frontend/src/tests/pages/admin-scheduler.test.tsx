/**
 * /admin/scheduler in jsdom: the real page, card, history list, AdminService
 * and apiFetch, with only `fetch` mocked. The responses are built from the
 * `SchedulerHealthResponse` and `SchedulerRunRecord` schemas of the committed
 * OpenAPI snapshot, so the page is fed what the API sends.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { SchedulerHealthPage } from '@/pages/admin/scheduler';
import { schemaExample } from '@/tests/helpers/schemaExample';
import { shapeErrors } from '@/tests/helpers/openapiShape';

jest.mock('@/components/admin/AdminLayout', () => ({
  AdminLayout: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

// The page reads who is signed in: a superuser who is an ADMIN (the one person
// the page's reads and the test button all accept; #2277).
jest.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    status: 'authenticated',
    user: { id: 'user-1', role: 'ADMIN', is_superuser: true },
  }),
}));

const mockFetch = jest.fn();

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: '',
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

const HEALTH = [
  schemaExample('SchedulerHealthResponse', {
    scheduler_name: 'experiment',
    is_running: true,
    last_run_status: 'success',
    last_run_at: '2026-11-20T10:00:00',
    next_run_at: '2026-11-20T10:15:00',
    consecutive_failures: 0,
  }),
  schemaExample('SchedulerHealthResponse', {
    scheduler_name: 'safety',
    is_running: false,
    last_run_status: 'failed',
    last_run_at: null,
    next_run_at: null,
    consecutive_failures: 4,
  }),
];

const HISTORY = [
  schemaExample('SchedulerRunRecord', {
    id: 'run-a',
    scheduler_name: 'safety',
    started_at: '2026-11-20T09:00:00',
    completed_at: '2026-11-20T09:00:02',
    status: 'partial',
    items_processed: 12,
    items_failed: 3,
    error_message: 'flag 7 timed out',
  }),
];

describe('the responses the page is fed', () => {
  it('are the API\'s SchedulerHealthResponse and SchedulerRunRecord', () => {
    for (const item of HEALTH) expect(shapeErrors('SchedulerHealthResponse', item)).toEqual([]);
    for (const item of HISTORY) expect(shapeErrors('SchedulerRunRecord', item)).toEqual([]);
  });
});

let urls: string[];

beforeEach(() => {
  urls = [];
  mockFetch.mockReset();
  mockFetch.mockImplementation((url: string) => {
    urls.push(url);
    const { pathname } = new URL(url, 'http://localhost');
    if (pathname === '/api/v1/scheduler/health') return Promise.resolve(ok(HEALTH));
    if (pathname === '/api/v1/scheduler/safety/history') return Promise.resolve(ok(HISTORY));
    return Promise.reject(new Error(`unrouted ${url}`));
  });
  (global as unknown as { fetch: typeof fetch }).fetch = mockFetch as unknown as typeof fetch;
});

describe('/admin/scheduler with the API response', () => {
  it('shows each scheduler by its name, whether it runs, and its last and next run', async () => {
    render(<SchedulerHealthPage />);
    const cards = await screen.findAllByTestId('scheduler-status-card');
    expect(cards).toHaveLength(2);

    const [experiment, safety] = cards;
    expect(within(experiment).getByText('experiment')).toBeInTheDocument();
    expect(within(experiment).getByTestId('status-badge')).toHaveTextContent('running');
    expect(within(experiment).getByTestId('last-run-status')).toHaveTextContent('success');
    expect(within(experiment).getByTestId('last-run').textContent).toMatch(/2026/);
    expect(within(experiment).getByTestId('next-run').textContent).toMatch(/2026/);

    expect(within(safety).getByText('safety')).toBeInTheDocument();
    expect(within(safety).getByTestId('status-badge')).toHaveTextContent('idle');
    expect(within(safety).getByTestId('last-run-status')).toHaveTextContent('failed');
    expect(within(safety).getByTestId('last-run')).toHaveTextContent('—');
    expect(within(safety).getByTestId('consecutive-failures')).toHaveTextContent('4');
  });

  it('never prints undefined or NaN', async () => {
    render(<SchedulerHealthPage />);
    await screen.findAllByTestId('scheduler-status-card');
    const text = screen.getByTestId('scheduler-health-page').textContent ?? '';
    expect(text).not.toMatch(/undefined|NaN|Invalid Date/);
  });

  it('Show History asks for the scheduler by name and lists its runs', async () => {
    render(<SchedulerHealthPage />);
    const cards = await screen.findAllByTestId('scheduler-status-card');
    fireEvent.click(within(cards[1]).getByTestId('expand-button'));

    const row = await screen.findByTestId('run-row-run-a');
    expect(urls.some((u) => u.endsWith('/api/v1/scheduler/safety/history'))).toBe(true);
    expect(urls.some((u) => u.includes('/undefined/'))).toBe(false);
    expect(within(row).getByTestId('status-badge-run-a')).toHaveTextContent('partial');
    expect(within(row).getByTestId('processed-run-a')).toHaveTextContent('12');
    expect(within(row).getByTestId('failed-run-a')).toHaveTextContent('3');
    expect(within(row).getByText('flag 7 timed out')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId('run-history-loading')).not.toBeInTheDocument());
  });
});
