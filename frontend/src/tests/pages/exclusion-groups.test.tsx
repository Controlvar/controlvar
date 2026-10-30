/**
 * The exclusion-group list page (#1455; UX SCREEN 3a, the architect's S4).
 *
 * `GET` and `POST /api/v1/mutual-exclusion-groups` check EXPERIMENT CREATE:
 * ADMIN, DEVELOPER and superusers. So ANALYST and VIEWER see the refused
 * notice and the page sends them NO request (QA #1, #2). Every user here is
 * RESOLVED and, but for the superuser cells, not a superuser: a null user
 * would read as "may", and a superuser passes every check.
 *
 * The capability table's witness, `exclusion-groups-page`, marks only the
 * loaded list (QA #27; the v1.1 delta's N4).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import ExclusionGroupsPage from '@/pages/experiments/exclusion-groups/index';
import { ApiError, apiFetch } from '@/services/api';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';
import { GROUP_COPY } from '@/utils/exclusionGroups';
import { Route, apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments/exclusion-groups', asPath: '/experiments/exclusion-groups' });
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

const LIST = '/api/v1/mutual-exclusion-groups';
const WITNESS = 'exclusion-groups-page';

const GROUPS = [
  {
    id: 'grp-1',
    name: 'Checkout',
    description: 'PLANTED-DESCRIPTION',
    traffic_allocation: 0.5,
    status: 'active',
    owner_id: 'PLANTED-OWNER',
    experiments: [
      { id: 'exp-1', name: 'Button colour', status: 'active' },
      { id: 'exp-2', name: 'Copy', status: 'draft' },
    ],
    created_at: '2026-10-01T10:00:00',
    updated_at: '2026-10-01T10:00:00',
  },
  {
    id: 'grp-2',
    name: 'Old pricing',
    description: null,
    traffic_allocation: 1,
    status: 'archived',
    owner_id: null,
    experiments: [],
    created_at: '2026-09-01T10:00:00',
    updated_at: '2026-09-01T10:00:00',
  },
];

type Reply = () => unknown;
const pending: Reply = () => new Promise<never>(() => undefined);

function serve({ list = () => ({ items: GROUPS, total: GROUPS.length }), create }: { list?: Reply; create?: Reply } = {}) {
  const routes: Route[] = [{ path: LIST, handler: list }];
  if (create) routes.push({ method: 'POST', path: LIST, handler: create });
  mockedApiFetch.mockImplementation(routedApi(routes) as unknown as typeof apiFetch);
}

function signIn(role: string, superuser = false) {
  mockAuth.mockReturnValue({
    user: { id: 'u-1', email: `${role.toLowerCase()}@demo.com`, username: role.toLowerCase(), role, is_superuser: superuser },
    status: 'authenticated',
  });
}

const calls = () => mockedApiFetch.mock.calls.map(([path, options]) => [String(path), options ?? null]);

const ADMITTED: [string, string, boolean][] = [
  ['ADMIN', 'ADMIN', false],
  ['DEVELOPER', 'DEVELOPER', false],
  ['a superuser VIEWER', 'VIEWER', true],
];
const REFUSED_ROLES = ['ANALYST', 'VIEWER'] as const;

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockRouter.push.mockClear();
  signIn('DEVELOPER');
});

async function openForm() {
  fireEvent.click(await screen.findByTestId('new-exclusion-group-btn'));
  return screen.getByTestId('exclusion-group-form');
}

function fill(name: string, share?: string, description?: string) {
  fireEvent.change(screen.getByTestId('exclusion-group-name'), { target: { value: name } });
  if (share !== undefined) fireEvent.change(screen.getByTestId('exclusion-group-share'), { target: { value: share } });
  if (description !== undefined) {
    fireEvent.change(screen.getByTestId('exclusion-group-description'), { target: { value: description } });
  }
}

const posts = () => mockedApiFetch.mock.calls.filter(([, options]) => (options as { method?: string })?.method === 'POST');

// ---------------------------------------------------------------------------
// QA #1, #2, #27: who sees what, and exactly the requests each sends
// ---------------------------------------------------------------------------

describe('a role the API admits (ADMIN, DEVELOPER, a superuser)', () => {
  it.each(ADMITTED)('%s sees each group, its share as a percent, its count and its status as a word, after one read', async (_, role, superuser) => {
    signIn(role, superuser);
    serve();
    render(<ExclusionGroupsPage />);
    // The capability table's witness, written out: test_dashboard_and_api.py reads it here.
    const page = await screen.findByTestId('exclusion-groups-page');
    const rows = Array.from(page.querySelectorAll('tbody tr')).map((tr) =>
      Array.from(tr.querySelectorAll('td')).map((td) => td.textContent),
    );
    expect(rows).toEqual([
      ['Checkout', '50%', '2', 'Active'],
      ['Old pricing', '100%', '0', 'Archived'],
    ]);
    expect(screen.getAllByTestId('exclusion-group-link').map((a) => a.getAttribute('href'))).toEqual([
      '/experiments/exclusion-groups/grp-1',
      '/experiments/exclusion-groups/grp-2',
    ]);
    expect(Array.from(page.querySelectorAll('th')).map((th) => [th.textContent, th.getAttribute('scope')])).toEqual([
      ['Name', 'col'],
      ['Share of traffic', 'col'],
      ['Experiments', 'col'],
      ['Status', 'col'],
    ]);
    expect(screen.getByTestId('new-exclusion-group-btn')).toHaveTextContent(/^New group$/);
    // No options and no query: exactly the path, once.
    expect(calls()).toEqual([[LIST, null]]);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('shows the empty state, still under the witness', async () => {
    serve({ list: () => ({ items: [], total: 0 }) });
    render(<ExclusionGroupsPage />);
    await screen.findByTestId(WITNESS);
    expect(screen.getByTestId('exclusion-groups-empty')).toHaveTextContent(GROUP_COPY.empty);
    expect(GROUP_COPY.empty).toBe(
      'No exclusion groups yet. Create one so experiments that would interfere never share a user.',
    );
  });
});

describe('a role the API refuses (ANALYST, VIEWER)', () => {
  it.each(REFUSED_ROLES)('%s is told who can, sends no request, and sees no witness', async (role) => {
    signIn(role);
    serve({ list: () => ({ items: [{ ...GROUPS[0], name: 'PLANTED-GROUP' }], total: 1 }) });
    render(<ExclusionGroupsPage />);
    expect(screen.getByTestId('exclusion-groups-refused')).toHaveTextContent(
      /^Exclusion groups are managed by the ADMIN and DEVELOPER roles\.$/,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockedApiFetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId(WITNESS)).toBeNull();
    expect(screen.queryByTestId('new-exclusion-group-btn')).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

describe('the states the witness never marks (QA #27)', () => {
  it('a user not yet resolved sees "Loading…" and sends nothing', async () => {
    mockAuth.mockReturnValue({ user: null, status: 'loading' });
    serve();
    render(<ExclusionGroupsPage />);
    expect(screen.getByTestId('exclusion-groups-loading')).toHaveTextContent(/^Loading…$/);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockedApiFetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId(WITNESS)).toBeNull();
    expect(screen.queryByTestId('exclusion-groups-refused')).toBeNull();
  });

  it('while the list is loading', async () => {
    serve({ list: pending });
    render(<ExclusionGroupsPage />);
    expect(await screen.findByTestId('exclusion-groups-loading')).toHaveTextContent('Loading…');
    expect(screen.queryByTestId(WITNESS)).toBeNull();
  });

  it.each([404, 500])('a list answer of %i reads as the fixed load failure', async (status) => {
    serve({
      list: () => {
        throw apiError(status, 'PLANTED-7f3');
      },
    });
    render(<ExclusionGroupsPage />);
    expect(await screen.findByTestId('exclusion-groups-error')).toHaveTextContent(
      /^Could not load the exclusion groups\. Reload the page to try again\.$/,
    );
    expect(screen.queryByTestId(WITNESS)).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('a 403 that arrives anyway reads as the refused notice', async () => {
    serve({
      list: () => {
        throw apiError(403, 'PLANTED-7f3');
      },
    });
    render(<ExclusionGroupsPage />);
    expect(await screen.findByTestId('exclusion-groups-refused')).toHaveTextContent(GROUP_COPY.refused);
    expect(screen.queryByTestId(WITNESS)).toBeNull();
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('a server it cannot reach reads as the fixed copy', async () => {
    serve({
      list: () => {
        throw new ApiError({ status: 0, detail: 'PLANTED-7f3' });
      },
    });
    render(<ExclusionGroupsPage />);
    expect(await screen.findByTestId('exclusion-groups-error')).toHaveTextContent(UNREACHABLE);
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});

// ---------------------------------------------------------------------------
// QA #17, PE 4: what "Create group" sends, and what it refuses before sending
// ---------------------------------------------------------------------------

describe('New group', () => {
  it('sends exactly the name and the share as a fraction: 50% is 0.5', async () => {
    serve({ create: () => ({ ...GROUPS[0], id: 'grp-new', name: 'Search' }) });
    render(<ExclusionGroupsPage />);
    await openForm();
    fill('Search', '50');
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exclusion-groups/grp-new'));
    expect(posts()).toEqual([[LIST, { method: 'POST', json: { name: 'Search', traffic_allocation: 0.5 } }]]);
  });

  it('defaults to 100% (the fraction 1), and sends a description only when one is given', async () => {
    serve({ create: () => ({ ...GROUPS[0], id: 'grp-new' }) });
    render(<ExclusionGroupsPage />);
    await openForm();
    expect(screen.getByTestId('exclusion-group-share')).toHaveValue(100);
    fill('Search', undefined, 'Search ranking tests');
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalled());
    expect(posts()).toEqual([
      [LIST, { method: 'POST', json: { name: 'Search', description: 'Search ranking tests', traffic_allocation: 1 } }],
    ]);
  });

  it('says what the share means, tied to its field', async () => {
    serve();
    render(<ExclusionGroupsPage />);
    await openForm();
    const help = screen.getByTestId('exclusion-group-share-help');
    expect(help).toHaveTextContent(
      "Users outside this share get none of the group's experiments. Users inside it go to at most one, divided evenly among the group's running experiments. Users already in an experiment keep it.",
    );
    expect(screen.getByTestId('exclusion-group-share').getAttribute('aria-describedby')).toContain(help.id);
    expect(screen.getByLabelText('Share of traffic (%)')).toBe(screen.getByTestId('exclusion-group-share'));
    expect(screen.getByLabelText('Name')).toBe(screen.getByTestId('exclusion-group-name'));
    expect(screen.getByLabelText('Description (optional)')).toBe(screen.getByTestId('exclusion-group-description'));
  });

  it('refuses a name another group has, exactly, before any request', async () => {
    serve({ create: () => GROUPS[0] });
    render(<ExclusionGroupsPage />);
    await openForm();
    fill('Checkout', '50');
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    expect(screen.getByTestId('exclusion-group-name-error')).toHaveTextContent(/^A group with this name already exists\.$/);
    expect(screen.getByTestId('exclusion-group-form-problem')).toHaveTextContent(GROUP_COPY.nameTaken);
    const name = screen.getByTestId('exclusion-group-name');
    expect(name).toHaveAttribute('aria-invalid', 'true');
    expect(name.getAttribute('aria-describedby')).toBe(screen.getByTestId('exclusion-group-name-error').id);
    expect(document.activeElement).toBe(name);
    expect(posts()).toEqual([]);
  });

  it('accepts a name that differs only in case: "checkout" is another name, as the API holds', async () => {
    serve({ create: () => ({ ...GROUPS[0], id: 'grp-lower', name: 'checkout' }) });
    render(<ExclusionGroupsPage />);
    await openForm();
    fill('checkout', '50');
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0]).toEqual([LIST, { method: 'POST', json: { name: 'checkout', traffic_allocation: 0.5 } }]);
  });

  it.each(['', '   '])('refuses the empty name %p before any request', async (value) => {
    serve();
    render(<ExclusionGroupsPage />);
    await openForm();
    fill(value, '50');
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    expect(screen.getByTestId('exclusion-group-name-error')).toHaveTextContent(/^Enter a name\.$/);
    expect(posts()).toEqual([]);
  });

  it.each(['101', '-5', '50.5', ''])('refuses the share %p before any request', async (share) => {
    serve();
    render(<ExclusionGroupsPage />);
    await openForm();
    fill('Search', share);
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    expect(screen.getByTestId('exclusion-group-share-error')).toHaveTextContent(
      /^Enter a whole number from 0 to 100\.$/,
    );
    const field = screen.getByTestId('exclusion-group-share');
    expect(field).toHaveAttribute('aria-invalid', 'true');
    expect(document.activeElement).toBe(field);
    expect(posts()).toEqual([]);
  });

  it.each([
    [409, GROUP_COPY.nameTaken],
    [403, GROUP_COPY.refused],
    [422, GROUP_COPY.saveFailed],
    [500, GROUP_COPY.saveFailed],
    [0, UNREACHABLE],
  ])('a create answered %i shows fixed copy, never the detail, and stays on the page', async (status, copy) => {
    serve({
      create: () => {
        throw new ApiError({ status, detail: 'PLANTED-7f3 duplicate key', message: 'PLANTED-message' });
      },
    });
    render(<ExclusionGroupsPage />);
    await openForm();
    fill('Search', '50');
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    const alert = await screen.findByTestId('exclusion-group-form-error');
    expect(alert).toHaveTextContent(copy);
    expect(alert).toHaveAttribute('role', 'alert');
    await waitFor(() => expect(document.activeElement).toBe(alert));
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(mockRouter.push).not.toHaveBeenCalled();
  });

  it('Cancel closes the form, sends nothing and returns focus to New group', async () => {
    serve();
    render(<ExclusionGroupsPage />);
    await openForm();
    expect(document.activeElement).toBe(screen.getByTestId('exclusion-group-name'));
    fireEvent.click(screen.getByTestId('exclusion-group-cancel'));
    expect(screen.queryByTestId('exclusion-group-form')).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('new-exclusion-group-btn')));
    expect(posts()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// QA #28: accessibility in each state (jsdom: colour contrast is not computable)
// ---------------------------------------------------------------------------

describe('accessibility (axe-core in jsdom)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  // The page's own markup; the landmarks (main, nav) are the app shell's.
  async function noViolations(container: HTMLElement) {
    const result = await axe.run(container, axeOptions);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  }

  it('the list', async () => {
    serve();
    const { container } = render(<ExclusionGroupsPage />);
    await screen.findByTestId(WITNESS);
    await noViolations(container);
  });

  it('the empty list', async () => {
    serve({ list: () => ({ items: [], total: 0 }) });
    const { container } = render(<ExclusionGroupsPage />);
    await screen.findByTestId('exclusion-groups-empty');
    await noViolations(container);
  });

  it('the form with a refused name and share', async () => {
    serve();
    const { container } = render(<ExclusionGroupsPage />);
    await openForm();
    fill('Checkout', '500');
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    await screen.findByTestId('exclusion-group-name-error');
    await noViolations(container);
  });

  it('the form with a failed create', async () => {
    serve({
      create: () => {
        throw apiError(500, 'x');
      },
    });
    const { container } = render(<ExclusionGroupsPage />);
    await openForm();
    fill('Search', '50');
    fireEvent.click(screen.getByTestId('exclusion-group-submit'));
    await screen.findByTestId('exclusion-group-form-error');
    await noViolations(container);
  });

  it('the refused notice', async () => {
    signIn('VIEWER');
    serve();
    const { container } = render(<ExclusionGroupsPage />);
    await screen.findByTestId('exclusion-groups-refused');
    await noViolations(container);
  });

  it('the load failure', async () => {
    serve({
      list: () => {
        throw apiError(500, 'x');
      },
    });
    const { container } = render(<ExclusionGroupsPage />);
    await screen.findByTestId('exclusion-groups-error');
    await noViolations(container);
  });
});
