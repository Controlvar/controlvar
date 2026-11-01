/**
 * /warehouse: the connections and sources lists, per role, with connectors
 * disabled, enabled, and as shipped (Snowflake and BigQuery enabled, Athena not).
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ModulesProvider } from '@/contexts/ModulesContext';
import type { UserMe } from '@/services/api';
import WarehousePage from '@modules/pages/warehouse/index';
import { warehouseService } from '@modules/services/warehouse';
import { NO_CONNECTOR_TEXT } from '@modules/components/warehouse/common';
import { WAREHOUSE_INFO, apiError, connection, connectors, metricSource, source, user } from './fixtures';

let mockUser: UserMe | null = null;
let mockQuery: Record<string, string> = {};
const mockReplace = jest.fn();

jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));
jest.mock('next/router', () => ({
  useRouter: () => ({ pathname: '/warehouse', asPath: '/warehouse', query: mockQuery, isReady: true, push: jest.fn(), replace: mockReplace }),
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
    listConnections: jest.fn(),
    listSources: jest.fn(),
  },
}));

const svc = warehouseService as jest.Mocked<typeof warehouseService>;

function setData({
  enabled = {},
  conns = [connection()],
  srcs = [source(), metricSource()],
}: { enabled?: Parameters<typeof connectors>[0]; conns?: ReturnType<typeof connection>[]; srcs?: ReturnType<typeof source>[] } = {}) {
  svc.listConnectors.mockResolvedValue(connectors(enabled));
  svc.listConnections.mockResolvedValue(conns);
  svc.listSources.mockResolvedValue(srcs);
}

function renderPage() {
  return render(
    <ModulesProvider initial={WAREHOUSE_INFO}>
      <WarehousePage />
    </ModulesProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockQuery = {};
  mockUser = user('ADMIN');
  setData();
});

describe('states', () => {
  it('shows a loading status, then the connections', async () => {
    let resolve: (v: ReturnType<typeof connectors>) => void = () => {};
    svc.listConnectors.mockReturnValue(new Promise((r) => (resolve = r)));
    renderPage();
    expect(screen.getByRole('status')).toHaveTextContent('Loading warehouse connections');
    await act(async () => resolve(connectors()));
    expect(await screen.findByTestId('warehouse-connections-table')).toHaveTextContent('Prod analytics');
  });

  it('shows the empty state when there are no connections', async () => {
    setData({ conns: [], srcs: [] });
    renderPage();
    expect(await screen.findByTestId('warehouse-connections-empty')).toHaveTextContent('No warehouse connections yet.');
  });

  it('shows a load error with a retry that loads again', async () => {
    svc.listConnections.mockRejectedValueOnce(apiError(503, 'credentials_unavailable', 'Warehouse credentials can’t be stored.'));
    renderPage();
    const box = await screen.findByTestId('warehouse-load-error');
    expect(box).toHaveTextContent('Warehouse credentials can’t be stored.');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(within(box).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByTestId('warehouse-connections-table')).toBeInTheDocument();
    expect(svc.listConnections).toHaveBeenCalledTimes(2);
  });

  it('shows the Beta chip with its meaning on keyboard focus', async () => {
    renderPage();
    const chip = await screen.findByTestId('warehouse-beta-chip');
    expect(chip).toHaveTextContent('Beta');
    const tip = document.getElementById(chip.getAttribute('aria-describedby')!)!;
    expect(tip).toHaveTextContent('Warehouse analysis is in beta');
    expect(tip).toHaveClass('sr-only');
    fireEvent.focus(chip);
    expect(tip).not.toHaveClass('sr-only');
  });
});

describe('connectors that are not available', () => {
  it('lists every connector as not yet available and offers no way to create one', async () => {
    setData({ conns: [] });
    renderPage();
    expect(await screen.findByTestId('warehouse-connector-bigquery')).toHaveTextContent('BigQuery: Not yet available');
    expect(screen.getByTestId('warehouse-connector-snowflake')).toHaveTextContent('Not yet available');
    expect(screen.getByTestId('warehouse-connector-athena')).toHaveTextContent('Not yet available');
    expect(screen.getByTestId('warehouse-no-connector')).toHaveTextContent(NO_CONNECTOR_TEXT);
    expect(screen.getByTestId('warehouse-create-unavailable')).toBeInTheDocument();
    expect(screen.queryByTestId('warehouse-new-connection')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /new connection/i })).not.toBeInTheDocument();
  });

  it('offers New connection to ADMIN once a connector is enabled', async () => {
    setData({ enabled: { bigquery: true } });
    renderPage();
    const link = await screen.findByTestId('warehouse-new-connection');
    expect(link).toHaveAttribute('href', '/warehouse/connections/new');
    expect(screen.getByTestId('warehouse-connector-bigquery')).toHaveTextContent('BigQuery: Available');
    expect(screen.queryByTestId('warehouse-no-connector')).not.toBeInTheDocument();
  });

  it('as shipped: Snowflake and BigQuery are available and Amazon Athena is not yet', async () => {
    setData({ conns: [], enabled: { snowflake: true, bigquery: true } });
    renderPage();
    expect(await screen.findByTestId('warehouse-connector-snowflake')).toHaveTextContent('Snowflake: Available');
    expect(screen.getByTestId('warehouse-connector-bigquery')).toHaveTextContent('BigQuery: Available');
    expect(screen.getByTestId('warehouse-connector-athena')).toHaveTextContent('Amazon Athena: Not yet available');
    expect(screen.queryByTestId('warehouse-no-connector')).not.toBeInTheDocument();
    expect(screen.getByTestId('warehouse-new-connection')).toHaveAttribute('href', '/warehouse/connections/new');
  });

  it('marks a connection whose connector is not available', async () => {
    setData({ conns: [connection({ enabled: false })] });
    renderPage();
    const table = await screen.findByTestId('warehouse-connections-table');
    expect(table).toHaveTextContent('(not yet available)');
  });
});

describe('roles', () => {
  it('shows VIEWER the API’s refusal and requests nothing', async () => {
    mockUser = user('VIEWER');
    renderPage();
    expect(await screen.findByTestId('warehouse-role-notice')).toHaveTextContent(
      'Viewing warehouse connections requires the ADMIN, DEVELOPER or ANALYST role; you are VIEWER.',
    );
    expect(svc.listConnectors).not.toHaveBeenCalled();
    expect(svc.listConnections).not.toHaveBeenCalled();
    expect(svc.listSources).not.toHaveBeenCalled();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
  });

  it.each(['DEVELOPER', 'ANALYST'] as const)(
    'shows %s why there is no New connection, even with a connector enabled',
    async (role) => {
      mockUser = user(role);
      setData({ enabled: { bigquery: true, snowflake: true } });
      renderPage();
      expect(await screen.findByTestId('warehouse-role-notice')).toHaveTextContent(
        `Creating a warehouse connection requires the ADMIN role; you are ${role}.`,
      );
      expect(screen.queryByTestId('warehouse-new-connection')).not.toBeInTheDocument();
    },
  );

  it('lets ANALYST create metric sources but not assignment sources (D25)', async () => {
    mockUser = user('ANALYST');
    mockQuery = { tab: 'metric' };
    const view = renderPage();
    expect(await screen.findByTestId('warehouse-new-metric-source')).toHaveAttribute('href', '/warehouse/sources/new?kind=metric');
    view.unmount();

    mockQuery = { tab: 'assignment' };
    renderPage();
    expect(await screen.findByTestId('warehouse-role-notice')).toHaveTextContent(
      'Creating an assignment source requires the ADMIN or DEVELOPER role; you are ANALYST.',
    );
    expect(screen.queryByTestId('warehouse-new-assignment-source')).not.toBeInTheDocument();
  });

  it('lets DEVELOPER create both kinds of source', async () => {
    mockUser = user('DEVELOPER');
    mockQuery = { tab: 'assignment' };
    renderPage();
    expect(await screen.findByTestId('warehouse-new-assignment-source')).toBeInTheDocument();
  });

  it('treats a superuser VIEWER as ADMIN, as the API does', async () => {
    mockUser = user('VIEWER', true);
    setData({ enabled: { snowflake: true } });
    renderPage();
    expect(await screen.findByTestId('warehouse-new-connection')).toBeInTheDocument();
  });
});

describe('sources tabs', () => {
  it('tags a metric source marked lower is better in the list, outside its link', async () => {
    mockQuery = { tab: 'metric' };
    setData({
      srcs: [
        source(),
        metricSource({ id: 's-err', name: 'Checkout errors', lower_is_better: true }),
        metricSource({ id: 's-buy', name: 'Purchases', lower_is_better: false }),
      ],
    });
    renderPage();
    const table = await screen.findByTestId('warehouse-metric-table');
    const tags = within(table).getAllByTestId('warehouse-lower-is-better');
    expect(tags).toHaveLength(1);
    expect(tags[0]).toHaveTextContent('lower is better');
    const [errors, purchases] = within(table).getAllByRole('row').slice(1);
    expect(errors).toContainElement(tags[0]);
    expect(within(errors).getByRole('link')).toHaveAccessibleName('Checkout errors');
    expect(purchases).not.toHaveTextContent('lower is better');
  });

  it('lists each kind on its own tab, with its validation state', async () => {
    mockQuery = { tab: 'metric' };
    setData({ srcs: [source(), metricSource({ validated_at: null })] });
    renderPage();
    const table = await screen.findByTestId('warehouse-metric-table');
    expect(table).toHaveTextContent('Purchases');
    expect(table).toHaveTextContent('Not validated');
    expect(table).not.toHaveTextContent('Exposures');
  });

  it('says a source needs a connection when there is none', async () => {
    mockQuery = { tab: 'assignment' };
    setData({ conns: [], srcs: [] });
    renderPage();
    expect(await screen.findByTestId('warehouse-source-needs-connection')).toBeInTheDocument();
    expect(screen.getByTestId('warehouse-assignment-empty')).toHaveTextContent('No assignment sources yet.');
    // "An", not "A": the article follows the noun (#921).
    expect(screen.getByTestId('warehouse-source-needs-connection')).toHaveTextContent(
      'An assignment source reads a table through a warehouse connection.',
    );
  });

  it('says the same of a metric source, with its own article (#921)', async () => {
    mockQuery = { tab: 'metric' };
    setData({ conns: [], srcs: [] });
    renderPage();
    expect(await screen.findByTestId('warehouse-source-needs-connection')).toHaveTextContent(
      'A metric source reads a table through a warehouse connection.',
    );
  });

  it('moves between tabs with the arrow keys and keeps the URL in step', async () => {
    renderPage();
    const tab = await screen.findByRole('tab', { name: 'Connections' });
    expect(tab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', tab.id);
    fireEvent.keyDown(tab, { key: 'ArrowRight' });
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith({ pathname: '/warehouse', query: { tab: 'assignment' } }, undefined, { shallow: true }),
    );
    fireEvent.keyDown(tab, { key: 'End' });
    expect(mockReplace).toHaveBeenLastCalledWith({ pathname: '/warehouse', query: { tab: 'metric' } }, undefined, { shallow: true });
  });
});
