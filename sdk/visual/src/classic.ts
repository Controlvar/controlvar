/**
 * The classic script (dist/controlvar-visual.js): what a site's
 * `<script src=…>` loads. It publishes `window.controlvar.visual`, next to
 * whatever else the page keeps under `window.controlvar`.
 */
import { VERSION } from "./version";

interface VisualApi {
  readonly version: string;
}

interface ControlvarGlobal {
  visual?: VisualApi;
  [key: string]: unknown;
}

const host = window as unknown as { controlvar?: ControlvarGlobal };
const namespace: ControlvarGlobal = host.controlvar ?? (host.controlvar = {});
namespace.visual = { version: VERSION };
