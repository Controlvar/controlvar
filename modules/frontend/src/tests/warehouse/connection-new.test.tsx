/**
 * /warehouse/connections/new: ADMIN only; a disabled connector cannot be
 * chosen; the BigQuery key is sent in the body and kept nowhere after a save.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModulesProvider } from '@/contexts/ModulesContext';
import type { UserMe } from '@/services/api';
import NewConnectionPage from '@modules/pages/warehouse/connections/new';
import { warehouseService } from '@modules/services/warehouse';
import { KEY_NOT_SERVICE_ACCOUNT, SNOWFLAKE_ROLE_REFUSED } from '@modules/components/warehouse/validation';
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
  useRouter: () => ({ pathname: '/warehouse/connections/new', asPath: '/warehouse/connections/new', query: {}, isReady: true, push: mockPush, replace: jest.fn() }),
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
    listConnectors: jest.fn(),
    createConnection: jest.fn(),
    testUnsavedConnection: jest.fn(),
  },
}));

const svc = warehouseService as jest.Mocked<typeof warehouseService>;

function renderPage() {
  return render(
    <ModulesProvider initial={WAREHOUSE_INFO}>
      <NewConnectionPage />
    </ModulesProvider>,
  );
}

function type(label: RegExp | string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

async function fillBigQuery() {
  await screen.findByTestId('warehouse-connection-form');
  type(/^Name/, 'Prod analytics');
  type(/^Billing project/, 'acme-billing');
  type(/^Location/, 'EU');
  type(/^Most data a query may read/, '50');
  fireEvent.change(screen.getByTestId('wh-conn-service-account-json'), { target: { value: SERVICE_ACCOUNT_JSON } });
}

const consoleSpies: jest.SpyInstance[] = [];

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = user('ADMIN');
  svc.listConnectors.mockResolvedValue(connectors({ bigquery: true, snowflake: true }));
  svc.testUnsavedConnection.mockResolvedValue({ ok: true, warehouse_type: 'bigquery', promoted_pending_key: false });
  svc.createConnection.mockResolvedValue({ ...connection(), public_key: null });
  sessionStorage.clear();
  localStorage.clear();
  for (const method of ['log', 'info', 'warn', 'error', 'debug'] as const) {
    consoleSpies.push(jest.spyOn(console, method));
  }
});

afterEach(() => {
  // The key never reaches the console, whatever happened in the test.
  for (const spy of consoleSpies) {
    expect(JSON.stringify(spy.mock.calls)).not.toContain(KEY_SENTINEL);
    spy.mockRestore();
  }
  consoleSpies.length = 0;
});

describe('who may create', () => {
  it.each(['DEVELOPER', 'ANALYST', 'VIEWER'] as const)('shows %s the refusal and no form', async (role) => {
    mockUser = user(role);
    renderPage();
    expect(await screen.findByTestId('warehouse-role-notice')).toHaveTextContent(
      `Creating a warehouse connection requires the ADMIN role; you are ${role}.`,
    );
    expect(screen.queryByTestId('warehouse-connection-form')).not.toBeInTheDocument();
    expect(svc.listConnectors).not.toHaveBeenCalled();
  });
});

describe('connectors that are not available', () => {
  it('shows no form at all when none is enabled', async () => {
    svc.listConnectors.mockResolvedValue(connectors());
    renderPage();
    expect(await screen.findByTestId('warehouse-no-connector')).toBeInTheDocument();
    expect(screen.queryByTestId('warehouse-connection-form')).not.toBeInTheDocument();
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
  });

  it('as shipped: BigQuery and Snowflake can be chosen, and Amazon Athena cannot be', async () => {
    svc.listConnectors.mockResolvedValue(connectors({ snowflake: true, bigquery: true }));
    renderPage();
    const bigquery = await screen.findByRole('radio', { name: 'BigQuery' });
    expect(bigquery).toBeEnabled();
    expect(bigquery).toBeChecked();
    expect(screen.getByTestId('wh-conn-service-account-json')).toBeInTheDocument();
    const athena = screen.getByRole('radio', { name: 'Amazon Athena' });
    expect(athena).toBeDisabled();
    expect(athena).toHaveAccessibleDescription('Not yet available');
    const snowflake = screen.getByRole('radio', { name: 'Snowflake' });
    expect(snowflake).toBeEnabled();
    fireEvent.click(snowflake);
    expect(snowflake).toBeChecked();
    expect(screen.getByLabelText(/^Account/)).toBeInTheDocument();
    expect(screen.queryByTestId('wh-conn-service-account-json')).not.toBeInTheDocument();
  });

  it('lists a disabled connector as not yet available and does not let it be chosen', async () => {
    renderPage();
    const athena = await screen.findByRole('radio', { name: 'Amazon Athena' });
    expect(athena).toBeDisabled();
    expect(athena).toHaveAccessibleDescription('Not yet available');
    expect(screen.getByRole('radio', { name: 'BigQuery' })).toBeEnabled();
    await userEvent.setup().click(athena);
    expect(athena).not.toBeChecked();
    expect(screen.getByRole('radio', { name: 'BigQuery' })).toBeChecked();
  });

  it('refuses to send a type the deployment has not enabled, even if one is selected', async () => {
    // Only Athena is disabled; force its radio on (as a stale page might).
    renderPage();
    const athena = (await screen.findByRole('radio', { name: 'Amazon Athena' })) as HTMLInputElement;
    athena.disabled = false;
    fireEvent.click(athena);
    expect(athena).toBeChecked();
    // Save is disabled for a type that is not enabled ...
    expect(screen.getByRole('button', { name: 'Save connection' })).toBeDisabled();
    // ... and a submit that gets through anyway (Enter in a field) sends nothing.
    fireEvent.submit(screen.getByTestId('warehouse-connection-form'));
    expect(await screen.findByRole('alert')).toHaveTextContent("Amazon Athena isn't available on this deployment yet.");
    expect(svc.createConnection).not.toHaveBeenCalled();
    expect(svc.testUnsavedConnection).not.toHaveBeenCalled();
  });
});

describe('BigQuery (with the connector enabled)', () => {
  it('saves only after a test has passed with exactly these values', async () => {
    renderPage();
    await fillBigQuery();
    const save = screen.getByRole('button', { name: 'Save connection' });
    fireEvent.click(save);
    expect(await screen.findByRole('alert')).toHaveTextContent('Test the connection first');
    expect(svc.createConnection).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    const result = screen.getByTestId('wh-conn-test-result');
    await waitFor(() => expect(result).toHaveTextContent('Passed.'));
    expect(result).toHaveAttribute('aria-live', 'polite');
    expect(result).toHaveFocus();

    // An edit after the test makes it out of date; Save is refused again.
    type(/^Location/, 'US');
    expect(result).toHaveTextContent('Out of date: test again.');
    fireEvent.click(save);
    expect(await screen.findByRole('alert')).toHaveTextContent('Test the connection first');
    expect(svc.createConnection).not.toHaveBeenCalled();
  });

  it('sends the key in the body, then keeps it nowhere: not in the page, the field, storage or the console', async () => {
    renderPage();
    await fillBigQuery();
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await waitFor(() => expect(screen.getByTestId('wh-conn-test-result')).toHaveTextContent('Passed.'));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    });
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith('/warehouse/connections/c-1'));

    const sent = svc.createConnection.mock.calls[0][0];
    expect(sent).toMatchObject({
      warehouse_type: 'bigquery',
      name: 'Prod analytics',
      billing_project: 'acme-billing',
      location: 'EU',
      max_bytes_per_query: 50_000_000_000,
      query_timeout_seconds: 300,
      max_runs_per_day: 20,
      service_account_json: SERVICE_ACCOUNT_JSON,
    });
    expect(svc.testUnsavedConnection.mock.calls[0][0]).toMatchObject({ service_account_json: SERVICE_ACCOUNT_JSON });

    // After the save: the textarea is empty and the key is nowhere in the DOM.
    expect(screen.getByTestId('wh-conn-service-account-json')).toHaveValue('');
    expect(document.body.innerHTML).not.toContain(KEY_SENTINEL);
    for (const el of Array.from(document.querySelectorAll('input, textarea'))) {
      expect((el as HTMLInputElement).value).not.toContain(KEY_SENTINEL);
    }
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(KEY_SENTINEL);
    expect(JSON.stringify({ ...localStorage })).not.toContain(KEY_SENTINEL);
    expect(window.location.href).not.toContain(KEY_SENTINEL);
  });

  it('shows the API refusal next to the field it names, without the key', async () => {
    svc.createConnection.mockRejectedValue(
      apiError(422, 'invalid_billing_project', 'Use the billing project’s ID.', 'billing_project'),
    );
    renderPage();
    await fillBigQuery();
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await waitFor(() => expect(screen.getByTestId('wh-conn-test-result')).toHaveTextContent('Passed.'));
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    const field = screen.getByLabelText(/^Billing project/);
    await waitFor(() => expect(field).toHaveAttribute('aria-invalid', 'true'));
    expect(field).toHaveAccessibleDescription(expect.stringContaining('Use the billing project’s ID.'));
    expect(field).toHaveFocus();
  });

  it('refuses a user credential before sending anything', async () => {
    renderPage();
    await fillBigQuery();
    fireEvent.change(screen.getByTestId('wh-conn-service-account-json'), {
      target: { value: JSON.stringify({ type: 'authorized_user', client_id: 'x' }) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    const box = screen.getByTestId('wh-conn-service-account-json');
    expect(box).toHaveAttribute('aria-invalid', 'true');
    expect(box).toHaveAccessibleDescription(expect.stringContaining(KEY_NOT_SERVICE_ACCOUNT));
    expect(box).toHaveAttribute('autocomplete', 'off');
    expect(box).toHaveAttribute('spellcheck', 'false');
    expect(svc.testUnsavedConnection).not.toHaveBeenCalled();
  });

  it('reports a failed test with the API’s coded message', async () => {
    svc.testUnsavedConnection.mockRejectedValue(
      apiError(502, 'auth_failed', 'BigQuery did not accept the connection’s credentials.'),
    );
    renderPage();
    await fillBigQuery();
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await waitFor(() =>
      expect(screen.getByTestId('wh-conn-test-result')).toHaveTextContent(
        'Failed. Test failed: BigQuery did not accept the connection’s credentials.',
      ),
    );
  });

  it('reads the key from a chosen file, keyboard-operable alternative to pasting', async () => {
    renderPage();
    await fillBigQuery();
    fireEvent.change(screen.getByTestId('wh-conn-service-account-json'), { target: { value: '' } });
    const file = new File([SERVICE_ACCOUNT_JSON], 'key.json', { type: 'application/json' });
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Or choose the key file'), { target: { files: [file] } });
    });
    await waitFor(() => expect(screen.getByTestId('wh-conn-service-account-json')).toHaveValue(SERVICE_ACCOUNT_JSON));
  });
});

describe('Snowflake (with the connector enabled)', () => {
  it('refuses an administrative role before sending anything', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('radio', { name: 'Snowflake' }));
    type(/^Name/, 'SF');
    type(/^Account/, 'MYORG-MYACCOUNT');
    type(/^User/, 'CONTROLVAR_SVC');
    type(/^Role/, 'ACCOUNTADMIN');
    type(/^Warehouse/, 'ANALYTICS_XS');
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    const role = screen.getByLabelText(/^Role/);
    expect(role).toHaveAttribute('aria-invalid', 'true');
    expect(role).toHaveAccessibleDescription(expect.stringContaining(SNOWFLAKE_ROLE_REFUSED));
    expect(role).toHaveFocus();
    expect(svc.createConnection).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Test connection' })).not.toBeInTheDocument();
  });

  it('saves without a password and shows the statement that registers the generated key', async () => {
    svc.createConnection.mockResolvedValue({
      ...snowflakeConnection(),
      public_key: {
        public_key: 'MIIBIjANBgkq',
        public_key_fingerprint: 'SHA256:abc123=',
        statement: "ALTER USER CONTROLVAR_SVC SET RSA_PUBLIC_KEY='MIIBIjANBgkq';",
      },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('radio', { name: 'Snowflake' }));
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();
    type(/^Name/, 'Snowflake prod');
    type(/^Account/, 'MYORG-MYACCOUNT');
    type(/^User/, 'CONTROLVAR_SVC');
    type(/^Role/, 'CONTROLVAR_READER');
    type(/^Warehouse/, 'ANALYTICS_XS');
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    const sql = await screen.findByTestId('warehouse-key-statement-sql');
    expect(sql).toHaveTextContent("ALTER USER CONTROLVAR_SVC SET RSA_PUBLIC_KEY='MIIBIjANBgkq';");
    expect(sql).toHaveAttribute('tabindex', '0');
    expect(sql).toHaveAccessibleName('Snowflake statement');
    expect(screen.getByRole('button', { name: 'Copy Snowflake statement' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Go to the connection/ })).toHaveAttribute('href', '/warehouse/connections/c-sf');
    expect(mockPush).not.toHaveBeenCalled();
    expect(svc.createConnection.mock.calls[0][0]).toEqual({
      warehouse_type: 'snowflake',
      name: 'Snowflake prod',
      account: 'MYORG-MYACCOUNT',
      user: 'CONTROLVAR_SVC',
      role: 'CONTROLVAR_READER',
      warehouse: 'ANALYTICS_XS',
      query_timeout_seconds: 300,
      max_runs_per_day: 20,
    });
  });
});

describe('Amazon Athena (with the connector enabled)', () => {
  it('is saved first and tested afterwards: its external ID is generated on save', async () => {
    svc.listConnectors.mockResolvedValue(connectors({ athena: true }));
    renderPage();
    expect(await screen.findByRole('radio', { name: 'Amazon Athena' })).toBeChecked();
    expect(screen.queryByRole('button', { name: 'Test connection' })).not.toBeInTheDocument();
    expect(screen.getByText(/Saving generates the external ID/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/output|results location/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/external id/i)).not.toBeInTheDocument();
  });
});

describe('accessibility of the form', () => {
  it('labels every field and ties help text to it', async () => {
    renderPage();
    await screen.findByTestId('warehouse-connection-form');
    for (const el of Array.from(document.querySelectorAll('input:not([type=radio]), textarea, select'))) {
      expect(el).toHaveAccessibleName();
    }
    expect(screen.getByLabelText(/^Billing project/)).toHaveAccessibleDescription('Query jobs run and are billed in this project.');
    expect(screen.getByRole('group', { name: 'Warehouse' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Limits' })).toBeInTheDocument();
  });

  it('moves focus to the first field in error on submit', async () => {
    renderPage();
    await screen.findByTestId('warehouse-connection-form');
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    expect(screen.getByLabelText(/^Name/)).toHaveFocus();
    expect(screen.getByLabelText(/^Name/)).toHaveAccessibleDescription(expect.stringContaining('Name is required.'));
  });
});
