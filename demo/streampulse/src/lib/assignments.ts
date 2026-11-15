import { useEffect, useState } from 'react';
import { useExperimentation, type ExperimentAssignment } from '@controlvar/react-sdk';

/** How often the panel re-reads the SDK's assignments. A read is in memory: it sends nothing. */
export const ASSIGNMENT_READ_MS = 1000;

type Seen = { userId: string; byKey: Record<string, ExperimentAssignment> };

function same(a: ExperimentAssignment | undefined, b: ExperimentAssignment): boolean {
  return (
    a !== undefined &&
    a.variantKey === b.variantKey &&
    a.variantName === b.variantName &&
    a.assigned === b.assigned &&
    a.reason === b.reason
  );
}

/**
 * The experiments the screens have assigned the device to, by key, as the SDK's
 * `client.getAssignments(userId)` reports them. The panel shows these and assigns
 * nothing itself, so a device joins an experiment only on the screen that uses it.
 *
 * The SDK keeps an answer for its cache TTL (5 s here); this keeps every answer it
 * has read for the device, so a row does not empty when the cache entry expires.
 * Picking a device or pressing Refresh now re-mounts the provider and the panel,
 * which starts the record again.
 */
export function useAssignedExperiments(): Record<string, ExperimentAssignment> {
  const { client, user } = useExperimentation();
  const [seen, setSeen] = useState<Seen>(() => ({ userId: user.userId, byKey: {} }));

  useEffect(() => {
    const read = () =>
      setSeen((prev) => {
        const kept = prev.userId === user.userId ? prev.byKey : {};
        let changed = kept !== prev.byKey;
        const byKey = { ...kept };
        for (const assignment of client.getAssignments(user.userId)) {
          if (!same(byKey[assignment.experimentKey], assignment)) {
            byKey[assignment.experimentKey] = assignment;
            changed = true;
          }
        }
        return changed ? { userId: user.userId, byKey } : prev;
      });
    read();
    const timer = setInterval(read, ASSIGNMENT_READ_MS);
    return () => clearInterval(timer);
  }, [client, user.userId]);

  return seen.userId === user.userId ? seen.byKey : {};
}
