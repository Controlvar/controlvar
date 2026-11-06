import React, { useEffect, useRef, useState } from 'react';
import { Dialog } from '@/components/ui/Dialog';
import { CreatedUser, UserRole, USER_ROLE_LABELS } from '@/types/admin';
import { AdminService } from '@/services/admin';

interface InviteUserModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

const ROLES: UserRole[] = ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'];

// Unambiguous characters only (no 0/O, 1/l/I) so the password survives being read aloud.
const UPPER = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
const LOWER = 'abcdefghijkmnopqrstuvwxyz';
const DIGITS = '23456789';

function randomInt(max: number): number {
  const cryptoObj = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (cryptoObj && typeof cryptoObj.getRandomValues === 'function') {
    const limit = Math.floor(0x100000000 / max) * max; // rejection sampling: no modulo bias
    const buf = new Uint32Array(1);
    for (;;) {
      cryptoObj.getRandomValues(buf);
      if (buf[0] < limit) return buf[0] % max;
    }
  }
  return Math.floor(Math.random() * max);
}

/**
 * A one-time password satisfying `UserCreate.password` (min 8 chars, at least
 * one upper-case letter, one lower-case letter and one digit).
 */
export function generateTemporaryPassword(length = 16): string {
  const all = UPPER + LOWER + DIGITS;
  const chars = [
    UPPER[randomInt(UPPER.length)],
    LOWER[randomInt(LOWER.length)],
    DIGITS[randomInt(DIGITS.length)],
  ];
  while (chars.length < Math.max(length, 8)) chars.push(all[randomInt(all.length)]);
  for (let i = chars.length - 1; i > 0; i--) {
    const j = randomInt(i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join('');
}

/** Default username from the address's local part (`UserCreate.username`: 3–50 chars). */
export function usernameFromEmail(email: string): string {
  const local = email.split('@')[0] ?? '';
  return local
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^[-.]+|[-.]+$/g, '')
    .slice(0, 50);
}

/**
 * The Invite User dialog, on the shared Dialog (#369): focus opens on the email
 * field, Tab stays inside, the page behind is inert, and on close focus goes
 * back to the button that opened it. Escape closes the form, but not while the
 * create is in flight (`busy`; Cancel is disabled then too). Once the temporary
 * password is shown, Done is the only way out (`dismissible={false}`): the
 * password is shown once, and Escape must not throw it away.
 */
export function InviteUserModal({ isOpen, onClose, onSuccess }: InviteUserModalProps) {
  const [email, setEmail] = useState('');
  const [username, setUsername] = useState('');
  const [usernameEdited, setUsernameEdited] = useState(false);
  const [fullName, setFullName] = useState('');
  const [role, setRole] = useState<UserRole>('VIEWER');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<{ user: CreatedUser; password: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const copyRef = useRef<HTMLButtonElement>(null);

  // The form gives way to the password, and the focused control goes with it:
  // move focus to Copy, inside the dialog. Not Done, so that a second Enter
  // does not close the dialog before the password is copied.
  useEffect(() => {
    if (created) copyRef.current?.focus();
  }, [created]);

  const effectiveUsername = usernameEdited ? username : usernameFromEmail(email);
  const canSubmit = email.trim().length > 0 && effectiveUsername.trim().length >= 3 && !submitting;

  const resetForm = () => {
    setEmail('');
    setUsername('');
    setUsernameEdited(false);
    setFullName('');
    setRole('VIEWER');
    setError(null);
    setCreated(null);
    setCopied(false);
  };

  const handleEmailChange = (value: string) => {
    setEmail(value);
    if (!usernameEdited) setUsername(usernameFromEmail(value));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;

    setSubmitting(true);
    setError(null);

    const password = generateTemporaryPassword();
    try {
      const user = await AdminService.createUser({
        email: email.trim(),
        username: effectiveUsername.trim(),
        full_name: fullName.trim() || null,
        password,
        role,
        is_active: true,
        // Admin endpoints (`get_current_superuser`) check the superuser flag, not the role.
        is_superuser: role === 'ADMIN',
      });
      setCreated({ user, password });
      onSuccess();
    } catch (err) {
      setError((err as Error).message || 'Failed to create user');
    } finally {
      setSubmitting(false);
    }
  };

  const handleCopy = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.password);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const handleClose = () => {
    resetForm();
    onClose();
  };

  return (
    <Dialog
      open={isOpen}
      onClose={handleClose}
      title={created ? 'User created' : 'Invite User'}
      busy={submitting}
      dismissible={created === null}
      testId="invite-user-modal"
    >
      <div className="mt-3">
        {created ? (
          <div data-testid="invite-created" className="flex flex-col gap-4">
            <p className="text-sm text-slate-600">
              <span className="font-medium text-slate-800">{created.user.email}</span> can sign in
              as <span className="font-mono">{created.user.username}</span> with this temporary
              password. It is shown <strong>once</strong> — share it now. After signing in,
              they change it with <strong>Change password</strong>, under More at the top of the
              dashboard.
            </p>
            <div className="flex items-center gap-2">
              <code
                data-testid="invite-temp-password"
                className="flex-1 font-mono text-sm bg-slate-50 border border-slate-200 rounded px-3 py-2 select-all break-all"
              >
                {created.password}
              </code>
              <button
                type="button"
                ref={copyRef}
                data-testid="invite-copy-password-button"
                onClick={handleCopy}
                className="px-3 py-2 text-sm font-medium border border-slate-300 rounded-md hover:bg-slate-50"
              >
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <div className="flex justify-end">
              <button
                type="button"
                data-testid="invite-done-button"
                onClick={handleClose}
                className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700"
              >
                Done
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSubmit} noValidate>
            {/* Email */}
            <div className="mb-4">
              <label htmlFor="invite-email" className="block text-sm font-medium text-slate-700 mb-1">
                Email address
              </label>
              <input
                id="invite-email"
                data-testid="invite-email-input"
                type="email"
                value={email}
                onChange={(e) => handleEmailChange(e.target.value)}
                placeholder="user@example.com"
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                autoComplete="off"
              />
            </div>

            {/* Username */}
            <div className="mb-4">
              <label htmlFor="invite-username" className="block text-sm font-medium text-slate-700 mb-1">
                Username
              </label>
              <input
                id="invite-username"
                data-testid="invite-username-input"
                type="text"
                value={effectiveUsername}
                onChange={(e) => {
                  setUsernameEdited(true);
                  setUsername(e.target.value);
                }}
                placeholder="at least 3 characters"
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                autoComplete="off"
              />
            </div>

            {/* Full name */}
            <div className="mb-4">
              <label htmlFor="invite-full-name" className="block text-sm font-medium text-slate-700 mb-1">
                Full name <span className="text-slate-400 font-normal">(optional)</span>
              </label>
              <input
                id="invite-full-name"
                data-testid="invite-full-name-input"
                type="text"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                autoComplete="off"
              />
            </div>

            {/* Role */}
            <div className="mb-4">
              <label htmlFor="invite-role" className="block text-sm font-medium text-slate-700 mb-1">
                Role
              </label>
              <select
                id="invite-role"
                data-testid="invite-role-select"
                value={role}
                onChange={(e) => setRole(e.target.value as UserRole)}
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r === 'ADMIN' ? `${USER_ROLE_LABELS[r]} (superuser)` : USER_ROLE_LABELS[r]}
                  </option>
                ))}
              </select>
            </div>

            <p className="mb-4 text-xs text-slate-500">
              A temporary password is generated and shown once after the account is created.
            </p>

            {/* Error */}
            {error && (
              <div
                role="alert"
                data-testid="invite-error-message"
                className="mb-4 p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-700"
              >
                {error}
              </div>
            )}

            {/* Actions */}
            <div className="flex justify-end gap-3 mt-6">
              <button
                type="button"
                data-testid="invite-cancel-button"
                onClick={handleClose}
                className="px-4 py-2 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-md hover:bg-slate-50"
                disabled={submitting}
              >
                Cancel
              </button>
              <button
                type="submit"
                data-testid="invite-submit-button"
                disabled={!canSubmit}
                className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {submitting ? 'Creating...' : 'Create User'}
              </button>
            </div>
          </form>
        )}
      </div>
    </Dialog>
  );
}
