import { ENTITY_LABELS } from './actionLabels';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Whether `value` is an id the audit API accepts (it answers 422 to anything else). */
export function isAuditId(value: string): boolean {
  return UUID.test(value);
}

/** What the Audit Log page's URL can narrow the list to. */
export interface AuditScope {
  entity_type?: string;
  entity_id?: string;
  user_id?: string;
}

export interface ParsedAuditScope {
  scope: AuditScope;
  /** A value the URL carried was not usable and was left out. */
  ignored: boolean;
}

function single(value: string | string[] | undefined): string | undefined {
  return typeof value === 'string' && value !== '' ? value : undefined;
}

/**
 * Reads `entity_type`, `entity_id` and `user_id` from the page's query. A
 * value the API would refuse (an id that is not a UUID, an unknown entity
 * type) is dropped, so a mistyped link shows the unfiltered list and says so
 * instead of sending a request that answers 422.
 */
export function parseAuditScope(query: Record<string, string | string[] | undefined>): ParsedAuditScope {
  const scope: AuditScope = {};
  let ignored = false;
  const type = single(query.entity_type);
  const entityId = single(query.entity_id);
  const userId = single(query.user_id);
  if (type !== undefined) {
    if (Object.prototype.hasOwnProperty.call(ENTITY_LABELS, type)) scope.entity_type = type;
    else ignored = true;
  }
  if (entityId !== undefined) {
    if (isAuditId(entityId)) scope.entity_id = entityId;
    else ignored = true;
  }
  if (userId !== undefined) {
    if (isAuditId(userId)) scope.user_id = userId;
    else ignored = true;
  }
  return { scope, ignored };
}

/** The scope as a query object for `router.replace`; empty keys are left out. */
export function scopeQuery(scope: AuditScope): Record<string, string> {
  const out: Record<string, string> = {};
  if (scope.entity_type) out.entity_type = scope.entity_type;
  if (scope.entity_id) out.entity_id = scope.entity_id;
  if (scope.user_id) out.user_id = scope.user_id;
  return out;
}
