import React from 'react';
import { useExperimentation, useFeatureFlag } from '@controlvar/react-sdk';
import { useAssignedExperiments } from '@/lib/assignments';
import { DASHBOARD_URL, EXPERIMENT_KEYS, FLAG_KEYS } from '@/lib/env';
import { useEventLog } from '@/lib/eventLog';
import { assignmentPill, assignmentReasonLabel, flagReasonLabel, NOT_ASSIGNED_PILL, notAssignedLabel } from '@/lib/labels';

export const PANEL_EVENT_LIMIT = 8;

export const FLAG_ROWS: Array<{ key: string; label: string; feature: string }> = [
  { key: FLAG_KEYS.recsV2, label: 'Recommendations v2', feature: 'kill switch' },
  { key: FLAG_KEYS.playerV2, label: 'Player v2', feature: 'gradual rollout + safety' },
  { key: FLAG_KEYS.aiSearch, label: 'AI search', feature: 'targeting rules' },
  { key: FLAG_KEYS.offlineMode, label: 'Offline mode', feature: 'SDK cache' },
];

/** Each experiment with the screen that assigns it (the screen's own useExperiment). */
export const EXPERIMENT_ROWS: Array<{ key: string; label: string; feature: string; screen: string }> = [
  { key: EXPERIMENT_KEYS.pushFrequency, label: 'Push frequency', feature: 'sequential + guardrail', screen: 'Notifications' },
  { key: EXPERIMENT_KEYS.wrapped, label: '2026 Wrapped', feature: 'mutual exclusion', screen: 'Profile' },
  { key: EXPERIMENT_KEYS.profileBadges, label: 'Profile badges', feature: 'mutual exclusion', screen: 'Profile' },
  { key: EXPERIMENT_KEYS.onboardingSteps, label: 'Onboarding steps', feature: 'Bayesian', screen: 'Onboarding' },
  { key: EXPERIMENT_KEYS.upsellModal, label: 'Upsell modal', feature: 'audit trail', screen: 'Payments' },
];

interface Props {
  /** Clear the SDK cache and re-evaluate everything for the current device. */
  onRefresh: () => void;
}

/**
 * "Powered by Controlvar" panel: what the platform decided for the current
 * device (every flag with its `reason`, and each experiment a screen has
 * assigned, with its variant and enrolment status), the last 8 events,
 * dashboard links and "Refresh now". Must be rendered inside the SDK provider.
 */
