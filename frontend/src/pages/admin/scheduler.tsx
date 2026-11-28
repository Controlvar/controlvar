import React, { useCallback, useEffect, useState } from 'react';
import { AdminLayout } from '@/components/admin/AdminLayout';
import { SchedulerStatusCard } from '@/components/admin/scheduler/SchedulerStatusCard';
import { AdminService } from '@/services/admin';
import { SchedulerHealth } from '@/types/admin';
import { withAdminGuard } from '@/components/admin/withAdminGuard';
import { useAuth } from '@/contexts/AuthContext';
import { canTestSchedulerWebhook } from '@/components/admin/notifications/access';
import { SchedulerWebhookTest } from '@/components/admin/scheduler/SchedulerWebhookTest';

const REFRESH_INTERVAL_MS = 60_000;

export function SchedulerHealthPage() {
  const { user } = useAuth();
  const [schedulers, setSchedulers] = useState<SchedulerHealth[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchHealth = useCallback(async () => {
    setError(null);
    try {
      const data = await AdminService.getSchedulerHealth();
      setSchedulers(data);
    } catch (err) {
      setError((err as Error).message || 'Failed to load scheduler health');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchHealth();
    const interval = setInterval(fetchHealth, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [fetchHealth]);

  return (
    <AdminLayout title="Scheduler Health" currentPath="/admin/scheduler">
      <div data-testid="scheduler-health-page" className="flex flex-col gap-5">
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-semibold text-slate-900">Scheduler Health</h2>
          <span className="text-xs text-slate-400">Auto-refreshes every 60 seconds</span>
        </div>

        {canTestSchedulerWebhook(user) && <SchedulerWebhookTest />}

        {loading && (
          <div data-testid="scheduler-loading" className="text-sm text-slate-500 py-4">
            Loading scheduler health...
          </div>
        )}

        {!loading && error && (
          <div
            data-testid="scheduler-error"
            className="p-4 bg-red-50 border border-red-200 rounded-md text-sm text-red-600"
          >
            {error}
          </div>
        )}

        {!loading && !error && schedulers.length === 0 && (
          <div
            data-testid="scheduler-empty"
            className="p-4 text-center text-slate-500 text-sm"
          >
            No schedulers found.
          </div>
        )}

        {!loading && !error && schedulers.length > 0 && (
          <div className="flex flex-col gap-4">
            {schedulers.map((scheduler) => (
              <SchedulerStatusCard key={scheduler.scheduler_name} scheduler={scheduler} />
            ))}
          </div>
        )}
      </div>
    </AdminLayout>
  );
}

export default withAdminGuard(SchedulerHealthPage);
