import { EffectivePermissions, holds } from '@/utils/effectivePermissions';

/** The user fields a permission helper reads; `AuthContext`'s user has them. */
export type PermissionUser = { role?: string | null; is_superuser?: boolean } | null | undefined;

/**
 * Whether the API would accept a create from this user.
 *
 * Mirrors `check_permission(user, EXPERIMENT, CREATE)`: a superuser bypasses
 * the role table, ADMIN and DEVELOPER hold CREATE by role, and in the full
 * profile a custom role or direct grant can add it (the current user's
 * effective map, `effective`; see `@/utils/effectivePermissions`).
 *
 * Shared by the experiments list (the "+ New Experiment" button) and
 * `/experiments/new` (which shows a notice instead of the form), so the two
 * cannot disagree.
 */
const ROLES_THAT_CAN_CREATE = ['ADMIN', 'DEVELOPER'] as const;

export function canCreateExperiment(user: PermissionUser, effective?: EffectivePermissions): boolean {
  // No session yet (the page renders before the context resolves): offer
  // creation rather than flickering it in; the API still decides.
  if (!user) return true;
  if (user.is_superuser) return true;
  return (
    ROLES_THAT_CAN_CREATE.includes(user.role as 'ADMIN' | 'DEVELOPER') ||
    holds(effective, 'experiment', 'create')
  );
}

/**
 * Whether the API would let this user read an experiment's exclusion group
 * and the global holdout (#1449): `GET /api/v1/mutual-exclusion-groups/{id}`
 * and `GET /api/v1/holdout`.
 *
 * Both routes check exactly EXPERIMENT CREATE (`_require_developer`), so this
 * is `canCreateExperiment`, and its role list is the one
 * `test_dashboard_experiment_roles.py` already holds equal to `permissions.py`;
 * the effective map's `experiment: create` admits a user here too.
 * Like it, a null user (no session yet) answers true: a caller that must not
 * send a request before the session resolves checks for a user first
 * (`useExperimentEligibility` does).
 */
export function canReadEligibility(user: PermissionUser, effective?: EffectivePermissions): boolean {
  return canCreateExperiment(user, effective);
}

/**
 * Whether the API would let this user archive or unarchive an exclusion group
 * (#1455), and, from #1455's second half, list, create, edit, activate and
 * end global holdouts.
 *
 * Mirrors `_require_admin` in both `mutual_exclusion_groups.py` and
 * `global_holdout.py`: a superuser, or the ADMIN role itself. It checks the
 * role, not a permission from the role table (no permission there is held by
 * ADMIN alone), so `test_dashboard_experiment_roles.py` calls both
 * `_require_admin` functions for every role and holds the set they admit
 * equal to this list. No custom role or direct grant can give it, so it
 * takes no effective map.
 *
 * Unlike the helpers above, a null user (no session yet) answers FALSE: these
 * controls appear only once the user is known to be an ADMIN or a superuser.
 */
const ROLES_THAT_ADMINISTER_ELIGIBILITY = ['ADMIN'] as const;

export function canAdministerEligibility(
  user: { role?: string | null; is_superuser?: boolean } | null | undefined,
): boolean {
  if (!user) return false;
  if (user.is_superuser) return true;
  return ROLES_THAT_ADMINISTER_ELIGIBILITY.includes(user.role as 'ADMIN');
}

/**
 * Whether the API would accept a change to an experiment from this user:
 * start, pause, complete, archive, schedule, and edit a draft's details,
 * variants and metrics.
 *
 * Mirrors `deps.get_experiment_change_access`: a superuser is always
 * accepted; otherwise the role must hold EXPERIMENT UPDATE (ADMIN and
 * DEVELOPER do), or the effective map must. Who created the experiment is not
 * part of the question, so an ANALYST or VIEWER without that permission is
 * refused on an experiment they own as well.
 *
 * Delete checks EXPERIMENT DELETE instead (`canDeleteExperiment`). The same
 * roles hold it, so both read this list
 * (`backend/tests/unit/core/test_dashboard_experiment_roles.py` keeps it
 * equal to `permissions.py` for UPDATE and for DELETE), but a custom role or
 * direct grant can give either one alone, so each is answered separately.
 */
