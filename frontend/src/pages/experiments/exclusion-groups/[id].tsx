import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { PageTitle } from '@/components/PageTitle';
import { ExclusionGroupForm } from '@/components/experiments/ExclusionGroupForm';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { ExclusionGroupsService } from '@/services/exclusionGroups';
import { ExperimentsService } from '@/services/experiments';
import { ExclusionGroupDetail, ExclusionGroupMember } from '@/types/eligibility';
import { Experiment, experimentStatusLabel, ExperimentStatus } from '@/types/experiments';
import { canAdministerEligibility, canReadEligibility } from '@/utils/experimentPermissions';
import {
  GROUP_COPY,
  GroupDraft,
  archiveConfirm,
  groupChanges,
  groupErrorCopy,
  groupStatusLabel,
  isRefusal,
  removeConfirm,
  sharePercent,
} from '@/utils/exclusionGroups';

type GroupState =
  | { kind: 'loading' }
  | { kind: 'refused' }
  | { kind: 'error'; message: string }
  | { kind: 'loaded'; group: ExclusionGroupDetail };

/** The statuses the picker offers: an experiment that has not finished. */
const CANDIDATE_STATUSES: ExperimentStatus[] = ['draft', 'active', 'paused'];

/** The API's largest page of experiments (`GET /api/v1/experiments/`, `limit` ≤ 500). */
const CANDIDATE_PAGE = 500;

const BUTTON =
  'px-3 py-1.5 rounded-md text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const SECONDARY = `${BUTTON} bg-white text-slate-700 border-slate-300 hover:bg-slate-50`;
const DANGER = `${BUTTON} bg-white text-red-700 border-red-300 hover:bg-red-50`;
const PRIMARY = `${BUTTON} bg-blue-600 text-white border-blue-600 hover:bg-blue-700`;

function draftOf(group: ExclusionGroupDetail): GroupDraft {
  return {
    name: group.name,
    description: group.description ?? '',
    share: String(sharePercent(group.traffic_allocation)),
  };
}

/** The draft, active and paused experiments in no group, each status page read once. */
async function loadCandidates(): Promise<Experiment[]> {
  const pages = await Promise.all(
    CANDIDATE_STATUSES.map((status) => ExperimentsService.list({ status, limit: CANDIDATE_PAGE })),
  );
  return pages
    .flatMap((page) => (Array.isArray(page?.items) ? page.items : []))
    .filter((experiment) => CANDIDATE_STATUSES.includes(experiment.status) && !experiment.mutual_exclusion_group_id);
}

/**
 * One exclusion group (#1455): its experiments, adding and removing them, its
 * name, description and share of traffic, and, for an ADMIN, archiving.
 *
 * Membership lives here only (PLAN C5): the experiment page's group row links
 * to this page. The picker offers draft, active and paused experiments that
 * are in no group, and is not offered for an archived group; Remove stays, so
 * an experiment can be freed. Every change re-reads the group.
 *
 * Roles: ADMIN, DEVELOPER and superusers (`canReadEligibility`) do all of it
 * but archiving, which is `canAdministerEligibility` (ADMIN or a superuser).
 * ANALYST and VIEWER are told who can, and no request is sent for them.
 */
