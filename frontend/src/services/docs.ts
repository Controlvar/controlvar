/**
 * Where a "read the guide" link goes.
 *
 * The docs are markdown under `docs/` at the repository root. The dashboard
 * used to render that markdown itself, from a slug map with no test, inside
 * the authenticated bundle — every page read "coming soon" when it was not.
 * Now the dashboard links out, and `docsUrl()` is the single place that
 * decides where "out" is.
 *
 * DEFAULT: THE REPOSITORY, NOT THE MKDOCS SITE. The published site
 * (`mkdocs.yml` `site_url`, `MKDOCS_SITE_URL` below) is built and deployed by
 * `.github/workflows/docs.yml` from release tags only, so it follows the
 * latest release rather than `main`. A dashboard someone runs themselves may
 * be any version, and the repository's markdown is the copy that cannot be
 * missing: GitHub renders it, with no dependency on the release pipeline. So
 * the default is a GitHub blob URL. (The default once was the site, before it
 * had ever been deployed, and every link -- the homepage's primary button
 * among them -- answered 404. `docs.test.ts` pins that it is not again.)
 *
 * `NEXT_PUBLIC_DOCS_URL` overrides that with the origin of a *built* site, and
 * the URLs then take MkDocs's `use_directory_urls` shape instead. The
 * marketing build (`npm run build:marketing`) sets it to the published site,
 * which is where controlvar.com's links go; anyone serving their own
 * `mkdocs build` output can set it to that. Read at build time, like every
 * `NEXT_PUBLIC_*`.
 *
 * `url-literals.test.ts` does this for `/api/v1/` paths; `docs-links.test.ts`
 * does it here — every `docsUrl('...')` literal in the dashboard must resolve
 * to a file that exists under `docs/`. Its Python twin,
 * `backend/tests/unit/docs/test_dashboard_docs_links.py`, applies the same rule
 * on a docs-only pull request and pins itself to the Jest file's text.
 */

/** The repository the markdown lives in, and the branch links point at. */
export const DOCS_REPO = 'https://github.com/Controlvar/controlvar';
export const DOCS_BRANCH = 'main';

/** The published MkDocs site, deployed from release tags (mkdocs.yml `site_url`). */
export const MKDOCS_SITE_URL = 'https://docs.controlvar.com/';

/** A self-hosted built site, if one is configured. Empty means "use the repository". */
export const DOCS_SITE = (process.env.NEXT_PUBLIC_DOCS_URL || '').replace(/\/+$/, '');

/** The base every docs link is built on, whichever mode is in force. */
export const DOCS_URL = DOCS_SITE || `${DOCS_REPO}/tree/${DOCS_BRANCH}/docs`;

/**
 * The URL of a page, given its path under `docs/` without the `.md`
 * (`'getting-started/quick-start'`).
 *
 * A `README` addresses its directory in both modes: MkDocs publishes it as the
 * directory index, and GitHub renders it under the directory listing.
 */
export function docsUrl(page: string, anchor?: string): string {
  const path = page.replace(/^\/+|\/+$/g, '');
  const isIndex = path === '' || path === 'README' || path.endsWith('/README');
  const dir = path.replace(/(^|\/)README$/, '').replace(/\/+$/, '');

  let base: string;
  if (DOCS_SITE) {
    // A built MkDocs site: `use_directory_urls`, so every page is a directory.
    base = dir ? `${DOCS_SITE}/${dir}/` : `${DOCS_SITE}/`;
  } else if (isIndex) {
    // A directory on GitHub renders its README.
    base = `${DOCS_REPO}/tree/${DOCS_BRANCH}/docs${dir ? `/${dir}` : ''}`;
  } else {
    base = `${DOCS_REPO}/blob/${DOCS_BRANCH}/docs/${path}.md`;
  }

  return anchor ? `${base}#${anchor}` : base;
}
