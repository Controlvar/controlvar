/**
 * One exclusion group's page (#1455; UX SCREEN 3a, the architect's S4, PLAN
 * C5): its experiments, adding and removing them, renaming it and changing its
 * share of traffic, and, for an ADMIN or a superuser only, archiving it.
 *
 * Every group route checks EXPERIMENT CREATE (ADMIN, DEVELOPER, superusers);
 * archiving and unarchiving are `_require_admin` (ADMIN or a superuser). The
 * users here are RESOLVED, and only the cells that say so are superusers.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import ExclusionGroupPage from '@/pages/experiments/exclusion-groups/[id]';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { GROUP_COPY } from '@/utils/exclusionGroups';
import { Route, apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({
  pathname: '/experiments/exclusion-groups/[id]',
  asPath: '/experiments/exclusion-groups/grp-1',
  query: { id: 'grp-1' },
});
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => mockAuth(),
  useAuth: () => mockAuth(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const GROUP_PATH = '/api/v1/mutual-exclusion-groups/grp-1';
const LIST_PATH = '/api/v1/mutual-exclusion-groups';
const EXPERIMENTS_PATH = '/api/v1/experiments';

interface Member {
  id: string;
  name: string;
  status: string;
}

function makeGroup(overrides: Record<string, unknown> = {}) {
  return {
    id: 'grp-1',
    name: 'Checkout',
    description: 'Checkout flow tests',
    traffic_allocation: 0.5,
    status: 'active',
    owner_id: 'PLANTED-OWNER',
    experiments: [
      { id: 'exp-1', name: 'Button colour', status: 'active' },
      { id: 'exp-2', name: 'Copy', status: 'draft' },
    ] as Member[],
    created_at: '2026-10-01T10:00:00',
    updated_at: '2026-10-01T10:00:00',
    ...overrides,
  };
}

function experiment(id: string, name: string, status: string, group: string | null) {
  return {
    id,
    name,
    key: id,
    experiment_type: 'a_b',
    status,
    targeting_rules: null,
    owner_id: null,
    start_date: null,
    end_date: null,
    created_at: '2026-10-01T10:00:00',
    updated_at: '2026-10-01T10:00:00',
    variants: [],
    metrics: [],
    mutual_exclusion_group_id: group,
  };
}

/** What `GET /api/v1/experiments/?status_filter=…` answers, per status. */
const BY_STATUS: Record<string, ReturnType<typeof experiment>[]> = {
  draft: [
    experiment('exp-2', 'Copy', 'draft', 'grp-1'),
    experiment('exp-10', 'New draft', 'draft', null),
    experiment('exp-11', 'In another group', 'draft', 'grp-3'),
  ],
  active: [experiment('exp-1', 'Button colour', 'active', 'grp-1'), experiment('exp-12', 'Running free', 'active', null)],
  paused: [experiment('exp-13', 'Paused free', 'paused', null)],
};

type Reply = (path: string, options: ApiFetchOptions) => unknown;
const pending: Reply = () => new Promise<never>(() => undefined);

interface Serve {
  group?: Reply;
  list?: Reply;
  update?: Reply;
  archive?: Reply;
  add?: Reply;
  remove?: Reply;
  experiments?: Reply;
}

let current: ReturnType<typeof makeGroup>;

function serve(handlers: Serve = {}, groupOverrides: Record<string, unknown> = {}) {
  current = makeGroup(groupOverrides);
  const routes: Route[] = [
    { path: GROUP_PATH, handler: handlers.group ?? (() => current) },
    {
      path: LIST_PATH,
      handler:
        handlers.list ??
        (() => ({ items: [current, { ...makeGroup(), id: 'grp-3', name: 'Search', experiments: [] }], total: 2 })),
    },
    {
      path: EXPERIMENTS_PATH,
      handler:
        handlers.experiments ??
        ((_, options) => {
          const status = String((options.query as Record<string, unknown>).status_filter);
          const items = BY_STATUS[status] ?? [];
          return { items, total: items.length, skip: 0, limit: 500 };
        }),
    },
    {
      method: 'PUT',
      path: GROUP_PATH,
      handler:
        handlers.update ??
        ((_, options) => {
          current = { ...current, ...(options.json as Record<string, unknown>) };
          return current;
        }),
    },
    {
      method: 'DELETE',
      path: GROUP_PATH,
      handler:
        handlers.archive ??
        (() => {
          current = { ...current, status: 'archived' };
          return current;
        }),
    },
    {
      method: 'POST',
      path: `${GROUP_PATH}/experiments`,
      handler:
        handlers.add ??
        ((_, options) => {
          const id = (options.json as { experiment_id: string }).experiment_id;
          const found = Object.values(BY_STATUS)
            .flat()
            .find((e) => e.id === id)!;
          current = { ...current, experiments: [...current.experiments, { id, name: found.name, status: found.status }] };
          return { status: 'ok', experiment_id: id, group_id: 'grp-1' };
        }),
    },
    {
      method: 'DELETE',
      path: /^\/api\/v1\/mutual-exclusion-groups\/grp-1\/experiments\/[^/]+$/,
      handler:
        handlers.remove ??
        ((path) => {
          const id = path.split('/').pop();
          current = { ...current, experiments: current.experiments.filter((e) => e.id !== id) };
          return { status: 'ok' };
        }),
    },
  ];
  mockedApiFetch.mockImplementation(routedApi(routes) as unknown as typeof apiFetch);
}

