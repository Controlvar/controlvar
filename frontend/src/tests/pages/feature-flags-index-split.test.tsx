/**
 * The flag list's "Split" badge (#2012): a string, number or json flag with a
 * split in the environment on screen says so in text, beside its type. A flag
 * without one, and a boolean flag, has none. Production reads the listed
 * flag's own split; Staging and Development their settings there, from the
 * batch route, never Production's.
 */
import React from 'react';
import { screen, within } from '@testing-library/react';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import { EnvironmentSwitcher } from '@/components/feature-flags/EnvironmentSwitcher';
import { FlagEnvironmentProvider } from '@/contexts/FlagEnvironmentContext';
import { apiFetch } from '@/services/api';
import { FeatureFlagListItem, FlagEnvironmentSettings } from '@/services/featureFlags';
import { SPLIT_BADGE } from '@/utils/flagSplit';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

let mockRouter = makeRouter({ pathname: '/feature-flags', asPath: '/feature-flags' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const SPLIT = [
  { value: 'red', weight: 34 },
  { value: 'green', weight: 33 },
  { value: 'blue', weight: 33 },
];

function item(id: string, key: string, overrides: Partial<FeatureFlagListItem> = {}): FeatureFlagListItem {
  return {
    id,
    key,
    name: key,
    is_active: true,
    status: 'active',
    rollout_percentage: 100,
    targeting_rules: null,
    value_type: 'string',
    on_value: 'A',
    off_value: 'Z',
    split: null,
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
    ...overrides,
  };
}

function setting(environment: string, overrides: Partial<FlagEnvironmentSettings> = {}): FlagEnvironmentSettings {
  return {
    environment,
    status: 'active',
    is_active: true,
    rollout_percentage: 100,
    targeting_rules: null,
    split: null,
    configured: true,
    updated_at: '2026-11-20T00:00:00Z',
    ...overrides,
  };
}

// f1: a split in Production only; f2: in Staging only; f3: a boolean flag
// (whose split the API always reads as null; a stray one is not shown).
const FLAGS = [
  item('f1', 'checkout_copy', { split: SPLIT }),
  item('f2', 'timeout_tier', { value_type: 'number', on_value: 1, off_value: 0 }),
  item('f3', 'dark_mode', { value_type: 'boolean', on_value: true, off_value: false, split: SPLIT }),
];
const STAGING: Record<string, FlagEnvironmentSettings> = {
  f1: setting('staging'),
  f2: setting('staging', { split: [{ value: 1, weight: 50 }, { value: 2, weight: 50 }, { value: 3, weight: 0 }] }),
  f3: setting('staging', { split: SPLIT }),
};

function install() {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/feature-flags', handler: () => ({ items: FLAGS, total: FLAGS.length, skip: 0, limit: 100 }) },
      {
        path: /^\/api\/v1\/environments\/(staging|development)\/feature-flag-settings$/,
        handler: (p, opts) => {
          const env = p.split('/')[4];
          const ids = (opts.query?.flag_id as string[]) ?? [];
          const own = env === 'staging' ? STAGING : {};
          return { environment: env, settings: Object.fromEntries(ids.map((id) => [id, own[id] ?? setting(env)])) };
        },
      },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderList(env?: string) {
  mockRouter = makeRouter({
    pathname: '/feature-flags',
    asPath: env ? `/feature-flags?env=${env}` : '/feature-flags',
    query: env ? { env } : {},
  });
  renderInAppRoot(
    <FlagEnvironmentProvider>
      <EnvironmentSwitcher />
      <FeatureFlagsPage />
    </FlagEnvironmentProvider>,
  );
  await screen.findByTestId('flags-table');
}

const row = (key: string) =>
  screen.getAllByTestId('flag-row').find((r) => r.getAttribute('data-flag-key') === key) as HTMLElement;
const badge = (key: string) => within(row(key)).queryByTestId('flag-split-badge');

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u1@example.com', username: 'u1', role: 'VIEWER', is_superuser: false },
  });
  localStorage.clear();
  install();
});

it('Production: "Split" in text for the flag with a split there, none without, none on a boolean flag', async () => {
  await renderList('production');
  expect(badge('checkout_copy')).toHaveTextContent(SPLIT_BADGE);
  expect(badge('checkout_copy')?.textContent).toBe('Split');
  expect(within(row('checkout_copy')).getByTestId('flag-value-type')).toHaveTextContent('StringSplit');
  expect(badge('timeout_tier')).toBeNull();
  expect(badge('dark_mode')).toBeNull();
});

it("Staging: the badge follows Staging's own split, not Production's", async () => {
  await renderList('staging');
  await screen.findAllByTestId('flag-split-badge');
  expect(badge('checkout_copy')).toBeNull();
  expect(badge('timeout_tier')).toHaveTextContent('Split');
  expect(badge('dark_mode')).toBeNull();
});

it('Development: no split there, no badge', async () => {
  await renderList('development');
  expect(screen.queryAllByTestId('flag-split-badge')).toEqual([]);
});
