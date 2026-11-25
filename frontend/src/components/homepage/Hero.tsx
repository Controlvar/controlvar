import React from 'react';
import Link from 'next/link';
import { LOGIN_PATH } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { isMarketingSite } from '@/utils/site-mode';
import { DEMO_URL, REPO_URL } from '@/content/site-links';
import { Screenshot } from '@/components/homepage/Screenshot';
import { FOCUS, H1, MORE_LINK, WIDTH } from '@/components/homepage/styles';

/** Full width, so equal, on a phone (they were 124 and 221 px wide: F12); their own width from `sm`. */
const BUTTON = `inline-flex min-h-11 w-full items-center justify-center rounded-md px-5 font-medium sm:w-auto ${FOCUS}`;

/** A link of the row under the buttons: as tall as they are, so as easy to touch. */
const ROW_LINK = `inline-flex min-h-11 items-center ${MORE_LINK}`;

/** The dot between two links of that row, with the spaces that keep their words apart. */
const Dot = () => (
  <span aria-hidden="true" className="text-slate-500">
    {' · '}
  </span>
);

/**
 * The hero (site design 2026-11-11, hero B): what this is and the way in on
 * the left, the product on the right, one reading order.
 *
 * The commands that start it are step 1 of How it works now (they were here
 * and there both, and the dark box here took the eye before the headline).
 *
 * The second button, and the line under it, are what differ between the
 * builds. On controlvar.com it is "Try the live demo", the public read-only
 * demo (`DEMO_URL`; D381: the site links it only once it works), with a line
 * saying what the demo is. The power calculator it replaced is still linked
 * from the header, the statistics row and the footer. On a self-hoster's
 * platform build it is "Sign in to the dashboard" (`check-platform-export.mjs`
 * looks for those words), with no line. The screenshot is the marketing
 * build's only (`Screenshot`): the power calculator, until a results page is
 * recaptured. A platform build's hero is the text column alone.
 *
 * Under them, a row of links in the header's words (#2040): Docs and
 * Walkthroughs, which a phone's header keeps behind its menu, and Source. A
 * platform build has no /walkthroughs page, so its row is Docs and Source.
 * Below `sm` the hero is spaced tighter, so that a 390 x 664 px phone shows it
 * down to this row without scrolling (`tests/e2e/marketing-site.site.spec.ts`).
 *
 * From `lg` the columns are 5 and 7 of 12: at 1440 px the screenshot is
 * 632 px wide, under the 650 px at which its 1300 px capture would be
 * enlarged on a 2x display (Screenshot.tsx CAPTURE_SCALE).
 */
export function Hero() {
  const marketing = isMarketingSite();
  return (
    <section className="bg-gradient-to-b from-slate-50 to-white pb-14 lg:pb-16">
      <div className={`${WIDTH} grid gap-10 pt-6 sm:pt-14 lg:grid-cols-12 lg:items-center lg:gap-12`}>
        <div className={marketing ? 'min-w-0 lg:col-span-5' : 'min-w-0 lg:col-span-8'}>
          <p className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700">
            <span aria-hidden="true" className="h-2 w-2 rounded-full bg-blue-600" />
            Open source · Apache-2.0 · Pre-1.0
          </p>
          <h1 className={`mt-4 sm:mt-5 ${H1}`}>A/B testing and feature flags you run yourself.</h1>
          <p className="mt-4 max-w-xl text-lg leading-relaxed text-slate-600 sm:mt-5">
            Controlvar is an open-source experimentation platform for engineering teams that run their experiments and
            feature flags on their own infrastructure.
          </p>
          <div className="mt-6 flex flex-wrap items-center gap-3 sm:mt-8">
            <a href={docsUrl('getting-started/quick-start')} className={`${BUTTON} bg-blue-600 text-white hover:bg-blue-700`}>
              Get started
            </a>
            {marketing ? (
              <a
                href={DEMO_URL}
                aria-describedby="hero-demo-note"
                className={`${BUTTON} border border-slate-300 bg-white hover:bg-slate-50`}
              >
                Try the live demo
              </a>
            ) : (
              <Link href={LOGIN_PATH} className={`${BUTTON} border border-slate-300 bg-white hover:bg-slate-50`}>
                Sign in to the dashboard
              </Link>
            )}
          </div>
          {/* It also describes the button to a screen reader (aria-describedby). */}
          {marketing && (
            <p id="hero-demo-note" className="mt-3 text-sm text-slate-600">
              The demo is read-only, with simulated traffic, and is rebuilt every night.
            </p>
          )}
          <p className="mt-1 sm:mt-3">
            <Link href="/docs" className={ROW_LINK}>
              Docs
            </Link>
            <Dot />
            {marketing && (
              <>
                <Link href="/walkthroughs" className={ROW_LINK}>
                  Walkthroughs
                </Link>
                <Dot />
              </>
            )}
            <a href={REPO_URL} className={ROW_LINK}>
              Source
            </a>
          </p>
        </div>

        {marketing && (
          <div className="min-w-0 lg:col-span-7">
            <Screenshot id="power-calculator" hero />
          </div>
        )}
      </div>
    </section>
  );
}
