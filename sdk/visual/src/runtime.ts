/**
 * What the script does on an ordinary page view: ask which visual changes
 * this visitor gets on this page (POST /api/v1/tracking/visual), hide the
 * page meanwhile, check and apply the answer, and report what could not be
 * applied. Also `track()`, which records an event for the experiments this
 * visitor was assigned to in the last 30 days.
 *
 * Whatever goes wrong, the page is shown: every path that hid it ends by
 * showing it, and the hide has its own timer.
 */
import { createApplier } from "./apply";
import type { Captures } from "./captures";
import type { Config } from "./config";
import { hidePage, type Hider } from "./hide";
import { KEYS } from "./keys";
import { keepRules, readAnswerRules, rulesSkip } from "./pages";
import { createReporter } from "./reports";
import { FORMAT_VERSION } from "./rules";
import { backoffActive, startBackoff, type Send } from "./send";
import { validateChanges } from "./validate";
import { visitorId } from "./visitor";

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
}

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

export function createRuntime(env: RuntimeEnv): Runtime {
  const { window: win, captures, config, send } = env;
  const reporter = createReporter(send);
  let started = false;

  function remember(keys: string[]): void {
    if (keys.length === 0) return;
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
    startBackoff(env.session(), env.now());
    env.silence();
    if (status === 401 || status === 403) win.console.warn(KEY_REFUSED);
  }

  function answer(body: unknown, hider: Hider): void {
    if (!isRecord(body)) return hider.show();
    const rules = readAnswerRules(body.pages, env.now());
    if (rules) keepRules(env.local(), rules);
    const experiments = Array.isArray(body.experiments) ? body.experiments.slice(0, MAX_EXPERIMENTS) : [];
    const applier = createApplier({ window: win, captures, reporter });
    const assigned: string[] = [];
    for (const experiment of experiments) {
      if (!isRecord(experiment) || experiment.assigned !== true) continue;
      const key = experiment.experiment_key;
      if (typeof key !== "string" || key.length === 0) continue;
      assigned.push(key);
      if (experiment.changes === null || experiment.changes === undefined) continue;
      const verdict = validateChanges(experiment.changes);
      if (!verdict.ok) reporter.notApplied(key, null, verdict.reason);
      else applier.add(key, verdict.changes);
    }
    remember(assigned);
    if (applier.empty) return hider.show();
    applier.start(() => hider.show());
  }

  function ask(hider: Hider, userId: string): void {
    const location = win.location;
    const body: Record<string, unknown> = {
      user_id: userId,
      host: location.host,
      path: location.pathname,
      format_version: FORMAT_VERSION,
    };
    if (config.attributes !== null) body.context = config.attributes;
    const request = send(VISUAL_PATH, body, { apiKey: true });
    if (request === null) return hider.show();
    request
      .then(
        (response) => {
          if (!response.ok) {
            refused(response.status);
            return hider.show();
          }
          return response.json().then(
            (parsed: unknown) => answer(parsed, hider),
            () => hider.show(),
          );
        },
        () => {
          refused(0);
          hider.show();
        },
      )
      .then(undefined, () => hider.show());
  }

  return {
    start() {
      if (started) return;
      started = true;
      const location = win.location;
      if (backoffActive(env.session(), env.now())) return;
      if (location.host.length > MAX_HOST_LENGTH || location.pathname.length > MAX_PATH_LENGTH) return;
      if (rulesSkip(env.local(), location.host, location.pathname, env.now())) return;
      const userId = visitorId(win, config, captures);
      const hider = hidePage(win.document, captures, config.timeoutMs);
      try {
        ask(hider, userId);
      } catch {
        hider.show();
      }
    },
    track(event, options) {
      if (typeof event !== "string" || event.length < 1 || event.length > MAX_EVENT_LENGTH) {
        win.console.warn(BAD_EVENT);
        return;
      }
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
