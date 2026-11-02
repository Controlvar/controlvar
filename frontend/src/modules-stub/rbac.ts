/**
 * Core stub for `@modules/rbac`.
 *
 * Same exported shape as `modules/frontend/src/rbac.ts` so every caller
 * type-checks in both profiles, but every method rejects. It deliberately does
 * **not** return an empty list: "no custom roles" and "custom roles are not in
 * this build" are different facts, and a UI that shows the first when the
 * second is true is lying. Gate the call site with `useModule(MODULES.RBAC)`
 * instead — the throw is the backstop for a caller that forgot.
 */
import { CustomRole, PermissionGrant } from '@/types/admin';
import { MODULES } from '@/services/modules';

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

/** Thrown by every stubbed module call. */
export class ModuleNotInstalledError extends Error {
  readonly module: string;

  constructor(module: string) {
    super(`The "${module}" module is not installed in this build`);
    this.name = 'ModuleNotInstalledError';
    this.module = module;
    Object.setPrototypeOf(this, ModuleNotInstalledError.prototype);
  }
}

function notInstalled(): never {
  throw new ModuleNotInstalledError(MODULES.RBAC);
}

export const RbacService = {
  async listRoles(): Promise<CustomRole[]> {
    return notInstalled();
  },

  async createRole(data: CreateRoleRequest): Promise<CustomRole> {
    return notInstalled();
  },

  async updateRole(name: string, data: UpdateRoleRequest): Promise<CustomRole> {
    return notInstalled();
  },

  async deleteRole(name: string): Promise<void> {
    return notInstalled();
  },

  async getUserPermissions(userId: string): Promise<UserPermissionsResponse> {
    return notInstalled();
  },
};

export default RbacService;
