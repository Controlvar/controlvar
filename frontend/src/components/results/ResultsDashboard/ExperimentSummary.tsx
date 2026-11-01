import React from 'react';
import {
  CorrectionMethod,
  ExperimentResultsResponse,
  RecommendationAction,
  SampleSizeResult,
} from '@/types/results';
import { WinnerIndicator } from '@/components/results/shared/WinnerIndicator';
import { SRM_QUALIFIER } from './SrmNotice';
import { pct } from './SampleSizeMeter';
import {
  comparisonsOf,
  correctionName,
  describeSettings,
  formatConfidence,
  mostComparisons,
} from '@/components/results/shared/analysisSettings';
import {
  ImpactLine,
  NOT_ENOUGH_DATA,
  carriesImpact,
  formatImpactLine,
  impactLine,
  showsAdjusted,
} from '@/components/results/shared/resultFormat';
import { LeadWithInterval } from '@/components/results/shared/IntervalText';

interface ExperimentSummaryProps {
  experiment: ExperimentResultsResponse;
  /**
   * The experiment's stored settings, when they could be read. Only used to
   * say so when the results were computed with other ones.
   */
  stored?: { correction_method: CorrectionMethod; confidence_level: number } | null;
  /** Opens the Sample Size tab; the link to it is shown only when given. */
  onOpenSampleSize?: () => void;
  /**
   * The Sample Size tab's plan with the server's own inputs (none of the
   * user's changes on the tab). The Planned sample card reads it. Undefined
   * while it loads, null when it could not be loaded.
   */
  plannedSample?: SampleSizeResult | null;
}

/**
 * The effect the Planned sample is planned for, and the baseline it starts
 * from (#1257): the effect saved with the experiment, or the 5% default when
 * none was. An API older than 0.34.3 sends no mde_source and planned for the
 * default. null while the plan loads or when it could not be loaded.
 */
export function plannedEffectSentence(
  plannedSample: SampleSizeResult | null | undefined
): string | null {
  if (!plannedSample) return null;
  const source = plannedSample.mde_source ?? 'default';
  const effect = pct(plannedSample.mde);
  const baseline =
    plannedSample.required_sample_size_per_variant !== null &&
    plannedSample.baseline_rate !== null
      ? plannedSample.baseline_source === 'observed'
        ? ` on the control's ${pct(plannedSample.baseline_rate)}`
        : ` on a ${pct(plannedSample.baseline_rate)} baseline`
      : '';
  if (source === 'experiment') {
    return `To detect a ${effect} lift${baseline}. The ${effect} was set when the experiment was created.`;
  }
  if (source === 'request') {
    return `To detect a ${effect} lift${baseline}.`;
  }
  return `To detect a ${effect} lift (the default: no effect was saved with this experiment)${baseline}.`;
}

/**
 * The Planned sample card: Reached only when the smallest variant has the
 * planned sample per variant (#1244). It never reads Reached without a plan,
 * so at no events, or while the plan loads, it cannot say so.
 */
export function plannedSampleStatus(plannedSample: SampleSizeResult | null | undefined): {
  value: string;
  detail: string | null;
  valueClass: string;
} {
  if (plannedSample === undefined) {
    return { value: 'Loading', detail: null, valueClass: 'text-slate-600' };
  }
  if (plannedSample === null) {
    return { value: 'Not known', detail: null, valueClass: 'text-slate-600' };
  }
  const current = plannedSample.current_sample_size_per_variant;
  const required = plannedSample.required_sample_size_per_variant;
  if (required === null) {
    return {
      value: 'Not yet',
      detail: `${current.toLocaleString()} per variant, no plan yet`,
      valueClass: 'text-amber-700',
    };
  }
  const detail = `${current.toLocaleString()} of ${required.toLocaleString()} per variant`;
  return plannedSample.is_adequate && current >= required
    ? { value: 'Reached', detail, valueClass: 'text-green-700' }
    : { value: 'Not yet', detail, valueClass: 'text-amber-700' };
}

const RECOMMENDATION_CONFIG: Record<
  RecommendationAction,
  { label: string; className: string }
> = {
  SHIP_VARIANT: {
    label: 'Ship Variant ✓',
    className: 'bg-green-100 text-green-800',
  },
  KEEP_CONTROL: {
    label: 'Keep Control',
    className: 'bg-blue-100 text-blue-800',
  },
  CONTINUE_TESTING: {
    label: 'Continue Testing',
    className: 'bg-amber-100 text-amber-800',
  },
  INCONCLUSIVE: {
    label: 'Inconclusive',
    className: 'bg-gray-100 text-gray-700',
  },
};

