/**
 * Every change to an `[aria-live]` region's text, and whether an ancestor was
 * `inert` when it happened (#369).
 *
 * A screen reader may say nothing for a live region whose text changes under
 * an inert ancestor, so a page that closes a Dialog and announces the result
 * must write the text after the Dialog has taken `inert` off `#__next`. The
 * observer sees the `inert` attribute changes and the text changes as one
 * ordered list and replays them, so `underInert` is the state at the moment of
 * the change, not when the records are read. A Dialog sets `inert` only where
 * it is absent, so a record with no old value is an add and one with an old
 * value is a remove.
 *
 * The browser journey (tests/e2e/dialog-focus.journey.spec.ts) runs the same
 * replay in the page.
 */
/** An explicit live region, or a role that is one. */
export const LIVE_REGION = '[aria-live], [role="status"], [role="alert"], [role="log"]';

export interface LiveRegionChange {
  text: string;
  underInert: boolean;
}

export function watchLiveRegions(root: Node = document.body) {
  const inert = new Set<Element>(Array.from(document.querySelectorAll('[inert]')));
  const changes: LiveRegionChange[] = [];
  const handle = (records: MutationRecord[]) => {
    for (const record of records) {
      if (record.type === 'attributes') {
        if (record.oldValue === null) inert.add(record.target as Element);
        else inert.delete(record.target as Element);
        continue;
      }
      const node =
        record.target.nodeType === Node.ELEMENT_NODE ? (record.target as Element) : record.target.parentElement;
      const region = node?.closest(LIVE_REGION);
      if (!region) continue;
      changes.push({
        text: region.textContent ?? '',
        underInert: Array.from(inert).some((el) => el.contains(region)),
      });
    }
  };
  const observer = new MutationObserver(handle);
  observer.observe(root, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['inert'],
    attributeOldValue: true,
  });
  return {
    changes,
    /** Read what is still queued, and stop. */
    stop() {
      handle(observer.takeRecords());
      observer.disconnect();
    },
  };
}
