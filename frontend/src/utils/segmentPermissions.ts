import { EffectivePermissions, holds } from '@/utils/effectivePermissions';

/**
 * Whether the API would accept a change to a segment from this user (#440).
 *
 * Segments use the EXPERIMENT permissions (`segments.py`
 * `_require_permission`): create needs EXPERIMENT CREATE, update and the
 * member routes EXPERIMENT UPDATE, and archive EXPERIMENT DELETE, which
 * ADMIN and DEVELOPER hold by role and a custom role or grant can add in the
 * full profile (the current user's effective map); a superuser is not
 * limited by the table. Each action is answered separately.
 *
 * Unlike `canChangeExperiment`, a user who is not resolved yet gets no write
 * control: the Segments pages render after the session resolves, and the UI
 * is never more permissive than the API.
 */
const ROLES_THAT_CAN_CHANGE = ['ADMIN', 'DEVELOPER'];

type SegmentUser = { role?: string | null; is_superuser?: boolean } | null | undefined;

function segmentAllows(user: SegmentUser, action: string, effective?: EffectivePermissions): boolean {
  if (!user) return false;
  if (user.is_superuser) return true;
  return ROLES_THAT_CAN_CHANGE.indexOf(String(user.role)) !== -1 || holds(effective, 'experiment', action);
}

/** EXPERIMENT CREATE: "New segment" and `/segments/new`. */
export function canCreateSegments(user: SegmentUser, effective?: EffectivePermissions): boolean {
  return segmentAllows(user, 'create', effective);
}

/** EXPERIMENT UPDATE: a segment's rules and members. */
export function canChangeSegments(user: SegmentUser, effective?: EffectivePermissions): boolean {
  return segmentAllows(user, 'update', effective);
}

/** EXPERIMENT DELETE: archiving a segment. */
export function canArchiveSegments(user: SegmentUser, effective?: EffectivePermissions): boolean {
  return segmentAllows(user, 'delete', effective);
}
