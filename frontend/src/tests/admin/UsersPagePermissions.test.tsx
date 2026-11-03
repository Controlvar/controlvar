/**
 * The Permissions button on /admin/users (#891): with the rbac module it opens
 * that user's effective permissions; without it there is no button and no
 * request to a route the core backend does not serve.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { UserManagementPage } from '@/pages/admin/users';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { CORE_PROFILE, ModulesInfo } from '@/services/modules';
import { AdminService } from '@/services/admin';
import { RbacService } from '@modules/rbac';
import { AdminUser } from '@/types/admin';

jest.mock('@/services/admin');
jest.mock('@modules/rbac');
jest.mock('@/components/admin/AdminLayout', () => ({
  AdminLayout: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const FULL: ModulesInfo = { profile: 'full', modules: ['rbac'], version: '1.0.0' };

const ada: AdminUser = {
  id: 'u-ada',
  username: 'ada',
  email: 'ada@example.com',
  role: 'DEVELOPER',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
};

beforeEach(() => {
  jest.clearAllMocks();
  (AdminService.listUsers as jest.Mock).mockResolvedValue({ items: [ada], total: 1, skip: 0, limit: 50 });
  (RbacService.getUserPermissions as jest.Mock).mockResolvedValue({
    user_id: 'u-ada',
    username: 'ada',
    base_role: 'developer',
    custom_roles: [],
    permissions: { experiment: ['read'] },
    is_superuser: false,
  });
});

function renderPage(info: ModulesInfo) {
  return render(
    <ModulesProvider initial={info}>
      <UserManagementPage />
    </ModulesProvider>,
  );
}

describe('Permissions on the Users page', () => {
  it('full profile: the row button opens that user\'s permissions; Escape closes and focus returns', async () => {
    renderPage(FULL);
    const button = await screen.findByRole('button', { name: 'Permissions for ada' });
    button.focus();
    fireEvent.click(button);

    expect(screen.getByRole('dialog', { name: 'Effective permissions' })).toBeInTheDocument();
    await waitFor(() => expect(RbacService.getUserPermissions).toHaveBeenCalledWith('u-ada'));
    expect(await screen.findByText('Experiments: read')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByTestId('effective-permissions-modal')).not.toBeInTheDocument();
    expect(button).toHaveFocus();
  });

  it('core profile: no button, and no request to the rbac route', async () => {
    renderPage(CORE_PROFILE);
    await screen.findByText('ada');
    expect(screen.queryByRole('button', { name: /Permissions for/ })).not.toBeInTheDocument();
    expect(screen.queryByTestId('effective-permissions-modal')).not.toBeInTheDocument();
    expect(RbacService.getUserPermissions).not.toHaveBeenCalled();
  });
});
