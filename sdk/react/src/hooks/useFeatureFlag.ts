import { useState, useEffect, useRef } from 'react';
import { useBootstrap, useExperimentation } from '../context/ExperimentationProvider';
import { disabledEvaluation } from '../client/ExperimentationClient';
import { Seed } from '../client/bootstrap';
import { FeatureFlagEvaluation } from '../client/types';

/**
 * Evaluates a feature flag for the current user (server-side decision).
 *
 * Starts as `{ isEnabled: false, variant: null, config: null, loading: true }`,
 * or, when the Provider's `bootstrap` has this flag, as its answer with
 * `loading: false` (on the server and in the browser, with no request). Then
 * asks the server, and shows its answer. Re-evaluates when the flag key or
 * user changes. When the request fails, `error` is set: a hook that started
 * from the bootstrap keeps that answer; any other reports the flag off.
 */
export function useFeatureFlag(flagKey: string): FeatureFlagEvaluation {
  const { client, user } = useExperimentation();
  const bootstrap = useBootstrap();
  const [seed] = useState<Seed<FeatureFlagEvaluation> | null>(() => {
    const value = bootstrap.flags.get(flagKey);
    return value ? { key: flagKey, user, value } : null;
  });
  const [state, setState] = useState<FeatureFlagEvaluation>(
    () => seed?.value ?? disabledEvaluation(flagKey, { loading: true })
  );
  // The bootstrapped answer while the hook still shows it; cleared by its own
  // answer and by a change of key or user.
  const seeded = useRef(seed);

  useEffect(() => {
    let cancelled = false;
    const kept =
      seeded.current && seeded.current.key === flagKey && seeded.current.user === user
        ? seeded.current.value
        : null;
    if (!kept) {
      seeded.current = null;
      setState(disabledEvaluation(flagKey, { loading: true }));
    }

    client
      .evaluateFeatureFlagDetailed(user, flagKey)
      .then(evaluation => {
        if (!cancelled) {
          seeded.current = null;
          setState(evaluation);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const error = err instanceof Error ? err : new Error(String(err));
          setState(kept ? { ...kept, error } : disabledEvaluation(flagKey, { error }));
        }
      });

    return () => {
      cancelled = true;
    };
  }, [client, user, flagKey]);

  return state;
}
