import React, { useState, useEffect } from 'react';
import { CustomRole } from '@/types/admin';
import { RbacService } from '@modules/rbac';
import { PermissionCheckboxGrid } from './PermissionCheckboxGrid';
import { grantsToKeys, keysToGrants } from './grantable';

interface CreateRoleModalProps {
  isOpen: boolean;
  role?: CustomRole | null;
  onClose: () => void;
  onSuccess: () => void;
}

export function CreateRoleModal({
  isOpen,
  role,
  onClose,
  onSuccess,
}: CreateRoleModalProps) {
  const isEditMode = Boolean(role);

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [permissions, setPermissions] = useState<string[]>([]);
  // Pairs the role holds that can no longer be granted; saving drops them.
  const [legacy, setLegacy] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Populate form when role prop changes (edit mode)
  useEffect(() => {
    if (role) {
      setName(role.name);
      setDescription(role.description ?? '');
      const stored = grantsToKeys(role.permissions);
      setPermissions(stored.keys);
      setLegacy(stored.legacy);
    } else {
      setName('');
      setDescription('');
      setPermissions([]);
      setLegacy('');
    }
    setError(null);
  }, [role, isOpen]);

  if (!isOpen) {
    return null;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    setSubmitting(true);
    setError(null);

    try {
      if (isEditMode && role) {
        await RbacService.updateRole(role.name, {
          description,
          permissions: keysToGrants(permissions),
        });
      } else {
        await RbacService.createRole({ name, description, permissions: keysToGrants(permissions) });
      }
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      data-testid="create-role-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="role-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center"
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black bg-opacity-40"
        onClick={onClose}
      />

      {/* Modal panel */}
      <div className="relative bg-white rounded-xl shadow-xl w-full max-w-2xl mx-4 flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-200 flex items-center justify-between flex-shrink-0">
          <h2 id="role-modal-title" className="text-lg font-semibold text-slate-900">
            {isEditMode ? 'Edit Role' : 'Create Role'}
          </h2>
          <button
            data-testid="modal-close-x-button"
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="text-slate-600 hover:text-slate-900 transition-colors"
          >
                        <svg
              className="w-5 h-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        {/* Body */}
        <form onSubmit={handleSubmit} className="flex flex-col flex-1 overflow-hidden">
          <div className="px-6 py-4 space-y-4 overflow-y-auto flex-1">
            {error && (
              <div
                role="alert"
                data-testid="modal-error"
                className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 text-sm text-red-700"
              >
                {error}
              </div>
            )}

            {/* Name */}
            <div>
              <label
                htmlFor="role-name"
                className="block text-sm font-medium text-slate-700 mb-1"
              >
                Role Name <span className="text-red-500">*</span>
              </label>
              <input
                id="role-name"
                data-testid="role-name-input"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. data-scientist"
                disabled={isEditMode}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-slate-50 disabled:text-slate-500"
              />
            </div>

            {/* Description */}
            <div>
              <label
                htmlFor="role-description"
                className="block text-sm font-medium text-slate-700 mb-1"
              >
                Description
              </label>
              <textarea
                id="role-description"
                data-testid="role-description-input"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Describe what this role is for..."
                rows={3}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
              />
            </div>

            {/* Permissions */}
            <div>
              <p className="block text-sm font-medium text-slate-700 mb-2">
                Permissions
              </p>
              {legacy && (
                <p data-testid="legacy-permissions-note" className="mb-2 text-sm text-slate-700">
                  {`Not grantable, removed when you save: ${legacy}. No permission check reads them.`}
                </p>
              )}
              <PermissionCheckboxGrid
                selectedPermissions={permissions}
                onChange={setPermissions}
              />
            </div>
          </div>

          {/* Footer */}
          <div className="px-6 py-4 border-t border-slate-200 flex justify-end gap-3 flex-shrink-0">
            <button
              data-testid="modal-cancel-button"
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 transition-colors"
            >
              Cancel
            </button>
            <button
              data-testid="modal-submit-button"
              type="submit"
              disabled={!name.trim() || submitting}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting
                ? isEditMode
                  ? 'Saving...'
                  : 'Creating...'
                : isEditMode
                ? 'Save Changes'
                : 'Create Role'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
