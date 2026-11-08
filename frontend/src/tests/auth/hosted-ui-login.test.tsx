/**
 * `/login` and sign-out under the Cognito Hosted UI (#708), and the local
 * form whatever `/auth/config` answers. The API is a mock answering by path;
 * the browser's navigations are recorded.
 */
import {
  CONFIG,
  LOGOUT,
  POOL_A,
  POOL_B,
  api,
  assigned,
  authorizeHits,
  callsTo,
  jsonResponse,
  logoutHits,
  mockReplace,
  normalisedMarkup,
  renderLogin,
  resetHarness,
  restoreHarness,
  settle,
  storedPending,
  textResponse,
  timeline,
} from './hostedUiHarness';
import React from 'react';
import { createHash } from 'crypto';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { AuthConfigProvider, __resetAuthConfigCache } from '@/contexts/AuthConfigContext';
import { ModulesProvider, __resetModulesCache } from '@/contexts/ModulesContext';
import { AuthProvider, INACTIVE_ACCOUNT_MESSAGE, useAuth } from '@/contexts/AuthContext';
import LoginPage, { SIGNED_OUT_MESSAGE, SIGN_IN_NOT_SET_UP_MESSAGE } from '@/pages/login';
import { TOKEN_STORAGE_KEY } from '@/services/api';
import { safeAfterSignInPath } from '@/services/hostedUi';
const { modulesTreeAvailable } = require('../../../modules-alias') as { modulesTreeAvailable: () => boolean };

let registered = 0;
function row(name: string, fn: () => unknown) {
  registered += 1;
  it(name, async () => {
    await fn();
  });
}

beforeEach(() => {
  resetHarness();
});

afterEach(() => {
  restoreHarness();
  jest.restoreAllMocks();
});

async function clickSignIn(): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /sign in|try again/i }));
  });
  await settle();
}

function lastAuthorize(): URL {
  const hits = authorizeHits();
  return new URL(hits[hits.length - 1]);
}

describe('PKCE (row 3)', () => {
  row('the authorize URL carries S256 and the challenge of the verifier kept', async () => {
    await renderLogin();
    await clickSignIn();
    const url = lastAuthorize();
    const verifier = String(storedPending()?.verifier);
    expect(verifier.length).toBeGreaterThanOrEqual(43);
    expect(verifier.length).toBeLessThanOrEqual(128);
    expect(verifier).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
    expect(url.searchParams.get('code_challenge')).toBe(createHash('sha256').update(verifier).digest('base64url'));
  });
});

describe('each sign-in is new (row 4)', () => {
  row('two sign-ins differ in state, verifier and nonce', async () => {
    const view = await renderLogin();
    await clickSignIn();
    const a: Record<string, unknown> = { ...(storedPending() ?? {}), nonce: lastAuthorize().searchParams.get('nonce') };
    view.unmount();
    await renderLogin();
    await clickSignIn();
    const b: Record<string, unknown> = { ...(storedPending() ?? {}), nonce: lastAuthorize().searchParams.get('nonce') };
    expect(typeof a.state).toBe('string');
    expect(a.state).not.toEqual(b.state);
    expect(a.verifier).not.toEqual(b.verifier);
    expect(a.nonce).not.toEqual(b.nonce);
    expect(lastAuthorize().searchParams.get('state')).toBe(b.state);
  });
});

describe('regression #708: where a sign-in lands is a path on this origin (row 11)', () => {
  const REFUSED = ['https://evil.example', '//evil.example', '/\\evil.example', 'javascript:alert(1)', '/ok%0d%0a'];
  const KEPT = ['/experiments', '/feature-flags/abc?tab=rules'];

  row('the cases are exactly these', () => {
    expect(REFUSED).toHaveLength(5);
    expect(KEPT).toHaveLength(2);
  });

  row('a next that is not a plain path is kept as /experiments when the sign-in starts', async () => {
    const landed: string[] = [];
    for (const next of [...REFUSED, ...KEPT]) {
      const view = await renderLogin({ next });
      await clickSignIn();
      landed.push(String(storedPending()?.next));
      view.unmount();
    }
    expect(landed).toEqual([...REFUSED.map(() => '/experiments'), ...KEPT]);
  });

  row('and is checked again when the sign-in is finished', () => {
    for (const next of REFUSED) expect(`landed on ${safeAfterSignInPath(next)}`).toBe('landed on /experiments');
    for (const next of KEPT) expect(safeAfterSignInPath(next)).toBe(next);
  });
});

