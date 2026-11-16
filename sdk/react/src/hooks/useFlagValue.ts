import { useMemo } from 'react';
import { useFeatureFlag } from './useFeatureFlag';
import { FlagValueError, FlagValueType, readFlagValue } from '../client/flagValue';

/** What `useFlagValue` returns. */
export interface FlagValueResult<T> {
  /**
   * The value the flag serves the user (its on value when the flag is on for them, its off value
   * otherwise), or `defaultValue` while loading and whenever there is none to read (`error` says
   * why). A JSON value is this hook's own copy: changing it changes nothing another reader gets.
   */
  value: T;
  /** `true` until the server answers, unless the Provider's bootstrap has the flag. */
  loading: boolean;
  /** Why `value` is not a value the server served (see `FlagValueError`); `null` otherwise. */
  error: FlagValueError | null;
}

/**
 * Reads the value a string, number or JSON flag serves the current user, as the server decides
 * it. `type` is the type the flag serves: `'string'`, `'number'` or `'json'` (a JSON object or
 * array, checked to be one, not to match `T`).
 *
 * Starts as `{ value: defaultValue, loading: true, error: null }`, or, when the Provider's
 * `bootstrap` has this flag, as its value with `loading: false` (on the server and in the
 * browser, with no request). It shares `useFeatureFlag`'s request and cache: one request answers
 * both hooks for the same flag and user, and it asks again only when the flag key or the user
 * changes, never because `defaultValue` did (an inline object is fine). Never throws: it answers
 * `defaultValue` and sets `error` when the request fails, the flag serves another type
 * (`TYPE_MISMATCH`) or the server sends no value of it (`INVALID_RESPONSE`). A failed refresh of
 * a bootstrapped value keeps that value, with `error` set. Reading a value records no exposure.
 */
export function useFlagValue(flagKey: string, type: 'string', defaultValue: string): FlagValueResult<string>;
export function useFlagValue(flagKey: string, type: 'number', defaultValue: number): FlagValueResult<number>;
export function useFlagValue<T = unknown>(flagKey: string, type: 'json', defaultValue: T): FlagValueResult<T>;
export function useFlagValue(
  flagKey: string,
  type: FlagValueType,
  defaultValue: unknown
): FlagValueResult<unknown> {
  const evaluation = useFeatureFlag(flagKey);
  // Read once per answer and type, so `value` (a JSON copy) and `error` keep their identity
  // across re-renders. Until the answer for this key arrives there is nothing to read: right
  // after a key change the evaluation is still the previous key's for one render.
  const read = useMemo(
    () => (evaluation.loading || evaluation.flagKey !== flagKey ? null : readFlagValue(evaluation, type)),
    [evaluation, flagKey, type]
  );
  if (read === null) return { value: defaultValue, loading: true, error: null };
  // The default is read here, at render: it is never a dependency of the request.
  return { value: read.found ? read.value : defaultValue, loading: false, error: read.error };
}
