/**
 * Who may do what in warehouse analysis -- the API's role matrix, spelled for
 * the dashboard (founder decisions D11 and D25).
 *
 * The API is the enforcement; this only decides what the dashboard offers, so
 * nobody is walked into a 403. Each action carries the exact wording the API
 * uses in its refusal ("Creating a warehouse connection requires the ADMIN
 * role; you are DEVELOPER."), and
 * `tests/warehouse/warehouseRoles.test.ts` reads the API's own role test
 * (`modules/backend/tests/integration/warehouse/test_roles.py`) and fails if
 * the two tables differ in any action or role.
 *
 * Some response fields are given to fewer roles than the route that returns
 * them. `WAREHOUSE_FIELDS` lists those, each with the API field it gates, and
 * the same test reads the API's `FIELD_ROLES` table and fails if the roles
 * differ. The API sends such a field as null to the other roles; the dashboard
 * checks both, so it offers nothing the role cannot use.
 */
import type { Role, UserMe } from '@/services/api';

const ADMIN: readonly Role[] = ['ADMIN'];
const EDITORS: readonly Role[] = ['ADMIN', 'DEVELOPER'];
const READERS: readonly Role[] = ['ADMIN', 'DEVELOPER', 'ANALYST'];
const EVERYONE: readonly Role[] = ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'];

export interface WarehouseAction {
  /** The API's wording, used as the subject of the refusal sentence. */
  text: string;
  roles: readonly Role[];
}

export const WAREHOUSE_ACTIONS = {
  listConnectors: { text: 'Listing warehouse connectors', roles: EVERYONE },
  viewConnections: { text: 'Viewing warehouse connections', roles: READERS },
  createConnection: { text: 'Creating a warehouse connection', roles: ADMIN },
  changeConnection: { text: 'Changing a warehouse connection', roles: ADMIN },
  deleteConnection: { text: 'Deleting a warehouse connection', roles: ADMIN },
  testConnection: { text: 'Testing a warehouse connection', roles: ADMIN },
  regenerateKey: { text: "Regenerating a connection's key", roles: ADMIN },
  viewSources: { text: 'Viewing warehouse sources', roles: READERS },
  createAssignmentSource: { text: 'Creating an assignment source', roles: EDITORS },
  createMetricSource: { text: 'Creating a metric source', roles: READERS },
  editAssignmentSource: { text: 'Editing an assignment source', roles: EDITORS },
  editMetricSource: { text: 'Editing a metric source', roles: READERS },
  deleteAssignmentSource: { text: 'Deleting an assignment source', roles: EDITORS },
  deleteMetricSource: { text: 'Deleting a metric source', roles: EDITORS },
  validateAssignmentSource: { text: 'Validating an assignment source', roles: EDITORS },
  validateMetricSource: { text: 'Validating a metric source', roles: READERS },
  previewAssignmentSource: { text: 'Previewing an assignment source', roles: EDITORS },
  previewMetricSource: { text: 'Previewing a metric source', roles: READERS },
  startRun: { text: 'Starting a warehouse analysis', roles: EDITORS },
  viewRuns: { text: 'Viewing warehouse analyses', roles: READERS },
} as const satisfies Record<string, WarehouseAction>;

export type WarehouseActionName = keyof typeof WAREHOUSE_ACTIONS;

export interface WarehouseField extends WarehouseAction {
  /** The response field the API gives only to `roles` (null for the others). */
  field: string;
}

export const WAREHOUSE_FIELDS = {
  viewRunSql: { text: 'Viewing the SQL a run sent', roles: READERS, field: 'statements' },
} as const satisfies Record<string, WarehouseField>;

export type WarehouseFieldName = keyof typeof WAREHOUSE_FIELDS;

/** Anything `can` and `refusal` answer for: a route's action or a field. */
export type WarehouseCapability = WarehouseActionName | WarehouseFieldName;

const CAPABILITIES: Record<WarehouseCapability, WarehouseAction> = {
  ...WAREHOUSE_ACTIONS,
  ...WAREHOUSE_FIELDS,
};

type SessionUser = Pick<UserMe, 'role' | 'is_superuser'> | null | undefined;

/** The role the API sees: a superuser counts as ADMIN. */
export function warehouseRole(user: SessionUser): Role | null {
  if (!user) return null;
  if (user.is_superuser) return 'ADMIN';
  return user.role ?? null;
}

/**
 * Decided by the built-in role on purpose (#891, class B): the warehouse
 * API's role bands read the user's built-in role, and no custom role or
 * direct grant can hold a warehouse action (they hold only experiment and
 * feature_flag actions and audit_log read), so the effective map has nothing
 * to add here. `test_frontend_role_decisions.py` classifies this read.
 */
export function can(user: SessionUser, action: WarehouseCapability): boolean {
  const role = warehouseRole(user);
  return role !== null && CAPABILITIES[action].roles.includes(role);
}

function rolesText(roles: readonly Role[]): string {
  if (roles.length === 1) return `the ${roles[0]} role`;
  return `the ${roles.slice(0, -1).join(', ')} or ${roles[roles.length - 1]} role`;
}

/** The sentence the API answers with when `user` may not do `action`. */
export function refusal(user: SessionUser, action: WarehouseCapability): string {
  const { text, roles } = CAPABILITIES[action];
  const role = warehouseRole(user);
  const held = role ? `you are ${role}` : 'you have no role';
  return `${text} requires ${rolesText(roles)}; ${held}.`;
}

/** Per-kind action names for a source. */
export function sourceAction(
  verb: 'create' | 'edit' | 'delete' | 'validate' | 'preview',
  kind: 'assignment' | 'metric',
): WarehouseActionName {
  const noun = kind === 'assignment' ? 'AssignmentSource' : 'MetricSource';
  return `${verb}${noun}` as WarehouseActionName;
}
