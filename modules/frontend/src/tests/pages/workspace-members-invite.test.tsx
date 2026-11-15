/**
 * The members page's "Invite Member" dialog. No email is sent, so after
 * creating an invitation the dialog shows its link, with a Copy link button,
 * and stays open until the admin closes it.
 */
import React from 'react';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
// The dialog makes `#__next` inert and portals beside it, so the page is
// mounted the way Next mounts it (#369).
import { renderInAppRoot as render } from '@/tests/helpers/renderInAppRoot';
import { ModulesProvider } from '@/contexts/ModulesContext';
import WorkspaceMembersPage from '@modules/pages/workspaces/[id]/members';
import { workspaceService } from '@modules/services/workspaces';

jest.mock('next/router', () => ({
  useRouter: () => ({
    pathname: '/workspaces/[id]/members',
    asPath: '/workspaces/ws-1/members',
    query: { id: 'ws-1' },
    isReady: true,
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));

jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});

jest.mock('@modules/services/workspaces', () => ({
  workspaceService: {
    get: jest.fn(),
    listMembers: jest.fn(),
    sendInvite: jest.fn(),
    updateMember: jest.fn(),
    removeMember: jest.fn(),
  },
}));

const sendInvite = workspaceService.sendInvite as jest.Mock;

const TOKEN = 'a1b2c3';
const EXPIRES = '2026-10-05T12:00:00Z';
const CREATED = {
  token: TOKEN,
  workspace_name: 'Growth',
  inviter_username: 'owner',
  email: 'alice@example.com',
  role: 'DEVELOPER',
  expires_at: EXPIRES,
  accepted_at: null,
};

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', { value, configurable: true, writable: true });
}

beforeEach(() => {
  jest.clearAllMocks();
  (workspaceService.get as jest.Mock).mockResolvedValue({ id: 'ws-1', name: 'Growth' });
  (workspaceService.listMembers as jest.Mock).mockResolvedValue([]);
  sendInvite.mockResolvedValue(CREATED);
});

afterEach(() => {
  jest.useRealTimers();
  if (originalClipboard) {
    Object.defineProperty(navigator, 'clipboard', originalClipboard);
  } else {
    delete (navigator as { clipboard?: unknown }).clipboard;
  }
});

function renderPage() {
  return render(
    <ModulesProvider initial={{ profile: 'full', modules: ['workspaces'], version: 'test' }}>
      <WorkspaceMembersPage />
    </ModulesProvider>,
  );
}

async function openDialog() {
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: '+ Invite Member' }));
  return screen.getByRole('dialog', { name: 'Invite Member' });
}

