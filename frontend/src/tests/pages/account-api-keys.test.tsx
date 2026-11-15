/**
 * "Your API keys" (#1607): any signed-in user lists, creates and revokes their
 * own keys on the existing `/api/v1/api-keys` routes.
 *
 * V-UI2: the page acts only on the caller's own keys, sends the expiry the
 * contract names, and reads the API's times only through `parseApiTimestamp`
 * and `keyState`. V-UI3: axe finds nothing in any state, and every state can
 * be walked with the keyboard. jest pins the process zone to
 * America/Los_Angeles (jest.global-setup.js), which is what lets a test tell
 * a UTC reading of a zoneless API time from a local one.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import OwnApiKeysPage from '@/pages/account/api-keys';
import { AuthProvider } from '@/contexts/AuthContext';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import { ApiKey } from '@/types/admin';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
import {
  MCP_READ_SCOPE,
  MCP_READ_SCOPE_HELP,
  MCP_READ_SCOPE_LABEL,
} from '@/components/admin/api-keys/mcpReadScope';

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockReplace = jest.fn().mockResolvedValue(true);
jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: jest.fn(),
    pathname: '/account/api-keys',
    asPath: '/account/api-keys',
    query: {},
    isReady: true,
    events: { on: jest.fn(), off: jest.fn() },
  }),
}));

const mockFetch = jest.fn();
global.fetch = mockFetch;

const BASE = process.env.NEXT_PUBLIC_API_URL ?? '';
const KEYS = '/api/v1/api-keys';
const DAY_MS = 24 * 60 * 60 * 1000;

/** Throw `message` (the failure the plan names) unless `condition` holds. */
function assertThat(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

const VIEWER: UserMe = {
  id: 'viewer-1',
  email: 'viewer@example.com',
  username: 'viewer',
  full_name: 'Vi Ewer',
  role: 'VIEWER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'sso',
};

/** An API time with no zone marker, as `GET /api/v1/api-keys` sends one. */
function apiTime(msFromNow: number): string {
  return new Date(Date.now() + msFromNow).toISOString().slice(0, 19);
}

function makeKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: 'k-1',
    name: 'laptop',
    description: null,
    scopes: [],
    is_active: true,
    user_id: VIEWER.id,
    created_at: apiTime(-10 * DAY_MS),
    expires_at: null,
    last_used_at: null,
    ...overrides,
  };
}

/** Somebody else's key: the API returns it only to `?all=true` from an ADMIN. */
const OTHER_USERS_KEY = makeKey({ id: 'k-other', name: 'someone-elses-key', user_id: 'other-user' });

interface Call {
  path: string;
  method: string;
  body: Record<string, unknown> | undefined;
}

let calls: Call[];
let keys: ApiKey[];
let failNext: { method: string; status: number; detail: string } | null;

