import React, { useState } from 'react';
import { withModule } from '@/components/ModuleNotice';
import { MODULES } from '@/services/modules';
import { AdminLayout } from '@/components/admin/AdminLayout';
import { RoleTable } from '@modules/components/admin/roles/RoleTable';
import { CreateRoleModal } from '@modules/components/admin/roles/CreateRoleModal';
import { CustomRole } from '@/types/admin';
import { withAdminGuard } from '@/components/admin/withAdminGuard';
import { docsUrl } from '@/services/docs';

export function RoleManagementPage() {
  const [modalOpen, setModalOpen] = useState(false);
  const [selectedRole, setSelectedRole] = useState<CustomRole | null>(null);
  const [tableKey, setTableKey] = useState(0);

  const handleCreateRole = () => {
    setSelectedRole(null);
    setModalOpen(true);
  };

  const handleEditRole = (role: CustomRole) => {
    setSelectedRole(role);
    setModalOpen(true);
  };

  const handleModalClose = () => {
    setModalOpen(false);
    setSelectedRole(null);
  };

  const handleModalSuccess = () => {
    setModalOpen(false);
    setSelectedRole(null);
    // Force the RoleTable to re-fetch by changing its key
    setTableKey((k) => k + 1);
  };

  return (
    <AdminLayout title="Role Management" currentPath="/admin/roles">
      <div data-testid="role-management-page" className="space-y-6">
        {/* Page Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold text-slate-900">Role Management</h1>
            <p data-testid="roles-subtitle" className="text-sm text-slate-600 mt-1">
              {"Define roles that add permissions to a user's built-in role."}
            </p>
            {/* Custom roles and direct grants take effect (#891): the checks read them. */}
            <p data-testid="roles-access-note" className="text-sm text-slate-700 mt-1">
              A custom role adds its permissions to the built-in role of everyone who has it, from their
              next request, and never takes one away. Assign roles and grant permissions to a user through{' '}
              <a href={docsUrl('api/rbac')} className="text-blue-700 underline hover:text-blue-900">
                the RBAC API
              </a>
              .
            </p>
          </div>
          <button
            data-testid="create-role-button"
            onClick={handleCreateRole}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            Create Role
          </button>
        </div>

        {/* Role Table */}
        <RoleTable key={tableKey} onEditRole={handleEditRole} />

        {/* Create / Edit Modal */}
        <CreateRoleModal
          isOpen={modalOpen}
          role={selectedRole}
          onClose={handleModalClose}
          onSuccess={handleModalSuccess}
        />
      </div>
    </AdminLayout>
  );
}

export default withAdminGuard(
  withModule(RoleManagementPage, {
    title: 'Custom Roles',
    module: MODULES.RBAC,
    description:
      'Define roles beyond the four built-in ones and grant permissions directly to a user. ' +
      "Both add to what the user's built-in role allows.",
  }),
  { requiredRole: 'ADMIN' },
);
