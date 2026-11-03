/**
 * The current user's effective permissions, as the dashboard's permission
 * helpers read them (#891).
 *
 * In the full profile a custom role or a direct grant adds permissions to a
 * user's built-in role, and the API's checks admit them. The dashboard asks
 * `GET /api/v1/rbac/users/{id}/permissions` for the signed-in user's own map
 * (`EffectivePermissionsContext`) and every helper answers from the pair its
 * route checks:
 *
 *     allowed = is_superuser || ROLE_LIST.includes(role) || holds(effective, resource, action)
 *
 * `effective` is `undefined` when there is no map to read: the core profile
 * (no rbac module), a fetch that failed, or a page rendered outside the
 * provider. Then only the role lists apply, so the dashboard is never more
 * permissive than the API (fail closed).
 */

/** `EffectivePermissionsResponse.permissions`: a resource mapped to its actions. */
export type EffectivePermissions = Readonly<Record<string, ReadonlyArray<string>>>;

/** Whether the effective map holds `action` on `resource`. No map holds nothing. */
export function holds(
  effective: EffectivePermissions | undefined,
  resource: string,
  action: string,
): boolean {
  if (!effective || !Object.prototype.hasOwnProperty.call(effective, resource)) return false;
  const actions = effective[resource];
  return Array.isArray(actions) && actions.includes(action);
}

/**
 * The map out of an API answer, or `undefined` when the answer is not one:
 * `permissions` must be an object whose every value is a list of strings.
 * Anything else is treated as no map (role lists only), never as a grant.
 */
export function effectiveFromAnswer(body: unknown): EffectivePermissions | undefined {
  if (typeof body !== 'object' || body === null) return undefined;
  const permissions = (body as { permissions?: unknown }).permissions;
  if (typeof permissions !== 'object' || permissions === null || Array.isArray(permissions)) {
    return undefined;
  }
  const map: Record<string, string[]> = {};
  for (const [resource, actions] of Object.entries(permissions as Record<string, unknown>)) {
    if (!Array.isArray(actions) || !actions.every((a) => typeof a === 'string')) return undefined;
    map[resource] = [...(actions as string[])];
  }
  return map;
}
