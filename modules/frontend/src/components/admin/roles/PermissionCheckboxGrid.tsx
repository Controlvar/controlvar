import React, { useId } from 'react';
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

const ALREADY_HELD = 'Every user already has this.';

/**
 * What each pair lets its holder do (#891): the routes whose check reads it,
 * from the API's class-A inventory. Shown under each checkbox and linked to it
 * with `aria-describedby`.
 */
export const PERMISSION_DESCRIPTIONS: Readonly<Record<string, string>> = {
  'experiment:create':
    'Create and clone experiments. Also: read the global holdout, manage exclusion groups (not archive them), create segments.',
  'experiment:read': ALREADY_HELD,
  'experiment:update':
    'Change, schedule, start, pause, complete and archive any experiment, and change segments.',
  'experiment:delete': 'Delete draft experiments, and archive segments.',
  'experiment:list': ALREADY_HELD,
  'feature_flag:create': 'Create flags.',
  'feature_flag:read': ALREADY_HELD,
  'feature_flag:update': 'Change any flag: on and off, targeting, values, rollout and rollout schedules.',
  'feature_flag:delete': 'Delete any flag.',
  'feature_flag:list': ALREADY_HELD,
  'audit_log:read':
    "Read the audit log, including other users' entries, its statistics and each item's history. " +
    'Without it, Developer and Viewer users read only their own entries. ' +
    'It does not give the compliance audit events, reports or exports.',
};

/** Said once, after the grid, in reading order. */
export const SERVER_KEY_NOTE =
  'With Feature flags: Update, a user can also create server-side SDK keys (API keys with the sdk:ruleset scope) ' +
  'through the API. Assigning users in a batch with such a key also needs Experiments: Update.';

export function PermissionCheckboxGrid({
  selectedPermissions,
  onChange,
  readOnly = false,
}: PermissionCheckboxGridProps) {
  const uid = useId().replace(/[^A-Za-z0-9_-]/g, '');
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
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {group.actions.map((action) => {
              const permission = permissionKey(group.resource, action);
              const descriptionId = `${uid}-${group.resource}-${action}-description`;
              return (
                <div key={permission}>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      data-testid={`permission-checkbox-${permission}`}
                      checked={selectedPermissions.includes(permission)}
                      onChange={() => handleToggle(permission)}
                      disabled={readOnly}
                      aria-describedby={descriptionId}
                      className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500 disabled:opacity-50"
                    />
                    <span className="text-sm text-slate-700">{actionLabel(group.resource, action)}</span>
                  </label>
                  <p
                    id={descriptionId}
                    data-testid={`permission-description-${permission}`}
                    className="ml-6 mt-0.5 text-xs text-slate-600"
                  >
                    {PERMISSION_DESCRIPTIONS[permission]}
                  </p>
                </div>
              );
            })}
          </div>
        </fieldset>
      ))}
      <p data-testid="permission-server-key-note" className="text-sm text-slate-700">
        {SERVER_KEY_NOTE}
      </p>
    </div>
  );
}
