import { AdminService } from '@/services/admin';
import { ApiError, TOKEN_STORAGE_KEY } from '@/services/api';
import { SafetySettings } from '@/types/admin';

const mockFetch = jest.fn();
global.fetch = mockFetch;

const BASE = 'http://localhost:8000';

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  process.env.NEXT_PUBLIC_API_URL = BASE;
});

afterAll(() => {
  delete process.env.NEXT_PUBLIC_API_URL;
});

function mockOk(data: unknown) {
  mockFetch.mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: () => Promise.resolve(data),
  } as Response);
}

function mockError(status = 500, statusText = 'Internal Server Error') {
  mockFetch.mockResolvedValueOnce({
    ok: false,
    status,
    statusText,
  } as Response);
}

describe('AdminService', () => {
  describe('listUsers', () => {
    it('calls correct endpoint', async () => {
      mockOk({ items: [], total: 0, skip: 0, limit: 20 });
      await AdminService.listUsers();
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining(`${BASE}/api/v1/admin/users`),
        expect.any(Object),
      );
    });

    // #651: the API pages by skip/limit; it never read `page`.
    it('passes skip/limit/search params, and no page', async () => {
      mockOk({ items: [], total: 0, skip: 20, limit: 10 });
      await AdminService.listUsers({ skip: 20, limit: 10, search: 'ann' });
      const params = new URL(mockFetch.mock.calls[0][0] as string).searchParams;
      expect(params.get('skip')).toBe('20');
      expect(params.get('limit')).toBe('10');
      expect(params.get('search')).toBe('ann');
      expect(params.has('page')).toBe(false);
    });

    it('omits an empty search param', async () => {
      mockOk({ items: [] });
      await AdminService.listUsers({ search: '' });
      const url = mockFetch.mock.calls[0][0] as string;
      expect(url).not.toContain('search=');
    });

    it('sends the bearer token when one is stored', async () => {
      localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
      mockOk({ items: [] });
      await AdminService.listUsers();
      const init = mockFetch.mock.calls[0][1] as RequestInit;
      expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok');
    });
  });

  describe('getStats', () => {
    it('calls /admin/stats endpoint', async () => {
      mockOk({
        users: { total: 50, active: 48, superusers: 1 },
        experiments: { total: 10, active: 5 },
        events: { total: 900, daily_rate: 30 },
        feature_flags: { total: 20, active: 12 },
        timestamp: '2026-10-07T08:00:00.000000',
      });
      await AdminService.getStats();
      expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/admin/stats`, expect.any(Object));
    });
  });

  describe('updateUser', () => {
    it('sends PATCH (upper case) with only the fields given', async () => {
      mockOk({ id: '1', username: 'admin', email: 'admin@test.com', role: 'ADMIN', is_active: false, created_at: '' });
      await AdminService.updateUser('1', { is_active: false });
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe(`${BASE}/api/v1/admin/users/1`);
      // Exact: browsers do not upper-case PATCH, and the API answers 405 to `patch`.
      expect(init.method).toBe('PATCH');
      expect(init.headers['Content-Type']).toBe('application/json');
      expect(init.body).toBe(JSON.stringify({ is_active: false }));
    });

    it('sends a role change as {role}', async () => {
      mockOk({ id: '1', username: 'a', email: null, role: 'ANALYST', is_active: true, created_at: '' });
      await AdminService.updateUser('1', { role: 'ANALYST' });
      expect(mockFetch.mock.calls[0][1].body).toBe('{"role":"ANALYST"}');
    });
  });

  describe('deleteUser', () => {
    it('calls DELETE on correct endpoint', async () => {
      mockFetch.mockResolvedValueOnce({ ok: true, status: 204 } as Response);
      await AdminService.deleteUser('42');
      expect(mockFetch).toHaveBeenCalledWith(
        `${BASE}/api/v1/admin/users/42`,
        expect.objectContaining({ method: 'DELETE' })
      );
    });
  });

  describe('listAuditLogs', () => {
    it('passes filters as query params', async () => {
      mockOk({ items: [], total: 0, page: 1, limit: 20 });
      await AdminService.listAuditLogs({
        action_type: 'CREATE',
        entity_type: 'feature_flag',
        from_date: '2024-01-01T05:00:00.000Z',
        to_date: '2024-01-02T05:00:00.000Z',
      });
      const params = new URL(mockFetch.mock.calls[0][0] as string).searchParams;
      expect(params.get('action_type')).toBe('CREATE');
      expect(params.get('entity_type')).toBe('feature_flag');
      expect(params.get('from_date')).toBe('2024-01-01T05:00:00.000Z');
      expect(params.get('to_date')).toBe('2024-01-02T05:00:00.000Z');
    });

    // #2273: one item's and one person's entries.
    it('sends entity_id and user_id, and leaves them out when empty', async () => {
      mockOk({ items: [], total: 0, page: 1, limit: 20 });
      await AdminService.listAuditLogs({ entity_type: 'feature_flag', entity_id: 'e-1', user_id: 'u-1' });
      const set = new URL(mockFetch.mock.calls[0][0] as string).searchParams;
      expect(set.get('entity_id')).toBe('e-1');
      expect(set.get('user_id')).toBe('u-1');
      mockOk({ items: [], total: 0, page: 1, limit: 20 });
      await AdminService.listAuditLogs({ entity_id: '', user_id: '' });
      const empty = new URL(mockFetch.mock.calls[1][0] as string).searchParams;
      expect(empty.has('entity_id')).toBe(false);
      expect(empty.has('user_id')).toBe(false);
    });

    // #665: the API reads from_date/to_date and has no entity_name; the old
    // start_date/end_date/entity_name were dropped unread.
    it('sends none of the keys the API does not read', async () => {
      mockOk({ items: [], total: 0, page: 1, limit: 20 });
      await AdminService.listAuditLogs({ from_date: '2024-01-01T00:00:00.000Z', to_date: '2024-01-02T00:00:00.000Z' });
      const params = new URL(mockFetch.mock.calls[0][0] as string).searchParams;
      for (const key of ['start_date', 'end_date', 'entity_name', 'user_email']) {
        expect(params.has(key)).toBe(false);
      }
    });
  });

  describe('exportAuditLogs (#221)', () => {
    it('asks for the format and the filters with the token, and returns the text and headers', async () => {
      localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        text: () => Promise.resolve('[]'),
        headers: new Headers({ 'X-Total-Count': '0' }),
      });
      const result = await AdminService.exportAuditLogs('csv', {
        action_type: 'safety_rollback',
        from_date: '2024-01-01T00:00:00.000Z',
      });
      const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const parsed = new URL(url);
      expect(parsed.pathname).toBe('/api/v1/audit-logs/export');
      expect(parsed.searchParams.get('format')).toBe('csv');
      expect(parsed.searchParams.get('action_type')).toBe('safety_rollback');
      expect(parsed.searchParams.has('entity_type')).toBe(false);
      expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok');
      expect(result.text).toBe('[]');
      expect(result.headers.get('x-total-count')).toBe('0');
    });

    it('sends entity_id and user_id to the export too (#2273)', async () => {
      mockFetch.mockResolvedValueOnce({ ok: true, status: 200, text: () => Promise.resolve('[]'), headers: new Headers() });
      await AdminService.exportAuditLogs('json', { entity_type: 'experiment', entity_id: 'e-1', user_id: 'u-1' });
      const parsed = new URL((mockFetch.mock.calls[0] as [string])[0]);
      expect(parsed.searchParams.get('entity_id')).toBe('e-1');
      expect(parsed.searchParams.get('user_id')).toBe('u-1');
    });

    it('throws an ApiError carrying the 422 detail', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 422,
        text: () => Promise.resolve(JSON.stringify({ detail: '60001 entries match these filters' })),
        headers: new Headers({ 'content-type': 'application/json' }),
      });
      const err = await AdminService.exportAuditLogs('json').catch((e) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(422);
      expect(err.detail).toBe('60001 entries match these filters');
    });

    it('throws a status-0 ApiError when the API cannot be reached', async () => {
      mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
      const err = await AdminService.exportAuditLogs('json').catch((e) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(0);
    });
  });

  // `/api/v1/rbac/*` moved to the rbac module; covered by modules/frontend/src/rbac.test.ts.

  describe('safety', () => {
    const settings: SafetySettings = {
      id: 'settings-1',
      enable_automatic_rollbacks: true,
      default_metrics: {
        error_rate: { warning_threshold: 0.02, critical_threshold: 0.05, comparison_type: 'greater_than' },
      },
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };

    it('getSafetySettings calls GET /safety/settings', async () => {
      mockOk(settings);
      const result = await AdminService.getSafetySettings();
      expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/safety/settings`, expect.any(Object));
      expect(result.enable_automatic_rollbacks).toBe(true);
    });

    it('updateSafetySettings POSTs SafetySettingsUpdate to /safety/settings', async () => {
      mockOk(settings);
      await AdminService.updateSafetySettings({
        enable_automatic_rollbacks: true,
        default_metrics: settings.default_metrics,
      });
      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe(`${BASE}/api/v1/safety/settings`);
      expect(init.method).toBe('POST');
      expect(JSON.parse(init.body)).toEqual({
        enable_automatic_rollbacks: true,
        default_metrics: settings.default_metrics,
      });
    });

    it('does not duplicate the per-flag safety check (FeatureFlagsService.safetyCheck owns it)', () => {
      expect(AdminService).not.toHaveProperty('getFlagSafetyStatus');
    });

    it('rollbackFlag POSTs /safety/feature-flags/{id}/rollback with query params', async () => {
      mockOk({ success: true, feature_flag_id: 'flag-1', message: 'ok', timestamp: 'now' });
      await AdminService.rollbackFlag('flag-1', 'error spike');
      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe(
        `${BASE}/api/v1/safety/feature-flags/flag-1/rollback?percentage=0&reason=error+spike`,
      );
      expect(init.method).toBe('POST');
      expect(init.body).toBeUndefined();
    });

    it('rollbackFlag omits an empty reason and honours a custom percentage', async () => {
      mockOk({ success: true, feature_flag_id: 'flag-1', message: 'ok', timestamp: 'now' });
      await AdminService.rollbackFlag('flag-1', '   ', 10);
      const [url] = mockFetch.mock.calls[0];
      expect(url).toBe(`${BASE}/api/v1/safety/feature-flags/flag-1/rollback?percentage=10`);
    });
  });

  describe('getSchedulerHealth', () => {
    it('calls /scheduler/health endpoint', async () => {
      mockOk([]);
      await AdminService.getSchedulerHealth();
      expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/scheduler/health`, expect.any(Object));
    });
  });

  describe('sendTestNotification', () => {
    it('posts channel, message and recipient', async () => {
      mockOk({ success: true, channel: 'slack', message: 'sent' });
      await AdminService.sendTestNotification('slack', 'hello', '#ops');
      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe(`${BASE}/api/v1/notifications/test`);
      expect(init.method).toBe('POST');
      expect(JSON.parse(init.body)).toEqual({ channel: 'slack', message: 'hello', recipient: '#ops' });
    });
  });

  describe('error handling', () => {
    it('throws ApiError on non-ok response', async () => {
      mockError(404, 'Not Found');
      const err = await AdminService.getStats().catch((e) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(404);
    });
  });
});
