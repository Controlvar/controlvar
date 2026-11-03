/**
 * "Your API keys" takes the `mcp:read` scope, its checkbox label and its help
 * text from the one module the Admin -> API Keys dialog uses
 * (`mcpReadScope.ts`), not from a copy. The module is replaced here with
 * sentinel values: a page that inlined the words, even the same words, shows
 * none of them and fails.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { OwnApiKeys } from '@/components/account/OwnApiKeys';
import { TOKEN_STORAGE_KEY } from '@/services/api';

jest.mock('@/components/admin/api-keys/mcpReadScope', () => ({
  MCP_READ_SCOPE: 'sentinel:scope-from-mcpReadScope',
  MCP_READ_SCOPE_LABEL: 'SENTINEL label from mcpReadScope.ts',
  MCP_READ_SCOPE_HELP: 'SENTINEL help text from mcpReadScope.ts',
}));

const mockFetch = jest.fn();
global.fetch = mockFetch;

function respond(status: number, body?: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  mockFetch.mockImplementation(async (_url: string, init: RequestInit = {}) =>
    (init.method ?? 'GET').toUpperCase() === 'POST'
      ? respond(201, { id: 'k-1', name: 'n', key: 'eptk_x', prefix: 'eptk_x', created_at: '2026-11-02T00:00:00' })
      : respond(200, []),
  );
});

it('shows the label and help text exported by mcpReadScope.ts, and sends its scope value', async () => {
  render(<OwnApiKeys />);
  await waitFor(() => expect(screen.queryByTestId('own-key-loading')).not.toBeInTheDocument());

  const box = screen.queryByRole('checkbox', { name: 'SENTINEL label from mcpReadScope.ts' });
  if (box === null) throw new Error('the page does not take its mcp:read label from mcpReadScope.ts');
  expect(box).toHaveAccessibleDescription('SENTINEL help text from mcpReadScope.ts');

  fireEvent.change(screen.getByTestId('own-key-name'), { target: { value: 'n' } });
  fireEvent.click(box);
  await act(async () => {
    fireEvent.click(screen.getByTestId('own-key-submit'));
  });
  const post = mockFetch.mock.calls.find(([, init]) => (init?.method ?? '').toUpperCase() === 'POST');
  expect(post).toBeDefined();
  const body = JSON.parse(String(post?.[1]?.body)) as { scopes?: string[] };
  expect(body.scopes).toEqual(['sentinel:scope-from-mcpReadScope']);
});
