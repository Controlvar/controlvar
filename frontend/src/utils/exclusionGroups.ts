import { isApiError } from '@/services/api';
import { ExclusionGroupChanges, ExclusionGroupDetail } from '@/types/eligibility';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';

/**
 * The exclusion-group pages' fixed copy and rules (#1455; UX SCREEN 3a, the
 * architect's S4, PLAN C5). Nothing the server sends in an error is shown: each
 * failure reads as one of these sentences, chosen by its status alone.
 */
export const GROUP_COPY = {
  refused: 'Exclusion groups are managed by the ADMIN and DEVELOPER roles.',
  empty: 'No exclusion groups yet. Create one so experiments that would interfere never share a user.',
  loadFailed: 'Could not load the exclusion groups. Reload the page to try again.',
  notFound: 'This group no longer exists.',
  nameRequired: 'Enter a name.',
  nameTaken: 'A group with this name already exists.',
  shareInvalid: 'Enter a whole number from 0 to 100.',
  shareHelp:
    "Users outside this share get none of the group's experiments. Users inside it go to at most one, " +
    "divided evenly among the group's running experiments. Users already in an experiment keep it.",
  membership:
    'Adding, removing, starting or pausing a member, or changing the traffic, moves which new users ' +
    'each experiment can get; users already assigned keep their variant.',
  noCandidates: 'Every draft, running and paused experiment is already in a group.',
  inAnotherGroup: 'This experiment is already in another group. Remove it from that group first.',
  archiveAdminOnly: 'Archiving and unarchiving a group requires the ADMIN role.',
  archivedNote: 'Its experiments are no longer kept apart: new users can join more than one.',
  saveFailed: 'Could not save the group. Try again.',
} as const;

/** The status words: never a colour alone. */
export const GROUP_STATUS_LABELS: Record<string, string> = { active: 'Active', archived: 'Archived' };

export function groupStatusLabel(status: string): string {
  return GROUP_STATUS_LABELS[status] ?? status;
}

/** The API's limits (`MutualExclusionGroupCreate`). */
export const GROUP_NAME_MAX = 200;
export const GROUP_DESCRIPTION_MAX = 2000;

/** The confirmations, naming the group or the experiment. */
export function removeConfirm(experimentName: string): string {
  return `Remove “${experimentName}” from this group? New users can then be in it and in the group's other experiments.`;
}

export function archiveConfirm(groupName: string): string {
  return (
    `Archive “${groupName}”? Its experiments are no longer kept apart: new users can join more than one. ` +
    'They stay listed here, and an ADMIN can unarchive it.'
  );
}

/**
 * A stored share (a fraction, 0.0 to 1.0) as the whole percent the pages show
 * and prefill. 0.5 is 50.
 */
export function sharePercent(fraction: number): number {
  return Math.round(fraction * 100);
}

/**
 * The share field's text as the fraction the API stores, or null when it is
 * not a whole number from 0 to 100. "50" sends 0.5; "50.5", "-1", "101" and ""
 * are refused before any request.
 */
export function shareFraction(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d{1,3}$/.test(trimmed)) return null;
  const percent = Number(trimmed);
  if (percent > 100) return null;
  return percent / 100;
}

/**
 * Whether another group already has this name, compared as the API's unique
 * constraint compares it: exactly, case included. "Checkout" and "checkout"
 * are two names. `selfId` is the group being renamed, which may keep its name.
 */
export function nameTaken(name: string, groups: readonly { id: string; name: string }[], selfId?: string): boolean {
  return groups.some((group) => group.id !== selfId && group.name === name);
}

export interface GroupDraft {
  name: string;
  description: string;
  share: string;
}

export type DraftProblem = 'name-required' | 'name-taken' | 'share-invalid';

/** What stops a draft before any request, in field order. */
export function draftProblems(
  draft: GroupDraft,
  groups: readonly { id: string; name: string }[],
  selfId?: string,
): DraftProblem[] {
  const found: DraftProblem[] = [];
  if (draft.name.trim() === '') found.push('name-required');
  else if (nameTaken(draft.name, groups, selfId)) found.push('name-taken');
  if (shareFraction(draft.share) === null) found.push('share-invalid');
  return found;
}

/**
 * The body of a rename or traffic change: exactly the fields that changed.
 * The share is compared as the whole percent the form showed, so saving an
 * unchanged form never rounds a stored 0.333 to 0.33.
 */
export function groupChanges(group: ExclusionGroupDetail, draft: GroupDraft): ExclusionGroupChanges {
  const changes: ExclusionGroupChanges = {};
  if (draft.name !== group.name) changes.name = draft.name;
  if (draft.description !== (group.description ?? '')) changes.description = draft.description;
  const fraction = shareFraction(draft.share);
  if (fraction !== null && Number(draft.share.trim()) !== sharePercent(group.traffic_allocation)) {
    changes.traffic_allocation = fraction;
  }
  return changes;
}

export type GroupAction = 'list' | 'load' | 'save' | 'archive' | 'add' | 'remove';

const ACTION_COPY: Record<GroupAction, Record<number, string> & { fallback: string }> = {
  list: { 403: GROUP_COPY.refused, fallback: GROUP_COPY.loadFailed },
  load: { 403: GROUP_COPY.refused, 404: GROUP_COPY.notFound, fallback: GROUP_COPY.loadFailed },
  save: { 403: GROUP_COPY.refused, 404: GROUP_COPY.notFound, 409: GROUP_COPY.nameTaken, fallback: GROUP_COPY.saveFailed },
  archive: { 403: GROUP_COPY.archiveAdminOnly, 404: GROUP_COPY.notFound, fallback: GROUP_COPY.saveFailed },
  add: { 400: GROUP_COPY.inAnotherGroup, 403: GROUP_COPY.refused, 404: GROUP_COPY.notFound, fallback: GROUP_COPY.saveFailed },
  remove: { 403: GROUP_COPY.refused, 404: GROUP_COPY.notFound, fallback: GROUP_COPY.saveFailed },
};

/** The fixed sentence for a failed call, chosen by its status only. */
export function groupErrorCopy(action: GroupAction, err: unknown): string {
  if (!isApiError(err)) return ACTION_COPY[action].fallback;
  if (err.status === 0) return UNREACHABLE;
  return ACTION_COPY[action][err.status] ?? ACTION_COPY[action].fallback;
}

/** Whether a failure is the role refusal (403), which the pages show as the refused notice. */
export function isRefusal(err: unknown): boolean {
  return isApiError(err) && err.status === 403;
}
