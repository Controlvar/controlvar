/**
 * How long the script keeps a page's changes. Until the bound it re-applies
 * a change whose element the page replaces or rewrites; after it, nothing
 * watches the page. The page's bound is min(load + 5 s, navigation start +
 * 10 s). The answer that follows a single-page app's path change gets 5 s
 * from its arrival.
 */
import type { Captures } from "./captures";

export const AFTER_LOAD_MS = 5000;
export const LATEST_MS = 10000;

export interface Bound {
  readonly passed: boolean;
  /** Runs *callback* once, at the bound, or now if it has passed. */
  whenPassed(callback: () => void): void;
}

interface Armed extends Bound {
  arm(): void;
}

function bound(captures: Captures, at: () => number): Armed {
  let passed = false;
  let armed = 0;
  const waiting: Array<() => void> = [];
  const check = (generation: number): void => {
    if (passed || generation !== armed) return;
    if (at() - captures.now() > 0) return arm();
    passed = true;
    for (const callback of waiting.splice(0, waiting.length)) {
      try {
        callback();
      } catch {
        // One callback's failure stops no other.
      }
    }
  };
  function arm(): void {
    armed += 1;
    const generation = armed;
    captures.setTimeout(() => check(generation), Math.max(0, at() - captures.now()));
  }
  return {
    get passed() {
      return passed;
    },
    whenPassed(callback) {
      if (passed) callback();
      else waiting.push(callback);
    },
    arm,
  };
}

/** The page's bound. Made in the script's first run, so that the load event is never missed. */
export function pageBound(win: Window & typeof globalThis, captures: Captures): Bound {
  let loadAt: number | null = win.document.readyState === "complete" ? captures.now() : null;
  const page = bound(captures, () => (loadAt === null ? LATEST_MS : Math.min(loadAt + AFTER_LOAD_MS, LATEST_MS)));
  if (loadAt === null) {
    captures.addEventListener("load", () => {
      loadAt = captures.now();
      page.arm();
    });
  }
  page.arm();
  return page;
}

/** The bound of an answer that follows a path change: 5 s from now. */
export function answerBound(captures: Captures): Bound {
  const until = captures.now() + AFTER_LOAD_MS;
  const later = bound(captures, () => until);
  later.arm();
  return later;
}
