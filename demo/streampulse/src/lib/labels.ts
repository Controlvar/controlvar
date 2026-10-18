import type { ExperimentAssignment, FeatureFlagEvaluation } from '@controlvar/react-sdk';
import { HOLDOUT_NAME, PROFILE_MEG_NAME } from '@/lib/env';

/**
 * Human-readable explanation of a flag evaluation, built from the server's
 * `reason` (`targeting_rule` | `rollout` | `inactive` | `error`) and whether the
 * flag came back on or off.
 */
export function flagReasonLabel(flag: Pick<FeatureFlagEvaluation, 'isEnabled' | 'reason' | 'loading' | 'error'>): string {
  if (flag.loading) return 'evaluating…';
  if (flag.error) return `error: ${flag.error.message}`;
  switch (flag.reason) {
    case 'targeting_rule':
      return flag.isEnabled ? 'on: matched a targeting rule' : 'off: matched a rule but outside its rollout %';
    case 'rollout':
      return flag.isEnabled ? 'on: inside the rollout %' : 'off: outside the rollout %';
    case 'inactive':
      return 'off: flag is inactive';
    case 'error':
      return 'off: server evaluation error';
    case undefined:
      return flag.isEnabled ? 'on' : 'off';
    default:
      return `${flag.isEnabled ? 'on' : 'off'}: ${flag.reason}`;
  }
}

/**
 * Human-readable enrolment status for an experiment assignment. Ineligible
 * users (`assigned: false`) see the control variant; `reason` says why.
 */
export function assignmentReasonLabel(
  a: Pick<ExperimentAssignment, 'assigned' | 'reason' | 'loading' | 'error' | 'variantName'>,
): string {
  if (a.loading) return 'assigning…';
  if (a.error) return `error: ${a.error.message}`;
  if (a.assigned === false) {
    switch (a.reason) {
      case 'holdout':
        return `in global holdout ${HOLDOUT_NAME} (control shown)`;
      case 'mutual_exclusion':
        return `excluded: mutual exclusion group ${PROFILE_MEG_NAME} (control shown)`;
      case 'targeting':
        return 'excluded: targeting rules (control shown)';
      default:
        return `not enrolled: ${a.reason ?? 'unknown'} (control shown)`;
    }
  }
  return `assigned to ${a.variantName}`;
}

/** Short pill text for an experiment row. */
export function assignmentPill(a: Pick<ExperimentAssignment, 'assigned' | 'loading' | 'error' | 'variantName'>): string {
  if (a.loading) return 'loading…';
  if (a.error) return 'error';
  if (a.assigned === false) return 'not enrolled';
  return a.variantName;
}
