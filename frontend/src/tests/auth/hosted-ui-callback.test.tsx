/**
 * `/auth/callback`: the Cognito Hosted UI's return to the dashboard (#708).
 * The relay (`POST /api/v1/auth/cognito/exchange`) is a mock; the browser's
 * navigations are recorded, so the Hosted UI is the test walking back.
 */
import {
  ACCOUNT,
  AUTH_ME,
  RELAY,
  USERS_ME,
  api,
  apiSignsIn,
  assigned,
  authorizeHits,
  callsTo,
  hostedUiReturns,
  jsonResponse,
  logoutHits,
  mockReplace,
  renderCallback,
  renderLogin,
  resetHarness,
  restoreHarness,
  settle,
} from './hostedUiHarness';
import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { AuthConfigProvider } from '@/contexts/AuthConfigContext';
import { AuthProvider } from '@/contexts/AuthContext';
import CallbackPage from '@/pages/auth/callback';
import {
  SIGN_IN_CANCELLED_MESSAGE,
  SIGN_IN_EXPIRED_MESSAGE,
  SIGN_IN_REFUSED_MESSAGE,
  RATE_LIMITED_MESSAGE,
} from '@/pages/login';
import { INACTIVE_ACCOUNT_MESSAGE } from '@/contexts/AuthContext';
import { TOKEN_STORAGE_KEY, unreachableMessage } from '@/services/api';
import { PENDING_SIGN_IN_KEY, PENDING_SIGN_IN_TTL_MS, SILENT_SIGN_IN_KEY } from '@/services/hostedUi';

let registered = 0;
/** `it`, counted: the last test checks that this file collected all of them. */
function row(name: string, fn: () => unknown) {
  registered += 1;
  it(name, async () => {
    await fn();
  });
}

const T0 = Date.UTC(2026, 10, 8, 12, 0, 0);
let now = T0;

beforeEach(() => {
  resetHarness();
  now = T0;
  jest.spyOn(Date, 'now').mockImplementation(() => now);
});

afterEach(() => {
  restoreHarness();
  jest.restoreAllMocks();
});

/** Click "Sign in" on `/login` and return the query the Hosted UI sends back. */
async function startSignIn(query: Record<string, string> = {}, code = 'code-1'): Promise<string> {
  const view = await renderLogin(query);
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /sign in|try again/i }));
  });
  await settle();
  view.unmount();
  return hostedUiReturns(code);
}

/** The `/login` the callback sent the browser to: its alert text, or null. */
async function loginAlert(): Promise<string | null> {
  const view = await renderLogin();
  const alert = screen.queryByRole('alert');
  const text = alert ? alert.textContent : null;
  view.unmount();
  return text;
}

function storedToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY);
}

describe('positive control: a valid return signs the user in', () => {
  row('the code is exchanged, the account read, and the user lands on next', async () => {
    apiSignsIn();
    const back = await startSignIn({ next: '/feature-flags' });
    await renderCallback(back);
    expect(callsTo(RELAY)).toHaveLength(1);
    expect(JSON.parse(String(callsTo(RELAY)[0].init.body))).toEqual({
      code: 'code-1',
      code_verifier: expect.any(String),
      redirect_uri: 'http://localhost/auth/callback',
    });
    expect(storedToken()).toBe('tok-1');
    expect(mockReplace).toHaveBeenLastCalledWith('/feature-flags');
    expect(screen.getByTestId('who')).toHaveTextContent('authenticated:pat:VIEWER:cognito');
  });
});

describe('the state is checked (row 1)', () => {
  row('a state that is not the sign-in’s own is refused before the relay', async () => {
    apiSignsIn();
    await startSignIn();
    await renderCallback('?code=code-1&state=not-the-one');
    expect(callsTo(RELAY)).toHaveLength(0);
    expect(storedToken()).toBeNull();
    expect(await loginAlert()).toBe(SIGN_IN_EXPIRED_MESSAGE);
  });

  row('a state that only starts like the sign-in’s own is refused', async () => {
    apiSignsIn();
    const back = await startSignIn();
    const state = new URLSearchParams(back).get('state') ?? '';
    await renderCallback(`?code=code-1&state=${encodeURIComponent(state)}extra`);
    expect(callsTo(RELAY)).toHaveLength(0);
  });
});

