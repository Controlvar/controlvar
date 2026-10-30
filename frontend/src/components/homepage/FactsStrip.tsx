import React from 'react';
import { WIDTH } from '@/components/homepage/styles';

/**
 * Four facts under the hero, as a description list.
 *
 * No SDK count: the number drifted from `sdk/` before, so the pages carry none
 * (`content/sdks.ts`; `tests/content/sdks.test.tsx` fails a digit count).
 */
const FACTS: ReadonlyArray<readonly [string, string]> = [
  ['Apache-2.0, SDKs MIT', 'Both build profiles are the same licence and the same price: none.'],
  ['SDKs for web, mobile and server', 'Plus OpenFeature providers for JavaScript and Python.'],
  ['Compose, Helm or AWS CDK', 'One machine, Kubernetes, or your own AWS account.'],
  ['No hosted tier', 'There is nothing to be locked into.'],
];

export function FactsStrip() {
  return (
    <section aria-label="At a glance" className="border-y border-slate-200">
      <dl className={`${WIDTH} grid gap-6 py-12 sm:grid-cols-2 lg:grid-cols-4`}>
        {FACTS.map(([term, detail]) => (
          <div key={term} className="min-w-0">
            <dt className="font-semibold">{term}</dt>
            <dd className="mt-1 text-sm leading-relaxed text-slate-600">{detail}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
