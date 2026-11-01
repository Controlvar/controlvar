/**
 * `/workspaces/invites/[token]` (#265): only the account the invite was sent
 * to sees the Accept button. Anyone else sees who it was sent to (masked) and
 * can switch account; a 403 `invite_email_mismatch` from the API shows the
 * same state. The card names the workspace and the inviter, an accepted
 * invitation shows as used, and a bad link is told apart from a failed load.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { ApiError, UserMe } from '@/services/api';
import AcceptInvitePage from '@modules/pages/workspaces/invites/[token]';
import { workspaceService } from '@modules/services/workspaces';

const mockReplace = jest.fn().mockResolvedValue(true);
const mockPush = jest.fn().mockResolvedValue(true);

// One router object for every render, as Next.js gives a page between
// navigations, so a refetch can only come from the page's own dependencies.
const mockRouter = {
  replace: mockReplace,
  push: mockPush,
  pathname: '/workspaces/invites/[token]',
  asPath: '/workspaces/invites/tok123',
  query: { token: 'tok123' },
  isReady: true,
};

jest.mock('next/router', () => ({
  useRouter: () => mockRouter,
}));

jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});

const mockLogout = jest.fn().mockResolvedValue(undefined);
let mockUser: Partial<UserMe> | null = null;

jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => ({ user: mockUser, logout: mockLogout }),
  useAuth: () => ({ user: mockUser, logout: mockLogout }),
}));

jest.mock('@modules/services/workspaces', () => ({
  workspaceService: { getInvite: jest.fn(), acceptInvite: jest.fn() },
}));

const getInvite = workspaceService.getInvite as jest.Mock;
const acceptInvite = workspaceService.acceptInvite as jest.Mock;

const INVITED = 'alice@example.com';
const KELVIN = 'K';

function signedInAs(email: string | null, id = 'u-1') {
  mockUser = {
    id,
    email: email as string,
    username: 'someone',
    role: 'VIEWER',
    is_superuser: false,
    is_active: true,
  };
}

function renderPage() {
  return render(
    <ModulesProvider initial={{ profile: 'full', modules: ['workspaces'], version: 'test' }}>
      <AcceptInvitePage />
    </ModulesProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  getInvite.mockResolvedValue({
    token: 'tok123',
    workspace_name: 'Growth',
    inviter_username: 'owner',
    email: INVITED,
    role: 'DEVELOPER',
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    accepted_at: null,
  });
});

/** An invitation as the API returns it to the invitee, with overrides. */
function inviteFrom(overrides: Record<string, unknown> = {}) {
  return {
    token: 'tok123',
    workspace_name: 'Growth',
    inviter_username: 'owner',
    email: INVITED,
    role: 'DEVELOPER',
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    accepted_at: null,
    ...overrides,
  };
}

const YESTERDAY = () => new Date(Date.now() - 86_400_000).toISOString();

