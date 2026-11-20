import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ApiKeyTable, keyState } from '@/components/admin/api-keys/ApiKeyTable';
import type { ApiKey } from '@/types/admin';

/** Shape of `GET /api/v1/api-keys` items (`APIKeyRead`): no prefix, no secret. */
const makeApiKey = (overrides: Partial<ApiKey> = {}): ApiKey => ({
  id: 'key-1',
  name: 'My API Key',
  description: null,
  scopes: [],
  is_active: true,
  user_id: 'user-1',
  created_at: '2024-06-15T10:00:00Z',
  expires_at: null,
  last_used_at: null,
  ...overrides,
});

const mockFetch = (responseData: unknown, ok = true, status = 200) => {
  return jest.fn().mockResolvedValue({
    ok,
    status,
    json: async () => responseData,
    statusText: ok ? 'OK' : 'Internal Server Error',
  });
};

const calledUrls = (fetchMock: jest.Mock): string[] =>
  fetchMock.mock.calls.map((call: [string, ...unknown[]]) => String(call[0]));

beforeEach(() => {
  jest.clearAllMocks();
});

describe('keyState', () => {
  const now = new Date('2026-01-01T00:00:00Z');

  it('is active for a live key without expiry', () => {
    expect(keyState(makeApiKey(), now)).toBe('active');
  });

  it('is inactive when the API says so', () => {
    expect(keyState(makeApiKey({ is_active: false }), now)).toBe('inactive');
  });

  it('is expired when expires_at is in the past', () => {
    expect(keyState(makeApiKey({ expires_at: '2025-12-31T00:00:00Z' }), now)).toBe('expired');
    expect(keyState(makeApiKey({ expires_at: '2026-06-01T00:00:00Z' }), now)).toBe('active');
  });
});