export default function ControlvarPanel({ onRefresh }: Props) {
  const { client, user } = useExperimentation();
  const events = useEventLog();

  // Every flag is evaluated here, the same set the device simulator evaluates.
  // No experiment is: an assignment records that the device saw the experiment,
  // so only the screen that uses one assigns it, and the panel shows what the
  // screens have assigned.
  const recs = useFeatureFlag(FLAG_KEYS.recsV2);
  const player = useFeatureFlag(FLAG_KEYS.playerV2);
  const aiSearch = useFeatureFlag(FLAG_KEYS.aiSearch);
  const offline = useFeatureFlag(FLAG_KEYS.offlineMode);
  const assigned = useAssignedExperiments();

  const flags = [recs, player, aiSearch, offline];
  const recent = events.slice(-PANEL_EVENT_LIMIT).reverse();

  const refresh = () => {
    client.clearCache();
    onRefresh();
  };

  return (
    <section className="card p-4 text-sm" aria-labelledby="controlvar-panel-title">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 id="controlvar-panel-title" className="flex items-center gap-2 text-sm font-bold uppercase tracking-wide text-neutral-700">
          <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" aria-hidden="true" />
          Powered by Controlvar
        </h2>
        <button type="button" className="btn-secondary text-xs" onClick={refresh}>
          Refresh now
        </button>
      </div>

      <p className="mb-3 text-xs text-neutral-500">
        Decisions for <code className="rounded bg-neutral-100 px-1 font-mono" data-testid="panel-user-id">{user.userId}</code>: flags are
        fetched when you pick a device or press Refresh now, and each experiment when its screen opens.
      </p>

      <section className="mb-4" aria-labelledby="panel-flags-title">
        <h3 id="panel-flags-title" className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">
          Feature flags
        </h3>
        <ul className="divide-y divide-neutral-100">
          {FLAG_ROWS.map((row, i) => {
            const f = flags[i];
            const pill = f.loading ? 'pill-off' : f.error ? 'pill-error' : f.isEnabled ? 'pill-on' : 'pill-off';
            return (
              <li key={row.key} className="flex items-center justify-between gap-2 py-1.5" data-testid={`panel-flag-${row.key}`}>
                <div className="min-w-0">
                  <div className="font-medium">
                    {row.label} <span className="text-xs font-normal text-neutral-400">· {row.feature}</span>
                  </div>
                  <code className="block truncate text-xs text-neutral-500">{row.key}</code>
                  <div className="text-xs text-neutral-600" data-testid={`panel-flag-reason-${row.key}`}>
                    {flagReasonLabel(f)}
                  </div>
                </div>
                <span className={pill}>{f.loading ? 'loading…' : f.error ? 'error' : f.isEnabled ? 'on' : 'off'}</span>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="mb-4" aria-labelledby="panel-experiments-title">
        <h3 id="panel-experiments-title" className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">
          Experiments
        </h3>
        <ul className="divide-y divide-neutral-100">
          {EXPERIMENT_ROWS.map((row) => {
            const a = assigned[row.key];
            const pill = !a ? 'pill-off' : a.assigned === false ? 'pill-warn' : a.isControl ? 'pill-control' : 'pill-variant';
            return (
              <li key={row.key} className="flex items-center justify-between gap-2 py-1.5" data-testid={`panel-exp-${row.key}`}>
                <div className="min-w-0">
                  <div className="font-medium">
                    {row.label} <span className="text-xs font-normal text-neutral-400">· {row.feature}</span>
                  </div>
                  <code className="block truncate text-xs text-neutral-500">{row.key}</code>
                  <div className="text-xs text-neutral-600" data-testid={`panel-exp-reason-${row.key}`}>
                    {a ? assignmentReasonLabel(a) : notAssignedLabel(row.screen)}
                  </div>
                </div>
                <span className={pill} title={a?.variantKey}>
                  {a ? assignmentPill(a) : NOT_ASSIGNED_PILL}
                </span>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="mb-4" aria-labelledby="panel-events-title">
        <h3 id="panel-events-title" className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">
          Last {PANEL_EVENT_LIMIT} events
        </h3>
        {recent.length === 0 ? (
          <p className="text-xs text-neutral-500">No events yet — tap around in the phone.</p>
        ) : (
          <ol className="space-y-1 font-mono text-xs" data-testid="panel-events">
            {recent.map((e) => (
              <li key={e.id} className="truncate" title={JSON.stringify(e.properties ?? {})}>
                <span className="font-semibold text-neutral-800">{e.name}</span>
                {e.options?.experimentKey && <span className="text-pulse-700"> exp:{e.options.experimentKey}</span>}
                {e.options?.featureFlagKey && <span className="text-emerald-700"> flag:{e.options.featureFlagKey}</span>}
                {e.properties && <span className="text-neutral-500"> {JSON.stringify(e.properties)}</span>}
              </li>
            ))}
          </ol>
        )}
      </section>

      <nav className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-neutral-100 pt-3 text-xs font-semibold text-pulse-700" aria-label="Dashboard links">
        <a href={`${DASHBOARD_URL}/feature-flags`} target="_blank" rel="noreferrer" className="hover:underline">
          Feature flags ↗
        </a>
        <a href={`${DASHBOARD_URL}/experiments`} target="_blank" rel="noreferrer" className="hover:underline">
          Experiments ↗
        </a>
        <a href={`${DASHBOARD_URL}/admin/safety`} target="_blank" rel="noreferrer" className="hover:underline">
          Safety ↗
        </a>
        <a href={`${DASHBOARD_URL}/admin/audit`} target="_blank" rel="noreferrer" className="hover:underline">
          Audit log ↗
        </a>
      </nav>
    </section>
  );
}
