/**
 * useFlag — evaluates a feature flag for the current user (decided by the server).
 *
 * @example
 * ```tsx
 * function DarkModeButton() {
 *   const { enabled, config, loading } = useFlag('dark-mode');
 *   if (loading) return <ActivityIndicator />;
 *   return <Text>{enabled ? 'Dark mode ON' : 'Dark mode OFF'}</Text>;
 * }
 * ```
 */

import { useState, useEffect } from 'react';
import { useExperimentationContext } from '../context/ExperimentationContext';
import type { FlagState } from '../types';

function initialState(flagKey: string, loading: boolean): FlagState {
  return { key: flagKey, enabled: false, value: false, config: null, loading, error: null };
}

/**
 * Evaluates feature flag `flagKey` for the `userId` supplied to the enclosing
 * {@link ExperimentationProvider} via
 * `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…&context=…`.
 *
 * @param flagKey  The feature flag key to evaluate.
 * @param attributes  Sent as `context`, so targeting rules match. When omitted, the
 *   provider's `attributes` are sent (no merge). A change of attributes does not
 *   re-run the hook: see `ExperimentationClient.evaluateFlag`.
 * @returns `{ key, enabled, config, value, loading, error }` — `enabled` (and
 *   its alias `value`) is `true` when the server enabled the flag for this
 *   user, `false` while loading or on failure.
 */
export function useFlag(flagKey: string, attributes?: Record<string, unknown>): FlagState {
  const { client, userId, attributes: providerAttributes } = useExperimentationContext();

  const [state, setState] = useState<FlagState>(() => initialState(flagKey, true));

  useEffect(() => {
    let cancelled = false;

    setState(initialState(flagKey, true));

    client
      .evaluateFlag(flagKey, userId, attributes ?? providerAttributes)
      .then((evaluation) => {
        if (!cancelled) {
          setState({ ...evaluation, value: evaluation.enabled, loading: false, error: null });
        }
      })
      .catch((error: Error) => {
        if (!cancelled) {
          setState({ ...initialState(flagKey, false), error });
        }
      });

    return () => {
      cancelled = true;
    };
    // Re-evaluate when client, userId, or flagKey changes. Attributes are not a
    // dependency (an inline object is new on every render); the cache is keyed
    // by user + flag, so a re-run would be served the stored answer anyway.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, userId, flagKey]);

  return state;
}
