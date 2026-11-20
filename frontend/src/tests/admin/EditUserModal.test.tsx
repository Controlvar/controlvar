import React, { useState } from 'react';
import { screen, fireEvent, act, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import {
  DEACTIVATE_WARNING,
  EditUserModal,
  FORBIDDEN_MESSAGE,
  NOT_FOUND_MESSAGE,
  SELF_EDIT_NOTE,
  TWO_FACTOR_NOT_LOCAL_MESSAGE,
  TWO_FACTOR_RESET_SELF_NOTE,
  editErrorMessage,
  resetErrorMessage,
  twoFactorResetDone,
  twoFactorResetWarning,
} from '@/components/admin/users/EditUserModal';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { ApiError } from '@/services/api';
import { AdminUser, ApiKey } from '@/types/admin';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
// The dialog makes `#__next` inert and portals beside it, so every render is
// mounted the way Next mounts the page (#369).
import { APP_ROOT_ID, renderInAppRoot as render } from '@/tests/helpers/renderInAppRoot';

const mockUser: AdminUser = {
  id: 'user-1',
  username: 'jdoe',
  email: 'jdoe@example.com',
  role: 'DEVELOPER',
  is_active: true,
  is_superuser: false,
  created_at: '2024-01-01T00:00:00Z',
};

/** A promise and the functions that settle it, so a test decides when a save finishes. */
function deferred() {
  let resolve!: () => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('EditUserModal', () => {
  const defaultProps = {
    isOpen: true,
    user: mockUser,
    currentUserId: 'admin-1',
    onClose: jest.fn(),
    onSave: jest.fn(() => Promise.resolve()),
  };

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders modal when isOpen=true', () => {
    render(<EditUserModal {...defaultProps} />);
    expect(screen.getByTestId('edit-user-modal')).toBeInTheDocument();
  });

  it('renders nothing when closed', () => {
    render(<EditUserModal {...defaultProps} isOpen={false} />);
    expect(screen.queryByTestId('edit-user-modal')).not.toBeInTheDocument();
  });

  it('names the user and shows the email', () => {
    render(<EditUserModal {...defaultProps} />);
    const modal = screen.getByTestId('edit-user-modal');
    expect(modal).toHaveTextContent('jdoe · jdoe@example.com');
  });

  it('shows "No email" for an account with no email address (#342)', () => {
    render(<EditUserModal {...defaultProps} user={{ ...mockUser, email: null }} />);
    expect(screen.getByTestId('edit-user-modal')).toHaveTextContent('No email');
  });

  it('role select shows current user role', () => {
    render(<EditUserModal {...defaultProps} />);
    const roleSelect = screen.getByTestId('edit-role-select') as HTMLSelectElement;
    expect(roleSelect.value).toBe('DEVELOPER');
  });

  it('offers exactly the four base roles', () => {
    render(<EditUserModal {...defaultProps} />);
    const options = Array.from(
      (screen.getByTestId('edit-role-select') as HTMLSelectElement).options,
    ).map((o) => o.value);
    expect(options).toStrictEqual(['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER']);
  });

  it('a user with no role shows a disabled "No role set: choose one" option', () => {
    render(<EditUserModal {...defaultProps} user={{ ...mockUser, role: null }} />);
    const select = screen.getByTestId('edit-role-select') as HTMLSelectElement;
    expect(select.value).toBe('');
    const placeholder = select.options[0];
    expect(placeholder.textContent).toBe('No role set: choose one');
    expect(placeholder.disabled).toBe(true);
    expect(screen.getByTestId('edit-save-button')).toBeDisabled();
    fireEvent.change(select, { target: { value: 'VIEWER' } });
    expect(screen.getByTestId('edit-save-button')).toBeEnabled();
  });

  it('is_active toggle shows current state', () => {
    render(<EditUserModal {...defaultProps} />);
    const activeToggle = screen.getByTestId('edit-active-toggle') as HTMLInputElement;
    expect(activeToggle.checked).toBe(true);
  });

  it('Save is disabled until something changes, and again when it is changed back', () => {
    render(<EditUserModal {...defaultProps} />);
    const save = screen.getByTestId('edit-save-button');
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByTestId('edit-role-select'), { target: { value: 'ANALYST' } });
    expect(save).toBeEnabled();
    fireEvent.change(screen.getByTestId('edit-role-select'), { target: { value: 'DEVELOPER' } });
    expect(save).toBeDisabled();
  });

  it('changing only the role sends exactly {role}', async () => {
    const onSave = jest.fn(() => Promise.resolve());
    render(<EditUserModal {...defaultProps} onSave={onSave} />);
    fireEvent.change(screen.getByTestId('edit-role-select'), { target: { value: 'ANALYST' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-save-button'));
    });
    expect(onSave).toHaveBeenCalledTimes(1);
    const [id, changes] = (onSave.mock.calls[0] as unknown) as [string, object];
    expect(id).toBe('user-1');
    expect(changes).toStrictEqual({ role: 'ANALYST' });
  });

  it('changing only Active sends exactly {is_active}', async () => {
    const onSave = jest.fn(() => Promise.resolve());
    render(<EditUserModal {...defaultProps} onSave={onSave} />);
    fireEvent.click(screen.getByTestId('edit-active-toggle'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-save-button'));
    });
    expect(((onSave.mock.calls[0] as unknown) as [string, object])[1]).toStrictEqual({
      is_active: false,
    });
  });

  it('changing both sends both', async () => {
    const onSave = jest.fn(() => Promise.resolve());
    render(<EditUserModal {...defaultProps} onSave={onSave} />);
    fireEvent.change(screen.getByTestId('edit-role-select'), { target: { value: 'VIEWER' } });
    fireEvent.click(screen.getByTestId('edit-active-toggle'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-save-button'));
    });
    expect(((onSave.mock.calls[0] as unknown) as [string, object])[1]).toStrictEqual({
      role: 'VIEWER',
      is_active: false,
    });
  });

  it('while saving the button reads "Saving…", is disabled and aria-busy; Cancel stays enabled', async () => {
    const pending = deferred();
    render(<EditUserModal {...defaultProps} onSave={() => pending.promise} />);
    fireEvent.change(screen.getByTestId('edit-role-select'), { target: { value: 'ANALYST' } });
    fireEvent.click(screen.getByTestId('edit-save-button'));
    const save = screen.getByTestId('edit-save-button');
    expect(save).toHaveTextContent('Saving…');
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByTestId('edit-cancel-button')).toBeEnabled();
    await act(async () => {
      pending.resolve();
      await pending.promise;
    });
    expect(screen.getByTestId('edit-save-button')).toHaveAttribute('aria-busy', 'false');
  });

  it('a failed save shows the error inside the dialog and keeps the choices', async () => {
    const onSave = jest.fn(() =>
      Promise.reject(new ApiError({ status: 409, detail: 'Roles come from Cognito.' })),
    );
    render(<EditUserModal {...defaultProps} onSave={onSave} />);
    fireEvent.change(screen.getByTestId('edit-role-select'), { target: { value: 'ANALYST' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-save-button'));
    });
    const modal = screen.getByTestId('edit-user-modal');
    const error = within(modal).getByTestId('edit-error-message');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error).toHaveTextContent('Roles come from Cognito.');
    expect((screen.getByTestId('edit-role-select') as HTMLSelectElement).value).toBe('ANALYST');
    expect(screen.getByTestId('edit-save-button')).toHaveTextContent('Save changes');
  });

  it('shows the deactivation warning, with what stops working, when deactivating an active user', () => {
    render(<EditUserModal {...defaultProps} />);
    expect(screen.queryByTestId('deactivate-warning')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('edit-active-toggle'));
    expect(screen.getByTestId('deactivate-warning')).toHaveTextContent(DEACTIVATE_WARNING);
    expect(DEACTIVATE_WARNING).toBe(
      "This user won't be able to sign in. Requests with a token they already hold are refused, " +
        'and API keys they created stop working — including keys your applications use.',
    );
  });

  it('own row: Role and Active are disabled and described by the reason', () => {
    render(<EditUserModal {...defaultProps} currentUserId="user-1" />);
    const role = screen.getByTestId('edit-role-select');
    const active = screen.getByTestId('edit-active-toggle');
    expect(role).toBeDisabled();
    expect(active).toBeDisabled();
    const note = screen.getByTestId('edit-self-note');
    expect(note).toHaveTextContent(SELF_EDIT_NOTE);
    expect(SELF_EDIT_NOTE).toBe(
      "You can't change your own role or deactivate yourself. Ask another administrator.",
    );
    expect(role.getAttribute('aria-describedby')?.split(' ')).toContain(note.id);
    expect(active.getAttribute('aria-describedby')).toBe(note.id);
    expect(screen.getByTestId('edit-save-button')).toBeDisabled();
  });

  it('another row: Role and Active are enabled and there is no self note', () => {
    render(<EditUserModal {...defaultProps} />);
    expect(screen.getByTestId('edit-role-select')).toBeEnabled();
    expect(screen.getByTestId('edit-active-toggle')).toBeEnabled();
    expect(screen.queryByTestId('edit-self-note')).not.toBeInTheDocument();
  });

  it('says that superuser access, not the role, opens the admin area', () => {
    render(<EditUserModal {...defaultProps} />);
    expect(screen.getByTestId('edit-role-help')).toHaveTextContent(
      'Opening this admin area needs superuser access, shown in the Superuser column.',
    );
    expect(screen.getByTestId('edit-role-select').getAttribute('aria-describedby')).toContain(
      'edit-role-help',
    );
  });

  it('core profile: no custom-roles or SSO sentence', () => {
    render(
      <ModulesProvider initial={{ profile: 'core', modules: [], version: '' }}>
        <EditUserModal {...defaultProps} />
      </ModulesProvider>,
    );
    expect(screen.queryByTestId('edit-role-help-custom')).not.toBeInTheDocument();
    expect(screen.queryByTestId('edit-role-help-sso')).not.toBeInTheDocument();
  });

  it('full profile: says custom roles and grants add to the role (#891), and what SSO does to the role', () => {
    render(
      <ModulesProvider initial={{ profile: 'full', modules: ['rbac', 'sso'], version: '' }}>
        <EditUserModal {...defaultProps} />
      </ModulesProvider>,
    );
    expect(screen.getByTestId('edit-role-help-custom')).toHaveTextContent(
      'Custom roles and direct permission grants (full profile) add to the role set here. Assign them through the RBAC API.',
    );
    expect(screen.getByTestId('edit-role-help-sso')).toHaveTextContent(
      'If this user signs in with SSO and one of their groups is mapped to a role, that sign-in replaces the role set here.',
    );
  });

  it('calls onClose when cancel is clicked', () => {
    const onClose = jest.fn();
    render(<EditUserModal {...defaultProps} onClose={onClose} />);
    fireEvent.click(screen.getByTestId('edit-cancel-button'));
    expect(onClose).toHaveBeenCalledWith(undefined);
  });

  it('Escape closes the dialog', () => {
    const onClose = jest.fn();
    render(<EditUserModal {...defaultProps} onClose={onClose} />);
    fireEvent.keyDown(screen.getByTestId('edit-role-select'), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('moves focus to the Role select on open, or to Cancel on the own row', () => {
    const { unmount } = render(<EditUserModal {...defaultProps} />);
    expect(document.activeElement).toBe(screen.getByTestId('edit-role-select'));
    unmount();
    render(<EditUserModal {...defaultProps} currentUserId="user-1" />);
    expect(document.activeElement).toBe(screen.getByTestId('edit-cancel-button'));
  });

  it('Tab and Shift+Tab stay inside the dialog', () => {
    render(<EditUserModal {...defaultProps} />);
    fireEvent.change(screen.getByTestId('edit-role-select'), { target: { value: 'ANALYST' } });
    const first = screen.getByTestId('edit-role-select');
    const last = screen.getByTestId('edit-save-button');
    last.focus();
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it('after a 404, closing asks the page to reload the list', async () => {
    const onClose = jest.fn();
    const onSave = jest.fn(() =>
      Promise.reject(new ApiError({ status: 404, detail: 'User not found' })),
    );
    render(<EditUserModal {...defaultProps} onClose={onClose} onSave={onSave} />);
    fireEvent.click(screen.getByTestId('edit-active-toggle'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-save-button'));
    });
    expect(screen.getByTestId('edit-error-message')).toHaveTextContent(NOT_FOUND_MESSAGE);
    fireEvent.click(screen.getByTestId('edit-cancel-button'));
    expect(onClose).toHaveBeenCalledWith({ reload: true });
  });

  it('renders with data-testid="edit-user-modal"', () => {
    render(<EditUserModal {...defaultProps} />);
    expect(screen.getByTestId('edit-user-modal')).toBeInTheDocument();
  });
});

describe('editErrorMessage', () => {
  it.each([
    [400, "You can't deactivate your own account.", "You can't deactivate your own account."],
    [409, 'Roles on this deployment come from Cognito groups.', 'Roles on this deployment come from Cognito groups.'],
    [403, 'Not enough permissions', FORBIDDEN_MESSAGE],
    [404, 'User not found', NOT_FOUND_MESSAGE],
  ])('HTTP %i shows the right sentence', (status, detail, expected) => {
    expect(editErrorMessage(new ApiError({ status, detail }))).toBe(expected);
  });

  it('422 is prefixed with "Couldn\'t save:"', () => {
    const err = new ApiError({
      status: 422,
      detail: [{ loc: ['body', 'role'], msg: 'Input should be ADMIN', type: 'literal_error' }],
    });
    expect(editErrorMessage(err)).toBe("Couldn't save: role: Input should be ADMIN");
  });
});

/** A page with the real opener: a button that opens the dialog, as the users table does. */
function OpenedFromButton({ currentUserId = 'admin-1' }: { currentUserId?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Edit jdoe
      </button>
      <EditUserModal
        isOpen={open}
        user={mockUser}
        currentUserId={currentUserId}
        onClose={() => setOpen(false)}
        onSave={() => Promise.resolve()}
      />
    </>
  );
}

describe('EditUserModal on the shared Dialog (#369)', () => {
  const appRoot = () => document.getElementById(APP_ROOT_ID) as HTMLElement;

  it('opens as a modal dialog named "Edit user"', async () => {
    const user = userEvent.setup();
    render(<OpenedFromButton />);
    await user.click(screen.getByRole('button', { name: 'Edit jdoe' }));
    const dialog = screen.getByRole('dialog', { name: 'Edit user' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toBe(screen.getByTestId('edit-user-modal'));
  });

  it('Escape dispatched on document closes it, once', () => {
    const onClose = jest.fn();
    render(<EditUserModal isOpen user={mockUser} currentUserId="admin-1" onClose={onClose} onSave={jest.fn()} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledWith(undefined);
  });

  it('focus returns to the button that opened it, after Escape and after Cancel', async () => {
    const user = userEvent.setup();
    render(<OpenedFromButton />);
    const opener = screen.getByRole('button', { name: 'Edit jdoe' });

    await user.click(opener);
    expect(screen.getByTestId('edit-role-select')).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByTestId('edit-user-modal')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();

    await user.click(opener);
    await user.click(screen.getByTestId('edit-cancel-button'));
    expect(screen.queryByTestId('edit-user-modal')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('the page behind it is inert while it is open, and not after', async () => {
    const user = userEvent.setup();
    render(<OpenedFromButton />);
    await user.click(screen.getByRole('button', { name: 'Edit jdoe' }));
    expect(appRoot()).toHaveAttribute('inert');
    expect(appRoot().contains(screen.getByTestId('edit-user-modal'))).toBe(false);
    await user.keyboard('{Escape}');
    expect(appRoot()).not.toHaveAttribute('inert');
  });

  it('Tab from the last control wraps to the first; the page behind is never reached', async () => {
    const user = userEvent.setup();
    render(<OpenedFromButton />);
    await user.click(screen.getByRole('button', { name: 'Edit jdoe' }));
    // Save is disabled until something changes, so Cancel is the last control.
    expect(screen.getByTestId('edit-role-select')).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId('edit-active-toggle')).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId('edit-cancel-button')).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId('edit-role-select')).toHaveFocus();
  });
});

describe('EditUserModal accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  // Changed for #369 (was `violations(container)`): the dialog now portals to
  // document.body, outside the render container, so a container scan passed
  // while checking nothing. The scan is of the dialog node itself, and asserts
  // that the rule naming the dialog ran on it.
  async function dialogViolations() {
    // By role alone: a lost name must be axe's finding, not a failed query.
    const dialog = screen.getByRole('dialog');
    expect(document.body).toContainElement(dialog);
    const result = await axe.run(dialog, axeOptions);
    const violations = result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
    // A clean scan must have run the rule that names the dialog, on it.
    if (violations.length === 0) expect(result.passes.map((p) => p.id)).toContain('aria-dialog-name');
    return violations;
  }

  it('the dialog has no axe violations: editing another user, with the warning and an error', async () => {
    const onSave = jest.fn(() =>
      Promise.reject(new ApiError({ status: 403, detail: 'Not enough permissions' })),
    );
    render(<EditUserModal isOpen user={mockUser} currentUserId="admin-1" onClose={jest.fn()} onSave={onSave} />);
    fireEvent.click(screen.getByTestId('edit-active-toggle'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-save-button'));
    });
    expect(screen.getByTestId('edit-error-message')).toBeInTheDocument();
    expect(await dialogViolations()).toStrictEqual([]);
  });

  it('the dialog has no axe violations on the own row', async () => {
    render(<EditUserModal isOpen user={mockUser} currentUserId="user-1" onClose={jest.fn()} onSave={jest.fn()} />);
    expect(await dialogViolations()).toStrictEqual([]);
  });

  it('the dialog has no axe violations while it asks to confirm a two-step reset, and once it is done', async () => {
    render(
      <EditUserModal
        isOpen
        user={{ ...mockUser, totp_enabled: true }}
        currentUserId="admin-1"
        onClose={jest.fn()}
        onSave={jest.fn()}
        onResetTwoFactor={jest.fn(() => Promise.resolve())}
      />,
    );
    fireEvent.click(screen.getByTestId('edit-two-factor-reset'));
    expect(await dialogViolations()).toStrictEqual([]);
    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-two-factor-reset-confirm'));
    });
    expect(screen.getByTestId('edit-two-factor-reset-done')).toBeInTheDocument();
    expect(await dialogViolations()).toStrictEqual([]);
  });
});

describe('EditUserModal: Reset two-step sign-in (#1812)', () => {
  const twoStepUser: AdminUser = { ...mockUser, totp_enabled: true };
  const props = {
    isOpen: true,
    user: twoStepUser,
    currentUserId: 'admin-1',
    onClose: jest.fn(),
    onSave: jest.fn(() => Promise.resolve()),
  };

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('is offered only for an account with two-step sign-in on', () => {
    const onResetTwoFactor = jest.fn(() => Promise.resolve());
    const { rerender } = render(<EditUserModal {...props} onResetTwoFactor={onResetTwoFactor} />);
    expect(screen.getByTestId('edit-two-factor-reset')).toHaveTextContent('Reset two-step sign-in');
    rerender(<EditUserModal {...props} user={{ ...mockUser, totp_enabled: false }} onResetTwoFactor={onResetTwoFactor} />);
    expect(screen.queryByTestId('edit-two-factor')).not.toBeInTheDocument();
    // An older API that does not send the field: nothing is offered.
    rerender(<EditUserModal {...props} user={mockUser} onResetTwoFactor={onResetTwoFactor} />);
    expect(screen.queryByTestId('edit-two-factor')).not.toBeInTheDocument();
  });

  it('is not offered on your own row, which says who can reset yours', () => {
    const onResetTwoFactor = jest.fn(() => Promise.resolve());
    render(<EditUserModal {...props} currentUserId="user-1" onResetTwoFactor={onResetTwoFactor} />);
    expect(screen.queryByTestId('edit-two-factor-reset')).not.toBeInTheDocument();
    expect(screen.getByTestId('edit-two-factor-self-note')).toHaveTextContent(TWO_FACTOR_RESET_SELF_NOTE);
  });

  it('asks first, saying the session stays, then resets and says so; closing reloads the list', async () => {
    const onClose = jest.fn();
    const onResetTwoFactor = jest.fn(() => Promise.resolve());
    render(<EditUserModal {...props} onClose={onClose} onResetTwoFactor={onResetTwoFactor} />);

    fireEvent.click(screen.getByTestId('edit-two-factor-reset'));
    expect(onResetTwoFactor).not.toHaveBeenCalled();
    const confirm = screen.getByTestId('edit-two-factor-confirm');
    expect(confirm).toHaveTextContent(twoFactorResetWarning('jdoe@example.com'));
    expect(confirm).toHaveTextContent('Their current session stays valid until it expires.');
    expect(screen.getByTestId('edit-two-factor-keep')).toHaveFocus();

    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-two-factor-reset-confirm'));
    });
    expect(onResetTwoFactor).toHaveBeenCalledWith('user-1');
    const done = screen.getByTestId('edit-two-factor-reset-done');
    expect(done).toHaveTextContent(twoFactorResetDone('jdoe@example.com'));
    expect(done).toHaveAttribute('role', 'status');
    expect(done).toHaveFocus();

    fireEvent.click(screen.getByTestId('edit-cancel-button'));
    expect(onClose).toHaveBeenCalledWith({ reload: true });
  });

  it('Keep it goes back without resetting', () => {
    const onResetTwoFactor = jest.fn(() => Promise.resolve());
    render(<EditUserModal {...props} onResetTwoFactor={onResetTwoFactor} />);
    fireEvent.click(screen.getByTestId('edit-two-factor-reset'));
    fireEvent.click(screen.getByTestId('edit-two-factor-keep'));
    expect(screen.getByTestId('edit-two-factor-reset')).toBeInTheDocument();
    expect(onResetTwoFactor).not.toHaveBeenCalled();
  });

  it.each([
    ['403', new ApiError({ status: 403, detail: 'Not enough permissions' }), FORBIDDEN_MESSAGE],
    ['404, the user is gone', new ApiError({ status: 404, detail: 'User not found' }), NOT_FOUND_MESSAGE],
    [
      '404, not the local provider',
      new ApiError({ status: 404, detail: "Endpoint not available: AUTH_PROVIDER is not 'local'" }),
      TWO_FACTOR_NOT_LOCAL_MESSAGE,
    ],
  ])('a failed reset (%s) shows its message inside the dialog', async (_name, err, expected) => {
    const onResetTwoFactor = jest.fn(() => Promise.reject(err));
    render(<EditUserModal {...props} onResetTwoFactor={onResetTwoFactor} />);
    fireEvent.click(screen.getByTestId('edit-two-factor-reset'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-two-factor-reset-confirm'));
    });
    expect(screen.getByTestId('edit-error-message')).toHaveTextContent(expected);
    expect(screen.queryByTestId('edit-two-factor-reset-done')).not.toBeInTheDocument();
    expect(resetErrorMessage(err)).toBe(expected);
  });
});

describe('EditUserModal: the API keys that stop working when Active is cleared (#657)', () => {
  const LAST_USED = '2026-11-18T10:00:00';
  const lastUsedText = `last used ${parseApiTimestamp(LAST_USED).toLocaleString()}`;

  function makeKey(over: Partial<ApiKey> = {}): ApiKey {
    return {
      id: 'k-1',
      name: 'Checkout server',
      scopes: ['sdk:ruleset'],
      is_active: true,
      user_id: 'user-1',
      created_at: '2026-01-01T00:00:00',
      expires_at: null,
      last_used_at: LAST_USED,
      ...over,
    };
  }

  /** The three rows `?all=true` returns for this user's neighbours: one listed, two not. */
  const MIXED_KEYS: ApiKey[] = [
    makeKey({ id: 'k-1' }),
    makeKey({ id: 'k-2', name: 'Expired one', expires_at: '2020-01-01T00:00:00' }),
    makeKey({ id: 'k-3', name: "Someone else's", user_id: 'user-3' }),
  ];

  function deferredKeys() {
    let resolve!: (keys: ApiKey[]) => void;
    let reject!: (err: unknown) => void;
    const promise = new Promise<ApiKey[]>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  const baseProps = {
    isOpen: true,
    user: mockUser,
    currentUserId: 'admin-1',
    onClose: jest.fn(),
    onSave: jest.fn(() => Promise.resolve()),
  };

  const clearActive = () => fireEvent.click(screen.getByTestId('edit-active-toggle'));
  const summary = () => screen.getByTestId('deactivate-keys-summary');
  const status = () => screen.getByTestId('deactivate-keys-status');

  /** Clear Active and let the request settle. */
  async function clearActiveAndSettle() {
    await act(async () => {
      clearActive();
    });
  }

  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('when it asks (C1)', () => {
    it('does not ask on open or for a role-only change', () => {
      const listUserKeys = jest.fn(() => Promise.resolve([]));
      render(<EditUserModal {...baseProps} listUserKeys={listUserKeys} />);
      fireEvent.change(screen.getByTestId('edit-role-select'), { target: { value: 'ANALYST' } });
      expect(listUserKeys).not.toHaveBeenCalled();
      expect(screen.queryByTestId('deactivate-keys')).not.toBeInTheDocument();
    });

    it('asks once on clearing Active, keeps a success, and asks again after a failure', async () => {
      const listUserKeys = jest
        .fn<Promise<ApiKey[]>, [string]>()
        .mockResolvedValue([makeKey()]);
      render(<EditUserModal {...baseProps} listUserKeys={listUserKeys} />);
      await clearActiveAndSettle();
      expect(listUserKeys).toHaveBeenCalledTimes(1);
      expect(listUserKeys).toHaveBeenCalledWith('user-1');

      // Clear, tick, clear: the success is kept, so there is no second request.
      clearActive();
      await act(async () => {
        clearActive();
      });
      expect(listUserKeys).toHaveBeenCalledTimes(1);
      expect(summary()).toHaveTextContent('The API key that stops working:');
    });

    it('does not keep a failure: clearing Active again asks again', async () => {
      const listUserKeys = jest
        .fn<Promise<ApiKey[]>, [string]>()
        .mockRejectedValueOnce(new Error('boom'))
        .mockResolvedValueOnce([makeKey()]);
      render(<EditUserModal {...baseProps} listUserKeys={listUserKeys} />);
      await clearActiveAndSettle();
      expect(summary()).toHaveTextContent("Couldn't list this user's API keys: boom");
      clearActive();
      await act(async () => {
        clearActive();
      });
      expect(listUserKeys).toHaveBeenCalledTimes(2);
      expect(summary()).toHaveTextContent('The API key that stops working:');
    });

    it('asks again after the dialog was closed and opened again', async () => {
      const listUserKeys = jest.fn(() => Promise.resolve([makeKey()]));
      const { rerender } = render(<EditUserModal {...baseProps} listUserKeys={listUserKeys} />);
      await clearActiveAndSettle();
      expect(listUserKeys).toHaveBeenCalledTimes(1);
      rerender(<EditUserModal {...baseProps} isOpen={false} listUserKeys={listUserKeys} />);
      rerender(<EditUserModal {...baseProps} listUserKeys={listUserKeys} />);
      expect(screen.queryByTestId('deactivate-keys')).not.toBeInTheDocument();
      await clearActiveAndSettle();
      expect(listUserKeys).toHaveBeenCalledTimes(2);
    });

    it('ignores an answer that settles after the dialog was closed and opened again', async () => {
      const first = deferredKeys();
      const second = deferredKeys();
      const listUserKeys = jest
        .fn<Promise<ApiKey[]>, [string]>()
        .mockReturnValueOnce(first.promise)
        .mockReturnValueOnce(second.promise);
      const { rerender } = render(<EditUserModal {...baseProps} listUserKeys={listUserKeys} />);
      clearActive();
      rerender(<EditUserModal {...baseProps} isOpen={false} listUserKeys={listUserKeys} />);
      rerender(<EditUserModal {...baseProps} listUserKeys={listUserKeys} />);
      clearActive();
      expect(listUserKeys).toHaveBeenCalledTimes(2);

      await act(async () => {
        first.resolve([makeKey({ id: 'old', name: 'From the first opening' })]);
      });
      expect(summary()).toHaveTextContent('Checking which API keys this user has…');
      expect(screen.queryByText('From the first opening')).not.toBeInTheDocument();

      await act(async () => {
        second.resolve([makeKey({ id: 'new', name: 'From the second opening' })]);
      });
      expect(screen.getByText('From the second opening')).toBeInTheDocument();
    });

    it('does not ask when an inactive account is reactivated and cleared again', async () => {
      const listUserKeys = jest.fn(() => Promise.resolve([]));
      render(
        <EditUserModal {...baseProps} user={{ ...mockUser, is_active: false }} listUserKeys={listUserKeys} />,
      );
      clearActive();
      expect(listUserKeys).not.toHaveBeenCalled();
      expect(screen.queryByTestId('deactivate-warning')).not.toBeInTheDocument();
    });

    it('without listUserKeys the dialog shows only the general warning', () => {
      render(<EditUserModal {...baseProps} />);
      clearActive();
      expect(screen.getByTestId('deactivate-warning')).toHaveTextContent(DEACTIVATE_WARNING);
      expect(screen.queryByTestId('deactivate-keys')).not.toBeInTheDocument();
      expect(screen.queryByTestId('deactivate-keys-status')).not.toBeInTheDocument();
    });
  });

  describe('what it lists (C2, C3, C4)', () => {
    it('lists only the keys of this user that are active, not those of another user or an expired one', async () => {
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.resolve(MIXED_KEYS)} />);
      await clearActiveAndSettle();
      const items = screen.getAllByTestId('deactivate-key');
      expect(items).toHaveLength(1);
      expect(items[0].textContent).toBe(`Checkout server · sdk:ruleset · ${lastUsedText}`);
    });

    it('says one key and N keys in words, joins scopes, and says no scopes and never used', async () => {
      const keys = [
        makeKey({ id: 'a', name: 'Two scopes', scopes: ['read', 'track'] }),
        makeKey({ id: 'b', name: 'Bare', scopes: [], last_used_at: null }),
      ];
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.resolve(keys)} />);
      await clearActiveAndSettle();
      expect(summary().textContent).toBe('The 2 API keys that stop working:');
      const items = screen.getAllByTestId('deactivate-key').map((li) => li.textContent);
      expect(items).toStrictEqual([
        `Two scopes · read, track · ${lastUsedText}`,
        'Bare · no scopes · never used',
      ]);
      expect(screen.getByTestId('deactivate-keys-advice').textContent).toBe(
        'To keep an application running, cancel, switch it to a key created by an account that is staying, ' +
          'then deactivate this user. Reactivating this user makes these keys work again, unless ' +
          'they have expired.',
      );
    });

    it('says "The API key that stops working:" for one key', async () => {
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.resolve([makeKey()])} />);
      await clearActiveAndSettle();
      expect(summary().textContent).toBe('The API key that stops working:');
    });

    it('a user with no active keys: says so, with no list and no advice', async () => {
      render(
        <EditUserModal
          {...baseProps}
          listUserKeys={() => Promise.resolve(MIXED_KEYS.filter((k) => k.user_id !== 'user-1'))}
        />,
      );
      await clearActiveAndSettle();
      expect(summary().textContent).toBe('This user has no active API keys.');
      expect(screen.queryByTestId('deactivate-keys-list')).not.toBeInTheDocument();
      expect(screen.queryByRole('list')).not.toBeInTheDocument();
      expect(screen.queryByTestId('deactivate-keys-advice')).not.toBeInTheDocument();
    });

    it('the general warning stays above the list', async () => {
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.resolve([makeKey()])} />);
      await clearActiveAndSettle();
      const block = screen.getByTestId('deactivate-warning');
      expect(block.firstElementChild).toHaveTextContent(DEACTIVATE_WARNING);
      expect(block.firstElementChild).toHaveAttribute('id', 'edit-deactivate-warning');
    });
  });

  describe('Save is never held up by the list (C5)', () => {
    it('while the request is pending, Save is enabled and sends exactly {is_active: false}', async () => {
      const pending = deferredKeys();
      const onSave = jest.fn(() => Promise.resolve());
      render(
        <EditUserModal {...baseProps} onSave={onSave} listUserKeys={() => pending.promise} />,
      );
      clearActive();
      expect(summary().textContent).toBe('Checking which API keys this user has…');
      expect(screen.getByTestId('deactivate-warning')).toHaveTextContent(DEACTIVATE_WARNING);
      const save = screen.getByTestId('edit-save-button');
      expect(save).toBeEnabled();
      await act(async () => {
        fireEvent.click(save);
      });
      expect(onSave).toHaveBeenCalledTimes(1);
      expect((onSave.mock.calls[0] as unknown[])[1]).toStrictEqual({ is_active: false });
    });

    it('after a failed request, Save is enabled and sends exactly {is_active: false}', async () => {
      const onSave = jest.fn(() => Promise.resolve());
      render(
        <EditUserModal
          {...baseProps}
          onSave={onSave}
          listUserKeys={() => Promise.reject(new Error('nope'))}
        />,
      );
      await clearActiveAndSettle();
      await act(async () => {
        fireEvent.click(screen.getByTestId('edit-save-button'));
      });
      expect((onSave.mock.calls[0] as unknown[])[1]).toStrictEqual({ is_active: false });
    });
  });

  describe('when the request fails (C6)', () => {
    it('shows the message and the general warning, and Try again asks again', async () => {
      const listUserKeys = jest
        .fn<Promise<ApiKey[]>, [string]>()
        .mockRejectedValueOnce(new Error('The API did not answer.'))
        .mockResolvedValueOnce([makeKey(), makeKey({ id: 'k-9', name: 'Second' })]);
      render(<EditUserModal {...baseProps} listUserKeys={listUserKeys} />);
      await clearActiveAndSettle();
      expect(summary().textContent).toBe("Couldn't list this user's API keys: The API did not answer.");
      expect(screen.getByTestId('deactivate-warning')).toHaveTextContent(DEACTIVATE_WARNING);
      expect(screen.getByTestId('deactivate-keys-retry')).toHaveTextContent('Try again');

      await act(async () => {
        fireEvent.click(screen.getByTestId('deactivate-keys-retry'));
      });
      expect(listUserKeys).toHaveBeenCalledTimes(2);
      expect(summary().textContent).toBe('The 2 API keys that stop working:');
      expect(screen.queryByTestId('deactivate-keys-retry')).not.toBeInTheDocument();
      // On success, focus moves to the line that now says what was found.
      expect(summary()).toHaveFocus();
    });

    it('while Try again is busy it reads Checking…, is aria-disabled, ignores a click and keeps focus; on failure focus stays', async () => {
      const retry = deferredKeys();
      const listUserKeys = jest
        .fn<Promise<ApiKey[]>, [string]>()
        .mockRejectedValueOnce(new Error('first'))
        .mockReturnValueOnce(retry.promise);
      render(<EditUserModal {...baseProps} listUserKeys={listUserKeys} />);
      await clearActiveAndSettle();
      const button = screen.getByTestId('deactivate-keys-retry');
      button.focus();
      fireEvent.click(button);
      expect(screen.getByTestId('deactivate-keys-retry')).toHaveTextContent('Checking…');
      expect(screen.getByTestId('deactivate-keys-retry')).toHaveAttribute('aria-disabled', 'true');
      fireEvent.click(screen.getByTestId('deactivate-keys-retry'));
      expect(listUserKeys).toHaveBeenCalledTimes(2);
      expect(screen.getByTestId('deactivate-keys-retry')).toHaveFocus();

      await act(async () => {
        retry.reject(new Error('second'));
      });
      expect(summary().textContent).toBe("Couldn't list this user's API keys: second");
      const again = screen.getByTestId('deactivate-keys-retry');
      expect(again).toHaveTextContent('Try again');
      expect(again).not.toHaveAttribute('aria-disabled', 'true');
      expect(again).toHaveFocus();
    });
  });

  describe('what a screen reader is given (C7, C8, C9)', () => {
    it('Save is described by the general warning and the summary, not the list', async () => {
      const keys = [makeKey(), makeKey({ id: 'k-9', name: 'Second' })];
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.resolve(keys)} />);
      const save = screen.getByTestId('edit-save-button');
      expect(save).not.toHaveAttribute('aria-describedby');
      await clearActiveAndSettle();
      expect(save).toHaveAccessibleDescription(`${DEACTIVATE_WARNING} The 2 API keys that stop working:`);
      expect(save).toHaveAccessibleDescription(/The 2 API keys that stop working/);
      expect(save).not.toHaveAccessibleDescription(/Checkout server/);
      expect(screen.getByTestId('edit-active-toggle')).toHaveAccessibleDescription(DEACTIVATE_WARNING);
    });

    it('one polite status region is empty until the request settles, then holds the summary', async () => {
      const pending = deferredKeys();
      render(<EditUserModal {...baseProps} listUserKeys={() => pending.promise} />);
      expect(status()).toHaveAttribute('role', 'status');
      expect(status().textContent).toBe('');
      clearActive();
      // Loading is not announced: the region stays empty.
      expect(status().textContent).toBe('');
      await act(async () => {
        pending.resolve([makeKey(), makeKey({ id: 'k-9', name: 'Second' })]);
      });
      // Read out as a sentence: the visible line ends in a colon, this one in a full stop.
      expect(status().textContent).toBe('The 2 API keys that stop working.');
      expect(summary().textContent).toBe('The 2 API keys that stop working:');
    });

    it('the status region says it too when the request fails', async () => {
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.reject(new Error('down'))} />);
      await clearActiveAndSettle();
      expect(status().textContent).toBe("Couldn't list this user's API keys: down");
    });

    it('focus stays on Active when the list arrives', async () => {
      const pending = deferredKeys();
      render(<EditUserModal {...baseProps} listUserKeys={() => pending.promise} />);
      const toggle = screen.getByTestId('edit-active-toggle');
      toggle.focus();
      fireEvent.click(toggle);
      expect(toggle).toHaveFocus();
      await act(async () => {
        pending.resolve([makeKey()]);
      });
      expect(toggle).toHaveFocus();
    });
  });

  describe('your own row, and ticking Active again (C10, C11)', () => {
    it('your own row: Active is disabled, nothing is asked, there is no block and no status region', async () => {
      const listUserKeys = jest.fn(() => Promise.resolve([makeKey()]));
      render(<EditUserModal {...baseProps} currentUserId="user-1" listUserKeys={listUserKeys} />);
      const toggle = screen.getByTestId('edit-active-toggle');
      expect(toggle).toBeDisabled();
      // userEvent does what a browser does with a disabled box: nothing.
      await userEvent.setup().click(toggle);
      expect(listUserKeys).not.toHaveBeenCalled();
      expect(screen.queryByTestId('deactivate-warning')).not.toBeInTheDocument();
      expect(screen.queryByTestId('deactivate-keys-status')).not.toBeInTheDocument();
    });

    it('ticking Active again removes the block and empties the status region; Save is off again', async () => {
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.resolve([makeKey()])} />);
      await clearActiveAndSettle();
      expect(screen.getByTestId('deactivate-warning')).toBeInTheDocument();
      clearActive();
      expect(screen.queryByTestId('deactivate-warning')).not.toBeInTheDocument();
      expect(status().textContent).toBe('');
      expect(screen.getByTestId('edit-save-button')).toBeDisabled();
      expect(screen.getByTestId('edit-save-button')).not.toHaveAttribute('aria-describedby');
    });
  });

  describe('axe finds nothing in the dialog (C12; colour contrast is not computable in jsdom)', () => {
    const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

    async function dialogViolations() {
      const dialog = screen.getByRole('dialog');
      const result = await axe.run(dialog, axeOptions);
      return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
    }

    it('while loading', async () => {
      render(<EditUserModal {...baseProps} listUserKeys={() => new Promise<ApiKey[]>(() => {})} />);
      clearActive();
      expect(summary()).toBeInTheDocument();
      expect(await dialogViolations()).toStrictEqual([]);
    });

    it('with two keys listed', async () => {
      const keys = [makeKey(), makeKey({ id: 'k-9', name: 'Second', scopes: [] })];
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.resolve(keys)} />);
      await clearActiveAndSettle();
      expect(screen.getAllByTestId('deactivate-key')).toHaveLength(2);
      expect(await dialogViolations()).toStrictEqual([]);
    });

    it('with no keys', async () => {
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.resolve([])} />);
      await clearActiveAndSettle();
      expect(summary().textContent).toBe('This user has no active API keys.');
      expect(await dialogViolations()).toStrictEqual([]);
    });

    it('after a failure', async () => {
      render(<EditUserModal {...baseProps} listUserKeys={() => Promise.reject(new Error('down'))} />);
      await clearActiveAndSettle();
      expect(screen.getByTestId('deactivate-keys-retry')).toBeInTheDocument();
      expect(await dialogViolations()).toStrictEqual([]);
    });
  });
});
