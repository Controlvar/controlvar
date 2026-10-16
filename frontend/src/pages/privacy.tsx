import { PageTitle } from '@/components/PageTitle';
import { CONTACT_EMAIL } from '@/utils/site-mode';

/**
 * The privacy notice for the website at controlvar.com.
 *
 * It covers the website only, not the platform: a self-hosted Controlvar
 * keeps its data on its operator's own infrastructure, and that is not
 * described here.
 *
 * Every statement below was checked against the marketing build when it was
 * written: the frontend loads no analytics or tracking script, sets no cookie,
 * and the pages the marketing build ships write nothing to browser storage
 * (the only `localStorage` write is the dashboard's sign-in token, and the
 * marketing build has no sign-in). If any of that changes, change this page in
 * the same pull request.
 */
export default function PrivacyPage() {
  return (
    <>
      <PageTitle
        title="Privacy"
        description="What the controlvar.com website collects: no cookies, no analytics, and the hosting provider's standard access logs."
      />
      <div className="flex-1 bg-white">
        <article className="mx-auto max-w-3xl px-6 py-14 text-slate-700">
          <h1 className="text-3xl font-semibold tracking-tight text-slate-900">Privacy</h1>
          <p className="mt-2 text-sm text-slate-500">Last updated 3 October 2026.</p>

          <p className="mt-6 leading-relaxed">
            This page covers the website at controlvar.com. It does not cover a copy of
            Controlvar that you run yourself; that runs on your own infrastructure and its data
            stays with you.
          </p>

          <h2 className="mt-10 text-xl font-semibold text-slate-900">What this site collects</h2>
          <p className="mt-3 leading-relaxed">
            This site sets no cookies and runs no analytics or tracking scripts. It does not store
            anything in your browser, and it has no accounts or forms. The power calculator works
            out its results in your browser; the numbers you enter are not sent to us.
          </p>

          <h2 className="mt-10 text-xl font-semibold text-slate-900">Hosting</h2>
          <p className="mt-3 leading-relaxed">
            Like any website, this one is served by a hosting provider, which keeps standard access
            logs of the requests it serves. These typically include your IP address, the time of
            the request, the page requested and the browser&apos;s user-agent string. We use them
            only to keep the site running.
          </p>

          <h2 className="mt-10 text-xl font-semibold text-slate-900">Other sites</h2>
          <p className="mt-3 leading-relaxed">
            Links to the documentation and the source code go to GitHub, which has its own privacy
            policy.
          </p>

          <h2 className="mt-10 text-xl font-semibold text-slate-900">Contact</h2>
          <p className="mt-3 leading-relaxed">
            Questions about this page, or anything else:{' '}
            <a href={`mailto:${CONTACT_EMAIL}`} className="text-blue-700 underline">
              {CONTACT_EMAIL}
            </a>
            . If you write to us, we use your address only to reply.
          </p>
        </article>
      </div>
    </>
  );
}
