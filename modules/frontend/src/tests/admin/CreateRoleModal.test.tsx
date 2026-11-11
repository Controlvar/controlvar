import React, { useState } from 'react';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
// The dialog makes `#__next` inert and portals beside it, so every render is
// mounted the way Next mounts the page (#369).
import { APP_ROOT_ID, renderInAppRoot as render } from '@/tests/helpers/renderInAppRoot';
import { CreateRoleModal } from '@modules/components/admin/roles/CreateRoleModal';
import { RbacService } from '@modules/rbac';
import { makeRole } from './rbacFixtures';
import { shapeErrors } from '@/tests/helpers/openapiShape';
import { ApiError } from '@/services/api';

jest.mock('@modules/rbac');
const mockCreateRole = RbacService.createRole as jest.Mock;
const mockUpdateRole = RbacService.updateRole as jest.Mock;

beforeEach(() => {
  jest.clearAllMocks();
});

const mockRole = makeRole({
  name: 'existing-role',
  description: 'An existing role',
  permissions: [{ resource: 'experiment', actions: ['read'] }],
});

describe('CreateRoleModal', () => {
  it('renders modal when isOpen=true', () => {
    render(
      <CreateRoleModal
        isOpen={true}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />
    );
    expect(screen.getByTestId('create-role-modal')).toBeInTheDocument();
  });

  it('does not render when isOpen=false', () => {
    render(
      <CreateRoleModal
        isOpen={false}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />
    );
    expect(screen.queryByTestId('create-role-modal')).not.toBeInTheDocument();
  });

  it('shows name and description inputs', () => {
    render(
      <CreateRoleModal
        isOpen={true}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />
    );
    expect(screen.getByTestId('role-name-input')).toBeInTheDocument();
    expect(screen.getByTestId('role-description-input')).toBeInTheDocument();
  });

  it('shows permission checkboxes grid', () => {
    render(
      <CreateRoleModal
        isOpen={true}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />
    );
    expect(screen.getByTestId('permission-checkbox-grid')).toBeInTheDocument();
  });

  it('submit disabled when name is empty', () => {
    render(
      <CreateRoleModal
        isOpen={true}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />
    );
    const submitButton = screen.getByTestId('modal-submit-button');
    expect(submitButton).toBeDisabled();
  });

  it('calls RbacService.createRole with correct data on submit in create mode', async () => {
    mockCreateRole.mockResolvedValue(makeRole({ name: 'new-role' }));

    const onSuccess = jest.fn();
    render(
      <CreateRoleModal
        isOpen={true}
        onClose={jest.fn()}
        onSuccess={onSuccess}
      />
    );

    fireEvent.change(screen.getByTestId('role-name-input'), {
      target: { value: 'new-role' },
    });
    fireEvent.change(screen.getByTestId('role-description-input'), {
      target: { value: 'A new role' },
    });

    // Check a permission
    const permCheckbox = screen.getByTestId('permission-checkbox-experiment:read');
    fireEvent.click(permCheckbox);
    fireEvent.click(screen.getByTestId('permission-checkbox-audit_log:read'));

    fireEvent.click(screen.getByTestId('modal-submit-button'));

    await waitFor(() => {
      expect(mockCreateRole).toHaveBeenCalledWith({
        name: 'new-role',
        description: 'A new role',
        permissions: [
          { resource: 'experiment', actions: ['read'] },
          { resource: 'audit_log', actions: ['read'] },
        ],
      });
    });
    // What goes on the wire is what the API's CustomRoleCreate accepts.
    expect(shapeErrors('CustomRoleCreate', mockCreateRole.mock.calls[0][0])).toEqual([]);

    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalled();
    });
  });

  it('populates form with existing role data in edit mode', () => {
    render(
      <CreateRoleModal
        isOpen={true}
        role={mockRole}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />
    );
    expect(screen.getByTestId('role-name-input')).toHaveValue('existing-role');
    expect(screen.getByTestId('role-description-input')).toHaveValue('An existing role');
  });

  it('calls RbacService.updateRole in edit mode', async () => {
    mockUpdateRole.mockResolvedValue({
      ...mockRole,
      description: 'Updated description',
    });

    const onSuccess = jest.fn();
    render(
      <CreateRoleModal
        isOpen={true}
        role={mockRole}
        onClose={jest.fn()}
        onSuccess={onSuccess}
      />
    );

    fireEvent.change(screen.getByTestId('role-description-input'), {
      target: { value: 'Updated description' },
    });

    fireEvent.click(screen.getByTestId('modal-submit-button'));

    await waitFor(() => {
      expect(mockUpdateRole).toHaveBeenCalledWith(
        'existing-role',
        { description: 'Updated description', permissions: [{ resource: 'experiment', actions: ['read'] }] }
      );
      expect(shapeErrors('CustomRoleUpdate', mockUpdateRole.mock.calls[0][1])).toEqual([]);
    });

    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalled();
    });
  });

  it('shows the API\'s refusal in an alert, and stays open (#891)', async () => {
    const refusal =
      'export (read) cannot be granted. A custom role or a direct grant can hold only ' +
      'experiment (create, read, update, delete, list), feature_flag (create, read, update, delete, list) ' +
      'and audit_log (read)';
    mockCreateRole.mockRejectedValue(
      new ApiError({
        status: 422,
        detail: [{ type: 'not_grantable', loc: ['body', 'permissions'], msg: refusal }],
      }),
    );
    const onSuccess = jest.fn();
    render(<CreateRoleModal isOpen={true} onClose={jest.fn()} onSuccess={onSuccess} />);
    fireEvent.change(screen.getByTestId('role-name-input'), { target: { value: 'bad-role' } });
    fireEvent.click(screen.getByTestId('modal-submit-button'));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(`permissions: ${refusal}`);
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it('shows a stored pair that can no longer be granted, and leaves it out of the PUT body (#891)', async () => {
    const stored = makeRole({
      name: 'old-role',
      permissions: [
        { resource: 'experiment', actions: ['read'] },
        { resource: 'export', actions: ['read'] },
        { resource: 'report', actions: ['read', 'list'] },
      ],
    });
    mockUpdateRole.mockResolvedValue(stored);
    render(<CreateRoleModal isOpen={true} role={stored} onClose={jest.fn()} onSuccess={jest.fn()} />);
    expect(screen.getByTestId('legacy-permissions-note')).toHaveTextContent(
      'Not grantable, removed when you save: export (read), report (read, list). They give no permission.',
    );
    expect(screen.getByTestId('permission-checkbox-experiment:read')).toBeChecked();
    fireEvent.click(screen.getByTestId('modal-submit-button'));
    await waitFor(() => expect(mockUpdateRole).toHaveBeenCalled());
    expect(mockUpdateRole.mock.calls[0][1].permissions).toEqual([{ resource: 'experiment', actions: ['read'] }]);
  });

  // Check #47: an edit says who it reaches, for 0, 1 and 3 users (#891).
  it.each([
    [0, 'No user has this role.'],
    [1, 'Changes apply at once to the 1 user who has this role.'],
    [3, 'Changes apply at once to the 3 users who have this role.'],
  ])('editing a role with %i users says who the change reaches', (userCount, text) => {
    const role = makeRole({ name: 'data-scientist', user_count: userCount });
    render(<CreateRoleModal isOpen={true} role={role} onClose={jest.fn()} onSuccess={jest.fn()} />);
    expect(screen.getByTestId('role-users-affected')).toHaveTextContent(new RegExp(`^${text.replace(/\./g, '\\.')}$`));
  });

  it('creating a role says nothing about users', () => {
    render(<CreateRoleModal isOpen={true} onClose={jest.fn()} onSuccess={jest.fn()} />);
    expect(screen.queryByTestId('role-users-affected')).toBeNull();
  });

  it('names the audit log checkbox "Read the audit log", inside its group', () => {
    render(<CreateRoleModal isOpen={true} onClose={jest.fn()} onSuccess={jest.fn()} />);
    const group = screen.getByRole('group', { name: 'Audit log' });
    expect(within(group).getByRole('checkbox', { name: 'Read the audit log' })).toBeInTheDocument();
  });

  it('is a labelled dialog whose close button has a name and a readable colour', () => {
    render(<CreateRoleModal isOpen={true} onClose={jest.fn()} onSuccess={jest.fn()} />);
    expect(screen.getByRole('dialog', { name: 'Create Role' })).toBeInTheDocument();
    const close = screen.getByRole('button', { name: 'Close' });
    expect(close.className).not.toMatch(/slate-400/);
  });

  it('calls onClose when cancel clicked', () => {
    const onClose = jest.fn();
    render(
      <CreateRoleModal
        isOpen={true}
        onClose={onClose}
        onSuccess={jest.fn()}
      />
    );
    fireEvent.click(screen.getByTestId('modal-cancel-button'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

/** A page with the real opener: the button that opens the dialog, as the roles page has. */
function OpenedFromButton({ role = null }: { role?: typeof mockRole | null }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Create role
      </button>
      <CreateRoleModal isOpen={open} role={role} onClose={() => setOpen(false)} onSuccess={() => setOpen(false)} />
    </>
  );
}

describe('CreateRoleModal on the shared Dialog (#369)', () => {
  const appRoot = () => document.getElementById(APP_ROOT_ID) as HTMLElement;

  async function open(role: typeof mockRole | null = null) {
    const user = userEvent.setup();
    render(<OpenedFromButton role={role} />);
    const opener = screen.getByRole('button', { name: 'Create role' });
    await user.click(opener);
    return { user, opener };
  }

  it('opens as a modal dialog named "Create Role", or "Edit Role" for an existing role', async () => {
    await open();
    const dialog = screen.getByRole('dialog', { name: 'Create Role' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toBe(screen.getByTestId('create-role-modal'));
  });

  it('is named "Edit Role" for an existing role', async () => {
    await open(mockRole);
    expect(screen.getByRole('dialog', { name: 'Edit Role' })).toBeInTheDocument();
  });

  it('the page behind it is inert while it is open, and not after', async () => {
    const { user } = await open();
    expect(appRoot()).toHaveAttribute('inert');
    expect(appRoot().contains(screen.getByTestId('create-role-modal'))).toBe(false);
    await user.click(screen.getByTestId('modal-cancel-button'));
    expect(appRoot()).not.toHaveAttribute('inert');
  });

  it('Escape now closes it, once, and focus returns to the opener', async () => {
    const { user, opener } = await open();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByTestId('create-role-modal')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();

    await user.click(opener);
    await user.keyboard('{Escape}');
    expect(screen.queryByTestId('create-role-modal')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('Escape calls onClose exactly once', () => {
    const onClose = jest.fn();
    render(<CreateRoleModal isOpen onClose={onClose} onSuccess={jest.fn()} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a click on the backdrop no longer closes it', () => {
    const onClose = jest.fn();
    render(<CreateRoleModal isOpen onClose={onClose} onSuccess={jest.fn()} />);
    // The Dialog's overlay; before #369 the role dialog's own `absolute inset-0` layer.
    const backdrop = (document.querySelector('[data-dialog-root]') ??
      screen.getByTestId('create-role-modal').querySelector('.absolute.inset-0')) as HTMLElement;
    expect(backdrop).not.toBeNull();
    fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByTestId('create-role-modal')).toBeInTheDocument();
  });

  it('focus returns to the opener after Cancel and after the close button', async () => {
    const { user, opener } = await open();
    await user.click(screen.getByTestId('modal-cancel-button'));
    expect(opener).toHaveFocus();
    await user.click(opener);
    await user.click(screen.getByTestId('modal-close-x-button'));
    expect(opener).toHaveFocus();
  });

  it('the first focus is the name field to create', async () => {
    await open();
    expect(screen.getByTestId('role-name-input')).toHaveFocus();
  });

  it('the first focus is the description to edit, because the name is fixed then', async () => {
    await open(mockRole);
    expect(screen.getByTestId('role-description-input')).toHaveFocus();
  });

  it('the dialog has no axe violations, and the rule that names it ran on it', async () => {
    await open();
    const dialog = screen.getByRole('dialog');
    expect(document.body).toContainElement(dialog);
    const result = await axe.run(dialog, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toStrictEqual([]);
    expect(result.passes.map((p) => p.id)).toContain('aria-dialog-name');
  });
});
