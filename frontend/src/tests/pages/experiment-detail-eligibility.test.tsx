/**
 * The experiment page's "Exclusion group" and "Global holdout" rows, and the
 * note under the header that links to where they are set (#1449, plan-1449
 * #4, #5, #7, #9, #11, #16).
 *
 * `GET /api/v1/mutual-exclusion-groups/{id}` and `GET /api/v1/holdout` both
 * check EXPERIMENT CREATE: ADMIN, DEVELOPER and superusers read them, ANALYST
 * and VIEWER are refused. So the page asks only for the first three, and the
 * last two send no request and read the refusal from the role. Every user here
 * is RESOLVED and, but for one cell, not a superuser: a null user would read as
 * "may read", and a superuser passes every check.
 */
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { ApiError, apiFetch } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { Experiment } from '@/types/experiments';
import { Route, apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments/[id]', query: { id: 'exp-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockUseAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const GROUP_PATH = '/api/v1/mutual-exclusion-groups/grp-1';
const HOLDOUT_PATH = '/api/v1/holdout';

const EXPERIMENT: Experiment = {
  id: 'exp-1',
  name: 'Checkout copy',
  key: 'checkout-copy',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'active',
  targeting_rules: null,
  owner_id: 'someone-else',
  start_date: '2026-09-01T10:00:00Z',
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [
    { id: 'v-1', name: 'control', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'treatment', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [],
  mutual_exclusion_group_id: 'grp-1',
};

/**
 * The API's whole answers. The page shows the group's name and status and the
 * holdout's name and percentage; every other field carries PLANTED, so a page
 * that showed one fails the "no PLANTED" checks.
 */
const GROUP = {
  id: 'grp-1',
  name: 'Checkout tests',
  description: 'PLANTED-DESCRIPTION',
  traffic_allocation: 1.0,
  status: 'active',
  owner_id: 'PLANTED-OWNER',
  experiments: [{ id: 'exp-9', name: 'PLANTED-EXPERIMENT', key: 'planted', status: 'active' }],
  created_at: '2026-09-01T10:00:00',
  updated_at: '2026-09-01T10:00:00',
};
const HOLDOUT = {
  id: 'PLANTED-HOLDOUT-ID',
  name: 'Q4 baseline',
  description: 'PLANTED-HOLDOUT-DESCRIPTION',
  holdout_percentage: 5,
  is_active: true,
  owner_id: 'PLANTED-OWNER',
  activated_at: '2026-09-01T10:00:00',
  deactivated_at: null,
  created_at: '2026-09-01T10:00:00',
  updated_at: '2026-09-01T10:00:00',
};

const HOLDOUT_SENTENCE = 'Q4 baseline: keeps 5% of new users out of every experiment';
const GROUP_REFUSED = 'In a group. Seeing its name requires the ADMIN or DEVELOPER role.';
const GROUP_ERROR = 'In a group. Its name could not be loaded; reload the page to try again.';
const HOLDOUT_REFUSED = 'Seeing this requires the ADMIN or DEVELOPER role.';
const HOLDOUT_ERROR = 'Could not be loaded; reload the page to try again.';

/** How a mocked route answers: a value, a promise, or a throw. */
type Reply = () => unknown;
/** An answer that never comes: the row stays loading. */
const pending: Reply = () => new Promise<never>(() => undefined);

function serve(
  experiment: Partial<Experiment> = {},
  { group = () => GROUP, holdout = () => HOLDOUT }: { group?: Reply; holdout?: Reply } = {},
) {
  const current = { ...EXPERIMENT, ...experiment };
  const routes: Route[] = [
    { path: '/api/v1/experiments/exp-1', handler: () => current },
    { path: GROUP_PATH, handler: group },
    { path: HOLDOUT_PATH, handler: holdout },
  ];
  mockedApiFetch.mockImplementation(routedApi(routes) as unknown as typeof apiFetch);
}

/** A resolved user. `role` alone is never a superuser. */
function userFor(role: string, superuser = false) {
  return {
    id: 'user-1',
    email: `${role.toLowerCase()}@demo.com`,
    username: role.toLowerCase(),
    role,
    is_superuser: superuser,
  };
}

function signIn(role: string, superuser = false) {
  mockUseAuth.mockReturnValue({ user: userFor(role, superuser), status: 'authenticated' });
}

/** The calls to either read, as `[path, ...rest]`, in path order. */
const eligibilityCalls = () =>
  mockedApiFetch.mock.calls
    .filter(([path]) => /^\/api\/v1\/(holdout|mutual-exclusion-groups)/.test(String(path)))
    .sort(([a], [b]) => String(a).localeCompare(String(b)));

async function open() {
  render(<ExperimentDetailPage />);
  return screen.findByTestId('experiment-detail');
}

const ADMITTED: [string, string, boolean][] = [
  ['ADMIN', 'ADMIN', false],
  ['DEVELOPER', 'DEVELOPER', false],
  ['a superuser VIEWER', 'VIEWER', true],
];
const REFUSED_ROLES = ['ANALYST', 'VIEWER'] as const;

const SUCCESS_IDS = ['experiment-exclusion-group', 'experiment-holdout'];

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn('ADMIN');
});

// ---------------------------------------------------------------------------
// #4, #5: each role, each state, and exactly the requests it sends
// ---------------------------------------------------------------------------

describe('a role that may read them (ADMIN, DEVELOPER, a superuser)', () => {
  it.each(ADMITTED)('%s sees the group and the active holdout, after one read of each', async (_, role, superuser) => {
    signIn(role, superuser);
    serve();
    await open();
    expect(await screen.findByTestId('experiment-exclusion-group')).toHaveTextContent(/^Checkout tests$/);
    expect(await screen.findByTestId('experiment-holdout')).toHaveTextContent(new RegExp(`^${HOLDOUT_SENTENCE}$`));
    // No options and no query: exactly the path, once each.
    expect(eligibilityCalls()).toEqual([[HOLDOUT_PATH], [GROUP_PATH]]);
  });

  it.each(ADMITTED)('%s sees "None" and "None active", and the group is not asked for', async (_, role, superuser) => {
    signIn(role, superuser);
    serve({ mutual_exclusion_group_id: null }, { holdout: () => null });
    await open();
    expect(await screen.findByTestId('experiment-holdout')).toHaveTextContent(/^None active$/);
    expect(screen.getByTestId('experiment-exclusion-group')).toHaveTextContent(/^None$/);
    expect(eligibilityCalls()).toEqual([[HOLDOUT_PATH]]);
  });

  it('an archived group is named, and marked as no longer applying', async () => {
    serve({}, { group: () => ({ ...GROUP, status: 'archived' }) });
    await open();
    expect(await screen.findByTestId('experiment-exclusion-group')).toHaveTextContent(
      /^Checkout tests \(archived; it no longer applies\)$/,
    );
  });

  // #1455 (PLAN C5): membership is changed on the group's page, so the row
  // links there, by the experiment's own group id, underlined (not by colour).
  it.each(['active', 'archived'])("the %s group's name links to the group's page", async (status) => {
    serve({}, { group: () => ({ ...GROUP, status }) });
    await open();
    const row = await screen.findByTestId('experiment-exclusion-group');
    const links = Array.from(row.querySelectorAll('a'));
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      [row.textContent, '/experiments/exclusion-groups/grp-1'],
    ]);
    expect(Array.from(links[0].classList)).toContain('underline');
  });

  it.each([
    ['None', { mutual_exclusion_group_id: null }, {}, 'experiment-exclusion-group'],
    ['loading', {}, { group: pending }, 'experiment-exclusion-group-loading'],
    [
      'error',
      {},
      {
        group: () => {
          throw apiError(500, 'x');
        },
      },
      'experiment-exclusion-group-error',
    ],
  ] as [string, Partial<Experiment>, { group?: Reply }, string][])(
    'the %s state links nowhere',
    async (_, experiment, replies, id) => {
      serve(experiment, replies);
      await open();
      const row = await screen.findByTestId(id);
      expect(row.querySelectorAll('a')).toHaveLength(0);
    },
  );

  it('a group id the API leaves out reads "None", and is not asked for', async () => {
    const { mutual_exclusion_group_id: _omitted, ...rest } = EXPERIMENT;
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/experiments/exp-1', handler: () => rest },
        { path: HOLDOUT_PATH, handler: () => null },
      ]) as unknown as typeof apiFetch,
    );
    await open();
    expect(await screen.findByTestId('experiment-holdout')).toHaveTextContent(/^None active$/);
    expect(screen.getByTestId('experiment-exclusion-group')).toHaveTextContent(/^None$/);
    expect(eligibilityCalls()).toEqual([[HOLDOUT_PATH]]);
  });

  it('shows "Loading…" in each row until its answer comes, under its own test id', async () => {
    serve({}, { group: pending, holdout: pending });
    await open();
    expect(screen.getByTestId('experiment-exclusion-group-loading')).toHaveTextContent(/^Loading…$/);
    expect(screen.getByTestId('experiment-holdout-loading')).toHaveTextContent(/^Loading…$/);
    for (const id of SUCCESS_IDS) expect(screen.queryByTestId(id)).toBeNull();
  });

  it('a 403 that arrives anyway reads as the role refusal', async () => {
    serve(
      {},
      {
        group: () => {
          throw apiError(403, 'PLANTED-7f3 Not enough permissions to manage');
        },
        holdout: () => {
          throw apiError(403, 'PLANTED-7f3 Not enough permissions to manage');
        },
      },
    );
    await open();
    expect(await screen.findByTestId('experiment-exclusion-group-refused')).toHaveTextContent(GROUP_REFUSED);
    expect(await screen.findByTestId('experiment-holdout-refused')).toHaveTextContent(HOLDOUT_REFUSED);
    for (const id of SUCCESS_IDS) expect(screen.queryByTestId(id)).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

describe('a role that may not read them (ANALYST, VIEWER)', () => {
  it.each(REFUSED_ROLES)('%s is told who can, sends no request, and sees nothing the API holds', async (role) => {
    signIn(role);
    // Were a request sent, these would answer with text the page must not show.
    serve(
      {},
      {
        group: () => ({ ...GROUP, name: 'PLANTED-GROUP' }),
        holdout: () => ({ ...HOLDOUT, name: 'PLANTED-HOLDOUT' }),
      },
    );
    await open();
    expect(screen.getByTestId('experiment-exclusion-group-refused')).toHaveTextContent(GROUP_REFUSED);
    expect(screen.getByTestId('experiment-holdout-refused')).toHaveTextContent(HOLDOUT_REFUSED);
    for (const id of SUCCESS_IDS) expect(screen.queryByTestId(id)).toBeNull();
    // Give any request a chance to be sent and answered.
    await waitFor(() => expect(screen.getByTestId('experiment-api-settings-note')).toBeInTheDocument());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(eligibilityCalls()).toEqual([]);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it.each(REFUSED_ROLES)('%s sees "None" for an experiment in no group: the experiment says so', async (role) => {
    signIn(role);
    serve({ mutual_exclusion_group_id: null });
    await open();
    expect(screen.getByTestId('experiment-exclusion-group')).toHaveTextContent(/^None$/);
    expect(screen.getByTestId('experiment-holdout-refused')).toHaveTextContent(HOLDOUT_REFUSED);
    expect(eligibilityCalls()).toEqual([]);
  });
});

describe('when the reads are made, and when they are not', () => {
  it('a user not yet resolved sends nothing, then a VIEWER is refused without a request', async () => {
    mockUseAuth.mockReturnValue({ user: null, status: 'loading' });
    serve();
    const view = render(<ExperimentDetailPage />);
    await screen.findByTestId('experiment-detail');
    expect(screen.getByTestId('experiment-exclusion-group-loading')).toHaveTextContent('Loading…');
    expect(screen.getByTestId('experiment-holdout-loading')).toHaveTextContent('Loading…');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(eligibilityCalls()).toEqual([]);

    signIn('VIEWER');
    view.rerender(<ExperimentDetailPage />);
    expect(screen.getByTestId('experiment-holdout-refused')).toHaveTextContent(HOLDOUT_REFUSED);
    expect(screen.getByTestId('experiment-exclusion-group-refused')).toHaveTextContent(GROUP_REFUSED);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(eligibilityCalls()).toEqual([]);
  });

  it.each([404, 500])('an experiment that answers %s sends neither read', async (status) => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments/exp-1',
          handler: () => {
            throw apiError(status, 'PLANTED-7f3');
          },
        },
        { path: GROUP_PATH, handler: () => GROUP },
        { path: HOLDOUT_PATH, handler: () => HOLDOUT },
      ]) as unknown as typeof apiFetch,
    );
    render(<ExperimentDetailPage />);
    await screen.findByTestId(status === 404 ? 'experiment-not-found' : 'experiment-error');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(eligibilityCalls()).toEqual([]);
  });

  it('the role is read at every render: an ADMIN who becomes a VIEWER sees the refusal, not what was read', async () => {
    serve(
      {},
      {
        group: () => ({ ...GROUP, name: 'PLANTED-GROUP' }),
        holdout: () => ({ ...HOLDOUT, name: 'PLANTED-HOLDOUT' }),
      },
    );
    const view = render(<ExperimentDetailPage />);
    expect(await screen.findByTestId('experiment-exclusion-group')).toHaveTextContent('PLANTED-GROUP');
    expect(await screen.findByTestId('experiment-holdout')).toHaveTextContent('PLANTED-HOLDOUT');

    signIn('VIEWER');
    view.rerender(<ExperimentDetailPage />);
    expect(screen.getByTestId('experiment-exclusion-group-refused')).toHaveTextContent(GROUP_REFUSED);
    expect(screen.getByTestId('experiment-holdout-refused')).toHaveTextContent(HOLDOUT_REFUSED);
    for (const id of SUCCESS_IDS) expect(screen.queryByTestId(id)).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

// ---------------------------------------------------------------------------
// #7: no leak, and every failure reads as fixed copy
// ---------------------------------------------------------------------------

describe('failures read as fixed copy, never the server text', () => {
  it.each([403, 404, 500])('a group answer of %s', async (status) => {
    serve(
      {},
      {
        group: () => {
          throw apiError(status, 'PLANTED-7f3');
        },
      },
    );
    await open();
    await screen.findByTestId('experiment-holdout');
    const id = status === 403 ? 'experiment-exclusion-group-refused' : 'experiment-exclusion-group-error';
    expect(await screen.findByTestId(id)).toHaveTextContent(status === 403 ? GROUP_REFUSED : GROUP_ERROR);
    expect(screen.queryByTestId('experiment-exclusion-group')).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it.each([403, 404, 500])('a holdout answer of %s', async (status) => {
    serve(
      {},
      {
        holdout: () => {
          throw apiError(status, 'PLANTED-7f3');
        },
      },
    );
    await open();
    await screen.findByTestId('experiment-exclusion-group');
    const id = status === 403 ? 'experiment-holdout-refused' : 'experiment-holdout-error';
    expect(await screen.findByTestId(id)).toHaveTextContent(status === 403 ? HOLDOUT_REFUSED : HOLDOUT_ERROR);
    expect(screen.queryByTestId('experiment-holdout')).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  // apiFetch resolves `undefined` for an empty 2xx body and throws for a
  // non-JSON one with a body (a proxy's page). Neither is "none active".
  it.each([
    ['an empty body (undefined)', () => undefined],
    [
      'a non-JSON body (apiFetch throws)',
      () => {
        throw new ApiError({
          status: 200,
          detail: 'PLANTED-7f3 Expected a JSON response from /api/v1/holdout but received text/html',
        });
      },
    ],
    ['an inactive holdout', () => ({ ...HOLDOUT, is_active: false })],
    ['a holdout with no percentage', () => ({ ...HOLDOUT, holdout_percentage: undefined, percentage: 5 })],
    ['a list', () => [HOLDOUT]],
  ])('a holdout answered with %s reads as the failure, never "None active"', async (_, holdout) => {
    serve({}, { holdout });
    await open();
    expect(await screen.findByTestId('experiment-holdout-error')).toHaveTextContent(HOLDOUT_ERROR);
    expect(screen.queryByTestId('experiment-holdout')).toBeNull();
    expect(document.body.textContent).not.toContain('None active');
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it.each([
    ['an empty body (undefined)', () => undefined],
    ['a group with no name', () => ({ ...GROUP, name: undefined })],
  ])('a group answered with %s reads as the failure', async (_, group) => {
    serve({}, { group });
    await open();
    expect(await screen.findByTestId('experiment-exclusion-group-error')).toHaveTextContent(GROUP_ERROR);
    expect(screen.queryByTestId('experiment-exclusion-group')).toBeNull();
  });

  it("never shows the group's description or experiments, or the holdout's id", async () => {
    serve();
    await open();
    await screen.findByTestId('experiment-exclusion-group');
    await screen.findByTestId('experiment-holdout');
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

// ---------------------------------------------------------------------------
// #9: the holdout is global
// ---------------------------------------------------------------------------

describe('the global holdout reads as global', () => {
  it('two experiments show the same holdout sentence, read from the one route', async () => {
    const seen: string[] = [];
    for (const experiment of [
      { name: 'First', mutual_exclusion_group_id: 'grp-1' },
      { name: 'Second', mutual_exclusion_group_id: null },
    ]) {
      mockedApiFetch.mockReset();
      serve(experiment);
      const view = render(<ExperimentDetailPage />);
      seen.push((await screen.findByTestId('experiment-holdout')).textContent ?? '');
      expect(eligibilityCalls().filter(([path]) => String(path).startsWith('/api/v1/holdout'))).toEqual([
        [HOLDOUT_PATH],
      ]);
      view.unmount();
    }
    expect(seen).toEqual([HOLDOUT_SENTENCE, HOLDOUT_SENTENCE]);
    expect(HOLDOUT_SENTENCE).toContain('5%');
    expect(HOLDOUT_SENTENCE).toContain('new users');
    expect(HOLDOUT_SENTENCE).toContain('every experiment');
  });
});

// ---------------------------------------------------------------------------
// #11 (per state) and #16 (the note's links)
// ---------------------------------------------------------------------------

describe('accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function settled(testIds: string[]) {
    for (const id of testIds) await screen.findByTestId(id);
  }

  const fail: Reply = () => {
    throw apiError(500, 'x');
  };
  type State = [string, string, Partial<Experiment>, { group?: Reply; holdout?: Reply }, string[]];
  const STATES: State[] = [
    ['present', 'ADMIN', {}, {}, ['experiment-exclusion-group', 'experiment-holdout']],
    [
      'absent',
      'ADMIN',
      { mutual_exclusion_group_id: null },
      { holdout: () => null },
      ['experiment-exclusion-group', 'experiment-holdout'],
    ],
    [
      'archived',
      'DEVELOPER',
      {},
      { group: () => ({ ...GROUP, status: 'archived' }) },
      ['experiment-exclusion-group', 'experiment-holdout'],
    ],
    ['refused', 'VIEWER', {}, {}, ['experiment-exclusion-group-refused', 'experiment-holdout-refused']],
    [
      'loading',
      'ADMIN',
      {},
      { group: pending, holdout: pending },
      ['experiment-exclusion-group-loading', 'experiment-holdout-loading'],
    ],
    ['error', 'ADMIN', {}, { group: fail, holdout: fail }, ['experiment-exclusion-group-error', 'experiment-holdout-error']],
  ];

  it.each(STATES)('the %s state has no axe violations, and every header item is one dt and its dd', async (_, role, experiment, replies, ids) => {
    signIn(role);
    serve(experiment, replies);
    const page = await open();
    await settled(ids);
    const result = await axe.run(page, axeOptions);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
    const dl = page.querySelector('dl') as HTMLElement;
    expect(Array.from(dl.children).map((item) => Array.from(item.children).map((c) => c.tagName))).toEqual(
      Array.from({ length: dl.children.length }, () => ['DT', 'DD']),
    );
    expect(Array.from(dl.querySelectorAll('dt')).map((dt) => dt.textContent).slice(-2)).toEqual([
      'Exclusion group',
      'Global holdout',
    ]);
  });
});

describe('the note under the header', () => {
  it.each(['ADMIN', 'VIEWER'])('says, to %s, where each setting is made, with links that are always underlined', async (role) => {
    signIn(role);
    serve();
    await open();
    const note = screen.getByTestId('experiment-api-settings-note');
    // Exclusion groups are no longer named (#1455): the group's own page
    // changes them, and the group row links there.
    expect(note).toHaveTextContent(
      /^Set through the API, not on this page: the global holdout, a paused experiment's resume, sequential testing\.$/,
    );
    const links = Array.from(note.querySelectorAll('a'));
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['the global holdout', docsUrl('api/mutual-exclusion-groups', 'global-holdout')],
      // A draft's dates are set on this page (#1454); a paused one's resume is not.
      ["a paused experiment's resume", docsUrl('guides/user-guide', 'pausing-and-stopping')],
      ['sequential testing', docsUrl('api/sequential-testing', 'turning-it-on')],
    ]);
    for (const a of links) {
      // Blue on slate is 1.09:1, so colour alone cannot mark a link (WCAG 1.4.1).
      expect(Array.from(a.classList)).toContain('underline');
      expect(Array.from(a.classList).filter((c) => c.startsWith('hover:'))).toEqual([]);
      expect(a.getAttribute('target')).toBe('_blank');
      expect(a.getAttribute('rel')).toBe('noreferrer');
    }
  });
});
