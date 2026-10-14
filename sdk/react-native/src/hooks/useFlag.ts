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
 * `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…`.
 *
 * @param flagKey  The feature flag key to evaluate.
 * @param _attributes  Accepted for signature compatibility and not sent: the
 *   evaluate endpoint takes a context, which this SDK does not send yet.
 * @returns `{ key, enabled, config, value, loading, error }` — `enabled` (and
 *   its alias `value`) is `true` when the server enabled the flag for this
 *   user, `false` while loading or on failure.
 */
export function useFlag(flagKey: string, _attributes?: Record<string, unknown>): FlagState {
  const { client, userId } = useExperimentationContext();

  const [state, setState] = useState<FlagState>(() => initialState(flagKey, true));

  useEffect(() => {
    let cancelled = false;

    setState(initialState(flagKey, true));

    client
      .evaluateFlag(flagKey, userId)
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
    // Re-evaluate when client, userId, or flagKey changes.
  }, [client, userId, flagKey]);

  return state;
}
