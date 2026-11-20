/**
 * Withdrawing consent: what revokeConsent() deletes from the browser. The
 * visitor-id cookie (the tag's data-cookie-name, and the default name if the
 * tag names another), and every key with the controlvar_ prefix in this
 * site's localStorage and sessionStorage: the keys this version writes, and
 * the ones the closed key list reserves. Each step is on its own, so a
 * storage the browser refuses does not stop the others.
 */
import { KEYS, PREFIX } from "./keys";

function deleteCookie(win: Window, name: string): void {
  const secure = win.location.protocol === "https:" ? "; Secure" : "";
  try {
    // The same Path the script set it with, or the browser keeps it.
    win.document.cookie = `${name}=; Max-Age=0; Path=/; SameSite=Lax${secure}`;
  } catch {
    // Cookies refused: there is none to delete.
  }
}

function prefixed(storage: Storage): string[] {
  const names: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const name = storage.key(index);
    if (name !== null && name.indexOf(PREFIX) === 0) names.push(name);
  }
  return names;
}

function clear(win: Window & typeof globalThis, area: "localStorage" | "sessionStorage"): void {
  let storage: Storage | null = null;
  try {
    storage = win[area];
  } catch {
    return;
  }
  if (!storage) return;
  const names: string[] = Object.keys(KEYS).map((name) => KEYS[name as keyof typeof KEYS]);
  try {
    for (const name of prefixed(storage)) if (names.indexOf(name) < 0) names.push(name);
  } catch {
    // The listed keys are still deleted.
  }
  for (const name of names) {
    try {
      storage.removeItem(name);
    } catch {
      // The next key is still deleted.
    }
  }
}

/** Deletes the cookie the script sets and every controlvar_ key. Never throws. */
export function deleteStored(win: Window & typeof globalThis, cookieName: string): void {
  deleteCookie(win, cookieName);
  if (cookieName !== KEYS.uid) deleteCookie(win, KEYS.uid);
  clear(win, "localStorage");
  clear(win, "sessionStorage");
}
