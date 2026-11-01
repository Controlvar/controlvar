import React from 'react';
import Link from 'next/link';
import { LOGIN_PATH } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { isMarketingSite } from '@/utils/site-mode';
import { Screenshot } from '@/components/homepage/Screenshot';
import { FOCUS, WIDTH } from '@/components/homepage/styles';

const BUTTON = `inline-flex min-h-11 items-center rounded-md px-5 font-medium ${FOCUS}`;

/**
 * The hero: what this is, the way in, and the three commands that start it.
 *
 * The second button is the only sentence that differs between the builds: the
 * power calculator on controlvar.com, which has no API, and "Sign in to the
 * dashboard" on a self-hoster's platform build (`check-platform-export.mjs`
 * looks for those words). The results screenshot is the marketing build's
 * only (`Screenshot`).
 *
 * The commands have no `$` prompts: a copied prompt breaks the command, and
 * axe passes a line of punctuation whatever its contrast. They wrap rather
 * than scroll on a phone.
 */
export function Hero() {
  const marketing = isMarketingSite();
  return (
    <section className="bg-gradient-to-b from-slate-50 to-white pb-16">
      <div className={`${WIDTH} grid gap-12 pt-12 sm:pt-16 lg:grid-cols-2 lg:items-center`}>
        <div className="min-w-0">
          <p className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700">
            <span aria-hidden="true" className="h-2 w-2 rounded-full bg-blue-600" />
            Open source · Apache-2.0
          </p>
          <h1 className="mt-5 text-4xl font-semibold tracking-tight sm:text-5xl">
            A/B testing and feature flags you run yourself.
          </h1>
          <p className="mt-5 max-w-xl text-lg leading-relaxed text-slate-600">
            Controlvar is an open-source experimentation platform: experiments, feature flags, targeting rules and
            statistics, deployed on your own infrastructure so the assignment data never leaves it.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <a href={docsUrl('getting-started/quick-start')} className={`${BUTTON} bg-blue-600 text-white hover:bg-blue-700`}>
              Get started
            </a>
            {marketing ? (
              <Link href="/power-calculator" className={`${BUTTON} border border-slate-300 bg-white hover:bg-slate-50`}>
                Try the power calculator
              </Link>
            ) : (
              <Link href={LOGIN_PATH} className={`${BUTTON} border border-slate-300 bg-white hover:bg-slate-50`}>
                Sign in to the dashboard
              </Link>
            )}
          </div>
        </div>

        <div className="min-w-0">
          <div className="overflow-hidden rounded-xl bg-slate-900 shadow-xl">
            <div className="flex items-center gap-2 border-b border-slate-700 px-4 py-3">
              <span aria-hidden="true" className="h-2.5 w-2.5 rounded-full bg-slate-600" />
              <span aria-hidden="true" className="h-2.5 w-2.5 rounded-full bg-slate-600" />
              <span aria-hidden="true" className="h-2.5 w-2.5 rounded-full bg-slate-600" />
              <p className="ml-2 text-xs font-medium text-slate-300">Run it locally</p>
            </div>
            {/* Focusable, like every code block on the page (axe scrollable-region-focusable;
                tests/e2e/marketing-site.site.spec.ts), though these lines wrap. */}
            <pre
              tabIndex={0}
              role="region"
              aria-label="Run it locally"
              className={`whitespace-pre-wrap break-words p-5 font-mono text-sm leading-relaxed text-slate-100 ${FOCUS}`}
            >
              <code>{`git clone https://github.com/Controlvar/controlvar.git
cd controlvar
docker compose up -d --wait`}</code>
            </pre>
          </div>
          <p className="mt-4 text-sm leading-relaxed text-slate-600">
            Four containers — Postgres, Redis, the API and the dashboard — with the database created and seeded on the
            way up.
          </p>
          <p className="mt-2 text-sm leading-relaxed text-slate-600">
            Then open <span className="font-mono">localhost:3000</span>. The{' '}
            <a href={docsUrl('getting-started/quick-start')} className={`text-blue-700 underline ${FOCUS}`}>
              quick start
            </a>{' '}
            has the default credentials and what to change before anyone else can reach it.
          </p>
        </div>
      </div>

      {marketing && (
        <div className={`${WIDTH} pt-14`}>
          <Screenshot id="results" hero />
        </div>
      )}
    </section>
  );
}
