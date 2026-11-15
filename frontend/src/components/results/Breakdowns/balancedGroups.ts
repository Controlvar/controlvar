/**
 * The balanced groups on the results' Breakdowns tab (beta, #448): what the
 * option and the tables are called, the notes and when each shows, for an
 * experiment that balances its split within groups.
 *
 * Pure TypeScript with no React: BalancedGroupsView renders these, and every
 * sentence it says is built here.
 */
import { BalanceWithin } from '@/types/experiments';
import {
  BalancedGroupsNoEstimateReason,
  DimensionalBreakdownResponse,
  MergedGroup,
} from '@/types/results';
import { BREAKDOWN_OPTIONS } from '@/components/results/Breakdowns/BreakdownSelector';

/** The majority-unknown note shows only from this many users. */
export const UNKNOWN_NOTE_MIN_USERS = 100;

export const OVERALL_CAPTION = 'All groups, weighted by size';
export const DECISION_UNCHANGED = "The experiment's own results and their decision do not change.";
export const UNKNOWN_NOTE_LINK = 'How a user gets a group';
export const OUTSIDE_NOTE_LINK = 'Why users are split without balancing';

const OTHER_GROUP = 'other';
const UNKNOWN_GROUP = 'unknown';

/** True when the experiment balances its split within an attribute or saved segments. */
export function balances(setting: BalanceWithin | null | undefined): setting is BalanceWithin {
  if (!setting || typeof setting !== 'object') return false;
  if (Array.isArray(setting.segments)) return true;
  return typeof setting.attribute === 'string' && setting.attribute !== '';
}

function bySegments(setting: BalanceWithin): boolean {
  return Array.isArray(setting.segments);
}

/** What the groups are of: the selector's label for a key it offers ("Platform"), the key itself, or "saved segments". */
export function groupsName(setting: BalanceWithin): string {
  if (bySegments(setting)) return 'saved segments';
  const key = setting.attribute ?? '';
  return BREAKDOWN_OPTIONS.find((o) => o.value === key)?.label ?? key;
}

/** The selector's first option: "Platform (balanced)", "tier (balanced)", "Saved segments (balanced)". */
export function balancedOptionLabel(setting: BalanceWithin): string {
  return bySegments(setting) ? 'Saved segments (balanced)' : `${groupsName(setting)} (balanced)`;
}

/** The groups table's caption. */
export function groupsCaption(setting: BalanceWithin): string {
  return `Results by balanced group: ${groupsName(setting)}`;
}

/**
 * How a group is shown. A saved segment's group is stored as its id, which is
 * never shown: its name, "saved segment 2" (its place in the list) while the
 * names load or if they cannot be, and "an unknown segment" for one the list
 * does not hold. `other` and `unknown`, and an attribute's values, are as stored.
 */
export function groupLabel(
  value: string,
  setting: BalanceWithin,
  names: ReadonlyMap<string, string> | null,
): string {
  if (!bySegments(setting) || value === OTHER_GROUP || value === UNKNOWN_GROUP) return value;
  const name = names?.get(value);
  if (name) return name;
  const place = (setting.segments ?? []).indexOf(value);
  if (names === null && place >= 0) return `saved segment ${place + 1}`;
  return 'an unknown segment';
}

/**
 * Always shown: a user is in the group stored when they were assigned, which
 * a breakdown by the same key need not agree with. The Breakdowns tab offers
 * only some keys, so another key's breakdown is named without its path.
 */
export function countingNote(setting: BalanceWithin): string {
  const first = 'Each user is counted in the group they were balanced in when they were assigned.';
  if (bySegments(setting)) return first;
  const key = setting.attribute ?? '';
  const breakdown = BREAKDOWN_OPTIONS.some((o) => o.value === key)
    ? `Breakdowns › ${key}`
    : `A breakdown by ${key}`;
  return (
    `${first} ${breakdown} counts users by the first value they were seen with, ` +
    `so the two can differ for users assigned from a list or without ${key}.`
  );
}

/** Every user the rows count: per variant, they add up to the experiment's results. */
export function usersIn(groups: DimensionalBreakdownResponse): number {
  return groups.segments.reduce((sum, segment) => sum + segment.sample_size, 0);
}

