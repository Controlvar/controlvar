import React from 'react';
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react';
import { AuthProvider, useAuth, useOptionalAuth } from '@/contexts/AuthContext';
import { TOKEN_STORAGE_KEY, UserMe, navigation } from '@/services/api';

const mockFetch = jest.fn();
global.fetch = mockFetch;

const mockUser: UserMe = {
  id: 'u-1',
  email: 'admin@demo.com',
  username: 'admin',
  full_name: 'Demo Admin',
  role: 'ADMIN',
  is_superuser: true,
  is_active: true,
  auth_provider: 'local',
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

function Consumer() {
  const { user, status, isAuthenticated, login, logout, hasRole } = useAuth();
  const [error, setError] = React.useState<string | null>(null);
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="authed">{String(isAuthenticated)}</span>
      <span data-testid="email">{user?.email ?? 'none'}</span>
      <span data-testid="is-admin">{String(hasRole('ADMIN'))}</span>
      <span data-testid="error">{error ?? ''}</span>
      <button
        onClick={() =>
          login('admin@demo.com', 'Demo1234!').catch((e: Error) => setError(e.message))
        }
      >
        login
      </button>
      <button onClick={() => void logout()}>logout</button>
    </div>
  );
}

let assignSpy: jest.SpyInstance;

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  delete process.env.NEXT_PUBLIC_API_URL;
  assignSpy = jest.spyOn(navigation, 'assign').mockImplementation(() => {});
});

afterEach(() => {
  assignSpy.mockRestore();
});

