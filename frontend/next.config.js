/** @type {import('next').NextConfig} */
const path = require('path');
const {
  REPO_ROOT,
  modulesAliasTargets,
  modulesAliasTurbopack,
  modulesTreeAvailable,
} = require('./modules-alias');

// The modules seam. `./modules-alias.js` owns the rule; this file applies it to
// both bundlers — Turbopack, which `next build` uses by default from Next 16,
// and webpack, which `next build --webpack` still uses. Next also derives
// aliases from `tsconfig.json`'s `paths` via its own resolve plugin — if that
// kept mapping `@modules/*` at `../modules/frontend/src/*` it would quietly win
// back the real module in a core build, the exact silent failure to avoid — so
// a core build is also pointed at `tsconfig.core.json`, whose `paths` list the
// stub tree alone.
const full = modulesTreeAvailable();

module.exports = {
  reactStrictMode: true,
  output: 'export',
  images: {
    unoptimized: true,
  },
  // Always defined, "marketing" or "platform". An unset NEXT_PUBLIC_ variable is
  // not replaced at build time, so `process.env.NEXT_PUBLIC_SITE_MODE ===
  // 'marketing'` stayed a runtime test in the platform build and the bundler
  // could not drop what it guards (components/Wordmark.tsx: the logo's path
  // data, which only the public site ships). Any value but "marketing" means
  // platform, as in utils/site-mode.ts.
  env: {
    NEXT_PUBLIC_SITE_MODE: process.env.NEXT_PUBLIC_SITE_MODE === 'marketing' ? 'marketing' : 'platform',
  },
  typescript: {
    tsconfigPath: full ? 'tsconfig.json' : 'tsconfig.core.json',
  },
  experimental: {
    // The modules tree is `../modules/frontend/src`, outside this package.
    // Without this, Next's SWC loader only compiles sources inside the project
    // directory and a `@modules/*` import of a real module fails with "Module
    // parse failed: Unexpected token". (webpack only; Turbopack uses
    // `turbopack.root` below for the same purpose.)
    externalDir: true,
  },
  turbopack: {
    // Turbopack resolves nothing above its root, and the modules tree is a
    // sibling of this package. Pinning the root to the repository also stops
    // Turbopack inferring one from the nearest lockfile it can find — on a
    // developer machine that was $HOME, which it then warned about and
    // ignored.
    root: REPO_ROOT,
    resolveAlias: modulesAliasTurbopack(),
  },
  // `next build --webpack`. Kept in step with the Turbopack alias above by
  // src/tests/modules-alias.test.ts, which asserts both spell the same rule.
  webpack: (config) => {
    config.resolve = config.resolve || {};
    config.resolve.alias = {
      ...(config.resolve.alias || {}),
      // Array alias: webpack tries each directory in order and uses the first
      // that contains the requested module.
      '@modules': modulesAliasTargets(),
    };
    // The modules tree is outside this package, so the node_modules walk-up
    // from `../modules/frontend/src/...` finds nothing. Appended, not
    // prepended: the walk-up still wins wherever it finds something, so nested
    // package versions inside node_modules resolve exactly as before.
    config.resolve.modules = [
      ...(config.resolve.modules || ['node_modules']),
      path.resolve(__dirname, 'node_modules'),
    ];

    // The profile is external state that webpack cannot see, so it must be
    // part of the cache key. Without this, `next build` after a build of the
    // other profile reuses the cached resolution and silently ships the wrong
    // modules — observed: a full build emitting the 1.1 kB stub pages from a
    // previous core build. Changing `cache.version` invalidates the filesystem
    // cache, so the two profiles can never share entries.
    if (config.cache && typeof config.cache === 'object') {
      const version = config.cache.version ? `${config.cache.version}|` : '';
      config.cache = {
        ...config.cache,
        version: `${version}profile=${full ? 'full' : 'core'}`,
      };
    }

    return config;
  },
  // Security headers are applied at the serving layer (nginx/edge/CDN) for
  // static export deployments.
};
