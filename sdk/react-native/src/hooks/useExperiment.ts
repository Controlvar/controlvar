/**
 * useExperiment — assigns the current user to an experiment (sticky, decided by the server).
 *
 * @example
 * ```tsx
 * function CheckoutPage() {
 *   const { variant, configuration, loading } = useExperiment('checkout-experiment');
 *   if (loading) return <ActivityIndicator />;
 *   return variant === 'treatment' ? <NewCheckout /> : <OldCheckout />;
 * }
 * ```
 */

import { useState, useEffect } from 'react';
import { useExperimentationContext } from '../context/ExperimentationContext';
import type { ExperimentState } from '../types';

function initialState(experimentKey: string, loading: boolean): ExperimentState {
  return {
    experimentKey,
    variant: null,
    variantId: null,
    variantName: null,
    isControl: false,
    configuration: null,
    loading,
    error: null,
  };
}

/**
 * Assigns the current user to `experimentKey` via `POST /api/v1/tracking/assign`;
 * the provider's `attributes` are sent as `context`.
 *
 * @param experimentKey  The experiment key to retrieve an assignment for.
 * @returns `{ experimentKey, variant, variantId, variantName, isControl, configuration, loading, error }`
 *   — `variant` (alias of `variantName`) is `null` while loading or when the
 *   assignment failed (no experiment has the key, network error with nothing persisted).
 */
export function useExperiment(experimentKey: string): ExperimentState {
  const { client, userId, attributes } = useExperimentationContext();

  const [state, setState] = useState<ExperimentState>(() => initialState(experimentKey, true));

  useEffect(() => {
    let cancelled = false;

    setState(initialState(experimentKey, true));

    client
      .getAssignment(experimentKey, userId, attributes)
      .then((assignment) => {
        if (cancelled) return;
        if (!assignment) {
          setState(initialState(experimentKey, false));
          return;
        }
        setState({
          experimentKey: assignment.experimentKey,
          variant: assignment.variantName,
          variantId: assignment.variantId,
          variantName: assignment.variantName,
          isControl: assignment.isControl,
          configuration: assignment.configuration,
          loading: false,
          error: null,
        });
      })
      .catch((error: Error) => {
        if (!cancelled) {
          setState({ ...initialState(experimentKey, false), error });
        }
      });

    return () => {
      cancelled = true;
    };
    // Attributes are intentionally not a dependency: assignment is sticky per user.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, userId, experimentKey]);

  return state;
}
