import React from 'react';
import { docsUrl } from '@/services/docs';
import { REPO_URL } from '@/content/site-links';
import { FOCUS_ON_DARK, WIDTH } from '@/components/homepage/styles';

const BUTTON = `inline-flex min-h-11 w-full items-center justify-center rounded-md px-5 font-semibold sm:w-auto ${FOCUS_ON_DARK}`;

/**
 * The closing band. "Source" is the header's word for the repository link
 * (SiteHeader.tsx), so the page has one name for it.
 */
export function ClosingBand() {
  return (
    <section aria-labelledby="closing-heading" className={`${WIDTH} pb-20 pt-8`}>
      <div className="flex flex-wrap items-center justify-between gap-8 rounded-2xl bg-slate-900 px-6 py-10 text-white sm:px-12 sm:py-12">
        <div className="min-w-0 max-w-2xl">
          <h2 id="closing-heading" className="text-3xl font-semibold tracking-tight">
            Open, and checkable
          </h2>
          <p className="mt-3 leading-relaxed text-slate-300">
            The whole platform is Apache-2.0 and the SDKs are MIT. The test suites, the automated checks and the release
            pipeline are all in the repository, so you can read them rather than take our word for it.
          </p>
        </div>
        <div className="flex w-full flex-wrap gap-3 sm:w-auto">
          <a href={docsUrl('getting-started/quick-start')} className={`${BUTTON} bg-blue-600 text-white hover:bg-blue-700`}>
            Get started
          </a>
          <a href={REPO_URL} className={`${BUTTON} border border-slate-600 text-white hover:bg-slate-800`}>
            Source
          </a>
        </div>
      </div>
    </section>
  );
}
