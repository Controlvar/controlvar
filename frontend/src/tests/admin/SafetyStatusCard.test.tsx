import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import axe from 'axe-core';
import { SafetyStatusCard } from '@/components/admin/safety/SafetyStatusCard';
import { flagHealth, toFlagSafetyStatus } from '@/services/admin';
import { FlagSafetyStatus } from '@/types/admin';
import { SafetyCheckResponse, SafetyMetricStatus } from '@/types/safety';

const metric = (overrides: Partial<SafetyMetricStatus> = {}): SafetyMetricStatus => ({
  name: 'error_rate',
  current_value: 0.025,
  threshold: 0.05,
  is_healthy: true,
  details: { warning: false, measured: true },
  ...overrides,
});

const check = (overrides: Partial<SafetyCheckResponse> = {}): SafetyCheckResponse => ({
  feature_flag_id: 'flag-123',
  is_healthy: true,
  metrics: [metric(), metric({ name: 'latency', current_value: 120, threshold: 500 })],
  last_checked: '2026-01-01T00:00:00Z',
  details: { feature_flag_key: 'my-feature-flag' },
  ...overrides,
});

const makeFlag = (overrides: Partial<FlagSafetyStatus> = {}): FlagSafetyStatus => ({
  ...toFlagSafetyStatus(
    { id: 'flag-123', name: 'my-feature-flag', key: 'my-feature-flag', status: 'active' },
    check(),
  ),
  ...overrides,
});

describe('toFlagSafetyStatus / flagHealth', () => {
  it('maps a healthy check to "healthy" with error rate and latency metrics', () => {
    const status = toFlagSafetyStatus({ id: 'f', name: 'Flag', key: 'flag' }, check());
    expect(status.health).toBe('healthy');
    expect(status.error_rate).toBe(0.025);
    expect(status.latency_ms).toBe(120);
    expect(status.flag_key).toBe('flag');
  });

  it('is "critical" when the backend reports unhealthy', () => {
    expect(flagHealth(check({ is_healthy: false }))).toBe('critical');
  });

  it('is "warning" when a metric breached its warning threshold only', () => {
    const c = check({ metrics: [metric({ details: { warning: true, measured: true } })] });
    expect(flagHealth(c)).toBe('warning');
  });

  it('reports null for metrics the backend could not measure', () => {
    const c = check({
      metrics: [metric({ details: { warning: false, measured: false } })],
      details: { unmeasured_metrics: ['error_rate'] },
    });
    const status = toFlagSafetyStatus({ id: 'f', name: 'Flag', key: 'flag' }, c);
    expect(status.error_rate).toBeNull();
    expect(status.latency_ms).toBeNull();
  });
});

