import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  ReactNode,
} from 'react';
import {
  ApiError,
  LoginResponse,
  Role,
  TOKEN_STORAGE_KEY,
  UserMe,
  apiFetch,
  clearToken,
  getToken,
  loginPathWithNext,
  navigation,
  setToken,
  unreachableMessage,
} from '@/services/api';
import { useAuthConfig } from '@/contexts/AuthConfigContext';
import type { AuthConfig } from '@/services/authConfig';
import {
  beginSilentSignIn,
  hostedUiLogoutUrl,
  silentSignInAllowed,
  writeSignInNotice,
} from '@/services/hostedUi';

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

export interface LogoutOptions {
  /**
   * The page the next sign-in should open, for a caller that signs out on
   * purpose to come back (an invitation meant for another account). Without
   * it the next sign-in opens the default page.
   */
  next?: string;
}

export interface AuthContextValue {
  user: UserMe | null;
  status: AuthStatus;
  /** Convenience: `status === 'authenticated'`. */
  isAuthenticated: boolean;
  /** `POST /api/v1/auth/login`; stores the token and resolves with the user. */
  login: (email: string, password: string) => Promise<UserMe>;
  /**
   * Signs out at once: clears the token and the user before anything else,
   * then sends `POST /api/v1/auth/logout` without waiting for it (best effort;
   * its answer, failure or silence changes nothing). Resolves straight away.
   * Under Cognito it then sends the browser to Cognito's `/logout`, which
   * returns to `/login`.
   */
  logout: (options?: LogoutOptions) => Promise<void>;
  /**
   * Where a protected page sends this tab after `logout()`: `/login`, or
   * `/login?next=` when the logout named a `next`. Never the page the
   * signed-out user was on, so the next person to sign in does not land there
   * (#1006). `null` until this tab logs out, and again after a sign-in.
   */
  afterLogoutPath: string | null;
  /**
   * Re-fetch `/auth/me` (e.g. after a profile change). Resolves `null`, and
   * changes nothing, when a sign-in, sign-out or newer check came first.
   */
  refresh: () => Promise<UserMe | null>;
  /** True when the current user has one of the given roles. */
  hasRole: (...roles: Role[]) => boolean;
  /**
   * Why a stored session could not be restored, when the reason was not a
   * rejected token: a server error or an unreachable API. The login page shows
   * it, so the redirect there is not silent. `null` otherwise.
   */
  sessionError: string | null;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

const ME_PATH = '/api/v1/auth/me';
/**
 * Who the user is under Cognito (#708): `GET /auth/me` there answers Cognito's
 * own profile, not the account, so the dashboard reads `GET /users/me`.
 */
const COGNITO_ME_PATH = '/api/v1/users/me';
const LOGIN_PATH = '/api/v1/auth/login';
const LOGOUT_PATH = '/api/v1/auth/logout';

/** The sign-in page's copy for an account the API reports as not active. */
export const INACTIVE_ACCOUNT_MESSAGE = 'Your account is not active. Ask an administrator.';

/** `GET /users/me`'s answer: the account, without `auth_provider`. */
interface UsersMeAnswer {
  id: string;
  email: string | null;
  username: string;
  full_name?: string | null;
  role?: Role | null;
  is_superuser: boolean;
  is_active: boolean;
}

/**
 * The signed-in user under Cognito: `/users/me`'s account, with
 * `auth_provider` from `/auth/config` (the account carries none).
 */
function cognitoUser(me: UsersMeAnswer): UserMe {
  return {
    id: String(me.id),
    email: me.email ?? '',
    username: me.username,
    full_name: me.full_name ?? null,
    role: me.role ?? 'VIEWER',
    is_superuser: me.is_superuser === true,
    is_active: me.is_active !== false,
    auth_provider: 'cognito',
  };
}

function isCognito(config: AuthConfig): boolean {
  return config.kind !== 'local';
}

function onLoginPage(): boolean {
  if (typeof window === 'undefined' || !window.location) return false;
  return window.location.pathname.replace(/\/+$/, '') === '/login';
}

/** Copy for a session that could not be restored for a reason other than 401. */
export function sessionErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.timedOut) return err.message;
  if (err instanceof ApiError && err.isNetworkError) return unreachableMessage();
  if (err instanceof ApiError && err.isServerError) {
    const id = err.requestId ? ` Request ID: ${err.requestId}.` : '';
    return `Signed out because the server returned an error (HTTP ${err.status}).${id}`;
  }
  return 'Signed out because your session could not be checked. Sign in again.';
}

