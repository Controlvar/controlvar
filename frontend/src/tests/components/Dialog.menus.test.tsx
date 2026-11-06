/**
 * Escape with a menu open behind a Dialog closes the Dialog only (#369).
 *
 * The site header's phone menu and the shell's "More" group each listen for
 * Escape on `document`, in the bubble phase. A Dialog listens on `document` in
 * the capture phase and stops the event there, so neither menu sees an Escape
 * that a Dialog took. Escape is dispatched on `document` itself (where the
 * order of capture and bubble listeners at the target decides it), on a
 * control inside the dialog and on an element inside the page.
 */
import React, { useState } from 'react';
import { fireEvent, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Dialog } from '@/components/ui/Dialog';
import { SiteHeader } from '@/components/SiteHeader';
import { AppShell } from '@/components/AppShell';
import { AuthProvider } from '@/contexts/AuthContext';
import { ModulesProvider, __resetModulesCache } from '@/contexts/ModulesContext';
import { MODULES } from '@/services/modules';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';

const routerListeners: Record<string, Array<() => void>> = {};
jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: jest.fn().mockResolvedValue(true),
    push: jest.fn(),
    pathname: '/experiments',
    asPath: '/experiments',
    query: {},
    isReady: true,
    events: {
      on: (event: string, cb: () => void) => {
        (routerListeners[event] ??= []).push(cb);
      },
      off: (event: string, cb: () => void) => {
        routerListeners[event] = (routerListeners[event] ?? []).filter((c) => c !== cb);
      },
    },
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

const ADMIN: UserMe = {
  id: 'u-1',
  email: 'admin@demo.com',
  username: 'admin',
  full_name: 'Demo Admin',
  role: 'ADMIN',
  is_superuser: true,
  is_active: true,
  auth_provider: 'local',
};

function json(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

/** A page control that opens a Dialog. */
function PageWithDialog() {
  const [open, setOpen] = useState(false);
  return (
    <div data-testid="page-content">
      <button type="button" onClick={() => setOpen(true)}>
        Open dialog
      </button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Rename flag">
        <button type="button">Cancel</button>
      </Dialog>
    </div>
  );
}

beforeEach(() => {
  __resetModulesCache();
  mockFetch.mockReset();
  localStorage.clear();
});

describe('Escape with the site header menu open', () => {
  it('closes the Dialog only, and focus goes back to the dialog’s opener', async () => {
    const user = userEvent.setup();
    renderInAppRoot(
      <>
        <SiteHeader />
        <PageWithDialog />
      </>,
    );
    const menuButton = screen.getByRole('button', { name: 'Menu' });
    await user.click(menuButton);
    expect(menuButton).toHaveAttribute('aria-expanded', 'true');
    const opener = screen.getByRole('button', { name: 'Open dialog' });

    const targets: Array<() => Document | Element> = [
      () => document,
      () => screen.getByRole('button', { name: 'Cancel' }),
      () => screen.getByTestId('site-header'),
    ];
    for (const target of targets) {
      await user.click(opener);
      expect(screen.getByRole('dialog', { name: 'Rename flag' })).toBeInTheDocument();
      fireEvent.keyDown(target(), { key: 'Escape' });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(menuButton).toHaveAttribute('aria-expanded', 'true');
      expect(opener).toHaveFocus();
    }

    // With no dialog open, the menu's own Escape still works.
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(menuButton).toHaveAttribute('aria-expanded', 'false');
  });
});

describe('Escape with the shell’s More group open', () => {
  it('closes the Dialog only, and focus goes back to the dialog’s opener', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockResolvedValueOnce(json(200, ADMIN));
    renderInAppRoot(
      <ModulesProvider initial={{ profile: 'full', modules: [MODULES.WORKSPACES], version: '1.0.0' }}>
        <AuthProvider>
          <AppShell>
            <PageWithDialog />
          </AppShell>
        </AuthProvider>
      </ModulesProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('user-menu')).toBeInTheDocument());
    const group = screen.getAllByTestId('more-nav-group')[0] as HTMLDetailsElement;
    fireEvent.click(group.querySelector('summary') as HTMLElement);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0)); // the queued toggle event
    });
    expect(group).toHaveAttribute('open');
    const opener = screen.getByRole('button', { name: 'Open dialog' });

    const targets: Array<() => Document | Element> = [
      () => document,
      () => screen.getByRole('button', { name: 'Cancel' }),
      () => screen.getByTestId('page-content'),
    ];
    for (const target of targets) {
      // From the keyboard: a pointer press outside the group would close it.
      opener.focus();
      fireEvent.click(opener);
      expect(screen.getByRole('dialog', { name: 'Rename flag' })).toBeInTheDocument();
      fireEvent.keyDown(target(), { key: 'Escape' });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(group).toHaveAttribute('open');
      expect(opener).toHaveFocus();
    }

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(group).not.toHaveAttribute('open'));
  });
});
