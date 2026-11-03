import { useState, useEffect, useRef } from 'react';
import { useBootstrap, useExperimentation } from '../context/ExperimentationProvider';
import { defaultAssignment } from '../client/ExperimentationClient';
import { Seed } from '../client/bootstrap';
import { ExperimentAssignment } from '../client/types';

/**
 * Assigns the current user to an experiment via `POST /api/v1/tracking/assign`.
 *
 * While loading or on error the control defaults are returned
 * (`variantKey: 'control'`, `variantName: 'Control'`, `variantId: null`,
 * `isControl: true`, `configuration: null`). When the Provider's `bootstrap`
 * has this experiment, it starts from that assignment with `loading: false`
 * instead, then asks the server as always; if that request fails it keeps the
 * bootstrapped assignment, with `error` set. Re-assigns when the experiment
 * key or user changes (the server keeps assignments sticky).
 */
export function useExperiment(experimentKey: string): ExperimentAssignment {
  const { client, user } = useExperimentation();
  const bootstrap = useBootstrap();
  const [seed] = useState<Seed<ExperimentAssignment> | null>(() => {
    const value = bootstrap.experiments.get(experimentKey);
    return value ? { key: experimentKey, user, value } : null;
  });
  const [state, setState] = useState<ExperimentAssignment>(
    () => seed?.value ?? defaultAssignment(experimentKey, { loading: true })
  );
  // The bootstrapped assignment while the hook still shows it; cleared by its
  // own answer and by a change of key or user.
  const seeded = useRef(seed);

  useEffect(() => {
    let cancelled = false;
    const kept =
      seeded.current && seeded.current.key === experimentKey && seeded.current.user === user
        ? seeded.current.value
        : null;
    if (!kept) {
      seeded.current = null;
      setState(defaultAssignment(experimentKey, { loading: true }));
    }

    client
      .assignExperiment(user, experimentKey)
      .then(assignment => {
        if (!cancelled) {
          seeded.current = null;
          setState(assignment);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const error = err instanceof Error ? err : new Error(String(err));
          setState(kept ? { ...kept, error } : defaultAssignment(experimentKey, { error }));
        }
      });

    return () => {
      cancelled = true;
    };
  }, [client, user, experimentKey]);

  return state;
}