function respond(status: number, body?: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

/** A small fake of the three routes (and `/auth/me`), recording every call. */
function installApi(user: UserMe = VIEWER) {
  mockFetch.mockImplementation(async (url: string, init: RequestInit = {}) => {
    const method = (init.method ?? 'GET').toUpperCase();
    const path = url.startsWith(BASE) ? url.slice(BASE.length) : url;
    const body = typeof init.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : undefined;
    calls.push({ path, method, body });
    if (failNext && failNext.method === method && path.startsWith(KEYS)) {
      const { status, detail } = failNext;
      failNext = null;
      return respond(status, { detail });
    }
    if (path.startsWith('/api/v1/auth/me')) return respond(200, user);
    if (path.split('?')[0] === KEYS && method === 'GET') {
      const all = new URLSearchParams(path.split('?')[1] ?? '').get('all') === 'true';
      return respond(200, all ? [...keys, OTHER_USERS_KEY] : keys);
    }
    if (path === KEYS && method === 'POST') {
      const id = `k-new-${keys.length + 1}`;
      const created = {
        id,
        name: body?.name,
        key: `eptk_${id}_plaintext`,
        prefix: 'eptk_abcd',
        created_at: apiTime(0),
        expires_at: typeof body?.expires_at === 'string' ? body.expires_at : null,
      };
      keys = [
        makeKey({
          id,
          name: String(body?.name),
          scopes: (body?.scopes as string[] | undefined) ?? [],
          created_at: apiTime(0),
          expires_at: created.expires_at,
        }),
        ...keys,
      ];
      return respond(201, created);
    }
    if (path.startsWith(`${KEYS}/`) && method === 'DELETE') {
      const id = decodeURIComponent(path.slice(KEYS.length + 1));
      keys = keys.filter((k) => k.id !== id);
      return respond(204);
    }
    return respond(404, { detail: `not faked: ${method} ${path}` });
  });
}

function renderPage() {
  return render(
    <AuthProvider>
      <OwnApiKeysPage />
    </AuthProvider>,
  );
}

/** The page, once the session and the first list have loaded. */
async function openPage() {
  const view = renderPage();
  await screen.findByTestId('own-api-keys-page');
  await waitFor(() => expect(screen.queryByTestId('own-key-loading')).not.toBeInTheDocument());
  return view;
}

function keyCalls(method: string): Call[] {
  return calls.filter((c) => c.method === method && c.path.startsWith(KEYS));
}

async function createKey(name: string, options: { mcpRead?: boolean; expiry?: string } = {}) {
  fireEvent.change(screen.getByTestId('own-key-name'), { target: { value: name } });
  if (options.mcpRead) fireEvent.click(screen.getByTestId('own-key-mcp-read'));
  if (options.expiry) fireEvent.change(screen.getByTestId('own-key-expiry'), { target: { value: options.expiry } });
  await act(async () => {
    fireEvent.click(screen.getByTestId('own-key-submit'));
  });
  await waitFor(() => expect(keyCalls('POST')).toHaveLength(1));
  return keyCalls('POST')[0].body as Record<string, unknown>;
}

function optionLabels(): string[] {
  const select = screen.getByTestId('own-key-expiry') as HTMLSelectElement;
  return Array.from(select.options).map((o) => o.textContent ?? '');
}

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

async function expectNoAxeViolations(node: Element, state: string) {
  const result = await axe.run(node, axeOptions);
  const summary = result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);
  expect({ state, violations: summary }).toEqual({ state, violations: [] });
}

beforeEach(() => {
  calls = [];
  keys = [];
  failNext = null;
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  installApi();
});

