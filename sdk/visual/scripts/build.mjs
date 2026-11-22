// Builds the two files the package ships, from src/ (esbuild, bundled):
//
//   dist/controlvar-visual.js  the classic script a site loads with <script src>
//                              (an IIFE: no import, no export, no globals but
//                              window.controlvar)
//   dist/index.mjs             the ESM entry, for a site that bundles it
//
// dist/ is emptied first, and the build fails unless both files exist after.
import { build } from "esbuild";
import { existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");

const OUTPUTS = {
  classic: join(dist, "controlvar-visual.js"),
  esm: join(dist, "index.mjs"),
};

rmSync(dist, { recursive: true, force: true });

const common = {
  absWorkingDir: root,
  bundle: true,
  platform: "browser",
  target: ["es2019"],
  legalComments: "none",
  logLevel: "warning",
};

await build({ ...common, entryPoints: ["src/classic.ts"], format: "iife", outfile: OUTPUTS.classic });
await build({ ...common, entryPoints: ["src/index.ts"], format: "esm", outfile: OUTPUTS.esm });

for (const file of Object.values(OUTPUTS)) {
  if (!existsSync(file)) {
    throw new Error(`the build did not write ${file}`);
  }
}
