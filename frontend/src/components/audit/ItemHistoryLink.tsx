import React from 'react';
import Link from 'next/link';

const LINK_CLASS =
  'text-sm font-medium text-blue-700 underline-offset-2 hover:underline focus:outline-none focus:ring-2 focus:ring-blue-500 rounded';

/** The Audit Log page, filtered to one item's entries (`entity_type` and `entity_id`). */
export function itemHistoryHref(entityType: string, entityId: string): string {
  const query = new URLSearchParams({ entity_type: entityType, entity_id: entityId });
  return `/admin/audit?${query.toString()}`;
}

/** The Audit Log page, filtered to the entries one person made (`user_id`). */
export function personHistoryHref(userId: string): string {
  return `/admin/audit?${new URLSearchParams({ user_id: userId }).toString()}`;
}

interface ItemHistoryLinkProps {
  /** `feature_flag` or `experiment`: the audit log's own entity types. */
  entityType: string;
  entityId: string;
  /** The item's name, for the link's accessible name. */
  name: string;
}

/**
 * "History": a link to the Audit Log page filtered to one flag or experiment.
 * Every role may follow it; the audit API narrows what the page then lists to
 * the caller's own entries where it narrows at all.
 */
export function ItemHistoryLink({ entityType, entityId, name }: ItemHistoryLinkProps) {
  return (
    <Link
      href={itemHistoryHref(entityType, entityId)}
      data-testid="item-history-link"
      aria-label={`History of ${name}`}
      className={LINK_CLASS}
    >
      History
    </Link>
  );
}

interface PersonHistoryLinkProps {
  userId: string;
  username: string;
}

/** "History" on a Users row: the Audit Log page filtered to what that person changed. */
export function PersonHistoryLink({ userId, username }: PersonHistoryLinkProps) {
  return (
    <Link
      href={personHistoryHref(userId)}
      data-testid="item-history-link"
      aria-label={`History of changes made by ${username}`}
      className={LINK_CLASS}
    >
      History
    </Link>
  );
}
