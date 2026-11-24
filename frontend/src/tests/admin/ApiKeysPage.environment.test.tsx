/**
 * Admin -> API Keys and an API key's environment (#2011): the dialog offers
 * the choice, a key is shown only when the API says it is of the environment
 * picked, and the list names each key's. The real page, dialog, table and
 * apiFetch, with only `fetch` mocked.
 */
import React from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
// The dialog makes `#__next` inert and portals beside it, so the page is
// mounted the way Next mounts it (#369).
import { renderInAppRoot as render } from '@/tests/helpers/renderInAppRoot';
import { ApiKeysPage } from '@/pages/admin/api-keys';
import { MCP_READ_SCOPE_LABEL } from '@/components/admin/api-keys/mcpReadScope';

jest.mock('@/components/admin/AdminLayout', () => ({
  AdminLayout: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => ({
    user: { id: 'u-1', email: 'u@example.test', role: 'ADMIN', is_superuser: true, is_active: true },
  }),
}));

const KEYS = '/api/v1/api-keys';
const SECRET = 'eptk_' + 'b'.repeat(32);

/** Field for field what 0.48.0 answers a create with, whatever it was asked. */
function answerOf0480(name: string): Record<string, unknown> {
  return {
    id: 'k-1',
    name,
    key: SECRET,
    prefix: SECRET.slice(0, 9),
    created_at: '2026-11-24T10:00:00',
    expires_at: null,
  };
}

const UNCONFIRMED = 'The API did not confirm that the new key is a Staging key';
const NEXT =
  'This happens while an earlier release of the API, which has no environments, is still running: create the key again when the upgrade has finished.';
const MCP_READ_REFUSED =
  'mcp:read needs a production key: the routes it opens read production only. Create the key for production, or without mcp:read.';

interface Call {
  method: string;
  path: string;
  body: Record<string, unknown> | undefined;
}

let calls: Call[];
/** What `POST /api/v1/api-keys` answers, given the body sent. */
let create: (body: Record<string, unknown>) => Response | Promise<Response>;
/** What `DELETE /api/v1/api-keys/{id}` answers. */
let revoke: () => Response;
let listed: Record<string, unknown>[];

function respond(status: number, body?: unknown): Response {
  const payload = body === undefined ? '' : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(payload),
  } as unknown as Response;
}

beforeEach(() => {
  calls = [];
  listed = [];
  create = (body) => respond(201, { ...answerOf0480(String(body.name)), environment: body.environment });
  revoke = () => respond(204);
  global.fetch = jest.fn(async (url: string, init: RequestInit = {}) => {
    const { pathname } = new URL(url, 'http://localhost');
    const method = (init.method ?? 'GET').toUpperCase();
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    calls.push({ method, path: pathname, body });
    if (pathname === KEYS && method === 'GET') return respond(200, listed);
    if (pathname === KEYS && method === 'POST') return create(body);
    if (pathname === `${KEYS}/k-1` && method === 'DELETE') return revoke();
    return respond(404, { detail: `no route ${method} ${pathname}` });
  }) as unknown as typeof fetch;
});

function made(method: string): Call[] {
  return calls.filter((call) => call.method === method);
}

async function openDialog() {
  render(<ApiKeysPage />);
  fireEvent.click(await screen.findByTestId('create-api-key-button'));
  return screen.findByRole('dialog', { name: 'Create API Key' });
}

const environmentSelect = () => screen.getByRole('combobox', { name: 'Environment' }) as HTMLSelectElement;

/** Fill the form and submit it. */
async function submit(environment?: string) {
  fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'ci' } });
  if (environment) fireEvent.change(environmentSelect(), { target: { value: environment } });
  await act(async () => {
    fireEvent.click(screen.getByTestId('create-api-key-submit'));
  });
}

/** The created key's value is nowhere in the page: not in text, not in an attribute. */
function expectKeyNowhere() {
  expect(document.documentElement.outerHTML).not.toContain(SECRET);
  expect(document.documentElement.outerHTML).not.toContain(SECRET.slice(0, 8));
  expect(screen.queryByTestId('new-key-banner')).not.toBeInTheDocument();
  expect(screen.queryByTestId('masked-key-value')).not.toBeInTheDocument();
}

