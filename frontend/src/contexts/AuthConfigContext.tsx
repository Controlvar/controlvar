import React, { createContext, ReactNode, useContext, useEffect, useState } from 'react';
import { AuthConfig, LOCAL_AUTH_CONFIG, fetchAuthConfig } from '@/services/authConfig';
import { isMarketingSite } from '@/utils/site-mode';

/**
 * How this dashboard signs in (#708), for the sign-in page, the callback and
 * `AuthProvider`.
 *
 * `_app.tsx` mounts the provider, which reads `GET /api/v1/auth/config` once
 * per page load: `loading` is true until it has an answer, and every answer
 * that is not a well-formed Cognito one is the local provider
 * (`services/authConfig.ts`). Outside a provider the value is the local
 * provider, already loaded: a page or a test that renders without it gets
 * the sign-in it had before this context existed.
 */
export interface AuthConfigState {
  /** True until the configuration has been read. */
  loading: boolean;
  /** The local provider while `loading`; the answer after. */
  config: AuthConfig;
}

const READY_LOCAL: AuthConfigState = Object.freeze({ loading: false, config: LOCAL_AUTH_CONFIG });

const AuthConfigContext = createContext<AuthConfigState | undefined>(undefined);

let pending: Promise<AuthConfig> | null = null;

/** One read per page load, shared by every provider mounted in it. */
function loadOnce(): Promise<AuthConfig> {
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
    isMarketingSite() ? READY_LOCAL : { loading: true, config: LOCAL_AUTH_CONFIG },
  );

  useEffect(() => {
    if (!state.loading) return undefined;
    let mounted = true;
    void loadOnce().then((config) => {
      if (mounted) setState({ loading: false, config });
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
