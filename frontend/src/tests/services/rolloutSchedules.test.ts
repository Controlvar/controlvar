/**
 * Each rollout-schedule write's exact method and URL (C11, #1456).
 *
 * url-literals.test.ts passes any call whose path matches SOME operation, and
 * `/rollout-schedules/stages/${id}` also matches `/{schedule_id}/stages`
 * (architect X5), so a wrong method or a swapped path there would pass it.
 * This pins each call as it goes over the wire.
 */
import { RolloutSchedulesService } from '@/services/rolloutSchedules';

const mockFetch = jest.fn();
global.fetch = mockFetch;

const BASE = 'http://localhost:8000';

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  process.env.NEXT_PUBLIC_API_URL = BASE;
  mockFetch.mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) } as Response);
});

afterAll(() => {
  delete process.env.NEXT_PUBLIC_API_URL;
});

/** [url, method, body] of the one request made. */
function sent(): [string, string, unknown] {
  expect(mockFetch).toHaveBeenCalledTimes(1);
  const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
  return [url, String(init.method), init.body === undefined ? undefined : JSON.parse(String(init.body))];
}

const STAGE = { name: 'Canary', stage_order: 1, target_percentage: 10, trigger_type: 'manual' as const };

describe('RolloutSchedulesService sends each write to its own route', () => {
  it('create: POST /api/v1/rollout-schedules/ with the body as given', async () => {
    const body = { name: 'Gradual', feature_flag_id: 'flag-1', max_percentage: 100, stages: [STAGE] };
    await RolloutSchedulesService.create(body);
    expect(sent()).toEqual([`${BASE}/api/v1/rollout-schedules/`, 'POST', body]);
  });

  it('addStage: POST /api/v1/rollout-schedules/{schedule_id}/stages', async () => {
    await RolloutSchedulesService.addStage('sched-1', STAGE);
    expect(sent()).toEqual([`${BASE}/api/v1/rollout-schedules/sched-1/stages`, 'POST', STAGE]);
  });

  it('updateStage: PUT /api/v1/rollout-schedules/stages/{stage_id}', async () => {
    await RolloutSchedulesService.updateStage('stage-1', { target_percentage: 20 });
    expect(sent()).toEqual([`${BASE}/api/v1/rollout-schedules/stages/stage-1`, 'PUT', { target_percentage: 20 }]);
  });

  it('deleteStage: DELETE /api/v1/rollout-schedules/stages/{stage_id}', async () => {
    await RolloutSchedulesService.deleteStage('stage-1');
    expect(sent()).toEqual([`${BASE}/api/v1/rollout-schedules/stages/stage-1`, 'DELETE', undefined]);
  });

  it.each([
    ['activate', 'activate'],
    ['pause', 'pause'],
    ['cancel', 'cancel'],
  ] as const)('%s: POST /api/v1/rollout-schedules/{schedule_id}/%s with no body', async (method, verb) => {
    await RolloutSchedulesService[method]('sched-1');
    expect(sent()).toEqual([`${BASE}/api/v1/rollout-schedules/sched-1/${verb}`, 'POST', undefined]);
  });

  it('advance: POST /api/v1/rollout-schedules/stages/{stage_id}/advance with no body', async () => {
    await RolloutSchedulesService.advance('stage-1');
    expect(sent()).toEqual([`${BASE}/api/v1/rollout-schedules/stages/stage-1/advance`, 'POST', undefined]);
  });
});
