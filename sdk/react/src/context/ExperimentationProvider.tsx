import React, { createContext, useContext, useEffect, useMemo, useRef, ReactNode } from 'react';
import { ExperimentationClient } from '../client/ExperimentationClient';
import { BootstrapProblem, ReadBootstrap, readBootstrap } from '../client/bootstrap';
import { checkOnExposure } from '../client/exposure';
import { ExperimentationBootstrap, Exposure, SdkConfig, UserContext } from '../client/types';

export interface ExperimentationContextValue {
  client: ExperimentationClient;
  user: UserContext;
}

const ExperimentationContext = createContext<ExperimentationContextValue | null>(null);

/** The Provider's bootstrap as the hooks read it; internal, not exported from the package. */
const BootstrapContext = createContext<ReadBootstrap>(readBootstrap(undefined, ''));

interface ProviderProps {
  config: SdkConfig;
  user: UserContext;
  /**
   * Answers the server already has for this user (`ServerClient.bootstrap()`):
   * each hook whose key it holds starts from its answer, with no request, then
   * asks as it always does. Used only when its `userId` is `user.userId`. A
   * hook reads it when it mounts. `null` or `undefined` sets none.
   */
  bootstrap?: ExperimentationBootstrap | null;
  children: ReactNode;
}

export function ExperimentationProvider({ config, user, bootstrap, children }: ProviderProps) {
  // The client below only ever sees the wrapper, so the Provider refuses a
  // value that is not a function itself, as the client's constructor would.
  const onExposure = checkOnExposure(config.onExposure);
  // The latest render's onExposure, set during render (an effect does not run
  // in a server render, and useLayoutEffect warns there), so the wrapper calls
  // it without the client being recreated when an inline function changes.
  const exposureRef = useRef(onExposure);
  exposureRef.current = onExposure;

  // Stabilise the client — only recreate when a config primitive changes, so
  // callers may pass an inline config object without losing the cache.
  const client = useMemo(
    () =>
      new ExperimentationClient({
        ...config,
        // Returns what the hook returns, so the client's guard sees a Promise
        // it returns and handles its rejection.
        onExposure: (exposure: Exposure) => exposureRef.current?.(exposure),
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [config.apiKey, config.baseUrl, config.timeoutMs, config.cacheTtlMs]
  );

  // Stabilise the user reference by content so hooks only re-run when the
  // identity or attributes actually change.
  const attributesKey = JSON.stringify(user.attributes ?? null);
  const value = useMemo(
    () => ({ client, user }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, user.userId, attributesKey]
  );

  // From the props alone, so the server render and the hydration decide the same.
  const read = useMemo(() => readBootstrap(bootstrap, user.userId), [bootstrap, user.userId]);

  // Warnings: from an effect (never during a server render, never changing
  // what renders), once per reason for this Provider, StrictMode included.
  const warned = useRef(new Set<BootstrapProblem>());
  useEffect(() => {
    for (const [problem, text] of read.warnings) {
      if (warned.current.has(problem)) continue;
      warned.current.add(problem);
      // eslint-disable-next-line no-console
      console.warn(text);
    }
  }, [read]);

  return (
    <ExperimentationContext.Provider value={value}>
      <BootstrapContext.Provider value={read}>{children}</BootstrapContext.Provider>
    </ExperimentationContext.Provider>
  );
}

/** Access the SDK client and current user. Must be used inside ExperimentationProvider. */
export function useExperimentation(): ExperimentationContextValue {
  const ctx = useContext(ExperimentationContext);
  if (!ctx) {
    throw new Error('useExperimentation must be used within ExperimentationProvider');
  }
  return ctx;
}

/** Alias of {@link useExperimentation}, kept for backwards compatibility. */
export const useExperimentationContext = useExperimentation;

/**
 * The Provider's bootstrap as a hook reads it when it mounts. Internal: the
 * hooks of this package call it; it is not exported from the package.
 */
export function useBootstrap(): ReadBootstrap {
  return useContext(BootstrapContext);
}
