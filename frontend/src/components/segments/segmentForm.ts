import { checkTargeting } from '@/components/experiments/new/formState';
import type { AudiencePreview, SegmentKind } from '@/services/segments';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
import { formatCount } from '@/utils/segmentIds';
import type { TargetingRules } from '@/types/targeting';

/** What a segment's rule builder says with no groups. */
export const SEGMENT_RULES_EMPTY = 'No conditions yet. A segment needs at least one condition.';

/** Problems found before a segment is sent, in the order the form shows them. */
export function segmentFormProblems(name: string, kind: SegmentKind, rules: TargetingRules): string[] {
  const problems: string[] = [];
  const trimmed = name.trim();
  if (trimmed.length < 2 || trimmed.length > 128) problems.push('Name: 2 to 128 characters.');
  if (kind === 'rules') problems.push(...segmentRulesProblems(rules));
  return problems;
}

/** Problems with a segment's rules found before they are sent. */
export function segmentRulesProblems(rules: TargetingRules): string[] {
  if (rules.groups.length === 0) return [SEGMENT_RULES_EMPTY];
  return checkTargeting(rules);
}

/** What the preview says when no user seen recently sent attributes. */
export const PREVIEW_NO_ATTRIBUTES =
  "No users have attributes yet, so the size can't be estimated. Send the user's attributes as context " +
  'when you call assign, and users who are assigned from then on will count. The segment still works: it ' +
  "is matched against each request's attributes.";

const SINCE = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  timeZone: 'UTC',
  timeZoneName: 'short',
});

/** `sampled_since` as the preview shows it, in UTC: "Nov 6, 9:12 AM UTC". */
export function previewSince(sampledSince: string): string {
  const when = parseApiTimestamp(sampledSince);
  return Number.isNaN(when.getTime()) ? sampledSince : SINCE.format(when);
}

/**
 * The copy for a preview answer. The sample is the users seen most recently,
 * so the text says so, with the time of the oldest one read; only a read that
 * reached the start of the window says "in the last 30 days". No percentage is
 * shown when the answer has none (`estimated_percentage: null`).
 */
export function previewText(preview: AudiencePreview): string {
  const n = preview.sample_size;
  if (n === 0) return PREVIEW_NO_ATTRIBUTES;
  if (preview.estimated_percentage === null) {
    return n === 1
      ? 'Only 1 user has attributes so far, too few to estimate a share.'
      : `Only ${formatCount(n)} users have attributes so far, too few to estimate a share.`;
  }
  const byWindow = preview.ended_by === 'window';
  const who = byWindow
    ? `the ${formatCount(n)} users seen in the last ${preview.window_days} days`
    : `the ${formatCount(n)} users seen most recently`;
  const since = !byWindow && preview.sampled_since ? ` (since ${previewSince(preview.sampled_since)})` : '';
  if (preview.matched === 0) return `None of ${who} match${since}.`;
  const rounded = Math.round(preview.estimated_percentage);
  const share = rounded === 0 ? 'Under 1%' : `About ${rounded}%`;
  return `${share} of ${who} match${since}.`;
}

