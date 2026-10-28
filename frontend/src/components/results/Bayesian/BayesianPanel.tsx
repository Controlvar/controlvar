import React from 'react';
import { BayesianDecision, BayesianResultsResponse } from '@/types/results';
import { isFiniteNumber, NOT_ENOUGH_DATA } from '@/components/results/shared/resultFormat';
import { SRM_QUALIFIER } from '@/components/results/ResultsDashboard/SrmNotice';

interface BayesianPanelProps {
  bayesian: BayesianResultsResponse | null | undefined;
  /**
   * The experiment's own setting, when it could be read. With the setting on
   * and no Bayesian block, the panel says the analysis could not be computed
   * rather than showing nothing.
   */
  bayesianEnabled?: boolean;
  /** The server's sample-ratio warning: the Bayesian numbers share the split. */
  srmWarning?: boolean;
  /**
   * What the analysis compares, when that is not the primary metric's own
   * value: for a revenue primary, the share of users who sent its event.
   * Shown under the heading.
   */
  note?: React.ReactNode;
}

/** Fixed copy for each decision the API can return; nothing else is shown. */
export const BAYESIAN_DECISION_LABELS: Record<BayesianDecision, string> = {
  CONTINUE: 'Continue',
  STOP_WINNER: 'Stop: a winner is clear',
  STOP_EQUIVALENT: 'Stop: the variants are equivalent',
  STOP_FUTILE: 'Stop: a meaningful difference is unlikely',
};

export const BAYESIAN_UNAVAILABLE =
  'Bayesian analysis is on for this experiment, but it could not be computed for these results.';
export const BAYESIAN_NO_DATA = 'The Bayesian analysis has no data yet.';

function decisionLabel(decision: BayesianDecision | null | undefined): string {
  if (decision && Object.prototype.hasOwnProperty.call(BAYESIAN_DECISION_LABELS, decision)) {
    return BAYESIAN_DECISION_LABELS[decision];
  }
  return 'No decision';
}

/** A probability: "> 99.9%" and "< 0.1%" at the ends, so a Monte Carlo 1 or 0 is not overstated. */
export function formatProbability(p: number | null | undefined): string {
  if (!isFiniteNumber(p)) return NOT_ENOUGH_DATA;
  if (p >= 0.999) return '> 99.9%';
  if (p <= 0.001) return '< 0.1%';
  return `${(p * 100).toFixed(1)}%`;
}

/** A rate (posterior mean or interval bound) as a percentage. */
function formatRate(v: number | null | undefined): string {
  return isFiniteNumber(v) ? `${(v * 100).toFixed(1)}%` : NOT_ENOUGH_DATA;
}

/** Expected loss, in the rate's own units. */
export function formatLoss(v: number | null | undefined): string {
  if (!isFiniteNumber(v)) return NOT_ENOUGH_DATA;
  if (v === 0) return '0';
  if (Math.abs(v) < 0.0001) return '< 0.0001';
  return String(Number(v.toPrecision(2)));
}

function formatInterval(lower: number | undefined, upper: number | undefined): string {
  if (!isFiniteNumber(lower) || !isFiniteNumber(upper)) return NOT_ENOUGH_DATA;
  return `${formatRate(lower)} – ${formatRate(upper)}`;
}

/**
 * The Bayesian analysis of the primary metric, at the end of the Overview tab.
 *
 * - `bayesian.is_enabled`: the decision and a row per variant.
 * - enabled with no variant rows: says there is no data yet.
 * - no block (or not enabled) while the experiment has Bayesian on: a muted
 *   line saying it could not be computed.
 * - neither: nothing.
 */
export function BayesianPanel({
  bayesian,
  bayesianEnabled,
  srmWarning,
  note,
}: BayesianPanelProps) {
  if (!bayesian?.is_enabled) {
    if (bayesianEnabled === true) {
      return (
        <p className="text-sm text-slate-500" data-testid="bayesian-unavailable">
          {BAYESIAN_UNAVAILABLE}
        </p>
      );
    }
    return null;
  }

  const rows = bayesian.variant_results ?? [];
  return (
    <section aria-labelledby="bayesian-heading" data-testid="bayesian-panel" className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h3 id="bayesian-heading" className="text-base font-semibold text-slate-800">
          Bayesian analysis (primary metric)
        </h3>
        <span
          className="inline-flex px-2.5 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-800"
          data-testid="bayesian-decision"
        >
          Decision: {decisionLabel(bayesian.decision)}
        </span>
      </div>
      {note && (
        <p className="text-sm text-slate-600" data-testid="bayesian-share-note">
          {note}
        </p>
      )}
      {srmWarning && (
        <p className="text-sm font-medium text-amber-900" data-testid="bayesian-srm-qualifier">
          <span aria-hidden="true">⚠ </span>
          {SRM_QUALIFIER}
        </p>
      )}
      {rows.length === 0 ? (
        <p className="text-sm text-slate-500" data-testid="bayesian-no-data">
          {BAYESIAN_NO_DATA}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm" data-testid="bayesian-table">
            <thead>
              <tr className="text-left text-slate-600 border-b border-slate-200">
                <th scope="col" className="py-2 pr-4 font-medium">Variant</th>
                <th scope="col" className="py-2 pr-4 font-medium">Chance to be best</th>
                <th scope="col" className="py-2 pr-4 font-medium">Expected loss</th>
                <th scope="col" className="py-2 pr-4 font-medium">Posterior mean</th>
                <th scope="col" className="py-2 pr-4 font-medium">Credible interval</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr
                  key={`${r.variant_key}-${i}`}
                  className="border-b border-slate-100"
                  data-testid="bayesian-row"
                >
                  <th scope="row" className="py-2 pr-4 font-medium text-slate-800 text-left">
                    {r.variant_key}
                  </th>
                  <td className="py-2 pr-4" data-testid="bayesian-prob-best">
                    {formatProbability(r.probability_to_be_best)}
                  </td>
                  <td className="py-2 pr-4" data-testid="bayesian-expected-loss">
                    {formatLoss(r.expected_loss)}
                  </td>
                  <td className="py-2 pr-4" data-testid="bayesian-mean">
                    {formatRate(r.posterior?.mean)}
                  </td>
                  <td className="py-2 pr-4" data-testid="bayesian-interval">
                    {formatInterval(
                      r.posterior?.credible_interval_lower,
                      r.posterior?.credible_interval_upper
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {isFiniteNumber(bayesian.n_samples) && (
        <p className="text-xs text-slate-500" data-testid="bayesian-provenance">
          Estimated from {bayesian.n_samples.toLocaleString('en-US')} simulated draws per variant
          {bayesian.engine_version ? `, statistics engine ${bayesian.engine_version}` : ''}. This
          is a second analysis of the primary metric; it does not change the recommendation above.
        </p>
      )}
    </section>
  );
}
