/**
 * Every browser storage key the script may use, by its name in
 * tests/sdk-contract/visual-storage-keys.json (a unit test pins the two
 * equal). The cookie's name is data-cookie-name's default.
 */
export const KEYS = {
  /** cookie: the visitor's random id. */
  uid: "controlvar_uid",
  /** localStorage: the visit's start and last activity. */
  visit: "controlvar_visit",
  /** localStorage: when this browser was first seen. */
  seen: "controlvar_seen",
  /** localStorage: the experiments this visitor was assigned to, and when. */
  assigned: "controlvar_assigned",
  /** localStorage: the goals counted for this visitor. */
  goals: "controlvar_goals",
  /** localStorage: goal events not yet delivered. */
  outbox: "controlvar_outbox",
  /** localStorage: the page rules of this site, for 60 seconds. */
  pages: "controlvar_pages",
  /** sessionStorage: send nothing until this time (ms since the epoch). */
  backoff: "controlvar_backoff",
  /** sessionStorage: a preview link's token, for this tab. */
  preview: "controlvar_preview",
  /** sessionStorage: the editor link was opened in this tab. */
  editorOpen: "controlvar_editor_open",
} as const;

/** Every key's prefix; revokeConsent() deletes every key that has it. */
export const PREFIX = "controlvar_";

/** The keys this version of the script writes. */
export const WRITTEN = [KEYS.uid, KEYS.assigned, KEYS.pages, KEYS.backoff, KEYS.preview, KEYS.editorOpen] as const;
