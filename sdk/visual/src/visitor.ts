/**
 * The visitor's id: the site's own data-user-id, else the id in the
 * visitor-id cookie, else a new random id, which is then written to that
 * cookie for 30 days. The cookie holds nothing but that id.
 */
import type { Captures } from "./captures";
import type { Config } from "./config";

export const COOKIE_MAX_AGE_SECONDS = 2592000;
const ID = /^[A-Za-z0-9_-]{1,255}$/;

function readCookie(cookies: string, name: string): string | null {
  for (const part of cookies.split(";")) {
    const at = part.indexOf("=");
    if (at > 0 && part.slice(0, at).trim() === name) return part.slice(at + 1).trim();
  }
  return null;
}

/** A random version 4 UUID from the captured getRandomValues: a new visitor's id, and each page view's. */
export function newId(captures: Captures): string {
  const bytes = captures.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  let hex = "";
  for (let index = 0; index < bytes.length; index += 1) hex += (bytes[index] + 0x100).toString(16).slice(1);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function visitorId(win: Window, config: Config, captures: Captures): string {
  if (config.userId !== null) return config.userId;
  const doc = win.document;
  let existing: string | null = null;
  try {
    existing = readCookie(doc.cookie, config.cookieName);
  } catch {
    existing = null;
  }
  if (existing !== null && ID.test(existing)) return existing;
  const id = newId(captures);
  const secure = win.location.protocol === "https:" ? "; Secure" : "";
  try {
    doc.cookie = `${config.cookieName}=${id}; Max-Age=${COOKIE_MAX_AGE_SECONDS}; Path=/; SameSite=Lax${secure}`;
  } catch {
    // Cookies refused: the id lasts for this page.
  }
  return id;
}
