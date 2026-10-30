import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useRouter } from 'next/router';
import Link from 'next/link';
import { TargetingRuleBuilder } from '@/components/targeting';
import { OUTSIDE_BUILDER_NOTE, SaveAlert } from '@/components/experiments/TargetingSection';
import { checkTargeting } from '@/components/experiments/new/formState';
import { TargetingRules } from '@/types/targeting';
import { FLAG_OPERATOR_OPTIONS, createEmptyRules } from '@/utils/targeting';
import {
  TARGETING_SAVE_INCOMPLETE,
  TargetingSaveError,
  describeTargetingSaveError,
} from '@/utils/experimentTargeting';
import {
  NOT_GROUPS_NOTE,
  isEditableFlagTargeting,
  rulesFromStored,
  targetingToSend,
  usesNotGroups,
} from '@/utils/flagTargeting';
import { docsUrl } from '@/services/docs';
import { isApiError } from '@/services/api';
import {
  FeatureFlag,
  FeatureFlagsService,
  RolloutSchedule,
  UpdateFeatureFlagRequest,
  isFlagOn,
} from '@/services/featureFlags';
import type { SafetyCheckResponse } from '@/types/safety';
import { PageTitle } from '@/components/PageTitle';
import { FlagToggle } from '@/components/feature-flags/FlagToggle';
import { RolloutScheduleManager } from '@/components/feature-flags/RolloutScheduleManager';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { FLAG_ROLE_NOTE, canChangeFeatureFlags } from '@/utils/experimentPermissions';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

// The rollout schedule's stage summary lives with the component that draws
// it; the page's tests read it from here.
export { stageSummary } from '@/components/feature-flags/RolloutScheduleManager';

const SCHEDULE_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-slate-100 text-slate-700',
  active: 'bg-green-100 text-green-800',
  paused: 'bg-yellow-100 text-yellow-800',
  completed: 'bg-blue-100 text-blue-800',
  cancelled: 'bg-gray-100 text-gray-600',
};

/** The rollout schedule section's load failure, in fixed words. */
export const SCHEDULES_LOAD_ERROR = 'Could not load the rollout schedule. Reload the page to try again.';

/** Under the slider while a schedule is active. */
export const SLIDER_SCHEDULE_NOTE =
  'A rollout schedule is active: the next stage that starts sets this percentage again.';