describe('V-UI2: the page acts only on the caller\'s own keys, through the existing API', () => {
  it('renders for a VIEWER who is not a superuser', async () => {
    renderPage();
    try {
      await screen.findByTestId('own-api-keys-page', {}, { timeout: 3000 });
    } catch {
      assertThat(false, 'a VIEWER saw no page');
    }
    expect(screen.queryByTestId('require-auth-forbidden')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'Your API keys' })).toBeInTheDocument();
  });

  it('lists with GET /api/v1/api-keys and no `all` parameter', async () => {
    keys = [makeKey({ id: 'k-mine', name: 'my-key' })];
    await openPage();
    await screen.findByTestId('own-key-row-k-mine');
    const gets = keyCalls('GET');
    expect(gets.length).toBeGreaterThan(0);
    for (const call of gets) {
      assertThat(call.path === KEYS && !/[?&]all=/.test(call.path), "own-keys page listed another user's key");
    }
    assertThat(
      screen.queryByTestId(`own-key-row-${OTHER_USERS_KEY.id}`) === null &&
        screen.queryAllByText(OTHER_USERS_KEY.name).length === 0,
      "own-keys page listed another user's key",
    );
  });

  it('with mcp:read ticked, POSTs scopes [mcp:read] and an expires_at 90 days ahead, in UTC ending in Z', async () => {
    await openPage();
    const before = Date.now();
    const body = await createKey('mcp laptop', { mcpRead: true });
    const after = Date.now();
    expect(keyCalls('POST')[0].path).toBe(KEYS);
    expect(body.name).toBe('mcp laptop');
    expect(body.scopes).toEqual([MCP_READ_SCOPE]);
    assertThat(typeof body.expires_at === 'string', 'POST body has no expires_at for the default 90 days');
    const expiresAt = body.expires_at as string;
    assertThat(expiresAt.endsWith('Z'), 'expires_at is not UTC');
    const at = Date.parse(expiresAt);
    expect(at).toBeGreaterThanOrEqual(before + 90 * DAY_MS - 1000);
    expect(at).toBeLessThanOrEqual(after + 90 * DAY_MS + 1000);
  });

  it('while mcp:read is ticked, does not offer "No expiry", and the default is 90 days', async () => {
    await openPage();
    // Picked first, then the box is ticked: the pick is not offered any more.
    fireEvent.change(screen.getByTestId('own-key-expiry'), { target: { value: 'never' } });
    fireEvent.click(screen.getByTestId('own-key-mcp-read'));
    assertThat(!optionLabels().includes('No expiry'), '"No expiry" offered while mcp:read is ticked');
    const select = screen.getByTestId('own-key-expiry') as HTMLSelectElement;
    assertThat(
      !Array.from(select.options).some((o) => o.value === 'never'),
      '"No expiry" offered while mcp:read is ticked',
    );
    expect(optionLabels()).toEqual(['30 days', '90 days', '1 year (365 days)']);
    expect(select.value).toBe('90');
    expect(screen.getByTestId('own-key-expiry-help')).toHaveTextContent(
      'A key with mcp:read expires after 30 days, 90 days or 1 year. The default is 90 days.',
    );
  });

  it('an unticked key sends no expires_at and no scopes by default (No expiry)', async () => {
    await openPage();
    const body = await createKey('sdk key');
    assertThat(!('expires_at' in body), 'unticked key sent a default expires_at');
    expect(body).toEqual({ name: 'sdk key' });
    // The form is back at its defaults: unticked, No expiry chosen.
    await screen.findByTestId('own-key-revealed');
    expect(screen.getByTestId('own-key-mcp-read')).not.toBeChecked();
    expect(optionLabels()).toEqual(['30 days', '90 days', '1 year (365 days)', 'No expiry']);
    expect((screen.getByTestId('own-key-expiry') as HTMLSelectElement).value).toBe('never');
    expect(screen.getByTestId('own-key-expiry-help')).toHaveTextContent(
      'The default is No expiry. You can choose 30 days, 90 days or 1 year instead.',
    );
  });

  it('a key ticked and then unticked goes back to No expiry, unless a time was picked', async () => {
    await openPage();
    fireEvent.click(screen.getByTestId('own-key-mcp-read'));
    fireEvent.click(screen.getByTestId('own-key-mcp-read'));
    expect((screen.getByTestId('own-key-expiry') as HTMLSelectElement).value).toBe('never');
    fireEvent.change(screen.getByTestId('own-key-expiry'), { target: { value: '30' } });
    fireEvent.click(screen.getByTestId('own-key-mcp-read'));
    expect((screen.getByTestId('own-key-expiry') as HTMLSelectElement).value).toBe('30');
  });

  it.each([
    ['30', 30, false],
    ['90', 90, false],
    ['365', 365, false],
    ['30', 30, true],
    ['365', 365, true],
  ])('a chosen expiry of %s days sends now plus %i days (mcp:read %s)', async (choice, days, mcpRead) => {
    await openPage();
    const before = Date.now();
    const body = await createKey('k', { mcpRead, expiry: choice });
    const after = Date.now();
    assertThat(typeof body.expires_at === 'string', `POST body has no expires_at for the chosen ${choice} days`);
    const expiresAt = body.expires_at as string;
    assertThat(expiresAt.endsWith('Z'), 'expires_at is not UTC');
    expect(Date.parse(expiresAt)).toBeGreaterThanOrEqual(before + days * DAY_MS - 1000);
    expect(Date.parse(expiresAt)).toBeLessThanOrEqual(after + days * DAY_MS + 1000);
  });

  it('reads a returned expires_at as UTC (parseApiTimestamp, keyState), never as local time', async () => {
    // Two hours ago in UTC. Read as Los Angeles wall-clock time it would be
    // hours in the future, so the key would look active.
    const pastExpiry = apiTime(-2 * 60 * 60 * 1000);
    const futureExpiry = apiTime(2 * 60 * 60 * 1000);
    keys = [
      makeKey({ id: 'k-expired', name: 'expired-key', expires_at: pastExpiry }),
      makeKey({ id: 'k-live', name: 'live-key', expires_at: futureExpiry }),
    ];
    await openPage();
    await screen.findByTestId('own-key-row-k-expired');
    expect(screen.getByTestId('own-key-state-k-expired')).toHaveTextContent('Expired');
    expect(screen.getByTestId('own-key-state-k-live')).toHaveTextContent('Active');
    const shown = screen.getByTestId('own-key-expires-k-expired').textContent;
    expect(shown).toBe(parseApiTimestamp(pastExpiry).toLocaleString());
    expect(shown).not.toBe(new Date(pastExpiry).toLocaleString());
  });

  it('revokes with DELETE /api/v1/api-keys/{id}, only after a confirmation', async () => {
    keys = [makeKey({ id: 'k-1', name: 'old laptop' }), makeKey({ id: 'k-2', name: 'ci' })];
    await openPage();
    fireEvent.click(await screen.findByTestId('own-key-revoke-k-1'));
    const confirm = screen.getByTestId('own-key-revoke-confirm');
    expect(within(confirm).getByRole('heading', { name: 'Revoke old laptop?' })).toBeInTheDocument();
    expect(keyCalls('DELETE')).toHaveLength(0);
    await act(async () => {
      fireEvent.click(screen.getByTestId('own-key-revoke-yes'));
    });
    await waitFor(() => expect(keyCalls('DELETE')).toHaveLength(1));
    expect(keyCalls('DELETE')[0].path).toBe(`${KEYS}/k-1`);
    await waitFor(() => expect(screen.queryByTestId('own-key-row-k-1')).not.toBeInTheDocument());
    expect(screen.getByTestId('own-key-row-k-2')).toBeInTheDocument();
    expect(screen.getByTestId('own-key-revoked')).toHaveTextContent('Revoked old laptop.');
  });

  it('cancelling a revoke sends nothing', async () => {
    keys = [makeKey({ id: 'k-1', name: 'old laptop' })];
    await openPage();
    fireEvent.click(await screen.findByTestId('own-key-revoke-k-1'));
    fireEvent.click(screen.getByTestId('own-key-revoke-cancel'));
    expect(screen.queryByTestId('own-key-revoke-confirm')).not.toBeInTheDocument();
    expect(keyCalls('DELETE')).toHaveLength(0);
  });

  it('shows the new key once, with Copy announced as Copied and the warning as text', async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    await openPage();
    await createKey('desktop', { mcpRead: true });
    const revealed = await screen.findByTestId('own-key-revealed');
    expect(within(revealed).getByTestId('own-key-value')).toHaveTextContent('eptk_k-new-1_plaintext');
    expect(within(revealed).getByTestId('own-key-warning')).toHaveTextContent("Copy this key now. It won't be shown again.");
    const status = screen.getByTestId('own-key-copy-status');
    expect(status).toHaveAttribute('aria-live', 'polite');
    expect(status).toHaveTextContent('');
    await act(async () => {
      fireEvent.click(screen.getByTestId('own-key-copy'));
    });
    expect(writeText).toHaveBeenCalledWith('eptk_k-new-1_plaintext');
    expect(status).toHaveTextContent('Copied');
    // The list now has the key; the plaintext is not in it.
    await screen.findByTestId('own-key-row-k-new-1');
    expect(within(screen.getByTestId('own-key-list')).queryByText(/plaintext/)).toBeNull();
    fireEvent.click(screen.getByTestId('own-key-done'));
    expect(screen.queryByTestId('own-key-revealed')).not.toBeInTheDocument();
    expect(screen.queryByText('eptk_k-new-1_plaintext')).not.toBeInTheDocument();
  });

  it('an empty name sends nothing and says what is wrong', async () => {
    await openPage();
    await act(async () => {
      fireEvent.click(screen.getByTestId('own-key-submit'));
    });
    expect(keyCalls('POST')).toHaveLength(0);
    const input = screen.getByTestId('own-key-name');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input).toHaveAccessibleDescription('Enter a name for the key.');
    expect(input).toHaveFocus();
  });

  it('the checkbox carries PR 1c\'s mcp:read label and help text', async () => {
    await openPage();
    const box = screen.getByRole('checkbox', { name: MCP_READ_SCOPE_LABEL });
    expect(box).toHaveAccessibleDescription(MCP_READ_SCOPE_HELP);
    expect(screen.getByTestId('own-key-mcp-read-help')).toHaveTextContent(MCP_READ_SCOPE_HELP);
  });

  it('offers no other scope', async () => {
    await openPage();
    expect(screen.getAllByRole('checkbox')).toHaveLength(1);
    expect(screen.queryByText(/sdk:ruleset/)).not.toBeInTheDocument();
  });

  it('shows the API\'s answer when a create fails, and keeps the form', async () => {
    await openPage();
    failNext = { method: 'POST', status: 422, detail: 'name is too long' };
    fireEvent.change(screen.getByTestId('own-key-name'), { target: { value: 'x' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('own-key-submit'));
    });
    expect(await screen.findByTestId('own-key-create-error')).toHaveTextContent('name is too long');
    expect(screen.queryByTestId('own-key-revealed')).not.toBeInTheDocument();
    expect(screen.getByTestId('own-key-name')).toHaveValue('x');
  });
});

