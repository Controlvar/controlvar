/**
 * /admin/roles says that custom roles and direct permission grants do not
 * change what a user can do yet (#891): no permission check reads them, and
 * each user's built-in role decides.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { RoleManagementPage } from '@modules/pages/admin/roles';
import { RbacService } from '@modules/rbac';
import { makeRole } from '../admin/rbacFixtures';
import { shapeErrors } from '@/tests/helpers/openapiShape';

jest.mock('@modules/rbac');

jest.mock('@/components/admin/AdminLayout', () => ({
  AdminLayout: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const mockListRoles = RbacService.listRoles as jest.Mock;

describe('RoleManagementPage', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockListRoles.mockResolvedValue([]);
  });

  it('says that custom roles and direct grants are recorded and do not change access yet', async () => {
    render(<RoleManagementPage />);
    expect(screen.getByTestId('roles-access-note')).toHaveTextContent(
      "Custom roles and direct permission grants are recorded, but they do not change what anyone can do yet: each user's built-in role decides."
    );
    // The page loaded its table, so the note is shown with it, not instead of it.
    expect(await screen.findByText('No custom roles found')).toBeInTheDocument();
    expect(mockListRoles).toHaveBeenCalledTimes(1);
  });

  it('saves a custom role the way the API takes it: a list of {resource, actions} (#891)', async () => {
    (RbacService.createRole as jest.Mock).mockResolvedValue(makeRole({ name: 'release-manager' }));
    render(<RoleManagementPage />);
    await screen.findByText('No custom roles found');
    fireEvent.click(screen.getByTestId('create-role-button'));
    fireEvent.change(screen.getByTestId('role-name-input'), { target: { value: 'release-manager' } });
    fireEvent.click(screen.getByTestId('permission-checkbox-feature_flag:read'));
    fireEvent.click(screen.getByTestId('permission-checkbox-feature_flag:update'));
    fireEvent.click(screen.getByTestId('modal-submit-button'));

    await waitFor(() => expect(RbacService.createRole).toHaveBeenCalledTimes(1));
    const body = (RbacService.createRole as jest.Mock).mock.calls[0][0];
    expect(body.permissions).toEqual([{ resource: 'feature_flag', actions: ['read', 'update'] }]);
    expect(shapeErrors('CustomRoleCreate', body)).toEqual([]);
    // The table is loaded again after the save.
    await waitFor(() => expect(mockListRoles).toHaveBeenCalledTimes(2));
  });

  it('shows a stored role\'s grants checked when it is edited (#891)', async () => {
    mockListRoles.mockResolvedValue([
      makeRole({ name: 'releaser', permissions: [{ resource: 'feature_flag', actions: ['update'] }] }),
    ]);
    render(<RoleManagementPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit releaser' }));
    expect(screen.getByTestId('permission-checkbox-feature_flag:update')).toBeChecked();
    expect(screen.getByTestId('permission-checkbox-experiment:update')).not.toBeChecked();
  });
});
