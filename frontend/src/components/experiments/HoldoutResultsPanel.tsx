import React, { useEffect, useRef, useState } from 'react';
import { HoldoutsService } from '@/services/holdouts';
import { HoldoutResults, HoldoutResultsGroup } from '@/types/eligibility';
import {
  HOLDOUT_COPY,
  METRIC_MAX,
  pValueText,
  points,
  ratePercent,
  resultsErrorCopy,
  unavailableCopy,
} from '@/utils/holdouts';

/** The two arms in the API's order, under the page's own names (never the server's `label`). */
const GROUP_NAMES: Record<HoldoutResultsGroup['group'], string> = {
  in_holdout: 'In holdout',
  not_in_holdout: 'Not in holdout',
};

type ResultsState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'loaded'; results: HoldoutResults; metric: string };

/**
 * A holdout's results (beta, #1455): one event name, matched exactly, sent as
 * `query: {metric}`. The users the holdout kept out against everyone else
 * first seen while it was active. Each `unavailable_reason` reads as the
 * page's own sentence; the answer's `message` is never shown.
 */
export function HoldoutResultsPanel({ holdoutId, holdoutName }: { holdoutId: string; holdoutName: string }) {
  const [metric, setMetric] = useState('');
  const [missing, setMissing] = useState(false);
  const [state, setState] = useState<ResultsState>({ kind: 'idle' });
  const heading = useRef<HTMLHeadingElement>(null);
  const metricInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    heading.current?.focus();
  }, [holdoutId]);

  const show = async (event: React.FormEvent) => {
    event.preventDefault();
    if (metric.trim() === '') {
      setMissing(true);
      metricInput.current?.focus();
      return;
    }
    setMissing(false);
    setState({ kind: 'loading' });
    try {
      const results = await HoldoutsService.results(holdoutId, metric);
      setState({ kind: 'loaded', results, metric });
    } catch (err) {
      setState({ kind: 'error', message: resultsErrorCopy(err) });
    }
  };

  const results = state.kind === 'loaded' ? state.results : null;
  const groups = results && Array.isArray(results.groups) ? results.groups : [];
  const difference = results?.difference ?? null;

  return (
    <section
      aria-labelledby="holdout-results-heading"
      className="rounded-lg border border-slate-200 bg-white p-6 space-y-4"
      data-testid="holdout-results"
    >
      <h2
        ref={heading}
        tabIndex={-1}
        id="holdout-results-heading"
        className="text-lg font-semibold text-slate-900 focus:outline-none"
      >
        Results (beta): {holdoutName}
      </h2>
      <form onSubmit={(event) => void show(event)} noValidate className="flex flex-wrap items-end gap-2">
        <div>
          <label htmlFor="holdout-results-metric" className="block text-sm font-medium text-slate-700">
            Event name
          </label>
          <input
            ref={metricInput}
            id="holdout-results-metric"
            type="text"
            value={metric}
            maxLength={METRIC_MAX}
            aria-invalid={missing ? true : undefined}
            aria-describedby={missing ? 'holdout-results-metric-error holdout-results-metric-help' : 'holdout-results-metric-help'}
            onChange={(event) => setMetric(event.target.value)}
            className="mt-1 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900"
            data-testid="holdout-results-metric"
          />
          <p id="holdout-results-metric-help" className="mt-1 text-xs text-slate-600">
            {HOLDOUT_COPY.eventHelp}
          </p>
          {missing && (
            <p id="holdout-results-metric-error" role="alert" className="mt-1 text-xs text-red-700">
              {HOLDOUT_COPY.eventRequired}
            </p>
          )}
        </div>
        <button
          type="submit"
          disabled={state.kind === 'loading'}
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          data-testid="holdout-results-show"
        >
          Show results
        </button>
      </form>

      {state.kind === 'loading' && (
        <p role="status" className="text-sm text-slate-600" data-testid="holdout-results-loading">
          Loading…
        </p>
      )}
      {state.kind === 'error' && (
        <p role="alert" className="text-sm text-red-800" data-testid="holdout-results-error">
          {state.message}
        </p>
      )}

      {results && groups.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="holdout-results-table">
            <thead className="border-b border-slate-200">
              <tr>
                <th scope="col" className="text-left px-3 py-2 text-slate-700 font-medium">
                  Group
                </th>
                <th scope="col" className="text-left px-3 py-2 text-slate-700 font-medium">
                  Users
                </th>
                <th scope="col" className="text-left px-3 py-2 text-slate-700 font-medium">
                  Converted
                </th>
                <th scope="col" className="text-left px-3 py-2 text-slate-700 font-medium">
                  Rate
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {groups.map((g) => (
                <tr key={g.group}>
                  <th scope="row" className="text-left px-3 py-2 font-medium text-slate-800">
                    {GROUP_NAMES[g.group] ?? g.group}
                  </th>
                  <td className="px-3 py-2 tabular-nums text-slate-800">{g.users}</td>
                  <td className="px-3 py-2 tabular-nums text-slate-800">{g.conversions}</td>
                  <td className="px-3 py-2 tabular-nums text-slate-800">{ratePercent(g.conversion_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {results && results.unavailable_reason && (
        <p className="text-sm text-slate-800" data-testid="holdout-results-unavailable">
          {unavailableCopy(results.unavailable_reason, state.kind === 'loaded' ? state.metric : metric)}
        </p>
      )}

      {results && !results.unavailable_reason && difference && (
        <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1 text-sm" data-testid="holdout-results-difference">
          <dt className="text-slate-700">Difference (not in holdout minus in holdout)</dt>
          <dd className="tabular-nums text-slate-900">{points(difference.absolute)} percentage points</dd>
          <dt className="text-slate-700">95% interval</dt>
          <dd className="tabular-nums text-slate-900">
            {points(difference.ci_lower)} to {points(difference.ci_upper)} percentage points
          </dd>
          <dt className="text-slate-700">p-value</dt>
          <dd className="tabular-nums text-slate-900">{pValueText(difference.p_value)}</dd>
          <dt className="text-slate-700">Result</dt>
          <dd className="text-slate-900" data-testid="holdout-results-significance">
            {difference.is_significant ? 'Significant' : 'Not significant'}
          </dd>
        </dl>
      )}
    </section>
  );
}
