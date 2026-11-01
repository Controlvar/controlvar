import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { RequireAuth } from '@/components/RequireAuth';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';

const mockReplace = jest.fn();
let mockAsPath = '/experiments/abc?tab=results';

jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: jest.fn(),
    pathname: '/experiments/[id]',
    asPath: mockAsPath,
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

const mockFetch = jest.fn();
global.fetch = mockFetch;

const viewer: UserMe = {
  id: 'u-2',
  email: 'viewer@demo.com',
  username: 'viewer',
  full_name: 'Demo Viewer',
  role: 'VIEWER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

function meResponse(user: UserMe): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(user),
    text: () => Promise.resolve(JSON.stringify(user)),
  } as unknown as Response;
}

function renderGuarded(roles?: UserMe['role'][]) {
  return render(
    <AuthProvider>
      <RequireAuth roles={roles}>
        <div data-testid="protected">secret</div>
      </RequireAuth>
    </AuthProvider>,
  );
}

beforeEach(() => {
  mockFetch.mockReset();
  mockReplace.mockReset();
  localStorage.clear();
  mockAsPath = '/experiments/abc?tab=results';
});

describe('RequireAuth', () => {
  it('shows the loading state while the session resolves and hides children', () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockImplementation(() => new Promise(() => {}));
    renderGuarded();
    expect(screen.getByTestId('require-auth-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('protected')).not.toBeInTheDocument();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it('redirects anonymous users to /login?next=<current path>', async () => {
    renderGuarded();
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith('/login?next=%2Fexperiments%2Fabc%3Ftab%3Dresults'),
    );
    expect(screen.queryByTestId('protected')).not.toBeInTheDocument();
  });

  it('redirects to plain /login when the current path is the root', async () => {
    mockAsPath = '/';
    renderGuarded();
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/login'));
  });

  it('renders children for an authenticated user with no role restriction', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockResolvedValueOnce(meResponse(viewer));
    renderGuarded();
    await waitFor(() => expect(screen.getByTestId('protected')).toBeInTheDocument());
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it('renders children when the user holds one of the allowed roles', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockResolvedValueOnce(meResponse({ ...viewer, role: 'ADMIN' }));
    renderGuarded(['ADMIN', 'DEVELOPER']);
    await waitFor(() => expect(screen.getByTestId('protected')).toBeInTheDocument());
  });

  describe('after a logout in this tab (#1006)', () => {
    function LogoutButtons() {
      const { logout, login } = useAuth();
      return (
        <>
          <button onClick={() => void logout()}>logout</button>
          <button onClick={() => void logout({ next: '/workspaces/invites/tok' })}>logout-to-invite</button>
          <button onClick={() => void login('viewer@demo.com', 'pw')}>login</button>
        </>
      );
    }

    function okJson(body: unknown, status = 200): Response {
      return {
        ok: true,
        status,
        headers: { get: () => 'application/json' },
        json: () => Promise.resolve(body),
        text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
      } as unknown as Response;
    }

    function renderWithLogout() {
      localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
      mockFetch.mockImplementation((url: string) => {
        if (url === '/api/v1/auth/login') {
          return Promise.resolve(
            okJson({ access_token: 'tok-2', token_type: 'bearer', expires_in: 60, user: viewer }),
          );
        }
        if (url === '/api/v1/auth/logout') return Promise.resolve(okJson(undefined, 204));
        return Promise.resolve(meResponse(viewer));
      });
      return render(
        <AuthProvider>
          <LogoutButtons />
          <RequireAuth>
            <div data-testid="protected">secret</div>
          </RequireAuth>
        </AuthProvider>,
      );
    }

    const withNext = () => mockReplace.mock.calls.map(([url]) => url).filter((url) => url.includes('next='));

    it('goes to plain /login, never /login?next=<the signed-out page> (#1006)', async () => {
      renderWithLogout();
      await waitFor(() => expect(screen.getByTestId('protected')).toBeInTheDocument());

      await act(async () => {
        fireEvent.click(screen.getByText('logout'));
      });

      await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/login'));
      expect(withNext()).toEqual([]);
    });

    it('goes to the next the logout named, the same path its caller navigates to (#1006)', async () => {
      renderWithLogout();
      await waitFor(() => expect(screen.getByTestId('protected')).toBeInTheDocument());

      await act(async () => {
        fireEvent.click(screen.getByText('logout-to-invite'));
      });

      await waitFor(() => expect(mockReplace).toHaveBeenCalled());
      expect(mockReplace.mock.calls.map(([url]) => url)).toEqual([
        '/login?next=%2Fworkspaces%2Finvites%2Ftok',
      ]);
    });

    it('records the page as next again once someone has signed in (positive control)', async () => {
      renderWithLogout();
      await waitFor(() => expect(screen.getByTestId('protected')).toBeInTheDocument());
      await act(async () => {
        fireEvent.click(screen.getByText('logout'));
      });
      await act(async () => {
        fireEvent.click(screen.getByText('login'));
      });
      await waitFor(() => expect(screen.getByTestId('protected')).toBeInTheDocument());
      mockReplace.mockReset();

      // The new session ends without a logout here (another tab drops the token).
      localStorage.removeItem(TOKEN_STORAGE_KEY);
      await act(async () => {
        window.dispatchEvent(new StorageEvent('storage', { key: TOKEN_STORAGE_KEY, newValue: null }));
      });

      await waitFor(() =>
        expect(mockReplace).toHaveBeenCalledWith('/login?next=%2Fexperiments%2Fabc%3Ftab%3Dresults'),
      );
    });
  });

  it('renders the inline 403 view when the role is not allowed', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockResolvedValueOnce(meResponse(viewer));
    renderGuarded(['ADMIN']);
    await waitFor(() => expect(screen.getByTestId('require-auth-forbidden')).toBeInTheDocument());
    expect(screen.queryByTestId('protected')).not.toBeInTheDocument();
    expect(screen.getByText(/do not have permission/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /go to home/i })).toHaveAttribute('href', '/experiments');
    expect(mockReplace).not.toHaveBeenCalled();
  });
});
