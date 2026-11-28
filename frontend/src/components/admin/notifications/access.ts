import type { UserMe } from '@/services/api';

/**
 * Who reads everyone's notification settings and the delivery log (#2277):
 * `GET /api/v1/notifications/admin/preferences` and
 * `GET /api/v1/notifications/delivery-log` both check `role == ADMIN` and
 * nothing else, so a superuser who is not an ADMIN is refused and an ADMIN
 * who is not a superuser is accepted. The page itself is a superuser's
 * (`withAdminGuard`), so in effect it is a superuser who is also an ADMIN.
 *
 * `test_dashboard_permission_table.py` calls both routes for every role, with
 * and without the superuser flag, and `permissionMatrix.test.ts` holds this
 * helper to the answer.
 */
export function readsAllNotificationSettings(user: Pick<UserMe, 'role'> | null | undefined): boolean {
  return user?.role === 'ADMIN';
}

/**
 * Who sends a test notification from this page: `POST /api/v1/notifications/test`
 * answers 403 unless `current_user.role` is ADMIN or DEVELOPER. It reads the
 * role alone, so the superuser flag does not admit anyone and a custom role or
 * grant changes nothing.
 */
export function sendsTestNotification(user: Pick<UserMe, 'role'> | null | undefined): boolean {
  return user?.role === 'ADMIN' || user?.role === 'DEVELOPER';
}

/**
 * Who sends the scheduler's test notification (#2277):
 * `POST /api/v1/scheduler/notify/test` depends on `get_current_superuser`, so
 * it is the superuser flag and no role.
 */
export function canTestSchedulerWebhook(user: Pick<UserMe, 'is_superuser'> | null | undefined): boolean {
  return user?.is_superuser === true;
}
