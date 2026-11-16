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
}

export type Send = (path: string, body: unknown, options?: SendOptions) => Promise<Response> | null;

function backoffActive(storage: Storage | null, now: number): boolean {
  try {
    const until = Number(storage ? storage.getItem(KEYS.backoff) : null);
    return Number.isFinite(until) && until > now;
  } catch {
    return false;
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
