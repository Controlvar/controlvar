import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import LoginPage, { loginErrorMessage } from '@/pages/login';
import { AuthProvider } from '@/contexts/AuthContext';
import { ApiError, TOKEN_STORAGE_KEY, UserMe } from '@/services/api';

const mockReplace = jest.fn().mockResolvedValue(true);
let mockQuery: Record<string, string | string[]> = {};

jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: jest.fn(),
    pathname: '/login',
    asPath: '/login',
    query: mockQuery,
    isReady: true,
  }),
}));

jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});

const mockFetch = jest.fn();
global.fetch = mockFetch;

const admin: UserMe = {
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

function renderLogin() {
  return render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
}

async function fillAndSubmit(email = 'admin@demo.com', password = 'Demo1234!') {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: email } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: password } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));
  });
}

beforeEach(() => {
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  mockQuery = {};
  delete process.env.NEXT_PUBLIC_API_URL;
});

describe('LoginPage', () => {
  it('renders the wordmark, email/password fields and a submit button', async () => {
    renderLogin();
    expect(screen.getByText('Controlvar')).toBeInTheDocument();

    const email = screen.getByLabelText('Email');
    expect(email).toHaveAttribute('type', 'email');
    expect(email).toHaveAttribute('name', 'email');

    const password = screen.getByLabelText('Password');
    expect(password).toHaveAttribute('type', 'password');
    expect(password).toHaveAttribute('name', 'password');

    const submit = screen.getByRole('button', { name: 'Sign in' });
    expect(submit).toHaveAttribute('type', 'submit');

    // No public sign-up.
    expect(screen.queryByText(/sign up/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /create account/i })).not.toBeInTheDocument();
    await waitFor(() => expect(mockReplace).not.toHaveBeenCalled());
  });

  it('matches the Playwright page-object selectors', () => {
    const { container } = renderLogin();
    expect(container.querySelector('input[type="email"]')).not.toBeNull();
    expect(container.querySelector('input[name="email"]')).not.toBeNull();
    expect(container.querySelector('input[type="password"]')).not.toBeNull();
    expect(container.querySelector('button[type="submit"]')).not.toBeNull();
  });

  it('toggles password visibility', () => {
    renderLogin();
    const password = screen.getByLabelText('Password');
    const toggle = screen.getByRole('button', { name: 'Show password' });
    fireEvent.click(toggle);
    expect(password).toHaveAttribute('type', 'text');
    expect(screen.getByRole('button', { name: 'Hide password' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Hide password' }));
    expect(password).toHaveAttribute('type', 'password');
  });

  it('shows the "forgot password" copy without a reset link', () => {
    renderLogin();
    expect(screen.getByText('Forgot password?')).toBeInTheDocument();
    // The dashboard has no password field anywhere; an administrator resets
    // it through the API. The copy must not send anyone to a screen that
    // cannot do it (#396).
    expect(screen.getByTestId('forgot-password-help')).toHaveTextContent(
      /^Ask an administrator to reset it\.$/,
    );
  });

  it('validates empty fields client-side', async () => {
    renderLogin();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Enter your email and password.');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('logs in, stores the token and redirects to /experiments by default', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, { access_token: 'jwt', token_type: 'bearer', expires_in: 100, user: admin }),
    );
    renderLogin();
    await fillAndSubmit();

    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/experiments'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt');
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe('/api/v1/auth/login');
    expect(JSON.parse(init.body)).toEqual({ email: 'admin@demo.com', password: 'Demo1234!' });
    expect(init.headers.Authorization).toBeUndefined();
  });

  it('honours a safe ?next= target', async () => {
    mockQuery = { next: '/feature-flags/42?tab=safety' };
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, { access_token: 'jwt', token_type: 'bearer', expires_in: 100, user: admin }),
    );
    renderLogin();
    await fillAndSubmit();
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/feature-flags/42?tab=safety'));
  });

  it('ignores an unsafe ?next= target', async () => {
    mockQuery = { next: '//evil.example' };
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, { access_token: 'jwt', token_type: 'bearer', expires_in: 100, user: admin }),
    );
    renderLogin();
    await fillAndSubmit();
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/experiments'));
  });

  it('explains a wrong-Host 400 in the alert box, naming this address and the setting', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(400, { detail: 'Invalid host header' }));
    renderLogin();
    await fillAndSubmit();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      `This Controlvar server isn't configured for the address you used (${window.location.host}). ` +
        `An administrator needs to set PUBLIC_BASE_URL to ${window.location.origin} ` +
        `(or add ${window.location.host} to ALLOWED_HOSTS, if that is set).`,
    );
    expect(alert).not.toHaveTextContent(/^Invalid host header$/);
    // Plain text: a link to this host is the thing that does not work.
    expect(alert.querySelector('a')).toBeNull();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it('shows the invalid-credentials message in a role="alert" box and refocuses email', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Invalid email or password' }));
    renderLogin();
    await fillAndSubmit('nobody@demo.com', 'wrong');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Email or password is incorrect.');
    expect(alert.className).toContain('login-error');
    expect(screen.getByLabelText('Email')).toHaveFocus();
    expect(mockReplace).not.toHaveBeenCalled();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.getByRole('button', { name: 'Sign in' })).not.toBeDisabled();
  });

  it('shows the lockout detail on 423', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(423, { detail: 'Too many attempts. Try again in 15 min.' }),
    );
    renderLogin();
    await fillAndSubmit();
    expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts. Try again in 15 min.');
  });

  it('explains when the API is unreachable', async () => {
    process.env.NEXT_PUBLIC_API_URL = 'http://localhost:8000';
    mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    renderLogin();
    await fillAndSubmit();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "Can't reach the API at http://localhost:8000. Check that the backend is running and that " +
        "this dashboard's origin is listed in CORS_ORIGINS.",
    );
  });

  it('disables the button and marks inputs read-only while submitting', async () => {
    let resolveLogin: (value: Response) => void = () => {};
    mockFetch.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          resolveLogin = resolve;
        }),
    );
    renderLogin();
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'admin@demo.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Demo1234!' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    });

    const button = screen.getByRole('button', { name: /signing in/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByLabelText('Email')).toHaveAttribute('readonly');
    expect(screen.getByLabelText('Password')).toHaveAttribute('readonly');

    await act(async () => {
      resolveLogin(
        jsonResponse(200, { access_token: 'jwt', token_type: 'bearer', expires_in: 100, user: admin }),
      );
    });
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/experiments'));
  });

  it('says why a stored session ended when /auth/me answered 500 (#72)', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      headers: {
        get: (name: string) =>
          ({ 'content-type': 'text/plain; charset=utf-8', 'x-request-id': 'req-me-500' })[
            name.toLowerCase()
          ] ?? null,
      },
      text: () => Promise.resolve('Internal Server Error'),
    } as unknown as Response);
    renderLogin();
    expect(await screen.findByTestId('login-session-error')).toHaveTextContent(
      'Signed out because the server returned an error (HTTP 500). Request ID: req-me-500.',
    );
    expect(screen.getByRole('alert')).not.toHaveTextContent("Can't reach");
    // The token is kept: the next reload may find the server healthy again.
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('tok');
  });

  it('shows no session message for a visitor who was never signed in', async () => {
    renderLogin();
    await waitFor(() => expect(mockFetch).not.toHaveBeenCalled());
    expect(screen.queryByTestId('login-session-error')).not.toBeInTheDocument();
  });

  it('a failed sign-in replaces the session message rather than stacking under it', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    renderLogin();
    expect(await screen.findByTestId('login-session-error')).toHaveTextContent("Can't reach the API");
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Incorrect email or password' }));
    await fillAndSubmit();
    expect(await screen.findByTestId('login-error')).toHaveTextContent('Email or password is incorrect.');
    expect(screen.queryByTestId('login-session-error')).not.toBeInTheDocument();
    expect(screen.getAllByRole('alert')).toHaveLength(1);
  });

  it('redirects straight through when already authenticated', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockResolvedValueOnce(jsonResponse(200, admin));
    mockQuery = { next: '/admin' };
    renderLogin();
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/admin'));
  });
});

