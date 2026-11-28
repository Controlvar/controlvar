/**
 * The Audit Log page filtered to one item or person (#2273): the address's
 * `entity_type`, `entity_id` and `user_id`, the removable "Showing changes
 * to" line, the Person box, and the filters reaching both the list and the
 * export.
 *
 * Every case renders the page module with the signed-in person resolved, for
 * the five people the API tells apart: a VIEWER who is a superuser, an ADMIN
 * who is not, a DEVELOPER, an ANALYST and a VIEWER. The audit API narrows the
 * list to the caller's own entries for DEVELOPER and VIEWER; the page narrows
 * nothing itself, so what it must get right is which request it makes.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AuditLogPage, OWN_SCOPE_NOTE, SCOPE_IGNORED_NOTE } from '@/pages/admin/audit';
import { PERSON_INVALID_MESSAGE } from '@/components/admin/audit/AuditLogFilter';
import { AdminService } from '@/services/admin';
import { AuditLog } from '@/types/admin';
import { makeRouter } from './helpers/apiMock';

jest.mock('@/services/admin', () => ({
  AdminService: { listAuditLogs: jest.fn(), exportAuditLogs: jest.fn() },
}));

let mockRouter = makeRouter({ pathname: '/admin/audit', query: {} });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockAuth() }));

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

const FLAG_ID = '3f2a9c1e-5b7d-4e8a-9c3f-1a2b3c4d5e6f';
const PERSON_ID = '9b1d6c2a-7e4f-4a3b-8d5c-0e1f2a3b4c5d';

const PEOPLE = [
  ['superuser VIEWER', 'VIEWER', true, true],
  ['ADMIN', 'ADMIN', false, true],
  ['DEVELOPER', 'DEVELOPER', false, false],
  ['ANALYST', 'ANALYST', false, true],
  ['VIEWER', 'VIEWER', false, false],
] as const;

function signIn(role: string, isSuperuser: boolean) {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: {
      id: 'u-me',
      username: 'me',
      email: 'me@example.com',
      full_name: null,
      role,
      is_superuser: isSuperuser,
      is_active: true,
      auth_provider: 'local',
    },
  });
}

const entry = (overrides: Partial<AuditLog> = {}): AuditLog => ({
  id: 'log-1',
  user_id: 'u-me',
  user_email: 'me@example.com',
  action_type: 'toggle_disable',
  entity_type: 'feature_flag',
  entity_id: FLAG_ID,
  entity_name: 'Player v2',
  old_value: 'ACTIVE',
  new_value: 'INACTIVE',
  reason: undefined,
  timestamp: '2026-10-01T10:30:00Z',
  created_at: '2026-10-01T10:30:00Z',
  action_description: 'disabled',
  ...overrides,
});

function open(query: Record<string, string | string[]>) {
  mockRouter = makeRouter({ pathname: '/admin/audit', query });
}

async function renderPage() {
  const view = render(<AuditLogPage />);
  await screen.findByTestId('audit-log-row-log-1');
  return view;
}

/** Every argument the list was asked with, in order. */
const listArgs = () => listAuditLogs.mock.calls.map(([args]) => args ?? {});
const exportArgs = () => exportAuditLogs.mock.calls.map(([, args]) => args ?? {});

beforeEach(() => {
  jest.clearAllMocks();
  open({});
  signIn('ANALYST', false);
  listAuditLogs.mockResolvedValue({ items: [entry()], total: 1, page: 1, limit: 50 });
  exportAuditLogs.mockResolvedValue({ text: '', headers: new Headers() });
});