function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const d = parseApiTimestamp(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

function humanize(value: string): string {
  return value.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

/** Metrics computed by dividing by the flag's recorded evaluations. */
const RATE_METRICS = new Set(['error_rate']);

/**
 * Errors reported but no evaluations recorded in the check's window: the error
 * rate has nothing to divide by and reads 0, which is not "healthy". Returns
 * the counts to show, or null when the rate is measurable.
 */
export function unmeasuredErrorRate(
  check: SafetyCheckResponse | null,
): { errors: number; minutes: number } | null {
  const details = check?.details;
  if (!details) return null;
  const errors = details.error_count;
  const evaluations = details.total_evaluations;
  if (typeof errors !== 'number' || typeof evaluations !== 'number') return null;
  if (errors <= 0 || evaluations !== 0) return null;
  const minutes = typeof details.timeframe_minutes === 'number' ? details.timeframe_minutes : 15;
  return { errors, minutes };
}

/**
 * Error reports in the check's window that were stored but do not count toward
 * the error rate or automatic rollback: they were sent with a key without the
 * `sdk:ruleset` scope, or one whose owner can no longer change flags. Returns
 * the counts to show, or null when there are none.
 */
export function uncountedErrorReports(
  check: SafetyCheckResponse | null,
): { reports: number; minutes: number } | null {
  const details = check?.details;
  if (!details) return null;
  const reports = details.uncounted_error_count;
  if (typeof reports !== 'number' || reports <= 0) return null;
  const minutes = typeof details.timeframe_minutes === 'number' ? details.timeframe_minutes : 15;
  return { reports, minutes };
}

/**
 * The flag's stored `targeting_rules` exactly as the API returned them, under
 * either key the detail and list shapes use. Unlike `flagRules`, a value that
 * is not an object is kept, so it is shown rather than read as "no rules".
 */
function storedRules(flag: Pick<FeatureFlag, 'targeting_rules' | 'rules'>): unknown {
  return flag.targeting_rules ?? flag.rules ?? null;
}

/** Pick the schedule worth showing: active > paused > draft > most recent. */
export function pickSchedule(items: RolloutSchedule[]): RolloutSchedule | null {
  if (items.length === 0) return null;
  const rank: Record<string, number> = { active: 0, paused: 1, draft: 2, completed: 3, cancelled: 4 };
  return [...items].sort((a, b) => {
    const r = (rank[a.status] ?? 9) - (rank[b.status] ?? 9);
    if (r !== 0) return r;
    return parseApiTimestamp(b.updated_at).getTime() - parseApiTimestamp(a.updated_at).getTime();
  })[0];
}

export default function FeatureFlagDetailPage() {
  const router = useRouter();
  const rawId = router.query.id;
  const id = typeof rawId === 'string' ? rawId : undefined;
  const auth = useOptionalAuth();
  // ANALYST and VIEWER read a flag and change none of it (#917): no switch,
  // the rule builder read-only, the slider disabled, no Save, and a note why.
  const canChange = canChangeFeatureFlags(auth?.user);

  const [flag, setFlag] = useState<FeatureFlag | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<{ status: number; message: string } | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  // On/off
  const [toggling, setToggling] = useState(false);
  const [toggleError, setToggleError] = useState<string | null>(null);

  // Editable fields. `stored` is the flag's targeting_rules as last loaded or
  // saved: the baseline a save compares the builder against. `rules` is the
  // builder's state, or null while stored rules the builder cannot show are
  // displayed read-only; `replaced` is set once the user confirms replacing them.
  const [stored, setStored] = useState<unknown>(null);
  const [rules, setRules] = useState<TargetingRules | null>(null);
  const [replaced, setReplaced] = useState(false);
  const [confirmingReplace, setConfirmingReplace] = useState(false);
  const [targetingError, setTargetingError] = useState<TargetingSaveError | null>(null);
  const [rolloutPercentage, setRolloutPercentage] = useState(0);
  const saveErrorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (saveError) saveErrorRef.current?.focus();
  }, [saveError]);

  /** Show `value` as the stored rules: in the builder when it can, read-only otherwise. */
  const loadRules = useCallback((value: unknown) => {
    setStored(value);
    setRules(isEditableFlagTargeting(value) ? rulesFromStored(value) : null);
    setReplaced(false);
    setConfirmingReplace(false);
  }, []);

  // Rollout schedule
  const [schedules, setSchedules] = useState<RolloutSchedule[] | null>(null);
  const [scheduleError, setScheduleError] = useState<string | null>(null);

  // Safety
  const [safety, setSafety] = useState<SafetyCheckResponse | null>(null);
  const [safetyError, setSafetyError] = useState<string | null>(null);
  const [safetyLoading, setSafetyLoading] = useState(false);
  const unmeasured = unmeasuredErrorRate(safety);
  const uncounted = uncountedErrorReports(safety);

  const loadSafety = useCallback(async (flagId: string) => {
    setSafetyLoading(true);
    setSafetyError(null);
    try {
      setSafety(await FeatureFlagsService.safetyCheck(flagId));
    } catch (err) {
      setSafetyError(err instanceof Error ? err.message : 'Safety check unavailable');
    } finally {
      setSafetyLoading(false);
    }
  }, []);

  const loadSchedules = useCallback(async (flagId: string) => {
    setScheduleError(null);
    try {
      const res = await FeatureFlagsService.listRolloutSchedules(flagId);
      setSchedules(res.items ?? []);
    } catch {
      setSchedules([]);
      setScheduleError(SCHEDULES_LOAD_ERROR);
    }
  }, []);

  /**
   * After a schedule change: the schedules again and, when a stage was
   * advanced, the flag too. An advance sets the flag's rollout percentage on
   * the server, so the slider takes the new value; a Save with the old one
   * would undo the stage.
   */
  const afterRolloutChange = useCallback(
    async (flagId: string, { flagMoved }: { flagMoved: boolean }) => {
      await loadSchedules(flagId);
      if (!flagMoved) return;
      try {
        const data = await FeatureFlagsService.get(flagId);
        setFlag((current) => (current ? { ...current, ...data } : data));
        setRolloutPercentage(data.rollout_percentage ?? 0);
      } catch {
        // The schedule list above already shows the stage; the slider keeps
        // its value until the page is reloaded.
      }
    },
    [loadSchedules],
  );

  useEffect(() => {
    if (!id) return;
    let cancelled = false;

    const fetchFlag = async () => {
      setIsLoading(true);
      setLoadError(null);
      try {
        const data = await FeatureFlagsService.get(id);
        if (cancelled) return;
        setFlag(data);
        loadRules(storedRules(data));
        setRolloutPercentage(data.rollout_percentage ?? 0);
        // Secondary panels load in parallel; their failures are non-fatal.
        void loadSchedules(id);
        void loadSafety(id);
      } catch (err) {
        if (cancelled) return;
        setLoadError({
          status: isApiError(err) ? err.status : 0,
          message: err instanceof Error ? err.message : 'Failed to load feature flag',
        });
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    void fetchFlag();
    return () => {
      cancelled = true;
    };
  }, [id, loadSchedules, loadSafety, loadRules]);

  const handleToggle = async (next: boolean) => {
    if (!flag) return;
    const previous = flag;
    setToggleError(null);
    setToggling(true);
    setFlag({ ...flag, status: next ? 'active' : 'inactive', is_active: next });
    try {
      const res = await FeatureFlagsService.setEnabled(flag.id, next);
      setFlag((current) =>
        current
          ? { ...current, status: res.status, is_active: res.status === 'active', updated_at: res.updated_at }
          : current,
      );
    } catch (err) {
      setFlag(previous);
      setToggleError(err instanceof Error ? err.message : 'Failed to update the flag');
    } finally {
      setToggling(false);
    }
  };

  const handleSave = async () => {
    if (!flag) return;
    setSaveError(null);
    setTargetingError(null);
    setSaveSuccess(false);

    // Rules are sent only when they were changed (or replaced), so a save
    // that moves only the rollout leaves the stored rules exactly as they are.
    const targeting = targetingToSend(rules, stored, replaced);
    if (targeting !== undefined && rules) {
      const problems = checkTargeting(rules);
      if (problems.length > 0) {
        setTargetingError({ message: TARGETING_SAVE_INCOMPLETE, problems });
        return;
      }
    }

    const body: UpdateFeatureFlagRequest = { rollout_percentage: rolloutPercentage };
    if (targeting !== undefined) body.targeting_rules = targeting;

    setIsSaving(true);
    try {
      const updated = await FeatureFlagsService.update(flag.id, body);
      setFlag((current) => ({ ...(current ?? updated), ...updated }));
      const answered = 'targeting_rules' in updated || 'rules' in updated;
      loadRules(answered ? storedRules(updated) : (targeting ?? stored));
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
    } catch (err) {
      const described = describeTargetingSaveError(err);
      if (described.problems) {
        setTargetingError(described);
      } else {
        setSaveError(err instanceof Error ? err.message : 'Failed to save changes');
      }
    } finally {
      setIsSaving(false);
    }
  };

  if (!id || isLoading) {
    return (
      <div className="flex-1 bg-slate-50 flex items-center justify-center">
        <PageTitle title="Feature Flag" />
        <div className="text-center" data-testid="flag-loading">
          <div className="inline-block w-6 h-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
          <p className="text-slate-500 mt-2 text-sm">Loading feature flag...</p>
        </div>
      </div>
    );
  }

  if (loadError || !flag) {
    const notFound = loadError?.status === 404;
    return (
      <div className="flex-1 bg-slate-50 flex items-center justify-center">
        <PageTitle title={notFound ? 'Flag not found' : 'Feature Flag'} />
        <div className="text-center max-w-md px-4" data-testid={notFound ? 'flag-not-found' : 'flag-error'}>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
            {notFound ? '404' : 'Error'}
          </p>
          <p className="text-slate-800 text-lg mb-2">
            {notFound ? 'Feature flag not found' : 'Could not load this feature flag'}
          </p>
          {!notFound && loadError?.message && (
            <p className="text-sm text-red-600 mb-4">{loadError.message}</p>
          )}
          <Link href="/feature-flags" className="text-blue-600 hover:underline text-sm">
            &larr; Back to Feature Flags
          </Link>
        </div>
      </div>
    );
  }

  const on = isFlagOn(flag);
  const schedule = schedules ? pickSchedule(schedules) : null;

  return (
    <div className="flex-1 bg-slate-50" data-testid="flag-detail">
      <PageTitle title={flag.name} />
      <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Header */}
        <div className="flex items-center gap-4 mb-4">
          <Link href="/feature-flags" className="text-slate-500 hover:text-slate-700 text-sm">
            &larr; Feature Flags
          </Link>
        </div>

        <div className="bg-white rounded-lg border border-slate-200 p-6 mb-6">
          <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-3">
                <h1 className="text-2xl font-bold text-slate-900 break-words" data-testid="flag-name">
                  {flag.name}
                </h1>
                <span
                  className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                    on ? 'bg-green-100 text-green-800' : 'bg-slate-100 text-slate-700'
                  }`}
                  data-testid="flag-status"
                  data-status={on ? 'active' : 'inactive'}
                >
                  {on ? 'On' : 'Off'}
                </span>
              </div>
              <p className="text-sm text-slate-500 mt-1 font-mono" data-testid="flag-key">
                {flag.key}
              </p>
              {flag.description && (
                <p className="text-sm text-slate-600 mt-2" data-testid="flag-description">
                  {flag.description}
                </p>
              )}
            </div>

            <div className="flex items-center gap-3 shrink-0">
              <span className="text-sm text-slate-600">{on ? 'Serving' : 'Not serving'}</span>
              {canChange && (
                <FlagToggle
                  on={on}
                  disabled={toggling}
                  label={`Turn ${flag.key} ${on ? 'off' : 'on'}`}
                  onChange={(next) => void handleToggle(next)}
                  data-testid="flag-toggle"
                />
              )}
            </div>
          </div>
          {!canChange && (
            <p className="mt-4 text-sm text-slate-600" data-testid="flag-role-note">
              {FLAG_ROLE_NOTE}
            </p>
          )}
          {toggleError && (
            <div
              role="alert"
              className="mt-4 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
              data-testid="flag-toggle-error"
            >
              {toggleError}
            </div>
          )}
        </div>

        <div className="grid gap-6 lg:grid-cols-2 mb-6">
          {/* Rollout schedule */}
          <section className="bg-white rounded-lg border border-slate-200 p-6" data-testid="rollout-schedule-section">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-base font-semibold text-slate-800">Rollout schedule</h2>
              {schedule && (
                <span
                  className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                    SCHEDULE_STATUS_COLORS[schedule.status] ?? 'bg-slate-100 text-slate-700'
                  }`}
                  data-testid="rollout-schedule-status"
                >
                  {humanize(schedule.status)}
                </span>
              )}
            </div>

            {schedules === null && !scheduleError && (
              <p className="text-sm text-slate-500" data-testid="rollout-schedule-loading">
                Loading…
              </p>
            )}
            {scheduleError && (
              <p role="alert" className="text-sm text-red-700" data-testid="rollout-schedule-error">
                {scheduleError}
              </p>
            )}
            {schedules !== null && !scheduleError && !schedule && (
              <p className="text-sm text-slate-500" data-testid="rollout-schedule-empty">
                {canChange ? (
                  <>
                    No rollout schedule. The rollout percentage below applies at once; a schedule
                    raises it in stages. Create one here, or through{' '}
                    <code className="font-mono">/api/v1/rollout-schedules</code>.
                  </>
                ) : (
                  'No rollout schedule.'
                )}
              </p>
            )}
            {schedules !== null && !scheduleError && (
              <RolloutScheduleManager
                flagId={flag.id}
                flagPercentage={flag.rollout_percentage ?? 0}
                schedule={schedule}
                canChange={canChange}
                onChanged={(change) => afterRolloutChange(flag.id, change)}
              />
            )}
          </section>

          {/* Safety */}
          <section className="bg-white rounded-lg border border-slate-200 p-6" data-testid="safety-section">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-base font-semibold text-slate-800">Safety check</h2>
              <div className="flex items-center gap-2">
                {safety && unmeasured && (
                  <span
                    className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border border-slate-300 bg-white text-slate-800"
                    data-testid="safety-status"
                    data-healthy="unknown"
                  >
                    Error rate unknown
                  </span>
                )}
                {safety && !unmeasured && (
                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                      safety.is_healthy ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                    }`}
                    data-testid="safety-status"
                    data-healthy={safety.is_healthy ? 'true' : 'false'}
                  >
                    {safety.is_healthy ? 'Healthy' : 'Unhealthy'}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => void loadSafety(flag.id)}
                  disabled={safetyLoading}
                  className="text-xs text-blue-600 hover:underline disabled:opacity-50"
                  data-testid="safety-recheck"
                >
                  {safetyLoading ? 'Checking…' : 'Re-check'}
                </button>
              </div>
            </div>

            {!safety && safetyLoading && (
              <p className="text-sm text-slate-400" data-testid="safety-loading">Checking…</p>
            )}
            {safetyError && (
              <p className="text-sm text-red-600" data-testid="safety-error">{safetyError}</p>
            )}
            {safety && (
              <div>
                {unmeasured && (
                  <p className="text-sm text-slate-700 mb-3" role="status" data-testid="safety-no-evaluations">
                    {unmeasured.errors} {unmeasured.errors === 1 ? 'error' : 'errors'} reported, but no
                    evaluations reported in the last {unmeasured.minutes} minutes: the error rate cannot be
                    computed.
                  </p>
                )}
                {uncounted && (
                  <p className="text-sm text-slate-700 mb-3" role="status" data-testid="safety-uncounted-reports">
                    {uncounted.reports === 1
                      ? `1 error report in the last ${uncounted.minutes} minutes was sent with a key that does not count toward automatic rollback (no sdk:ruleset scope, or the key's owner can no longer change flags). It is stored, but does not count toward the error rate.`
                      : `${uncounted.reports} error reports in the last ${uncounted.minutes} minutes were sent with keys that do not count toward automatic rollback (no sdk:ruleset scope, or the key's owner can no longer change flags). They are stored, but do not count toward the error rate.`}
                  </p>
                )}
                {safety.metrics.length === 0 ? (
                  <p className="text-sm text-slate-500">No safety metrics recorded yet.</p>
                ) : (
                  <ul className="space-y-2" data-testid="safety-metrics">
                    {safety.metrics.map((m) => (
                      <li
                        key={m.name}
                        className="flex items-center justify-between gap-3 text-sm"
                        data-testid="safety-metric"
                      >
                        <span className="text-slate-800">{humanize(m.name)}</span>
                        <span className="flex items-center gap-2 shrink-0">
                          <span className="tabular-nums text-slate-700">
                            {m.current_value}
                            {m.unit ? ` ${m.unit}` : ''}{' '}
                            <span className="text-slate-400">/ {m.threshold}{m.unit ? ` ${m.unit}` : ''}</span>
                          </span>
                          {unmeasured && RATE_METRICS.has(m.name) ? (
                            <span className="text-xs text-slate-700" data-testid="safety-metric-not-computed">
                              not computed
                            </span>
                          ) : (
                            <span
                              className={`inline-block h-2 w-2 rounded-full ${m.is_healthy ? 'bg-green-500' : 'bg-red-500'}`}
                              aria-label={m.is_healthy ? 'healthy' : 'unhealthy'}
                            />
                          )}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
                <p className="text-xs text-slate-400 mt-3" data-testid="safety-last-checked">
                  Last checked {formatDateTime(safety.last_checked)}
                </p>
              </div>
            )}
          </section>
        </div>

        <div className="space-y-6">
          {/* Targeting Rules */}
          <section className="bg-white rounded-lg border border-slate-200 p-6" data-testid="flag-targeting-section">
            {rules !== null ? (
              <>
                <TargetingRuleBuilder
                  value={rules}
                  onChange={setRules}
                  operatorOptions={FLAG_OPERATOR_OPTIONS}
                  readOnly={!canChange}
                  data-testid="targeting-rule-builder"
                />
                {replaced && (
                  <div
                    className="mt-3 flex flex-wrap items-center gap-3 text-sm text-amber-900"
                    data-testid="targeting-replacing"
                  >
                    <p>The stored rules are replaced when you save. Until then, nothing changes.</p>
                    <button
                      type="button"
                      onClick={() => loadRules(stored)}
                      className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
                      data-testid="targeting-keep-stored"
                    >
                      Keep the stored rules
                    </button>
                  </div>
                )}
              </>
            ) : (
              <div data-testid="targeting-raw">
                <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
                  <h3 className="text-sm font-semibold text-slate-700">Targeting Rules</h3>
                  {!confirmingReplace && canChange && (
                    <button
                      type="button"
                      onClick={() => setConfirmingReplace(true)}
                      className="px-3 py-1.5 rounded-md text-sm font-medium bg-white text-slate-700 border border-slate-300 hover:bg-slate-50"
                      data-testid="targeting-replace"
                    >
                      Replace rules
                    </button>
                  )}
                </div>
                {/* Rules the builder cannot show only because of a NOT group
                    are applied as stored, so they get their own note (#918);
                    every other shape keeps the general one. */}
                <p className="text-sm text-amber-900 mb-2" data-testid="targeting-raw-note">
                  {usesNotGroups(stored) ? NOT_GROUPS_NOTE : OUTSIDE_BUILDER_NOTE}{' '}
                  <a
                    href={docsUrl('api/endpoints', 'targeting-rules')}
                    className="text-blue-600 hover:underline"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Read about targeting rules
                  </a>
                  .
                </p>
                <pre
                  role="region"
                  aria-label="Stored targeting rules"
                  tabIndex={0}
                  className="bg-slate-50 border border-slate-200 text-xs text-slate-800 rounded-md p-3 overflow-x-auto focus:outline-none focus:ring-2 focus:ring-blue-600"
                  data-testid="targeting-raw-json"
                >
                  {JSON.stringify(stored, null, 2)}
                </pre>
                {confirmingReplace && (
                  <div
                    role="dialog"
                    aria-label="Replace rules"
                    className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3"
                    data-testid="targeting-replace-confirm"
                  >
                    <p className="text-sm text-amber-900">
                      These rules will be discarded when you save new ones. Until you save, nothing changes.
                    </p>
                    <div className="mt-3 flex gap-2">
                      <button
                        type="button"
                        onClick={() => setConfirmingReplace(false)}
                        className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
                        data-testid="targeting-replace-cancel"
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setConfirmingReplace(false);
                          setReplaced(true);
                          setRules(createEmptyRules());
                        }}
                        className="px-3 py-1.5 rounded-md text-sm font-medium bg-amber-600 text-white hover:bg-amber-700"
                        data-testid="targeting-replace-yes"
                      >
                        Start with no rules
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
            {targetingError && <SaveAlert error={targetingError} />}
          </section>

          {/* Rollout Percentage */}
          <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4">
            <h2 className="text-base font-semibold text-slate-800">Rollout</h2>
            <div>
              <div className="flex items-center justify-between mb-2">
                <label htmlFor="rollout-percentage" className="text-sm font-medium text-slate-700">
                  Rollout percentage
                </label>
                <span className="text-sm font-semibold text-slate-900" data-testid="rollout-value">
                  {rolloutPercentage}%
                </span>
              </div>
              <input
                id="rollout-percentage"
                name="rollout_percentage"
                type="range"
                min={0}
                max={100}
                value={rolloutPercentage}
                onChange={(e) => setRolloutPercentage(Number(e.target.value))}
                disabled={!canChange}
                className="w-full accent-blue-600"
                data-testid="rollout-percentage"
              />
              <div className="flex justify-between text-xs text-slate-400 mt-1">
                <span>0%</span>
                <span>100%</span>
              </div>
              {schedule?.status === 'active' && (
                <p className="mt-2 text-sm text-slate-600" data-testid="rollout-schedule-slider-note">
                  {SLIDER_SCHEDULE_NOTE}
                </p>
              )}
            </div>
          </section>

          {saveError && (
            <div
              ref={saveErrorRef}
              role="alert"
              tabIndex={-1}
              className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700 focus:outline-none focus:ring-2 focus:ring-blue-600"
              data-testid="save-error"
            >
              {saveError}
            </div>
          )}
          {saveSuccess && (
            <div
              role="status"
              className="rounded-lg bg-green-50 border border-green-200 p-3 text-sm text-green-700"
              data-testid="save-success"
            >
              Changes saved successfully.
            </div>
          )}

          {canChange && (
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => void handleSave()}
                disabled={isSaving}
                className="px-6 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                data-testid="save-flag"
              >
                {isSaving ? 'Saving...' : 'Save Changes'}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
