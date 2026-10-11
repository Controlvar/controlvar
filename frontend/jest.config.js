const path = require('path');
const { MODULES_DIR, MODULES_STUB_DIR, modulesTreeAvailable } = require('./modules-alias');

// The modules seam: `./modules-alias.js` owns the rule, this file applies it
// to jest. The mapper value is an array — jest tries each entry and uses the
// first that resolves — so a full-profile tree gets `modules/frontend/src/x`
// and falls back to the stub for anything it does not carry, while a core run
// (`modules/` deleted, or EXPERIMENTLY_PROFILE=core) can only ever reach the
// stub.
//
// The module tests live beside the modules, outside this package, so `roots`
// gains that directory only when the profile uses it: a full run collects
// `modules/frontend/src/**/*.test.ts(x)`, a core run never looks there (and
// jest would refuse a root that does not exist, which is exactly the case in
// a built core tree).
const full = modulesTreeAvailable();
const moduleTargets = full
  ? [path.join(MODULES_DIR, '$1'), path.join(MODULES_STUB_DIR, '$1')]
  : [path.join(MODULES_STUB_DIR, '$1')];

module.exports = {
  testEnvironment: 'jsdom',
  // Pins the process zone to America/Los_Angeles; see the file for why.
  globalSetup: '<rootDir>/jest.global-setup.js',
  setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
  roots: full ? ['<rootDir>', MODULES_DIR] : ['<rootDir>'],
  // Searched after the normal node_modules walk-up (like NODE_PATH), so the
  // modules tree — outside this package — finds `react` here and core files
  // resolve exactly as before.
  modulePaths: ['<rootDir>/node_modules'],
  transform: {
    '^.+\\.(ts|tsx)$': ['ts-jest', {
      tsconfig: {
        jsx: 'react-jsx',
        module: 'commonjs',
      },
    }],
  },
  moduleNameMapper: {
    // `@modules/…` first: it is the more specific pattern of the two.
    '^@modules/(.*)$': moduleTargets,
    '^@/(.*)$': '<rootDir>/src/$1',
    '\\.(css|less|scss|png|jpg|jpeg|gif|svg)$': 'identity-obj-proxy',
    '^recharts$': '<rootDir>/src/__mocks__/recharts.tsx',
  },
  testMatch: ['**/*.test.ts', '**/*.test.tsx'],
  testPathIgnorePatterns: [
    '/node_modules/',
    '/.next/',
    '/out/',
    // Belt and braces for a core run on an undeleted tree: the modules root
    // is not listed above, and its tests are ignored even if something else
    // reaches them.
    ...(full ? [] : [`^${MODULES_DIR.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/`]),
  ],
  // Known limit: the modules tree is not in the coverage report. Its tests
  // run, but babel-plugin-istanbul (test-exclude) refuses to instrument any
  // file outside jest's rootDir, and `../modules/...` globs here cannot
  // change that. Reporting it needs a second jest project rooted at
  // MODULES_DIR with its own ts-jest tsconfig; not worth it while coverage is
  // informational.
  collectCoverageFrom: [
    'src/components/**/*.{ts,tsx}',
    'src/services/**/*.{ts,tsx}',
    'src/hooks/**/*.{ts,tsx}',
    'src/contexts/**/*.{ts,tsx}',
    '!src/**/*.d.ts',
  ],
};
