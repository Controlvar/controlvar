import React, { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { PageTitle } from '@/components/PageTitle';
import { BanditWeightsSection } from '@/components/experiments/BanditWeightsSection';
import { ExperimentManageSection } from '@/components/experiments/ExperimentManageSection';
import { TargetingSection } from '@/components/experiments/TargetingSection';
import { ApiKeyRouteHint } from '@/components/experiments/ApiKeyRouteHint';
import WarehouseAnalysisSection from '@modules/components/warehouse/runs/WarehouseAnalysisSection';
import { useAuth } from '@/contexts/AuthContext';
import { EligibilityUser, useExperimentEligibility } from '@/hooks/useExperimentEligibility';
import { isApiError } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { ExperimentsService } from '@/services/experiments';
import { GlobalHoldout, MutualExclusionGroup } from '@/types/eligibility';
import {
  Experiment,
  ExperimentStatus,
  METRIC_TYPE_LABELS,
  MetricType,
  OPTIMIZATION_TYPE_LABELS,
  OptimizationType,
  experimentStatusColor,
  experimentStatusLabel,
  experimentTypeLabel,
} from '@/types/experiments';
import { canChangeExperiment } from '@/utils/experimentPermissions';
import { correctionName, formatConfidence } from '@/components/results/shared/analysisSettings';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

/** Lifecycle transitions exposed per status (mirrors the backend guards). */
export type LifecycleAction = 'start' | 'pause' | 'complete' | 'archive';

export const ACTIONS_BY_STATUS: Record<ExperimentStatus, LifecycleAction[]> = {
  draft: ['start'],
  active: ['pause', 'complete'],
  paused: ['start', 'complete'],
  completed: ['archive'],
  archived: [],
};

const ACTION_META: Record<
  LifecycleAction,
  { label: string; pending: string; className: string; confirm?: string }
> = {
  start: {
    label: 'Start',
    pending: 'Starting…',
    className: 'bg-green-600 text-white hover:bg-green-700',
  },
  pause: {
    label: 'Pause',
    pending: 'Pausing…',
    className: 'bg-white text-slate-700 border border-slate-300 hover:bg-slate-50',
  },
  complete: {
    label: 'Complete',
    pending: 'Completing…',
    className: 'bg-blue-600 text-white hover:bg-blue-700',
    confirm: 'Complete this experiment? Assignment stops and the status cannot be reverted.',
  },
  archive: {
    label: 'Archive',
    pending: 'Archiving…',
    className: 'bg-white text-slate-700 border border-slate-300 hover:bg-slate-50',
    confirm: 'Archive this experiment? It will be hidden from the default list.',
  },
};

function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const d = parseApiTimestamp(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

/**
 * The traffic algorithm of an adaptive experiment in words, or null for a
 * fixed split (or none given). An algorithm this page does not know is shown
 * as the API names it rather than hidden.
 */
export function algorithmLabel(optimizationType: string | null | undefined): string | null {
  if (!optimizationType || optimizationType === 'fixed') return null;
  return OPTIMIZATION_TYPE_LABELS[optimizationType as OptimizationType] ?? optimizationType;
}

/** The header's term for an experiment's start or end date. */
export type ScheduleTerm = 'Starts' | 'Started' | 'Start date' | 'Ends' | 'Ended' | 'End date';

/**
 * How the header names an experiment's start or end date (#1443), and whether
 * that date has come without the experiment starting or ending ("due").
 *
 * The status decides first, never the browser's clock, so a clock that is
 * wrong can never write "Started" or "Ended":
 *
 * - start: active, paused or completed → "Started"; archived → "Start date";
 *   anything else (a draft) → "Starts".
 * - end: completed → "Ended"; archived → "End date"; anything else → "Ends".
 *
 * An archived experiment gets the neutral terms because the API archives a
 * draft too, so archived does not mean it ever started (plan-1449 C1). Only
 * "Starts" and "Ends" can be due: the date is at or before `nowMs`, the
 * scheduler's own `<=`, which starts a draft and ends an active experiment on
 * its next pass and never ends a paused one. A value that is not a time is
 * never due. The value is the API's, so it is read with `parseApiTimestamp`:
 * one without a zone is UTC (#1134).
 *
 * The caller passes the clock. Nothing re-renders the page on a timer; any
 * re-render reads the clock again.
 */
export function scheduleLabel(
  which: 'start' | 'end',
  value: string,
  status: string,
  nowMs: number,
): { term: ScheduleTerm; due: boolean } {
  if (status === 'archived') {
    return { term: which === 'start' ? 'Start date' : 'End date', due: false };
  }
  const happened =
    which === 'start'
      ? status === 'active' || status === 'paused' || status === 'completed'
      : status === 'completed';
  if (happened) return { term: which === 'start' ? 'Started' : 'Ended', due: false };
  return {
    term: which === 'start' ? 'Starts' : 'Ends',
    due: parseApiTimestamp(value).getTime() <= nowMs,
  };
}

const DUE_NOTE: Record<'start' | 'end', string> = {
  start: ' (due, not started yet)',
  end: ' (due, not ended yet)',
};

/**
 * The words of the "Exclusion group" and "Global holdout" rows (#1449). Fixed
 * copy: nothing the server sends in an error is shown, nor the group's
 * description or its experiments, nor the holdout's id.
 */
export const ELIGIBILITY_COPY = {
  loading: 'Loading…',
  groupNone: 'None',
  groupArchived: ' (archived; it no longer applies)',
  groupRefused: 'In a group. Seeing its name requires the ADMIN or DEVELOPER role.',
  groupError: 'In a group. Its name could not be loaded; reload the page to try again.',
  holdoutNone: 'None active',
  holdoutRefused: 'Seeing this requires the ADMIN or DEVELOPER role.',
  holdoutError: 'Could not be loaded; reload the page to try again.',
} as const;

/** An exclusion group as its row shows it: the name, marked when archived. */
export function groupLabel(group: MutualExclusionGroup): string {
  return group.status === 'archived' ? `${group.name}${ELIGIBILITY_COPY.groupArchived}` : group.name;
}

/** The active global holdout as its row shows it. It applies to every experiment, not this one. */
export function holdoutLabel(holdout: GlobalHoldout): string {
  return `${holdout.name}: keeps ${holdout.holdout_percentage}% of new users out of every experiment`;
}

/**
 * The header's "Exclusion group" and "Global holdout" items. A component of
 * its own so that its reads start only once the experiment has loaded (the
 * page renders the header only then), and in this file so that the header's
 * term pin (a11y/grey-text-contrast.test.ts) reads its dt's too.
 *
 * Each state has its own test id; `experiment-exclusion-group` and
 * `experiment-holdout`, the capability table's witnesses, mark only the
 * answered state, never loading, refused or error.
 */
function EligibilityRows({
  groupId,
  user,
}: {
  groupId: string | null | undefined;
  user: EligibilityUser | null;
}) {
  const { group, holdout } = useExperimentEligibility(groupId, user);
  return (
    <>
      <div className="flex items-baseline gap-1.5">
        <dt className="text-slate-500">Exclusion group</dt>
        {group.kind === 'absent' && <dd data-testid="experiment-exclusion-group">{ELIGIBILITY_COPY.groupNone}</dd>}
        {group.kind === 'present' && (
          <dd data-testid="experiment-exclusion-group">{groupLabel(group.value)}</dd>
        )}
        {group.kind === 'loading' && (
          <dd data-testid="experiment-exclusion-group-loading">{ELIGIBILITY_COPY.loading}</dd>
        )}
        {group.kind === 'refused' && (
          <dd data-testid="experiment-exclusion-group-refused">{ELIGIBILITY_COPY.groupRefused}</dd>
        )}
        {group.kind === 'error' && (
          <dd data-testid="experiment-exclusion-group-error">{ELIGIBILITY_COPY.groupError}</dd>
        )}
      </div>
      <div className="flex items-baseline gap-1.5">
        <dt className="text-slate-500">Global holdout</dt>
        {holdout.kind === 'absent' && <dd data-testid="experiment-holdout">{ELIGIBILITY_COPY.holdoutNone}</dd>}
        {holdout.kind === 'present' && <dd data-testid="experiment-holdout">{holdoutLabel(holdout.value)}</dd>}
        {holdout.kind === 'loading' && (
          <dd data-testid="experiment-holdout-loading">{ELIGIBILITY_COPY.loading}</dd>
        )}
        {holdout.kind === 'refused' && (
          <dd data-testid="experiment-holdout-refused">{ELIGIBILITY_COPY.holdoutRefused}</dd>
        )}
        {holdout.kind === 'error' && (
          <dd data-testid="experiment-holdout-error">{ELIGIBILITY_COPY.holdoutError}</dd>
        )}
      </div>
    </>
  );
}

/** A link in the note under the header: always underlined, never on hover only (WCAG 1.4.1). */
const NOTE_LINK = 'text-blue-700 underline';

/**
 * A variant's configuration as the page shows it: indented JSON, rendered as
 * text (never as markup), or null when the variant has none.
 */
export function formatConfiguration(configuration: unknown): string | null {
  if (configuration === null || configuration === undefined) return null;
  return JSON.stringify(configuration, null, 2);
}

function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

/**
 * The experiment a change answered with, keeping the owner's name from the
 * read (#921). Only `GET /experiments/{id}` names the owner; the lifecycle
 * routes and the edit and targeting saves answer without `owner_name`, so
 * taking their answer as it is would turn the name back into a short id.
 */
export function keepOwnerName(previous: Experiment | null, updated: Experiment): Experiment {
  if (updated.owner_name !== undefined) return updated;
  const sameOwner = previous !== null && previous.owner_id === updated.owner_id;
  return { ...updated, owner_name: sameOwner ? (previous.owner_name ?? null) : null };
}

export default function ExperimentDetailPage() {
  const router = useRouter();
  const { user } = useAuth();
  const rawId = router.query.id;
  const id = typeof rawId === 'string' ? rawId : undefined;

  const [experiment, setExperiment] = useState<Experiment | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<{ status: number; message: string } | null>(null);
  const [pendingAction, setPendingAction] = useState<LifecycleAction | null>(null);
  const [confirming, setConfirming] = useState<LifecycleAction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // While "Edit variants and metrics" is open its form stands in for the
  // read-only Variants and Metrics cards. The form exists on a draft only.
  const [designOpen, setDesignOpen] = useState(false);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setLoadError(null);
    try {
      const data = await ExperimentsService.get(id);
      setExperiment(data);
    } catch (err) {
      const status = isApiError(err) ? err.status : 0;
      setLoadError({
        status,
        message: err instanceof Error ? err.message : 'Failed to load experiment',
      });
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  // Every change answers without the owner's name, so it is merged, not set.
  const onSaved = useCallback((updated: Experiment) => {
    setExperiment((previous) => keepOwnerName(previous, updated));
  }, []);

  const runAction = async (action: LifecycleAction) => {
    if (!experiment) return;
    setConfirming(null);
    setActionError(null);
    setPendingAction(action);
    try {
      const updated = await ExperimentsService[action](experiment.id);
      onSaved(updated);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : `Failed to ${action} experiment`);
    } finally {
      setPendingAction(null);
    }
  };

  const onActionClick = (action: LifecycleAction) => {
    if (ACTION_META[action].confirm) {
      setActionError(null);
      setConfirming(action);
      return;
    }
    void runAction(action);
  };

  const copyKey = async () => {
    if (!experiment?.key) return;
    try {
      await navigator.clipboard?.writeText(experiment.key);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };

  if (!id || loading) {
    return (
      <div className="flex-1 bg-slate-50 flex items-center justify-center">
        <PageTitle title="Experiment" />
        <div className="text-center" data-testid="experiment-loading">
          <div className="inline-block w-6 h-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
          <p className="text-slate-500 mt-2 text-sm">Loading experiment…</p>
        </div>
      </div>
    );
  }

  if (loadError || !experiment) {
    const notFound = loadError?.status === 404;
    return (
      <div className="flex-1 bg-slate-50 flex items-center justify-center">
        <PageTitle title={notFound ? 'Experiment not found' : 'Experiment'} />
        <div className="text-center max-w-md px-4" data-testid={notFound ? 'experiment-not-found' : 'experiment-error'}>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
            {notFound ? '404' : 'Error'}
          </p>
          <p className="text-slate-800 text-lg mb-2">
            {notFound ? 'Experiment not found' : 'Could not load this experiment'}
          </p>
          {!notFound && loadError?.message && (
            <p className="text-sm text-red-600 mb-4">{loadError.message}</p>
          )}
          <div className="flex items-center justify-center gap-4">
            {!notFound && (
              <button
                type="button"
                onClick={() => void load()}
                className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700"
                data-testid="experiment-retry"
              >
                Retry
              </button>
            )}
            <Link href="/experiments" className="text-blue-600 hover:underline text-sm">
              &larr; Back to Experiments
            </Link>
          </div>
        </div>
      </div>
    );
  }

  const status = experiment.status;
  const statusActions = ACTIONS_BY_STATUS[status] ?? [];
  const mayChange = canChangeExperiment(user);
  // Offer only what the API would accept: a role without EXPERIMENT UPDATE is
  // refused every lifecycle change, including on an experiment it owns.
  const actions = mayChange ? statusActions : [];
  // A null owner (the creator's account was removed) is nobody, so it is
  // never you.
  const isOwner =
    user !== null && experiment.owner_id !== null && user.id === experiment.owner_id;
  const resultsAvailable = status !== 'draft';
  const showRoleNote = !mayChange && statusActions.length > 0;
  const primaryMetric = experiment.metrics.find((m) => m.is_primary) ?? experiment.metrics[0];
  const algorithm = algorithmLabel(experiment.optimization_type);
  // Read at render, after the experiment has loaded in the browser (#1443).
  const nowMs = Date.now();
  const startLabel = experiment.start_date
    ? scheduleLabel('start', experiment.start_date, status, nowMs)
    : null;
  const endLabel = experiment.end_date
    ? scheduleLabel('end', experiment.end_date, status, nowMs)
    : null;

  return (
    <div className="flex-1 bg-slate-50" data-testid="experiment-detail">
      <PageTitle title={experiment.name} />
      <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="mb-4">
          <Link href="/experiments" className="text-slate-500 hover:text-slate-700 text-sm">
            &larr; Experiments
          </Link>
        </div>

        {/* Header */}
        <div className="bg-white rounded-lg border border-slate-200 p-6 mb-6">
          <div className="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-4">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-3">
                <h1 className="text-2xl font-bold text-slate-900 break-words" data-testid="experiment-name">
                  {experiment.name}
                </h1>
                <span
                  className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${experimentStatusColor(status)}`}
                  data-testid="experiment-status"
                  data-status={status}
                >
                  {experimentStatusLabel(status)}
                </span>
              </div>
              {/* Every dt is slate-500 (4.76:1 on white), never slate-400 (2.56:1): a11y/grey-text-contrast.test.ts. */}
              <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm text-slate-600">
                <div className="flex items-center gap-1.5">
                  <dt className="text-slate-500">Key</dt>
                  {/* The Copy button sits inside the dd: a dl may hold only dt and dd groups. */}
                  <dd className="flex items-center gap-1.5">
                    <span className="font-mono text-slate-800" data-testid="experiment-key">
                      {experiment.key ?? '—'}
                    </span>
                    {experiment.key && (
                      <button
                        type="button"
                        onClick={() => void copyKey()}
                        className="text-xs text-blue-600 hover:underline"
                        data-testid="copy-experiment-key"
                      >
                        {copied ? 'Copied' : 'Copy'}
                      </button>
                    )}
                  </dd>
                </div>
                <div className="flex items-center gap-1.5">
                  <dt className="text-slate-500">Type</dt>
                  <dd data-testid="experiment-type">{experimentTypeLabel(experiment.experiment_type)}</dd>
                </div>
                {experiment.correction_method && typeof experiment.confidence_level === 'number' && (
                  <div className="flex items-center gap-1.5">
                    <dt className="text-slate-500">Analysis</dt>
                    <dd data-testid="experiment-analysis">
                      {formatConfidence(experiment.confidence_level)} confidence ·{' '}
                      {experiment.correction_method === 'none'
                        ? 'no correction'
                        : correctionName(experiment.correction_method)}
                    </dd>
                  </div>
                )}
                {typeof experiment.bayesian_enabled === 'boolean' && (
                  <div className="flex items-center gap-1.5">
                    <dt className="text-slate-500">Bayesian analysis</dt>
                    <dd data-testid="experiment-bayesian">{experiment.bayesian_enabled ? 'On' : 'Off'}</dd>
                  </div>
                )}
                {typeof experiment.sequential_testing_enabled === 'boolean' && (
                  <div className="flex items-center gap-1.5">
                    <dt className="text-slate-500">Sequential testing (beta)</dt>
                    <dd data-testid="experiment-sequential">
                      {experiment.sequential_testing_enabled ? 'On' : 'Off'}
                    </dd>
                  </div>
                )}
                {algorithm && (
                  <div className="flex items-center gap-1.5">
                    <dt className="text-slate-500">Traffic algorithm</dt>
                    <dd data-testid="experiment-algorithm">{algorithm}</dd>
                  </div>
                )}
                <div className="flex items-center gap-1.5">
                  <dt className="text-slate-500">Owner</dt>
                  <dd data-testid="experiment-owner" title={experiment.owner_id ?? undefined}>
                    {isOwner
                      ? `You (${user?.email ?? user?.username})`
                      : experiment.owner_name ||
                        (experiment.owner_id === null ? 'No owner' : shortId(experiment.owner_id))}
                  </dd>
                </div>
                <div className="flex items-center gap-1.5">
                  <dt className="text-slate-500">Created</dt>
                  <dd>{formatDate(experiment.created_at)}</dd>
                </div>
                {startLabel && (
                  <div className="flex items-center gap-1.5">
                    <dt className="text-slate-500">{startLabel.term}</dt>
                    <dd data-testid="experiment-start-date">
                      {formatDate(experiment.start_date)}
                      {startLabel.due && DUE_NOTE.start}
                    </dd>
                  </div>
                )}
                {endLabel && (
                  <div className="flex items-center gap-1.5">
                    <dt className="text-slate-500">{endLabel.term}</dt>
                    <dd data-testid="experiment-end-date">
                      {formatDate(experiment.end_date)}
                      {endLabel.due && DUE_NOTE.end}
                    </dd>
                  </div>
                )}
                <EligibilityRows groupId={experiment.mutual_exclusion_group_id} user={user} />
              </dl>
              <p className="mt-2 text-xs text-slate-600" data-testid="experiment-api-settings-note">
                Set through the API, not on this page:{' '}
                <a
                  href={docsUrl('api/mutual-exclusion-groups', 'mutual-exclusion-groups')}
                  className={NOTE_LINK}
                  target="_blank"
                  rel="noreferrer"
                >
                  exclusion groups
                </a>
                ,{' '}
                <a
                  href={docsUrl('api/mutual-exclusion-groups', 'global-holdout')}
                  className={NOTE_LINK}
                  target="_blank"
                  rel="noreferrer"
                >
                  the global holdout
                </a>
                ,{' '}
                <a
                  href={docsUrl('guides/user-guide', 'pausing-and-stopping')}
                  className={NOTE_LINK}
                  target="_blank"
                  rel="noreferrer"
                >
                  a paused experiment&apos;s resume
                </a>
                ,{' '}
                <a
                  href={docsUrl('api/sequential-testing', 'turning-it-on')}
                  className={NOTE_LINK}
                  target="_blank"
                  rel="noreferrer"
                >
                  sequential testing
                </a>
                .
              </p>
            </div>

            {/* Actions */}
            <div className="flex flex-wrap items-center gap-2 shrink-0" data-testid="experiment-actions">
              {actions.map((action) => {
                const meta = ACTION_META[action];
                const busy = pendingAction === action;
                return (
                  <button
                    key={action}
                    type="button"
                    onClick={() => onActionClick(action)}
                    disabled={pendingAction !== null}
                    className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${meta.className}`}
                    data-testid={`action-${action}`}
                  >
                    {busy ? meta.pending : meta.label}
                  </button>
                );
              })}
              {resultsAvailable ? (
                <Link
                  href={`/results/${experiment.id}`}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-900 text-white hover:bg-slate-800 transition-colors"
                  data-testid="view-results"
                >
                  View results
                </Link>
              ) : (
                <span
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-100 text-slate-400 cursor-not-allowed"
                  title="Results are available once the experiment has started"
                  data-testid="view-results-disabled"
                >
                  View results
                </span>
              )}
            </div>
          </div>

          {showRoleNote && (
            <p className="mt-4 text-sm text-slate-700" data-testid="experiment-role-note">
              {isOwner
                ? 'You own this experiment, but starting, pausing, completing and archiving it requires the ADMIN or DEVELOPER role; '
                : 'Starting, pausing, completing and archiving an experiment requires the ADMIN or DEVELOPER role; '}
              you are {user?.role || 'signed in without a role'}.{' '}
              {resultsAvailable
                ? 'You can read its results.'
                : 'You can read its results once it has started.'}
            </p>
          )}

          {confirming && (
            <div
              role="dialog"
              aria-label={`Confirm ${ACTION_META[confirming].label}`}
              className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3"
              data-testid="confirm-action"
            >
              <p className="text-sm text-amber-900">{ACTION_META[confirming].confirm}</p>
              <div className="flex gap-2 shrink-0">
                <button
                  type="button"
                  onClick={() => setConfirming(null)}
                  className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
                  data-testid="confirm-cancel"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void runAction(confirming)}
                  className="px-3 py-1.5 rounded-md text-sm font-medium bg-amber-600 text-white hover:bg-amber-700"
                  data-testid="confirm-yes"
                >
                  Confirm
                </button>
              </div>
            </div>
          )}

          {actionError && (
            <div
              role="alert"
              className="mt-4 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
              data-testid="action-error"
            >
              {actionError}
            </div>
          )}

          <ExperimentManageSection
            experiment={experiment}
            user={user}
            onSaved={onSaved}
            onDesignOpenChange={setDesignOpen}
          />

          {(experiment.description || experiment.hypothesis) && (
            <div className="mt-5 pt-5 border-t border-slate-100 grid gap-4 sm:grid-cols-2">
              {experiment.description && (
                <div>
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    Description
                  </h2>
                  <p className="text-sm text-slate-700 whitespace-pre-line" data-testid="experiment-description">
                    {experiment.description}
                  </p>
                </div>
              )}
              {experiment.hypothesis && (
                <div>
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    Hypothesis
                  </h2>
                  <p className="text-sm text-slate-700 whitespace-pre-line" data-testid="experiment-hypothesis">
                    {experiment.hypothesis}
                  </p>
                </div>
              )}
            </div>
          )}
        </div>

        {!(designOpen && status === 'draft') && (
          <div className="grid gap-6 lg:grid-cols-5" data-testid="experiment-design-cards">
            {/* Variants */}
            <section className="lg:col-span-3 bg-white rounded-lg border border-slate-200 overflow-hidden">
              <div className="px-5 py-4 border-b border-slate-200 flex items-center justify-between">
                <h2 className="text-base font-semibold text-slate-800">Variants</h2>
                <span className="text-xs text-slate-500">{experiment.variants.length} total</span>
              </div>
              {experiment.variants.length === 0 ? (
                <p className="px-5 py-6 text-sm text-slate-500">No variants defined.</p>
              ) : (
                <table className="w-full text-sm" data-testid="variants-table">
                  <thead className="bg-slate-50 border-b border-slate-200">
                    <tr>
                      <th scope="col" className="text-left px-5 py-2.5 text-slate-600 font-medium">Name</th>
                      <th scope="col" className="text-right px-5 py-2.5 text-slate-600 font-medium">Allocation</th>
                      <th scope="col" className="text-left px-5 py-2.5 text-slate-600 font-medium">Role</th>
                      <th scope="col" className="text-left px-5 py-2.5 text-slate-600 font-medium">Configuration</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {experiment.variants.map((variant, index) => {
                      const configuration = formatConfiguration(variant.configuration);
                      return (
                        <tr key={variant.id} data-testid="variant-row">
                          <td className="px-5 py-3">
                            <div className="font-medium text-slate-800">{variant.name}</div>
                            {variant.description && (
                              <div className="text-xs text-slate-400 mt-0.5">{variant.description}</div>
                            )}
                          </td>
                          <td className="px-5 py-3 text-right text-slate-700 tabular-nums">
                            {variant.traffic_allocation}%
                          </td>
                          <td className="px-5 py-3">
                            {variant.is_control ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">
                                Control
                              </span>
                            ) : (
                              <span className="text-xs text-slate-400">Treatment</span>
                            )}
                          </td>
                          <td className="px-5 py-3 align-top">
                            {configuration === null ? (
                              <span className="text-slate-400" data-testid={`variant-configuration-none-${index}`}>
                                <span aria-hidden="true">—</span>
                                <span className="sr-only">None</span>
                              </span>
                            ) : (
                              // Text only: React escapes it, so a configuration holding markup shows as written.
                              <pre
                                tabIndex={0}
                                className="max-h-40 max-w-xs overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-2 font-mono text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-600"
                                data-testid={`variant-configuration-view-${index}`}
                              >
                                {configuration}
                              </pre>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </section>

            {/* Metrics */}
            <section className="lg:col-span-2 bg-white rounded-lg border border-slate-200 overflow-hidden">
              <div className="px-5 py-4 border-b border-slate-200 flex items-center justify-between">
                <h2 className="text-base font-semibold text-slate-800">Metrics</h2>
                <span className="text-xs text-slate-500">{experiment.metrics.length} total</span>
              </div>
              {experiment.metrics.length === 0 ? (
                <p className="px-5 py-6 text-sm text-slate-500" data-testid="metrics-empty">
                  No metrics defined. Results need at least one metric.
                </p>
              ) : (
                <ul className="divide-y divide-slate-100" data-testid="metrics-list">
                  {experiment.metrics.map((metric) => (
                    <li key={metric.id} className="px-5 py-3" data-testid="metric-row">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium text-slate-800">{metric.name}</span>
                        {metric.is_primary && (
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800">
                            Primary
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-slate-500 mt-0.5 flex flex-wrap gap-x-3">
                        <span>
                          event <code className="font-mono text-slate-700">{metric.event_name}</code>
                        </span>
                        <span>{METRIC_TYPE_LABELS[metric.metric_type as MetricType] ?? metric.metric_type}</span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}

        <TargetingSection
          experiment={experiment}
          mayChange={mayChange}
          role={user?.role}
          onPause={() => onActionClick('pause')}
          pauseBusy={pendingAction === 'pause'}
          onSaved={onSaved}
        />

        <BanditWeightsSection experiment={experiment} />

        {/* Warehouse analysis (beta): the `warehouse` module; renders nothing without it. */}
        <WarehouseAnalysisSection experiment={experiment} />

        {/* SDK hint */}
        {experiment.key && (
          <section className="mt-6 bg-white rounded-lg border border-slate-200 p-5" data-testid="sdk-hint">
            <h2 className="text-sm font-semibold text-slate-800 mb-1">Assign a user from your app</h2>
            <p className="text-xs text-slate-500 mb-3">
              {/* Admin → API Keys opens only for a superuser; everyone else gets the route (#920). */}
              {user?.is_superuser ? (
                <>
                  Use the experiment key with an API key from{' '}
                  <Link
                    href="/admin/api-keys"
                    className="text-blue-600 hover:underline"
                    data-testid="sdk-hint-admin-api-keys"
                  >
                    Admin → API Keys
                  </Link>
                  .
                </>
              ) : (
                <>
                  Use the experiment key with an API key.{' '}
                  <ApiKeyRouteHint testId="sdk-hint-api-key-route" />
                </>
              )}{' '}
              Track conversions by sending the metric event name
              {primaryMetric ? (
                <>
                  {' '}
                  (<code className="font-mono">{primaryMetric.event_name}</code>)
                </>
              ) : null}{' '}
              to <code className="font-mono">/api/v1/tracking/track</code>.
            </p>
            <pre className="bg-slate-900 text-slate-100 text-xs rounded-md p-3 overflow-x-auto">
              {`curl -X POST "$API_URL/api/v1/tracking/assign" \\
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \\
  -d '{"experiment_key": "${experiment.key}", "user_id": "user-123"}'`}
            </pre>
          </section>
        )}
      </div>
    </div>
  );
}
