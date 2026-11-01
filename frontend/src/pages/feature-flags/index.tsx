import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { PageTitle } from '@/components/PageTitle';
import { FlagToggle } from '@/components/feature-flags/FlagToggle';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { FLAG_ROLE_NOTE, canChangeFeatureFlags } from '@/utils/experimentPermissions';
import {
  FeatureFlag,
  FeatureFlagStatus,
  FeatureFlagsService,
  isFlagOn,
} from '@/services/featureFlags';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
import { flagValueType, typeLabel } from '@/utils/flagValues';

const STATUS_FILTERS: Array<{ label: string; value: FeatureFlagStatus | 'all' }> = [
  { label: 'All', value: 'all' },
  { label: 'On', value: 'active' },
  { label: 'Off', value: 'inactive' },
];

function formatDate(value: string): string {
  const d = parseApiTimestamp(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleDateString();
}

export default function FeatureFlagsPage() {
  const auth = useOptionalAuth();
  // ANALYST and VIEWER read flags and change none (#917): they get no
  // "+ New Flag", no create link and no On/Off column, and a note saying why.
  const canChange = canChangeFeatureFlags(auth?.user);
  const [statusFilter, setStatusFilter] = useState<FeatureFlagStatus | 'all'>('all');
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [totalFlags, setTotalFlags] = useState(0);
  const [toggling, setToggling] = useState<Record<string, boolean>>({});
  // Keyed by flag id: two toggles can fail at once and both messages matter.
  const [toggleErrors, setToggleErrors] = useState<Record<string, string>>({});

  /**
   * Sequence number of the most recent list request. Switching filters quickly
   * leaves the earlier request in flight; without this guard a slow "On"
   * response can land after a fast "Off" one and paint the wrong rows (which a
   * subsequent toggle would then act on).
   */
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    requestSeq.current += 1;
    const seq = requestSeq.current;
    const isCurrent = () => seq === requestSeq.current;

    setLoading(true);
    setError(null);
    try {
      const data = await FeatureFlagsService.list(
        statusFilter !== 'all' ? { status: statusFilter } : undefined,
      );
      if (!isCurrent()) return;
      setFlags(data.items ?? []);
      setTotalFlags(data.total ?? (data.items ?? []).length);
    } catch (err) {
      if (!isCurrent()) return;
      setError(err instanceof Error ? err.message : 'Failed to load feature flags');
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * Optimistic on/off: flip locally, call enable/disable, and on failure revert
   * only this row — a whole-list snapshot would also undo any other toggle that
   * succeeded while this request was in flight.
   */
  const handleToggle = async (flag: FeatureFlag) => {
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

  const isEmpty = !loading && !error && flags.length === 0;

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="Feature Flags" />
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold text-slate-900">Feature Flags</h1>
            <p className="text-sm text-slate-500 mt-1">
              Turn features on and off, roll them out gradually and watch their safety signals
            </p>
          </div>
          {canChange ? (
            <Link
              href="/feature-flags/new"
              className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
              data-testid="new-flag-btn"
            >
              + New Flag
            </Link>
          ) : (
            <p className="text-sm text-slate-600" data-testid="flags-role-note">
              {FLAG_ROLE_NOTE}
            </p>
          )}
        </div>

        {/* Filters */}
        {totalFlags > flags.length && flags.length > 0 && (
          <p data-testid="flag-limit-notice" className="text-xs text-slate-500 mb-3">
            Showing {flags.length} of {totalFlags} flags.
          </p>
        )}

        <div className="flex gap-2 mb-6 flex-wrap" data-testid="flag-status-filter">
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

        {loading && (
          <div className="text-center py-12" data-testid="flags-loading">
            <div className="inline-block w-6 h-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
            <p className="text-slate-500 mt-2 text-sm">Loading feature flags...</p>
          </div>
        )}

        {!loading && error && (
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
              {statusFilter === 'all' ? 'No feature flags yet' : 'No flags match this filter'}
            </p>
            {statusFilter === 'all' ? (
              canChange && (
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
                onClick={() => setStatusFilter('all')}
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

        {!loading && !error && flags.length > 0 && (
          <div
            className="bg-white rounded-lg border border-slate-200 overflow-hidden"
            data-testid="flags-table"
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
                {flags.map((flag) => {
                  const on = isFlagOn(flag);
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
                          href={`/feature-flags/${flag.id}`}
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
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                            on ? 'bg-green-100 text-green-800' : 'bg-slate-100 text-slate-700'
                          }`}
                          data-testid="flag-status-pill"
                        >
                          {on ? 'On' : 'Off'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-700">
                        {flag.rollout_percentage}%
                      </td>
                      <td className="px-4 py-3 text-slate-500">{formatDate(flag.updated_at)}</td>
                      {canChange && (
                        <td className="px-4 py-3 text-right">
                          <FlagToggle
                            on={on}
                            disabled={Boolean(toggling[flag.id])}
                            label={`Turn ${flag.key} ${on ? 'off' : 'on'}`}
                            onChange={() => void handleToggle(flag)}
                            data-testid={`flag-toggle-${flag.key}`}
                          />
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
