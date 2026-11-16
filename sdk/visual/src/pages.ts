/**
 * Page rules: which pages of a site run a visual experiment. The answer to
 * each call carries every running experiment's rules on this host, and the
 * script keeps them (localStorage controlvar_pages, at most 60 seconds) so
 * that a page none of them covers makes no call and is never hidden.
 */
import { KEYS } from "./keys";
import { normalizeHost, normalizePath } from "./url";

export interface PageRule {
  match: "exact" | "prefix";
  path: string;
}

export interface KeptRules {
  host: string;
  rules: PageRule[];
  /** Until when the rules may be used, in ms since the epoch. */
  until: number;
}

/** The longest the script keeps page rules, whatever the answer says. */
export const MAX_RULES_AGE_SECONDS = 60;

/**
 * Whether a page at *path* is one *rule* covers (the server's page_matches,
 * on a host already compared). The query and the fragment are ignored;
 * `exact` means the same normalised path, `prefix` the same path or one that
 * continues after a `/`.
 */
export function ruleMatches(path: string, rule: PageRule): boolean {
  const wanted = normalizePath(rule.path);
  const actual = normalizePath(path.split("#")[0].split("?")[0]);
  if (rule.match === "exact") return actual === wanted;
  return actual === wanted || wanted === "/" || actual.indexOf(`${wanted}/`) === 0;
}

function isRule(value: unknown): value is PageRule {
  if (typeof value !== "object" || value === null) return false;
  const rule = value as Record<string, unknown>;
  return (rule.match === "exact" || rule.match === "prefix") && typeof rule.path === "string";
}

/** The answer's `pages`, checked, or null. */
export function readAnswerRules(pages: unknown, now: number): KeptRules | null {
  if (typeof pages !== "object" || pages === null) return null;
  const { host, rules, max_age: maxAge } = pages as Record<string, unknown>;
  if (typeof host !== "string" || !Array.isArray(rules) || !rules.every(isRule)) return null;
  const age = typeof maxAge === "number" && maxAge >= 0 ? Math.min(maxAge, MAX_RULES_AGE_SECONDS) : 0;
  return {
    host: normalizeHost(host),
    rules: rules.map((rule) => ({ match: rule.match, path: rule.path })),
    until: now + age * 1000,
  };
}

export function keepRules(storage: Storage | null, kept: KeptRules): void {
  try {
    if (storage) storage.setItem(KEYS.pages, JSON.stringify(kept));
  } catch {
    // Storage full or refused: the next page makes its call.
  }
}

/**
 * Whether kept rules let this page skip its call: they are fresh, for this
 * host, and none of them covers the path.
 */
export function rulesSkip(storage: Storage | null, host: string, path: string, now: number): boolean {
  let kept: unknown;
  try {
    const text = storage ? storage.getItem(KEYS.pages) : null;
    if (text === null) return false;
    kept = JSON.parse(text);
  } catch {
    return false;
  }
  if (typeof kept !== "object" || kept === null) return false;
  const { host: keptHost, rules, until } = kept as Record<string, unknown>;
  if (typeof until !== "number" || until <= now || until > now + MAX_RULES_AGE_SECONDS * 1000) return false;
  if (keptHost !== normalizeHost(host) || !Array.isArray(rules) || !rules.every(isRule)) return false;
  return !rules.some((rule) => ruleMatches(path, rule));
}
