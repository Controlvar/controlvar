import React, { ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useAuth } from '@/contexts/AuthContext';
import { Wordmark } from '@/components/Wordmark';
import { LOGIN_PATH, Role, UserMe, safeNextPath } from '@/services/api';
import { isMarketingSite } from '@/utils/site-mode';
import { ROLE_COLORS, USER_ROLE_LABELS } from '@/types/admin';
import { useModules } from '@/contexts/ModulesContext';
import { MODULES, MODULES_DOC_PATH, ModulesInfo, moduleInstalled } from '@/services/modules';
import { AUDIT_LOG_ROLES } from '@/components/admin/audit/access';

export interface NavItem {
  label: string;
  href: string;
  testId: string;
  /** When set, the item is only shown to users holding one of these roles. */
  roles?: Role[];
  /**
   * When true, the item is shown only to superusers -- the same flag the
   * admin API enforces (`deps.get_current_superuser` on all six endpoints).
   *
   * Gating on a role instead was #84: the item was shown to ADMIN and
   * DEVELOPER, the page guard admitted both, and then every request under
   * /api/v1/admin returned 403. `role` and `is_superuser` are independent
   * columns -- `PUT /admin/users/{id}` sets either without the other -- so
   * a role check could not have matched the API even restricted to ADMIN.
   */
  superuser?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { label: 'Experiments', href: '/experiments', testId: 'nav-experiments' },
  { label: 'Feature Flags', href: '/feature-flags', testId: 'nav-feature-flags' },
  { label: 'Segments', href: '/segments', testId: 'nav-segments' },
  // Every role: the audit API answers each one, narrowed to their own entries
  // for DEVELOPER and VIEWER. `roles` also keeps it off anonymous renders.
  { label: 'Audit Log', href: '/admin/audit', testId: 'nav-audit-log', roles: AUDIT_LOG_ROLES },
  { label: 'Admin', href: '/admin', testId: 'nav-admin', superuser: true },
  { label: 'Docs', href: '/docs', testId: 'nav-docs' },
];

/** The page where a local-sign-in user changes their own password. */
export const CHANGE_PASSWORD_PATH = '/account/password';

/**
 * "Change password", offered inside "More" rather than in the header row
 * (#1069). In the row, beside a superuser's nav, the name, the role chip and
 * Log out, it took the header past the row's 1216 px (the row is capped at
 * 1280 px, so every wider window is the same): measured in Chromium with the
 * name "Platform Admin", 1221 px in macOS's system font and 1288 px in DejaVu
 * Sans, so "Feature Flags" and "Audit Log" wrapped and the name was cut.
 */
export const CHANGE_PASSWORD_ITEM: NavItem = {
  label: 'Change password',
  href: CHANGE_PASSWORD_PATH,
  testId: 'change-password-link',
};

export function isNavActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * The one item to mark as the current page: the longest href that matches.
 * `/admin/audit` matches both "Audit Log" and "Admin" by prefix; only the
 * more specific one is current there, while `/admin/users` still marks Admin.
 */
export function activeNavHref(pathname: string, items: NavItem[]): string | null {
  return items
    .filter((item) => isNavActive(pathname, item.href))
    .reduce<string | null>((best, item) => (best === null || item.href.length > best.length ? item.href : best), null);
}

/**
 * Routes that belong to a module. They are kept out of `NAV_ITEMS` — the
 * primary nav is core chrome and must not hard-link a route the core profile
 * does not serve — and surface instead inside the collapsed "More" group
 * below, once the module is installed.
 */
export const MODULE_NAV_ITEMS: (NavItem & { module: string })[] = [
  {
    label: 'Workspaces',
    href: '/workspaces',
    testId: 'nav-workspaces',
    module: MODULES.WORKSPACES,
  },
  {
    // The warehouse API refuses VIEWER every connection and source route, so
    // the link is not offered to one. A superuser counts as ADMIN there.
    label: 'Warehouse',
    href: '/warehouse',
    testId: 'nav-warehouse',
    module: MODULES.WAREHOUSE,
    roles: ['ADMIN', 'DEVELOPER', 'ANALYST'],
  },
];

