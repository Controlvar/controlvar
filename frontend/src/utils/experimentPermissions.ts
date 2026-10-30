/**
 * Whether the API would accept a create from this user.
 *
 * Mirrors `check_permission(user, EXPERIMENT, CREATE)`: a superuser bypasses
 * the role table, otherwise ADMIN and DEVELOPER hold CREATE. A user with a
 * custom role or a direct grant (`GET /api/v1/rbac/users/{id}/permissions`)
 * is not covered here — they are offered creation only if their built-in role
 * allows it, so the UI is never more permissive than the API.
 *
 * Shared by the experiments list (the "+ New Experiment" button) and
 * `/experiments/new` (which shows a notice instead of the form), so the two
 * cannot disagree.
 */
const ROLES_THAT_CAN_CREATE = ['ADMIN', 'DEVELOPER'] as const;

export function canCreateExperiment(
  user: { role?: string; is_superuser?: boolean } | null | undefined,
): boolean {
  // No session yet (the page renders before the context resolves): offer
  // creation rather than flickering it in; the API still decides.
  if (!user) return true;
  if (user.is_superuser) return true;
  return ROLES_THAT_CAN_CREATE.includes(user.role as 'ADMIN' | 'DEVELOPER');
}

/**
 * Whether the API would let this user read an experiment's exclusion group
 * and the global holdout (#1449): `GET /api/v1/mutual-exclusion-groups/{id}`
 * and `GET /api/v1/holdout`.
 *
 * Both routes check exactly EXPERIMENT CREATE (`_require_developer`), so this
 * is `canCreateExperiment`, and its role list is the one
 * `test_dashboard_experiment_roles.py` already holds equal to `permissions.py`.
 * Like it, a null user (no session yet) answers true: a caller that must not
 * send a request before the session resolves checks for a user first
 * (`useExperimentEligibility` does).
 */
export function canReadEligibility(
  user: { role?: string; is_superuser?: boolean } | null | undefined,
): boolean {
  return canCreateExperiment(user);
}

/**
 * Whether the API would accept a lifecycle change (start, pause, complete,
 * archive) from this user.
 *
 * Mirrors `deps.get_experiment_change_access`: a superuser is always
 * accepted; otherwise the role must hold EXPERIMENT UPDATE, which ADMIN and
 * DEVELOPER do. Who created the experiment is not part of the question, so an
 * ANALYST or VIEWER is refused on an experiment they own as well.
 *
 * Schedule uses the same check; the page offers it on a draft. Delete checks
 * EXPERIMENT DELETE instead, which exactly the same roles hold, so the
 * page's "Edit details" and "Delete draft" both use this helper
 * (`backend/tests/unit/core/test_dashboard_experiment_roles.py` keeps the
 * role lists here equal to `permissions.py`).
 */
const ROLES_THAT_CAN_CHANGE = ['ADMIN', 'DEVELOPER'] as const;

export function canChangeExperiment(
  user: { role?: string | null; is_superuser?: boolean } | null | undefined,
): boolean {
  // No session yet: keep the buttons rather than flickering them in; the
  // API still decides.
  if (!user) return true;
  if (user.is_superuser) return true;
  return ROLES_THAT_CAN_CHANGE.includes(user.role as 'ADMIN' | 'DEVELOPER');
}

/**
 * Whether the API would accept a change to a feature flag from this user
 * (#917): create, turn on or off, and update, including its targeting rules
 * and rollout percentage.
 *
 * Mirrors `can_act_on_feature_flag`: a superuser is accepted before the role
 * table is read; otherwise the role must hold FEATURE_FLAG CREATE, UPDATE or
 * DELETE, which exactly the same roles hold -- ADMIN and DEVELOPER -- so one
 * function answers all three questions. Who created the flag is not part of
 * the question (access to a flag is by role, not ownership). ANALYST and
 * VIEWER read any flag and change none.
 *
 * Shared by the flag list ("+ New Flag" and the row switches),
 * `/feature-flags/new` (a notice instead of the form) and the flag page (the
 * switch, the rule builder, the rollout slider and "Save Changes"), so the
 * three cannot disagree. `backend/tests/unit/core/test_dashboard_experiment_roles.py`
 * keeps the role list here equal to `permissions.py`.
 */
const FLAG_ROLES_THAT_CAN_CHANGE = ['ADMIN', 'DEVELOPER'] as const;

/** Shown by the three flag pages in place of the controls the role lacks. */
export const FLAG_ROLE_NOTE = 'Feature flags are created and changed by the ADMIN and DEVELOPER roles.';

export function canChangeFeatureFlags(
  user: { role?: string | null; is_superuser?: boolean } | null | undefined,
): boolean {
  // No session yet: keep the controls rather than flickering them in; the
  // API still decides. In the dashboard `RequireAuth` renders a flag page only
  // once the user is resolved, so this branch is reached only by a page
  // mounted outside an AuthProvider.
  if (!user) return true;
  if (user.is_superuser) return true;
  return FLAG_ROLES_THAT_CAN_CHANGE.includes(user.role as 'ADMIN' | 'DEVELOPER');
}
