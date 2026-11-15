import React, { useEffect } from 'react';
import { useRouter } from 'next/router';
import { useAuth } from '@/contexts/AuthContext';
import { useAuthConfig } from '@/contexts/AuthConfigContext';
import { LOGIN_PATH } from '@/services/api';
import { PageTitle } from '@/components/PageTitle';
import { SiteHeader } from '@/components/SiteHeader';
import { isMarketingSite } from '@/utils/site-mode';
import { Hero } from '@/components/homepage/Hero';
import { FactsStrip } from '@/components/homepage/FactsStrip';
import { HowItWorks } from '@/components/homepage/HowItWorks';
import { Books } from '@/components/homepage/Books';
import { ProductRows } from '@/components/homepage/ProductRows';
import { SdkChips } from '@/components/homepage/SdkChips';
import { WalkthroughsTeaser } from '@/components/homepage/WalkthroughsTeaser';
import { ClosingBand } from '@/components/homepage/ClosingBand';
import { HomeFooter } from '@/components/homepage/HomeFooter';

export const HOME_AFTER_LOGIN = '/experiments';

/**
 * The public homepage.
 *
 * `/` is a `bare` route (see `utils/routes.ts`): no application shell and no
 * `RequireAuth`, so an anonymous visitor sees this page. Every dashboard route
 * is unaffected — `routeKind` returns `protected` for anything not explicitly
 * listed, so authentication is fail-closed by default and this change adds no
 * route to that list.
 *
 * An authenticated visitor is sent straight to the dashboard, which keeps the
 * behaviour anyone already signed in is used to.
 *
 * EVERY CLAIM ON THIS PAGE HAS TO BE TRUE. The landing page that used to live
 * here advertised an event volume, an uptime figure and two compliance
 * certifications for software that has never been deployed, so it could not
 * simply be restored -- `scripts/leak_guard.py` refuses to let a tree
 * containing those be pushed at all, and `index.test.tsx` asserts the absence
 * of the whole class. (The literals are deliberately not repeated here: this
 * file is scanned, and quoting a forbidden claim to explain it trips the gate.
 * That has now happened four times in four different files, most recently in
 * the commit message of the change that introduced leak_guard.py.)
 *
 * The sections are in `components/homepage/` (#1459). Every sentence they
 * render, and every alt, aria-label and title, is a row of the inventory in
 * `tests/pages/helpers/homeInventory.ts` with its source, and
 * `tests/pages/home-page.test.tsx` fails a rendered sentence that is not a row
 * and a row that is not rendered, in both builds. The two builds differ in
 * these only: the walkthroughs teaser and the screenshots are the marketing
 * build's; its second hero button is the power calculator where a self-hosted
 * platform build offers "Sign in to the dashboard"; and the books and
 * walkthroughs links go to the marketing build's own /books and
 * /walkthroughs pages, which a platform build does not have.
 *
 * The order is the site design of 2026-11-11 (D377): the hero, the facts
 * strip (which took in the deploy cards), How it works, the books, the
 * product rows, the SDKs, the walkthroughs teaser and the closing band.
 */
export default function HomePage() {
  const router = useRouter();
  const { status } = useAuth();
  const { demo } = useAuthConfig();
  // There is no hosted tier, so the public site offers no sign-in: the button
  // posted to an endpoint that 404s. See utils/site-mode.ts.
  const marketing = isMarketingSite();

  useEffect(() => {
    // A read-only demo has no homepage: once `/auth/config` says this is one,
    // `/` goes to the sign-in page, which sends a visitor already signed in
    // on to the dashboard. Until it has answered the homepage shows, as on
    // any install: the demo box is meant to redirect `/` at its proxy, and
    // this is the fallback.
    if (demo) {
      void router.replace(LOGIN_PATH);
    } else if (status === 'authenticated') {
      void router.replace(HOME_AFTER_LOGIN);
    }
  }, [demo, status, router]);

  // An authenticated visitor is on their way to the dashboard, and a demo's
  // visitor on their way to /login: render nothing rather than paint the
  // whole marketing page and replace it a moment later. `loading` still
  // renders the page (and so does the static export), so an anonymous
  // visitor -- who is every visitor this page exists for -- sees no spinner.
  if (demo || status === 'authenticated') {
    return <PageTitle />;
  }

  return (
    <>
      <PageTitle
        path="/"
        description="Open-source A/B testing and feature flags you run on your own infrastructure. Experiments, targeting rules, frequentist and Bayesian statistics, and MIT-licensed SDKs for web, mobile and server. Apache-2.0."
      />
      <div data-testid="public-home" className="min-h-screen bg-white text-slate-900">
        <SiteHeader />

        <main>
          <Hero />
          <FactsStrip />
          <HowItWorks />
          <Books />
          <ProductRows />
          <SdkChips />
          {marketing && <WalkthroughsTeaser />}
          <ClosingBand />
        </main>

        <HomeFooter />
      </div>
    </>
  );
}
