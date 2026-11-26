import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { PageTitle } from '@/components/PageTitle';
import { FlagToggle } from '@/components/feature-flags/FlagToggle';
import {
  ARCHIVED_LABEL,
  ArchivedFlagRowNote,
  isFlagArchived,
} from '@/components/feature-flags/ArchivedFlag';
import { hasDeletedFlagNotice, takeDeletedFlagNotice } from '@/components/feature-flags/FlagManageSection';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { useEffectivePermissions } from '@/contexts/EffectivePermissionsContext';
import { useDemoMode } from '@/contexts/AuthConfigContext';
import { DemoNote } from '@/components/DemoNote';
import { DEMO_FLAGS_ACTION } from '@/utils/demoMode';
import { FLAG_ROLE_NOTE, canChangeFeatureFlags, canCreateFeatureFlags } from '@/utils/experimentPermissions';
import {
  FeatureFlagListItem,
  FeatureFlagStatus,
  FeatureFlagsService,
  FlagEnvironmentSettings,
  isFlagOn,
} from '@/services/featureFlags';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
import { flagValueType, typeLabel } from '@/utils/flagValues';
import { useFlagEnvironment } from '@/contexts/FlagEnvironmentContext';
import { ProductionBanner } from '@/components/feature-flags/FlagEnvironmentSections';
import {
  FLAG_ENVIRONMENT_LABELS,
  NEW_FLAGS_IN_PRODUCTION,
  NonProductionEnvironment,
  inEnvironmentUrl,
  noFlagsConfiguredText,
  notConfiguredText,
} from '@/utils/flagEnvironments';

const STATUS_FILTERS: Array<{ label: string; value: FeatureFlagStatus | 'all' }> = [
  { label: 'All', value: 'all' },
  { label: 'On', value: 'active' },
  { label: 'Off', value: 'inactive' },
  // An archived flag is neither: "Off" lists only inactive ones (#1748).
  { label: 'Archived', value: 'archived' },
];

const STALE_FILTERS: Array<{ label: string; value: 'any' | 'stale' }> = [
  { label: 'Any', value: 'any' },
  { label: 'Stale', value: 'stale' },
];

/** One line per reason; N is the server's `stale_days`, never a constant here. */
function staleReasonText(reason: string, days: number | undefined): string {
  const n = days === undefined ? 'N' : String(days);
  switch (reason) {
    case 'not_evaluated':
      return `Not evaluated in ${n} days`;
    case 'on_for_everyone':
      return `On for everyone, unchanged for ${n} days`;
    case 'off_for_everyone':
      return `Off for everyone, unchanged for ${n} days`;
    default:
      return reason;
  }
}

