import React, { useCallback, useRef, useState } from 'react';
import {
  formatShare,
  formatWhen,
  isBandit,
} from '@/components/experiments/BanditWeightsSection';
import { BanditService } from '@/services/bandit';
import { BanditHistoryTick } from '@/types/bandit';
import { Experiment } from '@/types/experiments';

/**
 * Fixed copy. Nothing the server sends in an error is shown: every failure
 * reads the same, so a stack trace or a stale message never reaches the page.
 */
export const HISTORY_INTRO =
  'Each row is one weight update by the bandit, newest first. Weights set by hand are not listed.';

export const HISTORY_LOADING = 'Loading the weight history…';

export const HISTORY_EMPTY =
  'No weight updates yet. The bandit records one each time it updates the weights, every 5 ' +
  'minutes by default while the experiment is active.';

export const HISTORY_ERROR = 'The weight history could not be loaded.';

export const HISTORY_CAPTION = 'Weight updates, newest first';

export const HISTORY_NOT_RECORDED = 'Not recorded';

/** The route's default page: when this many come back, older updates exist. */
export const HISTORY_PAGE = 200;

/** "Showing the 3 most recent updates." and its one-row and full-page forms. */
export function historyCount(count: number): string {
  if (count === 1) return 'Showing the most recent update.';
  if (count >= HISTORY_PAGE) {
    return `Showing the ${HISTORY_PAGE} most recent updates; older ones are not shown here.`;
  }
  return `Showing the ${count} most recent updates.`;
}

type State =
  | { kind: 'closed' }
  | { kind: 'loading' }
  | { kind: 'error' }
  | { kind: 'loaded'; ticks: BanditHistoryTick[] };

/**
 * "Weight history": a bandit's past weight updates as a table, one row per
 * update. Nothing is requested until the reader opens it, and each opening
 * sends one request. Read-only for every role, because the route is open to
 * anyone who may view the experiment. Renders nothing for a fixed-allocation
 * experiment. The witness marks the loaded state only.
 */
export function BanditWeightHistory({ experiment }: { experiment: Experiment }) {
  const bandit = isBandit(experiment);
  const [state, setState] = useState<State>({ kind: 'closed' });
  const heading = useRef<HTMLHeadingElement>(null);
  // Which request the page is waiting for; an answer to an earlier one is dropped.
  const latest = useRef(0);

  const load = useCallback(async () => {
    const mine = ++latest.current;
    setState({ kind: 'loading' });
    try {
      const ticks = await BanditService.history(experiment.id);
      if (mine === latest.current) setState({ kind: 'loaded', ticks });
    } catch {
      if (mine === latest.current) setState({ kind: 'error' });
    }
  }, [experiment.id]);

  const toggle = () => {
    if (state.kind === 'closed') {
      void load();
      return;
    }
    latest.current += 1;
    setState({ kind: 'closed' });
  };

  const retry = () => {
    void load();
    heading.current?.focus();
  };

  if (!bandit) return null;

  const open = state.kind !== 'closed';

  return (
    <section
      className="mt-6 bg-white rounded-lg border border-slate-200 p-5"
      aria-labelledby="bandit-weight-history-heading"
      data-testid="bandit-weight-history-section"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 mb-2">
        <h2
          id="bandit-weight-history-heading"
          ref={heading}
          tabIndex={-1}
          className="text-base font-semibold text-slate-800"
        >
          Weight history
        </h2>
        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          aria-controls="bandit-weight-history-body"
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-white text-slate-700 border border-slate-300 hover:bg-slate-50"
          data-testid="bandit-weight-history-toggle"
        >
          {open ? 'Hide weight history' : 'Show weight history'}
        </button>
      </div>
      <p className="text-sm text-slate-600 mb-3">{HISTORY_INTRO}</p>

      <div id="bandit-weight-history-body">
        {state.kind === 'loading' && (
          <p role="status" className="text-sm text-slate-600" data-testid="bandit-weight-history-loading">
            {HISTORY_LOADING}
          </p>
        )}

        {state.kind === 'error' && (
          <div
            role="alert"
            className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
            data-testid="bandit-weight-history-error"
          >
            <p>{HISTORY_ERROR}</p>
            <button
              type="button"
              onClick={retry}
              className="mt-2 px-3 py-1.5 rounded-md text-sm font-medium bg-white text-slate-700 border border-slate-300 hover:bg-slate-50"
              data-testid="bandit-weight-history-retry"
            >
              Try again
            </button>
          </div>
        )}

        {state.kind === 'loaded' && state.ticks.length === 0 && (
          <p className="text-sm text-slate-700" data-testid="bandit-weight-history">
            {HISTORY_EMPTY}
          </p>
        )}

        {state.kind === 'loaded' && state.ticks.length > 0 && (
          <div data-testid="bandit-weight-history">
            <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="bandit-weight-history-table">
                <caption className="sr-only">{HISTORY_CAPTION}</caption>
                <thead className="bg-slate-50 border-b border-slate-200">
                  <tr>
                    <th scope="col" className="text-left px-3 py-2 text-slate-600 font-medium">
                      Updated
                    </th>
                    {experiment.variants.map((v) => (
                      <th key={v.id} scope="col" className="text-right px-3 py-2 text-slate-600 font-medium">
                        {v.name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {state.ticks.map((tick) => (
                    <tr key={tick.tick_at} data-testid="bandit-weight-history-row">
                      <th scope="row" className="text-left px-3 py-2 font-normal text-slate-800">
                        {formatWhen(tick.tick_at)}
                      </th>
                      {experiment.variants.map((v) => (
                        <td key={v.id} className="px-3 py-2 text-right tabular-nums text-slate-700">
                          {typeof tick.weights[v.id] === 'number'
                            ? formatShare(tick.weights[v.id])
                            : HISTORY_NOT_RECORDED}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-xs text-slate-600" data-testid="bandit-weight-history-count">
              {historyCount(state.ticks.length)}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