function StatusBadge({ status }: { status: string }) {
  const colorMap: Record<string, string> = {
    ACTIVE: 'bg-green-100 text-green-800',
    COMPLETED: 'bg-blue-100 text-blue-800',
    PAUSED: 'bg-amber-100 text-amber-800',
    DRAFT: 'bg-gray-100 text-gray-700',
  };
  const cls = colorMap[status] ?? 'bg-gray-100 text-gray-700';
  return (
    <span className={`inline-flex px-2.5 py-0.5 rounded-full text-xs font-medium ${cls}`}>
      {status}
    </span>
  );
}

/**
 * The Analysis row: the confidence level and correction the results were
 * computed with, and how many comparisons the correction covered. "k" is the
 * most treatments with a p-value on any one metric.
 */
export function analysisSentence(experiment: ExperimentResultsResponse): string {
  const level = formatConfidence(experiment.confidence_level);
  const method = experiment.correction_method;
  const k = mostComparisons(experiment.metrics);
  if (k === 1) {
    return `${level} confidence · one comparison with the control on each metric, so no correction is needed`;
  }
  if (method === 'none') {
    return k >= 2
      ? `${level} confidence · no correction (chosen for this experiment)`
      : `${level} confidence · no correction`;
  }
  return k >= 2
    ? `${level} confidence · ${correctionName(method)} correction for the ${k} comparisons with the control on each metric`
    : `${level} confidence · ${correctionName(method)} correction`;
}

/** One treatment's line under "Impact on {metric}:". */
export interface SummaryImpactRow {
  variantId: string;
  variantName: string;
  /** Null when there is no impact to show: "Not enough data to estimate". */
  line: ImpactLine | null;
  significant: boolean;
}

/**
 * The summary card's impact (#1496): the primary metric (the one marked
 * primary, else the first, as the chart picks it) and one row per treatment.
 * Null when the server does not report the impact, so an older server's
 * card is unchanged. Each interval is marked "unadjusted" exactly when that
 * treatment's row in the table is.
 */
export function summaryImpact(
  experiment: ExperimentResultsResponse,
): { metricName: string; rows: SummaryImpactRow[] } | null {
  const metric = experiment.metrics.find((m) => m.is_primary) ?? experiment.metrics[0];
  if (!metric || !metric.variants.some(carriesImpact)) return null;
  const k = comparisonsOf(metric);
  const rows = metric.variants
    .filter((v) => !v.is_control)
    .map((v) => ({
      variantId: v.variant_id,
      variantName: v.variant_name,
      line: impactLine(
        metric,
        v,
        experiment.confidence_level,
        showsAdjusted(v, k, experiment.correction_method),
      ),
      significant: v.is_significant,
    }));
  return rows.length > 0 ? { metricName: metric.metric_name, rows } : null;
}

/**
 * A row's text: "One page: +124 converting users (95% CI +40 to +208)",
 * with ", not significant" when the treatment is not.
 */
export function summaryImpactText(row: SummaryImpactRow): string {
  if (!row.line) return `${row.variantName}: ${NOT_ENOUGH_DATA}`;
  return `${row.variantName}: ${formatImpactLine(row.line)}${row.significant ? '' : ', not significant'}`;
}

