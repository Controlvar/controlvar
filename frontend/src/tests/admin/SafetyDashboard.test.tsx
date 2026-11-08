import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { SafetyDashboard, SAFETY_CHECK_CONCURRENCY } from '@/pages/admin/safety';
import { AdminService } from '@/services/admin';
import { ApiError } from '@/services/api';
import { FeatureFlagsService } from '@/services/featureFlags';
import { SafetySettings } from '@/types/admin';
import { SafetyCheckResponse } from '@/types/safety';

jest.mock('@/services/admin', () => {
  const actual = jest.requireActual('@/services/admin');
  return {
    ...actual,
    AdminService: {
      getSafetySettings: jest.fn(),
      updateSafetySettings: jest.fn(),
      rollbackFlag: jest.fn(),
    },
  };
});

jest.mock('@/services/featureFlags', () => ({
  ...jest.requireActual('@/services/featureFlags'),
  FeatureFlagsService: { list: jest.fn(), safetyCheck: jest.fn() },
}));

// Mock AdminLayout
jest.mock('@/components/admin/AdminLayout', () => ({
  AdminLayout: ({ children, title }: { children: React.ReactNode; title: string }) => (
    <div data-testid="admin-layout">
      <h1>{title}</h1>
      {children}
    </div>
  ),
}));

// Mock SafetySettingsForm
jest.mock('@/components/admin/safety/SafetySettingsForm', () => ({
  SafetySettingsForm: () => <div data-testid="safety-settings-form">Safety Settings Form</div>,
}));

// Mock SafetyStatusCard
jest.mock('@/components/admin/safety/SafetyStatusCard', () => ({
  SafetyStatusCard: ({
    flag,
    onRollback,
  }: {
    flag: { flag_id: string; flag_name: string; health: string; is_on?: boolean };
    onRollback: (id: string) => void;
  }) => (
    <div
      data-testid="safety-status-card"
      data-health={flag.health}
      data-flag-id={flag.flag_id}
      tabIndex={-1}
    >
      <span>{flag.flag_name}</span>
      {flag.is_on === false ? (
        <span data-testid="flag-off-indicator">Off</span>
      ) : (
        <button data-testid="rollback-button" onClick={() => onRollback(flag.flag_id)}>
          Roll back
        </button>
      )}
    </div>
  ),
}));

// Mock RollbackHistoryTable
jest.mock('@/components/admin/safety/RollbackHistoryTable', () => ({
  RollbackHistoryTable: ({ rollbacks }: { rollbacks: { flag_name: string; reason: string }[] }) => (
    <div data-testid="rollback-history-table">
      Rollback History ({rollbacks.length})
      {rollbacks.map((r, i) => (
        <span key={i} data-testid="rollback-history-row">
          {r.flag_name}: {r.reason}
        </span>
      ))}
    </div>
  ),
}));

// Mock Next.js router
jest.mock('next/router', () => ({
  useRouter: () => ({
    pathname: '/admin/safety',
    push: jest.fn(),
  }),
}));

const mockSettings: SafetySettings = {
  id: 'settings-1',
  enable_automatic_rollbacks: false,
  default_metrics: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const flags = [
  { id: 'flag-1', key: 'checkout-v2', name: 'Checkout v2', status: 'active', rollout_percentage: 50, owner_id: null, created_at: '', updated_at: '' },
  { id: 'flag-2', key: 'payments', name: 'Payments redesign', status: 'active', rollout_percentage: 25, owner_id: null, created_at: '', updated_at: '' },
];

const healthyCheck: SafetyCheckResponse = {
  feature_flag_id: 'flag-1',
  is_healthy: true,
  metrics: [],
  last_checked: '2026-01-01T00:00:00Z',
  details: null,
};

const criticalCheck: SafetyCheckResponse = {
  feature_flag_id: 'flag-2',
  is_healthy: false,
  metrics: [
    {
      name: 'error_rate',
      current_value: 0.12,
      threshold: 0.05,
      is_healthy: false,
      details: { warning: true, measured: true },
    },
  ],
  last_checked: '2026-01-01T00:00:00Z',
  details: null,
};

const mockGetSafetySettings = AdminService.getSafetySettings as jest.Mock;
const mockGetFlagSafetyStatus = FeatureFlagsService.safetyCheck as jest.Mock;
const mockRollbackFlag = AdminService.rollbackFlag as jest.Mock;
const mockListFlags = FeatureFlagsService.list as jest.Mock;

/** `n` flags named flag-1…flag-n, for the concurrency bound. */
function manyFlags(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    id: `flag-${i + 1}`,
    key: `flag_${i + 1}`,
    name: `Flag ${i + 1}`,
    rollout_percentage: 10,
    owner_id: null,
    created_at: '',
    updated_at: '',
  }));
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGetSafetySettings.mockResolvedValue(mockSettings);
  mockListFlags.mockResolvedValue({ items: flags, total: 2, skip: 0, limit: 100 });
  mockGetFlagSafetyStatus.mockImplementation((id: string) =>
    Promise.resolve(id === 'flag-2' ? criticalCheck : healthyCheck),
  );
  mockRollbackFlag.mockResolvedValue({
    success: true,
    feature_flag_id: 'flag-2',
    message: 'Rolled back from 25% to 0%',
    trigger_type: 'manual',
    previous_percentage: 25,
    new_percentage: 0,
    rollback_record_id: 'rec-1',
    timestamp: '2026-01-01T00:00:01Z',
  });
});

