/**
 * What the script does on an ordinary page view: ask which visual changes
 * this visitor gets on this page (POST /api/v1/tracking/visual), hide the
 * page meanwhile if it starts before the page's <body> exists, check and
 * apply the answer, and report what could not be applied. Also `track()`, which records an event for the experiments this
 * visitor was assigned to in the last 30 days.
 *
 * Whatever goes wrong, the page is shown: every path that hid it ends by
 * showing it, and the hide has its own timer.
 *
 * On a single-page app the script follows the path: after each
 * history.pushState or replaceState (it wraps both, calling the page's own
 * first) and each popstate, it reads the kept page rules once against the
 * current path. When the path has changed it undoes the changes it made and
 * asks again for the new path, unless fresh rules cover nothing there. A
 * change of the part after `#` alone asks nothing.
 *
 * In a tab showing a preview link (preview.ts) it asks for that link's
 * variant instead (POST /api/v1/tracking/visual/preview, which records
 * nothing), applies it only on the experiment's pages, and shows the preview
 * banner (own-ui.ts) with [Exit preview]. Every other experiment is off, it
 * keeps no page rules or assignments, sets no cookie and track() sends nothing.
 *
 * The call for the page also carries the website details data-collect turns
 * on (details.ts, visit.ts), merged into its `context` after data-attributes.
 * They are read on each page view the script follows, and never in a
 * preview tab; besides that call, only goal events carry them.
 *
 * Goals (goals.ts): the answer names each assigned experiment's goals, which
 * the script keeps for 30 days. Each page load, and each path change it
 * follows, is one page view with its own random id. A page_view goal whose
 * page this is records one event per page view; a click goal records one
 * when a click a person makes (never one a script makes) lands on the one
 * element its selector matches, or inside it, at most once per page view.
 * One click listener on the document, in the capture phase, added once,
 * sees every click. A goal whose selector matches no element or several on
 * a click is reported once per page view. Events go to the outbox
 * (outbox.ts), which sends them; nothing of this runs in a preview tab.
 *
 * Nothing is read from the visitor's cookie or the browser, stored or sent
 * unless `allowed()`: consent given (or not needed) and not revoked. An answer
 * that arrives after revokeConsent() is dropped.
 */
import { createApplier, type Applier } from "./apply";
import { answerBound, type Bound } from "./bound";
import type { Captures } from "./captures";
import type { Config } from "./config";
import { detailsFor, pageDetails, readPageSignals } from "./details";
import { answerGoals, clickVerdict, goalPageMatches, keepGoals, readGoals, type Goal, type Named } from "./goals";
import { hidePage, type Hider } from "./hide";
import { KEYS } from "./keys";
import { createOutbox, type GoalEvent } from "./outbox";
import { createOwnUi, type OwnUi } from "./own-ui";
import { keepRules, readAnswerRules, ruleMatches, rulesSkip, type KeptRules } from "./pages";
import {
  endPreview,
  EXIT_PREVIEW,
  PREVIEW_FAILED,
  PREVIEW_GONE,
  PREVIEW_LABEL,
  PREVIEW_LOADING,
  PREVIEW_PATH,
  PREVIEW_SCOPED_KEY,
  previewElsewhere,
  previewShowing,
} from "./preview";
import { createReporter } from "./reports";
import { FORMAT_VERSION } from "./rules";
import { backoffActive, startBackoff, type Send } from "./send";
import { normalizeHost } from "./url";
import { validateChanges } from "./validate";
import { newId, visitorId } from "./visitor";
import { touchVisit } from "./visit";

export const VISUAL_PATH = "/api/v1/tracking/visual";
export const BATCH_PATH = "/api/v1/tracking/batch";
export const MAX_EXPERIMENTS = 10;
export const ASSIGNED_DAYS = 30;
const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_HOST_LENGTH = 261;
const MAX_PATH_LENGTH = 2048;
const MAX_EVENT_LENGTH = 100;
const MAX_KEY_LENGTH = 100;

