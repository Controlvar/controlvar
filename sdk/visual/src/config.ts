import { COLLECT_TOKENS, DEFAULT_COLLECT, type Collect, type CollectToken } from "./details";

/** What the script tag says, read once, in the first run. */
export interface Config {
  /** The Controlvar API's base URL, without a trailing slash; null when missing or not http(s). */
  apiUrl: string | null;
  apiKey: string | null;
  /** The dashboard's origin, exactly (`https://host[:port]`); null when absent or not an origin. */
  editorOrigin: string | null;
  requireConsent: boolean;
  /** How long the page may stay hidden: data-timeout-ms, 0 to 4000, 1000 by default. */
  timeoutMs: number;
  /** The site's own id for the visitor (data-user-id), 1 to 255 characters; null when absent. */
  userId: string | null;
  /** The visitor-id cookie's name (data-cookie-name). */
  cookieName: string;
  /**
   * The site's visitor attributes (data-attributes: a JSON object of at most
   * 4096 bytes, as written and as sent), without any web_ key; null when
   * absent or refused.
   */
  attributes: Record<string, unknown> | null;
  /** The website details the script collects (data-collect; details.ts). */
  collect: Collect;
  /** One console warning per problem found in the tag's optional attributes. */
  warnings: string[];
}

export const DEFAULT_TIMEOUT_MS = 1000;
export const MAX_TIMEOUT_MS = 4000;
export const DEFAULT_COOKIE_NAME = "controlvar_uid";
/** data-attributes' limit in UTF-8 bytes, for the tag's text and for the JSON the script sends. */
export const MAX_ATTRIBUTES_BYTES = 4096;
/** The prefix of the keys the script sets itself; data-attributes may not use it. */
export const DETAIL_PREFIX = "web_";

export const EMAIL_USER_ID =
  "Controlvar: data-user-id looks like an email address. It is sent with every request; use an id that does not identify the person.";
export const BAD_ATTRIBUTES =
  "Controlvar: data-attributes is not a JSON object of at most 4 KB, so it was ignored.";
export const WEB_ATTRIBUTES =
  "Controlvar: data-attributes keys that start with web_ are set by the script itself, so they were ignored.";
export const BAD_USER_ID = "Controlvar: data-user-id is longer than 255 characters, so it was ignored.";
export const BAD_COOKIE_NAME =
  "Controlvar: data-cookie-name may hold only letters, digits, - and _ (at most 64), so the default name is used.";
export const UNKNOWN_COLLECT =
  "Controlvar: data-collect takes device, browser, language, visitor, referrer, utm, landing or none, separated by commas; the values it does not know were ignored.";
export const NO_COLLECT =
  "Controlvar: data-collect is empty or names none with other values, so the script collects no website details.";

/** The bytes of `text` in UTF-8 (a lone surrogate counts 3, as the server counts it). */
export function utf8Bytes(text: string): number {
  let bytes = 0;
  for (let index = 0; index < text.length; index += 1) {
    const unit = text.charCodeAt(index);
    if (unit < 0x80) bytes += 1;
    else if (unit < 0x800) bytes += 2;
    else if (unit >= 0xd800 && unit <= 0xdbff && index + 1 < text.length) {
      const next = text.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        bytes += 4;
        index += 1;
      } else bytes += 3;
    } else bytes += 3;
  }
  return bytes;
}

function collectFrom(value: string | null, warnings: string[]): Collect {
  const on: Record<CollectToken, boolean> = {
    device: false,
    browser: false,
    language: false,
    visitor: false,
    referrer: false,
    utm: false,
    landing: false,
  };
  if (value === null) {
    for (const token of DEFAULT_COLLECT) on[token] = true;
    return on;
  }
  const tokens = value
    .split(",")
    .map((token) => token.trim())
    .filter((token) => token !== "");
  if (tokens.indexOf("none") >= 0 || tokens.length === 0) {
    if (tokens.length !== 1) warnings.push(NO_COLLECT);
    return on;
  }
  let unknown = false;
  for (const token of tokens) {
    if ((COLLECT_TOKENS as readonly string[]).indexOf(token) >= 0) on[token as CollectToken] = true;
    else unknown = true;
  }
  if (unknown) warnings.push(UNKNOWN_COLLECT);
  return on;
}

/** data-timeout-ms as a whole number of milliseconds, clamped to 0..4000; the default when absent or not a number. */
export function timeoutFrom(value: string | null): number {
  if (value === null || value.trim() === "") return DEFAULT_TIMEOUT_MS;
  const number = Number(value);
  if (!Number.isFinite(number)) return DEFAULT_TIMEOUT_MS;
  return Math.min(MAX_TIMEOUT_MS, Math.max(0, Math.round(number)));
}

/**
 * data-attributes: a JSON object of at most 4096 UTF-8 bytes as the tag writes
 * it (measured before it is parsed) and as the script sends it (compact JSON,
 * so that a short text cannot grow, as `[1e20]` does, into a larger call).
 * Its web_ keys are dropped with one warning: the script sets those.
 */
function attributesFrom(value: string | null, warnings: string[]): Record<string, unknown> | null {
  if (value === null) return null;
  try {
    const parsed: unknown = utf8Bytes(value) <= MAX_ATTRIBUTES_BYTES ? JSON.parse(value) : null;
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      // No prototype: every key, whatever its name, stays the object's own key.
      const attributes: Record<string, unknown> = Object.create(null);
      let dropped = false;
      for (const key of Object.keys(parsed)) {
        if (key.indexOf(DETAIL_PREFIX) === 0) dropped = true;
        else attributes[key] = (parsed as Record<string, unknown>)[key];
      }
      if (utf8Bytes(JSON.stringify(attributes)) <= MAX_ATTRIBUTES_BYTES) {
        if (dropped) warnings.push(WEB_ATTRIBUTES);
        return attributes;
      }
    }
  } catch {
    // Reported below.
  }
  warnings.push(BAD_ATTRIBUTES);
  return null;
}

export const MISSING_CONFIG =
  "Controlvar: the script tag needs data-api-url (an http or https URL) and data-api-key; the script does nothing without them.";

function httpUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  } catch {
    return null;
  }
  return value.replace(/\/+$/, "");
}

function exactOrigin(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if ((url.protocol === "http:" || url.protocol === "https:") && url.origin === value) return value;
  } catch {
    return null;
  }
  return null;
}

export function readConfig(script: Element | null): Config {
  const attr = (name: string) => (script ? script.getAttribute(name) : null);
  const warnings: string[] = [];
  let userId = attr("data-user-id") || null;
  if (userId !== null && userId.length > 255) {
    warnings.push(BAD_USER_ID);
    userId = null;
  }
  if (userId !== null && userId.indexOf("@") >= 0) warnings.push(EMAIL_USER_ID);
  let cookieName = attr("data-cookie-name") || DEFAULT_COOKIE_NAME;
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(cookieName)) {
    warnings.push(BAD_COOKIE_NAME);
    cookieName = DEFAULT_COOKIE_NAME;
  }
  return {
    apiUrl: httpUrl(attr("data-api-url")),
    apiKey: attr("data-api-key") || null,
    editorOrigin: exactOrigin(attr("data-editor-origin")),
    requireConsent: script ? script.hasAttribute("data-require-consent") : false,
    timeoutMs: timeoutFrom(attr("data-timeout-ms")),
    userId,
    cookieName,
    attributes: attributesFrom(attr("data-attributes"), warnings),
    collect: collectFrom(attr("data-collect"), warnings),
    warnings,
  };
}

export function configured(config: Config): boolean {
  return config.apiUrl !== null && config.apiKey !== null;
}
