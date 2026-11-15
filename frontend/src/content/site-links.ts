/**
 * The homepage's links that leave the site and are not docs pages (#1459).
 *
 * They live here, outside `components/homepage/`, on purpose: that folder
 * writes no `href` as an absolute URL (`tests/content/home-quotes.test.tsx`),
 * because a docs page must go through a call of docsUrl with a literal page,
 * the one kind of link `docs-links.test.ts` can check. (This comment names no
 * call: that test reads comments too.) None of these is a docs page,
 * and `tests/pages/home-page.test.tsx` holds each to that: none points under
 * the repository's `docs/` or at the docs site.
 *
 * `COMPOSE_GUIDE_URL` is the one that looks like a docs link. It is the
 * self-hosting stack's README, which mkdocs.yml's nav lists by this absolute
 * URL as "Docker Compose (self-hosting stack)": the file is under `deploy/`,
 * not `docs/`, so docsUrl cannot address it. The same test holds it to that
 * nav entry and to the file.
 */

/** The repository, as the header's and the footer's "Source" link it. */
export const REPO_URL = 'https://github.com/Controlvar/controlvar';

export const RELEASES_URL = `${REPO_URL}/releases`;

export const SECURITY_URL = `${REPO_URL}/blob/main/SECURITY.md`;

export const COMPOSE_GUIDE_URL = `${REPO_URL}/blob/main/deploy/compose/README.md`;

/**
 * The roadmap (D380): ROADMAP.md, at the repository's root, so not a docs
 * page. mkdocs.yml's nav lists it by this absolute URL as "Roadmap", under
 * Getting Started. The docs hub's "Start here" row and the footer link it;
 * `tests/pages/home-page.test.tsx` holds it to that nav entry and to the file.
 */
export const ROADMAP_URL = `${REPO_URL}/blob/main/ROADMAP.md`;

/**
 * The facts strip's checkable facts (site design 2026-11-11, BORROW 1): the
 * licence, the changelog, and the tests that hold the statistics to known
 * answers. Each is a file or a folder of this repository, which
 * `tests/pages/home-page.test.tsx` holds to the working tree.
 */
export const LICENSE_URL = `${REPO_URL}/blob/main/LICENSE`;

export const CHANGELOG_URL = `${REPO_URL}/blob/main/CHANGELOG.md`;

export const STATS_TESTS_URL = `${REPO_URL}/tree/main/backend/tests/unit/stats_validation`;

/**
 * The Controlvar books (#1859): their own repository, not this one's. The
 * homepage's books section and the footer link it; mkdocs.yml's nav, the docs
 * home page and README.md carry the same URL
 * (`backend/tests/unit/docs/test_books_links.py`).
 */
export const BOOKS_URL = 'https://github.com/amarkanday/controlvar-books';

/**
 * The public read-only demo (D381): the dashboard as a viewer, rebuilt every
 * night, which the marketing hero's "Try the live demo" button opens. It is
 * the host `deploy/demo/` serves; README.md and the docs page
 * `getting-started/live-demo.md` name the same address
 * (`backend/tests/unit/docs/test_live_demo_page.py`).
 */
export const DEMO_URL = 'https://demo.controlvar.com';

/** The books' licence, as their README links it: Creative Commons Attribution 4.0 International. */
export const BOOKS_LICENCE_URL = 'https://creativecommons.org/licenses/by/4.0/';

/** The footer's credit. */
export const BUILDER_URL = 'https://www.linkedin.com/in/ashish-markanday/';
