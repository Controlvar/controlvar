/**
 * What the Segments pages show when a request fails (#440).
 *
 * Fixed copy chosen by the status and by what the page was doing. The
 * server's own text (`err.message`, `detail`) is never shown: a 5xx body can
 * carry anything, and a 403's wording is not ours to vouch for. Two parts of
 * an answer are read, both data rather than prose: the targeting problems of
 * a 422 in the builder's path form ("Group 1, Condition 2: ..."), and the
 * lists of a 409 `segment_in_use`.
 */
import { isApiError, unreachableMessage } from '@/services/api';
import { describeTargetingIssue } from '@/components/experiments/new/createErrors';

export type SegmentAction =
  | 'load'
  | 'load-segment'
  | 'create'
  | 'save-rules'
  | 'archive'
  | 'members'
  | 'check'
  | 'preview'
  | 'usage';

export const ROLE_NOTE = 'Segments are created and changed by the ADMIN and DEVELOPER roles.';

const SERVER_FAILED =
  'Something went wrong on the server. Try again; if it keeps happening, your administrator can find the details in the API log.';

const FORBIDDEN: Record<SegmentAction, string> = {
  load: 'Your account cannot view segments.',
  'load-segment': 'Your account cannot view segments.',
  create: `Your role cannot create segments. ${ROLE_NOTE}`,
  'save-rules': `Your role cannot change segments. ${ROLE_NOTE}`,
  archive: `Your role cannot archive segments. ${ROLE_NOTE}`,
  members: `Your role cannot change a segment's IDs. ${ROLE_NOTE}`,
  check: 'Your account cannot check segment membership.',
  preview: 'Your account cannot estimate segment sizes.',
  usage: 'Your account cannot see where this segment is used.',
};

/** Why stored rules are not used, in one place for the list, the page and a 409 from `/evaluate`. */
export const RULES_NOT_VALID =
  "This segment's rules are not valid: they were saved before rules were checked. " +
  'Flags that use this segment answer every user with “off” (reason “error”), and experiments ' +
  'that use it enrol no new users. Replace the rules to use this segment.';

const CONFLICT: Partial<Record<SegmentAction, string>> = {
  members: "This segment's IDs cannot be changed: it is archived, or it is a rules segment.",
  check: RULES_NOT_VALID,
  archive: 'This segment cannot be archived while targeting rules use it.',
  'save-rules': 'This segment cannot be changed right now.',
};

const UNPROCESSABLE: Partial<Record<SegmentAction, string>> = {
  create: 'The segment was not accepted. The name must be 2 to 128 characters, the description at most 512, and rules need at least one group with a condition.',
  'save-rules': 'The rules were not accepted.',
  members:
    'The server refused this chunk: the segment would pass 1,000,000 IDs, or an ID is outside the limits (1 to 255 characters).',
  check: 'Enter a user ID of 1 to 255 characters.',
  preview:
    'These rules are over the preview limits (20 groups, 50 conditions, 10 regex conditions, 1,000 list values).',
};

/** The copy for a failed request while doing `action`. */
export function segmentErrorCopy(err: unknown, action: SegmentAction): string {
  if (!isApiError(err)) return 'Something went wrong in the dashboard. Reload the page and try again.';
  const { status } = err;
  // A time-out keeps the dashboard's own time-out sentence (#1040).
  if (err.timedOut) return err.message;
  if (status === 0) return unreachableMessage();
  if (status === 401) return 'Your session has ended. Sign in again.';
  if (status === 403) return FORBIDDEN[action];
  if (status === 404) return 'This segment no longer exists. It may have been removed by another user.';
  if (status === 409) return CONFLICT[action] ?? SERVER_FAILED;
  if (status === 422) return UNPROCESSABLE[action] ?? 'The request was not accepted.';
  if (status === 429) return 'Too many requests. Wait a minute and try again.';
  const id = err.requestId ? ` Request ID: ${err.requestId}.` : '';
  return `${SERVER_FAILED}${id}`;
}

/**
 * The targeting problems of a 422, in the builder's words ("Group 1,
 * Condition 2: unknown operator"), for an item whose `loc` ends in
 * `fieldName`. Only messages in the path form are kept.
 */
export function rulesProblems(err: unknown, fieldName = 'rules'): string[] {
  if (!isApiError(err) || err.status !== 422 || !Array.isArray(err.detail)) return [];
  const out: string[] = [];
  for (const item of err.detail) {
    if (!item || typeof item !== 'object') continue;
    const { loc, msg } = item as { loc?: unknown; msg?: unknown };
    if (!Array.isArray(loc) || loc[loc.length - 1] !== fieldName || typeof msg !== 'string') continue;
    const text = describeTargetingIssue(msg);
    if (/^Group \d{1,4}(, Condition \d{1,4})?: /.test(text)) out.push(text);
  }
  return out;
}

export interface SegmentInUse {
  flags: Array<{ id: string; name: string; key?: string }>;
  experiments: Array<{ id: string; name: string; key?: string; status?: string }>;
}

interface NamedItem {
  id: string;
  name: string;
  key?: string;
  status?: string;
}

function namedItems(value: unknown): NamedItem[] {
  if (!Array.isArray(value)) return [];
  const out: NamedItem[] = [];
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const text = (k: string) => (typeof rec[k] === 'string' && rec[k] ? (rec[k] as string) : undefined);
    const id = text('id');
    if (!id) continue;
    out.push({ id, name: text('name') ?? text('key') ?? id, key: text('key'), status: text('status') });
  }
  return out;
}

/** The lists of a 409 `segment_in_use`, or null for any other error. */
export function segmentInUse(err: unknown): SegmentInUse | null {
  if (!isApiError(err) || err.status !== 409 || err.code !== 'segment_in_use') return null;
  const detail = err.detail as Record<string, unknown>;
  return {
    flags: namedItems(detail.feature_flags),
    experiments: namedItems(detail.experiments),
  };
}

/** "1 flag and 2 experiments". */
export function usageCount(flags: number, experiments: number): string {
  const parts: string[] = [];
  if (flags) parts.push(`${flags} flag${flags === 1 ? '' : 's'}`);
  if (experiments) parts.push(`${experiments} experiment${experiments === 1 ? '' : 's'}`);
  return parts.join(' and ');
}
