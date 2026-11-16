/**
 * The interactions page's fixed copy (#1960) and the pure functions that
 * build its sentences. Nothing here shows the server's own words: not its
 * `detail`, not its `recommendations`.
 */
import { formatSignedAmount } from '@/components/results/shared/resultFormat';
import { isApiError } from '@/services/api';
import {
  InteractionArm,
  InteractionPairResponse,
  InteractionRiskLevel,
  InteractionScanResponse,
} from '@/types/interactions';

export const INTERACTIONS_COPY = {
  intro:
    'Two experiments running at the same time usually share users. That is harmless unless they interact: one ' +
    "experiment's effect is different depending on which variant of the other a user sees.",
  guideLink: 'How interactions are tested',
  listFailed: 'The experiments could not be loaded. Try again.',
  scanHeading: 'Active experiments that share users',
  scanExplanation:
    'The scan measures how many users each pair of active experiments shares. It does not test for an interaction; ' +
    'use the pair test for that.',
  scanButton: 'Scan active experiments',
  scanAgain: 'Scan again',
  scanning: 'Scanning…',
  scanCaption: 'Pairs of active experiments that share many users',
  scanUnder:
    'Overlap is the users in both divided by the users in either. A small experiment inside a large one has a low ' +
    'overlap and is not listed: test it with the pair test.',
  scanEmpty:
    'No two active experiments share many users (an overlap above 0.3). A small experiment inside a large one has a ' +
    'low overlap and is not listed: test it with the pair test.',
  scanTooFew: 'Fewer than two experiments are active, so there is no pair to scan.',
  scanFailedServer: 'The scan could not be run. Try again; if it keeps failing, check the API logs.',
  scanFailed: 'The scan could not be run.',
  pairHeading: 'Pair test',
  pairExplanation:
    "Tests whether each treatment's lift on its experiment's primary conversion metric differs across the other " +
    "experiment's variants, among the users in both. Any two experiments, in any status.",
  firstLabel: 'First experiment',
  secondLabel: 'Second experiment',
  placeholder: 'Choose an experiment',
  pairButton: 'Test the pair',
  sameTwice: 'Choose two different experiments.',
  testing: 'Testing the pair…',
  noSharedUsers: 'They share no users.',
  verdictFound: 'An interaction was found: see the rows marked Significant.',
  verdictNone:
    'No interaction was found in any row. With few users that is weak evidence: the test has little power when the ' +
    'groups are small.',
  verdictUnknown:
    'No answer: at least one row could not be tested, and no tested row is significant. Not tested never means no ' +
    'interaction.',
  rowsCaption: "Each treatment's lift across the other experiment's variants",
  allTreatments: 'All treatments',
  significantNote:
    "A significant row means that experiment's result is still a valid average over the other's current split, but " +
    'it may change once the other ships one variant: consider deciding the other first. The same pattern can also ' +
    'appear when one experiment changes who enters the other; the guide shows how to check.',
  footnote: "Lift is in percentage points. p-values are corrected across each experiment's own treatments, not across pairs.",
  pairGone: 'One of the two experiments no longer exists. Choose again.',
  pairFailedServer: 'The pair test could not be run. Try again; if it keeps failing, check the API logs.',
  pairFailed: 'The pair test could not be run.',
} as const;

/** The scan table's column headers, in order. */
export const SCAN_COLUMNS = ['First experiment', 'Second experiment', 'Overlap', 'Overlap level'] as const;

/** The pair test table's column headers, in order. */
export const ROW_COLUMNS = [
  'Experiment',
  'Treatment',
  'Metric',
  'Lift in each variant of the other experiment',
  'Result',
] as const;

/** The scan's level is the overlap's band (it measures nothing else), so the column says "level", not "risk". */
export const OVERLAP_LEVELS: Record<InteractionRiskLevel, string> = {
  low: 'Low',
  medium: 'Medium',
  high: 'High',
};

/** The API's own refusal, word for word (`interactions._require_developer`). */
const NEEDS_ROLE = 'Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role';

/** The sentence for a user the API would refuse: its refusal, naming the user's role. */
export function roleNote(role: string | null | undefined): string {
  const who = role ? role : 'signed in without a role';
  return `${NEEDS_ROLE}; you are ${who}.`;
}

/**
 * The scan's failure in fixed copy, never the server's words. A refusal is
 * the page's to show (the role note). Status 0 is the dashboard's own
 * sentence (unreachable, or timed out), passed on as it is.
 */
