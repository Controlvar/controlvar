/**
 * Which site is this build?
 *
 * One codebase serves two things:
 *
 *   platform   what a self-hoster runs. The dashboard, behind authentication,
 *              against their own API. This is the default, because it is the
 *              product.
 *   marketing  the public site at controlvar.com. Static, no API behind
 *              it, and there is no hosted tier -- the homepage says so.
 *
 * WHY THIS EXISTS. The marketing build shipped the whole dashboard:
 * `/experiments`, `/feature-flags`, `/admin/*`, `/results`, `/workspaces`, and
 * three "Sign in" buttons. None of it can work there -- `POST
 * /api/v1/auth/login` returns the site's own HTML with a 404 -- so a visitor
 * evaluating the project met a login form that silently fails. A dead sign-in
 * button is worse than no sign-in button: it reads as a broken product rather
 * than as software you run yourself.
 *
 * Set at BUILD time, like every `NEXT_PUBLIC_*`. Anything reading this must
 * tolerate both values at runtime; nothing may assume the marketing build is
 * the only static one.
 */

export type SiteMode = 'platform' | 'marketing';

/**
 * Defaults to `platform`. The default matters: a self-hoster who sets nothing
 * must get the full dashboard, and only the deliberate act of building the
 * public site removes it. Failing the other way would ship a platform with no
 * dashboard to someone who typed `docker compose up`.
 */
export const SITE_MODE: SiteMode =
  process.env.NEXT_PUBLIC_SITE_MODE === 'marketing' ? 'marketing' : 'platform';

/** The public site's contact address, shown in the homepage footer and on /privacy. */
export const CONTACT_EMAIL = 'hello@controlvar.com';

export const isMarketingSite = (): boolean => SITE_MODE === 'marketing';

/**
 * Route prefixes that only exist when there is an API to talk to. The
 * marketing build prunes these from the static export
 * (`scripts/prune-marketing-site.mjs`) rather than shipping shells that can
 * never load data.
 *
 * `/docs` and `/power-calculator` are deliberately absent: both work with no
 * backend, and the calculator computes in the browser.
 */
export const PLATFORM_ONLY_PREFIXES: readonly string[] = [
  'admin',
  'experiments',
  'feature-flags',
  'segments',
  'results',
  'workspaces',
  'warehouse',
  'login',
];