describe('/workspaces/invites/[token]', () => {
  it('offers Accept to the invited address, whatever its case', async () => {
    signedInAs('  Alice@Example.COM ');
    renderPage();

    expect(await screen.findByRole('button', { name: 'Accept Invitation' })).toBeInTheDocument();
    expect(screen.queryByText('This invitation is for a different account')).not.toBeInTheDocument();
  });

  it.each([
    ['a different address', 'bob@other.com'],
    ['a plus alias of it', 'alice+x@example.com'],
  ])('shows the mismatch state, with no Accept button, to %s', async (_label, email) => {
    signedInAs(email);
    renderPage();

    expect(
      await screen.findByRole('heading', { name: 'This invitation is for a different account' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Accept Invitation' })).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(
      `It was sent to a•••@example.com, and you are signed in as ${email}. ` +
        `Sign in with the invited address to accept it, or ask a workspace admin to invite ${email}.`,
    );
    expect(document.body.textContent).not.toContain(INVITED);
  });

  it('treats a non-ASCII character that lower-cases to ASCII as a different address', async () => {
    const lookAlike = `${KELVIN}im@example.com`;
    getInvite.mockResolvedValue({
      token: 'tok123',
      workspace_name: 'Growth',
      inviter_username: 'owner',
      email: 'kim@example.com',
      role: 'DEVELOPER',
      expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    });
    expect(lookAlike.toLowerCase()).toBe('kim@example.com');
    signedInAs(lookAlike);
    renderPage();

    expect(
      await screen.findByRole('heading', { name: 'This invitation is for a different account' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Accept Invitation' })).not.toBeInTheDocument();
  });

  it('shows the mismatch state to an account with no email address', async () => {
    signedInAs(null);
    renderPage();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'It was sent to a•••@example.com, and the account you are signed in with has no email address.',
    );
    expect(screen.queryByRole('button', { name: 'Accept Invitation' })).not.toBeInTheDocument();
  });

  it('"Use a different account" signs out and returns to this link after sign-in', async () => {
    signedInAs('bob@other.com');
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Use a different account' }));

    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        `/login?next=${encodeURIComponent('/workspaces/invites/tok123')}`,
      ),
    );
    expect(mockLogout).toHaveBeenCalledTimes(1);
    // The logout names the same page, so the guard's redirect agrees (#1006).
    expect(mockLogout).toHaveBeenCalledWith({ next: '/workspaces/invites/tok123' });
    expect(acceptInvite).not.toHaveBeenCalled();
  });

  it('"Use a different account" still returns to sign-in when sign-out fails', async () => {
    signedInAs('bob@other.com');
    mockLogout.mockRejectedValueOnce(new Error('network down'));
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Use a different account' }));

    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        `/login?next=${encodeURIComponent('/workspaces/invites/tok123')}`,
      ),
    );
  });

  it('shows the mismatch state when the API answers 403 invite_email_mismatch', async () => {
    signedInAs(INVITED);
    acceptInvite.mockRejectedValue(
      new ApiError({
        status: 403,
        detail: {
          code: 'invite_email_mismatch',
          message: 'This invite was sent to a different email address.',
        },
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Accept Invitation' }));

    expect(
      await screen.findByRole('heading', { name: 'This invitation is for a different account' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Accept Invitation' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Use a different account' })).toBeInTheDocument();
  });

  it('treats an address the API masked as a different address, even one spelled the same', async () => {
    // The API masks the address for anyone but the invitee. An account whose
    // own address is literally the masked string is still not the invitee.
    const masked = 'a•••@example.com';
    getInvite.mockResolvedValue({
      token: 'tok123',
      workspace_name: 'Growth',
      inviter_username: 'owner',
      email: masked,
      role: 'DEVELOPER',
      expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    });
    signedInAs(masked);
    renderPage();

    expect(
      await screen.findByRole('heading', { name: 'This invitation is for a different account' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Accept Invitation' })).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(`It was sent to ${masked}`);
  });

  it('fetches the invitation again when the signed-in account changes', async () => {
    const invite = {
      token: 'tok123',
      workspace_name: 'Growth',
      inviter_username: 'owner',
      role: 'DEVELOPER',
      expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    };
    // Another account gets the masked address; the invitee gets it in full.
    getInvite.mockImplementation(async () => ({
      ...invite,
      email: mockUser?.email === INVITED ? INVITED : 'a•••@example.com',
    }));
    signedInAs('bob@other.com', 'u-bob');
    const view = renderPage();

    expect(
      await screen.findByRole('heading', { name: 'This invitation is for a different account' }),
    ).toBeInTheDocument();
    expect(getInvite).toHaveBeenCalledTimes(1);

    signedInAs(INVITED, 'u-alice');
    view.rerender(
      <ModulesProvider initial={{ profile: 'full', modules: ['workspaces'], version: 'test' }}>
        <AcceptInvitePage />
      </ModulesProvider>,
    );

    expect(await screen.findByRole('button', { name: 'Accept Invitation' })).toBeInTheDocument();
    expect(screen.getByText(INVITED)).toBeInTheDocument();
    expect(screen.queryByText('This invitation is for a different account')).not.toBeInTheDocument();
    expect(getInvite).toHaveBeenCalledTimes(2);
  });

  it('shows any other accept failure as an alert beside the Accept button', async () => {
    signedInAs(INVITED);
    acceptInvite.mockRejectedValue(
      new ApiError({ status: 409, detail: 'This invite has already been accepted.' }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Accept Invitation' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This invite has already been accepted.',
    );
    expect(screen.getByRole('button', { name: 'Accept Invitation' })).toBeInTheDocument();
  });
});

describe('/workspaces/invites/[token]: what the card says', () => {
  it('names the inviter and the workspace on a valid invitation', async () => {
    signedInAs(INVITED);
    renderPage();

    await screen.findByRole('button', { name: 'Accept Invitation' });
    const sentence = screen.getByText((_content, el) =>
      el?.tagName === 'P' && /has invited you/.test(el.textContent ?? ''),
    );
    expect(sentence.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'owner has invited you to join the workspace Growth as DEVELOPER.',
    );
    expect(document.body.textContent).not.toMatch(/undefined|null/);
  });

  it('says "You\'ve been invited" when the API names no inviter', async () => {
    signedInAs(INVITED);
    getInvite.mockResolvedValue(inviteFrom({ inviter_username: null }));
    renderPage();

    await screen.findByRole('button', { name: 'Accept Invitation' });
    const sentence = screen.getByText((_content, el) =>
      el?.tagName === 'P' && /been invited/.test(el.textContent ?? ''),
    );
    expect(sentence.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      "You've been invited to join the workspace Growth as DEVELOPER.",
    );
    expect(document.body.textContent).not.toMatch(/undefined|null|has invited you/);
  });

  it.each([
    ['to the invitee', INVITED, {}],
    ['to another account', 'bob@other.com', { email: 'a•••@example.com', inviter_username: null }],
    ['when it has also expired', INVITED, { expires_at: YESTERDAY() }],
  ])('shows an accepted invitation as used %s', async (_label, email, overrides) => {
    signedInAs(email);
    getInvite.mockResolvedValue(
      inviteFrom({ accepted_at: new Date().toISOString(), ...overrides }),
    );
    renderPage();

    expect(
      await screen.findByRole('heading', { name: 'Invitation already used' }),
    ).toBeInTheDocument();
    expect(
      screen.getByText((_content, el) =>
        el?.tagName === 'P' && /already been accepted/.test(el.textContent ?? ''),
      ).textContent?.replace(/\s+/g, ' ').trim(),
    ).toBe(
      "This invitation to Growth has already been accepted. If that wasn't you, ask a workspace admin.",
    );
    expect(screen.getByRole('link', { name: 'Go to Workspaces' })).toHaveAttribute(
      'href',
      '/workspaces',
    );
    expect(screen.queryByRole('button', { name: 'Accept Invitation' })).not.toBeInTheDocument();
    expect(screen.queryByText("You're invited!")).not.toBeInTheDocument();
    expect(screen.queryByText('Invitation Expired')).not.toBeInTheDocument();
    expect(screen.queryByText('This invitation is for a different account')).not.toBeInTheDocument();
  });

  it('fills in the workspace name on an expired invitation', async () => {
    signedInAs(INVITED);
    getInvite.mockResolvedValue(inviteFrom({ expires_at: YESTERDAY() }));
    renderPage();

    expect(await screen.findByRole('heading', { name: 'Invitation Expired' })).toBeInTheDocument();
    expect(
      screen.getByText((_content, el) =>
        el?.tagName === 'P' && /has expired/.test(el.textContent ?? ''),
      ).textContent?.replace(/\s+/g, ' ').trim(),
    ).toBe('This invitation to Growth has expired.');
  });

  it('says a link that does not exist is not valid (404)', async () => {
    signedInAs(INVITED);
    getInvite.mockRejectedValue(new ApiError({ status: 404, detail: 'Invite token not found.' }));
    renderPage();

    expect(await screen.findByRole('heading', { name: 'Invitation Not Found' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(
      "This invitation link isn't valid. Check you copied all of it, or ask for a new one.",
    );
    expect(document.body.textContent).not.toContain('already been used');
  });

  it.each([
    ['a server error', new ApiError({ status: 500, detail: 'Server Error' })],
    ['a network failure', new ApiError({ status: 0, detail: null })],
    ['a non-API error', new Error('boom')],
  ])('says the invitation could not be loaded on %s', async (_label, err) => {
    signedInAs(INVITED);
    getInvite.mockRejectedValue(err);
    renderPage();

    expect(
      await screen.findByRole('heading', { name: 'Invitation unavailable' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(
      "We couldn't load this invitation. Try again in a moment.",
    );
    expect(screen.queryByText('Invitation Not Found')).not.toBeInTheDocument();
  });

  it('announces loading, hides decorative icons, and uses no low-contrast grey', async () => {
    signedInAs(INVITED);
    let resolve: (v: unknown) => void = () => undefined;
    getInvite.mockReturnValue(new Promise((r) => (resolve = r)));
    const view = renderPage();

    expect(screen.getByRole('status')).toHaveTextContent('Loading invitation...');
    const spinner = screen.getByRole('status').querySelector('.animate-spin');
    expect(spinner).toHaveAttribute('aria-hidden', 'true');

    resolve(inviteFrom());
    await screen.findByRole('button', { name: 'Accept Invitation' });

    const svgs = view.container.querySelectorAll('svg');
    expect(svgs.length).toBeGreaterThan(0);
    svgs.forEach((svg) => expect(svg).toHaveAttribute('aria-hidden', 'true'));
    expect(view.container.innerHTML).not.toContain('text-slate-400');
  });

  it('announces the accepted state', async () => {
    signedInAs(INVITED);
    acceptInvite.mockResolvedValue({ workspace_id: 'ws-1' });
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Accept Invitation' }));

    expect(await screen.findByRole('status')).toHaveTextContent('Invitation Accepted!');
  });
});
