import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/router';
import { AdminLayout } from '@/components/admin/AdminLayout';
import { StatTile } from '@/components/admin/StatTile';
import { AdminService } from '@/services/admin';
import { AdminStats } from '@/types/admin';
import { withAdminGuard } from '@/components/admin/withAdminGuard';

export function AdminDashboard() {
  const router = useRouter();
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    AdminService.getStats()
      .then((data) => {
        setStats(data);
        setLoading(false);
      })
      .catch((err: Error) => {
        setError(err.message || 'Failed to load stats');
        setLoading(false);
      });
  }, []);

  return (
    <AdminLayout title="Dashboard" currentPath={router.pathname}>
      <div data-testid="admin-dashboard">
        {loading && (
          <div data-testid="loading-skeleton" className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {Array.from({ length: 5 }).map((_, i) => (
              <div
                key={i}
                className="bg-white border border-slate-200 rounded-lg p-5 h-24 animate-pulse"
              >
                <div className="h-4 bg-slate-200 rounded w-1/2 mb-3" />
                <div className="h-8 bg-slate-200 rounded w-1/3" />
              </div>
            ))}
          </div>
        )}

        {error && !loading && (
          <div
            data-testid="error-state"
            className="bg-red-50 border border-red-200 rounded-lg p-6 text-center"
          >
            <p className="text-red-700 font-medium">Failed to load dashboard stats</p>
            <p className="text-red-700 text-sm mt-1">{error}</p>
          </div>
        )}

        {stats && !loading && !error && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            <StatTile
              label="Total Experiments"
              value={stats.experiments.total}
              color="blue"
            />
            <StatTile
              label="Active Experiments"
              value={stats.experiments.active}
              color="green"
            />
            <StatTile
              label="Total Feature Flags"
              value={stats.feature_flags.total}
              color="blue"
            />
            <StatTile
              label="Active Feature Flags"
              value={stats.feature_flags.active}
              color="green"
            />
            <StatTile
              label="Total Users"
              value={stats.users.total}
              color="slate"
            />
          </div>
        )}
      </div>
    </AdminLayout>
  );
}

export default withAdminGuard(AdminDashboard);