describe('ApiKeyTable', () => {
  it('renders loading state initially', () => {
    global.fetch = jest.fn().mockImplementation(() => new Promise(() => {}));
    render(<ApiKeyTable onCreateKey={jest.fn()} />);
    expect(screen.getByTestId('api-key-table-loading')).toBeInTheDocument();
  });

  it("lists the caller's keys from GET /api/v1/api-keys (no query by default)", async () => {
    const keys = [
      makeApiKey({ id: 'key-1', name: 'Production Key' }),
      makeApiKey({ id: 'key-2', name: 'Dev Key', scopes: ['assign', 'track'] }),
    ];
    const fetchMock = mockFetch(keys);
    global.fetch = fetchMock;
    render(<ApiKeyTable onCreateKey={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByTestId('api-key-row-key-1')).toBeInTheDocument();
      expect(screen.getByTestId('api-key-row-key-2')).toBeInTheDocument();
    });
    expect(calledUrls(fetchMock)[0]).toMatch(/\/api\/v1\/api-keys$/);
    expect(screen.getByText('assign, track')).toBeInTheDocument();
    expect(screen.queryByText(/prefix/i)).not.toBeInTheDocument();
  });

  it('shows created_at, last used and expiry per row', async () => {
    const keys = [
      makeApiKey({
        id: 'key-1',
        created_at: '2024-06-15T10:00:00Z',
        last_used_at: null,
        expires_at: null,
      }),
    ];
    global.fetch = mockFetch(keys);
    render(<ApiKeyTable onCreateKey={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByTestId('created-at-key-1')).toBeInTheDocument();
    });
    // Unset dates render as a dash rather than "Invalid Date".
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('derives the status badge from is_active and expires_at', async () => {
    const keys = [
      makeApiKey({ id: 'key-active', name: 'Active Key' }),
      makeApiKey({ id: 'key-inactive', name: 'Inactive Key', is_active: false }),
      makeApiKey({ id: 'key-expired', name: 'Expired Key', expires_at: '2000-01-01T00:00:00Z' }),
    ];
    global.fetch = mockFetch(keys);
    render(<ApiKeyTable onCreateKey={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByTestId('status-badge-key-active')).toHaveTextContent('active');
      expect(screen.getByTestId('status-badge-key-inactive')).toHaveTextContent('inactive');
      expect(screen.getByTestId('status-badge-key-expired')).toHaveTextContent('expired');
    });
  });

  it('"All users" and "Include inactive" toggles add the query parameters', async () => {
    const fetchMock = mockFetch([]);
    global.fetch = fetchMock;
    render(<ApiKeyTable onCreateKey={jest.fn()} />);
    await waitFor(() => expect(screen.getByTestId('api-key-empty-state')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('show-all-keys'));
    await waitFor(() => expect(calledUrls(fetchMock).some((u) => u.includes('all=true'))).toBe(true));

    fireEvent.click(screen.getByTestId('include-inactive-keys'));
    await waitFor(() =>
      expect(
        calledUrls(fetchMock).some((u) => u.includes('all=true') && u.includes('include_inactive=true')),
      ).toBe(true),
    );

    // The exact query strings, unchanged by the move to `query:`: a false
    // toggle is absent rather than `=false`.
    const queries = calledUrls(fetchMock)
      .filter((u) => /\/api\/v1\/api-keys(\?|$)/.test(u))
      .map((u) => u.split('?')[1] ?? '');
    expect(queries).toEqual(['', 'all=true', 'all=true&include_inactive=true']);
  });

  it('delete asks for inline confirmation, then calls DELETE /api/v1/api-keys/{id}', async () => {
    const keys = [makeApiKey({ id: 'key-99', name: 'To Delete' })];
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => keys }) // GET
      .mockResolvedValueOnce({ ok: true, status: 204, json: async () => ({}) }) // DELETE
      .mockResolvedValueOnce({ ok: true, json: async () => [] }); // GET (refetch)
    global.fetch = fetchMock;
    render(<ApiKeyTable onCreateKey={jest.fn()} />);

    await waitFor(() => expect(screen.getByTestId('delete-button-key-99')).toBeInTheDocument());
    // One action, one name: Your API keys says Revoke, so does this page (#1974).
    expect(screen.getByTestId('delete-button-key-99')).toHaveTextContent(/^Revoke$/);
    fireEvent.click(screen.getByTestId('delete-button-key-99'));

    // Nothing deleted yet; the confirmation names the key.
    expect(screen.getByTestId('delete-confirm')).toHaveTextContent('To Delete');
    expect(screen.getByTestId('delete-confirm')).toHaveTextContent(/^Revoke To Delete\?/);
    expect(screen.getByTestId('confirm-delete')).toHaveTextContent('Revoke key');
    expect(screen.queryByText(/Delete/i, { selector: 'button' })).toBeNull();
    expect(
      fetchMock.mock.calls.some((c: [string, { method?: string }?]) => c[1]?.method === 'DELETE'),
    ).toBe(false);

    fireEvent.click(screen.getByTestId('confirm-delete'));
    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls.find(
        (call: [string, ...unknown[]]) =>
          String(call[0]).includes('/api/v1/api-keys/key-99') &&
          (call[1] as { method?: string })?.method === 'DELETE',
      );
      expect(deleteCall).toBeTruthy();
    });
    await waitFor(() => expect(screen.getByTestId('api-key-empty-state')).toBeInTheDocument());
  });

  it('cancel dismisses the confirmation without calling the API', async () => {
    const keys = [makeApiKey({ id: 'key-5', name: 'Keep me' })];
    const fetchMock = mockFetch(keys);
    global.fetch = fetchMock;
    render(<ApiKeyTable onCreateKey={jest.fn()} />);
    await waitFor(() => expect(screen.getByTestId('delete-button-key-5')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('delete-button-key-5'));
    fireEvent.click(screen.getByTestId('cancel-delete'));
    expect(screen.queryByTestId('delete-confirm')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('shows a delete error inline and keeps the row', async () => {
    const keys = [makeApiKey({ id: 'key-7', name: 'Stubborn' })];
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => keys })
      .mockResolvedValueOnce({
        ok: false,
        status: 403,
        statusText: 'Forbidden',
        json: async () => ({ detail: 'You do not have permission to delete api_key' }),
      });
    global.fetch = fetchMock;
    render(<ApiKeyTable onCreateKey={jest.fn()} />);
    await waitFor(() => expect(screen.getByTestId('delete-button-key-7')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('delete-button-key-7'));
    fireEvent.click(screen.getByTestId('confirm-delete'));
    await waitFor(() => expect(screen.getByTestId('delete-error')).toBeInTheDocument());
    expect(screen.getByTestId('api-key-row-key-7')).toBeInTheDocument();
  });

  it('refetches when refreshToken changes', async () => {
    const fetchMock = mockFetch([]);
    global.fetch = fetchMock;
    const { rerender } = render(<ApiKeyTable onCreateKey={jest.fn()} refreshToken={0} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    rerender(<ApiKeyTable onCreateKey={jest.fn()} refreshToken={1} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it('shows empty state when no keys', async () => {
    global.fetch = mockFetch([]);
    render(<ApiKeyTable onCreateKey={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByTestId('api-key-empty-state')).toBeInTheDocument();
    });
  });

  it('shows error state on fetch failure', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('Network error'));
    render(<ApiKeyTable onCreateKey={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByTestId('api-key-error-state')).toBeInTheDocument();
    });
  });

  it('"Create API Key" button is rendered and wired', async () => {
    const onCreateKey = jest.fn();
    global.fetch = mockFetch([]);
    render(<ApiKeyTable onCreateKey={onCreateKey} />);
    await waitFor(() => expect(screen.getByTestId('create-api-key-button')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('create-api-key-button'));
    expect(onCreateKey).toHaveBeenCalled();
  });
});
