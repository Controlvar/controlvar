import { PermissionGrant } from '@/types/admin';

/**
 * The (resource, action) pairs a custom role may hold: the API refuses any
 * other with a 422 (`GRANTABLE` in `modules/backend/app/schemas/rbac.py`).
 * The grid offers exactly these, in this order.
 */
export const GRANTABLE_GROUPS: ReadonlyArray<{
  resource: string;
  label: string;
  actions: ReadonlyArray<string>;
}> = [
  {
    resource: 'experiment',
    label: 'Experiments',
    actions: ['create', 'read', 'update', 'delete', 'list'],
  },
  {
    resource: 'feature_flag',
    label: 'Feature flags',
    actions: ['create', 'read', 'update', 'delete', 'list'],
  },
  { resource: 'audit_log', label: 'Audit log', actions: ['read'] },
];

/** The checkbox key of a pair, e.g. `experiment:read`. */
export function permissionKey(resource: string, action: string): string {
  return `${resource}:${action}`;
}

const GRANTABLE_KEYS = new Set(
  GRANTABLE_GROUPS.flatMap((g) => g.actions.map((a) => permissionKey(g.resource, a))),
);

/**
 * Splits a stored role's grants into the keys the grid can show and the pairs
 * it cannot (a role stored before the API refused them reads back unchanged).
 * `legacy` is the text for the dialog: `export (read), report (read, list)`.
 */
export function grantsToKeys(grants: PermissionGrant[]): { keys: string[]; legacy: string } {
  const keys: string[] = [];
  const refused = new Map<string, string[]>();
  for (const grant of grants) {
    for (const action of grant.actions) {
      const key = permissionKey(grant.resource, action);
      if (GRANTABLE_KEYS.has(key)) {
        if (!keys.includes(key)) keys.push(key);
      } else {
        const list = refused.get(grant.resource) ?? [];
        if (!list.includes(action)) list.push(action);
        refused.set(grant.resource, list);
      }
    }
  }
  const legacy = Array.from(refused, ([resource, actions]) => `${resource} (${actions.join(', ')})`).join(', ');
  return { keys, legacy };
}

/** The request body's `permissions` for the checked keys: one grant per resource. */
export function keysToGrants(keys: string[]): PermissionGrant[] {
  const grants: PermissionGrant[] = [];
  for (const group of GRANTABLE_GROUPS) {
    const actions = group.actions.filter((a) => keys.includes(permissionKey(group.resource, a)));
    if (actions.length > 0) grants.push({ resource: group.resource, actions });
  }
  return grants;
}

/** A role's grants as text for the table: `experiment: create, update · audit_log: read`. */
export function describeGrants(grants: PermissionGrant[]): string {
  return grants
    .filter((g) => g.actions.length > 0)
    .map((g) => `${g.resource}: ${g.actions.join(', ')}`)
    .join(' · ');
}