async function expectNoAxeViolations(node: Element, state: string) {
  const { violations } = await axe.run(node, { rules: { 'color-contrast': { enabled: false } } });
  const summary = violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);
  expect({ state, violations: summary }).toEqual({ state, violations: [] });
}

describe('the choice', () => {
  it('is a labelled select, Production first and chosen, described before anything is sent', async () => {
    await openDialog();
    const select = environmentSelect();
    expect(select.value).toBe('production');
    expect(Array.from(select.options).map((o) => [o.value, o.textContent])).toEqual([
      ['production', 'Production'],
      ['staging', 'Staging'],
      ['development', 'Development'],
    ]);
    const help =
      'A key works in one environment, and that cannot be changed later. Experiments and event tracking need a Production key.';
    expect(screen.getByTestId('api-key-environment-help')).toBeVisible();
    expect(select).toHaveAccessibleDescription(help);
    expect(made('POST')).toEqual([]);
  });

  it('is reached by Tab after Name and changed from the keyboard', async () => {
    const user = userEvent.setup();
    await openDialog();
    expect(screen.getByTestId('api-key-name-input')).toHaveFocus();
    await user.tab();
    expect(environmentSelect()).toHaveFocus();
    await user.selectOptions(environmentSelect(), 'Development');
    expect(environmentSelect().value).toBe('development');
  });

  it.each(['production', 'staging', 'development'])('sends %s and shows the key the API says is one', async (env) => {
    await openDialog();
    await submit(env);
    expect(await screen.findByTestId('new-key-value')).toHaveTextContent(SECRET);
    expect(made('POST').map((call) => call.body)).toEqual([{ name: 'ci', environment: env }]);
    expect(made('DELETE')).toEqual([]);
  });

  it('is Production again the next time the dialog opens', async () => {
    await openDialog();
    fireEvent.change(environmentSelect(), { target: { value: 'staging' } });
    fireEvent.click(screen.getByTestId('modal-cancel-button'));
    fireEvent.click(screen.getByTestId('create-api-key-button'));
    expect(environmentSelect().value).toBe('production');
  });
});

describe('an answer that does not confirm the environment', () => {
  it.each([
    ['names none, as 0.48.0 answers', undefined],
    ['names another', 'production'],
  ])('%s: the key is revoked, is never shown, and the dialog says why', async (_, environment) => {
    create = (body) => respond(201, { ...answerOf0480(String(body.name)), ...(environment && { environment }) });
    const dialog = await openDialog();
    await submit('staging');
    const error = await within(dialog).findByRole('alert');
    expect(error).toHaveTextContent(`${UNCONFIRMED}, so the key was revoked. ${NEXT}`);
    expect(calls.map((c) => `${c.method} ${c.path}`)).toContain(`DELETE ${KEYS}/k-1`);
    expectKeyNowhere();
    // The form is as it was, and the list is read again.
    expect(screen.getByTestId('api-key-name-input')).toHaveValue('ci');
    expect(environmentSelect().value).toBe('staging');
    await waitFor(() => expect(made('GET')).toHaveLength(2));
  });

  it('a Production key that the API did not confirm is refused too', async () => {
    create = (body) => respond(201, answerOf0480(String(body.name)));
    await openDialog();
    await submit();
    expect(await screen.findByTestId('modal-error')).toHaveTextContent(
      'The API did not confirm that the new key is a Production key, so the key was revoked.',
    );
    expect(made('DELETE')).toHaveLength(1);
    expectKeyNowhere();
  });

  it('when the revoke fails, says the key still exists and where, and lists it', async () => {
    create = (body) => {
      listed = [{ ...answerOf0480(String(body.name)), key: undefined, scopes: [], is_active: true, user_id: 'u-1' }];
      return respond(201, answerOf0480(String(body.name)));
    };
    revoke = () => respond(500, { detail: 'Internal server error' });
    await openDialog();
    await submit('staging');
    expect(await screen.findByTestId('modal-error')).toHaveTextContent(
      `${UNCONFIRMED}, and revoking it failed (Internal server error). ` +
        `The key “ci” still exists: revoke it in the list on this page. ${NEXT}`,
    );
    expectKeyNowhere();
    const row = await screen.findByTestId('api-key-row-k-1');
    // The list could not say which environment it is for.
    expect(within(row).getByTestId('environment-k-1')).toHaveTextContent('—');
    expect(within(row).getByTestId('delete-button-k-1')).toBeInTheDocument();
  });

  it('after the dialog was closed, the page says why, and the key is never shown', async () => {
    let finish: (response: Response) => void = () => undefined;
    create = () => new Promise<Response>((resolve) => (finish = resolve));
    await openDialog();
    await submit('staging');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await act(async () => {
      finish(respond(201, answerOf0480('ci')));
    });
    const alert = await screen.findByTestId('key-environment-mismatch');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert).toHaveTextContent(`${UNCONFIRMED}, so the key was revoked. ${NEXT}`);
    expect(made('DELETE')).toHaveLength(1);
    expectKeyNowhere();
    await expectNoAxeViolations(screen.getByTestId('api-keys-page'), 'the page with the reason');
    // Opening the dialog again starts over.
    fireEvent.click(screen.getByTestId('create-api-key-button'));
    expect(screen.queryByTestId('key-environment-mismatch')).not.toBeInTheDocument();
    expect(screen.queryByTestId('modal-error')).not.toBeInTheDocument();
  });
});

