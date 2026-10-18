import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import {
  AppShell,
  MODULE_NAV_ITEMS,
  NAV_ITEMS,
  displayName,
  isNavActive,
  installedModuleNav,
} from '@/components/AppShell';
import { AuthProvider } from '@/contexts/AuthContext';
import { ModulesProvider, __resetModulesCache } from '@/contexts/ModulesContext';
import { CORE_PROFILE, MODULES, ModulesInfo } from '@/services/modules';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import { MODULES_DOC_PATH } from '@/services/modules';

const mockReplace = jest.fn().mockResolvedValue(true);
let mockPathname = '/experiments';
let mockAsPath = '/experiments';

// A tiny router event bus so tests can fire `routeChangeStart` the way Next
// does on a client-side navigation.
const routerListeners: Record<string, Array<() => void>> = {};
const mockRouterEvents = {
  on: (event: string, cb: () => void) => {
    (routerListeners[event] ??= []).push(cb);
  },
  off: (event: string, cb: () => void) => {
    routerListeners[event] = (routerListeners[event] ?? []).filter((c) => c !== cb);
  },
  emit: (event: string) => {
    (routerListeners[event] ?? []).forEach((cb) => cb());
  },
};

jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: jest.fn(),
    pathname: mockPathname,
    asPath: mockAsPath,
    query: {},
    isReady: true,
    events: mockRouterEvents,
  }),
}));

jest.mock('next/link', () => {
  const MockLink = ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});

const mockFetch = jest.fn();
global.fetch = mockFetch;

