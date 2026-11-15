/**
 * The global-holdout client sends exactly the calls the API reads (#1455,
 * PLAN C11): each method's exact path, verb, body and query, with exact
 * arrays, never `objectContaining`. url-literals is lenient where templates
 * overlap (`/holdout/${id}/results` also matches `/holdout/check/{user_id}`),
 * so this is the pin.
 */
import { apiFetch } from '@/services/api';
import { HoldoutsService } from '@/services/holdouts';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mocked = apiFetch as jest.MockedFunction<typeof apiFetch>;
const HOLDOUT = '7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';

beforeEach(() => {
  mocked.mockReset();
  mocked.mockResolvedValue({} as never);
});

describe('HoldoutsService', () => {
  it('lists every holdout from /all, with no query', async () => {
    await HoldoutsService.listAll();
    expect(mocked.mock.calls).toEqual([['/api/v1/holdout/all']]);
  });

  it('creates with the body it is given: a size of 5% is the whole number 5', async () => {
    await HoldoutsService.create({ name: 'Q4 holdout', holdout_percentage: 5, is_active: false });
    expect(mocked.mock.calls).toEqual([
      ['/api/v1/holdout', { method: 'POST', json: { name: 'Q4 holdout', holdout_percentage: 5, is_active: false } }],
    ]);
  });

  it('activates, ends and renames with PUT on its own path, the changed fields only', async () => {
    await HoldoutsService.update(HOLDOUT, { is_active: true });
    await HoldoutsService.update(HOLDOUT, { is_active: false });
    await HoldoutsService.update(HOLDOUT, { name: 'Q1 holdout' });
    expect(mocked.mock.calls).toEqual([
      [`/api/v1/holdout/${HOLDOUT}`, { method: 'PUT', json: { is_active: true } }],
      [`/api/v1/holdout/${HOLDOUT}`, { method: 'PUT', json: { is_active: false } }],
      [`/api/v1/holdout/${HOLDOUT}`, { method: 'PUT', json: { name: 'Q1 holdout' } }],
    ]);
  });

  it('reads the results (beta) with query {metric}, never a hand-built query string', async () => {
    await HoldoutsService.results(HOLDOUT, 'purchase');
    expect(mocked.mock.calls).toEqual([[`/api/v1/holdout/${HOLDOUT}/results`, { query: { metric: 'purchase' } }]]);
  });
});