describe('a History link opens the page already filtered', () => {
  it.each(PEOPLE)('%s: the first list request names the item, and no unfiltered one is made', async (_n, role, su) => {
    signIn(role, su);
    open({ entity_type: 'feature_flag', entity_id: FLAG_ID });
    await renderPage();
    expect(listArgs()).toHaveLength(1);
    expect(listArgs()[0]).toMatchObject({ entity_type: 'feature_flag', entity_id: FLAG_ID });
    expect(listArgs()[0]).not.toHaveProperty('user_id');
    const line = screen.getByTestId('audit-scope-item');
    expect(line).toHaveTextContent(`Showing changes to feature flag ${FLAG_ID}`);
  });

  it('holds the list back until the address has been read', async () => {
    open({});
    mockRouter = makeRouter({ pathname: '/admin/audit', query: {}, isReady: false });
    render(<AuditLogPage />);
    expect(screen.getByTestId('audit-log-filter')).toBeInTheDocument();
    expect(screen.queryByTestId('audit-log-table')).toBeNull();
    expect(listAuditLogs).not.toHaveBeenCalled();
  });

  it('a bare Audit Log page shows no "Showing changes to" line and asks for everything', async () => {
    await renderPage();
    expect(screen.queryByTestId('audit-scope-item')).toBeNull();
    expect(listArgs()[0]).not.toHaveProperty('entity_id');
  });

  it('removing the line lists everything again and clears the address', async () => {
    open({ entity_type: 'feature_flag', entity_id: FLAG_ID });
    await renderPage();
    fireEvent.click(screen.getByTestId('audit-scope-remove'));
    await waitFor(() => expect(listArgs().some((a) => !('entity_id' in a))).toBe(true));
    expect(screen.queryByTestId('audit-scope-item')).toBeNull();
    expect(mockRouter.replace).toHaveBeenCalledTimes(1);
    const [target] = mockRouter.replace.mock.calls[0];
    expect(target.query).not.toHaveProperty('entity_id');
  });

  it('Clear filters removes the item too', async () => {
    open({ entity_type: 'feature_flag', entity_id: FLAG_ID });
    await renderPage();
    fireEvent.click(screen.getByTestId('clear-filters-button'));
    await waitFor(() => expect(screen.queryByTestId('audit-scope-item')).toBeNull());
    expect(listArgs()[listArgs().length - 1]).not.toHaveProperty('entity_id');
  });

  it('the downloads carry the item, and the person where one is set', async () => {
    signIn('ADMIN', false);
    open({ entity_type: 'experiment', entity_id: FLAG_ID, user_id: PERSON_ID });
    await renderPage();
    fireEvent.click(screen.getByTestId('audit-download-csv'));
    await waitFor(() => expect(exportAuditLogs).toHaveBeenCalledTimes(1));
    expect(exportArgs()[0]).toMatchObject({ entity_type: 'experiment', entity_id: FLAG_ID, user_id: PERSON_ID });
  });

  it('a value the API would refuse is left out, and the page says so', async () => {
    open({ entity_type: 'feature_flag', entity_id: 'not-an-id' });
    await renderPage();
    expect(listArgs()).toHaveLength(1);
    expect(listArgs()[0]).toMatchObject({ entity_type: 'feature_flag' });
    expect(JSON.stringify(listArgs())).not.toContain('not-an-id');
    expect(screen.getByTestId('audit-scope-ignored')).toHaveTextContent(SCOPE_IGNORED_NOTE);
    expect(screen.queryByTestId('audit-scope-item')).toBeNull();
  });

  it('an unknown entity type is left out too', async () => {
    open({ entity_type: 'bogus', entity_id: FLAG_ID });
    await renderPage();
    expect(listArgs()[0].entity_type).toBeUndefined();
    expect(listArgs()[0]).toMatchObject({ entity_id: FLAG_ID });
    expect(screen.getByTestId('audit-scope-ignored')).toBeInTheDocument();
  });
});

describe('the Person filter', () => {
  it.each(PEOPLE)('%s: the box is present only for people who read everyone\'s entries', async (_n, role, su, reads) => {
    signIn(role, su);
    await renderPage();
    if (reads) {
      expect(screen.getByTestId('filter-person')).toBeInTheDocument();
      expect(screen.queryByTestId('audit-log-scope-own')).toBeNull();
    } else {
      expect(screen.queryByTestId('filter-person')).toBeNull();
      expect(screen.getByTestId('audit-log-scope-own')).toHaveTextContent(OWN_SCOPE_NOTE);
    }
  });

  it.each(PEOPLE)('%s: a user_id in the address is sent only where the box is shown', async (_n, role, su, reads) => {
    signIn(role, su);
    open({ user_id: PERSON_ID });
    await renderPage();
    const sent = listArgs().some((a) => a.user_id === PERSON_ID);
    expect(sent).toBe(reads);
    if (reads) expect(screen.getByTestId('filter-person')).toHaveValue(PERSON_ID);
  });

  it('typing a valid id asks for that person, and emptying the box asks for everyone', async () => {
    signIn('ADMIN', false);
    await renderPage();
    const box = screen.getByTestId('filter-person');
    fireEvent.change(box, { target: { value: PERSON_ID } });
    await waitFor(() => expect(listArgs().some((a) => a.user_id === PERSON_ID)).toBe(true));
    expect(mockRouter.replace).toHaveBeenCalled();
    fireEvent.change(box, { target: { value: '' } });
    await waitFor(() => expect(listArgs()[listArgs().length - 1]).not.toHaveProperty('user_id'));
  });

  it('an id with spaces around it is sent without them', async () => {
    signIn('ADMIN', false);
    await renderPage();
    fireEvent.change(screen.getByTestId('filter-person'), { target: { value: `  ${PERSON_ID} ` } });
    await waitFor(() => expect(listArgs().some((a) => a.user_id === PERSON_ID)).toBe(true));
  });

  it('something that is not an id is explained at the box and sends nothing', async () => {
    signIn('ADMIN', false);
    await renderPage();
    const before = listAuditLogs.mock.calls.length;
    const box = screen.getByTestId('filter-person');
    fireEvent.change(box, { target: { value: 'alice' } });
    const error = await screen.findByTestId('filter-person-error');
    expect(error).toHaveTextContent(PERSON_INVALID_MESSAGE);
    expect(box).toHaveAttribute('aria-invalid', 'true');
    expect(box).toHaveAttribute('aria-describedby', 'filter-person-error');
    expect(box).toHaveValue('alice');
    expect(listAuditLogs.mock.calls).toHaveLength(before);
    fireEvent.change(box, { target: { value: PERSON_ID } });
    await waitFor(() => expect(screen.queryByTestId('filter-person-error')).toBeNull());
    expect(box).not.toHaveAttribute('aria-invalid');
  });
});

