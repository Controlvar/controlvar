import React, { createContext, useContext, useMemo, useRef, ReactNode } from 'react';
import { ExperimentationClient } from '../client/ExperimentationClient';
import { checkOnExposure } from '../client/exposure';
import { Exposure, SdkConfig, UserContext } from '../client/types';

export interface ExperimentationContextValue {
  client: ExperimentationClient;
  user: UserContext;
}

const ExperimentationContext = createContext<ExperimentationContextValue | null>(null);

interface ProviderProps {
  config: SdkConfig;
  user: UserContext;
  children: ReactNode;
}

export function ExperimentationProvider({ config, user, children }: ProviderProps) {
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

  return (
    <ExperimentationContext.Provider value={value}>
      {children}
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