/**
 * Only the bearer token is persisted (`localStorage["experimently.token"]`).
 * The user object is always re-derived from `GET /api/v1/auth/me` (under
 * Cognito, `GET /api/v1/users/me`) on mount so a stale or revoked token can
 * never resurrect a session.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserMe | null>(null);
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [afterLogoutPath, setAfterLogoutPath] = useState<string | null>(null);
  const mounted = useRef(true);
  // Bumped by every sign-in, sign-out and session check (#1579). A check that
  // is no longer the latest when its answer arrives changes nothing, whatever
  // the answer: it was about a session that has since ended or been replaced.
  const generation = useRef(0);

  const applyAnonymous = useCallback(() => {
    if (!mounted.current) return;
    setUser(null);
    setStatus('anonymous');
  }, []);

  // How this dashboard signs in (#708). Outside an AuthConfigProvider it is
  // the local provider, already known, and nothing below changes.
  const { loading: configLoading, config } = useAuthConfig();
  const cognito = isCognito(config);

  const loadMe = useCallback(async (): Promise<UserMe | null> => {
    const check = ++generation.current;
    const sent = getToken();
    if (!sent) {
      applyAnonymous();
      return null;
    }
    try {
      const me = cognito
        ? cognitoUser(await apiFetch<UsersMeAnswer>(COGNITO_ME_PATH, { redirectOn401: false }))
        : await apiFetch<UserMe>(ME_PATH, { redirectOn401: false });
      if (check !== generation.current) return null;
      if (mounted.current) {
        setUser(me);
        setStatus('authenticated');
        setSessionError(null);
        setAfterLogoutPath(null);
      }
      return me;
    } catch (err) {
      if (check !== generation.current) return null;
      if (err instanceof ApiError && err.status === 401) {
        // apiFetch cleared the token if it was still the one this check sent.
        applyAnonymous();
        // Under Cognito a session that has run out is started again once,
        // without a page while the Hosted UI's own session lasts; never from
        // the sign-in page, and never twice in ten minutes.
        if (
          config.kind === 'cognito' &&
          !getToken() &&
          !onLoginPage() &&
          silentSignInAllowed()
        ) {
          void beginSilentSignIn(config, navigation.currentPath()).catch(() => undefined);
        }
        return null;
      }
      if (cognito && err instanceof ApiError && err.status === 400) {
        // "Inactive user": the account exists and is switched off. The token
        // can never work, so it is not kept for the next reload.
        if (getToken() === sent) clearToken();
        if (mounted.current) setSessionError(INACTIVE_ACCOUNT_MESSAGE);
        applyAnonymous();
        return null;
      }
      // API unreachable or 5xx: keep the token for the next reload but treat
      // the session as anonymous so protected pages fall back to /login, and
      // say why there.
      if (mounted.current) setSessionError(sessionErrorMessage(err));
      applyAnonymous();
      return null;
    }
  }, [applyAnonymous, cognito, config]);

  useEffect(() => {
    mounted.current = true;
    // The session check waits for the configuration: it decides which
    // route says who the user is.
    if (!configLoading) void loadMe();
    return () => {
      mounted.current = false;
    };
  }, [loadMe, configLoading]);

  // Keep tabs in sync: logging out (or in) elsewhere updates this tab too.
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const onStorage = (event: StorageEvent) => {
      if (event.key !== TOKEN_STORAGE_KEY && event.key !== null) return;
      if (!getToken()) {
        generation.current += 1;
        applyAnonymous();
      } else {
        void loadMe();
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [applyAnonymous, loadMe]);

  const login = useCallback(async (email: string, password: string): Promise<UserMe> => {
    const result = await apiFetch<LoginResponse>(LOGIN_PATH, {
      method: 'POST',
      json: { email, password },
      auth: false,
    });
    // A check still in flight from before this sign-in must not undo it.
    generation.current += 1;
    setToken(result.access_token);
    if (mounted.current) {
      setUser(result.user);
      setStatus('authenticated');
      setSessionError(null);
      setAfterLogoutPath(null);
    }
    return result.user;
  }, []);

  const logout = useCallback(
    async (options: LogoutOptions = {}): Promise<void> => {
      const token = getToken();
      // A session check still in flight must not sign this tab back in (#1579).
      generation.current += 1;
      // Sign out here first (#1040): awaiting the server left the user signed
      // in for as long as a stalled API took to answer. These updates are made
      // together, so a protected page sees `anonymous` and `afterLogoutPath`
      // in the same render and never redirects with this page as `next`.
      clearToken();
      setSessionError(null);
      setAfterLogoutPath(loginPathWithNext(options.next));
      applyAnonymous();
      // Then tell the server, without waiting, and whatever it answers. The
      // token is passed by hand with `auth: false`, so an answer arriving
      // after the next sign-in (even a 401) cannot clear that session's
      // token. Under the local provider the API revokes nothing (its tokens
      // are stateless). Under Cognito it signs the user out at Cognito
      // (GlobalSignOut), so the token stops working everywhere; there the
      // request is sent with `keepalive`, so that it still goes out when the
      // page leaves for Cognito's `/logout` straight after.
      if (token) {
        void apiFetch<void>(LOGOUT_PATH, {
          method: 'POST',
          auth: false,
          redirectOn401: false,
          ...(cognito ? { keepalive: true } : {}),
          headers: { Authorization: `Bearer ${token}` },
        }).catch(() => undefined);
      }
      // Under Cognito, the Hosted UI's own session ends too: otherwise the
      // next "Sign in" on this browser signs the same user in without a page.
      // Its logout_uri is always this origin's /login, which then says so.
      if (config.kind === 'cognito') {
        writeSignInNotice({ code: 'signed_out', next: options.next ?? '' });
        navigation.assign(hostedUiLogoutUrl(config));
      }
    },
    [applyAnonymous, cognito, config],
  );

  const hasRole = useCallback(
    (...roles: Role[]): boolean => !!user && roles.includes(user.role),
    [user],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      status,
      isAuthenticated: status === 'authenticated',
      login,
      logout,
      afterLogoutPath,
      refresh: loadMe,
      hasRole,
      sessionError,
    }),
    [user, status, login, logout, afterLogoutPath, loadMe, hasRole, sessionError],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return ctx;
}

/** Like `useAuth` but returns `null` outside an `AuthProvider`. */
export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext) ?? null;
}
