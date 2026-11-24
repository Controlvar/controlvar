/**
 * "Your API keys" and an API key's environment (#2011): the form offers the
 * choice to every signed-in user, a key is shown only when the API says it is
 * of the environment picked, and the list names each key's. The real component
 * and apiFetch, with only `fetch` mocked.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { OwnApiKeys } from '@/components/account/OwnApiKeys';
import { MCP_READ_SCOPE_LABEL } from '@/components/admin/api-keys/mcpReadScope';
import { TOKEN_STORAGE_KEY } from '@/services/api';

const KEYS = '/api/v1/api-keys';
const SECRET = 'eptk_' + 'c'.repeat(32);

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

/** A key as the list returns it; with no `environment`, as 0.48.0 lists one. */
function listedKey(id: string, environment?: string): Record<string, unknown> {
  return {
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
  };
}

const UNCONFIRMED = 'The API did not confirm that the new key is a Staging key';
const NEXT =
  'This happens while an earlier release of the API, which has no environments, is still running: create the key again once the API is upgraded.';
const MCP_READ_REFUSED =
  'mcp:read needs a production key: the routes it opens read production only. Create the key for production, or without mcp:read.';

interface Call {
  method: string;
  path: string;
  body: Record<string, unknown> | undefined;
}

let calls: Call[];
/** What `POST /api/v1/api-keys` answers, given the body sent. */
let create: (body: Record<string, unknown>) => Response;
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
  localStorage.clear();
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
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

async function open() {
  const view = render(<OwnApiKeys />);
  await waitFor(() => expect(screen.queryByTestId('own-key-loading')).not.toBeInTheDocument());
  return view;
}

const environmentSelect = () => screen.getByRole('combobox', { name: 'Environment' }) as HTMLSelectElement;

/** Fill the form and submit it. */
async function submit(environment?: string) {
  fireEvent.change(screen.getByTestId('own-key-name'), { target: { value: 'laptop' } });
  if (environment) fireEvent.change(environmentSelect(), { target: { value: environment } });
  await act(async () => {
    fireEvent.click(screen.getByTestId('own-key-submit'));
  });
}

/** The created key's value is nowhere in the page: not in text, not in an attribute. */
function expectKeyNowhere() {
  expect(document.documentElement.outerHTML).not.toContain(SECRET);
  expect(document.documentElement.outerHTML).not.toContain(SECRET.slice(0, 8));
  expect(screen.queryByTestId('own-key-revealed')).not.toBeInTheDocument();
}

async function expectNoAxeViolations(node: Element, state: string) {
  const { violations } = await axe.run(node, { rules: { 'color-contrast': { enabled: false } } });
  const summary = violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);
  expect({ state, violations: summary }).toEqual({ state, violations: [] });
}

describe('the choice', () => {
  it('is a labelled select, Production first and chosen, described before anything is sent', async () => {
    await open();
    const select = environmentSelect();
    expect(select.value).toBe('production');
    expect(Array.from(select.options).map((o) => [o.value, o.textContent])).toEqual([
      ['production', 'Production'],
      ['staging', 'Staging'],
      ['development', 'Development'],
    ]);
    const help =
      'A key works in one environment, and that cannot be changed later. Experiments and event tracking need a Production key.';
    expect(screen.getByTestId('own-key-environment-help')).toBeVisible();
    expect(select).toHaveAccessibleDescription(help);
    expect(made('POST')).toEqual([]);
  });

  it('is changed from the keyboard', async () => {
    const user = userEvent.setup();
    await open();
    await user.click(screen.getByTestId('own-key-name'));
    await user.tab();
    expect(environmentSelect()).toHaveFocus();
    await user.selectOptions(environmentSelect(), 'Staging');
    expect(environmentSelect().value).toBe('staging');
  });

  it.each(['production', 'staging', 'development'])('sends %s and shows the key the API says is one', async (env) => {
    await open();
    await submit(env);
    expect(await screen.findByTestId('own-key-value')).toHaveTextContent(SECRET);
    expect(made('POST').map((call) => call.body)).toEqual([{ name: 'laptop', environment: env }]);
    expect(made('DELETE')).toEqual([]);
    // The next key starts at the default again.
    expect(environmentSelect().value).toBe('production');
  });
});

