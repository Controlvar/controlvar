/**
 * Sources: create per kind and role (D25: ANALYST may define metric sources),
 * validate, preview (counts only) and delete, with the API's role matrix.
 */
import React from 'react';
import axe from 'axe-core';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ModulesProvider } from '@/contexts/ModulesContext';
import type { UserMe } from '@/services/api';
import NewSourcePage from '@modules/pages/warehouse/sources/new';
import SourcePage from '@modules/pages/warehouse/sources/[id]';
import { warehouseService } from '@modules/services/warehouse';
import { WAREHOUSE_INFO, apiError, connection, metricSource, source, user } from './fixtures';

let mockUser: UserMe | null = null;
let mockQuery: Record<string, string> = {};
const mockPush = jest.fn();

jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));
jest.mock('next/router', () => ({
  useRouter: () => ({ pathname: '/warehouse/sources', asPath: '/warehouse/sources', query: mockQuery, isReady: true, push: mockPush, replace: jest.fn() }),
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
    listConnections: jest.fn(),
    getSource: jest.fn(),
    createSource: jest.fn(),
    updateSource: jest.fn(),
    deleteSource: jest.fn(),
    validateSource: jest.fn(),
    previewSource: jest.fn(),
  },
}));

const svc = warehouseService as jest.Mocked<typeof warehouseService>;

function renderWith(page: React.ReactElement) {
  return render(<ModulesProvider initial={WAREHOUSE_INFO}>{page}</ModulesProvider>);
}

function type(label: RegExp | string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = user('ADMIN');
  mockQuery = {};
  svc.listConnections.mockResolvedValue([connection()]);
  svc.getSource.mockResolvedValue(source());
});