function makeUser(overrides: Partial<UserMe> = {}): UserMe {
  return {
    id: 'u-1',
    email: 'admin@demo.com',
    username: 'admin',
    full_name: 'Demo Admin',
    role: 'ADMIN',
    is_superuser: true,
    is_active: true,
    auth_provider: 'local',
    ...overrides,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

function signInAs(user: UserMe) {
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  mockFetch.mockResolvedValueOnce(jsonResponse(200, user));
}

function full(overrides: Partial<ModulesInfo> = {}): ModulesInfo {
  return {
    profile: 'full',
    modules: [MODULES.WORKSPACES],
    version: '1.0.0',
    ...overrides,
  };
}

function renderShell(info: ModulesInfo = CORE_PROFILE) {
  return render(
    <ModulesProvider initial={info}>
      <AuthProvider>
        <AppShell>
          <div data-testid="page-content">page</div>
        </AppShell>
      </AuthProvider>
    </ModulesProvider>,
  );
}

beforeEach(() => {
  // The modules cache lives at module scope and outlives a test; an unseeded
  // provider in one test would otherwise reuse the previous test's answer.
  __resetModulesCache();
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  mockPathname = '/experiments';
  mockAsPath = '/experiments';
});

describe('AppShell', () => {
  it('renders children and the wordmark, with no status pill on it', async () => {
    signInAs(makeUser());
    renderShell();
    expect(screen.getByTestId('page-content')).toBeInTheDocument();
    expect(screen.getByText('Controlvar')).toBeInTheDocument();
    // The wordmark is the badge letter and the name, nothing appended.
    expect(screen.getByLabelText('Controlvar home')).toHaveTextContent(/^CControlvar$/);
    await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
  });

  it('shows Experiments, Feature Flags, Segments, Admin and Docs for an ADMIN', async () => {
    signInAs(makeUser());
    renderShell();
    await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    expect(nav).toHaveTextContent('Experiments');
    expect(nav).toHaveTextContent('Feature Flags');
    expect(nav).toHaveTextContent('Segments');
    expect(nav).toHaveTextContent('Admin');
    expect(nav).toHaveTextContent('Docs');
    expect(screen.getByTestId('nav-segments')).toHaveAttribute('href', '/segments');
    expect(screen.getByTestId('nav-admin')).toHaveAttribute('href', '/admin');
    expect(screen.getByTestId('nav-experiments')).toHaveAttribute('aria-current', 'page');
  });

  describe('the Audit Log item (#915)', () => {
    it.each([
      ['ADMIN', false],
      ['DEVELOPER', false],
      ['ANALYST', false],
      ['VIEWER', false],
      ['ADMIN', true],
    ] as const)('links a %s (superuser: %s) to /admin/audit', async (role, isSuperuser) => {
      signInAs(makeUser({ role, is_superuser: isSuperuser }));
      renderShell();
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.getByTestId('nav-audit-log')).toHaveAttribute('href', '/admin/audit');
      expect(screen.getByTestId('nav-audit-log')).toHaveTextContent('Audit Log');
    });

    it('is not offered to an anonymous visitor', async () => {
      mockPathname = '/docs/[...slug]';
      mockAsPath = '/docs/quick-start';
      renderShell();
      await waitFor(() => expect(screen.getByTestId('nav-sign-in')).toBeInTheDocument());
      expect(screen.queryByTestId('nav-audit-log')).not.toBeInTheDocument();
      expect(document.querySelectorAll('a[href="/admin/audit"]')).toHaveLength(0);
    });

    it('marks only Audit Log current on /admin/audit, and only Admin on /admin/users', async () => {
      for (const [path, current] of [
        ['/admin/audit', '/admin/audit'],
        ['/admin/users', '/admin'],
      ] as const) {
        localStorage.clear();
        mockFetch.mockReset();
        mockPathname = path;
        mockAsPath = path;
        signInAs(makeUser());
        const view = renderShell();
        await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());

        const desktop = screen.getByRole('navigation', { name: 'Primary' });
        const desktopCurrent = Array.from(desktop.querySelectorAll('a[aria-current="page"]'));
        expect(desktopCurrent.map((a) => a.getAttribute('href'))).toEqual([current]);

        // The mobile links carry no test ids: query inside the mobile nav.
        fireEvent.click(screen.getByTestId('mobile-nav-toggle'));
        const mobile = screen.getByTestId('mobile-nav');
        const mobileCurrent = Array.from(mobile.querySelectorAll('a[aria-current="page"]'));
        expect(mobileCurrent.map((a) => a.getAttribute('href'))).toEqual([current]);
        expect(mobile.querySelector('a[href="/admin/audit"]')).toHaveTextContent('Audit Log');
        view.unmount();
      }
    });
  });

  it('shows Admin to a superuser whatever their role', async () => {
    // The admin API is uniformly `deps.get_current_superuser`, so that flag --
    // not the role -- is what the nav has to match (#84).
    for (const role of ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'] as const) {
      localStorage.clear();
      mockFetch.mockReset();
      signInAs(makeUser({ role, is_superuser: true }));
      const view = renderShell();
      await waitFor(() => expect(screen.getByTestId('nav-admin')).toBeInTheDocument());
      view.unmount();
    }
  });

  it('hides Admin from a non-superuser, including one whose role is ADMIN', async () => {
    // The ADMIN case is the one a role check could never have got right:
    // `role` and `is_superuser` are independent columns and
    // `PUT /admin/users/{id}` sets either without the other, so an
    // ADMIN-without-superuser is reachable. Before this, they saw the item,
    // the page guard admitted them, and every admin request returned 403.
    for (const role of ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'] as const) {
      localStorage.clear();
      mockFetch.mockReset();
      signInAs(makeUser({ role, is_superuser: false }));
      const view = renderShell();
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.queryByTestId('nav-admin')).not.toBeInTheDocument();
      expect(screen.getByTestId('nav-docs')).toBeInTheDocument();
      view.unmount();
    }
  });

  it('shows the user name, role chip and a visible Log out button', async () => {
    signInAs(makeUser({ full_name: 'Ada Lovelace', role: 'ADMIN' }));
    renderShell();
    await waitFor(() => expect(screen.getByTestId('user-menu-name')).toHaveTextContent('Ada Lovelace'));
    expect(screen.getByTestId('user-menu-role')).toHaveTextContent('Admin');
    expect(screen.getByRole('button', { name: 'Log out' })).toBeVisible();
  });

  it('offers Change password to a local sign-in user, opening /account/password', async () => {
    signInAs(makeUser({ auth_provider: 'local' }));
    renderShell();
    const link = await screen.findByTestId('change-password-link');
    expect(link).toHaveTextContent('Change password');
    expect(link).toHaveAttribute('href', '/account/password');
  });

  describe('a superuser\'s header fits on one line (#1069)', () => {
    // jsdom lays nothing out, so these read the contract the layout rests on;
    // tests/e2e/header-breakpoint.journey.spec.ts measures it at 1280, 1440
    // and 1920 px.
    const classesOf = (el: Element) => el.className.split(/\s+/);

    it('keeps every primary link on one line and lets only the name give way', async () => {
      signInAs(makeUser({ full_name: 'Platform Admin' }));
      renderShell();
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      const nav = screen.getByRole('navigation', { name: 'Primary' });
      const links = Array.from(nav.querySelectorAll(':scope > a'));
      expect(links.map((a) => a.textContent)).toEqual([
        'Experiments',
        'Feature Flags',
        'Segments',
        'Audit Log',
        'Admin',
        'Docs',
      ]);
      for (const link of links) expect(classesOf(link)).toContain('whitespace-nowrap');
      // The wordmark-and-nav column does not shrink, so the nav cannot be
      // squeezed into wrapping; the user area can, and in it only the name
      // (truncated, the email as its title).
      const column = nav.parentElement as Element;
      expect(classesOf(column)).toContain('shrink-0');
      expect(classesOf(column)).not.toContain('min-w-0');
      const name = screen.getByTestId('user-menu-name');
      expect(name).toHaveTextContent('Platform Admin');
      expect(classesOf(name)).toContain('truncate');
      expect(name).toHaveAttribute('title', 'admin@demo.com');
      expect(classesOf(screen.getByTestId('user-menu'))).toContain('min-w-0');
      expect(classesOf(screen.getByTestId('user-menu-role'))).toContain('shrink-0');
      expect(classesOf(screen.getByTestId('logout-button'))).toContain('shrink-0');
    });

    it('puts Change password inside More, not in the header row', async () => {
      signInAs(makeUser({ auth_provider: 'local' }));
      renderShell();
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      const link = screen.getByTestId('change-password-link');
      expect(screen.getByTestId('user-menu')).not.toContainElement(link);
      const group = screen.getAllByTestId('more-nav-group')[0] as HTMLDetailsElement;
      expect(group).toContainElement(link);
      // Last in the group, after the modules guide, with a rule between them.
      const panelLinks = Array.from(group.querySelectorAll('a'));
      expect(panelLinks.map((a) => a.textContent)).toEqual(['Modules', 'Change password']);
      expect(group.querySelector('[role="separator"]')).not.toBeNull();

      // Reachable: More opens, the link is followed, More closes.
      fireEvent.click(group.querySelector('summary')!);
      await waitFor(() => expect(group).toHaveAttribute('open'));
      fireEvent.click(link);
      await waitFor(() => expect(group).not.toHaveAttribute('open'));
    });

    it('offers Change password in the mobile menu\'s More too', async () => {
      signInAs(makeUser({ auth_provider: 'local' }));
      renderShell(full());
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      fireEvent.click(screen.getByTestId('mobile-nav-toggle'));
      const mobile = screen.getByTestId('mobile-nav');
      const link = mobile.querySelector('[data-testid="change-password-link"]');
      expect(link).toHaveAttribute('href', '/account/password');
      expect(mobile.querySelector('[data-testid="more-nav-group"]')).toContainElement(link as HTMLElement);
    });

    it('shows More with only Change password while the modules are still being probed', async () => {
      // The password page does not depend on the modules, so it does not wait
      // for the probe; the module routes and the guide link still do.
      mockFetch.mockImplementation((url: string) =>
        url.endsWith('/api/v1/modules')
          ? new Promise(() => {})
          : Promise.resolve(jsonResponse(200, makeUser({ auth_provider: 'local' }))),
      );
      localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
      render(
        <ModulesProvider>
          <AuthProvider>
            <AppShell>
              <div data-testid="page-content">page</div>
            </AppShell>
          </AuthProvider>
        </ModulesProvider>,
      );
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      const group = screen.getAllByTestId('more-nav-group')[0];
      expect(Array.from(group.querySelectorAll('a')).map((a) => a.textContent)).toEqual(['Change password']);
      expect(group.querySelector('[role="separator"]')).toBeNull();
    });
  });

  it('hides Change password from a user who does not sign in locally', async () => {
    // Under any other provider the route answers 404, so the item would only
    // lead to an error.
    for (const provider of ['cognito', 'sso', '']) {
      localStorage.clear();
      mockFetch.mockReset();
      signInAs(makeUser({ auth_provider: provider }));
      const view = renderShell();
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.getByTestId('logout-button')).toBeInTheDocument();
      expect(screen.queryByTestId('change-password-link')).not.toBeInTheDocument();
      view.unmount();
    }
  });

  it('falls back to username when full_name is empty', () => {
    expect(displayName(makeUser({ full_name: null }))).toBe('admin');
    expect(displayName(makeUser({ full_name: '  ' }))).toBe('admin');
    expect(displayName(makeUser({ full_name: null, username: '' }))).toBe('admin@demo.com');
  });

  it('logs out and navigates to /login', async () => {
    signInAs(makeUser());
    mockFetch.mockResolvedValueOnce(jsonResponse(204, undefined));
    renderShell();
    await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    });

    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/login'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(mockFetch.mock.calls[1][0]).toBe('/api/v1/auth/logout');
  });

  it('shows a Sign in link carrying ?next= for anonymous visitors on open pages', async () => {
    mockPathname = '/docs/[...slug]';
    mockAsPath = '/docs/quick-start';
    renderShell();
    await waitFor(() => expect(screen.getByTestId('nav-sign-in')).toBeInTheDocument());
    expect(screen.getByTestId('nav-sign-in')).toHaveAttribute('href', '/login?next=%2Fdocs%2Fquick-start');
    expect(screen.queryByTestId('nav-admin')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument();
  });

  it('shows a placeholder while the session loads', () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockImplementation(() => new Promise(() => {}));
    renderShell();
    expect(screen.getByTestId('user-menu-loading')).toBeInTheDocument();
  });

  it('toggles the mobile navigation', async () => {
    signInAs(makeUser());
    renderShell();
    await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
    expect(screen.queryByTestId('mobile-nav')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('mobile-nav-toggle'));
    expect(screen.getByTestId('mobile-nav')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('mobile-nav-toggle'));
    expect(screen.queryByTestId('mobile-nav')).not.toBeInTheDocument();
  });

  it('collapses to the menu button below 1280 px, one breakpoint for all three (#926)', async () => {
    // Between 768 and 1279 px the inline nav reached the user menu (a
    // superuser's at 1024 px, every role's at 768 px), so the header collapses
    // at Tailwind's xl (1280 px) instead of md (768 px). The nav, the toggle
    // and the mobile nav must name the same breakpoint: a toggle left at
    // md:hidden beside an xl:flex nav leaves a 768-1279 px band with no
    // navigation at all. jsdom applies no media queries, so this reads the
    // classes; the layout is measured in tests/e2e/header-breakpoint.journey.spec.ts.
    signInAs(makeUser());
    renderShell(full());
    await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
    const classesOf = (el: Element) => el.className.split(/\s+/);

    const desktop = screen.getByRole('navigation', { name: 'Primary' });
    expect(classesOf(desktop)).toContain('hidden');
    expect(classesOf(desktop)).toContain('xl:flex');
    expect(classesOf(desktop)).not.toContain('md:flex');

    const toggle = screen.getByTestId('mobile-nav-toggle');
    expect(classesOf(toggle)).toContain('xl:hidden');

    fireEvent.click(toggle);
    const mobile = screen.getByTestId('mobile-nav');
    expect(classesOf(mobile)).toContain('xl:hidden');

    // The prefix each element carries, read from the DOM rather than typed
    // again: one of the three drifting fails here whatever the others say.
    const prefixOf = (el: Element, utility: string) => {
      const variants = classesOf(el).filter((c) => c.endsWith(`:${utility}`));
      expect(variants).toHaveLength(1);
      return variants[0].split(':')[0];
    };
    expect(prefixOf(toggle, 'hidden')).toBe(prefixOf(desktop, 'flex'));
    expect(prefixOf(mobile, 'hidden')).toBe(prefixOf(desktop, 'flex'));
  });

  describe('module chrome', () => {
    it('keeps the primary nav free of module routes', () => {
      expect(NAV_ITEMS.map((i) => i.href)).toEqual([
        '/experiments',
        '/feature-flags',
        '/segments',
        '/admin/audit',
        '/admin',
        '/docs',
      ]);
    });

    it('offers a collapsed More group that is closed by default', async () => {
      signInAs(makeUser());
      renderShell();
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      const group = screen.getAllByTestId('more-nav-group')[0];
      expect(group).toBeInTheDocument();
      expect(group).not.toHaveAttribute('open');
      expect(group.querySelector('summary')).toHaveTextContent('More');
    });

    it('links to the modules guide in the core profile, and not in the full one', async () => {
      signInAs(makeUser());
      const view = renderShell();
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.getAllByTestId('nav-modules-docs')[0]).toHaveAttribute('href', MODULES_DOC_PATH);
      expect(screen.getAllByTestId('nav-modules-docs')[0]).toHaveTextContent('Modules');
      view.unmount();

      localStorage.clear();
      mockFetch.mockReset();
      signInAs(makeUser());
      renderShell(full());
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.queryByTestId('nav-modules-docs')).not.toBeInTheDocument();
    });

    it('closes the More group when a link in it is followed, and on navigation', async () => {
      // _app.tsx keeps one AppShell across client-side navigations, so an
      // uncontrolled <details> stayed open -- a panel over the next page --
      // after any link inside it was clicked.
      signInAs(makeUser());
      renderShell(full());
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      const group = screen.getAllByTestId('more-nav-group')[0] as HTMLDetailsElement;

      fireEvent.click(group.querySelector('summary')!);
      await waitFor(() => expect(group).toHaveAttribute('open'));

      fireEvent.click(screen.getAllByTestId('nav-workspaces')[0]);
      await waitFor(() => expect(group).not.toHaveAttribute('open'));

      // Opened again, then a navigation started elsewhere (browser back, say).
      fireEvent.click(group.querySelector('summary')!);
      await waitFor(() => expect(group).toHaveAttribute('open'));
      act(() => mockRouterEvents.emit('routeChangeStart'));
      await waitFor(() => expect(group).not.toHaveAttribute('open'));
    });

    it('closes the More group on an outside click and on Escape', async () => {
      signInAs(makeUser());
      renderShell(full());
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      const group = screen.getAllByTestId('more-nav-group')[0] as HTMLDetailsElement;

      fireEvent.click(group.querySelector('summary')!);
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0)); // let the queued toggle land
      });
      expect(group).toHaveAttribute('open');
      fireEvent.mouseDown(document.body);
      await waitFor(() => expect(group).not.toHaveAttribute('open'));

      fireEvent.click(group.querySelector('summary')!);
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
      expect(group).toHaveAttribute('open');
      fireEvent.keyDown(document, { key: 'Escape' });
      await waitFor(() => expect(group).not.toHaveAttribute('open'));
    });

    it('carries no Workspaces link in the core profile', async () => {
      signInAs(makeUser());
      renderShell();
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.queryByTestId('nav-workspaces')).not.toBeInTheDocument();
    });

    it('adds the Workspaces link once the workspaces module is installed', async () => {
      signInAs(makeUser());
      renderShell(full());
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.getAllByTestId('nav-workspaces')[0]).toHaveAttribute('href', '/workspaces');
    });

    it('shows no More group at all in a full profile with no routed module installed', async () => {
      // A full instance whose installed modules carry no dashboard route has
      // nothing to put in the group, and an empty disclosure is noise.
      // An SSO user: a local one's group would still hold Change password.
      signInAs(makeUser({ auth_provider: 'sso' }));
      renderShell(full({ modules: [MODULES.HIPAA] }));
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.queryByTestId('more-nav-group')).not.toBeInTheDocument();
    });

    it('hides the More group while the modules are still being probed', async () => {
      // The provider's initial state is core, so an unseeded shell would
      // otherwise paint the "Modules" guide link on a full instance and swap
      // it for the routes when the probe resolved.
      // An SSO user: a local one's group holds Change password from the start.
      mockFetch.mockImplementation((url: string) =>
        url.endsWith('/api/v1/modules')
          ? new Promise(() => {})
          : Promise.resolve(jsonResponse(200, makeUser({ auth_provider: 'sso' }))),
      );
      localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
      render(
        <ModulesProvider>
          <AuthProvider>
            <AppShell>
              <div data-testid="page-content">page</div>
            </AppShell>
          </AuthProvider>
        </ModulesProvider>,
      );
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.queryByTestId('more-nav-group')).not.toBeInTheDocument();
      expect(screen.queryByTestId('nav-modules-docs')).not.toBeInTheDocument();
    });

    it('shows no Modules guide link when the probe failed', async () => {
      // A failed probe also resolves to core, and the guide link says "this
      // instance runs the core profile" -- which the dashboard cannot know
      // when all that happened is that /api/v1/modules did not answer.
      // An SSO user: a local one's group holds Change password whatever the probe says.
      mockFetch.mockImplementation((url: string) =>
        url.endsWith('/api/v1/modules')
          ? Promise.reject(new TypeError('Failed to fetch'))
          : Promise.resolve(jsonResponse(200, makeUser({ auth_provider: 'sso' }))),
      );
      localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
      render(
        <ModulesProvider>
          <AuthProvider>
            <AppShell>
              <div data-testid="page-content">page</div>
            </AppShell>
          </AuthProvider>
        </ModulesProvider>,
      );
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      await waitFor(() =>
        expect(mockFetch).toHaveBeenCalledWith(
          expect.stringContaining('/api/v1/modules'),
          expect.anything(),
        ),
      );
      expect(screen.queryByTestId('nav-modules-docs')).not.toBeInTheDocument();
      expect(screen.queryByTestId('more-nav-group')).not.toBeInTheDocument();
    });

    it('installedModuleNav lists exactly the routes whose module is installed', () => {
      expect(installedModuleNav(CORE_PROFILE)).toEqual([]);
      expect(installedModuleNav(full())).toHaveLength(1);
      const both = full({ modules: [MODULES.WORKSPACES, MODULES.WAREHOUSE] });
      expect(installedModuleNav(both, makeUser())).toHaveLength(MODULE_NAV_ITEMS.length);
      expect(installedModuleNav(full({ modules: ['hipaa'] }))).toEqual([]);
      expect(installedModuleNav(full({ profile: 'core' }))).toHaveLength(1);
      expect(MODULE_NAV_ITEMS.map((i) => i.href)).toEqual(['/workspaces', '/warehouse']);
    });

    it('offers the Warehouse link to the roles the warehouse API lets read connections', () => {
      // The API refuses VIEWER every connection and source route, and counts
      // a superuser as ADMIN.
      const info = full({ modules: [MODULES.WAREHOUSE] });
      const hrefs = (user: UserMe | null) => installedModuleNav(info, user).map((i) => i.href);
      for (const role of ['ADMIN', 'DEVELOPER', 'ANALYST'] as const) {
        expect(hrefs(makeUser({ role, is_superuser: false }))).toEqual(['/warehouse']);
      }
      expect(hrefs(makeUser({ role: 'VIEWER', is_superuser: false }))).toEqual([]);
      expect(hrefs(makeUser({ role: 'VIEWER', is_superuser: true }))).toEqual(['/warehouse']);
      expect(hrefs(null)).toEqual([]);
      expect(installedModuleNav(info)).toEqual([]);
    });

    it('renders the Warehouse link for an ANALYST and not for a VIEWER', async () => {
      signInAs(makeUser({ role: 'ANALYST', is_superuser: false }));
      const view = renderShell(full({ modules: [MODULES.WAREHOUSE] }));
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.getAllByTestId('nav-warehouse')[0]).toHaveAttribute('href', '/warehouse');
      view.unmount();

      signInAs(makeUser({ role: 'VIEWER', is_superuser: false }));
      renderShell(full({ modules: [MODULES.WAREHOUSE] }));
      await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
      expect(screen.queryByTestId('nav-warehouse')).not.toBeInTheDocument();
    });
  });

  it('isNavActive matches exact and nested paths only', () => {
    expect(isNavActive('/experiments', '/experiments')).toBe(true);
    expect(isNavActive('/experiments/[id]', '/experiments')).toBe(true);
    expect(isNavActive('/experiments-archive', '/experiments')).toBe(false);
    expect(isNavActive('/feature-flags', '/experiments')).toBe(false);
    expect(NAV_ITEMS.map((i) => i.label)).toEqual([
      'Experiments',
      'Feature Flags',
      'Segments',
      'Audit Log',
      'Admin',
      'Docs',
    ]);
  });
});
