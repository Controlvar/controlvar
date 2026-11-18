/**
 * The classic script (dist/controlvar-visual.js): what a site's
 * `<script src=…>` loads. Its first synchronous run keeps the browser
 * functions it needs (captures.ts) before it does anything else, then reads
 * its tag and handles a visual editor link. On any other page view it asks
 * for the page's visual changes and applies them (runtime.ts), unless the
 * tag waits for consent. It publishes `window.controlvar.visual`, next to
 * whatever else the page keeps under `window.controlvar`.
 */
import { pageBound } from "./bound";
import { capture } from "./captures";
import { configured, MISSING_CONFIG, readConfig } from "./config";
import { runEditorLink } from "./editor-link";
import { createRuntime, type Runtime } from "./runtime";
import { createSend } from "./send";
import { VERSION } from "./version";

interface VisualApi {
  readonly version: string;
  track(event: unknown, options?: unknown): void;
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

function firstRun(): Runtime | null {
  const captures = capture(win);
  const config = readConfig(win.document.currentScript);
  if (!configured(config)) win.console.error(MISSING_CONFIG);
  for (const warning of config.warnings) win.console.warn(warning);

  const state = { consented: !config.requireConsent, silent: false };
  const session = storage("sessionStorage");
  const now = (): number => Date.now();
  const send = createSend(
    captures,
    config,
    { consented: () => state.consented, silent: () => state.silent },
    session,
    now,
  );

  const link = runEditorLink({ window: win, captures, config, send });
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
  });
  if (!link.opened && configured(config) && state.consented) runtime.start();
  return runtime;
}

let runtime: Runtime | null = null;
try {
  runtime = firstRun();
} catch (error) {
  win.console.error("Controlvar: the visual script stopped:", error);
} finally {
  const host = win as unknown as { controlvar?: ControlvarGlobal };
  const namespace: ControlvarGlobal = host.controlvar ?? (host.controlvar = {});
  const started = runtime;
  namespace.visual = {
    version: VERSION,
    track: (event, options) => {
      try {
        if (started) started.track(event, options);
      } catch {
        // track() never throws into the page.
      }
    },
  };
}