function signIn(role: string, superuser = false) {
  mockAuth.mockReturnValue({
    user: { id: 'u-1', email: `${role.toLowerCase()}@demo.com`, username: role.toLowerCase(), role, is_superuser: superuser },
    status: 'authenticated',
  });
}

const method = (options: unknown) => ((options as ApiFetchOptions | undefined)?.method ?? 'GET').toUpperCase();
const writes = () =>
  mockedApiFetch.mock.calls
    .filter(([, options]) => method(options) !== 'GET')
    .map(([path, options]) => [path, options]);
const reads = () =>
  mockedApiFetch.mock.calls.filter(([, options]) => method(options) === 'GET').map(([path, options]) => [path, options ?? null]);

const LOADED_READS = [
  [GROUP_PATH, null],
  [LIST_PATH, null],
  ...['draft', 'active', 'paused'].map((status) => [
    EXPERIMENTS_PATH,
    {
      query: {
        status_filter: status,
        skip: undefined,
        limit: 500,
        search: undefined,
        sort_by: undefined,
        sort_order: undefined,
      },
    },
  ]),
];

async function open() {
  render(<ExclusionGroupPage />);
  return screen.findByTestId('exclusion-group-detail');
}

async function candidatesLoaded() {
  await screen.findByTestId('exclusion-group-add-select');
}

const optionTexts = () =>
  Array.from((screen.getByTestId('exclusion-group-add-select') as HTMLSelectElement).options).map((o) => [
    o.value,
    o.textContent,
  ]);

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn('DEVELOPER');
});

const ADMITTED: [string, string, boolean][] = [
  ['ADMIN', 'ADMIN', false],
  ['DEVELOPER', 'DEVELOPER', false],
  ['a superuser VIEWER', 'VIEWER', true],
];

// ---------------------------------------------------------------------------
// QA #1, #2: what each role sees, and exactly what it reads
// ---------------------------------------------------------------------------

