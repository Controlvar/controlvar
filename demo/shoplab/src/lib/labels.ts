import type { ExperimentAssignment } from '@controlvar/react-sdk';

type Answer = Pick<ExperimentAssignment, 'assigned' | 'reason' | 'variantName'>;

/**
 * The panel's pill for one experiment: the variant a page assigned, "not enrolled"
 * or "completed" when the API answered control without enrolling the visitor, and
 * "not assigned yet" before any page that uses the experiment has opened.
 * The words are StreamPulse's panel's (demo/streampulse/src/lib/labels.ts).
 */
export function assignmentPill(answer: Answer | undefined): string {
  if (!answer) return 'not assigned yet';
  if (answer.assigned === false) return answer.reason === 'completed' ? 'completed' : 'not enrolled';
  return answer.variantName;
}

/**
 * The line under an experiment's key: where it is assigned, or why the visitor sees
 * control. None for a visitor a page assigned to a variant (the pill says it).
 */
export function assignmentNote(answer: Answer | undefined, page: string): string | null {
  if (!answer) return `assigned when ${page} opens`;
  if (answer.assigned !== false) return null;
  switch (answer.reason) {
    case 'holdout':
      return 'in the global holdout (control shown)';
    case 'mutual_exclusion':
      return 'excluded: mutual exclusion group (control shown)';
    case 'targeting':
      return 'excluded: targeting rules (control shown)';
    case 'completed':
      return 'experiment completed (control shown)';
    default:
      return `not enrolled: ${answer.reason ?? 'unknown'} (control shown)`;
  }
}
