/**
 * The classic script (dist/controlvar-visual.js): what a site's
 * `<script src=…>` loads. Its first synchronous run keeps the browser
 * functions it needs (captures.ts) before it does anything else, then reads
 * its tag and handles a visual editor link. On any other page view it asks
 * for the page's visual changes and applies them (runtime.ts), unless the
 * tag waits for consent. It publishes `window.controlvar.visual`, next to
 * whatever else the page keeps under `window.controlvar`: version, track(),
 * consent() and revokeConsent().
 *
 * consent() lets a tag with data-require-consent start. revokeConsent() is
 * synchronous, idempotent and never throws: it sets the one flag the send
 * function checks first, deletes the cookie and every controlvar_ key
 * (consent.ts), and, with data-require-consent, makes the script wait for
 * consent() again. Requests already sent are not recalled, and changes
 * already shown stay until the next page.
 */
import { pageBound } from "./bound";
import { capture } from "./captures";
import { configured, DEFAULT_COOKIE_NAME, MISSING_CONFIG, readConfig } from "./config";
import { deleteStored } from "./consent";
import { runEditorLink } from "./editor-link";
import { storedPreview, storePreview, takePreviewLink } from "./preview";
import { createRuntime, type Runtime } from "./runtime";
import { createSend } from "./send";
import { VERSION } from "./version";

interface VisualApi {
  readonly version: string;
  track(event: unknown, options?: unknown): void;
  consent(): void;
  revokeConsent(): void;
}

interface Controls {
  runtime: Runtime;
  consent(): void;
  revoke(): void;
}

interface ControlvarGlobal {
  visual?: VisualApi;
  [key: string]: unknown;
}

const win = window;

function storage(name: "localStorage" | "sessionStorage"): () => Storage | null {
  return () => {
    try {
      return win[name];
    } catch {
      return null;
    }
  };
}

function firstRun(): Controls {
  const captures = capture(win);
  const config = readConfig(win.document.currentScript);
  if (!configured(config)) win.console.error(MISSING_CONFIG);
  for (const warning of config.warnings) win.console.warn(warning);

  const state = { consented: !config.requireConsent, silent: false, revoked: false };
  const allowed = (): boolean => state.consented && !state.revoked;
  const session = storage("sessionStorage");
  const now = (): number => Date.now();
  const send = createSend(
    captures,
    config,
    { consented: () => state.consented, silent: () => state.silent, revoked: () => state.revoked },
    session,
    now,
  );

  const link = runEditorLink({ window: win, captures, config, send });
  // A preview link's token: kept for the tab once the script may store; until then, for this page.
  let previewLink = link.opened ? null : takePreviewLink(win, captures);
  const keepPreview = (): void => {
    if (previewLink !== null && allowed()) storePreview(session(), previewLink);
  };
  keepPreview();
  const bound = pageBound(win, captures);
  const runtime = createRuntime({
    bound,
    window: win,
    captures,
    config,
    send,
    local: storage("localStorage"),
    session,
    now,
    silence: () => {
      state.silent = true;
    },
    allowed,
    previewToken: () => (previewLink !== null ? previewLink : storedPreview(session())),
  });
  const startIfAllowed = (): void => {
    if (!link.opened && configured(config) && allowed()) runtime.start();
  };
  startIfAllowed();
  return {
    runtime,
    consent() {
      state.revoked = false;
      state.consented = true;
      keepPreview();
      startIfAllowed();
    },
    revoke() {
      state.revoked = true;
      if (config.requireConsent) state.consented = false;
      previewLink = null;
      deleteStored(win, config.cookieName);
    },
  };
}

let controls: Controls | null = null;
try {
  controls = firstRun();
} catch (error) {
  win.console.error("Controlvar: the visual script stopped:", error);
} finally {
  const host = win as unknown as { controlvar?: ControlvarGlobal };
  const namespace: ControlvarGlobal = host.controlvar ?? (host.controlvar = {});
  const ready = controls;
  namespace.visual = {
    version: VERSION,
    track: (event, options) => {
      try {
        if (ready) ready.runtime.track(event, options);
      } catch {
        // track() never throws into the page.
      }
    },
    consent: () => {
      try {
        if (ready) ready.consent();
      } catch {
        // consent() never throws into the page.
      }
    },
    revokeConsent: () => {
      try {
        if (ready) ready.revoke();
        else deleteStored(win, DEFAULT_COOKIE_NAME);
      } catch {
        // revokeConsent() never throws into the page.
      }
    },
  };
}
