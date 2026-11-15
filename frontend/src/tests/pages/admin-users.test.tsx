/**
 * /admin/users end to end in jsdom: the real page, guard, table, dialog,
 * AdminService and apiFetch, with only `fetch` mocked. What the dialog puts on
 * the wire is checked against the PATCH operation in the committed stable
 * OpenAPI snapshot, which the backend smoke suite keeps equal to the API.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import UsersPage from '@/pages/admin/users';
// The edit dialog makes `#__next` inert and portals beside it, so the page is
// mounted the way Next mounts it (#369).
import { APP_ROOT_ID, renderInAppRoot as render } from '@/tests/helpers/renderInAppRoot';
import { watchLiveRegions } from '@/tests/helpers/watchLiveRegions';
import { AuthProvider } from '@/contexts/AuthContext';
import { FORBIDDEN_MESSAGE, NOT_FOUND_MESSAGE } from '@/components/admin/users/EditUserModal';
import { TOKEN_STORAGE_KEY, serverErrorMessage, unreachableMessage } from '@/services/api';
import type { AdminUser, UserRole } from '@/types/admin';

jest.mock('next/router', () => ({
  useRouter: () => ({
    push: jest.fn(),
    replace: jest.fn(),
    pathname: '/admin/users',
    asPath: '/admin/users',
    query: {},
    isReady: true,
  }),
}));

jest.mock('next/link', () => {
  const MockLink = ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});

jest.mock('@/components/admin/AdminLayout', () => ({
  AdminLayout: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const STABLE_SNAPSHOT = path.join(REPO_ROOT, 'docs', 'api', 'openapi-v1.stable.json');
const USER_PATH = '/api/v1/admin/users/{user_id}';

// ---------------------------------------------------------------------------
// The contract: the request body schema of the operation the dialog called
// ---------------------------------------------------------------------------

type Schema = Record<string, unknown>;

function loadOperation(method: string): Schema | undefined {
  const spec = JSON.parse(fs.readFileSync(STABLE_SNAPSHOT, 'utf8'));
  const item = spec.paths?.[USER_PATH] as Record<string, Schema> | undefined;
  const op = item?.[method.toLowerCase()];
  if (!op) return undefined;
  const ref = (op.requestBody as Schema | undefined)?.content as Record<string, Schema> | undefined;
  const schemaRef = (ref?.['application/json']?.schema as Schema | undefined)?.$ref as string | undefined;
  const schema = schemaRef
    ? spec.components.schemas[schemaRef.replace('#/components/schemas/', '')]
    : undefined;
  return { ...op, bodySchema: schema };
}

/** Every way `body` breaks `schema`, as readable strings; `[]` when it conforms. */
function contractProblems(body: Record<string, unknown>, schema: Schema | undefined): string[] {
  if (!schema) return ['no request body schema'];
  const problems: string[] = [];
  const properties = (schema.properties ?? {}) as Record<string, Schema>;
  if (schema.additionalProperties !== false) problems.push('additionalProperties is not false');
  for (const key of (schema.required ?? []) as string[]) {
    if (!(key in body)) problems.push(`missing required: ${key}`);
  }
  for (const [name, prop] of Object.entries(properties)) {
    const anyOf = (prop.anyOf ?? []) as Schema[];
    const types = Array.isArray(prop.type) ? prop.type : [prop.type];
    if (types.includes('null') || anyOf.some((branch) => branch.type === 'null')) {
      problems.push(`${name} has a null branch`);
    }
  }
  for (const [key, value] of Object.entries(body)) {
    const prop = properties[key];
    if (!prop) {
      problems.push(`${key} not in properties`);
      continue;
    }
    if (prop.type === 'boolean' && typeof value !== 'boolean') problems.push(`${key} is not a boolean`);
    if (prop.type === 'string' && typeof value !== 'string') problems.push(`${key} is not a string`);
    if (Array.isArray(prop.enum) && !prop.enum.includes(value)) problems.push(`${key} ${String(value)} not in enum`);
  }
  return problems;
}