async function createInvitation() {
  await openDialog();
  fireEvent.change(screen.getByLabelText(/Email Address/), {
    target: { value: 'alice@example.com' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Create invitation' }));
  return screen.findByRole('textbox', { name: 'Invitation link' });
}

const LINK = `${window.location.origin}/workspaces/invites/${TOKEN}`;

describe('Invite Member dialog', () => {
  it('shows the invitation link after creating it, focused and selected', async () => {
    const link = (await createInvitation()) as HTMLInputElement;

    expect(sendInvite).toHaveBeenCalledWith('ws-1', 'alice@example.com', 'DEVELOPER');
    expect(link.value).toBe(LINK);
    expect(link).toHaveAttribute('readonly');
    expect(link).toHaveFocus();
    expect(link.selectionStart).toBe(0);
    expect(link.selectionEnd).toBe(LINK.length);
  });

  it('encodes the token into the link', async () => {
    sendInvite.mockResolvedValue({ ...CREATED, token: 'a/b?c' });
    const link = (await createInvitation()) as HTMLInputElement;

    expect(link.value).toBe(`${window.location.origin}/workspaces/invites/a%2Fb%3Fc`);
  });

  it('says the invitation was created, for whom and until when, and never "sent"', async () => {
    await openDialog();
    expect(screen.getByRole('button', { name: 'Create invitation' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /send/i })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Email Address/), {
      target: { value: 'alice@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create invitation' }));
    await screen.findByRole('textbox', { name: 'Invitation link' });
    const date = new Date(EXPIRES).toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    });
    expect(
      screen.getByText(/^Invitation created\./).textContent?.replace(/\s+/g, ' ').trim(),
    ).toBe(
      `Invitation created. Send this link to alice@example.com. It works only for that address and expires on ${date}.`,
    );
    expect(document.body.textContent).not.toMatch(/sent successfully|invitation sent/i);
    expect(screen.getByLabelText(/Email Address/)).toBeDisabled();
    expect(screen.getByLabelText(/Role/)).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Create invitation' })).not.toBeInTheDocument();
    // The x in the header and the footer button.
    expect(screen.getAllByRole('button', { name: 'Close' })).toHaveLength(2);
  });

  it('stays open after creating, until the admin closes it', async () => {
    jest.useFakeTimers();
    await createInvitation();

    act(() => {
      jest.advanceTimersByTime(5000);
    });

    expect(screen.getByRole('dialog', { name: 'Invite Member' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Invitation link' })).toBeInTheDocument();
  });

  it('shows the API error as an alert, with no link', async () => {
    sendInvite.mockRejectedValue(new Error('Requires ADMIN role or above.'));
    await openDialog();
    fireEvent.change(screen.getByLabelText(/Email Address/), {
      target: { value: 'alice@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create invitation' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Requires ADMIN role or above.');
    expect(screen.queryByRole('textbox', { name: 'Invitation link' })).not.toBeInTheDocument();
  });

  describe('Copy link', () => {
    it('copies the link and says so', async () => {
      const writeText = jest.fn().mockResolvedValue(undefined);
      setClipboard({ writeText });
      await createInvitation();

      fireEvent.click(screen.getByRole('button', { name: 'Copy link' }));

      await waitFor(() => expect(screen.getByText('Link copied')).toBeInTheDocument());
      expect(writeText).toHaveBeenCalledWith(LINK);
    });

    it('falls back to selecting the link when the browser refuses', async () => {
      setClipboard({ writeText: jest.fn().mockRejectedValue(new Error('NotAllowedError')) });
      const link = (await createInvitation()) as HTMLInputElement;
      link.blur();

      fireEvent.click(screen.getByRole('button', { name: 'Copy link' }));

      expect(
        await screen.findByText("Couldn't copy the link. Select it and copy it instead."),
      ).toBeInTheDocument();
      expect(screen.queryByText('Link copied')).not.toBeInTheDocument();
      expect(link).toHaveFocus();
      expect(link.selectionEnd! - link.selectionStart!).toBe(LINK.length);
    });

    it('falls back to selecting the link when there is no clipboard (plain http)', async () => {
      setClipboard(undefined);
      const link = (await createInvitation()) as HTMLInputElement;
      link.blur();

      fireEvent.click(screen.getByRole('button', { name: 'Copy link' }));

      expect(
        await screen.findByText("Couldn't copy the link. Select it and copy it instead."),
      ).toBeInTheDocument();
      expect(link).toHaveFocus();
      expect(link.selectionEnd! - link.selectionStart!).toBe(LINK.length);
    });
  });

  describe('accessibility', () => {
    it('is a modal dialog named by its heading', async () => {
      const dialog = await openDialog();

      expect(dialog).toHaveAttribute('aria-modal', 'true');
      expect(dialog).toHaveAccessibleName('Invite Member');
    });

    it('labels its fields, describes the email field, and focuses it first', async () => {
      await openDialog();

      const email = screen.getByRole('textbox', { name: /Email Address/ });
      expect(email).toHaveFocus();
      expect(email).toHaveAccessibleDescription(
        'Only someone signed in with this address can accept. Use the address they sign in with; for single sign-on, their work email.',
      );
      const role = screen.getByRole('combobox', { name: /Role/ });
      expect(role).toHaveAccessibleDescription(
        'Owner role can only be transferred, not assigned via invite.',
      );
    });

    it('closes on Escape', async () => {
      await openDialog();

      fireEvent.keyDown(document, { key: 'Escape' });

      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });

    it('announces the created invitation', async () => {
      await createInvitation();

      const statuses = screen.getAllByRole('status');
      expect(statuses.some((s) => /^Invitation created\./.test(s.textContent ?? ''))).toBe(true);
    });

    it('uses no low-contrast grey (the created state shows every part)', async () => {
      await createInvitation();
      const dialog = screen.getByRole('dialog', { name: 'Invite Member' });
      expect(dialog.innerHTML).not.toContain('text-slate-400');
    });
  });
});
