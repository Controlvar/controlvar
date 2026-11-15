import React from 'react';
import Link from 'next/link';
import { LOGIN_PATH } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { isMarketingSite } from '@/utils/site-mode';
import { DEMO_URL } from '@/content/site-links';
import { Screenshot } from '@/components/homepage/Screenshot';
import { FOCUS, H1, WIDTH } from '@/components/homepage/styles';

/** Full width, so equal, on a phone (they were 124 and 221 px wide: F12); their own width from `sm`. */
const BUTTON = `inline-flex min-h-11 w-full items-center justify-center rounded-md px-5 font-medium sm:w-auto ${FOCUS}`;

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
 * From `lg` the columns are 5 and 7 of 12: at 1440 px the screenshot is
 * 632 px wide, under the 650 px at which its 1300 px capture would be
 * enlarged on a 2x display (Screenshot.tsx CAPTURE_SCALE).
 */
export function Hero() {
  const marketing = isMarketingSite();
  return (
    <section className="bg-gradient-to-b from-slate-50 to-white pb-14 lg:pb-16">
      <div className={`${WIDTH} grid gap-10 pt-10 sm:pt-14 lg:grid-cols-12 lg:items-center lg:gap-12`}>
        <div className={marketing ? 'min-w-0 lg:col-span-5' : 'min-w-0 lg:col-span-8'}>
          <p className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700">
            <span aria-hidden="true" className="h-2 w-2 rounded-full bg-blue-600" />
            Open source · Apache-2.0
          </p>
          <h1 className={`mt-5 ${H1}`}>A/B testing and feature flags you run yourself.</h1>
          <p className="mt-5 max-w-xl text-lg leading-relaxed text-slate-600">
            Controlvar is an open-source experimentation platform: experiments, feature flags, targeting rules and
            statistics, deployed on your own infrastructure so the assignment data never leaves it.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
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
