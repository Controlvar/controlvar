/**
 * The experiment page's "Notes and learnings" (#2274): the `notes` and
 * `learnings` keys of `experiment_metadata`, saved with
 * `POST /experiments/{id}/metadata`, which merges the keys it is sent.
 *
 * Every case renders the page module itself with `apiFetch` mocked and a
 * RESOLVED user (a null user is offered everything).
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { apiFetch } from '@/services/api';
import { Experiment } from '@/types/experiments';
import { NOTES_EMPTY, NOTES_ERROR_COPY, NOTES_HELP, NOTES_SAVED } from '@/components/experiments/ExperimentNotesSection';
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

const METADATA_PATH = '/api/v1/experiments/exp-1/metadata';

const BASE: Experiment = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'active',
  targeting_rules: null,
  tags: null,
  owner_id: 'someone-else',
  owner_name: 'Someone Else',
  start_date: '2026-10-20T10:00:00',
  end_date: null,
  created_at: '2026-10-01T10:00:00',
  updated_at: '2026-10-01T10:00:00',
  variants: [
    { id: 'v-1', name: 'Control', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'Blue CTA', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [{ id: 'm-1', name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true }],
  experiment_metadata: {},
};

const withMetadata = (experiment_metadata: Record<string, unknown> | null, status: Experiment['status'] = 'active'): Experiment => ({
  ...BASE,
  status,
  experiment_metadata,
});

const as = (role: string, extra: Record<string, unknown> = {}) => ({
  id: 'user-1',
  email: `${role.toLowerCase()}@demo.com`,
  username: role.toLowerCase(),
  role,
  is_superuser: false,
  ...extra,
});

const signIn = (user: Record<string, unknown>) => mockUseAuth.mockReturnValue({ user, status: 'authenticated' });

/** Text the server might send, which must never reach the page. */
const PLANTED = 'Traceback (most recent call last): PLANTED-notes-7c1';

type Handler = (body: Record<string, unknown>, current: Experiment) => unknown;

/** The route merges the keys it is sent into the stored metadata. */
const merge: Handler = (body, current) => ({
  ...current,
  owner_name: undefined,
  experiment_metadata: { ...(current.experiment_metadata ?? {}), ...body },
});

function install(current: Experiment, save: Handler = merge) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => current },
      { path: '/api/v1/holdout', handler: () => null },
      {
        method: 'POST',
        path: METADATA_PATH,
        handler: (_p, o) => save(o.json as Record<string, unknown>, current),
      },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderPage(current: Experiment = withMetadata({}), save?: Handler) {
  install(current, save);
  const view = render(<ExperimentDetailPage />);
  await screen.findByTestId('experiment-detail');
  return view;
}

const type = (field: 'notes' | 'learnings', value: string) =>
  fireEvent.change(screen.getByTestId(`notes-${field}`), { target: { value } });
const save = () => fireEvent.click(screen.getByTestId('notes-save'));
const posts = () => mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'POST');
const toTheRoute = () => mockedApiFetch.mock.calls.filter(([url]) => String(url).endsWith('/metadata'));

const sentBody = () => {
  expect(posts()).toHaveLength(1);
  const [url, options] = posts()[0];
  expect(url).toBe(METADATA_PATH);
  return options?.json as Record<string, unknown>;
};

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn(as('DEVELOPER'));
});

describe('who is offered the fields', () => {
  const offered: [string, Record<string, unknown>][] = [
    ['superuser VIEWER', as('VIEWER', { is_superuser: true })],
    ['ADMIN', as('ADMIN')],
    ['DEVELOPER', as('DEVELOPER')],
  ];
  const refused: [string, Record<string, unknown>][] = [
    ['ANALYST', as('ANALYST')],
    ['VIEWER', as('VIEWER')],
  ];

  it.each(offered)('%s sees the fields and the witness on the page', async (_name, user) => {
    signIn(user);
    await renderPage();
    expect(screen.getByTestId('experiment-notes')).toBeInTheDocument();
    expect(screen.getByTestId('notes-notes')).toBeInTheDocument();
    expect(screen.getByTestId('notes-learnings')).toBeInTheDocument();
    expect(screen.getByTestId('notes-save')).toBeInTheDocument();
  });

  it.each(refused)('%s loads the page, sees no fields, and sends nothing to the route', async (_name, user) => {
    signIn(user);
    await renderPage(withMetadata({ notes: 'Ran over a holiday week.' }));
    expect(screen.getByTestId('experiment-notes')).toBeInTheDocument();
    expect(screen.queryByTestId('experiment-notes-form')).toBeNull();
    expect(screen.queryByTestId('notes-notes')).toBeNull();
    expect(screen.queryByTestId('notes-save')).toBeNull();
    expect(toTheRoute()).toEqual([]);
    expect(posts()).toEqual([]);
  });

  it.each(refused)('%s reads the notes and learnings that are stored', async (_name, user) => {
    signIn(user);
    await renderPage(withMetadata({ notes: 'Ran over a holiday week.', learnings: 'Blue wins on mobile.' }));
    expect(screen.getByTestId('experiment-notes-notes')).toHaveTextContent('Ran over a holiday week.');
    expect(screen.getByTestId('experiment-notes-learnings')).toHaveTextContent('Blue wins on mobile.');
  });

  it.each(['draft', 'active', 'paused', 'completed', 'archived'] as const)('the fields are offered on a %s experiment', async (status) => {
    await renderPage(withMetadata({}, status));
    expect(screen.getByTestId('notes-notes')).toBeInTheDocument();
  });
});

