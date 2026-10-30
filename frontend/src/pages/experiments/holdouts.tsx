import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { PageTitle } from '@/components/PageTitle';
import { HoldoutForm } from '@/components/experiments/HoldoutForm';
import { HoldoutResultsPanel } from '@/components/experiments/HoldoutResultsPanel';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { holdoutAnswer } from '@/hooks/useExperimentEligibility';
import { isApiError } from '@/services/api';
import { EligibilityService } from '@/services/eligibility';
import { HoldoutsService } from '@/services/holdouts';
import { HoldoutCreate, HoldoutDetail } from '@/types/eligibility';
import { canAdministerEligibility, canReadEligibility } from '@/utils/experimentPermissions';
import {
  DEFAULT_SIZE,
  HOLDOUT_COPY,
  HoldoutDraft,
  STATUS_LABELS,
  activateConfirm,
  canActivate,
  endConfirm,
  hasBeenActive,
  holdoutChanges,
  holdoutErrorCopy,
  holdoutLabel,
  holdoutStatus,
  loadErrorCopy,
  localTime,
  sizePercent,
} from '@/utils/holdouts';

/** What the page holds once it has loaded: every holdout for an ADMIN, the active one for a DEVELOPER. */
type Loaded =
  | { kind: 'loaded'; admin: true; holdouts: HoldoutDetail[] }
  | { kind: 'loaded'; admin: false; active: HoldoutDetail | null };

type PageState = { kind: 'loading' } | { kind: 'refused' } | { kind: 'error'; message: string } | Loaded;

type Confirm =
  | { kind: 'activate'; holdout: HoldoutDetail }
  | { kind: 'create'; draft: HoldoutDraft }
  | { kind: 'end'; holdout: HoldoutDetail };

const BUTTON =
  'px-3 py-1.5 rounded-md text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const SECONDARY = `${BUTTON} bg-white text-slate-700 border-slate-300 hover:bg-slate-50`;
const DANGER = `${BUTTON} bg-white text-red-700 border-red-300 hover:bg-red-50`;
const PRIMARY = `${BUTTON} bg-blue-600 text-white border-blue-600 hover:bg-blue-700`;

const NEW_DRAFT: HoldoutDraft = { name: '', description: '', size: String(DEFAULT_SIZE) };

function draftOf(holdout: HoldoutDetail): HoldoutDraft {
  return { name: holdout.name, description: holdout.description ?? '', size: String(holdout.holdout_percentage) };
}

/**
 * The global holdout (#1455; UX SCREEN 3b, the architect's S5, PLAN C4 and C6).
 *
 * The scope note comes first in every state. Then, by role (T134):
 * - ADMIN or a superuser (`canAdministerEligibility`): every holdout from
 *   `GET /api/v1/holdout/all`; create, edit, activate and end; results (beta).
 * - DEVELOPER: the active holdout (`GET /api/v1/holdout`) and its results
 *   (beta). Never `/holdout/all`, which would answer 403.
 * - ANALYST and VIEWER: told who can see it, and no request is sent.
 *
 * Only one holdout is active at a time, so after every activate or end the
 * list is read again rather than patched: the page shows what the server
 * holds, with exactly one "Active". `holdouts-page`, the capability table's
 * witness, marks only the loaded state.
 */
