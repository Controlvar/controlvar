import React from 'react';
import {
  DEMO_BANNER_SIGNED_IN,
  DEMO_BANNER_SIGNED_OUT,
  DEMO_RUN_YOUR_OWN_URL,
  DEMO_SOURCE_URL,
} from '@/utils/demoMode';

const LINK =
  'underline font-medium hover:text-amber-950 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-900';

/**
 * The read-only demo's banner, above the header on every page of the shell
 * (`AppShell` renders it only in demo mode). A labelled region rather than an
 * alert or a status, so a screen reader does not announce it on every page;
 * it cannot be dismissed. Amber-900 on amber-50 is about 8.7:1, and the row
 * wraps, so it reflows at 320 px.
 */
export function DemoBanner({ signedIn }: { signedIn: boolean }) {
  return (
    <section
      aria-label="Read-only demo"
      data-testid="demo-banner"
      className="bg-amber-50 border-b border-amber-200 text-amber-900"
    >
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
        <p className="min-w-0">{signedIn ? DEMO_BANNER_SIGNED_IN : DEMO_BANNER_SIGNED_OUT}</p>
        <p className="flex flex-wrap gap-x-3 whitespace-nowrap">
          <a href={DEMO_RUN_YOUR_OWN_URL} className={LINK} data-testid="demo-banner-run-your-own">
            Run your own copy
          </a>
          <span aria-hidden="true">·</span>
          <a href={DEMO_SOURCE_URL} className={LINK} data-testid="demo-banner-source">
            Source
          </a>
        </p>
      </div>
    </section>
  );
}

export default DemoBanner;
