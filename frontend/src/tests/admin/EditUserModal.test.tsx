import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import axe from 'axe-core';
import {
  DEACTIVATE_WARNING,
  EditUserModal,
  FORBIDDEN_MESSAGE,
  NOT_FOUND_MESSAGE,
  SELF_EDIT_NOTE,
  editErrorMessage,
} from '@/components/admin/users/EditUserModal';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { ApiError } from '@/services/api';
import { AdminUser } from '@/types/admin';

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

describe('EditUserModal accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function violations(node: Element) {
    const result = await axe.run(node, axeOptions);
    return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
  }

  it('the dialog has no axe violations: editing another user, with the warning and an error', async () => {
    const onSave = jest.fn(() =>
      Promise.reject(new ApiError({ status: 403, detail: 'Not enough permissions' })),
    );
    const { container } = render(
      <EditUserModal isOpen user={mockUser} currentUserId="admin-1" onClose={jest.fn()} onSave={onSave} />,
    );
    fireEvent.click(screen.getByTestId('edit-active-toggle'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('edit-save-button'));
    });
    expect(screen.getByTestId('edit-error-message')).toBeInTheDocument();
    expect(await violations(container)).toStrictEqual([]);
  });

  it('the dialog has no axe violations on the own row', async () => {
    const { container } = render(
      <EditUserModal isOpen user={mockUser} currentUserId="user-1" onClose={jest.fn()} onSave={jest.fn()} />,
    );
    expect(await violations(container)).toStrictEqual([]);
  });
});
