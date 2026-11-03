/**
 * The guard around `onExposure` (#1499). Nothing here is exported from the
 * package: the client uses it, and its tests call it directly.
 */

import type { Exposure } from './types';

/** The refusal the constructor throws for an `onExposure` that is not a function. */
export const ON_EXPOSURE_REFUSAL = 'ExperimentationClient: onExposure must be a function';

/**
 * `onExposure` as given, or `null` for none. Throws {@link ON_EXPOSURE_REFUSAL}
 * for any value other than a function, `null` or `undefined`.
 */
export function checkOnExposure(value: unknown): ((exposure: Exposure) => unknown) | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'function') throw new Error(ON_EXPOSURE_REFUSAL);
  return value as (exposure: Exposure) => unknown;
}

/**
 * Call `hook` with `exposure`, and pass anything it throws, or any rejection
 * of a thenable it returns, to `report`. Nothing reaches the caller: a hook's
 * failure must never change or delay an answer. A returned thenable is not
 * awaited; without the rejection handler attached here, an async hook that
 * rejects would be an unhandled rejection. Anything `report` itself throws is
 * dropped.
 */
export function callExposureHook(
  hook: (exposure: Exposure) => unknown,
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

/**
 * The error `onError` receives, with the operation `'exposure'`, for what a
 * hook threw or rejected with: the value itself when it is an `Error`,
 * otherwise an `Error` whose message is the value as a string. This SDK has no
 * error codes; the operation is what marks a hook's failure.
 */
export function exposureError(thrown: unknown): Error {
  if (thrown instanceof Error) return thrown;
  let message: string;
  try {
    message = String(thrown);
  } catch {
    message = 'onExposure threw a value that cannot be turned into a string';
  }
  return new Error(message);
}
