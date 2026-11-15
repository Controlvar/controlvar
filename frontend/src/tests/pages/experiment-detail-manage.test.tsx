/**
 * The experiment page's "manage" controls (#442): clone any experiment, and
 * edit the details of, or delete, a draft.
 *
 * Every case renders the page module itself, with `apiFetch` mocked and a
 * RESOLVED user (a null user is offered everything, so a role case rendered
 * without one would pass for the wrong reason).
 */
import React from 'react';
import fs from 'fs';
import path from 'path';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { ApiError, apiFetch } from '@/services/api';
import { Experiment, ExperimentStatus } from '@/types/experiments';
import {
  DELETE_CONFIRM,
  ERROR_COPY,
  NAME_REQUIRED,
  SAVED,
  UNREACHABLE,
} from '@/components/experiments/ExperimentManageSection';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

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

const BASE: Experiment = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: 'Blue vs green CTA',
  hypothesis: 'Blue converts better',
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

const experiment = (overrides: Partial<Experiment> = {}): Experiment => ({ ...BASE, ...overrides });

const as = (role: string, extra: Record<string, unknown> = {}) => ({
  id: 'user-1',
  email: `${role.toLowerCase()}@demo.com`,
  username: role.toLowerCase(),
  role,
  is_superuser: false,
  ...extra,
});

function signIn(user: Record<string, unknown>) {
  mockUseAuth.mockReturnValue({ user, status: 'authenticated' });
}

/** Text the server might send, which must never reach the page. */
const PLANTED = 'Traceback (most recent call last): sqlalchemy.exc.PLANTED-7f3';

interface Handlers {
  put?: (body: unknown) => unknown;
  clone?: () => unknown;
  del?: () => unknown;
}

function install(current: Experiment, handlers: Handlers = {}) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => current },
      {
        method: 'PUT',
        path: '/api/v1/experiments/exp-1',
        handler: (_p, o) =>
          handlers.put ? handlers.put(o.json) : { ...current, ...(o.json as Partial<Experiment>) },
      },
      {
        method: 'POST',
        path: '/api/v1/experiments/exp-1/clone',
        handler: () =>
          handlers.clone
            ? handlers.clone()
            : { ...current, id: 'exp-2', name: `Copy of ${current.name}`, status: 'draft' },
      },
      {
        method: 'DELETE',
        path: '/api/v1/experiments/exp-1',
        handler: () => (handlers.del ? handlers.del() : undefined),
      },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderPage(current: Experiment = experiment(), handlers: Handlers = {}) {
  install(current, handlers);
  const view = render(<ExperimentDetailPage />);
  await screen.findByTestId('experiment-detail');
  return view;
}

const callsTo = (method: string) =>
  mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === method);

const MANAGE_IDS = ['experiment-clone', 'experiment-edit-details', 'experiment-delete'];
const shownControls = () => MANAGE_IDS.filter((id) => screen.queryByTestId(id) !== null);

let confirmSpy: jest.SpyInstance;

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockRouter.push.mockClear();
  signIn(as('ADMIN'));
  confirmSpy = jest.spyOn(window, 'confirm').mockImplementation(() => true);
});

afterEach(() => {
  // The confirmation is in the page; the browser's own dialog is never used.
  expect(confirmSpy).not.toHaveBeenCalled();
  confirmSpy.mockRestore();
});

// ---------------------------------------------------------------------------
// Which controls, for which role and status (QA groups C and E)
// ---------------------------------------------------------------------------

