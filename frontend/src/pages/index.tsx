import React, { useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useAuth } from '@/contexts/AuthContext';
import { PageTitle } from '@/components/PageTitle';
import { Wordmark } from '@/components/Wordmark';
import { LOGIN_PATH } from '@/services/api';
import { CONTACT_EMAIL, isMarketingSite } from '@/utils/site-mode';
import { docsUrl } from '@/services/docs';

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
 * the commit message of the change that introduced leak_guard.py.) What is written
 * below is either checked by a test in this repository or is a statement about
 * the licence and the architecture.
 */
export default function HomePage() {
  const router = useRouter();
  const { status } = useAuth();
  // There is no hosted tier, so the public site offers no sign-in: the button
  // posted to an endpoint that 404s. See utils/site-mode.ts.
  const marketing = isMarketingSite();

  useEffect(() => {
    if (status === 'authenticated') {
      void router.replace(HOME_AFTER_LOGIN);
    }
  }, [status, router]);

  // An authenticated visitor is on their way to the dashboard: render nothing
  // rather than paint the whole marketing page and replace it a moment later.
  // `loading` still renders the page, so an anonymous visitor -- who is every
  // visitor this page exists for -- sees no spinner.
  if (status === 'authenticated') {
    return <PageTitle />;
  }

  return (
    <>
      <PageTitle
        description="Open-source A/B testing and feature flags you run on your own infrastructure. Experiments, targeting rules, frequentist and Bayesian statistics, and MIT-licensed SDKs for web, mobile and server. Apache-2.0."
      />
      <div data-testid="public-home" className="min-h-screen bg-white text-slate-900">
        <header className="mx-auto flex max-w-5xl items-center justify-between px-6 py-5">
          <Wordmark href={null} />
          <nav className="flex items-center gap-6 text-sm">
            <Link href="/docs" className="text-slate-600 hover:text-slate-900">
              Docs
            </Link>
            <Link href="/power-calculator" className="text-slate-600 hover:text-slate-900">
              Power calculator
            </Link>
            {marketing ? (
              <a
                href="https://github.com/Controlvar/controlvar"
                className="rounded-md bg-blue-600 px-3.5 py-1.5 font-medium text-white hover:bg-blue-700"
              >
                Source
              </a>
            ) : (
              <Link
                href={LOGIN_PATH}
                className="rounded-md bg-blue-600 px-3.5 py-1.5 font-medium text-white hover:bg-blue-700"
              >
                Sign in
              </Link>
            )}
          </nav>
        </header>

        <main>
          <section className="mx-auto max-w-5xl px-6 pb-16 pt-10">
            <h1 className="max-w-3xl text-4xl font-semibold tracking-tight sm:text-5xl">
              A/B testing and feature flags you run yourself.
            </h1>
            <p className="mt-5 max-w-2xl text-lg leading-relaxed text-slate-600">
              Controlvar is an open-source experimentation platform: experiments, feature
              flags, targeting rules and statistics, deployed on your own infrastructure so
              the assignment data never leaves it.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <a
                href={docsUrl('getting-started/quick-start')}
                className="rounded-md bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700"
              >
                Get started
              </a>
              {marketing ? (
                <Link
                  href="/power-calculator"
                  className="rounded-md border border-slate-300 px-4 py-2 font-medium hover:bg-slate-50"
                >
                  Try the power calculator
                </Link>
              ) : (
                <Link
                  href={LOGIN_PATH}
                  className="rounded-md border border-slate-300 px-4 py-2 font-medium hover:bg-slate-50"
                >
                  Sign in to the dashboard
                </Link>
              )}
            </div>
            <p className="mt-6 text-sm text-slate-500">
              Apache-2.0. Both build profiles are the same licence and the same price: none.
            </p>
          </section>

          <section className="border-t border-slate-200 bg-slate-50">
            <div className="mx-auto grid max-w-5xl gap-8 px-6 py-14 sm:grid-cols-2 lg:grid-cols-3">
              {[
                {
                  title: 'Experiments and feature flags',
                  body: 'Sticky assignment, gradual rollouts with configurable stages, mutual-exclusion groups and a global holdout, and automated safety rollback on error-rate and latency thresholds.',
                },
                {
                  title: 'Targeting that is actually expressive',
                  body: '20+ operators — semantic version comparison, geo-distance, time windows, JSON path and array operations — compiled and cached, with throughput floors checked by a nightly benchmark job.',
                },
                {
                  title: 'Statistics beyond a p-value',
                  body: 'Frequentist and Bayesian analysis, sequential testing with mSPRT for early stopping, multi-armed bandits, and per-segment breakdowns with a Bonferroni correction across segments.',
                },
                {
                  title: 'SDKs for the stack you have',
                  body: "JavaScript, React, React Native, Edge, Python, Go, Java, Android, iOS, Flutter, Ruby, PHP, .NET and Elixir, plus OpenFeature providers for JavaScript and Python, all MIT-licensed. The JavaScript SDK is on npm (npm install @getexperimently/js-sdk) and the Python SDK is on PyPI (pip install experimently), both as betas; the others are not on npm or the other package registries yet, and each of their guides shows how to install it from the repository. By default, flag evaluation and experiment assignment happen on the server. The JavaScript and Python SDKs can also evaluate flags locally (beta), tested against cases generated by the server's own evaluator.",
                },
                {
                  title: 'Deploy it yourself',
                  body: 'Run the published container images with Docker Compose on one machine, install them on Kubernetes with the Helm chart attached to each release, or use the CDK app to deploy to ECS Fargate, Aurora PostgreSQL and ElastiCache in your own AWS account. There is no hosted tier, so there is nothing to be locked into.',
                },
                {
                  title: 'Open, and checkable',
                  body: 'The whole platform is Apache-2.0 and the SDKs are MIT. The test suites, the automated checks and the release pipeline are all in the repository, so you can read them rather than take our word for it.',
                },
              ].map((card) => (
                <div key={card.title}>
                  <h2 className="text-base font-semibold">{card.title}</h2>
                  <p className="mt-2 text-sm leading-relaxed text-slate-600">{card.body}</p>
                </div>
              ))}
            </div>
          </section>

          <section className="mx-auto max-w-5xl px-6 py-14">
            <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
              <h2 className="text-2xl font-semibold tracking-tight">Run it locally</h2>
              <a
                href={docsUrl('self-hosting/kubernetes')}
                className="text-sm text-blue-700 underline"
              >
                Self-host on Kubernetes
              </a>
            </div>
            <p className="mt-3 max-w-2xl text-slate-600">
              Four containers — Postgres, Redis, the API and the dashboard — with the
              database created and seeded on the way up.
            </p>
            <pre className="mt-5 overflow-x-auto rounded-lg bg-slate-900 p-5 text-sm leading-relaxed text-slate-100">
              <code>{`git clone https://github.com/Controlvar/controlvar.git
cd controlvar
docker compose up -d --wait`}</code>
            </pre>
            <p className="mt-4 text-sm text-slate-500">
              Then open <span className="font-mono">localhost:3000</span>. The{' '}
              <a href={docsUrl('getting-started/quick-start')} className="text-blue-700 underline">
                quick start
              </a>{' '}
              has the default credentials and what to change before anyone else can reach it.
            </p>
          </section>
        </main>

        <footer className="border-t border-slate-200">
          <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-4 px-6 py-8 text-sm text-slate-500">
            <span>
              Apache-2.0. SDKs MIT. Contact:{' '}
              <a href={`mailto:${CONTACT_EMAIL}`} className="text-slate-700 underline hover:text-slate-900">
                {CONTACT_EMAIL}
              </a>
              . Built by{' '}
              <a
                href="https://www.linkedin.com/in/ashish-markanday/"
                target="_blank"
                rel="noopener noreferrer"
                className="text-slate-700 underline hover:text-slate-900"
              >
                Ashish Markanday
              </a>
              .
            </span>
            <nav className="flex gap-5">
              <Link href="/docs" className="hover:text-slate-900">
                Docs
              </Link>
              <a
                href="https://github.com/Controlvar/controlvar"
                target="_blank"
                rel="noopener noreferrer"
                className="hover:text-slate-900"
              >
                Source
              </a>
              <Link href="/privacy" className="hover:text-slate-900">
                Privacy
              </Link>
              {!marketing && (
                <Link href={LOGIN_PATH} className="hover:text-slate-900">
                  Sign in
                </Link>
              )}
            </nav>
          </div>
        </footer>
      </div>
    </>
  );
}
