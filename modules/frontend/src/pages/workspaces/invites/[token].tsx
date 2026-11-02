import React, { useState, useEffect } from 'react';
import { withModule } from '@/components/ModuleNotice';
import { MODULES } from '@/services/modules';
import { PageTitle } from '@/components/PageTitle';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { isApiError, loginPathWithNext } from '@/services/api';
import { WorkspaceInvite, workspaceService } from '@modules/services/workspaces';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

/** The typed code of the API's 403 when the signed-in account is not the invitee. */
const INVITE_EMAIL_MISMATCH_CODE = 'invite_email_mismatch';

function isTokenExpired(invite: WorkspaceInvite): boolean {
  return parseApiTimestamp(invite.expires_at) < new Date();
}

/**
 * An address as the API compares it: trimmed, ASCII only, lower-cased.
 * `null` when there is none or it is not ASCII (the API refuses those).
 */
function comparableEmail(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  // eslint-disable-next-line no-control-regex
  if (!trimmed || !/^[\x00-\x7f]*$/.test(trimmed)) return null;
  return trimmed.toLowerCase();
}

/**
 * Whether the signed-in address is the invited one. Presentation only: the
 * API decides, and a 403 from it shows the same state.
 */
function inviteEmailMatches(
  invited: string | null | undefined,
  signedIn: string | null | undefined,
): boolean {
  const a = comparableEmail(invited);
  const b = comparableEmail(signedIn);
  return a !== null && b !== null && a === b;
}

/**
 * `alice@example.com` -> `a•••@example.com`, for display on this page.
 * The API masks it too for anyone but the invitee; masking a masked address
 * returns it unchanged.
 */
function maskEmail(email: string): string {
  const trimmed = email.trim();
  const at = trimmed.lastIndexOf('@');
  if (at <= 0) return '•••';
  return `${Array.from(trimmed)[0]}•••${trimmed.slice(at)}`;
}

