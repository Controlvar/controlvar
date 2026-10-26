/**
 * The keys a key-less `track()` is sent to, kept apart from the TTL value cache.
 *
 * Every successful answer (an assignment, `assigned: false` included, or a flag
 * evaluation, local or from the server) records its key for the user. A key-less
 * event goes to each key recorded for its user, so a purchase made after the
 * cache entry expired still reaches the experiment the user was answered for.
 * `clearCache()` does not touch this record: a user's sticky assignments do not
 * change when their attributes do.
 */

/** Users kept per client instance; the user answered least recently is dropped first. */
export const MAX_ATTRIBUTED_USERS = 10_000;

/** A key is used for this long after its latest successful answer, then dropped. */
export const ATTRIBUTION_HORIZON_MS = 24 * 60 * 60 * 1000;

/**
 * An experiment whose latest answer gave one of these reasons is not running, so a
 * key-less event is not sent to it. Other reasons (`holdout`, `mutual_exclusion`,
 * `targeting`) are kept: holdout results count those users' events.
 */
export const LIFECYCLE_REASONS: ReadonlySet<string> = new Set(['draft', 'paused', 'completed', 'archived']);

interface Answer {
  answeredAt: number;
  reason?: string;
}

interface UserKeys {
  experiments: Map<string, Answer>;
  flags: Map<string, Answer>;
}

export interface AttributedKeyList {
  experiments: string[];
  flags: string[];
}

export class AttributedKeys {
  /** Map order is answer order: the first user is the one answered least recently. */
  private readonly users = new Map<string, UserKeys>();

  recordExperiment(userId: string, experimentKey: string, reason?: string): void {
    this.record(userId, experimentKey, reason, 'experiments');
  }

  recordFlag(userId: string, flagKey: string): void {
    this.record(userId, flagKey, undefined, 'flags');
  }

  /**
   * The user's keys within the horizon, experiments then flags, each in the order
   * they were first answered. Keys past the horizon are dropped here.
   */
  keys(userId: string): AttributedKeyList {
    const keys = this.users.get(userId);
    if (!keys) return { experiments: [], flags: [] };
    const now = Date.now();
    const list = { experiments: live(keys.experiments, now), flags: live(keys.flags, now) };
    if (keys.experiments.size === 0 && keys.flags.size === 0) this.users.delete(userId);
    return list;
  }

  /** How many users are recorded. */
  get size(): number {
    return this.users.size;
  }

  private record(userId: string, key: string, reason: string | undefined, kind: keyof UserKeys): void {
    let keys = this.users.get(userId);
    if (keys) {
      this.users.delete(userId); // re-inserted below, as the most recently answered user
    } else {
      keys = { experiments: new Map(), flags: new Map() };
    }
    // `set` on an existing key keeps its place, so the order stays first-answer order.
    keys[kind].set(key, { answeredAt: Date.now(), reason });
    this.users.set(userId, keys);
    while (this.users.size > MAX_ATTRIBUTED_USERS) {
      const oldest = this.users.keys().next().value as string;
      this.users.delete(oldest);
    }
  }
}

function live(answers: Map<string, Answer>, now: number): string[] {
  const out: string[] = [];
  for (const [key, answer] of answers) {
    if (now - answer.answeredAt > ATTRIBUTION_HORIZON_MS) {
      answers.delete(key);
    } else if (answer.reason === undefined || !LIFECYCLE_REASONS.has(answer.reason)) {
      out.push(key);
    }
  }
  return out;
}