export function ExperimentSummary({
  experiment,
  stored,
  onOpenSampleSize,
  plannedSample,
}: ExperimentSummaryProps) {
  const { summary } = experiment;
  const planned = plannedSampleStatus(plannedSample);
  const plannedEffect = plannedEffectSentence(plannedSample);
  const rec = RECOMMENDATION_CONFIG[summary.recommendation];
  const impact = summaryImpact(experiment);

  // Find the winning variant name if there is one
  let winnerName = '';
  if (summary.has_winner && summary.winning_variant_id) {
    for (const metric of experiment.metrics) {
      const v = metric.variants.find(
        (vr) => vr.variant_id === summary.winning_variant_id
      );
      if (v) {
        winnerName = v.variant_name;
        break;
      }
    }
  }

  const isWinner =
    summary.recommendation === 'SHIP_VARIANT' && summary.has_winner && winnerName !== '';
  const isLeading = !isWinner && summary.has_winner && winnerName !== '';

  return (
    <div
      className="bg-white rounded-xl border border-slate-200 p-6 space-y-4"
      data-testid="experiment-summary"
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-slate-900">
            {experiment.experiment_name}
          </h2>
          <div className="mt-1 flex items-center gap-2">
            <StatusBadge status={experiment.status} />
            <span className="text-sm text-slate-500">
              {summary.duration_days} day{summary.duration_days !== 1 ? 's' : ''}
            </span>
          </div>
        </div>
        {/* Recommendation pill */}
        <span
          className={`inline-flex px-3 py-1.5 rounded-lg text-sm font-semibold ${rec.className}`}
          data-testid="recommendation-pill"
        >
          {rec.label}
        </span>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <Stat label="Total Users" value={summary.total_users.toLocaleString()} />
        <Stat label="Total Events" value={summary.total_events.toLocaleString()} />
        <Stat label="Duration" value={`${summary.duration_days}d`} />
        {/* The planned sample per variant, as on the Sample Size tab (#1244).
            The smaller floor the recommendation needs is in its reason. */}
        <Stat
          label="Planned sample"
          value={planned.value}
          valueClass={planned.valueClass}
          detail={planned.detail}
          testId="planned-sample"
        />
      </div>

      {/* Which effect the Planned sample is for (#1257), full width: the
          card's cell is a quarter of the row. */}
      {plannedEffect && (
        <p className="text-sm text-slate-700" data-testid="planned-sample-effect">
          {plannedEffect}
        </p>
      )}

      <div className="text-sm text-slate-700" data-testid="analysis-summary">
        <p>
          <span className="font-medium text-slate-900">Analysis:</span>{' '}
          <span data-testid="analysis-summary-text">{analysisSentence(experiment)}</span>
        </p>
        {stored &&
          (stored.correction_method !== experiment.correction_method ||
            Math.abs(stored.confidence_level - experiment.confidence_level) > 1e-9) && (
            <p data-testid="analysis-summary-override">
              Shown with{' '}
              {describeSettings(experiment.confidence_level, experiment.correction_method)};
              this experiment is set to{' '}
              {describeSettings(stored.confidence_level, stored.correction_method)}.
            </p>
          )}
      </div>

      {onOpenSampleSize && (
        <button
          type="button"
          onClick={onOpenSampleSize}
          className="text-sm text-blue-700 underline hover:text-blue-800"
          data-testid="open-sample-size-tab"
        >
          See the Sample Size tab for how the plan is calculated, or to try another effect.
        </button>
      )}

      {/* Why: the engine's own sentence, so the pill is never unexplained. */}
      {summary.recommendation_reason && (
        <p className="text-sm text-slate-700" data-testid="recommendation-reason">
          {summary.recommendation_reason}
        </p>
      )}

      {/* What the primary metric's change amounts to over each treatment's
          own users (#1496). Plain paragraphs: no live region, no tooltip. */}
      {impact && (
        <div className="text-sm text-slate-700" data-testid="summary-impact">
          <p className="font-medium text-slate-900">Impact on {impact.metricName}:</p>
          {impact.rows.map((row) => (
            <p key={row.variantId} data-testid="summary-impact-line">
              {`${row.variantName}: `}
              {row.line ? (
                <>
                  <LeadWithInterval
                    lead={`${row.line.value} ${row.line.noun}`}
                    interval={row.line.interval}
                  />
                  {row.significant ? '' : ', not significant'}
                </>
              ) : (
                NOT_ENOUGH_DATA
              )}
            </p>
          ))}
          {/* The server's verdict only: a null check is not a mismatch. */}
          {experiment.srm?.warning === true && (
            <p className="mt-1 font-medium text-amber-900" data-testid="summary-impact-srm">
              <span aria-hidden="true">⚠ </span>
              {SRM_QUALIFIER}
            </p>
          )}
        </div>
      )}

      {/* Winner only when the recommendation is to ship it. A significant
          variant under any other recommendation is "leading", not a winner. */}
      {isWinner && (
        <div className="pt-2">
          <p className="text-sm text-slate-600 mb-1">Winner</p>
          <WinnerIndicator variantName={winnerName} show />
        </div>
      )}
      {isLeading && (
        <p className="pt-2 text-sm font-medium text-slate-700" data-testid="leading-variant">
          Leading: {winnerName}
          {experiment.sample_size_adequate ? '' : ' (not yet adequate sample)'}
          {/* The server's verdict only: a null check is not a mismatch. */}
          {experiment.srm?.warning === true && (
            <span
              className="block mt-1 font-medium text-amber-900"
              data-testid="leading-srm-qualifier"
            >
              <span aria-hidden="true">⚠ </span>
              {SRM_QUALIFIER}
            </span>
          )}
        </p>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  valueClass = 'text-slate-800',
  detail,
  testId,
}: {
  label: string;
  value: string;
  valueClass?: string;
  detail?: string | null;
  testId?: string;
}) {
  return (
    <div data-testid={testId}>
      <p className="text-xs text-slate-500 uppercase tracking-wide">{label}</p>
      <p className={`text-lg font-semibold ${valueClass}`}>{value}</p>
      {detail && <p className="text-xs text-slate-600">{detail}</p>}
    </div>
  );
}