const ROLES_THAT_CAN_CHANGE = ['ADMIN', 'DEVELOPER'] as const;

export function canChangeExperiment(user: PermissionUser, effective?: EffectivePermissions): boolean {
  // No session yet: keep the buttons rather than flickering them in; the
  // API still decides.
  if (!user) return true;
  if (user.is_superuser) return true;
  return (
    ROLES_THAT_CAN_CHANGE.includes(user.role as 'ADMIN' | 'DEVELOPER') ||
    holds(effective, 'experiment', 'update')
  );
}

/** Whether the API would delete a draft for this user: EXPERIMENT DELETE (see above). */
export function canDeleteExperiment(user: PermissionUser, effective?: EffectivePermissions): boolean {
  if (!user) return true;
  if (user.is_superuser) return true;
  return (
    ROLES_THAT_CAN_CHANGE.includes(user.role as 'ADMIN' | 'DEVELOPER') ||
    holds(effective, 'experiment', 'delete')
  );
}

/**
 * Whether the API would accept a change to a feature flag from this user
 * (#917): create (`canCreateFeatureFlags`), turn on or off and update,
 * including its targeting rules, values, rollout percentage and rollout
 * schedules (`canChangeFeatureFlags`), and delete (`canDeleteFeatureFlags`).
 *
 * Mirrors `can_act_on_feature_flag` and `deps.can_create_feature_flag`: a
 * superuser is accepted before the role table is read; otherwise the role
 * must hold FEATURE_FLAG CREATE, UPDATE or DELETE (ADMIN and DEVELOPER), or
 * the effective map must hold the pair the route checks; each action is
 * answered separately. Who created the flag is not part of the question
 * (access to a flag is by permission, not ownership).
 *
 * Shared by the flag list ("+ New Flag" and the row switches),
 * `/feature-flags/new` (a notice instead of the form) and the flag page (the
 * switch, the rule builder, the rollout slider and "Save Changes"), so the
 * three cannot disagree. `backend/tests/unit/core/test_dashboard_experiment_roles.py`
 * keeps the role list here equal to `permissions.py` for all three actions.
 */
const FLAG_ROLES_THAT_CAN_CHANGE = ['ADMIN', 'DEVELOPER'] as const;

/** Shown by the three flag pages in place of the controls the user lacks. */
export const FLAG_ROLE_NOTE =
  'Feature flags are created and changed with the feature flag permissions, which the ADMIN and DEVELOPER roles have.';

function flagAllows(user: PermissionUser, action: string, effective?: EffectivePermissions): boolean {
  // No session yet: keep the controls rather than flickering them in; the
  // API still decides. In the dashboard `RequireAuth` renders a flag page only
  // once the user is resolved, so this branch is reached only by a page
  // mounted outside an AuthProvider.
  if (!user) return true;
  if (user.is_superuser) return true;
  return (
    FLAG_ROLES_THAT_CAN_CHANGE.includes(user.role as 'ADMIN' | 'DEVELOPER') ||
    holds(effective, 'feature_flag', action)
  );
}

/** FEATURE_FLAG CREATE: "+ New Flag" and `/feature-flags/new`. */
export function canCreateFeatureFlags(user: PermissionUser, effective?: EffectivePermissions): boolean {
  return flagAllows(user, 'create', effective);
}

/** FEATURE_FLAG UPDATE: the switches, the flag form and the rollout schedule. */
export function canChangeFeatureFlags(user: PermissionUser, effective?: EffectivePermissions): boolean {
  return flagAllows(user, 'update', effective);
}

/**
 * FEATURE_FLAG DELETE: "Delete" on the flag's page (#442). The same roles
 * hold it as UPDATE, but a custom role or direct grant can give either one
 * alone, so it is answered separately (`DELETE /feature-flags/{id}` checks
 * exactly this pair).
 */
export function canDeleteFeatureFlags(user: PermissionUser, effective?: EffectivePermissions): boolean {
  return flagAllows(user, 'delete', effective);
}