describe('SafetyStatusCard', () => {
  it('renders flag name and key', () => {
    const flag = makeFlag({ flag_name: 'Checkout v2', flag_key: 'checkout-v2' });
    render(<SafetyStatusCard flag={flag} onRollback={jest.fn()} />);
    expect(screen.getByText('Checkout v2')).toBeInTheDocument();
    expect(screen.getByText('checkout-v2')).toBeInTheDocument();
  });

  it('renders error_rate fraction as a percentage', () => {
    const flag = makeFlag({ error_rate: 0.0375 });
    render(<SafetyStatusCard flag={flag} onRollback={jest.fn()} />);
    expect(screen.getByTestId('error-rate-value')).toHaveTextContent('3.75%');
  });

  it('renders latency with "ms" suffix', () => {
    const flag = makeFlag({ latency_ms: 250 });
    render(<SafetyStatusCard flag={flag} onRollback={jest.fn()} />);
    expect(screen.getByTestId('latency-value')).toHaveTextContent('250 ms');
  });

  it('renders n/a when a metric is not measured', () => {
    const flag = makeFlag({ error_rate: null, latency_ms: null });
    render(<SafetyStatusCard flag={flag} onRollback={jest.fn()} />);
    expect(screen.getByTestId('error-rate-value')).toHaveTextContent('n/a');
    expect(screen.getByTestId('latency-value')).toHaveTextContent('n/a');
  });

  it('shows green badge for "healthy"', () => {
    render(<SafetyStatusCard flag={makeFlag({ health: 'healthy' })} onRollback={jest.fn()} />);
    expect(screen.getByTestId('status-badge')).toHaveClass('bg-green-100');
  });

  it('shows yellow badge for "warning"', () => {
    render(<SafetyStatusCard flag={makeFlag({ health: 'warning' })} onRollback={jest.fn()} />);
    expect(screen.getByTestId('status-badge')).toHaveClass('bg-yellow-100');
  });

  it('shows red badge for "critical"', () => {
    render(<SafetyStatusCard flag={makeFlag({ health: 'critical' })} onRollback={jest.fn()} />);
    expect(screen.getByTestId('status-badge')).toHaveClass('bg-red-100');
  });

  it('"Roll back" button is visible for critical/warning only and reports the flag id', () => {
    const onRollback = jest.fn();
    const { rerender } = render(
      <SafetyStatusCard flag={makeFlag({ health: 'warning' })} onRollback={onRollback} />,
    );
    fireEvent.click(screen.getByTestId('rollback-button'));
    expect(onRollback).toHaveBeenCalledWith('flag-123');

    rerender(<SafetyStatusCard flag={makeFlag({ health: 'critical' })} onRollback={onRollback} />);
    expect(screen.getByTestId('rollback-button')).toBeInTheDocument();

    rerender(<SafetyStatusCard flag={makeFlag({ health: 'healthy' })} onRollback={onRollback} />);
    expect(screen.queryByTestId('rollback-button')).not.toBeInTheDocument();
  });

  it('notes when no metrics are configured or some are unmeasured', () => {
    const none = makeFlag({ check: check({ metrics: [] }) });
    const { rerender } = render(<SafetyStatusCard flag={none} onRollback={jest.fn()} />);
    expect(screen.getByTestId('safety-disabled-note')).toBeInTheDocument();

    const partial = makeFlag({ check: check({ details: { unmeasured_metrics: ['latency'] } }) });
    rerender(<SafetyStatusCard flag={partial} onRollback={jest.fn()} />);
    expect(screen.getByTestId('safety-unmeasured-note')).toHaveTextContent('latency');
  });

  it('notes the error reports that were stored but not counted, only when there are some', () => {
    const some = makeFlag({ check: check({ details: { uncounted_error_count: 3 } }) });
    const { rerender } = render(<SafetyStatusCard flag={some} onRollback={jest.fn()} />);
    const note = screen.getByTestId('safety-uncounted-note');
    expect(note).toHaveTextContent('3 reports not counted');
    // slate-500 on white is 4.76:1 (AA); slate-400 is about 2.6:1.
    expect(note.className).toContain('text-slate-500');

    rerender(
      <SafetyStatusCard
        flag={makeFlag({ check: check({ details: { uncounted_error_count: 1 } }) })}
        onRollback={jest.fn()}
      />,
    );
    expect(screen.getByTestId('safety-uncounted-note')).toHaveTextContent('1 report not counted');

    rerender(
      <SafetyStatusCard
        flag={makeFlag({ check: check({ details: { uncounted_error_count: 0 } }) })}
        onRollback={jest.fn()}
      />,
    );
    expect(screen.queryByTestId('safety-uncounted-note')).not.toBeInTheDocument();

    rerender(<SafetyStatusCard flag={makeFlag()} onRollback={jest.fn()} />);
    expect(screen.queryByTestId('safety-uncounted-note')).not.toBeInTheDocument();
  });

  it('renders with data-testid="safety-status-card"', () => {
    render(<SafetyStatusCard flag={makeFlag()} onRollback={jest.fn()} />);
    expect(screen.getByTestId('safety-status-card')).toBeInTheDocument();
  });
});

describe('SafetyStatusCard when the flag is off (#629)', () => {
  it('carries the list item status: active is on, inactive and archived are off', () => {
    const base = { id: 'f', name: 'Flag', key: 'flag' };
    expect(toFlagSafetyStatus({ ...base, status: 'active' }, check()).is_on).toBe(true);
    expect(toFlagSafetyStatus({ ...base, status: 'ACTIVE' }, check()).is_on).toBe(true);
    expect(toFlagSafetyStatus({ ...base, status: 'inactive' }, check()).is_on).toBe(false);
    expect(toFlagSafetyStatus({ ...base, status: 'archived' }, check()).is_on).toBe(false);
    expect(toFlagSafetyStatus({ ...base, is_active: false }, check()).is_on).toBe(false);
  });

  it('labels the button with the verb "Roll back"', () => {
    render(<SafetyStatusCard flag={makeFlag({ health: 'critical' })} onRollback={jest.fn()} />);
    expect(screen.getByTestId('rollback-button')).toHaveTextContent(/^Roll back$/);
  });

  it.each(['critical', 'warning', 'healthy'] as const)(
    'an inactive %s flag shows "Off" as text and no Roll back button',
    (health) => {
      const flag = makeFlag({ health, is_on: false });
      render(<SafetyStatusCard flag={flag} onRollback={jest.fn()} />);
      expect(screen.getByTestId('flag-off-indicator')).toHaveTextContent(/^Off$/);
      expect(screen.queryByTestId('rollback-button')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /roll ?back/i })).not.toBeInTheDocument();
    },
  );

  it('an active flag shows no "Off" indicator', () => {
    render(<SafetyStatusCard flag={makeFlag({ health: 'critical' })} onRollback={jest.fn()} />);
    expect(screen.queryByTestId('flag-off-indicator')).not.toBeInTheDocument();
  });
});

describe('SafetyStatusCard accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  it.each([
    ['critical and on', { health: 'critical', is_on: true }],
    ['critical and off', { health: 'critical', is_on: false }],
    ['healthy and on', { health: 'healthy', is_on: true }],
  ] as const)('has no axe violations: %s', async (_label, overrides) => {
    const { container } = render(
      <SafetyStatusCard flag={makeFlag({ ...overrides })} onRollback={jest.fn()} />,
    );
    const result = await axe.run(container, axeOptions);
    expect(result.violations.map((v) => v.id)).toStrictEqual([]);
  });
});
