import React from 'react';
import { docsUrl } from '@/services/docs';
import { Quote } from '@/components/homepage/Quote';
import { CodeLine } from '@/components/homepage/CodeLine';
import { CODE_BLOCK, FOCUS, H2, MORE_LINK, WIDTH } from '@/components/homepage/styles';

function Step({
  n,
  title,
  wide = false,
  children,
}: {
  n: number;
  title: string;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <li className={`min-w-0 rounded-xl border border-slate-200 bg-white p-6${wide ? ' lg:col-span-2' : ''}`}>
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

/** Step 1's commands: the quick start's, without `$` prompts (a copied prompt breaks the command). */
export const RUN_COMMANDS = [
  'git clone https://github.com/Controlvar/controlvar.git',
  'cd controlvar',
  'docker compose up -d --wait',
] as const;

/**
 * Three steps: run it, add the SDK, read the result.
 *
 * Step 1 spans the row (site design 2026-11-11, item 3): it holds the three
 * commands and the localhost:3000 line that were the hero's, so the page says
 * them once. A third of the row could not hold the clone URL. Steps 2 and 3
 * share the next row from `lg`, which gives the SDK code half the row instead
 * of a third. Step 1 also carries the AWS installer's sentence
 * (`content/home-quotes.json`), the other way to run it, since the deploy
 * cards that held it are folded into the facts strip.
 *
 * The SDK step's code is lines 68 and 80 of docs/sdk/javascript.md, verbatim:
 * the `sdk-snippet` row of `content/home-quotes.json`, which both of that
 * table's readers hold to the page.
 *
 * Every code block wraps (CODE_BLOCK's `whitespace-pre-wrap`), and every line
 * is a CodeLine: it breaks only at a space, right after a `(`, or right after
 * a `/` of the clone URL, never inside a token such as `'user-123'`, and the
 * rest of a wrapped line is indented under its start. Below `sm` the code is
 * 12 px. The text, and what a reader copies, is still the guide's lines. Every
 * block keeps `tabIndex` and its region name. The SDK excerpt does not run on
 * its own (`client` and `user` are defined above it in the guide), hence the
 * link to the whole example.
 *
 * Step 3 links where the guide explains each part of a results page, so its
 * card ends with a way on, as step 2's does.
 */
export function HowItWorks() {
  return (
    <section aria-labelledby="how-it-works-heading" className={`${WIDTH} py-16 lg:py-20`}>
      <h2 id="how-it-works-heading" className={H2}>
        How it works
      </h2>
      <ol role="list" className="mt-10 grid gap-6 lg:grid-cols-2">
        <Step n={1} title="Run it" wide>
          <div className="lg:grid lg:grid-cols-2 lg:items-start lg:gap-10">
            <div className="min-w-0">
              <p className="mt-2 leading-relaxed text-slate-600">
                Clone the repository and start everything from its root: four containers — Postgres, Redis, the API and
                the dashboard. The first start builds the images, creates the database and applies the demo seed.
              </p>
              <p className="mt-3 leading-relaxed text-slate-600">
                Then open <span className="font-mono">localhost:3000</span>. The{' '}
                <a href={docsUrl('getting-started/quick-start')} className={`text-blue-700 underline ${FOCUS}`}>
                  quick start
                </a>{' '}
                has the default credentials and what to change before anyone else can reach it.
              </p>
            </div>
            <div className="min-w-0">
              <pre tabIndex={0} role="region" aria-label="Run it" className={`mt-4 lg:mt-2 ${CODE_BLOCK}`}>
                <code>
                  {RUN_COMMANDS.map((line, i) => (
                    <CodeLine key={line} text={line} last={i === RUN_COMMANDS.length - 1} />
                  ))}
                </code>
              </pre>
              <p className="mt-3 text-sm leading-relaxed text-slate-600">
                <Quote id="aws-installer" />
              </p>
            </div>
          </div>
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
            <a href={docsUrl('sdk/javascript')} className={MORE_LINK}>
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
            <a href={docsUrl('guides/user-guide', 'reading-results')} className={MORE_LINK}>
              How to read a results page, in the user guide <span aria-hidden="true">→</span>
            </a>
          </p>
        </Step>
      </ol>
    </section>
  );
}
