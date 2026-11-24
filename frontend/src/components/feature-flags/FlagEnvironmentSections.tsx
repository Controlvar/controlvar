import React, { useCallback, useEffect, useId, useState } from 'react';
import { FeatureFlagsService, FlagEnvironmentSettings, FlagEnvironmentsResponse } from '@/services/featureFlags';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
import {
  FLAG_ENVIRONMENTS,
  FLAG_ENVIRONMENT_LABELS,
  FlagEnvironment,
  PRODUCTION_BANNER,
  PRODUCTION_ONLY_HEADING,
  PRODUCTION_ONLY_NOTE,
  SHARED_SETTINGS_NOTE,
} from '@/utils/flagEnvironments';

/** On the flag pages in Production, for a user who may change flags. Text, not a live region. */
export function ProductionBanner({ className = 'mb-4' }: { className?: string }) {
  return (
    <p
      className={`${className} rounded-lg border border-amber-700 bg-amber-50 p-3 text-sm font-medium text-amber-900`}
      data-testid="production-banner"
    >
      {PRODUCTION_BANNER}
    </p>
  );
}

/**
 * On a flag's page in Staging or Development, in place of the rollout
 * schedule and the safety check: what runs in Production only, and what the
 * page does not change outside it.
 */
export function NonProductionNote() {
  return (
    <section
      className="bg-white rounded-lg border border-slate-200 p-6 mb-6 space-y-2 text-sm text-slate-700"
      data-testid="production-only-note"
    >
      <h2 className="text-base font-semibold text-slate-800">{PRODUCTION_ONLY_HEADING}</h2>
      <p>{PRODUCTION_ONLY_NOTE}</p>
      <p data-testid="shared-settings-note">{SHARED_SETTINGS_NOTE}</p>
    </section>
  );
}

/** The comparison's columns: the switcher's order. */
const COMPARE_ORDER: FlagEnvironment[] = [...FLAG_ENVIRONMENTS].reverse();

function statusText(settings: FlagEnvironmentSettings): string {
  const status = typeof settings.status === 'string' ? settings.status.toLowerCase() : '';
  if (status === 'archived') return 'Archived';
  return status === 'active' ? 'On' : 'Off';
}

/** Targeting rules in a few words: the dashboard shape's groups and conditions, or "None". */
export function rulesSummary(rules: unknown): string {
  if (rules === null || rules === undefined) return 'None';
  if (typeof rules !== 'object') return 'Set';
  const groups = (rules as { groups?: unknown }).groups;
  if (!Array.isArray(groups)) return 'Set';
  if (groups.length === 0) return 'None';
  const conditions = groups.reduce<number>((n, group) => {
    const list = (group as { conditions?: unknown })?.conditions;
    return n + (Array.isArray(list) ? list.length : 0);
  }, 0);
  const c = `${conditions} ${conditions === 1 ? 'condition' : 'conditions'}`;
  const g = `${groups.length} ${groups.length === 1 ? 'group' : 'groups'}`;
  return `${c} in ${g}`;
}

function sameRules(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

function changedText(value: string | null): string {
  if (!value) return '—';
  const d = parseApiTimestamp(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

/**
 * "Compare environments": the same flag in Development, Staging and
 * Production side by side, read from `GET /feature-flags/{id}/environments`.
 * It is loaded when opened, and again whenever `refreshKey` changes while it
 * is open (after a change on the page). A failure stays inside the section.
 */
export function EnvironmentComparison({ flagId, refreshKey = 0 }: { flagId: string; refreshKey?: number }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<FlagEnvironmentsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const regionId = useId();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await FeatureFlagsService.getEnvironments(flagId));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load the environments.');
    } finally {
      setLoading(false);
    }
  }, [flagId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load, refreshKey]);

  const byEnvironment = new Map<string, FlagEnvironmentSettings>(
    (data?.environments ?? []).map((settings) => [String(settings.environment), settings]),
  );
  const production = byEnvironment.get('production');

  const cells = (render: (settings: FlagEnvironmentSettings, env: FlagEnvironment) => React.ReactNode) =>
    COMPARE_ORDER.map((env) => {
      const settings = byEnvironment.get(env);
      return (
        <td key={env} className="px-3 py-2 text-slate-800" data-environment={env}>
          {settings ? render(settings, env) : '—'}
        </td>
      );
    });

  return (
    <section className="bg-white rounded-lg border border-slate-200 p-6" data-testid="environment-comparison">
      <h2 className="text-base font-semibold text-slate-800">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={regionId}
          onClick={() => setOpen((was) => !was)}
          className="inline-flex items-center gap-2 rounded-md text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
          data-testid="environment-comparison-toggle"
        >
          <span aria-hidden="true">{open ? '▾' : '▸'}</span>
          Compare environments
        </button>
      </h2>
      <div id={regionId} hidden={!open} className="mt-3">
        {loading && !data && (
          <p className="text-sm text-slate-600" data-testid="environment-comparison-loading">
            Loading…
          </p>
        )}
        {error && (
          <p role="alert" className="text-sm text-red-700" data-testid="environment-comparison-error">
            {error}
          </p>
        )}
        {data && (
          // A phone scrolls the table inside this region, never the page.
          <div
            className="overflow-x-auto rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
            role="region"
            aria-label={`${data.key} in each environment`}
            tabIndex={0}
          >
            <table className="w-full text-sm" data-testid="environment-comparison-table">
              <caption className="sr-only">{`${data.key} in each environment`}</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  <th scope="col" className="px-3 py-2 text-left font-medium text-slate-600">
                    Setting
                  </th>
                  {COMPARE_ORDER.map((env) => (
                    <th key={env} scope="col" className="px-3 py-2 text-left font-medium text-slate-600">
                      {FLAG_ENVIRONMENT_LABELS[env]}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                <tr data-testid="compare-status">
                  <th scope="row" className="px-3 py-2 text-left font-medium text-slate-700">
                    Status
                  </th>
                  {cells((settings) => (
                    <>
                      {statusText(settings)}
                      {!settings.configured && <span className="block text-xs text-slate-600">Not configured</span>}
                    </>
                  ))}
                </tr>
                <tr data-testid="compare-rollout">
                  <th scope="row" className="px-3 py-2 text-left font-medium text-slate-700">
                    Rollout
                  </th>
                  {cells((settings) => `${settings.rollout_percentage ?? 0}%`)}
                </tr>
                <tr data-testid="compare-rules">
                  <th scope="row" className="px-3 py-2 text-left font-medium text-slate-700">
                    Targeting rules
                  </th>
                  {cells((settings, env) => (
                    <>
                      {rulesSummary(settings.targeting_rules)}
                      {env !== 'production' &&
                        production &&
                        settings.targeting_rules != null &&
                        sameRules(settings.targeting_rules, production.targeting_rules) && (
                          <span className="block text-xs text-slate-600">Same as Production</span>
                        )}
                    </>
                  ))}
                </tr>
                <tr data-testid="compare-updated">
                  <th scope="row" className="px-3 py-2 text-left font-medium text-slate-700">
                    Last changed
                  </th>
                  {cells((settings) => changedText(settings.updated_at))}
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
