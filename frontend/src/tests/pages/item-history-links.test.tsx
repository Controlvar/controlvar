/**
 * "History" on a flag's page, an experiment's page and a Users row (#2273).
 *
 * The link goes to the Audit Log page filtered to that item or person, and
 * every role sees it: the page it opens narrows the list to the caller's own
 * entries where the audit API narrows. The pages must not call the routes
 * that refuse some roles (`/audit-logs/entity|stats|stream|user`) or the
 * flag history route; the Audit Log page's own list is the only reader.
 *
 * Every case renders the page module with the signed-in person resolved.
 */
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import { UserTable } from '@/components/admin/users/UserTable';
import { AdminService } from '@/services/admin';
import { apiFetch } from '@/services/api';
import { AdminUser } from '@/types/admin';
import { Experiment } from '@/types/experiments';
import { FeatureFlag } from '@/services/featureFlags';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

let mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({
  useAuth: () => mockAuth(),
  useOptionalAuth: () => mockAuth(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const FLAG_ID = '3f2a9c1e-5b7d-4e8a-9c3f-1a2b3c4d5e6f';
const EXPERIMENT_ID = '7c0e4b1a-2d3f-4a5b-8c6d-9e0f1a2b3c4d';

const PEOPLE = [
  ['superuser VIEWER', 'VIEWER', true],
  ['ADMIN', 'ADMIN', false],
  ['DEVELOPER', 'DEVELOPER', false],
  ['ANALYST', 'ANALYST', false],
  ['VIEWER', 'VIEWER', false],
] as const;

function signIn(role: string, isSuperuser: boolean) {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u@example.com', username: 'u', role, is_superuser: isSuperuser },
  });
}

const FLAG: FeatureFlag = {
  id: FLAG_ID,
  key: 'checkout_v2',
  name: 'Checkout v2',
  description: null,
  status: 'inactive',
  rollout_percentage: 25,
  rules: { logical_operator: 'AND', groups: [] },
  owner_id: 'user-1',
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-05T10:00:00Z',
} as unknown as FeatureFlag;

const EXPERIMENT: Experiment = {
  id: EXPERIMENT_ID,
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'draft',
  targeting_rules: null,
  tags: null,
  owner_id: 'someone-else',
  start_date: null,
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [
    { id: 'v-1', name: 'Control', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'Blue CTA', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [{ id: 'm-1', name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true }],
};

/** Paths of every request the page made. */
const requested = () => mockedApiFetch.mock.calls.map(([path]) => String(path).split('?')[0]);

/** What no first-tier page may read: routes some roles are refused, and the flag history route. */
const FORBIDDEN = /\/audit-logs\/(entity|stats|stream|user)|\/history(\/|$)/;

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe('a flag page links to its history', () => {
  beforeEach(() => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: FLAG_ID } });
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: `/api/v1/feature-flags/${FLAG_ID}`, handler: () => FLAG },
        { path: '/api/v1/rollout-schedules', handler: () => ({ items: [], total: 0, skip: 0, limit: 50 }) },
      ]) as unknown as typeof apiFetch,
    );
  });

  it.each(PEOPLE)('%s sees History, which opens the Audit Log filtered to this flag', async (_n, role, su) => {
    signIn(role, su);
    render(<FeatureFlagDetailPage />);
    const link = await screen.findByTestId('item-history-link');
    expect(link).toHaveTextContent('History');
    expect(link).toHaveAccessibleName('History of Checkout v2');
    expect(link).toHaveAttribute('href', `/admin/audit?entity_type=feature_flag&entity_id=${FLAG_ID}`);
    expect(screen.getAllByTestId('item-history-link')).toHaveLength(1);
    await waitFor(() => expect(requested().length).toBeGreaterThan(0));
    expect(requested().filter((p) => FORBIDDEN.test(p))).toEqual([]);
  });

  it('shows no History link while the flag is still loading', async () => {
    signIn('ADMIN', false);
    mockedApiFetch.mockImplementation(() => new Promise(() => undefined));
    render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-loading');
    expect(screen.queryByTestId('item-history-link')).toBeNull();
  });
});

describe('an experiment page links to its history', () => {
  beforeEach(() => {
    mockRouter = makeRouter({ pathname: '/experiments/[id]', query: { id: EXPERIMENT_ID } });
    mockedApiFetch.mockImplementation(
      routedApi([{ path: `/api/v1/experiments/${EXPERIMENT_ID}`, handler: () => EXPERIMENT }]) as unknown as typeof apiFetch,
    );
  });

  it.each(PEOPLE)('%s sees History, which opens the Audit Log filtered to this experiment', async (_n, role, su) => {
    signIn(role, su);
    render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
    const link = screen.getByTestId('item-history-link');
    expect(link).toHaveTextContent('History');
    expect(link).toHaveAccessibleName('History of Checkout button colour');
    expect(link).toHaveAttribute('href', `/admin/audit?entity_type=experiment&entity_id=${EXPERIMENT_ID}`);
    expect(screen.getAllByTestId('item-history-link')).toHaveLength(1);
    expect(requested().filter((p) => FORBIDDEN.test(p))).toEqual([]);
  });
});

describe('each Users row links to what that person changed', () => {
  const PERSON = '9b1d6c2a-7e4f-4a3b-8d5c-0e1f2a3b4c5d';
  const user = (id: string, username: string): AdminUser => ({
    id,
    username,
    email: `${username}@example.com`,
    role: 'DEVELOPER',
    is_active: true,
    created_at: '2024-01-01T00:00:00Z',
  });

  it('puts one History link on every row, naming the person', async () => {
    const list = jest.spyOn(AdminService, 'listUsers').mockResolvedValue({
      items: [user(PERSON, 'alice'), user('1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9', 'bob')],
      total: 2,
      skip: 0,
      limit: 50,
    });
    render(<UserTable />);
    const links = await screen.findAllByTestId('item-history-link');
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAccessibleName('History of changes made by alice');
    expect(links[0]).toHaveAttribute('href', `/admin/audit?user_id=${PERSON}`);
    expect(links[1]).toHaveAccessibleName('History of changes made by bob');
    expect(links[1]).toHaveAttribute('href', '/admin/audit?user_id=1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9');
    list.mockRestore();
  });
});
