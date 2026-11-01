export type ExperimentationErrorCode =
  /** The server answered with a non-2xx status; see `status` and `body`. */
  | 'HTTP_ERROR'
  /** `fetch` rejected (DNS, connection refused, CORS, ...). */
  | 'NETWORK_ERROR'
  /** The request exceeded `timeoutMs`. */
  | 'TIMEOUT'
  /** The server answered 2xx but the body was not what the SDK expects. */
  | 'INVALID_RESPONSE'
  /**
   * A typed getter (`getStringValue`, `getNumberValue`, `getJsonValue`) was called for a flag
   * that serves another type; it returned the caller's default.
   */
  | 'TYPE_MISMATCH';

export interface ExperimentationErrorOptions {
  code: ExperimentationErrorCode;
  status?: number;
  body?: unknown;
  cause?: unknown;
}

/**
 * Error thrown by `getAssignment`, `evaluateFlag`, `getAllFlags` and `fetchAssignments`, and
 * passed to `onError` for the failures the other methods swallow.
 */
export class ExperimentationError extends Error {
  readonly code: ExperimentationErrorCode;
  /** HTTP status code when the server answered; `undefined` for network errors and timeouts. */
  readonly status: number | undefined;
  /** Parsed JSON error body when available (e.g. `{detail: "..."}`). */
  readonly body: unknown;
  readonly cause: unknown;

  constructor(message: string, options: ExperimentationErrorOptions) {
    super(message);
    this.name = 'ExperimentationError';
    this.code = options.code;
    this.status = options.status;
    this.body = options.body;
    this.cause = options.cause;
  }
}

/** Wrap anything thrown inside the SDK into an `ExperimentationError`. */
export function asExperimentationError(err: unknown): ExperimentationError {
  if (err instanceof ExperimentationError) return err;
  const message = err instanceof Error ? err.message : String(err);
  return new ExperimentationError(message, { code: 'NETWORK_ERROR', cause: err });
}
