import React from 'react';
import { docsUrl } from '@/services/docs';
import type { FeatureFlag } from '@/services/featureFlags';

/**
 * An archived flag in the dashboard (#1748, #442).
 *
 * The API refuses every request that would turn an archived flag on (400,
 * "This flag is archived. Unarchive it before turning it on."). So an
 * archived flag reads "Archived", never "Off", and has no on/off switch on
 * either page; in its place each page says to unarchive it first. Unarchive
 * itself is on the flag's page, in "Manage this flag" (FlagManageSection),
 * for a user who may change flags.
 */

/** True when the API says the flag is archived (`status`, in any case). */
export function isFlagArchived(flag: Pick<FeatureFlag, 'status'>): boolean {
  return typeof flag.status === 'string' && flag.status.toLowerCase() === 'archived';
}

/** The status, as text: the colour of the pill is not the only signal. */
export const ARCHIVED_LABEL = 'Archived';

/** Where the switch would be, on the flag page and in a list row. */
export const ARCHIVED_FLAG_NOTE = 'This flag is archived. Unarchive it before turning it on.';

export const ARCHIVED_DOCS_LINK = 'Read about archived flags';

/** The flag guide's "Archived flags" section. */
export const ARCHIVED_DOCS_URL = docsUrl('feature-flags/create', 'archived-flags');

/** The flag page's note: what to do before it can be turned on, and the docs. */
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

/** The list row's note, in the On / Off column where the switch would be. */
export function ArchivedFlagRowNote({ flagKey }: { flagKey: string }) {
  return (
    <span className="text-sm text-slate-700" data-testid={`flag-archived-help-${flagKey}`}>
      {ARCHIVED_FLAG_NOTE}
    </span>
  );
}