describe('loginErrorMessage', () => {
  it('maps statuses to copy', () => {
    expect(loginErrorMessage(new ApiError({ status: 401, detail: 'x' }))).toBe(
      'Email or password is incorrect.',
    );
    expect(loginErrorMessage(new ApiError({ status: 423 }))).toBe(
      'Too many failed attempts. Try again in a few minutes.',
    );
    expect(loginErrorMessage(new ApiError({ status: 429 }))).toContain('Too many attempts');
    expect(loginErrorMessage(new ApiError({ status: 500, detail: 'boom' }))).toBe('boom');
    expect(loginErrorMessage(new ApiError({ status: 400, detail: 'Invalid host header' }))).toBe(
      `This Controlvar server isn't configured for the address you used (${window.location.host}). ` +
        `An administrator needs to set PUBLIC_BASE_URL to ${window.location.origin} ` +
        `(or add ${window.location.host} to ALLOWED_HOSTS, if that is set).`,
    );
    // Only that exact 400: any other 400 keeps its own detail.
    expect(loginErrorMessage(new ApiError({ status: 400, detail: 'Bad email' }))).toBe('Bad email');
    expect(loginErrorMessage(new Error('plain'))).toBe('plain');
    expect(loginErrorMessage('???')).toBe('Something went wrong. Please try again.');
  });
});
