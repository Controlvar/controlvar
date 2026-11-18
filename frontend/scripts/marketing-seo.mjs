/**
 * What crawlers are told about controlvar.com: its pages, as one list.
 *
 * `KEPT_PAGES` is the one source of "which pages the marketing build
 * publishes besides the homepage". scripts/prune-marketing-site.mjs keeps
 * exactly these and writes out/sitemap.xml and out/robots.txt from this
 * module, so the sitemap cannot list a page the prune step removed, and a page
 * added to the list is in the sitemap without a second edit.
 *
 * Both files come from the marketing build step and NEVER from public/: public/
 * is copied into every export, so a sitemap there would be served by the
 * self-hosted dashboard image and by demo.controlvar.com too, naming
 * controlvar.com's pages from a host that is not controlvar.com.
 * scripts/check-platform-export.mjs fails a platform export that has a
 * sitemap or a `Sitemap:` line.
 *
 * The addresses are the ones `PageTitle` writes into og:url and the canonical
 * link (src/components/PageTitle.tsx): the homepage with its slash, every
 * other page without one. scripts/site-head.test.mjs compares them.
 */

export const ORIGIN = 'https://controlvar.com';

/** The pages besides the homepage; each is `<page>.html` in the export. */
export const KEPT_PAGES = ['docs', 'books', 'walkthroughs', 'power-calculator', 'privacy'];

/** Every address the marketing build publishes, as og:url and the canonical link spell it. */
export const pageUrls = () => [`${ORIGIN}/`, ...KEPT_PAGES.map((p) => `${ORIGIN}/${p}`)];

export const SITEMAP_URL = `${ORIGIN}/sitemap.xml`;

export function sitemapXml() {
  const entries = pageUrls().map((url) => `  <url><loc>${url}</loc></url>`);
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ...entries,
    '</urlset>',
    '',
  ].join('\n');
}

/** Lets every crawler in, as public/robots.txt does, and names the sitemap. */
export function robotsTxt() {
  return `User-agent: *\nAllow: /\n\nSitemap: ${SITEMAP_URL}\n`;
}
