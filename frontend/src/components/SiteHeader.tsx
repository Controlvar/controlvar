import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Wordmark } from '@/components/Wordmark';
import { LOGIN_PATH } from '@/services/api';
import { isMarketingSite } from '@/utils/site-mode';

/** The id of the phone menu, which the menu button's aria-controls names. */
export const SITE_MENU_ID = 'site-menu';

export const SOURCE_URL = 'https://github.com/Controlvar/controlvar';

/**
 * The public site's header (#1458): the homepage, and on the marketing build
 * every page in the shell too (/docs, /power-calculator, /privacy), so the
 * wordmark sits in the same place on each.
 *
 * From Tailwind's `md` (768 px) up, the links sit in one row. Below it they go
 * behind a menu button: it says whether the menu is open (aria-expanded),
 * names the menu it opens (aria-controls), and Escape closes the menu and puts
 * focus back on the button. The menu is always rendered and `hidden` while
 * closed, so aria-controls always names an element that exists.
 *
 * The links are the ones the homepage header had: Docs, Power calculator and,
 * on the marketing build, Source (there is nothing to sign in to there), or
 * Sign in on a self-hosted platform.
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

  const links: Array<{ label: string; href: string }> = [
    { label: 'Docs', href: '/docs' },
    { label: 'Power calculator', href: '/power-calculator' },
  ];

  return (
    <header data-testid="site-header" className="sticky top-0 z-40 border-b border-slate-200 bg-white">
      <div
        data-testid="site-header-row"
        className="mx-auto flex h-16 max-w-[1180px] items-center justify-between gap-4 px-6"
      >
        <Wordmark href={router.pathname === '/' ? null : '/'} />

        <nav aria-label="Main" className="hidden md:flex items-center gap-7 text-sm font-medium">
          {links.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="whitespace-nowrap text-slate-600 hover:text-slate-900"
            >
              {link.label}
            </Link>
          ))}
          {marketing ? (
            <a
              href={SOURCE_URL}
              data-testid="nav-source"
              className="whitespace-nowrap rounded-md border border-slate-300 px-3.5 py-1.5 text-slate-900 hover:bg-slate-50"
            >
              Source
            </a>
          ) : (
            <Link
              href={LOGIN_PATH}
              data-testid="nav-sign-in"
              className="whitespace-nowrap rounded-md bg-blue-600 px-3.5 py-1.5 text-white hover:bg-blue-700"
            >
              Sign in
            </Link>
          )}
        </nav>

        <button
          ref={button}
          type="button"
          data-testid="site-menu-button"
          aria-label="Menu"
          aria-expanded={open}
          aria-controls={SITE_MENU_ID}
          onClick={() => setOpen((was) => !was)}
          className="md:hidden inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-slate-300 bg-white text-slate-900 hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
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
            className="flex min-h-11 items-center text-base font-medium text-slate-700 hover:text-slate-900"
          >
            {link.label}
          </Link>
        ))}
        {marketing ? (
          <a
            href={SOURCE_URL}
            onClick={close}
            className="flex min-h-11 items-center text-base font-medium text-slate-700 hover:text-slate-900"
          >
            Source
          </a>
        ) : (
          <Link
            href={LOGIN_PATH}
            onClick={close}
            className="flex min-h-11 items-center text-base font-medium text-slate-700 hover:text-slate-900"
          >
            Sign in
          </Link>
        )}
      </nav>
    </header>
  );
}

export default SiteHeader;
