const path = require('path');

// The React SDK is consumed straight from source (no build step) so changes in
// sdk/react/src show up in the storefront immediately.
const SDK_ENTRY = path.resolve(__dirname, '../../sdk/react/src/index.ts');

const REPO_ROOT = path.resolve(__dirname, '../..');

// One rule, both bundlers. Next 16 runs Turbopack by default, and a `webpack`
// config with no `turbopack` config is a hard error there ("This build is
// using Turbopack, with a `webpack` config and no `turbopack` config"), not a
// warning -- which is what broke this build on the Next 14 -> 16 bump. The
// webpack block is kept because `next build --webpack` still uses it, and
// because `npm run dev` runs `next dev --webpack` (#1172): on Next 16.4.0 the
// Turbopack dev server fails /products on a direct visit (the address typed,
// or the page reloaded) with "the module factory is not available", because
// src/data/products.ts is in none of the page's chunks. The page loads when
// it is reached from another page, and every other page loads either way.
// Moving one import above another hid it, which is not something to rely on.
// `build` and `start` are unchanged.
// One rule, two spellings. webpack takes absolute paths; Turbopack's
// `resolveAlias` takes paths RELATIVE TO THIS PACKAGE and prefixes anything
// else with `./` -- an absolute path there became
// `./Users/<you>/node_modules/react` and failed to resolve.
const ALIAS_TARGETS = {
  '@controlvar/react-sdk': SDK_ENTRY,
  // Force a single React copy. Without this, the SDK source could resolve
  // sdk/react/node_modules/react and hooks would throw "Invalid hook call".
  react: path.resolve(__dirname, 'node_modules/react'),
  'react-dom': path.resolve(__dirname, 'node_modules/react-dom'),
};

const relativeToPackage = (abs) => {
  const rel = path.relative(__dirname, abs).split(path.sep).join('/');
  return rel.startsWith('.') ? rel : `./${rel}`;
};
const RESOLVE_ALIAS_TURBOPACK = Object.fromEntries(
  Object.entries(ALIAS_TARGETS).map(([k, v]) => [k, relativeToPackage(v)])
);

/** @type {import('next').NextConfig} */
module.exports = {
  reactStrictMode: true,
  experimental: {
    // Allow importing files that live outside this package directory (../../sdk/react/src).
    // webpack only; Turbopack uses `turbopack.root` below for the same purpose.
    externalDir: true,
  },
  turbopack: {
    // Turbopack resolves nothing above its root, and the SDK source is two
    // directories up. Pinning it also stops Turbopack inferring a root from
    // the nearest lockfile it can find -- on this machine that was $HOME,
    // which it warned about and ignored.
    root: REPO_ROOT,
    resolveAlias: RESOLVE_ALIAS_TURBOPACK,
  },
  webpack: (config) => {
    config.resolve.alias = { ...config.resolve.alias, ...ALIAS_TARGETS };
    return config;
  },
};