export const KEY_REFUSED =
  "Controlvar: the API refused this page's key (401 or 403), so the script sends nothing in this tab for 5 minutes. Use a key with no scopes, made for your website.";
export const BAD_EVENT = "Controlvar: track() needs an event name of 1 to 100 characters.";

export interface RuntimeEnv {
  window: Window & typeof globalThis;
  captures: Captures;
  config: Config;
  send: Send;
  local(): Storage | null;
  session(): Storage | null;
  now(): number;
  /** Set when this page must send nothing more (a refused or failed call). */
  silence(): void;
  /** The page's bound (bound.ts), made in the first run. */
  bound: Bound;
  /** Whether the script may read the visitor's id, store or send now: consent given (or not needed), not revoked. */
  allowed(): boolean;
  /** The preview link's token this tab shows, or null for an ordinary page view; read once, by start(). */
  previewToken(): string | null;
}

const NO_HIDE: Hider = { show: () => undefined, hidden: false };

export interface Runtime {
  /** Asks for this page's changes and applies them. Runs once. */
  start(): void;
  track(event: unknown, options?: unknown): void;
}

type Assigned = Record<string, number>;

function readAssigned(storage: Storage | null, now: number): Assigned {
  try {
    const parsed: unknown = JSON.parse((storage && storage.getItem(KEYS.assigned)) || "{}");
    const fresh: Assigned = {};
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return fresh;
    for (const key of Object.keys(parsed)) {
      const at = (parsed as Record<string, unknown>)[key];
      if (typeof at === "number" && at <= now && now - at < ASSIGNED_DAYS * DAY_MS && key.length <= MAX_KEY_LENGTH) {
        fresh[key] = at;
      }
    }
    return fresh;
  } catch {
    return {};
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** One page view of an ordinary page: its id, its page, and the goals it counted and reported. */
interface PageView {
  id: string;
  host: string;
  path: string;
  /** The website details of the page view, which its goal events carry. */
  details: Record<string, string>;
  counted: Set<string>;
  reported: Set<string>;
}

export function createRuntime(env: RuntimeEnv): Runtime {
  const { window: win, captures, config, send } = env;
  const reporter = createReporter(send);
  const outbox = createOutbox({ local: env.local, now: env.now, send, path: BATCH_PATH, allowed: env.allowed });
  const written = new WeakSet<Element>();
  /** The current page view; null before the first, and in a preview tab. */
  let view: PageView | null = null;
  let listening = false;
  let started = false;
  /** The changes applied for the current path. */
  let current: Applier | null = null;
  /** The path last asked about, and how many times the script has asked. */
  let askedPath: string | null = null;
  let asked = 0;
  /** The preview link's token while this tab shows one; the banner while it is on the page. */
  let preview: string | null = null;
  let banner: OwnUi | null = null;
  /** The page load's own details (device, browser, language, referrer), read on the first page view. */
  let loaded: Record<string, string> | null = null;
  const owns = (element: Element): boolean =>
    element === win.document.documentElement || (banner !== null && banner.owns(element));

  function remember(keys: string[]): void {
    if (keys.length === 0 || !env.allowed()) return;
    const storage = env.local();
    const assigned = readAssigned(storage, env.now());
    for (const key of keys) assigned[key] = env.now();
    try {
      if (storage) storage.setItem(KEYS.assigned, JSON.stringify(assigned));
    } catch {
      // Storage refused: track() names only what it can read.
    }
  }

  function refused(status: number): void {
    if (env.allowed()) startBackoff(env.session(), env.now());
    env.silence();
    if (status === 401 || status === 403) win.console.warn(KEY_REFUSED);
  }

  /** The preview call failed: the banner says why, and a link that no longer works is forgotten. */
  function previewRefused(status: number): void {
    if (status === 404) endPreview(env.session());
    if (banner) banner.say(status === 404 ? PREVIEW_GONE : status === 403 ? PREVIEW_SCOPED_KEY : PREVIEW_FAILED);
  }

  function apply(applier: Applier, hider: Hider, which: number): void {
    if (applier.empty) return hider.show();
    current = applier;
    applier.start(() => hider.show(), which === 1 ? env.bound : answerBound(captures));
  }

  function answerPreview(body: unknown, hider: Hider, which: number): void {
    if (!env.allowed() || !isRecord(body) || which !== asked || preview === null) return hider.show();
    const variant = typeof body.variant_name === "string" ? body.variant_name : "";
    const key = typeof body.experiment_key === "string" ? body.experiment_key : "";
    const rules = readAnswerRules(body.pages, env.now());
    const location = win.location;
    const covered =
      rules !== null &&
      rules.host === normalizeHost(location.host) &&
      rules.rules.some((rule) => ruleMatches(location.pathname, rule));
    if (banner) banner.say(covered ? previewShowing(variant) : previewElsewhere(variant));
    if (!covered || body.changes === null || body.changes === undefined) return hider.show();
    const verdict = validateChanges(body.changes);
    if (!verdict.ok) {
      reporter.notApplied(key, null, verdict.reason);
      return hider.show();
    }
    const applier = createApplier({ window: win, captures, reporter, written, owns });
    applier.add(key, verdict.changes);
    apply(applier, hider, which);
  }

  function answer(body: unknown, hider: Hider, which: number): void {
    if (!env.allowed() || !isRecord(body) || which !== asked) return hider.show();
    const rules = readAnswerRules(body.pages, env.now());
    if (rules) keepRules(env.local(), rules);
    const experiments = Array.isArray(body.experiments) ? body.experiments.slice(0, MAX_EXPERIMENTS) : [];
    const applier = createApplier({ window: win, captures, reporter, written, owns });
    const assigned: string[] = [];
    const named: Named[] = [];
    for (const experiment of experiments) {
      if (!isRecord(experiment)) continue;
      const key = experiment.experiment_key;
      if (typeof key !== "string" || key.length === 0) continue;
      if (experiment.assigned !== true) {
        named.push({ key, goals: null });
        continue;
      }
      assigned.push(key);
      named.push({ key, goals: answerGoals(experiment.goals) });
      if (experiment.changes === null || experiment.changes === undefined) continue;
      const verdict = validateChanges(experiment.changes);
      if (!verdict.ok) reporter.notApplied(key, null, verdict.reason);
      else applier.add(key, verdict.changes);
    }
    remember(assigned);
    try {
      keepAnswerGoals(named, rules);
    } catch {
      // The goals stay as they were.
    }
    apply(applier, hider, which);
  }

  /** Keeps the answer's goals for the page it answered, then counts this page view's page_view goals. */
  function keepAnswerGoals(named: Named[], rules: KeptRules | null): void {
    if (view === null) return;
    const page = view;
    const covering =
      rules !== null && rules.host === normalizeHost(page.host) ? rules.rules.filter((rule) => ruleMatches(page.path, rule)) : [];
    keepGoals(env.local(), env.now(), named, covering, page.path, env.allowed);
    countPageViews();
  }

  /** The kept goals, each with its experiment's key and its position there, that apply to the current page view. */
  function goalsHere(kind: Goal["kind"]): { key: string; index: number; goal: Goal }[] {
    const page = view;
    if (page === null || preview !== null || !env.allowed()) return [];
    const here: { key: string; index: number; goal: Goal }[] = [];
    readGoals(env.local(), env.now()).forEach((entry, key) => {
      entry.goals.forEach((goal, index) => {
        if (goal.kind === kind && goalPageMatches(goal.page, page.host, page.path)) here.push({ key, index, goal });
      });
    });
    return here;
  }

  /** A goal's event for this page view; null when the page view has counted it already. */
  function goalEvent(page: PageView, key: string, goal: Goal): GoalEvent | null {
    const name = `${key}\n${goal.event_name}`;
    if (page.counted.has(name)) return null;
    page.counted.add(name);
    const event: GoalEvent = {
      event_type: goal.event_name,
      user_id: visitorId(win, config, captures),
      experiment_key: key,
      event_id: `${page.id}:${goal.event_name}`,
    };
    if (Object.keys(page.details).length > 0) event.metadata = { ...page.details };
    return event;
  }

  /** Records this page view's page_view goals, each once. */
  function countPageViews(): void {
    const page = view;
    if (page === null) return;
    const events: GoalEvent[] = [];
    for (const { key, goal } of goalsHere("page_view")) {
      const event = goalEvent(page, key, goal);
      if (event !== null) events.push(event);
    }
    outbox.add(events);
  }

  /** The one click listener: a click a person makes counts for each click goal of this page view whose element it lands on. */
  function onClick(event: Event): void {
    try {
      if (event.isTrusted !== true) return;
      const page = view;
      if (page === null) return;
      const events: GoalEvent[] = [];
      for (const { key, index, goal } of goalsHere("click")) {
        if (goal.kind !== "click" || page.counted.has(`${key}\n${goal.event_name}`)) continue;
        const verdict = clickVerdict(win.document, goal.selector, event.target);
        if (verdict === "counted") {
          const made = goalEvent(page, key, goal);
          if (made !== null) events.push(made);
        } else if (verdict !== "elsewhere" && !page.reported.has(`${key}\n${goal.event_name}`)) {
          page.reported.add(`${key}\n${goal.event_name}`);
          reporter.goalMissed(key, index, verdict === "not_found" ? "goal_not_found" : "goal_ambiguous");
        }
      }
      outbox.add(events);
    } catch {
      // The page's click goes on as it would without the script.
    }
  }

  function listen(): void {
    if (listening) return;
    listening = true;
    win.document.addEventListener("click", onClick, true);
  }

  /**
   * One page view of an ordinary page (never a preview): the website details
   * for its call. The page load's own are read once; the visit is touched on
   * every page view.
   */
  function viewDetails(): Record<string, string> {
    const location = win.location;
    if (loaded === null) loaded = pageDetails(readPageSignals(win, config.collect), config.collect, location.hostname);
    const visit = touchVisit(
      env.local(),
      env.now(),
      { search: location.search, pathname: location.pathname },
      config.collect,
      env.allowed,
    );
    return detailsFor(loaded, visit, config.collect);
  }

  function ask(hider: Hider, path: string, which: number, details: Record<string, string>): void {
    const location = win.location;
    const previewing = preview;
    let request: Promise<Response> | null;
    if (previewing !== null) {
      request = send(PREVIEW_PATH, { token: previewing, host: location.host, path }, { apiKey: true });
    } else {
      const body: Record<string, unknown> = {
        user_id: visitorId(win, config, captures),
        host: location.host,
        path,
        format_version: FORMAT_VERSION,
      };
      if (config.attributes !== null || Object.keys(details).length > 0) {
        body.context = { ...(config.attributes || {}), ...details };
      }
      request = send(VISUAL_PATH, body, { apiKey: true });
    }
    if (request === null) return hider.show();
    request
      .then(
        (response) => {
          if (!response.ok) {
            if (previewing !== null) previewRefused(response.status);
            if (previewing === null || response.status !== 404) refused(response.status);
            return hider.show();
          }
          return response.json().then(
            (parsed: unknown) =>
              previewing !== null ? answerPreview(parsed, hider, which) : answer(parsed, hider, which),
            () => hider.show(),
          );
        },
        () => {
          if (previewing !== null) previewRefused(0);
          refused(0);
          hider.show();
        },
      )
      .then(undefined, () => hider.show());
  }

  /**
   * One re-evaluation: one read of the kept page rules against the current
   * path. A new path undoes the current changes and asks for its own, unless
   * fresh rules cover nothing there. Only the first one may hide the page.
   */
  function evaluate(hide: boolean): void {
    if (!env.allowed()) return;
    const location = win.location;
    const path = location.pathname;
    const skip = preview === null && rulesSkip(env.local(), location.host, path, env.now());
    if (path === askedPath) return;
    askedPath = path;
    asked += 1;
    const which = asked;
    if (current) {
      const leaving = current;
      current = null;
      leaving.stop();
    }
    // A page view of the visit whether or not it makes a call, so a visit that
    // starts on a page no experiment covers keeps that page's campaign.
    let details: Record<string, string> = {};
    if (preview === null) {
      try {
        details = viewDetails();
      } catch {
        details = {};
      }
      try {
        view = { id: newId(captures), host: location.host, path, details, counted: new Set(), reported: new Set() };
        countPageViews();
      } catch {
        // This page view counts no goal.
      }
    }
    if (skip || location.host.length > MAX_HOST_LENGTH || path.length > MAX_PATH_LENGTH) return;
    const hider = hide ? hidePage(win.document, captures, config.timeoutMs) : NO_HIDE;
    try {
      ask(hider, path, which, details);
    } catch {
      hider.show();
    }
  }

  /** [Exit preview]: the preview's changes are undone, the banner goes, and the tab forgets the link. */
  function exitPreview(): void {
    endPreview(env.session());
    preview = null;
    if (current) {
      const leaving = current;
      current = null;
      leaving.stop();
    }
    if (banner) {
      const leaving = banner;
      banner = null;
      leaving.remove();
    }
  }

  function openBanner(): void {
    try {
      banner = createOwnUi(win, captures, PREVIEW_LABEL, { text: EXIT_PREVIEW, run: exitPreview });
      banner.say(PREVIEW_LOADING);
    } catch {
      banner = null;
    }
  }

  /** Follows the path of a single-page app, and reports changes lost after the bound as the page goes away. */
  function follow(): void {
    const later = (): void => {
      captures.setTimeout(() => {
        try {
          evaluate(false);
        } catch {
          // The page carries on as it is.
        }
      }, 0);
    };
    const history = win.history;
    for (const name of ["pushState", "replaceState"] as const) {
      const original = history[name];
      if (typeof original !== "function") continue;
      history[name] = function (this: History, ...args: Parameters<History["pushState"]>): void {
        const result = original.apply(this, args);
        later();
        return result;
      };
    }
    captures.addEventListener("popstate", later);
    const lost = (): void => {
      if (current) current.lost();
    };
    captures.addEventListener("pagehide", lost);
    captures.addEventListener("visibilitychange", () => {
      if (win.document.visibilityState === "hidden") lost();
    });
  }

  return {
    start() {
      if (started) return;
      started = true;
      preview = env.previewToken();
      if (preview !== null) openBanner();
      if (backoffActive(env.session(), env.now())) {
        if (banner) banner.say(PREVIEW_FAILED);
        return;
      }
      try {
        follow();
      } catch {
        // A page whose history cannot be wrapped still gets this page's changes.
      }
      try {
        listen();
      } catch {
        // No click goal counts on this page.
      }
      // The page is hidden only while nothing of it can have been painted (its
      // <body> does not exist yet); a later start, such as a consent() after the
      // page has rendered, applies the changes in view.
      evaluate(win.document.body === null);
      // Goal events kept from an earlier page; a preview tab sends none.
      try {
        if (preview === null) outbox.flush();
      } catch {
        // They are sent with the next event, or from the next page.
      }
    },
    track(event, options) {
      if (typeof event !== "string" || event.length < 1 || event.length > MAX_EVENT_LENGTH) {
        win.console.warn(BAD_EVENT);
        return;
      }
      // Before consent, after revokeConsent() and in a preview, track() reads and sends nothing.
      if (!env.allowed() || preview !== null) return;
      const keys = Object.keys(readAssigned(env.local(), env.now())).slice(0, 100);
      if (keys.length === 0) return;
      const value = isRecord(options) && typeof options.value === "number" && isFinite(options.value) ? options.value : null;
      const userId = visitorId(win, config, captures);
      const events = keys.map((key) => {
        const entry: Record<string, unknown> = { event_type: event, user_id: userId, experiment_key: key };
        if (value !== null) entry.value = value;
        return entry;
      });
      const request = send(BATCH_PATH, { events }, { apiKey: true, keepalive: true });
      if (request) request.then(undefined, () => undefined);
    },
  };
}
