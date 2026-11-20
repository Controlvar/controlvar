// Builds one standalone React fixture of the page-runtime project (#1545):
// the page rendered on the server with react-dom/server, and the bundle that
// hydrates it (esbuild, from sdk/visual's devDependencies). Run by
// ../build.mjs with NODE_ENV=production:
//   node standalone/build.mjs <name> <directory whose node_modules holds react> <split: 0|1>
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const [name, reactRoot, split] = process.argv.slice(2);
if (!name || !reactRoot || !["0", "1"].includes(split) || process.env.NODE_ENV !== "production") {
  throw new Error("usage: NODE_ENV=production node standalone/build.mjs <name> <react root> <0|1>");
}
const here = dirname(fileURLToPath(import.meta.url));
const fixtures = join(here, "..");
const sdkVisual = join(fixtures, "..", "..", "..", "..", "..", "sdk", "visual");
const fromReact = createRequire(join(reactRoot, "package.json"));
const esbuild = createRequire(join(sdkVisual, "package.json"))("esbuild");

const React = fromReact("react");
const { renderToString } = fromReact("react-dom/server");
const { makeApp } = await import("../shared/app.mjs");
const markup = renderToString(React.createElement(makeApp(React, { split: split === "1" })));

const out = join(fixtures, name, "out");
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
writeFileSync(
  join(out, "index.html"),
  `<!doctype html><html lang="en"><head><meta charset="utf-8">` +
    `<script src="/controlvar-visual.js" data-api-url="http://localhost:4100" data-api-key="test-website-key"></script>` +
    `<title>${name}</title></head><body><div id="root">${markup}</div><script src="/client.js"></script></body></html>`,
);

// Every import of react, react-dom and their subpaths resolves in reactRoot,
// so the bundle carries the React the page was rendered with.
const sameReact = {
  name: "same-react",
  setup(build) {
    build.onResolve({ filter: /^(react|react-dom|scheduler)(\/.*)?$/ }, (args) => ({ path: fromReact.resolve(args.path) }));
  },
};
await esbuild.build({
  entryPoints: [join(here, "client.mjs")],
  bundle: true,
  minify: true,
  format: "iife",
  platform: "browser",
  define: { "process.env.NODE_ENV": '"production"', SPLIT: split === "1" ? "true" : "false" },
  plugins: [sameReact],
  outfile: join(out, "client.js"),
  logLevel: "warning",
});
console.log(`${name}: React ${React.version}, ${markup.length} characters of server markup`);
