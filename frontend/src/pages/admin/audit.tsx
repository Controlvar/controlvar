import React, { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/router';
import { AdminLayout } from '@/components/admin/AdminLayout';
import { PageTitle } from '@/components/PageTitle';
import { AuditLogDownload } from '@/components/admin/audit/AuditLogDownload';
import { AuditLogFilter, AuditLogFilters } from '@/components/admin/audit/AuditLogFilter';
import { AuditLogTable } from '@/components/admin/audit/AuditLogTable';
import { AUDIT_LOG_ROLES, readsAllAuditLogs } from '@/components/admin/audit/access';
import { entityLabel } from '@/components/admin/audit/actionLabels';
import { AuditScope, parseAuditScope, scopeQuery } from '@/components/admin/audit/scope';
import { withAdminGuard } from '@/components/admin/withAdminGuard';
import { useAuth } from '@/contexts/AuthContext';
import { useEffectivePermissions } from '@/contexts/EffectivePermissionsContext';

const TITLE = 'Audit Log';

/** Shown when a History link's address carried a value the API would refuse. */
export const SCOPE_IGNORED_NOTE =
  'The address named an item or person this page cannot filter by, so every entry is listed.';

const SCOPE_KEYS = ['entity_type', 'entity_id', 'user_id'] as const;

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
  const router = useRouter();
  const [filters, setFilters] = useState<AuditLogFilters>({});
  const [scopeIgnored, setScopeIgnored] = useState(false);
  // The list is not asked for until the address has been read, so a History
  // link never first lists everything and then narrows.
  const [scopeRead, setScopeRead] = useState(false);
  const superuser = user?.is_superuser === true;
  // Not while the effective map is on its way: a custom role or direct grant
  // of audit_log (read) may still widen the answer (#891).
  const ownScope = user !== null && !loading && !readsAllAuditLogs(user, effective);
  const showPerson = user !== null && !loading && readsAllAuditLogs(user, effective);

  const ready = router.isReady;
  const urlScope = SCOPE_KEYS.map((key) => {
    const value = router.query[key];
    return typeof value === 'string' ? value : '';
  }).join('|');
  useEffect(() => {
    if (!ready) return;
    const { scope, ignored } = parseAuditScope(router.query);
    setFilters((current) => {
      const same = SCOPE_KEYS.every((key) => current[key] === scope[key]);
      return same ? current : { ...current, entity_type: scope.entity_type, entity_id: scope.entity_id, user_id: scope.user_id };
    });
    setScopeIgnored(ignored);
    setScopeRead(true);
    // `urlScope` stands for the three query values this reads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, urlScope]);

  // Keep the address in step with the item and person filters, so the page
  // can be shared and reloaded.
  const change = (next: AuditLogFilters) => {
    setFilters(next);
    const scope: AuditScope = { entity_type: next.entity_type, entity_id: next.entity_id, user_id: next.user_id };
    if (SCOPE_KEYS.some((key) => (router.query[key] ?? undefined) !== (scope[key] ?? undefined))) {
      void router.replace({ pathname: router.pathname, query: scopeQuery(scope) }, undefined, { shallow: true });
    }
  };

  // A person filter means nothing to someone who sees only their own entries.
  const applied = useMemo<AuditLogFilters>(
    () => (showPerson || !filters.user_id ? filters : { ...filters, user_id: undefined }),
    [filters, showPerson],
  );

  const content = (heading: React.ReactNode) => (
    <div data-testid="audit-log-page" className="flex flex-col gap-5">
      {heading}
      {ownScope && (
        <p data-testid="audit-log-scope-own" className="text-sm text-slate-600">
          {OWN_SCOPE_NOTE}
        </p>
      )}
      {scopeIgnored && (
        <p data-testid="audit-scope-ignored" role="status" className="text-sm text-slate-700">
          {SCOPE_IGNORED_NOTE}
        </p>
      )}
      {filters.entity_id && (
        <p
          data-testid="audit-scope-item"
          className="flex flex-wrap items-center gap-3 text-sm text-slate-800 bg-blue-50 border border-blue-200 rounded px-3 py-2"
        >
          <span>
            Showing changes to {filters.entity_type ? entityLabel(filters.entity_type).toLowerCase() : 'item'}{' '}
            <span className="font-mono">{filters.entity_id}</span>
          </span>
          <button
            type="button"
            data-testid="audit-scope-remove"
            onClick={() => change({ ...filters, entity_id: undefined })}
            className="text-blue-700 underline-offset-2 hover:underline focus:outline-none focus:ring-2 focus:ring-blue-500 rounded"
          >
            Remove this filter
          </button>
        </p>
      )}
      <AuditLogFilter filters={applied} onFilterChange={change} showPerson={showPerson} />
      {scopeRead && (
        <>
          <AuditLogDownload filters={applied} />
          <AuditLogTable filters={applied} />
        </>
      )}
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