describe('V-UI3: axe finds no violation in any state (jsdom: colour contrast is not computable here)', () => {
  it('no keys', async () => {
    const { container } = await openPage();
    expect(screen.getByTestId('own-key-empty')).toHaveTextContent('You have no API keys yet.');
    await expectNoAxeViolations(container, 'no keys');
  });

  it('a list with keys: one with mcp:read, one expired', async () => {
    keys = [
      makeKey({ id: 'k-mcp', name: 'claude', scopes: [MCP_READ_SCOPE], expires_at: apiTime(30 * DAY_MS), last_used_at: apiTime(-60_000) }),
      makeKey({ id: 'k-old', name: 'old', expires_at: apiTime(-DAY_MS) }),
    ];
    const { container } = await openPage();
    await screen.findByTestId('own-key-row-k-old');
    expect(screen.getByTestId('own-key-state-k-old')).toHaveTextContent('Expired');
    await expectNoAxeViolations(container, 'list');
  });

  it('the create form with mcp:read ticked', async () => {
    const { container } = await openPage();
    fireEvent.click(screen.getByTestId('own-key-mcp-read'));
    await expectNoAxeViolations(container, 'create form');
  });

  it('a validation error (empty name)', async () => {
    const { container } = await openPage();
    await act(async () => {
      fireEvent.click(screen.getByTestId('own-key-submit'));
    });
    expect(screen.getByTestId('own-key-name-error')).toBeInTheDocument();
    await expectNoAxeViolations(container, 'validation error');
  });

  it('a create the API refused', async () => {
    const { container } = await openPage();
    failNext = { method: 'POST', status: 403, detail: 'refused' };
    fireEvent.change(screen.getByTestId('own-key-name'), { target: { value: 'x' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('own-key-submit'));
    });
    await screen.findByTestId('own-key-create-error');
    await expectNoAxeViolations(container, 'create error');
  });

  it('the key-revealed state, after Copy', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: jest.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
    const { container } = await openPage();
    await createKey('desktop', { mcpRead: true });
    await screen.findByTestId('own-key-revealed');
    await act(async () => {
      fireEvent.click(screen.getByTestId('own-key-copy'));
    });
    await expectNoAxeViolations(container, 'key revealed');
  });

  it('the revoke confirmation, and after a revoke', async () => {
    keys = [makeKey({ id: 'k-1', name: 'old laptop' }), makeKey({ id: 'k-2', name: 'ci' })];
    const { container } = await openPage();
    fireEvent.click(await screen.findByTestId('own-key-revoke-k-1'));
    await expectNoAxeViolations(container, 'revoke confirmation');
    await act(async () => {
      fireEvent.click(screen.getByTestId('own-key-revoke-yes'));
    });
    await screen.findByTestId('own-key-revoked');
    await expectNoAxeViolations(container, 'after a revoke');
  });

  it('the list could not be loaded', async () => {
    failNext = { method: 'GET', status: 500, detail: 'down' };
    const { container } = renderPage();
    await screen.findByTestId('own-key-load-error');
    await expectNoAxeViolations(container, 'load error');
  });
});

