import React, { useState } from 'react';
import { AdminLayout } from '@/components/admin/AdminLayout';
import { PageTitle } from '@/components/PageTitle';
import { AuditLogDownload } from '@/components/admin/audit/AuditLogDownload';
import { AuditLogFilter, AuditLogFilters } from '@/components/admin/audit/AuditLogFilter';
import { AuditLogTable } from '@/components/admin/audit/AuditLogTable';
import { AUDIT_LOG_ROLES, readsAllAuditLogs } from '@/components/admin/audit/access';
import { withAdminGuard } from '@/components/admin/withAdminGuard';
import { useAuth } from '@/contexts/AuthContext';
import { useEffectivePermissions } from '@/contexts/EffectivePermissionsContext';

const TITLE = 'Audit Log';

/** Shown to a user the audit API answers with their own entries only. */
export const OWN_SCOPE_NOTE =
  "You see the audit entries you made yourself. Reading other users' entries needs permission to read the audit log, which the ADMIN and ANALYST roles have.";

/**
 * The audit log, for every role (see `AUDIT_LOG_ROLES`).
 *
 * A superuser gets the admin area's layout and sidebar, as before. Everyone
 * else gets a plain page inside the application shell: the admin sidebar
 * links to pages only a superuser can open, so it is not shown to them. The
 * rows are whatever the audit API returns for the signed-in user; the page
 * narrows nothing itself.
 */
export function AuditLogPage() {
  const { user } = useAuth();
  const { effective, loading } = useEffectivePermissions();
  const [filters, setFilters] = useState<AuditLogFilters>({});
  const superuser = user?.is_superuser === true;
  // Not while the effective map is on its way: a custom role or direct grant
  // of audit_log (read) may still widen the answer (#891).
  const ownScope = user !== null && !loading && !readsAllAuditLogs(user, effective);

  const content = (heading: React.ReactNode) => (
    <div data-testid="audit-log-page" className="flex flex-col gap-5">
      {heading}
      {ownScope && (
        <p data-testid="audit-log-scope-own" className="text-sm text-slate-600">
          {OWN_SCOPE_NOTE}
        </p>
      )}
      <AuditLogFilter filters={filters} onFilterChange={setFilters} />
      <AuditLogDownload filters={filters} />
      <AuditLogTable filters={filters} />
    </div>
  );

  if (superuser) {
    return (
      <AdminLayout title={TITLE} currentPath="/admin/audit">
        {content(<h2 className="text-xl font-semibold text-slate-900">{TITLE}</h2>)}
      </AdminLayout>
    );
  }

  // No <main> here: AppShell already renders the page's main landmark.
  return (
    <div data-testid="audit-log-plain-layout" className="flex-1 bg-slate-50">
      <PageTitle title={TITLE} />
      <div className="max-w-7xl w-full mx-auto p-6">
        {content(<h1 className="text-xl font-semibold text-slate-900">{TITLE}</h1>)}
      </div>
    </div>
  );
}

export default withAdminGuard(AuditLogPage, { superuser: false, roles: AUDIT_LOG_ROLES });
