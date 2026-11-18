import React from 'react';
import Head from 'next/head';
import { isMarketingSite } from '@/utils/site-mode';

export const APP_NAME = 'Controlvar';

/** The public site's origin: the base of every absolute URL in a link preview. */
export const SITE_ORIGIN = 'https://controlvar.com';

/** The link-preview image, `frontend/public/og.png` (#1458). */
export const SHARE_IMAGE = { path: '/og.png', width: 1200, height: 630 } as const;

export function formatPageTitle(title?: string | null): string {
  const trimmed = title?.trim();
  return trimmed ? `${trimmed} · ${APP_NAME}` : APP_NAME;
}

interface PageTitleProps {
  /** Page name; rendered as `<Page> · Controlvar`. Omit for the bare app name. */
  title?: string | null;
  /** Optional `<meta name="description">`. */
  description?: string;
  /**
   * The page's path on controlvar.com (`/`, `/docs`). With a description, the
   * marketing build adds the Open Graph and Twitter card tags a shared link's
   * preview is built from (#1458).
   */
  path?: string;
  /**
   * A complete `<title>`, used instead of `<title> · Controlvar` on the
   * marketing build only (the homepage's descriptive title). The dashboard
   * keeps `title`.
   */
  marketingTitle?: string;
}

/**
 * Sets the document `<title>` (and optional description) for a page.
 *
 * On the marketing build, a page that gives both a `description` and its
 * `path` also gets the link-preview tags: og:title, og:description, og:url,
 * og:image (an absolute URL) and twitter:*, and one canonical link equal to
 * og:url. A self-hosted dashboard gets none
 * of them: its pages are not at controlvar.com, and an og:url saying they were
 * would be wrong. `scripts/site-head.test.mjs` checks the marketing export.
 */
export function PageTitle({ title, description, path, marketingTitle }: PageTitleProps) {
  const marketing = isMarketingSite();
  const fullTitle = marketing && marketingTitle ? marketingTitle : formatPageTitle(title);
  const share = marketing && description && path;
  const image = `${SITE_ORIGIN}${SHARE_IMAGE.path}`;
  return (
    <Head>
      <title>{fullTitle}</title>
      {description ? <meta name="description" content={description} /> : null}
      {share ? (
        <>
          <link key="canonical" rel="canonical" href={`${SITE_ORIGIN}${path}`} />
          <meta key="og:type" property="og:type" content="website" />
          <meta key="og:title" property="og:title" content={fullTitle} />
          <meta key="og:description" property="og:description" content={description} />
          <meta key="og:url" property="og:url" content={`${SITE_ORIGIN}${path}`} />
          <meta key="og:image" property="og:image" content={image} />
          <meta key="og:image:width" property="og:image:width" content={String(SHARE_IMAGE.width)} />
          <meta key="og:image:height" property="og:image:height" content={String(SHARE_IMAGE.height)} />
          <meta key="twitter:card" name="twitter:card" content="summary_large_image" />
          <meta key="twitter:title" name="twitter:title" content={fullTitle} />
          <meta key="twitter:description" name="twitter:description" content={description} />
          <meta key="twitter:image" name="twitter:image" content={image} />
        </>
      ) : null}
    </Head>
  );
}

export default PageTitle;
