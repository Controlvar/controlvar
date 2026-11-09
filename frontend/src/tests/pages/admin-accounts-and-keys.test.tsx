/**
 * Admin → Users and Admin → API Keys, as the flag guide's prerequisites name
 * them: the real pages, guard, dialogs and apiFetch, with only `fetch` mocked.
 *
 * What is pinned:
 *  - the admin area's sidebar names both pages;
 *  - a superuser creates an account under Admin → Users (Invite User sends
 *    `POST /api/v1/users/`) and a key under Admin → API Keys (`POST
 *    /api/v1/api-keys`);
 *  - an ADMIN or DEVELOPER who is not a superuser is turned away from both.
 */
import React from 'react';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
// The API key and invite dialogs make `#__next` inert and portal beside it, so
// the pages are mounted the way Next mounts them (#369).
import { APP_ROOT_ID, renderInAppRoot as render } from '@/tests/helpers/renderInAppRoot';
import { watchLiveRegions } from '@/tests/helpers/watchLiveRegions';
import UsersPage from '@/pages/admin/users';
import ApiKeysPage from '@/pages/admin/api-keys';
import { AdminSidebar } from '@/components/admin/AdminSidebar';
import { AuthProvider } from '@/contexts/AuthContext';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { CORE_PROFILE } from '@/services/modules';
import { TOKEN_STORAGE_KEY } from '@/services/api';