describe('which controls are offered', () => {
  const STATUSES: ExperimentStatus[] = ['draft', 'active', 'paused', 'completed', 'archived'];
  const WRITER = { draft: MANAGE_IDS, other: ['experiment-clone'] };

  const table: [string, Record<string, unknown>, ExperimentStatus, string[]][] = [];
  for (const status of STATUSES) {
    const writer = status === 'draft' ? WRITER.draft : WRITER.other;
    table.push(['ADMIN', as('ADMIN'), status, writer]);
    table.push(['DEVELOPER', as('DEVELOPER'), status, writer]);
    table.push(['ANALYST', as('ANALYST'), status, []]);
    table.push(['VIEWER', as('VIEWER'), status, []]);
    table.push(['superuser VIEWER', as('VIEWER', { is_superuser: true }), status, writer]);
  }

  it.each(table)('%s on a %s experiment it does not own sees exactly %j', async (_n, user, status, expected) => {
    signIn(user);
    await renderPage(experiment({ status }));
    expect(shownControls()).toEqual(expected);
    if (expected.length === 0) {
      expect(screen.queryByTestId('experiment-manage')).toBeNull();
    }
  });

  it('the controls sit outside experiment-actions, whose buttons are unchanged', async () => {
    await renderPage(experiment({ status: 'draft' }));
    const actions = screen.getByTestId('experiment-actions');
    // Draft: Start only, as before this region existed.
    expect(Array.from(actions.querySelectorAll('button')).map((b) => b.dataset.testid)).toEqual(['action-start']);
    const manage = screen.getByTestId('experiment-manage');
    expect(actions.contains(manage)).toBe(false);
    expect(manage.contains(actions)).toBe(false);
    for (const id of MANAGE_IDS) expect(within(actions).queryByTestId(id)).toBeNull();
  });

  it('an archived experiment still has no button in experiment-actions, and Clone outside it', async () => {
    await renderPage(experiment({ status: 'archived' }));
    expect(screen.getByTestId('experiment-actions').querySelectorAll('button')).toHaveLength(0);
    expect(screen.getByTestId('experiment-clone')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Clone (A2)
// ---------------------------------------------------------------------------

describe('Clone', () => {
  it('POSTs /clone with no body and goes to the NEW experiment', async () => {
    await renderPage(experiment({ status: 'active' }));
    fireEvent.click(screen.getByTestId('experiment-clone'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-2'));
    expect(callsTo('POST')).toEqual([['/api/v1/experiments/exp-1/clone', { method: 'POST' }]]);
    expect(mockRouter.push).not.toHaveBeenCalledWith('/experiments/exp-1');
  });

  it('a DEVELOPER clones an experiment someone else owns', async () => {
    signIn(as('DEVELOPER'));
    await renderPage(experiment({ status: 'completed' }));
    fireEvent.click(screen.getByTestId('experiment-clone'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-2'));
  });
});

// ---------------------------------------------------------------------------
// Edit details: the PUT body is exactly the changed subset (A2, EM 7)
// ---------------------------------------------------------------------------

describe('Edit details', () => {
  async function openEdit(current = experiment()) {
    await renderPage(current);
    fireEvent.click(screen.getByTestId('experiment-edit-details'));
    return screen.getByTestId('manage-edit-form');
  }

  const type = (id: string, value: string) =>
    fireEvent.change(screen.getByTestId(id), { target: { value } });

  const save = () => fireEvent.click(screen.getByTestId('manage-edit-save'));

  const puts = () => callsTo('PUT');

  it.each<[string, () => void, Record<string, unknown>]>([
    ['the name only', () => type('manage-edit-name', 'Checkout CTA'), { name: 'Checkout CTA' }],
    ['the description only', () => type('manage-edit-description', 'Green vs blue'), { description: 'Green vs blue' }],
    ['the hypothesis only', () => type('manage-edit-hypothesis', 'Green wins'), { hypothesis: 'Green wins' }],
    ['a cleared hypothesis', () => type('manage-edit-hypothesis', '   '), { hypothesis: null }],
    [
      'all three',
      () => {
        type('manage-edit-name', '  New name  ');
        type('manage-edit-description', 'D');
        type('manage-edit-hypothesis', 'H');
      },
      { name: 'New name', description: 'D', hypothesis: 'H' },
    ],
  ])('changing %s sends exactly that', async (_n, change, body) => {
    await openEdit();
    change();
    save();
    await screen.findByTestId('manage-saved');
    expect(puts()).toEqual([['/api/v1/experiments/exp-1', { method: 'PUT', json: body }]]);
  });

  it('never sends variants, metrics or any other field', async () => {
    await openEdit();
    type('manage-edit-name', 'Renamed');
    save();
    await screen.findByTestId('manage-saved');
    const json = puts()[0][1]?.json as Record<string, unknown>;
    expect(Object.keys(json)).toEqual(['name']);
    expect(json).not.toHaveProperty('variants');
    expect(json).not.toHaveProperty('metrics');
  });

  it('a description first set on a draft that had none is sent', async () => {
    await openEdit(experiment({ description: null }));
    expect(screen.getByTestId('manage-edit-description')).toHaveValue('');
    type('manage-edit-description', 'Now described');
    save();
    await screen.findByTestId('manage-saved');
    expect(puts()).toEqual([['/api/v1/experiments/exp-1', { method: 'PUT', json: { description: 'Now described' } }]]);
  });

  it('nothing changed (or only surrounding spaces) sends nothing and closes the form', async () => {
    await openEdit();
    type('manage-edit-name', '  Checkout button colour ');
    save();
    await waitFor(() => expect(screen.queryByTestId('manage-edit-form')).toBeNull());
    expect(puts()).toEqual([]);
    expect(screen.getByTestId('experiment-edit-details')).toHaveFocus();
  });

  it('an empty name is refused before sending', async () => {
    await openEdit();
    type('manage-edit-name', '   ');
    save();
    expect(screen.getByTestId('manage-edit-name-error')).toHaveTextContent(NAME_REQUIRED);
    expect(screen.getByTestId('manage-edit-name')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('manage-edit-name')).toHaveFocus();
    expect(puts()).toEqual([]);
  });

  it('the page shows the saved details from the API answer', async () => {
    await renderPage(experiment(), {
      put: () => experiment({ name: 'As the server stored it' }),
    });
    fireEvent.click(screen.getByTestId('experiment-edit-details'));
    type('manage-edit-name', 'Typed name');
    save();
    expect(await screen.findByTestId('manage-saved')).toHaveTextContent(SAVED);
    expect(screen.getByTestId('experiment-name')).toHaveTextContent('As the server stored it');
    expect(screen.queryByTestId('manage-edit-form')).toBeNull();
  });

  it('the form opens on the name, carries the limits, and Cancel returns focus', async () => {
    await openEdit();
    expect(screen.getByTestId('manage-edit-name')).toHaveFocus();
    expect(screen.getByTestId('manage-edit-name')).toHaveAttribute('maxLength', '100');
    expect(screen.getByTestId('manage-edit-description')).toHaveAttribute('maxLength', '2000');
    expect(screen.getByTestId('manage-edit-hypothesis')).toHaveAttribute('maxLength', '2000');
    fireEvent.click(screen.getByTestId('manage-edit-cancel'));
    expect(screen.queryByTestId('manage-edit-form')).toBeNull();
    expect(screen.getByTestId('experiment-edit-details')).toHaveFocus();
    expect(puts()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Delete draft: confirmation in the page (EM 7)
// ---------------------------------------------------------------------------

describe('Delete draft', () => {
  it('asks in the page, then DELETEs with the experiment key and goes to the list', async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId('experiment-delete'));
    const confirm = screen.getByTestId('manage-delete-confirm');
    expect(confirm).toHaveTextContent(DELETE_CONFIRM);
    expect(confirm).toHaveFocus();
    expect(callsTo('DELETE')).toEqual([]);

    fireEvent.click(screen.getByTestId('manage-delete-yes'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments'));
    expect(callsTo('DELETE')).toEqual([
      ['/api/v1/experiments/exp-1', { method: 'DELETE', query: { experiment_key: 'exp-1' } }],
    ]);
  });

  it('Cancel and Escape send nothing and return focus to the button', async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId('experiment-delete'));
    fireEvent.click(screen.getByTestId('manage-delete-cancel'));
    expect(screen.queryByTestId('manage-delete-confirm')).toBeNull();
    expect(screen.getByTestId('experiment-delete')).toHaveFocus();

    fireEvent.click(screen.getByTestId('experiment-delete'));
    fireEvent.keyDown(screen.getByTestId('manage-delete-confirm'), { key: 'Escape' });
    expect(screen.queryByTestId('manage-delete-confirm')).toBeNull();
    expect(screen.getByTestId('experiment-delete')).toHaveFocus();
    expect(callsTo('DELETE')).toEqual([]);
    expect(mockRouter.push).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Errors map to fixed copy (QA groups D and D')
// ---------------------------------------------------------------------------

describe('errors are fixed copy, never the server text', () => {
  const failing = (err: unknown) => () => {
    throw err;
  };

  type Case = [string, unknown, string];
  const cases = (action: 'clone' | 'edit' | 'delete', statuses: number[]): Case[] => [
    ...statuses.map((s): Case => [`${s}`, apiError(s, PLANTED), ERROR_COPY[action][s]]),
    ['500 with a JSON body', new ApiError({ status: 500, detail: PLANTED }), ERROR_COPY[action].fallback],
    ['status 0', new ApiError({ status: 0, detail: PLANTED, message: PLANTED }), UNREACHABLE],
    ['a browser exception', new TypeError(`Cannot read properties of undefined (reading 'x') ${PLANTED}`), ERROR_COPY[action].fallback],
  ];

  async function expectFixed(copy: string) {
    const alert = await screen.findByTestId('manage-error');
    expect(alert).toHaveTextContent(copy);
    expect(alert).toHaveAttribute('role', 'alert');
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(document.body.textContent).not.toContain('Cannot read');
  }

  it.each(cases('clone', [403, 404, 409, 501]))('clone, %s', async (_n, err, copy) => {
    await renderPage(experiment({ status: 'active' }), { clone: failing(err) });
    fireEvent.click(screen.getByTestId('experiment-clone'));
    await expectFixed(copy);
    expect(mockRouter.push).not.toHaveBeenCalled();
    expect(screen.getByTestId('experiment-clone')).not.toBeDisabled();
  });

  it.each(cases('edit', [400, 403, 404, 422]))('edit, %s keeps what was typed', async (_n, err, copy) => {
    await renderPage(experiment(), { put: failing(err) });
    fireEvent.click(screen.getByTestId('experiment-edit-details'));
    fireEvent.change(screen.getByTestId('manage-edit-name'), { target: { value: 'Typed name' } });
    fireEvent.click(screen.getByTestId('manage-edit-save'));
    await expectFixed(copy);
    // An error is not an empty form: the answers are still there to retry.
    expect(screen.getByTestId('manage-edit-name')).toHaveValue('Typed name');
    expect(screen.getByTestId('experiment-name')).toHaveTextContent('Checkout button colour');
  });

  it.each(cases('delete', [400, 403, 404]))('delete, %s stays on the page', async (_n, err, copy) => {
    await renderPage(experiment(), { del: failing(err) });
    fireEvent.click(screen.getByTestId('experiment-delete'));
    fireEvent.click(screen.getByTestId('manage-delete-yes'));
    await expectFixed(copy);
    expect(mockRouter.push).not.toHaveBeenCalled();
    expect(screen.getByTestId('experiment-detail')).toBeInTheDocument();
    expect(screen.getByTestId('experiment-delete')).not.toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// The routes are stable, so no beta notice is owed (QA group B)
// ---------------------------------------------------------------------------

describe('the operations this region calls', () => {
  it('are stable in the OpenAPI document, so the region shows no beta notice', async () => {
    const doc = JSON.parse(
      fs.readFileSync(path.join(__dirname, '../../../../docs/api/openapi-v1.full.json'), 'utf-8'),
    ) as { paths: Record<string, Record<string, { 'x-stability'?: string }>> };
    const ops: [string, string][] = [
      ['/api/v1/experiments/{experiment_id}/clone', 'post'],
      ['/api/v1/experiments/{experiment_id}', 'put'],
      ['/api/v1/experiments/{experiment_id}', 'delete'],
    ];
    for (const [p, m] of ops) {
      expect([p, m, doc.paths[p][m]['x-stability'] ?? 'stable']).toEqual([p, m, 'stable']);
    }
    await renderPage();
    expect(screen.getByTestId('experiment-manage').textContent).not.toMatch(/beta/i);
  });
});

// ---------------------------------------------------------------------------
// a11y (QA group G). Scoped to the region this file owns. The header <dl> has
// no axe violations (experiment-detail-configuration.test.tsx scans the whole
// page, and pins each of its items as one dt followed by one dd).
// ---------------------------------------------------------------------------

describe('accessibility', () => {
  async function noViolations() {
    const region = screen.getByTestId('experiment-manage');
    const result = await axe.run(region, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  }

  it('in every state: buttons, edit form, name error, delete confirmation, error, saved', async () => {
    await renderPage(experiment(), { del: () => { throw apiError(500, PLANTED); } });
    await noViolations();

    fireEvent.click(screen.getByTestId('experiment-edit-details'));
    await noViolations();

    fireEvent.change(screen.getByTestId('manage-edit-name'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('manage-edit-save'));
    await noViolations();

    fireEvent.click(screen.getByTestId('experiment-delete'));
    await noViolations();

    fireEvent.click(screen.getByTestId('manage-delete-yes'));
    await screen.findByTestId('manage-error');
    await noViolations();

    fireEvent.click(screen.getByTestId('experiment-edit-details'));
    fireEvent.change(screen.getByTestId('manage-edit-name'), { target: { value: 'Fine' } });
    fireEvent.click(screen.getByTestId('manage-edit-save'));
    await screen.findByTestId('manage-saved');
    await noViolations();
  });
});
