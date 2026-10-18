/**
 * src/lib/env.ts reads each setting under its name, then under the name it had
 * before the project was renamed, then defaults. Three distinct values per
 * setting, so a reader that ignores a name cannot pass by landing on another.
 *
 * Jest reads process.env at run time; that the build inlines both literal reads
 * is held by the source pin in backend/tests/unit/scripts/test_demo_apps_catalogues.py.
 */

const NAMES = {
  API_URL: ['NEXT_PUBLIC_CONTROLVAR_API_URL', 'NEXT_PUBLIC_EXPERIMENTLY_API_URL', 'http://localhost:8000'],
  API_KEY: ['NEXT_PUBLIC_CONTROLVAR_API_KEY', 'NEXT_PUBLIC_EXPERIMENTLY_API_KEY', ''],
  DASHBOARD_URL: ['NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL', 'NEXT_PUBLIC_EXPERIMENTLY_DASHBOARD_URL', 'http://localhost:3100'],
} as const;

type Setting = keyof typeof NAMES;
const NEW_VALUE: Record<Setting, string> = {
  API_URL: 'http://127.0.0.1:9',
  API_KEY: 'new-key',
  DASHBOARD_URL: 'http://127.0.0.1:9',
};
const OLD_VALUE: Record<Setting, string> = {
  API_URL: 'http://127.0.0.1:7',
  API_KEY: 'old-key',
  DASHBOARD_URL: 'http://127.0.0.1:7',
};

const saved = { ...process.env };

function load(env: Record<string, string>): Record<Setting, string> {
  for (const [name, old] of Object.values(NAMES)) {
    delete process.env[name];
    delete process.env[old];
  }
  Object.assign(process.env, env);
  let mod: Record<Setting, string> = {} as Record<Setting, string>;
  jest.isolateModules(() => {
    mod = require('@/lib/env');
  });
  return mod;
}

afterEach(() => {
  process.env = { ...saved };
});

describe.each(Object.keys(NAMES) as Setting[])('%s', (setting) => {
  const [name, old, fallback] = NAMES[setting];

  it('reads the new name', () => {
    expect(load({ [name]: NEW_VALUE[setting] })[setting]).toBe(NEW_VALUE[setting]);
  });

  it('reads the name before the rename when the new one is not set', () => {
    expect(load({ [old]: OLD_VALUE[setting] })[setting]).toBe(OLD_VALUE[setting]);
  });

  it('prefers the new name when both are set', () => {
    const value = load({ [name]: NEW_VALUE[setting], [old]: OLD_VALUE[setting] })[setting];
    expect(value).toBe(NEW_VALUE[setting]);
    expect(value).not.toBe(OLD_VALUE[setting]);
  });

  it('defaults when neither is set', () => {
    expect(load({})[setting]).toBe(fallback);
  });
});
