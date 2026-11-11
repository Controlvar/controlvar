import Link from 'next/link';
import { PageTitle } from '@/components/PageTitle';
import { FOCUS, H1, WIDTH } from '@/components/homepage/styles';

const BUTTON = `inline-flex min-h-11 items-center rounded-md px-5 font-medium ${FOCUS}`;

/**
 * The not-found page: the export's `404.html`.
 *
 * Next writes a `404.html` into every static export whether or not this file
 * exists; without it, that page was Next's own "404: This page could not be
 * found" in the site's header and nothing else. `/404` is an `open` route
 * (`utils/routes.ts`), so it sits in the shell: the public site's header and
 * footer on the marketing build, the dashboard's header on a platform build.
 *
 * A self-hosted dashboard's nginx serves this file for a missing path
 * (`error_page 404` in `frontend/nginx.conf`). On controlvar.com an unknown
 * path gets whatever the Amplify console's 404 rule names, and that rule is
 * not in this repository: on 2026-11-10 it answered with the homepage.
 *
 * Both links work in both builds: `/` and the `/docs` hub are in every export.
 */
export default function NotFoundPage() {
  return (
    <>
      <PageTitle title="Page not found" />
      <div className="flex-1 bg-white">
        <div className={`${WIDTH} py-20`}>
          <p className="text-sm font-semibold text-blue-700">404</p>
          <h1 className={`mt-2 ${H1}`}>Page not found</h1>
          <p className="mt-4 max-w-xl leading-relaxed text-slate-600">
            There is no page at this address. Check it for a typing mistake, or start from the homepage or the
            documentation.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link href="/" className={`${BUTTON} bg-blue-600 text-white hover:bg-blue-700`}>
              Go to the homepage
            </Link>
            <Link href="/docs" className={`${BUTTON} border border-slate-300 bg-white text-slate-900 hover:bg-slate-50`}>
              Read the documentation
            </Link>
          </div>
        </div>
      </div>
    </>
  );
}
