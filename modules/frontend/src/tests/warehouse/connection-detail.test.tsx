/**
 * /warehouse/connections/[id]: every reader sees the non-secret settings;
 * only ADMIN edits, tests, regenerates a key or deletes.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ModulesProvider } from '@/contexts/ModulesContext';
import type { UserMe } from '@/services/api';
import ConnectionPage from '@modules/pages/warehouse/connections/[id]';
import { warehouseService } from '@modules/services/warehouse';
import {
  KEY_SENTINEL,
  SERVICE_ACCOUNT_JSON,
  WAREHOUSE_INFO,
  apiError,
  connection,
  connectors,
  snowflakeConnection,
  user,
} from './fixtures';

let mockUser: UserMe | null = null;
const mockPush = jest.fn();

jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));
jest.mock('next/router', () => ({
  useRouter: () => ({
    pathname: '/warehouse/connections/[id]',
    asPath: '/warehouse/connections/c-1',
    query: { id: 'c-1' },
    isReady: true,
    push: mockPush,
    replace: jest.fn(),
  }),
}));
jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});
jest.mock('next/link', () => {
  const MockLink = ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});
jest.mock('@modules/services/warehouse', () => ({
  ...jest.requireActual('@modules/services/warehouse'),
  warehouseService: {
    getConnection: jest.fn(),
    listConnectors: jest.fn(),
    updateConnection: jest.fn(),
    deleteConnection: jest.fn(),
    testConnection: jest.fn(),
    regenerateKey: jest.fn(),
  },
}));

const svc = warehouseService as jest.Mocked<typeof warehouseService>;

function renderPage() {
  return render(
    <ModulesProvider initial={WAREHOUSE_INFO}>
      <ConnectionPage />
    </ModulesProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = user('ADMIN');
  svc.getConnection.mockResolvedValue(connection());
  svc.listConnectors.mockResolvedValue(connectors({ bigquery: true, snowflake: true }));
  svc.testConnection.mockResolvedValue({ ok: true, warehouse_type: 'bigquery', promoted_pending_key: false });
});

it('shows the non-secret settings and the credential status', async () => {
  renderPage();
  const details = await screen.findByTestId('warehouse-connection-details');
  expect(details).toHaveTextContent('Billing project');
  expect(details).toHaveTextContent('acme-billing');
  expect(details).toHaveTextContent('controlvar@acme-billing.iam.gserviceaccount.com');
  expect(details).toHaveTextContent('Credentials stored');
  expect(details).toHaveTextContent('50 GB');
  expect(details).toHaveTextContent('20 (UTC day, previews included)');
  expect(details.querySelector('time')).toHaveAttribute('datetime', '2026-09-28T10:00:00.000Z');
});

it.each(['DEVELOPER', 'ANALYST'] as const)('shows %s the settings and the refusal instead of the controls', async (role) => {
  mockUser = user(role);
  renderPage();
  await screen.findByTestId('warehouse-connection-details');
  expect(screen.getByTestId('warehouse-role-notice')).toHaveTextContent(
    `Changing a warehouse connection requires the ADMIN role; you are ${role}.`,
  );
  for (const name of ['Edit', 'Test connection', 'Delete']) {
    expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
  }
});

it('shows VIEWER the refusal and loads nothing', async () => {
  mockUser = user('VIEWER');
  renderPage();
  expect(await screen.findByTestId('warehouse-role-notice')).toHaveTextContent('you are VIEWER.');
  expect(svc.getConnection).not.toHaveBeenCalled();
});

it('shows a load error with retry', async () => {
  svc.getConnection.mockRejectedValueOnce(apiError(404, 'not_found', 'Warehouse connection not found.'));
  renderPage();
  expect(await screen.findByTestId('warehouse-load-error')).toHaveTextContent('Warehouse connection not found.');
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findByTestId('warehouse-connection-details')).toBeInTheDocument();
});

it('tests the connection and announces the result, moving focus to it', async () => {
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: 'Test connection' }));
  const result = screen.getByTestId('warehouse-connection-test-result');
  await waitFor(() => expect(result).toHaveTextContent('Test passed at'));
  expect(result).toHaveAttribute('aria-live', 'polite');
  expect(result).toHaveFocus();
  expect(svc.testConnection).toHaveBeenCalledWith('c-1');
});

it('reports a failed test with the coded message, never a vendor text', async () => {
  svc.testConnection.mockRejectedValue(apiError(502, 'permission_denied', 'The warehouse role does not have permission for this query.'));
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: 'Test connection' }));
  await waitFor(() =>
    expect(screen.getByTestId('warehouse-connection-test-result')).toHaveTextContent(
      'Test failed: The warehouse role does not have permission for this query.',
    ),
  );
});

describe('a connection whose connector is not available', () => {
  it('says so and does not offer test, edit or a new key; delete stays', async () => {
    svc.getConnection.mockResolvedValue(snowflakeConnection({ enabled: false }));
    svc.listConnectors.mockResolvedValue(connectors());
    renderPage();
    expect(await screen.findByTestId('warehouse-connection-unavailable')).toHaveTextContent(
      "Snowflake isn't available on this deployment yet.",
    );
    expect(screen.getByRole('button', { name: 'Test connection' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Generate a new key' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
  });
});

describe('Snowflake keys', () => {
  it('generates a pending key and shows its statement; a passing test switches to it', async () => {
    svc.getConnection.mockResolvedValue(snowflakeConnection());
    svc.regenerateKey.mockResolvedValue({
      ...snowflakeConnection({ credentials_status: 'pending_key', pending_public_key_fingerprint: 'SHA256:new=' }),
      public_key: {
        public_key: 'MIIBnew',
        public_key_fingerprint: 'SHA256:new=',
        statement: "ALTER USER CONTROLVAR_SVC SET RSA_PUBLIC_KEY_2='MIIBnew';",
      },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Generate a new key' }));
    const sql = await screen.findByTestId('warehouse-key-statement-sql');
    expect(sql).toHaveTextContent('RSA_PUBLIC_KEY_2');
    expect(screen.getByTestId('warehouse-key-statement')).toHaveTextContent(
      'The current key keeps working until a test with the new key passes.',
    );
    expect(screen.getByTestId('warehouse-connection-details')).toHaveTextContent('New key waiting');

    svc.testConnection.mockResolvedValue({ ok: true, warehouse_type: 'snowflake', promoted_pending_key: true });
    svc.getConnection.mockResolvedValue(snowflakeConnection({ public_key_fingerprint: 'SHA256:new=' }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await waitFor(() =>
      expect(screen.getByTestId('warehouse-connection-test-result')).toHaveTextContent('The new key is now in use.'),
    );
    expect(screen.queryByTestId('warehouse-key-statement')).not.toBeInTheDocument();
  });
});

describe('edit', () => {
  it('keeps the stored BigQuery key unless a new one is given, and clears a new one after saving', async () => {
    svc.updateConnection.mockImplementation(async (_id, body) => connection({ name: body.name }));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const key = screen.getByTestId('wh-conn-service-account-json');
    expect(key).toHaveValue('');
    expect(key).toHaveAccessibleDescription(expect.stringContaining('Leave empty to keep the stored key.'));
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'Renamed' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(svc.updateConnection).toHaveBeenCalledTimes(1));
    expect(svc.updateConnection.mock.calls[0][1]).not.toHaveProperty('service_account_json');
    expect(await screen.findByText('Changes saved.')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByTestId('wh-conn-service-account-json'), { target: { value: SERVICE_ACCOUNT_JSON } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    });
    await waitFor(() => expect(svc.updateConnection).toHaveBeenCalledTimes(2));
    expect(svc.updateConnection.mock.calls[1][1]).toMatchObject({ service_account_json: SERVICE_ACCOUNT_JSON });
    await screen.findByTestId('warehouse-connection-details');
    expect(document.body.innerHTML).not.toContain(KEY_SENTINEL);
  });
});

describe('delete', () => {
  it('asks first in a dialog that closes on Escape and returns focus to its trigger', async () => {
    renderPage();
    const trigger = await screen.findByRole('button', { name: 'Delete' });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'Delete “Prod analytics”?' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining('sources that use it'));
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(svc.deleteConnection).not.toHaveBeenCalled();
  });

  it('keeps Tab inside the dialog', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    const dialog = screen.getByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Delete connection' });
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' });
    confirm.focus();
    fireEvent.keyDown(dialog, { key: 'Tab' });
    expect(cancel).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
    expect(confirm).toHaveFocus();
  });

  it('deletes on confirmation and goes back to the list', async () => {
    svc.deleteConnection.mockResolvedValue(undefined);
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete connection' }));
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith('/warehouse'));
    expect(svc.deleteConnection).toHaveBeenCalledWith('c-1');
  });

  it('shows a refused delete inside the dialog as an alert', async () => {
    svc.deleteConnection.mockRejectedValue(apiError(403, 'role_required', 'Deleting a warehouse connection requires the ADMIN role; you are DEVELOPER.'));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete connection' }));
    expect(await within(screen.getByRole('dialog')).findByRole('alert')).toHaveTextContent('requires the ADMIN role');
  });
});

it('shows an Athena connection’s external ID with a specific copy button', async () => {
  svc.getConnection.mockResolvedValue(
    connection({
      warehouse_type: 'athena',
      parameters: { region: 'eu-west-1', role_arn: 'arn:aws:iam::123456789012:role/X', workgroup: 'wg', database: 'db' },
      external_id: 'exp-0123456789abcdef0123456789abcdef',
    }),
  );
  const writeText = jest.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: 'Copy external ID' }));
  await waitFor(() => expect(screen.getByText('Copied')).toBeInTheDocument());
  expect(writeText).toHaveBeenCalledWith('exp-0123456789abcdef0123456789abcdef');
});
