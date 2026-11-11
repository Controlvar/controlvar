import React from 'react';
import { docsUrl } from '@/services/docs';
import { Quote } from '@/components/homepage/Quote';
import { CodeLine } from '@/components/homepage/CodeLine';
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
 * table's readers hold to the page. Those lines are 77 and 101 characters; at
 * 13 px the card's box holds about 35 at 1440 px and 33 on a 390 px phone. A
 * block that scrolled sideways showed about 37 of them and cut the rest
 * mid-word, with no scrollbar on macOS or iOS; at 320 px step 1's one line was
 * cut too.
 *
 * So both blocks wrap (CODE_BLOCK's `whitespace-pre-wrap`), and every line is
 * a CodeLine: it breaks only at a space or right after a `(`, never inside a
 * token such as `'user-123'`, and the rest of a wrapped line is indented under
 * its start. Below `sm` the code is 12 px, so the widest token (24 characters)
 * fits a 320 px phone's continuation line. The text, and what a reader copies,
 * is still the guide's two lines. Both blocks keep `tabIndex` and their region
 * names, so the Tab order is unchanged. The excerpt does not run on its own
 * (`client` and `user` are defined above it in the guide), hence the link to
 * the whole example.
 *
 * Step 3 links where the guide explains each part of a results page, so its
 * card ends with a way on, as step 2's does.
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
            <code>
              <CodeLine text="docker compose up -d --wait" last />
            </code>
          </pre>
        </Step>
        <Step n={2} title="Add the SDK">
          <p className="mt-2 leading-relaxed text-slate-600">
            Sticky experiment assignment, then a conversion tracked for that experiment. The JavaScript and Python SDKs
            are on npm and PyPI as betas.
          </p>
          <pre tabIndex={0} role="region" aria-label="Add the SDK" className={`mt-4 ${CODE_BLOCK}`}>
            <CodeLine text="// npm install @controlvar/js-sdk" className="text-slate-400" />
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
          <p className="mt-4">
            <a
              href={docsUrl('guides/user-guide', 'reading-results')}
              className={`font-semibold text-blue-700 hover:text-blue-900 ${FOCUS}`}
            >
              How to read a results page, in the user guide <span aria-hidden="true">→</span>
            </a>
          </p>
        </Step>
      </ol>
    </section>
  );
}
