import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { AuditLogPage } from '@/pages/admin/audit';
import { AdminService } from '@/services/admin';
import { ApiError } from '@/services/api';
import { AuditLog } from '@/types/admin';
import { DOWNLOAD_FAILED_MESSAGE } from '@/components/admin/audit/AuditLogDownload';

jest.mock('@/services/admin', () => ({
  AdminService: { listAuditLogs: jest.fn(), exportAuditLogs: jest.fn() },
}));

// The page reads the signed-in user to choose its layout. These tests run on
// the plain (non-superuser) layout as an ANALYST, who reads other users' entries too; who
// opens the page, and the superuser layout, are in AuditLogAccess.test.tsx.
jest.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      id: 'u-analyst',
      username: 'analyst',
      email: 'analyst@example.com',
      full_name: null,
      role: 'ANALYST',
      is_superuser: false,
      is_active: true,
      auth_provider: 'local',
    },
    status: 'authenticated',
  }),
}));

jest.mock('@/components/admin/AdminLayout', () => ({
  AdminLayout: ({ children, title }: { children: React.ReactNode; title: string }) => (
    <main data-testid="admin-layout">
      <h1>{title}</h1>
      {children}
    </main>
  ),
}));

const listAuditLogs = AdminService.listAuditLogs as jest.Mock;
const exportAuditLogs = AdminService.exportAuditLogs as jest.Mock;

const entry = (overrides: Partial<AuditLog> = {}): AuditLog => ({
  id: 'log-1',
  user_id: 'user-1',
  user_email: 'alice@example.com',
  action_type: 'toggle_disable',
  entity_type: 'feature_flag',
  entity_id: 'flag-1',
  entity_name: 'Player v2',
  old_value: 'ACTIVE',
  new_value: 'INACTIVE',
  reason: 'Incident',
  timestamp: '2026-10-01T10:30:00Z',
  created_at: '2026-10-01T10:30:00Z',
  action_description: 'disabled',
  ...overrides,
});

const ENTRIES = [
  entry(),
  entry({
    id: 'log-2',
    user_id: null,
    user_email: 'system:safety-monitor',
    action_type: 'safety_rollback',
    entity_name: 'Checkout',
  }),
  entry({ id: 'log-3', user_id: null, user_email: 'gone@example.com', entity_name: 'Search' }),
];

function headers(total: string | null): Headers {
  const h = new Headers();
  if (total !== null) h.set('X-Total-Count', total);
  return h;
}

const createObjectURL = jest.fn(() => 'blob:audit');
const revokeObjectURL = jest.fn();

beforeEach(() => {
  jest.clearAllMocks();
  listAuditLogs.mockResolvedValue({ items: ENTRIES, total: 3, page: 1, limit: 50 });
  Object.assign(URL, { createObjectURL, revokeObjectURL });
  jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
});

afterEach(() => {
  jest.restoreAllMocks();
});

async function renderPage() {
  const view = render(<AuditLogPage />);
  await screen.findByTestId('audit-log-row-log-1');
  return view;
}

describe('the Audit Log page (#915)', () => {
  it('renders the page with its filters, downloads and table', async () => {
    await renderPage();
    expect(screen.getByTestId('audit-log-page')).toBeInTheDocument();
    expect(screen.getByTestId('audit-log-download')).toBeInTheDocument();
    expect(screen.getByTestId('audit-log-table')).toBeInTheDocument();
  });
});

describe('rows open from the keyboard (#221)', () => {
  it('opens a row with Enter, moves focus into the panel, and returns it on close', async () => {
    const user = userEvent.setup();
    await renderPage();
    const open = screen.getByTestId('audit-log-open-log-1');
    expect(open).toHaveAttribute('aria-expanded', 'false');
    expect(open).toHaveAccessibleName('View details for Player v2');

    open.focus();
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('audit-log-detail-panel')).toBeInTheDocument();
    expect(open).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('detail-panel-heading')).toHaveFocus();

    await user.tab();
    expect(screen.getByTestId('detail-panel-close')).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(screen.queryByTestId('audit-log-detail-panel')).not.toBeInTheDocument();
    expect(screen.getByTestId('audit-log-open-log-1')).toHaveFocus();
  });

  it('reaches every row button with Tab', async () => {
    const user = userEvent.setup();
    await renderPage();
    const reached: string[] = [];
    for (let i = 0; i < 30; i += 1) {
      await user.tab();
      const id = document.activeElement?.getAttribute('data-testid');
      if (id?.startsWith('audit-log-open-')) reached.push(id);
    }
    expect(new Set(reached)).toStrictEqual(
      new Set(['audit-log-open-log-1', 'audit-log-open-log-2', 'audit-log-open-log-3']),
    );
  });
});