describe('AuthProvider', () => {
  it('throws when useAuth is used outside AuthProvider', () => {
    const spy = jest.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<Consumer />)).toThrow('useAuth must be used within AuthProvider');
    spy.mockRestore();
  });

  it('useOptionalAuth returns null outside a provider', () => {
    function Probe() {
      const auth = useOptionalAuth();
      return <span data-testid="probe">{auth === null ? 'null' : 'ctx'}</span>;
    }
    render(<Probe />);
    expect(screen.getByTestId('probe')).toHaveTextContent('null');
  });

  it('is anonymous without a token and never calls /auth/me', async () => {
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(screen.getByTestId('authed')).toHaveTextContent('false');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('starts loading, then calls /auth/me with the stored token', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-1');
    mockFetch.mockResolvedValueOnce(jsonResponse(200, mockUser));
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    expect(screen.getByTestId('status')).toHaveTextContent('loading');
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    expect(screen.getByTestId('email')).toHaveTextContent('admin@demo.com');
    expect(screen.getByTestId('is-admin')).toHaveTextContent('true');
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe('/api/v1/auth/me');
    expect(init.headers.Authorization).toBe('Bearer tok-1');
  });

  it('becomes anonymous and drops the token when /auth/me returns 401 (no redirect)', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'stale');
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Could not validate credentials' }));
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it('keeps the token but reports anonymous when the API is unreachable', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-keep');
    mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('tok-keep');
  });

  it('login posts credentials without a bearer, stores only the token and sets the user', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        access_token: 'jwt-abc',
        token_type: 'bearer',
        expires_in: 43200,
        user: mockUser,
      }),
    );
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));

    await act(async () => {
      fireEvent.click(screen.getByText('login'));
    });

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe('/api/v1/auth/login');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ email: 'admin@demo.com', password: 'Demo1234!' });
    expect(init.headers.Authorization).toBeUndefined();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt-abc');
    expect(localStorage.getItem('admin_user')).toBeNull();
    expect(localStorage.length).toBe(1);
    expect(screen.getByTestId('email')).toHaveTextContent('admin@demo.com');
  });

  it('login rejects with the API detail on 401 and stays anonymous', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Invalid email or password' }));
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    await act(async () => {
      fireEvent.click(screen.getByText('login'));
    });
    await waitFor(() =>
      expect(screen.getByTestId('error')).toHaveTextContent('Invalid email or password'),
    );
    expect(screen.getByTestId('status')).toHaveTextContent('anonymous');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it('logout calls /auth/logout, clears the token and goes anonymous', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-1');
    mockFetch
      .mockResolvedValueOnce(jsonResponse(200, mockUser))
      .mockResolvedValueOnce(jsonResponse(204, undefined));
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    await act(async () => {
      fireEvent.click(screen.getByText('logout'));
    });

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    const [url, init] = mockFetch.mock.calls[1];
    expect(url).toBe('/api/v1/auth/logout');
    expect(init.method).toBe('POST');
  });

  it('logout still clears the session when /auth/logout fails', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-1');
    mockFetch
      .mockResolvedValueOnce(jsonResponse(200, mockUser))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    await act(async () => {
      fireEvent.click(screen.getByText('logout'));
    });
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  describe('logout signs out before the server answers (#1040)', () => {
    interface Deferred<T> {
      promise: Promise<T>;
      resolve: (value: T) => void;
      reject: (reason: unknown) => void;
    }

    function deferred<T>(): Deferred<T> {
      let resolve!: (value: T) => void;
      let reject!: (reason: unknown) => void;
      const promise = new Promise<T>((res, rej) => {
        resolve = res;
        reject = rej;
      });
      return { promise, resolve, reject };
    }

    async function renderSignedIn() {
      localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-1');
      mockFetch.mockResolvedValueOnce(jsonResponse(200, mockUser));
      render(
        <AuthProvider>
          <Consumer />
        </AuthProvider>,
      );
      await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    }

    it('is signed out at once while the server never answers (#1040)', async () => {
      await renderSignedIn();
      mockFetch.mockImplementationOnce(() => new Promise(() => {}));

      await act(async () => {
        fireEvent.click(screen.getByText('logout'));
      });

      expect(screen.getByTestId('status')).toHaveTextContent('anonymous');
      expect(screen.getByTestId('email')).toHaveTextContent('none');
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
      // The server is still told, with the token being discarded.
      const [url, init] = mockFetch.mock.calls[1];
      expect(url).toBe('/api/v1/auth/logout');
      expect(init.method).toBe('POST');
      expect(init.headers.Authorization).toBe('Bearer tok-1');
    });

    it.each([
      ['is unreachable', (server: Deferred<Response>) => server.reject(new TypeError('Failed to fetch'))],
      ['answers 500', (server: Deferred<Response>) => server.resolve(jsonResponse(500, { detail: 'boom' }))],
    ])('a server that %s changes nothing: signed out first, and still (#1040)', async (_label, fail) => {
      await renderSignedIn();
      const server = deferred<Response>();
      mockFetch.mockImplementationOnce(() => server.promise);

      await act(async () => {
        fireEvent.click(screen.getByText('logout'));
      });
      // Signed out before the call settles...
      expect(screen.getByTestId('status')).toHaveTextContent('anonymous');

      await act(async () => {
        fail(server);
      });
      // ...and its failure neither restores the session nor redirects.
      expect(screen.getByTestId('status')).toHaveTextContent('anonymous');
      expect(screen.getByTestId('error')).toHaveTextContent('');
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
      expect(assignSpy).not.toHaveBeenCalled();
    });

    it("a late 401 to the logout cannot end the next person's session (#1040)", async () => {
      await renderSignedIn();
      const server = deferred<Response>();
      mockFetch.mockImplementationOnce(() => server.promise);
      await act(async () => {
        fireEvent.click(screen.getByText('logout'));
      });

      mockFetch.mockResolvedValueOnce(
        jsonResponse(200, { access_token: 'jwt-next', token_type: 'bearer', expires_in: 43200, user: mockUser }),
      );
      await act(async () => {
        fireEvent.click(screen.getByText('login'));
      });
      await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

      await act(async () => {
        server.resolve(jsonResponse(401, { detail: 'Could not validate credentials' }));
      });
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt-next');
      expect(screen.getByTestId('status')).toHaveTextContent('authenticated');
      expect(assignSpy).not.toHaveBeenCalled();
    });
  });

  it('reacts to the token being removed in another tab', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-1');
    mockFetch.mockResolvedValueOnce(jsonResponse(200, mockUser));
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    localStorage.removeItem(TOKEN_STORAGE_KEY);
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', { key: TOKEN_STORAGE_KEY, newValue: null }));
    });
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
  });
});