jest.mock('next/router', () => ({
  useRouter: () => ({
    push: jest.fn(),
    replace: jest.fn(),
    pathname: '/admin',
    asPath: '/admin',
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

jest.mock('@/components/admin/AdminLayout', () => ({
  AdminLayout: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

interface Me {
  role: string;
  is_superuser: boolean;
}

let me: Me;
let posted: Array<{ path: string; body: Record<string, unknown> }>;

function respond(status: number, body: unknown): Response {
  const payload = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: () => Promise.resolve(JSON.parse(payload)),
    text: () => Promise.resolve(payload),
  } as unknown as Response;
}

function route(url: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? 'GET').toString().toUpperCase();
  const { pathname } = new URL(url, 'http://localhost');
  if (pathname === '/api/v1/auth/me') {
    return Promise.resolve(
      respond(200, {
        id: 'me-1',
        username: 'root',
        email: 'root@example.com',
        full_name: null,
        is_active: true,
        auth_provider: 'local',
        ...me,
      }),
    );
  }
  if (method === 'POST') {
    const body = JSON.parse(String(init.body ?? '{}'));
    posted.push({ path: pathname, body });
    if (pathname === '/api/v1/users/') {
      return Promise.resolve(respond(201, { id: 'u-9', ...body, created_at: '2026-01-01T00:00:00Z' }));
    }
    if (pathname === '/api/v1/api-keys') {
      return Promise.resolve(
        respond(201, { id: 'k-1', name: body.name, key: 'eptk_' + '1'.repeat(32), scopes: [], created_at: '2026-01-01T00:00:00Z' }),
      );
    }
  }
  if (pathname === '/api/v1/admin/users') {
    return Promise.resolve(respond(200, { items: [], total: 0, skip: 0, limit: 50 }));
  }
  if (pathname === '/api/v1/api-keys') return Promise.resolve(respond(200, []));
  return Promise.resolve(respond(404, { detail: `no route ${method} ${pathname}` }));
}

const mockFetch = jest.fn();

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  me = { role: 'DEVELOPER', is_superuser: true };
  posted = [];
  mockFetch.mockReset();
  mockFetch.mockImplementation(route);
  global.fetch = mockFetch as unknown as typeof fetch;
});

function renderPage(Page: React.ComponentType) {
  return render(
    <AuthProvider>
      <Page />
    </AuthProvider>,
  );
}

// The key the mocked POST returns (a fake value).
const PLAIN_KEY = 'eptk_' + '1'.repeat(32);

async function createKeyThroughDialog() {
  fireEvent.click(await screen.findByTestId('create-api-key-button'));
  fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'flag-guide' } });
  fireEvent.click(screen.getByTestId('create-api-key-submit'));
  await screen.findByTestId('new-key-banner');
}

describe('the API keys dialog on /admin/api-keys', () => {
  afterEach(() => {
    jest.useRealTimers();
  });

  it('never puts the key in the live region, whether created, copied or dismissed', async () => {
    renderPage(ApiKeysPage);
    await screen.findByTestId('new-key-live-region');
    const region = () => screen.getByTestId('new-key-live-region');
    const noKey = () => {
      expect(region().textContent ?? '').not.toContain(PLAIN_KEY);
      expect(region().textContent ?? '').not.toContain(PLAIN_KEY.slice(0, 8));
    };
    expect(region()).toHaveAttribute('aria-live', 'polite');
    noKey();

    await createKeyThroughDialog();
    // The key is on screen, and the region says only that one was created.
    expect(screen.getByTestId('new-key-value')).toHaveTextContent(PLAIN_KEY);
    // Changed for #369 (was an immediate check): "Key created" is written one
    // commit after the dialog closes, once the page behind it is no longer
    // inert, so the check waits for it.
    await waitFor(() => expect(region()).toHaveTextContent('Key created'));
    noKey();

    fireEvent.click(screen.getByTestId('copy-new-key'));
    await waitFor(() => expect(region()).toHaveTextContent('Key copied'));
    noKey();

    fireEvent.click(screen.getByTestId('dismiss-key-banner'));
    expect(region().textContent).toBe('');
    noKey();
  });

  it('does not announce "Key created" again when the copied state wears off', async () => {
    renderPage(ApiKeysPage);
    await createKeyThroughDialog();
    jest.useFakeTimers();
    fireEvent.click(screen.getByTestId('copy-new-key'));
    await act(async () => {});
    expect(screen.getByTestId('new-key-live-region')).toHaveTextContent('Key copied');

    act(() => {
      jest.advanceTimersByTime(2500);
    });
    expect(screen.getByTestId('copy-new-key')).toHaveTextContent('Copy');
    expect(screen.getByTestId('new-key-live-region').textContent).toBe('');
  });

  it('opens a fresh dialog the second time, with focus inside it and Escape working', async () => {
    renderPage(ApiKeysPage);
    await createKeyThroughDialog();
    fireEvent.click(screen.getByTestId('dismiss-key-banner'));

    const opener = screen.getByTestId('create-api-key-button');
    opener.focus();
    fireEvent.click(opener);

    const dialog = await screen.findByRole('dialog', { name: 'Create API Key' });
    expect(screen.getByTestId('api-key-name-input')).toHaveValue('');
    expect(screen.queryByTestId('masked-key-value')).not.toBeInTheDocument();
    expect(screen.queryByTestId('modal-done-button')).not.toBeInTheDocument();
    expect(dialog.contains(document.activeElement)).toBe(true);
    // Nothing of the earlier key is anywhere in the page.
    expect(document.body.textContent ?? '').not.toContain(PLAIN_KEY.slice(0, 8));

    fireEvent.keyDown(document.activeElement as Element, { key: 'Escape' });
    expect(screen.queryByTestId('create-api-key-modal')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('"Key created" is announced after the page behind the dialog stops being inert, never the key (#369)', async () => {
    renderPage(ApiKeysPage);
    const opener = await screen.findByTestId('create-api-key-button');
    opener.focus();
    fireEvent.click(opener);
    await screen.findByRole('dialog', { name: 'Create API Key' });
    expect(document.getElementById(APP_ROOT_ID)).toHaveAttribute('inert');

    const watch = watchLiveRegions();
    fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'flag-guide' } });
    fireEvent.click(screen.getByTestId('create-api-key-submit'));
    await screen.findByTestId('new-key-banner');
    await waitFor(() => expect(screen.getByTestId('new-key-live-region')).toHaveTextContent('Key created'));
    watch.stop();

    expect(document.getElementById(APP_ROOT_ID)).not.toHaveAttribute('inert');
    expect(screen.queryByTestId('create-api-key-modal')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(watch.changes).toContainEqual({ text: 'Key created', underInert: false });
    expect(watch.changes.filter((c) => c.underInert)).toEqual([]);
    for (const change of watch.changes) {
      expect(change.text).not.toContain(PLAIN_KEY.slice(0, 8));
    }
  });
});

describe('the admin area', () => {
  it('names Users and API Keys in its sidebar', () => {
    render(
      <ModulesProvider initial={CORE_PROFILE}>
        <AdminSidebar currentPath="/admin" />
      </ModulesProvider>,
    );
    expect(screen.getByRole('link', { name: /users/i })).toHaveAttribute('href', '/admin/users');
    expect(screen.getByRole('link', { name: /api keys/i })).toHaveAttribute('href', '/admin/api-keys');
  });

  it('a superuser creates an account under Admin → Users', async () => {
    renderPage(UsersPage);
    fireEvent.click(await screen.findByTestId('invite-user-button'));
    fireEvent.change(screen.getByTestId('invite-email-input'), { target: { value: 'new@example.com' } });
    fireEvent.change(screen.getByTestId('invite-full-name-input'), { target: { value: 'New Person' } });
    fireEvent.click(screen.getByTestId('invite-submit-button'));
    await screen.findByTestId('invite-created');
    expect(posted.map((p) => p.path)).toEqual(['/api/v1/users/']);
    expect(posted[0].body).toMatchObject({ email: 'new@example.com', is_active: true });
  });

  it('a superuser creates an API key under Admin → API Keys', async () => {
    renderPage(ApiKeysPage);
    fireEvent.click(await screen.findByTestId('create-api-key-button'));
    fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'flag-guide' } });
    fireEvent.click(screen.getByTestId('create-api-key-submit'));
    await waitFor(() => expect(posted.map((p) => p.path)).toEqual(['/api/v1/api-keys']));
    expect(posted[0].body).toMatchObject({ name: 'flag-guide' });
  });

  it.each([
    ['ADMIN', UsersPage, 'user-management-page'],
    ['DEVELOPER', UsersPage, 'user-management-page'],
    ['ADMIN', ApiKeysPage, 'api-keys-page'],
    ['DEVELOPER', ApiKeysPage, 'api-keys-page'],
  ])('turns away an %s who is not a superuser (%#)', async (role, Page, testId) => {
    me = { role, is_superuser: false };
    renderPage(Page as React.ComponentType);
    expect(await screen.findByText(/a superuser account/i)).toBeInTheDocument();
    expect(screen.queryByTestId(testId as string)).not.toBeInTheDocument();
    expect(posted).toEqual([]);
  });
});