describe('the redirect_uri (row 12)', () => {
  row('is this origin’s /auth/callback, byte for byte, whatever the page’s own URL', async () => {
    await renderLogin({ next: '/experiments', extra: 'x' });
    window.history.replaceState(null, '', '/login?next=%2Fexperiments&extra=x#part');
    await clickSignIn();
    const url = lastAuthorize();
    expect(url.searchParams.get('redirect_uri')).toBe('http://localhost/auth/callback');
    expect(url.origin + url.pathname).toBe('https://pool-a.auth.eu-west-1.amazoncognito.com/oauth2/authorize');
    expect(url.searchParams.get('response_type')).toBe('code');
    expect(url.searchParams.get('scope')).toBe('openid email profile aws.cognito.signin.user.admin');
  });
});

function SignOutButton() {
  const { logout, status } = useAuth();
  return (
    <button type="button" data-status={status} onClick={() => void logout()}>
      out
    </button>
  );
}

async function signedInUnderCognito(): Promise<void> {
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-1');
  api.routes['/api/v1/users/me'] = () =>
    jsonResponse(200, { id: 'a', email: 'pat@example.com', username: 'pat', role: 'VIEWER', is_superuser: false, is_active: true });
  api.routes[LOGOUT] = () => jsonResponse(204, undefined);
  render(
    <AuthConfigProvider>
      <AuthProvider>
        <SignOutButton />
      </AuthProvider>
    </AuthConfigProvider>,
  );
  await settle();
  expect(screen.getByRole('button', { name: 'out' })).toHaveAttribute('data-status', 'authenticated');
}

describe('sign-out (row 13)', () => {
  row('sends POST /auth/logout with keepalive, forgets the token, then goes to Cognito’s /logout', async () => {
    await signedInUnderCognito();
    timeline.length = 0;
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'out' }));
    });
    await settle();
    expect(timeline).toEqual(['fetch POST /api/v1/auth/logout', 'navigate /logout']);
    const posts = callsTo(LOGOUT);
    expect(posts).toHaveLength(1);
    expect(posts[0].init.method).toBe('POST');
    expect(posts[0].init.keepalive).toBe(true);
    expect((posts[0].init.headers as Record<string, string>).Authorization).toBe('Bearer tok-1');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(logoutHits()).toHaveLength(1);
    const url = new URL(logoutHits()[0]);
    expect(url.origin + url.pathname).toBe('https://pool-a.auth.eu-west-1.amazoncognito.com/logout');
    expect(url.searchParams.get('client_id')).toBe('clientpoola');
    expect(url.searchParams.get('logout_uri')).toBe('http://localhost/login');
  });

  row('the API’s answer to the sign-out changes nothing: a 401 still ends at Cognito’s /logout', async () => {
    await signedInUnderCognito();
    api.routes[LOGOUT] = () => jsonResponse(401, { detail: 'Could not validate credentials' });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'out' }));
    });
    await settle();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(logoutHits()).toHaveLength(1);
  });

  row('Cognito’s /logout returns to /login, which says the user signed out', async () => {
    await signedInUnderCognito();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'out' }));
    });
    await settle();
    await renderLogin();
    expect(screen.getByRole('status')).toHaveTextContent(SIGNED_OUT_MESSAGE);
    expect(screen.getAllByRole('button', { name: 'Sign in' })).toHaveLength(1);
  });
});

describe('logout_uri comes from nowhere but this origin (row 14)', () => {
  row('a logout_uri in the page’s address is not used', async () => {
    window.history.replaceState(null, '', '/experiments?logout_uri=https%3A%2F%2Fevil.example');
    await signedInUnderCognito();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'out' }));
    });
    await settle();
    expect(logoutHits()).toHaveLength(1);
    expect(logoutHits()[0]).not.toContain('evil');
  });
});