describe('an absent or used sign-in is refused (row 2)', () => {
  row('no state in the return', async () => {
    apiSignsIn();
    await startSignIn();
    await renderCallback('?code=code-1');
    expect(callsTo(RELAY)).toHaveLength(0);
    expect(await loginAlert()).toBe(SIGN_IN_EXPIRED_MESSAGE);
  });

  row('no started sign-in in this tab, and the return names no state either', async () => {
    apiSignsIn();
    await renderCallback('?code=code-1');
    expect(callsTo(RELAY)).toHaveLength(0);
  });

  row('no started sign-in in this tab', async () => {
    apiSignsIn();
    const back = await startSignIn();
    sessionStorage.removeItem(PENDING_SIGN_IN_KEY);
    await renderCallback(back);
    expect(callsTo(RELAY)).toHaveLength(0);
    expect(await loginAlert()).toBe(SIGN_IN_EXPIRED_MESSAGE);
  });

  row('a started sign-in older than ten minutes', async () => {
    apiSignsIn();
    const back = await startSignIn();
    now = T0 + PENDING_SIGN_IN_TTL_MS + 1;
    await renderCallback(back);
    expect(callsTo(RELAY)).toHaveLength(0);
    expect(await loginAlert()).toBe(SIGN_IN_EXPIRED_MESSAGE);
  });

  row('the same return again, after the sign-in was used', async () => {
    apiSignsIn();
    const back = await startSignIn();
    await renderCallback(back);
    expect(callsTo(RELAY)).toHaveLength(1);
    localStorage.clear();
    await renderCallback(back);
    expect(callsTo(RELAY)).toHaveLength(1);
    expect(storedToken()).toBeNull();
  });
});

describe('regression #708: the started sign-in is gone before the exchange, and a code is used once (row 5)', () => {
  row('the entry is absent, and the query gone, when the relay is called', async () => {
    const seen: Array<{ entry: string | null; search: string }> = [];
    apiSignsIn();
    api.routes[RELAY] = () => {
      seen.push({ entry: sessionStorage.getItem(PENDING_SIGN_IN_KEY), search: window.location.search });
      return jsonResponse(200, { access_token: 'tok-1', expires_in: 3600 });
    };
    const back = await startSignIn();
    expect(sessionStorage.getItem(PENDING_SIGN_IN_KEY)).not.toBeNull();
    await renderCallback(back);
    expect(seen).toEqual([{ entry: null, search: '' }]);
  });

  row('a second mount in the same page load starts no second exchange', async () => {
    apiSignsIn();
    const back = await startSignIn();
    window.history.replaceState(null, '', `/auth/callback${back}`);
    render(
      <React.StrictMode>
        <AuthConfigProvider>
          <AuthProvider>
            <CallbackPage />
          </AuthProvider>
        </AuthConfigProvider>
      </React.StrictMode>,
    );
    await settle();
    expect(callsTo(RELAY)).toHaveLength(1);
  });
});

describe('only the access token is kept (row 6)', () => {
  row('the relay’s other fields are stored nowhere', async () => {
    apiSignsIn(ACCOUNT, { id_token: 'other-1', refresh_token: 'other-2', token_type: 'Bearer' });
    const back = await startSignIn();
    await renderCallback(back);
    expect(Object.keys(localStorage)).toEqual([TOKEN_STORAGE_KEY]);
    expect(storedToken()).toBe('tok-1');
    const everything = [...Object.values(localStorage), ...Object.values(sessionStorage)].join('\n');
    expect(everything).not.toContain('other-1');
    expect(everything).not.toContain('other-2');
  });
});