export default function HoldoutsPage() {
  const auth = useOptionalAuth();
  const user = auth?.user ?? null;
  const resolved = user !== null;
  const mayRead = resolved && canReadEligibility(user);
  const isAdmin = mayRead && canAdministerEligibility(user);

  const [state, setState] = useState<PageState>({ kind: 'loading' });
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<HoldoutDetail | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<HoldoutDetail | null>(null);
  const confirmRef = useRef<HTMLDivElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  // The control that opened the confirmation: Cancel and Escape return focus to it.
  const opener = useRef<HTMLElement | null>(null);

  const ask = (next: Confirm, from: HTMLElement | null) => {
    opener.current = from;
    setConfirm(next);
  };

  const cancelConfirm = () => {
    setConfirm(null);
    opener.current?.focus();
  };

  const load = useCallback(async (admin: boolean) => {
    try {
      if (admin) {
        const list = await HoldoutsService.listAll();
        setState({ kind: 'loaded', admin: true, holdouts: Array.isArray(list?.items) ? list.items : [] });
        return;
      }
      const answer = holdoutAnswer(await EligibilityService.activeHoldout());
      if (answer.kind === 'present') setState({ kind: 'loaded', admin: false, active: answer.value as HoldoutDetail });
      else if (answer.kind === 'absent') setState({ kind: 'loaded', admin: false, active: null });
      else setState({ kind: 'error', message: HOLDOUT_COPY.loadFailed });
    } catch (err) {
      if (isApiError(err) && err.status === 403) setState({ kind: 'refused' });
      else setState({ kind: 'error', message: loadErrorCopy(err) });
    }
  }, []);

  useEffect(() => {
    if (!mayRead) return;
    setState({ kind: 'loading' });
    void load(isAdmin);
  }, [mayRead, isAdmin, load]);

  useEffect(() => {
    if (confirm) confirmRef.current?.focus();
  }, [confirm]);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  const holdouts = state.kind === 'loaded' && state.admin ? state.holdouts : [];
  const active =
    state.kind === 'loaded' ? (state.admin ? (state.holdouts.find((h) => h.is_active) ?? null) : state.active) : null;

  /** Send one change, then read the list again whatever the answer (a 409 or 422 means it moved). */
  const change = async (send: () => Promise<unknown>, activates: boolean, onDone?: () => void) => {
    setBusy(true);
    setError(null);
    try {
      await send();
      onDone?.();
    } catch (err) {
      setError(holdoutErrorCopy(err, { activates }));
    }
    await load(true);
    setBusy(false);
  };

  const create = (draft: HoldoutDraft, activateNow: boolean) => {
    const size = sizePercent(draft.size);
    if (size === null) return;
    const body: HoldoutCreate = { name: draft.name, holdout_percentage: size, is_active: activateNow };
    if (draft.description !== '') body.description = draft.description;
    setBusy(true);
    setFormError(null);
    HoldoutsService.create(body).then(
      async () => {
        setCreating(false);
        await load(true);
        setBusy(false);
      },
      async (err: unknown) => {
        setFormError(holdoutErrorCopy(err, { activates: activateNow }));
        // A 409 may mean another holdout moved: read again so the page shows it.
        if (isApiError(err) && err.status === 409) await load(true);
        setBusy(false);
      },
    );
  };

  const submitNew = (draft: HoldoutDraft, activateNow: boolean) => {
    if (activateNow) {
      setFormError(null);
      ask({ kind: 'create', draft }, document.activeElement as HTMLElement | null);
    } else create(draft, false);
  };

  const save = (holdout: HoldoutDetail, draft: HoldoutDraft) => {
    const changes = holdoutChanges(holdout, draft);
    if (Object.keys(changes).length === 0) {
      setEditing(null);
      return;
    }
    setBusy(true);
    setFormError(null);
    HoldoutsService.update(holdout.id, changes).then(
      async () => {
        setEditing(null);
        await load(true);
        setBusy(false);
      },
      async (err: unknown) => {
        setFormError(holdoutErrorCopy(err));
        if (isApiError(err) && (err.status === 409 || err.status === 422)) await load(true);
        setBusy(false);
      },
    );
  };

  const runConfirm = () => {
    if (!confirm) return;
    const current = confirm;
    setConfirm(null);
    if (current.kind === 'create') {
      create(current.draft, true);
    } else if (current.kind === 'activate') {
      void change(() => HoldoutsService.update(current.holdout.id, { is_active: true }), true);
    } else {
      void change(() => HoldoutsService.update(current.holdout.id, { is_active: false }), false);
    }
  };

  const confirmText = (c: Confirm): string => {
    if (c.kind === 'end') return endConfirm(c.holdout.name);
    const target =
      c.kind === 'activate'
        ? c.holdout
        : { name: c.draft.name, holdout_percentage: sizePercent(c.draft.size) ?? DEFAULT_SIZE };
    const other = active && (c.kind === 'create' || active.id !== c.holdout.id) ? active : null;
    return activateConfirm(target, other);
  };

  const refused = resolved && (!mayRead || state.kind === 'refused');
  const loading = !resolved || (mayRead && state.kind === 'loading');

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="Global holdouts" />
      <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <Link href="/experiments" className="text-sm text-blue-700 underline">
          ← Experiments
        </Link>
        <h1 className="text-2xl font-bold text-slate-900">Global holdouts</h1>

        {/* First in every state, never collapsed (UX SCREEN 3b). */}
        <p
          className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-700"
          data-testid="holdout-scope-note"
        >
          {HOLDOUT_COPY.scope}
        </p>

        {loading && (
          <p className="text-sm text-slate-600" role="status" data-testid="holdouts-loading">
            Loading…
          </p>
        )}

        {refused && (
          <p
            className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-700"
            data-testid="holdouts-refused"
          >
            {HOLDOUT_COPY.refused}
          </p>
        )}

        {mayRead && state.kind === 'error' && (
          <div
            role="alert"
            className="rounded-lg bg-red-50 border border-red-200 p-4 text-sm text-red-800"
            data-testid="holdouts-error"
          >
            {state.message}
          </div>
        )}

        {mayRead && state.kind === 'loaded' && (
          <div className="space-y-6" data-testid="holdouts-page">
            <section
              aria-labelledby="holdout-active-heading"
              className="rounded-lg border border-slate-200 bg-white p-6 space-y-3"
            >
              <h2 id="holdout-active-heading" className="text-lg font-semibold text-slate-900">
                Active holdout
              </h2>
              {active ? (
                <div className="space-y-2" data-testid="holdout-active-card">
                  <p className="text-sm text-slate-800" data-testid="holdout-active">
                    {holdoutLabel(active)}
                  </p>
                  {active.activated_at && (
                    <p className="text-sm text-slate-700" data-testid="holdout-active-since">
                      Active since {localTime(active.activated_at)}
                    </p>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {isAdmin && (
                      <button
                        type="button"
                        className={DANGER}
                        disabled={busy}
                        onClick={(event) => ask({ kind: 'end', holdout: active }, event.currentTarget)}
                        data-testid="holdout-end"
                      >
                        End holdout
                      </button>
                    )}
                    <button
                      type="button"
                      className={SECONDARY}
                      onClick={() => setResults(active)}
                      data-testid="holdout-results-open"
                    >
                      Results (beta)
                    </button>
                  </div>
                </div>
              ) : (
                <p className="text-sm text-slate-700" data-testid="holdout-none">
                  {HOLDOUT_COPY.none}
                </p>
              )}
              {!isAdmin && (
                <p className="text-sm text-slate-700" data-testid="holdout-admin-only">
                  {HOLDOUT_COPY.adminOnly}
                </p>
              )}
            </section>

            {confirm && (
              <div
                ref={confirmRef}
                role="dialog"
                aria-label={confirm.kind === 'end' ? 'Confirm End holdout' : 'Confirm Activate'}
                tabIndex={-1}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') cancelConfirm();
                }}
                className="rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-3 focus:outline-none focus:ring-2 focus:ring-blue-600"
                data-testid="holdout-confirm"
              >
                <p className="text-sm text-amber-900">{confirmText(confirm)}</p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    className={SECONDARY}
                    onClick={cancelConfirm}
                    data-testid="holdout-confirm-cancel"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className={confirm.kind === 'end' ? DANGER : PRIMARY}
                    onClick={runConfirm}
                    data-testid="holdout-confirm-yes"
                  >
                    {confirm.kind === 'end' ? 'End holdout' : 'Activate'}
                  </button>
                </div>
              </div>
            )}

            {error && (
              <div
                ref={errorRef}
                tabIndex={-1}
                role="alert"
                className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800"
                data-testid="holdout-action-error"
              >
                {error}
              </div>
            )}

            {isAdmin && (
              <section
                aria-labelledby="holdouts-all-heading"
                className="rounded-lg border border-slate-200 bg-white p-6 space-y-4"
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <h2 id="holdouts-all-heading" className="text-lg font-semibold text-slate-900">
                    All holdouts
                  </h2>
                  {!creating && (
                    <button
                      type="button"
                      className={PRIMARY}
                      disabled={busy}
                      onClick={() => {
                        setEditing(null);
                        setFormError(null);
                        setCreating(true);
                      }}
                      data-testid="new-holdout-btn"
                    >
                      New holdout
                    </button>
                  )}
                </div>

                {creating && (
                  <HoldoutForm
                    idPrefix="new-holdout"
                    heading="New holdout"
                    submitLabel="Create holdout"
                    initial={NEW_DRAFT}
                    holdouts={holdouts}
                    offerActivate
                    busy={busy}
                    error={formError}
                    onSubmit={submitNew}
                    onCancel={() => {
                      setCreating(false);
                      setFormError(null);
                    }}
                  />
                )}

                {holdouts.length > 0 && (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm" data-testid="holdouts-table">
                      <thead className="border-b border-slate-200">
                        <tr>
                          {['Name', 'Size', 'Status', 'Activated', 'Ended', 'Actions'].map((label) => (
                            <th key={label} scope="col" className="text-left px-3 py-2 text-slate-700 font-medium">
                              {label}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {holdouts.map((holdout) => {
                          const status = holdoutStatus(holdout);
                          return (
                            <tr key={holdout.id} data-testid="holdout-row">
                              <td className="px-3 py-2 text-slate-900">{holdout.name}</td>
                              <td className="px-3 py-2 tabular-nums text-slate-800">{holdout.holdout_percentage}%</td>
                              <td className="px-3 py-2 text-slate-800" data-testid="holdout-status">
                                {STATUS_LABELS[status]}
                              </td>
                              <td className="px-3 py-2 text-slate-800">{localTime(holdout.activated_at)}</td>
                              <td className="px-3 py-2 text-slate-800">{localTime(holdout.deactivated_at)}</td>
                              <td className="px-3 py-2">
                                <div className="flex flex-wrap gap-2">
                                  {canActivate(holdout) && (
                                    <button
                                      type="button"
                                      className={PRIMARY}
                                      disabled={busy}
                                      aria-label={`Activate ${holdout.name}`}
                                      onClick={(event) => ask({ kind: 'activate', holdout }, event.currentTarget)}
                                      data-testid="holdout-activate"
                                    >
                                      Activate
                                    </button>
                                  )}
                                  {status === 'active' && (
                                    <button
                                      type="button"
                                      className={DANGER}
                                      disabled={busy}
                                      aria-label={`End ${holdout.name}`}
                                      onClick={(event) => ask({ kind: 'end', holdout }, event.currentTarget)}
                                      data-testid="holdout-row-end"
                                    >
                                      End
                                    </button>
                                  )}
                                  <button
                                    type="button"
                                    className={SECONDARY}
                                    disabled={busy}
                                    aria-label={`Edit ${holdout.name}`}
                                    onClick={() => {
                                      setCreating(false);
                                      setFormError(null);
                                      setEditing(holdout);
                                    }}
                                    data-testid="holdout-edit"
                                  >
                                    Edit
                                  </button>
                                  {status !== 'not_activated' && (
                                    <button
                                      type="button"
                                      className={SECONDARY}
                                      aria-label={`Results (beta) for ${holdout.name}`}
                                      onClick={() => setResults(holdout)}
                                      data-testid="holdout-row-results"
                                    >
                                      Results (beta)
                                    </button>
                                  )}
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}

                {editing && (
                  <HoldoutForm
                    key={editing.id}
                    idPrefix="edit-holdout"
                    heading={`Edit ${editing.name}`}
                    submitLabel="Save changes"
                    initial={draftOf(editing)}
                    holdouts={holdouts}
                    selfId={editing.id}
                    sizeLocked={hasBeenActive(editing)}
                    busy={busy}
                    error={formError}
                    onSubmit={(draft) => save(editing, draft)}
                    onCancel={() => {
                      setEditing(null);
                      setFormError(null);
                    }}
                  />
                )}
              </section>
            )}

            {results && <HoldoutResultsPanel key={results.id} holdoutId={results.id} holdoutName={results.name} />}
          </div>
        )}
      </div>
    </div>
  );
}
