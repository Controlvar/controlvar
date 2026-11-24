import React, { ReactNode, createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/router';
import { useOptionalAuth } from '@/contexts/AuthContext';
import {
  DEFAULT_FLAG_ENVIRONMENT,
  FLAG_ENVIRONMENT_QUERY,
  FlagEnvironment,
  parseFlagEnvironment,
} from '@/utils/flagEnvironments';

/**
 * The environment the flag pages show (Production, Staging or Development).
 *
 * Where the choice comes from, in order:
 *   1. `?env=` in the page's URL, so a link opens the environment it names;
 *   2. the signed-in user's last choice, kept in this browser per user;
 *   3. Production.
 * Choosing an environment stores it for the user and writes `?env=` into the
 * URL. The choice is kept across client-side navigation (the provider sits in
 * `_app.tsx`), and a later `?env=` in a link still wins.
 *
 * Until the router is ready and the session is known, `environment` is null:
 * a page waits rather than showing Production first and then switching.
 */
export interface FlagEnvironmentValue {
  /** The environment on screen, or null while it is being resolved. */
  environment: FlagEnvironment | null;
  /** Show another environment: stored for the user and written to `?env=`. */
  choose: (next: FlagEnvironment) => void;
  /** True while the URL is being changed to name the chosen environment. */
  routing: boolean;
  /** What the page says it has on screen; null while it loads, and on a page that says nothing. */
  shown: ShownEnvironment | null;
  /** A flag page's own report: loaded, failed, or (null) loading. */
  reportShown: (shown: ShownEnvironment | null) => void;
  /** False outside a provider (Production only, nothing to choose). */
  switchable: boolean;
}

/** An environment a flag page has on screen, and whether its data loaded. */
export interface ShownEnvironment {
  environment: FlagEnvironment;
  loaded: boolean;
}

/** Outside a provider: Production, fixed. The pages then behave as they did before environments. */
const PRODUCTION_ONLY: FlagEnvironmentValue = {
  environment: DEFAULT_FLAG_ENVIRONMENT,
  choose: () => undefined,
  routing: false,
  shown: null,
  reportShown: () => undefined,
  switchable: false,
};

const FlagEnvironmentContext = createContext<FlagEnvironmentValue | null>(null);

/** The browser storage key for one user's choice. */
export const FLAG_ENVIRONMENT_STORAGE_PREFIX = 'controlvar.flag-environment.';

export function flagEnvironmentStorageKey(userId: string): string {
  return `${FLAG_ENVIRONMENT_STORAGE_PREFIX}${userId}`;
}

/** The user's stored choice, or null (none, unreadable, or storage unavailable). */
export function readStoredFlagEnvironment(userId: string | null): FlagEnvironment | null {
  if (!userId || typeof window === 'undefined') return null;
  try {
    return parseFlagEnvironment(window.localStorage.getItem(flagEnvironmentStorageKey(userId)));
  } catch {
    return null;
  }
}

function storeFlagEnvironment(userId: string | null, environment: FlagEnvironment): void {
  if (!userId || typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(flagEnvironmentStorageKey(userId), environment);
  } catch {
    // A private window or full storage: the choice still applies to this tab.
  }
}

export function FlagEnvironmentProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const auth = useOptionalAuth();
  const userId = auth?.user?.id ?? null;
  // No provider, or the session answered: either way the user is known.
  const sessionKnown = !auth || auth.status !== 'loading';
  const ready = router.isReady && sessionKnown;
  const queryEnvironment = ready ? parseFlagEnvironment(router.query?.[FLAG_ENVIRONMENT_QUERY]) : null;

  // The environment on screen, and whose choice it is.
  const [current, setCurrent] = useState<{ environment: FlagEnvironment; userId: string | null } | null>(null);

  useEffect(() => {
    if (!ready) return;
    if (queryEnvironment) {
      setCurrent({ environment: queryEnvironment, userId });
      return;
    }
    setCurrent((was) =>
      was && was.userId === userId
        ? was
        : { environment: readStoredFlagEnvironment(userId) ?? DEFAULT_FLAG_ENVIRONMENT, userId },
    );
  }, [ready, queryEnvironment, userId]);

  // The URL changes still under way (two quick choices overlap).
  const [routeChanges, setRouteChanges] = useState(0);
  const [shown, reportShown] = useState<ShownEnvironment | null>(null);

  const choose = useCallback(
    (next: FlagEnvironment) => {
      setCurrent({ environment: next, userId });
      storeFlagEnvironment(userId, next);
      setRouteChanges((n) => n + 1);
      void Promise.resolve(
        router.replace(
          { pathname: router.pathname, query: { ...router.query, [FLAG_ENVIRONMENT_QUERY]: next } },
          undefined,
          { shallow: true, scroll: false },
        ),
      )
        // A later navigation may replace this one: the choice stands.
        .catch(() => undefined)
        .finally(() => setRouteChanges((n) => n - 1));
    },
    [router, userId],
  );

  const value = useMemo<FlagEnvironmentValue>(
    () => ({
      environment: ready && current && current.userId === userId ? current.environment : null,
      choose,
      routing: routeChanges > 0,
      shown,
      reportShown,
      switchable: true,
    }),
    [ready, current, userId, choose, routeChanges, shown],
  );

  return <FlagEnvironmentContext.Provider value={value}>{children}</FlagEnvironmentContext.Provider>;
}

export function useFlagEnvironment(): FlagEnvironmentValue {
  return useContext(FlagEnvironmentContext) ?? PRODUCTION_ONLY;
}
