import { Role, UserMe } from '@/services/api';
import { EffectivePermissions, holds } from '@/utils/effectivePermissions';

/**
 * Who opens the Audit Log page: every role, because `GET /api/v1/audit-logs/`
 * and `GET /api/v1/audit-logs/export` answer every signed-in user (they depend
 * on `deps.get_current_active_user`, not the superuser check) and narrow the
 * rows to the caller's own for anyone who may not read the audit log.
 *
 * The page guard and the top-navigation item both use this list, so the link
 * and the page cannot disagree (#84 was a nav item, a guard and an API that
 * did). Keep all four: `RequireAuth` checks the role with no superuser
 * exception, so dropping one would also refuse a superuser holding that role.
 */
export const AUDIT_LOG_ROLES: Role[] = ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'];

/** The roles the audit API gives other users' entries to (`AUDIT_LOG_READ_ALL_ROLES`). */
const READ_ALL_ROLES: Role[] = ['ADMIN', 'ANALYST'];

/**
 * Whether the audit API returns other users' entries to this user too, or
 * only their own.
 *
 * Mirrors `can_read_all_audit_logs` in `backend/app/core/permissions.py`:
 * a superuser first, then ADMIN and ANALYST, then, in the full profile, the
 * effective map's `audit_log: read` (a custom role or direct grant). Display
 * only -- it chooses the sentence that tells anyone else they see their own
 * entries. The API does the narrowing; nothing here does.
 */
export function readsAllAuditLogs(
  user: Pick<UserMe, 'role' | 'is_superuser'>,
  effective?: EffectivePermissions,
): boolean {
  return user.is_superuser === true || READ_ALL_ROLES.includes(user.role) || holds(effective, 'audit_log', 'read');
}
