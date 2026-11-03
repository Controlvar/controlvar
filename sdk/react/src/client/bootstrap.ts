import { assignmentFromResponse, evaluationFromResponse } from './ExperimentationClient';
import type {
  ExperimentAssignResponse,
  ExperimentAssignment,
  FeatureFlagEvaluateResponse,
  FeatureFlagEvaluation,
} from './types';

/** The Provider's warning when the bootstrap is for another user. */
export const BOOTSTRAP_USER_WARNING =
  "@controlvar/react-sdk: bootstrap not used: its userId is not the provider's user.userId." +
  ' Pass the same user to ServerClient.bootstrap() and to the provider.';

/** The Provider's warning when the bootstrap is not an object of the right shape. */
export const BOOTSTRAP_SHAPE_WARNING =
  '@controlvar/react-sdk: bootstrap not used: it must be an object with a userId string and' +
  ' optional flags and experiments objects, as ServerClient.bootstrap() returns.';

/** The Provider's warning for entries that are not responses of the API. Counts only, no values. */
export function bootstrapEntriesWarning(flags: number, experiments: number): string {
  return (
    `@controlvar/react-sdk: bootstrap entries not used for ${flags} flag(s) and` +
    ` ${experiments} experiment(s): a flags entry must be a flag evaluation response (enabled` +
    ' true or false) and an experiments entry an assignment response (with variant_name), as' +
    ' ServerClient.bootstrap() returns.'
  );
}

/** Why a bootstrap, or some of it, is not used: one warning per reason per Provider. */
export type BootstrapProblem = 'user' | 'shape' | 'entries';

/** A bootstrap as the hooks read it. */
export interface ReadBootstrap {
  /** Flag key -> the evaluation its entry stands for (well-formed entries only). */
  readonly flags: ReadonlyMap<string, FeatureFlagEvaluation>;
  /** Experiment key -> the assignment its entry stands for (well-formed entries only). */
  readonly experiments: ReadonlyMap<string, ExperimentAssignment>;
  /** The warnings it earns, each with its reason. */
  readonly warnings: ReadonlyArray<readonly [BootstrapProblem, string]>;
}

const NONE: ReadBootstrap = { flags: new Map(), experiments: new Map(), warnings: [] };

function ignored(problem: BootstrapProblem, warning: string): ReadBootstrap {
  return { flags: new Map(), experiments: new Map(), warnings: [[problem, warning]] };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** A body of the evaluate route: an object with a boolean `enabled`. */
export function isFlagResponse(value: unknown): value is FeatureFlagEvaluateResponse {
  return isObject(value) && typeof value.enabled === 'boolean';
}

/** A body of the assign route: an object with a string `variant_name`. */
export function isAssignResponse(value: unknown): value is ExperimentAssignResponse {
  return isObject(value) && typeof value.variant_name === 'string';
}

/**
 * What the hooks of a Provider for `userId` start from, given its `bootstrap`
 * prop. Depends on its arguments only, so a server render and the hydration
 * that follows it decide the same. Never throws.
 *
 * - `undefined` or `null`: no bootstrap, no warning.
 * - Not an object, a `userId` that is not a string, or a `flags` or
 *   `experiments` that is present and not an object: nothing used, warning 2.
 * - A `userId` other than `userId`: nothing used, warning 1.
 * - Otherwise each entry that is a response of its route is used; any other
 *   entry is skipped for its key alone, and warning 3 counts them.
 */
export function readBootstrap(bootstrap: unknown, userId: string): ReadBootstrap {
  if (bootstrap === undefined || bootstrap === null) return NONE;
  try {
    if (
      !isObject(bootstrap) ||
      typeof bootstrap.userId !== 'string' ||
      (bootstrap.flags !== undefined && !isObject(bootstrap.flags)) ||
      (bootstrap.experiments !== undefined && !isObject(bootstrap.experiments))
    ) {
      return ignored('shape', BOOTSTRAP_SHAPE_WARNING);
    }
    if (bootstrap.userId !== userId) return ignored('user', BOOTSTRAP_USER_WARNING);

    const flags = new Map<string, FeatureFlagEvaluation>();
    const experiments = new Map<string, ExperimentAssignment>();
    let badFlags = 0;
    let badExperiments = 0;
    const flagEntries = (bootstrap.flags ?? {}) as Record<string, unknown>;
    for (const key of Object.keys(flagEntries)) {
      const body = flagEntries[key];
      if (isFlagResponse(body)) flags.set(key, evaluationFromResponse(key, body));
      else badFlags += 1;
    }
    const experimentEntries = (bootstrap.experiments ?? {}) as Record<string, unknown>;
    for (const key of Object.keys(experimentEntries)) {
      const body = experimentEntries[key];
      if (isAssignResponse(body)) experiments.set(key, assignmentFromResponse(key, body));
      else badExperiments += 1;
    }
    const warnings: Array<readonly [BootstrapProblem, string]> =
      badFlags + badExperiments > 0
        ? [['entries', bootstrapEntriesWarning(badFlags, badExperiments)]]
        : [];
    return { flags, experiments, warnings };
  } catch {
    // A getter or proxy that throws: no JSON value does, so treat it as the wrong shape.
    return ignored('shape', BOOTSTRAP_SHAPE_WARNING);
  }
}

/** What a hook started from: its bootstrapped answer, for one key (or key list) and user. */
export interface Seed<T> {
  readonly key: string;
  readonly user: unknown;
  readonly value: T;
}
