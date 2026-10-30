import { isApiError } from '@/services/api';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';
import { GlobalHoldout, HoldoutChanges, HoldoutDetail, HoldoutUnavailableReason } from '@/types/eligibility';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

/**
 * The global-holdout page's fixed copy and rules (#1455; UX SCREEN 3b, the
 * architect's S5, PLAN C6). Nothing the server sends is shown in its own
 * words: not an error's detail, and not a result's `message`.
 */
export const HOLDOUT_COPY = {
  scope:
    'A global holdout keeps a share of new users out of EVERY experiment, not one, so you can measure what ' +
    'all your experiments changed together. Only one holdout is active at a time: activating another ends ' +
    'this one. Feature flags ignore it. Users who already had an assignment keep it.',
  none: 'No global holdout is active.',
  refused: 'Seeing the global holdout requires the ADMIN or DEVELOPER role.',
  adminOnly: 'Creating, activating and ending holdouts requires the ADMIN role.',
  sizeHelp: 'Recommended 1-5%, up to 10% when traffic is low. It cannot change once the holdout has been active.',
  sizeFixed: 'Fixed once a holdout has been active. Create a new holdout to change it.',
  conflict: 'Another holdout was activated at the same moment. Reload the page to see which is active.',
  changed: 'This holdout has changed since the page loaded. Reload the page.',
  notFound: 'This holdout no longer exists.',
  nameRequired: 'Enter a name.',
  nameTaken: 'A holdout with this name already exists.',
  sizeInvalid: 'Enter a whole number from 1 to 20.',
  checkField: 'Check the highlighted field.',
  loadFailed: 'Could not load the global holdout. Reload the page to try again.',
  saveFailed: 'Could not save the holdout. Try again.',
  eventHelp: 'Matched exactly, for example purchase.',
  eventRequired: 'Enter an event name.',
  resultsFailed: 'Could not load the results. Try again.',
} as const;

/** The form's default size (PLAN C6): the API's own default of 10 is unchanged. */
export const DEFAULT_SIZE = 5;
export const SIZE_MIN = 1;
export const SIZE_MAX = 20;
export const NAME_MAX = 200;
export const DESCRIPTION_MAX = 2000;
export const METRIC_MAX = 255;

/** The active holdout's sentence (#1449's wording, shared with the experiment page). */
export function holdoutLabel(holdout: Pick<GlobalHoldout, 'name' | 'holdout_percentage'>): string {
  return `${holdout.name}: keeps ${holdout.holdout_percentage}% of new users out of every experiment`;
}

export type HoldoutStatus = 'not_activated' | 'active' | 'ended';

/**
 * Where a holdout is in its life, as the API decides it: active; ended once it
 * has been deactivated (it cannot restart); otherwise never activated.
 */
export function holdoutStatus(holdout: HoldoutDetail): HoldoutStatus {
  if (holdout.is_active) return 'active';
  if (holdout.deactivated_at || holdout.activated_at) return 'ended';
  return 'not_activated';
}

export const STATUS_LABELS: Record<HoldoutStatus, string> = {
  not_activated: 'Not activated',
  active: 'Active',
  ended: 'Ended',
};

/** The API's `_has_been_active`: from then on the percentage is locked (422). */
export function hasBeenActive(holdout: HoldoutDetail): boolean {
  return Boolean(holdout.is_active || holdout.activated_at || holdout.deactivated_at);
}

/** Only a holdout never activated can be: an ended one cannot restart (422). */
export function canActivate(holdout: HoldoutDetail): boolean {
  return holdoutStatus(holdout) === 'not_activated';
}