describe('a role the API admits (ADMIN, DEVELOPER, a superuser)', () => {
  it.each(ADMITTED)('%s sees the group, its experiments and the picker, after exactly these reads', async (_, role, superuser) => {
    signIn(role, superuser);
    serve();
    await open();
    await candidatesLoaded();
    expect(screen.getByTestId('exclusion-group-name-heading')).toHaveTextContent(/^Checkout$/);
    expect(screen.getByTestId('exclusion-group-status')).toHaveTextContent(/^Active$/);
    expect(screen.getByTestId('exclusion-group-share-value')).toHaveTextContent(/^50%$/);
    const members = screen.getAllByTestId('exclusion-group-member');
    expect(members.map((li) => li.textContent)).toEqual(['Button colour (Active)Remove', 'Copy (Draft)Remove']);
    expect(members.map((li) => within(li).getByRole('link').getAttribute('href'))).toEqual([
      '/experiments/exp-1',
      '/experiments/exp-2',
    ]);
    expect(within(members[0]).getByRole('button', { name: 'Remove Button colour' })).toBeInTheDocument();
    expect(reads()).toEqual(LOADED_READS);
    expect(writes()).toEqual([]);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('the picker offers draft, active and paused experiments that are in no group, and nothing else', async () => {
    serve();
    await open();
    await candidatesLoaded();
    expect(optionTexts()).toEqual([
      ['exp-10', 'New draft (Draft)'],
      ['exp-12', 'Running free (Active)'],
      ['exp-13', 'Paused free (Paused)'],
    ]);
    expect(screen.getByLabelText('Add an experiment')).toBe(screen.getByTestId('exclusion-group-add-select'));
  });

  it('a completed experiment the API returned anyway is not offered', async () => {
    serve({
      experiments: (_, options) => {
        const status = String((options.query as Record<string, unknown>).status_filter);
        const items = status === 'draft' ? [experiment('exp-20', 'Finished', 'completed', null)] : [];
        return { items, total: items.length, skip: 0, limit: 500 };
      },
    });
    await open();
    expect(await screen.findByTestId('exclusion-group-no-candidates')).toHaveTextContent(
      /^Every draft, running and paused experiment is already in a group\.$/,
    );
    expect(screen.queryByTestId('exclusion-group-add')).toBeNull();
  });

  it('says that a change moves which NEW users each experiment can get, and that assigned users keep their variant', async () => {
    serve();
    await open();
    expect(screen.getByTestId('exclusion-group-membership-note')).toHaveTextContent(
      /^Adding, removing, starting or pausing a member, or changing the traffic, moves which new users each experiment can get; users already assigned keep their variant\.$/,
    );
  });
});

describe('a role the API refuses (ANALYST, VIEWER)', () => {
  it.each(['ANALYST', 'VIEWER'])('%s is told who can, and no request is sent', async (role) => {
    signIn(role);
    serve({}, { name: 'PLANTED-GROUP' });
    render(<ExclusionGroupPage />);
    expect(screen.getByTestId('exclusion-group-refused')).toHaveTextContent(
      /^Exclusion groups are managed by the ADMIN and DEVELOPER roles\.$/,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockedApiFetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId('exclusion-group-detail')).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

describe('loading and failures', () => {
  it('a user not yet resolved sees "Loading…" and nothing is sent', async () => {
    mockAuth.mockReturnValue({ user: null, status: 'loading' });
    serve();
    render(<ExclusionGroupPage />);
    expect(screen.getByTestId('exclusion-group-loading')).toHaveTextContent(/^Loading…$/);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it('while the group loads, nothing else is read', async () => {
    serve({ group: pending });
    render(<ExclusionGroupPage />);
    expect(await screen.findByTestId('exclusion-group-loading')).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(reads()).toEqual([[GROUP_PATH, null]]);
    expect(screen.queryByTestId('exclusion-group-detail')).toBeNull();
  });

  it.each([
    [404, GROUP_COPY.notFound],
    [500, GROUP_COPY.loadFailed],
  ])('a group answered %i reads as fixed copy, and nothing else is read', async (status, copy) => {
    serve({
      group: () => {
        throw apiError(status, 'PLANTED-7f3');
      },
    });
    render(<ExclusionGroupPage />);
    expect(await screen.findByTestId('exclusion-group-error')).toHaveTextContent(copy);
    expect(screen.queryByTestId('exclusion-group-detail')).toBeNull();
    expect(reads()).toEqual([[GROUP_PATH, null]]);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('a 403 that arrives anyway reads as the refused notice', async () => {
    serve({
      group: () => {
        throw apiError(403, 'PLANTED-7f3');
      },
    });
    render(<ExclusionGroupPage />);
    expect(await screen.findByTestId('exclusion-group-refused')).toHaveTextContent(GROUP_COPY.refused);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it("the picker's read failing says so, and the rest of the page stands", async () => {
    serve({
      experiments: () => {
        throw apiError(500, 'PLANTED-7f3');
      },
    });
    await open();
    expect(await screen.findByTestId('exclusion-group-candidates-error')).toHaveTextContent(GROUP_COPY.loadFailed);
    expect(screen.getAllByTestId('exclusion-group-member')).toHaveLength(2);
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

// ---------------------------------------------------------------------------
// Membership (C5): add and remove, each re-reading the group
// ---------------------------------------------------------------------------

describe('adding an experiment', () => {
  it('sends POST {experiment_id} for the chosen one, then shows it as a member', async () => {
    serve();
    await open();
    await candidatesLoaded();
    fireEvent.change(screen.getByTestId('exclusion-group-add-select'), { target: { value: 'exp-12' } });
    fireEvent.click(screen.getByTestId('exclusion-group-add'));
    await waitFor(() => expect(screen.getAllByTestId('exclusion-group-member')).toHaveLength(3));
    expect(writes()).toEqual([[`${GROUP_PATH}/experiments`, { method: 'POST', json: { experiment_id: 'exp-12' } }]]);
    expect(screen.getAllByTestId('exclusion-group-member')[2]).toHaveTextContent('Running free (Active)');
    // The group and the picker were read again.
    expect(reads().filter(([path]) => path === GROUP_PATH)).toHaveLength(2);
    expect(reads().filter(([path]) => path === EXPERIMENTS_PATH)).toHaveLength(6);
  });

  it.each([
    [400, GROUP_COPY.inAnotherGroup],
    [403, GROUP_COPY.refused],
    [500, GROUP_COPY.saveFailed],
  ])('an add answered %i shows fixed copy, never the detail, and re-reads the group', async (status, copy) => {
    serve({
      add: () => {
        throw new ApiError({ status, detail: 'PLANTED-7f3 Experiment exp-12 is already in another group' });
      },
    });
    await open();
    await candidatesLoaded();
    fireEvent.click(screen.getByTestId('exclusion-group-add'));
    const alert = await screen.findByTestId('exclusion-group-action-error');
    expect(alert).toHaveTextContent(copy);
    expect(alert).toHaveAttribute('role', 'alert');
    await waitFor(() => expect(reads().filter(([path]) => path === GROUP_PATH)).toHaveLength(2));
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('is not offered for an archived group, and its experiments are not read; Remove stays', async () => {
    serve({}, { status: 'archived' });
    await open();
    expect(screen.getByTestId('exclusion-group-status')).toHaveTextContent(/^Archived$/);
    expect(screen.getByTestId('exclusion-group-archived-note')).toHaveTextContent(
      /^Its experiments are no longer kept apart: new users can join more than one\.$/,
    );
    await waitFor(() => expect(reads()).toEqual([
      [GROUP_PATH, null],
      [LIST_PATH, null],
    ]));
    expect(screen.queryByTestId('exclusion-group-add-section')).toBeNull();
    expect(screen.getAllByTestId('exclusion-group-remove')).toHaveLength(2);
  });
});

describe('removing an experiment', () => {
  it('asks first, naming it, then sends DELETE on its own path and re-reads', async () => {
    serve();
    await open();
    fireEvent.click(screen.getByRole('button', { name: 'Remove Button colour' }));
    const dialog = screen.getByTestId('exclusion-group-remove-confirm');
    expect(dialog).toHaveAttribute('role', 'dialog');
    expect(dialog).toHaveTextContent(
      "Remove “Button colour” from this group? New users can then be in it and in the group's other experiments.",
    );
    expect(document.activeElement).toBe(dialog);
    expect(writes()).toEqual([]);
    fireEvent.click(screen.getByTestId('exclusion-group-remove-yes'));
    await waitFor(() => expect(screen.getAllByTestId('exclusion-group-member')).toHaveLength(1));
    expect(writes()).toEqual([[`${GROUP_PATH}/experiments/exp-1`, { method: 'DELETE' }]]);
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('heading', { level: 2, name: 'Experiments' })),
    );
  });

  it('Cancel and Escape send nothing', async () => {
    serve();
    await open();
    fireEvent.click(screen.getByRole('button', { name: 'Remove Copy' }));
    fireEvent.click(screen.getByTestId('exclusion-group-remove-cancel'));
    expect(screen.queryByTestId('exclusion-group-remove-confirm')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Remove Copy' }));
    fireEvent.keyDown(screen.getByTestId('exclusion-group-remove-confirm'), { key: 'Escape' });
    expect(screen.queryByTestId('exclusion-group-remove-confirm')).toBeNull();
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('heading', { level: 2, name: 'Experiments' })),
    );
    expect(writes()).toEqual([]);
  });

  it('a failed remove shows fixed copy, never the detail', async () => {
    serve({
      remove: () => {
        throw apiError(400, 'PLANTED-7f3 not found in group');
      },
    });
    await open();
    fireEvent.click(screen.getByRole('button', { name: 'Remove Copy' }));
    fireEvent.click(screen.getByTestId('exclusion-group-remove-yes'));
    expect(await screen.findByTestId('exclusion-group-action-error')).toHaveTextContent(GROUP_COPY.saveFailed);
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

// ---------------------------------------------------------------------------
// Rename and traffic (PE 4, QA #17)
// ---------------------------------------------------------------------------

describe('editing the group', () => {
  async function edit() {
    await open();
    await candidatesLoaded();
    fireEvent.click(screen.getByTestId('exclusion-group-edit'));
    return screen.getByTestId('exclusion-group-form');
  }

  it('is prefilled with the name, the description and the share as a percent', async () => {
    serve();
    await edit();
    expect(screen.getByTestId('exclusion-group-name')).toHaveValue('Checkout');
    expect(screen.getByTestId('exclusion-group-description')).toHaveValue('Checkout flow tests');
    expect(screen.getByTestId('exclusion-group-share')).toHaveValue(50);
  });

  it('refuses a name another group has, exactly, before any request', async () => {
    serve();
    await edit();
    fireEvent.change(screen.getByTestId('exclusion-group-name'), { target: { value: 'Search' } });
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    expect(screen.getByTestId('exclusion-group-name-error')).toHaveTextContent(GROUP_COPY.nameTaken);
    expect(writes()).toEqual([]);
  });

  it('sends a name that differs only in case, and only the name: "search" is not "Search"', async () => {
    serve();
    await edit();
    fireEvent.change(screen.getByTestId('exclusion-group-name'), { target: { value: 'search' } });
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    await waitFor(() => expect(screen.getByTestId('exclusion-group-name-heading')).toHaveTextContent('search'));
    expect(writes()).toEqual([[GROUP_PATH, { method: 'PUT', json: { name: 'search' } }]]);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('exclusion-group-edit')));
  });

  it('sends a new share as a fraction, and nothing else: 25% is 0.25', async () => {
    serve();
    await edit();
    fireEvent.change(screen.getByTestId('exclusion-group-share'), { target: { value: '25' } });
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    await waitFor(() => expect(screen.getByTestId('exclusion-group-share-value')).toHaveTextContent(/^25%$/));
    expect(writes()).toEqual([[GROUP_PATH, { method: 'PUT', json: { traffic_allocation: 0.25 } }]]);
  });

  it('keeping its own name is not a clash, and an unchanged form sends nothing', async () => {
    serve();
    await edit();
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    await waitFor(() => expect(screen.queryByTestId('exclusion-group-form')).toBeNull());
    expect(writes()).toEqual([]);
  });

  it.each([
    [409, GROUP_COPY.nameTaken],
    [404, GROUP_COPY.notFound],
    [403, GROUP_COPY.refused],
    [500, GROUP_COPY.saveFailed],
  ])('a rename answered %i shows fixed copy, never the detail', async (status, copy) => {
    serve({
      update: () => {
        throw new ApiError({ status, detail: "PLANTED-7f3 A mutual exclusion group with the name 'x' already exists." });
      },
    });
    await edit();
    fireEvent.change(screen.getByTestId('exclusion-group-name'), { target: { value: 'Brand new' } });
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    expect(await screen.findByTestId('exclusion-group-form-error')).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

// ---------------------------------------------------------------------------
// Archive and unarchive: ADMIN or a superuser only (canAdministerEligibility)
// ---------------------------------------------------------------------------

describe('archiving', () => {
  it.each([
    ['ADMIN', 'ADMIN', false],
    ['a superuser VIEWER', 'VIEWER', true],
  ] as [string, string, boolean][])('%s is asked first, then DELETE archives the group', async (_, role, superuser) => {
    signIn(role, superuser);
    serve();
    await open();
    fireEvent.click(screen.getByTestId('exclusion-group-archive'));
    const dialog = screen.getByTestId('exclusion-group-archive-confirm');
    expect(dialog).toHaveTextContent(
      'Archive “Checkout”? Its experiments are no longer kept apart: new users can join more than one. They stay listed here, and an ADMIN can unarchive it.',
    );
    expect(writes()).toEqual([]);
    fireEvent.click(screen.getByTestId('exclusion-group-archive-yes'));
    await waitFor(() => expect(screen.getByTestId('exclusion-group-status')).toHaveTextContent(/^Archived$/));
    expect(writes()).toEqual([[GROUP_PATH, { method: 'DELETE' }]]);
    expect(screen.queryByTestId('exclusion-group-add-section')).toBeNull();
    expect(screen.getByTestId('exclusion-group-unarchive')).toBeInTheDocument();
  });

  it('a DEVELOPER is told it needs the ADMIN role, and has no control for it', async () => {
    signIn('DEVELOPER');
    serve();
    await open();
    expect(screen.getByTestId('exclusion-group-archive-role-note')).toHaveTextContent(
      /^Archiving and unarchiving a group requires the ADMIN role\.$/,
    );
    expect(screen.queryByTestId('exclusion-group-archive')).toBeNull();
    expect(screen.queryByTestId('exclusion-group-unarchive')).toBeNull();
  });

  it('a DEVELOPER on an archived group has no Unarchive either', async () => {
    signIn('DEVELOPER');
    serve({}, { status: 'archived' });
    await open();
    expect(screen.getByTestId('exclusion-group-archive-role-note')).toBeInTheDocument();
    expect(screen.queryByTestId('exclusion-group-unarchive')).toBeNull();
  });

  it('an ADMIN unarchives with PUT {status: "active"}, and the picker returns', async () => {
    signIn('ADMIN');
    serve({}, { status: 'archived' });
    await open();
    fireEvent.click(screen.getByTestId('exclusion-group-unarchive'));
    await waitFor(() => expect(screen.getByTestId('exclusion-group-status')).toHaveTextContent(/^Active$/));
    expect(writes()).toEqual([[GROUP_PATH, { method: 'PUT', json: { status: 'active' } }]]);
    await candidatesLoaded();
  });

  it('Cancel sends nothing and returns focus to Archive group', async () => {
    signIn('ADMIN');
    serve();
    await open();
    fireEvent.click(screen.getByTestId('exclusion-group-archive'));
    fireEvent.click(screen.getByTestId('exclusion-group-archive-cancel'));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('exclusion-group-archive')));
    expect(writes()).toEqual([]);
  });

  it.each([
    [403, GROUP_COPY.archiveAdminOnly],
    [500, GROUP_COPY.saveFailed],
  ])('an archive answered %i shows fixed copy, never the detail', async (status, copy) => {
    signIn('ADMIN');
    serve({
      archive: () => {
        throw apiError(status, 'PLANTED-7f3 Admin permissions required');
      },
    });
    await open();
    fireEvent.click(screen.getByTestId('exclusion-group-archive'));
    fireEvent.click(screen.getByTestId('exclusion-group-archive-yes'));
    expect(await screen.findByTestId('exclusion-group-action-error')).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

// ---------------------------------------------------------------------------
// QA #28: accessibility in each state (jsdom: colour contrast is not computable)
// ---------------------------------------------------------------------------

describe('accessibility (axe-core in jsdom)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function noViolations(container: HTMLElement) {
    const result = await axe.run(container, axeOptions);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  }

  it('the group, as an ADMIN', async () => {
    signIn('ADMIN');
    serve();
    const { container } = render(<ExclusionGroupPage />);
    await candidatesLoaded();
    await noViolations(container);
  });

  it('an archived group, as a DEVELOPER', async () => {
    serve({}, { status: 'archived' });
    const { container } = render(<ExclusionGroupPage />);
    await screen.findByTestId('exclusion-group-archived-note');
    await noViolations(container);
  });

  it('the remove and archive confirmations', async () => {
    signIn('ADMIN');
    serve();
    const { container } = render(<ExclusionGroupPage />);
    await candidatesLoaded();
    fireEvent.click(screen.getByRole('button', { name: 'Remove Copy' }));
    await noViolations(container);
    fireEvent.click(screen.getByTestId('exclusion-group-remove-cancel'));
    fireEvent.click(screen.getByTestId('exclusion-group-archive'));
    await noViolations(container);
  });

  it('the edit form with a refused name, and a failed add', async () => {
    serve({
      add: () => {
        throw apiError(400, 'x');
      },
    });
    const { container } = render(<ExclusionGroupPage />);
    await candidatesLoaded();
    fireEvent.click(screen.getByTestId('exclusion-group-add'));
    await screen.findByTestId('exclusion-group-action-error');
    fireEvent.click(screen.getByTestId('exclusion-group-edit'));
    fireEvent.change(screen.getByTestId('exclusion-group-name'), { target: { value: 'Search' } });
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    await screen.findByTestId('exclusion-group-name-error');
    await noViolations(container);
  });

  it('the refused notice and the load failure', async () => {
    signIn('VIEWER');
    serve();
    const first = render(<ExclusionGroupPage />);
    await screen.findByTestId('exclusion-group-refused');
    await noViolations(first.container);
    first.unmount();
    signIn('ADMIN');
    serve({
      group: () => {
        throw apiError(500, 'x');
      },
    });
    const second = render(<ExclusionGroupPage />);
    await screen.findByTestId('exclusion-group-error');
    await noViolations(second.container);
  });
});
