import { CustomRole } from '@/types/admin';
import { shapeErrors } from '@/tests/helpers/openapiShape';

/**
 * A role as `GET /api/v1/rbac/roles` returns it. Checked against the
 * snapshot's `CustomRoleResponse` when it is made, so a fixture that drifts
 * from the API fails here, not in the page that reads it.
 */
export function makeRole(overrides: Partial<CustomRole> = {}): CustomRole {
  const role: CustomRole = {
    id: 'role-1',
    name: 'data-scientist',
    description: 'Runs experiments',
    is_system_role: false,
    permissions: [
      { resource: 'experiment', actions: ['create', 'read', 'update'] },
      { resource: 'audit_log', actions: ['read'] },
    ],
    created_at: '2026-09-01T00:00:00Z',
    user_count: 3,
    ...overrides,
  };
  const errors = shapeErrors('CustomRoleResponse', role);
  if (errors.length) throw new Error(`fixture is not a CustomRoleResponse:\n${errors.join('\n')}`);
  return role;
}
