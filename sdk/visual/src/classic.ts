/**
 * The classic script (dist/controlvar-visual.js): what a site's
 * `<script src=…>` loads. Its first synchronous run keeps the browser
 * functions it needs (captures.ts) before it does anything else, then reads
 * its tag and handles a visual editor link. It publishes
 * `window.controlvar.visual`, next to whatever else the page keeps under
 * `window.controlvar`.
 */
import { capture } from "./captures";
import { configured, MISSING_CONFIG, readConfig } from "./config";
import { runEditorLink } from "./editor-link";
import { createSend } from "./send";
import { VERSION } from "./version";

interface VisualApi {
  readonly version: string;
}

interface ControlvarGlobal {
  visual?: VisualApi;
  [key: string]: unknown;
}

const win = window;

function firstRun(): void {
  const captures = capture(win);
  const config = readConfig(win.document.currentScript);
  if (!configured(config)) win.console.error(MISSING_CONFIG);

  const state = { consented: !config.requireConsent };
  const sessionStorageOrNull = (): Storage | null => {
    try {
      return win.sessionStorage;
    } catch {
      return null;
    }
  };
  const send = createSend(
    captures,
    config,
    { consented: () => state.consented },
    sessionStorageOrNull,
    () => Date.now(),
  );

  runEditorLink({ window: win, captures, config, send });
}

try {
  firstRun();
} catch (error) {
  win.console.error("Controlvar: the visual script stopped:", error);
} finally {
  const host = win as unknown as { controlvar?: ControlvarGlobal };
  const namespace: ControlvarGlobal = host.controlvar ?? (host.controlvar = {});
  namespace.visual = { version: VERSION };
}