export function scanErrorCopy(err: unknown): string {
  if (isApiError(err)) {
    if (err.status === 0) return err.message;
    if (err.status >= 500) return INTERACTIONS_COPY.scanFailedServer;
  }
  return INTERACTIONS_COPY.scanFailed;
}

/** The pair test's failure in fixed copy, as `scanErrorCopy`; a 404 means one of the two has gone. */
export function pairErrorCopy(err: unknown): string {
  if (isApiError(err)) {
    if (err.status === 0) return err.message;
    if (err.status === 404) return INTERACTIONS_COPY.pairGone;
    if (err.status === 422) return INTERACTIONS_COPY.sameTwice;
    if (err.status >= 500) return INTERACTIONS_COPY.pairFailedServer;
  }
  return INTERACTIONS_COPY.pairFailed;
}

/** Shown when the list holds fewer experiments than there are. */
export function partialListLine(shown: number, total: number): string {
  return `The lists show ${shown} of the ${total} experiments. To test a pair that is not listed, use the API.`;
}

/** "{N} active experiments. {P} pairs share many users (an overlap above 0.3); {H} of them above 0.6." */
export function scanSummary(scan: InteractionScanResponse): string {
  const n = scan.total_active_experiments;
  const p = scan.pairs_analyzed;
  const h = scan.high_risk_pairs;
  const active = `${n} active experiment${n === 1 ? '' : 's'}.`;
  if (p === 1) {
    const high = h >= 1 ? 'it is above 0.6' : 'it is not above 0.6';
    return `${active} 1 pair shares many users (an overlap above 0.3); ${high}.`;
  }
  return `${active} ${p} pairs share many users (an overlap above 0.3); ${h} of them above 0.6.`;
}

/** A share as a whole percentage. */
function percent(share: number): string {
  return `${Math.round(share * 100)}%`;
}

/** The pair's overlap, in a sentence naming both experiments. */
export function overlapLine(pair: InteractionPairResponse, nameA: string, nameB: string): string {
  if (pair.shared_users <= 0) return INTERACTIONS_COPY.noSharedUsers;
  const users = pair.shared_users === 1 ? '1 user' : `${pair.shared_users} users`;
  return (
    `They share ${users}: ${percent(pair.share_of_a)} of ${nameA} and ${percent(pair.share_of_b)} of ${nameB} ` +
    `(overlap ${pair.overlap_coefficient.toFixed(2)}).`
  );
}

/** The verdict: true, false and null are three answers, and null is never "no interaction". */
export function verdictText(hasInteraction: boolean | null | undefined): string {
  if (hasInteraction === true) return INTERACTIONS_COPY.verdictFound;
  if (hasInteraction === false) return INTERACTIONS_COPY.verdictNone;
  return INTERACTIONS_COPY.verdictUnknown;
}

/** Why a row was not tested, in words; a code the page does not know reads "Not tested.". */
export function reasonText(reason: string, pair: Pick<InteractionPairResponse, 'min_users_per_cell' | 'min_expected_per_cell'>): string {
  switch (reason) {
    case 'mutual_exclusion_group':
      return 'Not tested: the two experiments are in the same mutual exclusion group.';
    case 'no_shared_users':
      return 'Not tested: the two experiments share no users.';
    case 'no_metric':
      return 'Not tested: this experiment has no metric.';
    case 'not_a_proportion_metric':
      return 'Not tested: its primary metric is not a conversion metric.';
    case 'no_control_variant':
      return 'Not tested: this experiment has no control variant.';
    case 'too_few_shared_users':
      return (
        "Not tested yet: too few shared users; each combination of the two experiments' variants needs at least " +
        `${pair.min_users_per_cell}.`
      );
    case 'too_few_conversions':
      return (
        'Not tested yet: too few conversions, or too few users without one, in some combination of variants; each ' +
        `needs about ${pair.min_expected_per_cell}.`
      );
    default:
      return 'Not tested.';
  }
}

/** Each variant of the other experiment and the treatment's lift there, in percentage points. */
export function liftCell(arms: InteractionArm[] | undefined): string {
  if (!arms || arms.length === 0) return '—';
  return arms
    .map((arm) =>
      typeof arm.effect === 'number' && Number.isFinite(arm.effect)
        ? `${arm.other_variant_name}: ${formatSignedAmount(arm.effect * 100)} percentage points`
        : `${arm.other_variant_name}: not measured`,
    )
    .join('; ');
}
