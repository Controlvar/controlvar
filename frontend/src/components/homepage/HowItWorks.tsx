import React from 'react';
import { docsUrl } from '@/services/docs';
import { Quote } from '@/components/homepage/Quote';
import { CODE_BLOCK, FOCUS, WIDTH } from '@/components/homepage/styles';

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <li className="min-w-0 rounded-xl border border-slate-200 bg-white p-6">
      <span
        aria-hidden="true"
        className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-blue-50 text-sm font-bold text-blue-700"
      >
        {n}
      </span>
      <h3 className="mt-4 text-lg font-semibold">{title}</h3>
      {children}
    </li>
  );
}

/**
 * Three steps: run it, add the SDK, read the result.
 *
 * The SDK step's code is lines 68 and 80 of docs/sdk/javascript.md, verbatim:
 * the `sdk-snippet` row of `content/home-quotes.json`, which both of that
 * table's readers hold to the page. It is not reflowed to fit a phone, so the
 * block scrolls sideways there and takes focus (axe scrollable-region-focusable).
 * The excerpt does not run on its own (`client` and `user` are defined above
 * it in the guide), hence the link to the whole example.
 */
export function HowItWorks() {
  return (
    <section aria-labelledby="how-it-works-heading" className={`${WIDTH} py-20`}>
      <h2 id="how-it-works-heading" className="text-3xl font-semibold tracking-tight">
        How it works
      </h2>
      <ol role="list" className="mt-10 grid gap-6 lg:grid-cols-3">
        <Step n={1} title="Run it">
          <p className="mt-2 leading-relaxed text-slate-600">
            Clone the repository and start everything from its root: the first start builds the images, creates the
            database and applies the demo seed.
          </p>
          <pre tabIndex={0} role="region" aria-label="Run it" className={`mt-4 ${CODE_BLOCK}`}>
            <code>docker compose up -d --wait</code>
          </pre>
        </Step>
        <Step n={2} title="Add the SDK">
          <p className="mt-2 leading-relaxed text-slate-600">
            Sticky experiment assignment, then a conversion tracked for that experiment. The JavaScript and Python SDKs
            are on npm and PyPI as betas.
          </p>
          <pre tabIndex={0} role="region" aria-label="Add the SDK" className={`mt-4 ${CODE_BLOCK}`}>
            <span className="text-slate-400">{'// npm install @controlvar/js-sdk'}</span>
            {'\n'}
            <Quote id="sdk-snippet" />
          </pre>
          <p className="mt-4">
            <a href={docsUrl('sdk/javascript')} className={`font-semibold text-blue-700 hover:text-blue-900 ${FOCUS}`}>
              The whole example in the JavaScript guide <span aria-hidden="true">→</span>
            </a>
          </p>
        </Step>
        <Step n={3} title="Read the result">
          <p className="mt-2 leading-relaxed text-slate-600">
            The change on each metric with its p-value, whether the planned sample is reached, and a sample ratio
            check of the traffic split.
          </p>
        </Step>
      </ol>
    </section>
  );
}
