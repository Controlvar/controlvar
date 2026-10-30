/**
 * useExperimentEligibility (#1449): only the answer for the current group id
 * is shown, an answer that arrives after unmount sets nothing, and the
 * answers are judged by their shape.
 */
import { act, renderHook } from '@testing-library/react';
import { apiFetch } from '@/services/api';
import { groupAnswer, holdoutAnswer, useExperimentEligibility } from '@/hooks/useExperimentEligibility';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const ADMIN = { role: 'ADMIN', is_superuser: false };

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

  it('an answer that arrives after unmount sets nothing', async () => {
    const late = deferred<unknown>();
    mockedApiFetch.mockImplementation((() => late.promise) as unknown as typeof apiFetch);
    const errors = jest.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const { unmount } = renderHook(() => useExperimentEligibility('grp-1', ADMIN));
      unmount();
      await act(async () => {
        late.resolve({ id: 'grp-1', name: 'Late', status: 'active' });
      });
      expect(errors).not.toHaveBeenCalled();
    } finally {
      errors.mockRestore();
    }
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
