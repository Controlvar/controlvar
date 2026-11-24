import React from 'react';
import { docsUrl } from '@/services/docs';
import { COMPOSE_GUIDE_URL, LICENSE_URL, STATS_TESTS_URL } from '@/content/site-links';
import { Quote } from '@/components/homepage/Quote';
import { CARD_TITLE, FOCUS, H2, MORE_LINK, WIDTH } from '@/components/homepage/styles';

const LINK = `text-blue-700 underline hover:text-blue-900 ${FOCUS}`;
const TEXT = 'mt-2 text-sm leading-relaxed text-slate-600';

function Item({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <li className="min-w-0">
      <h3 className={CARD_TITLE}>{title}</h3>
      {children}
    </li>
  );
}

/** The docs page behind an item, or its section, by its title. It sits outside the quote, whose own link must be in the quote's words. */
function Guide({ href, title }: { href: string; title: string }) {
  return (
    <p className="mt-2 text-sm">
      <a href={href} className={MORE_LINK}>
        {title} <span aria-hidden="true">→</span>
      </a>
    </p>
  );
}

/**
 * What is specific to Controlvar (#2040): six items under the hero, in both
 * builds, each a heading, what it says and the link that checks it, then one
 * line that links what Controlvar does not do and what it does.
 *
 * A sentence that names a capability is a <Quote>, so it says what its list
 * item of docs/capabilities.md says, and says "beta" when that item does. The
 * deployment item has three, because one sentence rests on one list item. The
 * licence and the statistics tests have no list item there: their words are
 * typed, and each links the file or the folder that checks it.
 *
 * No SDK count: the number drifted from `sdk/` before, so the pages carry none
 * (`content/sdks.ts`; `tests/content/sdks.test.tsx` fails a digit count).
 */
export function Specifics() {
  return (
    <section aria-labelledby="specifics-heading" className="border-y border-slate-200">
      <div className={`${WIDTH} py-12 lg:py-16`}>
        <h2 id="specifics-heading" className={H2}>
          Specific to Controlvar
        </h2>
        <ul role="list" className="mt-8 grid gap-x-10 gap-y-8 sm:grid-cols-2 lg:grid-cols-3">
          <Item title="Apache-2.0, SDKs MIT">
            <p className={TEXT}>
              The core and full builds are both free and have the same licence. Read the{' '}
              <a href={LICENSE_URL} className={LINK}>
                LICENSE
              </a>
              .
            </p>
          </Item>
          <Item title="Flags that roll themselves back">
            <p className={TEXT}>
              <Quote id="specific-rollback" />
            </p>
            <Guide href={docsUrl('feature-flags/safety')} title="Safety Monitoring" />
          </Item>
          <Item title="Corrected for several variants">
            <p className={TEXT}>
              <Quote id="specific-correction" />
            </p>
            <Guide href={docsUrl('api/endpoints', 'how-results-are-judged')} title="How results are judged" />
          </Item>
          <Item title="Sequential testing">
            <p className={TEXT}>
              <Quote id="specific-sequential" />
            </p>
            <Guide href={docsUrl('api/sequential-testing')} title="Sequential Testing & Early Stopping" />
          </Item>
          <Item title="Compose, Helm or AWS CDK">
            <p className={TEXT}>
              <a href={COMPOSE_GUIDE_URL} className={LINK}>
                Docker Compose
              </a>
              : <Quote id="specific-compose" />
            </p>
            <p className={TEXT}>
              <a href={docsUrl('self-hosting/kubernetes')} className={LINK}>
                Kubernetes
              </a>
              : <Quote id="specific-kubernetes" />
            </p>
            <p className={TEXT}>
              <a href={docsUrl('self-hosting/cdk')} className={LINK}>
                AWS CDK
              </a>
              : <Quote id="specific-cdk" />
            </p>
          </Item>
          <Item title="Tested statistics">
            <p className={TEXT}>
              <a href={STATS_TESTS_URL} className={LINK}>
                Tests
              </a>{' '}
              check the analysis against known answers: the false-positive rate of A/A tests, interval coverage and a
              planted lift.
            </p>
          </Item>
        </ul>
        <p className="mt-8 leading-relaxed text-slate-600">
          What Controlvar does not do is in{' '}
          <a href={docsUrl('getting-started/limitations')} className={LINK}>
            Limitations
          </a>
          ; its main capabilities are in{' '}
          <a href={docsUrl('capabilities')} className={LINK}>
            What Controlvar does today
          </a>
          .
        </p>
      </div>
    </section>
  );
}
