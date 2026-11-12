import React from 'react';
import { DEMO_RUN_YOUR_OWN_URL, DEMO_WALKTHROUGHS_URL } from '@/utils/demoMode';

const LINK = 'text-blue-700 underline hover:text-blue-900';

/**
 * What a page says in demo mode where it would otherwise say what a role
 * cannot do, and where "+ New" would sit: "Creating and changing experiments
 * is turned off in this read-only demo. Watch it done in the walkthroughs, or
 * run your own copy." Plain text with links, never a tooltip on a disabled
 * control.
 *
 * `action` is the sentence's subject ("Creating and changing experiments").
 * Without `walkthroughs` the second sentence only offers your own copy (the
 * account pages: the walkthroughs do not show those).
 */
export function DemoNote({
  action,
  walkthroughs = true,
  className = 'text-sm text-slate-600',
}: {
  action: string;
  walkthroughs?: boolean;
  className?: string;
}) {
  return (
    <p className={className} data-testid="demo-note">
      {action} is turned off in this read-only demo.{' '}
      {walkthroughs ? (
        <>
          Watch it done in the{' '}
          <a href={DEMO_WALKTHROUGHS_URL} className={LINK} data-testid="demo-note-walkthroughs">
            walkthroughs
          </a>
          , or{' '}
          <a href={DEMO_RUN_YOUR_OWN_URL} className={LINK} data-testid="demo-note-run-your-own">
            run your own copy
          </a>
          .
        </>
      ) : (
        <>
          To try it,{' '}
          <a href={DEMO_RUN_YOUR_OWN_URL} className={LINK} data-testid="demo-note-run-your-own">
            run your own copy
          </a>
          .
        </>
      )}
    </p>
  );
}

export default DemoNote;
