import { useEffect, useState } from 'react';
import { isApiError } from '@/services/api';
import { EligibilityService } from '@/services/eligibility';
import { GlobalHoldout, MutualExclusionGroup } from '@/types/eligibility';
import { canReadEligibility } from '@/utils/experimentPermissions';

/**
 * One read's state on the experiment page (#1449).
 *
 * - `loading`: not answered yet, or the session has not resolved.
 * - `present`: the group, or the active holdout.
 * - `absent`: the experiment is in no group; no holdout is active.
 * - `refused`: the role may not read it (decided from the role, or a 403).
 * - `error`: anything else, a 404 included, and an answer of the wrong shape.
 */
export type EligibilityRead<T> =
  | { kind: 'loading' }
  | { kind: 'present'; value: T }
  | { kind: 'absent' }
  | { kind: 'refused' }
  | { kind: 'error' };

type Answer<T> = Exclude<EligibilityRead<T>, { kind: 'loading' }>;

/** The user fields the decision reads; `AuthContext`'s user has them. */
export interface EligibilityUser {
  role?: string;
  is_superuser?: boolean;
}

const LOADING = { kind: 'loading' } as const;
const ABSENT = { kind: 'absent' } as const;
const REFUSED = { kind: 'refused' } as const;
const ERROR = { kind: 'error' } as const;

function failure(err: unknown): Answer<never> {
  return isApiError(err) && err.status === 403 ? REFUSED : ERROR;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** A group answer, or `error` when it lacks what the page shows. */
export function groupAnswer(body: unknown): Answer<MutualExclusionGroup> {
  if (isObject(body) && typeof body.name === 'string' && typeof body.status === 'string') {
    return { kind: 'present', value: body as unknown as MutualExclusionGroup };
  }
  return ERROR;
}

/**
 * A holdout answer. JSON `null` is "none active"; an active holdout with a name
 * and a percentage is present. Anything else, `undefined` (an empty 2xx body)
 * and an inactive holdout included, is `error`: never "none", which would be a
 * claim the page cannot back.
 */
export function holdoutAnswer(body: unknown): Answer<GlobalHoldout> {
  if (body === null) return ABSENT;
  if (
    isObject(body) &&
    body.is_active === true &&
    typeof body.name === 'string' &&
    typeof body.holdout_percentage === 'number' &&
    Number.isFinite(body.holdout_percentage)
  ) {
    return { kind: 'present', value: body as unknown as GlobalHoldout };
  }
  return ERROR;
}

/**
 * The experiment's exclusion group and the global holdout, as the page shows
 * them. Mounted inside the experiment page's loaded header, so a page whose
 * experiment did not load reads neither.
 *
 * It sends a request only once the user has resolved and `canReadEligibility`
 * admits them (ADMIN, DEVELOPER, a superuser): ANALYST and VIEWER, and a user
 * not yet resolved, send none. The role is read again at every render, so a
 * user who loses the role sees the refused state, not what was read before.
 *
 * The group is read once per group id, and only the answer for the current id
 * is shown; the holdout once per mount (again only if the role stops and then
 * starts admitting the user). Neither is read again on a timer.
 */
export function useExperimentEligibility(
  groupId: string | null | undefined,
  user: EligibilityUser | null | undefined,
): { group: EligibilityRead<MutualExclusionGroup>; holdout: EligibilityRead<GlobalHoldout> } {
  const resolved = user !== null && user !== undefined;
  const mayRead = resolved && canReadEligibility(user);
  const id = groupId || null;

  const [groupState, setGroupState] = useState<{
    id: string;
    answer: Answer<MutualExclusionGroup>;
  } | null>(null);
  const [holdoutState, setHoldoutState] = useState<Answer<GlobalHoldout> | null>(null);

  useEffect(() => {
    if (!mayRead || id === null) return undefined;
    let current = true;
    EligibilityService.group(id).then(
      (body) => {
        if (current) setGroupState({ id, answer: groupAnswer(body) });
      },
      (err: unknown) => {
        if (current) setGroupState({ id, answer: failure(err) });
      },
    );
    return () => {
      current = false;
    };
  }, [mayRead, id]);

  useEffect(() => {
    if (!mayRead) return undefined;
    let current = true;
    EligibilityService.activeHoldout().then(
      (body) => {
        if (current) setHoldoutState(holdoutAnswer(body));
      },
      (err: unknown) => {
        if (current) setHoldoutState(failure(err));
      },
    );
    return () => {
      current = false;
    };
  }, [mayRead]);

  let group: EligibilityRead<MutualExclusionGroup>;
  if (id === null) group = ABSENT;
  else if (!resolved) group = LOADING;
  else if (!mayRead) group = REFUSED;
  else group = groupState !== null && groupState.id === id ? groupState.answer : LOADING;

  let holdout: EligibilityRead<GlobalHoldout>;
  if (!resolved) holdout = LOADING;
  else if (!mayRead) holdout = REFUSED;
  else holdout = holdoutState ?? LOADING;

  return { group, holdout };
}