describe('the configuration is read at run time (row 19)', () => {
  row('the authorize URL uses the domain and client the API names, for either of two pools', async () => {
    const seen: string[] = [];
    for (const pool of [POOL_A, POOL_B]) {
      resetHarness(pool);
      const view = await renderLogin();
      await clickSignIn();
      const url = lastAuthorize();
      seen.push(`${url.hostname} ${url.searchParams.get('client_id')}`);
      view.unmount();
    }
    expect(seen).toEqual([
      'pool-a.auth.eu-west-1.amazoncognito.com clientpoola',
      'pool-b.auth.us-east-2.amazoncognito.com clientpoolb',
    ]);
    expect(seen.join(' ')).not.toContain('undefined');
  });
});

/** The page alone (no `who` span), inside the providers `_app.tsx` mounts. */
async function renderPageOnly(): Promise<HTMLElement> {
  const { container } = render(
    <AuthConfigProvider>
      <AuthProvider>
        <LoginPage />
      </AuthProvider>
    </AuthConfigProvider>,
  );
  await settle();
  return container;
}

/** Today's form: the page as rendered with no configuration at all. */
function localForm(): string {
  const { container, unmount } = render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
  const markup = normalisedMarkup(container);
  unmount();
  return markup;
}

describe('any answer that is not a well-formed Cognito one is the local form (row 20)', () => {
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown) => unhandled.push(reason);
  beforeAll(() => process.on('unhandledRejection', onUnhandled));
  afterAll(() => process.off('unhandledRejection', onUnhandled));

  const ANSWERS: Array<[string, () => Response | Promise<Response>]> = [
    ['404 from an API without the route', () => jsonResponse(404, { detail: 'Not Found' })],
    ['no answer (network error)', () => Promise.reject(new TypeError('Failed to fetch'))],
    ['a 200 that is not JSON', () => textResponse(200, 'ok', 'text/plain')],
    ['the export’s HTML page with a 200', () => textResponse(200, '<!DOCTYPE html><html><body>app</body></html>', 'text/html')],
    ['a body that does not parse', () => textResponse(200, '{"provider": "cogn', 'application/json')],
    ['a Cognito body without a domain', () => jsonResponse(200, { provider: 'cognito', client_id: 'clientpoola' })],
    ['a Cognito body without a client', () => jsonResponse(200, { provider: 'cognito', hosted_ui_domain: 'pool-a.auth.eu-west-1.amazoncognito.com' })],
    ['a Cognito body whose domain is a URL', () => jsonResponse(200, { ...POOL_A, hosted_ui_domain: 'https://pool-a.example.com/x' })],
    ['the local provider', () => jsonResponse(200, { provider: 'local' })],
  ];

  row('the answers are exactly these', () => {
    expect(ANSWERS).toHaveLength(9);
  });

  it.each(ANSWERS)('%s', async (_name, answer) => {
    const reference = localForm();
    api.routes[CONFIG] = answer;
    __resetAuthConfigCache();
    const container = await renderPageOnly();
    expect(normalisedMarkup(container)).toBe(reference);
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(screen.queryByTestId('hosted-ui-sign-in')).toBeNull();
    expect(assigned).toEqual([]);
    expect(unhandled).toEqual([]);
  });
  registered += 1;

  row('configured:false says sign-in is not set up, names the setting, and offers no redirect', async () => {
    api.routes[CONFIG] = () => jsonResponse(200, { provider: 'cognito', configured: false, missing: ['COGNITO_DOMAIN'] });
    await renderLogin();
    expect(screen.getByRole('alert')).toHaveTextContent(SIGN_IN_NOT_SET_UP_MESSAGE);
    expect(screen.getByTestId('login-missing-settings')).toHaveTextContent('Not set: COGNITO_DOMAIN.');
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByLabelText('Password')).toBeNull();
    expect(assigned).toEqual([]);
  });

  row('a local user signs in when the API has no /auth/config', async () => {
    api.routes[CONFIG] = () => jsonResponse(404, { detail: 'Not Found' });
    api.routes['/api/v1/auth/login'] = () =>
      jsonResponse(200, {
        access_token: 'local-1',
        token_type: 'bearer',
        expires_in: 3600,
        user: { id: 'u', email: 'pat@example.com', username: 'pat', full_name: null, role: 'ADMIN', is_superuser: true, is_active: true, auth_provider: 'local' },
      });
    await renderLogin();
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'pat@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw-1' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    });
    await settle();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('local-1');
    expect(mockReplace).toHaveBeenCalledWith('/experiments');
  });
});