export default function ExclusionGroupPage() {
  const router = useRouter();
  const rawId = router.query.id;
  const id = typeof rawId === 'string' && rawId !== '' ? rawId : null;
  const auth = useOptionalAuth();
  const user = auth?.user ?? null;
  const resolved = user !== null;
  const mayManage = resolved && canReadEligibility(user);
  const mayAdminister = canAdministerEligibility(user);

  const [state, setState] = useState<GroupState>({ kind: 'loading' });
  const [allGroups, setAllGroups] = useState<ExclusionGroupDetail[]>([]);
  const [candidates, setCandidates] = useState<Experiment[] | null>(null);
  const [candidatesFailed, setCandidatesFailed] = useState(false);
  const [chosen, setChosen] = useState('');
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<'save' | 'add' | 'remove' | 'archive' | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState<ExclusionGroupMember | null>(null);
  const [confirmArchive, setConfirmArchive] = useState(false);
  // Where focus goes once a form or a confirmation closes, applied after the
  // render that brings the control back.
  const [returnFocus, setReturnFocus] = useState<'edit' | 'archive' | 'members' | null>(null);

  const editButton = useRef<HTMLButtonElement>(null);
  const membersHeading = useRef<HTMLHeadingElement>(null);
  const archiveButton = useRef<HTMLButtonElement>(null);
  const removeRegion = useRef<HTMLDivElement>(null);
  const archiveRegion = useRef<HTMLDivElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (returnFocus === null || busy !== null) return;
    const target = { edit: editButton, archive: archiveButton, members: membersHeading }[returnFocus];
    target.current?.focus();
    setReturnFocus(null);
  }, [returnFocus, busy, editing, confirmArchive]);

  const readGroup = useCallback(async (groupId: string) => {
    try {
      const group = await ExclusionGroupsService.get(groupId);
      setState({ kind: 'loaded', group });
      return group;
    } catch (err) {
      setState(isRefusal(err) ? { kind: 'refused' } : { kind: 'error', message: groupErrorCopy('load', err) });
      return null;
    }
  }, []);

  const readCandidates = useCallback(async () => {
    try {
      const found = await loadCandidates();
      setCandidates(found);
      setCandidatesFailed(false);
      setChosen((current) => (found.some((e) => e.id === current) ? current : (found[0]?.id ?? '')));
    } catch {
      setCandidates(null);
      setCandidatesFailed(true);
    }
  }, []);

  useEffect(() => {
    if (!mayManage || id === null) return;
    let current = true;
    setState({ kind: 'loading' });
    void (async () => {
      const group = await readGroup(id);
      if (!current || group === null) return;
      // The other groups, for the rename check; the server's 409 stays the guarantee.
      ExclusionGroupsService.list().then(
        (list) => {
          if (current) setAllGroups(Array.isArray(list?.items) ? list.items : []);
        },
        () => undefined,
      );
      if (group.status !== 'archived') await readCandidates();
    })();
    return () => {
      current = false;
    };
  }, [mayManage, id, readGroup, readCandidates]);

  useEffect(() => {
    if (confirmRemove) removeRegion.current?.focus();
  }, [confirmRemove]);

  useEffect(() => {
    if (confirmArchive) archiveRegion.current?.focus();
  }, [confirmArchive]);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  /** After a change: the group again, and the picker when the group takes experiments. */
  const refresh = async (groupId: string) => {
    const group = await readGroup(groupId);
    if (group && group.status !== 'archived') await readCandidates();
  };

  const save = async (group: ExclusionGroupDetail, draft: GroupDraft) => {
    const changes = groupChanges(group, draft);
    if (Object.keys(changes).length === 0) {
      setEditing(false);
      setReturnFocus('edit');
      return;
    }
    setBusy('save');
    setFormError(null);
    try {
      const updated = await ExclusionGroupsService.update(group.id, changes);
      setState({ kind: 'loaded', group: { ...group, ...updated } });
      setAllGroups((groups) => groups.map((g) => (g.id === group.id ? { ...g, name: updated.name } : g)));
      setEditing(false);
      setReturnFocus('edit');
    } catch (err) {
      setFormError(groupErrorCopy('save', err));
    } finally {
      setBusy(null);
    }
  };

  const add = async (group: ExclusionGroupDetail) => {
    if (!chosen) return;
    setBusy('add');
    setError(null);
    try {
      await ExclusionGroupsService.addExperiment(group.id, chosen);
    } catch (err) {
      setError(groupErrorCopy('add', err));
    }
    await refresh(group.id);
    setBusy(null);
  };

  const remove = async (group: ExclusionGroupDetail, member: ExclusionGroupMember) => {
    setConfirmRemove(null);
    setBusy('remove');
    setError(null);
    try {
      await ExclusionGroupsService.removeExperiment(group.id, member.id);
      setReturnFocus('members');
    } catch (err) {
      setError(groupErrorCopy('remove', err));
    }
    await refresh(group.id);
    setBusy(null);
  };

  const setArchived = async (group: ExclusionGroupDetail, archived: boolean) => {
    setConfirmArchive(false);
    setBusy('archive');
    setError(null);
    try {
      if (archived) await ExclusionGroupsService.archive(group.id);
      else await ExclusionGroupsService.unarchive(group.id);
    } catch (err) {
      setError(groupErrorCopy('archive', err));
    }
    await refresh(group.id);
    setBusy(null);
  };

  const refused = resolved && (!mayManage || state.kind === 'refused');
  const group = mayManage && state.kind === 'loaded' ? state.group : null;
  const archived = group?.status === 'archived';
  const members = group && Array.isArray(group.experiments) ? group.experiments : [];

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title={group ? group.name : 'Exclusion group'} />
      <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <Link href="/experiments/exclusion-groups" className="text-sm text-blue-700 underline">
          ← Exclusion groups
        </Link>

        {(!resolved || (mayManage && state.kind === 'loading')) && (
          <p className="text-sm text-slate-600" role="status" data-testid="exclusion-group-loading">
            Loading…
          </p>
        )}

        {refused && (
          <p
            className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-700"
            data-testid="exclusion-group-refused"
          >
            {GROUP_COPY.refused}
          </p>
        )}

        {mayManage && state.kind === 'error' && (
          <div
            role="alert"
            className="rounded-lg bg-red-50 border border-red-200 p-4 text-sm text-red-800"
            data-testid="exclusion-group-error"
          >
            {state.message}
          </div>
        )}

        {group && (
          <div className="space-y-6" data-testid="exclusion-group-detail">
            <header className="rounded-lg border border-slate-200 bg-white p-6 space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <h1 className="text-2xl font-bold text-slate-900" data-testid="exclusion-group-name-heading">
                  {group.name}
                </h1>
                <span
                  className="inline-flex px-2 py-0.5 rounded-full text-xs font-medium border border-slate-300 text-slate-700"
                  data-testid="exclusion-group-status"
                >
                  {groupStatusLabel(group.status)}
                </span>
              </div>
              {group.description ? <p className="text-sm text-slate-700">{group.description}</p> : null}
              <dl className="text-sm text-slate-700">
                <div className="flex items-baseline gap-1.5">
                  <dt className="text-slate-600">Share of traffic</dt>
                  <dd className="tabular-nums" data-testid="exclusion-group-share-value">
                    {sharePercent(group.traffic_allocation)}%
                  </dd>
                </div>
              </dl>
              {archived && (
                <p className="text-sm text-amber-900" data-testid="exclusion-group-archived-note">
                  {GROUP_COPY.archivedNote}
                </p>
              )}
              {!editing && (
                <button
                  ref={editButton}
                  type="button"
                  className={SECONDARY}
                  disabled={busy !== null}
                  onClick={() => {
                    setFormError(null);
                    setEditing(true);
                  }}
                  data-testid="exclusion-group-edit"
                >
                  Edit
                </button>
              )}
            </header>

            {editing && (
              <ExclusionGroupForm
                idPrefix="edit-group"
                heading="Edit group"
                submitLabel="Save changes"
                initial={draftOf(group)}
                groups={allGroups}
                selfId={group.id}
                busy={busy === 'save'}
                error={formError}
                onSubmit={(draft) => void save(group, draft)}
                onCancel={() => {
                  setEditing(false);
                  setFormError(null);
                  setReturnFocus('edit');
                }}
              />
            )}

            <section
              className="rounded-lg border border-slate-200 bg-white p-6 space-y-4"
              aria-labelledby="exclusion-group-members-heading"
              data-testid="exclusion-group-members"
            >
              <h2
                ref={membersHeading}
                tabIndex={-1}
                id="exclusion-group-members-heading"
                className="text-lg font-semibold text-slate-900 focus:outline-none"
              >
                Experiments
              </h2>
              <p className="text-sm text-slate-700" data-testid="exclusion-group-membership-note">
                {GROUP_COPY.membership}
              </p>

              {members.length === 0 ? (
                <p className="text-sm text-slate-700" data-testid="exclusion-group-no-members">
                  None
                </p>
              ) : (
                <ul className="divide-y divide-slate-100" data-testid="exclusion-group-member-list">
                  {members.map((member) => (
                    <li
                      key={member.id}
                      className="flex flex-wrap items-center justify-between gap-2 py-2"
                      data-testid="exclusion-group-member"
                    >
                      <span className="text-sm text-slate-800">
                        <Link href={`/experiments/${member.id}`} className="text-blue-700 underline">
                          {member.name}
                        </Link>{' '}
                        <span className="text-slate-600">
                          ({experimentStatusLabel(member.status as ExperimentStatus)})
                        </span>
                      </span>
                      <button
                        type="button"
                        className={SECONDARY}
                        disabled={busy !== null}
                        aria-label={`Remove ${member.name}`}
                        onClick={() => {
                          setError(null);
                          setConfirmRemove(member);
                        }}
                        data-testid="exclusion-group-remove"
                      >
                        Remove
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              {confirmRemove && (
                <div
                  ref={removeRegion}
                  role="dialog"
                  aria-label={`Confirm Remove ${confirmRemove.name}`}
                  tabIndex={-1}
                  onKeyDown={(event) => {
                    if (event.key === 'Escape') {
                      setConfirmRemove(null);
                      setReturnFocus('members');
                    }
                  }}
                  className="rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-3 focus:outline-none focus:ring-2 focus:ring-blue-600"
                  data-testid="exclusion-group-remove-confirm"
                >
                  <p className="text-sm text-amber-900">{removeConfirm(confirmRemove.name)}</p>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      className={SECONDARY}
                      onClick={() => {
                        setConfirmRemove(null);
                        setReturnFocus('members');
                      }}
                      data-testid="exclusion-group-remove-cancel"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      className={DANGER}
                      onClick={() => void remove(group, confirmRemove)}
                      data-testid="exclusion-group-remove-yes"
                    >
                      Remove
                    </button>
                  </div>
                </div>
              )}

              {!archived && (
                <div className="space-y-2" data-testid="exclusion-group-add-section">
                  {candidates === null && !candidatesFailed && (
                    <p className="text-sm text-slate-600" data-testid="exclusion-group-candidates-loading">
                      Loading…
                    </p>
                  )}
                  {candidatesFailed && (
                    <p className="text-sm text-red-800" data-testid="exclusion-group-candidates-error">
                      {GROUP_COPY.loadFailed}
                    </p>
                  )}
                  {candidates !== null && candidates.length === 0 && (
                    <p className="text-sm text-slate-700" data-testid="exclusion-group-no-candidates">
                      {GROUP_COPY.noCandidates}
                    </p>
                  )}
                  {candidates !== null && candidates.length > 0 && (
                    <div className="flex flex-wrap items-end gap-2">
                      <div>
                        <label htmlFor="exclusion-group-add-select" className="block text-sm font-medium text-slate-700">
                          Add an experiment
                        </label>
                        <select
                          id="exclusion-group-add-select"
                          value={chosen}
                          onChange={(event) => setChosen(event.target.value)}
                          className="mt-1 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900"
                          data-testid="exclusion-group-add-select"
                        >
                          {candidates.map((experiment) => (
                            <option key={experiment.id} value={experiment.id}>
                              {experiment.name} ({experimentStatusLabel(experiment.status)})
                            </option>
                          ))}
                        </select>
                      </div>
                      <button
                        type="button"
                        className={PRIMARY}
                        disabled={busy !== null || !chosen}
                        onClick={() => void add(group)}
                        data-testid="exclusion-group-add"
                      >
                        {busy === 'add' ? 'Adding…' : 'Add'}
                      </button>
                    </div>
                  )}
                </div>
              )}
            </section>

            <section
              className="rounded-lg border border-slate-200 bg-white p-6 space-y-3"
              aria-labelledby="exclusion-group-archive-heading"
              data-testid="exclusion-group-archive-section"
            >
              <h2 id="exclusion-group-archive-heading" className="text-lg font-semibold text-slate-900">
                {archived ? 'Unarchive' : 'Archive'}
              </h2>
              {!mayAdminister && (
                <p className="text-sm text-slate-700" data-testid="exclusion-group-archive-role-note">
                  {GROUP_COPY.archiveAdminOnly}
                </p>
              )}
              {mayAdminister && archived && (
                <button
                  type="button"
                  className={SECONDARY}
                  disabled={busy !== null}
                  onClick={() => void setArchived(group, false)}
                  data-testid="exclusion-group-unarchive"
                >
                  {busy === 'archive' ? 'Saving…' : 'Unarchive'}
                </button>
              )}
              {mayAdminister && !archived && !confirmArchive && (
                <button
                  ref={archiveButton}
                  type="button"
                  className={DANGER}
                  disabled={busy !== null}
                  onClick={() => {
                    setError(null);
                    setConfirmArchive(true);
                  }}
                  data-testid="exclusion-group-archive"
                >
                  {busy === 'archive' ? 'Saving…' : 'Archive group'}
                </button>
              )}
              {mayAdminister && !archived && confirmArchive && (
                <div
                  ref={archiveRegion}
                  role="dialog"
                  aria-label="Confirm Archive group"
                  tabIndex={-1}
                  onKeyDown={(event) => {
                    if (event.key === 'Escape') {
                      setConfirmArchive(false);
                      setReturnFocus('archive');
                    }
                  }}
                  className="rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-3 focus:outline-none focus:ring-2 focus:ring-blue-600"
                  data-testid="exclusion-group-archive-confirm"
                >
                  <p className="text-sm text-amber-900">{archiveConfirm(group.name)}</p>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      className={SECONDARY}
                      onClick={() => {
                        setConfirmArchive(false);
                        setReturnFocus('archive');
                      }}
                      data-testid="exclusion-group-archive-cancel"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      className={DANGER}
                      onClick={() => void setArchived(group, true)}
                      data-testid="exclusion-group-archive-yes"
                    >
                      Archive
                    </button>
                  </div>
                </div>
              )}
            </section>

            {error && (
              <div
                ref={errorRef}
                tabIndex={-1}
                role="alert"
                className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800"
                data-testid="exclusion-group-action-error"
              >
                {error}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
