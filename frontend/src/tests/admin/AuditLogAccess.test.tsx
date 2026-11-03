/**
 * Who opens the Audit Log page, and what each of them is shown (#915).
 *
 * The audit list and export routes answer every signed-in role, narrowed to
 * the caller's own entries for anyone who may not read the audit log (by
 * role, DEVELOPER and VIEWER, unless a custom role or grant gives
 * `audit_log: read`), so the page admits all
 * four roles. A superuser keeps the admin layout and its sidebar; everyone
 * else gets a plain page with no link to a superuser-only page.
 *
 * This renders the page's DEFAULT export (guard included) under the real
 * AuthProvider, and does not mock AdminLayout, AdminSidebar or AdminService:
 * a mocked layout renders no sidebar and no links, which would make the
 * layout checks pass whatever the page did. `fetch` is routed by URL instead,
 * so the test also sees every API path the page requests.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AuditLogPageDefault, { OWN_SCOPE_NOTE } from '@/pages/admin/audit';
import { AuthProvider } from '@/contexts/AuthContext';
import { AdminService } from '@/services/admin';
import { Role, TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import { readsAllAuditLogs } from '@/components/admin/audit/access';

jest.mock('next/router', () => ({
  useRouter: () => ({
    push: jest.fn(),
    replace: jest.fn(),
    pathname: '/admin/audit',
    asPath: '/admin/audit',
    query: {},
    isReady: true,
  }),
}));

jest.mock('next/link', () => {
  const MockLink = ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});

/** Pages under /admin that only a superuser can open (D44). */
const SUPERUSER_ONLY_HREFS = [
  '/admin',
  '/admin/users',
  '/admin/roles',
  '/admin/safety',
  '/admin/scheduler',
  '/admin/api-keys',
  '/admin/notifications',
];

const NON_SUPERUSER_ROLES: Role[] = ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'];

function makeUser(role: Role, isSuperuser = false): UserMe {
  return {
    id: `u-${role.toLowerCase()}`,
    username: role.toLowerCase(),
    email: `${role.toLowerCase()}@example.com`,
    full_name: null,
    role,
    is_superuser: isSuperuser,
    is_active: true,
    auth_provider: 'local',
  };
}

function response(status: number, body: string, headers: Record<string, string>): Response {
  const h = new Headers(headers);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: h,
    json: () => Promise.resolve(JSON.parse(body)),
    text: () => Promise.resolve(body),
  } as unknown as Response;
}

const json = (status: number, body: unknown) =>
  response(status, JSON.stringify(body), { 'content-type': 'application/json' });

/** Every API path requested, without its query string. */
let requested: string[] = [];
/** Every full URL requested. */
let requestedUrls: string[] = [];

function routeFetch(me: UserMe) {
  global.fetch = jest.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost');
    requested.push(url.pathname);
    requestedUrls.push(url.toString());
    if (url.pathname === '/api/v1/auth/me') return Promise.resolve(json(200, me));
    if (url.pathname === '/api/v1/audit-logs/') {
      return Promise.resolve(
        json(200, {
          items: [
            {
              id: 'log-1',
              user_id: me.id,
              user_email: me.email,
              action_type: 'user_login',
              entity_type: 'user',
              entity_id: me.id,
              entity_name: me.email,
              old_value: null,
              new_value: null,
              reason: null,
              timestamp: '2026-10-01T10:30:00Z',
              created_at: '2026-10-01T10:30:00Z',
              action_description: 'signed in',
            },
          ],
          total: 1,
          page: 1,
          limit: 50,
        }),
      );
    }
    if (url.pathname === '/api/v1/audit-logs/export') {
      return Promise.resolve(
        response(200, 'id,user_email\nlog-1,someone@example.com\n', {
          'content-type': 'text/csv',
          'x-total-count': '1',
        }),
      );
    }
    return Promise.resolve(json(404, { detail: 'Not Found' }));
  }) as unknown as typeof fetch;
}

async function renderAs(me: UserMe) {
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  routeFetch(me);
  const view = render(
    <AuthProvider>
      <AuditLogPageDefault />
    </AuthProvider>,
  );
  // Settled: either the page or the guard's refusal, never the spinner.
  await waitFor(() =>
    expect(
      screen.queryByTestId('audit-log-page') ?? screen.queryByTestId('require-auth-forbidden'),
    ).not.toBeNull(),
  );
  return view;
}

const createObjectURL = jest.fn(() => 'blob:audit');
const revokeObjectURL = jest.fn();