describe('mcp:read outside production', () => {
  it('is sent as picked, and the refusal is shown as its sentence, with nothing revoked', async () => {
    create = () =>
      respond(422, {
        detail: [
          { type: 'value_error', loc: ['body', 'scopes'], msg: `Value error, ${MCP_READ_REFUSED}`, input: ['mcp:read'] },
        ],
      });
    await openDialog();
    fireEvent.click(screen.getByRole('checkbox', { name: MCP_READ_SCOPE_LABEL }));
    await submit('staging');
    const error = await screen.findByTestId('modal-error');
    expect(error.textContent).toBe(MCP_READ_REFUSED);
    expect(made('POST').map((call) => call.body)).toEqual([
      { name: 'ci', environment: 'staging', scopes: ['mcp:read'] },
    ]);
    expect(made('DELETE')).toEqual([]);
    expect(environmentSelect().value).toBe('staging');
    expect(screen.getByRole('checkbox', { name: MCP_READ_SCOPE_LABEL })).toBeChecked();
  });
});

describe('the list', () => {
  it("names each key's environment, and a dash where the API did not say", async () => {
    const key = (id: string, environment?: string) => ({
      id,
      name: id,
      description: null,
      scopes: [],
      is_active: true,
      user_id: 'u-1',
      created_at: '2026-11-24T10:00:00',
      expires_at: null,
      last_used_at: null,
      ...(environment && { environment }),
    });
    listed = [key('k-p', 'production'), key('k-s', 'staging'), key('k-d', 'development'), key('k-old')];
    render(<ApiKeysPage />);
    await screen.findByTestId('api-key-row-k-old');
    expect(screen.getByRole('columnheader', { name: 'Environment' })).toBeInTheDocument();
    expect(screen.getByTestId('environment-k-p')).toHaveTextContent('Production');
    expect(screen.getByTestId('environment-k-s')).toHaveTextContent('Staging');
    expect(screen.getByTestId('environment-k-d')).toHaveTextContent('Development');
    expect(screen.getByTestId('environment-k-old')).toHaveTextContent('—');
    await expectNoAxeViolations(screen.getByTestId('api-keys-page'), 'the list');
  });
});

describe('axe (colour contrast is not computable in jsdom)', () => {
  it('finds nothing in the open form, and fires on a select that lost its label (positive control)', async () => {
    const dialog = await openDialog();
    await expectNoAxeViolations(dialog, 'the open form');
    dialog.querySelector('label[for="api-key-environment"]')?.removeAttribute('for');
    const { violations } = await axe.run(dialog, { rules: { 'color-contrast': { enabled: false } } });
    expect(violations.map((v) => v.id)).toContain('select-name');
  });

  it('finds nothing when the key was refused', async () => {
    create = (body) => respond(201, answerOf0480(String(body.name)));
    const dialog = await openDialog();
    await submit('staging');
    await within(dialog).findByRole('alert');
    await expectNoAxeViolations(dialog, 'the key was refused');
  });
});
