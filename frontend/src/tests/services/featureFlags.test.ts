import { FeatureFlagsService, isFlagOn, flagRules } from '@/services/featureFlags';
import { ApiError, TOKEN_STORAGE_KEY } from '@/services/api';

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

const jsonInit = (method: string, data: unknown) =>
  expect.objectContaining({
    method,
    headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(data),
  });

describe('FeatureFlagsService.list', () => {
  it('calls the correct URL', async () => {
    mockOk({ items: [], total: 0 });
    await FeatureFlagsService.list();
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toBe(`${BASE}/api/v1/feature-flags`);
  });

  it('upper-cases the status param to match the DB enum', async () => {
    mockOk({ items: [] });
    await FeatureFlagsService.list({ status: 'active' });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('status=ACTIVE');
  });

  it('appends skip and limit params', async () => {
    mockOk({ items: [] });
    await FeatureFlagsService.list({ skip: 15, limit: 5 });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('skip=15');
    expect(url).toContain('limit=5');
  });

  it('returns parsed JSON', async () => {
    const payload = { items: [{ id: 'f1' }], total: 1 };
    mockOk(payload);
    const result = await FeatureFlagsService.list();
    expect(result).toEqual(payload);
  });

  it('sends the bearer token when one is stored', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockOk({ items: [] });
    await FeatureFlagsService.list();
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok');
  });

  it('throws ApiError on error', async () => {
    mockError(500, 'Server Error');
    const err = await FeatureFlagsService.list().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(500);
    expect(err.message).toContain('Something went wrong on the server (HTTP 500).');
  });
});

describe('FeatureFlagsService.get', () => {
  it('calls the correct URL', async () => {
    mockOk({ id: 'f1' });
    await FeatureFlagsService.get('f1');
    expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/feature-flags/f1`, expect.any(Object));
  });

  it('throws on error', async () => {
    mockError(404, 'Not Found');
    await expect(FeatureFlagsService.get('f1')).rejects.toThrow('Not Found');
  });
});

describe('FeatureFlagsService.create', () => {
  it('sends POST with JSON body', async () => {
    const data = { name: 'New Flag', key: 'new_flag' };
    mockOk({ id: 'f1', ...data });
    await FeatureFlagsService.create(data);
    expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/feature-flags`, jsonInit('POST', data));
  });

  it('throws on error', async () => {
    mockError(400, 'Bad Request');
    await expect(FeatureFlagsService.create({ name: '', key: '' })).rejects.toThrow('Bad Request');
  });
});

