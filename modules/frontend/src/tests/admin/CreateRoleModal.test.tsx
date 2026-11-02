import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
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
      'Not grantable, removed when you save: export (read), report (read, list). No permission check reads them.',
    );
    expect(screen.getByTestId('permission-checkbox-experiment:read')).toBeChecked();
    fireEvent.click(screen.getByTestId('modal-submit-button'));
    await waitFor(() => expect(mockUpdateRole).toHaveBeenCalled());
    expect(mockUpdateRole.mock.calls[0][1].permissions).toEqual([{ resource: 'experiment', actions: ['read'] }]);
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
