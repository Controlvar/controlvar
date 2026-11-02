import actionTypes from './action-types.json';

/**
 * Display text for the audit log page (#221).
 *
 * `action-types.json` lists every `action_type` the API writes; a backend
 * test keeps it equal to the API's own list. Every type in it has a label and
 * a filter group here, and the action filter offers exactly those types.
 */

/** Every action type the API writes, from `action-types.json`. */
export const WRITTEN_ACTION_TYPES: readonly string[] = actionTypes;

export type ActionGroup =
  | 'Flags'
  | 'Experiments'
  | 'Users and access'
  | 'Holdouts, groups and segments'
  | 'Safety';

/** The order the action filter shows its groups in. */
export const ACTION_GROUP_ORDER: readonly ActionGroup[] = [
  'Flags',
  'Experiments',
  'Users and access',
  'Holdouts, groups and segments',
  'Safety',
];

/** Label and filter group for each action type. */
export const ACTIONS: Readonly<Record<string, { label: string; group: ActionGroup }>> = {
  feature_flag_create: { label: 'Flag created', group: 'Flags' },
  feature_flag_update: { label: 'Flag changed', group: 'Flags' },
  feature_flag_delete: { label: 'Flag deleted', group: 'Flags' },
  feature_flag_activate: { label: 'Flag activated', group: 'Flags' },
  feature_flag_deactivate: { label: 'Flag deactivated', group: 'Flags' },
  toggle_enable: { label: 'Flag turned on', group: 'Flags' },
  toggle_disable: { label: 'Flag turned off', group: 'Flags' },
  experiment_create: { label: 'Experiment created', group: 'Experiments' },
  experiment_update: { label: 'Experiment changed', group: 'Experiments' },
  experiment_delete: { label: 'Experiment deleted', group: 'Experiments' },
  experiment_start: { label: 'Experiment started', group: 'Experiments' },
  experiment_pause: { label: 'Experiment paused', group: 'Experiments' },
  experiment_complete: { label: 'Experiment completed', group: 'Experiments' },
  user_create: { label: 'User created', group: 'Users and access' },
  user_delete: { label: 'User deleted', group: 'Users and access' },
  user_activate: { label: 'User reactivated', group: 'Users and access' },
  user_deactivate: { label: 'User deactivated', group: 'Users and access' },
  role_assign: { label: 'Role changed', group: 'Users and access' },
  role_unassign: { label: 'Custom role removed', group: 'Users and access' },
  role_create: { label: 'Custom role created', group: 'Users and access' },
  role_update: { label: 'Custom role changed', group: 'Users and access' },
  role_delete: { label: 'Custom role deleted', group: 'Users and access' },
  permission_grant: { label: 'Permission granted', group: 'Users and access' },
  permission_revoke: { label: 'Permission revoked', group: 'Users and access' },
  user_login: { label: 'Signed in', group: 'Users and access' },
  api_key_create: { label: 'API key created', group: 'Users and access' },
  api_key_revoke: { label: 'API key revoked', group: 'Users and access' },
  holdout_create: { label: 'Holdout created', group: 'Holdouts, groups and segments' },
  holdout_update: { label: 'Holdout changed', group: 'Holdouts, groups and segments' },
  holdout_activate: { label: 'Holdout activated', group: 'Holdouts, groups and segments' },
  holdout_deactivate: { label: 'Holdout deactivated', group: 'Holdouts, groups and segments' },
  mutual_exclusion_group_create: {
    label: 'Exclusion group created',
    group: 'Holdouts, groups and segments',
  },
  mutual_exclusion_group_update: {
    label: 'Exclusion group changed',
    group: 'Holdouts, groups and segments',
  },
  mutual_exclusion_group_archive: {
    label: 'Exclusion group archived',
    group: 'Holdouts, groups and segments',
  },
  segment_create: { label: 'Segment created', group: 'Holdouts, groups and segments' },
  segment_update: { label: 'Segment changed', group: 'Holdouts, groups and segments' },
  segment_archive: { label: 'Segment archived', group: 'Holdouts, groups and segments' },
  safety_rollback: { label: 'Safety rollback', group: 'Safety' },
};

/** The label for an action type; an unknown type (an older entry) shows as itself. */
export function actionLabel(actionType: string): string {
  return ACTIONS[actionType]?.label ?? actionType;
}

/** The action filter's groups: only written types, in `ACTION_GROUP_ORDER`. */
export function actionFilterGroups(): { group: ActionGroup; options: { value: string; label: string }[] }[] {
  return ACTION_GROUP_ORDER.map((group) => ({
    group,
    options: WRITTEN_ACTION_TYPES.filter((type) => ACTIONS[type]?.group === group).map((type) => ({
      value: type,
      label: ACTIONS[type].label,
    })),
  })).filter((g) => g.options.length > 0);
}

export const ENTITY_LABELS: Readonly<Record<string, string>> = {
  feature_flag: 'Feature flag',
  experiment: 'Experiment',
  user: 'User',
  role: 'Custom role',
  api_key: 'API key',
  holdout: 'Holdout',
  mutual_exclusion_group: 'Exclusion group',
  segment: 'Segment',
};

/** The label for an entity type; an unknown one shows as itself. */
export function entityLabel(entityType: string): string {
  return ENTITY_LABELS[entityType] ?? entityType;
}

/**
 * The platform's own actors: the reserved `user_email` values the API writes
 * with no `user_id` (`SYSTEM_ACTOR_EMAILS` in `audit_service.py`).
 */
export const SYSTEM_ACTORS: Readonly<Record<string, string>> = {
  'system:experiment-scheduler': 'Experiment scheduler',
  'system:rollout-scheduler': 'Rollout scheduler',
  'system:safety-monitor': 'Safety monitor',
  'system:cognito-sync': 'Cognito sync',
  'system:sso-sync': 'SSO sync',
};

/**
 * Who made an entry, as the page shows it. "(automatic)" only when the entry
 * has no `user_id` AND its email is one of `SYSTEM_ACTORS`: a deleted user's
 * entries also lose their `user_id`, but keep the user's own email.
 */
export function actorLabel(entry: { user_id?: string | null; user_email: string }): string {
  const name = SYSTEM_ACTORS[entry.user_email];
  if (name && (entry.user_id === null || entry.user_id === undefined)) {
    return `${name} (automatic)`;
  }
  return entry.user_email;
}
