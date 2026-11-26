import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { SchedulerStatusCard } from '@/components/admin/scheduler/SchedulerStatusCard';
import { SchedulerHealth } from '@/types/admin';
import { schemaExample } from '@/tests/helpers/schemaExample';

// Mock the child SchedulerRunHistory component so we can test expand/collapse independently
jest.mock('@/components/admin/scheduler/SchedulerRunHistory', () => ({
  SchedulerRunHistory: ({ schedulerName }: { schedulerName: string }) => (
    <div data-testid="scheduler-run-history">Run history for {schedulerName}</div>
  ),
}));

// What `GET /api/v1/scheduler/health` sends, built from its schema.
const makeScheduler = (overrides: Record<string, unknown> = {}): SchedulerHealth =>
  schemaExample<SchedulerHealth>('SchedulerHealthResponse', {
    scheduler_name: 'experiment',
    is_running: false,
    last_run_status: 'success',
    last_run_at: '2024-06-15T10:00:00Z',
    next_run_at: '2024-06-15T10:15:00Z',
    consecutive_failures: 0,
    ...overrides,
  });

describe('SchedulerStatusCard', () => {
  it('renders scheduler name', () => {
    render(<SchedulerStatusCard scheduler={makeScheduler({ scheduler_name: 'rollout' })} />);
    expect(screen.getByText('rollout')).toBeInTheDocument();
  });

  it('shows "idle" in a grey badge when the scheduler is not running', () => {
    render(<SchedulerStatusCard scheduler={makeScheduler({ is_running: false })} />);
    const badge = screen.getByTestId('status-badge');
    expect(badge).toHaveTextContent('idle');
    expect(badge.className).toMatch(/slate/i);
  });

  it('shows "running" in a blue badge when the scheduler is running', () => {
    render(<SchedulerStatusCard scheduler={makeScheduler({ is_running: true })} />);
    const badge = screen.getByTestId('status-badge');
    expect(badge).toHaveTextContent('running');
    expect(badge.className).toMatch(/blue/i);
  });

  it.each([
    ['success', /green/],
    ['partial', /amber/],
    ['failed', /red/],
    ['skipped', /slate/],
  ])('shows the last run status %s in its own colour', (status, colour) => {
    render(<SchedulerStatusCard scheduler={makeScheduler({ last_run_status: status })} />);
    const badge = screen.getByTestId('last-run-status');
    expect(badge).toHaveTextContent(status);
    expect(badge.className).toMatch(colour);
  });

  it('shows dashes when the scheduler has not run yet', () => {
    render(
      <SchedulerStatusCard
        scheduler={makeScheduler({ last_run_at: null, last_run_status: null, next_run_at: null })}
      />,
    );
    expect(screen.getByTestId('last-run')).toHaveTextContent('—');
    expect(screen.getByTestId('last-run-status')).toHaveTextContent('—');
    expect(screen.getByTestId('next-run')).toHaveTextContent('—');
  });

  it('shows failures in a row', () => {
    render(<SchedulerStatusCard scheduler={makeScheduler({ consecutive_failures: 5 })} />);
    expect(screen.getByTestId('consecutive-failures')).toHaveTextContent('5');
  });

  it('shows no run count or error count, which the API does not send', () => {
    render(<SchedulerStatusCard scheduler={makeScheduler()} />);
    expect(screen.queryByTestId('run-count')).not.toBeInTheDocument();
    expect(screen.queryByTestId('error-count')).not.toBeInTheDocument();
  });

  it('renders last_run_at and next_run_at as times, not the raw string', () => {
    render(<SchedulerStatusCard scheduler={makeScheduler()} />);
    expect(screen.getByTestId('last-run').textContent).not.toContain('2024-06-15T');
    expect(screen.getByTestId('next-run').textContent).not.toContain('2024-06-15T');
    expect(screen.getByTestId('last-run').textContent).toMatch(/2024|6\/15|15/);
  });

  it('expand button toggles run history section visibility', () => {
    render(<SchedulerStatusCard scheduler={makeScheduler()} />);
    expect(screen.queryByTestId('scheduler-run-history')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('expand-button'));
    expect(screen.getByTestId('scheduler-run-history')).toHaveTextContent('Run history for experiment');

    fireEvent.click(screen.getByTestId('expand-button'));
    expect(screen.queryByTestId('scheduler-run-history')).not.toBeInTheDocument();
  });

  it('renders with data-testid="scheduler-status-card"', () => {
    render(<SchedulerStatusCard scheduler={makeScheduler()} />);
    expect(screen.getByTestId('scheduler-status-card')).toBeInTheDocument();
  });
});
