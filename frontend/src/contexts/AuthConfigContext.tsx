import React, { createContext, ReactNode, useContext, useEffect, useState } from 'react';
import {
  AuthConfig,
  AuthConfigAnswer,
  DemoMode,
  LOCAL_AUTH_CONFIG,
  fetchAuthConfig,
} from '@/services/authConfig';
import { isMarketingSite } from '@/utils/site-mode';

/**
 * How this dashboard signs in (#708), for the sign-in page, the callback and
 * `AuthProvider`, and whether the API is a read-only demo.
 *
 * `_app.tsx` mounts the provider, which reads `GET /api/v1/auth/config` once
 * per page load: `loading` is true until it has an answer, and every answer
 * that is not a well-formed Cognito one is the local provider
 * (`services/authConfig.ts`). Outside a provider the value is the local
 * provider, already loaded, with demo mode off: a page or a test that renders
 * without it gets the sign-in it had before this context existed.
 */
export interface AuthConfigState {
  /** True until the configuration has been read. */
  loading: boolean;
  /** The local provider while `loading`; the answer after. */
  config: AuthConfig;
  /** Demo mode, or null: off, and while `loading`. */
  demo: DemoMode | null;
}

const READY_LOCAL: AuthConfigState = Object.freeze({ loading: false, config: LOCAL_AUTH_CONFIG, demo: null });

const AuthConfigContext = createContext<AuthConfigState | undefined>(undefined);

let pending: Promise<AuthConfigAnswer> | null = null;

/** One read per page load, shared by every provider mounted in it. */
function loadOnce(): Promise<AuthConfigAnswer> {
  if (pending === null) pending = fetchAuthConfig();
  return pending;
}

/** Test hook: forget the page load's answer. */
export function __resetAuthConfigCache(): void {
  pending = null;
}

export function AuthConfigProvider({ children }: { children: ReactNode }) {
  // The marketing build has no API to ask.
  const [state, setState] = useState<AuthConfigState>(() =>
    isMarketingSite() ? READY_LOCAL : { loading: true, config: LOCAL_AUTH_CONFIG, demo: null },
  );

  useEffect(() => {
    if (!state.loading) return undefined;
    let mounted = true;
    void loadOnce().then(({ config, demo }) => {
      if (mounted) setState({ loading: false, config, demo });
    });
    return () => {
      mounted = false;
    };
    // Once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <AuthConfigContext.Provider value={state}>{children}</AuthConfigContext.Provider>;
}

/** The configuration, or the local provider (loaded) outside an `AuthConfigProvider`. */
export function useAuthConfig(): AuthConfigState {
  return useContext(AuthConfigContext) ?? READY_LOCAL;
}

/**
 * Demo mode, or null when it is off or not known yet. Everything demo mode
 * changes in the dashboard (the banner, the demo note, the hidden account
 * pages, no live results stream, the sign-in page) reads this.
 */
export function useDemoMode(): DemoMode | null {
  return useAuthConfig().demo;
}