describe('a refused sign-in leaves no half session, and says why (row 15)', () => {
  /** A silent re-sign-in was just made, so a refusal ends the sign-in at once. */
  function silentJustUsed() {
    sessionStorage.setItem(SILENT_SIGN_IN_KEY, JSON.stringify(now));
  }

  const cases: Array<[string, () => Promise<string>, string, boolean]> = [
    [
      'cancelled on the Hosted UI',
      async () => {
        await startSignIn();
        return '?error=access_denied&state=x';
      },
      SIGN_IN_CANCELLED_MESSAGE,
      false,
    ],
    [
      'the relay refuses the code (400)',
      async () => {
        api.routes[RELAY] = () => jsonResponse(400, { detail: 'Cognito did not accept the sign-in code.' });
        return startSignIn();
      },
      SIGN_IN_EXPIRED_MESSAGE,
      false,
    ],
    [
      'a state mismatch',
      async () => {
        await startSignIn();
        return '?code=code-1&state=wrong';
      },
      SIGN_IN_EXPIRED_MESSAGE,
      false,
    ],
    [
      'no verifier kept',
      async () => {
        const back = await startSignIn();
        const entry = JSON.parse(sessionStorage.getItem(PENDING_SIGN_IN_KEY) ?? '{}');
        delete entry.verifier;
        sessionStorage.setItem(PENDING_SIGN_IN_KEY, JSON.stringify(entry));
        return back;
      },
      SIGN_IN_EXPIRED_MESSAGE,
      false,
    ],
    [
      'no code and no error',
      async () => {
        await startSignIn();
        return '';
      },
      SIGN_IN_EXPIRED_MESSAGE,
      false,
    ],
    [
      'the account read answers 401',
      async () => {
        api.routes[USERS_ME] = () => jsonResponse(401, { detail: 'Could not validate credentials' });
        silentJustUsed();
        return startSignIn();
      },
      SIGN_IN_REFUSED_MESSAGE,
      true,
    ],
    [
      'the account read answers 400 Inactive user',
      async () => {
        api.routes[USERS_ME] = () => jsonResponse(400, { detail: 'Inactive user' });
        return startSignIn();
      },
      INACTIVE_ACCOUNT_MESSAGE,
      true,
    ],
    [
      'the relay answers 429',
      async () => {
        api.routes[RELAY] = () => jsonResponse(429, { detail: 'Too many requests' }, { 'retry-after': '30' });
        return startSignIn();
      },
      RATE_LIMITED_MESSAGE,
      false,
    ],
    [
      'the API is unreachable',
      async () => {
        api.routes[RELAY] = () => Promise.reject(new TypeError('Failed to fetch'));
        return startSignIn();
      },
      unreachableMessage(),
      false,
    ],
  ];

  it.each(cases)('%s', async (_name, arrange, copy, throughLogout) => {
    apiSignsIn();
    // Each arrangement overrides what it needs after the default.
    const back = await arrange();
    await renderCallback(back);
    expect(storedToken()).toBeNull();
    expect(logoutHits()).toHaveLength(throughLogout ? 1 : 0);
    if (throughLogout) {
      const logout = new URL(logoutHits()[0]);
      expect(logout.searchParams.get('logout_uri')).toBe('http://localhost/login');
    }
    expect(await loginAlert()).toBe(copy);
  });
  registered += 1;

  row('the triggers in different groups each have their own copy', () => {
    const copies = new Set(cases.map(([, , copy]) => copy));
    expect(copies.size).toBe(6);
    expect(cases).toHaveLength(9);
  });

  row('the token is stored only after the account read answers', async () => {
    let storedWhenRead: string | null = 'unset';
    api.routes[RELAY] = () => jsonResponse(200, { access_token: 'tok-1', expires_in: 3600 });
    api.routes[USERS_ME] = () => {
      storedWhenRead = storedToken();
      return jsonResponse(401, { detail: 'Could not validate credentials' });
    };
    silentJustUsed();
    const back = await startSignIn();
    await renderCallback(back);
    expect(storedWhenRead).toBeNull();
    expect(storedToken()).toBeNull();
  });
});

describe('the error input is inert (row 16)', () => {
  const MARKER = 'Please phone 555 0100 to keep your account';

  row('error_description is never shown, as markup or as text', async () => {
    apiSignsIn();
    await startSignIn();
    const description = `<img src=x onerror="window.__ran=1"> ${MARKER}`;
    const view = await renderCallback(
      `?error=access_denied&error_description=${encodeURIComponent(description)}&state=x`,
    );
    expect(document.body.innerHTML).not.toContain(MARKER);
    expect(document.body.querySelector('img')).toBeNull();
    view.unmount();
    const login = await renderLogin();
    expect(document.body.innerHTML).not.toContain(MARKER);
    expect(document.body.innerHTML).not.toContain('onerror');
    expect(document.body.querySelector('img')).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent(SIGN_IN_CANCELLED_MESSAGE);
    login.unmount();
  });

  row('an unknown error code reads as an expired sign-in, not as its own text', async () => {
    apiSignsIn();
    await startSignIn();
    await renderCallback(`?error=${encodeURIComponent(MARKER)}&state=x`);
    expect(await loginAlert()).toBe(SIGN_IN_EXPIRED_MESSAGE);
    expect(document.body.innerHTML).not.toContain(MARKER);
  });
});

describe('the role comes from the account read (row 24) and the provider from the configuration (row 25)', () => {
  row('the dashboard shows /users/me’s role, and reads neither /auth/me nor the relay for it', async () => {
    apiSignsIn({ ...ACCOUNT, role: 'VIEWER', auth_provider: 'local' }, { role: 'ADMIN' });
    const back = await startSignIn();
    await renderCallback(back);
    expect(screen.getByTestId('who')).toHaveTextContent('authenticated:pat:VIEWER:cognito');
    expect(callsTo(AUTH_ME)).toHaveLength(0);
    expect(callsTo(USERS_ME).length).toBeGreaterThan(0);
  });
});

