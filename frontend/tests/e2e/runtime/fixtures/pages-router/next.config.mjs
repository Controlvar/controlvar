// A customer site built with Next's Pages Router (#1545, the page-runtime
// project), from the dashboard's own next and react. A static export:
// `node tests/e2e/runtime/fixtures/build.mjs` writes it to out/ here.
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "..");

export default {
  output: "export",
  reactStrictMode: true,
  turbopack: { root: frontend },
  outputFileTracingRoot: frontend,
};
