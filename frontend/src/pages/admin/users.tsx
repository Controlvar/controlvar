import React, { useCallback, useRef, useState } from 'react';
import { AdminLayout } from '@/components/admin/AdminLayout';
import { UserTable } from '@/components/admin/users/UserTable';
import { InviteUserModal } from '@/components/admin/users/InviteUserModal';
import { EditUserCloseOptions, EditUserModal } from '@/components/admin/users/EditUserModal';
import { EffectivePermissionsModal } from '@/components/admin/users/EffectivePermissionsModal';
import { AdminUser, AdminUserPatch } from '@/types/admin';
import { AdminService } from '@/services/admin';
import { withAdminGuard } from '@/components/admin/withAdminGuard';
import { RequiresModule, useModule } from '@/contexts/ModulesContext';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { MODULES } from '@/services/modules';

export function UserManagementPage() {
  const [inviteOpen, setInviteOpen] = useState(false);
  const [editUser, setEditUser] = useState<AdminUser | null>(null);
  const [permissionsUser, setPermissionsUser] = useState<AdminUser | null>(null);
  // Bumped to reload the table after an invite or an edit. The table keeps its
  // page and search term; it is not remounted.
  const [reloadToken, setReloadToken] = useState(0);
  // The row whose Edit button gets focus back when the dialog closes.
  const [focusUserId, setFocusUserId] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  // The Permissions button that opened the dialog; it gets focus back on close.
  const permissionsTrigger = useRef<HTMLButtonElement | null>(null);
  // The dialog reads a route of the rbac module: no button, no request without it.
  const hasRbac = useModule(MODULES.RBAC);
  const currentUserId = useOptionalAuth()?.user?.id ?? null;

  const handleInviteSuccess = () => {
    setReloadToken((n) => n + 1);
  };

  const handleEdit = (user: AdminUser) => {
    setStatus('');
    setEditUser(user);
  };

  // Rejects with the request's error, which the dialog shows; it stays open.
  const handleSaveUser = async (userId: string, changes: AdminUserPatch) => {
    await AdminService.updateUser(userId, changes);
    const name = editUser?.username ?? 'the user';
    setEditUser(null);
    setFocusUserId(userId);
    setReloadToken((n) => n + 1);
    setStatus(`Saved changes to ${name}.`);
  };

  const handleCloseEdit = (options?: EditUserCloseOptions) => {
    if (editUser) setFocusUserId(editUser.id);
    setEditUser(null);
    if (options?.reload) setReloadToken((n) => n + 1);
  };

  const handleOpenPermissions = (user: AdminUser, trigger: HTMLButtonElement) => {
    permissionsTrigger.current = trigger;
    setPermissionsUser(user);
  };

  const handleClosePermissions = useCallback(() => {
    setPermissionsUser(null);
    permissionsTrigger.current?.focus();
    permissionsTrigger.current = null;
  }, []);

  const handleFocusHandled = useCallback(() => setFocusUserId(null), []);

  return (
    <AdminLayout title="User Management" currentPath="/admin/users">
      <div data-testid="user-management-page">
        {/* Page Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-xl font-semibold text-slate-900">User Management</h1>
            <p className="text-sm text-slate-500 mt-1">
              Manage platform users, roles, and permissions.
            </p>
          </div>
          <button
            data-testid="invite-user-button"
            onClick={() => setInviteOpen(true)}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            Invite User
          </button>
        </div>

        {/* Announces a saved edit to screen readers and shows it on screen. */}
        <div
          role="status"
          aria-live="polite"
          data-testid="user-save-status"
          className={status ? 'mb-4 p-3 bg-green-50 border border-green-200 rounded-md text-sm text-green-800' : ''}
        >
          {status}
        </div>

        {/* User Table */}
        <UserTable
          onEdit={handleEdit}
          onPermissions={hasRbac ? handleOpenPermissions : undefined}
          reloadToken={reloadToken}
          focusUserId={focusUserId}
          onFocusHandled={handleFocusHandled}
        />

        {/* Invite Modal */}
        <InviteUserModal
          isOpen={inviteOpen}
          onClose={() => setInviteOpen(false)}
          onSuccess={handleInviteSuccess}
        />

        {/* Edit Modal */}
        <EditUserModal
          isOpen={editUser !== null}
          user={editUser}
          currentUserId={currentUserId}
          onClose={handleCloseEdit}
          onSave={handleSaveUser}
        />

        {/* Effective Permissions Modal — reads `/api/v1/rbac/users/{id}/permissions`,
            a route of the rbac module, so it is not mounted at all unless that
            module is installed. */}
        <RequiresModule name={MODULES.RBAC}>
          <EffectivePermissionsModal
            isOpen={permissionsUser !== null}
            userId={permissionsUser?.id ?? null}
            userEmail={permissionsUser?.email ?? undefined}
            onClose={handleClosePermissions}
          />
        </RequiresModule>
      </div>
    </AdminLayout>
  );
}

// Superuser only, like every /api/v1/admin route this page calls: a superuser
// whose role is not ADMIN can open it too (see withAdminGuard's docstring).
export default withAdminGuard(UserManagementPage);
