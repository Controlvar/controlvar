import React from 'react';
import Link from 'next/link';
import { isMarketingSite } from '@/utils/site-mode';

interface WordmarkProps {
  href?: string | null;
  className?: string;
}

/**
 * The public site's logo (#2040): 5.6 kB of path data, which the platform build
 * must not carry. A self-hoster's `/` and the dashboard draw the "C" below.
 *
 * A static `import` shipped it anyway (measured: the platform export's home page
 * loaded one more chunk, with the path in it), because the bundler cannot tell
 * that `isMarketingSite()` is always false there. So the module is `require`d
 * under a condition the bundler can fold: next.config.js defines
 * `NEXT_PUBLIC_SITE_MODE` in both builds, the platform build's condition becomes
 * `false`, and the require goes with the dead branch (scripts/check-platform-
 * export.mjs fails an export that still carries the path). A test run loads it too (Jest sets
 * NODE_ENV to "test", a build sets "production"), so a test switches the site
 * mode with `isMarketingSite`, as the other tests do.
 *
 * No flash and no layout shift: the logo is in the static HTML, not loaded later.
 */
const LogoLockup: typeof import('@/components/LogoLockup').LogoLockup | null =
  process.env.NEXT_PUBLIC_SITE_MODE === 'marketing' || process.env.NODE_ENV === 'test'
    ? // eslint-disable-next-line @typescript-eslint/no-require-imports -- a static import ships the path data in the platform build
      require('@/components/LogoLockup').LogoLockup
    : null;

/** "Controlvar" wordmark. A wordmark is a wordmark: it carries no status. */
export function Wordmark({ href = '/', className = '' }: WordmarkProps) {
  const inner = (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <span
        aria-hidden="true"
        className="inline-flex h-7 w-7 items-center justify-center rounded-md bg-blue-600 text-sm font-bold text-white"
      >
        C
      </span>
      <span className="text-lg font-semibold tracking-tight text-slate-900">Controlvar</span>
    </span>
  );

  // The public site shows the logo. Inside the "Controlvar home" link the link
  // names itself and the drawing is hidden; unlinked, the drawing is the name.
  const content =
    LogoLockup && isMarketingSite() ? (
      // align-top: an unlinked inline-flex box holding a block SVG has no text
      // baseline, which left a descender gap and grew the footer by 5 px.
      <span className={`inline-flex items-center align-top ${className}`}>
        <LogoLockup decorative={Boolean(href)} />
      </span>
    ) : (
      inner
    );

  if (!href) return content;
  return (
    <Link href={href} className="inline-flex items-center" aria-label="Controlvar home">
      {content}
    </Link>
  );
}

export default Wordmark;
