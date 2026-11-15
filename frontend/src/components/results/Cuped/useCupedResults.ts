import { useCallback, useEffect, useRef, useState } from 'react';
import { ResultsService } from '@/services/results';
import { ApiError } from '@/services/api';
import { CupedResultsResponse } from '@/types/results';

/** Fixed copy for a failed CUPED load: the server's text is never shown. */
export const CUPED_FORBIDDEN = 'You do not have access to these results.';
export const CUPED_NOT_FOUND = 'No results were found for this experiment.';
export const CUPED_SERVER_ERROR =
  'The server could not compute the CUPED results. Try again; if it keeps failing, check the API logs.';
export const CUPED_LOAD_FAILED = 'The CUPED results could not be loaded.';

/**
 * A 5xx body can carry a stack trace and a 4xx detail can be out of date, so
 * neither is shown. An unreachable API (status 0) keeps its own message, which
 * the dashboard writes itself.
 */
export function cupedErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 0) return e.message;
    if (e.status === 404) return CUPED_NOT_FOUND;
    if (e.status === 403) return CUPED_FORBIDDEN;
    if (e.status >= 500) return CUPED_SERVER_ERROR;
  }
  return CUPED_LOAD_FAILED;
}

export type CupedState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; data: CupedResultsResponse }
  | { status: 'error'; message: string };

/**
 * GET /results/{id}/cuped, asked the first time `enabled` becomes true (the
 * tab opens) and never again until `retry`: the answer stays here, so
 * switching to another tab and back sends nothing. Every call of the route
 * writes an analysis snapshot, which is why nothing asks it before it is wanted.
 */
export function useCupedResults(experimentId: string, enabled: boolean) {
  const [state, setState] = useState<CupedState>({ status: 'idle' });
  const request = useRef(0);
  const askedFor = useRef<string | null>(null);

  const load = useCallback(async () => {
    // Only the latest request may write state.
    const mine = ++request.current;
    setState({ status: 'loading' });
    try {
      const data = await ResultsService.getCupedResults(experimentId);
      if (mine === request.current) setState({ status: 'ready', data });
    } catch (e) {
      if (mine === request.current) setState({ status: 'error', message: cupedErrorMessage(e) });
    }
  }, [experimentId]);

  useEffect(() => {
    if (!enabled || askedFor.current === experimentId) return;
    askedFor.current = experimentId;
    void load();
  }, [enabled, experimentId, load]);

  return { state, retry: load };
}
