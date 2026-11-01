import { ExperimentationError } from './errors';
import type { Exposure } from './types';

/**
 * Call `hook` with `exposure`, and pass anything it throws, or any rejection of a thenable it
 * returns, to `report`. Nothing reaches the caller: a hook's failure must never change or delay
 * an answer. A returned thenable is not awaited; without the rejection handler attached here, an
 * async hook that rejects would end a Node process (unhandled rejection). Anything `report`
 * itself throws is dropped.
 */
export function callExposureHook(
  hook: (exposure: Exposure) => void,
  exposure: Exposure,
  report: (thrown: unknown) => void
): void {
  const reportSafely = (thrown: unknown): void => {
    try {
      report(thrown);
    } catch {
      // a throwing report must not reach the caller, or reject a promise nobody handles
    }
  };
  try {
    const result: unknown = hook(exposure);
    if (result !== null && (typeof result === 'object' || typeof result === 'function')) {
      // Read `then` once, inside the try: a getter that throws is the hook's failure too.
      const then = (result as { then?: unknown }).then;
      if (typeof then === 'function') then.call(result, undefined, reportSafely);
    }
  } catch (thrown) {
    reportSafely(thrown);
  }
}

/** The error `onError` receives, with the operation `'exposure'`, for what a hook threw. */
export function exposureError(thrown: unknown): ExperimentationError {
  let message: string;
  try {
    message = thrown instanceof Error ? String(thrown.message) : String(thrown);
  } catch {
    message = 'it threw a value that cannot be turned into a string';
  }
  return new ExperimentationError(`onExposure failed: ${message}`, { code: 'HOOK_ERROR', cause: thrown });
}
