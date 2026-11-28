import React, { ReactNode, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useAuth } from '@/contexts/AuthContext';
import { LOGIN_PATH, Role, safeNextPath } from '@/services/api';

export interface RequireAuthProps {
  children: ReactNode;
  /** When set, the user must hold one of these roles. */
  roles?: Role[];
  /**
   * When true, the user must be a superuser -- the same flag the admin API
   * enforces. `role` and `is_superuser` are independent columns, so a role
   * check cannot stand in for it (#84).
   */
  superuser?: boolean;
  /** Where the "Go back" link on the 403 view points. Default `/experiments`. */
  fallbackPath?: string;
  /** Custom element rendered while the session is being resolved. */
  loading?: ReactNode;
}

export interface ForbiddenViewProps {
  user: { email?: string | null; username?: string | null; role?: string | null };
  /** What the page requires, in a phrase: "a superuser account", "permission to create experiments". */
  requirement: string;
  fallbackPath?: string;
}

/**
 * The 403 view: one for every refused page, so each says which condition the
 * account failed rather than a generic refusal.
 */
export function ForbiddenView({ user, requirement, fallbackPath = '/experiments' }: ForbiddenViewProps) {
  return (
    <div
      data-testid="require-auth-forbidden"
      className="flex flex-col items-center justify-center min-h-[40vh] gap-4 px-4 text-center"
    >
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-600">403</p>
      <h1 className="text-slate-700 text-lg">You do not have permission to access this page.</h1>
      <p className="text-sm text-slate-500">
        Signed in as{' '}
        <span className="font-medium">
          {user.email || user.username || 'an account with no email address'}
        </span>{' '}
        ({user.role}). This page requires {requirement}.
      </p>
      <Link href={fallbackPath} className="text-blue-600 hover:underline" aria-label="Go to Home">
        Go to Home
      </Link>
    </div>
  );
}

/**
 * Client-side gate for protected pages (the API is the real enforcement).
 *
 * - `loading`    → spinner
 * - `anonymous`  → `router.replace('/login?next=<current path>')`, or, after
 *                  a logout in this tab, to the logout's own sign-in path
 *                  (`afterLogoutPath`, plain `/login` unless it named a `next`)
 * - wrong role   → inline 403 view (`data-testid="require-auth-forbidden"`)
 */
export function RequireAuth({ children, roles, superuser, fallbackPath = '/experiments', loading }: RequireAuthProps) {
  const { status, user, afterLogoutPath } = useAuth();
  const router = useRouter();

  const { isReady, asPath } = router;

  useEffect(() => {
    if (status !== 'anonymous') return;
    // On a static export `asPath` is the route pattern (`/experiments/[id]`)
    // until the router is ready; wait so `next=` carries the real URL.
    if (!isReady) return;
    // After a logout, this page belonged to the person who signed out: as
    // `next` it took the next person to sign in there (#1006). The logout's
    // path is also where its caller navigates, so the two cannot disagree.
    if (afterLogoutPath) {
      void router.replace(afterLogoutPath);
      return;
    }
    const next = safeNextPath(asPath, '/');
    const target =
      next === '/' || next.startsWith(LOGIN_PATH)
        ? LOGIN_PATH
        : `${LOGIN_PATH}?next=${encodeURIComponent(next)}`;
    void router.replace(target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, isReady, asPath, afterLogoutPath]);

  if (status === 'loading' || status === 'anonymous' || !user) {
    return (
      <>
        {loading ?? (
          <div
            data-testid="require-auth-loading"
            className="flex items-center justify-center min-h-[40vh]"
            role="status"
            aria-live="polite"
          >
            <div className="inline-block w-6 h-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
            <span className="sr-only">Loading…</span>
          </div>
        )}
      </>
    );
  }

  // Superuser first: it is what the admin API actually enforces, so failing it
  // is the more specific answer when a page asks for both.
  if (superuser && user.is_superuser !== true) {
    return <ForbiddenView user={user} requirement="a superuser account" fallbackPath={fallbackPath} />;
  }

  if (roles && roles.length > 0 && !roles.includes(user.role)) {
    return <ForbiddenView user={user} requirement={roles.join(' or ')} fallbackPath={fallbackPath} />;
  }

  return <>{children}</>;
}

export default RequireAuth;
