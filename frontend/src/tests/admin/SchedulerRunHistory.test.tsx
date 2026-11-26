import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { SchedulerRunHistory } from '@/components/admin/scheduler/SchedulerRunHistory';
import { AdminService } from '@/services/admin';
import { SchedulerRun } from '@/types/admin';
import { schemaExample } from '@/tests/helpers/schemaExample';

jest.mock('@/services/admin');

const mockGetSchedulerHistory = AdminService.getSchedulerHistory as jest.Mock;

// What `GET /api/v1/scheduler/{name}/history` sends, built from its schema.
const makeRun = (overrides: Record<string, unknown> = {}): SchedulerRun =>
  schemaExample<SchedulerRun>('SchedulerRunRecord', {
    id: 'run-1',
    scheduler_name: 'experiment',
    started_at: '2024-06-15T10:00:00Z',
    completed_at: '2024-06-15T10:00:05Z',
    status: 'success',
    items_processed: 7,
    items_failed: 0,
    error_message: null,
    ...overrides,
  });

beforeEach(() => {
  jest.clearAllMocks();
});

describe('SchedulerRunHistory', () => {
  it('renders loading state while fetching', () => {
    mockGetSchedulerHistory.mockImplementation(() => new Promise(() => {}));
    render(<SchedulerRunHistory schedulerName="experiment" />);
    expect(screen.getByTestId('run-history-loading')).toBeInTheDocument();
  });

  it('asks for the history of the scheduler it was given', async () => {
    mockGetSchedulerHistory.mockResolvedValue([]);
    render(<SchedulerRunHistory schedulerName="rollout" />);
    await waitFor(() => expect(screen.getByTestId('run-history-empty')).toBeInTheDocument());
    expect(mockGetSchedulerHistory).toHaveBeenCalledWith('rollout');
  });

  it('renders run history rows after load', async () => {
    mockGetSchedulerHistory.mockResolvedValue([makeRun({ id: 'run-1' }), makeRun({ id: 'run-2' })]);
    render(<SchedulerRunHistory schedulerName="experiment" />);
    await waitFor(() => {
      expect(screen.getByTestId('run-row-run-1')).toBeInTheDocument();
      expect(screen.getByTestId('run-row-run-2')).toBeInTheDocument();
    });
  });

  it('renders a run whose id is null, keyed by its start time', async () => {
    mockGetSchedulerHistory.mockResolvedValue([makeRun({ id: null })]);
    render(<SchedulerRunHistory schedulerName="experiment" />);
    await waitFor(() => {
      expect(screen.getByTestId('run-row-2024-06-15T10:00:00Z')).toBeInTheDocument();
    });
  });

  it.each([
    ['success', /green/],
    ['partial', /amber/],
    ['failed', /red/],
    ['skipped', /slate/],
  ])('shows status %s in its own colour', async (status, colour) => {
    mockGetSchedulerHistory.mockResolvedValue([makeRun({ id: 'r', status })]);
    render(<SchedulerRunHistory schedulerName="experiment" />);
    await waitFor(() => {
      const badge = screen.getByTestId('status-badge-r');
      expect(badge).toHaveTextContent(status);
      expect(badge.className).toMatch(colour);
    });
  });

  it('shows items processed and items failed', async () => {
    mockGetSchedulerHistory.mockResolvedValue([
      makeRun({ id: 'run-1', items_processed: 12, items_failed: 3, status: 'partial' }),
    ]);
    render(<SchedulerRunHistory schedulerName="experiment" />);
    await waitFor(() => {
      expect(screen.getByTestId('processed-run-1')).toHaveTextContent('12');
      expect(screen.getByTestId('failed-run-1')).toHaveTextContent('3');
    });
  });

  it('shows the error message of a failed run', async () => {
    mockGetSchedulerHistory.mockResolvedValue([
      makeRun({ id: 'run-err', status: 'failed', error_message: 'DB connection failed' }),
    ]);
    render(<SchedulerRunHistory schedulerName="experiment" />);
    await waitFor(() => {
      expect(screen.getByText('DB connection failed')).toBeInTheDocument();
    });
  });

  it('renders with data-testid="scheduler-run-history"', () => {
    mockGetSchedulerHistory.mockImplementation(() => new Promise(() => {}));
    render(<SchedulerRunHistory schedulerName="experiment" />);
    expect(screen.getByTestId('scheduler-run-history')).toBeInTheDocument();
  });
});
