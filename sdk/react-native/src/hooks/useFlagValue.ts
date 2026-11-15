/**
 * useFlagValue — reads the value a string, number or JSON flag serves the
 * current user (decided by the server).
 *
 * @example
 * ```tsx
 * function Banner() {
 *   const { value: headline, loading } = useFlagValue('headline', 'string', 'Welcome');
 *   if (loading) return <ActivityIndicator />;
 *   return <Text>{headline}</Text>;
 * }
 * ```
 */

import { useEffect, useState } from 'react';
import { useExperimentationContext } from '../context/ExperimentationContext';
import { asError, readFlagValue, readValueOf, requestFailed } from '../client';
import type { ExperimentationClient, FlagValueError, FlagValueRead, FlagValueType } from '../client';
import type { FlagEvaluation } from '../types';

/** What `useFlagValue` returns. */
export interface FlagValueResult<T> {
  /**
   * The value the flag serves the user (its on value when the flag is on for
   * them, its off value otherwise), or `defaultValue` while loading and
   * whenever there is none to read (`error` says why). A JSON value is this
   * hook's own copy: changing it changes nothing another reader gets.
   */
  value: T;
  /** `true` until the client answers. */
  loading: boolean;
  /** Why `value` is not a value the server served (see `FlagValueError`); `null` otherwise. */
  error: FlagValueError | null;
}

/**
 * The read for one answer: through the client's typed read, or, for a client this copy of the
 * SDK did not build (a test's plain-object mock of `evaluateFlag`, as the SDK page's "Mocking the
 * client" shows, or a client from another installed copy), through its `evaluateFlag` and the
 * same reader, a rejection or a throw becoming `REQUEST_FAILED`. Never rejects.
 */
function read(
  client: ExperimentationClient,
  flagKey: string,
  userId: string,
  type: FlagValueType,
  attributes: Record<string, unknown> | undefined
): Promise<FlagValueRead> {
  if (typeof client[readFlagValue] === 'function') {
    return client[readFlagValue](flagKey, userId, type, attributes, 'useFlagValue');
  }
  return Promise.resolve()
    .then(() => client.evaluateFlag(flagKey, userId, attributes))
    .then((evaluation: FlagEvaluation) => readValueOf(evaluation, type, flagKey, 'useFlagValue'))
    .catch((err: unknown) => ({ found: false, value: undefined, error: requestFailed(flagKey, asError(err)) }));
}

/** One answer, and the client, user, flag and type it was read for. */
interface Answered {
  client: ExperimentationClient;
  userId: string;
  flagKey: string;
  type: FlagValueType;
  read: FlagValueRead;
}

/**
 * Reads the value flag `flagKey` serves the `userId` of the enclosing
 * {@link ExperimentationProvider}, through the client's typed read: the same
 * request, cache and AsyncStorage entry as `useFlag` and `evaluateFlag`.
 * `type` is the type the flag serves: `'string'`, `'number'` or `'json'` (a
 * JSON object or array, checked to be one, not to match `T`).
 *
 * Starts as `{ value: defaultValue, loading: true, error: null }`. It asks
 * again only when the client, the user, the flag key or `type` changes, never
 * because `defaultValue` did (an inline object is fine): the default is read
 * at render. Until the answer for the current client, user, flag and type
 * arrives it returns the loading state, never another user's or flag's value.
 * Never throws: it answers `defaultValue` and sets `error` when the request
 * fails (`REQUEST_FAILED`, with the request's error as `cause`), the flag
 * serves another type (`TYPE_MISMATCH`) or the server sends no value of it
 * (`INVALID_RESPONSE`), and calls the client's `onError` once with `'value'`.
 * When the request fails and the offline fallback holds a value of this type,
 * it answers that value, with `error` set. Reading a value records no
 * exposure. Under a mocked client (a plain object whose `evaluateFlag` is
 * stubbed) it reads the `value` and `valueType` that `evaluateFlag` resolves
 * to, by the same rules; a rejection is `REQUEST_FAILED`.
 *
 * @param attributes  Sent as `context`, so targeting rules match. When
 *   omitted, the provider's `attributes` are sent (no merge). A change of
 *   attributes does not re-run the hook: see `ExperimentationClient.evaluateFlag`.
 */
export function useFlagValue(
  flagKey: string,
  type: 'string',
  defaultValue: string,
  attributes?: Record<string, unknown>
): FlagValueResult<string>;
export function useFlagValue(
  flagKey: string,
  type: 'number',
  defaultValue: number,
  attributes?: Record<string, unknown>
): FlagValueResult<number>;
export function useFlagValue<T = unknown>(
  flagKey: string,
  type: 'json',
  defaultValue: T,
  attributes?: Record<string, unknown>
): FlagValueResult<T>;
export function useFlagValue(
  flagKey: string,
  type: FlagValueType,
  defaultValue: unknown,
  attributes?: Record<string, unknown>
): FlagValueResult<unknown> {
  const { client, userId, attributes: providerAttributes } = useExperimentationContext();
  const [answered, setAnswered] = useState<Answered | null>(null);

  useEffect(() => {
    let cancelled = false;
    read(client, flagKey, userId, type, attributes ?? providerAttributes).then((answer) => {
      if (!cancelled) setAnswered({ client, userId, flagKey, type, read: answer });
    });
    return () => {
      cancelled = true;
    };
    // Re-read when client, userId, flagKey or type changes. Neither `defaultValue`
    // nor the attributes are dependencies (an inline value is new on every render);
    // the cache is keyed by user + flag, as for useFlag.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, userId, flagKey, type]);

  const current =
    answered !== null &&
    answered.client === client &&
    answered.userId === userId &&
    answered.flagKey === flagKey &&
    answered.type === type
      ? answered.read
      : null;
  if (current === null) return { value: defaultValue, loading: true, error: null };
  // The default is read here, at render: it is never a dependency of the request.
  return { value: current.found ? current.value : defaultValue, loading: false, error: current.error };
}
