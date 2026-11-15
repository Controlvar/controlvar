/**
 * The demo note with demo mode off and on: on a read-only demo every page
 * that tells a viewer what their role cannot do says instead that creating
 * and changing things is turned off in this demo, and links the walkthroughs.
 * The account pages show the note in place of their forms, and a segment
 * offers neither "Check a user" nor "Estimate size" (reads the API takes as
 * POSTs, which a demo refuses).
 *
 * The signed-in user is a VIEWER, the demo's account. Demo mode comes from
 * `useDemoMode`; that it follows `/auth/config` is covered by the banner and
 * sign-in tests.
 */
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import ExperimentsPage from '@/pages/experiments/index';
import NewExperimentPage from '@/pages/experiments/new';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import NewFeatureFlagPage from '@/pages/feature-flags/new';
import SegmentsPage from '@/pages/segments/index';
import NewSegmentPage from '@/pages/segments/new';
import SegmentDetailPage from '@/pages/segments/[id]';
import ChangePasswordPage from '@/pages/account/password';
import OwnApiKeysPage from '@/pages/account/api-keys';
import { apiFetch } from '@/services/api';
import type { DemoMode } from '@/services/authConfig';
import { ROLE_NOTE } from '@/utils/segmentErrors';
import { FLAG_ROLE_NOTE } from '@/utils/experimentPermissions';
import { ROLE_CANNOT_CREATE } from '@/components/experiments/new/createErrors';
import {
  DEMO_EXPERIMENTS_ACTION,
  DEMO_FLAGS_ACTION,
  DEMO_RUN_YOUR_OWN_URL,
  DEMO_SEGMENTS_ACTION,
  DEMO_WALKTHROUGHS_URL,
} from '@/utils/demoMode';
import { makeRouter, routedApi } from './helpers/apiMock';
import { BOTH_STATES, DemoState, TEST_DEMO_ACCOUNT } from '../helpers/demoMode';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const SEG = '3f2b9c1e-8d4a-4c6b-9e2f-1a2b3c4d5e6f';
let mockRouter = makeRouter();
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const viewer = {
  id: 'u-viewer',
  email: 'viewer@example.test',
  username: 'viewer',
  full_name: null,
  role: 'VIEWER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};
jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => ({ status: 'authenticated', user: viewer }),
  useAuth: () => ({ status: 'authenticated', user: viewer }),
}));

let mockDemo: DemoMode | null = null;
jest.mock('@/contexts/AuthConfigContext', () => ({
  ...jest.requireActual('@/contexts/AuthConfigContext'),
  useDemoMode: () => mockDemo,
}));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mocked = apiFetch as jest.MockedFunction<typeof apiFetch>;

const experiment = {
  id: 'exp-1',
  name: 'Checkout CTA',
  key: 'checkout-cta',
  experiment_type: 'a_b',
  status: 'active',
  targeting_rules: null,
  owner_id: null,
  start_date: null,
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [],
  metrics: [],
};

const flag = {
  id: 'flag-1',
  key: 'checkout_v2',
  name: 'Checkout v2',
  description: null,
  is_active: true,
  rollout_percentage: 25,
  targeting_rules: null,
  owner_id: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-05T10:00:00Z',
};

function segment(kind: 'id_list' | 'rules') {
  return {
    id: SEG,
    name: kind === 'rules' ? 'Pro plan' : 'Enterprise pilot',
    description: null,
    kind,
    rules:
      kind === 'rules'
        ? { logical_operator: 'AND', groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'plan', operator: 'equals', value: 'pro' }] }] }
        : null,
    status: 'active',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-02T00:00:00Z',
    member_count: kind === 'rules' ? null : 10,
  };
}

const flagDetail = {
  ...flag,
  status: 'active',
  rules: { logical_operator: 'AND', groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'country', operator: 'equals', value: 'US' }] }] },
};

function serve(segmentKind: 'id_list' | 'rules' = 'id_list') {
  mocked.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments', handler: () => ({ items: [experiment], total: 1, skip: 0, limit: 100 }) },
      { path: '/api/v1/experiments/exp-1', handler: () => ({ ...experiment, status: 'draft' }) },
      { path: '/api/v1/feature-flags/flag-1', handler: () => flagDetail },
      { path: '/api/v1/rollout-schedules', handler: () => ({ items: [], total: 0, skip: 0, limit: 50 }) },
      {
        path: '/api/v1/safety/feature-flags/flag-1/check',
        handler: () => ({ feature_flag_id: 'flag-1', is_healthy: true, metrics: [], last_checked: '2026-09-11T08:00:00Z' }),
      },
      { path: '/api/v1/feature-flags', handler: () => ({ items: [flag], total: 1, skip: 0, limit: 100 }) },
      { path: '/api/v1/segments', handler: () => [segment(segmentKind)] },
      { path: `/api/v1/segments/${SEG}`, handler: () => segment(segmentKind) },
      { path: `/api/v1/segments/${SEG}/experiments`, handler: () => ({ segment_id: SEG, experiments: [], feature_flags: [] }) },
      { path: '/api/v1/api-keys', handler: () => [] },
    ]) as unknown as typeof apiFetch,
  );
}

