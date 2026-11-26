/**
 * Goals: the clicks and page views that count as an experiment's conversions.
 *
 * The answer for a page names each experiment the visitor is assigned to
 * (the control included) with its goals, at most 10: `{event_name, kind:
 * "click", selector, page}` or `{event_name, kind: "page_view", page}`, where
 * `page` is a page rule (`exact` or `prefix`) on one of the experiment's
 * hosts. The script keeps them in localStorage `controlvar_goals`, per
 * experiment, for 30 days after the last answer that named it, so a goal on
 * a page no experiment covers (a thank-you page) still counts there:
 *
 *     {"<experiment key>": {"at": <ms>, "pages": [<rule>…], "goals": [<goal>…]}}
 *
 * `pages` are the answer's page rules that covered the page the experiment
 * was named on. An answer for a page every one of those rules covers that no
 * longer names the experiment removes its goals (it has stopped, or the
 * visitor is no longer in it). At most 10 experiments are kept, the ones
 * named most recently.
 *
 * Every goal is checked against its form when it arrives and again when it
 * is read back (a page can write the key); an experiment with one goal that
 * fails keeps no goals at all, so a goal's position stays the one the
 * experiment gives it.
 */
import { KEYS } from "./keys";
import { ruleMatches, type PageRule } from "./pages";
import { normalizeHost, normalizePath, splitUrl, urlProblem } from "./url";
import { selectorProblem } from "./validate";

export const GOAL_DAYS = 30;
const DAY_MS = 24 * 60 * 60 * 1000;
/** The most experiments whose goals are kept (an answer names at most 10). */
export const MAX_GOAL_EXPERIMENTS = 10;
/** The most goals an experiment has. */
export const MAX_GOALS = 10;
/** The most page rules kept with an experiment's goals. */
export const MAX_GOAL_PAGES = 10;
/** A goal's event name, as the server checks it. */
export const EVENT_NAME = /^[a-z0-9_]{1,100}$/;
const MAX_KEY_LENGTH = 100;

/** A goal's page, as page rules compare it: the host in lower case without a port, and the path. */
export interface GoalPage {
  match: "exact" | "prefix";
  host: string;
  path: string;
}

export interface ClickGoal {
  kind: "click";
  event_name: string;
  selector: string;
  page: GoalPage;
}

export interface PageViewGoal {
  kind: "page_view";
  event_name: string;
  page: GoalPage;
}

export type Goal = ClickGoal | PageViewGoal;

export interface GoalEntry {
  /** When an answer last named the experiment, in ms since the epoch. */
  at: number;
  pages: PageRule[];
  goals: Goal[];
}

/** What one answer says about one experiment: its checked goals, or null to keep none (not assigned, no goals, or goals that fail). */
export interface Named {
  key: string;
  goals: Goal[] | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isMatch(value: unknown): value is GoalPage["match"] {
  return value === "exact" || value === "prefix";
}

/** A goal page from a page rule's URL (one the server accepts as a page, with no query), or null. */
function pageFromUrl(match: unknown, url: unknown): GoalPage | null {
  if (!isMatch(match) || typeof url !== "string" || url.indexOf("?") >= 0 || urlProblem(url)) return null;
  const parts = splitUrl(url);
  return { match, host: normalizeHost(parts.netloc), path: normalizePath(parts.path) };
}

/** A kept goal page, checked again: it must be what pageFromUrl makes of its own host and path. */
function keptPage(value: unknown): GoalPage | null {
  if (!isRecord(value)) return null;
  const { match, host, path } = value;
  if (typeof host !== "string" || typeof path !== "string" || path.charAt(0) !== "/") return null;
  const again = pageFromUrl(match, `https://${host}${path}`);
  return again !== null && again.host === host && again.path === path ? again : null;
}

function goalFrom(value: unknown, page: (value: unknown) => GoalPage | null): Goal | null {
  if (!isRecord(value)) return null;
  const { event_name: name, kind } = value;
  if (typeof name !== "string" || !EVENT_NAME.test(name)) return null;
  const where = page(value.page);
  if (where === null) return null;
  if (kind === "page_view") return { kind, event_name: name, page: where };
  if (kind === "click" && !selectorProblem(value.selector)) {
    return { kind, event_name: name, selector: value.selector as string, page: where };
  }
  return null;
}

function goalsFrom(value: unknown, page: (value: unknown) => GoalPage | null): Goal[] | null {
  if (!Array.isArray(value) || value.length > MAX_GOALS) return null;
  const goals: Goal[] = [];
  const names = new Set<string>();
  for (const item of value) {
    const goal = goalFrom(item, page);
    if (goal === null || names.has(goal.event_name)) return null;
    names.add(goal.event_name);
    goals.push(goal);
  }
  return goals;
}

/** An answer's goals for one experiment, checked: null when any fails, so the experiment keeps none. */
export function answerGoals(value: unknown): Goal[] | null {
  if (value === null || value === undefined) return [];
  return goalsFrom(value, (page) => (isRecord(page) ? pageFromUrl(page.match, page.url) : null));
}

function isRule(value: unknown): value is PageRule {
  return isRecord(value) && isMatch(value.match) && typeof value.path === "string" && value.path.charAt(0) === "/";
}

function isTime(value: unknown, now: number): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 && value <= now && now - value < GOAL_DAYS * DAY_MS;
}

