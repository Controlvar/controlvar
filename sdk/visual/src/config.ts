/** What the script tag says, read once, in the first run. */
export interface Config {
  /** The Controlvar API's base URL, without a trailing slash; null when missing or not http(s). */
  apiUrl: string | null;
  apiKey: string | null;
  /** The dashboard's origin, exactly (`https://host[:port]`); null when absent or not an origin. */
  editorOrigin: string | null;
  requireConsent: boolean;
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
  return {
    apiUrl: httpUrl(attr("data-api-url")),
    apiKey: attr("data-api-key") || null,
    editorOrigin: exactOrigin(attr("data-editor-origin")),
    requireConsent: script ? script.hasAttribute("data-require-consent") : false,
  };
}

export function configured(config: Config): boolean {
  return config.apiUrl !== null && config.apiKey !== null;
}
