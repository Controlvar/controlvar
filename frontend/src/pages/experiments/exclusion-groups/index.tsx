import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { PageTitle } from '@/components/PageTitle';
import { ExclusionGroupForm } from '@/components/experiments/ExclusionGroupForm';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { ExclusionGroupsService } from '@/services/exclusionGroups';
import { ExclusionGroupCreate, ExclusionGroupDetail } from '@/types/eligibility';
import { canReadEligibility } from '@/utils/experimentPermissions';
import {
  GROUP_COPY,
  GroupDraft,
  groupErrorCopy,
  groupStatusLabel,
  isRefusal,
  shareFraction,
  sharePercent,
} from '@/utils/exclusionGroups';

type ListState =
  | { kind: 'loading' }
  | { kind: 'refused' }
  | { kind: 'error'; message: string }
  | { kind: 'loaded'; groups: ExclusionGroupDetail[] };

const NEW_DRAFT: GroupDraft = { name: '', description: '', share: '100' };

/**
 * Exclusion groups (#1455): the list, and "New group".
 *
 * ADMIN, DEVELOPER and superusers (`canReadEligibility`, the API's EXPERIMENT
 * CREATE) read the list and create groups. ANALYST and VIEWER are told who
 * can, and the page sends them no request; neither does it send one before the
 * session has resolved. `exclusion-groups-page`, the capability table's
 * witness, marks only the loaded list (empty or not), never loading, refused
 * or error.
 */
export default function ExclusionGroupsPage() {
  const router = useRouter();
  const auth = useOptionalAuth();
  const user = auth?.user ?? null;
  const resolved = user !== null;
  const mayManage = resolved && canReadEligibility(user);

  const [state, setState] = useState<ListState>({ kind: 'loading' });
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const newButton = useRef<HTMLButtonElement>(null);
  const [returnFocus, setReturnFocus] = useState(false);

  const load = useCallback(async () => {
    setState({ kind: 'loading' });
    try {
      const list = await ExclusionGroupsService.list();
      setState({ kind: 'loaded', groups: Array.isArray(list?.items) ? list.items : [] });
    } catch (err) {
      setState(isRefusal(err) ? { kind: 'refused' } : { kind: 'error', message: groupErrorCopy('list', err) });
    }
  }, []);

  useEffect(() => {
    if (mayManage) void load();
  }, [mayManage, load]);

  useEffect(() => {
    if (returnFocus && !creating) {
      newButton.current?.focus();
      setReturnFocus(false);
    }
  }, [returnFocus, creating]);

  const create = async (draft: GroupDraft) => {
    const fraction = shareFraction(draft.share);
    if (fraction === null) return;
    const body: ExclusionGroupCreate = { name: draft.name, traffic_allocation: fraction };
    if (draft.description !== '') body.description = draft.description;
    setBusy(true);
    setCreateError(null);
    try {
      const created = await ExclusionGroupsService.create(body);
      await router.push(`/experiments/exclusion-groups/${created.id}`);
    } catch (err) {
      setCreateError(groupErrorCopy('save', err));
      setBusy(false);
    }
  };

  const refused = resolved && (!mayManage || state.kind === 'refused');
  const groups = state.kind === 'loaded' ? state.groups : [];

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="Exclusion groups" />
      <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <Link href="/experiments" className="text-sm text-blue-700 underline">
          ← Experiments
        </Link>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-2xl font-bold text-slate-900">Exclusion groups</h1>
          {mayManage && state.kind === 'loaded' && !creating && (
            <button
              ref={newButton}
              type="button"
              onClick={() => {
                setCreateError(null);
                setCreating(true);
              }}
              className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700"
              data-testid="new-exclusion-group-btn"
            >
              New group
            </button>
          )}
        </div>

        {!resolved || (mayManage && state.kind === 'loading') ? (
          <p className="text-sm text-slate-600" role="status" data-testid="exclusion-groups-loading">
            Loading…
          </p>
        ) : null}

        {refused && (
          <p
            className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-700"
            data-testid="exclusion-groups-refused"
          >
            {GROUP_COPY.refused}
          </p>
        )}

        {mayManage && state.kind === 'error' && (
          <div
            role="alert"
            className="rounded-lg bg-red-50 border border-red-200 p-4 text-sm text-red-800"
            data-testid="exclusion-groups-error"
          >
            {state.message}
          </div>
        )}

        {mayManage && state.kind === 'loaded' && (
          <div className="space-y-6" data-testid="exclusion-groups-page">
            {creating && (
              <ExclusionGroupForm
                idPrefix="new-group"
                heading="New group"
                submitLabel="Create group"
                initial={NEW_DRAFT}
                groups={groups}
                busy={busy}
                error={createError}
                onSubmit={(draft) => void create(draft)}
                onCancel={() => {
                  setCreating(false);
                  setCreateError(null);
                  setReturnFocus(true);
                }}
              />
            )}

            {groups.length === 0 ? (
              <p
                className="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-700"
                data-testid="exclusion-groups-empty"
              >
                {GROUP_COPY.empty}
              </p>
            ) : (
              <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
                <table className="w-full text-sm" data-testid="exclusion-groups-table">
                  <thead className="border-b border-slate-200">
                    <tr>
                      <th scope="col" className="text-left px-4 py-3 text-slate-700 font-medium">
                        Name
                      </th>
                      <th scope="col" className="text-left px-4 py-3 text-slate-700 font-medium">
                        Share of traffic
                      </th>
                      <th scope="col" className="text-left px-4 py-3 text-slate-700 font-medium">
                        Experiments
                      </th>
                      <th scope="col" className="text-left px-4 py-3 text-slate-700 font-medium">
                        Status
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {groups.map((group) => (
                      <tr key={group.id} data-testid="exclusion-group-row">
                        <td className="px-4 py-3">
                          <Link
                            href={`/experiments/exclusion-groups/${group.id}`}
                            className="font-medium text-blue-700 underline"
                            data-testid="exclusion-group-link"
                          >
                            {group.name}
                          </Link>
                        </td>
                        <td className="px-4 py-3 text-slate-700 tabular-nums">
                          {sharePercent(group.traffic_allocation)}%
                        </td>
                        <td className="px-4 py-3 text-slate-700 tabular-nums">
                          {Array.isArray(group.experiments) ? group.experiments.length : 0}
                        </td>
                        <td className="px-4 py-3 text-slate-700">{groupStatusLabel(group.status)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
