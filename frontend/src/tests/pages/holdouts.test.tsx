/**
 * The global-holdout page (#1455; UX SCREEN 3b, the architect's S5, PLAN C4
 * and C6, T134).
 *
 * ADMIN and superusers (`_require_admin`) list every holdout (`/holdout/all`),
 * create, edit, activate and end them. A DEVELOPER reads the active one
 * (`GET /holdout`) and its results (beta), and never asks for `/all`. ANALYST
 * and VIEWER are refused with no request. The scope note comes first in every
 * state. Users here are RESOLVED, and only the cells that say so are
 * superusers.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import HoldoutsPage from '@/pages/experiments/holdouts';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';
import { HOLDOUT_COPY } from '@/utils/holdouts';
import { Route, apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments/holdouts', asPath: '/experiments/holdouts' });
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

const ALL = '/api/v1/holdout/all';
const ACTIVE = '/api/v1/holdout';
const SCOPE =
  'A global holdout keeps a share of new users out of EVERY experiment, not one, so you can measure what all your experiments changed together. Only one holdout is active at a time: activating another ends this one. Feature flags ignore it. Users who already had an assignment keep it.';

interface Holdout {
  id: string;
  name: string;
  description: string | null;
  holdout_percentage: number;
  is_active: boolean;
  owner_id: string | null;
  activated_at: string | null;
  deactivated_at: string | null;
  created_at: string;
  updated_at: string;
}

function holdout(id: string, name: string, overrides: Partial<Holdout> = {}): Holdout {
  return {
    id,
    name,
    description: null,
    holdout_percentage: 5,
    is_active: false,
    owner_id: 'PLANTED-OWNER',
    activated_at: null,
    deactivated_at: null,
    created_at: '2026-09-01T10:00:00',
    updated_at: '2026-09-01T10:00:00',
    ...overrides,
  };
}

const START: Holdout[] = [
  holdout('h-q4', 'Q4 holdout', { is_active: true, activated_at: '2026-10-01T09:00:00' }),
  holdout('h-q1', 'Q1 holdout', { holdout_percentage: 3 }),
  holdout('h-old', 'Old holdout', {
    holdout_percentage: 10,
    activated_at: '2026-06-01T09:00:00',
    deactivated_at: '2026-09-01T09:00:00',
  }),
];

let server: Holdout[] = [];

type Reply = (path: string, options: ApiFetchOptions) => unknown;
const pending: Reply = () => new Promise<never>(() => undefined);

const RESULTS = {
  holdout_id: 'h-q4',
  holdout_name: 'Q4 holdout',
  holdout_percentage: 5,
  metric: 'purchase',
  unavailable_reason: null,
  message: null,
  groups: [
    { group: 'in_holdout', label: 'PLANTED-LABEL', users: 400, conversions: 40, conversion_rate: 0.1, excluded_users: 2 },
    { group: 'not_in_holdout', label: 'PLANTED-LABEL', users: 7600, conversions: 912, conversion_rate: 0.12, excluded_users: 30 },
  ],
  difference: {
    absolute: 0.02,
    relative_pct: 20,
    ci_lower: -0.011,
    ci_upper: 0.051,
    p_value: 0.0312,
    is_significant: true,
    always_valid_ci_lower: -0.05,
    always_valid_ci_upper: 0.09,
  },
  analysis_status: 'beta',
  analysis_notice: 'PLANTED-NOTICE',
};

function serve(handlers: { all?: Reply; active?: Reply; create?: Reply; update?: Reply; results?: Reply } = {}) {
  server = START.map((h) => ({ ...h }));
  const routes: Route[] = [
    { path: ALL, handler: handlers.all ?? (() => ({ items: server, total: server.length })) },
    { path: ACTIVE, handler: handlers.active ?? (() => server.find((h) => h.is_active) ?? null) },
    {
      method: 'POST',
      path: ACTIVE,
      handler:
        handlers.create ??
        ((_, options) => {
          const body = options.json as Partial<Holdout>;
          if (body.is_active) {
            server = server.map((h) => (h.is_active ? { ...h, is_active: false, deactivated_at: '2026-10-30T08:00:00' } : h));
          }
          const created = holdout('h-new', String(body.name), {
            ...body,
            activated_at: body.is_active ? '2026-10-30T08:00:00' : null,
          } as Partial<Holdout>);
          server = [...server, created];
          return created;
        }),
    },
    {
      method: 'PUT',
      path: /^\/api\/v1\/holdout\/[^/]+$/,
      handler:
        handlers.update ??
        ((path, options) => {
          const id = path.split('/').pop();
          const body = options.json as Partial<Holdout>;
          server = server.map((h) => {
            if (h.id === id) {
              const next = { ...h, ...body };
              if (body.is_active === true) next.activated_at = '2026-10-30T08:00:00';
              if (body.is_active === false) next.deactivated_at = '2026-10-30T08:00:00';
              return next;
            }
            if (body.is_active === true && h.is_active) {
              return { ...h, is_active: false, deactivated_at: '2026-10-30T08:00:00' };
            }
            return h;
          });
          return server.find((h) => h.id === id);
        }),
    },
    { path: /^\/api\/v1\/holdout\/[^/]+\/results$/, handler: handlers.results ?? (() => RESULTS) },
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
const calls = () => mockedApiFetch.mock.calls.map(([path, options]) => [path, options ?? null]);
const writes = () => mockedApiFetch.mock.calls.filter(([, o]) => method(o) !== 'GET').map(([p, o]) => [p, o]);
const reads = (path: string) => mockedApiFetch.mock.calls.filter(([p, o]) => p === path && method(o) === 'GET');

const statuses = () => screen.getAllByTestId('holdout-status').map((td) => td.textContent);

async function openAdmin() {
  render(<HoldoutsPage />);
  await screen.findByTestId('holdouts-table');
}

function row(name: string) {
  return screen.getAllByTestId('holdout-row').find((tr) => tr.querySelector('td')?.textContent === name)!;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn('ADMIN');
});

// ---------------------------------------------------------------------------
// The scope note, first in every state (UX SCREEN 3b)
// ---------------------------------------------------------------------------

describe('the scope note', () => {
  const ROLES: [string, () => void, Parameters<typeof serve>[0]][] = [
    ['ADMIN', () => signIn('ADMIN'), {}],
    ['DEVELOPER', () => signIn('DEVELOPER'), {}],
    ['a superuser VIEWER', () => signIn('VIEWER', true), {}],
    ['ANALYST', () => signIn('ANALYST'), {}],
    ['VIEWER', () => signIn('VIEWER'), {}],
    ['a user not yet resolved', () => mockAuth.mockReturnValue({ user: null, status: 'loading' }), {}],
    [
      'a load failure',
      () => signIn('ADMIN'),
      {
        all: () => {
          throw apiError(500, 'x');
        },
      },
    ],
  ];

  it.each(ROLES)('comes first, in full, for %s', async (_, sign, handlers) => {
    sign();
    serve(handlers);
    render(<HoldoutsPage />);
    const note = screen.getByTestId('holdout-scope-note');
    expect(note).toHaveTextContent(new RegExp(`^${SCOPE.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`));
    expect(note.previousElementSibling?.tagName).toBe('H1');
    // Nothing the page shows comes before it but the heading and the way back.
    const testIds = Array.from(document.querySelectorAll('[data-testid]')).map((el) => el.getAttribute('data-testid'));
    expect(testIds[0]).toBe('holdout-scope-note');
    expect(Array.from(note.classList)).toContain('text-slate-700');
    expect(Array.from(note.classList)).toContain('bg-white');
  });
});

// ---------------------------------------------------------------------------
// Roles (T134) and exactly the requests each sends
// ---------------------------------------------------------------------------

describe('who sees what', () => {
  it.each([
    ['ADMIN', 'ADMIN', false],
    ['a superuser VIEWER', 'VIEWER', true],
  ] as [string, string, boolean][])('%s reads /holdout/all once, and sees every holdout and the active one', async (_, role, superuser) => {
    signIn(role, superuser);
    serve();
    render(<HoldoutsPage />);
    // The capability table's witness, written out: test_dashboard_and_api.py reads it here.
    await screen.findByTestId('holdouts-page');
    expect(calls()).toEqual([[ALL, null]]);
    expect(screen.getByTestId('holdout-active')).toHaveTextContent(
      /^Q4 holdout: keeps 5% of new users out of every experiment$/,
    );
    expect(screen.getByTestId('holdout-active-since')).toHaveTextContent(/^Active since /);
    const rows = screen
      .getAllByTestId('holdout-row')
      .map((tr) => Array.from(tr.querySelectorAll('td')).slice(0, 3).map((td) => td.textContent));
    expect(rows).toEqual([
      ['Q4 holdout', '5%', 'Active'],
      ['Q1 holdout', '3%', 'Not activated'],
      ['Old holdout', '10%', 'Ended'],
    ]);
    expect(screen.getByTestId('new-holdout-btn')).toHaveTextContent(/^New holdout$/);
    expect(screen.getByTestId('holdout-end')).toBeInTheDocument();
    expect(screen.queryByTestId('holdout-admin-only')).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('a DEVELOPER reads the active holdout only, never /holdout/all, and is told who changes it', async () => {
    signIn('DEVELOPER');
    serve();
    render(<HoldoutsPage />);
    await screen.findByTestId('holdouts-page');
    expect(screen.getByTestId('holdout-active')).toHaveTextContent(
      'Q4 holdout: keeps 5% of new users out of every experiment',
    );
    expect(screen.getByTestId('holdout-admin-only')).toHaveTextContent(
      /^Creating, activating and ending holdouts requires the ADMIN role\.$/,
    );
    for (const id of ['holdouts-table', 'new-holdout-btn', 'holdout-end', 'holdout-activate', 'holdout-edit']) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
    // Results (beta) are the DEVELOPER's too.
    fireEvent.click(screen.getByTestId('holdout-results-open'));
    fireEvent.change(screen.getByTestId('holdout-results-metric'), { target: { value: 'purchase' } });
    fireEvent.click(screen.getByTestId('holdout-results-show'));
    await screen.findByTestId('holdout-results-table');
    expect(calls()).toEqual([
      [ACTIVE, null],
      ['/api/v1/holdout/h-q4/results', { query: { metric: 'purchase' } }],
    ]);
    expect(reads(ALL)).toEqual([]);
  });

  it('a DEVELOPER with no active holdout is told so', async () => {
    signIn('DEVELOPER');
    serve({ active: () => null });
    render(<HoldoutsPage />);
    expect(await screen.findByTestId('holdout-none')).toHaveTextContent(/^No global holdout is active\.$/);
    expect(screen.getByTestId('holdouts-page')).toBeInTheDocument();
  });

  it.each(['ANALYST', 'VIEWER'])('%s is refused, and no request is sent', async (role) => {
    signIn(role);
    serve();
    render(<HoldoutsPage />);
    expect(screen.getByTestId('holdouts-refused')).toHaveTextContent(
      /^Seeing the global holdout requires the ADMIN or DEVELOPER role\.$/,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockedApiFetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId('holdouts-page')).toBeNull();
  });
});

describe('the states the witness never marks (QA #27)', () => {
  it('a user not yet resolved: "Loading…", and nothing is sent', async () => {
    mockAuth.mockReturnValue({ user: null, status: 'loading' });
    serve();
    render(<HoldoutsPage />);
    expect(screen.getByTestId('holdouts-loading')).toHaveTextContent(/^Loading…$/);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockedApiFetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId('holdouts-page')).toBeNull();
  });

  it('while loading', async () => {
    serve({ all: pending });
    render(<HoldoutsPage />);
    expect(await screen.findByTestId('holdouts-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('holdouts-page')).toBeNull();
  });

  it.each([
    [500, HOLDOUT_COPY.loadFailed],
    [0, UNREACHABLE],
  ])('a list answered %i reads as fixed copy', async (status, copy) => {
    serve({
      all: () => {
        throw new ApiError({ status, detail: 'PLANTED-7f3' });
      },
    });
    render(<HoldoutsPage />);
    expect(await screen.findByTestId('holdouts-error')).toHaveTextContent(copy);
    expect(screen.queryByTestId('holdouts-page')).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('a 403 that arrives anyway reads as the refusal', async () => {
    signIn('DEVELOPER');
    serve({
      active: () => {
        throw apiError(403, 'PLANTED-7f3');
      },
    });
    render(<HoldoutsPage />);
    expect(await screen.findByTestId('holdouts-refused')).toHaveTextContent(HOLDOUT_COPY.refused);
    expect(screen.queryByTestId('holdouts-page')).toBeNull();
  });

  it('an active holdout of the wrong shape is a failure, never "No global holdout is active."', async () => {
    signIn('DEVELOPER');
    serve({ active: () => ({ ...START[0], is_active: false }) });
    render(<HoldoutsPage />);
    expect(await screen.findByTestId('holdouts-error')).toHaveTextContent(HOLDOUT_COPY.loadFailed);
    expect(screen.queryByTestId('holdout-none')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// New holdout (C6: 5% by default; QA #17: 5% sends 5)
// ---------------------------------------------------------------------------

describe('New holdout', () => {
  async function openForm() {
    await openAdmin();
    fireEvent.click(screen.getByTestId('new-holdout-btn'));
    return screen.getByTestId('holdout-form');
  }

  it('defaults to 5% and not activated, says what the size means, and sends exactly that', async () => {
    serve();
    await openForm();
    expect(screen.getByTestId('holdout-size')).toHaveValue(5);
    expect(screen.getByTestId('holdout-activate-now')).not.toBeChecked();
    expect(screen.getByTestId('holdout-size-help')).toHaveTextContent(
      /^Recommended 1-5%, up to 10% when traffic is low\. It cannot change once the holdout has been active\.$/,
    );
    expect(screen.getByLabelText('Size (% of new users)')).toBe(screen.getByTestId('holdout-size'));
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'Q2 holdout' } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    await waitFor(() => expect(screen.queryByTestId('holdout-form')).toBeNull());
    expect(writes()).toEqual([
      [ACTIVE, { method: 'POST', json: { name: 'Q2 holdout', holdout_percentage: 5, is_active: false } }],
    ]);
    expect(reads(ALL)).toHaveLength(2);
    expect(statuses()).toEqual(['Active', 'Not activated', 'Ended', 'Not activated']);
  });

  it('"Activate now" asks first, naming the holdout it ends, then sends is_active: true, and one holdout is Active', async () => {
    serve();
    await openForm();
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'Q2 holdout' } });
    fireEvent.change(screen.getByTestId('holdout-size'), { target: { value: '4' } });
    fireEvent.click(screen.getByTestId('holdout-activate-now'));
    fireEvent.click(screen.getByTestId('holdout-submit'));
    expect(screen.getByTestId('holdout-confirm')).toHaveTextContent(
      'Activate “Q2 holdout”? “Q4 holdout” ends now, and an ended holdout cannot be activated again. From now on Q2 holdout keeps 4% of new users out of every experiment.',
    );
    expect(writes()).toEqual([]);
    fireEvent.click(screen.getByTestId('holdout-confirm-yes'));
    await waitFor(() => expect(statuses()).toEqual(['Ended', 'Not activated', 'Ended', 'Active']));
    expect(writes()).toEqual([
      [ACTIVE, { method: 'POST', json: { name: 'Q2 holdout', holdout_percentage: 4, is_active: true } }],
    ]);
    expect(statuses().filter((s) => s === 'Active')).toHaveLength(1);
  });

  it('refuses a name another holdout has, exactly, before any request; another case is another name', async () => {
    serve();
    await openForm();
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'Q4 holdout' } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    expect(screen.getByTestId('holdout-name-error')).toHaveTextContent(/^A holdout with this name already exists\.$/);
    expect(screen.getByTestId('holdout-name')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('holdout-name').getAttribute('aria-describedby')).toBe(
      screen.getByTestId('holdout-name-error').id,
    );
    // The alert is a short summary; the problem itself is said once, by the field.
    expect(screen.getByTestId('holdout-form-problem')).toHaveTextContent(/^Check the highlighted field\.$/);
    expect(screen.getAllByText(HOLDOUT_COPY.nameTaken)).toHaveLength(1);
    expect(writes()).toEqual([]);
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'q4 holdout' } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toEqual([
      ACTIVE,
      { method: 'POST', json: { name: 'q4 holdout', holdout_percentage: 5, is_active: false } },
    ]);
  });

  it.each(['0', '21', '2.5', ''])('refuses the size %p before any request', async (size) => {
    serve();
    await openForm();
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'Q2 holdout' } });
    fireEvent.change(screen.getByTestId('holdout-size'), { target: { value: size } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    expect(screen.getByTestId('holdout-size-error')).toHaveTextContent(/^Enter a whole number from 1 to 20\.$/);
    expect(document.activeElement).toBe(screen.getByTestId('holdout-size'));
    expect(writes()).toEqual([]);
  });

  it.each([
    [false, 409, HOLDOUT_COPY.nameTaken, true],
    [true, 409, HOLDOUT_COPY.conflict, true],
    [false, 403, HOLDOUT_COPY.adminOnly, false],
    [false, 500, HOLDOUT_COPY.saveFailed, false],
  ])(
    'a create (activate now: %s) answered %i shows fixed copy, never the detail; only a 409 reads the list again (%s)',
    async (activate, status, copy, rereads) => {
      serve({
        create: () => {
          // Another ADMIN got there first: the server now holds a holdout this page has not read.
          server = [...server, holdout('h-other', 'Q2 holdout')];
          throw apiError(status, 'PLANTED-7f3 duplicate key value violates unique constraint');
        },
      });
      await openForm();
      fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'Q2 holdout' } });
      if (activate) fireEvent.click(screen.getByTestId('holdout-activate-now'));
      fireEvent.click(screen.getByTestId('holdout-submit'));
      if (activate) fireEvent.click(screen.getByTestId('holdout-confirm-yes'));
      expect(await screen.findByTestId('holdout-form-error')).toHaveTextContent(copy);
      expect(document.body.textContent).not.toContain('PLANTED');
      // The form is no longer busy once the error shows unless it reads again; wait for that either way.
      await waitFor(() => expect(screen.getByTestId('holdout-submit')).not.toBeDisabled());
      expect(reads(ALL)).toHaveLength(rereads ? 2 : 1);
      // A 409 means something moved: the page shows what the server now holds.
      expect(statuses()).toHaveLength(rereads ? 4 : 3);
      expect(screen.queryAllByTestId('holdout-row').some((tr) => tr.textContent?.startsWith('Q2 holdout'))).toBe(
        rereads,
      );
    },
  );
});

// ---------------------------------------------------------------------------
// Activate and end: one active at a time (QA #15, #16)
// ---------------------------------------------------------------------------

describe('activating and ending', () => {
  it('Activate is offered only on a holdout never activated; End only on the active one', async () => {
    serve();
    await openAdmin();
    expect(within(row('Q1 holdout')).getByRole('button', { name: 'Activate Q1 holdout' })).toBeInTheDocument();
    expect(within(row('Q4 holdout')).queryByTestId('holdout-activate')).toBeNull();
    expect(within(row('Old holdout')).queryByTestId('holdout-activate')).toBeNull();
    expect(within(row('Q4 holdout')).getByRole('button', { name: 'End Q4 holdout' })).toBeInTheDocument();
    expect(within(row('Old holdout')).queryByTestId('holdout-row-end')).toBeNull();
  });

  it('activating asks first, naming the holdout it ends; then PUT is_active: true, a re-read, and one Active', async () => {
    serve();
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: 'Activate Q1 holdout' }));
    const dialog = screen.getByTestId('holdout-confirm');
    expect(dialog).toHaveTextContent(
      'Activate “Q1 holdout”? “Q4 holdout” ends now, and an ended holdout cannot be activated again. From now on Q1 holdout keeps 3% of new users out of every experiment.',
    );
    expect(document.activeElement).toBe(dialog);
    fireEvent.click(screen.getByTestId('holdout-confirm-yes'));
    await waitFor(() => expect(statuses()).toEqual(['Ended', 'Active', 'Ended']));
    expect(writes()).toEqual([['/api/v1/holdout/h-q1', { method: 'PUT', json: { is_active: true } }]]);
    expect(reads(ALL)).toHaveLength(2);
    expect(statuses().filter((s) => s === 'Active')).toHaveLength(1);
    expect(screen.getByTestId('holdout-active')).toHaveTextContent('Q1 holdout: keeps 3%');
  });

  it('with none active, the confirmation says the size is then fixed', async () => {
    serve({ all: () => ({ items: START.slice(1), total: 2 }) });
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: 'Activate Q1 holdout' }));
    expect(screen.getByTestId('holdout-confirm')).toHaveTextContent(
      /^Activate “Q1 holdout”\? From now on Q1 holdout keeps 3% of new users out of every experiment\. Its size is then fixed\./,
    );
  });

  it.each([
    [409, HOLDOUT_COPY.conflict],
    [422, HOLDOUT_COPY.changed],
    [404, HOLDOUT_COPY.notFound],
  ])('an activation answered %i shows fixed copy and reads the list again', async (status, copy) => {
    serve({
      update: () => {
        throw apiError(status, 'PLANTED-7f3 A holdout that has ended cannot restart.');
      },
    });
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: 'Activate Q1 holdout' }));
    fireEvent.click(screen.getByTestId('holdout-confirm-yes'));
    const alert = await screen.findByTestId('holdout-action-error');
    expect(alert).toHaveTextContent(copy);
    await waitFor(() => expect(reads(ALL)).toHaveLength(2));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('holdout-action-error')));
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('ending asks first, then PUT is_active: false, and none is active', async () => {
    serve();
    await openAdmin();
    fireEvent.click(screen.getByTestId('holdout-end'));
    const dialog = screen.getByTestId('holdout-confirm');
    expect(dialog).toHaveTextContent(
      'End “Q4 holdout”? New users are no longer kept out of experiments. An ended holdout cannot be activated again; its results stay readable.',
    );
    expect(dialog).toHaveAttribute('role', 'dialog');
    expect(document.activeElement).toBe(dialog);
    expect(writes()).toEqual([]);
    fireEvent.click(screen.getByTestId('holdout-confirm-yes'));
    await waitFor(() => expect(screen.getByTestId('holdout-none')).toHaveTextContent(HOLDOUT_COPY.none));
    expect(writes()).toEqual([['/api/v1/holdout/h-q4', { method: 'PUT', json: { is_active: false } }]]);
    expect(statuses()).toEqual(['Ended', 'Not activated', 'Ended']);
  });

  const activeHeading = () => screen.getByRole('heading', { name: 'Active holdout' });

  it.each([
    [
      'Activate, from its row',
      () => fireEvent.click(screen.getByRole('button', { name: 'Activate Q1 holdout' })),
      ['/api/v1/holdout/h-q1', { method: 'PUT', json: { is_active: true } }],
      'Q1 holdout: keeps 3% of new users out of every experiment',
    ],
    [
      'End holdout, on the active card',
      () => fireEvent.click(screen.getByTestId('holdout-end')),
      ['/api/v1/holdout/h-q4', { method: 'PUT', json: { is_active: false } }],
      HOLDOUT_COPY.none,
    ],
    [
      'End, from its row',
      () => fireEvent.click(screen.getByRole('button', { name: 'End Q4 holdout' })),
      ['/api/v1/holdout/h-q4', { method: 'PUT', json: { is_active: false } }],
      HOLDOUT_COPY.none,
    ],
    [
      'Activate now, on a new holdout',
      () => {
        fireEvent.click(screen.getByTestId('new-holdout-btn'));
        fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'Q2 holdout' } });
        fireEvent.click(screen.getByTestId('holdout-activate-now'));
        fireEvent.click(screen.getByTestId('holdout-submit'));
      },
      [ACTIVE, { method: 'POST', json: { name: 'Q2 holdout', holdout_percentage: 5, is_active: true } }],
      'Q2 holdout: keeps 5% of new users out of every experiment',
    ],
  ] as [string, () => void, unknown[], string][])(
    '%s: once it lands, focus is on the active holdout heading',
    async (_, ask, write, shown) => {
      serve();
      await openAdmin();
      ask();
      fireEvent.click(screen.getByTestId('holdout-confirm-yes'));
      // The confirmation has gone; focus does not fall to the page.
      await waitFor(() => expect(activeHeading()).toHaveFocus());
      expect(writes()).toEqual([write]);
      expect(reads(ALL)).toHaveLength(2);
      // The heading leads the section that now shows the change.
      expect(activeHeading().parentElement).toHaveTextContent(shown);
      expect(activeHeading()).toHaveAttribute('tabindex', '-1');
    },
  );

  it('a refused activation leaves focus on the error, never the heading', async () => {
    // The re-read after the refusal answers only when the test says so, as a
    // network answers after the page has drawn the error: no timer decides
    // which comes first.
    let answerReread: () => void = () => undefined;
    serve({
      all: () => {
        const list = { items: server, total: server.length };
        if (reads(ALL).length === 1) return list;
        return new Promise((resolve) => {
          answerReread = () => resolve(list);
        });
      },
      update: () => {
        throw apiError(409, 'PLANTED-7f3');
      },
    });
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: 'Activate Q1 holdout' }));
    fireEvent.click(screen.getByTestId('holdout-confirm-yes'));
    // The error is shown and focused while the re-read is still out.
    await waitFor(() => expect(screen.getByTestId('holdout-action-error')).toHaveFocus());
    expect(reads(ALL)).toHaveLength(2);
    await act(async () => answerReread());
    // The re-read is done and the buttons are back: focus stayed on the error.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Activate Q1 holdout' })).not.toBeDisabled());
    expect(screen.getByTestId('holdout-action-error')).toHaveFocus();
    expect(activeHeading()).not.toHaveFocus();
  });

  it('Cancel and Escape send nothing, and return focus to the button that asked', async () => {
    serve();
    await openAdmin();
    fireEvent.click(screen.getByTestId('holdout-end'));
    fireEvent.click(screen.getByTestId('holdout-confirm-cancel'));
    expect(document.activeElement).toBe(screen.getByTestId('holdout-end'));
    const activate = screen.getByRole('button', { name: 'Activate Q1 holdout' });
    fireEvent.click(activate);
    fireEvent.keyDown(screen.getByTestId('holdout-confirm'), { key: 'Escape' });
    expect(screen.queryByTestId('holdout-confirm')).toBeNull();
    expect(document.activeElement).toBe(activate);
    expect(writes()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Edit: names exact and case-sensitive; the size locks once active (QA #16)
// ---------------------------------------------------------------------------

describe('editing', () => {
  it('a holdout never activated: its size can change, and only what changed is sent', async () => {
    serve();
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: 'Edit Q1 holdout' }));
    expect(screen.getByTestId('holdout-size')).toHaveValue(3);
    fireEvent.change(screen.getByTestId('holdout-size'), { target: { value: '2' } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    await waitFor(() => expect(screen.queryByTestId('holdout-form')).toBeNull());
    expect(writes()).toEqual([['/api/v1/holdout/h-q1', { method: 'PUT', json: { holdout_percentage: 2 } }]]);
    expect(screen.queryByTestId('holdout-activate-now')).toBeNull();
  });

  it.each(['Q4 holdout', 'Old holdout'])('%s has been active: its size is text, never a field, and is never sent', async (name) => {
    serve();
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: `Edit ${name}` }));
    expect(screen.queryByTestId('holdout-size')).toBeNull();
    expect(screen.getByTestId('holdout-size-locked')).toHaveTextContent(
      'Fixed once a holdout has been active. Create a new holdout to change it.',
    );
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: `${name} renamed` } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect((writes()[0][1] as ApiFetchOptions).json).toEqual({ name: `${name} renamed` });
  });

  it('refuses a name another holdout has, exactly, and accepts it in another case', async () => {
    serve();
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: 'Edit Q1 holdout' }));
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'Old holdout' } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    expect(screen.getByTestId('holdout-name-error')).toHaveTextContent(HOLDOUT_COPY.nameTaken);
    expect(writes()).toEqual([]);
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'old holdout' } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    await waitFor(() => expect(writes()).toEqual([['/api/v1/holdout/h-q1', { method: 'PUT', json: { name: 'old holdout' } }]]));
  });

  it('keeping its own name is not a clash, and an unchanged form sends nothing', async () => {
    serve();
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: 'Edit Q4 holdout' }));
    fireEvent.click(screen.getByTestId('holdout-submit'));
    await waitFor(() => expect(screen.queryByTestId('holdout-form')).toBeNull());
    expect(writes()).toEqual([]);
  });

  it.each([
    [409, HOLDOUT_COPY.nameTaken],
    [422, HOLDOUT_COPY.changed],
  ])('a rename answered %i shows fixed copy, never the detail, and reads the list again', async (status, copy) => {
    serve({
      update: () => {
        // Another ADMIN took the name first: the server now holds a holdout this page has not read.
        server = [...server, holdout('h-other', 'Brand new')];
        throw apiError(status, 'PLANTED-7f3');
      },
    });
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: 'Edit Q1 holdout' }));
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'Brand new' } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    expect(await screen.findByTestId('holdout-form-error')).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain('PLANTED');
    await waitFor(() => expect(reads(ALL)).toHaveLength(2));
    await waitFor(() => expect(statuses()).toHaveLength(4));
    // The form stays open with what was typed, and the name it now knows is taken is refused before any request.
    await waitFor(() => expect(screen.getByTestId('holdout-submit')).not.toBeDisabled());
    expect(screen.getByTestId('holdout-name')).toHaveValue('Brand new');
    fireEvent.click(screen.getByTestId('holdout-submit'));
    expect(screen.getByTestId('holdout-name-error')).toHaveTextContent(HOLDOUT_COPY.nameTaken);
    expect(writes()).toHaveLength(1);
  });

  it('a holdout activated while its form was open: after the 422 its size is fixed text at the stored size, and is never sent', async () => {
    let raced = false;
    serve({
      update: (path, options) => {
        if (!raced) {
          raced = true;
          // Another ADMIN activated Q1 after this page loaded, so its size is now locked.
          server = server.map((h) =>
            h.id === 'h-q1'
              ? { ...h, is_active: true, activated_at: '2026-10-30T08:00:00' }
              : h.is_active
                ? { ...h, is_active: false, deactivated_at: '2026-10-30T08:00:00' }
                : h,
          );
          throw apiError(422, 'PLANTED-7f3 The percentage of a holdout that has been active cannot change.');
        }
        const body = options.json as Partial<Holdout>;
        server = server.map((h) => (h.id === 'h-q1' ? { ...h, ...body } : h));
        return server.find((h) => h.id === path.split('/').pop());
      },
    });
    await openAdmin();
    fireEvent.click(screen.getByRole('button', { name: 'Edit Q1 holdout' }));
    fireEvent.change(screen.getByTestId('holdout-name'), { target: { value: 'Q1 renamed' } });
    fireEvent.change(screen.getByTestId('holdout-size'), { target: { value: '2' } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    expect(await screen.findByTestId('holdout-form-error')).toHaveTextContent(HOLDOUT_COPY.changed);
    await waitFor(() => expect(reads(ALL)).toHaveLength(2));
    // The form now edits what the server holds: the size is no longer a field, and reads 3%, not the 2 typed.
    await waitFor(() => expect(screen.queryByTestId('holdout-size')).toBeNull());
    expect(screen.getByTestId('holdout-size-locked')).toHaveTextContent(
      /^Size \(% of new users\)3%Fixed once a holdout has been active\. Create a new holdout to change it\.$/,
    );
    expect(screen.getByTestId('holdout-name')).toHaveValue('Q1 renamed');
    expect(document.body.textContent).not.toContain('PLANTED');
    await waitFor(() => expect(screen.getByTestId('holdout-submit')).not.toBeDisabled());
    fireEvent.click(screen.getByTestId('holdout-submit'));
    await waitFor(() => expect(screen.queryByTestId('holdout-form')).toBeNull());
    expect(writes()).toEqual([
      ['/api/v1/holdout/h-q1', { method: 'PUT', json: { name: 'Q1 renamed', holdout_percentage: 2 } }],
      ['/api/v1/holdout/h-q1', { method: 'PUT', json: { name: 'Q1 renamed' } }],
    ]);
  });
});

// ---------------------------------------------------------------------------
// Results (beta): query {metric}; each unavailable_reason in fixed copy
// ---------------------------------------------------------------------------

describe('results (beta)', () => {
  async function showFor(metric: string, open = () => fireEvent.click(screen.getByTestId('holdout-results-open'))) {
    await openAdmin();
    open();
    fireEvent.change(screen.getByTestId('holdout-results-metric'), { target: { value: metric } });
    fireEvent.click(screen.getByTestId('holdout-results-show'));
  }

  it('sends query {metric}, then shows each group, the difference, its interval, p and the verdict as a word', async () => {
    serve();
    await showFor('purchase');
    const table = await screen.findByTestId('holdout-results-table');
    const rows = Array.from(table.querySelectorAll('tbody tr')).map((tr) =>
      Array.from(tr.children).map((cell) => cell.textContent),
    );
    expect(rows).toEqual([
      ['In holdout', '400', '40', '10.0%'],
      ['Not in holdout', '7600', '912', '12.0%'],
    ]);
    const difference = screen.getByTestId('holdout-results-difference');
    expect(difference).toHaveTextContent('Difference (not in holdout minus in holdout)+2.0 percentage points');
    expect(difference).toHaveTextContent('95% interval−1.1 to +5.1 percentage points');
    expect(difference).toHaveTextContent('p-value0.031');
    expect(screen.getByTestId('holdout-results-significance')).toHaveTextContent(/^Significant$/);
    expect(screen.getByTestId('holdout-results')).toHaveTextContent('Results (beta): Q4 holdout');
    expect(mockedApiFetch.mock.calls.filter(([p]) => String(p).endsWith('/results'))).toEqual([
      ['/api/v1/holdout/h-q4/results', { query: { metric: 'purchase' } }],
    ]);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('says "Not significant" as a word', async () => {
    serve({
      results: () => ({ ...RESULTS, difference: { ...RESULTS.difference, p_value: 0.4, is_significant: false } }),
    });
    await showFor('purchase');
    expect(await screen.findByTestId('holdout-results-significance')).toHaveTextContent(/^Not significant$/);
  });

  it.each([
    [
      'activated_before_measurement',
      'This holdout was active before this release recorded who it kept out, so it cannot be measured. End it and create a new one.',
    ],
    ['not_activated', 'It has not been activated yet.'],
    ['too_few_users', 'Each group needs 100 users; one has fewer so far.'],
    ['no_events', 'Neither group has sent a “signup” event. Check the name; it must match exactly.'],
  ])('%s reads as the page\'s own sentence, never the message', async (reason, copy) => {
    serve({
      results: () => ({
        ...RESULTS,
        metric: 'signup',
        unavailable_reason: reason,
        message: 'PLANTED-MESSAGE',
        difference: null,
        groups: reason === 'too_few_users' ? RESULTS.groups.map((g) => ({ ...g, users: 12 })) : [],
      }),
    });
    await showFor('signup');
    expect(await screen.findByTestId('holdout-results-unavailable')).toHaveTextContent(copy);
    expect(screen.queryByTestId('holdout-results-difference')).toBeNull();
    if (reason === 'too_few_users') expect(screen.getByTestId('holdout-results-table')).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('an empty event name is refused before any request', async () => {
    serve();
    await showFor('   ');
    expect(screen.getByText(HOLDOUT_COPY.eventRequired)).toBeInTheDocument();
    expect(screen.getByTestId('holdout-results-metric')).toHaveAttribute('aria-invalid', 'true');
    expect(mockedApiFetch.mock.calls.filter(([p]) => String(p).endsWith('/results'))).toEqual([]);
  });

  it.each([
    [404, HOLDOUT_COPY.notFound],
    [500, HOLDOUT_COPY.resultsFailed],
    [0, UNREACHABLE],
  ])('results answered %i read as fixed copy', async (status, copy) => {
    serve({
      results: () => {
        throw new ApiError({ status, detail: 'PLANTED-7f3' });
      },
    });
    await showFor('purchase');
    expect(await screen.findByTestId('holdout-results-error')).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('an ended holdout keeps its results; one never activated offers none', async () => {
    serve();
    await openAdmin();
    expect(within(row('Q1 holdout')).queryByTestId('holdout-row-results')).toBeNull();
    fireEvent.click(within(row('Old holdout')).getByRole('button', { name: 'Results (beta) for Old holdout' }));
    expect(screen.getByTestId('holdout-results')).toHaveTextContent('Results (beta): Old holdout');
    fireEvent.change(screen.getByTestId('holdout-results-metric'), { target: { value: 'purchase' } });
    fireEvent.click(screen.getByTestId('holdout-results-show'));
    await screen.findByTestId('holdout-results-table');
    expect(mockedApiFetch.mock.calls.filter(([p]) => String(p).endsWith('/results'))).toEqual([
      ['/api/v1/holdout/h-old/results', { query: { metric: 'purchase' } }],
    ]);
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

  it('an ADMIN with the table, the new-holdout form refused, and a confirmation', async () => {
    serve();
    const { container } = render(<HoldoutsPage />);
    await screen.findByTestId('holdouts-table');
    await noViolations(container);
    fireEvent.click(screen.getByTestId('new-holdout-btn'));
    fireEvent.change(screen.getByTestId('holdout-size'), { target: { value: '30' } });
    fireEvent.click(screen.getByTestId('holdout-submit'));
    await screen.findByTestId('holdout-size-error');
    await noViolations(container);
    fireEvent.click(screen.getByTestId('holdout-cancel'));
    fireEvent.click(screen.getByTestId('holdout-end'));
    await noViolations(container);
  });

  it('results, and an unavailable reason', async () => {
    serve();
    const { container } = render(<HoldoutsPage />);
    await screen.findByTestId('holdouts-table');
    fireEvent.click(screen.getByTestId('holdout-results-open'));
    fireEvent.change(screen.getByTestId('holdout-results-metric'), { target: { value: 'purchase' } });
    fireEvent.click(screen.getByTestId('holdout-results-show'));
    await screen.findByTestId('holdout-results-table');
    await noViolations(container);
  });

  it.each([
    ['a DEVELOPER', () => signIn('DEVELOPER'), 'holdouts-page'],
    ['a VIEWER', () => signIn('VIEWER'), 'holdouts-refused'],
  ] as [string, () => void, string][])('%s', async (_, sign, id) => {
    sign();
    serve();
    const { container } = render(<HoldoutsPage />);
    await screen.findByTestId(id);
    await noViolations(container);
  });
});
