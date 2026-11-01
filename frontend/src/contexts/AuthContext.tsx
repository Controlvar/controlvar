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
  setToken,
  unreachableMessage,
} from '@/services/api';

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
   */
  logout: (options?: LogoutOptions) => Promise<void>;
  /**
   * Where a protected page sends this tab after `logout()`: `/login`, or
   * `/login?next=` when the logout named a `next`. Never the page the
   * signed-out user was on, so the next person to sign in does not land there
   * (#1006). `null` until this tab logs out, and again after a sign-in.
   */
  afterLogoutPath: string | null;
  /** Re-fetch `/auth/me` (e.g. after a profile change). */
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
const LOGIN_PATH = '/api/v1/auth/login';
const LOGOUT_PATH = '/api/v1/auth/logout';

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
 * The user object is always re-derived from `GET /api/v1/auth/me` on mount so
 * a stale or revoked token can never resurrect a session.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserMe | null>(null);
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [afterLogoutPath, setAfterLogoutPath] = useState<string | null>(null);
  const mounted = useRef(true);

  const applyAnonymous = useCallback(() => {
    if (!mounted.current) return;
    setUser(null);
    setStatus('anonymous');
  }, []);

  const loadMe = useCallback(async (): Promise<UserMe | null> => {
    if (!getToken()) {
      applyAnonymous();
      return null;
    }
    try {
      const me = await apiFetch<UserMe>(ME_PATH, { redirectOn401: false });
      if (mounted.current) {
        setUser(me);
        setStatus('authenticated');
        setSessionError(null);
        setAfterLogoutPath(null);
      }
      return me;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        // apiFetch already cleared the token.
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
  }, [applyAnonymous]);

  useEffect(() => {
    mounted.current = true;
    void loadMe();
    return () => {
      mounted.current = false;
    };
  }, [loadMe]);

  // Keep tabs in sync: logging out (or in) elsewhere updates this tab too.
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const onStorage = (event: StorageEvent) => {
      if (event.key !== TOKEN_STORAGE_KEY && event.key !== null) return;
      if (!getToken()) {
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
      // Sign out here first (#1040): awaiting the server left the user signed
      // in for as long as a stalled API took to answer. These updates are made
      // together, so a protected page sees `anonymous` and `afterLogoutPath`
      // in the same render and never redirects with this page as `next`.
      clearToken();
      setSessionError(null);
      setAfterLogoutPath(loginPathWithNext(options.next));
      applyAnonymous();
      if (!token) return;
      // Then tell the server, without waiting. The token is passed by hand
      // with `auth: false`, so an answer arriving after the next sign-in
      // (even a 401) cannot clear that session's token. The API revokes
      // nothing at logout (tokens are stateless), so nothing depends on the
      // request arriving.
      void apiFetch<void>(LOGOUT_PATH, {
        method: 'POST',
        auth: false,
        redirectOn401: false,
        headers: { Authorization: `Bearer ${token}` },
      }).catch(() => undefined);
    },
    [applyAnonymous],
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