// ---------------------------------------------------------------------------
// fetch, routed
// ---------------------------------------------------------------------------

interface FakeResponse {
  status: number;
  body?: unknown;
  /** Send `body` as plain text, not JSON. */
  text?: string;
  headers?: Record<string, string>;
}

function respond({ status, body, text, headers = {} }: FakeResponse): Response {
  const payload = text ?? (body === undefined ? '' : JSON.stringify(body));
  const all: Record<string, string> = {
    'content-type': text !== undefined ? 'text/plain' : 'application/json',
    ...headers,
  };
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    headers: { get: (name: string) => all[name.toLowerCase()] ?? null },
    json: () => Promise.resolve(JSON.parse(payload)),
    text: () => Promise.resolve(payload),
  } as unknown as Response;
}

interface Captured {
  method: string;
  url: string;
  body: Record<string, unknown>;
}

const ME = {
  id: 'me-1',
  username: 'carol',
  email: 'carol@example.com',
  full_name: null,
  // A superuser whose role is not ADMIN: the guard must let them in.
  role: 'DEVELOPER' as UserRole,
  is_superuser: true,
  is_active: true,
  auth_provider: 'local',
};

let users: AdminUser[];
let patchResponse: FakeResponse | 'network';
let captured: Captured[];
let listUrls: string[];
const mockFetch = jest.fn();

function makeUsers(): AdminUser[] {
  return [
    { id: 'me-1', username: 'carol', email: 'carol@example.com', role: 'DEVELOPER', is_active: true, is_superuser: true, created_at: '2024-01-01T00:00:00Z' },
    { id: 'u-2', username: 'bob', email: 'bob@example.com', role: 'VIEWER', is_active: true, is_superuser: false, created_at: '2024-01-02T00:00:00Z' },
  ];
}

function route(url: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? 'GET').toString();
  const { pathname } = new URL(url, 'http://localhost');
  if (pathname === '/api/v1/auth/me') return Promise.resolve(respond({ status: 200, body: ME }));
  if (pathname === '/api/v1/admin/users' && method === 'GET') {
    listUrls.push(url);
    return Promise.resolve(
      respond({ status: 200, body: { items: users, total: users.length, skip: 0, limit: 50 } }),
    );
  }
  const match = /^\/api\/v1\/admin\/users\/([^/]+)$/.exec(pathname);
  if (match && method !== 'GET' && method !== 'DELETE') {
    const body = JSON.parse(String(init.body ?? '{}'));
    captured.push({ method, url, body });
    if (patchResponse === 'network') return Promise.reject(new TypeError('Failed to fetch'));
    if (patchResponse.status === 200) {
      users = users.map((u) => (u.id === match[1] ? { ...u, ...body } : u));
      return Promise.resolve(respond({ status: 200, body: users.find((u) => u.id === match[1]) }));
    }
    return Promise.resolve(respond(patchResponse));
  }
  return Promise.resolve(respond({ status: 404, body: { detail: `no route ${method} ${pathname}` } }));
}

async function renderPage() {
  render(
    <AuthProvider>
      <UsersPage />
    </AuthProvider>,
  );
  await screen.findByTestId('user-management-page');
  await screen.findByTestId('edit-user-u-2');
}

async function openBobAndChooseAnalyst() {
  fireEvent.click(screen.getByTestId('edit-user-u-2'));
  const modal = await screen.findByTestId('edit-user-modal');
  fireEvent.change(within(modal).getByTestId('edit-role-select'), { target: { value: 'ANALYST' } });
  return modal;
}

async function save() {
  await act(async () => {
    fireEvent.click(screen.getByTestId('edit-save-button'));
  });
}

let alertSpy: jest.SpyInstance;