/**
 * The module routes this instance actually serves -- and, given a user, the
 * ones that user's role can use (a superuser passes any role check, as the
 * module APIs treat one as ADMIN). Without a user, role-gated items are left
 * out.
 */
export function installedModuleNav(
  info: ModulesInfo,
  user?: Pick<UserMe, 'role' | 'is_superuser'> | null,
): NavItem[] {
  return MODULE_NAV_ITEMS.filter((item) => {
    if (!moduleInstalled(info, item.module)) return false;
    if (!item.roles) return true;
    return !!user && (user.is_superuser === true || item.roles.includes(user.role));
  });
}

/**
 * Collapsed "More" disclosure. Closed by default and never opened for the
 * viewer. It carries the installed modules' routes, which otherwise have no
 * navigation at all, in the core profile a "Modules" link to the guide
 * that says what the modules are and how to run the full profile, and, for a
 * local sign-in user, "Change password".
 */
function MoreNavGroup({
  items,
  showModulesGuide,
  accountItems,
  id,
  onNavigate,
}: {
  items: NavItem[];
  /** Add the "Modules" documentation link (the core profile). */
  showModulesGuide: boolean;
  /** The signed-in user's own pages, listed last ("Change password"). */
  accountItems: NavItem[];
  id: string;
  /** Called when a link inside the group is followed (closes the mobile menu). */
  onNavigate?: () => void;
}) {
  // Controlled, not a bare <details>: _app.tsx keeps one AppShell across
  // client-side navigations, so an uncontrolled disclosure stayed open --
  // an absolutely positioned panel over the *next* page -- after any link
  // in it was clicked. It closes on the click and on every route change.
  const [open, setOpen] = useState(false);
  const details = useRef<HTMLDetailsElement>(null);
  const router = useRouter();
  // Close both the React state and the element itself. The browser flips the
  // `open` attribute the moment <summary> is clicked and reports it through a
  // *queued* toggle event, so a close requested before that event lands would
  // otherwise be a no-op for React (prop false -> false) while the DOM stays
  // open.
  const close = useCallback(() => {
    setOpen(false);
    if (details.current) details.current.open = false;
  }, []);
  useEffect(() => {
    router.events?.on('routeChangeStart', close);
    return () => {
      router.events?.off('routeChangeStart', close);
    };
  }, [router.events, close]);
  // A native <details> never closes on an outside click or Escape, and this
  // one is an absolutely positioned panel over the page: while it is open,
  // a click anywhere else or Escape closes it.
  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: MouseEvent) => {
      if (details.current && !details.current.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, close]);
  const follow = () => {
    close();
    onNavigate?.();
  };
  return (
    <details
      ref={details}
      data-testid="more-nav-group"
      className="relative"
      open={open}
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer list-none px-3 py-1.5 rounded-md text-sm font-medium text-slate-500 hover:text-slate-900 hover:bg-slate-50">
        More
      </summary>
      <div
        id={id}
        className="absolute right-0 z-40 mt-1 w-56 rounded-md border border-slate-200 bg-white p-1 shadow-lg"
      >
        {items.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            data-testid={item.testId}
            onClick={follow}
            className="block px-3 py-2 rounded-md text-sm text-slate-700 hover:bg-slate-50"
          >
            {item.label}
          </Link>
        ))}
        {showModulesGuide && (
          <a
            href={MODULES_DOC_PATH}
            target="_blank"
            rel="noreferrer"
            data-testid="nav-modules-docs"
            onClick={follow}
            className="block px-3 py-2 rounded-md text-sm text-slate-600 hover:bg-slate-50"
          >
            Modules
          </a>
        )}
        {accountItems.length > 0 && (items.length > 0 || showModulesGuide) && (
          <div role="separator" className="my-1 border-t border-slate-200" />
        )}
        {accountItems.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            data-testid={item.testId}
            onClick={follow}
            className="block px-3 py-2 rounded-md text-sm text-slate-700 hover:bg-slate-50"
          >
            {item.label}
          </Link>
        ))}
      </div>
    </details>
  );
}