describe('the silent re-sign-in happens once at most (row L1)', () => {
  /** Walk the Hosted UI back to the callback while the browser is sent to it. */
  async function followHostedUi(limit = 5): Promise<void> {
    for (let i = 0; i < limit; i += 1) {
      const last = assigned[assigned.length - 1];
      if (!last || new URL(last).pathname !== '/oauth2/authorize') return;
      await renderCallback(hostedUiReturns(`code-${i + 1}`));
    }
  }

  row('the API refuses the token: the click, one silent re-sign-in, then the copy and the button', async () => {
    apiSignsIn();
    api.routes[USERS_ME] = () => jsonResponse(401, { detail: 'Could not validate credentials' });
    await startSignIn();
    expect(authorizeHits()).toHaveLength(1);
    await followHostedUi();
    expect(authorizeHits()).toHaveLength(2);
    expect(logoutHits()).toHaveLength(1);
    expect(storedToken()).toBeNull();
    await renderLogin();
    expect(screen.getByRole('alert')).toHaveTextContent(SIGN_IN_REFUSED_MESSAGE);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible();
    expect(authorizeHits()).toHaveLength(2);
  });

  row('a session that ran out is started again once, and not again within ten minutes', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'expired-1');
    api.routes[USERS_ME] = () => jsonResponse(401, { detail: 'Could not validate credentials' });
    window.history.replaceState(null, '', '/experiments');
    const first = render(
      <AuthConfigProvider>
        <AuthProvider>
          <span />
        </AuthProvider>
      </AuthConfigProvider>,
    );
    await settle();
    first.unmount();
    expect(authorizeHits()).toHaveLength(1);
    expect(new URL(authorizeHits()[0]).hostname).toBe('pool-a.auth.eu-west-1.amazoncognito.com');
    localStorage.setItem(TOKEN_STORAGE_KEY, 'expired-2');
    now = T0 + 60_000;
    render(
      <AuthConfigProvider>
        <AuthProvider>
          <span />
        </AuthProvider>
      </AuthConfigProvider>,
    );
    await settle();
    expect(authorizeHits()).toHaveLength(1);
  });

  row('a token that ran out, still stored when the callback opens, starts no second sign-in', async () => {
    apiSignsIn();
    api.routes[USERS_ME] = (init) =>
      (init.headers as Record<string, string>).Authorization === 'Bearer tok-1'
        ? jsonResponse(200, ACCOUNT)
        : jsonResponse(401, { detail: 'Could not validate credentials' });
    // The exchange answers only once the session check has refused the old token.
    let release: () => void = () => undefined;
    api.routes[RELAY] = () =>
      new Promise<Response>((resolve) => {
        release = () => resolve(jsonResponse(200, { access_token: 'tok-1', expires_in: 3600 }));
      });
    const back = await startSignIn();
    localStorage.setItem(TOKEN_STORAGE_KEY, 'expired-1');
    await renderCallback(back);
    expect(
      callsTo(USERS_ME).map((c) => (c.init.headers as Record<string, string>).Authorization),
    ).toEqual(['Bearer expired-1']);
    release();
    await settle();
    expect(authorizeHits()).toHaveLength(1);
    expect(storedToken()).toBe('tok-1');
    expect(mockReplace).toHaveBeenLastCalledWith('/experiments');
  });

  row('on the sign-in page a session that ran out starts nothing', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'expired-1');
    api.routes[USERS_ME] = () => jsonResponse(401, { detail: 'Could not validate credentials' });
    await renderLogin();
    expect(assigned).toEqual([]);
  });
});

describe('regression #708: a sign-in with an extra step on the Hosted UI (row 31)', () => {
  row('a code that comes back nine minutes later, after MFA or a new password, signs the user in', async () => {
    apiSignsIn();
    const back = await startSignIn({ next: '/experiments' });
    now = T0 + 9 * 60 * 1000;
    await renderCallback(back);
    expect(storedToken()).toBe('tok-1');
    expect(mockReplace).toHaveBeenLastCalledWith('/experiments');
  });
});

describe('the callback page', () => {
  row('says it is signing in with role=status, and asks for no referrer', async () => {
    api.routes[RELAY] = () => new Promise(() => undefined);
    const back = await startSignIn();
    await renderCallback(back);
    expect(screen.getByRole('status')).toHaveTextContent('Signing you in…');
    // React puts a <meta> in the document's head wherever it is rendered.
    expect(document.querySelector('meta[name="referrer"]')?.getAttribute('content')).toBe('no-referrer');
  });
});

describe('this file', () => {
  it('collects every test it declares', () => {
    expect(registered).toBe(23);
  });
});