describe('reading', () => {
  it('says there are no notes when nothing is stored, for a writer and for a reader', async () => {
    await renderPage(withMetadata(null));
    expect(screen.getByTestId('experiment-notes-empty')).toHaveTextContent(NOTES_EMPTY);
  });

  it('says there are no notes to a reader, with no fields', async () => {
    signIn(as('ANALYST'));
    await renderPage(withMetadata({}));
    expect(screen.getByTestId('experiment-notes-empty')).toHaveTextContent(NOTES_EMPTY);
    expect(screen.queryByTestId('notes-notes')).toBeNull();
  });

  it('shows no empty sentence once there is text', async () => {
    await renderPage(withMetadata({ learnings: 'Keep the shorter label.' }));
    expect(screen.queryByTestId('experiment-notes-empty')).toBeNull();
    expect(screen.getByTestId('notes-learnings')).toHaveValue('Keep the shorter label.');
    expect(screen.getByTestId('notes-notes')).toHaveValue('');
  });

  it('shows other keys read-only, as stored, and gives them no field', async () => {
    await renderPage(withMetadata({ ticket: 'EXP-12', insights: { lift: 0.04, segments: ['mobile'] } }));
    expect(screen.getByTestId('experiment-notes-other-ticket')).toHaveTextContent('EXP-12');
    expect(screen.getByTestId('experiment-notes-other-insights').textContent).toContain('"lift": 0.04');
    expect(screen.queryByTestId('notes-ticket')).toBeNull();
    expect(screen.queryByTestId('experiment-notes-empty')).toBeNull();
  });

  it('shows a notes value that is not text read-only and offers no field for it', async () => {
    await renderPage(withMetadata({ notes: { a: 1 }, learnings: 'Fine.' }));
    expect(screen.queryByTestId('notes-notes')).toBeNull();
    expect(screen.getByTestId('notes-learnings')).toHaveValue('Fine.');
    expect(screen.getByTestId('experiment-notes-other-notes').textContent).toContain('"a": 1');
  });
});

describe('saving', () => {
  it('Save is disabled until a field changes', async () => {
    await renderPage(withMetadata({ notes: 'Ran over a holiday week.' }));
    expect(screen.getByTestId('notes-save')).toBeDisabled();
    type('notes', 'Ran over a holiday week!');
    expect(screen.getByTestId('notes-save')).toBeEnabled();
    type('notes', 'Ran over a holiday week.');
    expect(screen.getByTestId('notes-save')).toBeDisabled();
  });

  it('sends only the key that changed, and the page shows the saved text', async () => {
    await renderPage(withMetadata({ notes: 'Old notes.', ticket: 'EXP-12' }));
    type('learnings', 'Blue wins on mobile.');
    save();
    await screen.findByTestId('notes-saved');
    expect(sentBody()).toEqual({ learnings: 'Blue wins on mobile.' });
    expect(screen.getByTestId('notes-saved')).toHaveTextContent(NOTES_SAVED);
    expect(screen.getByTestId('notes-save')).toBeDisabled();
    expect(screen.getByTestId('notes-learnings')).toHaveValue('Blue wins on mobile.');
    // The key that was not sent is still there, and so is the other metadata.
    expect(screen.getByTestId('notes-notes')).toHaveValue('Old notes.');
    expect(screen.getByTestId('experiment-notes-other-ticket')).toHaveTextContent('EXP-12');
  });

  it('sends both keys when both changed, exactly as typed (nothing trimmed)', async () => {
    await renderPage();
    type('notes', '  Line one\nLine two  ');
    type('learnings', 'Learned.');
    save();
    await screen.findByTestId('notes-saved');
    expect(sentBody()).toEqual({ notes: '  Line one\nLine two  ', learnings: 'Learned.' });
  });

  it('clearing a field sends an empty string for that key, and the page then says there are no notes', async () => {
    await renderPage(withMetadata({ notes: 'Old notes.' }));
    type('notes', '');
    save();
    await screen.findByTestId('notes-saved');
    expect(sentBody()).toEqual({ notes: '' });
    expect(screen.getByTestId('experiment-notes-empty')).toHaveTextContent(NOTES_EMPTY);
  });

  it('editing after a save clears the saved line', async () => {
    await renderPage();
    type('notes', 'One.');
    save();
    await screen.findByTestId('notes-saved');
    type('notes', 'Two.');
    expect(screen.queryByTestId('notes-saved')).toBeNull();
  });

  it.each([
    [403, NOTES_ERROR_COPY[403]],
    [404, NOTES_ERROR_COPY[404]],
    [422, NOTES_ERROR_COPY[422]],
    [500, NOTES_ERROR_COPY.fallback],
  ])('a %s from the API shows fixed copy, never the server detail', async (status, copy) => {
    await renderPage(withMetadata({}), () => {
      throw apiError(status, PLANTED);
    });
    type('notes', 'Something.');
    save();
    const alert = await screen.findByTestId('notes-error');
    expect(alert).toHaveTextContent(copy);
    expect(alert).toHaveAttribute('role', 'alert');
    expect(document.body.textContent).not.toContain('PLANTED');
    // The text typed is kept, and Save is offered again.
    await waitFor(() => expect(screen.getByTestId('notes-save')).toBeEnabled());
    expect(screen.getByTestId('notes-notes')).toHaveValue('Something.');
  });

  it('a 422 marks the fields invalid and links the error to them', async () => {
    await renderPage(withMetadata({}), () => {
      throw apiError(422, PLANTED);
    });
    type('notes', 'Something.');
    save();
    await screen.findByTestId('notes-error');
    const field = screen.getByTestId('notes-notes');
    expect(field).toHaveAttribute('aria-invalid', 'true');
    expect(field.getAttribute('aria-describedby')?.split(' ')).toEqual(['notes-error', 'notes-help']);
  });
});