async function openRollbackModalForSecondFlag() {
  const view = render(<SafetyDashboard />);
  await waitFor(() => {
    expect(screen.getAllByTestId('safety-status-card')).toHaveLength(2);
  });
  // A real click focuses the button first; fireEvent.click does not.
  const trigger = screen.getAllByTestId('rollback-button')[1];
  trigger.focus();
  fireEvent.click(trigger);
  await waitFor(() => {
    expect(screen.getByTestId('rollback-modal')).toBeInTheDocument();
  });
  return { ...view, trigger };
}

describe('SafetyDashboard', () => {
  it('renders SafetySettingsForm', async () => {
    render(<SafetyDashboard />);
    await waitFor(() => {
      expect(screen.getByTestId('safety-settings-form')).toBeInTheDocument();
    });
  });

  it('lists every flag and runs a safety check for each', async () => {
    render(<SafetyDashboard />);
    await waitFor(() => {
      expect(screen.getAllByTestId('safety-status-card')).toHaveLength(2);
    });
    expect(mockListFlags).toHaveBeenCalledWith({ limit: 100 });
    expect(mockGetFlagSafetyStatus).toHaveBeenCalledWith('flag-1');
    expect(mockGetFlagSafetyStatus).toHaveBeenCalledWith('flag-2');
    const cards = screen.getAllByTestId('safety-status-card');
    expect(cards[0]).toHaveAttribute('data-health', 'healthy');
    expect(cards[1]).toHaveAttribute('data-health', 'critical');
  });

  it('shows an empty state when there are no flags', async () => {
    mockListFlags.mockResolvedValue({ items: [], total: 0, skip: 0, limit: 100 });
    render(<SafetyDashboard />);
    await waitFor(() => {
      expect(screen.getByTestId('flag-status-empty')).toBeInTheDocument();
    });
  });

  it('shows an error when the flag list cannot be loaded', async () => {
    mockListFlags.mockRejectedValue(new Error('API down'));
    render(<SafetyDashboard />);
    await waitFor(() => {
      expect(screen.getByTestId('flag-status-error')).toHaveTextContent('API down');
    });
  });

  it('keeps the other cards and warns when one safety check fails', async () => {
    mockGetFlagSafetyStatus.mockImplementation((id: string) =>
      id === 'flag-2' ? Promise.reject(new Error('404')) : Promise.resolve(healthyCheck),
    );
    render(<SafetyDashboard />);
    await waitFor(() => {
      expect(screen.getByTestId('flag-status-partial')).toHaveTextContent('payments');
    });
    expect(screen.getAllByTestId('safety-status-card')).toHaveLength(1);
  });

  it('checks flags through a bounded worker pool instead of all at once', async () => {
    const flagCount = 24;
    mockListFlags.mockResolvedValue({ items: manyFlags(flagCount), total: flagCount, skip: 0, limit: 100 });

    let inFlight = 0;
    let peak = 0;
    mockGetFlagSafetyStatus.mockImplementation(async () => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 0));
      inFlight -= 1;
      return healthyCheck;
    });

    render(<SafetyDashboard />);
    await waitFor(() => {
      expect(screen.getAllByTestId('safety-status-card')).toHaveLength(flagCount);
    });
    expect(mockGetFlagSafetyStatus).toHaveBeenCalledTimes(flagCount);
    expect(peak).toBeLessThanOrEqual(SAFETY_CHECK_CONCURRENCY);
    // …but still concurrent: a serial implementation would peak at 1.
    expect(peak).toBeGreaterThan(1);
  });

  it('reports a 429 as rate limiting rather than a failed check', async () => {
    mockGetFlagSafetyStatus.mockImplementation((id: string) =>
      id === 'flag-2'
        ? Promise.reject(new ApiError({ status: 429, detail: 'Rate limit exceeded' }))
        : Promise.resolve(healthyCheck),
    );
    render(<SafetyDashboard />);
    const banner = await screen.findByTestId('flag-status-partial');
    expect(banner).toHaveTextContent(/rate limited/i);
    expect(banner).toHaveTextContent(/retry/i);
    expect(banner).toHaveTextContent('payments');
    expect(banner).not.toHaveTextContent(/safety check failed/i);
    expect(screen.getAllByTestId('safety-status-card')).toHaveLength(1);
  });

  it('shows rollback confirmation modal with the flag name', async () => {
    await openRollbackModalForSecondFlag();
    expect(screen.getByTestId('rollback-modal-flag-name')).toHaveTextContent('Payments redesign');
  });

  it('says the rollback turns the flag off for every user and pauses its schedule (#629)', async () => {
    await openRollbackModalForSecondFlag();
    expect(screen.getByTestId('rollback-modal-effect')).toHaveTextContent(
      'The flag will be turned off for every user, including users matched by a targeting ' +
        "rule. They get the flag's off value. Its rollout schedule is paused. To serve " +
        "it again, turn it on from the flag's page.",
    );
    expect(screen.getByTestId('rollback-modal')).not.toHaveTextContent(/stays active/i);
  });

  it('confirming rollback calls AdminService.rollbackFlag with the id and reason', async () => {
    await openRollbackModalForSecondFlag();
    fireEvent.change(screen.getByTestId('rollback-reason-input'), {
      target: { value: 'error spike' },
    });
    fireEvent.click(screen.getByTestId('rollback-confirm-button'));
    await waitFor(() => {
      expect(mockRollbackFlag).toHaveBeenCalledWith('flag-2', 'error spike');
    });
  });

  it('shows success, records the rollback and re-checks flags', async () => {
    await openRollbackModalForSecondFlag();
    mockGetFlagSafetyStatus.mockClear();
    fireEvent.click(screen.getByTestId('rollback-confirm-button'));
    await waitFor(() => {
      expect(screen.getByTestId('rollback-success-message')).toBeInTheDocument();
    });
    expect(screen.getByTestId('rollback-success-message')).toHaveTextContent(
      'Payments redesign is off. Turn it on from its page when the cause is fixed.',
    );
    expect(screen.getByTestId('rollback-history-row')).toHaveTextContent(
      'Payments redesign: Rolled back from 25% to 0%',
    );
    await waitFor(() => {
      expect(mockGetFlagSafetyStatus).toHaveBeenCalledWith('flag-2');
    });
  });

  it('shows the API error when the rollback fails', async () => {
    mockRollbackFlag.mockRejectedValue(new Error('Not enough permissions'));
    await openRollbackModalForSecondFlag();
    fireEvent.click(screen.getByTestId('rollback-confirm-button'));
    await waitFor(() => {
      expect(screen.getByTestId('rollback-error')).toHaveTextContent('Not enough permissions');
    });
    expect(screen.queryByTestId('rollback-history-row')).not.toBeInTheDocument();
  });

  it('renders rollback history section', async () => {
    render(<SafetyDashboard />);
    await waitFor(() => {
      expect(screen.getByTestId('rollback-history-table')).toBeInTheDocument();
    });
  });

  it('renders with data-testid="safety-dashboard"', () => {
    render(<SafetyDashboard />);
    expect(screen.getByTestId('safety-dashboard')).toBeInTheDocument();
  });
});

