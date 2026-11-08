import React from 'react';
import { docsUrl } from '@/services/docs';
import type { FeatureFlag } from '@/services/featureFlags';

/**
 * An archived flag in the dashboard (#1748).
 *
 * The API refuses every request that would turn an archived flag on (400,
 * "This flag is archived. Unarchive it before turning it on."), and the
 * dashboard has no archive or unarchive action: unarchiving is
 * `POST /api/v1/feature-flags/{id}/unarchive` (beta), an API call. So an
 * archived flag reads "Archived", never "Off"; it has no on/off switch; and
 * the list and the flag's page say how to unarchive it, linking the docs.
 */

/** True when the API says the flag is archived (`status`, in any case). */
export function isFlagArchived(flag: Pick<FeatureFlag, 'status'>): boolean {
  return typeof flag.status === 'string' && flag.status.toLowerCase() === 'archived';
}

/** The status, as text: the colour of the pill is not the only signal. */
export const ARCHIVED_LABEL = 'Archived';

/** The flag page's sentence, in place of the switch. */
export const ARCHIVED_FLAG_NOTE =
  'This flag is archived: every evaluation answers it as off, and it cannot be turned on until it ' +
  'is unarchived. Unarchiving is an API call for now, POST /api/v1/feature-flags/{id}/unarchive ' +
  '(beta), and the flag comes back off.';

export const ARCHIVED_DOCS_LINK = 'Read about archived flags';

/** The list row's link, in the On / Off column where the switch would be. */
export const ARCHIVED_ROW_LINK = 'Unarchive through the API';

/** The flag guide's "Archived flags" section. */
export const ARCHIVED_DOCS_URL = docsUrl('feature-flags/create', 'archived-flags');

/** The flag page's note: what archived means here and how to undo it. */
export function ArchivedFlagNote() {
  return (
    <p className="mt-4 text-sm text-slate-700" data-testid="flag-archived-note">
      {ARCHIVED_FLAG_NOTE}{' '}
      <a
        href={ARCHIVED_DOCS_URL}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-600 hover:underline"
      >
        {ARCHIVED_DOCS_LINK}
      </a>
      .
    </p>
  );
}

/**
 * The list row's link. Its accessible name starts with the visible text and
 * names the flag, since every archived row carries one.
 */
export function ArchivedFlagRowLink({ flagKey }: { flagKey: string }) {
  return (
    <a
      href={ARCHIVED_DOCS_URL}
      target="_blank"
      rel="noopener noreferrer"
      className="text-sm text-blue-600 hover:underline"
      data-testid={`flag-archived-help-${flagKey}`}
    >
      {ARCHIVED_ROW_LINK}
      <span className="sr-only"> ({flagKey}: read about archived flags)</span>
    </a>
  );
}