describe('FeatureFlagsService.update', () => {
  it('sends PUT with JSON body', async () => {
    const data = { name: 'Updated' };
    mockOk({ id: 'f1', ...data });
    await FeatureFlagsService.update('f1', data);
    expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/feature-flags/f1`, jsonInit('PUT', data));
  });

  it('throws on error', async () => {
    mockError(403, 'Forbidden');
    await expect(FeatureFlagsService.update('f1', {})).rejects.toThrow('Forbidden');
  });
});

describe('FeatureFlagsService.delete', () => {
  it('sends DELETE request', async () => {
    mockOk(undefined);
    await FeatureFlagsService.delete('f1');
    expect(mockFetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/feature-flags/f1`,
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('throws on error', async () => {
    mockError(404, 'Not Found');
    await expect(FeatureFlagsService.delete('f1')).rejects.toThrow('Not Found');
  });
});

describe('FeatureFlagsService.setEnabled', () => {
  it('POSTs to /enable with a ToggleRequest body when turning on', async () => {
    mockOk({ id: 'f1', status: 'active' });
    await FeatureFlagsService.setEnabled('f1', true, 'launch');
    expect(mockFetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/feature-flags/f1/enable`,
      jsonInit('POST', { reason: 'launch' }),
    );
  });

  it('POSTs to /disable when turning off', async () => {
    mockOk({ id: 'f1', status: 'inactive' });
    await FeatureFlagsService.setEnabled('f1', false);
    expect(mockFetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/feature-flags/f1/disable`,
      jsonInit('POST', { reason: null }),
    );
  });
});

describe('FeatureFlagsService.listRolloutSchedules', () => {
  it('filters /rollout-schedules by feature_flag_id', async () => {
    mockOk({ items: [], total: 0, skip: 0, limit: 50 });
    await FeatureFlagsService.listRolloutSchedules('f1');
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url.startsWith(`${BASE}/api/v1/rollout-schedules?`)).toBe(true);
    expect(url).toContain('feature_flag_id=f1');
  });
});

describe('FeatureFlagsService.safetyCheck', () => {
  it('calls the safety check route', async () => {
    mockOk({ feature_flag_id: 'f1', is_healthy: true, metrics: [] });
    await FeatureFlagsService.safetyCheck('f1');
    expect(mockFetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/safety/feature-flags/f1/check`,
      expect.any(Object),
    );
  });
});

// Flag environments (beta routes). Staging and Development only: the service
// offers no way to send Production through them.
describe('FeatureFlagsService environment routes', () => {
  const settings = (environment: string) => ({
    environment,
    status: 'inactive',
    is_active: false,
    rollout_percentage: 0,
    targeting_rules: null,
    configured: false,
    updated_at: null,
  });

  it('reads the flag in every environment', async () => {
    mockOk({ flag_id: 'f1', key: 'k', environments: [] });
    await FeatureFlagsService.getEnvironments('f1');
    expect(mockFetch.mock.calls[0][0]).toBe(`${BASE}/api/v1/feature-flags/f1/environments`);
  });

  it('reads one environment by name', async () => {
    mockOk(settings('staging'));
    await FeatureFlagsService.getInEnvironment('f1', 'staging');
    expect(mockFetch.mock.calls[0][0]).toBe(`${BASE}/api/v1/feature-flags/f1/environments/staging`);
  });

  it('PUTs a change to the environment route with only the fields given', async () => {
    mockOk(settings('development'));
    await FeatureFlagsService.updateInEnvironment('f1', 'development', { is_active: true });
    expect(mockFetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/feature-flags/f1/environments/development`,
      jsonInit('PUT', { is_active: true }),
    );
  });

  it('reads the batch route with one flag_id per flag', async () => {
    mockOk({ environment: 'staging', settings: { a: settings('staging'), b: settings('staging') } });
    const got = await FeatureFlagsService.listEnvironmentSettings('staging', ['a', 'b']);
    expect(mockFetch.mock.calls[0][0]).toBe(
      `${BASE}/api/v1/environments/staging/feature-flag-settings?flag_id=a&flag_id=b`,
    );
    expect(Object.keys(got)).toEqual(['a', 'b']);
  });

  it('asks in parts of at most 100 ids, and not at all for none', async () => {
    const ids = Array.from({ length: 205 }, (_, i) => `f${i}`);
    for (let i = 0; i < 3; i += 1) mockOk({ environment: 'staging', settings: {} });
    await FeatureFlagsService.listEnvironmentSettings('staging', ids);
    const counts = mockFetch.mock.calls.map(([url]) => new URL(String(url)).searchParams.getAll('flag_id').length);
    expect(counts).toEqual([100, 100, 5]);
    mockFetch.mockReset();
    await FeatureFlagsService.listEnvironmentSettings('staging', []);
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe('isFlagOn / flagRules helpers', () => {
  it('prefers status over is_active', () => {
    expect(isFlagOn({ status: 'active', is_active: false })).toBe(true);
    expect(isFlagOn({ status: 'INACTIVE', is_active: true })).toBe(false);
    expect(isFlagOn({ is_active: true })).toBe(true);
    expect(isFlagOn({})).toBe(false);
  });

  it('reads targeting_rules, then the legacy rules key', () => {
    const rules = { logical_operator: 'AND', groups: [] };
    expect(flagRules({ targeting_rules: rules })).toBe(rules);
    expect(flagRules({ rules })).toBe(rules);
    expect(flagRules({})).toBeNull();
  });
});
