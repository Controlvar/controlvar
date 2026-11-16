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
  /** The site's visitor attributes (data-attributes: a JSON object, at most 4 KB); null when absent or refused. */
  attributes: Record<string, unknown> | null;
  /** One console warning per problem found in the tag's optional attributes. */
  warnings: string[];
}

export const DEFAULT_TIMEOUT_MS = 1000;
export const MAX_TIMEOUT_MS = 4000;
export const DEFAULT_COOKIE_NAME = "controlvar_uid";
export const MAX_ATTRIBUTES_LENGTH = 4096;

export const EMAIL_USER_ID =
  "Controlvar: data-user-id looks like an email address. It is sent with every request; use an id that does not identify the person.";
export const BAD_ATTRIBUTES =
  "Controlvar: data-attributes is not a JSON object of at most 4 KB, so it was ignored.";
export const BAD_USER_ID = "Controlvar: data-user-id is longer than 255 characters, so it was ignored.";
export const BAD_COOKIE_NAME =
  "Controlvar: data-cookie-name may hold only letters, digits, - and _ (at most 64), so the default name is used.";

/** data-timeout-ms as a whole number of milliseconds, clamped to 0..4000; the default when absent or not a number. */
export function timeoutFrom(value: string | null): number {
  if (value === null || value.trim() === "") return DEFAULT_TIMEOUT_MS;
  const number = Number(value);
  if (!Number.isFinite(number)) return DEFAULT_TIMEOUT_MS;
  return Math.min(MAX_TIMEOUT_MS, Math.max(0, Math.round(number)));
}

function attributesFrom(value: string | null, warnings: string[]): Record<string, unknown> | null {
  if (value === null) return null;
  try {
    const parsed: unknown = value.length <= MAX_ATTRIBUTES_LENGTH ? JSON.parse(value) : null;
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
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
    warnings,
  };
}

export function configured(config: Config): boolean {
  return config.apiUrl !== null && config.apiKey !== null;
}
