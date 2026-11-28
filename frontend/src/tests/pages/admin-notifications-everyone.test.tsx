/**
 * /admin/notifications: "Everyone's settings" and the delivery log (#2277).
 *
 * Both reads answer role ADMIN only (`GET /notifications/admin/preferences`,
 * `GET /notifications/delivery-log`): a superuser who is not an ADMIN is
 * refused, an ADMIN who is not a superuser is accepted, and the page itself
 * is a superuser's. The five people of each control are rendered with the
 * real page, AdminService and apiFetch's mock; whoever is refused loads the
 * page, is told so, and sends no request to either route.
 */
import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import NotificationsPage, { NotificationsAdminPage, DELIVERY_LOG_REFUSED } from '@/pages/admin/notifications';
import { apiFetch } from '@/services/api';
import { EVERYONE_EMPTY, EVERYONE_FAILED, EVERYONE_REFUSED } from '@/components/admin/notifications/AllNotificationSettings';
import { schemaExample } from '@/tests/helpers/schemaExample';
import { shapeErrors } from '@/tests/helpers/openapiShape';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/admin/notifications' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockUseAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const ALL = '/api/v1/notifications/admin/preferences';
const LOG = '/api/v1/notifications/delivery-log';
const MINE = '/api/v1/notifications/preferences';

const PLANTED = 'Traceback (most recent call last): PLANTED-everyone-3b9';

const pref = (over: Record<string, unknown>) =>
  schemaExample('NotificationPreferenceResponse', {
    id: 'pref-a',
    user_id: 'user-a',
    notify_experiment_started: true,
    notify_experiment_completed: false,
    notify_safety_rollback: true,
    notify_rollout_advanced: false,
    slack_channel: '#alerts',
    email_override: null,
    created_at: '2026-11-01T10:00:00',
    updated_at: '2026-11-01T10:00:00',
    ...over,
  });

const ROWS = [
  pref({}),
  pref({ id: 'pref-b', user_id: 'user-b', notify_rollout_advanced: true, slack_channel: null, email_override: 'ops@example.com' }),
];

const as = (role: string, extra: Record<string, unknown> = {}) => ({
  id: 'user-1',
  email: `${role.toLowerCase()}@demo.com`,
  username: role.toLowerCase(),
  role,
  is_superuser: false,
  ...extra,
});
const signIn = (user: Record<string, unknown>) => mockUseAuth.mockReturnValue({ user, status: 'authenticated' });

function install(all: () => unknown = () => ROWS) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: MINE, handler: () => ROWS[0] },
      { path: ALL, handler: () => all() },
      { path: LOG, handler: () => ({ items: [], total: 0, page: 1, limit: 20 }) },
    ]) as unknown as typeof apiFetch,
  );
}

const callsTo = (path: string) => mockedApiFetch.mock.calls.filter(([url]) => String(url).split('?')[0] === path);

/** Renders the page and waits until its own reads have been answered. */
async function renderPage(all?: () => unknown) {
  install(all);
  const view = render(<NotificationsAdminPage />);
  await screen.findByTestId('notification-prefs-form');
  await act(async () => {});
  return view;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn(as('ADMIN', { is_superuser: true }));
});

describe('the rows the page is fed', () => {
  it("are the API's NotificationPreferenceResponse", () => {
    for (const row of ROWS) expect(shapeErrors('NotificationPreferenceResponse', row)).toEqual([]);
  });
  it('a row with a number for a flag is refused by the shape check (the positive control)', () => {
    expect(shapeErrors('NotificationPreferenceResponse', { ...ROWS[0], notify_safety_rollback: 5 })).not.toEqual([]);
  });
});

