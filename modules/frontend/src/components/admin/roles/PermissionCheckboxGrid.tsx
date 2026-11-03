import React from 'react';
import { GRANTABLE_GROUPS, permissionKey } from './grantable';

interface PermissionCheckboxGridProps {
  /** Checked pairs as `resource:action`, e.g. `experiment:read`. */
  selectedPermissions: string[];
  onChange: (permissions: string[]) => void;
  readOnly?: boolean;
}

/** The label of one checkbox; the audit log has the one, and it says what it reads. */
function actionLabel(resource: string, action: string): string {
  if (resource === 'audit_log' && action === 'read') return 'Read the audit log';
  return action.charAt(0).toUpperCase() + action.slice(1);
}

export function PermissionCheckboxGrid({
  selectedPermissions,
  onChange,
  readOnly = false,
}: PermissionCheckboxGridProps) {
  const handleToggle = (permission: string) => {
    if (selectedPermissions.includes(permission)) {
      onChange(selectedPermissions.filter((p) => p !== permission));
    } else {
      onChange([...selectedPermissions, permission]);
    }
  };

  return (
    <div data-testid="permission-checkbox-grid" className="space-y-4">
      {GRANTABLE_GROUPS.map((group) => (
        <fieldset key={group.resource} className="border border-slate-200 rounded-lg p-4">
          <legend className="px-1 text-sm font-semibold text-slate-700">{group.label}</legend>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            {group.actions.map((action) => {
              const permission = permissionKey(group.resource, action);
              return (
                <label key={permission} className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    data-testid={`permission-checkbox-${permission}`}
                    checked={selectedPermissions.includes(permission)}
                    onChange={() => handleToggle(permission)}
                    disabled={readOnly}
                    className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500 disabled:opacity-50"
                  />
                  <span className="text-sm text-slate-700">{actionLabel(group.resource, action)}</span>
                </label>
              );
            })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}