beforeEach(() => {
  localStorage.clear();
  requested = [];
  requestedUrls = [];
  Object.assign(URL, { createObjectURL, revokeObjectURL });
  jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('Audit Log page access (#915)', () => {
  // V1
  it.each(NON_SUPERUSER_ROLES)('opens for a %s who is not a superuser', async (role) => {
    await renderAs(makeUser(role));
    expect(screen.getByTestId('audit-log-page')).toBeInTheDocument();
    expect(screen.queryByTestId('require-auth-forbidden')).toBeNull();
  });

  // V2
  it.each(['ADMIN', 'VIEWER'] as Role[])(
    'keeps the admin layout and sidebar for a superuser whose role is %s',
    async (role) => {
      await renderAs(makeUser(role, true));
      expect(screen.getByTestId('audit-log-page')).toBeInTheDocument();
      expect(screen.getByTestId('admin-layout')).toBeInTheDocument();
      expect(screen.getByTestId('admin-sidebar')).toBeInTheDocument();
      expect(screen.queryByTestId('audit-log-plain-layout')).toBeNull();
      expect(screen.queryByTestId('require-auth-forbidden')).toBeNull();
    },
  );

  // V3
  it.each(NON_SUPERUSER_ROLES)(
    'shows a %s who is not a superuser no link to a superuser-only page',
    async (role) => {
      const { container } = await renderAs(makeUser(role));
      expect(screen.getByTestId('audit-log-plain-layout')).toBeInTheDocument();
      expect(screen.queryByTestId('admin-sidebar')).toBeNull();
      expect(screen.queryByTestId('admin-layout')).toBeNull();
      for (const href of SUPERUSER_ONLY_HREFS) {
        expect(container.querySelectorAll(`a[href="${href}"]`)).toHaveLength(0);
      }
    },
  );

  // C3: AppShell owns the main landmark; the plain page has its own h1.
  it('renders no main element and one level-1 heading in the plain layout', async () => {
    const { container } = await renderAs(makeUser('ANALYST'));
    expect(container.querySelectorAll('main')).toHaveLength(0);
    const headings = screen.getAllByRole('heading', { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0]).toHaveTextContent(/^Audit Log$/);
  });

  // V4
  it.each([
    ['DEVELOPER', false, true],
    ['VIEWER', false, true],
    ['ADMIN', false, false],
    ['ANALYST', false, false],
    ['VIEWER', true, false],
    ['DEVELOPER', true, false],
  ] as [Role, boolean, boolean][])(
    'a %s (superuser: %s) is told they see only their own entries: %s',
    async (role, superuser, ownNote) => {
      await renderAs(makeUser(role, superuser));
      const note = screen.queryByTestId('audit-log-scope-own');
      if (ownNote) {
        expect(note).toHaveTextContent(OWN_SCOPE_NOTE);
      } else {
        expect(note).toBeNull();
      }
    },
  );

  it('mirrors can_read_all_audit_logs: superuser, then ADMIN and ANALYST, then a grant of audit_log (read)', () => {
    for (const role of NON_SUPERUSER_ROLES) {
      expect(readsAllAuditLogs({ role, is_superuser: true })).toBe(true);
    }
    expect(readsAllAuditLogs({ role: 'ADMIN', is_superuser: false })).toBe(true);
    expect(readsAllAuditLogs({ role: 'ANALYST', is_superuser: false })).toBe(true);
    expect(readsAllAuditLogs({ role: 'DEVELOPER', is_superuser: false })).toBe(false);
    expect(readsAllAuditLogs({ role: 'VIEWER', is_superuser: false })).toBe(false);
    expect(readsAllAuditLogs({ role: 'VIEWER', is_superuser: false }, { audit_log: ['read'] })).toBe(true);
    expect(readsAllAuditLogs({ role: 'DEVELOPER', is_superuser: false }, { experiment: ['read'] })).toBe(false);
  });

  // V5 and V6
  it.each(NON_SUPERUSER_ROLES)(
    'offers a %s both downloads, sends no user_id, and calls only the list and export routes',
    async (role) => {
      const listSpy = jest.spyOn(AdminService, 'listAuditLogs');
      const exportSpy = jest.spyOn(AdminService, 'exportAuditLogs');
      await renderAs(makeUser(role));
      await screen.findByTestId('audit-log-row-log-1');

      expect(screen.getByTestId('audit-download-csv')).toBeInTheDocument();
      expect(screen.getByTestId('audit-download-json')).toBeInTheDocument();

      fireEvent.click(screen.getByTestId('audit-download-csv'));
      await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
      expect(screen.queryByTestId('audit-download-error')).toBeNull();

      expect(listSpy).toHaveBeenCalled();
      expect(exportSpy).toHaveBeenCalledTimes(1);
      expect(exportSpy.mock.calls[0][0]).toBe('csv');
      expect(exportSpy.mock.calls[0][1]).not.toHaveProperty('user_id');
      for (const call of listSpy.mock.calls) {
        expect(call[0] ?? {}).not.toHaveProperty('user_id');
      }
      for (const url of requestedUrls) {
        expect(new URL(url).searchParams.has('user_id')).toBe(false);
      }

      // Exactly these, so a new call (stats, entity history, /admin/*) fails
      // this, and so does a page that calls nothing.
      expect(new Set(requested)).toEqual(
        new Set(['/api/v1/auth/me', '/api/v1/audit-logs/', '/api/v1/audit-logs/export']),
      );
    },
  );
});
