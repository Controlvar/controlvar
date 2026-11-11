import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Wordmark } from '@/components/Wordmark';
// GitHub's own mark, MIT-licensed: the one file of the dashboard that is not Apache-2.0 (REUSE.toml, NOTICE).
import { GitHubMark } from '@/components/GitHubMark';
import { LOGIN_PATH } from '@/services/api';
import { isMarketingSite } from '@/utils/site-mode';

/** The id of the phone menu, which the menu button's aria-controls names. */
export const SITE_MENU_ID = 'site-menu';

export const SOURCE_URL = 'https://github.com/Controlvar/controlvar';

/** The header's links, by build: the marketing build has the books and the walkthroughs as pages of its own. */
export function siteLinks(marketing: boolean): Array<{ label: string; href: string }> {
  return marketing
    ? [
        { label: 'Docs', href: '/docs' },
        { label: 'Books', href: '/books' },
        { label: 'Walkthroughs', href: '/walkthroughs' },
        { label: 'Power calculator', href: '/power-calculator' },
      ]
    : [
        { label: 'Docs', href: '/docs' },
        { label: 'Power calculator', href: '/power-calculator' },
      ];
}

const FOCUS =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600';

/**
 * The public site's header (#1458): the homepage, and on the marketing build
 * every page in the shell too (/docs, /books, /walkthroughs,
 * /power-calculator, /privacy), so the wordmark sits in the same place on
 * each.
 *
 * The marketing build's links are Docs, Books, Walkthroughs and Power
 * calculator (site design 2026-11-11, item 0); a self-hosted platform's are
 * Docs and Power calculator, because the books and walkthroughs pages are the
 * marketing build's only. From Tailwind's `md` (768 px) up they sit in one row.
 * Below it they go behind a menu button: it says whether the menu is open
 * (aria-expanded), names the menu it opens (aria-controls), and Escape closes
 * the menu and puts focus back on the button. The menu is always rendered and
 * `hidden` while closed, so aria-controls always names an element that exists.
 *
 * The call to action is outside the menu, at every width: "Source" with
 * GitHub's mark on the marketing build (kept on a phone, beside the menu
 * button), or "Sign in" on a self-hosted platform. Below 360 px the word
 * "Source" is visually hidden and the mark alone shows, so the row fits a
 * 320 px phone; the link's name is still "Source".
 */
export function SiteHeader() {
  const router = useRouter();
  const marketing = isMarketingSite();
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);

  const close = useCallback(() => setOpen(false), []);

  // A link followed inside the menu, or any navigation, closes it.
  useEffect(() => {
    router.events?.on('routeChangeStart', close);
    return () => {
      router.events?.off('routeChangeStart', close);
    };
  }, [router.events, close]);

  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setOpen(false);
      button.current?.focus();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open]);

  const links = siteLinks(marketing);

  return (
    <header data-testid="site-header" className="sticky top-0 z-40 border-b border-slate-200 bg-white">
      <div
        data-testid="site-header-row"
        className="mx-auto flex h-16 max-w-[1180px] items-center justify-between gap-3 px-6"
      >
        <Wordmark href={router.pathname === '/' ? null : '/'} />

        <div className="flex min-w-0 items-center gap-2 md:gap-7">
          <nav aria-label="Main" className="hidden md:flex items-center gap-7 text-sm font-medium">
            {links.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className={`whitespace-nowrap text-slate-600 hover:text-slate-900 ${FOCUS}`}
              >
                {link.label}
              </Link>
            ))}
          </nav>

          {marketing ? (
            <a
              href={SOURCE_URL}
              data-testid="nav-source"
              className={`inline-flex min-h-11 items-center gap-1.5 whitespace-nowrap rounded-md border border-slate-300 px-2.5 text-sm font-medium text-slate-900 hover:bg-slate-50 md:min-h-0 md:px-3.5 md:py-1.5 ${FOCUS}`}
            >
              <GitHubMark />
              <span className="max-[359px]:sr-only">Source</span>
            </a>
          ) : (
            <Link
              href={LOGIN_PATH}
              data-testid="nav-sign-in"
              className={`inline-flex min-h-11 items-center whitespace-nowrap rounded-md bg-blue-600 px-3.5 text-sm font-medium text-white hover:bg-blue-700 md:min-h-0 md:py-1.5 ${FOCUS}`}
            >
              Sign in
            </Link>
          )}

          <button
            ref={button}
            type="button"
            data-testid="site-menu-button"
            aria-label="Menu"
            aria-expanded={open}
            aria-controls={SITE_MENU_ID}
            onClick={() => setOpen((was) => !was)}
            className={`md:hidden inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-slate-300 bg-white text-slate-900 hover:bg-slate-50 ${FOCUS}`}
          >
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              {open ? (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 7h16M4 12h16M4 17h16" />
              )}
            </svg>
          </button>
        </div>
      </div>

      <nav
        id={SITE_MENU_ID}
        aria-label="Main"
        data-testid="site-menu"
        hidden={!open}
        className="md:hidden border-t border-slate-200 bg-white px-6 py-2"
      >
        {links.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            onClick={close}
            className={`flex min-h-11 items-center text-base font-medium text-slate-700 hover:text-slate-900 ${FOCUS}`}
          >
            {link.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}

export default SiteHeader;
