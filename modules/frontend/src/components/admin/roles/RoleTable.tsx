import React, { useEffect, useState, useCallback } from 'react';
import { CustomRole } from '@/types/admin';
import { RbacService } from '@modules/rbac';
import { describeGrants } from './grantable';

interface RoleTableProps {
  onEditRole: (role: CustomRole) => void;
}

export function RoleTable({ onEditRole }: RoleTableProps) {
  const [roles, setRoles] = useState<CustomRole[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchRoles = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await RbacService.listRoles();
      setRoles(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch roles');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRoles();
  }, [fetchRoles]);

  const handleDelete = async (role: CustomRole) => {
    const confirmed = window.confirm(
      `Are you sure you want to delete the role "${role.name}"? This action cannot be undone.`
    );
    if (!confirmed) return;

    try {
      await RbacService.deleteRole(role.name);
      await fetchRoles();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete role');
    }
  };

  return (
    <div data-testid="role-table" className="bg-white rounded-xl border border-slate-200 overflow-x-auto">
      {loading && (
        <div data-testid="role-table-loading" className="p-8 flex justify-center">
          <div className="animate-spin h-6 w-6 rounded-full border-2 border-blue-600 border-t-transparent" />
          <span className="sr-only">Loading roles...</span>
        </div>
      )}

      {error && !loading && (
        <div
          data-testid="role-table-error"
          className="p-6 bg-red-50 border-b border-red-100 text-center"
        >
          <p className="text-red-700 font-medium">Failed to load roles</p>
          <p className="text-red-500 text-sm mt-1">{error}</p>
          <button
            onClick={fetchRoles}
            className="mt-3 text-sm text-blue-600 hover:underline"
          >
            Retry
          </button>
        </div>
      )}

      {!loading && !error && roles.length === 0 && (
        <div
          data-testid="role-table-empty"
          className="p-12 text-center"
        >
          <p className="text-slate-500 font-medium">No custom roles found</p>
          <p className="text-slate-600 text-sm mt-1">
            Create your first custom role to get started.
          </p>
        </div>
      )}

      {!loading && !error && roles.length > 0 && (
        <table className="min-w-full divide-y divide-slate-200">
          <thead className="bg-slate-50">
            <tr>
              <th className="px-6 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider">
                Role Name
              </th>
              <th className="px-6 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider">
                Description
              </th>
              <th className="px-6 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider">
                Permissions
              </th>
              <th className="px-6 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider">
                Users
              </th>
              <th className="px-6 py-3 text-right text-xs font-semibold text-slate-500 uppercase tracking-wider">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {roles.map((role) => (
              <tr key={role.name} className="hover:bg-slate-50 transition-colors">
                <td className="px-6 py-4">
                  <span className="text-sm font-medium text-slate-900">
                    {role.name}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-slate-600">
                    {role.description || '-'}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-slate-700">
                    {describeGrants(role.permissions) || 'None'}
                  </span>
                </td>
                <td className="px-6 py-4 text-sm text-slate-700">{role.user_count}</td>
                <td className="px-6 py-4 text-right space-x-2">
                  <button
                    data-testid="edit-role-button"
                    aria-label={`Edit ${role.name}`}
                    onClick={() => onEditRole(role)}
                    className="text-sm text-blue-600 hover:text-blue-800 font-medium transition-colors"
                  >
                    Edit
                  </button>
                  <button
                    data-testid="delete-role-button"
                    aria-label={`Delete ${role.name}`}
                    onClick={() => handleDelete(role)}
                    className="text-sm text-red-600 hover:text-red-800 font-medium transition-colors"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
