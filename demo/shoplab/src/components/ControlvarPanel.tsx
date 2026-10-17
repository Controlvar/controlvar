import React, { useState } from 'react';
import { useExperiment, useExperimentation, useFeatureFlag } from '@getexperimently/react-sdk';
import { DASHBOARD_URL, EXPERIMENT_KEYS, FLAG_KEYS } from '@/lib/env';
import { useEventLog } from '@/lib/eventLog';
import { resetVisitor } from '@/lib/visitor';

export const PANEL_EVENT_LIMIT = 8;

const EXPERIMENT_ROWS: Array<{ key: string; label: string }> = [
  { key: EXPERIMENT_KEYS.hero, label: 'Hero banner' },
  { key: EXPERIMENT_KEYS.plpSort, label: 'Product sort (bandit)' },
  { key: EXPERIMENT_KEYS.pdpBuyButton, label: 'Buy button (MVT)' },
  { key: EXPERIMENT_KEYS.checkoutFlow, label: 'Checkout flow' },
];

const FLAG_ROWS: Array<{ key: string; label: string }> = [
  { key: FLAG_KEYS.newSearch, label: 'New search' },
  { key: FLAG_KEYS.freeShippingBanner, label: 'Free-shipping banner' },
];

interface Props {
  /** Override for tests; defaults to regenerating the visitor id and reloading. */
  onNewVisitor?: () => void;
  defaultOpen?: boolean;
}

function defaultNewVisitor() {
  resetVisitor();
  window.location.reload();
}

export default function ControlvarPanel({ onNewVisitor = defaultNewVisitor, defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const { user } = useExperimentation();
  const events = useEventLog();

  // Hooks must be called unconditionally, so the four experiments and two flags are
  // evaluated up-front. This mirrors the simulator, which assigns every visitor to all
  // four experiments too.
  const hero = useExperiment(EXPERIMENT_KEYS.hero);
  const plp = useExperiment(EXPERIMENT_KEYS.plpSort);
  const pdp = useExperiment(EXPERIMENT_KEYS.pdpBuyButton);
  const checkout = useExperiment(EXPERIMENT_KEYS.checkoutFlow);
  const newSearch = useFeatureFlag(FLAG_KEYS.newSearch);
  const freeShipping = useFeatureFlag(FLAG_KEYS.freeShippingBanner);

  const assignments = [hero, plp, pdp, checkout];
  const flags = [newSearch, freeShipping];
  const recent = events.slice(-PANEL_EVENT_LIMIT).reverse();

  return (
    <aside className="fixed bottom-4 right-4 z-50 w-[min(24rem,calc(100vw-2rem))] text-sm" aria-label="Controlvar panel">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls="controlvar-panel-body"
        className="ml-auto flex items-center gap-2 rounded-full bg-neutral-900 px-4 py-2 font-semibold text-white shadow-lg hover:bg-neutral-800"
      >
        <span className="inline-block h-2 w-2 rounded-full bg-emerald-400" aria-hidden="true" />
        Powered by Controlvar
        <span aria-hidden="true">{open ? '▾' : '▴'}</span>
      </button>
      {open && (
        <div
          id="controlvar-panel-body"
          className="mt-2 max-h-[70vh] overflow-y-auto rounded-lg border border-neutral-200 bg-white p-4 shadow-xl"
        >
          <section className="mb-4">
            <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">Visitor</h2>
            <code className="block truncate rounded bg-neutral-100 px-2 py-1 font-mono text-xs" data-testid="panel-visitor-id">
              {user.userId}
            </code>
            <dl className="mt-1 flex gap-3 text-xs text-neutral-600">
              {Object.entries(user.attributes ?? {}).map(([k, v]) => (
                <div key={k}>
                  <dt className="inline">{k}: </dt>
                  <dd className="inline font-medium">{String(v)}</dd>
                </div>
              ))}
            </dl>
          </section>

          <section className="mb-4">
            <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">Experiments</h2>
            <ul className="divide-y divide-neutral-100">
              {EXPERIMENT_ROWS.map((row, i) => {
                const a = assignments[i];
                return (
                  <li key={row.key} className="flex items-center justify-between py-1.5" data-testid={`panel-exp-${row.key}`}>
                    <div>
                      <div className="font-medium">{row.label}</div>
                      <code className="text-xs text-neutral-500">{row.key}</code>
                    </div>
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                        a.error ? 'bg-red-100 text-red-800' : a.isControl ? 'bg-neutral-100 text-neutral-700' : 'bg-accent-100 text-accent-700'
                      }`}
                      title={a.error ? a.error.message : a.variantKey}
                    >
                      {a.loading ? 'loading…' : a.error ? 'error' : a.variantName}
                    </span>
                  </li>
                );
              })}
            </ul>
          </section>

          <section className="mb-4">
            <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">Feature flags</h2>
            <ul className="divide-y divide-neutral-100">
              {FLAG_ROWS.map((row, i) => {
                const f = flags[i];
                return (
                  <li key={row.key} className="flex items-center justify-between py-1.5" data-testid={`panel-flag-${row.key}`}>
                    <div>
                      <div className="font-medium">{row.label}</div>
                      <code className="text-xs text-neutral-500">{row.key}</code>
                    </div>
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                        f.isEnabled ? 'bg-emerald-100 text-emerald-800' : 'bg-neutral-100 text-neutral-600'
                      }`}
                    >
                      {f.loading ? 'loading…' : f.isEnabled ? 'on' : 'off'}
                    </span>
                  </li>
                );
              })}
            </ul>
          </section>

          <section className="mb-4">
            <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">
              Last {PANEL_EVENT_LIMIT} events
            </h2>
            {recent.length === 0 ? (
              <p className="text-xs text-neutral-500">No events yet.</p>
            ) : (
              <ol className="space-y-1 font-mono text-xs" data-testid="panel-events">
                {recent.map((e) => (
                  <li key={e.id} className="truncate" title={JSON.stringify(e.properties ?? {})}>
                    <span className="font-semibold text-neutral-800">{e.name}</span>
                    {e.options?.value !== undefined && <span className="text-emerald-700"> ${e.options.value}</span>}
                    {e.options?.featureFlagKey && <span className="text-neutral-500"> flag:{e.options.featureFlagKey}</span>}
                    {e.properties && <span className="text-neutral-500"> {JSON.stringify(e.properties)}</span>}
                  </li>
                ))}
              </ol>
            )}
          </section>

          <div className="flex items-center justify-between gap-2 border-t border-neutral-100 pt-3">
            <button type="button" onClick={onNewVisitor} className="btn-secondary text-xs">
              New visitor
            </button>
            <a
              href={`${DASHBOARD_URL}/experiments`}
              target="_blank"
              rel="noreferrer"
              className="text-xs font-semibold text-accent-600 hover:underline"
            >
              Open dashboard ↗
            </a>
          </div>
        </div>
      )}
    </aside>
  );
}