describe('accessibility', () => {
  it('each field has a visible label and is described by the help line', async () => {
    await renderPage();
    const notes = screen.getByLabelText('Notes');
    const learnings = screen.getByLabelText('Learnings');
    expect(notes).toBe(screen.getByTestId('notes-notes'));
    expect(learnings).toBe(screen.getByTestId('notes-learnings'));
    expect(notes).toHaveAttribute('aria-describedby', 'notes-help');
    expect((screen.getByTestId('notes-help').textContent ?? '').replace(/\s+/g, ' ')).toBe(NOTES_HELP);
    const result = await axe.run(screen.getByTestId('experiment-notes'), {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('the section is a labelled region for a reader too', async () => {
    signIn(as('VIEWER'));
    await renderPage(withMetadata({ notes: 'Text.', ticket: 'EXP-1' }));
    expect(screen.getByRole('region', { name: 'Notes and learnings' })).toBe(screen.getByTestId('experiment-notes'));
    const result = await axe.run(screen.getByTestId('experiment-notes'), {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });
});

describe('while a save is in flight, and after a failure', () => {
  it('shows Saving, disables Save and the fields, and a second click sends nothing', async () => {
    let release: (v: unknown) => void = () => undefined;
    await renderPage(withMetadata({}), (body, current) => new Promise((resolve) => { release = () => resolve(merge(body, current)); }));
    type('notes', 'One.');
    save();
    await waitFor(() => expect(screen.getByTestId('notes-save')).toHaveTextContent('Saving…'));
    expect(screen.getByTestId('notes-save')).toBeDisabled();
    expect(screen.getByTestId('notes-notes')).toBeDisabled();
    save();
    expect(posts()).toHaveLength(1);
    await act(async () => { release(null); });
    await screen.findByTestId('notes-saved');
    expect(posts()).toHaveLength(1);
  });

  it('a failed save shows no saved line, and the error goes when the text is edited', async () => {
    await renderPage(withMetadata({}), () => { throw apiError(500, 'x'); });
    type('notes', 'One.');
    save();
    await screen.findByTestId('notes-error');
    expect(screen.queryByTestId('notes-saved')).toBeNull();
    type('notes', 'Two.');
    expect(screen.queryByTestId('notes-error')).toBeNull();
  });

  it('a retry that works clears the earlier error', async () => {
    let fail = true;
    await renderPage(withMetadata({}), (body, current) => { if (fail) throw apiError(500, 'x'); return merge(body, current); });
    type('notes', 'One.');
    save();
    await screen.findByTestId('notes-error');
    fail = false;
    save();
    await screen.findByTestId('notes-saved');
    expect(screen.queryByTestId('notes-error')).toBeNull();
  });

  it('a 422 says the fixed sentence word for word', async () => {
    await renderPage(withMetadata({}), () => { throw apiError(422, 'x'); });
    type('notes', 'One.');
    save();
    expect((await screen.findByTestId('notes-error')).textContent).toBe('The text was not accepted. It may contain a character that cannot be stored.');
  });
});