describe('no password form before the configuration is known (row 21)', () => {
  row('the first render says Loading and shows no form', async () => {
    api.routes[CONFIG] = () => new Promise<Response>(() => undefined);
    render(
      <AuthConfigProvider>
        <AuthProvider>
          <LoginPage />
        </AuthProvider>
      </AuthConfigProvider>,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Loading…');
    expect(screen.queryByLabelText('Password')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
    await settle();
    expect(screen.queryByLabelText('Password')).toBeNull();
  });
});

/** `/login` inside the providers `_app.tsx` mounts, with the SSO module installed. */
async function renderLoginWithSso(): Promise<void> {
  __resetModulesCache();
  api.routes['/api/v1/modules'] = () => jsonResponse(200, { profile: 'full', modules: ['sso'], version: '0.0.0' });
  render(
    <ModulesProvider>
      <AuthConfigProvider>
        <AuthProvider>
          <LoginPage />
        </AuthProvider>
      </AuthConfigProvider>
    </ModulesProvider>,
  );
  await settle();
}

describe('one button under Cognito (row 22)', () => {
  row('no SSO button under Cognito, where the local form offers one with the module installed', async () => {
    // The control needs the SSO module's component; a core run has its stub.
    if (modulesTreeAvailable()) {
      resetHarness({ provider: 'local' });
      await renderLoginWithSso();
      expect(screen.getByRole('button', { name: 'Sign in with SSO' })).toBeInTheDocument();
      cleanup();
    }
    resetHarness(POOL_A);
    await renderLoginWithSso();
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Sign in with SSO' })).toBeNull();
  });

  row('exactly one button, no inputs, no SSO and no password help', async () => {
    const view = await renderLogin();
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.getByRole('button')).toHaveTextContent('Sign in');
    expect(view.container.querySelectorAll('input')).toHaveLength(0);
    expect(screen.queryByText(/single sign-on|sso/i)).toBeNull();
    expect(screen.queryByText(/forgot password/i)).toBeNull();
    expect(screen.queryByText(/change password/i)).toBeNull();
    expect(screen.getByText("You will sign in on your organisation's Cognito page.")).toBeInTheDocument();
  });
});

describe('the local provider is unchanged (row 23)', () => {
  row('{"provider":"local"} renders the local form exactly, and no Hosted UI button', async () => {
    const reference = localForm();
    api.routes[CONFIG] = () => jsonResponse(200, { provider: 'local' });
    const container = await renderPageOnly();
    expect(normalisedMarkup(container)).toBe(reference);
    expect(screen.queryByTestId('hosted-ui-sign-in')).toBeNull();
  });
});

describe('accessibility of the Cognito sign-in page (row 29)', () => {
  row('a real button, no navigation without a click, then Redirecting…', async () => {
    await renderLogin();
    const button = screen.getByRole('button', { name: 'Sign in' });
    expect(button.tagName).toBe('BUTTON');
    expect(button).toHaveAttribute('type', 'button');
    await settle();
    expect(assigned).toEqual([]);
    api.routes = { ...api.routes };
    await clickSignIn();
    expect(authorizeHits()).toHaveLength(1);
    expect(screen.getByRole('button')).toHaveTextContent('Redirecting…');
    expect(screen.getByRole('button')).toHaveAttribute('aria-disabled', 'true');
  });

  row('an error is an alert that takes the focus, with a visible Try again', async () => {
    sessionStorage.setItem('controlvar.hostedUi.notice', JSON.stringify({ code: 'expired', next: '/experiments' }));
    await renderLogin();
    const alert = screen.getByRole('alert');
    expect(document.activeElement).toBe(alert);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible();
  });
});

describe('the session check under Cognito (row 15)', () => {
  row('an account the API reports inactive (400) does not keep its token, and /login says why', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-1');
    api.routes['/api/v1/users/me'] = () => jsonResponse(400, { detail: 'Inactive user' });
    await renderLogin();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent(INACTIVE_ACCOUNT_MESSAGE);
    expect(callsTo('/api/v1/auth/me')).toHaveLength(0);
    expect(assigned).toEqual([]);
  });
});

describe('this file', () => {
  it('collects every test it declares', () => {
    expect(registered).toBe(22);
  });
});