describe('new source', () => {
  it('lets ANALYST define a metric source and sends the mapping (no SQL)', async () => {
    mockUser = user('ANALYST');
    mockQuery = { kind: 'metric' };
    svc.createSource.mockResolvedValue(metricSource({ id: 's-new' }));
    renderWith(<NewSourcePage />);
    await screen.findByTestId('warehouse-source-form');
    expect(screen.queryByLabelText(/sql/i)).not.toBeInTheDocument();
    type(/^Name/, 'Revenue');
    type(/^Table or view/, 'acme-billing.experiments.orders');
    fireEvent.click(screen.getByLabelText('Mean'));
    type(/^User ID column/, 'user_id');
    type(/^Event time column/, 'ordered_at');
    type(/^Value column/, 'amount');
    type(/^Cap per user/, '500');
    fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    type(/^Filter 1 column/, 'country');
    fireEvent.change(screen.getByLabelText('Condition'), { target: { value: 'in' } });
    type(/^Values \(comma-separated\)/, 'GB, FR');
    fireEvent.click(screen.getByRole('button', { name: 'Save source' }));
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith('/warehouse/sources/s-new'));
    expect(svc.createSource.mock.calls[0][0]).toEqual({
      kind: 'metric',
      connection_id: 'c-1',
      name: 'Revenue',
      table: 'acme-billing.experiments.orders',
      columns: { unit_id: 'user_id', event_at: 'ordered_at', value: 'amount' },
      metric_type: 'mean',
      conversion_window_hours: 168,
      cap_value: 500,
      filters: [{ column: 'country', operator: 'in', value: ['GB', 'FR'] }],
    });
  });

  it('sends lower_is_better: true only when "Lower is better" is ticked', async () => {
    mockQuery = { kind: 'metric' };
    svc.createSource.mockResolvedValue(metricSource({ id: 's-new' }));
    renderWith(<NewSourcePage />);
    await screen.findByTestId('warehouse-source-form');
    const box = screen.getByRole('checkbox', { name: 'Lower is better' });
    expect(box).not.toBeChecked();
    expect(box).toHaveAccessibleDescription(expect.stringContaining('such as an error rate or churn'));
    type(/^Name/, 'Checkout errors');
    type(/^Table or view/, 'acme-billing.experiments.errors');
    type(/^User ID column/, 'user_id');
    type(/^Event time column/, 'failed_at');
    fireEvent.click(box);
    fireEvent.click(screen.getByRole('button', { name: 'Save source' }));
    await waitFor(() => expect(svc.createSource).toHaveBeenCalledTimes(1));
    expect(svc.createSource.mock.calls[0][0]).toEqual({
      kind: 'metric',
      connection_id: 'c-1',
      name: 'Checkout errors',
      table: 'acme-billing.experiments.errors',
      columns: { unit_id: 'user_id', event_at: 'failed_at' },
      metric_type: 'proportion',
      conversion_window_hours: 168,
      lower_is_better: true,
      filters: [],
    });
  });

  it('keeps the box ticked when the metric type changes', async () => {
    mockQuery = { kind: 'metric' };
    renderWith(<NewSourcePage />);
    await screen.findByTestId('warehouse-source-form');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Lower is better' }));
    fireEvent.click(screen.getByLabelText('Mean'));
    expect(screen.getByRole('checkbox', { name: 'Lower is better' })).toBeChecked();
  });

  it('has no "Lower is better" for an assignment source', async () => {
    mockQuery = { kind: 'assignment' };
    renderWith(<NewSourcePage />);
    await screen.findByTestId('warehouse-source-form');
    expect(screen.queryByRole('checkbox', { name: 'Lower is better' })).not.toBeInTheDocument();
  });

  it.each([false, true])('the metric form (ticked: %s) has no axe violations', async (ticked) => {
    mockQuery = { kind: 'metric' };
    const { container } = renderWith(<NewSourcePage />);
    await screen.findByTestId('warehouse-source-form');
    if (ticked) fireEvent.click(screen.getByRole('checkbox', { name: 'Lower is better' }));
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('refuses ANALYST an assignment source, with the API’s wording', async () => {
    mockUser = user('ANALYST');
    mockQuery = { kind: 'assignment' };
    renderWith(<NewSourcePage />);
    expect(await screen.findByTestId('warehouse-role-notice')).toHaveTextContent(
      'Creating an assignment source requires the ADMIN or DEVELOPER role; you are ANALYST.',
    );
    expect(screen.queryByTestId('warehouse-source-form')).not.toBeInTheDocument();
    expect(svc.listConnections).not.toHaveBeenCalled();
  });

  it('refuses VIEWER a metric source', async () => {
    mockUser = user('VIEWER');
    mockQuery = { kind: 'metric' };
    renderWith(<NewSourcePage />);
    expect(await screen.findByTestId('warehouse-role-notice')).toHaveTextContent('you are VIEWER.');
  });

  it('refuses a quoted table reference and a quoted filter value before sending, tied to their fields', async () => {
    mockUser = user('DEVELOPER');
    mockQuery = { kind: 'assignment' };
    renderWith(<NewSourcePage />);
    await screen.findByTestId('warehouse-source-form');
    type(/^Name/, 'Exposures');
    type(/^Table or view/, 'acme-billing.experiments.`exposures`');
    for (const [label, value] of [
      [/^User ID column/, 'user_id'],
      [/^Experiment key column/, 'experiment_key'],
      [/^Variant column/, 'variant'],
      [/^Assignment time column/, 'exposed_at'],
    ] as const) {
      type(label, value);
    }
    fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    type(/^Filter 1 column/, 'platform');
    type(/^Value$/, "web' OR 1=1");
    fireEvent.click(screen.getByRole('button', { name: 'Save source' }));
    const table = screen.getByLabelText(/^Table or view/);
    expect(table).toHaveAttribute('aria-invalid', 'true');
    expect(table).toHaveFocus();
    expect(table).toHaveAccessibleDescription(expect.stringMatching(/^Use project\.dataset\.table/));
    const value = screen.getByLabelText(/^Value$/);
    expect(value).toHaveAttribute('aria-invalid', 'true');
    expect(value).toHaveAccessibleDescription(expect.stringContaining('refused, not escaped'));
    expect(svc.createSource).not.toHaveBeenCalled();
  });

  it('shows an API refusal next to the field it names', async () => {
    mockQuery = { kind: 'assignment' };
    svc.createSource.mockRejectedValue(apiError(409, 'source_name_taken', 'This connection already has a source of this kind with that name.', 'name'));
    renderWith(<NewSourcePage />);
    await screen.findByTestId('warehouse-source-form');
    type(/^Name/, 'Exposures');
    type(/^Table or view/, 'acme-billing.experiments.exposures');
    type(/^User ID column/, 'user_id');
    type(/^Experiment key column/, 'experiment_key');
    type(/^Variant column/, 'variant');
    type(/^Assignment time column/, 'exposed_at');
    fireEvent.click(screen.getByRole('button', { name: 'Save source' }));
    const name = screen.getByLabelText(/^Name/);
    await waitFor(() => expect(name).toHaveAttribute('aria-invalid', 'true'));
    expect(name).toHaveAccessibleDescription(expect.stringContaining('already has a source'));
  });

  it('says a connection is needed when there is none', async () => {
    mockQuery = { kind: 'metric' };
    svc.listConnections.mockResolvedValue([]);
    renderWith(<NewSourcePage />);
    expect(await screen.findByTestId('warehouse-source-needs-connection')).toHaveTextContent('Create one first.');
  });
});

describe('source detail', () => {
  beforeEach(() => {
    mockQuery = { id: 's-1' };
  });

  it.each([
    ['ADMIN', 'assignment', ['Edit', 'Validate', 'Delete'], null],
    ['DEVELOPER', 'assignment', ['Edit', 'Validate', 'Delete'], null],
    ['ANALYST', 'assignment', [], 'Editing an assignment source requires the ADMIN or DEVELOPER role; you are ANALYST.'],
    ['ANALYST', 'metric', ['Edit', 'Validate'], 'Deleting a metric source requires the ADMIN or DEVELOPER role; you are ANALYST.'],
  ] as const)('offers %s on a %s source exactly what the API allows', async (role, kind, buttons, notice) => {
    mockUser = user(role);
    svc.getSource.mockResolvedValue(kind === 'metric' ? metricSource() : source());
    renderWith(<SourcePage />);
    await screen.findByTestId('warehouse-source-details');
    for (const name of ['Edit', 'Validate', 'Delete']) {
      if ((buttons as readonly string[]).includes(name)) expect(screen.getByRole('button', { name })).toBeInTheDocument();
      else expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    if (notice) expect(screen.getByTestId('warehouse-role-notice')).toHaveTextContent(notice);
    else expect(screen.queryByTestId('warehouse-role-notice')).not.toBeInTheDocument();
    const preview = screen.queryByRole('button', { name: 'Run preview' });
    expect(!!preview).toBe(role !== 'ANALYST' || kind === 'metric');
  });

  it('refuses VIEWER and loads nothing', async () => {
    mockUser = user('VIEWER');
    renderWith(<SourcePage />);
    expect(await screen.findByTestId('warehouse-role-notice')).toHaveTextContent(
      'Viewing warehouse sources requires the ADMIN, DEVELOPER or ANALYST role; you are VIEWER.',
    );
    expect(svc.getSource).not.toHaveBeenCalled();
  });

  it('validates and announces the resolved columns', async () => {
    svc.getSource.mockResolvedValue(source({ validated_at: null }));
    svc.validateSource.mockResolvedValue({
      source: source(),
      columns: [
        { name: 'user_id', type: 'STRING' },
        { name: 'exposed_at', type: 'TIMESTAMP' },
      ],
    });
    renderWith(<SourcePage />);
    expect(await screen.findByTestId('warehouse-preview-needs-validation')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }));
    const result = screen.getByTestId('warehouse-validation-result');
    await waitFor(() => expect(result).toHaveTextContent('Validated: the table and the mapped columns exist'));
    expect(result).toHaveAttribute('aria-live', 'polite');
    expect(result).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Run preview' })).toBeInTheDocument();
  });

  it('shows a refused validation with the API’s message', async () => {
    svc.validateSource.mockRejectedValue(
      apiError(422, 'unsupported_column_type', 'columns.exposed_at must be a timestamp column.', 'columns.exposed_at'),
    );
    renderWith(<SourcePage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Validate' }));
    await waitFor(() =>
      expect(screen.getByTestId('warehouse-validation-result')).toHaveTextContent('Not validated: columns.exposed_at must be a timestamp column.'),
    );
  });

  it('previews counts only, with the window and experiment key sent in UTC', async () => {
    svc.previewSource.mockResolvedValue({
      kind: 'assignment',
      window_start: '2026-09-01T00:00:00Z',
      window_end: '2026-09-08T00:00:00Z',
      total_rows: 12345,
      null_unit_rows: 2,
      null_variant_rows: 0,
      variants: [
        { label: 'control', units: 6000 },
        { label: 'treatment', units: 6100 },
      ],
      null_value_rows: null,
      earliest: '2026-09-01T00:03:00Z',
      latest: '2026-09-07T23:59:00Z',
      run_id: 'r-1',
    });
    renderWith(<SourcePage />);
    await screen.findByTestId('warehouse-source-details');
    fireEvent.change(screen.getByLabelText('From (UTC date)'), { target: { value: '2026-09-01' } });
    fireEvent.change(screen.getByLabelText('Until (UTC date, not included)'), { target: { value: '2026-09-08' } });
    fireEvent.change(screen.getByLabelText('Experiment key (optional)'), { target: { value: 'checkout-v2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run preview' }));
    const result = screen.getByTestId('warehouse-preview-result');
    await waitFor(() => expect(result).toHaveTextContent('12,345'));
    expect(svc.previewSource).toHaveBeenCalledWith('s-1', {
      window_start: '2026-09-01T00:00:00Z',
      window_end: '2026-09-08T00:00:00Z',
      experiment_key: 'checkout-v2',
    });
    const variants = within(result).getByRole('table', { name: 'Users per variant value' });
    expect(variants).toHaveTextContent('treatment');
    expect(variants).toHaveTextContent('6,100');
    expect(result.querySelectorAll('time')[0]).toHaveAttribute('datetime', '2026-09-01T00:00:00.000Z');
    expect(screen.getByText(/counts towards this connection's limit of 20 analyses per day/)).toBeInTheDocument();
  });

  it('shows the daily-limit refusal from a preview', async () => {
    svc.previewSource.mockRejectedValue(
      apiError(429, 'daily_run_limit_reached', 'Not run: Prod analytics has reached its limit of 20 analyses per day (UTC, previews included).'),
    );
    renderWith(<SourcePage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Run preview' }));
    await waitFor(() =>
      expect(screen.getByTestId('warehouse-preview-result')).toHaveTextContent('Preview failed: Not run: Prod analytics has reached its limit'),
    );
  });

  it('does not offer validate or preview when the connection’s connector is not available', async () => {
    svc.listConnections.mockResolvedValue([connection({ enabled: false })]);
    renderWith(<SourcePage />);
    expect(await screen.findByTestId('warehouse-connection-unavailable')).toHaveTextContent("BigQuery isn't available");
    expect(screen.getByRole('button', { name: 'Validate' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Run preview' })).not.toBeInTheDocument();
  });

  it('edits in place and says the source needs validating again', async () => {
    svc.updateSource.mockImplementation(async (_id, body) => source({ name: body.name, validated_at: null }));
    renderWith(<SourcePage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    type(/^Name/, 'Exposures v2');
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByText('Changes saved. Validate the source again before using it.')).toBeInTheDocument();
    const body = svc.updateSource.mock.calls[0][1];
    expect(body).not.toHaveProperty('connection_id');
    expect(body).toMatchObject({ kind: 'assignment', name: 'Exposures v2' });
  });

  it.each([
    [true, 'Yes'],
    [false, 'No'],
  ])('shows a metric source marked lower is better: %s as "%s"', async (lower, text) => {
    svc.getSource.mockResolvedValue(metricSource({ lower_is_better: lower }));
    renderWith(<SourcePage />);
    const details = await screen.findByTestId('warehouse-source-details');
    const term = within(details).getByText('Lower is better');
    expect(term.nextElementSibling).toHaveTextContent(text);
  });

  it('shows no direction for an assignment source', async () => {
    renderWith(<SourcePage />);
    const details = await screen.findByTestId('warehouse-source-details');
    expect(within(details).queryByText('Lower is better')).not.toBeInTheDocument();
  });

  it('keeps a stored "lower is better" when an edit changes only the name', async () => {
    svc.getSource.mockResolvedValue(metricSource({ lower_is_better: true }));
    svc.updateSource.mockImplementation(async (_id, body) => metricSource({ name: body.name, validated_at: null }));
    renderWith(<SourcePage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByRole('checkbox', { name: 'Lower is better' })).toBeChecked();
    type(/^Name/, 'Checkout errors');
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(svc.updateSource).toHaveBeenCalledTimes(1));
    expect(svc.updateSource.mock.calls[0][1]).toMatchObject({ name: 'Checkout errors', lower_is_better: true });
  });

  it('sends no lower_is_better once it is unticked, which the API reads as false', async () => {
    svc.getSource.mockResolvedValue(metricSource({ lower_is_better: true }));
    svc.updateSource.mockImplementation(async () => metricSource({ validated_at: null }));
    renderWith(<SourcePage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Lower is better' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(svc.updateSource).toHaveBeenCalledTimes(1));
    expect(svc.updateSource.mock.calls[0][1]).not.toHaveProperty('lower_is_better');
  });

  it('the metric source page has no axe violations', async () => {
    svc.getSource.mockResolvedValue(metricSource({ lower_is_better: true }));
    const { container } = renderWith(<SourcePage />);
    await screen.findByTestId('warehouse-source-details');
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('deletes after confirming', async () => {
    svc.deleteSource.mockResolvedValue(undefined);
    renderWith(<SourcePage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete source' }));
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith('/warehouse?tab=assignment'));
  });
});
