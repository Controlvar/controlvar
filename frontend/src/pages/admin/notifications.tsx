import React, { useEffect, useState } from 'react';
import { AdminService } from '@/services/admin';
import { NotificationPrefsForm } from '@/components/admin/notifications/NotificationPrefsForm';
import { DeliveryLogTable } from '@/components/admin/notifications/DeliveryLogTable';
import { NotificationPreference, NotificationDeliveryLog } from '@/types/admin';
import { withAdminGuard } from '@/components/admin/withAdminGuard';
import { PageTitle } from '@/components/PageTitle';

export function NotificationsAdminPage() {
  const [prefs, setPrefs] = useState<NotificationPreference | null>(null);
  const [logs, setLogs] = useState<NotificationDeliveryLog[]>([]);
  const [logsLoading, setLogsLoading] = useState(true);
  const [prefsError, setPrefsError] = useState<string | null>(null);
  const [logsError, setLogsError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);

  useEffect(() => {
    AdminService.getMyNotificationPrefs()
      .then(setPrefs)
      .catch(err => setPrefsError((err as Error).message || 'Failed to load preferences'));
    AdminService.getNotificationDeliveryLog({ limit: 20 })
      .then(r => setLogs(r.items))
      .catch(err => setLogsError((err as Error).message || 'Failed to load the delivery log'))
      .finally(() => setLogsLoading(false));
  }, []);

  const handleSave = async (data: Partial<NotificationPreference>) => {
    const updated = await AdminService.updateMyNotificationPrefs(data);
    setPrefs(updated);
  };

  const handleTest = async () => {
    try {
      const result = await AdminService.sendTestNotification('slack', 'Test from Admin UI');
      setTestResult(result.success ? 'Test notification sent!' : 'Test notification failed');
    } catch {
      setTestResult('Error sending test notification');
    }
    setTimeout(() => setTestResult(null), 3000);
  };

  return (
    <div data-testid="notifications-admin-page" className="p-6 space-y-8">
      <PageTitle title="Notifications · Admin" />
      <h1 className="text-2xl font-bold text-slate-900">Notifications</h1>

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

      <section>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-slate-800">Delivery Log</h2>
          <button
            data-testid="send-test-btn"
            onClick={handleTest}
            className="px-3 py-1.5 bg-slate-700 text-white text-sm rounded-md hover:bg-slate-800"
          >
            Send Test
          </button>
        </div>
        {testResult && (
          <p data-testid="test-result" className="text-sm text-slate-600 mb-2">{testResult}</p>
        )}
        {!logsLoading && logsError ? (
          <p className="text-red-600" data-testid="log-error">{logsError}</p>
        ) : (
          <DeliveryLogTable logs={logs} loading={logsLoading} />
        )}
      </section>
    </div>
  );
}

export default withAdminGuard(NotificationsAdminPage);
