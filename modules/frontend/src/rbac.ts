/**
 * The rbac module: custom roles and direct permission grants (`/api/v1/rbac/*`).
 *
 * These five calls used to sit in `frontend/src/services/admin.ts`, a core
 * file, which meant a core build shipped code calling routes its backend does
 * not serve. They now live in the modules tree (`modules/frontend/src`) and
 * are reached through the `@modules/rbac` alias, which resolves to
 * `frontend/src/modules-stub/rbac.ts` when that tree is absent.
 *
 * Callers must gate on `useModule(MODULES.RBAC)` before invoking any of
 * these — the stub throws rather than pretending to have answered.
 */
import { apiFetch } from '@/services/api';
import { CustomRole, PermissionGrant } from '@/types/admin';

/** `CustomRoleCreate`: permissions are `{resource, actions}` objects, never strings. */
export interface CreateRoleRequest {
  name: string;
  description?: string | null;
  permissions: PermissionGrant[];
}

/** `CustomRoleUpdate`: the role is named in the path, so the body has no name. */
export interface UpdateRoleRequest {
  description?: string | null;
  permissions?: PermissionGrant[] | null;
}

/** `EffectivePermissionsResponse`: `permissions` maps a resource to its actions. */
export interface UserPermissionsResponse {
  user_id: string;
  username: string;
  base_role: string;
  custom_roles: string[];
  permissions: Record<string, string[]>;
  is_superuser: boolean;
}

export const RbacService = {
  async listRoles(): Promise<CustomRole[]> {
    return apiFetch<CustomRole[]>('/api/v1/rbac/roles');
  },

  async createRole(data: CreateRoleRequest): Promise<CustomRole> {
    return apiFetch<CustomRole>('/api/v1/rbac/roles', { method: 'POST', json: data });
  },

  async updateRole(name: string, data: UpdateRoleRequest): Promise<CustomRole> {
    return apiFetch<CustomRole>(`/api/v1/rbac/roles/${name}`, { method: 'PUT', json: data });
  },

  async deleteRole(name: string): Promise<void> {
    await apiFetch<void>(`/api/v1/rbac/roles/${name}`, { method: 'DELETE' });
  },

  async getUserPermissions(userId: string): Promise<UserPermissionsResponse> {
    return apiFetch<UserPermissionsResponse>(`/api/v1/rbac/users/${userId}/permissions`);
  },
};

export default RbacService;