function newestFirst(kept: Map<string, GoalEntry>): Map<string, GoalEntry> {
  const entries = Array.from(kept.entries()).sort((a, b) => b[1].at - a[1].at);
  return new Map(entries.slice(0, MAX_GOAL_EXPERIMENTS));
}

/** The kept goals, each experiment checked again; an experiment that fails, or is older than 30 days, is left out. */
export function readGoals(storage: Storage | null, now: number): Map<string, GoalEntry> {
  const kept = new Map<string, GoalEntry>();
  let parsed: unknown;
  try {
    const text = storage ? storage.getItem(KEYS.goals) : null;
    if (text === null) return kept;
    parsed = JSON.parse(text);
  } catch {
    return kept;
  }
  if (!isRecord(parsed)) return kept;
  for (const key of Object.keys(parsed)) {
    const entry = parsed[key];
    if (key.length < 1 || key.length > MAX_KEY_LENGTH || !isRecord(entry) || !isTime(entry.at, now)) continue;
    if (!Array.isArray(entry.pages) || entry.pages.length > MAX_GOAL_PAGES || !entry.pages.every(isRule)) continue;
    const goals = goalsFrom(entry.goals, keptPage);
    if (goals === null || goals.length === 0) continue;
    const pages = entry.pages.map((rule) => ({ match: rule.match, path: rule.path }));
    kept.set(key, { at: entry.at, pages, goals });
  }
  return newestFirst(kept);
}

/**
 * Keeps what one answer says. *covering* are the answer's page rules that
 * cover *path*, the page it answered. A named experiment's goals replace its
 * entry (none: the entry goes); an experiment kept but not named goes when
 * every rule kept with it covers *path*. `allowed` is asked before the write.
 */
export function keepGoals(
  storage: Storage | null,
  now: number,
  named: Named[],
  covering: PageRule[],
  path: string,
  allowed: () => boolean,
): void {
  if (!storage) return;
  const kept = readGoals(storage, now);
  const names = new Set(named.map((item) => item.key));
  for (const [key, entry] of Array.from(kept.entries())) {
    if (!names.has(key) && entry.pages.length > 0 && entry.pages.every((rule) => ruleMatches(path, rule))) kept.delete(key);
  }
  for (const item of named) {
    if (item.goals === null || item.goals.length === 0) kept.delete(item.key);
    else kept.set(item.key, { at: now, pages: covering.filter(isRule).slice(0, MAX_GOAL_PAGES), goals: item.goals });
  }
  const fresh = newestFirst(kept);
  if (!allowed()) return;
  try {
    if (fresh.size === 0) {
      if (storage.getItem(KEYS.goals) !== null) storage.removeItem(KEYS.goals);
      return;
    }
    const value: Record<string, GoalEntry> = {};
    for (const [key, entry] of fresh) Object.defineProperty(value, key, { value: entry, enumerable: true });
    storage.setItem(KEYS.goals, JSON.stringify(value));
  } catch {
    // Storage full or refused: the goals are not kept, and none counts.
  }
}

/** Whether a goal's page is the page at *host* and *path*, as the server's page_matches says: the host compared without a port, the query ignored. */
export function goalPageMatches(page: GoalPage, host: string, path: string): boolean {
  return normalizeHost(host) === page.host && ruleMatches(path, { match: page.match, path: page.path });
}

/** What a click means for one click goal: counted, elsewhere on the page, or a selector that matches no element or several. */
export type ClickVerdict = "counted" | "elsewhere" | "not_found" | "ambiguous";

/**
 * A click counts for a goal only when the selector matches exactly one
 * element and the click's target is that element or inside it.
 */
export function clickVerdict(doc: Document, selector: string, target: EventTarget | null): ClickVerdict {
  let matches: NodeListOf<Element>;
  try {
    matches = doc.querySelectorAll(selector);
  } catch {
    // The browser cannot read the selector: it matches nothing.
    return "not_found";
  }
  if (matches.length === 0) return "not_found";
  if (matches.length > 1) return "ambiguous";
  const element = matches[0];
  if (target === element) return "counted";
  try {
    return target !== null && element.contains(target as Node) ? "counted" : "elsewhere";
  } catch {
    return "elsewhere";
  }
}