function setState(state: DemoState) {
  mockDemo = state === 'on' ? { account: TEST_DEMO_ACCOUNT } : null;
}

/** The demo note, checked for its sentence and both links. */
function expectDemoNote(action: string) {
  const note = screen.getByTestId('demo-note');
  expect(note).toHaveTextContent(`${action} is turned off in this read-only demo.`);
  expect(within(note).getByRole('link', { name: 'walkthroughs' })).toHaveAttribute('href', DEMO_WALKTHROUGHS_URL);
  expect(within(note).getByRole('link', { name: 'run your own copy' })).toHaveAttribute('href', DEMO_RUN_YOUR_OWN_URL);
  return note;
}

beforeEach(() => {
  mocked.mockReset();
  mockRouter = makeRouter();
  serve();
});

describe.each(BOTH_STATES)('with demo mode %s', (state: DemoState) => {
  const on = state === 'on';
  beforeEach(() => setState(state));

  it(on ? 'the experiments list puts the demo note where "+ New Experiment" would be' : 'the experiments list has no demo note', async () => {
    mockRouter = makeRouter({ pathname: '/experiments', asPath: '/experiments' });
    render(<ExperimentsPage />);
    await screen.findByText('Checkout CTA');
    expect(screen.queryByTestId('new-experiment-btn')).not.toBeInTheDocument();
    if (on) expectDemoNote(DEMO_EXPERIMENTS_ACTION);
    else expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
  });

  it(on ? '/experiments/new shows the demo note, not the role note' : '/experiments/new shows the role note', async () => {
    mockRouter = makeRouter({ pathname: '/experiments/new', asPath: '/experiments/new' });
    render(<NewExperimentPage />);
    const notice = await screen.findByTestId('create-not-allowed');
    if (on) {
      expectDemoNote(DEMO_EXPERIMENTS_ACTION);
      expect(notice).not.toHaveTextContent(ROLE_CANNOT_CREATE);
    } else {
      expect(notice).toHaveTextContent(ROLE_CANNOT_CREATE);
      expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
    }
  });

  it(on ? 'an experiment shows the demo note, not the role note' : 'an experiment shows the role note', async () => {
    mockRouter = makeRouter({ pathname: '/experiments/[id]', asPath: '/experiments/exp-1', query: { id: 'exp-1' } });
    render(<ExperimentDetailPage />);
    await screen.findByRole('heading', { level: 1, name: 'Checkout CTA' });
    if (on) {
      expectDemoNote(DEMO_EXPERIMENTS_ACTION);
      expect(screen.queryByTestId('experiment-role-note')).not.toBeInTheDocument();
    } else {
      expect(screen.getByTestId('experiment-role-note')).toHaveTextContent(/needs permission to change experiments/);
      expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
    }
  });

  it(on ? 'a flag shows the demo note, not the role note' : 'a flag shows the role note', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/[id]', asPath: '/feature-flags/flag-1', query: { id: 'flag-1' } });
    render(<FeatureFlagDetailPage />);
    await screen.findByRole('heading', { level: 1, name: 'Checkout v2' });
    if (on) {
      expectDemoNote(DEMO_FLAGS_ACTION);
      expect(screen.queryByTestId('flag-role-note')).not.toBeInTheDocument();
    } else {
      expect(screen.getByTestId('flag-role-note')).toHaveTextContent(FLAG_ROLE_NOTE);
      expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
    }
  });

  it(on ? 'the flags list shows the demo note, not the role note' : 'the flags list shows the role note', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags', asPath: '/feature-flags' });
    render(<FeatureFlagsPage />);
    await screen.findByText('Checkout v2');
    if (on) {
      expectDemoNote(DEMO_FLAGS_ACTION);
      expect(screen.queryByTestId('flags-role-note')).not.toBeInTheDocument();
      expect(screen.queryByText(FLAG_ROLE_NOTE)).not.toBeInTheDocument();
    } else {
      expect(screen.getByTestId('flags-role-note')).toHaveTextContent(FLAG_ROLE_NOTE);
      expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
    }
  });

  it(on ? '/feature-flags/new shows the demo note' : '/feature-flags/new shows the role note', async () => {
    mockRouter = makeRouter({ pathname: '/feature-flags/new', asPath: '/feature-flags/new' });
    render(<NewFeatureFlagPage />);
    if (on) {
      expectDemoNote(DEMO_FLAGS_ACTION);
      expect(screen.queryByTestId('flag-new-role-note')).not.toBeInTheDocument();
    } else {
      expect(await screen.findByTestId('flag-new-role-note')).toHaveTextContent(FLAG_ROLE_NOTE);
      expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
    }
  });

  it(on ? 'the segments list shows the demo note, not the role note' : 'the segments list shows the role note', async () => {
    mockRouter = makeRouter({ pathname: '/segments', asPath: '/segments' });
    render(<SegmentsPage />);
    await screen.findByText('Enterprise pilot');
    if (on) {
      expectDemoNote(DEMO_SEGMENTS_ACTION);
      expect(screen.queryByTestId('segments-role-note')).not.toBeInTheDocument();
    } else {
      expect(screen.getByTestId('segments-role-note')).toHaveTextContent(ROLE_NOTE);
      expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
    }
  });

  it(on ? '/segments/new shows the demo note' : '/segments/new shows the role note', async () => {
    mockRouter = makeRouter({ pathname: '/segments/new', asPath: '/segments/new' });
    render(<NewSegmentPage />);
    if (on) {
      expectDemoNote(DEMO_SEGMENTS_ACTION);
      expect(screen.queryByTestId('segment-new-role-note')).not.toBeInTheDocument();
    } else {
      expect(await screen.findByTestId('segment-new-role-note')).toHaveTextContent(ROLE_NOTE);
      expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
    }
  });

  it(
    on
      ? 'an ID-list segment shows the demo note and no "Check a user"'
      : 'an ID-list segment shows the role note and "Check a user"',
    async () => {
      mockRouter = makeRouter({ pathname: '/segments/[id]', asPath: `/segments/${SEG}`, query: { id: SEG } });
      render(<SegmentDetailPage />);
      await screen.findByTestId('segment-members-section');
      if (on) {
        expectDemoNote(DEMO_SEGMENTS_ACTION);
        expect(screen.queryByTestId('segment-role-note')).not.toBeInTheDocument();
        expect(screen.queryByTestId('segment-check-user')).not.toBeInTheDocument();
        expect(screen.queryByText('Check a user')).not.toBeInTheDocument();
      } else {
        expect(screen.getByTestId('segment-role-note')).toHaveTextContent(ROLE_NOTE);
        expect(screen.getByTestId('segment-check-user')).toBeInTheDocument();
      }
    },
  );

  it(on ? 'a rules segment offers no "Estimate size"' : 'a rules segment offers "Estimate size"', async () => {
    serve('rules');
    mockRouter = makeRouter({ pathname: '/segments/[id]', asPath: `/segments/${SEG}`, query: { id: SEG } });
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-rules-section');
    if (on) expect(screen.queryByTestId('segment-preview')).not.toBeInTheDocument();
    else expect(screen.getByTestId('segment-preview')).toHaveTextContent('Estimate size');
  });

  it(on ? '/account/password shows the demo note and no form' : '/account/password shows the form', () => {
    mockRouter = makeRouter({ pathname: '/account/password', asPath: '/account/password' });
    const { container } = render(<ChangePasswordPage />);
    if (on) {
      const note = screen.getByTestId('demo-note');
      expect(note).toHaveTextContent('Changing your password is turned off in this read-only demo.');
      expect(within(note).getByRole('link', { name: 'run your own copy' })).toHaveAttribute('href', DEMO_RUN_YOUR_OWN_URL);
      expect(container.querySelector('input')).toBeNull();
      expect(screen.queryByTestId('change-password-submit')).not.toBeInTheDocument();
    } else {
      expect(container.querySelectorAll('input[type="password"]')).toHaveLength(3);
      expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
    }
  });

  it(on ? '/account/api-keys shows the demo note and asks for no keys' : '/account/api-keys lists your keys', async () => {
    mockRouter = makeRouter({ pathname: '/account/api-keys', asPath: '/account/api-keys' });
    render(<OwnApiKeysPage />);
    if (on) {
      expect(screen.getByTestId('demo-note')).toHaveTextContent(
        'Creating and changing API keys is turned off in this read-only demo.',
      );
      expect(screen.queryByTestId('own-api-keys-page')).not.toBeInTheDocument();
      expect(mocked.mock.calls.filter(([p]) => String(p).startsWith('/api/v1/api-keys'))).toEqual([]);
    } else {
      expect(await screen.findByTestId('own-api-keys-page')).toBeInTheDocument();
      await waitFor(() =>
        expect(mocked.mock.calls.filter(([p]) => String(p).startsWith('/api/v1/api-keys')).length).toBeGreaterThan(0),
      );
      expect(screen.queryByTestId('demo-note')).not.toBeInTheDocument();
    }
  });
});
