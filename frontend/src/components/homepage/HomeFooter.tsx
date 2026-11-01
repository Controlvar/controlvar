import React from 'react';
import Link from 'next/link';
import { Wordmark } from '@/components/Wordmark';
import { LOGIN_PATH } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { CONTACT_EMAIL, isMarketingSite } from '@/utils/site-mode';
import { BUILDER_URL, RELEASES_URL, REPO_URL, SECURITY_URL } from '@/content/site-links';
import { FOCUS, WIDTH } from '@/components/homepage/styles';

const LINK = `text-slate-700 hover:text-slate-900 ${FOCUS}`;

function Group({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div>
      <p id={id} className="font-semibold text-slate-900">
        {label}
      </p>
      <ul role="list" aria-labelledby={id} className="mt-3 flex flex-col gap-2.5">
        {children}
      </ul>
    </div>
  );
}

/**
 * The footer: three groups of links in one `<nav aria-label="Footer">` (the
 * group names are not headings), then the credit.
 *
 * Every same-site link is a path, never `https://controlvar.com/…`, so the
 * platform build's links stay on the self-hoster's own host. "Walkthroughs"
 * is the marketing build's only; the platform build has "Sign in" instead.
 * "Source" is the footer's only repository link (`index.site-mode.test.tsx`).
 */
export function HomeFooter() {
  const marketing = isMarketingSite();
  return (
    <footer className="border-t border-slate-200 bg-slate-50 text-sm">
      <div className={`${WIDTH} flex flex-wrap justify-between gap-10 pb-8 pt-12`}>
        <div className="min-w-0 max-w-xs">
          <Wordmark href={null} />
          <p className="mt-3 leading-relaxed text-slate-600">
            Open-source A/B testing and feature flags you run on your own infrastructure.
          </p>
        </div>
        <nav aria-label="Footer" className="grid grid-cols-2 gap-x-14 gap-y-8 sm:grid-cols-3">
          <Group id="footer-product" label="Product">
            <li>
              <a href={docsUrl('getting-started/quick-start')} className={LINK}>
                Quick start
              </a>
            </li>
            <li>
              <Link href="/docs" className={LINK}>
                Docs
              </Link>
            </li>
            <li>
              <Link href="/power-calculator" className={LINK}>
                Power calculator
              </Link>
            </li>
            <li>
              {marketing ? (
                <a href="#walkthroughs" className={LINK}>
                  Walkthroughs
                </a>
              ) : (
                <Link href={LOGIN_PATH} className={LINK}>
                  Sign in
                </Link>
              )}
            </li>
          </Group>
          <Group id="footer-project" label="Project">
            <li>
              <a href={REPO_URL} target="_blank" rel="noopener noreferrer" className={LINK}>
                Source
              </a>
            </li>
            <li>
              <a href={RELEASES_URL} className={LINK}>
                Releases
              </a>
            </li>
            <li>
              <a href={SECURITY_URL} className={LINK}>
                Security
              </a>
            </li>
            <li>
              <Link href="/privacy" className={LINK}>
                Privacy
              </Link>
            </li>
          </Group>
          <Group id="footer-contact" label="Contact">
            <li>
              <a href={`mailto:${CONTACT_EMAIL}`} className={LINK}>
                {CONTACT_EMAIL}
              </a>
            </li>
          </Group>
        </nav>
      </div>
      <div className={`${WIDTH}`}>
        <p className="border-t border-slate-200 py-6 text-slate-600">
          Apache-2.0. SDKs MIT. Built by{' '}
          <a href={BUILDER_URL} target="_blank" rel="noopener noreferrer" className={`underline ${LINK}`}>
            Ashish Markanday
          </a>
          .
        </p>
      </div>
    </footer>
  );
}