function isShare(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/**
 * How many users had their group stored when they were assigned: the base of
 * the API's `unknown_share`, which the response does not give as a number.
 *
 * A user with no stored group (an older server version's, or one assigned by a
 * path that does not balance) is counted in the `unknown` row, and has no
 * place in the blocks either (a place is stored only with a group), so is also
 * in `users_outside_blocks`; every other user is in the row of their stored
 * group. (`users_outside_blocks` also counts users who kept their group but
 * got no place: the formula does not use it except as a bound.) So with N users, U of them in `unknown` and a share
 * P < 1 of the stored users in `unknown`, N − U = S × (1 − P). With no user
 * outside the blocks, S = N. With P = 1 and some users outside the blocks, S
 * is only known to lie between N − users_outside_blocks and N: null.
 */
export function storedUsers(
  groups: DimensionalBreakdownResponse,
  unknownShare: number | null,
  outside: number,
): number | null {
  const users = usersIn(groups);
  if (!(outside > 0)) return users;
  if (!isShare(unknownShare) || unknownShare >= 1) return null;
  const inUnknown = groups.segments.find((s) => s.segment_value === UNKNOWN_GROUP)?.sample_size ?? 0;
  const stored = Math.round((users - inUnknown) / (1 - unknownShare));
  // The server reads the rows and the stored groups in two statements, so a
  // user assigned in between can put the result a little outside what the
  // counts allow; it is kept within them.
  return Math.min(users, Math.max(users - outside, stored));
}

/**
 * A share above a half, to a whole percent. Rounded, a share just over a half
 * would read 50% (not most) and one just under all 100% (some users did send
 * it), so those say "over 50%" and "over 99%".
 */
function sharePercent(share: number): string {
  const rounded = Math.round(share * 100);
  if (rounded <= 50) return 'over 50%';
  if (share < 1 && (share >= 0.995 || rounded >= 100)) return 'over 99%';
  return `${rounded}%`;
}

/**
 * When more than half of the users whose group was stored are in `unknown`
 * (`unknownShare`, the API's, leaves out users with no stored group), and
 * there are UNKNOWN_NOTE_MIN_USERS of those users or more. The sentence gives
 * the share of those users and how many they are (`storedUsers`); when their
 * number is not known exactly it is left out, and the note shows only when at
 * least that many users certainly had their group stored. Null otherwise.
 */
export function unknownNote(
  setting: BalanceWithin,
  unknownShare: number | null,
  groups: DimensionalBreakdownResponse,
  outside: number,
): string | null {
  if (!isShare(unknownShare) || !(unknownShare > 0.5)) return null;
  const stored = storedUsers(groups, unknownShare, outside);
  const atLeast = stored ?? usersIn(groups) - outside;
  if (atLeast < UNKNOWN_NOTE_MIN_USERS) return null;
  const share = sharePercent(unknownShare);
  const most =
    stored === null ? `Most users (${share})` : `Most users (${share} of ${stored.toLocaleString()})`;
  if (bySegments(setting)) {
    return (
      `${most} were balanced together as unknown, because whether they belonged to a listed segment ` +
      `could not be decided when they were assigned. Send the attributes the segments' rules use when you assign them.`
    );
  }
  const key = setting.attribute ?? '';
  return (
    `${most} were assigned without ${key}, so they are balanced together as unknown. ` +
    `Send ${key} in the user's attributes when you assign them.`
  );
}

/** When any user was split without balancing (`users_outside_blocks`), how many; null otherwise. */
export function outsideNote(outside: number, users: number): string | null {
  if (!(outside > 0)) return null;
  return `${outside.toLocaleString()} of ${users.toLocaleString()} users ${outside === 1 ? 'was' : 'were'} split without balancing.`;
}

/** In place of the row over all the groups, when the API gives no estimate. */
export function noEstimateText(reason: BalancedGroupsNoEstimateReason | null): string {
  switch (reason) {
    case 'mean_metric':
      return `${OVERALL_CAPTION}: no estimate, because the primary metric is not a conversion.`;
    case 'sequential_testing':
      return `${OVERALL_CAPTION}: no estimate, because the experiment uses sequential testing.`;
    case 'too_few_users':
      return `${OVERALL_CAPTION}: no estimate yet, because too few users have been assigned.`;
    default:
      return `${OVERALL_CAPTION}: no estimate.`;
  }
}

/** The groups the estimate counted with a larger one, one sentence each; null when none. */
export function mergedText(merged: MergedGroup[], label: (value: string) => string): string | null {
  if (merged.length === 0) return null;
  return merged
    .map(
      (m) =>
        `${label(m.group)} has too few users for an estimate of its own, so this estimate counts it with ${label(m.into)}.`,
    )
    .join(' ');
}
