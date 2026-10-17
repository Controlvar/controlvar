import React from 'react';
import Head from 'next/head';

export const APP_NAME = 'Controlvar';

export function formatPageTitle(title?: string | null): string {
  const trimmed = title?.trim();
  return trimmed ? `${trimmed} · ${APP_NAME}` : APP_NAME;
}

interface PageTitleProps {
  /** Page name; rendered as `<Page> · Controlvar`. Omit for the bare app name. */
  title?: string | null;
  /** Optional `<meta name="description">`. */
  description?: string;
}

/** Sets the document `<title>` (and optional description) for a page. */
export function PageTitle({ title, description }: PageTitleProps) {
  return (
    <Head>
      <title>{formatPageTitle(title)}</title>
      {description ? <meta name="description" content={description} /> : null}
    </Head>
  );
}

export default PageTitle;