function AcceptInvitePage() {
  const router = useRouter();
  const { token } = router.query as { token: string };
  const auth = useOptionalAuth();
  const user = auth?.user ?? null;

  const [invite, setInvite] = useState<WorkspaceInvite | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  // A 404 means the link itself is wrong; anything else may pass on a retry.
  const [fetchError, setFetchError] = useState<'not_found' | 'unavailable' | null>(null);
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState(false);
  const [serverMismatch, setServerMismatch] = useState(false);
  const [switchingAccount, setSwitchingAccount] = useState(false);

  // Decided from the signed-in account when it is known; a 403 with the typed
  // code from the API decides it too (for example after an email change in
  // another tab).
  const mismatch =
    serverMismatch || (!!invite && !!user && !inviteEmailMatches(invite.email, user.email));

  // The API returns the invited address in full only to the invitee, so the
  // preview is fetched again whenever the signed-in account changes.
  const userId = user?.id ?? null;

  useEffect(() => {
    if (!token) return;
    let current = true;

    // Authentication is enforced by <RequireAuth> in _app.tsx (this route is
    // protected), which redirects anonymous visitors to /login?next=<this page>.
    workspaceService
      .getInvite(token)
      .then((result) => {
        if (!current) return;
        setInvite(result);
        setFetchError(null);
      })
      .catch((err) => {
        if (!current) return;
        setFetchError(isApiError(err) && err.status === 404 ? 'not_found' : 'unavailable');
      })
      .finally(() => {
        if (current) setIsLoading(false);
      });
    return () => {
      current = false;
    };
  }, [token, router, userId]);

  // A 403 from accepting was about the previous account.
  useEffect(() => {
    setServerMismatch(false);
  }, [userId]);

  async function handleAccept() {
    if (!token) return;
    setAccepting(true);
    setAcceptError(null);
    try {
      const result = await workspaceService.acceptInvite(token);
      setAccepted(true);
      setTimeout(() => {
        router.push(`/workspaces/${result.workspace_id}`);
      }, 1500);
    } catch (err) {
      if (isApiError(err) && err.code === INVITE_EMAIL_MISMATCH_CODE) {
        setServerMismatch(true);
      } else {
        setAcceptError(err instanceof Error ? err.message : 'Failed to accept invitation');
      }
    } finally {
      setAccepting(false);
    }
  }

  async function handleUseDifferentAccount() {
    setSwitchingAccount(true);
    const next = `/workspaces/invites/${encodeURIComponent(token ?? '')}`;
    try {
      // Named in the logout as well, so the protected-page guard sends this
      // tab to the same sign-in path as the navigation below (#1006).
      await auth?.logout({ next }).catch(() => undefined);
    } finally {
      void router.replace(loginPathWithNext(next));
    }
  }

  return (
    <>
      <PageTitle title="Accept Invitation" />

      <div className="flex-1 bg-slate-50 flex flex-col">
        {/* Simple header */}

        <div className="flex-1 flex items-center justify-center px-4 py-12">
          <div className="w-full max-w-md">
            {/* Loading */}
            {isLoading && (
              <div
                role="status"
                className="bg-white rounded-xl border border-slate-200 p-10 text-center"
              >
                <div
                  aria-hidden="true"
                  className="inline-block w-8 h-8 border-2 border-blue-600 border-t-transparent rounded-full animate-spin mb-4"
                />
                <p className="text-slate-500 text-sm">Loading invitation...</p>
              </div>
            )}

            {/* Fetch error: a link that does not exist, or any other failure */}
            {!isLoading && fetchError && (
              <div className="bg-white rounded-xl border border-red-200 p-8 text-center">
                <div className="w-14 h-14 bg-red-100 rounded-full flex items-center justify-center mx-auto mb-4">
                  <svg aria-hidden="true" className="w-7 h-7 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                  </svg>
                </div>
                <h2 className="text-lg font-semibold text-slate-900 mb-2">
                  {fetchError === 'not_found' ? 'Invitation Not Found' : 'Invitation unavailable'}
                </h2>
                <p role="alert" className="text-sm text-slate-500 mb-6">
                  {fetchError === 'not_found'
                    ? "This invitation link isn't valid. Check you copied all of it, or ask for a new one."
                    : "We couldn't load this invitation. Try again in a moment."}
                </p>
                <Link
                  href="/workspaces"
                  className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
                >
                  Go to Workspaces
                </Link>
              </div>
            )}

            {/* Already accepted: wins over expiry and over a different account */}
            {!isLoading && !fetchError && invite && invite.accepted_at && (
              <div className="bg-white rounded-xl border border-slate-200 p-8 text-center">
                <h2 className="text-lg font-semibold text-slate-900 mb-2">
                  Invitation already used
                </h2>
                <p className="text-sm text-slate-600 mb-6">
                  This invitation to{' '}
                  <span className="font-medium">{invite.workspace_name}</span> has already been
                  accepted. If that wasn&apos;t you, ask a workspace admin.
                </p>
                <Link
                  href="/workspaces"
                  className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
                >
                  Go to Workspaces
                </Link>
              </div>
            )}

            {/* Expired invite */}
            {!isLoading && !fetchError && invite && !invite.accepted_at && isTokenExpired(invite) && (
              <div className="bg-white rounded-xl border border-amber-200 p-8 text-center">
                <div className="w-14 h-14 bg-amber-100 rounded-full flex items-center justify-center mx-auto mb-4">
                  <svg aria-hidden="true" className="w-7 h-7 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                </div>
                <h2 className="text-lg font-semibold text-slate-900 mb-2">Invitation Expired</h2>
                <p className="text-sm text-slate-500 mb-2">
                  This invitation to{' '}
                  <span className="font-medium">{invite.workspace_name}</span> has expired.
                </p>
                <p className="text-xs text-slate-500 mb-6">
                  Expired on {parseApiTimestamp(invite.expires_at).toLocaleDateString()}
                </p>
                <p className="text-sm text-slate-500 mb-6">
                  Please ask a workspace admin to send you a new invitation.
                </p>
                <Link
                  href="/workspaces"
                  className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
                >
                  Go to Workspaces
                </Link>
              </div>
            )}

            {/* Signed in as someone other than the invitee */}
            {!isLoading && !fetchError && invite && !invite.accepted_at && !isTokenExpired(invite) && mismatch && (
              <div className="bg-white rounded-xl border border-amber-200 p-8 text-center">
                <h2 className="text-lg font-semibold text-slate-900 mb-2">
                  This invitation is for a different account
                </h2>
                <p role="alert" className="text-sm text-slate-600 mb-6">
                  It was sent to <span className="font-medium">{maskEmail(invite.email)}</span>
                  {user?.email ? (
                    <>
                      , and you are signed in as{' '}
                      <span className="font-medium">{user.email}</span>. Sign in with the invited
                      address to accept it, or ask a workspace admin to invite {user.email}.
                    </>
                  ) : (
                    <>
                      , and the account you are signed in with has no email address. Sign in with
                      the invited address to accept it, or ask a workspace admin to invite you.
                    </>
                  )}
                </p>
                <button
                  type="button"
                  onClick={handleUseDifferentAccount}
                  disabled={switchingAccount}
                  className="w-full px-4 py-2.5 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  Use a different account
                </button>
              </div>
            )}

            {/* Valid invite */}
            {!isLoading && !fetchError && invite && !invite.accepted_at && !isTokenExpired(invite) && !mismatch && (
              <div className="bg-white rounded-xl border border-slate-200 p-8 text-center">
                {accepted ? (
                  <div role="status">
                    <div className="w-14 h-14 bg-green-100 rounded-full flex items-center justify-center mx-auto mb-4">
                      <svg aria-hidden="true" className="w-7 h-7 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                      </svg>
                    </div>
                    <h2 className="text-lg font-semibold text-slate-900 mb-2">
                      Invitation Accepted!
                    </h2>
                    <p className="text-sm text-slate-500">
                      Redirecting you to the workspace...
                    </p>
                  </div>
                ) : (
                  <>
                    <div className="w-14 h-14 bg-blue-100 rounded-full flex items-center justify-center mx-auto mb-4">
                      <svg aria-hidden="true" className="w-7 h-7 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
                      </svg>
                    </div>

                    <h2 className="text-xl font-bold text-slate-900 mb-1">
                      You&apos;re invited!
                    </h2>
                    <p className="text-sm text-slate-500 mb-5">
                      {invite.inviter_username ? (
                        <>
                          <span className="font-medium">{invite.inviter_username}</span> has
                          invited you
                        </>
                      ) : (
                        <>You&apos;ve been invited</>
                      )}{' '}
                      to join the workspace{' '}
                      <span className="font-medium text-slate-800">{invite.workspace_name}</span> as{' '}
                      <span className="font-medium">{invite.role}</span>.
                    </p>

                    <div className="bg-slate-50 rounded-lg p-3 mb-5 text-sm text-slate-600">
                      <span className="block text-xs text-slate-500 mb-1">Invitation for</span>
                      <span className="font-medium">{invite.email}</span>
                    </div>

                    {acceptError && (
                      <div role="alert" className="mb-4 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">
                        {acceptError}
                      </div>
                    )}

                    <button
                      onClick={handleAccept}
                      disabled={accepting}
                      className="w-full px-4 py-2.5 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      {accepting ? 'Accepting...' : 'Accept Invitation'}
                    </button>

                    <p className="mt-3 text-xs text-slate-500">
                      Invitation expires {parseApiTimestamp(invite.expires_at).toLocaleDateString()}
                    </p>
                  </>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

export default withModule(AcceptInvitePage, {
  title: 'Workspace invitations',
  module: MODULES.WORKSPACES,
  description: 'Accept an invitation to join a workspace.',
});
