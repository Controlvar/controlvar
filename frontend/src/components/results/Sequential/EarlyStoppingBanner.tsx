import React from 'react';
import {
  ConfidenceSequence,
  EvidenceStrength,
  MSPRTResult,
  RecommendedAction,
} from '@/types/sequential';

interface EarlyStoppingBannerProps {
  msprtResult: MSPRTResult | null;
  recommendedAction: RecommendedAction;
  /**
   * The always-valid interval on the difference in rates (treatment minus
   * control). Its sign, with `lowerIsBetter`, says whether a significant
   * change is an improvement.
   */
  confidenceSequence?: ConfidenceSequence | null;
  /**
   * Whether a lower value is the better one for the metric the analysis ran
   * on (the primary metric). Undefined when the metric could not be read, or
   * is not a conversion: the analysis then compares the share of users who
   * sent the event, and the banner does not judge direction.
   */
  lowerIsBetter?: boolean;
}

/**
 * What a significant change means for the metric.
 *
 * - `better` / `worse`: the interval lies wholly on one side of zero and the
 *   metric's direction is known.
 * - `unclear`: the interval is missing or includes zero. The test is
 *   two-sided and 0 lies outside its interval whenever it stops, so this
 *   should not happen; it is shown as neutral rather than guessed at.
 * - `unknown`: the interval is one-sided but the banner cannot judge its
 *   direction: whether higher or lower is better could not be read, or the
 *   metric is not a conversion.
 */
export type ChangeDirection = 'better' | 'worse' | 'unclear' | 'unknown';

export function changeDirection(
  confidenceSequence: ConfidenceSequence | null | undefined,
  lowerIsBetter: boolean | undefined
): ChangeDirection {
  if (!confidenceSequence) return 'unclear';
  const { lower, upper } = confidenceSequence;
  const increased = lower > 0 && upper > 0;
  const decreased = lower < 0 && upper < 0;
  if (!increased && !decreased) return 'unclear';
  if (lowerIsBetter === undefined) return 'unknown';
  return increased === !lowerIsBetter ? 'better' : 'worse';
}

const EVIDENCE_LABELS: Record<EvidenceStrength, string> = {
  strong_for_effect: 'Strong evidence for effect',
  moderate_for_effect: 'Moderate evidence for effect',
  inconclusive: 'Inconclusive',
  moderate_for_null: 'Moderate evidence for no effect',
  strong_for_null: 'Strong evidence for no effect',
};

interface BannerConfig {
  label: string;
  /** A second line that says what to do about it. */
  detail?: string;
  colorClass: string;
}

/**
 * `stop_for_effect` means the interval excludes zero, in either direction, so
 * its wording and colour follow `changeDirection`. A change for the worse is
 * stated in words as well as shown in red: nothing here may read as a reason
 * to ship it.
 */
const STOP_FOR_EFFECT_CONFIG: Record<ChangeDirection, BannerConfig> = {
  better: {
    label: 'Ready to stop — the variant is significantly better',
    colorClass: 'bg-green-50 border-green-400 text-green-800',
  },
  worse: {
    label: 'Stop — the variant is significantly worse',
    detail:
      'Do not ship it. The confidence interval lies entirely on the harmful side of no change.',
    colorClass: 'bg-red-50 border-red-500 text-red-800',
  },
  unclear: {
    label: 'Significant change detected — direction unclear',
    detail:
      'The confidence interval does not show which side of zero the change is on. Check it below before deciding.',
    colorClass: 'bg-slate-50 border-slate-400 text-slate-800',
  },
  unknown: {
    label: 'Significant change detected — direction unknown',
    detail:
      'The banner cannot say if this change is an improvement: it could not read whether a higher or a lower value is better for this metric, or the metric is not a conversion, where the test compares the share of users who sent the event, not the metric\'s value. Check the confidence interval below.',
    colorClass: 'bg-slate-50 border-slate-400 text-slate-800',
  },
};

const ACTION_CONFIG: Record<
  Exclude<RecommendedAction, 'stop_for_effect'>,
  BannerConfig
> = {
  stop_for_futility: {
    label: 'Consider stopping — unlikely to reach significance',
    colorClass: 'bg-amber-50 border-amber-400 text-amber-800',
  },
  continue: {
    label: 'Continue testing — more data needed',
    colorClass: 'bg-blue-50 border-blue-400 text-blue-800',
  },
};

export function EarlyStoppingBanner({
  msprtResult,
  recommendedAction,
  confidenceSequence,
  lowerIsBetter,
}: EarlyStoppingBannerProps) {
  let direction: ChangeDirection | null = null;
  let config: BannerConfig;
  if (recommendedAction === 'stop_for_effect') {
    direction = changeDirection(confidenceSequence, lowerIsBetter);
    config = STOP_FOR_EFFECT_CONFIG[direction];
  } else {
    config = ACTION_CONFIG[recommendedAction];
  }

  return (
    <div
      data-testid="early-stopping-banner"
      data-direction={direction ?? undefined}
      className={`rounded-lg border-l-4 p-4 ${config.colorClass}`}
      role="status"
      aria-live="polite"
    >
      <div className="flex items-center justify-between">
        <div>
          <p className="font-semibold text-sm" data-testid="action-label">
            {config.label}
          </p>
          {config.detail && (
            <p className="text-xs mt-1" data-testid="action-detail">
              {config.detail}
            </p>
          )}
          {msprtResult && (
            <p className="text-xs mt-1 opacity-80" data-testid="evidence-detail">
              {EVIDENCE_LABELS[msprtResult.evidence_strength]} — p-value:{' '}
              {msprtResult.always_valid_p_value.toFixed(4)}
            </p>
          )}
        </div>
        {msprtResult && (
          <div className="text-right" data-testid="lambda-display">
            <span className="text-2xl font-bold">
              {msprtResult.lambda_ratio.toFixed(2)}
            </span>
            <span className="block text-xs opacity-70">
              Lambda ratio (boundary: {msprtResult.boundary.toFixed(1)})
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