describe("who reads everyone's settings and the delivery log", () => {
  const allowed: [string, Record<string, unknown>][] = [
    ['superuser ADMIN', as('ADMIN', { is_superuser: true })],
    ['ADMIN who is not a superuser', as('ADMIN')],
  ];
  const refused: [string, Record<string, unknown>][] = [
    ['superuser VIEWER', as('VIEWER', { is_superuser: true })],
    ['DEVELOPER', as('DEVELOPER')],
    ['ANALYST', as('ANALYST')],
    ['VIEWER', as('VIEWER')],
  ];

  it.each(allowed)('%s sees the table, with the witness, and the log is read', async (_name, user) => {
    signIn(user);
    await renderPage();
    const table = await screen.findByTestId('notification-settings-all');
    expect(table).toBeInTheDocument();
    expect(screen.queryByTestId('notification-settings-refused')).toBeNull();
    expect(screen.queryByTestId('log-refused')).toBeNull();
    expect(callsTo(ALL)).toHaveLength(1);
    expect(callsTo(LOG)).toHaveLength(1);
  });

  it.each(refused)('%s loads the page, is told, and sends nothing to either route', async (_name, user) => {
    signIn(user);
    await renderPage();
    expect(screen.getByTestId('notification-settings-refused')).toHaveTextContent(EVERYONE_REFUSED);
    expect(screen.getByTestId('log-refused')).toHaveTextContent(DELIVERY_LOG_REFUSED);
    expect(screen.queryByTestId('notification-settings-all')).toBeNull();
    expect(callsTo(ALL)).toEqual([]);
    expect(callsTo(LOG)).toEqual([]);
    // The page itself still loaded, and the user's own preferences were read.
    expect(callsTo(MINE)).toHaveLength(1);
  });

  it('the page is a superuser\'s: an ADMIN who is not a superuser is stopped at the guard, with no request', async () => {
    signIn(as('ADMIN'));
    install();
    render(<NotificationsPage />);
    expect(await screen.findByTestId('require-auth-forbidden')).toBeInTheDocument();
    await act(async () => {});
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it('a superuser VIEWER passes the guard, so the refusal sentences are what that person reads', async () => {
    signIn(as('VIEWER', { is_superuser: true }));
    install();
    render(<NotificationsPage />);
    expect(await screen.findByTestId('notification-settings-refused')).toBeInTheDocument();
    expect(screen.queryByTestId('require-auth-forbidden')).toBeNull();
  });
});

describe("the table", () => {
  it('lists each user with their events, channel and override, as words', async () => {
    await renderPage();
    const a = await screen.findByTestId('notification-settings-row-user-a');
    const cells = Array.from(a.querySelectorAll('th, td')).map((c) => c.textContent);
    expect(cells).toEqual(['user-a', 'On', 'Off', 'On', 'Off', '#alerts', 'None']);
    const b = screen.getByTestId('notification-settings-row-user-b');
    expect(Array.from(b.querySelectorAll('th, td')).map((c) => c.textContent)).toEqual([
      'user-b', 'On', 'Off', 'On', 'On', 'None', 'ops@example.com',
    ]);
  });

  it('says so when nobody has saved settings', async () => {
    await renderPage(() => []);
    expect(await screen.findByTestId('notification-settings-empty')).toHaveTextContent(EVERYONE_EMPTY);
    expect(screen.queryByTestId('notification-settings-all')).toBeNull();
  });

  it.each([
    [403, EVERYONE_REFUSED],
    [500, EVERYONE_FAILED],
  ])('a %s shows fixed copy, never the server detail', async (status, copy) => {
    await renderPage(() => {
      throw apiError(status, PLANTED);
    });
    const alert = await screen.findByTestId('notification-settings-error');
    expect(alert).toHaveTextContent(copy);
    expect(alert).toHaveAttribute('role', 'alert');
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('a status 0 shows the dashboard\'s own sentence', async () => {
    const err = apiError(0, 'fetch failed');
    await renderPage(() => {
      throw err;
    });
    expect(await screen.findByTestId('notification-settings-error')).toHaveTextContent(err.message);
  });

  it('is a labelled table with row and column headers, and axe finds nothing', async () => {
    await renderPage();
    const table = await screen.findByTestId('notification-settings-all');
    expect(screen.getByRole('table', { name: "Every user's notification settings" })).toBeInTheDocument();
    expect(table.querySelectorAll('thead th[scope="col"]')).toHaveLength(7);
    expect(table.querySelectorAll('tbody th[scope="row"]')).toHaveLength(2);
    const result = await axe.run(screen.getByTestId('notifications-admin-page'), {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('shows the loading line until the answer arrives, then the table', async () => {
    let release: (rows: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    install();
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: MINE, handler: () => ROWS[0] },
        { path: ALL, handler: () => pending },
        { path: LOG, handler: () => ({ items: [], total: 0, page: 1, limit: 20 }) },
      ]) as unknown as typeof apiFetch,
    );
    render(<NotificationsAdminPage />);
    expect(await screen.findByTestId('notification-settings-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('notification-settings-all')).toBeNull();
    await act(async () => {
      release(ROWS);
    });
    await waitFor(() => expect(screen.getByTestId('notification-settings-all')).toBeInTheDocument());
    expect(screen.queryByTestId('notification-settings-loading')).toBeNull();
  });
});
