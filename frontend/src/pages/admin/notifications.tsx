import React, { useEffect, useState } from 'react';
import { AdminService } from '@/services/admin';
import { ApiError } from '@/services/api';
import { NotificationPrefsForm } from '@/components/admin/notifications/NotificationPrefsForm';
import { DeliveryLogTable } from '@/components/admin/notifications/DeliveryLogTable';
import { NotificationPreference, NotificationDeliveryLog } from '@/types/admin';
import { withAdminGuard } from '@/components/admin/withAdminGuard';
import { AdminLayout } from '@/components/admin/AdminLayout';
import { useAuth } from '@/contexts/AuthContext';
import { readsAllNotificationSettings, sendsTestNotification } from '@/components/admin/notifications/access';
import { AllNotificationSettings } from '@/components/admin/notifications/AllNotificationSettings';

/** Shown in place of the delivery log to anyone the route refuses (#2277). */
export const DELIVERY_LOG_REFUSED = 'The delivery log needs the ADMIN role.';

/** Shown when the API answers `success: false`: it says the message was not sent, not why. */
export const SEND_TEST_NOT_SENT =
  "The test notification was not sent. Check that SLACK_ENABLED is true and SLACK_BOT_TOKEN is set in the API's environment, and that the bot is invited to the channel.";

/** Shown in place of the Send Test button to anyone the route refuses. */
export const SEND_TEST_REFUSED = 'Sending a test notification needs the ADMIN or DEVELOPER role.';

export function NotificationsAdminPage() {
  const { user } = useAuth();
  // The page is a superuser's, but both reads below answer role ADMIN only.
  const readsAll = readsAllNotificationSettings(user);
  // `POST /notifications/test` answers ADMIN and DEVELOPER only, superuser or not.
  const sendsTest = sendsTestNotification(user);
  const [prefs, setPrefs] = useState<NotificationPreference | null>(null);
  const [logs, setLogs] = useState<NotificationDeliveryLog[]>([]);
  const [logsLoading, setLogsLoading] = useState(true);
  const [prefsError, setPrefsError] = useState<string | null>(null);
  const [logsError, setLogsError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ text: string; ok: boolean } | null>(null);

  useEffect(() => {
    AdminService.getMyNotificationPrefs()
      .then(setPrefs)
      .catch(err => setPrefsError((err as Error).message || 'Failed to load preferences'));
    if (!readsAll) {
      setLogsLoading(false);
      return;
    }
    AdminService.getNotificationDeliveryLog({ limit: 20 })
      .then(r => setLogs(r.items))
      .catch(err => setLogsError((err as Error).message || 'Failed to load the delivery log'))
      .finally(() => setLogsLoading(false));
  }, [readsAll]);

  const handleSave = async (data: Partial<NotificationPreference>) => {
    const updated = await AdminService.updateMyNotificationPrefs(data);
    setPrefs(updated);
  };

  const handleTest = async () => {
    setTestResult(null);
    try {
      const result = await AdminService.sendTestNotification('slack', 'Test from Admin UI');
      setTestResult(
        result.success
          ? { text: 'Test notification sent!', ok: true }
          : { text: SEND_TEST_NOT_SENT, ok: false },
      );
    } catch (err) {
      const reason = err instanceof ApiError || err instanceof Error ? err.message : '';
      setTestResult({
        text: reason ? `Test notification failed: ${reason}` : 'Error sending test notification',
        ok: false,
      });
    }
  };

  return (
    <AdminLayout title="Notifications" currentPath="/admin/notifications">
      <div data-testid="notifications-admin-page" className="space-y-8">

        <section>
          <h2 className="text-lg font-semibold text-slate-800 mb-4">My Preferences</h2>
          {prefs ? (
            <NotificationPrefsForm prefs={prefs} onSave={handleSave} />
          ) : prefsError ? (
            <p className="text-red-600" data-testid="prefs-error">{prefsError}</p>
          ) : (
            <p className="text-slate-500" data-testid="prefs-loading">Loading preferences...</p>
          )}
        </section>

        <AllNotificationSettings allowed={readsAll} />

        <section>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-slate-800">Delivery Log</h2>
            {sendsTest ? (
              <button
                data-testid="send-test-btn"
                onClick={handleTest}
                className="px-3 py-1.5 bg-slate-700 text-white text-sm rounded-md hover:bg-slate-800"
              >
                Send Test
              </button>
            ) : (
              <p className="text-sm text-slate-700" data-testid="send-test-refused">{SEND_TEST_REFUSED}</p>
            )}
          </div>
          <p
            role="status"
            data-testid="test-result"
            className={testResult ? `text-sm mb-2 ${testResult.ok ? 'text-green-800' : 'text-red-700'}` : 'sr-only'}
          >
            {testResult?.text}
          </p>
          {!readsAll ? (
            <p className="text-sm text-slate-700" data-testid="log-refused">{DELIVERY_LOG_REFUSED}</p>
          ) : !logsLoading && logsError ? (
            <p className="text-red-600" data-testid="log-error">{logsError}</p>
          ) : (
            <DeliveryLogTable logs={logs} loading={logsLoading} />
          )}
        </section>
      </div>
    </AdminLayout>
  );
}

export default withAdminGuard(NotificationsAdminPage);
