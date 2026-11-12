import React from 'react';
import { docsUrl } from '@/services/docs';
import { CHANGELOG_URL, COMPOSE_GUIDE_URL, LICENSE_URL, RELEASES_URL, STATS_TESTS_URL } from '@/content/site-links';
import { FOCUS, MORE_LINK, WIDTH } from '@/components/homepage/styles';

const LINK = `text-blue-700 underline hover:text-blue-900 ${FOCUS}`;

/**
 * Four facts under the hero, as a description list, each with the link that
 * checks it (site design 2026-11-11, item 2; the competitor study's BORROW 1):
 * the licence file, the three deploy guides, the tests that hold the
 * statistics to known answers, and the releases with their changelog. Under
 * them, in the same band, one link to the capability page (D380).
 *
 * The deploy fact replaced the "Deploy it yourself" cards, which said the
 * same thing three times lower down; its three guides are the cards' links.
 * No SDK count: the number drifted from `sdk/` before, so the pages carry none
 * (`content/sdks.ts`; `tests/content/sdks.test.tsx` fails a digit count).
 */
export function FactsStrip() {
  return (
    <section aria-label="At a glance" className="border-y border-slate-200">
      <dl className={`${WIDTH} grid gap-6 pt-10 sm:grid-cols-2 lg:grid-cols-4 lg:pt-12`}>
        <div className="min-w-0">
          <dt className="font-semibold">Apache-2.0, SDKs MIT</dt>
          <dd className="mt-1 text-sm leading-relaxed text-slate-600">
            Both build profiles are the same licence and the same price: none. Read the{' '}
            <a href={LICENSE_URL} className={LINK}>
              LICENSE
            </a>
            .
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="font-semibold">Compose, Helm or AWS CDK</dt>
          <dd className="mt-1 text-sm leading-relaxed text-slate-600">
            <a href={COMPOSE_GUIDE_URL} className={LINK}>
              One machine
            </a>
            ,{' '}
            <a href={docsUrl('self-hosting/kubernetes')} className={LINK}>
              Kubernetes
            </a>
            , or{' '}
            <a href={docsUrl('self-hosting/cdk')} className={LINK}>
              your own AWS account
            </a>
            . There is no hosted tier.
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="font-semibold">Statistics you can check</dt>
          <dd className="mt-1 text-sm leading-relaxed text-slate-600">
            <a href={STATS_TESTS_URL} className={LINK}>
              Tests
            </a>{' '}
            hold the analysis to known answers: the false-positive rate of A/A tests, interval coverage and a planted
            lift.
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="font-semibold">Released in the open</dt>
          <dd className="mt-1 text-sm leading-relaxed text-slate-600">
            Versions are published on the{' '}
            <a href={RELEASES_URL} className={LINK}>
              Releases
            </a>{' '}
            page, and the{' '}
            <a href={CHANGELOG_URL} className={LINK}>
              CHANGELOG
            </a>{' '}
            says what changed in each.
          </dd>
        </div>
      </dl>
      <p className={`${WIDTH} pb-10 pt-6 lg:pb-12`}>
        <a href={docsUrl('capabilities')} className={MORE_LINK}>
          See everything it does <span aria-hidden="true">→</span>
        </a>
      </p>
    </section>
  );
}
