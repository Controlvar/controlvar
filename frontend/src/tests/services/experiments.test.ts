import { ExperimentsService } from '@/services/experiments';
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

describe('ExperimentsService.list', () => {
  it('calls the correct URL', async () => {
    mockOk({ items: [], total: 0, page: 1, limit: 20 });
    await ExperimentsService.list();
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toBe(`${BASE}/api/v1/experiments`);
  });

  it('appends the backend status_filter param', async () => {
    mockOk({ items: [] });
    await ExperimentsService.list({ status: 'active' });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('status_filter=active');
    expect(url).not.toContain('status=active');
  });

  it('appends skip and limit params (offset pagination)', async () => {
    mockOk({ items: [] });
    await ExperimentsService.list({ skip: 20, limit: 10 });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('skip=20');
    expect(url).toContain('limit=10');
    expect(url).not.toContain('page=');
  });

  it('returns parsed JSON on success', async () => {
    const payload = { items: [{ id: '1' }], total: 1 };
    mockOk(payload);
    const result = await ExperimentsService.list();
    expect(result).toEqual(payload);
  });

  it('sends the bearer token when one is stored', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockOk({ items: [] });
    await ExperimentsService.list();
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok');
  });

  it('throws ApiError on non-ok response', async () => {
    mockError(500, 'Internal Server Error');
    const err = await ExperimentsService.list().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(500);
    expect(err.message).toContain('Something went wrong on the server (HTTP 500).');
  });
});

describe('ExperimentsService.get', () => {
  it('calls the correct URL', async () => {
    mockOk({ id: 'abc' });
    await ExperimentsService.get('abc');
    expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/experiments/abc`, expect.any(Object));
  });

  it('throws on error', async () => {
    mockError(404, 'Not Found');
    await expect(ExperimentsService.get('abc')).rejects.toThrow('Not Found');
  });
});

describe('ExperimentsService.create', () => {
  it('sends POST with JSON body', async () => {
    const data = { name: 'Test', experiment_type: 'a_b', variants: [], metrics: [] };
    mockOk({ id: '1', ...data });
    await ExperimentsService.create(data as never);
    expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/experiments`, jsonInit('POST', data));
  });

  it('throws on error', async () => {
    mockError(400, 'Bad Request');
    await expect(ExperimentsService.create({} as never)).rejects.toThrow('Bad Request');
  });
});

describe('ExperimentsService.update', () => {
  it('sends PUT with JSON body', async () => {
    const data = { name: 'Updated' };
    mockOk({ id: 'abc', ...data });
    await ExperimentsService.update('abc', data);
    expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/experiments/abc`, jsonInit('PUT', data));
  });

  it('throws on error', async () => {
    mockError(403, 'Forbidden');
    await expect(ExperimentsService.update('abc', {})).rejects.toThrow('Forbidden');
  });
});

describe('ExperimentsService.updateDetails', () => {
  it('sends PUT with exactly the body it is given', async () => {
    mockOk({ id: 'abc' });
    await ExperimentsService.updateDetails('abc', { name: 'Renamed', hypothesis: null });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE}/api/v1/experiments/abc`);
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body as string)).toEqual({ name: 'Renamed', hypothesis: null });
  });
});

describe('ExperimentsService.updateDesign', () => {
  it('sends PUT /experiments/{id} with exactly the two lists it is given', async () => {
    const body = {
      variants: [
        { name: 'A', description: null, is_control: true, traffic_allocation: 50, configuration: null },
        { name: 'B', description: 'b', is_control: false, traffic_allocation: 50, configuration: { x: 1 } },
      ],
      metrics: [
        {
          name: 'M',
          description: null,
          event_name: 'm',
          metric_type: 'conversion' as const,
          is_primary: true,
          expected_effect: null,
          event_value_path: null,
          lower_is_better: false,
        },
      ],
    };
    mockOk({ id: 'abc' });
    await ExperimentsService.updateDesign('abc', body);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE}/api/v1/experiments/abc`);
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body as string)).toEqual(body);
  });

  it('throws on error', async () => {
    mockError(400, 'Bad Request');
    await expect(ExperimentsService.updateDesign('abc', { variants: [], metrics: [] })).rejects.toThrow(
      'Bad Request',
    );
  });
});

describe('ExperimentsService.clone', () => {
  it('sends POST to /clone with no body and returns the new experiment', async () => {
    mockOk({ id: 'new-id', name: 'Copy of X', status: 'draft' });
    const created = await ExperimentsService.clone('abc');
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE}/api/v1/experiments/abc/clone`);
    expect(init.method).toBe('POST');
    expect(init.body).toBeUndefined();
    expect(created.id).toBe('new-id');
  });

  it('throws on error', async () => {
    mockError(403, 'Forbidden');
    await expect(ExperimentsService.clone('abc')).rejects.toThrow('Forbidden');
  });
});

describe('ExperimentsService.delete', () => {
  // Regression (#603): the route requires `experiment_key` (the id itself);
  // without it every call answered 422.
  it('sends DELETE with the required experiment_key', async () => {
    mockOk(undefined);
    await ExperimentsService.delete('abc');
    expect(mockFetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/experiments/abc?experiment_key=abc`,
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('throws on error', async () => {
    mockError(404, 'Not Found');
    await expect(ExperimentsService.delete('abc')).rejects.toThrow('Not Found');
  });
});

describe('ExperimentsService lifecycle actions', () => {
  it.each([
    ['start', 'start'],
    ['pause', 'pause'],
    ['complete', 'complete'],
    ['archive', 'archive'],
  ] as const)('%s POSTs to /experiments/{id}/%s', async (method, segment) => {
    mockOk({ id: 'abc', status: 'active' });
    await ExperimentsService[method]('abc');
    expect(mockFetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/experiments/abc/${segment}`,
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('surfaces the backend detail on a rejected transition', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      text: () => Promise.resolve(JSON.stringify({ detail: 'Cannot start experiment with status: active' })),
    } as unknown as Response);
    await expect(ExperimentsService.start('abc')).rejects.toThrow(
      'Cannot start experiment with status: active',
    );
  });
});
