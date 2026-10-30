/**
 * The exclusion-group client sends exactly the calls the API reads (#1455,
 * PLAN C11): each method's exact path, verb and body, with
 * `toHaveBeenCalledWith`-exact arrays, never `objectContaining`.
 * url-literals is lenient where templates overlap, so this is the pin.
 */
import { apiFetch } from '@/services/api';
import { ExclusionGroupsService } from '@/services/exclusionGroups';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mocked = apiFetch as jest.MockedFunction<typeof apiFetch>;
const GROUP = '3f2b9c1e-8d4a-4c6b-9e2f-1a2b3c4d5e6f';
const EXP = '9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d';

beforeEach(() => {
  mocked.mockReset();
  mocked.mockResolvedValue({} as never);
});

describe('ExclusionGroupsService', () => {
  it('lists with no query, and reads one group', async () => {
    await ExclusionGroupsService.list();
    await ExclusionGroupsService.get(GROUP);
    expect(mocked.mock.calls).toEqual([
      ['/api/v1/mutual-exclusion-groups'],
      [`/api/v1/mutual-exclusion-groups/${GROUP}`],
    ]);
  });

  it('creates with the body it is given: a share of 50% is the fraction 0.5', async () => {
    await ExclusionGroupsService.create({ name: 'Checkout', traffic_allocation: 0.5 });
    await ExclusionGroupsService.create({ name: 'Pricing', description: 'Price tests', traffic_allocation: 1 });
    expect(mocked.mock.calls).toEqual([
      ['/api/v1/mutual-exclusion-groups', { method: 'POST', json: { name: 'Checkout', traffic_allocation: 0.5 } }],
      [
        '/api/v1/mutual-exclusion-groups',
        { method: 'POST', json: { name: 'Pricing', description: 'Price tests', traffic_allocation: 1 } },
      ],
    ]);
  });

  it('updates with the changed fields only, archives with DELETE and unarchives with PUT status active', async () => {
    await ExclusionGroupsService.update(GROUP, { name: 'Checkout v2' });
    await ExclusionGroupsService.update(GROUP, { traffic_allocation: 0.25 });
    await ExclusionGroupsService.archive(GROUP);
    await ExclusionGroupsService.unarchive(GROUP);
    expect(mocked.mock.calls).toEqual([
      [`/api/v1/mutual-exclusion-groups/${GROUP}`, { method: 'PUT', json: { name: 'Checkout v2' } }],
      [`/api/v1/mutual-exclusion-groups/${GROUP}`, { method: 'PUT', json: { traffic_allocation: 0.25 } }],
      [`/api/v1/mutual-exclusion-groups/${GROUP}`, { method: 'DELETE' }],
      [`/api/v1/mutual-exclusion-groups/${GROUP}`, { method: 'PUT', json: { status: 'active' } }],
    ]);
  });

  it('adds an experiment with POST {experiment_id}, and removes it with DELETE on its own path', async () => {
    await ExclusionGroupsService.addExperiment(GROUP, EXP);
    await ExclusionGroupsService.removeExperiment(GROUP, EXP);
    expect(mocked.mock.calls).toEqual([
      [`/api/v1/mutual-exclusion-groups/${GROUP}/experiments`, { method: 'POST', json: { experiment_id: EXP } }],
      [`/api/v1/mutual-exclusion-groups/${GROUP}/experiments/${EXP}`, { method: 'DELETE' }],
    ]);
  });
});
