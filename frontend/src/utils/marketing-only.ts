import { isMarketingSite } from '@/utils/site-mode';

/**
 * `getStaticProps` for a page of the marketing build only: /books and
 * /walkthroughs (site design 2026-11-11).
 *
 * In a platform build it answers `notFound`, so `next build` writes no file
 * for the page and a self-hoster's dashboard serves its not-found page there.
 * `scripts/check-platform-export.mjs` fails a platform export that has one,
 * and `scripts/site-pages.test.mjs` a marketing export that has none. Read at
 * build time, from the same NEXT_PUBLIC_SITE_MODE as everything else
 * (`utils/site-mode.ts`).
 */
export function marketingOnly(): { props: Record<string, never> } | { notFound: true } {
  return isMarketingSite() ? { props: {} } : { notFound: true };
}