export function displayName(user: UserMe): string {
  return user.full_name?.trim() || user.username || user.email;
}

function initials(user: UserMe): string {
  const name = displayName(user);
  const parts = name.split(/[\s@._-]+/).filter(Boolean);
  const letters = parts.slice(0, 2).map((p) => p[0]?.toUpperCase() ?? '');
  return letters.join('') || 'U';
}

interface AppShellProps {
  children: ReactNode;
}

/**
 * Application chrome: top navigation (Experiments · Feature Flags · Segments ·
 * Audit Log · Admin · Docs · More, which holds "Change password") and the user
 * area with a visible "Log out" button. Mounted by `_app.tsx` for every route
 * except `/login`.
 */
export function AppShell({ children }: AppShellProps) {
  const router = useRouter();
  const { user, status, logout } = useAuth();
  const { profile, modules, version, isLoading: modulesLoading, error: modulesError } = useModules();
  const [menuOpen, setMenuOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);

  const pathname = router.pathname;
  // The marketing build has no API, so every dashboard destination 404s and
  // nobody can ever sign in. Anonymous is the ONLY state there, so without
  // this the shell shows Experiments, Feature Flags and a Sign in button on
  // /docs and /power-calculator -- all three dead. The homepage was fixed
  // first and this was missed, because the button is rendered client-side
  // after auth resolves and so does not appear in the static HTML.
  const marketing = isMarketingSite();
  const visibleNav = NAV_ITEMS.filter((item) => {
    if (marketing) return item.href === '/docs';
    if (item.superuser) return user !== null && user.is_superuser === true;
    if (item.roles) return user !== null && item.roles.includes(user.role);
    return true;
  });
  const activeHref = activeNavHref(pathname, visibleNav);
  const moduleNav = installedModuleNav({ profile, modules, version }, user);
  // Not while the probe is outstanding: the provider's initial state is core,
  // so a full-profile instance would paint the "Modules" guide link on every
  // full page load and swap it for the routes when /api/v1/modules resolved.
  // Not on a *failed* probe either: that also resolves to core, and the link
  // says "this instance runs the core profile" -- a claim the dashboard has no
  // grounds for when all it knows is that the API did not answer.
  // Also a dashboard concern: the marketing build probes no API, so the
  // profile it reports is meaningless there.
  const showModulesGuide = !marketing && !modulesLoading && modulesError === null && profile === 'core';
  // Only local sign-in has a password the API can change; under any other
  // provider the route answers 404. It does not wait for the modules probe.
  const accountNav = !marketing && user?.auth_provider === 'local' ? [CHANGE_PASSWORD_ITEM] : [];
  const moreRoutes = modulesLoading ? [] : moduleNav;
  const showMore = accountNav.length > 0 || moreRoutes.length > 0 || showModulesGuide;

  const handleLogout = async () => {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await logout();
    } finally {
      setLoggingOut(false);
      setMenuOpen(false);
      void router.replace(LOGIN_PATH);
    }
  };

  const signInHref = (() => {
    // Before hydration `asPath` may still be the route pattern; link plainly then.
    if (!router.isReady) return LOGIN_PATH;
    const next = safeNextPath(router.asPath, '/');
    return next === '/' ? LOGIN_PATH : `${LOGIN_PATH}?next=${encodeURIComponent(next)}`;
  })();

  return (
    <div data-testid="app-shell" className="min-h-screen bg-slate-50 flex flex-col">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:bg-white focus:px-3 focus:py-2 focus:rounded-md focus:shadow"
      >
        Skip to content
      </a>

      <header className="bg-white border-b border-slate-200 sticky top-0 z-40">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between gap-4">
          {/* The wordmark and the nav keep their width; when the row is short,
              only the user's name gives way (it truncates, with the email as
              its title). */}
          <div className="flex items-center gap-6 shrink-0">
            <Wordmark href={status === 'authenticated' ? '/experiments' : '/'} />

            <nav aria-label="Primary" className="hidden xl:flex items-center gap-1">
              {visibleNav.map((item) => {
                const active = item.href === activeHref;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    data-testid={item.testId}
                    aria-current={active ? 'page' : undefined}
                    className={[
                      'px-3 py-1.5 rounded-md text-sm font-medium whitespace-nowrap transition-colors',
                      active
                        ? 'bg-blue-50 text-blue-700'
                        : 'text-slate-600 hover:text-slate-900 hover:bg-slate-50',
                    ].join(' ')}
                  >
                    {item.label}
                  </Link>
                );
              })}
              {showMore && (
                <MoreNavGroup
                  items={moreRoutes}
                  showModulesGuide={showModulesGuide}
                  accountItems={accountNav}
                  id="more-nav-desktop"
                />
              )}
            </nav>
          </div>

          <div className="flex items-center gap-3 min-w-0">
            {status === 'loading' && (
              <div data-testid="user-menu-loading" className="h-8 w-24 rounded-md bg-slate-100 animate-pulse" />
            )}

            {status === 'anonymous' && !marketing && (
              <Link
                href={signInHref}
                data-testid="nav-sign-in"
                className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700"
              >
                Sign in
              </Link>
            )}

            {status === 'anonymous' && marketing && (
              <a
                href="https://github.com/Controlvar/controlvar"
                data-testid="nav-source"
                className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700"
              >
                Source
              </a>
            )}

            {status === 'authenticated' && user && (
              <div data-testid="user-menu" className="flex items-center gap-3 min-w-0">
                <div className="hidden sm:flex items-center gap-2 min-w-0">
                  <span
                    aria-hidden="true"
                    className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-200 text-xs font-semibold text-slate-700"
                  >
                    {initials(user)}
                  </span>
                  <span
                    data-testid="user-menu-name"
                    className="text-sm font-medium text-slate-800 truncate max-w-[12rem]"
                    title={user.email}
                  >
                    {displayName(user)}
                  </span>
                  <span
                    data-testid="user-menu-role"
                    className={`inline-flex shrink-0 items-center px-2 py-0.5 rounded-full text-[11px] font-semibold whitespace-nowrap ${ROLE_COLORS[user.role]}`}
                  >
                    {USER_ROLE_LABELS[user.role]}
                  </span>
                </div>
                <button
                  type="button"
                  data-testid="logout-button"
                  onClick={() => void handleLogout()}
                  disabled={loggingOut}
                  className="shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 hover:bg-slate-50 hover:text-slate-900 disabled:opacity-60"
                >
                  {loggingOut ? 'Logging out…' : 'Log out'}
                </button>
              </div>
            )}

            <button
              type="button"
              className="xl:hidden inline-flex shrink-0 items-center justify-center h-8 w-8 rounded-md text-slate-600 hover:bg-slate-100"
              aria-label={menuOpen ? 'Close navigation' : 'Open navigation'}
              aria-expanded={menuOpen}
              aria-controls="mobile-nav"
              data-testid="mobile-nav-toggle"
              onClick={() => setMenuOpen((open) => !open)}
            >
              <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                {menuOpen ? (
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                ) : (
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
                )}
              </svg>
            </button>
          </div>
        </div>

        {menuOpen && (
          <nav
            id="mobile-nav"
            aria-label="Primary mobile"
            data-testid="mobile-nav"
            className="xl:hidden border-t border-slate-200 bg-white px-4 py-2 flex flex-col gap-1"
          >
            {visibleNav.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                onClick={() => setMenuOpen(false)}
                aria-current={item.href === activeHref ? 'page' : undefined}
                className={[
                  'px-3 py-2 rounded-md text-sm font-medium',
                  item.href === activeHref ? 'bg-blue-50 text-blue-700' : 'text-slate-700 hover:bg-slate-50',
                ].join(' ')}
              >
                {item.label}
              </Link>
            ))}
            {showMore && (
              <MoreNavGroup
                items={moreRoutes}
                showModulesGuide={showModulesGuide}
                accountItems={accountNav}
                id="more-nav-mobile"
                onNavigate={() => setMenuOpen(false)}
              />
            )}
          </nav>
        )}
      </header>

      <main id="main-content" className="flex-1 flex flex-col">
        {children}
      </main>
    </div>
  );
}

export default AppShell;