/** A stored, zone-less UTC time in the reader's own zone, or a dash. */
export function localTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = parseApiTimestamp(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

export function activateConfirm(
  target: Pick<HoldoutDetail, 'name' | 'holdout_percentage'>,
  current: Pick<HoldoutDetail, 'name'> | null,
): string {
  const tail = `From now on ${target.name} keeps ${target.holdout_percentage}% of new users out of every experiment.`;
  if (current) {
    return `Activate “${target.name}”? “${current.name}” ends now, and an ended holdout cannot be activated again. ${tail}`;
  }
  return `Activate “${target.name}”? ${tail} Its size is then fixed.`;
}

export function endConfirm(name: string): string {
  return (
    `End “${name}”? New users are no longer kept out of experiments. An ended holdout cannot be activated ` +
    'again; its results stay readable.'
  );
}

/** Whether another holdout has this name, exactly, case included, as the API's unique constraint compares. */
export function holdoutNameTaken(
  name: string,
  holdouts: readonly { id: string; name: string }[],
  selfId?: string,
): boolean {
  return holdouts.some((h) => h.id !== selfId && h.name === name);
}

/** The size field's text as the whole percent the API takes, or null unless it is 1 to 20. */
export function sizePercent(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d{1,2}$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value >= SIZE_MIN && value <= SIZE_MAX ? value : null;
}

export interface HoldoutDraft {
  name: string;
  description: string;
  size: string;
}

export type HoldoutProblem = 'name-required' | 'name-taken' | 'size-invalid';

export function holdoutDraftProblems(
  draft: HoldoutDraft,
  holdouts: readonly { id: string; name: string }[],
  selfId?: string,
  sizeLocked = false,
): HoldoutProblem[] {
  const found: HoldoutProblem[] = [];
  if (draft.name.trim() === '') found.push('name-required');
  else if (holdoutNameTaken(draft.name, holdouts, selfId)) found.push('name-taken');
  if (!sizeLocked && sizePercent(draft.size) === null) found.push('size-invalid');
  return found;
}

/** An edit's body: exactly the fields that changed; the size only while it can change. */
export function holdoutChanges(holdout: HoldoutDetail, draft: HoldoutDraft): HoldoutChanges {
  const changes: HoldoutChanges = {};
  if (draft.name !== holdout.name) changes.name = draft.name;
  if (draft.description !== (holdout.description ?? '')) changes.description = draft.description;
  const size = sizePercent(draft.size);
  if (!hasBeenActive(holdout) && size !== null && size !== holdout.holdout_percentage) {
    changes.holdout_percentage = size;
  }
  return changes;
}

/**
 * The fixed sentence for a failed call. A 409 means a name another holdout
 * has, or an activation that raced another: a request that activates reads it
 * as the race, any other as the name.
 */
export function holdoutErrorCopy(err: unknown, { activates = false }: { activates?: boolean } = {}): string {
  if (!isApiError(err)) return HOLDOUT_COPY.saveFailed;
  switch (err.status) {
    case 0:
      return UNREACHABLE;
    case 403:
      return HOLDOUT_COPY.adminOnly;
    case 404:
      return HOLDOUT_COPY.notFound;
    case 409:
      return activates ? HOLDOUT_COPY.conflict : HOLDOUT_COPY.nameTaken;
    case 422:
      return HOLDOUT_COPY.changed;
    default:
      return HOLDOUT_COPY.saveFailed;
  }
}

export function loadErrorCopy(err: unknown): string {
  if (isApiError(err) && err.status === 0) return UNREACHABLE;
  if (isApiError(err) && err.status === 404) return HOLDOUT_COPY.notFound;
  return HOLDOUT_COPY.loadFailed;
}

export function resultsErrorCopy(err: unknown): string {
  if (!isApiError(err)) return HOLDOUT_COPY.resultsFailed;
  if (err.status === 0) return UNREACHABLE;
  if (err.status === 403) return HOLDOUT_COPY.refused;
  if (err.status === 404) return HOLDOUT_COPY.notFound;
  return HOLDOUT_COPY.resultsFailed;
}

/** Each `unavailable_reason` in the page's own words (never the server's `message`). */
export function unavailableCopy(reason: HoldoutUnavailableReason | string, metric: string): string {
  switch (reason) {
    case 'activated_before_measurement':
      return 'This holdout was active before this release recorded who it kept out, so it cannot be measured. End it and create a new one.';
    case 'not_activated':
      return 'It has not been activated yet.';
    case 'too_few_users':
      return 'Each group needs 100 users; one has fewer so far.';
    case 'no_events':
      return `Neither group has sent a “${metric}” event. Check the name; it must match exactly.`;
    default:
      return HOLDOUT_COPY.resultsFailed;
  }
}

/** A rate (0 to 1) as a percent with one decimal: 0.123 is "12.3%". */
export function ratePercent(rate: number | null | undefined): string {
  return typeof rate === 'number' && Number.isFinite(rate) ? `${(rate * 100).toFixed(1)}%` : '—';
}

/** A difference of rates (0 to 1) in percentage points, signed: 0.012 is "+1.2 percentage points". */
export function points(value: number): string {
  const p = value * 100;
  const sign = p > 0 ? '+' : p < 0 ? '−' : '';
  return `${sign}${Math.abs(p).toFixed(1)}`;
}

export function pValueText(p: number | null | undefined): string {
  if (typeof p !== 'number' || !Number.isFinite(p)) return '—';
  return p < 0.001 ? '< 0.001' : p.toFixed(3);
}
