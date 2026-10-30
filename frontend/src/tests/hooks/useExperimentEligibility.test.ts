/**
 * useExperimentEligibility (#1449): only the answer for the current group id
 * is shown, an answer to a read that was cancelled never is, and the answers
 * are judged by their shape.
 *
 * A cancelled read's answer is tested where it can be seen: a group id that
 * changed, and a role that stopped and then started admitting the user. After
 * unmount nothing can see it (React 19 no longer warns about a state update
 * on an unmounted component), so no test claims that case.
 */
import { act, renderHook } from '@testing-library/react';
import { apiFetch } from '@/services/api';
import {
  EligibilityUser,
  groupAnswer,
  holdoutAnswer,
  useExperimentEligibility,
} from '@/hooks/useExperimentEligibility';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const ADMIN: EligibilityUser = { role: 'ADMIN', is_superuser: false };
const VIEWER: EligibilityUser = { role: 'VIEWER', is_superuser: false };

/** A promise the test settles by hand. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe('the group read', () => {
  it('shows the answer for the current id, never a late answer for an earlier one', async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    mockedApiFetch.mockImplementation(((path: string) => {
      if (path === '/api/v1/mutual-exclusion-groups/grp-1') return first.promise;
      if (path === '/api/v1/mutual-exclusion-groups/grp-2') return second.promise;
      return Promise.resolve(null);
    }) as unknown as typeof apiFetch);

    const { result, rerender } = renderHook(({ id }) => useExperimentEligibility(id, ADMIN), {
      initialProps: { id: 'grp-1' as string | null },
    });
    expect(result.current.group).toEqual({ kind: 'loading' });

    rerender({ id: 'grp-2' });
    await act(async () => {
      second.resolve({ id: 'grp-2', name: 'Second', status: 'active' });
    });
    expect(result.current.group).toEqual({
      kind: 'present',
      value: { id: 'grp-2', name: 'Second', status: 'active' },
    });

    // grp-1's answer arrives last: it was cancelled when the id changed.
    await act(async () => {
      first.resolve({ id: 'grp-1', name: 'First', status: 'active' });
    });
    expect(result.current.group).toEqual({
      kind: 'present',
      value: { id: 'grp-2', name: 'Second', status: 'active' },
    });
  });

  it("shows loading for a new id, never the previous id's answer, until the new one comes", async () => {
    const second = deferred<unknown>();
    mockedApiFetch.mockImplementation(((path: string) => {
      if (path === '/api/v1/mutual-exclusion-groups/grp-1') {
        return Promise.resolve({ id: 'grp-1', name: 'First', status: 'active' });
      }
      if (path === '/api/v1/mutual-exclusion-groups/grp-2') return second.promise;
      return Promise.resolve(null);
    }) as unknown as typeof apiFetch);

    const { result, rerender } = renderHook(({ id }) => useExperimentEligibility(id, ADMIN), {
      initialProps: { id: 'grp-1' as string | null },
    });
    await act(async () => undefined);
    expect(result.current.group).toEqual({
      kind: 'present',
      value: { id: 'grp-1', name: 'First', status: 'active' },
    });

    // grp-1 has answered; the experiment is now in grp-2, which has not.
    rerender({ id: 'grp-2' });
    expect(result.current.group).toEqual({ kind: 'loading' });
    await act(async () => undefined);
    expect(result.current.group).toEqual({ kind: 'loading' });

    await act(async () => {
      second.resolve({ id: 'grp-2', name: 'Second', status: 'archived' });
    });
    expect(result.current.group).toEqual({
      kind: 'present',
      value: { id: 'grp-2', name: 'Second', status: 'archived' },
    });
  });
});

describe('the holdout read', () => {
  it('never shows the answer to a read that a change of role cancelled', async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    let reads = 0;
    mockedApiFetch.mockImplementation(((path: string) => {
      if (path !== '/api/v1/holdout') return Promise.resolve(null);
      reads += 1;
      return reads === 1 ? first.promise : second.promise;
    }) as unknown as typeof apiFetch);

    const { result, rerender } = renderHook(({ user }) => useExperimentEligibility(null, user), {
      initialProps: { user: ADMIN },
    });
    // The role stops admitting the user (the first read is cancelled), then
    // admits them again (a second read).
    rerender({ user: VIEWER });
    expect(result.current.holdout).toEqual({ kind: 'refused' });
    rerender({ user: ADMIN });
    expect(reads).toBe(2);

    await act(async () => {
      second.resolve(null);
    });
    expect(result.current.holdout).toEqual({ kind: 'absent' });

    // The first read answers last, with an active holdout: it must not show.
    await act(async () => {
      first.resolve({ id: 'h-1', name: 'Stale', holdout_percentage: 5, is_active: true });
    });
    expect(result.current.holdout).toEqual({ kind: 'absent' });
  });
});

describe('judging an answer by its shape', () => {
  it.each([
    [{ id: 'g', name: 'G', status: 'active' }, 'present'],
    [{ id: 'g', name: 'G', status: 'archived' }, 'present'],
    [{ id: 'g', status: 'active' }, 'error'],
    [{ id: 'g', name: 'G' }, 'error'],
    [null, 'error'],
    [undefined, 'error'],
    ['G', 'error'],
  ])('a group answer %p is %s', (body, kind) => {
    expect(groupAnswer(body).kind).toBe(kind);
  });

  it.each([
    [null, 'absent'],
    [{ id: 'h', name: 'H', holdout_percentage: 5, is_active: true }, 'present'],
    [{ id: 'h', name: 'H', holdout_percentage: 5, is_active: false }, 'error'],
    [{ id: 'h', name: 'H', percentage: 5, is_active: true }, 'error'],
    [{ id: 'h', name: 'H', holdout_percentage: Number.NaN, is_active: true }, 'error'],
    [undefined, 'error'],
    [[], 'error'],
  ])('a holdout answer %p is %s', (body, kind) => {
    expect(holdoutAnswer(body).kind).toBe(kind);
  });
});
