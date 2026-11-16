/** The browser storage keys this script reads or writes so far. */
export const KEYS = {
  /** sessionStorage: the editor link was opened in this tab. */
  editorOpen: "controlvar_editor_open",
  /** sessionStorage: send nothing until this time (ms since the epoch). */
  backoff: "controlvar_backoff",
} as const;
