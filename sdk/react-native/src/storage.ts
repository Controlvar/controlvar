/**
 * AsyncStorage wrapper for persisting server results per user + key.
 *
 * Each entry is stored as `{ value, expiresAt }` so it can act both as a
 * TTL-bounded cache that survives app restarts and — past its TTL — as the
 * last-known value served when the API is unreachable (offline fallback).
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import type { Assignment, CacheEntry, FlagEvaluation } from './types';

const FLAG_PREFIX = 'ep_sdk_flag:';
const ASSIGNMENT_PREFIX = 'ep_sdk_asgn:';

/**
 * How the AsyncStorage the app installed removes several keys at once:
 * `multiRemove` in 1.x and 2.x, `removeMany` from 3.0, whose default export
 * has no `multiRemove`. The peer range (`>=1.21.0 <4`) allows all three, so
 * the SDK reads the default export as one of these shapes, which type-checks
 * against each major's declarations, and calls the method that exists.
 */
type BulkRemove =
  | { multiRemove(keys: string[]): Promise<void> }
  | { removeMany(keys: string[]): Promise<void> };

async function removeKeys(keys: string[]): Promise<void> {
  // An assertion, not an annotation: an annotated `const` would be narrowed
  // to the one shape the installed declarations match, and the other branch
  // would not type-check. The assertion still fails to compile against a
  // default export that has neither method.
  const storage = AsyncStorage as BulkRemove;
  if ('multiRemove' in storage) {
    await storage.multiRemove(keys);
  } else {
    await storage.removeMany(keys);
  }
}

function storageKey(prefix: string, userId: string, key: string): string {
  return `${prefix}${encodeURIComponent(userId)}:${encodeURIComponent(key)}`;
}

/**
 * Persists and retrieves flag evaluations / assignments using AsyncStorage.
 *
 * Keys are namespaced with `ep_sdk_flag:` or `ep_sdk_asgn:` (followed by the
 * URL-encoded user id and key) to avoid collisions with other AsyncStorage
 * users in the host app. Every method is best-effort and never throws.
 */
export class OfflineStorage {
  /** Persists a flag evaluation with its expiry. */
  async setFlag(userId: string, flagKey: string, value: FlagEvaluation, ttlMs: number): Promise<void> {
    await this.write(storageKey(FLAG_PREFIX, userId, flagKey), value, ttlMs);
  }

  /** Retrieves a persisted flag evaluation (expired or not), or `null` if not stored. */
  async getFlag(userId: string, flagKey: string): Promise<CacheEntry<FlagEvaluation> | null> {
    const entry = await this.read<FlagEvaluation>(storageKey(FLAG_PREFIX, userId, flagKey));
    return entry && typeof entry.value?.enabled === 'boolean' ? entry : null;
  }

  /** Removes a persisted flag evaluation (a deleted flag must not be served again). Never throws. */
  async removeFlag(userId: string, flagKey: string): Promise<void> {
    try {
      await AsyncStorage.removeItem(storageKey(FLAG_PREFIX, userId, flagKey));
    } catch {
      // Best-effort — never throw
    }
  }

  /** Persists an experiment assignment with its expiry. */
  async setAssignment(userId: string, experimentKey: string, value: Assignment, ttlMs: number): Promise<void> {
    await this.write(storageKey(ASSIGNMENT_PREFIX, userId, experimentKey), value, ttlMs);
  }

  /** Retrieves a persisted assignment (expired or not), or `null` if not stored. */
  async getAssignment(userId: string, experimentKey: string): Promise<CacheEntry<Assignment> | null> {
    const entry = await this.read<Assignment>(storageKey(ASSIGNMENT_PREFIX, userId, experimentKey));
    return entry && typeof entry.value?.variantName === 'string' ? entry : null;
  }

  /** Removes all SDK-namespaced entries from AsyncStorage. */
  async clear(): Promise<void> {
    try {
      const allKeys = await AsyncStorage.getAllKeys();
      const sdkKeys = allKeys.filter(
        (k) => k.startsWith(FLAG_PREFIX) || k.startsWith(ASSIGNMENT_PREFIX)
      );
      if (sdkKeys.length > 0) {
        await removeKeys(sdkKeys);
      }
    } catch {
      // Best-effort
    }
  }

  // ---------------------------------------------------------------------------

  private async write(key: string, value: unknown, ttlMs: number): Promise<void> {
    try {
      const entry: CacheEntry<unknown> = { value, expiresAt: Date.now() + ttlMs };
      await AsyncStorage.setItem(key, JSON.stringify(entry));
    } catch {
      // Best-effort — never throw
    }
  }

  private async read<T>(key: string): Promise<CacheEntry<T> | null> {
    try {
      const raw = await AsyncStorage.getItem(key);
      if (raw === null) return null;
      const parsed = JSON.parse(raw) as Partial<CacheEntry<T>> | null;
      if (!parsed || typeof parsed !== 'object' || typeof parsed.expiresAt !== 'number') return null;
      return { value: parsed.value as T, expiresAt: parsed.expiresAt };
    } catch {
      return null;
    }
  }
}
