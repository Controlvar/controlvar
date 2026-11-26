import type { ChildProcess } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { API_URL } from "../env";
import { startStaticServer } from "../free-port";
import { ORACLE_LOG } from "../oracles/oracle";

/**
 * U-SDK's page: sdk-page/index.html with this checkout's JavaScript SDK,
 * served by tests/e2e/static-server.mjs on PERSONAS_SDK_PORT (the
 * persona-days job sets it; a free port when unset), with /api passed on to
 * the API so the page and the API share an origin.
 *
 * The SDK is sdk/js/dist exactly as `npm run build` in sdk/js leaves it (the
 * job's "Build the JavaScript SDK" step): CommonJS files that require only
 * each other. No bundler is installed by anything the job installs, so
 * `sdkBundle` wraps each file in a function and adds the few lines of
 * `require` that link them, and the page reads the SDK's exports from
 * `window.Controlvar`. The files' own text is not changed.
 */

/** sdk/js's build output, from this checkout. */
export const SDK_DIST = join(__dirname, "..", "..", "..", "..", "sdk", "js", "dist");
const PAGE = join(__dirname, "sdk-page", "index.html");

/** What the page reads from config.json. */
export interface SdkPageConfig {
  apiKey: string;
  experimentKey: string;
  flagKey: string;
}

/** sdk/js/dist's CommonJS files as one script for a browser; `window.Controlvar` is its index. */
export function sdkBundle(dist: string = SDK_DIST): string {
  if (!existsSync(join(dist, "index.js"))) {
    throw new Error(`${dist} has no index.js: build the SDK first (cd sdk/js && npm ci && npm run build)`);
  }
  const modules = readdirSync(dist)
    .filter((name) => name.endsWith(".js"))
    .sort()
    .map((name) => {
      const source = readFileSync(join(dist, name), "utf8").replace(/^\/\/# sourceMappingURL=.*$/m, "");
      return `${JSON.stringify(`./${name.slice(0, -3)}`)}: function (module, exports, require) {\n${source}\n}`;
    });
  return [
    "(function () {",
    `  var defined = {\n${modules.join(",\n")}\n  };`,
    "  var loaded = {};",
    "  function load(id) {",
    '    if (!Object.prototype.hasOwnProperty.call(defined, id)) throw new Error("the SDK build has no module " + id);',
    "    if (!loaded[id]) {",
    "      var module = { exports: {} };",
    "      loaded[id] = module;",
    "      defined[id].call(module.exports, module, module.exports, load);",
    "    }",
    "    return loaded[id].exports;",
    "  }",
    '  window.Controlvar = load("./index");',
    "})();",
    "",
  ].join("\n");
}

/** The page, its config and the SDK in a directory beside the oracle log, served; `stop` ends the server. */
export async function serveSdkPage(config: SdkPageConfig): Promise<{ origin: string; stop: () => Promise<void> }> {
  const dir = join(dirname(ORACLE_LOG), "sdk-page");
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });
  copyFileSync(PAGE, join(dir, "index.html"));
  writeFileSync(join(dir, "sdk.js"), sdkBundle());
  writeFileSync(join(dir, "config.json"), JSON.stringify(config));

  const { child, port } = await startStaticServer(["--dir", dir, "--api", API_URL], "PERSONAS_SDK_PORT");
  return { origin: `http://127.0.0.1:${port}`, stop: () => stopped(child) };
}

function stopped(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    child.once("exit", () => resolve());
    child.kill("SIGTERM");
  });
}
