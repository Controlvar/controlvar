/**
 * Preview links: a page opened with `#controlvar-preview=<43 base64url
 * characters>`, made on the experiment's page in Controlvar. The first run
 * takes the token out of the address; the tab then keeps it (sessionStorage
 * controlvar_preview) until [Exit preview], so the preview lasts for the tab.
 * Any other value after `#controlvar-preview=` is ignored.
 */
import type { Captures } from "./captures";
import { KEYS } from "./keys";

export const PREVIEW_FRAGMENT = /^#controlvar-preview=([A-Za-z0-9_-]{43})$/;
export const PREVIEW_TOKEN = /^[A-Za-z0-9_-]{43}$/;
export const PREVIEW_PATH = "/api/v1/tracking/visual/preview";

export const PREVIEW_LABEL = "Controlvar preview";
export const PREVIEW_LOADING = "Loading the preview…";
export const PREVIEW_GONE =
  "This preview link has expired, was revoked, or does not exist. Create a new one on the experiment's page in Controlvar.";
export const PREVIEW_SCOPED_KEY =
  "This preview cannot be shown: the Controlvar snippet on this page uses a key with scopes, which belongs on a server. Ask your developer to use a website key.";
export const PREVIEW_FAILED = "This preview cannot be shown: Controlvar did not answer. Reload the page to try again.";
export const EXIT_PREVIEW = "Exit preview";

export function previewShowing(variant: string): string {
  return `Variant ${variant}, as visitors in it see this page. Nothing is recorded.`;
}

export function previewElsewhere(variant: string): string {
  return `Variant ${variant} changes none of this page: it is not one of the experiment's pages. Nothing is recorded.`;
}

/** In the first run: the token of a preview link this page was opened with, taken out of the address. */
export function takePreviewLink(win: Window, captures: Captures): string | null {
  const match = PREVIEW_FRAGMENT.exec(win.location.hash);
  if (!match) return null;
  captures.replaceState(win.history.state, "", win.location.pathname + win.location.search);
  return match[1];
}

export function storePreview(storage: Storage | null, token: string): void {
  try {
    if (storage) storage.setItem(KEYS.preview, token);
  } catch {
    // Storage refused: the preview lasts for this page.
  }
}

export function storedPreview(storage: Storage | null): string | null {
  try {
    const token = storage ? storage.getItem(KEYS.preview) : null;
    return token !== null && PREVIEW_TOKEN.test(token) ? token : null;
  } catch {
    return null;
  }
}

export function endPreview(storage: Storage | null): void {
  try {
    if (storage) storage.removeItem(KEYS.preview);
  } catch {
    // Storage refused: nothing was kept.
  }
}
