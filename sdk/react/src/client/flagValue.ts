import type { FeatureFlagEvaluation } from './types';

/** The value types `useFlagValue` reads. A boolean flag is read with `useFeatureFlag`. */
export type FlagValueType = 'string' | 'number' | 'json';

/**
 * Why `useFlagValue` answered with its default (or, after a bootstrap, with the bootstrapped
 * value) instead of a value the server served:
 *
 * - `TYPE_MISMATCH`: the flag serves another type than the one asked for.
 * - `INVALID_RESPONSE`: the server answered without a value of that type: a server without flag
 *   values, one that cannot serve the flag's stored value, or a value that is not of the type the
 *   answer names.
 * - `REQUEST_FAILED`: the request failed (a network error, a timeout, or a non-2xx status: a flag
 *   key the server does not know is a 404); `cause` is the request's error.
 */
export type FlagValueErrorCode = 'TYPE_MISMATCH' | 'INVALID_RESPONSE' | 'REQUEST_FAILED';

/** `useFlagValue`'s `error`: why it did not answer with a value the server served. */
export class FlagValueError extends Error {
  readonly code: FlagValueErrorCode;
  /** The flag the hook asked for. */
  readonly flagKey: string;
  /** For `REQUEST_FAILED`, the request's error; otherwise `undefined`. */
  readonly cause: unknown;

  constructor(message: string, code: FlagValueErrorCode, flagKey: string, cause?: unknown) {
    super(message);
    this.name = 'FlagValueError';
    this.code = code;
    this.flagKey = flagKey;
    this.cause = cause;
  }
}

/** The largest integer a JSON number carries exactly in every SDK: 2^53 - 1. */
const MAX_SAFE_INTEGER = 9007199254740991;

/**
 * Whether `value` is a value of `type` as the server serves one: a string; a number that is
 * finite and within ±(2^53−1), never a boolean; a JSON object or array.
 */
function isValueOfType(value: unknown, type: FlagValueType): boolean {
  switch (type) {
    case 'string':
      return typeof value === 'string';
    case 'number':
      return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= MAX_SAFE_INTEGER;
    case 'json':
      return value !== null && typeof value === 'object';
    default:
      return false;
  }
}

/** The JSON type of a value the server sent without a `value_type`. */
function jsonTypeOf(value: unknown): string {
  return value !== null && typeof value === 'object' ? 'json' : typeof value;
}

/** A value type as the error messages name it. */
function describeType(type: string): string {
  switch (type) {
    case 'boolean':
      return 'a boolean';
    case 'string':
      return 'a string';
    case 'number':
      return 'a number';
    case 'json':
      return 'a JSON value';
    default:
      return `a value of type ${JSON.stringify(type.slice(0, 64))}`;
  }
}

/**
 * A copy of a JSON value that shares nothing with it: an object or array is copied deeply by a
 * JSON round trip (it came from JSON, so nothing is lost); anything else is returned as it is.
 */
function copyValue<T>(value: T): T {
  return value !== null && typeof value === 'object' ? (JSON.parse(JSON.stringify(value)) as T) : value;
}

/** What `evaluation` gives a reader of `type`: the value it holds, or why there is none. */
export interface FlagValueRead {
  /** `true` when `value` is a value the server served, of the type asked for. */
  readonly found: boolean;
  /** The value when `found` (a JSON value is a new copy); `undefined` otherwise. */
  readonly value: unknown;
  /** `null` when `found` and the request did not fail. */
  readonly error: FlagValueError | null;
}

/**
 * The value `evaluation` holds for a reader of `type`, read as every Controlvar SDK reads one
 * (`tests/sdk-contract/value-vectors.json`):
 *
 * 1. no value -> not found, `INVALID_RESPONSE`;
 * 2. the served type (`valueType`, else the value's JSON type) is not `type` -> `TYPE_MISMATCH`;
 * 3. the value is not a value of `type` -> `INVALID_RESPONSE`;
 * 4. otherwise the value, a JSON object or array as a new copy.
 *
 * An evaluation whose request failed (`evaluation.error`) gives `REQUEST_FAILED`, with the value
 * it still holds (a bootstrapped answer the hook kept) when that value is readable.
 */
export function readFlagValue(
  evaluation: Pick<FeatureFlagEvaluation, 'flagKey' | 'value' | 'valueType' | 'error'>,
  type: FlagValueType
): FlagValueRead {
  const { flagKey, value, valueType } = evaluation;
  const flag = JSON.stringify(String(flagKey));
  let found = false;
  let error: FlagValueError | null = null;
  if (value === undefined || value === null) {
    error = new FlagValueError(
      `Flag ${flag} came back without a value (from a server without flag values, or one that ` +
        `cannot serve this flag's stored value); useFlagValue returned your default`,
      'INVALID_RESPONSE',
      flagKey
    );
  } else {
    const served = typeof valueType === 'string' ? valueType : jsonTypeOf(value);
    if (served !== type) {
      error = new FlagValueError(
        `Flag ${flag} serves ${describeType(served)}; useFlagValue returned your default`,
        'TYPE_MISMATCH',
        flagKey
      );
    } else if (!isValueOfType(value, type)) {
      error = new FlagValueError(
        `Flag ${flag} serves ${describeType(type)}, but the server's value is not one; ` +
          `useFlagValue returned your default`,
        'INVALID_RESPONSE',
        flagKey
      );
    } else {
      found = true;
    }
  }
  if (evaluation.error) {
    const cause = evaluation.error;
    error = new FlagValueError(
      `The request for flag ${flag} failed (${cause.message})`,
      'REQUEST_FAILED',
      flagKey,
      cause
    );
  }
  return { found, value: found ? copyValue(value) : undefined, error };
}
