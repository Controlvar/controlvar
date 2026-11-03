import { useState, useEffect, useRef } from 'react';
import { useBootstrap, useExperimentation } from '../context/ExperimentationProvider';
import { disabledEvaluation } from '../client/ExperimentationClient';
import { Seed } from '../client/bootstrap';
import { FeatureFlagEvaluation } from '../client/types';

/**
 * Evaluates multiple feature flags in parallel.
 *
 * Returns a map of flagKey -> FeatureFlagEvaluation (or null during initial load).
 * Flags start as null and are populated once evaluation completes; a flag the
 * Provider's `bootstrap` has starts from its answer instead (`loading: false`),
 * and keeps it, with `error` set, if its request fails.
 * Re-evaluates when the flagKeys array changes (by JSON comparison).
 */
export function useMultipleFlags(
  flagKeys: string[]
): Record<string, FeatureFlagEvaluation | null> {
  const { client, user } = useExperimentation();
  const bootstrap = useBootstrap();

  // Serialise keys to a stable string so useEffect detects changes correctly
  const keysJson = JSON.stringify(flagKeys);

  const [seed] = useState<Seed<ReadonlyMap<string, FeatureFlagEvaluation>> | null>(() => {
    const value = new Map<string, FeatureFlagEvaluation>();
    for (const key of flagKeys) {
      const answer = bootstrap.flags.get(key);
      if (answer) value.set(key, answer);
    }
    return value.size > 0 ? { key: keysJson, user, value } : null;
  });
  const [state, setState] = useState<Record<string, FeatureFlagEvaluation | null>>(
    () => Object.fromEntries(flagKeys.map(k => [k, seed?.value.get(k) ?? null]))
  );
  // The bootstrapped answers while the hook still shows them; cleared by its
  // own answers and by a change of keys or user.
  const seeded = useRef(seed);

  useEffect(() => {
    const keys: string[] = JSON.parse(keysJson);

    if (keys.length === 0) {
      setState({});
      return;
    }

    const kept =
      seeded.current && seeded.current.key === keysJson && seeded.current.user === user
        ? seeded.current.value
        : null;
    if (!kept) {
      seeded.current = null;
      // Reset to null for all keys while loading
      setState(Object.fromEntries(keys.map(k => [k, null])));
    }

    let cancelled = false;

    Promise.all(
      keys.map(async (flagKey): Promise<[string, FeatureFlagEvaluation]> => {
        try {
          return [flagKey, await client.evaluateFeatureFlagDetailed(user, flagKey)];
        } catch (err) {
          const error = err instanceof Error ? err : new Error(String(err));
          const started = kept?.get(flagKey);
          return [flagKey, started ? { ...started, error } : disabledEvaluation(flagKey, { error })];
        }
      })
    ).then(entries => {
      if (!cancelled) {
        seeded.current = null;
        setState(Object.fromEntries(entries));
      }
    });

    return () => {
      cancelled = true;
    };
  }, [client, user, keysJson]);

  return state;
}
