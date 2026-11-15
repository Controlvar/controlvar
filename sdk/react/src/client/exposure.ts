import type { Exposure } from './types';

/** How the message of every {@link ExposureHookError} starts. */
export const EXPOSURE_ERROR_PREFIX = 'onExposure failed: ';

/**
 * This SDK's kind for a failure in `onExposure`, as `HOOK_ERROR` is the
 * JavaScript SDK's: its `code` is `'HOOK_ERROR'`, its message starts with
 * `onExposure failed: `, and its `cause` is what the hook threw or rejected
 * with. It is never a request error. Not exported from the package: this SDK
 * has no error channel, so the client drops it (see `callExposureHook`).
 */
export class ExposureHookError extends Error {
  readonly code = 'HOOK_ERROR' as const;
  readonly cause: unknown;

  constructor(message: string, cause: unknown) {
    super(message);
    this.name = 'ExposureHookError';
    this.cause = cause;
  }
}

/** The hook error for what `onExposure` threw, or the reason its Promise rejected. */
export function exposureError(thrown: unknown): ExposureHookError {
  let message: string;
  try {
    message = thrown instanceof Error ? String(thrown.message) : String(thrown);
  } catch {
    message = 'it threw a value that cannot be turned into a string';
  }
  return new ExposureHookError(`${EXPOSURE_ERROR_PREFIX}${message}`, thrown);
}

/**
 * Call `hook` with `exposure`, and pass a hook error for anything it throws,
 * or for any rejection of a thenable it returns, to `report`. Nothing reaches
 * the caller: a hook's failure must never change or delay an answer. A
 * returned thenable is not awaited; without the rejection handler attached
 * here, an async hook that rejects would end a Node process (unhandled
 * rejection). Anything `report` itself throws is dropped. The clients pass
 * {@link dropExposureError}: this SDK reports the failure nowhere.
 */
export function callExposureHook(
  hook: (exposure: Exposure) => unknown,
  exposure: Exposure,
  report: (error: ExposureHookError) => void
): void {
  const fail = (thrown: unknown): void => {
    try {
      report(exposureError(thrown));
    } catch {
      // a throwing report must not reach the caller, or reject a promise nobody handles
    }
  };
  try {
    const result: unknown = hook(exposure);
    if (result !== null && (typeof result === 'object' || typeof result === 'function')) {
      // Read `then` once, inside the try: a getter that throws is the hook's failure too.
      const then = (result as { then?: unknown }).then;
      if (typeof then === 'function') then.call(result, undefined, fail);
    }
  } catch (thrown) {
    fail(thrown);
  }
}

/** The `report` the clients give {@link callExposureHook}: this SDK has no `onError`. */
export function dropExposureError(_error: ExposureHookError): void {
  // Intentionally empty: a failing onExposure leaves the answer unchanged and is reported nowhere.
}

/**
 * `onExposure` as given, or `null` for none. Throws `onExposure must be a
 * function` for any value other than a function, `null` or `undefined`.
 */
export function checkOnExposure(value: unknown): ((exposure: Exposure) => unknown) | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'function') throw new Error('onExposure must be a function');
  return value as (exposure: Exposure) => unknown;
}