describe('the address changes after the page has opened', () => {
  const OTHER_ID = '5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a';
  const lastList = () => listArgs()[listArgs().length - 1];

  it('a different item in the address replaces the old one', async () => {
    open({ entity_type: 'feature_flag', entity_id: FLAG_ID });
    const { rerender } = await renderPage();
    open({ entity_type: 'experiment', entity_id: OTHER_ID });
    rerender(<AuditLogPage />);
    await waitFor(() => expect(lastList()).toMatchObject({ entity_type: 'experiment', entity_id: OTHER_ID }));
    expect(screen.getByTestId('audit-scope-item')).toHaveTextContent(`Showing changes to experiment ${OTHER_ID}`);
  });

  it('an address with no item drops the old filter and its line', async () => {
    open({ entity_type: 'feature_flag', entity_id: FLAG_ID });
    const { rerender } = await renderPage();
    open({});
    rerender(<AuditLogPage />);
    await waitFor(() => expect(screen.queryByTestId('audit-scope-item')).toBeNull());
    await waitFor(() => expect(lastList()).not.toHaveProperty('entity_id'));
    expect(lastList().entity_type).toBeUndefined();
  });

  it('a different person in the address fills the Person box again', async () => {
    signIn('ADMIN', false);
    const SECOND = '1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9';
    open({ user_id: PERSON_ID });
    const { rerender } = await renderPage();
    expect(screen.getByTestId('filter-person')).toHaveValue(PERSON_ID);
    open({ user_id: SECOND });
    rerender(<AuditLogPage />);
    await waitFor(() => expect(screen.getByTestId('filter-person')).toHaveValue(SECOND));
    await waitFor(() => expect(lastList()).toMatchObject({ user_id: SECOND }));
  });

  it('keeps the filters the address does not carry', async () => {
    open({ entity_type: 'feature_flag', entity_id: FLAG_ID });
    const { rerender } = await renderPage();
    fireEvent.change(screen.getByTestId('filter-action-type'), { target: { value: 'toggle_disable' } });
    await waitFor(() => expect(lastList()).toMatchObject({ action_type: 'toggle_disable' }));
    open({ entity_type: 'experiment', entity_id: OTHER_ID });
    rerender(<AuditLogPage />);
    await waitFor(() => expect(lastList()).toMatchObject({ entity_id: OTHER_ID }));
    expect(lastList()).toMatchObject({ action_type: 'toggle_disable' });
  });

  it('asks for the list once, with the item, when the address becomes ready', async () => {
    mockRouter = makeRouter({ pathname: '/admin/audit', query: {}, isReady: false });
    const { rerender } = render(<AuditLogPage />);
    expect(listAuditLogs).not.toHaveBeenCalled();
    mockRouter = makeRouter({
      pathname: '/admin/audit',
      query: { entity_type: 'feature_flag', entity_id: FLAG_ID },
      isReady: true,
    });
    rerender(<AuditLogPage />);
    await screen.findByTestId('audit-log-row-log-1');
    expect(listArgs()).toHaveLength(1);
    expect(listArgs()[0]).toMatchObject({ entity_type: 'feature_flag', entity_id: FLAG_ID });
  });
});

describe('when the list cannot be read', () => {
  it('shows the failure and still offers to remove the item', async () => {
    listAuditLogs.mockRejectedValue(new Error('boom'));
    open({ entity_type: 'feature_flag', entity_id: FLAG_ID });
    render(<AuditLogPage />);
    await screen.findByTestId('audit-log-error-state');
    expect(screen.getByTestId('audit-scope-remove')).toBeInTheDocument();
  });

  it('shows "No audit logs found" for an item with no entries', async () => {
    listAuditLogs.mockResolvedValue({ items: [], total: 0, page: 1, limit: 50 });
    open({ entity_type: 'feature_flag', entity_id: FLAG_ID });
    render(<AuditLogPage />);
    await screen.findByTestId('audit-log-empty-state');
    expect(screen.getByTestId('audit-scope-item')).toBeInTheDocument();
  });
});
