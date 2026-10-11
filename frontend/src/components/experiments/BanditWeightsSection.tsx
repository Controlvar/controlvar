import React, { useCallback, useEffect, useState } from 'react';
import { BanditService } from '@/services/bandit';
import { BanditStatus } from '@/types/bandit';
import { Experiment } from '@/types/experiments';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

/**
 * Fixed copy. Nothing the server sends in an error is shown: a refusal or a
 * failure reads the same, so a stack trace or a stale message never reaches
 * the page.
 */
/** True in every state, so it is shown in every state. */
export const BANDIT_INTRO =
  'Traffic moves toward the better-performing variant automatically. People already in the ' +
  'experiment keep their variant.';

/** Shown only once weights exist: before then new users follow the starting allocation. */
export const BANDIT_FOLLOWS =
  'The Allocation column is the starting split; new users follow the weights below.';

export const BANDIT_EMPTY =
  'No weights yet. Weights appear after the first update; until then new users are split by ' +
  "each variant's starting allocation.";

export const BANDIT_ERROR = 'Current weights could not be loaded. Use Refresh to try again.';

export const BANDIT_FOOTNOTE =
  'Weights are recomputed on a schedule, and this panel does not refresh by itself. ' +
  'Past weights are not shown.';

/** A fraction in [0, 1] as a percentage with one decimal, e.g. 0.4567 → "45.7%". */
export function formatShare(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function formatCount(value: number): string {
  return value.toLocaleString('en-US');
}

function formatWhen(value: string): string {
  const d = parseApiTimestamp(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

/** Whether an experiment's traffic is set by a bandit rather than a fixed split. */
export function isBandit(experiment: Pick<Experiment, 'optimization_type'>): boolean {
  return (experiment.optimization_type ?? 'fixed') !== 'fixed';
}

type State =
  | { kind: 'loading' }
  | { kind: 'error' }
  | { kind: 'loaded'; status: BanditStatus };

/**
 * "Current traffic weights": a bandit experiment's live split, read once on
 * load and again on Refresh. Read-only for every role, because reading it is
 * allowed to every signed-in user and changing it is not offered here.
 * Renders nothing for a fixed-allocation experiment.
 */
export function BanditWeightsSection({ experiment }: { experiment: Experiment }) {
  const bandit = isBandit(experiment);
  const [state, setState] = useState<State>({ kind: 'loading' });

  const load = useCallback(async () => {
    setState({ kind: 'loading' });
    try {
      const status = await BanditService.status(experiment.id);
      setState({ kind: 'loaded', status });
    } catch {
      setState({ kind: 'error' });
    }
  }, [experiment.id]);

  useEffect(() => {
    if (bandit) void load();
  }, [bandit, load]);

  if (!bandit) return null;

  const loaded = state.kind === 'loaded' ? state.status : null;
  const neverUpdated = loaded !== null && loaded.last_updated === null;

  return (
    <section
      className="mt-6 bg-white rounded-lg border border-slate-200 p-5"
      aria-labelledby="bandit-weights-heading"
      data-testid="bandit-weights"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 mb-2">
        <h2 id="bandit-weights-heading" className="text-base font-semibold text-slate-800">
          Current traffic weights
        </h2>
        <button
          type="button"
          onClick={() => void load()}
          disabled={state.kind === 'loading'}
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 disabled:opacity-50"
          data-testid="bandit-weights-refresh"
        >
          Refresh
        </button>
      </div>
      <p className="text-sm text-slate-600 mb-3">{BANDIT_INTRO}</p>

      {state.kind === 'loading' && (
        <p role="status" className="text-sm text-slate-500" data-testid="bandit-weights-loading">
          Loading weights…
        </p>
      )}

      {state.kind === 'error' && (
        <div
          role="alert"
          className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
          data-testid="bandit-weights-error"
        >
          {BANDIT_ERROR}
        </div>
      )}

      {neverUpdated && (
        <p className="text-sm text-slate-700" data-testid="bandit-weights-empty">
          {BANDIT_EMPTY}
        </p>
      )}

      {loaded && !neverUpdated && (
        <>
          <p className="text-sm text-slate-600 mb-3" data-testid="bandit-weights-follows">
            {BANDIT_FOLLOWS}
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="bandit-weights-table">
              <thead className="bg-slate-50 border-b border-slate-200">
                <tr>
                  <th scope="col" className="text-left px-3 py-2 text-slate-600 font-medium">
                    Variant
                  </th>
                  <th scope="col" className="text-right px-3 py-2 text-slate-600 font-medium">
                    Current weight
                  </th>
                  <th scope="col" className="text-right px-3 py-2 text-slate-600 font-medium">
                    Pulls
                  </th>
                  <th scope="col" className="text-right px-3 py-2 text-slate-600 font-medium">
                    Successes
                  </th>
                  <th scope="col" className="text-right px-3 py-2 text-slate-600 font-medium">
                    Conversion rate
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {loaded.current_weights.map((w) => (
                  <tr key={w.variant_id} data-testid="bandit-weight-row" data-variant-id={w.variant_id}>
                    <td className="px-3 py-2 font-medium text-slate-800" data-testid="bandit-weight-name">
                      {w.variant_name}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums" data-testid="bandit-weight-share">
                      {formatShare(w.current_weight)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums" data-testid="bandit-weight-pulls">
                      {formatCount(w.pulls)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums" data-testid="bandit-weight-successes">
                      {formatCount(w.successes)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums" data-testid="bandit-weight-rate">
                      {formatShare(w.conversion_rate)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs text-slate-500" data-testid="bandit-weights-updated">
            Last updated {formatWhen(loaded.last_updated as string)}. {BANDIT_FOOTNOTE}
          </p>
        </>
      )}
    </section>
  );
}