describe('an answer that does not confirm the environment', () => {
  it.each([
    ['names none, as 0.48.0 answers', undefined],
    ['names another', 'production'],
  ])('%s: the key is revoked, is never shown, and the form says why', async (_, environment) => {
    create = (body) => respond(201, { ...answerOf0480(String(body.name)), ...(environment && { environment }) });
    await open();
    await submit('staging');
    const error = await screen.findByTestId('own-key-create-error');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error.textContent).toBe(`${UNCONFIRMED}, so the key was revoked. ${NEXT}`);
    expect(made('DELETE').map((call) => call.path)).toEqual([`${KEYS}/k-1`]);
    expectKeyNowhere();
    // The form is as it was, and the list is read again.
    expect(screen.getByTestId('own-key-name')).toHaveValue('laptop');
    expect(environmentSelect().value).toBe('staging');
    await waitFor(() => expect(made('GET')).toHaveLength(2));
  });

  it('a Production key that the API did not confirm is refused too', async () => {
    create = (body) => respond(201, answerOf0480(String(body.name)));
    await open();
    await submit();
    expect(await screen.findByTestId('own-key-create-error')).toHaveTextContent(
      'The API did not confirm that the new key is a Production key, so the key was revoked.',
    );
    expect(made('DELETE')).toHaveLength(1);
    expectKeyNowhere();
  });

  it.each([
    ['403', () => respond(403, { detail: "You don't have permission to delete API keys" }), 'HTTP 403', 'still exists'],
    ['404', () => respond(404, { detail: 'API key not found' }), 'HTTP 404', 'may still exist'],
    ['500', () => respond(500, { detail: 'Internal server error' }), 'HTTP 500', 'may still exist'],
    [
      'no answer',
      () => {
        throw new TypeError('Failed to fetch');
      },
      'no answer',
      'may still exist',
    ],
  ] as [string, () => Response, string, string][])('when the revoke fails (%s), says so and where to revoke it, and lists the key', async (_, answer, reason, exists) => {
    create = (body) => {
      listed = [{ ...listedKey('k-1'), name: body.name }];
      return respond(201, answerOf0480(String(body.name)));
    };
    revoke = answer;
    await open();
    await submit('staging');
    const error = await screen.findByTestId('own-key-create-error');
    expect(error.textContent).toBe(
      `${UNCONFIRMED}, and revoking it failed (${reason}). ` +
        `The key “laptop” ${exists}: revoke it in the list on this page. ${NEXT}`,
    );
    expectKeyNowhere();
    const row = await screen.findByTestId('own-key-row-k-1');
    expect(within(row).getByTestId('own-key-environment-k-1')).toHaveTextContent('—');
    expect(within(row).getByTestId('own-key-revoke-k-1')).toBeInTheDocument();
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
    await open();
    fireEvent.click(screen.getByRole('checkbox', { name: MCP_READ_SCOPE_LABEL }));
    await submit('staging');
    const error = await screen.findByTestId('own-key-create-error');
    expect(error.textContent).toBe(`The key was not created: ${MCP_READ_REFUSED}`);
    expect(made('POST')[0].body).toMatchObject({ name: 'laptop', environment: 'staging', scopes: ['mcp:read'] });
    expect(made('DELETE')).toEqual([]);
    expect(environmentSelect().value).toBe('staging');
    expect(screen.getByRole('checkbox', { name: MCP_READ_SCOPE_LABEL })).toBeChecked();
  });
});

describe('the list', () => {
  it("names each key's environment, and a dash where the API did not say", async () => {
    listed = [
      listedKey('k-p', 'production'),
      listedKey('k-s', 'staging'),
      listedKey('k-d', 'development'),
      listedKey('k-old'),
    ];
    const { container } = await open();
    expect(screen.getByRole('columnheader', { name: 'Environment' })).toBeInTheDocument();
    expect(screen.getByTestId('own-key-environment-k-p')).toHaveTextContent('Production');
    expect(screen.getByTestId('own-key-environment-k-s')).toHaveTextContent('Staging');
    expect(screen.getByTestId('own-key-environment-k-d')).toHaveTextContent('Development');
    expect(screen.getByTestId('own-key-environment-k-old')).toHaveTextContent('—');
    await expectNoAxeViolations(container, 'the list');
  });
});

describe('axe (colour contrast is not computable in jsdom)', () => {
  it('finds nothing in the form, and fires on a select that lost its label (positive control)', async () => {
    const { container } = await open();
    await expectNoAxeViolations(container, 'the form');
    container.querySelector(`label[for="${environmentSelect().id}"]`)?.removeAttribute('for');
    const { violations } = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(violations.map((v) => v.id)).toContain('select-name');
  });

  it('finds nothing when the key was refused', async () => {
    create = (body) => respond(201, answerOf0480(String(body.name)));
    const { container } = await open();
    await submit('staging');
    await screen.findByTestId('own-key-create-error');
    await expectNoAxeViolations(container, 'the key was refused');
  });
});