describe('V-UI3: the keyboard reaches every control in order, and focus goes where the user is', () => {
  it('walks the form, the list, the revoke confirmation and back', async () => {
    const user = userEvent.setup();
    keys = [makeKey({ id: 'k-1', name: 'old laptop' })];
    await openPage();
    await screen.findByTestId('own-key-row-k-1');
    (document.activeElement as HTMLElement | null)?.blur();

    await user.tab();
    expect(screen.getByTestId('own-key-name')).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId('own-key-mcp-read')).toHaveFocus();
    await user.keyboard(' ');
    expect(screen.getByTestId('own-key-mcp-read')).toBeChecked();
    await user.tab();
    expect(screen.getByTestId('own-key-expiry')).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId('own-key-submit')).toHaveFocus();
    await user.tab();
    const revoke = screen.getByTestId('own-key-revoke-k-1');
    expect(revoke).toHaveFocus();
    expect(revoke).toHaveAccessibleName('Revoke old laptop');

    // The confirmation takes focus, is named by its heading, and Escape
    // closes it and returns focus to the button that opened it.
    await user.keyboard('{Enter}');
    const confirm = screen.getByTestId('own-key-revoke-confirm');
    expect(confirm).toHaveFocus();
    expect(confirm).toHaveAccessibleName('Revoke old laptop?');
    await user.tab();
    expect(screen.getByTestId('own-key-revoke-cancel')).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId('own-key-revoke-yes')).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByTestId('own-key-revoke-confirm')).not.toBeInTheDocument();
    expect(revoke).toHaveFocus();
    expect(keyCalls('DELETE')).toHaveLength(0);

    // Cancel by keyboard returns focus too.
    await user.keyboard('{Enter}');
    await user.tab();
    await user.keyboard('{Enter}');
    expect(screen.queryByTestId('own-key-revoke-confirm')).not.toBeInTheDocument();
    expect(revoke).toHaveFocus();

    // Revoke by keyboard: focus goes to the list's heading, with the outcome.
    await user.keyboard('{Enter}');
    await user.tab();
    await user.tab();
    await user.keyboard('{Enter}');
    await screen.findByTestId('own-key-revoked');
    expect(screen.getByRole('heading', { level: 2, name: 'Your keys' })).toHaveFocus();
  });

  it('creates by keyboard, moves focus to the new key, and Done returns to the form', async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: jest.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
    await openPage();
    await user.click(screen.getByTestId('own-key-name'));
    await user.keyboard('desktop{Enter}');
    const heading = await screen.findByRole('heading', { level: 2, name: 'Key created: desktop' });
    await waitFor(() => expect(heading).toHaveFocus());
    await user.tab();
    expect(screen.getByTestId('own-key-copy')).toHaveFocus();
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.getByTestId('own-key-copy-status')).toHaveTextContent('Copied'));
    await user.tab();
    expect(screen.getByTestId('own-key-done')).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(screen.queryByTestId('own-key-revealed')).not.toBeInTheDocument();
    expect(screen.getByTestId('own-key-name')).toHaveFocus();
  });
});
