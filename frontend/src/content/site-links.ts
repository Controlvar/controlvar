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

/** The footer's credit. */
export const BUILDER_URL = 'https://www.linkedin.com/in/ashish-markanday/';
