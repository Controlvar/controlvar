import type { Captures } from "./captures";
import { configured, type Config } from "./config";
import { KEYS } from "./keys";

export interface SendOptions {
  /** Send the page's API key (X-API-Key). */
  apiKey?: boolean;
  /**
   * The editor link's claim: it skips the consent gate and the backoff gate,
   * and only those two.
   */
  editorLink?: boolean;
  keepalive?: boolean;
}

export interface Gates {
  /** False while the tag has data-require-consent and consent() has not been called. */
  consented(): boolean;
  /** True while this page has been told to send nothing (a backoff kept in memory only). */
  silent?(): boolean;
}

export type Send = (path: string, body: unknown, options?: SendOptions) => Promise<Response> | null;

/** How long the script stays silent after a refused or failed call. */
export const BACKOFF_MS = 5 * 60 * 1000;

export function backoffActive(storage: Storage | null, now: number): boolean {
  try {
    const until = Number(storage ? storage.getItem(KEYS.backoff) : null);
    return Number.isFinite(until) && until > now;
  } catch {
    return false;
  }
}

/** Starts the silence in this tab (sessionStorage controlvar_backoff). */
export function startBackoff(storage: Storage | null, now: number): void {
  try {
    if (storage) storage.setItem(KEYS.backoff, String(now + BACKOFF_MS));
  } catch {
    // Refused storage: the caller keeps the silence in memory for this page.
  }
}

/**
 * The one function that sends anything. Every request is a JSON POST that
 * carries no cookies and no Referer. It answers null when it sends nothing.
 */
export function createSend(
  captures: Captures,
  config: Config,
  gates: Gates,
  storage: () => Storage | null,
  now: () => number,
): Send {
  return (path, body, options = {}) => {
    if (!configured(config)) return null;
    if (!options.editorLink) {
      if (!gates.consented()) return null;
      if (gates.silent && gates.silent()) return null;
      if (backoffActive(storage(), now())) return null;
    }
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (options.apiKey && config.apiKey) headers["X-API-Key"] = config.apiKey;
    try {
      return captures.fetch(`${config.apiUrl}${path}`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        credentials: "omit",
        referrerPolicy: "no-referrer",
        keepalive: options.keepalive === true,
      });
    } catch {
      return null;
    }
  };
}