beforeEach(() => {
  jest.useFakeTimers();
  localStorage.clear();
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  users = makeUsers();
  patchResponse = { status: 200 };
  captured = [];
  listUrls = [];
  mockFetch.mockReset();
  mockFetch.mockImplementation(route);
  global.fetch = mockFetch as unknown as typeof fetch;
  alertSpy = jest.spyOn(window, 'alert').mockImplementation(() => {});
});

afterEach(() => {
  expect(alertSpy).not.toHaveBeenCalled();
  alertSpy.mockRestore();
  jest.useRealTimers();
});

describe('/admin/users guard', () => {
  it('a superuser whose role is DEVELOPER can open the page', async () => {
    render(
      <AuthProvider>
        <UsersPage />
      </AuthProvider>,
    );
    expect(await screen.findByTestId('user-management-page')).toBeInTheDocument();
  });

  it('an ADMIN who is not a superuser cannot', async () => {
    mockFetch.mockImplementation((url: string, init?: RequestInit) =>
      new URL(url, 'http://localhost').pathname === '/api/v1/auth/me'
        ? Promise.resolve(respond({ status: 200, body: { ...ME, role: 'ADMIN', is_superuser: false } }))
        : route(url, init),
    );
    render(
      <AuthProvider>
        <UsersPage />
      </AuthProvider>,
    );
    expect(await screen.findByText(/a superuser account/i)).toBeInTheDocument();
    expect(screen.queryByTestId('user-management-page')).not.toBeInTheDocument();
  });
});