function formatDate(value: string): string {
  // Empty only for a flag never set in Staging or Development.
  if (!value) return '—';
  const d = parseApiTimestamp(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleDateString();
}

/**
 * A listed flag as it is in Staging or Development: that environment's on/off,
 * rollout and last change (empty when never set). An archived flag stays
 * archived. A stale mark is about the flag in every environment and is shown in
 * Production's list only, so the view is never stale.
 */
function inEnvironment(flag: FeatureFlagListItem, settings: FlagEnvironmentSettings): FeatureFlagListItem {
  return {
    ...flag,
    status: isFlagArchived(flag) ? flag.status : settings.status,
    is_active: isFlagArchived(flag) ? false : settings.is_active,
    rollout_percentage: settings.rollout_percentage,
    targeting_rules: settings.targeting_rules,
    updated_at: settings.updated_at ?? '',
    stale: false,
    stale_reasons: [],
  };
}

/** A listed flag with no setting in the environment: off there, and never changed. */
function unconfigured(environment: string): FlagEnvironmentSettings {
  return {
    environment,
    status: 'inactive',
    is_active: false,
    rollout_percentage: 0,
    targeting_rules: null,
    configured: false,
    updated_at: null,
  };
}

export default function FeatureFlagsPage() {
  const auth = useOptionalAuth();
  const { effective, loading: permissionsLoading } = useEffectivePermissions();
  // Each control asks the API's own question (#917, #891): "+ New Flag" and
  // the create link FEATURE_FLAG CREATE, the On/Off column FEATURE_FLAG
  // UPDATE, from the role or the effective map. A user without CREATE gets a
  // note saying why in place of the button, once the map has answered.
  const canCreate = canCreateFeatureFlags(auth?.user, effective);
  const canChange = canChangeFeatureFlags(auth?.user, effective);
  const createPending = !canCreate && permissionsLoading;
  // A read-only demo puts the demo note where "+ New Flag" or the role note would be.
  const demo = useDemoMode();
  // Production lists the flags as before. Staging and Development list the
  // same flags with their settings there (the batch route), and a switch
  // changes the setting through the environment's route.
  const { environment, reportShown, switchable } = useFlagEnvironment();
  const otherEnvironment: NonProductionEnvironment | null =
    environment && environment !== 'production' ? environment : null;
  const production = environment === 'production';
  // Settings by flag id in `otherEnvironment`, and the environment the rows were loaded for.
  const [envSettings, setEnvSettings] = useState<Record<string, FlagEnvironmentSettings>>({});
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<FeatureFlagStatus | 'all'>('all');
  const [staleFilter, setStaleFilter] = useState<'any' | 'stale'>('any');
  const [staleDays, setStaleDays] = useState<number | undefined>(undefined);
  const [evaluationsSeen, setEvaluationsSeen] = useState(true);
  const [flags, setFlags] = useState<FeatureFlagListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [totalFlags, setTotalFlags] = useState(0);
  const [toggling, setToggling] = useState<Record<string, boolean>>({});
  // Rows with a press in flight, set at once: `toggling` only changes on the next
  // render, so a second press in the same tick would otherwise send a second request.
  const toggleLock = useRef<Set<string>>(new Set());
  // Keyed by flag id: two toggles can fail at once and both messages matter.
  const [toggleErrors, setToggleErrors] = useState<Record<string, string>>({});
  // "Deleted {name}." when the flag's page has just deleted one (#442). Its
  // status region is on the page from the first render, only then, and the
  // text is set after it, so a screen reader announces it.
  const [announcesDelete] = useState(hasDeletedFlagNotice);
  const [deletedStatus, setDeletedStatus] = useState('');

  useEffect(() => {
    const notice = takeDeletedFlagNotice();
    if (notice) setDeletedStatus(notice);
  }, []);

  /**
   * Sequence number of the most recent list request. Switching filters quickly
   * leaves the earlier request in flight; without this guard a slow "On"
   * response can land after a fast "Off" one and paint the wrong rows (which a
   * subsequent toggle would then act on).
   */
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    if (!environment) return;
    requestSeq.current += 1;
    const seq = requestSeq.current;
    const isCurrent = () => seq === requestSeq.current;
    const inEnvironment = environment === 'production' ? null : environment;

    setLoading(true);
    setError(null);
    try {
      // `stale` is sent only while "Stale" is pressed: "Any" sends no parameter.
      // The status is Production's, and stale is about every environment: both
      // are asked in Production only.
      const data = await FeatureFlagsService.list(
        inEnvironment
          ? {}
          : {
              ...(statusFilter !== 'all' ? { status: statusFilter } : {}),
              ...(staleFilter === 'stale' ? { stale: true } : {}),
            },
      );
      const items = data.items ?? [];
      const settings = inEnvironment
        ? await FeatureFlagsService.listEnvironmentSettings(
            inEnvironment,
            items.map((f) => f.id),
          )
        : {};
      if (!isCurrent()) return;
      setStaleDays(data.stale_days);
      setEvaluationsSeen(data.evaluations_seen !== false);
      setFlags(items);
      setEnvSettings(settings);
      setTotalFlags(data.total ?? items.length);
      setLoadedFor(environment);
    } catch (err) {
      if (!isCurrent()) return;
      setLoadedFor(environment);
      setError(err instanceof Error ? err.message : 'Failed to load feature flags');
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [statusFilter, staleFilter, environment]);

  useEffect(() => {
    void load();
  }, [load]);

  // A failed switch in one environment says nothing about another, and an
  // answer that arrives after the environment was changed is not shown.
  const shownEnvironment = useRef(environment);
  useEffect(() => {
    shownEnvironment.current = environment;
    setToggleErrors({});
  }, [environment]);

  /** On/off in Staging or Development: the environment's PUT, never the flag's enable or disable. */
  const handleEnvironmentToggle = async (flag: FeatureFlagListItem, env: NonProductionEnvironment) => {
    const previous = envSettings[flag.id] ?? unconfigured(env);
    const next = !isFlagOn(previous);
    const optimistic: FlagEnvironmentSettings = {
      ...previous,
      is_active: next,
      status: next ? 'active' : 'inactive',
      configured: true,
    };
    const stillShown = () => shownEnvironment.current === env;
    setToggleErrors((e) => {
      const copy = { ...e };
      delete copy[flag.id];
      return copy;
    });
    setToggling((t) => ({ ...t, [flag.id]: true }));
    setEnvSettings((current) => ({ ...current, [flag.id]: optimistic }));
    try {
      // The PUT answers the setting as it now is.
      const saved = await FeatureFlagsService.updateInEnvironment(flag.id, env, { is_active: next });
      if (stillShown()) setEnvSettings((current) => ({ ...current, [flag.id]: saved }));
    } catch (err) {
      if (!stillShown()) return;
      setEnvSettings((current) => ({ ...current, [flag.id]: previous }));
      setToggleErrors((e) => ({
        ...e,
        [flag.id]:
          err instanceof Error
            ? err.message
            : `Failed to turn ${next ? 'on' : 'off'} ${flag.key} in ${FLAG_ENVIRONMENT_LABELS[env]}`,
      }));
    } finally {
      setToggling((t) => {
        const copy = { ...t };
        delete copy[flag.id];
        return copy;
      });
    }
  };

  /**
   * Optimistic on/off: flip locally, call enable/disable, and on failure revert
   * only this row — a whole-list snapshot would also undo any other toggle that
   * succeeded while this request was in flight.
   */
  const handleToggle = async (flag: FeatureFlagListItem) => {
    if (otherEnvironment) {
      await handleEnvironmentToggle(flag, otherEnvironment);
      return;
    }
    const next = !isFlagOn(flag);
    const previousStatus = flag.status;
    const previousIsActive = flag.is_active;
    setToggleErrors((e) => {
      const copy = { ...e };
      delete copy[flag.id];
      return copy;
    });
    setToggling((t) => ({ ...t, [flag.id]: true }));
    setFlags((current) =>
      current.map((f) =>
        f.id === flag.id ? { ...f, status: next ? 'active' : 'inactive', is_active: next } : f,
      ),
    );
    try {
      const res = await FeatureFlagsService.setEnabled(flag.id, next);
      setFlags((current) =>
        current.map((f) =>
          f.id === flag.id
            ? { ...f, status: res.status, is_active: res.status === 'active', updated_at: res.updated_at }
            : f,
        ),
      );
    } catch (err) {
      setFlags((current) =>
        current.map((f) =>
          f.id === flag.id ? { ...f, status: previousStatus, is_active: previousIsActive } : f,
        ),
      );
      if (shownEnvironment.current !== 'production') return;
      setToggleErrors((e) => ({
        ...e,
        [flag.id]:
          err instanceof Error ? err.message : `Failed to turn ${next ? 'on' : 'off'} ${flag.key}`,
      }));
    } finally {
      setToggling((t) => {
        const copy = { ...t };
        delete copy[flag.id];
        return copy;
      });
    }
  };

  // Until the rows belong to the environment on screen, the list shows its
  // loading state: one environment's state is never shown under another's name.
  const busy = loading || loadedFor !== environment;
  // What is on screen, for the environment switcher's status.
  const failed = error !== null;
  useEffect(() => {
    reportShown(environment && !busy ? { environment, loaded: !failed } : null);
    return () => reportShown(null);
  }, [environment, busy, failed, reportShown]);
  const isEmpty = !busy && !error && flags.length === 0;
  // Outside Production: no flag has a setting there yet (the list holds every flag).
  const noneConfigured =
    otherEnvironment !== null &&
    flags.length > 0 &&
    totalFlags <= flags.length &&
    !flags.some((f) => envSettings[f.id]?.configured === true);
  const environmentLabel = otherEnvironment ? FLAG_ENVIRONMENT_LABELS[otherEnvironment] : null;

  return (
    <div className="flex-1 bg-slate-50">
      {/* Not while the rows load: on Back the list renders once with the environment just left. */}
      <PageTitle title={environmentLabel && !busy ? `Feature Flags in ${environmentLabel}` : 'Feature Flags'} />
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold text-slate-900">Feature Flags</h1>
            <p className="text-sm text-slate-500 mt-1">
              Turn features on and off, roll them out gradually and watch their safety signals
            </p>
          </div>
          {demo ? (
            <DemoNote action={DEMO_FLAGS_ACTION} className="text-sm text-slate-600 max-w-md" />
          ) : canCreate ? (
            // A new flag's settings are Production's, so it is created from there.
            production ? (
              <Link
                href="/feature-flags/new"
                className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
                data-testid="new-flag-btn"
              >
                + New Flag
              </Link>
            ) : (
              <p className="text-sm text-slate-600" data-testid="flags-created-in-production">
                {NEW_FLAGS_IN_PRODUCTION}
              </p>
            )
          ) : createPending ? null : (
            <p className="text-sm text-slate-600" data-testid="flags-role-note">
              {FLAG_ROLE_NOTE}
            </p>
          )}
        </div>

        {production && canChange && !demo && <ProductionBanner className="mb-6" />}

        {announcesDelete && (
          <p
            role="status"
            className={
              deletedStatus
                ? 'mb-4 rounded-lg bg-green-50 border border-green-200 p-3 text-sm text-green-700'
                : undefined
            }
            data-testid="flags-deleted-status"
          >
            {deletedStatus}
          </p>
        )}

        {/* Filters */}
        {totalFlags > flags.length && flags.length > 0 && (
          <p data-testid="flag-limit-notice" className="text-xs text-slate-500 mb-3">
            Showing {flags.length} of {totalFlags} flags.
          </p>
        )}

        {/* The status is Production's, and stale is about every environment: both are offered in Production only. */}
        {production && (
        <div className="flex gap-x-6 gap-y-2 mb-6 flex-wrap">
        <div
          className="flex gap-2 flex-wrap"
          data-testid="flag-status-filter"
          role="group"
          aria-label="Status filter"
        >
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              data-testid={`flag-filter-${f.value}`}
              aria-pressed={statusFilter === f.value}
              onClick={() => setStatusFilter(f.value)}
              className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
                statusFilter === f.value
                  ? 'bg-blue-600 text-white'
                  : 'bg-white text-slate-600 border border-slate-300 hover:bg-slate-50'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div
          className="flex gap-2 flex-wrap"
          data-testid="flag-stale-filter"
          role="group"
          aria-label="Stale filter"
        >
          {STALE_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              data-testid={`flag-stale-filter-${f.value}`}
              aria-pressed={staleFilter === f.value}
              onClick={() => setStaleFilter(f.value)}
              className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
                staleFilter === f.value
                  ? 'bg-blue-600 text-white'
                  : 'bg-white text-slate-600 border border-slate-300 hover:bg-slate-50'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        </div>
        )}

        {production && staleFilter === 'stale' && (
          <p data-testid="flag-stale-hint" className="text-xs text-slate-500 mb-3">
            Stale is a suggestion: nothing is changed or archived for you. A flag tagged permanent
            is never stale; tags are set through the API.
          </p>
        )}

        {/* On the page before its text, which is announced once the rows have loaded. */}
        {otherEnvironment && (
          <div role="status">
            {!busy && !error && noneConfigured && (
              <p
                data-testid="flags-environment-empty"
                className="mb-4 rounded-lg bg-white border border-slate-200 p-3 text-sm text-slate-700"
              >
                {noFlagsConfiguredText(otherEnvironment)}
              </p>
            )}
          </div>
        )}

        {production && !busy && !error && !evaluationsSeen && (
          <p
            role="status"
            data-testid="flag-stale-notice"
            className="mb-4 rounded-lg bg-slate-100 border border-slate-200 p-3 text-sm text-slate-700"
          >
            {`No flag evaluations have been recorded in the last ${
              staleDays === undefined ? 'N' : staleDays
            } days, so no flag is marked “Not evaluated”.`}
          </p>
        )}

        {busy && (
          <div className="text-center py-12" data-testid="flags-loading">
            <div className="inline-block w-6 h-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
            <p className="text-slate-500 mt-2 text-sm">Loading feature flags...</p>
          </div>
        )}

        {!busy && error && (
          <div
            role="alert"
            className="rounded-lg bg-red-50 border border-red-200 p-4 text-sm text-red-700 flex items-center justify-between gap-4"
            data-testid="flags-error"
          >
            <span>{error}</span>
            <button
              type="button"
              onClick={() => void load()}
              className="px-3 py-1 rounded-md text-xs font-medium bg-white border border-red-200 hover:bg-red-100"
              data-testid="flags-retry"
            >
              Retry
            </button>
          </div>
        )}

        {isEmpty && (
          <div
            className="text-center py-16 bg-white rounded-lg border border-slate-200"
            data-testid="flags-empty"
          >
            <p className="text-slate-500 mb-4">
              {!production
                ? 'No feature flags yet'
                : staleFilter === 'stale'
                  ? 'No stale flags right now.'
                  : statusFilter === 'all'
                    ? 'No feature flags yet'
                    : 'No flags match this filter'}
            </p>
            {!production ? null : statusFilter === 'all' && staleFilter === 'any' ? (
              canCreate && (
                <Link
                  href="/feature-flags/new"
                  className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
                >
                  Create your first flag
                </Link>
              )
            ) : (
              <button
                type="button"
                onClick={() => {
                  setStatusFilter('all');
                  setStaleFilter('any');
                }}
                className="text-sm text-blue-600 hover:underline"
              >
                Show all flags
              </button>
            )}
          </div>
        )}

        {Object.keys(toggleErrors).length > 0 && (
          <div
            role="alert"
            className="mb-4 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700 space-y-1"
            data-testid="flag-toggle-error"
          >
            {Object.entries(toggleErrors).map(([id, message]) => (
              <p key={id} data-testid={`flag-toggle-error-${id}`}>
                {message}
              </p>
            ))}
          </div>
        )}

        {!busy && !error && flags.length > 0 && (
          <div
            className="bg-white rounded-lg border border-slate-200 overflow-x-auto"
            data-testid="flags-table"
            data-environment={environment ?? undefined}
          >
            <table className="w-full text-sm">
              <thead className="bg-slate-50 border-b border-slate-200">
                <tr>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Key</th>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Name</th>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Type</th>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Status</th>
                  <th className="text-right px-4 py-3 text-slate-600 font-medium">Rollout</th>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Updated</th>
                  {canChange && (
                    <th className="text-right px-4 py-3 text-slate-600 font-medium">On / Off</th>
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {flags.map((listed) => {
                  // Outside Production the row is the flag as it is there
                  // (`inEnvironment`): none means off, and the row says it was
                  // never set. In Production it is the listed flag itself.
                  const settings = otherEnvironment ? (envSettings[listed.id] ?? unconfigured(otherEnvironment)) : null;
                  const flag = settings ? inEnvironment(listed, settings) : listed;
                  const on = isFlagOn(flag);
                  // The API refuses to turn an archived flag on (400), so its
                  // row has no switch: it says to unarchive it first, which is
                  // done on the flag's page (#1748, #442).
                  const archived = isFlagArchived(flag);
                  return (
                    <tr
                      key={flag.id}
                      className="hover:bg-slate-50 transition-colors"
                      data-testid="flag-row"
                      data-flag-id={flag.id}
                      data-flag-key={flag.key}
                    >
                      <td className="px-4 py-3 font-mono text-xs text-slate-700">{flag.key}</td>
                      <td className="px-4 py-3">
                        <Link
                          href={inEnvironmentUrl(`/feature-flags/${flag.id}`, switchable ? environment : null)}
                          className="font-medium text-blue-600 hover:text-blue-800 hover:underline"
                          data-testid="flag-link"
                        >
                          {flag.name}
                        </Link>
                        {flag.description && (
                          <p className="text-xs text-slate-500 mt-0.5 truncate max-w-xs">
                            {flag.description}
                          </p>
                        )}
                      </td>
                      {/* What the flag serves (#1497); a flag from a server without values is Boolean. */}
                      <td className="px-4 py-3 text-slate-700" data-testid="flag-value-type">
                        {typeLabel(flagValueType(flag))}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span
                            className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                              on
                                ? 'bg-green-100 text-green-800'
                                : archived
                                  ? 'bg-slate-200 text-slate-800'
                                  : 'bg-slate-100 text-slate-700'
                            }`}
                            data-testid="flag-status-pill"
                          >
                            {archived ? ARCHIVED_LABEL : on ? 'On' : 'Off'}
                          </span>
                          {otherEnvironment && settings && !settings.configured && (
                            <span className="text-xs text-slate-700" data-testid="flag-not-configured">
                              {notConfiguredText(otherEnvironment)}
                            </span>
                          )}
                          {/* From the server's `stale`, never from tags or rules read here (#1808). */}
                          {flag.stale === true && (
                            <span
                              className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-200 text-slate-800"
                              data-testid="flag-stale-pill"
                            >
                              Stale
                            </span>
                          )}
                        </div>
                        {flag.stale === true && (flag.stale_reasons ?? []).length > 0 && (
                          <ul className="mt-1 text-xs text-slate-500" data-testid="flag-stale-reasons">
                            {(flag.stale_reasons ?? []).map((reason) => (
                              <li key={reason}>{staleReasonText(reason, staleDays)}</li>
                            ))}
                          </ul>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-700">
                        {flag.rollout_percentage}%
                      </td>
                      <td className="px-4 py-3 text-slate-500">{formatDate(flag.updated_at)}</td>
                      {canChange && (
                        <td className="px-4 py-3 text-right">
                          {archived ? (
                            <ArchivedFlagRowNote flagKey={flag.key} />
                          ) : (
                            <FlagToggle
                              on={on}
                              pending={Boolean(toggling[flag.id])}
                              label={`Turn ${flag.key} ${on ? 'off' : 'on'}${environmentLabel ? ` in ${environmentLabel}` : ''}`}
                              onChange={async () => {
                                if (toggleLock.current.has(flag.id)) return;
                                toggleLock.current.add(flag.id);
                                try {
                                  await handleToggle(flag);
                                } finally {
                                  toggleLock.current.delete(flag.id);
                                }
                              }}
                              data-testid={`flag-toggle-${flag.key}`}
                            />
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