describe('who made an entry (#221)', () => {
  it('marks the safety monitor automatic and not a deleted user', async () => {
    await renderPage();
    expect(screen.getByTestId('actor-log-2')).toHaveTextContent('Safety monitor (automatic)');
    expect(screen.getByTestId('actor-log-3')).toHaveTextContent('gone@example.com');
    expect(screen.getByTestId('actor-log-3')).not.toHaveTextContent('automatic');
    expect(screen.getByTestId('action-log-2')).toHaveTextContent('Safety rollback');
  });
});

describe('Download CSV and Download JSON (#221)', () => {
  const CSV = 'id,timestamp\r\na,t\r\nb,t\r\n';

  it('saves the file when the rows received equal X-Total-Count', async () => {
    exportAuditLogs.mockResolvedValue({ text: CSV, headers: headers('2') });
    await renderPage();
    fireEvent.click(screen.getByTestId('audit-download-csv'));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(exportAuditLogs).toHaveBeenCalledWith('csv', expect.any(Object));
    expect(screen.queryByTestId('audit-download-error')).not.toBeInTheDocument();
  });

  it.each([
    ['csv', CSV, '3', 'The download was incomplete: 2 of 3 entries arrived'],
    ['json', '[{"id":"a"}]', '2', 'The download was incomplete: 1 of 2 entries arrived'],
    ['json', '[{"id":"a"}', '1', 'could not be checked'],
    ['csv', CSV, null, 'could not be checked'],
  ])(
    'shows an error and saves nothing when %s rows and the count differ',
    async (format, text, total, message) => {
      exportAuditLogs.mockResolvedValue({ text, headers: headers(total as string | null) });
      await renderPage();
      fireEvent.click(screen.getByTestId(`audit-download-${format}`));
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent(message);
      expect(createObjectURL).not.toHaveBeenCalled();
    },
  );

  it('says how to narrow an export above the cap', async () => {
    exportAuditLogs.mockRejectedValue(
      new ApiError({
        status: 422,
        detail:
          '61234 entries match these filters; an export holds at most 50000. Narrow from_date and to_date, or filter by action_type or entity_type.',
      }),
    );
    await renderPage();
    fireEvent.click(screen.getByTestId('audit-download-json'));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This export would hold 61,234 entries, more than the 50,000 an export can hold.',
    );
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('shows the failure message when the download fails', async () => {
    exportAuditLogs.mockRejectedValue(new ApiError({ status: 0, detail: 'down' }));
    await renderPage();
    fireEvent.click(screen.getByTestId('audit-download-csv'));
    expect(await screen.findByRole('alert')).toHaveTextContent(DOWNLOAD_FAILED_MESSAGE);
  });

  it('is busy while downloading', async () => {
    let finish: (v: unknown) => void = () => undefined;
    exportAuditLogs.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    await renderPage();
    fireEvent.click(screen.getByTestId('audit-download-csv'));
    const button = screen.getByTestId('audit-download-csv');
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button).toHaveTextContent('Downloading…');
    expect(screen.getByTestId('audit-download-json')).toBeDisabled();
    finish({ text: CSV, headers: headers('2') });
    await waitFor(() => expect(button).toHaveAttribute('aria-busy', 'false'));
    expect(button).toHaveTextContent('Download CSV');
  });

  it('passes the page filters to the export', async () => {
    exportAuditLogs.mockResolvedValue({ text: '[]', headers: headers('0') });
    await renderPage();
    fireEvent.change(screen.getByTestId('filter-action-type'), {
      target: { value: 'safety_rollback' },
    });
    await screen.findByTestId('audit-log-row-log-1');
    fireEvent.click(screen.getByTestId('audit-download-json'));
    await waitFor(() =>
      expect(exportAuditLogs).toHaveBeenCalledWith(
        'json',
        expect.objectContaining({ action_type: 'safety_rollback' }),
      ),
    );
  });
});

describe('AuditLogPage accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function violations(node: Element) {
    const result = await axe.run(node, axeOptions);
    return result.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target).join(', ')}`);
  }

  it('has no axe violations with a row expanded', async () => {
    const { container } = await renderPage();
    fireEvent.click(screen.getByTestId('audit-log-open-log-2'));
    expect(screen.getByTestId('audit-log-detail-panel')).toBeInTheDocument();
    expect(await violations(container)).toStrictEqual([]);
  });

  it('has no axe violations with a download error shown', async () => {
    exportAuditLogs.mockResolvedValue({ text: '[', headers: headers('1') });
    const { container } = await renderPage();
    fireEvent.click(screen.getByTestId('audit-download-json'));
    await screen.findByRole('alert');
    expect(await violations(container)).toStrictEqual([]);
  });
});