describe('SafetyDashboard rollback dialog: keyboard and screen reader (#629)', () => {
  it('names the dialog by its "Confirm Rollback" heading', async () => {
    await openRollbackModalForSecondFlag();
    const dialog = screen.getByRole('dialog', { name: 'Confirm Rollback' });
    const labelledBy = dialog.getAttribute('aria-labelledby');
    expect(labelledBy).toBeTruthy();
    expect(document.getElementById(labelledBy as string)).toHaveTextContent('Confirm Rollback');
  });

  it('moves focus into the dialog when it opens', async () => {
    await openRollbackModalForSecondFlag();
    const dialog = screen.getByTestId('rollback-modal');
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    expect(screen.getByTestId('rollback-reason-input')).toHaveFocus();
  });

  it('closes on Escape without rolling back', async () => {
    await openRollbackModalForSecondFlag();
    fireEvent.keyDown(document.activeElement as Element, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('rollback-modal')).not.toBeInTheDocument();
    });
    expect(mockRollbackFlag).not.toHaveBeenCalled();
  });

  it('returns focus to the button that opened it, on Escape and on Cancel', async () => {
    const { trigger } = await openRollbackModalForSecondFlag();
    fireEvent.keyDown(document.activeElement as Element, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('rollback-modal')).not.toBeInTheDocument();
    });
    expect(trigger).toHaveFocus();

    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByTestId('rollback-reason-input')).toHaveFocus();
    fireEvent.click(screen.getByTestId('rollback-cancel-button'));
    expect(screen.queryByTestId('rollback-modal')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('after a rollback, Close lands focus on the flag card once its button has gone', async () => {
    await openRollbackModalForSecondFlag();
    mockListFlags.mockResolvedValue({
      items: [flags[0], { ...flags[1], status: 'inactive' }],
      total: 2,
      skip: 0,
      limit: 100,
    });
    fireEvent.click(screen.getByTestId('rollback-confirm-button'));
    await screen.findByTestId('rollback-success-message');
    // The confirm button is gone; focus must not fall out of the dialog.
    expect(screen.getByTestId('rollback-cancel-button')).toHaveFocus();
    await screen.findByTestId('flag-off-indicator');

    fireEvent.click(screen.getByTestId('rollback-cancel-button'));
    const card = screen
      .getAllByTestId('safety-status-card')
      .find((c) => c.getAttribute('data-flag-id') === 'flag-2');
    expect(card).toHaveFocus();
  });

  // Safari, and Firefox on macOS, do not focus a button on a mouse click, so
  // document.activeElement is <body> when the dialog opens.
  async function openWithoutFocusingTrigger() {
    render(<SafetyDashboard />);
    await waitFor(() => {
      expect(screen.getAllByTestId('safety-status-card')).toHaveLength(2);
    });
    (document.activeElement as HTMLElement | null)?.blur();
    expect(document.activeElement).toBe(document.body);
    fireEvent.click(screen.getAllByTestId('rollback-button')[1]);
    await screen.findByTestId('rollback-modal');
  }

  it('returns focus to the Roll back button when the click did not focus it', async () => {
    await openWithoutFocusingTrigger();
    fireEvent.keyDown(document.activeElement as Element, { key: 'Escape' });
    expect(screen.queryByTestId('rollback-modal')).not.toBeInTheDocument();
    expect(screen.getAllByTestId('rollback-button')[1]).toHaveFocus();
  });

  it('lands focus on the card after a rollback when the click did not focus the button', async () => {
    await openWithoutFocusingTrigger();
    mockListFlags.mockResolvedValue({
      items: [flags[0], { ...flags[1], status: 'inactive' }],
      total: 2,
      skip: 0,
      limit: 100,
    });
    fireEvent.click(screen.getByTestId('rollback-confirm-button'));
    await screen.findByTestId('rollback-success-message');
    await screen.findByTestId('flag-off-indicator');
    fireEvent.click(screen.getByTestId('rollback-cancel-button'));
    const card = screen
      .getAllByTestId('safety-status-card')
      .find((c) => c.getAttribute('data-flag-id') === 'flag-2');
    expect(card).toHaveFocus();
  });

  it('passes the inactive status through to the card (#629)', async () => {
    mockListFlags.mockResolvedValue({
      items: [flags[0], { ...flags[1], status: 'inactive' }],
      total: 2,
      skip: 0,
      limit: 100,
    });
    render(<SafetyDashboard />);
    expect(await screen.findByTestId('flag-off-indicator')).toHaveTextContent('Off');
    expect(screen.getAllByTestId('rollback-button')).toHaveLength(1);
  });

  it('says automatic rollbacks are not listed under the session table', async () => {
    render(<SafetyDashboard />);
    expect(await screen.findByTestId('rollback-history-automatic-note')).toHaveTextContent(
      'Automatic rollbacks are not listed here. Find them in the Audit Log under Safety rollback.',
    );
  });
});

describe('SafetyDashboard accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function violations(node: Element) {
    const result = await axe.run(node, axeOptions);
    return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
  }

  it('has no axe violations with the grid loaded', async () => {
    const { container } = render(<SafetyDashboard />);
    await waitFor(() => {
      expect(screen.getAllByTestId('safety-status-card')).toHaveLength(2);
    });
    expect(await violations(container)).toStrictEqual([]);
  });

  it('has no axe violations with the rollback dialog open', async () => {
    const { container } = await openRollbackModalForSecondFlag();
    expect(await violations(container)).toStrictEqual([]);
  });

  it('has no axe violations after a rollback succeeds', async () => {
    const { container } = await openRollbackModalForSecondFlag();
    fireEvent.click(screen.getByTestId('rollback-confirm-button'));
    await screen.findByTestId('rollback-success-message');
    expect(await violations(container)).toStrictEqual([]);
  });
});