describe('/admin/users editing a user', () => {
  it('what the dialog sends matches the PATCH request body in the stable OpenAPI snapshot', async () => {
    await renderPage();
    const modal = await openBobAndChooseAnalyst();
    fireEvent.click(within(modal).getByTestId('edit-active-toggle'));
    await save();

    expect(captured).toHaveLength(1);
    const [request] = captured;
    expect(request.method).toBe('PATCH');
    expect(request.url).toMatch(/\/api\/v1\/admin\/users\/u-2$/);
    expect(request.body).toStrictEqual({ role: 'ANALYST', is_active: false });

    const op = loadOperation(request.method);
    expect(op).toBeDefined();
    expect(contractProblems(request.body, op?.bodySchema as Schema | undefined)).toStrictEqual([]);
  });

  it('the contract check itself refuses what the old PUT sent and a body PATCH does not accept', () => {
    const patch = loadOperation('PATCH')?.bodySchema as Schema;
    const put = loadOperation('PUT')?.bodySchema as Schema;
    expect(contractProblems({ active: false }, patch)).toContain('active not in properties');
    expect(contractProblems({ role: 'analyst' }, patch)).toContain('role analyst not in enum');
    expect(contractProblems({ role: 'ANALYST' }, put)).toEqual(
      expect.arrayContaining(['missing required: username']),
    );
  });

  it('a save closes the dialog, reloads the same search, announces it and returns focus', async () => {
    await renderPage();
    fireEvent.change(screen.getByTestId('user-search'), { target: { value: 'bo' } });
    act(() => {
      jest.advanceTimersByTime(300);
    });
    await waitFor(() => expect(listUrls[listUrls.length - 1]).toContain('search=bo'));
    await screen.findByTestId('edit-user-u-2');
    const listsBefore = listUrls.length;

    await openBobAndChooseAnalyst();
    await save();

    await waitFor(() => expect(screen.queryByTestId('edit-user-modal')).not.toBeInTheDocument());
    await waitFor(() => expect(listUrls.length).toBe(listsBefore + 1));
    expect(listUrls[listUrls.length - 1]).toContain('search=bo');
    expect(listUrls[listUrls.length - 1]).toContain('skip=0');
    const status = screen.getByTestId('user-save-status');
    expect(status).toHaveAttribute('aria-live', 'polite');
    expect(status).toHaveTextContent('Saved changes to bob.');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('edit-user-u-2')));
    const row = screen.getByText('bob').closest('tr') as HTMLElement;
    expect(row).toHaveTextContent('Analyst');
  });

  it('the save is announced after the page behind the dialog stops being inert (#369)', async () => {
    await renderPage();
    await openBobAndChooseAnalyst();
    expect(document.getElementById(APP_ROOT_ID)).toHaveAttribute('inert');
    const watch = watchLiveRegions();
    await save();
    await waitFor(() => expect(screen.getByTestId('user-save-status')).toHaveTextContent('Saved changes to bob.'));
    watch.stop();
    expect(document.getElementById(APP_ROOT_ID)).not.toHaveAttribute('inert');
    expect(watch.changes).toEqual([{ text: 'Saved changes to bob.', underInert: false }]);
  });

  it.each<[string, FakeResponse | 'network', () => string]>([
    [
      '400 shows the API sentence',
      { status: 400, body: { detail: "You can't change your own role. Ask another administrator to do it." } },
      () => "You can't change your own role. Ask another administrator to do it.",
    ],
    ['403 says who can do this', { status: 403, body: { detail: 'Not enough permissions' } }, () => FORBIDDEN_MESSAGE],
    ['404 says the user is gone', { status: 404, body: { detail: 'User not found' } }, () => NOT_FOUND_MESSAGE],
    [
      '409 shows the API sentence',
      {
        status: 409,
        body: {
          detail:
            "Roles on this deployment come from Cognito groups and are updated on every request. Change this user's group in Cognito instead.",
        },
      },
      () =>
        "Roles on this deployment come from Cognito groups and are updated on every request. Change this user's group in Cognito instead.",
    ],
    [
      '422 shows the field and message',
      {
        status: 422,
        body: { detail: [{ type: 'literal_error', loc: ['body', 'role'], msg: "Input should be 'ADMIN'" }] },
      },
      () => "Couldn't save: role: Input should be 'ADMIN'",
    ],
    [
      '5xx shows the server error with the request ID',
      { status: 500, text: 'Server Error', headers: { 'x-request-id': 'req-607' } },
      () => serverErrorMessage(500, 'req-607'),
    ],
    ['an unreachable API says so', 'network', () => unreachableMessage()],
  ])('%s, inside the dialog, which stays open with the choice kept', async (_name, response, expected) => {
    patchResponse = response;
    await renderPage();
    const modal = await openBobAndChooseAnalyst();
    await save();

    const error = within(modal).getByTestId('edit-error-message');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error).toHaveTextContent(expected());
    expect(screen.getByTestId('edit-user-modal')).toBeInTheDocument();
    expect((within(modal).getByTestId('edit-role-select') as HTMLSelectElement).value).toBe('ANALYST');
    expect(screen.getByTestId('user-save-status')).toHaveTextContent('');
  });

  it('after a 404, closing the dialog reloads the list', async () => {
    patchResponse = { status: 404, body: { detail: 'User not found' } };
    await renderPage();
    await openBobAndChooseAnalyst();
    await save();
    const listsBefore = listUrls.length;
    fireEvent.click(screen.getByTestId('edit-cancel-button'));
    await waitFor(() => expect(listUrls.length).toBe(listsBefore + 1));
    await waitFor(() => expect(screen.queryByTestId('loading-skeleton')).not.toBeInTheDocument());
  });

  it('Escape closes the dialog and focus returns to the row it was opened from', async () => {
    await renderPage();
    const modal = await openBobAndChooseAnalyst();
    fireEvent.keyDown(within(modal).getByTestId('edit-role-select'), { key: 'Escape' });
    expect(screen.queryByTestId('edit-user-modal')).not.toBeInTheDocument();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('edit-user-u-2')));
    expect(captured).toHaveLength(0);
  });

  it('the signed-in user cannot change their own role or deactivate themselves', async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId('edit-user-me-1'));
    const modal = await screen.findByTestId('edit-user-modal');
    expect(within(modal).getByTestId('edit-role-select')).toBeDisabled();
    expect(within(modal).getByTestId('edit-active-toggle')).toBeDisabled();
    expect(within(modal).getByTestId('edit-self-note')).toBeInTheDocument();
  });
});
